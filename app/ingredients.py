import json

from functools import wraps
from inspect import signature
from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field


MAX_SERVINGS = 10_000
# way past anything a kitchen holds, but keeps sums and dates from overflowing
MAX_AMOUNT = 1_000_000_000


class IngredientIn(BaseModel):
    product_id: Optional[int] = Field(default=None, gt=0)
    name: str = Field(default='', max_length=200)
    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    unit_id: Optional[int] = Field(default=None, gt=0)
    note: str = ''


def normalized_name(name):
    return ' '.join(name.split()).casefold()


def atomic_save(fn):
    @wraps(fn)
    def save(*args, **kwargs):
        bound = signature(fn).bind(*args, **kwargs).arguments
        db = bound['db']
        data = bound['data']
        token = getattr(data, 'request_id', None)
        user_id = bound['user']['id']
        payload = json.dumps({'data': data.model_dump(), 'id': bound.get('meal_id', bound.get('recipe_id'))}, sort_keys=True)
        try:
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            if token:
                previous = db.execute('SELECT payload,response FROM planning_requests WHERE user_id=? AND kind=? AND request_id=?',
                                      (user_id, fn.__name__, token)).fetchone()
                if previous:
                    if previous['payload'] != payload:
                        raise HTTPException(409, 'This save was already completed. Reopen the form to make another change.')
                    db.commit()
                    return json.loads(previous['response'])
            result = fn(*args, **kwargs)
            if token:
                db.execute('INSERT INTO planning_requests(user_id,kind,request_id,payload,response) VALUES(?,?,?,?,?)',
                           (user_id, fn.__name__, token, payload, json.dumps(result)))
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
    return save


def resolve_ingredients(db, ingredients):
    resolved = []
    created = {}
    for i in ingredients:
        if i.unit_id is not None and not db.execute('SELECT id FROM units WHERE id=?', (i.unit_id,)).fetchone():
            raise HTTPException(400, 'Ingredient unit not found')
        pid = i.product_id
        if pid:
            if not db.execute('SELECT id FROM products WHERE id=?', (pid,)).fetchone():
                raise HTTPException(400, 'Ingredient product not found')
        else:
            name = ' '.join(i.name.split())
            key = normalized_name(name)
            if not key or i.unit_id is None:
                raise HTTPException(400, 'A new ingredient needs a name and stock unit')
            if key in created:
                pid, unit = created[key]
                if unit != i.unit_id:
                    raise HTTPException(400, 'Use the same stock unit for a new ingredient')
            else:
                matches = [p for p in db.execute('SELECT id,name FROM products') if normalized_name(p['name']) == key]
                if matches:
                    raise HTTPException(409, f'{name} is already in the catalogue. Choose the existing product explicitly.')
                pid = db.execute('INSERT INTO products(name,unit_id) VALUES(?,?)', (name,i.unit_id)).lastrowid
                created[key] = (pid, i.unit_id)
        resolved.append((pid, i.amount, i.unit_id, i.note))
    return resolved


def save_ingredients(db, recipe_id, ingredients):
    resolved = resolve_ingredients(db, ingredients)
    db.execute('DELETE FROM recipe_ingredients WHERE recipe_id=?', (recipe_id,))
    db.executemany('INSERT INTO recipe_ingredients(recipe_id,product_id,amount,unit_id,note) VALUES(?,?,?,?,?)',
                   [(recipe_id, *i) for i in resolved])
