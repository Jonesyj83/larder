import math
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
        SELECT m.*, r.name AS recipe, r.servings AS recipe_servings, u.username, pc.servings AS cook_servings
        FROM meal_plan m LEFT JOIN recipes r ON r.id = m.recipe_id LEFT JOIN users u ON u.id = m.user_id
        LEFT JOIN planned_cooks pc ON pc.id = m.cook_event_id
        WHERE m.day BETWEEN ? AND ? ORDER BY m.day, m.id""", (first.isoformat(), last.isoformat())))
    return {"start": first.isoformat(), "end": last.isoformat(), "slots": SLOTS, "meals": meals}


@router.post("/mealplan", status_code=201)
@atomic_save
def add_meal(data: MealIn, db=Depends(get_db), user=Depends(current_user)):
    return _create_meal(data, db, user)


def _create_meal(data, db, user):
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


def review_cook_recipe(db, parsed, wanted=None):
    def positive(value):
        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value <= 0 or value > 1000000:
            raise HTTPException(502, 'Recipe has invalid amounts or servings; nothing was saved')
        return value
    if not isinstance(parsed,dict):
        raise HTTPException(502, 'Invalid recipe; nothing was saved')
    servings = positive(parsed.get('servings'))
    items = parsed.get('ingredients')
    if not isinstance(items,list) or not 1 <= len(items) <= 50:
        raise HTTPException(502, 'Recipe needs 1–50 ingredients; nothing was saved')
    units = {}
    for u in rows(db.execute('SELECT * FROM units')):
        for name in (u['name'],u['name_plural']):
            if name: units[name.casefold()] = u['id']
    products = rows(db.execute('SELECT * FROM products'))
    stock_units = {u['id']: u for u in rows(db.execute('SELECT * FROM units'))}
    ingredients = []
    remaining = {}
    for item in items:
        if not isinstance(item,dict) or not isinstance(item.get('name'),str) or not 1 <= len(item['name'].strip()) <= 200:
            raise HTTPException(502, 'Invalid ingredient name; nothing was saved')
        amount = positive(item.get('amount')) * (wanted / servings if wanted else 1)
        if not math.isfinite(amount):
            raise HTTPException(502, 'Invalid ingredient amount')
        unit = item.get('unit')
        if not isinstance(unit,str) or len(unit) > 80:
            raise HTTPException(502, 'Invalid ingredient unit; nothing was saved')
        name = item['name'].strip()
        match = next((p for p in products if p['name'].casefold() == name.casefold()),None)
        unit_id = units.get(unit.strip().casefold())
        have = short = short_unit_id = short_unit = None
        if name.casefold() in {'water', 'tap water', 'iced water', 'ice', 'ice cubes', 'ice cube'}:
            ingredients.append(dict(name=name, amount=amount, unit=unit, unit_id=unit_id,
                                    product_id=None, product='always on hand', matched_product_id=None,
                                    have=None, short=0, short_unit=unit, short_unit_id=unit_id, note=''))
            continue
        if match and unit_id is not None:
            try:
                need = st.to_stock_units(db, match, amount, unit_id)
                have = st.in_stock(db, match['id'])
                available = remaining.get(match['id'], have)
                short = max(0.0, need - available)
                remaining[match['id']] = max(0.0, available - need)
                short_unit_id = match['unit_id']  # short is in the product's own stock unit
                short_unit = stock_units.get(short_unit_id, {}).get('name')
            except st.StockError:
                pass  # a unit we can't convert just skips the have/short hint
        ingredients.append(dict(name=name, amount=amount, unit=unit, unit_id=unit_id,
                                product_id=None, product=match['name'] if match else None,
                                matched_product_id=match['id'] if match else None,
                                have=have, short=short if match else amount,
                                short_unit=short_unit if match else unit,
                                short_unit_id=short_unit_id if match else unit_id,
                                note='' if unit.strip().casefold() in units else 'Proposed unit: ' + unit + '. Choose a valid unit.'))
    return dict(name=str(parsed.get('name') or 'Proposed recipe')[:200],
                description=str(parsed.get('description') or '')[:300],
                servings=wanted or servings,
                ingredients=ingredients, instructions=str(parsed.get('instructions') or '')[:20000])


class CookIngredientIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    amount: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    unit: str = Field(max_length=80)


class CookUseIn(BaseModel):
    day: str
    slot: Slot
    portions: float = Field(gt=0, le=MAX_SERVINGS, allow_inf_nan=False)
    twist: str = Field(default="", max_length=200)


class CookEventIn(BaseModel):
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=200)
    servings: float = Field(gt=0, le=MAX_SERVINGS, allow_inf_nan=False)
    ingredients: list[CookIngredientIn] = Field(min_length=1, max_length=50)
    instructions: str = Field(default="", max_length=20000)
    uses: list[CookUseIn] = Field(min_length=1, max_length=10)


def _shop_for_needed_ingredients(db, needed, user, source_label):
    """Anything the batch needs that stock doesn't cover - a brand-new product (starts
    at zero) or a shortfall on one that already exists - goes straight on the shopping
    list, so 'Add to plan' is the one step that actually covers what's needed."""
    for item in needed:
        if item.get("product_id"):
            pid = item["product_id"]
        else:
            p = row(db.execute("SELECT id FROM products WHERE name = ? COLLATE NOCASE", (item["name"].strip(),)))
            pid = p["id"] if p else None
        if pid and item["amount"] > 1e-9:
            add_item(db, pid, item["amount"], item["unit_id"], user_id=user["id"],
                     source="meal_plan", source_label=source_label)


@router.post("/mealplan/from-cook-event", status_code=201)
@atomic_save
def meal_from_cook_event(data: CookEventIn, db=Depends(get_db), user=Depends(current_user)):
    """Save one cook event - a whole batch - across every slot it covers: one recipe,
    several meal plan entries, each eating a share of the same batch."""
    for u in data.uses:
        _day(u.day)
    reviewed = review_cook_recipe(db, dict(name=data.name, servings=data.servings,
                                       ingredients=[i.model_dump() for i in data.ingredients],
                                       instructions=data.instructions))
    ingredients, not_added, needed = [], [], []
    for item in reviewed["ingredients"]:
        reason = None
        if item["matched_product_id"]:
            if item["short"] is None:
                reason = "not added - choose a valid unit that converts to the product's stock unit"
            else:
                ingredients.append(IngredientIn(product_id=item["matched_product_id"],
                                                amount=item["amount"], unit_id=item["unit_id"]))
                if item["short"] > 0:
                    needed.append(dict(product_id=item["matched_product_id"],
                                       amount=item["short"], unit_id=item["short_unit_id"]))
        elif item["unit_id"]:
            # no product for this one yet, but the unit is real - create the product so the
            # recipe actually has every ingredient, and queue it to go on the shopping list
            # below once it exists (it starts at zero stock, so the full amount is needed)
            ingredients.append(IngredientIn(name=item["name"], amount=item["amount"], unit_id=item["unit_id"]))
            needed.append(dict(name=item["name"], amount=item["amount"], unit_id=item["unit_id"]))
        else:
            reason = "not added - no matching product, and the unit isn't one we track; add it by hand"
        if reason:
            not_added.append(dict(item, reason=reason))
    # Always save the proposed recipe rather than matching by name alone. A same-name
    # recipe can have different ingredients or amounts; silently reusing it loses the
    # meal idea and its shopping needs.
    recipe_id = None
    if ingredients:
        recipe_id = simple_recipe(db, data.name, data.servings, ingredients, user)
        db.execute("UPDATE recipes SET instructions=? WHERE id=?", (data.instructions, recipe_id))
        _shop_for_needed_ingredients(db, needed, user, data.name)
    cook_event_id = None
    if recipe_id:
        cur = db.execute("INSERT INTO planned_cooks(recipe_id,servings,created_by) VALUES(?,?,?)",
                         (recipe_id, data.servings, user["id"]))
        cook_event_id = cur.lastrowid
    # a single-use batch is "owned" by its one meal, same as any other simple meal, so
    # editing it updates the recipe in place. A multi-use batch's recipe is shared, so
    # its meals stay plain recipe-linked meals - editing one (e.g. moving the day) must
    # not fork or touch the shared recipe the other uses still depend on.
    owns_recipe = bool(recipe_id) and len(data.uses) == 1
    meals = []
    for u in data.uses:
        label = f"{u.twist.strip()} ({data.name.strip()})" if u.twist.strip() else data.name.strip()
        if recipe_id:
            meal = _create_meal(MealIn(day=u.day, slot=u.slot, note=label, servings=u.portions,
                                       recipe_id=recipe_id), db, user)
        else:
            meal = _create_meal(MealIn(day=u.day, slot=u.slot, note=label, servings=u.portions), db, user)
        meals.append(meal)
        if recipe_id:
            db.execute("UPDATE meal_plan SET simple_meal=?, cook_event_id=? WHERE id=?",
                       (1 if owns_recipe else 0, cook_event_id, meal["id"]))
            meal["simple_meal"] = 1 if owns_recipe else 0
            meal["cook_event_id"] = cook_event_id
    return {"meals": meals, "not_added": not_added, "reused_recipe": False}


@router.put("/mealplan/{meal_id}")
@atomic_save
def edit_meal(meal_id: int, data: MealEdit, db=Depends(get_db), user=Depends(current_user)):
    m = row(db.execute("SELECT * FROM meal_plan WHERE id = ?", (meal_id,)))
    if not m:
        raise HTTPException(404, "Not in the plan")
    original_recipe_id = m["recipe_id"]
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
    # the recipe actually changed (forked, switched, or cleared) - this meal no longer
    # eats from whatever batch it used to, so needs() must stop grouping it with one
    if m["recipe_id"] != original_recipe_id:
        m["cook_event_id"] = None
    db.execute("UPDATE meal_plan SET day=?, slot=?, note=?, servings=?, recipe_id=?, simple_meal=?, cook_event_id=? WHERE id=?",
               (m["day"], m["slot"], m["note"].strip(), m["servings"], m["recipe_id"], m["simple_meal"], m["cook_event_id"], meal_id))
    return row(db.execute("SELECT * FROM meal_plan WHERE id=?", (meal_id,)))


@router.delete("/mealplan/{meal_id}")
def delete_meal(meal_id: int, db=Depends(get_db), user=Depends(current_user)):
    db.execute("DELETE FROM meal_plan WHERE id = ?", (meal_id,))
    db.commit()
    return {"ok": True}


class FromBatchIn(BaseModel):
    day: str
    slot: Slot
    batch_id: int


@router.post("/mealplan/from-batch", status_code=201)
@atomic_save
def meal_from_batch(data: FromBatchIn, db=Depends(get_db), user=Depends(current_user)):
    """Pull a stored cooked meal straight into a slot - no stock to take out, that
    already happened when it was cooked. Uses up one portion of the batch."""
    _day(data.day)
    b = row(db.execute("SELECT * FROM cooked_batches WHERE id = ?", (data.batch_id,)))
    if not b or b["portions_left"] <= 0:
        raise HTTPException(404, "That stored meal isn't there anymore")
    cur = db.execute("INSERT INTO meal_plan (day, slot, note, servings, user_id, batch_id) VALUES (?, ?, ?, ?, ?, ?)",
                     (data.day, data.slot, b["name"], 1, user["id"], b["id"]))
    left = round(b["portions_left"] - 1, 6)
    if left <= 0:
        db.execute("UPDATE meal_plan SET batch_id = NULL WHERE batch_id = ?", (b["id"],))
        db.execute("DELETE FROM cooked_batches WHERE id = ?", (b["id"],))
    else:
        db.execute("UPDATE cooked_batches SET portions_left = ? WHERE id = ?", (left, b["id"]))
    meal = row(db.execute("SELECT * FROM meal_plan WHERE id = ?", (cur.lastrowid,)))
    return {"meal": meal, "portions_left": max(0.0, left)}


@router.get("/mealplan/needs")
def needs(start: str, end: str, db=Depends(get_db), user=Depends(current_user)):
    """Add up every ingredient for the planned meals between two dates and compare with stock."""
    _day(start), _day(end)
    meals = rows(db.execute("""
        SELECT m.id, m.day, m.slot, m.note, m.servings, m.cook_event_id,
               r.id AS recipe_id, r.name AS recipe, r.servings AS recipe_servings,
               pc.servings AS cook_servings
        FROM meal_plan m LEFT JOIN recipes r ON r.id = m.recipe_id
        LEFT JOIN planned_cooks pc ON pc.id = m.cook_event_id
        WHERE m.day BETWEEN ? AND ?""", (start, end)))
    totals, problems, unmapped = {}, [], []
    processed_cooks = set()
    for m in meals:
        if m["cook_event_id"]:
            if m["cook_event_id"] in processed_cooks:
                continue
            processed_cooks.add(m["cook_event_id"])
            # a batch spread across two needs windows (e.g. Sunday dinner, Monday
            # lunch next week) must only be counted once - attribute it to whichever
            # window holds its earliest use, so the other window skips it entirely
            first_day = row(db.execute(
                "SELECT MIN(day) AS d FROM meal_plan WHERE cook_event_id = ?", (m["cook_event_id"],)))["d"]
            if not (start <= first_day <= end):
                continue
        ingredients = rows(db.execute("SELECT * FROM recipe_ingredients WHERE recipe_id = ?", (m["recipe_id"],)))
        if not ingredients:
            unmapped.append({"id": m["id"], "day": m["day"], "slot": m["slot"],
                             "note": m["note"] or m["recipe"], "reason": "No ingredients linked"})
            continue
        # older rows can have 0 servings on the recipe; treat that as "as written"
        base = m["recipe_servings"] if m["recipe_servings"] and m["recipe_servings"] > 0 else None
        scale = (m["cook_servings"] / base if m["cook_servings"] else
                 (m["servings"] or base) / base) if base else 1
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
