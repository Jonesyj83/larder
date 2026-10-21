# Manual smoke test only: requires a live Larder instance; excluded from the main test suite.
import asyncio
from datetime import date
from uuid import uuid4

import server


async def main():
    server.config()
    await server.get_stock()
    await server.get_expiring()
    products = await server.search_products('')
    await server.get_shopping_list()
    recipes = await server.get_recipes()
    if recipes:
        await server.get_recipe(recipes[0]['id'])
    if products:
        await server.get_price_history(products[0]['id'])
    today = date.today().isoformat()
    await server.get_meal_plan(today, today)
    await server.get_spending_summary()
    token = 'smoketest' + uuid4().hex
    note = await server.remember_food_note('habit', token, 'smoke-test')
    try:
        recalled = await server.recall_food_notes(token, 'habit')
        assert any(item['id'] == note['id'] for item in recalled)
    finally:
        await server.forget_food_note(note['id'])
    print('Smoke test passed')


if __name__ == '__main__':
    asyncio.run(main())
