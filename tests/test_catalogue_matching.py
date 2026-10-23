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
        return pricing.strongest_match(query, pricing.search(store, query, conn=self.db), store=store)

    def test_home_brand_only_settles_one_remaining_category_choice(self):
        home = dict(store_item_id='1', name='Woolworths Whole Milk Full Cream Milk',
                    brand='Woolworths', size='1L')
        named = dict(store_item_id='2', name='Devondale Full Cream Milk',
                     brand='Devondale', size='1L')
        candidates = [named, home]
        self.assertIsNone(pricing.strongest_match('milk', candidates))
        self.assertEqual(pricing.strongest_match('milk', candidates, store='woolworths'), home)
        self.assertIsNone(pricing.strongest_match('milk', candidates, store='aldi'))
        self.assertIsNone(pricing.strongest_match('milk', candidates +
            [dict(home, store_item_id='3', size='2L')], store='woolworths'))
        self.assertEqual(pricing.strongest_match('devondale milk', candidates,
                         store='woolworths'), named)
        self.assertIsNone(pricing.strongest_match('milk', candidates,
                         preferred_type={'skim'}, store='woolworths'))
        # A premium line is not the plain house label.
        self.assertIsNone(pricing.strongest_match('milk', [named,
            dict(home, brand='Macro', name='Macro Full Cream Milk')], store='woolworths'))

    def test_grated_cheese_does_not_claim_shredded_house_lines(self):
        self.db.executescript("""
            INSERT INTO woolworths_catalogue(stockcode,name,brand,size) VALUES
                (17497,'Dairyworks Tasty Cheese Grated','Dairyworks','500g'),
                (6000143,'Bega Tasty Grated Cheese','Bega','250g'),
                (85587,'Woolworths Shredded Parmesan Cheese','Woolworths','250g');
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('334960','Grana Padano Grated Cheese 150g','EMPORIUM SELECTION','150 g'),
                ('399737','Long-life Grated Parmesan Cheese 100g','REMANO','100 g');
        """)
        self.assertIsNone(self.match('woolworths', 'grated cheese'))
        self.assertIsNone(self.match('aldi', 'grated cheese'))

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

    def test_coke_retrieval_includes_formal_and_literal_names(self):
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (1,'Coca Cola Classic','Coca-Cola','2L'),
                (2,'Diet Coke 10x375ml','Coca-Cola','3.75L');
        """)
        candidates = pricing.search('coles', '2l coke', conn=self.db)
        self.assertEqual({c['store_item_id'] for c in candidates}, {'1', '2'})
        self.assertEqual(pricing.strongest_match('2l coke', candidates)['store_item_id'], '1')

    def test_coke_size_does_not_choose_between_classic_and_diet(self):
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (1,'Classic Soft Drink Bottle','Coca-Cola','2L'),
                (2,'Diet Soft Drink Bottle','Coca-Cola','2L'),
                (3,'Zero Sugar Soft Drink Bottle','Coca-Cola','2L');
        """)
        self.assertIsNone(self.match('coles', '2l coke'))
        self.assertEqual(self.match('coles', '2l coke classic')['store_item_id'], '1')
        self.assertEqual(self.match('coles', '2l coke diet')['store_item_id'], '2')

    def test_milk_window_reaches_full_cream_default(self):
        self.db.executemany("INSERT INTO coles_catalogue(coles_id,name,size) VALUES(?,?,'2L')",
                            [(i, 'Flavoured Milk') for i in range(40)])
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,size) VALUES
                (100,'Full Cream Milk','1L'),(101,'Full Skim Milk','1L');
        """)
        candidates = pricing.search('coles', '1l milk', conn=self.db)
        self.assertIn('100', [c['store_item_id'] for c in candidates])
        full = next(c for c in candidates if c['store_item_id'] == '100')
        self.assertEqual(pricing.strongest_match('1l milk', [full]), full)
        self.assertEqual(pricing.strongest_match('1l milk', candidates), full)
        self.db.executemany("INSERT INTO coles_catalogue(coles_id,name) VALUES(?,'Milk')",
                            [(i,) for i in range(200, 450)])
        self.assertEqual(len(pricing.search('coles', 'milk', conn=self.db)), 200)

    def test_milk_default_only_applies_to_a_bare_category(self):
        full = dict(store_item_id='1', name='Full Cream Milk', size='1L')
        skim = dict(store_item_id='2', name='Skim Milk', size='1L')
        soy = dict(store_item_id='3', name='Soy Milk', size='1L')
        for query in ('milk', '1l milk'):
            self.assertEqual(pricing.strongest_match(query, [skim, soy, full]), full)
        self.assertEqual(pricing.strongest_match('skim milk', [full, skim]), skim)
        self.assertEqual(pricing.strongest_match('soy milk', [full, soy]), soy)
        self.assertIsNone(pricing.strongest_match('milk', [full, dict(full, store_item_id='4')]))
        self.assertIsNone(pricing.strongest_match('bread', [
            dict(store_item_id='1', name='White Bread'),
            dict(store_item_id='2', name='Wholemeal Bread')]))

    def test_milk_default_prefers_plain_over_premium_lines(self):
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (301721,'Full Cream Long Life Milk','Devondale','1L'),
                (1079516,'Organic Dairy Full Cream Milk','Living Planet','1L'),
                (1329781,'High Protein Full Cream Milk','Devondale','1L'),
                (1850656,'Lactose Free Long Life Full Cream Milk','Coles','1L'),
                (2067115,'Instant Full Cream Milk Powder','Devondale','1kg');
        """)
        self.assertEqual(self.match('coles', '1l milk')['store_item_id'], '301721')
        self.assertEqual(self.match('coles', '1l living planet organic milk')['store_item_id'], '1079516')
        self.assertEqual(self.match('coles', '1l devondale high protein milk')['store_item_id'], '1329781')
        self.assertEqual(self.match('coles', '1l lactose free long life milk')['store_item_id'], '1850656')
        # Equal-length names still distinguish an everyday default from
        # an organic line, without dropping any explicitly typed words.
        plain = dict(store_item_id='1', name='Full Cream Long Life Milk', brand='Dairy', size='1L')
        organic = dict(store_item_id='2', name='Organic Dairy Full Cream Milk', brand='Farm', size='1L')
        self.assertEqual(len(pricing.name_words(plain['name'])), len(pricing.name_words(organic['name'])))
        self.assertEqual(pricing.strongest_match('1l milk', [organic, plain]), plain)
        self.assertEqual(pricing.strongest_match('organic dairy full cream milk', [organic, plain]), organic)
        self.assertEqual(pricing.strongest_match('milk', [organic, plain],
                         preferred_type={'organic', 'full', 'cream'}), organic)

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

        fixtures = [
            ('10', 'Lemonade 1.25L', 'FIZZI', '1.25 l'),
            ('11', 'Orange Soft Drink 1.25L', 'SUNLIT', '1.25 l'),
            ('12', 'Mango Mojo No sugar Energy Drink 500ml', 'FLYING POWER', '500 ml'),
            ('13', 'Sugar Free Original Energy Drink 4 Pack 250ml', 'FLYING POWER', '4 x 250 ml'),
            ('14', 'Choc Hazelnut Spread 400g', 'BRAMWELLS', '400 g'),
            ('15', 'Corn Chips Nacho Cheese 200g', 'SPRINTERS', '200 g'),
            ('16', 'Sea Salt Flavoured Deli Style Potato Chips 175g', 'BLACKSTONE GOURMET SNACK CO.', '175 g'),
            ('17', 'Corn Flakes 700g', 'HILLCREST', '700 g'),
            ('18', 'Smooth Peanut Butter 500g', 'BRAMWELLS', '500 g'),
            ('19', 'Baked Beans in Tomato Sauce 420g', 'CORALE', '420 g'),
            ('20', 'Chicken Noodles 5 Pack 425g', 'SIMPLEE', '425 g'),
            ('21', 'Mi Goreng Noodles 5 Pack 425g', 'SIMPLEE', '425 g'),
            ('22', 'Choco Rice Cereal 650g', 'HILLCREST', '650 g'),
            ('23', 'No Sugar Cola 1.25L', 'REGAL', '1.25 l'),
            ('24', 'Cola 1.25L', 'REGAL', '1.25 l'),
        ]
        self.db.executemany('INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES(?,?,?,?)', fixtures)
        for query, item_id in (
                ('1.25l sprite', '10'), ('1.25l 7up', '10'), ('1.25l fanta', '11'),
                ('monster mango 500ml', '12'), ('mother mango 500ml', '12'),
                ('monster sugar free original 4x250ml', '13'), ('400g nutella', '14'),
                ('200g doritos nacho cheese', '15'), ('175g kettle sea salt', '16'),
                ('175g red rock deli sea salt', '16'), ('700g kelloggs corn flakes', '17'),
                ("700g kellogg's corn flakes", '17'), ('500g skippy smooth', '18'),
                ('500g kraft peanut butter smooth', '18'), ('heinz baked beans 420g', '19'),
                ('maggi chicken noodles 425g', '20'), ('indomie mi goreng 425g', '21'),
                ('coco pops cereal 650g', '22'), ('1.25l pepsi max', '23'), ('1.25l pepsi', '24')):
            with self.subTest(query=query):
                self.assertEqual(self.match('aldi', query)['store_item_id'], item_id)
        # National labels really do occur in the range. They take
        # precedence over the category retry, even at a different size.
        self.db.executescript("""
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('30','Pepsi Max 2L','PEPSI MAX','2 l'),
                ('31','Nutella 400g','FERRERO','400 g'),
                ('32','Weet-Bix 375g','SANITARIUM','375 g');
        """)
        self.assertEqual(self.match('aldi', '2l pepsi max')['store_item_id'], '30')
        self.assertEqual(self.match('aldi', '400g nutella')['store_item_id'], '31')
        self.assertIsNone(self.match('aldi', '1.25l pepsi max'))
        self.assertIsNone(self.match('aldi', '1.12kg weet-bix'))
        for query in ('2l sprite', 'doritos purple unicorn', '400g nutella dairy free',
                      'monster mango lactose free 500ml'):
            self.assertIsNone(self.match('aldi', query))
        self.assertEqual(pricing.search('coles', '1.25l sprite', conn=self.db), [])

    def test_aldi_partial_results_require_a_verified_category_and_label(self):
        self.db.executescript("""
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('1','Cola Soft Drink Zero','Store','2L'),
                ('2','Cola Soft Drink','Store','2L'),
                ('3','Orange Soft Drink Zero','Store','2L');
        """)
        self.assertIsNone(self.match('aldi', 'coca-cola zero'))
        self.assertTrue(all('_match_query' not in c for c in
            pricing.search('aldi', 'coca-cola zero', conn=self.db)))
        self.db.execute("INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES('4','No Sugar Cola 1.25L','REGAL','1.25 l')")
        self.assertEqual(self.match('aldi', '1.25l coca-cola zero')['store_item_id'], '4')
        self.assertIsNone(self.match('aldi', '1.25l coca-cola zero cherry'))
        self.db.execute("INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES('5','Coke Coca Cola Zero Sugar','COCA-COLA','600ml')")
        self.assertIsNone(self.match('aldi', '1.25l coca-cola zero'))
        self.assertTrue(all('_match_query' not in c for c in
            pricing.search('aldi', '1.25l coca-cola zero', conn=self.db)))

    def test_size_tie_requires_complete_comparable_sizes(self):
        candidates = [dict(store_item_id='1', name='Water', size='600mL'),
                      dict(store_item_id='2', name='Water', size='24x600mL')]
        for size in ('', '1kg', '600mL'):
            with self.subTest(size=size):
                unknown = dict(store_item_id='3', name='Water', size=size)
                self.assertIsNone(pricing.strongest_match('Water', candidates + [unknown]))
