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
from .mealplan import PORTION_FACTOR, SLOTS, _day
from ..ingredients import MAX_SERVINGS
from .recipes import list_recipes

router = APIRouter(prefix="/api", tags=["suggestions"])


class SettingsIn(BaseModel):
    provider: str = Field(default="anthropic", max_length=120)
    api_key: str | None = Field(default=None, max_length=1000)  # None = keep the one that's saved
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=500)
    shared: bool = False


MAX_IDEA_CELLS = 60


class MealIdeaCell(BaseModel):
    day: str
    slot: str


class MealIdeasIn(BaseModel):
    cells: list[MealIdeaCell] = Field(default_factory=list, max_length=500)
    note: str = Field(default="", max_length=2000)
    mode: Literal["all", "stock", "few"] = "all"


class RecipeIdeaIn(BaseModel):
    idea: str = Field(default="", max_length=2000)
    use_expiring: bool = True
    mode: Literal["all", "stock", "few"] = "all"


def _mode_text(mode):
    """How free the suggestion is to need a shopping trip."""
    if mode == "stock":
        return ("Only suggest something fully makeable from what's in stock right now - every "
                "ingredient has to already be on hand. If nothing good is genuinely possible from "
                "stock alone, say so honestly in the name/instructions rather than forcing a bad "
                "combination together just to avoid buying anything.")
    if mode == "few":
        return ("Lean toward using what's already in stock, but it's fine to need a few extra "
                "things if that makes for a genuinely better meal - don't force a worse combination "
                "just to avoid buying one or two items.")
    return ""


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
            SELECT p.name, u.name AS unit, SUM(s.amount) AS amount, MIN(s.expires_on) AS exp, p.shelf_life_kind,
                   GROUP_CONCAT(DISTINCT l.name) AS locations
            FROM stock s JOIN products p ON p.id = s.product_id JOIN units u ON u.id = p.unit_id
            LEFT JOIN locations l ON l.id = s.location_id
            GROUP BY p.id ORDER BY exp IS NULL, exp""")):
        line = f"- {s['name']}: {round(s['amount'], 2):g} {s['unit']}"
        if s["locations"]:
            line += f" [{s['locations']}]"
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


DIET_TAGS = [
    "Vegetarian", "Vegan", "Pescatarian", "No seafood", "No pork", "No red meat",
    "No shellfish", "Halal", "Kosher", "Gluten-free", "Dairy-free", "Lactose-free",
    "Nut-free", "Egg-free", "Low-carb", "Keto", "No added sugar",
    "Gastric sleeve", "Gastric bypass", "High protein", "Low sugar",
]

# Medical/dietary tags that need spelling out in plain cooking terms, not just a label -
# a model won't reliably turn "gastric sleeve" into the right portion/texture advice on its own.
DIET_GUIDANCE = {
    "Gastric sleeve": "a sleeve patient can only manage a small portion, well under a normal "
        "plate - protein first, then veg, carbs last and often skipped. Low sugar, no fizzy "
        "drinks. Avoid dry, fibrous or doughy textures that are hard to get down.",
    "Gastric bypass": "a bypass patient can only manage a small portion, well under a normal "
        "plate - protein first. Low sugar and not too rich/fatty - sugary or fatty food in any "
        "real amount risks dumping syndrome. No fizzy drinks.",
    "High protein": "lean toward protein-dense meals and bulk them up with extra meat, eggs, "
        "beans or dairy where it makes sense.",
    "Low sugar": "avoid added sugar and very sweet dishes or sauces.",
}


def _preferences(db):
    p = row(db.execute("SELECT * FROM household_preferences WHERE id = 1"))
    return dict(diet=json.loads(p["diet"]) if p else [], dislikes=json.loads(p["dislikes"]) if p else [],
                other=p["other"] if p else "")


def _preferences_text(db):
    """Household diet/taste rules, written out for the prompt. Empty when nothing is set."""
    p = _preferences(db)
    lines = []
    if p["diet"]:
        lines.append("Dietary requirements (hard rules, never break these): " + ", ".join(p["diet"]) + ".")
        for tag in p["diet"]:
            if tag in DIET_GUIDANCE:
                lines.append(f"For \"{tag}\": " + DIET_GUIDANCE[tag])
    if p["dislikes"]:
        lines.append("Avoid these ingredients/dishes entirely: " + ", ".join(p["dislikes"]) + ".")
    if p["other"].strip():
        lines.append("Other household notes: " + p["other"].strip())
    return "\n".join(lines)


SYSTEM = ("You help a household plan good meals they'll actually enjoy cooking and eating. "
          "That is the goal - a genuinely good meal, not a puzzle of using up whatever's in the "
          "cupboard. Buying extra ingredients for a good meal is completely normal and expected; "
          "never force two unrelated things together, or pick a worse meal, just because the "
          "pieces happen to be in stock. If something in stock is about to go off and it genuinely "
          "suits a good meal, that's a nice bonus - mention it, but don't let it drive the choice. "
          "Keep it practical and short, home cooking, no fancy equipment. Respect portion sizes: "
          "'small' means about half a normal portion and the person really can't manage more, "
          "'large' means a big appetite. Name dishes the way a person actually would "
          "- 'chilli con carne', 'spaghetti bolognese', 'fried rice' - not a made-up "
          "description like 'Beef and Bean Skillet'. Only invent a name when there "
          "genuinely isn't a standard one for what you're describing. Stock is tagged "
          "with where it lives, e.g. '[Freezer]'. Anything in the freezer is frozen, "
          "and if the name says crumbed, battered, tenders, nuggets or schnitzel it's a "
          "pre-cooked or coated product meant to go in an oven, air fryer or frying pan "
          "per its packet - never boiled, never added raw to a soup or stew. If a stock "
          "name doesn't tell you how it's meant to be cooked, cook it the ordinary way "
          "that kind of food is actually cooked at home, not a method that happens to "
          "fit the recipe you're writing. If stock already has a packet, sachet or jar "
          "that does the whole job - a taco seasoning, curry paste, a 'Chilli Con Carne' "
          "or similar named mix - use just that one product for the seasoning, exactly "
          "as a person would cook from the packet. Don't also add separate raw spices "
          "(chilli powder, paprika, garlic powder, cumin and so on) on top of it; that mix "
          "already contains them. Only add raw spices to a dish that has no ready-made mix "
          "for it, or when the mix needs a single genuine thing a packet always calls for "
          "(like extra garlic or onion). If two different complete mixes could both make "
          "the same dish, pick the one whose name matches the dish, not both.")


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


class PreferencesIn(BaseModel):
    diet: list[str] = Field(default_factory=list, max_length=30)
    dislikes: list[str] = Field(default_factory=list, max_length=50)
    other: str = Field(default="", max_length=2000)


@router.get("/household/preferences")
def get_preferences(db=Depends(get_db), user=Depends(current_user)):
    p = _preferences(db)
    p["diet_tags"] = DIET_TAGS
    return p


@router.put("/household/preferences")
def save_preferences(data: PreferencesIn, db=Depends(get_db), user=Depends(current_user)):
    diet = [t.strip() for t in data.diet if t.strip()][:30]
    dislikes = [t.strip()[:60] for t in data.dislikes if t.strip()][:50]
    db.execute("""INSERT INTO household_preferences (id, diet, dislikes, other) VALUES (1, ?, ?, ?)
                  ON CONFLICT(id) DO UPDATE SET diet = excluded.diet, dislikes = excluded.dislikes, other = excluded.other""",
               (json.dumps(diet), json.dumps(dislikes), data.other.strip()))
    db.commit()
    return get_preferences(db, user)


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


def _grouped_cells(cells):
    """Cells grouped by day, in slot order, for a readable prompt - e.g.
    'Tue 20 Oct: lunch, dinner'."""
    by_day = {}
    for c in cells:
        by_day.setdefault(c.day, set()).add(c.slot)
    lines = []
    for day in sorted(by_day):
        wanted = [s for s in SLOTS if s in by_day[day]]
        lines.append(f"{date.fromisoformat(day).strftime('%a %d %b')}: {', '.join(wanted)}")
    return "\n".join(lines)


@router.post("/suggestions/meals")
def meal_ideas(data: MealIdeasIn, db=Depends(get_db), user=Depends(current_user)):
    if not data.cells:
        raise HTTPException(400, "Pick at least one day and meal to get ideas for")
    if len(data.cells) > MAX_IDEA_CELLS:
        raise HTTPException(400, f"That's too many day/meal combinations at once - stick to {MAX_IDEA_CELLS} or fewer")
    for c in data.cells:
        _day(c.day)
        if c.slot not in SLOTS:
            raise HTTPException(400, f"'{c.slot}' isn't a meal slot")
    recipes = list_recipes(db, user)
    known = "\n".join(f"- {r['name']}" + (" (everything in stock)" if r["can_make"] else f" ({r['missing_count']} things missing)")
                      + (f" - rated {r['rating']}/5" if r["rating"] else "")
                      for r in recipes) or "(none saved yet)"
    me = f"{user['username']}, who likes {user['portion_size']} portions (about {PORTION_FACTOR[user['portion_size']]}x normal)"
    prompt = f"""Suggest meals for exactly these days and meals:
{_grouped_cells(data.cells)}

Asked by {me}.

People in the house:
{_people(db)}

{_preferences_text(db)}

In stock right now:
{_kitchen(db)}

Recipes we already have saved:
{known}

{('Extra wishes: ' + data.note) if data.note.strip() else ''}

Use saved recipes where they genuinely fit, especially highly-rated ones - lean away from
anything rated 1-2 unless nothing else works. For each meal give the name, roughly what goes
in it and how much to make for the portion sizes above. Pick good, properly varied dinners
first - don't just reshuffle what's already in stock into odd combinations. Buying extra
ingredients is completely normal; point out anything we'd need to buy, don't avoid suggesting it.
{_mode_text(data.mode)}
Reply with only JSON, nothing before or after:
{{"meals": [{{"day": "2026-10-20", "slot": "dinner", "name": "...", "servings": 2,
"ingredients": [{{"name": "...", "amount": 1, "unit": "piece"}}], "instructions": "1. ..."}}]}}
Return one meal per requested cell, using these exact day/slot pairs:
{json.dumps([c.model_dump() for c in data.cells])}
Use the stock ingredient names where possible. Units: piece, g, kg, ml, l, tin, pack.
Give 1–50 ingredients per meal."""
    text = _run(db, user, prompt, SYSTEM)
    parsed = _parse_json(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("meals"), list) or len(parsed["meals"]) > MAX_IDEA_CELLS:
        raise HTTPException(502, "Malformed provider meals; nothing was saved")
    requested = {(c.day, c.slot) for c in data.cells}
    seen, meals = set(), []
    for item in parsed["meals"]:
        if not isinstance(item, dict) or not isinstance(item.get("day"), str) or not isinstance(item.get("slot"), str):
            raise HTTPException(502, "Invalid suggested day or meal slot; nothing was saved")
        cell = (item["day"], item["slot"])
        if cell not in requested or cell in seen:
            raise HTTPException(502, "Suggested meals must use each requested day and slot at most once; nothing was saved")
        if not isinstance(item.get("name"), str) or not 1 <= len(item["name"].strip()) <= 200 or not isinstance(item.get("instructions"), str):
            raise HTTPException(502, "Invalid suggested meal name or instructions; nothing was saved")
        meal = review_recipe(db, item)
        if meal["servings"] > MAX_SERVINGS:
            raise HTTPException(502, "Suggested meal has too many servings; nothing was saved")
        meals.append(dict(meal, day=cell[0], slot=cell[1]))
        seen.add(cell)
    order = lambda cell: (cell[0], SLOTS.index(cell[1]))
    meals.sort(key=lambda meal: order((meal["day"], meal["slot"])))
    missing = [dict(day=d, slot=s) for d, s in sorted(requested - seen, key=order)]
    return {"meals": meals, "missing_cells": missing, "provenance": getattr(text, "provenance", None)}


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
    mode_line = _mode_text(data.mode)
    recent = [r["name"] for r in rows(db.execute(
        "SELECT name FROM recent_suggestions WHERE user_id = ? ORDER BY id DESC LIMIT 5", (user["id"],)))]
    prompt = f"""Draft a proposed recipe for user review. Amounts are estimates, not authoritative
nutrition or medical advice.
{(('The main thing: ' + ask + '. This is what they actually want - give them that dish, even if we '
   "don't have everything for it. Don't substitute a different dish just because of what's in stock."
   + (' ' + mode_line if mode_line else ''))
 if ask else (mode_line or 'Suggest one genuinely good meal. Using stock is a bonus, not a requirement - '
 "pick whatever's actually a good meal and say what needs buying, don't settle for a worse combination just because the pieces are in stock."))}
{("Where it fits the dish above, lean towards using things that are going off soonest, "
  "but don't change what dish this is just to use them up.") if data.use_expiring else ''}
{('Already suggested recently, so pick something different this time unless the request above '
  'genuinely only fits one of these: ' + ', '.join(recent) + '.') if recent and not ask else ''}
{_preferences_text(db)}

In stock right now (for reference - it's fine, expected even, if the dish above needs things we
don't have; list them as normal ingredients, don't leave them out or swap them for what we have):
{_kitchen(db)}

Reply with only JSON, nothing before or after, like this:
{{"name": "...", "servings": 2, "ingredients": [{{"name": "...", "amount": 1, "unit": "piece"}}], "instructions": "1. ...\\n2. ..."}}
Use the same ingredient names as the stock list where an ingredient is something we already have.
Units should be simple: piece, g, kg, ml, l, tin, pack."""
    text = _run(db, user, prompt, SYSTEM)
    parsed = _parse_json(text)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("ingredients"), list):
        raise HTTPException(502, "Malformed provider recipe; nothing was saved")

    recipe = review_recipe(db, parsed)
    db.execute("INSERT INTO recent_suggestions (user_id, name) VALUES (?, ?)", (user["id"], recipe["name"]))
    db.execute("""DELETE FROM recent_suggestions WHERE user_id = ? AND id NOT IN
                  (SELECT id FROM recent_suggestions WHERE user_id = ? ORDER BY id DESC LIMIT 10)""",
               (user["id"], user["id"]))
    db.commit()
    return {"recipe": recipe, "text": None, "provenance": getattr(text,'provenance',None)}


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
                                suggested_product_id=match['id'] if match else None,
                                have=have, short=short if match else amount,
                                short_unit=short_unit if match else unit,
                                short_unit_id=short_unit_id if match else unit_id,
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
    from .shopping import _check_unit, add_item, listed_amount
    for item in data.ingredients:
        _check_unit(db, item.unit_id)
    begin(db)
    added = 0
    totals = {}
    for item in data.ingredients:
        pid = item.suggested_product_id
        if pid:
            p = row(db.execute("SELECT * FROM products WHERE id = ?", (pid,)))
            if not p:
                pid = None
        if pid:
            if item.unit_id is None and item.unit.strip():
                db.rollback()
                raise HTTPException(400, "Choose a valid unit before adding this ingredient")
            try:
                need = st.to_stock_units(db, p, item.amount, item.unit_id)
            except st.StockError:
                db.rollback()
                raise HTTPException(400, "Choose a unit that converts to this product's stock unit")
            key = (pid, p["unit_id"], "", "")
            totals[key] = totals.get(key, 0) + need
        elif item.name.strip():
            note = "Proposed unit: " + item.unit.strip() if item.unit.strip() and item.unit_id is None else ""
            key = (None, item.unit_id, item.name.strip(), note)
            totals[key] = totals.get(key, 0) + item.amount
    for (pid, unit_id, name, note), need in totals.items():
        if pid:
            extra = need - listed_amount(db, pid)
            if extra > 1e-9:
                add_item(db, pid, extra, unit_id, user_id=user["id"],
                         source="suggestion", source_label=data.name)
                added += 1
        else:
            listed = sum(i["amount"] for i in rows(db.execute(
                "SELECT name, amount FROM shopping_list WHERE product_id IS NULL AND done=0 AND unit_id IS ? AND note=?",
                (unit_id, note))) if i["name"].casefold() == name.casefold())
            extra = need - listed
            if extra > 1e-9:
                add_item(db, None, extra, unit_id, name=name, note=note,
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
{_preferences_text(db)}
Return only JSON: {{"name":"...","servings":{data.servings},"ingredients":[{{"name":"Bread","amount":2,"unit":"slice"}}],"instructions":"..."}}.
Use valid units where possible: {units}. Between 1 and 50 ingredients."""
        text = _run(db,user,prompt,'You draft practical home recipes for explicit human review. '
                    'Name dishes the way a person actually would (chilli con carne, fried rice, '
                    'bolognese) rather than inventing a made-up description, unless there genuinely '
                    "isn't a standard name for it. If an ingredient is normally sold frozen, crumbed, "
                    "battered or pre-cooked (tenders, nuggets, schnitzel), cook it the way the packet "
                    "actually says - oven, air fryer or pan - never boiled or added raw to a soup or stew.")
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
