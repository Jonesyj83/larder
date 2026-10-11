import json
import re
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import math

from .. import providers
from .. import stock as st
from ..auth import current_user
from ..db import begin, get_db, row, rows
from .mealplan import PORTION_FACTOR, SLOTS
from .recipes import list_recipes

router = APIRouter(prefix="/api", tags=["suggestions"])


class SettingsIn(BaseModel):
    provider: str = Field(default="anthropic", max_length=120)
    api_key: str | None = Field(default=None, max_length=1000)  # None = keep the one that's saved
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=500)
    shared: bool = False


class MealIdeasIn(BaseModel):
    days: int = 1
    slots: list[str] = Field(default=["dinner"], max_length=10)
    note: str = Field(default="", max_length=2000)


class RecipeIdeaIn(BaseModel):
    idea: str = Field(default="", max_length=2000)
    use_expiring: bool = True


def settings_for(db, user):
    """The user's own provider settings, or a household-shared one if they don't have any."""
    s = row(db.execute("SELECT * FROM suggestion_settings WHERE user_id = ?", (user["id"],)))
    if s and providers.is_configured(s):
        return s
    for s in rows(db.execute("SELECT * FROM suggestion_settings WHERE shared = 1 ORDER BY user_id")):
        if providers.is_configured(s):
            return s
    return None


def _run(db, user, prompt, system):
    s = settings_for(db, user)
    if not s:
        raise HTTPException(400, "Suggestions unavailable; connect a provider in Settings or search saved recipes")
    try:
        result = providers.ask_with_provenance(s, prompt, system)
        text = GeneratedText(result.pop('text'))
        text.provenance = result
        return text
    except providers.ProviderError as e:
        raise HTTPException(502, str(e))


class GeneratedText(str):
    pass


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
            label = "use by" if s["shelf_life_kind"] == "use_by" else "best by"
            try:
                days = (date.fromisoformat(s["exp"]) - today).days
                line += f" ({label} {s['exp']}, {days} days)"
            except ValueError:
                line += f" ({label} {s['exp']})"  # an odd date from an old import shouldn't break this
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
          "'large' means a big appetite. Name dishes the way a person actually would "
          "- 'chilli con carne', 'spaghetti bolognese', 'fried rice' - not a made-up "
          "description like 'Beef and Bean Skillet'. Only invent a name when there "
          "genuinely isn't a standard one for what you're describing.")


@router.get("/suggestions/status")
def status(db=Depends(get_db), user=Depends(current_user)):
    s = settings_for(db, user)
    return {"enabled": bool(s), "provider": s["provider"] if s else None}


@router.get("/me/suggestions")
def get_settings(db=Depends(get_db), user=Depends(current_user)):
    s = row(db.execute("SELECT * FROM suggestion_settings WHERE user_id = ?", (user["id"],)))
    if not s:
        s = dict(provider="anthropic", model="", base_url="", shared=False, api_key="")
    key = s.pop('api_key')
    s['has_key'] = bool(key)
    s['shared'] = bool(s['shared'])
    s['providers'] = providers.catalogue()
    s['state'] = 'Connection not verified' if providers.is_configured(dict(s,api_key=key)) else 'Setup required'
    return s


@router.put("/me/suggestions")
def save_settings(data: SettingsIn, db=Depends(get_db), user=Depends(current_user)):
    try:
        providers.validate_settings(data.provider, data.model.strip(), data.base_url.strip())
        if data.api_key is not None:
            providers.validate_key(data.api_key.strip())
    except providers.ProviderError as e:
        raise HTTPException(400, str(e))
    old = row(db.execute("SELECT * FROM suggestion_settings WHERE user_id = ?", (user["id"],)))
    same_scope = old and old['provider'] == data.provider and old['base_url'] == data.base_url.strip()
    key = data.api_key.strip() if data.api_key is not None else (old['api_key'] if same_scope else '')
    db.execute("""INSERT INTO suggestion_settings (user_id, provider, api_key, model, base_url, shared)
                  VALUES (?, ?, ?, ?, ?, ?)
                  ON CONFLICT(user_id) DO UPDATE SET provider = excluded.provider, api_key = excluded.api_key,
                      model = excluded.model, base_url = excluded.base_url, shared = excluded.shared""",
               (user['id'], data.provider, key, data.model.strip(), data.base_url.strip(), int(data.shared)))
    db.commit()
    return get_settings(db, user)


@router.delete("/me/suggestions")
def clear_settings(db=Depends(get_db), user=Depends(current_user)):
    db.execute("DELETE FROM suggestion_settings WHERE user_id = ?", (user["id"],))
    db.commit()
    return {"ok": True}


class TestIn(BaseModel):
    provider: str | None = Field(default=None, max_length=120)
    api_key: str | None = Field(default=None, max_length=1000)
    model: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)


@router.post("/me/suggestions/test")
def test_settings(data: TestIn = TestIn(), db=Depends(get_db), user=Depends(current_user)):
    if data.provider:
        # Test exactly what's in the form right now, not what's been saved -
        # a key that hasn't been saved yet still needs to be testable.
        saved = row(db.execute("SELECT * FROM suggestion_settings WHERE user_id = ?", (user["id"],)))
        base_url = (data.base_url or "").strip()
        same_scope = saved and saved["provider"] == data.provider and saved["base_url"] == base_url
        key = data.api_key.strip() if data.api_key else (saved["api_key"] if same_scope else "")
        settings = dict(provider=data.provider, api_key=key, model=(data.model or "").strip(), base_url=base_url)
        try:
            providers.validate_settings(settings["provider"], settings["model"], settings["base_url"])
            if settings["api_key"]:
                providers.validate_key(settings["api_key"])
        except providers.ProviderError as e:
            raise HTTPException(400, str(e))
        try:
            result = providers.ask_with_provenance(settings, "Reply with just the word: ok", None)
        except providers.ProviderError as e:
            raise HTTPException(502, str(e))
        return {"reply": "Connection responded", "provenance": {k: v for k, v in result.items() if k != "text"}}
    text = _run(db, user, "Reply with just the word: ok", None)
    return {"reply": "Connection responded", "provenance": getattr(text,"provenance",None)}


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
    text = _run(db, user, prompt, SYSTEM)
    return {"text": text, "provenance": getattr(text,"provenance",None)}


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
    ask = data.idea.strip()
    prompt = f"""Draft a proposed recipe for user review. Amounts are estimates, not authoritative
nutrition or medical advice.
{('The main thing: ' + ask + '. This is what they actually want - give them that dish, even if we '
  "don't have everything for it. Don't substitute a different dish just because of what's in stock.")
 if ask else 'Suggest something using what is in stock.'}
{("Where it fits the dish above, lean towards using things that are going off soonest, "
  "but don't change what dish this is just to use them up.") if data.use_expiring else ''}

In stock right now (for reference - it's fine, expected even, if the dish above needs things we
don't have; list them as normal ingredients, don't leave them out or swap them for what we have):
{_kitchen(db)}

Reply with only JSON, nothing before or after, like this:
{{"name": "...", "servings": 2, "ingredients": [{{"name": "...", "amount": 1, "unit": "piece"}}], "instructions": "1. ...\\n2. ..."}}
Use the same ingredient names as the stock list where an ingredient is something we already have.
Units should be simple: piece, g, kg, ml, l, tin, pack."""
    text = _run(db, user, prompt, SYSTEM)
    parsed = _parse_json(text)
    if not parsed or not isinstance(parsed.get("ingredients"), list):
        raise HTTPException(502, "Malformed provider recipe; nothing was saved")

    return {"recipe": review_recipe(db, parsed), "text": None, "provenance": getattr(text,'provenance',None)}


class DiscoveryIn(BaseModel):
    idea: str = Field(min_length=1, max_length=500)
    servings: float = Field(default=1, gt=0, le=1000, allow_inf_nan=False)
    generate: bool = False


def review_recipe(db, parsed, wanted=None):
    def positive(value):
        if isinstance(value,bool) or not isinstance(value,(float,int)) or not math.isfinite(value) or value <= 0 or value > 1000000:
            raise HTTPException(502, 'Suggestion has invalid amounts or servings; nothing was saved')
        return value
    if not isinstance(parsed,dict):
        raise HTTPException(502, 'Invalid suggestion; nothing was saved')
    servings = positive(parsed.get('servings'))
    items = parsed.get('ingredients')
    if not isinstance(items,list) or not 1 <= len(items) <= 50:
        raise HTTPException(502, 'Suggestion needs 1–50 ingredients; nothing was saved')
    units = {}
    for u in rows(db.execute('SELECT * FROM units')):
        for name in (u['name'],u['name_plural']):
            if name: units[name.casefold()] = u['id']
    products = rows(db.execute('SELECT id,name,unit_id FROM products'))
    ingredients = []
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
        have = short = None
        if match:
            try:
                need = st.to_stock_units(db, match, amount, unit_id)
                have = st.in_stock(db, match['id'])
                short = max(0.0, need - have)
            except st.StockError:
                pass  # a unit we can't convert just skips the have/short hint
        ingredients.append(dict(name=name, amount=amount, unit=unit, unit_id=unit_id,
                                product_id=None, product=match['name'] if match else None,
                                suggested_product_id=match['id'] if match else None,
                                have=have, short=short if match else amount,
                                note='' if unit.strip().casefold() in units else 'Proposed unit: ' + unit + '. Choose a valid unit.'))
    return dict(name=str(parsed.get('name') or 'Proposed recipe')[:200], servings=wanted or servings,
                ingredients=ingredients, instructions=str(parsed.get('instructions') or '')[:20000])


class MissingIngredientIn(BaseModel):
    name: str = Field(default="", max_length=200)
    amount: float = Field(gt=0, le=1000000, allow_inf_nan=False)
    unit: str = Field(default="", max_length=80)
    unit_id: int | None = None
    suggested_product_id: int | None = None


class AddMissingIn(BaseModel):
    name: str = Field(default="Suggested recipe", max_length=200)
    ingredients: list[MissingIngredientIn] = Field(default_factory=list, max_length=50)


@router.post("/suggestions/add-missing")
def add_missing_from_suggestion(data: AddMissingIn, db=Depends(get_db), user=Depends(current_user)):
    """Put a suggested recipe's shortfall onto the shopping list, without saving it
    as a recipe first. The amount sent for each line is already net of what's in
    stock (the review screen already worked that out) - this only avoids double
    counting against whatever's already sitting on the shopping list."""
    from .shopping import add_item, listed_amount
    begin(db)
    added = 0
    for item in data.ingredients:
        pid = item.suggested_product_id
        if pid:
            p = row(db.execute("SELECT * FROM products WHERE id = ?", (pid,)))
            if not p:
                pid = None
        if pid:
            try:
                need = st.to_stock_units(db, p, item.amount, item.unit_id)
            except st.StockError:
                continue
            extra = need - listed_amount(db, pid)
            if extra > 1e-9:
                add_item(db, pid, extra, p["unit_id"], user_id=user["id"],
                         source="suggestion", source_label=data.name)
                added += 1
        elif item.name.strip():
            add_item(db, None, item.amount, item.unit_id, name=item.name.strip(),
                     user_id=user["id"], source="suggestion", source_label=data.name)
            added += 1
    db.commit()
    return {"added": added}


@router.post('/suggestions/ingredients')
def discover_ingredients(data: DiscoveryIn, db=Depends(get_db), user=Depends(current_user)):
    from .recipes import check
    query = data.idea.strip().casefold()
    matches = []
    for r in rows(db.execute('SELECT id,name FROM recipes ORDER BY name COLLATE NOCASE')):
        if query in r['name'].casefold() or r['name'].casefold() in query:
            recipe = check(db,r['id'],data.servings)
            recipe['ingredients'] = [dict(i, amount=i['amount_scaled'], unit_id=i['unit_id'] or i['stock_unit_id']) for i in recipe['ingredients']]
            matches.append(recipe)
            if len(matches) == 20: break
    result = dict(matches=matches, recipe=None, available=bool(settings_for(db,user)),
                  message='Search includes saved recipes with no stock. Review ingredients before saving; shopping is a separate action.')
    if data.generate:
        units = ', '.join(u['name'] for u in rows(db.execute('SELECT name FROM units')))
        prompt = f"""Draft a proposed recipe for {data.idea}, for {data.servings} servings.
Do not limit ingredients to current stock. Respect the requested variant (e.g. Vegemite or egg toast).
Amounts are proposed estimates for user review, never established facts or nutrition/medical advice.
Return only JSON: {{"name":"...","servings":{data.servings},"ingredients":[{{"name":"Bread","amount":2,"unit":"slice"}}],"instructions":"..."}}.
Use valid units where possible: {units}. Between 1 and 50 ingredients."""
        text = _run(db,user,prompt,'You draft practical home recipes for explicit human review. '
                    'Name dishes the way a person actually would (chilli con carne, fried rice, '
                    'bolognese) rather than inventing a made-up description, unless there genuinely '
                    "isn't a standard name for it.")
        result['recipe'] = review_recipe(db,_parse_json(text),data.servings)
        result['provenance'] = getattr(text,'provenance',None)
    return result


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
