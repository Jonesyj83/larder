"""Stdio tools for the Larder REST API."""
import os
from datetime import date, timedelta

import httpx

try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as exc:
    if exc.name != 'mcp.server.fastmcp':
        raise
    from mcp.server.mcpserver import MCPServer as FastMCP

mcp = FastMCP('Larder')


def config():
    url = os.environ.get('LARDER_URL', '').strip().rstrip('/')
    key = os.environ.get('LARDER_API_KEY', '').strip()
    if not url or not key:
        raise ValueError('Set LARDER_URL and LARDER_API_KEY')
    parsed = httpx.URL(url)
    if parsed.scheme not in ('http', 'https') or not parsed.host or parsed.userinfo or parsed.query or parsed.fragment:
        raise ValueError('LARDER_URL must be an HTTP URL without credentials, query or fragment')
    return url, key


async def request(method, path, *, params=None, body=None):
    url, key = config()
    async with httpx.AsyncClient(headers={'x-api-key': key}, timeout=30, follow_redirects=False) as client:
        response = await client.request(method, url + path, params=params, json=body)
    if not response.is_success:
        try:
            detail = response.json().get('detail', 'Request failed')
        except (ValueError, AttributeError):
            detail = 'Request failed'
        raise ValueError(f'Larder returned {response.status_code}: {detail}'.replace(key, '[redacted]'))
    return response.json()


async def named_id(path, name, *, plural=False):
    entries = await request('GET', path)
    wanted = name.strip().casefold()
    matches = [item for item in entries if wanted == item['name'].strip().casefold()
               or (plural and wanted == item.get('name_plural', '').strip().casefold())]
    if len(matches) != 1:
        raise ValueError(f'Choose one existing name from {path}: {name}')
    return matches[0]['id']


async def unit_id(unit):
    return await named_id('/api/units', unit, plural=True) if unit is not None else None


async def location_id(location):
    return await named_id('/api/locations', location) if location is not None else None


async def product_named(name, *, required=True):
    products = await search_products(name.strip())
    matches = [p for p in products if p['name'].strip().casefold() == name.strip().casefold()]
    if len(matches) > 1:
        raise ValueError(f'More than one product is named {name}')
    if matches:
        return matches[0]
    if required:
        raise ValueError(f'Product not found: {name}')
    return None


@mcp.tool()
async def get_stock(location: str | None = None):
    """List stock, optionally restricted to an existing location name."""
    lid = await location_id(location)
    return await request('GET', '/api/stock', params={'location_id': lid} if lid is not None else {})


@mcp.tool()
async def get_expiring(days: int = 3):
    """List stock expiring within the given number of days."""
    return await request('GET', '/api/stock/expiring', params={'days': days})


@mcp.tool()
async def search_products(q: str):
    """Search the product catalogue by name."""
    return await request('GET', '/api/products', params={'q': q})


@mcp.tool()
async def create_product(name: str, unit: str, location: str | None = None,
                         shelf_life_kind: str = 'best_by', default_days: int | None = None):
    """Create a catalogue product using an existing unit and optional location name."""
    return await request('POST', '/api/products', body={
        'name': name, 'unit_id': await unit_id(unit), 'location_id': await location_id(location),
        'shelf_life_kind': shelf_life_kind, 'default_days': default_days})


@mcp.tool()
async def add_stock(product_name: str, amount: float, unit: str | None = None,
                    location: str | None = None, price: float | None = None, expires_on: str | None = None):
    """Record a purchase. Price is the total purchase cost; expiry is YYYY-MM-DD.

    Existing products default to their stock unit. A new product requires an explicit existing unit.
    """
    uid, lid = await unit_id(unit), await location_id(location)
    product = await product_named(product_name, required=False)
    if product is None:
        if unit is None:
            raise ValueError('Pass a unit when adding a new product')
        product = await create_product(product_name, unit, location)
    return await request('POST', '/api/stock/purchase', body={
        'product_id': product['id'], 'amount': amount, 'unit_id': uid, 'location_id': lid,
        'price': price, 'expires_on': expires_on})


@mcp.tool()
async def consume_stock(product_name: str, amount: float, unit: str | None = None, spoiled: bool = False):
    """Consume stock by exact product name, optionally recording spoilage."""
    product = await product_named(product_name)
    return await request('POST', '/api/stock/consume', body={
        'product_id': product['id'], 'amount': amount, 'unit_id': await unit_id(unit), 'spoiled': spoiled})


@mcp.tool()
async def get_shopping_list():
    """Read the shopping list."""
    return await request('GET', '/api/shopping')


@mcp.tool()
async def add_shopping_item(name: str, amount: float | None = None, unit: str | None = None, note: str = ''):
    """Add a catalogue product or a freeform item. Omitted amount defaults to one."""
    product = await product_named(name, required=False)
    return await request('POST', '/api/shopping', body={
        'product_id': product['id'] if product else None, 'name': name,
        'amount': amount if amount is not None else 1, 'unit_id': await unit_id(unit), 'note': note})


@mcp.tool()
async def mark_shopping_bought(item_id: int):
    """Mark a shopping item bought and record stock for linked products."""
    return await request('POST', f'/api/shopping/{item_id}/bought', body={})


@mcp.tool()
async def get_recipes(q: str | None = None):
    """List saved recipes, optionally filtering their names."""
    recipes = await request('GET', '/api/recipes')
    return [r for r in recipes if q.casefold() in r['name'].casefold()] if q else recipes


@mcp.tool()
async def get_recipe(recipe_id: int):
    """Read a saved recipe with its ingredients and stock needs."""
    return await request('GET', f'/api/recipes/{recipe_id}')


@mcp.tool()
async def save_recipe(name: str, servings: float, ingredients: list[dict], instructions: str = ''):
    """Save a recipe. Ingredients use amount, product_id or name, unit_id, and optional note.

    A textual unit may replace unit_id; it must match an existing unit name.
    A name without product_id creates a catalogue ingredient without purchasing stock.
    """
    prepared = []
    for ingredient in ingredients:
        item = dict(ingredient)
        if 'unit' in item:
            resolved = await unit_id(item.pop('unit'))
            if item.get('unit_id') is not None and item['unit_id'] != resolved:
                raise ValueError('Ingredient unit and unit_id disagree')
            item['unit_id'] = resolved
        prepared.append(item)
    return await request('POST', '/api/recipes', body={
        'name': name, 'servings': servings, 'ingredients': prepared, 'instructions': instructions})


@mcp.tool()
async def rate_recipe(recipe_id: int, rating: int):
    """Set a saved recipe's rating from zero to five."""
    return await request('PUT', f'/api/recipes/{recipe_id}/rating', body={'rating': rating})


@mcp.tool()
async def get_meal_plan(start_day: str, end_day: str):
    """Read an inclusive date range (YYYY-MM-DD)."""
    first, last = date.fromisoformat(start_day), date.fromisoformat(end_day)
    if last < first:
        raise ValueError('end_day must be on or after start_day')
    meals, slots = [], []
    cursor = first
    while cursor <= last:
        days = min((last - cursor).days + 1, 42)
        result = await request('GET', '/api/mealplan', params={'start': cursor.isoformat(), 'days': days})
        meals.extend(result['meals'])
        slots = result['slots']
        cursor += timedelta(days=days)
    return {'start': first.isoformat(), 'end': last.isoformat(), 'slots': slots, 'meals': meals}


@mcp.tool()
async def save_meal(day: str, slot: str, recipe_id: int, servings: float | None = None, note: str = ''):
    """Add a recipe to a meal-plan slot on a YYYY-MM-DD date."""
    return await request('POST', '/api/mealplan', body={
        'day': day, 'slot': slot, 'recipe_id': recipe_id, 'servings': servings, 'note': note})


@mcp.tool()
async def remember_food_note(kind: str, content: str, source: str = 'chat'):
    """Save a household note: like, dislike, maybe, condition, habit, buying_pattern, recipe_note or cost_note."""
    return await request('POST', '/api/memory', body={'kind': kind, 'content': content, 'source': source})


@mcp.tool()
async def recall_food_notes(query: str | None = None, kind: str | None = None):
    """Recall active household notes, optionally using FTS search and a kind filter."""
    return await request('GET', '/api/memory', params={k: v for k, v in {'q': query, 'kind': kind}.items() if v is not None})


@mcp.tool()
async def forget_food_note(note_id: int):
    """Permanently delete a household note."""
    return await request('DELETE', f'/api/memory/{note_id}')


@mcp.tool()
async def get_price_history(product_id: int):
    """Read the permanent purchase ledger for a product, oldest first."""
    return await request('GET', f'/api/stock/price-history/{product_id}')


@mcp.tool()
async def get_spending_summary(days: int = 30):
    """Read purchase spending totals by product for the given number of days."""
    return await request('GET', '/api/stock/spending', params={'days': days})


if __name__ == '__main__':
    config()
    mcp.run(transport='stdio')
