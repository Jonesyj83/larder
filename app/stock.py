from datetime import date, timedelta

from .db import row, rows


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
    c = db.execute("SELECT factor FROM unit_conversions WHERE from_unit_id = ? AND to_unit_id = ?",
                   (unit_id, product["unit_id"])).fetchone()
    if c:
        return amount * c[0]
    c = db.execute("SELECT factor FROM unit_conversions WHERE from_unit_id = ? AND to_unit_id = ?",
                   (product["unit_id"], unit_id)).fetchone()
    if c and c[0]:
        return amount / c[0]
    # try going via the purchase unit, e.g. recipe asks for 1 kg, stock is in packs of 500 g
    if product["purchase_unit_id"]:
        c = db.execute("SELECT factor FROM unit_conversions WHERE from_unit_id = ? AND to_unit_id = ?",
                       (unit_id, product["purchase_unit_id"])).fetchone()
        if c:
            return amount * c[0] * product["purchase_factor"]
    u = db.execute("SELECT name FROM units WHERE id = ?", (unit_id,)).fetchone()
    raise StockError(f"Don't know how to convert {u[0] if u else 'that unit'} for {product['name']}")


def in_stock(db, product_id):
    return db.execute("SELECT COALESCE(SUM(amount), 0) FROM stock WHERE product_id = ?", (product_id,)).fetchone()[0]


def purchase(db, product_id, amount, unit_id=None, location_id=None, expires_on=None,
             price=None, user_id=None):
    p = get_product(db, product_id)
    if amount <= 0:
        raise StockError("Amount has to be more than zero")
    qty = to_stock_units(db, p, amount, unit_id)
    location_id = location_id or p["location_id"]
    if not expires_on and p["default_days"]:
        expires_on = (date.today() + timedelta(days=p["default_days"])).isoformat()
    cur = db.execute(
        "INSERT INTO stock (product_id, location_id, amount, expires_on, price) VALUES (?, ?, ?, ?, ?)",
        (product_id, location_id, qty, expires_on or None, price))
    db.execute("INSERT INTO stock_log (product_id, user_id, kind, amount, location_id) VALUES (?, ?, 'purchase', ?, ?)",
               (product_id, user_id, qty, location_id))
    db.commit()
    return {"stock_id": cur.lastrowid, "added": qty, "in_stock": in_stock(db, product_id)}


def consume(db, product_id, amount, unit_id=None, spoiled=False, location_id=None, user_id=None,
            allow_partial=False):
    p = get_product(db, product_id)
    if amount <= 0:
        raise StockError("Amount has to be more than zero")
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
    db.commit()
    return {"removed": qty, "in_stock": in_stock(db, product_id)}


def fmt(n):
    return f"{round(n, 2):g}"
