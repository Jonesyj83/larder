"""Edge cases and abuse cases: bad numbers, races, unit maths and
picture address checks. Scratch SQLite only (memory or a temp dir), no network."""
import json
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from app import db as dbmodule
from app import netcheck, pictures
from app import stock as st
from app.db import BUILTIN_CONVERSIONS, SCHEMA

ROOT = Path(__file__).resolve().parents[1]
USER = {'id': 1, 'username': 'test', 'portion_size': 'regular'}
UNITS = ['piece', 'pack', 'g', 'kg', 'oz', 'lb', 'ml', 'l', 'fl oz (US)']


def prepare(db):
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    db.execute("INSERT INTO shopping_lists(id,name,kind) VALUES(1,'Shopping','order')")
    db.execute("INSERT INTO users(id,username,password_hash,portion_size) VALUES(1,'test','unused','regular')")
    db.execute("INSERT INTO users(id,username,password_hash,portion_size) VALUES(2,'small','unused','small')")
    db.execute("INSERT INTO users(id,username,password_hash,portion_size) VALUES(3,'large','unused','large')")
    db.executemany('INSERT INTO units(id,name) VALUES(?,?)', list(enumerate(UNITS, 1)))
    ids = unit_ids()
    rules = BUILTIN_CONVERSIONS + [('kg', 'g', 1000), ('l', 'ml', 1000)]
    db.executemany('INSERT OR IGNORE INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(?,?,?)',
                   [(ids[a], ids[b], f) for a, b, f in rules])
    db.execute("INSERT INTO locations(id,name) VALUES(1,'Pantry')")
    db.commit()
    return db


def unit_ids():
    return {name: i for i, name in enumerate(UNITS, 1)}


def product(db, name, unit, purchase_unit=None, factor=1, **extra):
    ids = unit_ids()
    cols = dict(name=name, unit_id=ids[unit], purchase_unit_id=ids.get(purchase_unit), purchase_factor=factor, **extra)
    cur = db.execute(f"INSERT INTO products({','.join(cols)}) VALUES({','.join('?' * len(cols))})", list(cols.values()))
    db.commit()
    return cur.lastrowid


def count(db, table, where='1=1', args=()):
    return db.execute(f'SELECT COUNT(*) FROM {table} WHERE {where}', args).fetchone()[0]


class MemoryDB(unittest.TestCase):
    def setUp(self):
        self.db = prepare(sqlite3.connect(':memory:'))
        self.ids = unit_ids()

    def tearDown(self):
        self.db.close()


class StockInput(MemoryDB):
    def test_bad_amounts_never_reach_the_database(self):
        from app.routes.stock import ConsumeIn, EntryEdit, PurchaseIn
        pid = product(self.db, 'Rice', 'g')
        for bad in (0, -1, float('nan'), float('inf'), float('-inf')):
            with self.subTest(amount=bad):
                with self.assertRaises(st.StockError):
                    st.purchase(self.db, pid, bad)
                self.db.rollback()
                with self.assertRaises(st.StockError):
                    st.consume(self.db, pid, bad)
                self.db.rollback()
                with self.assertRaises(ValidationError):
                    PurchaseIn(product_id=pid, amount=bad)
                with self.assertRaises(ValidationError):
                    ConsumeIn(product_id=pid, amount=bad)
        for bad in (1e12, 1e309):
            with self.assertRaises(ValidationError):
                PurchaseIn(product_id=pid, amount=bad)
        for price in (-0.01, float('nan'), float('inf')):
            with self.assertRaises(ValidationError):
                PurchaseIn(product_id=pid, amount=1, price=price)
        with self.assertRaises(st.StockError):
            st.purchase(self.db, pid, 1, price=-5)
        self.db.rollback()
        with self.assertRaises(ValidationError):
            EntryEdit(amount=-1)
        self.assertEqual(EntryEdit(amount=0).amount, 0)
        self.assertEqual((count(self.db, 'stock'), count(self.db, 'stock_log')), (0, 0))

    def test_unknown_location_is_a_400_not_a_crash(self):
        from app.routes.stock import EntryEdit, PurchaseIn, edit_entry, purchase
        pid = product(self.db, 'Rice', 'g')
        with self.assertRaises(HTTPException) as e:
            purchase(PurchaseIn(product_id=pid, amount=1, location_id=99), self.db, USER)
        self.assertEqual(e.exception.status_code, 400)
        self.assertEqual(count(self.db, 'stock'), 0)
        stock_id = st.purchase(self.db, pid, 5, location_id=1)['stock_id']
        with self.assertRaises(HTTPException) as e:
            edit_entry(stock_id, EntryEdit(amount=3, location_id=99), self.db, USER)
        self.assertEqual(e.exception.status_code, 400)
        self.assertEqual(st.in_stock(self.db, pid), 5)

    def test_editing_an_entry_to_zero_removes_and_logs_it(self):
        from app.routes.stock import EntryEdit, edit_entry
        pid = product(self.db, 'Rice', 'g')
        stock_id = st.purchase(self.db, pid, 5)['stock_id']
        edit_entry(stock_id, EntryEdit(amount=0), self.db, USER)
        self.assertEqual(count(self.db, 'stock'), 0)
        self.assertEqual(self.db.execute("SELECT amount FROM stock_log WHERE kind='adjust'").fetchone()[0], -5)

    def test_shelf_life_bounds(self):
        from app.routes.products import ProductIn
        for days in (-1, 36501, 10 ** 12):
            with self.assertRaises(ValidationError):
                ProductIn(name='Milk', unit_id=1, default_days=days)
        # a silly value left over in an old database doesn't break buying it
        pid = product(self.db, 'Old', 'piece', default_days=10 ** 9)
        self.assertEqual(st.purchase(self.db, pid, 1)['added'], 1)
        self.assertIsNone(self.db.execute('SELECT expires_on FROM stock').fetchone()[0])

    def test_zero_day_shelf_life_means_today(self):
        from datetime import date
        pid = product(self.db, 'Eat today', 'piece', default_days=0)
        st.purchase(self.db, pid, 1)
        self.assertEqual(self.db.execute('SELECT expires_on FROM stock').fetchone()[0], date.today().isoformat())

    def test_query_bounds(self):
        from app.routes.stock import expiring, log
        pid = product(self.db, 'Rice', 'g')
        for _ in range(5):
            st.purchase(self.db, pid, 1)
        # sqlite reads LIMIT -1 as "everything"
        self.assertEqual(len(log(limit=-1, product_id=None, db=self.db, user=USER)), 1)
        self.assertEqual(len(log(limit=10 ** 6, product_id=None, db=self.db, user=USER)), 5)
        expiring(days=10 ** 12, db=self.db, user=USER)
        expiring(days=-10 ** 12, db=self.db, user=USER)

    def test_expiring_entries_keep_product_picture_and_plural(self):
        from datetime import date
        from app.routes.stock import expiring
        pid = product(self.db, 'Eggs', 'piece', picture='eggs.jpg')
        self.db.execute("UPDATE units SET name_plural='pieces' WHERE name='piece'")
        self.db.commit()
        st.purchase(self.db, pid, 6, expires_on=date.today().isoformat())
        item = expiring(db=self.db, user=USER)[0]
        self.assertEqual((item['picture'], item['unit_plural']), ('eggs.jpg', 'pieces'))

    def test_shopping_list_validation(self):
        from app.routes.shopping import ItemEdit, ItemIn, ListIn, add, create_list, edit
        for bad in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValidationError):
                ItemIn(name='Milk', amount=bad)
            with self.assertRaises(ValidationError):
                ItemEdit(amount=bad)
        with self.assertRaises(HTTPException) as e:
            add(ItemIn(name='Milk', unit_id=999), self.db, USER)
        self.assertEqual(e.exception.status_code, 400)
        plain = create_list(ListIn(name='Errand list'), self.db, USER)['id']
        item = add(ItemIn(name='Milk'), self.db, USER, list_id=plain)
        with self.assertRaises(HTTPException):
            edit(item['id'], ItemEdit(name='   '), self.db, USER, list_id=plain)
        with self.assertRaises(HTTPException):
            edit(item['id'], ItemEdit(unit_id=999), self.db, USER, list_id=plain)
        self.assertEqual(add(ItemIn(name='日本茶 🍵'), self.db, USER, list_id=plain)['label'], '日本茶 🍵')

    def test_duplicate_barcodes(self):
        from app.routes.products import BarcodeIn, ProductIn, add_barcode, add_product
        first = add_product(ProductIn(name='Beans', unit_id=1, barcodes=['5000157024671']), self.db)
        with self.assertRaises(HTTPException) as e:
            add_product(ProductIn(name='Other beans', unit_id=1, barcodes=['5000157024671']), self.db)
        self.assertIn('Beans', e.exception.detail)
        self.assertEqual(count(self.db, 'products', 'name=?', ('Other beans',)), 0)
        twice = add_product(ProductIn(name='Twice', unit_id=1, barcodes=['111', ' 111 ', '']), self.db)
        self.assertEqual([b['code'] for b in twice['barcodes']], ['111'])
        with self.assertRaises(HTTPException):
            add_barcode(first['id'], BarcodeIn(code='   '), self.db)
        with self.assertRaises(ValidationError):
            BarcodeIn(code='1', amount=-1)

    def test_location_and_conversion_bounds(self):
        from app.routes.core import ConversionIn, LocationIn
        with self.assertRaises(ValidationError):
            LocationIn(name='Shed', sort=10 ** 30)
        for factor in (0, -1, float('nan'), float('inf'), 1e12, 1e-12):
            with self.assertRaises(ValidationError):
                ConversionIn(from_unit_id=1, to_unit_id=2, factor=factor)

    def test_stock_unit_edit_does_not_relabel_existing_amounts(self):
        from app.routes.products import ProductIn, edit_product
        pid = product(self.db, 'Rice', 'g')
        st.purchase(self.db, pid, 500)
        data = ProductIn(name='Rice', unit_id=self.ids['kg'])
        with self.assertRaises(HTTPException) as error:
            edit_product(pid, data, self.db)
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(st.get_product(self.db, pid)['unit_id'], self.ids['g'])
        self.assertEqual(st.in_stock(self.db, pid), 500)
        st.consume(self.db, pid, 500)
        with self.assertRaises(HTTPException):
            edit_product(pid, data, self.db)  # history must still mean grams
        unused = product(self.db, 'Unused', 'g')
        self.assertEqual(edit_product(unused, ProductIn(name='Unused', unit_id=self.ids['kg']),
                                      self.db)['unit_id'], self.ids['kg'])

    def test_stock_unit_edit_preserves_recipe_and_shopping_references(self):
        from app.routes.products import ProductIn, edit_product
        from app.routes.recipes import RecipeIn, add_recipe
        from app.routes.shopping import ItemIn, add
        for name, reference in (('Recipe rice', 'recipe'), ('Listed rice', 'shopping')):
            pid = product(self.db, name, 'g')
            if reference == 'recipe':
                add_recipe(RecipeIn(name='Rice dish', ingredients=[{'product_id':pid,'amount':500}]),
                           self.db, USER)
            else:
                add(ItemIn(product_id=pid, amount=500), self.db, USER)
            with self.assertRaises(HTTPException):
                edit_product(pid, ProductIn(name=name, unit_id=self.ids['kg']), self.db)


class Races(unittest.TestCase):
    """Two tabs (two connections) doing the same thing at the same moment."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix='larder-race-')
        self.path = Path(self.dir.name) / 'larder.db'
        db = prepare(sqlite3.connect(self.path))
        db.execute('PRAGMA journal_mode=WAL')
        self.pid = product(db, 'Milk', 'piece')
        db.close()

    def tearDown(self):
        self.dir.cleanup()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        return db

    def twice(self, fn):
        barrier = threading.Barrier(2)
        results = []

        def run():
            db = self.connect()
            try:
                barrier.wait()
                results.append(('ok', fn(db)))
            except Exception as e:
                results.append(('error', e))
            finally:
                db.close()
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        return results

    def test_using_the_last_one_in_two_tabs(self):
        db = self.connect()
        st.purchase(db, self.pid, 1)
        db.close()
        results = self.twice(lambda db: st.consume(db, self.pid, 1, user_id=1))
        self.assertEqual(sorted(r[0] for r in results), ['error', 'ok'], results)
        self.assertTrue(all(isinstance(r[1], st.StockError) for r in results if r[0] == 'error'), results)
        db = self.connect()
        self.assertEqual(st.in_stock(db, self.pid), 0)
        self.assertEqual(count(db, 'stock_log', "kind='consume'"), 1)
        db.close()

    def test_got_it_pressed_in_two_tabs(self):
        from app.routes.shopping import bought
        db = self.connect()
        db.execute('INSERT INTO shopping_list(id,product_id,amount,unit_id,list_id) VALUES(1,?,2,1,1)', (self.pid,))
        db.commit()
        db.close()
        results = self.twice(lambda db: bought(1, None, db, USER))
        self.assertEqual(sorted(r[0] for r in results), ['error', 'ok'], results)
        db = self.connect()
        self.assertEqual(st.in_stock(db, self.pid), 2)
        self.assertEqual(count(db, 'stock_log', "kind='purchase'"), 1)
        self.assertEqual(count(db, 'shopping_list'), 0)
        db.close()

    def test_adding_the_same_product_in_two_tabs(self):
        from app.routes.shopping import ItemIn, add
        self.twice(lambda db: add(ItemIn(product_id=self.pid, amount=1), db, USER))
        db = self.connect()
        self.assertEqual(db.execute('SELECT COUNT(*), SUM(amount) FROM shopping_list').fetchone()[:], (1, 2))
        db.close()


class Units(MemoryDB):
    def test_imperial_factors(self):
        ids = self.ids
        oz = {'name': 'x', 'unit_id': ids['oz'], 'purchase_unit_id': None, 'purchase_factor': 1}
        self.assertAlmostEqual(st.to_stock_units(self.db, oz, 1, ids['lb']), 16, places=9)
        self.assertAlmostEqual(st.to_stock_units(self.db, oz, 1, ids['kg']), 35.27396194958, places=9)
        ml = dict(oz, unit_id=ids['ml'])
        self.assertAlmostEqual(st.to_stock_units(self.db, ml, 8, ids['fl oz (US)']), 236.5882365, places=9)
        self.assertAlmostEqual(st.to_stock_units(self.db, ml, 1, ids['l']), 1000, places=9)
        floz = dict(oz, unit_id=ids['fl oz (US)'])
        self.assertAlmostEqual(st.to_stock_units(self.db, floz, 1, ids['l']), 33.814022701843, places=9)
        # mass and volume never convert into each other
        with self.assertRaises(st.StockError):
            st.to_stock_units(self.db, ml, 1, ids['oz'])

    def test_no_drift_buying_and_using_in_another_unit(self):
        pid = product(self.db, 'Flour', 'g')
        for _ in range(10):
            st.purchase(self.db, pid, 3, self.ids['oz'])
        st.consume(self.db, pid, 30, self.ids['oz'])
        self.assertEqual(count(self.db, 'stock'), 0)
        for _ in range(7):
            st.purchase(self.db, pid, 0.1, self.ids['lb'])
        st.consume(self.db, pid, 0.7, self.ids['lb'])
        self.assertEqual(count(self.db, 'stock'), 0)

    def test_packs_and_pieces(self):
        pid = product(self.db, 'Eggs', 'piece', 'pack', 6)
        self.assertEqual(st.purchase(self.db, pid, 2, self.ids['pack'])['added'], 12)
        self.assertEqual(st.consume(self.db, pid, 1, self.ids['pack'])['in_stock'], 6)
        with self.assertRaises(st.StockError):
            st.consume(self.db, pid, 1, self.ids['kg'])
        self.db.rollback()
        with self.assertRaises(st.StockError):
            st.consume(self.db, pid, 2, self.ids['pack'])
        self.db.rollback()
        self.assertEqual(st.in_stock(self.db, pid), 6)

    def test_zero_factor_rule_from_an_old_database(self):
        self.db.execute("INSERT INTO units(id,name) VALUES(20,'cup')")
        self.db.execute('INSERT INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(20,?,0)', (self.ids['ml'],))
        self.db.execute('INSERT INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(?,20,0)', (self.ids['l'],))
        p = {'name': 'Milk', 'unit_id': self.ids['ml'], 'purchase_unit_id': None, 'purchase_factor': 1}
        self.assertIsNone(st.conversion_factor(self.db, 20, self.ids['ml']))
        with self.assertRaises(st.StockError):
            st.to_stock_units(self.db, p, 1, 20)


class PlanMaths(MemoryDB):
    def test_portion_sizes_scale_the_recipe(self):
        from app.routes.mealplan import MealIn, add_meal, needs
        from app.routes.recipes import RecipeIn, add_recipe
        pid = product(self.db, 'Lentils', 'g')
        r = add_recipe(RecipeIn(name='Soup', servings=4, ingredients=[{'product_id': pid, 'amount': 400, 'unit_id': self.ids['g']}]), self.db, USER)
        m = add_meal(MealIn(day='2026-10-12', slot='dinner', recipe_id=r['id'], eaters=[2, 3, 3]), self.db, USER)
        self.assertEqual(m['servings'], 2.0)  # half + one and a half, each person counted once
        add_meal(MealIn(day='2026-10-13', slot='dinner', recipe_id=r['id'], servings=1), self.db, USER)
        item = needs('2026-10-12', '2026-10-18', self.db, USER)['items'][0]
        self.assertAlmostEqual(item['need'], 300)
        # the recipe needs kg, stock is counted in g
        r2 = add_recipe(RecipeIn(name='Big soup', servings=1, ingredients=[{'product_id': pid, 'amount': 0.5, 'unit_id': self.ids['kg']}]), self.db, USER)
        add_meal(MealIn(day='2026-10-14', slot='dinner', recipe_id=r2['id'], servings=2), self.db, USER)
        self.assertAlmostEqual(needs('2026-10-12', '2026-10-18', self.db, USER)['items'][0]['need'], 1300)

    def test_packs_already_on_the_list_count(self):
        from app.routes.mealplan import MealIn, add_meal, plan_to_shopping
        from app.routes.shopping import add_item
        pid = product(self.db, 'Eggs', 'piece', 'pack', 6)
        add_item(self.db, pid, 1, self.ids['pack'], user_id=1)
        self.db.commit()
        add_meal(MealIn(day='2026-10-12', slot='breakfast', note='Omelette', servings=1,
                        ingredients=[{'product_id': pid, 'amount': 4, 'unit_id': self.ids['piece']}]), self.db, USER)
        self.assertEqual(plan_to_shopping('2026-10-12', '2026-10-18', self.db, USER)['added'], 0)
        add_meal(MealIn(day='2026-10-13', slot='breakfast', note='Frittata', servings=1,
                        ingredients=[{'product_id': pid, 'amount': 6, 'unit_id': self.ids['piece']}]), self.db, USER)
        self.assertEqual(plan_to_shopping('2026-10-12', '2026-10-18', self.db, USER)['added'], 1)
        listed = dict(self.db.execute('SELECT unit_id, amount FROM shopping_list').fetchall())
        self.assertEqual(listed, {self.ids['pack']: 1, self.ids['piece']: 4})
        self.assertEqual(plan_to_shopping('2026-10-12', '2026-10-18', self.db, USER)['added'], 0)

    def test_recipe_shortfall_counts_packs_on_the_list(self):
        from app.routes.recipes import RecipeIn, add_recipe, missing_to_shopping
        from app.routes.shopping import add_item
        pid = product(self.db, 'Eggs', 'piece', 'pack', 6)
        r = add_recipe(RecipeIn(name='Cake', servings=1, ingredients=[{'product_id': pid, 'amount': 3, 'unit_id': self.ids['piece']}]), self.db, USER)
        add_item(self.db, pid, 1, self.ids['pack'], user_id=1)
        self.db.commit()
        self.assertEqual(missing_to_shopping(r['id'], None, self.db, USER)['added'], 0)
        self.assertEqual(missing_to_shopping(r['id'], 4, self.db, USER)['added'], 1)
        self.assertEqual(self.db.execute('SELECT amount FROM shopping_list WHERE unit_id=?', (self.ids['piece'],)).fetchone()[0], 6)

    def test_tiny_shortfall_never_adds_a_zero_row(self):
        from app.routes.mealplan import MealIn, add_meal, plan_to_shopping
        pid = product(self.db, 'Salt', 'g')
        st.purchase(self.db, pid, 99.9996)
        add_meal(MealIn(day='2026-10-12', slot='dinner', note='Brine', servings=1,
                        ingredients=[{'product_id': pid, 'amount': 100, 'unit_id': self.ids['g']}]), self.db, USER)
        self.assertEqual(plan_to_shopping('2026-10-12', '2026-10-18', self.db, USER)['added'], 0)
        self.assertEqual(count(self.db, 'shopping_list', 'amount <= 0'), 0)

    def test_recipe_with_zero_servings_from_an_old_database(self):
        from app.routes.mealplan import needs
        from app.routes.recipes import check
        pid = product(self.db, 'Bread', 'piece')
        self.db.execute("INSERT INTO recipes(id,name,servings) VALUES(5,'Old',0)")
        self.db.execute('INSERT INTO recipe_ingredients(recipe_id,product_id,amount) VALUES(5,?,2)', (pid,))
        self.db.execute("INSERT INTO meal_plan(day,slot,recipe_id,servings) VALUES('2026-10-12','lunch',5,3)")
        self.db.commit()
        self.assertEqual(needs('2026-10-12', '2026-10-18', self.db, USER)['items'][0]['need'], 2)
        self.assertEqual(check(self.db, 5)['ingredients'][0]['need'], 2)

    def test_calendar_edges(self):
        from app.routes.mealplan import _day, week
        with self.assertRaises(HTTPException) as e:
            week('9999-12-31', 7, self.db, USER)
        self.assertEqual(e.exception.status_code, 400)
        self.assertEqual(week('9999-12-25', 7, self.db, USER)['end'], '9999-12-31')
        for bad in ('2026-13-01', '2026-W41-1', '20261012', ' 2026-10-12', '2026-10-12T00:00', '', None, '12/10/2026'):
            with self.assertRaises(HTTPException):
                _day(bad)
        self.assertEqual(_day('2024-02-29').isoformat(), '2024-02-29')

    def test_cooking_takes_from_stock_once(self):
        from app.routes.recipes import RecipeIn, add_recipe, cook
        pid = product(self.db, 'Bread', 'piece')
        st.purchase(self.db, pid, 3)
        r = add_recipe(RecipeIn(name='Toast', servings=1, ingredients=[{'product_id': pid, 'amount': 2, 'unit_id': self.ids['piece']}]), self.db, USER)
        self.assertEqual(cook(r['id'], None, db=self.db, user=USER)['used'], ['Bread'])
        self.assertEqual(st.in_stock(self.db, pid), 1)
        cook(r['id'], None, db=self.db, user=USER)  # uses what's left
        self.assertEqual(cook(r['id'], None, db=self.db, user=USER)['skipped'], ['Bread'])
        self.assertEqual(st.in_stock(self.db, pid), 0)
        for bad in (0, -2, float('nan'), 10 ** 7):
            with self.assertRaises(HTTPException):
                cook(r['id'], bad, db=self.db, user=USER)


def resolver(table):
    def fake(host, port, *args, **kwargs):
        if host not in table:
            raise OSError('no such host')
        return [(None, None, None, '', (table[host], port))]
    return fake


HOSTS = {'metadata.internal': '169.254.169.254', 'intranet.example': '192.168.1.10',
         'gpu-box.lan': '192.168.1.30', 'api.example.com': '93.184.216.34',
         'ollama.example.com': '93.184.216.35', 'sneaky.example.com': '127.0.0.1'}


class Pictures(unittest.TestCase):
    PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32
    JPEG = b'\xff\xd8\xff\xe0' + b'\x00' * 32

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory(prefix='larder-pictures-')
        self.uploads = Path(self.dir.name)
        for p in (patch.object(pictures, 'UPLOAD_DIR', self.uploads),
                  patch.object(netcheck.socket, 'getaddrinfo', side_effect=resolver(HOSTS))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.dir.cleanup()

    def files(self):
        return sorted(p.name for p in self.uploads.iterdir())

    def test_uploads_are_checked_by_content(self):
        for data, kind in ((b'<html><script>alert(1)</script>', 'image/png'), (b'', 'image/png'),
                           (b'GIF8', 'image/gif'), (self.PNG, 'text/html'), (self.PNG, 'image/svg+xml')):
            with self.subTest(data=data[:10], kind=kind), self.assertRaises(ValueError):
                pictures.save_picture(data, kind)
        with self.assertRaises(ValueError):
            pictures.save_picture(self.PNG + b'\x00' * pictures.MAX_SIZE, 'image/png')
        self.assertEqual(self.files(), [])
        self.assertTrue(pictures.save_picture(self.PNG, 'image/png').endswith('.png'))
        # named after what it is, not what the browser said
        self.assertTrue(pictures.save_picture(self.JPEG, 'image/png').endswith('.jpg'))
        self.assertTrue(pictures.save_picture(b'RIFF\x00\x00\x00\x00WEBPVP8 ', 'image/webp').endswith('.webp'))

    def test_fetching_from_inside_the_network_is_refused(self):
        def handler(request):
            raise AssertionError('should not have fetched ' + str(request.url))
        with patch.object(pictures, '_transport', httpx.MockTransport(handler)):
            for url in ('http://169.254.169.254/latest/meta-data/', 'http://127.0.0.1:8000/', 'http://[::1]/x.png',
                        'http://localhost/x.png', 'http://metadata.internal/x', 'http://intranet.example/a.png',
                        'file:///etc/passwd', 'gopher://api.example.com/', 'http://user:pw@api.example.com/x.png'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    pictures.fetch_picture(url)

    def test_redirects_get_the_same_check(self):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            if request.url.path == '/hop':
                return httpx.Response(302, headers={'location': '/pic.png'})
            if request.url.path == '/pic.png':
                return httpx.Response(200, content=self.PNG, headers={'content-type': 'image/png'})
            return httpx.Response(302, headers={'location': 'http://169.254.169.254/latest/meta-data/'})
        with patch.object(pictures, '_transport', httpx.MockTransport(handler)):
            with self.assertRaises(ValueError):
                pictures.fetch_picture('https://api.example.com/evil')
            self.assertNotIn('169.254.169.254', seen)
            self.assertTrue(pictures.fetch_picture('https://api.example.com/hop').endswith('.png'))

    def test_fetch_size_cap(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=self.PNG * 100, headers={'content-type': 'image/png'}))
        with patch.object(pictures, '_transport', transport), patch.object(pictures, 'MAX_SIZE', 200):
            with self.assertRaises(ValueError):
                pictures.fetch_picture('https://api.example.com/big.png')
        self.assertEqual(self.files(), [])

    def test_remove_picture_stays_in_the_folder(self):
        outside = self.uploads.parent / (self.uploads.name + '-keep.txt')
        outside.write_text('keep')
        try:
            for name in ('../' + outside.name, '..', '.', '', None, '..\\x'):
                pictures.remove_picture(name)
            self.assertTrue(outside.exists())
        finally:
            outside.unlink()


class Sessions(MemoryDB):
    def request(self, session=None, headers=None):
        return SimpleNamespace(session=session if session is not None else {}, headers=headers or {})

    def test_password_change_ends_old_sessions(self):
        from app.auth import hash_password, start_session, user_from_request
        self.db.execute('UPDATE users SET password_hash=? WHERE id=1', (hash_password('first-password'),))
        self.db.commit()
        user = dict(self.db.execute('SELECT * FROM users WHERE id=1').fetchone())
        other_tab = self.request()
        start_session(other_tab, user)
        self.assertEqual(user_from_request(other_tab, self.db)['id'], 1)
        self.assertIsNone(user_from_request(self.request({'user_id': 1}), self.db))  # cookie with no check
        self.assertIsNone(user_from_request(self.request({'user_id': 1, 'check': 'forged'}), self.db))
        self.db.execute('UPDATE users SET password_hash=? WHERE id=1', (hash_password('second-password'),))
        self.db.commit()
        self.assertIsNone(user_from_request(other_tab, self.db))

    def test_bad_api_key_never_falls_back_to_the_cookie(self):
        from app.auth import start_session, user_from_request
        user = dict(self.db.execute('SELECT * FROM users WHERE id=1').fetchone())
        r = self.request(headers={'x-api-key': 'lk_wrong'})
        start_session(r, user)
        self.assertIsNone(user_from_request(r, self.db))

    def test_secret_file_is_private(self):
        from app import auth
        with tempfile.TemporaryDirectory(prefix='larder-secret-') as d, patch.object(auth, 'DATA_DIR', Path(d)):
            first = auth.secret_key()
            self.assertEqual(auth.secret_key(), first)
            self.assertGreater(len(first), 40)
            self.assertEqual(os.stat(Path(d) / 'secret.key').st_mode & 0o777, 0o600)
            self.assertEqual(os.listdir(d), ['secret.key'])


class Migrations(unittest.TestCase):
    def test_startup_from_several_workers_at_once(self):
        with tempfile.TemporaryDirectory(prefix='larder-migrate-') as d:
            root = Path(d)
            # an install from before the date/measurement settings, restarted with several workers
            old = sqlite3.connect(root / 'larder.db')
            old.execute('PRAGMA journal_mode=WAL')
            old.executescript(SCHEMA.replace("    date_format TEXT NOT NULL DEFAULT 'DD/MM/YYYY',\n", '')
                              .replace("    measurement_system TEXT NOT NULL DEFAULT 'metric',\n", ''))
            old.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'old','unused')")
            old.commit()
            old.close()
            errors = []
            with patch.multiple(dbmodule, DATA_DIR=root, DB_PATH=root / 'larder.db', UPLOAD_DIR=root / 'uploads'):
                def run():
                    try:
                        dbmodule.init_db()
                    except Exception as e:
                        errors.append(e)
                threads = [threading.Thread(target=run) for _ in range(3)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(60)
                self.assertEqual(errors, [])
                db = dbmodule.connect()
                before = [db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('units', 'locations', 'unit_conversions')]
                db.close()
                dbmodule.init_db()
                db = dbmodule.connect()
                after = [db.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('units', 'locations', 'unit_conversions')]
                self.assertEqual(before, after)
                self.assertEqual(before[1], 4)
                self.assertEqual(db.execute('SELECT username, date_format, measurement_system FROM users').fetchone()[:],
                                 ('old', 'DD/MM/YYYY', 'metric'))
                db.close()


class BrowserHelpers(unittest.TestCase):
    def test_api_header_and_date_round_trips(self):
        script = (ROOT / 'app/static/app.js').read_text()
        pre = """
const assert = require('node:assert/strict');
const window = {USER_PREFS: {date_format: 'DD/MM/YYYY', measurement_system: 'metric'}};
const document = {addEventListener() {}}; const navigator = {};
let seen = null;
const fetch = async (url, o) => { seen = o; return {status: 200, ok: true, text: async () => '{}'}; };
"""
        body = r"""
(async () => {
  await api('/api/shopping', {method: 'POST', body: {name: 'Milk'}});
  assert.equal(seen.headers['X-Requested-With'], 'fetch');
  assert.equal(seen.headers['Content-Type'], 'application/json');
  const fd = new FormData();
  await api('/api/products/1/picture', {method: 'POST', body: fd});
  assert.equal(seen.headers['X-Requested-With'], 'fetch');
  assert.equal(seen.headers['Content-Type'], undefined);
  for (const f of ['DD/MM/YYYY', 'MM/DD/YYYY', 'YYYY-MM-DD']) {
    for (let i = 0; i < 366 * 2; i++) {
      const iso = new Date(Date.UTC(2024, 0, 1) + i * 86400000).toISOString().slice(0, 10);
      assert.equal(parseDate(formatDate(iso, f), f), iso);
    }
  }
  // the same text means different days depending on the setting, never silently swapped
  assert.equal(parseDate('01/02/2026', 'DD/MM/YYYY'), '2026-02-01');
  assert.equal(parseDate('01/02/2026', 'MM/DD/YYYY'), '2026-01-02');
  for (const [text, f] of [['13/01/2026', 'MM/DD/YYYY'], ['01/13/2026', 'DD/MM/YYYY'], ['00/01/2026', 'DD/MM/YYYY'],
                           ['29/02/2026', 'DD/MM/YYYY'], ['2026-02-01', 'DD/MM/YYYY'], ['01/02/2026', 'YYYY-MM-DD'],
                           ['2026-02-30', 'YYYY-MM-DD'], ['1/2/2026', 'MM/DD/YYYY'], ['01/02/26', 'DD/MM/YYYY'],
                           [' 01/02/2026', 'DD/MM/YYYY'], ['0000-01-01', 'YYYY-MM-DD']]) {
    assert.throws(() => parseDate(text, f), Error, text + ' ' + f);
  }
  window.UNIT_RULES = null;
  assert.ok(Math.abs(unitFactor('lb', 'oz') - 16) < 1e-12);
  assert.ok(Math.abs(unitFactor('fl oz (US)', 'ml') - 29.5735295625) < 1e-12);
})().catch(e => { console.error(e); process.exit(1); });
"""
        r = subprocess.run(['node'], input=pre + script + '\n' + body, text=True, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == '__main__':
    unittest.main()
