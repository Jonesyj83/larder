from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import stock as st
from ..auth import current_user
from ..db import get_db, row, rows
from .stock import DatedInput

router = APIRouter(prefix="/api/shopping", tags=["shopping list"])


class ItemIn(BaseModel):
    product_id: Optional[int] = None
    name: str = ""  # for things that aren't a product yet
    amount: float = 1
    unit_id: Optional[int] = None
    note: str = ""


class ItemEdit(BaseModel):
    amount: Optional[float] = None
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


def add_item(db, product_id, amount, unit_id=None, name="", note="", user_id=None, source="manual", source_ref="", source_label=""):
    """Adds to the list, or bumps the amount if the product is already on it."""
    if product_id:
        p = row(db.execute("SELECT unit_id, purchase_unit_id FROM products WHERE id = ?", (product_id,)))
        if not p:
            raise HTTPException(404, "Product not found")
        unit_id = unit_id or p['unit_id']
        existing = row(db.execute(
            "SELECT id FROM shopping_list WHERE product_id = ? AND unit_id = ? AND done = 0", (product_id,unit_id)))
        if existing:
            db.execute("UPDATE shopping_list SET amount = amount + ? WHERE id = ?", (amount, existing["id"]))
            record_source(db, existing["id"], source, source_ref, source_label, amount, user_id)
            return existing["id"]
    elif not name.strip():
        raise HTTPException(400, "Need a product or a name")
    cur = db.execute(
        "INSERT INTO shopping_list (product_id, name, amount, unit_id, note, added_by) VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, name.strip(), amount, unit_id, note, user_id))
    record_source(db, cur.lastrowid, source, source_ref, source_label, amount, user_id)
    return cur.lastrowid


def record_source(db, item_id, kind, ref, label, amount, user_id):
    db.execute('INSERT INTO shopping_sources(item_id,kind,source_ref,label,amount,added_by) VALUES(?,?,?,?,?,?)',
               (item_id,kind,str(ref),label,amount,user_id))


@router.get("")
def list_items(db=Depends(get_db), user=Depends(current_user)):
    items = rows(db.execute(ITEM_SQL + " ORDER BY i.done, label COLLATE NOCASE"))
    for item in items:
        item['sources'] = rows(db.execute('SELECT kind,source_ref,label,amount,created_at FROM shopping_sources WHERE item_id=? ORDER BY id', (item['id'],)))
        recorded = sum(s['amount'] for s in item['sources'])
        item['origin_unknown'] = not item['sources'] or abs(recorded - item['amount']) > 1e-9
    return items


@router.post("", status_code=201)
def add(data: ItemIn, db=Depends(get_db), user=Depends(current_user)):
    item_id = add_item(db, data.product_id, data.amount, data.unit_id, data.name, data.note, user["id"])
    db.commit()
    return row(db.execute(ITEM_SQL + " WHERE i.id = ?", (item_id,)))


@router.post("/add-missing")
def add_missing(db=Depends(get_db), user=Depends(current_user)):
    """Put everything that's below its minimum stock on the list."""
    missing = rows(db.execute("""
        SELECT p.id, p.unit_id, p.min_stock - COALESCE((SELECT SUM(amount) FROM stock s WHERE s.product_id = p.id), 0) AS need
        FROM products p WHERE p.min_stock > 0"""))
    added = 0
    for m in missing:
        if m["need"] > 0:
            on_list = db.execute("SELECT 1 FROM shopping_list WHERE product_id = ? AND done = 0", (m["id"],)).fetchone()
            if not on_list:
                add_item(db, m["id"], m["need"], m["unit_id"], user_id=user["id"], source="low_stock", source_label="Low stock")
                added += 1
    db.commit()
    return {"added": added}


@router.put("/{item_id}")
def edit(item_id: int, data: ItemEdit, db=Depends(get_db), user=Depends(current_user)):
    item = row(db.execute("SELECT * FROM shopping_list WHERE id = ?", (item_id,)))
    if not item:
        raise HTTPException(404, "Not on the list")
    for field in ("amount", "unit_id", "note", "name"):
        val = getattr(data, field)
        if val is not None:
            item[field] = val
    if data.done is not None:
        item["done"] = int(data.done)
    db.execute("UPDATE shopping_list SET amount = ?, unit_id = ?, note = ?, name = ?, done = ? WHERE id = ?",
               (item["amount"], item["unit_id"], item["note"], item["name"], item["done"], item_id))
    db.commit()
    return row(db.execute(ITEM_SQL + " WHERE i.id = ?", (item_id,)))


@router.post("/{item_id}/bought")
def bought(item_id: int, data: BoughtIn | None = None, db=Depends(get_db), user=Depends(current_user)):
    """Tick it off and put it straight into stock."""
    item = row(db.execute("SELECT * FROM shopping_list WHERE id = ?", (item_id,)))
    if not item:
        raise HTTPException(404, "Not on the list")
    result = None
    if item["product_id"]:
        data = data or BoughtIn()
        try:
            result = st.purchase(db, item["product_id"], item["amount"], item["unit_id"],
                                 data.location_id, data.expires_on, None, user["id"])
        except st.StockError as e:
            raise HTTPException(400, str(e))
    db.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))
    db.commit()
    return {"ok": True, "stock": result}


@router.delete("/done")
def clear_done(db=Depends(get_db), user=Depends(current_user)):
    db.execute("DELETE FROM shopping_list WHERE done = 1")
    db.commit()
    return {"ok": True}


@router.delete("/{item_id}")
def remove(item_id: int, db=Depends(get_db), user=Depends(current_user)):
    db.execute("DELETE FROM shopping_list WHERE id = ?", (item_id,))
    db.commit()
    return {"ok": True}
