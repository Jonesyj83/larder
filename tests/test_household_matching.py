import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from app import db as database, order_history, pricing
from test_pricing import prepare


class HouseholdMatching(unittest.TestCase):
    def setUp(self):
        self.db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(self.db.close)
        self.db.execute("UPDATE products SET name='Ham' WHERE id=1")
        self.db.execute("UPDATE store_settings SET enabled=(store='coles')")
        self.plain = dict(store_item_id='1', name='Ham', brand='Coles', size='100g', price=1)
        self.bought = dict(store_item_id='2', name='Leg Shaved Ham', brand='Deli', size='100g', price=4)
        for c in (self.plain, self.bought):
            self.db.execute('INSERT INTO coles_catalogue(coles_id,name,brand,size,price) VALUES(?,?,?,?,?)',
                            (c['store_item_id'], c['name'], c['brand'], c['size'], c['price']))
        self.db.commit()

    def run_match(self, path, candidates):
        with patch.object(pricing, 'search', return_value=candidates):
            if path == 'batch':
                pricing.auto_match_all(self.db)
            elif path == 'live':
                pricing.auto_match_live(self.db, 1, 'Ham')
            else:
                pricing.auto_match(self.db, 1, 'Ham')
        return self.db.execute("SELECT * FROM product_store_links WHERE store='coles'").fetchone()

    def test_locked_store_never_searches_changed_catalogue_in_any_path(self):
        pricing.link_product(self.db, 1, 'coles', '2', self.bought['name'], matched_by=1)
        self.db.execute("DELETE FROM coles_catalogue WHERE coles_id='2'")
        with patch.object(pricing, 'search', side_effect=AssertionError('locked store searched')) as search:
            pricing.auto_match(self.db, 1, 'Ham')
            pricing.auto_match_live(self.db, 1, 'Ham')
            pricing.auto_match_all(self.db)
        search.assert_not_called()
        self.assertEqual(self.db.execute('SELECT store_item_id FROM product_store_links').fetchone()[0], '2')

    def test_purchase_identity_survives_unlink_and_beats_plain_cheap_option(self):
        pricing.link_product(self.db, 1, 'coles', '2', self.bought['name'], matched_by=1)
        order_history.record(self.db, dict(product_id=1, amount=1, unit_id=1), 'coles')
        self.db.execute('DELETE FROM product_store_links')
        for path in ('automatic', 'live', 'batch'):
            with self.subTest(path=path):
                self.db.execute('DELETE FROM product_store_links')
                # Remembered IDs are looked up directly, even outside the search window.
                link = self.run_match(path, [self.plain])
                self.assertEqual(link['store_item_id'], '2')
                self.assertEqual(link['needs_review'], 0)
                self.assertEqual(link['is_substitute'], 0)

    def test_remembered_type_precedes_plain_title_in_every_path(self):
        pricing.link_product(self.db, 1, 'woolworths', 'other', 'Leg Shaved Ham', matched_by=1)
        for path in ('automatic', 'live', 'batch'):
            with self.subTest(path=path):
                self.db.execute("DELETE FROM product_store_links WHERE store='coles'")
                link = self.run_match(path, [self.plain, self.bought])
                self.assertEqual(link['store_item_id'], '2')

    def test_unavailable_remembered_identity_is_a_substitute(self):
        pricing.link_product(self.db, 1, 'coles', '2', self.bought['name'], matched_by=1)
        order_history.record(self.db, dict(product_id=1, amount=1, unit_id=1), 'coles')
        self.db.execute('DELETE FROM product_store_links')
        self.db.execute("DELETE FROM coles_catalogue WHERE coles_id='2'")
        link = self.run_match('automatic', [self.plain])
        self.assertEqual(link['is_substitute'], 1)
        self.assertEqual(link['needs_review'], 0)
        pricing.link_product(self.db, 1, 'coles', '1', 'Ham', matched_by=1)
        self.assertEqual(self.db.execute('SELECT is_substitute FROM product_store_links').fetchone()[0], 1)

    def test_no_history_uses_plainest_then_cheapest(self):
        expensive = dict(self.plain, store_item_id='3', price=5)
        link = self.run_match('automatic', [self.bought, expensive, self.plain])
        self.assertEqual(link['store_item_id'], '1')
        self.assertEqual(link['is_substitute'], 0)

    def test_water_source_words_do_not_force_a_national_brand(self):
        fiji = dict(store_item_id='fiji', name='Water 1.5L', brand='Fiji', size='1.5L', price=3.1)
        home = dict(store_item_id='home', name='Natural Spring Water', brand='Coles', size='1.5L', price=1)
        selected = pricing.strongest_match('water', [fiji, home], store='coles')
        self.assertEqual(selected['store_item_id'], 'home')

    def test_descriptive_bare_titles_remain_eligible_but_prepared_foods_do_not(self):
        soup = dict(self.plain, name='Pea Ham Soup', price=.5)
        selected = pricing.strongest_match('ham', [soup, self.bought], store='coles')
        self.assertEqual(selected['store_item_id'], '2')
        bread = dict(self.plain, name='White Sandwich Sliced Bread', brand='BAKERS LIFE')
        cake = dict(self.plain, store_item_id='cake', name='Banana Bread Mix', price=.5)
        self.assertEqual(pricing.strongest_match('bread', [cake, bread], store='aldi'), bread)

    def test_remembered_national_brand_precedes_a_cheaper_house_label(self):
        self.db.execute("INSERT INTO woolworths_catalogue(stockcode,name,brand,size,price) VALUES(11,'Ham','Deli','100g',4)")
        pricing.link_product(self.db, 1, 'woolworths', '11', 'Ham', matched_by=1)
        for path in ('automatic', 'live', 'batch'):
            with self.subTest(path=path):
                self.db.execute("DELETE FROM product_store_links WHERE store='coles'")
                link = self.run_match(path, [self.plain, self.bought])
                self.assertEqual((link['store_item_id'], link['is_substitute']), ('2', 0))

    def test_upgrade_marks_existing_substitutes_without_inventing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conn = prepare(sqlite3.connect(root / 'larder.db'))
            conn.execute("UPDATE products SET name='Fountain Tomato Sauce' WHERE id=1")
            conn.execute("INSERT INTO coles_catalogue(coles_id,name,brand) VALUES(1,'Tomato Sauce','Fountain')")
            conn.execute("INSERT INTO aldi_catalogue(item_id,name,brand) VALUES('sauce','Tomato Sauce','COLWAY')")
            conn.execute("INSERT INTO product_store_links(product_id,store,store_item_id,store_name) VALUES(1,'aldi','sauce','Tomato Sauce')")
            conn.execute("INSERT INTO order_history(product_id,amount,unit_id,store) VALUES(1,1,1,'aldi')")
            conn.execute('ALTER TABLE product_store_links DROP COLUMN is_substitute')
            for column in ('store_item_id', 'store_name', 'store_brand', 'is_substitute'):
                conn.execute(f'ALTER TABLE order_history DROP COLUMN {column}')
            conn.commit()
            conn.close()
            with patch.multiple(database, DATA_DIR=root, DB_PATH=root / 'larder.db', UPLOAD_DIR=root / 'uploads'):
                for _ in range(2):
                    database.init_db()
                    conn = database.connect()
                    link = conn.execute('SELECT * FROM product_store_links').fetchone()
                    self.assertEqual((link['store_item_id'], link['is_substitute'], link['needs_review']), ('sauce', 1, 0))
                    history = conn.execute('SELECT * FROM order_history').fetchall()
                    self.assertEqual(len(history), 1)
                    self.assertEqual((history[0]['store_item_id'], history[0]['store_brand']), ('', ''))
                    self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
                    conn.close()

    def test_known_brand_substitute_does_not_relabel_a_generic_category(self):
        known = {'fountain', 'milk'}
        sauce = dict(self.plain, name='Tomato Sauce', brand='COLWAY')
        self.assertTrue(pricing.substitute_pick('Fountain tomato sauce', 'aldi', sauce, known))
        self.assertTrue(pricing.substitute_pick('1L Fountain tomato sauce', 'aldi', sauce, known))
        self.assertFalse(pricing.substitute_pick('Fountain tomato sauce', 'coles',
                         dict(sauce, brand='Fountain'), known))
        self.assertFalse(pricing.substitute_pick('1L milk', 'aldi',
                         dict(self.plain, name='Full Cream Milk', brand='FARMDALE'), known))

    def test_remembered_classification_precedes_an_unclassified_cheap_guess(self):
        preferred = {frozenset({'packaged', 'ham'})}
        wanted = dict(self.bought, classification={'secondary': [['Packaged Ham']]})
        picked = pricing.strongest_match('ham', [self.plain, wanted], store='coles',
                                         preferred_classification=preferred)
        self.assertEqual(picked['store_item_id'], '2')

    def test_missing_remembered_type_falls_back_as_a_substitute(self):
        pricing.link_product(self.db, 1, 'woolworths', 'other', 'Smoked Ham', matched_by=1)
        link = self.run_match('automatic', [self.plain])
        self.assertEqual((link['store_item_id'], link['is_substitute']), ('1', 1))

    def test_private_label_substitute_is_distinct_from_stocked_brand_and_review(self):
        self.db.execute("UPDATE products SET name='Smiths cheese and onion' WHERE id=1")
        self.db.execute("UPDATE store_settings SET enabled=(store='aldi')")
        self.db.execute("INSERT INTO aldi_catalogue(item_id,name,brand,size,price) VALUES('chips','Chips Cheese & Onion 175g','SPRINTERS','175g',2)")
        candidates = pricing.search('aldi', 'Smiths cheese and onion', conn=self.db)
        selected = pricing.strongest_match('Smiths cheese and onion', candidates, store='aldi')
        self.assertIn('_match_query', selected)
        self.assertTrue(pricing.substitute_pick('Smiths cheese and onion', 'aldi', selected))
        with patch.object(pricing, 'search', return_value=candidates):
            pricing.auto_match_live(self.db, 1, 'Smiths cheese and onion')
        link = self.db.execute('SELECT * FROM product_store_links').fetchone()
        self.assertEqual((link['is_substitute'], link['needs_review']), (1, 0))
        pricing.link_product(self.db, 1, 'aldi', 'chips', selected['name'], matched_by=1)
        self.assertEqual(self.db.execute('SELECT is_substitute FROM product_store_links').fetchone()[0], 1)
        self.db.execute("INSERT INTO aldi_catalogue(item_id,name,brand,size,price) VALUES('direct','Cheese & Onion Chips','Smiths','175g',4)")
        direct = pricing.search('aldi', 'Smiths cheese and onion', conn=self.db)
        selected = pricing.strongest_match('Smiths cheese and onion', direct, store='aldi')
        self.assertEqual(selected['store_item_id'], 'direct')
        self.assertFalse(pricing.substitute_pick('Smiths cheese and onion', 'aldi', selected))
        self.assertFalse(pricing.substitute_pick('Chips Cheese Onion', 'aldi', candidates[0] | {'_match_query': None}))
        tied = pricing.strongest_match('grated cheese', [
            dict(self.plain, name='Grated Tasty Cheese'),
            dict(self.bought, name='Grated Parmesan Cheese')], store='coles')
        self.assertTrue(tied['_ambiguous'])
        self.assertFalse(pricing.substitute_pick('grated cheese', 'coles', tied))

    def test_new_item_searches_other_stores_with_seed_description_without_brand(self):
        self.db.execute('UPDATE store_settings SET enabled=1')
        calls = []

        def fetch(store, query, postcode='', conn=None):
            calls.append((store, query))
            brand = {'aldi': 'BERG', 'coles': 'Coles', 'iga': 'Community Co',
                     'woolworths': 'Woolworths'}[store]
            wanted = dict(self.bought, name=brand + ' Leg Shaved Ham', brand=brand)
            return [wanted] if store == 'aldi' else [dict(self.plain, brand=brand), wanted]

        for path in ('automatic', 'batch', 'live'):
            with self.subTest(path=path):
                self.db.execute('DELETE FROM product_store_links')
                calls.clear()
                with patch.object(pricing, 'search', side_effect=fetch):
                    if path == 'automatic':
                        matched = pricing.auto_match(self.db, 1, 'Ham', search_fn=fetch)
                    elif path == 'batch':
                        matched = pricing.auto_match_all(self.db, search_fn=fetch, before_search=lambda: True)['matched']
                    else:
                        matched = pricing.auto_match_live(self.db, 1, 'Ham')
                self.assertEqual(len(matched), 4)
                self.assertEqual(calls[0], ('aldi', 'Ham'))
                for store, query in calls[1:]:
                    self.assertEqual(pricing.name_words(query), {'leg', 'shaved', 'ham'})
                    self.assertNotIn('berg', query.casefold())
                self.assertEqual(len(calls), 4)
                self.assertTrue(all('Leg Shaved Ham' in row[0] for row in self.db.execute(
                    'SELECT store_name FROM product_store_links')))

    def test_ambiguous_store_waits_for_a_clean_seed_and_is_searched_again(self):
        self.db.execute('UPDATE store_settings SET enabled=1')
        calls = []

        def fetch(store, query, postcode):
            calls.append((store, query))
            if store == 'aldi' and query == 'Ham':
                return [dict(self.plain, name='Smoked Ham'), self.bought]
            return [self.bought]

        matched = pricing.auto_match(self.db, 1, 'Ham', search_fn=fetch)
        self.assertEqual(len(matched), 4)
        self.assertEqual(calls[:2], [('aldi', 'Ham'), ('coles', 'Ham')])
        self.assertEqual(calls[2], ('aldi', 'ham leg shaved'))
        self.assertTrue(all(query == 'ham leg shaved' for _, query in calls[2:]))

    def test_seed_retrieval_preserves_explicit_pack_request(self):
        self.db.execute('UPDATE store_settings SET enabled=1')
        calls = []
        pack = dict(self.plain, name='Natural Spring Water 24x600ml', size='24x600ml')

        def fetch(store, query, postcode):
            calls.append(query)
            return [pack] if store == 'aldi' else [dict(pack, size='600ml', name='Natural Spring Water')]

        matched = pricing.auto_match(self.db, 1, 'water 24', search_fn=fetch)
        self.assertEqual([m['store'] for m in matched], ['aldi'])
        self.assertTrue(all(pricing.pack_size(q).get('count') == 24 for q in calls[1:]))

    def test_existing_history_keeps_the_household_query(self):
        order_history.record(self.db, dict(product_id=1, amount=1, unit_id=1))
        self.db.execute('UPDATE store_settings SET enabled=1')
        queries = []

        def fetch(store, query, postcode):
            queries.append(query)
            return [self.bought]

        pricing.auto_match(self.db, 1, 'Ham', search_fn=fetch)
        self.assertEqual(queries, ['Ham'] * 4)
