import sqlite3
from typing import Literal, Optional

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import begin, get_db, row, rows
from ..ingredients import MAX_AMOUNT
from ..pictures import MAX_SIZE, fetch_picture, remove_picture, save_picture

router = APIRouter(prefix="/api", tags=["products"], dependencies=[Depends(current_user)])


class ProductIn(BaseModel):
    name: str = Field(max_length=200)
    location_id: Optional[int] = None
    unit_id: int
    purchase_unit_id: Optional[int] = None
    purchase_factor: float = Field(default=1, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    shelf_life_kind: Literal["best_by", "use_by"] = "best_by"
    default_days: Optional[int] = Field(default=None, ge=0, le=36500)
    min_stock: float = Field(default=0, ge=0, le=MAX_AMOUNT, allow_inf_nan=False)
    notes: str = ""
    barcodes: list[str] = []
    picture_url: Optional[str] = None  # fetched and saved locally on create


class BarcodeIn(BaseModel):
    code: str = Field(max_length=500)
    amount: Optional[float] = Field(default=None, gt=0, le=MAX_AMOUNT, allow_inf_nan=False)


class PictureUrlIn(BaseModel):
    url: str


PRODUCT_SQL = """
SELECT p.*, l.name AS location, u.name AS unit, u.name_plural AS unit_plural, pu.name AS purchase_unit,
       COALESCE((SELECT SUM(amount) FROM stock s WHERE s.product_id = p.id), 0) AS in_stock,
       (SELECT MIN(expires_on) FROM stock s WHERE s.product_id = p.id) AS next_expiry
FROM products p
LEFT JOIN locations l ON l.id = p.location_id
JOIN units u ON u.id = p.unit_id
LEFT JOIN units pu ON pu.id = p.purchase_unit_id
"""


def load_product(db, product_id):
    p = row(db.execute(PRODUCT_SQL + " WHERE p.id = ?", (product_id,)))
    if not p:
        raise HTTPException(404, "Product not found")
    p["barcodes"] = rows(db.execute("SELECT id, code, amount FROM barcodes WHERE product_id = ?", (product_id,)))
    p["stock"] = rows(db.execute("""
        SELECT s.*, l.name AS location FROM stock s LEFT JOIN locations l ON l.id = s.location_id
        WHERE s.product_id = ? ORDER BY s.expires_on IS NULL, s.expires_on, s.id""", (product_id,)))
    return p


def _check(data):
    if not data.name.strip():
        raise HTTPException(400, "Name can't be empty")
    if data.purchase_factor <= 0:
        raise HTTPException(400, "Purchase conversion has to be above zero")


def _add_barcode(db, product_id, code, amount=None):
    code = code.strip()
    if not code:
        return
    try:
        db.execute("INSERT INTO barcodes (product_id, code, amount) VALUES (?, ?, ?)", (product_id, code, amount))
    except sqlite3.IntegrityError:
        other = db.execute("SELECT p.name FROM barcodes b JOIN products p ON p.id = b.product_id WHERE b.code = ?",
                           (code,)).fetchone()
        raise HTTPException(400, f"Barcode {code} already belongs to {other[0] if other else 'another product'}")


@router.get("/products")
def list_products(q: str = "", location_id: Optional[int] = None, db=Depends(get_db)):
    sql = PRODUCT_SQL + " WHERE 1=1"
    args = []
    if q:
        sql += " AND p.name LIKE ?"
        args.append(f"%{q}%")
    if location_id:
        sql += " AND p.location_id = ?"
        args.append(location_id)
    sql += " ORDER BY p.name COLLATE NOCASE"
    return rows(db.execute(sql, args))


@router.get("/products/{product_id}")
def get_product(product_id: int, db=Depends(get_db)):
    return load_product(db, product_id)


@router.post("/products", status_code=201)
def add_product(data: ProductIn, db=Depends(get_db)):
    _check(data)
    picture = None
    if data.picture_url:
        try:
            picture = fetch_picture(data.picture_url)
        except (ValueError, httpx.HTTPError):
            picture = None  # not worth failing the whole thing over a picture
    try:
        cur = db.execute("""
            INSERT INTO products (name, location_id, unit_id, purchase_unit_id, purchase_factor,
                                  shelf_life_kind, default_days, min_stock, notes, picture)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.name.strip(), data.location_id, data.unit_id, data.purchase_unit_id, data.purchase_factor,
             data.shelf_life_kind, data.default_days, data.min_stock, data.notes, picture))
        # the same code twice in the list would otherwise clash with itself
        for code in dict.fromkeys(c.strip() for c in data.barcodes):
            _add_barcode(db, cur.lastrowid, code)
    except HTTPException:
        db.rollback()
        remove_picture(picture)
        raise
    except sqlite3.IntegrityError:
        db.rollback()
        remove_picture(picture)
        raise HTTPException(400, "Unknown unit or location")
    db.commit()
    return load_product(db, cur.lastrowid)


@router.put("/products/{product_id}")
def edit_product(product_id: int, data: ProductIn, db=Depends(get_db)):
    _check(data)
    begin(db)
    existing_product = load_product(db, product_id)
    if data.unit_id != existing_product["unit_id"]:
        used = any(db.execute(f"SELECT 1 FROM {table} WHERE product_id=? LIMIT 1", (product_id,)).fetchone()
                   for table in ("stock", "stock_log", "recipe_ingredients", "shopping_list"))
        if used:
            db.rollback()
            raise HTTPException(400, "Stock unit is already used by stock, recipes or shopping. Keep this unit or make a separate product.")
    try:
        db.execute("""
            UPDATE products SET name = ?, location_id = ?, unit_id = ?, purchase_unit_id = ?, purchase_factor = ?,
                   shelf_life_kind = ?, default_days = ?, min_stock = ?, notes = ?
            WHERE id = ?""",
            (data.name.strip(), data.location_id, data.unit_id, data.purchase_unit_id, data.purchase_factor,
             data.shelf_life_kind, data.default_days, data.min_stock, data.notes, product_id))
    except sqlite3.IntegrityError:
        db.rollback()
        raise HTTPException(400, "Unknown unit or location")
    # barcodes list on edit replaces the existing set, but keeps per-barcode amounts
    existing = {b["code"] for b in rows(db.execute("SELECT code FROM barcodes WHERE product_id = ?", (product_id,)))}
    wanted = {c.strip() for c in data.barcodes if c.strip()}
    for code in existing - wanted:
        db.execute("DELETE FROM barcodes WHERE product_id = ? AND code = ?", (product_id, code))
    try:
        for code in wanted - existing:
            _add_barcode(db, product_id, code)
    except HTTPException:
        db.rollback()
        raise
    db.commit()
    return load_product(db, product_id)


@router.delete("/products/{product_id}")
def delete_product(product_id: int, db=Depends(get_db)):
    p = load_product(db, product_id)
    used = db.execute("SELECT COUNT(DISTINCT recipe_id) FROM recipe_ingredients WHERE product_id = ?",
                      (product_id,)).fetchone()[0]
    if used:
        raise HTTPException(400, f"Used in {used} recipe(s), take it out of those first")
    db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    db.commit()
    remove_picture(p["picture"])
    return {"ok": True}


@router.post("/products/{product_id}/barcodes", status_code=201)
def add_barcode(product_id: int, data: BarcodeIn, db=Depends(get_db)):
    load_product(db, product_id)
    if not data.code.strip():
        raise HTTPException(400, "Barcode can't be empty")
    _add_barcode(db, product_id, data.code, data.amount)
    db.commit()
    return load_product(db, product_id)


@router.delete("/products/{product_id}/barcodes/{barcode_id}")
def delete_barcode(product_id: int, barcode_id: int, db=Depends(get_db)):
    db.execute("DELETE FROM barcodes WHERE id = ? AND product_id = ?", (barcode_id, product_id))
    db.commit()
    return {"ok": True}


def _set_picture(db, product_id, name):
    p = load_product(db, product_id)
    db.execute("UPDATE products SET picture = ? WHERE id = ?", (name, product_id))
    db.commit()
    remove_picture(p["picture"])
    return load_product(db, product_id)


@router.post("/products/{product_id}/picture")
async def upload_picture(product_id: int, file: UploadFile = File(...), db=Depends(get_db)):
    load_product(db, product_id)
    # read one byte past the limit rather than the whole upload, however big it is
    data = await file.read(MAX_SIZE + 1)
    try:
        name = save_picture(data, file.content_type)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _set_picture(db, product_id, name)


@router.post("/products/{product_id}/picture-url")
def picture_from_url(product_id: int, data: PictureUrlIn, db=Depends(get_db)):
    load_product(db, product_id)
    try:
        name = fetch_picture(data.url)
    except ValueError as e:
        raise HTTPException(400, f"Couldn't fetch that picture: {e}")
    except httpx.HTTPError:
        raise HTTPException(400, "Couldn't fetch that picture")
    return _set_picture(db, product_id, name)


@router.delete("/products/{product_id}/picture")
def delete_picture(product_id: int, db=Depends(get_db)):
    return _set_picture(db, product_id, None)
