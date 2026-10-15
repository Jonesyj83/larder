import math
import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import stock as st
from ..auth import current_user
from ..db import begin, get_db, row, rows
from .shopping import add_item, listed_amount
from ..ingredients import MAX_SERVINGS, IngredientIn, atomic_save, save_ingredients

router = APIRouter(prefix="/api/recipes", tags=["recipes"])


class RecipeIn(BaseModel):
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    name: str
    servings: float = Field(default=2, gt=0, le=MAX_SERVINGS, allow_inf_nan=False)
    instructions: str = ""
    ingredients: list[IngredientIn] = []


def check(db, recipe_id, servings=None):
    """Work out what's needed vs what's in stock. Plain sums, nothing clever."""
    r = row(db.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)))
    if not r:
        raise HTTPException(404, "Recipe not found")
    if servings is not None:
        if not math.isfinite(servings) or servings <= 0 or servings > MAX_SERVINGS:
            raise HTTPException(400, "Servings has to be finite and above zero")
    servings = servings if servings is not None else r["servings"]
    scale = servings / r["servings"] if r["servings"] and r["servings"] > 0 else 1
    ings = rows(db.execute("""
        SELECT i.*, p.name, p.unit_id AS stock_unit_id, su.name AS stock_unit, u.name AS unit
        FROM recipe_ingredients i
        JOIN products p ON p.id = i.product_id
        JOIN units su ON su.id = p.unit_id
        LEFT JOIN units u ON u.id = i.unit_id
        WHERE i.recipe_id = ? ORDER BY i.id""", (recipe_id,)))
    missing = 0
    remaining = {}
    for i in ings:
        p = st.get_product(db, i["product_id"])
        i["amount_scaled"] = i["amount"] * scale
        i["unit"] = i["unit"] or i["stock_unit"]
        i["have"] = st.in_stock(db, i["product_id"])
        available = remaining.get(i["product_id"], i["have"])
        try:
            i["need"] = st.to_stock_units(db, p, i["amount_scaled"], i["unit_id"])
            i["error"] = None
        except st.StockError as e:
            i["need"] = None
            i["error"] = str(e)
        if i["need"] is None:
            i["missing"] = None
            i["enough"] = False
        else:
            i["missing"] = max(0.0, i["need"] - available)
            remaining[i["product_id"]] = max(0.0, available - i["need"])
            i["enough"] = i["missing"] <= 1e-9
        if not i["enough"]:
            missing += 1
    r["servings_wanted"] = servings
    r["ingredients"] = ings
    r["missing_count"] = missing
    r["can_make"] = missing == 0 and bool(ings)
    return r


_save_ingredients = save_ingredients


@router.get("")
def list_recipes(db=Depends(get_db), user=Depends(current_user)):
    out = []
    for r in rows(db.execute("SELECT id FROM recipes ORDER BY name COLLATE NOCASE")):
        full = check(db, r["id"])
        full["ingredient_count"] = len(full.pop("ingredients"))
        full.pop("instructions")
        out.append(full)
    return out


@router.get("/makeable")
def what_can_i_make(db=Depends(get_db), user=Depends(current_user)):
    """Every recipe, the ones you can make right now first, then by how little is missing."""
    out = list_recipes(db, user)
    out.sort(key=lambda r: (not r["can_make"], r["missing_count"], r["name"].lower()))
    return out


@router.get("/{recipe_id}")
def get_recipe(recipe_id: int, servings: Optional[float] = None, db=Depends(get_db), user=Depends(current_user)):
    return check(db, recipe_id, servings)


@router.post("", status_code=201)
@atomic_save
def add_recipe(data: RecipeIn, db=Depends(get_db), user=Depends(current_user)):
    if not data.name.strip():
        raise HTTPException(400, "Recipe needs a name")
    if data.servings <= 0:
        raise HTTPException(400, "Servings has to be above zero")
    try:
        cur = db.execute("INSERT INTO recipes (name, servings, instructions, created_by) VALUES (?, ?, ?, ?)",
                         (data.name.strip(), data.servings, data.instructions, user["id"]))
        _save_ingredients(db, cur.lastrowid, data.ingredients)
    except sqlite3.IntegrityError:
        db.rollback()
        raise HTTPException(400, "One of the ingredients points at a product or unit that doesn't exist")
    except HTTPException:
        db.rollback()
        raise
    return check(db, cur.lastrowid)


@router.put("/{recipe_id}")
@atomic_save
def edit_recipe(recipe_id: int, data: RecipeIn, db=Depends(get_db), user=Depends(current_user)):
    check(db, recipe_id)
    if not data.name.strip():
        raise HTTPException(400, "Recipe needs a name")
    if data.servings <= 0:
        raise HTTPException(400, "Servings has to be above zero")
    try:
        db.execute("UPDATE recipes SET name = ?, servings = ?, instructions = ? WHERE id = ?",
                   (data.name.strip(), data.servings, data.instructions, recipe_id))
        _save_ingredients(db, recipe_id, data.ingredients)
    except sqlite3.IntegrityError:
        db.rollback()
        raise HTTPException(400, "One of the ingredients points at a product or unit that doesn't exist")
    except HTTPException:
        db.rollback()
        raise
    return check(db, recipe_id)


@router.delete("/{recipe_id}")
def delete_recipe(recipe_id: int, db=Depends(get_db), user=Depends(current_user)):
    db.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    db.commit()
    return {"ok": True}


class RatingIn(BaseModel):
    rating: int = Field(ge=0, le=5)


@router.put("/{recipe_id}/rating")
def rate_recipe(recipe_id: int, data: RatingIn, db=Depends(get_db), user=Depends(current_user)):
    if not db.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone():
        raise HTTPException(404, "Recipe not found")
    db.execute("UPDATE recipes SET rating = ? WHERE id = ?", (data.rating, recipe_id))
    db.commit()
    return {"rating": data.rating}


@router.post("/{recipe_id}/shopping")
def missing_to_shopping(recipe_id: int, servings: Optional[float] = None, db=Depends(get_db), user=Depends(current_user)):
    """Put whatever's short onto the shopping list, in stock units."""
    begin(db)
    r = check(db, recipe_id, servings)
    added = 0
    shortfalls = {}
    for i in r["ingredients"]:
        if i["need"] is None:
            continue
        pid = i["product_id"]
        shortfalls[pid] = shortfalls.get(pid, 0) + i["missing"]
    for pid, short in shortfalls.items():
        extra = short - listed_amount(db, pid)
        if extra > 1e-9:
            add_item(db, pid, extra, st.get_product(db, pid)["unit_id"], user_id=user["id"], source="recipe", source_ref=recipe_id, source_label=r["name"])
            added += 1
    db.commit()
    return {"added": added, "problems": [i["error"] for i in r["ingredients"] if i["error"]]}


@router.post("/{recipe_id}/cook")
def cook(recipe_id: int, servings: Optional[float] = None, db=Depends(get_db), user=Depends(current_user)):
    """Take the ingredients out of stock. Uses what's there if something's short."""
    # all or nothing, and a double click can't take everything out twice
    begin(db)
    r = check(db, recipe_id, servings)
    used, skipped = [], []
    try:
        for i in r["ingredients"]:
            if not i["need"] or i["have"] <= 0:
                skipped.append(i["name"])
                continue
            st.consume(db, i["product_id"], i["need"], None, user_id=user["id"], allow_partial=True, commit=False)
            used.append(i["name"])
    except st.StockError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    db.commit()
    return {"used": used, "skipped": skipped}
