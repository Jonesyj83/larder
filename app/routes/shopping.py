import logging
from typing import Optional

from .. import pricing

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import stock as st
from ..auth import current_user
from ..db import begin, get_db, row, rows
from .stock import MAX_AMOUNT, DatedInput

router = APIRouter(prefix="/api/shopping", tags=["shopping list"])


class ItemIn(BaseModel):
    product_id: Optional[int] = None
    name: str = ""  # for things that aren't a product yet
    amount: float = Field(default=1, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    unit_id: Optional[int] = None
    note: str = ""


class ItemEdit(BaseModel):
    amount: Optional[float] = Field(default=None, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    unit_id: Optional[int] = None
    note: Optional[str] = None
    done: Optional[bool] = None
    name: Optional[str] = None


class BoughtIn(DatedInput):
    location_id: Optional[int] = None
    expires_on: Optional[str] = None


ITEM_SQL = """
SELECT i.*, COALESCE(p.name, i.name) AS label, u.name AS unit, p.picture,
       p.unit_id AS stock_unit_id, p.purchase_unit_id
FROM shopping_list i
LEFT JOIN products p ON p.id = i.product_id
LEFT JOIN units u ON u.id = i.unit_id
"""


def add_item(db, product_id, amount, unit_id=None, name="", note="", user_id=None, source="manual", source_ref="", source_label="", list_id=None):
    """Adds to the list, or bumps the amount if the product is already on it."""
    list_id = resolve_list(db, list_id)
    if product_id:
        p = row(db.execute("SELECT unit_id, purchase_unit_id FROM products WHERE id = ?", (product_id,)))
        if not p:
            raise HTTPException(404, "Product not found")
        unit_id = unit_id or p['purchase_unit_id'] or p['unit_id']
        existing = row(db.execute(
            "SELECT id FROM shopping_list WHERE product_id = ? AND unit_id = ? AND done = 0 AND list_id = ?", (product_id,unit_id,list_id)))
        if existing:
            db.execute("UPDATE shopping_list SET amount = amount + ? WHERE id = ?", (amount, existing["id"]))
            record_source(db, existing["id"], source, source_ref, source_label, amount, user_id)
            return existing["id"]
    elif not name.strip():
        raise HTTPException(400, "Need a product or a name")
    cur = db.execute(
        "INSERT INTO shopping_list (product_id, name, amount, unit_id, note, added_by, list_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (product_id, name.strip(), amount, unit_id, note, user_id, list_id))
    record_source(db, cur.lastrowid, source, source_ref, source_label, amount, user_id)
    return cur.lastrowid


def record_source(db, item_id, kind, ref, label, amount, user_id):
    db.execute('INSERT INTO shopping_sources(item_id,kind,source_ref,label,amount,added_by) VALUES(?,?,?,?,?,?)',
               (item_id,kind,str(ref),label,amount,user_id))


def listed_amount(db, product_id, list_id=None):
    """How much of a product is already on the list, in its stock unit. A pack
    on the list counts towards pieces the plan needs, instead of being ignored."""
    list_id = resolve_list(db, list_id)
    p = st.get_product(db, product_id)
    total = 0.0
    for item in rows(db.execute("SELECT amount, unit_id FROM shopping_list WHERE product_id = ? AND done = 0 AND list_id = ?",
                                (product_id,list_id))):
        try:
            total += st.to_stock_units(db, p, item["amount"], item["unit_id"])
        except st.StockError:
            pass  # a unit we can't convert doesn't count either way
    return total


def _check_unit(db, unit_id):
    if unit_id is not None and not db.execute("SELECT 1 FROM units WHERE id = ?", (unit_id,)).fetchone():
        raise HTTPException(400, "No such unit")


def resolve_list(db, list_id=None):
    if list_id is None:
        found = db.execute("SELECT id FROM shopping_lists WHERE kind='order' ORDER BY id LIMIT 1").fetchone()
    else:
        found = db.execute("SELECT id FROM shopping_lists WHERE id=?", (list_id,)).fetchone()
    if not found:
        raise HTTPException(404, "Shopping list not found")
    return found["id"]


class ListIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


def list_name(data):
    name = data.name.strip()
    if not name:
        raise HTTPException(400, "Need a list name")
    return name


@router.get("/lists")
def lists(db=Depends(get_db), user=Depends(current_user), kind: Optional[str] = None):
    return rows(db.execute("""SELECT l.*, (SELECT COUNT(*) FROM shopping_list i
        WHERE i.list_id=l.id AND i.done=0) AS item_count FROM shopping_lists l WHERE (? IS NULL OR l.kind=?) ORDER BY l.id""", (kind, kind)))


@router.post("/lists", status_code=201)
def create_list(data: ListIn, db=Depends(get_db), user=Depends(current_user)):
    cur = db.execute("INSERT INTO shopping_lists(name,created_by) VALUES (?,?)", (list_name(data), user["id"]))
    db.commit()
    return row(db.execute("SELECT * FROM shopping_lists WHERE id=?", (cur.lastrowid,)))


@router.put("/lists/{list_id}")
def rename_list(list_id: int, data: ListIn, db=Depends(get_db), user=Depends(current_user)):
    resolve_list(db, list_id)
    if db.execute("SELECT kind FROM shopping_lists WHERE id=?", (list_id,)).fetchone()['kind'] == 'order':
        raise HTTPException(409, "The Order list cannot be renamed")
    db.execute("UPDATE shopping_lists SET name=? WHERE id=?", (list_name(data), list_id))
    db.commit()
    return row(db.execute("SELECT * FROM shopping_lists WHERE id=?", (list_id,)))


@router.delete("/lists/{list_id}")
def delete_list(list_id: int, db=Depends(get_db), user=Depends(current_user)):
    begin(db)
    resolve_list(db, list_id)
    if db.execute("SELECT kind FROM shopping_lists WHERE id=?", (list_id,)).fetchone()['kind'] == 'order':
        raise HTTPException(409, "The Order list cannot be deleted")
    target = db.execute("SELECT id FROM shopping_lists WHERE id!=? AND kind='plain' ORDER BY id LIMIT 1", (list_id,)).fetchone()
    if not target:
        raise HTTPException(409, "Cannot delete the last shopping list")
    db.execute("UPDATE shopping_list SET list_id=? WHERE list_id=?", (target["id"], list_id))
    db.execute("DELETE FROM shopping_lists WHERE id=?", (list_id,))
    db.commit()
    return {"ok": True}


class MoveIn(BaseModel):
    list_id: Optional[int] = None
    name: Optional[str] = Field(default=None, max_length=200)


@router.post("/{item_id}/move")
def move(item_id: int, data: MoveIn, db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    begin(db)
    item = db.execute("SELECT id FROM shopping_list WHERE id=? AND list_id=?",
                      (item_id, resolve_list(db, list_id))).fetchone()
    if not item:
        raise HTTPException(404, "Not on the list")
    if (data.list_id is None) == (data.name is None):
        raise HTTPException(400, "Choose a list or a new list name")
    if data.list_id is not None:
        target = db.execute("SELECT id FROM shopping_lists WHERE id=? AND kind='plain'", (data.list_id,)).fetchone()
        if not target:
            raise HTTPException(404, "Shopping list not found")
        target_id = target['id']
    else:
        name = data.name.strip()
        if not name:
            raise HTTPException(400, "Need a list name")
        target = db.execute("SELECT id FROM shopping_lists WHERE kind='plain' AND name=? COLLATE NOCASE ORDER BY id LIMIT 1", (name,)).fetchone()
        target_id = target['id'] if target else db.execute(
            "INSERT INTO shopping_lists(name,created_by) VALUES(?,?)", (name, user['id'])).lastrowid
    db.execute("UPDATE shopping_list SET list_id=? WHERE id=?", (target_id, item_id))
    db.commit()
    return row(db.execute(ITEM_SQL + " WHERE i.id=?", (item_id,)))


@router.get("")
def list_items(db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    items = rows(db.execute(ITEM_SQL + " WHERE i.list_id = ? ORDER BY i.done, label COLLATE NOCASE", (resolve_list(db, list_id),)))
    for item in items:
        item['stock_covers_need'] = False
        if item['product_id']:
            product = st.get_product(db, item['product_id'])
            try:
                needed = st.to_stock_units(db, product, item['amount'], item['unit_id'])
                item['stock_covers_need'] = st.in_stock(db, item['product_id']) + 1e-9 >= needed
            except st.StockError:
                pass
        item['sources'] = rows(db.execute('SELECT kind,source_ref,label,amount,created_at FROM shopping_sources WHERE item_id=? ORDER BY id', (item['id'],)))
        recorded = sum(s['amount'] for s in item['sources'])
        item['origin_unknown'] = not item['sources'] or abs(recorded - item['amount']) > 1e-9
    return items


@router.post("", status_code=201)
def add(data: ItemIn, db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    _check_unit(db, data.unit_id)
    begin(db)
    item_id = add_item(db, data.product_id, data.amount, data.unit_id, data.name, data.note, user["id"], list_id=list_id)
    db.commit()
    if data.product_id and db.execute("SELECT kind FROM shopping_lists WHERE id=?", (resolve_list(db, list_id),)).fetchone()['kind'] == 'order':
        try:
            missing = db.execute("""SELECT 1 FROM store_settings s WHERE enabled=1 AND NOT EXISTS (
                SELECT 1 FROM product_store_links l WHERE l.product_id=? AND l.store=s.store)""",
                (data.product_id,)).fetchone()
            if missing:
                product = db.execute("SELECT name FROM products WHERE id=?", (data.product_id,)).fetchone()
                pricing.auto_match(db, data.product_id, product["name"])
        except Exception:
            db.rollback()
            logging.getLogger(__name__).warning("Shopping item pricing failed", exc_info=True)
    return row(db.execute(ITEM_SQL + " WHERE i.id = ?", (item_id,)))


@router.post("/add-missing")
def add_missing(db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    """Put everything that's below its minimum stock on the list."""
    begin(db)
    list_id = resolve_list(db, list_id)
    missing = rows(db.execute("""
        SELECT p.id, p.unit_id, p.min_stock - COALESCE((SELECT SUM(amount) FROM stock s WHERE s.product_id = p.id), 0) AS need
        FROM products p WHERE p.min_stock > 0"""))
    added = 0
    for m in missing:
        if m["need"] > 0:
            on_list = db.execute("SELECT 1 FROM shopping_list WHERE product_id = ? AND done = 0 AND list_id = ?", (m["id"],list_id)).fetchone()
            if not on_list:
                add_item(db, m["id"], m["need"], m["unit_id"], user_id=user["id"], source="low_stock", source_label="Low stock", list_id=list_id)
                added += 1
    db.commit()
    return {"added": added}


@router.put("/{item_id}")
def edit(item_id: int, data: ItemEdit, db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    begin(db)
    item = row(db.execute("SELECT * FROM shopping_list WHERE id = ? AND list_id = ?", (item_id, resolve_list(db, list_id))))
    if not item:
        raise HTTPException(404, "Not on the list")
    _check_unit(db, data.unit_id)
    if data.name is not None and not item["product_id"] and not data.name.strip():
        raise HTTPException(400, "Need a name")
    for field in ("amount", "unit_id", "note", "name"):
        val = getattr(data, field)
        if val is not None:
            item[field] = val.strip() if field == "name" else val
    if data.done is not None:
        item["done"] = int(data.done)
    db.execute("UPDATE shopping_list SET amount = ?, unit_id = ?, note = ?, name = ?, done = ? WHERE id = ?",
               (item["amount"], item["unit_id"], item["note"], item["name"], item["done"], item_id))
    db.commit()
    return row(db.execute(ITEM_SQL + " WHERE i.id = ?", (item_id,)))


class MakeProductIn(BaseModel):
    pass


@router.post("/{item_id}/make-product")
def make_product(item_id: int, data: MakeProductIn | None = None, db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    """Turn a freeform shopping entry into a product and add the purchase to stock."""
    begin(db)
    item = row(db.execute("SELECT * FROM shopping_list WHERE id=? AND list_id=?", (item_id,resolve_list(db, list_id))))
    if not item:
        db.rollback()
        raise HTTPException(404, "Not on the list")
    if item["product_id"]:
        db.rollback()
        raise HTTPException(409, "This item is already linked to a product")
    name = item["name"].strip()
    product = row(db.execute("SELECT id FROM products WHERE name=? COLLATE NOCASE", (name,)))
    if product:
        product_id = product["id"]
        existing = st.get_product(db, product_id)
        unit_id = existing['purchase_unit_id'] or existing['unit_id']
    else:
        default_unit = db.execute("""SELECT id FROM units
            WHERE lower(name) IN ('each','piece','pieces','item','items','unit','count')
            ORDER BY CASE lower(name) WHEN 'each' THEN 0 WHEN 'piece' THEN 1 ELSE 2 END, id
            LIMIT 1""").fetchone()
        if default_unit:
            unit_id = default_unit['id']
        else:
            unit_id = db.execute("INSERT INTO units(name) VALUES('each')").lastrowid
        cur = db.execute("INSERT INTO products(name,unit_id) VALUES(?,?)", (name, unit_id))
        product_id = cur.lastrowid
    try:
        stock = st.purchase(db, product_id, item["amount"], unit_id, user_id=user["id"], commit=False)
    except st.StockError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.execute("DELETE FROM shopping_list WHERE id=?", (item_id,))
    db.commit()
    from .products import load_product
    return {"product": load_product(db, product_id), "stock": stock}


@router.post("/{item_id}/bought")
def bought(item_id: int, data: BoughtIn | None = None, db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    """Tick it off and put it straight into stock."""
    # one transaction, so pressing "Got it" in two tabs can't add the stock twice
    begin(db)
    item = row(db.execute("SELECT * FROM shopping_list WHERE id = ? AND list_id = ?", (item_id,resolve_list(db, list_id))))
    if not item:
        db.rollback()
        raise HTTPException(404, "Not on the list")
    result = None
    if item["product_id"]:
        data = data or BoughtIn()
        try:
            result = st.purchase(db, item["product_id"], item["amount"], item["unit_id"],
                                 data.location_id, data.expires_on, None, user["id"], commit=False)
        except st.StockError as e:
            db.rollback()
            raise HTTPException(400, str(e))
    db.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))
    db.commit()
    return {"ok": True, "stock": result}


@router.delete("/done")
def clear_done(db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    db.execute("DELETE FROM shopping_list WHERE done = 1 AND list_id = ?", (resolve_list(db, list_id),))
    db.commit()
    return {"ok": True}


@router.delete("/{item_id}")
def remove(item_id: int, db=Depends(get_db), user=Depends(current_user), list_id: Optional[int] = None):
    db.execute("DELETE FROM shopping_list WHERE id = ? AND list_id = ?", (item_id,resolve_list(db, list_id)))
    db.commit()
    return {"ok": True}
