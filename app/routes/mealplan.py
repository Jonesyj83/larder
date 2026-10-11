from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import stock as st
from ..auth import current_user
from ..db import begin, get_db, row, rows
from .shopping import add_item, listed_amount
from ..ingredients import MAX_SERVINGS, IngredientIn, atomic_save, save_ingredients
from .stock import canonical_date

router = APIRouter(prefix="/api", tags=["meal plan"])

SLOTS = ["breakfast", "lunch", "dinner", "snack", "dessert"]
Slot = Literal["breakfast", "lunch", "dinner", "snack", "dessert"]

# how much of a "normal" portion each setting means
PORTION_FACTOR = {"small": 0.5, "regular": 1.0, "large": 1.5}


class MealIn(BaseModel):
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    day: str
    slot: Slot
    recipe_id: Optional[int] = None
    note: str = ""
    servings: Optional[float] = Field(default=None, gt=0, le=MAX_SERVINGS, allow_inf_nan=False)
    ingredients: list[IngredientIn] = []
    eaters: list[int] = Field(default=[], max_length=200)  # user ids, used to work out servings from portion sizes


class MealEdit(BaseModel):
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    recipe_id: Optional[int] = None
    ingredients: Optional[list[IngredientIn]] = None
    day: Optional[str] = None
    slot: Optional[Slot] = None
    note: Optional[str] = None
    servings: Optional[float] = Field(default=None, gt=0, le=MAX_SERVINGS, allow_inf_nan=False)


def _day(s):
    try:
        canonical_date(s)
        if not s:
            raise ValueError()
        return date.fromisoformat(s)
    except (TypeError, ValueError):
        raise HTTPException(400, "Dates need to look like 2026-01-31")


def simple_recipe(db, note, servings, ingredients, user):
    """Save intentional ingredients as a reusable recipe."""
    if not note.strip():
        raise HTTPException(400, "Give the simple meal a name")
    cur = db.execute("INSERT INTO recipes(name,servings,created_by) VALUES(?,?,?)",
                     (note.strip(), servings or 1, user["id"]))
    save_ingredients(db, cur.lastrowid, ingredients)
    return cur.lastrowid


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
    try:
        last = first + timedelta(days=days - 1)
    except OverflowError:
        raise HTTPException(400, "That week is past the end of the calendar")
    meals = rows(db.execute("""
        SELECT m.*, r.name AS recipe, r.servings AS recipe_servings, u.username
        FROM meal_plan m LEFT JOIN recipes r ON r.id = m.recipe_id LEFT JOIN users u ON u.id = m.user_id
        WHERE m.day BETWEEN ? AND ? ORDER BY m.day, m.id""", (first.isoformat(), last.isoformat())))
    return {"start": first.isoformat(), "end": last.isoformat(), "slots": SLOTS, "meals": meals}


@router.post("/mealplan", status_code=201)
@atomic_save
def add_meal(data: MealIn, db=Depends(get_db), user=Depends(current_user)):
    _day(data.day)
    if not data.recipe_id and not data.note.strip():
        raise HTTPException(400, "Pick a recipe or write something")
    servings = data.servings or servings_for(db, data.eaters)
    if data.recipe_id and data.ingredients:
        raise HTTPException(400, "Choose a recipe or add simple meal ingredients")
    if data.recipe_id:
        r = row(db.execute("SELECT servings FROM recipes WHERE id = ?", (data.recipe_id,)))
        if not r:
            raise HTTPException(404, "Recipe not found")
        servings = servings or r["servings"]
    recipe_id = data.recipe_id
    if data.ingredients:
        recipe_id = simple_recipe(db, data.note, servings, data.ingredients, user)
        servings = servings or 1
    cur = db.execute("INSERT INTO meal_plan (day, slot, recipe_id, note, servings, user_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (data.day, data.slot, recipe_id, data.note.strip(), servings, user["id"]))
    if data.ingredients:
        db.execute("UPDATE meal_plan SET simple_meal=1 WHERE id=?", (cur.lastrowid,))
    return row(db.execute("SELECT * FROM meal_plan WHERE id = ?", (cur.lastrowid,)))


@router.put("/mealplan/{meal_id}")
@atomic_save
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
    if "recipe_id" in data.model_fields_set:
        m["recipe_id"] = data.recipe_id
    if m["recipe_id"]:
        recipe = row(db.execute("SELECT * FROM recipes WHERE id=?", (m["recipe_id"],)))
        if not recipe:
            raise HTTPException(404, "Recipe not found")
        m["servings"] = m["servings"] or recipe["servings"]
    if not m["recipe_id"] and not m["note"].strip():
        raise HTTPException(400, "Pick a recipe or name the meal")
    if data.ingredients is not None:
        if data.recipe_id and data.ingredients:
            raise HTTPException(400, "Choose a recipe or simple meal ingredients")
        if data.ingredients:
            if not m["note"].strip():
                raise HTTPException(400, "Give the simple meal a name")
            original = row(db.execute("SELECT * FROM meal_plan WHERE id=?", (meal_id,)))
            owned = original["simple_meal"] and original["recipe_id"] and db.execute(
                "SELECT COUNT(*) FROM meal_plan WHERE recipe_id=?", (original["recipe_id"],)).fetchone()[0] == 1
            if owned:
                m["recipe_id"] = original["recipe_id"]
                db.execute("UPDATE recipes SET name=?,servings=? WHERE id=?",
                           (m["note"].strip(), m["servings"] or 1, m["recipe_id"]))
                save_ingredients(db, m["recipe_id"], data.ingredients)
            else:
                m["recipe_id"] = simple_recipe(db, m["note"], m["servings"], data.ingredients, user)
            m["simple_meal"] = 1
            m["servings"] = m["servings"] or 1
        elif m["simple_meal"]:
            m["recipe_id"] = None
            m["simple_meal"] = 0
    elif "recipe_id" in data.model_fields_set:
        m["simple_meal"] = 0
    if not m["recipe_id"] and not m["note"].strip():
        raise HTTPException(400, "Name the meal or choose a recipe")
    db.execute("UPDATE meal_plan SET day=?, slot=?, note=?, servings=?, recipe_id=?, simple_meal=? WHERE id=?",
               (m["day"], m["slot"], m["note"].strip(), m["servings"], m["recipe_id"], m["simple_meal"], meal_id))
    return row(db.execute("SELECT * FROM meal_plan WHERE id=?", (meal_id,)))


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
        SELECT m.id, m.day, m.slot, m.note, m.servings, r.id AS recipe_id, r.name AS recipe, r.servings AS recipe_servings
        FROM meal_plan m LEFT JOIN recipes r ON r.id = m.recipe_id WHERE m.day BETWEEN ? AND ?""", (start, end)))
    totals, problems, unmapped = {}, [], []
    for m in meals:
        ingredients = rows(db.execute("SELECT * FROM recipe_ingredients WHERE recipe_id = ?", (m["recipe_id"],)))
        if not ingredients:
            unmapped.append({"id": m["id"], "day": m["day"], "slot": m["slot"],
                             "note": m["note"] or m["recipe"], "reason": "No ingredients linked"})
            continue
        # older rows can have 0 servings on the recipe; treat that as "as written"
        base = m["recipe_servings"] if m["recipe_servings"] and m["recipe_servings"] > 0 else None
        scale = (m["servings"] or base) / base if base else 1
        for i in ingredients:
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
    return {"items": out, "problems": sorted(set(problems)), "unmapped": unmapped}


@router.post("/mealplan/shopping")
def plan_to_shopping(start: str, end: str, db=Depends(get_db), user=Depends(current_user)):
    begin(db)
    n = needs(start, end, db, user)
    added = 0
    for i in n["items"]:
        if i["missing"] > 1e-9:
            # don't keep piling on if it's already listed with enough, in whatever unit
            extra = round(i["missing"] - listed_amount(db, i["id"]), 3)
            if extra > 0:
                add_item(db, i["id"], extra, i["unit_id"], user_id=user["id"], source="meal_plan", source_ref=start + ":" + end, source_label="Meal plan " + start + " to " + end)
                added += 1
    db.commit()
    return {"added": added, "problems": n["problems"], "unmapped": n["unmapped"]}
