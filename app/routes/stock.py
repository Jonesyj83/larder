from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from .. import stock as st
from ..auth import current_user
from ..db import begin, get_db, row, rows
from ..ingredients import MAX_AMOUNT

router = APIRouter(prefix="/api/stock", tags=["stock"])


def canonical_date(value):
    if value in (None, ""):
        return None
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError()
    except (TypeError, ValueError):
        raise ValueError("Enter a valid ISO date (YYYY-MM-DD)")
    return value


class DatedInput(BaseModel):
    expires_on: Optional[str] = None

    @field_validator("expires_on")
    @classmethod
    def validate_date(cls, value):
        return canonical_date(value)


class PurchaseIn(DatedInput):
    product_id: int
    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    unit_id: Optional[int] = None  # defaults to the product's stock unit
    location_id: Optional[int] = None
    expires_on: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0, le=MAX_AMOUNT, allow_inf_nan=False)


class ConsumeIn(BaseModel):
    product_id: int
    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    unit_id: Optional[int] = None
    location_id: Optional[int] = None
    spoiled: bool = False


class EntryEdit(DatedInput):
    amount: float = Field(ge=0, le=MAX_AMOUNT, allow_inf_nan=False)  # 0 removes the entry
    location_id: Optional[int] = None
    expires_on: Optional[str] = None
    opened: bool = False


@router.get("")
def overview(location_id: Optional[int] = None, db=Depends(get_db), user=Depends(current_user)):
    """One row per product that has something in stock."""
    sql = """
        SELECT p.id AS product_id, p.name, p.picture, p.shelf_life_kind, p.min_stock,
               u.name AS unit, u.name_plural AS unit_plural, p.unit_id,
               SUM(s.amount) AS amount, MIN(s.expires_on) AS next_expiry,
               GROUP_CONCAT(DISTINCT l.name) AS locations
        FROM stock s
        JOIN products p ON p.id = s.product_id
        JOIN units u ON u.id = p.unit_id
        LEFT JOIN locations l ON l.id = s.location_id
    """
    args = []
    if location_id:
        sql += " WHERE s.location_id = ?"
        args.append(location_id)
    sql += " GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
    return rows(db.execute(sql, args))


@router.get("/expiring")
def expiring(days: int = 3, db=Depends(get_db), user=Depends(current_user)):
    days = max(-3650, min(days, 3650))
    cutoff = (date.today() + timedelta(days=days)).isoformat()
    return rows(db.execute("""
        SELECT s.*, p.name, p.picture, p.shelf_life_kind, u.name AS unit, u.name_plural AS unit_plural, l.name AS location
        FROM stock s JOIN products p ON p.id = s.product_id JOIN units u ON u.id = p.unit_id
        LEFT JOIN locations l ON l.id = s.location_id
        WHERE s.expires_on IS NOT NULL AND s.expires_on <= ?
        ORDER BY s.expires_on""", (cutoff,)))


@router.get("/missing")
def below_minimum(db=Depends(get_db), user=Depends(current_user)):
    return rows(db.execute("""
        SELECT p.id AS product_id, p.name, p.min_stock, u.name AS unit,
               COALESCE((SELECT SUM(amount) FROM stock s WHERE s.product_id = p.id), 0) AS amount
        FROM products p JOIN units u ON u.id = p.unit_id
        WHERE p.min_stock > 0 AND
              COALESCE((SELECT SUM(amount) FROM stock s WHERE s.product_id = p.id), 0) < p.min_stock
        ORDER BY p.name"""))


@router.get("/log")
def log(limit: int = 50, product_id: Optional[int] = None, db=Depends(get_db), user=Depends(current_user)):
    sql = """SELECT g.*, p.name, u.name AS unit, us.username FROM stock_log g
             JOIN products p ON p.id = g.product_id JOIN units u ON u.id = p.unit_id
             LEFT JOIN users us ON us.id = g.user_id"""
    args = []
    if product_id:
        sql += " WHERE g.product_id = ?"
        args.append(product_id)
    sql += " ORDER BY g.id DESC LIMIT ?"
    # sqlite treats a negative LIMIT as no limit at all
    args.append(max(1, min(limit, 500)))
    return rows(db.execute(sql, args))


@router.post("/purchase")
def purchase(data: PurchaseIn, db=Depends(get_db), user=Depends(current_user)):
    try:
        return st.purchase(db, data.product_id, data.amount, data.unit_id, data.location_id,
                           data.expires_on, data.price, user["id"])
    except st.StockError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.post("/consume")
def consume(data: ConsumeIn, db=Depends(get_db), user=Depends(current_user)):
    try:
        return st.consume(db, data.product_id, data.amount, data.unit_id, data.spoiled,
                          data.location_id, user["id"])
    except st.StockError as e:
        db.rollback()
        raise HTTPException(400, str(e))


@router.put("/entries/{entry_id}")
def edit_entry(entry_id: int, data: EntryEdit, db=Depends(get_db), user=Depends(current_user)):
    begin(db)
    e = row(db.execute("SELECT * FROM stock WHERE id = ?", (entry_id,)))
    if not e:
        db.rollback()
        raise HTTPException(404, "Stock entry not found")
    if data.location_id and not db.execute("SELECT 1 FROM locations WHERE id = ?", (data.location_id,)).fetchone():
        db.rollback()
        raise HTTPException(400, "No such location")
    if data.amount <= 0:
        db.execute("DELETE FROM stock WHERE id = ?", (entry_id,))
    else:
        db.execute("UPDATE stock SET amount = ?, location_id = ?, expires_on = ?, opened = ? WHERE id = ?",
                   (data.amount, data.location_id, data.expires_on or None, int(data.opened), entry_id))
    if data.amount != e["amount"]:
        db.execute("INSERT INTO stock_log (product_id, user_id, kind, amount, location_id) VALUES (?, ?, 'adjust', ?, ?)",
                   (e["product_id"], user["id"], max(data.amount, 0) - e["amount"], data.location_id))
    db.commit()
    return {"ok": True}
