import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware

from app import db as database, pricing
from app.auth import current_user
from app.pricing import stores
from app.routes import pricing as routes, products
from scripts.refresh_prices import refresh_prices


def prepare(db):
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(database.SCHEMA)
    db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'test','unused')")
    db.execute("INSERT INTO units(id,name) VALUES(1,'piece')")
    db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Milk 2L',1)")
    db.executemany('INSERT INTO store_settings(store) VALUES(?)', [(s,) for s in stores.STORES])
    db.commit()
    return db


CANDIDATE = dict(store_item_id='123', name='Milk 2L', brand='', size='2L', price=3.4,
                 was_price=4, unit_price=1.7, unit_measure='1L', on_special=True)


class SchemaAndRefresh(unittest.TestCase):
    def setUp(self):
        self.db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(self.db.close)

    def test_upsert_ledger_and_cascades(self):
        db = self.db
        first = pricing.link_product(db, 1, 'coles', '123', 'Milk', 1, size='2L')
        pricing.record_price(db, first, CANDIDATE)
        pricing.record_price(db, first, dict(CANDIDATE, price=3.6))
        self.assertEqual(first, pricing.link_product(db, 1, 'coles', '123', 'Updated', 1))
        self.assertEqual(db.execute('SELECT size FROM product_store_links').fetchone()[0], '2L')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM store_price_checks').fetchone()[0], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute("INSERT INTO product_store_links(product_id,store,store_item_id) VALUES(1,'coles','other')")
        db.execute('DELETE FROM users WHERE id=1')
        self.assertIsNone(db.execute('SELECT matched_by FROM product_store_links').fetchone()[0])
        pricing.link_product(db, 1, 'coles', '456', 'Different')
        self.assertEqual(db.execute('SELECT size FROM product_store_links').fetchone()[0], '')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM product_store_links').fetchone()[0], 1)
        self.assertEqual(db.execute('SELECT COUNT(*) FROM store_price_checks').fetchone()[0], 0)
        db.execute('DELETE FROM products WHERE id=1')
        self.assertEqual(db.execute('SELECT COUNT(*) FROM product_store_links').fetchone()[0], 0)

    def test_migration_repeatable_preserves_settings_and_legacy_products(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.multiple(database, DATA_DIR=root, DB_PATH=root/'larder.db', UPLOAD_DIR=root/'uploads'):
                db = sqlite3.connect(root/'larder.db')
                db.executescript("CREATE TABLE products(id INTEGER PRIMARY KEY,name TEXT NOT NULL,unit_id INTEGER NOT NULL); INSERT INTO products VALUES(1,'Legacy',1);")
                db.close()
                database.init_db()
                db = database.connect()
                self.assertEqual(len(db.execute('SELECT * FROM store_settings').fetchall()), 4)
                db.execute("UPDATE store_settings SET enabled=0,postcode='6000' WHERE store='iga'")
                db.execute("INSERT INTO product_store_links(product_id,store,store_item_id,store_name) VALUES(1,'coles','legacy','Legacy')")
                db.execute("ALTER TABLE product_store_links DROP COLUMN size")
                db.commit()
                db.close()
                database.init_db()
                db = database.connect()
                self.assertEqual(tuple(db.execute("SELECT enabled,postcode FROM store_settings WHERE store='iga'").fetchone()), (0, '6000'))
                self.assertEqual(db.execute('SELECT name FROM products').fetchone()[0], 'Legacy')
                self.assertEqual(tuple(db.execute('SELECT store_item_id,size FROM product_store_links').fetchone()), ('legacy', ''))
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                db.close()

    def add_links(self, store, count):
        for number in range(count):
            pid = self.db.execute("INSERT INTO products(name,unit_id) VALUES('Milk',1)").lastrowid
            pricing.link_product(self.db, pid, store, str(number), 'Milk')
        self.db.commit()

    def test_breaker_stops_one_store_and_continues_others_with_pacing(self):
        self.add_links('coles', 6)
        self.add_links('woolworths', 1)
        self.add_links('aldi', 1)
        self.db.execute("UPDATE store_settings SET enabled=0 WHERE store='aldi'")
        self.db.commit()
        def fetch(store, name, postcode):
            if store == 'coles':
                raise httpx.ReadTimeout('unavailable')
            return [dict(CANDIDATE, store_item_id='0')]
        fetch = MagicMock(side_effect=fetch)
        sleep = MagicMock()
        with self.assertLogs(level='WARNING'):
            result = refresh_prices(self.db, fetch, sleep)
        self.assertEqual(result, dict(products_checked=4, prices_updated=1, circuit_breakers=['coles']))
        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(sleep.call_count, 3)
        sleep.assert_called_with(1.5)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM store_price_checks').fetchone()[0], 4)

    def test_nightly_missing_matches_are_bounded_and_paced(self):
        self.db.execute("UPDATE store_settings SET enabled=(store='coles')")
        self.db.execute("INSERT INTO products(name,unit_id) VALUES('Bread',1)")
        fetch = MagicMock(return_value=[CANDIDATE])
        sleep = MagicMock()
        refresh_prices(self.db, fetch, sleep, match_limit=1)
        fetch.assert_called_once_with('coles', 'Milk 2L', '')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM product_store_links').fetchone()[0], 1)
        refresh_prices(self.db, fetch, sleep, match_limit=1)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 1)

    def test_missing_matches_share_refresh_breaker(self):
        self.add_links('coles', 3)
        self.db.execute("UPDATE store_settings SET enabled=(store='coles')")
        self.db.commit()
        fetch = MagicMock(side_effect=ValueError('offline'))
        with self.assertLogs(level='WARNING'):
            refresh_prices(self.db, fetch, lambda _: None, match_limit=100)
        self.assertEqual(fetch.call_count, 3)

    def test_missing_matches_trip_their_own_breaker(self):
        self.add_links('aldi', 5)
        self.db.execute("UPDATE store_settings SET enabled=(store='coles')")
        self.db.commit()
        fetch = MagicMock(return_value=stores.FailedResults())
        sleep = MagicMock()
        with self.assertLogs(level='WARNING'):
            result = refresh_prices(self.db, fetch, sleep, match_limit=100)
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(result['circuit_breakers'], ['coles'])

    def test_only_and_exclude_stores_scope_both_the_refresh_and_the_matching(self):
        self.add_links('coles', 2)
        self.db.execute("UPDATE store_settings SET enabled=1")
        self.db.commit()
        fetch = MagicMock(return_value=[CANDIDATE])
        result = refresh_prices(self.db, fetch, lambda _: None, match_limit=10, only_stores=('coles',))
        self.assertTrue(all(call.args[0] == 'coles' for call in fetch.call_args_list))
        self.assertEqual(result['circuit_breakers'], [])
        fetch.reset_mock()
        result = refresh_prices(self.db, fetch, lambda _: None, match_limit=10, exclude_stores=('coles',))
        self.assertTrue(all(call.args[0] != 'coles' for call in fetch.call_args_list))
        self.assertEqual(result['circuit_breakers'], [])

    def test_auto_match_live_asks_every_store_concurrently(self):
        self.db.executescript("UPDATE store_settings SET enabled=1")
        self.db.commit()
        import time
        order = []

        def slow_search(store, query, postcode=''):
            delay = {'woolworths': 0.2, 'aldi': 0.05, 'iga': 0.05, 'coles': 0.2}[store]
            time.sleep(delay)
            order.append(store)
            if store == 'woolworths':
                return [CANDIDATE]
            return []

        t0 = time.time()
        with patch.object(pricing, 'search', side_effect=slow_search):
            matched = pricing.auto_match_live(self.db, 1, 'Milk 2L')
        elapsed = time.time() - t0
        # Run one after another this would be >= 0.2+0.2+0.05+0.05 = 0.5s;
        # run together it's bounded by the slowest one, ~0.2s.
        self.assertLess(elapsed, 0.4)
        self.assertEqual(len(order), 4)
        self.assertEqual([m['store'] for m in matched], ['woolworths'])
        link = self.db.execute("SELECT store_item_id FROM product_store_links WHERE product_id=1 AND store='woolworths'").fetchone()
        self.assertEqual(link['store_item_id'], '123')

    def test_auto_match_live_skips_excluded_and_survives_one_store_failing(self):
        self.db.executescript("UPDATE store_settings SET enabled=1")
        self.db.commit()

        def flaky_search(store, query, postcode=''):
            if store == 'coles':
                raise AssertionError('coles must never be asked live')
            if store == 'aldi':
                raise ValueError('aldi is down right now')
            return [CANDIDATE] if store == 'woolworths' else []

        with patch.object(pricing, 'search', side_effect=flaky_search), self.assertLogs(level='WARNING'):
            matched = pricing.auto_match_live(self.db, 1, 'Milk 2L', excluded_stores=('coles',))
        self.assertEqual([m['store'] for m in matched], ['woolworths'])


    def test_exact_id_missing_id_and_append_only_history(self):
        link = pricing.link_product(self.db, 1, 'coles', '123', 'Milk 2L')
        self.db.commit()
        refresh_prices(self.db, lambda *args: [dict(CANDIDATE, store_item_id='other'), CANDIDATE], lambda _: None)
        refresh_prices(self.db, lambda *args: [dict(CANDIDATE, store_item_id='other')], lambda _: None)
        self.assertEqual([r[0] for r in self.db.execute('SELECT price FROM store_price_checks WHERE link_id=? ORDER BY id', (link,))], [3.4, None])

    def test_breaker_resets_on_success_and_counts_parse_failures(self):
        self.add_links('coles', 7)
        fetch = MagicMock(side_effect=[ValueError('parse'), ValueError('parse'), [],
                                      stores.FailedResults(), {}, stores.FailedResults()])
        with self.assertLogs(level='WARNING'):
            result = refresh_prices(self.db, fetch, lambda _: None)
        self.assertEqual(result['products_checked'], 6)
        self.assertEqual(result['circuit_breakers'], ['coles'])


class PricingAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def tick():
            while True:
                await asyncio.sleep(.01)
        self.timer = asyncio.create_task(tick())
        self.db = prepare(sqlite3.connect(':memory:', check_same_thread=False))
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test")
        app.include_router(routes.router)
        app.include_router(products.router)
        app.dependency_overrides[database.get_db] = lambda: self.db
        app.dependency_overrides[current_user] = lambda: {'id': 1}
        self.app = app
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')
        self.patches = []
        self.searches = {}
        for store in stores.STORES:
            mocked = patch.object(stores, 'search_' + store, return_value=[])
            self.patches.append(mocked)
            self.searches[store] = mocked.start()
        coles_browser = patch.object(stores, 'search_coles_browser', return_value=[])
        self.patches.append(coles_browser)
        self.searches['coles_browser'] = coles_browser.start()

    async def asyncTearDown(self):
        self.timer.cancel()
        for mocked in self.patches:
            mocked.stop()
        await self.client.aclose()
        self.db.close()

    async def test_stores_search_validation_and_auth(self):
        self.assertEqual(len((await self.client.get('/api/pricing/stores')).json()), 4)
        response = await self.client.put('/api/pricing/stores/iga', json={'enabled': False, 'postcode': '6000'})
        self.assertEqual(response.json()['postcode'], '6000')
        self.db.execute("INSERT INTO iga_catalogue_store VALUES(1,'48323','6000')")
        self.db.execute("INSERT INTO iga_catalogue(item_id,name,price) VALUES('123','Milk',3.4)")
        found = (await self.client.get('/api/pricing/search?store=iga&q=milk')).json()
        self.assertEqual(found[0]['store_item_id'], '123')
        self.searches['iga'].assert_not_called()
        # Woolworths and Aldi are matched against their own local
        # catalogues too, same as Coles below -- never a live request.
        self.db.execute("INSERT INTO woolworths_catalogue(stockcode,name,price) VALUES(1,'Milk 2L',3.4)")
        self.db.execute("INSERT INTO aldi_catalogue(item_id,name,price) VALUES('a1','Milk 2L',3.4)")
        self.db.commit()
        for store in ('aldi', 'woolworths'):
            found = (await self.client.get(f'/api/pricing/search?store={store}&q=milk')).json()
            self.assertEqual(found[0]['name'], 'Milk 2L')
            self.searches[store].assert_not_called()
        # Coles doesn't go through its live search at all -- it's a lookup
        # against the catalogue crawled on a schedule (see
        # scripts/coles_catalogue_crawl.py), which is just the request's db.
        self.db.execute("INSERT INTO coles_catalogue(coles_id,name,price) VALUES(1,'Milk 2L',3.4)")
        self.db.commit()
        found = (await self.client.get('/api/pricing/search?store=coles&q=milk')).json()
        self.assertEqual(found[0]['name'], 'Milk 2L')
        self.searches['coles'].assert_not_called()
        with patch.object(pricing, '_catalogue_lookup', side_effect=ValueError('db error')), self.assertLogs(level='WARNING'):
            self.assertEqual((await self.client.get('/api/pricing/search?store=coles&q=milk')).status_code, 502)
        self.assertEqual((await self.client.put('/api/pricing/stores/unknown', json={'enabled': True})).status_code, 404)
        self.assertEqual((await self.client.get('/api/pricing/search?store=unknown&q=milk')).status_code, 404)
        self.assertEqual((await self.client.get('/api/pricing/search?store=coles&q=')).status_code, 422)
        self.app.dependency_overrides.pop(current_user)
        self.assertEqual((await self.client.get('/api/pricing/stores')).status_code, 401)

    async def test_link_replace_latest_check_delete_and_missing_product(self):
        url = '/api/pricing/products/1/link'
        body = dict(store='coles', store_item_id='123', store_name='Milk 2L')
        first = (await self.client.post(url, json=body)).json()
        self.assertEqual(first['matched_by'], 1)
        pricing.record_price(self.db, first['id'], CANDIDATE)
        pricing.record_price(self.db, first['id'], dict(CANDIDATE, price=0))
        self.db.commit()
        links = (await self.client.get('/api/pricing/products/1')).json()
        self.assertEqual(links[0]['latest_check']['price'], 0)
        second = (await self.client.post(url, json=body)).json()
        self.assertEqual(second['id'], first['id'])
        await self.client.post(url, json=dict(body, store_item_id='different'))
        links = (await self.client.get('/api/pricing/products/1')).json()
        self.assertEqual(len(links), 1)
        self.assertIsNone(links[0]['latest_check']['price'])
        self.assertEqual((await self.client.delete(url + '/coles')).status_code, 200)
        self.assertEqual((await self.client.get('/api/pricing/products/1')).json(), [])
        self.assertEqual((await self.client.get('/api/pricing/products/999')).status_code, 404)
        self.assertEqual((await self.client.post('/api/pricing/products/999/link', json=body)).status_code, 404)
        self.assertEqual((await self.client.post(url, json=dict(body, store='unknown'))).status_code, 404)

    async def test_manual_link_records_candidate_and_rejects_invalid_prices(self):
        body = dict(CANDIDATE, store='coles', store_name=CANDIDATE['name'])
        response = await self.client.post('/api/pricing/products/1/link', json=body)
        self.assertEqual(response.status_code, 200)
        check = (await self.client.get('/api/pricing/products/1')).json()[0]['latest_check']
        for field in ('price', 'was_price', 'unit_price', 'unit_measure', 'on_special'):
            self.assertEqual(check[field], CANDIDATE[field])
        response = await self.client.post('/api/pricing/products/1/link', json=dict(body, price=-1))
        self.assertEqual(response.status_code, 422)

    async def test_bulk_missing_links_results_pacing_and_failure_isolation(self):
        self.db.execute("UPDATE store_settings SET enabled=0 WHERE store NOT IN ('coles','iga')")
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(2,'Capsicum',1),(3,'Bread',1)")
        pricing.link_product(self.db, 1, 'coles', 'kept', 'Existing milk')
        self.db.execute("INSERT INTO coles_catalogue(coles_id,name,price) VALUES(55,'Red Capsicum',2.5)")
        self.db.commit()

        lookup = pricing._catalogue_lookup

        def catalogue_lookup(store, query, **kwargs):
            if store != 'iga':
                return lookup(store, query, **kwargs)
            if query.startswith('Milk'):
                raise RuntimeError('offline')
            return {'Bread': [dict(CANDIDATE, name='Bread')]}.get(query, [])

        with patch.object(pricing, '_catalogue_lookup', side_effect=catalogue_lookup), \
             patch.object(pricing.time, 'sleep') as sleep, self.assertLogs(level='WARNING'):
            response = await self.client.post('/api/pricing/products/auto-match')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            'matched': [{'product_id':2,'name':'Capsicum','store':'coles'},
                        {'product_id':3,'name':'Bread','store':'iga'}],
            'unmatched': [{'product_id':1,'name':'Milk 2L'}, {'product_id':2,'name':'Capsicum'},
                          {'product_id':3,'name':'Bread'}]})
        self.assertEqual(sleep.call_count, 4)
        sleep.assert_called_with(1.5)
        self.assertEqual(self.db.execute("SELECT store_item_id FROM product_store_links WHERE product_id=1").fetchone()[0], 'kept')
        self.searches['aldi'].assert_not_called()
        self.searches['coles'].assert_not_called()

    async def test_product_history_medians_specials_and_purchases(self):
        url = '/api/pricing/products/1/history'
        self.assertEqual((await self.client.get(url)).json(), {'stores': [], 'purchases': []})
        self.assertEqual((await self.client.get('/api/pricing/products/999/history')).status_code, 404)
        link = pricing.link_product(self.db, 1, 'coles', '123', 'Milk', 1)
        empty = pricing.link_product(self.db, 1, 'iga', '456', 'Milk', 1)
        self.db.execute("UPDATE store_settings SET enabled=0 WHERE store='iga'")
        # Insert out of chronological order, including a raised 'was' price,
        # a real saving, an ordinary price, and an unavailable check.
        for day, price, special, was in [(5, 10, True, 14), (1, 10, False, None),
                (2, 10, False, None), (3, 7, True, 10), (4, None, True, 10),
                (6, 9.5, True, 12), (7, 9.49, True, 12), (8, 10, False, None)]:
            pricing.record_price(self.db, link, dict(CANDIDATE, price=price, on_special=special, was_price=was))
            self.db.execute("UPDATE store_price_checks SET checked_at=? WHERE id=(SELECT MAX(id) FROM store_price_checks)",
                            (f'2026-10-{day:02d} 12:00:00',))
        self.db.execute("INSERT INTO stock_log(product_id,kind,amount,price,at) VALUES(1,'purchase',2,20,'2026-10-03')")
        self.db.execute("INSERT INTO stock_log(product_id,kind,amount,price,at) VALUES(1,'purchase',1,0,'2026-10-01')")
        self.db.execute("INSERT INTO stock_log(product_id,kind,amount,price) VALUES(1,'consume',-1,10)")
        self.db.commit()
        response = await self.client.get(url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        coles, iga = data['stores']
        self.assertEqual(coles['typical_price'], 10)
        self.assertEqual([c['price'] for c in coles['history']], [10, 10, 7, None, 10, 9.5, 9.49, 10])
        self.assertEqual([c['fake_special'] for c in coles['history']], [False, False, False, False, True, True, False, False])
        self.assertTrue(coles['history'][4]['on_special'])
        self.assertEqual(coles['history'][4]['was_price'], 14)
        self.assertEqual(iga['id'], empty)
        self.assertFalse(iga['enabled'])
        self.assertIsNone(iga['typical_price'])
        self.assertEqual(iga['history'], [])
        self.assertEqual([p['price'] for p in data['purchases']], [0, 20])

    async def test_creation_matches_only_unambiguous_names_and_enabled_stores(self):
        self.db.execute("UPDATE store_settings SET enabled=0 WHERE store='iga'")
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,price) VALUES(123,'MILK 2 L',3.4);
            INSERT INTO woolworths_catalogue(stockcode,name,price) VALUES(1,'Milk 2L',3.5),(2,'Milk 2L',3.6);
            INSERT INTO aldi_catalogue(item_id,name,price) VALUES('a1','Dark Chocolate Block',4.0);
        """)
        self.db.commit()
        response = await self.client.post('/api/products', json={'name': 'Milk 2L', 'unit_id': 1})
        self.assertEqual(response.status_code, 201)
        product_id = response.json()['id']
        links = (await self.client.get(f'/api/pricing/products/{product_id}')).json()
        self.assertEqual([link['store'] for link in links], ['coles'])
        self.assertEqual(links[0]['latest_check']['price'], 3.4)
        self.assertEqual(links[0]['store_item_id'], '123')
        self.searches['iga'].assert_not_called()
        self.searches['coles'].assert_not_called()
        self.searches['woolworths'].assert_not_called()
        self.searches['aldi'].assert_not_called()
        await self.client.put(f'/api/products/{product_id}', json={'name': 'Milk', 'unit_id': 1})
        self.searches['coles'].assert_not_called()

    async def test_creation_survives_site_and_whole_hook_failure(self):
        with patch.object(pricing, '_catalogue_lookup', side_effect=ValueError('db error')), self.assertLogs(level='WARNING'):
            response = await self.client.post('/api/products', json={'name': 'Milk', 'unit_id': 1})
        self.assertEqual(response.status_code, 201)
        # Coles, Woolworths and Aldi all go through the same local lookup
        # now -- one failing there must not stop the product from being
        # created, it just means none of those three end up linked.
        links = (await self.client.get(f"/api/pricing/products/{response.json()['id']}")).json()
        self.assertEqual([l['store'] for l in links if l['store'] in ('coles', 'woolworths', 'aldi')], [])
        with patch.object(pricing, 'auto_match', side_effect=RuntimeError('offline')), self.assertLogs(level='WARNING'):
            response = await self.client.post('/api/products', json={'name': 'Bread', 'unit_id': 1})
        self.assertEqual(response.status_code, 201)


class ScoredMatching(unittest.TestCase):
    def candidate(self, name, item='1', brand=''):
        return dict(CANDIDATE, name=name, store_item_id=item, brand=brand)

    def test_generic_staples_and_size_normalization(self):
        for name, candidate in [('Pork Chops','Pork Loin Chops'), ('Capsicum','Red Capsicum'),
                                ('Milk','Full Milk'), ('Milk 2L','MILK 2 L')]:
            with self.subTest(name=name):
                self.assertIsNotNone(pricing.strongest_match(name, [self.candidate(candidate)]))

    def test_a_single_word_brand_plus_a_real_size_still_matches(self):
        # "1.25L Pepsi" strips down to just the one word "pepsi" for
        # word-overlap purposes once the size token's taken out -- that
        # used to always fail the overlap floor against any real,
        # descriptive store title, even when there's only one sane
        # candidate and the size is an exact match. An explicitly different
        # variant remains invalid even when it is the only available line.
        candidates = [self.candidate('Pepsi Max Cola No Sugar Soft Drink Bottle', brand='Pepsi')]
        candidates[0]['size'] = '1.25L'
        self.assertIsNone(pricing.strongest_match('1.25L Pepsi', candidates))
        self.assertIsNotNone(pricing.strongest_match('400g Nutella', [dict(self.candidate(
            'Nutella Hazelnut Spread', brand='Nutella'), size='400g')]))

    def test_bare_brand_residual_variants_use_price_then_id(self):
        # Both full-recall variants clear the single-word floor. With no
        # further intent signal, the residual tie must resolve.
        candidates = [self.candidate('Vegemite Spread 220g', item='1', brand='Vegemite'),
                      self.candidate('Vegemite Cheesybite 220g', item='2', brand='Vegemite')]
        for c in candidates:
            c['size'] = '220g'
        # Both real spread variants tie on words and price; stable ID decides.
        self.assertEqual(pricing.strongest_match('Vegemite', candidates)['store_item_id'], '1')

    def test_ambiguous_variants_match_the_plainest_one_not_a_guess(self):
        # Several Pepsi lines all genuinely contain "pepsi" at the exact
        # size typed -- real Coles/Woolworths data looks just like this.
        # No flavour was named, so the plain one wins; naming a flavour
        # should still pick that flavour specifically.
        plain = self.candidate('Soft Drink Cola Bottle', item='1', brand='Pepsi')
        maxx = self.candidate('Max No Sugar Cola Soft Drink Bottle', item='2', brand='Pepsi')
        vanilla = self.candidate('Max No Sugar Cola Vanilla Soft Drink Bottle', item='3', brand='Pepsi')
        for c in (plain, maxx, vanilla):
            c['size'] = '1.25L'
        self.assertEqual(pricing.strongest_match('1.25L Pepsi', [plain, maxx, vanilla]), plain)
        self.assertEqual(pricing.strongest_match('1.25L Pepsi Max', [plain, maxx, vanilla]), maxx)

    def test_brand_stripped_clean_out_of_the_name_still_counts_for_recall(self):
        # Coles in particular often stores the brand separately from a
        # plain descriptive name ("Soft Drink Cola Bottle", brand="Pepsi")
        # -- a brand-only query has to recall that word from the brand
        # field, not just the name text, or it can never match at all.
        candidate = self.candidate('Soft Drink Cola Bottle', brand='Pepsi')
        candidate['size'] = '1.25L'
        self.assertEqual(pricing.strongest_match('1.25L Pepsi', [candidate]), candidate)

    def test_candidate_must_preserve_every_typed_word(self):
        # Extra catalogue words do not cancel an explicitly requested cut.
        self.assertEqual(pricing.strongest_match('Pork Loin Chops', [self.candidate('Pork Chops'),
            self.candidate('Pork Loin Chops Boneless', '2')])['store_item_id'], '2')

    def test_plain_plural_isnt_treated_as_a_different_word(self):
        # A household item typed as "carrot" has to find a store listing
        # that only ever says "Carrots" -- same thing, not a name mismatch.
        self.assertEqual(pricing.name_words('carrot'), pricing.name_words('Carrots'))
        self.assertIsNotNone(pricing.strongest_match('carrot', [self.candidate('Carrots')]))
        self.assertIsNotNone(pricing.strongest_match('onion', [self.candidate('Brown Onions')]))

    def test_catalogue_plural_retrieval_needs_no_retry(self):
        with sqlite3.connect(':memory:') as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(database.SCHEMA)
            conn.execute("INSERT INTO woolworths_catalogue(stockcode,name) VALUES(1,'Carrots')")
            with patch.object(pricing, '_fetch', wraps=pricing._fetch) as fetch:
                result = pricing.search('woolworths', 'carrot', conn=conn)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(result[0]['name'], 'Carrots')

    def test_catalogue_stores_match_locally_never_live(self):
        import sqlite3
        from app import db as database
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.executescript(database.SCHEMA)
        db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size,price) VALUES
                (1,'Carrots 1kg','',  '1kg', 2.1),
                (2,'Baby Spinach 120g','','120g', 3.5);
            INSERT INTO woolworths_catalogue(stockcode,name,brand,size,price) VALUES
                (10,'Carrots 1kg','','1kg', 2.2);
            INSERT INTO aldi_catalogue(item_id,name,brand,size,price) VALUES
                ('a1','Carrots 1kg','','1kg', 1.9);
        """)
        db.commit()
        with patch.object(pricing.stores, 'search_coles') as coles_live, \
             patch.object(pricing.stores, 'search_coles_browser') as coles_browser, \
             patch.object(pricing.stores, 'search_woolworths') as ww_live, \
             patch.object(pricing.stores, 'search_aldi') as aldi_live:
            coles = pricing.search('coles', 'carrot', conn=db)
            woolworths = pricing.search('woolworths', 'carrot', conn=db)
            aldi = pricing.search('aldi', 'carrot', conn=db)
        coles_live.assert_not_called()
        coles_browser.assert_not_called()
        ww_live.assert_not_called()
        aldi_live.assert_not_called()
        self.assertEqual([r['name'] for r in coles if r['name'] == 'Carrots 1kg'], ['Carrots 1kg'])
        self.assertEqual(woolworths[0]['name'], 'Carrots 1kg')
        self.assertEqual(aldi[0]['name'], 'Carrots 1kg')

    def test_wildly_different_size_is_rejected_not_just_scored_lower(self):
        # Same words, very different pack -- a known-size history must stop
        # this from matching, not merely nudge the score around.
        small = self.candidate('Yoghurt Tub', brand='')
        small['size'] = '170g'
        big = self.candidate('Yoghurt Tub', item='2', brand='')
        big['size'] = '1kg'
        self.assertEqual(pricing.strongest_match('Yoghurt Tub', [small, big], preferred_size={'mass': 170}), small)
        self.assertEqual(pricing.strongest_match('Yoghurt Tub', [big], preferred_size={'mass': 170}), None)
        # No size claim on the candidate at all is not evidence of a mismatch.
        no_size = dict(self.candidate('Yoghurt Tub', item='3', brand=''), size='')
        self.assertEqual(pricing.strongest_match('Yoghurt Tub', [no_size], preferred_size={'mass': 170}), no_size)

    def test_supermarket_noise_words_and_real_brand_lock(self):
        self.assertEqual(pricing.name_words('WOOLWORTHS Chilli Coles Ground ALDI iga'), {'chilli', 'ground'})
        for store in ('Coles', 'Aldi', ''):
            candidate = self.candidate((store + ' Chilli Ground').strip(), brand=store)
            self.assertEqual(pricing.strongest_match('Woolworths Chilli Ground', [candidate]), candidate)
        own = self.candidate('Devondale Full Cream Milk', brand='Devondale')
        other = self.candidate('Milk', item='2', brand='Other')
        self.assertEqual(pricing.strongest_match('Devondale Milk', [own, other]), own)

    def test_brand_guard_and_ambiguous_candidates(self):
        own = self.candidate('Vegemite Spread Jar', brand='Vegemite')
        other = self.candidate('Vegemite', '2', 'Home Brand')
        self.assertIsNone(pricing.strongest_match('Vegemite', [own, other]))
        own = self.candidate('Vegemite Spread', brand='Vegemite')
        self.assertEqual(pricing.strongest_match('Vegemite', [own, other]), own)
        # Both colours tie on words and price; stable ID decides.
        self.assertEqual(pricing.strongest_match('Capsicum', [self.candidate('Red Capsicum'),
            self.candidate('Green Capsicum', '2')])['store_item_id'], '1')
        self.assertIsNone(pricing.strongest_match('Milk', [self.candidate('Dark Chocolate')]))
        self.assertEqual(pricing.strongest_match('Pork Loin Chops', [self.candidate('Pork Chops'),
            self.candidate('Pork Loin Chops Boneless', '2')])['store_item_id'], '2')


class StoreParsers(unittest.TestCase):
    def session(self, handler):
        return httpx.Client(transport=httpx.MockTransport(handler), headers=stores.HEADERS, timeout=15, follow_redirects=True)

    def test_woolworths_handshake_and_nested_products(self):
        requests = []
        def handler(request):
            requests.append(request)
            self.assertIn('Mozilla', request.headers['user-agent'])
            self.assertEqual(request.extensions['timeout']['read'], 15)
            if request.method == 'GET':
                return httpx.Response(200, headers={'Set-Cookie': 'session=ready; Path=/'})
            self.assertIn('session=ready', request.headers['cookie'])
            self.assertEqual(json.loads(request.content)['SearchTerm'], 'milk')
            self.assertEqual(json.loads(request.content)['PageSize'], 36)
            return httpx.Response(200, json={'Products': [{'Products': [{'Stockcode': 123, 'Name': 'Milk', 'Price': 0}]}]})
        with patch.object(stores, 'client', lambda: self.session(handler)):
            found = stores.search_woolworths('milk', 100)
        self.assertEqual(found[0]['store_item_id'], '123')
        self.assertEqual(found[0]['price'], 0)
        self.assertEqual([r.method for r in requests], ['GET', 'POST'])

    def test_coles_next_data_and_non_product_filter(self):
        data = {'props': {'pageProps': {'searchResults': {'results': [
            {'_type': 'AD'}, {'_type': 'PRODUCT', 'id': 123, 'name': 'Milk', 'pricing': {
                'now': 3.4, 'was': 4, 'unit': {'price': 1.7, 'ofMeasureQuantity': 1, 'ofMeasureType': 'L'}, 'onlineSpecial': True}}
        ]}}}}
        html = '<script nonce="x" type="application/json" id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
        def handler(request):
            self.assertEqual(request.url.params['q'], 'milk')
            return httpx.Response(200, text=html)
        with patch.object(stores, 'client', lambda: self.session(handler)):
            found = stores.search_coles('milk')
        self.assertEqual(found[0]['price'], 3.4)
        self.assertEqual(found[0]['unit_measure'], '1L')
        self.assertTrue(found[0]['on_special'])
        with patch.object(stores, 'client', lambda: self.session(lambda _: httpx.Response(200, text='<html/>'))):
            with self.assertRaises(ValueError):
                stores.search_coles('milk')

    def test_coles_browser_fallback_parses_the_same_next_data_shape(self):
        data = {'props': {'pageProps': {'searchResults': {'results': [
            {'_type': 'AD'}, {'_type': 'PRODUCT', 'id': 9, 'name': 'Carrots', 'pricing': {'now': 2.5, 'unit': {}}}
        ]}}}}
        found = stores._coles_products_from_next_data(json.dumps(data), 12)
        self.assertEqual(found, [{'store_item_id': '9', 'name': 'Carrots', 'brand': '', 'size': '',
                                   'price': 2.5, 'was_price': None, 'unit_price': None, 'unit_measure': '',
                                   'on_special': False}])
        with self.assertRaises(ValueError):
            stores._coles_products_from_next_data(json.dumps({'props': {}}), 12)

    def test_coles_retries_with_fresh_session_then_matches_from_the_catalogue(self):
        data = {'props': {'pageProps': {'searchResults': {'results': []}}}}
        html = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
        with patch.object(stores, 'client', side_effect=[
                self.session(lambda _: httpx.Response(200, text='<html/>')),
                self.session(lambda _: httpx.Response(200, text=html))]) as clients, patch.object(stores.time, 'sleep') as sleep:
            self.assertEqual(stores.search_coles('milk'), [])
            self.assertEqual(clients.call_count, 2)
            sleep.assert_called_once_with(0.5)
        db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(db.close)
        db.execute("UPDATE store_settings SET enabled=(store='coles')")
        db.commit()
        # Nothing in the local catalogue for it yet -- stays unmatched,
        # same as any store genuinely not carrying an item, no live
        # request and no error either.
        result = pricing.auto_match_all(db)
        self.assertEqual(result['unmatched'], [{'product_id': 1, 'name': 'Milk 2L'}])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM product_store_links').fetchone()[0], 0)
        db.execute("INSERT INTO coles_catalogue(coles_id,name,price) VALUES(1,'Milk 2L',3.4)")
        db.commit()
        result = pricing.auto_match_all(db)
        self.assertEqual(result['matched'], [{'product_id': 1, 'name': 'Milk 2L', 'store': 'coles'}])

    def test_aldi_fallback_and_per_kg_produce(self):
        arr = [{'products': 1}, [2], {'sku': 3, 'name': 4, 'price': 5}, '123', 'Brown Onions per kg', 2.49]
        paths = []
        def handler(request):
            paths.append(request.url.path)
            return httpx.Response(200, text='<script id="__NUXT_DATA__">' + json.dumps(arr) + '</script>')
        with patch.object(stores, '_categories', return_value=['/products/fresh/k/1']), \
                patch.object(stores, 'client', lambda: self.session(handler)):
            result = stores.search_aldi('brown onions')
        self.assertEqual(paths, ['/products/fresh/k/1'])
        self.assertEqual(result[0]['name'], 'Brown Onions per kg')
        self.assertEqual(result[0]['unit_measure'], 'kg')
        self.assertEqual(result[0]['unit_price'], 2.49)

    def test_browse_aldi_category_reads_real_field_names_and_skips_junk(self):
        # Real Aldi product dicts use brandName/sellingSize, not brand/size
        # -- and the page data also carries small locale/currency objects
        # ({"code":"AUD","name":"Australian Dollar"}) that look just enough
        # like a product to slip through without the sku check. Nuxt's
        # devalue format treats every int as an index into this same
        # array, not a literal -- same convention the other Aldi tests
        # above already follow.
        arr = [{'junk': 1, 'product': 2},
               {'code': 3, 'name': 4},
               {'sku': 5, 'name': 6, 'brandName': 7, 'sellingSize': 8, 'price': 9},
               'AUD', 'Australian Dollar', '123', 'Full Cream Milk', 'MAMIA', '2 L', {'amount': 10}, 350]
        def handler(request):
            return httpx.Response(200, text='<script id="__NUXT_DATA__">' + json.dumps(arr) + '</script>')
        with patch.object(stores, 'client', lambda: self.session(handler)):
            products, total = stores.browse_aldi_category('/products/dairy/k/1')
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['brand'], 'MAMIA')
        self.assertEqual(products[0]['size'], '2 L')
        self.assertEqual(products[0]['price'], 3.5)

    def test_nuxt_references_shared_slots_wrappers_and_cycle(self):
        data = [{'product': 1, 'same': 1, 'cycle': 5}, {'name': 2, 'price': 3}, 'Milk', 3.4, None, {'self': 5}]
        found = stores._unflatten(data)
        self.assertEqual(found['product'], {'name': 'Milk', 'price': 3.4})
        self.assertEqual(found['same'], found['product'])
        self.assertEqual(found['cycle'], {'self': None})
        self.assertEqual(stores._unflatten([['Reactive', 1], {'name': 2}, 'Milk']), {'name': 'Milk'})

    def test_aldi_nav_cached_category_payload_and_failure(self):
        arr = [{'products': 1}, [2], {'sku': 3, 'name': 4, 'price': 5}, '123', 'Dairy Milk', 3.4]
        urls = []
        def handler(request):
            urls.append(str(request.url))
            if request.url.path == '/':
                return httpx.Response(200, text='<a href="/products/dairy-eggs-fridge/k/960000000">Dairy</a>')
            return httpx.Response(200, text='<script id="__NUXT_DATA__">' + json.dumps(arr) + '</script>')
        with tempfile.TemporaryDirectory() as directory, patch.object(database, 'DATA_DIR', Path(directory)), \
                patch.object(stores, '_category_cache', (0, [])), patch.object(stores, 'client', lambda: self.session(handler)):
            self.assertEqual(stores.search_aldi('Dairy Milk')[0]['price'], 3.4)
            self.assertEqual(stores.search_aldi('Dairy Milk')[0]['store_item_id'], '123')
            self.assertEqual(sum(url.endswith('.au/') for url in urls), 1)
            self.assertEqual(stores.search_aldi('Unknown'), [])
            with patch.object(stores, 'payload', side_effect=ValueError('bad data')), self.assertLogs(level='WARNING'):
                self.assertTrue(stores.search_aldi('Dairy Milk').failed)

    def test_iga_resolves_nearest_store_and_browses_a_category(self):
        stores._IGA_STORE_CACHE.clear()

        def handler(request):
            if 'nominatim' in str(request.url):
                return httpx.Response(200, json=[{'lat': '-32.13', 'lon': '115.91'}])
            if '/near/' in str(request.url):
                return httpx.Response(200, json={'items': [{'retailerStoreId': 48323}]})
            self.assertTrue(request.url.path.endswith('/stores/48323/categories/Meat/search'))
            self.assertEqual(request.url.params['skip'], '36')
            self.assertEqual(request.url.params['take'], '36')
            self.assertNotIn('q', request.url.params)
            return httpx.Response(200, json={'total': 37, 'items': [
                {'productId': '80080037', 'name': 'Pork Loin Chops', 'brand': '', 'priceNumeric': 18.495,
                 'wasPriceNumeric': None, 'wholePrice': 36.99, 'unitOfPrice': {'abbreviation': 'kg'},
                 'unitOfSize': {'size': '500', 'abbreviation': 'g'}, 'tprPrice': []}]})
        with self.session(handler) as session:
            store_id = stores._iga_store_for_location(session, 'Piara Waters')
            found, total = stores.browse_iga_category(store_id, 'Meat', page=2, session=session)
        self.assertEqual(total, 37)
        self.assertEqual(found[0]['item_id'], '80080037')
        self.assertEqual(found[0]['size'], '500 g')
        self.assertEqual(found[0]['price'], 18.495)
        with self.session(lambda r: httpx.Response(200, json={})) as session:
            with self.assertRaisesRegex(ValueError, 'Missing IGA'):
                stores.browse_iga_category(store_id, 'Meat', session=session)


class ColesCatalogueHistory(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)
        self.addCleanup(self.db.close)
        self.db.execute("INSERT INTO coles_catalogue(coles_id,name,price) VALUES(1,'Coffee Beans',20)")
        self.db.commit()

    def record(self, price, was_price=None):
        self.db.execute("""INSERT INTO coles_catalogue_history(coles_id,price,was_price,checked_at)
            VALUES(1,?,?,datetime('now'))""", (price, was_price))
        self.db.commit()

    def test_a_special_that_matches_the_usual_price_is_flagged_fake(self):
        # Permanently "on special" at the same price it always sells for --
        # exactly the Reddit-tracker pattern this is meant to catch.
        for _ in range(5):
            self.record(38, was_price=60)
        result = pricing.coles_catalogue_history(self.db, 1)
        self.assertEqual(result['typical_price'], 38)
        self.assertTrue(all(h['fake_special'] for h in result['history']))

    def test_a_genuine_drop_below_the_usual_price_is_not_flagged(self):
        for _ in range(4):
            self.record(10)
        self.record(6, was_price=10)
        result = pricing.coles_catalogue_history(self.db, 1)
        self.assertEqual(result['typical_price'], 10)
        self.assertFalse(result['history'][-1]['fake_special'])

    def test_no_history_yet_returns_no_typical_price(self):
        result = pricing.coles_catalogue_history(self.db, 1)
        self.assertIsNone(result['typical_price'])
        self.assertEqual(result['history'], [])


class ProductsHistoryLogic(unittest.TestCase):
    def test_lazy_history_empty_chart_and_plotting(self):
        import re
        import subprocess
        template = (Path(__file__).resolve().parents[1] / 'app/templates/products.html').read_text()
        self.assertIn('<template x-if="historyChart">', template)
        script = re.findall(r'<script>(.*?)</script>', template, re.S)[0]
        program = r'''
const assert = require('node:assert/strict');
const calls = [];
let history = {stores:[], purchases:[]};
let resolveHistory;
let delayed = false;
const api = async path => {
  calls.push(path);
  if (path.endsWith('/history')) {
    if (delayed) return new Promise(resolve => {resolveHistory = resolve;});
    return history;
  }
  if (path === '/api/products/1') return {id:1, barcodes:[]};
  return [];
};
const oops = e => {throw e;};
(async () => {
  const page = productsPage();
  await page.load();
  await page.edit(null);
  assert.ok(!calls.some(p => p.endsWith('/history')));
  assert.equal(page.historyChart, null);
  await page.edit({id:1});
  assert.equal(calls.filter(p => p.endsWith('/history')).length, 1);
  assert.equal(page.historyChart, null);
  const check = {id:1, price:10, checked_at:'2026-10-01 12:00:00', fake_special:true};
  history = {stores:[{store:'coles', enabled:1, history:[check]},
    {store:'iga', enabled:0, history:[{...check, price:100}]}],
    purchases:[{id:2, price:0, at:'2026-10-02'}, {id:3, price:null, at:'2026-10-02'}]};
  await page.edit({id:1});
  const chart = page.historyChart;
  assert.equal(chart.stores.length, 1);
  assert.equal(chart.purchases.length, 1);
  assert.equal(chart.low, '$0.00');
  assert.equal(chart.high, '$10.00');
  assert.match(chart.stores[0].points[0].label, /not really cheaper than usual/);
  assert.ok(Number.isFinite(chart.purchases[0].x));
  const element = tag => ({tag, children:[], attrs:{},
    setAttribute(key, value) {this.attrs[key] = value;},
    appendChild(child) {this.children.push(child);},
    replaceChildren() {this.children = [];}});
  global.document = {createElementNS: (namespace, tag) => {
    assert.equal(namespace, 'http://www.w3.org/2000/svg');
    return element(tag);
  }};
  const svg = element('svg');
  page.drawHistory(svg, chart);
  assert.equal(svg.children.filter(e => e.tag === 'polyline').length, 1);
  const circles = svg.children.filter(e => e.tag === 'circle');
  assert.equal(circles.length, 2);
  assert.equal(circles[0].attrs.fill, 'var(--card)');
  assert.match(circles[0].children[0].textContent, /not really cheaper than usual/);
  page.drawHistory(svg, null);
  assert.equal(svg.children.length, 0);
  const single = page.buildHistoryChart({stores:[], purchases:[{id:1, price:0, at:'2026-10-01'}]});
  assert.equal(single.purchases[0].x, 298);
  assert.equal(single.purchases[0].y, 95);
  assert.equal(page.buildHistoryChart({stores:[], purchases:[{price:null, at:'2026-10-01'}]}), null);
  delayed = true;
  const pending = page.edit({id:1});
  await new Promise(resolve => setImmediate(resolve));
  await page.edit(null);
  resolveHistory(history);
  await pending;
  assert.equal(page.form.id, null);
  assert.equal(page.historyChart, null);
})().catch(e => {console.error(e); process.exit(1);});
'''
        result = subprocess.run(['node'], input='function drawHistory(svg, chart)' + (Path(__file__).resolve().parents[1] / 'app/static/app.js').read_text().split('function drawHistory(svg, chart)')[1] + script + program, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
