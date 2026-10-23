"""Catalogue error classes and intent controls from linked household pairs."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import unittest

from app import db as database, pricing


CORPUS = json.loads((Path(__file__).parent / 'fixtures/catalogue_calibration.json').read_text())
FIXTURES = CORPUS['cases']


class CatalogueCalibration(unittest.TestCase):
    def test_frozen_candidate_calibration(self):
        for case in FIXTURES:
            with self.subTest(query=case['query'], store=case['store']):
                with closing(sqlite3.connect(':memory:')) as db:
                    db.row_factory = sqlite3.Row
                    db.executescript(database.SCHEMA)
                    db.execute('INSERT INTO products(id,name,unit_id) VALUES(1,?,1)', (case['query'],))
                    for link in case['history']:
                        table, key = pricing._CATALOGUE_TABLES[link['store']]
                        db.execute(f'INSERT INTO {table}({key},name,brand) VALUES(?,?,?)',
                                   (link['store_item_id'],link['store_name'],link['brand']))
                        db.execute('''INSERT INTO product_store_links(product_id,store,store_item_id,store_name)
                                      VALUES(1,?,?,?)''',
                                   (link['store'],link['store_item_id'],link['store_name']))
                    for candidate in CORPUS['catalogue'][case['store']].values():
                        table,key = pricing._CATALOGUE_TABLES[case['store']]
                        db.execute(f'INSERT OR REPLACE INTO {table}({key},name,brand,size) VALUES(?,?,?,?)',
                            (candidate['store_item_id'],candidate['name'],candidate['brand'],candidate['size']))
                    if case['store'] == 'iga':
                        db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'local','piara waters')")
                    if case['expected_id']:
                        retrieved = pricing.search(case['store'],case['query'],'Piara Waters',conn=db)
                        self.assertIn(case['expected_id'],[c['store_item_id'] for c in retrieved])
                    before = list(db.execute('SELECT * FROM product_store_links'))
                    preference = pricing.type_preference(db,1,case['store'])
                    selected = pricing.strongest_match(case['query'],[CORPUS['catalogue'][case['store']][key] for key in case['candidate_ids']],
                        case['preferred_size'],preference,store=case['store'])
                    self.assertEqual(selected['store_item_id'] if selected else None,case['expected_id'])
                    self.assertEqual(before,list(db.execute('SELECT * FROM product_store_links')))

    def test_normalization_keeps_numbers_outside_size_units(self):
        for left,right in [('a2 Milk','a2 milk'),('2 Minute Noodles','2 minute noodles'),
                           ("Big'N Chunky",'Big N Chunky'),('Coffee Sach','Coffee Sachets'),
                           ('WW Burger Slices','Woolworths Burger Slices'),
                           ('Potatoes','Potato'),('Tomatoes','Tomato'),
                           ('1 L Hi-Lo Milk','1L hilo milk')]:
            with self.subTest(left=left):
                self.assertEqual(pricing.name_words(left),pricing.name_words(right))
        self.assertEqual(pricing.name_words('a2 Milk'),{'a2','milk'})
        self.assertEqual(pricing.name_words('2 Minute Noodles'),{'2','minute','noodle'})

    def test_unrequested_cheese_type_uses_plainest_title(self):
        candidates=[dict(store_item_id='1',name='Shredded Parmesan Cheese',brand='Woolworths',size='250g'),
                    dict(store_item_id='2',name='Tasty Grated Cheese',brand='Dairyworks',size='250g')]
        # The house Parmesan title has fewer extra words than Dairyworks tasty.
        self.assertEqual(pricing.strongest_match('grated cheese',candidates,store='woolworths')['store_item_id'], '1')
        unspecified = dict(store_item_id='3',name='Shredded Cheese',brand='Example Dairy',size='250g')
        # The unspecified title is a plain subset with no unrequested cheese type.
        self.assertEqual(pricing.strongest_match('grated cheese',candidates+[unspecified],store='woolworths')['store_item_id'], '3')
        self.assertEqual(pricing.strongest_match('grated tasty cheese',candidates,
            preferred_type={'parmesan'},store='woolworths')['store_item_id'],'2')

    def test_exact_size_beats_a_shorter_title_in_either_candidate_order(self):
        candidates=[dict(store_item_id='1',name='Tomato Paste',brand='Woolworths',size='415g'),
                    dict(store_item_id='2',name='Woolworths Essentials Tomato Paste',brand='Woolworths Essentials',size='500g')]
        for ordered in (candidates,candidates[::-1]):
            self.assertEqual(pricing.strongest_match('500g tomato paste',ordered,store='woolworths')['store_item_id'],'2')

    def test_conflicting_history_cannot_force_a_preparation(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.executescript(database.SCHEMA)
            db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'black pepper',1)")
            for store,name in [('coles','Ground Black Pepper'),('woolworths','Black Pepper Grinder')]:
                table,key=pricing._CATALOGUE_TABLES[store]
                db.execute(f'INSERT INTO {table}({key},name,brand) VALUES(1,?,?)',(name,store))
                db.execute('''INSERT INTO product_store_links(product_id,store,store_item_id,store_name)
                              VALUES(1,?,'1',?)''',(store,name))
            for store in ('coles','woolworths','aldi'):
                self.assertEqual(pricing.type_preference(db,1,store),set())
