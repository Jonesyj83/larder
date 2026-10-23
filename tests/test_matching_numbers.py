import sqlite3
import unittest

from app import pricing
from test_pricing import prepare


class MatchingNumbers(unittest.TestCase):
    def test_decimal_measurements_do_not_leave_integer_words(self):
        title = 'Ashdene Elegant Rose Stainless Steel 500ml/24.5cm Travel Drink Water Bottle Mint'
        bottle_title = title
        words = pricing.name_words(title)
        self.assertIn('24.5cm', words)
        self.assertTrue({'24', '5cm'}.isdisjoint(words))
        for title, integer in [('Non Stick Dressings 7.5cm', '7'),
                               ('Wired Earphones With Mic 3.5mm Audio', '3'),
                               ('Cake 1.2kg', '1'), ('Liquid Bleach 2.5L', '2')]:
            with self.subTest(title=title):
                self.assertNotIn(integer, pricing.name_words(title))
        candidate = dict(store_item_id='bottle', name=bottle_title, brand='', size='')
        self.assertIsNone(pricing.strongest_match('water 24', [candidate]))

    def test_bare_pack_counts_match_size_in_any_household_order(self):
        pack = dict(store_item_id='pack', name='Spring Water Bottles 600mL x 24 pack',
                    brand='Woolworths', size='24 pack')
        steel = dict(store_item_id='steel', name='Ashdene Steel 500ml/24.5cm Water Bottle',
                     brand='Ashdene', size='500ml')
        single = dict(store_item_id='single', name='Spring Water Bottles', brand='', size='600ml')
        unknown = dict(single, store_item_id='unknown', size='')
        for query in ('water 24', '24 water', '24 bottle water'):
            with self.subTest(query=query):
                self.assertEqual(pricing.strongest_match(query, [steel, single, unknown, pack],
                                                       store='woolworths'), pack)
                self.assertIsNone(pricing.strongest_match(query, [steel, single]))
        noodles = dict(single, name='Maggi 2 Minute Noodles Beef Flavour')
        self.assertEqual(pricing.strongest_match('2 minute noodles', [noodles]), noodles)
        self.assertEqual(pricing.strongest_match('beef 2 minute noodles', [noodles]), noodles)

    def test_real_pack_formats_retrieve_and_select_at_each_store(self):
        db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(db.close)
        db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'local','piara waters')")
        fixtures = {
            'coles': ('Water Multipack 24x350ml', 'Coles', '24 Pack'),
            'woolworths': ('Woolworths Spring Water Bottles', 'Woolworths', '600mL x 24 pack'),
            'aldi': ('Natural Australian Spring Water 24x600ml', 'NORTHBROOK', '24 x 600 ml'),
            'iga': ('Community Co Still Water', 'Community Co', '24'),
        }
        for store, (title, brand, size) in fixtures.items():
            table, key = pricing._CATALOGUE_TABLES[store]
            db.execute(f'INSERT INTO {table}({key},name,brand,size) VALUES(?,?,?,?)',
                       ('1', title, brand, size))
            db.execute(f'INSERT INTO {table}({key},name,brand,size) VALUES(?,?,?,?)',
                       ('2', 'Steel Water Bottle 24.5cm', 'Ashdene', '500ml'))
            db.execute(f'INSERT INTO {table}({key},name,brand,size) VALUES(?,?,?,?)',
                       ('3', 'Water Colour Paint Set 24 Colours', 'Paints', ''))
            if store == 'woolworths':
                db.execute(f'INSERT INTO {table}({key},name,brand,size) VALUES(?,?,?,?)',
                           ('4', 'Fiji Water 500Ml Natural Artesian Water 24 Pack', 'Fiji', '24x500ml'))
            for query in ('water 24', '24 water', '24 bottle water'):
                with self.subTest(store=store, query=query):
                    candidates = pricing.search(store, query, 'Piara Waters', conn=db)
                    self.assertIn('1', [c['store_item_id'] for c in candidates])
                    self.assertEqual(pricing.strongest_match(query, candidates, store=store)['store_item_id'], '1')
                    self.assertIsNone(pricing.strongest_match(query, [c for c in candidates
                                                                     if c['store_item_id'] not in {'1', '4'}], store=store))
