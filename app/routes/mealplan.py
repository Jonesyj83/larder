from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import stock as st
from ..auth import current_user
from ..db import get_db, row, rows
from .shopping import add_item

router = APIRouter(prefix="/api", tags=["meal plan"])

SLOTS = ["breakfast", "lunch", "dinner", "snack", "dessert"]
Slot = Literal["breakfast", "lunch", "dinner", "snack", "dessert"]

# how much of a "normal" portion each setting means
PORTION_FACTOR = {"small": 0.5, "regular": 1.0, "large": 1.5}


class MealIn(BaseModel):
    day: str
    slot: Slot
    recipe_id: Optional[int] = None
    note: str = ""
    servings: Optional[float] = None
    eaters: list[int] = []  # user ids, used to work out servings from portion sizes


class MealEdit(BaseModel):
    day: Optional[str] = None
    slot: Optional[Slot] = None
    note: Optional[str] = None
    servings: Optional[float] = None


def _day(s):
    try:
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(400, "Dates need to look like 2026-01-31")


def servings_for(db, eaters):
    if not eaters:
        return None
    marks = ",".join("?" * len(eaters))
    users = rows(db.execute(f"SELECT portion_size FROM users WHERE id IN ({marks})", eaters))
    return sum(PORTION_FACTOR.get(u["portion_size"], 1.0) for u in users) or None


@router.get("/household", tags=["account"])
def household(db=Depends(get_db), user=Depends(current_user)):
    """Everyone's name and portion size, for picking who's eating."""
    return [dict(u, factor=PORTION_FACTOR.get(u["portion_size"], 1.0))
            for u in rows(db.execute("SELECT id, username, portion_size FROM users ORDER BY username"))]


@router.get("/mealplan")
def week(start: Optional[str] = None, days: int = 7, db=Depends(get_db), user=Depends(current_user)):
    first = _day(start) if start else date.today() - timedelta(days=date.today().weekday())
    days = max(1, min(days, 42))
    last = first + timedelta(days=days - 1)
    meals = rows(db.execute("""
        SELECT m.*, r.name AS recipe, r.servings AS recipe_servings, u.username
        FROM meal_plan m LEFT JOIN recipes r ON r.id = m.recipe_id LEFT JOIN users u ON u.id = m.user_id
        WHERE m.day BETWEEN ? AND ? ORDER BY m.day, m.id""", (first.isoformat(), last.isoformat())))
    return {"start": first.isoformat(), "end": last.isoformat(), "slots": SLOTS, "meals": meals}


@router.post("/mealplan", status_code=201)
def add_meal(data: MealIn, db=Depends(get_db), user=Depends(current_user)):
    _day(data.day)
    if not data.recipe_id and not data.note.strip():
        raise HTTPException(400, "Pick a recipe or write something")
    servings = data.servings or servings_for(db, data.eaters)
    if data.recipe_id:
        r = row(db.execute("SELECT servings FROM recipes WHERE id = ?", (data.recipe_id,)))
        if not r:
            raise HTTPException(404, "Recipe not found")
        servings = servings or r["servings"]
    cur = db.execute("INSERT INTO meal_plan (day, slot, recipe_id, note, servings, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (data.day, data.slot, data.recipe_id, data.note.strip(), servings, user["id"]))
    db.commit()
    return row(db.execute("SELECT * FROM meal_plan WHERE id = ?", (cur.lastrowid,)))


@router.put("/mealplan/{meal_id}")
def edit_meal(meal_id: int, data: MealEdit, db=Depends(get_db), user=Depends(current_user)):
    m = row(db.execute("SELECT * FROM meal_plan WHERE id = ?", (meal_id,)))
    if not m:
        raise HTTPException(404, "Not in the plan")
    if data.day is not None:
        _day(data.day)
        m["day"] = data.day
    for f in ("slot", "note", "servings"):
        if getattr(data, f) is not None:
            m[f] = getattr(data, f)
    db.execute("UPDATE meal_plan SET day = ?, slot = ?, note = ?, servings = ? WHERE id = ?",
               (m["day"], m["slot"], m["note"], m["servings"], meal_id))
    db.commit()
    return m


@router.delete("/mealplan/{meal_id}")
def delete_meal(meal_id: int, db=Depends(get_db), user=Depends(current_user)):
    db.execute("DELETE FROM meal_plan WHERE id = ?", (meal_id,))
    db.commit()
    return {"ok": True}


@router.get("/mealplan/needs")
def needs(start: str, end: str, db=Depends(get_db), user=Depends(current_user)):
    """Add up every ingredient for the planned meals between two dates and compare with stock."""
    _day(start), _day(end)
    meals = rows(db.execute("""
        SELECT m.servings, r.id AS recipe_id, r.servings AS recipe_servings
        FROM meal_plan m JOIN recipes r ON r.id = m.recipe_id WHERE m.day BETWEEN ? AND ?""", (start, end)))
    totals, problems = {}, []
    for m in meals:
        scale = (m["servings"] or m["recipe_servings"]) / m["recipe_servings"]
        for i in rows(db.execute("SELECT * FROM recipe_ingredients WHERE recipe_id = ?", (m["recipe_id"],))):
            p = st.get_product(db, i["product_id"])
            try:
                need = st.to_stock_units(db, p, i["amount"] * scale, i["unit_id"])
            except st.StockError as e:
                problems.append(str(e))
                continue
            totals[p["id"]] = totals.get(p["id"], 0) + need
    out = []
    for pid, need in totals.items():
        p = row(db.execute("SELECT p.id, p.name, p.unit_id, u.name AS unit FROM products p JOIN units u ON u.id = p.unit_id WHERE p.id = ?", (pid,)))
        have = st.in_stock(db, pid)
        out.append({**p, "need": need, "have": have, "missing": max(0.0, need - have)})
    out.sort(key=lambda x: x["name"].lower())
    return {"items": out, "problems": sorted(set(problems))}


@router.post("/mealplan/shopping")
def plan_to_shopping(start: str, end: str, db=Depends(get_db), user=Depends(current_user)):
    n = needs(start, end, db, user)
    added = 0
    for i in n["items"]:
        if i["missing"] > 1e-9:
            on_list = row(db.execute("SELECT amount FROM shopping_list WHERE product_id = ? AND done = 0 AND unit_id = ?",
                                     (i["id"], i["unit_id"])))
            # don't keep piling on if it's already listed with enough
            extra = i["missing"] - (on_list["amount"] if on_list else 0)
            if extra > 1e-9:
                add_item(db, i["id"], round(extra, 3), i["unit_id"], user_id=user["id"])
                added += 1
    db.commit()
    return {"added": added, "problems": n["problems"]}
