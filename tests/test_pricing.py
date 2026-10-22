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
        self.searches['iga'].return_value = [CANDIDATE]
        self.assertEqual((await self.client.get('/api/pricing/search?store=iga&q=milk')).json(), [CANDIDATE])
        self.searches['iga'].assert_called_once_with('milk', postcode='6000')
        for store in ('coles', 'aldi', 'woolworths'):
            self.searches[store].return_value = [CANDIDATE]
            self.assertEqual((await self.client.get(f'/api/pricing/search?store={store}&q=milk')).json(), [CANDIDATE])
        self.searches['coles'].side_effect = ValueError('parse')
        self.searches['coles_browser'].side_effect = ValueError('bot check too')
        with self.assertLogs(level='WARNING'):
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
        self.db.commit()

        def coles_lookup(query):
            return {'Capsicum': [dict(CANDIDATE, name='Red Capsicum')]}.get(query, [])

        def iga_lookup(query, postcode=''):
            if query.startswith('Milk'):
                raise RuntimeError('offline')
            return {'Bread': [dict(CANDIDATE, name='Bread')]}.get(query, [])

        self.searches['coles'].side_effect = coles_lookup
        self.searches['iga'].side_effect = iga_lookup
        with patch.object(pricing.time, 'sleep') as sleep, self.assertLogs(level='WARNING'):
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
        self.db.commit()
        self.searches['coles'].return_value = [dict(CANDIDATE, name='MILK 2 L')]
        self.searches['woolworths'].return_value = [CANDIDATE, dict(CANDIDATE, store_item_id='456')]
        self.searches['aldi'].return_value = [dict(CANDIDATE, name='Dark Chocolate Block')]
        response = await self.client.post('/api/products', json={'name': 'Milk 2L', 'unit_id': 1})
        self.assertEqual(response.status_code, 201)
        product_id = response.json()['id']
        links = (await self.client.get(f'/api/pricing/products/{product_id}')).json()
        self.assertEqual([link['store'] for link in links], ['coles'])
        self.assertEqual(links[0]['latest_check']['price'], 3.4)
        self.searches['iga'].assert_not_called()
        await self.client.put(f'/api/products/{product_id}', json={'name': 'Milk', 'unit_id': 1})
        self.assertEqual(self.searches['coles'].call_count, 1)

    async def test_creation_survives_site_and_whole_hook_failure(self):
        self.searches['coles'].side_effect = httpx.ConnectError('offline')
        self.searches['coles_browser'].side_effect = httpx.ConnectError('offline')
        with self.assertLogs(level='WARNING'):
            response = await self.client.post('/api/products', json={'name': 'Milk', 'unit_id': 1})
        self.assertEqual(response.status_code, 201)
        self.searches['woolworths'].assert_called()
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

    def test_plain_plural_isnt_treated_as_a_different_word(self):
        # A household item typed as "carrot" has to find a store listing
        # that only ever says "Carrots" -- same thing, not a name mismatch.
        self.assertEqual(pricing.name_words('carrot'), pricing.name_words('Carrots'))
        self.assertIsNotNone(pricing.strongest_match('carrot', [self.candidate('Carrots')]))
        self.assertIsNotNone(pricing.strongest_match('onion', [self.candidate('Brown Onions')]))

    def test_search_retries_the_other_plural_form_on_an_empty_result(self):
        calls = []
        def fake_search(store, query, postcode=''):
            calls.append(query)
            return [self.candidate('Carrots')] if query == 'carrots' else []
        with patch.object(pricing.stores, 'search_coles', side_effect=lambda q: fake_search('coles', q)):
            result = pricing.search('coles', 'carrot')
        self.assertEqual(calls, ['carrot', 'carrots'])
        self.assertEqual(result, [self.candidate('Carrots')])

    def test_coles_falls_back_to_the_browser_when_the_plain_request_is_blocked(self):
        with patch.object(pricing.stores, 'search_coles', side_effect=ValueError('bot check')), \
             patch.object(pricing.stores, 'search_coles_browser', return_value=[self.candidate('Carrots')]) as browser:
            self.assertEqual(pricing.search('coles', 'carrot'), [self.candidate('Carrots')])
        browser.assert_called_once_with('carrot')
        # And if the browser fallback is ALSO down, the original failure is
        # what should surface -- not a confusing second-hand error.
        with patch.object(pricing.stores, 'search_coles', side_effect=ValueError('bot check')), \
             patch.object(pricing.stores, 'search_coles_browser', side_effect=ValueError('no chromium')):
            with self.assertRaises(ValueError) as ctx:
                pricing.search('coles', 'carrot')
        self.assertEqual(str(ctx.exception), 'bot check')
        # Other stores never touch the browser path at all.
        with patch.object(pricing.stores, 'search_woolworths', return_value=[]), \
             patch.object(pricing.stores, 'search_coles_browser') as browser:
            pricing.search('woolworths', 'carrot')
        browser.assert_not_called()

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
        no_size = self.candidate('Yoghurt Tub', item='3', brand='')
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
        self.assertIsNone(pricing.strongest_match('Capsicum', [self.candidate('Red Capsicum'),
            self.candidate('Green Capsicum', '2')]))
        self.assertIsNone(pricing.strongest_match('Milk', [self.candidate('Dark Chocolate')]))
        self.assertIsNone(pricing.strongest_match('Pork Loin Chops', [self.candidate('Pork Chops'),
            self.candidate('Pork Loin Chops Boneless', '2')]))


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

    def test_coles_retries_with_fresh_session_and_leaves_failure_unmatched(self):
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
        with patch.object(stores, 'client', lambda: self.session(lambda _: httpx.Response(200, text='<html/>'))), \
                patch.object(stores.time, 'sleep'), self.assertLogs(level='WARNING'):
            result = pricing.auto_match_all(db)
        self.assertEqual(result['unmatched'], [{'product_id': 1, 'name': 'Milk 2L'}])
        self.assertEqual(db.execute('SELECT COUNT(*) FROM product_store_links').fetchone()[0], 0)

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

    def test_iga_resolves_nearest_store_and_normalizes_results(self):
        with self.assertRaisesRegex(ValueError, 'requires an area or postcode'):
            stores.search_iga('Milk', postcode='')

        def handler(request):
            if 'nominatim' in str(request.url):
                return httpx.Response(200, json=[{'lat': '-32.13', 'lon': '115.91'}])
            if '/near/' in str(request.url):
                return httpx.Response(200, json={'items': [{'retailerStoreId': 48323}]})
            self.assertEqual(request.url.params['q'], 'pork chops')
            return httpx.Response(200, json={'items': [
                {'productId': 80080037, 'name': 'Pork Loin Chops', 'brand': '', 'priceNumeric': 18.495,
                 'wasPriceNumeric': None, 'wholePrice': 36.99, 'unitOfPrice': {'abbreviation': 'kg'},
                 'unitOfSize': {'size': '500', 'abbreviation': 'g'}, 'tprPrice': None},
                {'productId': '', 'name': '', 'priceNumeric': 1},
            ]})
        with patch.object(stores, 'client', lambda: self.session(handler)):
            found = stores.search_iga('pork chops', postcode='Piara Waters')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['store_item_id'], '80080037')
        self.assertEqual(found[0]['size'], '500 g')
        self.assertEqual(found[0]['price'], 18.495)
        with patch.object(stores, 'client', lambda: self.session(lambda r: httpx.Response(500))), self.assertLogs(level='WARNING'):
            stores._IGA_STORE_CACHE.clear()
            self.assertTrue(stores.search_iga('pork chops', postcode='Piara Waters').failed)


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
