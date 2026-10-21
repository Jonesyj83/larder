from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import get_db, row, rows

router = APIRouter(prefix="/api/receipts", tags=["receipts"])


def canonical_date(value):
    if value in (None, ""):
        return None
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except (TypeError, ValueError):
        raise HTTPException(400, "Enter a valid ISO date (YYYY-MM-DD)")
    return value


class ReceiptIn(BaseModel):
    store: str = ""
    purchased_on: Optional[str] = None
    total: Optional[float] = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    note: str = ""


@router.get("")
def list_receipts(limit: int = 50, db=Depends(get_db), user=Depends(current_user)):
    return rows(db.execute(
        "SELECT * FROM receipts ORDER BY purchased_on DESC, id DESC LIMIT ?",
        (max(1, min(limit, 500)),)))


@router.get("/{receipt_id}")
def get_receipt(receipt_id: int, db=Depends(get_db), user=Depends(current_user)):
    r = row(db.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)))
    if not r:
        raise HTTPException(404, "No such receipt")
    items = rows(db.execute("""SELECT g.*, p.name FROM stock_log g
        JOIN products p ON p.id = g.product_id WHERE g.receipt_id = ? ORDER BY g.id""",
        (receipt_id,)))
    r = dict(r)
    r["items"] = items
    r["items_total"] = sum(i["price"] or 0 for i in items)
    return r


@router.post("")
def create_receipt(data: ReceiptIn, db=Depends(get_db), user=Depends(current_user)):
    purchased_on = canonical_date(data.purchased_on) or date.today().isoformat()
    cur = db.execute(
        "INSERT INTO receipts (store, purchased_on, total, note, added_by) VALUES (?, ?, ?, ?, ?)",
        (data.store.strip(), purchased_on, data.total, data.note.strip(), user["id"]))
    db.commit()
    return row(db.execute("SELECT * FROM receipts WHERE id = ?", (cur.lastrowid,)))
