import sqlite3
import unittest

from app import db as database, pricing


class CatalogueMatching(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)

    def match(self, store, query):
        return pricing.strongest_match(query, pricing.search(store, query, conn=self.db))

    def test_ranked_brand_search_and_single_can(self):
        self.db.executemany("INSERT INTO coles_catalogue(coles_id,name) VALUES(?,?)",
                            [(i, 'Red Onion') for i in range(40)])
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (100,'Energy Drink 12x250ml','Red Bull','3L'),
                (101,'Energy Drink Can','Red Bull','473mL'),
                (102,'Energy Drink Single Can','Red Bull','250mL'),
                (103,'Energy Drink White Peach','Red Bull','250mL');
        """)
        for query in ('red bull', 'redbull'):
            self.assertEqual(self.match('coles', query)['store_item_id'], '102')
        self.assertEqual(self.match('coles', 'red bull white peach')['store_item_id'], '103')

    def test_coke_alias_keeps_variant_and_brand_checks(self):
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (1,'Zero Sugar Coke 10x375ml','Coca-Cola','3.75L'),
                (2,'Zero Sugar Coke','Other','3.75L'),
                (3,'Original Taste Coke','Coca-Cola','3.75L');
        """)
        self.assertEqual(self.match('coles', 'coke zero')['store_item_id'], '1')

    def test_milk_window_reaches_real_variants_and_abstains(self):
        self.db.executemany("INSERT INTO coles_catalogue(coles_id,name,size) VALUES(?,?,'2L')",
                            [(i, 'Flavoured Milk') for i in range(40)])
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,size) VALUES
                (100,'Full Cream Milk','1L'),(101,'Full Skim Milk','1L');
        """)
        candidates = pricing.search('coles', '1l milk', conn=self.db)
        self.assertIn('100', [c['store_item_id'] for c in candidates])
        full = next(c for c in candidates if c['store_item_id'] == '100')
        self.assertIsNone(pricing.strongest_match('1l milk', [full]))
        # The full-cream candidate is evaluated but misses the name floor.
        # Neither the typed size nor word overlap identifies a milk type.
        self.assertIsNone(pricing.strongest_match('1l milk', candidates))
        self.db.executemany("INSERT INTO coles_catalogue(coles_id,name) VALUES(?,'Milk')",
                            [(i,) for i in range(200, 450)])
        self.assertEqual(len(pricing.search('coles', 'milk', conn=self.db)), 200)

    def test_aldi_category_substitute_and_direct_stock(self):
        self.db.executescript("""
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('1','Yeast Spread','BRAMWELLS','220g'),
                ('2','Strawberry Spread','BRAMWELLS','220g');
        """)
        self.assertEqual(self.match('aldi', 'vegemite')['store_item_id'], '1')
        self.db.execute("""INSERT INTO aldi_catalogue(item_id,name,brand,size)
                           VALUES('3','Vegemite','Vegemite','220g')""")
        candidates = pricing.search('aldi', 'vegemite', conn=self.db)
        self.assertEqual([c['store_item_id'] for c in candidates], ['3'])
        self.assertEqual(pricing.strongest_match('vegemite', candidates)['store_item_id'], '3')
        self.assertEqual(pricing.search('aldi', 'nutella', conn=self.db), [])

    def test_aldi_partial_direct_results_do_not_trigger_substitution(self):
        self.db.executescript("""
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('1','Cola Soft Drink Zero','Store','2L'),
                ('2','Cola Soft Drink','Store','2L'),
                ('3','Orange Soft Drink Zero','Store','2L');
        """)
        self.assertIsNone(self.match('aldi', 'coca-cola zero'))
        self.assertTrue(all('_match_query' not in c for c in
            pricing.search('aldi', 'coca-cola zero', conn=self.db)))

    def test_size_tie_requires_complete_comparable_sizes(self):
        candidates = [dict(store_item_id='1', name='Water', size='600mL'),
                      dict(store_item_id='2', name='Water', size='24x600mL')]
        for size in ('', '1kg', '600mL'):
            with self.subTest(size=size):
                unknown = dict(store_item_id='3', name='Water', size=size)
                self.assertIsNone(pricing.strongest_match('Water', candidates + [unknown]))
