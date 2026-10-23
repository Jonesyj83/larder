import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from app import db as database, pricing
from app.pricing import stores
from scripts.aldi_catalogue_crawl import crawl


class CatalogueSafety(unittest.TestCase):
    def test_aldi_migration_backfills_breadcrumbs_without_restarting_crawl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(database, 'DATA_DIR', root), patch.object(database, 'UPLOAD_DIR', root / 'uploads'), patch.object(database, 'DB_PATH', root / 'larder.db'):
                db = database.connect()
                db.executescript(database.SCHEMA.replace("    classification TEXT NOT NULL DEFAULT '{}',\n", ''))
                category = '/products/meat-seafood/sausage/k/1111111147'
                db.execute("INSERT INTO aldi_catalogue(item_id,name,category) VALUES('1','Pork Sausages 500g',?)", (category,))
                db.execute("INSERT INTO aldi_crawl_progress(category,next_page,done) VALUES(?,2,1)", (category,))
                db.commit()
                db.close()
                database.init_db()
                db = database.connect()
                self.assertEqual(json.loads(db.execute('SELECT classification FROM aldi_catalogue').fetchone()[0])['primary'], [['meat seafood', 'sausage']])
                self.assertEqual(tuple(db.execute('SELECT next_page,done FROM aldi_crawl_progress').fetchone()), (2, 1))
                self.assertEqual(pricing.search('aldi', 'sausages', conn=db)[0]['classification']['primary'], [['meat seafood', 'sausage']])
                db.execute('DELETE FROM aldi_crawl_progress')
                product = dict(item_id='1', name='Pork Sausages 500g', brand='', size='500g', price=4, was_price=None, unit_price=None, unit_measure='')
                crawl(db, categories=[category], fetch=lambda *_: ([product], 1), sleep=lambda _: None, log=lambda *_: None)
                self.assertEqual(json.loads(db.execute('SELECT classification FROM aldi_catalogue').fetchone()[0]), stores.aldi_classification(category))
                db.close()

    def test_aldi_url_paths_preserve_single_and_multiple_levels(self):
        self.assertEqual(stores.aldi_classification('/products/deli-chilled-meats/k/930000000')['primary'], [['deli chilled meats']])
        self.assertEqual(stores.aldi_classification('/products/pantry/confectionery/k/1111111181')['primary'], [['pantry', 'confectionery']])
        for value in ('', 'invalid', None):
            self.assertEqual(stores.aldi_classification(value), {})

    def test_nonfood_departments_require_typed_context_even_without_alternatives(self):
        for label, context in [('Dog and Puppy', 'dog'), ('Cat Food', 'cat'), ('Baby Food', 'baby'),
                               ('Hair Care', 'shampoo'), ('Cleaning Products', 'cleaner'),
                               ('Red Wine', 'wine'), ('Homewares', 'container')]:
            candidate = dict(store_item_id='1', name=context + ' Coconut', brand='', price=1,
                             classification={'primary': [[label]]})
            with self.subTest(label=label):
                self.assertIsNone(pricing.strongest_match('coconut', [candidate]))
                self.assertIsNotNone(pricing.strongest_match(context + ' coconut', [candidate]))
        for label in ['Health Foods', 'Sports and Diet Nutrition', 'Meat']:
            candidate = dict(store_item_id='1', name='Coconut', brand='', price=1,
                             classification={'primary': [[label]]})
            self.assertIsNotNone(pricing.strongest_match('coconut', [candidate]))

    def test_seed_preserves_original_class_and_retries_missing_store_description(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript(database.SCHEMA)
        db.execute("INSERT INTO units(id,name,name_plural) VALUES(1,'piece','pieces')")
        db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'sausages',1)")
        for store in ('coles', 'aldi', 'woolworths'):
            db.execute('INSERT INTO store_settings(store,postcode,enabled) VALUES(?,?,1)', (store, ''))
        def item(key, name, path):
            return dict(store_item_id=key, name=name, brand='', price=3, classification={'primary': [path]})
        seed = item('1', 'Classic Beef Sausages', ['BBQ MEAT', 'SAUSAGES'])
        pork = item('2', 'Pork Sausages', ['meat seafood', 'sausage'])
        roll = item('3', 'Classic Beef Sausage Roll', ['MEAT CONVENIENCE', 'VALUE ADDED PROTEIN', 'OTHER'])
        beef = item('4', 'Classic Beef Sausages', ['MEAT CONVENIENCE', 'SAUSAGES', 'GOURMET SAUSAGES'])
        # Aldi defers until a clean seed is found at Coles; its own wording
        # still qualifies when the seed's beef descriptor is not stocked.
        pork['_ambiguous'] = True
        def search(store, query, postcode):
            return {'aldi': [pork], 'coles': [seed], 'woolworths': [roll, beef]}[store]
        pricing.auto_match(db, 1, 'sausages', search_fn=search)
        links = {r['store']: r['store_item_id'] for r in db.execute('SELECT * FROM product_store_links')}
        self.assertEqual(links, {'aldi': '2', 'coles': '1', 'woolworths': '4'})

    def test_ampersand_initials_keep_identity_and_possessive_spelling(self):
        for spelling in ['M&M', 'M&Ms', "M&M's", 'M & M', 'M & Ms']:
            for singular in (True, False):
                self.assertEqual(pricing.name_words('peanut ' + spelling, singular=singular), {'peanut', 'm&m'})
        self.assertEqual(pricing.name_words('S & B'), {'s&b'})
        self.assertEqual(pricing.name_words('M&S'), {'m&s'})
        self.assertEqual(pricing.name_words("Ben & Jerry's"), {'ben', 'jerry'})
        self.assertEqual(pricing.name_words("Arnott's"), {'arnott'})
        self.assertEqual(pricing.name_words('salt & vinegar'), {'salt', 'vinegar'})

    def test_ampersand_retrieval_covers_stored_brand_spellings(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript(database.SCHEMA)
        for i, brand in enumerate(["M&M'S", 'M&Ms', 'M&m']):
            db.execute('INSERT INTO coles_catalogue(coles_id,name,brand) VALUES(?,?,?)', (i, 'Peanut Milk Chocolate', brand))
        db.execute("INSERT INTO coles_catalogue(coles_id,name,brand) VALUES(9,'Peanut Butter Std','M')")
        candidates = pricing.search('coles', 'peanut m&m', conn=db)
        self.assertEqual({c['store_item_id'] for c in candidates if 'm&m' in pricing.name_words(c['brand'])}, {'0', '1', '2'})
        self.assertIn(pricing.strongest_match('peanut m&m', candidates)['store_item_id'], {'0', '1', '2'})
        self.assertIsNone(pricing.strongest_match('peanut m&m', [c for c in candidates if c['store_item_id'] == '9']))

    def test_food_department_labels_are_not_nonfood_keyword_matches(self):
        for label in ['ENTERTAINMENT', 'Frozen Pies and Party Food', 'Health Foods']:
            candidate = dict(store_item_id='1', name='Cheese Dip', brand='', price=1,
                             classification={'primary': [[label]]})
            self.assertIsNotNone(pricing.strongest_match('cheese dip', [candidate]))
        for label, query in [('Health Care', 'medicine'), ('Pet Accessories', 'dog')]:
            self.assertTrue(pricing._department_allowed(pricing.name_words(query),
                            {'classification': {'primary': [[label]]}}))

    def test_new_items_use_catalogue_safety_through_both_matching_flows(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/catalogue_safety.json').read_text())
        for flow in (pricing.auto_match, pricing.auto_match_live):
            for query in ('sausages', 'peanut m&m', 'chicken breast'):
                with self.subTest(flow=flow.__name__, query=query):
                    db = sqlite3.connect(':memory:')
                    db.row_factory = sqlite3.Row
                    self.addCleanup(db.close)
                    db.executescript(database.SCHEMA)
                    db.execute("INSERT INTO units(id,name,name_plural) VALUES(1,'piece','pieces')")
                    db.execute('INSERT INTO products(id,name,unit_id) VALUES(1,?,1)', (query,))
                    db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'48108','piara waters')")
                    for store, rows in fixture.items():
                        db.execute('INSERT INTO store_settings(store,postcode,enabled) VALUES(?,?,1)', (store, 'Piara Waters'))
                        for row in rows:
                            columns = ','.join(row)
                            db.execute(f'INSERT INTO {store}_catalogue({columns}) VALUES({",".join("?" for _ in row)})', tuple(row.values()))
                    flow(db, 1, query)
                    links = {r['store']: dict(r) for r in db.execute('SELECT * FROM product_store_links')}
                    expected = {'coles', 'woolworths', 'iga'} if query == 'peanut m&m' else set(fixture)
                    self.assertEqual(set(links), expected)
                    for store, link in links.items():
                        title = pricing.name_words(link['store_name'])
                        if query == 'sausages':
                            self.assertNotIn('roll', title)
                        elif query == 'peanut m&m':
                            table, key = pricing._CATALOGUE_TABLES[store]
                            brand = db.execute(f'SELECT brand FROM {table} WHERE {key}=?', (link['store_item_id'],)).fetchone()[0]
                            self.assertIn('m&m', pricing.name_words(brand))
                            self.assertNotIn('butter', title)
                        else:
                            self.assertNotIn('pet', title)
                            self.assertNotIn('dog', title)
