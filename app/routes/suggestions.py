import json
import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import providers
from ..auth import current_user
from ..db import get_db, row, rows
from .mealplan import PORTION_FACTOR, SLOTS
from .recipes import list_recipes

router = APIRouter(prefix="/api", tags=["suggestions"])


class SettingsIn(BaseModel):
    provider: Literal["openai", "anthropic", "openrouter", "deepseek", "ollama"] = "openai"
    api_key: str | None = None  # None = keep the one that's saved
    model: str = ""
    base_url: str = ""
    shared: bool = False


class MealIdeasIn(BaseModel):
    days: int = 1
    slots: list[str] = ["dinner"]
    note: str = ""


class RecipeIdeaIn(BaseModel):
    idea: str = ""
    use_expiring: bool = True


def settings_for(db, user):
    """The user's own provider settings, or a household-shared one if they don't have any."""
    s = row(db.execute("SELECT * FROM suggestion_settings WHERE user_id = ?", (user["id"],)))
    if providers.is_configured(s):
        return s
    for s in rows(db.execute("SELECT * FROM suggestion_settings WHERE shared = 1 ORDER BY user_id")):
        if providers.is_configured(s):
            return s
    return None


def _run(db, user, prompt, system):
    s = settings_for(db, user)
    if not s:
        raise HTTPException(400, "Add an API key in Settings to use this")
    try:
        return providers.ask(s, prompt, system)
    except providers.ProviderError as e:
        raise HTTPException(502, str(e))


def _kitchen(db):
    """What's in stock, written out for the prompt."""
    today = date.today()
    lines = []
    for s in rows(db.execute("""
            SELECT p.name, u.name AS unit, SUM(s.amount) AS amount, MIN(s.expires_on) AS exp, p.shelf_life_kind
            FROM stock s JOIN products p ON p.id = s.product_id JOIN units u ON u.id = p.unit_id
            GROUP BY p.id ORDER BY exp IS NULL, exp""")):
        line = f"- {s['name']}: {round(s['amount'], 2):g} {s['unit']}"
        if s["exp"]:
            days = (date.fromisoformat(s["exp"]) - today).days
            label = "use by" if s["shelf_life_kind"] == "use_by" else "best by"
            line += f" ({label} {s['exp']}, {days} days)"
        lines.append(line)
    return "\n".join(lines) or "(nothing in stock)"


def _people(db):
    out = []
    for u in rows(db.execute("SELECT username, portion_size FROM users")):
        out.append(f"- {u['username']}: {u['portion_size']} portions")
    return "\n".join(out)


SYSTEM = ("You help a household plan meals from what they already have at home. "
          "Prefer using things that go off soonest. Keep it practical and short, "
          "home cooking, no fancy equipment. Respect portion sizes: 'small' means "
          "about half a normal portion and the person really can't manage more, "
          "'large' means a big appetite.")


@router.get("/suggestions/status")
def status(db=Depends(get_db), user=Depends(current_user)):
    s = settings_for(db, user)
    return {"enabled": bool(s), "provider": s["provider"] if s else None}


@router.get("/me/suggestions")
def get_settings(db=Depends(get_db), user=Depends(current_user)):
    s = row(db.execute("SELECT * FROM suggestion_settings WHERE user_id = ?", (user["id"],)))
    if not s:
        return {"provider": "openai", "model": "", "base_url": "", "shared": False, "has_key": False,
                "providers": list(providers.PROVIDERS)}
    key = s.pop("api_key")
    s["has_key"] = bool(key)
    s["key_hint"] = ("..." + key[-4:]) if len(key) > 8 else ""
    s["shared"] = bool(s["shared"])
    s["providers"] = list(providers.PROVIDERS)
    return s


@router.put("/me/suggestions")
def save_settings(data: SettingsIn, db=Depends(get_db), user=Depends(current_user)):
    old = row(db.execute("SELECT api_key FROM suggestion_settings WHERE user_id = ?", (user["id"],)))
    key = data.api_key.strip() if data.api_key is not None else (old["api_key"] if old else "")
    db.execute("""INSERT INTO suggestion_settings (user_id, provider, api_key, model, base_url, shared)
                  VALUES (?, ?, ?, ?, ?, ?)
                  ON CONFLICT(user_id) DO UPDATE SET provider = excluded.provider, api_key = excluded.api_key,
                      model = excluded.model, base_url = excluded.base_url, shared = excluded.shared""",
               (user["id"], data.provider, key, data.model.strip(), data.base_url.strip(), int(data.shared)))
    db.commit()
    return get_settings(db, user)


@router.delete("/me/suggestions")
def clear_settings(db=Depends(get_db), user=Depends(current_user)):
    db.execute("DELETE FROM suggestion_settings WHERE user_id = ?", (user["id"],))
    db.commit()
    return {"ok": True}


@router.post("/me/suggestions/test")
def test_settings(db=Depends(get_db), user=Depends(current_user)):
    text = _run(db, user, "Reply with just the word: ok", None)
    return {"reply": text.strip()[:200]}


@router.post("/suggestions/meals")
def meal_ideas(data: MealIdeasIn, db=Depends(get_db), user=Depends(current_user)):
    days = max(1, min(data.days, 7))
    slots = [s for s in data.slots if s in SLOTS] or ["dinner"]
    recipes = list_recipes(db, user)
    known = "\n".join(f"- {r['name']}" + (" (everything in stock)" if r["can_make"] else f" ({r['missing_count']} things missing)")
                      for r in recipes) or "(none saved yet)"
    me = f"{user['username']}, who likes {user['portion_size']} portions (about {PORTION_FACTOR[user['portion_size']]}x normal)"
    prompt = f"""Suggest meals for {days} day(s), covering: {', '.join(slots)}.

Asked by {me}.

People in the house:
{_people(db)}

In stock right now:
{_kitchen(db)}

Recipes we already have saved:
{known}

{('Extra wishes: ' + data.note) if data.note.strip() else ''}

Use saved recipes where they fit. For each meal give the name, roughly what goes in it and
how much to make for the portion sizes above. Point out anything we'd need to buy.
Plain text, no tables."""
    return {"text": _run(db, user, prompt, SYSTEM)}


def _parse_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


@router.post("/suggestions/recipe")
def recipe_idea(data: RecipeIdeaIn, db=Depends(get_db), user=Depends(current_user)):
    prompt = f"""Come up with one recipe we could make mostly from what's in stock.
{('What I fancy: ' + data.idea) if data.idea.strip() else ''}
{'Try to use up the things going off soonest.' if data.use_expiring else ''}

In stock:
{_kitchen(db)}

Reply with only JSON, nothing before or after, like this:
{{"name": "...", "servings": 2, "ingredients": [{{"name": "...", "amount": 1, "unit": "piece"}}], "instructions": "1. ...\\n2. ..."}}
Use the same ingredient names as the stock list where you can. Units should be simple: piece, g, kg, ml, l, tin, pack."""
    text = _run(db, user, prompt, SYSTEM)
    parsed = _parse_json(text)
    if not parsed or not isinstance(parsed.get("ingredients"), list):
        return {"recipe": None, "text": text}

    products = rows(db.execute("SELECT id, name, unit_id FROM products"))
    units = {u["name"].lower(): u["id"] for u in rows(db.execute("SELECT id, name FROM units"))}
    units.update({u["name_plural"].lower(): u["id"] for u in rows(db.execute("SELECT id, name_plural FROM units WHERE name_plural != ''"))})
    ingredients = []
    for i in parsed["ingredients"]:
        if not isinstance(i, dict):
            continue
        name = str(i.get("name", "")).strip()
        match = _match_product(products, name)
        try:
            amount = float(i.get("amount") or 1)
        except (TypeError, ValueError):
            amount = 1
        unit = str(i.get("unit", "")).strip().lower()
        ingredients.append({
            "name": name, "amount": amount, "unit": unit,
            "product_id": match["id"] if match else None,
            "product": match["name"] if match else None,
            "unit_id": units.get(unit) or (match["unit_id"] if match else None),
        })
    try:
        servings = float(parsed.get("servings") or 2)
    except (TypeError, ValueError):
        servings = 2
    return {"recipe": {"name": str(parsed.get("name") or "Suggested recipe"), "servings": servings,
                       "instructions": str(parsed.get("instructions") or ""), "ingredients": ingredients},
            "text": None}


def _match_product(products, name):
    n = name.lower()
    if not n:
        return None
    for p in products:
        if p["name"].lower() == n:
            return p
    # "chicken breast" vs "Tesco chicken breast fillets" and so on
    for p in products:
        pn = p["name"].lower()
        if n in pn or pn in n:
            return p
    return None
