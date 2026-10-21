import math
from datetime import date, timedelta

from .db import begin, row, rows


class StockError(Exception):
    pass


def get_product(db, product_id):
    p = row(db.execute("SELECT * FROM products WHERE id = ?", (product_id,)))
    if not p:
        raise StockError("No such product")
    return p


def to_stock_units(db, product, amount, unit_id):
    """Convert an amount in some unit into the product's stock unit."""
    if not unit_id or unit_id == product["unit_id"]:
        return amount
    if unit_id == product["purchase_unit_id"]:
        return amount * product["purchase_factor"]
    factor = conversion_factor(db, unit_id, product["unit_id"])
    if factor is not None:
        return amount * factor
    if product["purchase_unit_id"]:
        factor = conversion_factor(db, unit_id, product["purchase_unit_id"])
        if factor is not None:
            return amount * factor * product["purchase_factor"]
    u = db.execute("SELECT name FROM units WHERE id = ?", (unit_id,)).fetchone()
    raise StockError(f"Don't know how to convert {u[0] if u else 'that unit'} for {product['name']}")


def conversion_factor(db, source, target):
    """Walk reversible rules, so lb -> g -> kg works without count guesses."""
    direct = db.execute("SELECT factor FROM unit_conversions WHERE from_unit_id=? AND to_unit_id=?",
                        (source, target)).fetchone()
    if direct and direct[0] > 0:
        return direct[0]
    reverse = db.execute("SELECT factor FROM unit_conversions WHERE from_unit_id=? AND to_unit_id=?",
                         (target, source)).fetchone()
    if reverse and reverse[0] > 0:
        return 1 / reverse[0]
    graph = {}
    for r in db.execute("SELECT * FROM unit_conversions"):
        if r["factor"] > 0:
            graph.setdefault(r["from_unit_id"], []).append((r["to_unit_id"], r["factor"]))
            graph.setdefault(r["to_unit_id"], []).append((r["from_unit_id"], 1 / r["factor"]))
    queue, seen = [(source, 1.0)], {source}
    for unit, factor in queue:
        if unit == target:
            return factor
        for other, step in graph.get(unit, []):
            if other not in seen:
                seen.add(other)
                queue.append((other, factor * step))
    return None


def in_stock(db, product_id):
    return db.execute("SELECT COALESCE(SUM(amount), 0) FROM stock WHERE product_id = ?", (product_id,)).fetchone()[0]


def _check_amount(amount):
    if amount is None or not math.isfinite(amount) or amount <= 0:
        raise StockError("Amount has to be more than zero")


def purchase(db, product_id, amount, unit_id=None, location_id=None, expires_on=None,
             price=None, user_id=None, commit=True):
    begin(db)
    p = get_product(db, product_id)
    _check_amount(amount)
    if price is not None and (not math.isfinite(price) or price < 0):
        raise StockError("Price can't be negative")
    qty = to_stock_units(db, p, amount, unit_id)
    if not math.isfinite(qty) or qty <= 0:
        raise StockError("That amount is too big to store")
    location_id = location_id or p["location_id"]
    if location_id and not db.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
        raise StockError("No such location")
    if not expires_on and p["default_days"] is not None:
        try:
            expires_on = (date.today() + timedelta(days=p["default_days"])).isoformat()
        except OverflowError:
            expires_on = None
    cur = db.execute(
        "INSERT INTO stock (product_id, location_id, amount, expires_on, price) VALUES (?, ?, ?, ?, ?)",
        (product_id, location_id, qty, expires_on or None, price))
    db.execute("INSERT INTO stock_log (product_id, user_id, kind, amount, location_id, price) VALUES (?, ?, 'purchase', ?, ?, ?)",
               (product_id, user_id, qty, location_id, price))
    if commit:
        db.commit()
    return {"stock_id": cur.lastrowid, "added": qty, "in_stock": in_stock(db, product_id)}


def consume(db, product_id, amount, unit_id=None, spoiled=False, location_id=None, user_id=None,
            allow_partial=False, commit=True):
    # lock first: two tabs using the last of something must not both succeed
    begin(db)
    p = get_product(db, product_id)
    _check_amount(amount)
    qty = to_stock_units(db, p, amount, unit_id)

    sql = "SELECT id, amount FROM stock WHERE product_id = ?"
    args = [product_id]
    if location_id:
        sql += " AND location_id = ?"
        args.append(location_id)
    # oldest/soonest to go off first
    sql += " ORDER BY expires_on IS NULL, expires_on, purchased_on, id"
    entries = rows(db.execute(sql, args))
    available = sum(e["amount"] for e in entries)
    if qty > available + 1e-9:
        if not allow_partial or available <= 0:
            raise StockError(f"Only {fmt(available)} of {p['name']} in stock")
        qty = available

    left = qty
    for e in entries:
        if left <= 1e-9:
            break
        take = min(e["amount"], left)
        if e["amount"] - take <= 1e-9:
            db.execute("DELETE FROM stock WHERE id = ?", (e["id"],))
        else:
            db.execute("UPDATE stock SET amount = amount - ? WHERE id = ?", (take, e["id"]))
        left -= take

    db.execute("INSERT INTO stock_log (product_id, user_id, kind, amount, location_id) VALUES (?, ?, ?, ?, ?)",
               (product_id, user_id, "spoiled" if spoiled else "consume", -qty, location_id))
    if commit:
        db.commit()
    return {"removed": qty, "in_stock": in_stock(db, product_id)}


def fmt(n):
    return f"{round(n, 2):g}"
