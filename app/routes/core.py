import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import current_user
from ..db import get_db, row, rows

router = APIRouter(prefix="/api", tags=["locations & units"], dependencies=[Depends(current_user)])


class LocationIn(BaseModel):
    name: str
    is_freezer: bool = False
    sort: int = 0


class UnitIn(BaseModel):
    name: str
    name_plural: str = ""


class ConversionIn(BaseModel):
    from_unit_id: int
    to_unit_id: int
    factor: float


def _name(s):
    s = (s or "").strip()
    if not s:
        raise HTTPException(400, "Name can't be empty")
    return s


@router.get("/locations")
def list_locations(db=Depends(get_db)):
    return rows(db.execute("""
        SELECT l.*, (SELECT COUNT(DISTINCT product_id) FROM stock s WHERE s.location_id = l.id) AS product_count
        FROM locations l ORDER BY sort, name"""))


@router.post("/locations", status_code=201)
def add_location(data: LocationIn, db=Depends(get_db)):
    try:
        cur = db.execute("INSERT INTO locations (name, is_freezer, sort) VALUES (?, ?, ?)",
                         (_name(data.name), int(data.is_freezer), data.sort))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "There's already a location called that")
    db.commit()
    return row(db.execute("SELECT * FROM locations WHERE id = ?", (cur.lastrowid,)))


@router.put("/locations/{loc_id}")
def edit_location(loc_id: int, data: LocationIn, db=Depends(get_db)):
    try:
        db.execute("UPDATE locations SET name = ?, is_freezer = ?, sort = ? WHERE id = ?",
                   (_name(data.name), int(data.is_freezer), data.sort, loc_id))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "There's already a location called that")
    db.commit()
    loc = row(db.execute("SELECT * FROM locations WHERE id = ?", (loc_id,)))
    if not loc:
        raise HTTPException(404, "Location not found")
    return loc


@router.delete("/locations/{loc_id}")
def delete_location(loc_id: int, db=Depends(get_db)):
    db.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
    db.commit()
    return {"ok": True}


@router.get("/units")
def list_units(db=Depends(get_db)):
    return rows(db.execute("SELECT * FROM units ORDER BY name"))


@router.post("/units", status_code=201)
def add_unit(data: UnitIn, db=Depends(get_db)):
    name = _name(data.name)
    try:
        cur = db.execute("INSERT INTO units (name, name_plural) VALUES (?, ?)",
                         (name, data.name_plural.strip() or name))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "That unit already exists")
    db.commit()
    return row(db.execute("SELECT * FROM units WHERE id = ?", (cur.lastrowid,)))


@router.put("/units/{unit_id}")
def edit_unit(unit_id: int, data: UnitIn, db=Depends(get_db)):
    name = _name(data.name)
    try:
        db.execute("UPDATE units SET name = ?, name_plural = ? WHERE id = ?",
                   (name, data.name_plural.strip() or name, unit_id))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "That unit already exists")
    db.commit()
    return row(db.execute("SELECT * FROM units WHERE id = ?", (unit_id,)))


@router.delete("/units/{unit_id}")
def delete_unit(unit_id: int, db=Depends(get_db)):
    used = db.execute("SELECT COUNT(*) FROM products WHERE unit_id = ? OR purchase_unit_id = ?",
                      (unit_id, unit_id)).fetchone()[0]
    if used:
        raise HTTPException(400, f"Still used by {used} product(s)")
    try:
        db.execute("DELETE FROM units WHERE id = ?", (unit_id,))
        db.commit()
    except sqlite3.IntegrityError:
        raise HTTPException(400, "Unit is still used by a recipe")
    return {"ok": True}


@router.get("/conversions")
def list_conversions(db=Depends(get_db)):
    return rows(db.execute("""
        SELECT c.*, f.name AS from_unit, t.name AS to_unit FROM unit_conversions c
        JOIN units f ON f.id = c.from_unit_id JOIN units t ON t.id = c.to_unit_id
        ORDER BY f.name"""))


@router.post("/conversions", status_code=201)
def add_conversion(data: ConversionIn, db=Depends(get_db)):
    if data.factor <= 0 or data.from_unit_id == data.to_unit_id:
        raise HTTPException(400, "Pick two different units and a factor above zero")
    db.execute("""INSERT INTO unit_conversions (from_unit_id, to_unit_id, factor) VALUES (?, ?, ?)
                  ON CONFLICT(from_unit_id, to_unit_id) DO UPDATE SET factor = excluded.factor""",
               (data.from_unit_id, data.to_unit_id, data.factor))
    db.commit()
    return {"ok": True}


@router.delete("/conversions/{conv_id}")
def delete_conversion(conv_id: int, db=Depends(get_db)):
    db.execute("DELETE FROM unit_conversions WHERE id = ?", (conv_id,))
    db.commit()
    return {"ok": True}
