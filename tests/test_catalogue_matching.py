import json
from pathlib import Path
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

    def test_bare_water_excludes_changed_drinks(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'category_formats.json').read_text())
        for store, groups in fixture.items():
            candidates = groups['water']
            table, key = pricing._CATALOGUE_TABLES[store]
            self.db.executemany(
                f'INSERT INTO {table}({key},name,brand,size,price) VALUES(?,?,?,?,?)',
                [(c['store_item_id'], c['name'], c['brand'], c['size'], c['price']) for c in candidates])
            if store == 'iga':
                self.db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'local','piara waters')")
            found = pricing.search(store, 'water', 'Piara Waters', conn=self.db)
            self.assertIsNotNone(pricing.strongest_match('water', found, store=store))
            with self.subTest(store=store):
                for ordered in (candidates, list(reversed(candidates))):
                    best = pricing.strongest_match('water', ordered, store=store)
                    self.assertIsNotNone(best)
                    self.assertFalse(pricing.name_words(best['name']) & {'tonic', 'coconut', 'lime', 'lemon', 'flavoured'})
                tonic = pricing.strongest_match('tonic water', candidates, store=store)
                self.assertIn('tonic', pricing.name_words(tonic['name']))
                changed = [c for c in candidates if pricing.name_words(c['name']) & {'tonic', 'coconut', 'lime', 'lemon', 'flavoured'}]
                self.assertIsNone(pricing.strongest_match('water', changed, store=store))
                for c in changed:
                    self.assertEqual(pricing.strongest_match(' '.join(pricing.name_words(c['name'])), [c], store=store), c)

    def test_noodles_require_serving_format_words(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'category_formats.json').read_text())
        changed_words = {'cup', 'pot', 'bowl', 'soup'}
        for store, groups in fixture.items():
            candidates = groups['noodles']
            table, key = pricing._CATALOGUE_TABLES[store]
            self.db.executemany(
                f'INSERT INTO {table}({key},name,brand,size,price) VALUES(?,?,?,?,?)',
                [(c['store_item_id'], c['name'], c['brand'], c['size'], c['price']) for c in candidates])
            if store == 'iga':
                self.db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'local','piara waters')")
            for query in ('noodles', 'maggi chicken noodles'):
                with self.subTest(store=store, query=query):
                    found = pricing.search(store, query, 'Piara Waters', conn=self.db)
                    best = pricing.strongest_match(query, found, store=store)
                    if query != 'noodles':
                        self.assertIsNotNone(best)
                    if best:
                        self.assertFalse(pricing.name_words(best['name']) & changed_words)
            for word in ('cup', 'pot', 'bowl'):
                if any(word in pricing.name_words(c['name']) for c in candidates):
                    query = word + ' noodles'
                    found = pricing.search(store, query, 'Piara Waters', conn=self.db)
                    best = pricing.strongest_match(query, found, store=store)
                    self.assertIsNotNone(best)
                    self.assertIn(word, pricing.name_words(best['name']))
            for c in candidates:
                formats = pricing.name_words(c['name']) & changed_words
                if not formats:
                    continue
                with self.subTest(store=store, title=c['name']):
                    self.assertIsNone(pricing.strongest_match('noodles', [c], store=store))
                    # The same stored product stays available on explicit request.
                    query = ' '.join(pricing.name_words(c['name']))
                    self.assertEqual(pricing.strongest_match(query, [c], store=store), c)
            if store == 'coles':
                self.assertEqual(self.match(store, 'Maggi BEEF FLAVOUR NOODLES')['store_item_id'], '1616637')
            if store == 'aldi':
                self.assertEqual(self.match(store, 'Maggi CHICKEN NOODLES')['store_item_id'], '000000000000548906')

    def test_shorter_stored_brand_keeps_longer_word_prefix_eligible(self):
        chips = dict(store_item_id='1786160', name='Honey Soy Chicken Potato Chips',
                     brand='Red Rock Deli', size='165g', price=4)
        crackers = dict(store_item_id='3979019', name='Deli Honey Soy Chicken Deli Style Crackers',
                        brand='Red Rock', size='135g', price=5)
        capsicum = dict(store_item_id='135306', name='Red Capsicum', brand='Red', size='each')
        unrelated = dict(chips, store_item_id='other', brand='Red Rockefeller')
        for candidates in ([chips, crackers], [crackers, chips], [capsicum, chips]):
            self.assertEqual(pricing.strongest_match('red rock soy chips', candidates), chips)
        # The shorter label remains available when its product is requested.
        self.assertEqual(pricing.strongest_match('red rock soy crackers', [chips, crackers]), crackers)
        self.assertIsNone(pricing.strongest_match('red rock soy chips', [crackers, unrelated]))
        longer_crackers = dict(crackers, store_item_id='longer', brand='Red Rock Deli',
                               name='Honey Soy Chicken Crackers With Sesame')
        self.assertEqual(pricing.strongest_match('red rock soy crackers',
                         [longer_crackers, crackers]), crackers)
        self.assertIsNone(pricing.strongest_match('red rock soy chips 500g', [chips, crackers]))
        self.assertIsNone(pricing.strongest_match('red rock spicy chips', [chips, crackers]))
        self.db.executemany(
            'INSERT INTO coles_catalogue(coles_id,name,brand,size,price) VALUES(?,?,?,?,?)',
            [(c['store_item_id'], c['name'], c['brand'], c['size'], c['price'])
             for c in (chips, crackers)])
        self.assertEqual(self.match('coles', 'red rock soy chips')['store_item_id'], '1786160')
        self.assertEqual(self.match('coles', 'red rock soy crackers')['store_item_id'], '3979019')

    def test_shorter_brand_only_query_does_not_admit_unrelated_brand(self):
        crackers = dict(store_item_id='3979019', name='Deli Honey Soy Chicken Deli Style Crackers',
                        brand='Red Rock', size='135g')
        other = dict(crackers, store_item_id='other', brand='Other',
                     name='Red Rock Honey Soy Crackers', price=1)
        self.assertEqual(pricing.strongest_match('red rock soy crackers', [other, crackers]), crackers)
        self.assertIsNone(pricing.strongest_match('red rock soy chips', [other, crackers]))

    def test_soy_milk_residual_tie_uses_plainest_house_title(self):
        candidates = json.loads((Path(__file__).parent / 'fixtures' / 'soy_milk_tie.json').read_text())
        self.db.executemany(
            'INSERT INTO woolworths_catalogue(stockcode,name,brand,size) VALUES(?,?,?,?)',
            [(c['store_item_id'], c['name'], c['brand'], c['size']) for c in candidates])
        # Unrequested formulations now resolve by the closest remaining title;
        # remembered and explicit formulation words still take precedence.
        for ordered in (candidates, list(reversed(candidates))):
            # The shortest remaining soy titles use the confirmed house label.
            self.assertEqual(pricing.strongest_match('1l soy milk', ordered, store='woolworths')['store_item_id'], '779130')
            self.assertEqual(pricing.strongest_match('1l soy milk', ordered,
                             preferred_type={'long', 'life', 'uht'},
                             store='woolworths')['store_item_id'], '779130')
        # Retrieval and direct selection agree on the plainest house-label title.
        self.assertEqual(self.match('woolworths', '1l soy milk')['store_item_id'], '779130')
        for query in ('1l woolworths soy milk', '1l soy milk long life uht'):
            self.assertEqual(self.match('woolworths', query)['store_item_id'], '779130')
        # Explicit formulation words retain full recall; the shortest title wins.
        for query, expected in (('1l soy milk lite', '216826'),
                                ('1l soy milk calci plus', '153587')):
            self.assertEqual(self.match('woolworths', query)['store_item_id'], expected)
        for query, expected in (('1l vitasoy soy milky lite', '216826'),
                                ('1l soy milk vanilla', '5535')):
            self.assertEqual(self.match('woolworths', query)['store_item_id'], expected)

    def test_residual_tie_prefers_plain_words_before_shelf_price(self):
        from itertools import permutations
        plain = dict(store_item_id='3', name='Shaved Ham', price=6)
        same = dict(store_item_id='2', name='Shaved Ham', price=8)
        extra = dict(store_item_id='1', name='English Shaved Ham', price=1)
        # A lower price cannot buy away an extra, unrequested description.
        for ordered in permutations([extra, same, plain]):
            self.assertEqual(pricing.strongest_match('shaved ham', ordered), plain)

    def test_residual_price_tie_ignores_missing_and_invalid_prices(self):
        from itertools import permutations
        cheap = dict(store_item_id='9', name='Shaved Ham', price=3)
        dear = dict(store_item_id='8', name='Shaved Ham', price=7)
        for price in (None, 0, -1, float('nan'), float('inf'), '1', True):
            unknown = dict(store_item_id='1', name='Shaved Ham', price=price)
            for ordered in permutations([unknown, dear, cheap]):
                self.assertEqual(pricing.strongest_match('shaved ham', ordered), cheap)
        missing = dict(store_item_id='1', name='Shaved Ham')
        for ordered in ([missing, cheap], [cheap, missing]):
            self.assertEqual(pricing.strongest_match('shaved ham', ordered), cheap)
        # Equal shelf prices, or no valid prices, use stable IDs as a last resort.
        for price in (None, 3):
            first = dict(missing, price=price)
            second = dict(first, store_item_id='2')
            for ordered in ([first, second], [second, first]):
                self.assertEqual(pricing.strongest_match('shaved ham', ordered), first)

    def test_residual_variant_tie_uses_fewer_words_then_price(self):
        parmesan = dict(store_item_id='3', name='Grated Parmesan Cheese', price=5)
        tasty = dict(store_item_id='2', name='Grated Tasty Cheese', price=3)
        cheddar = dict(store_item_id='1', name='Grated Mature Cheddar Cheese', price=1)
        # No shared plain subset exists. Equal-length types use shelf price,
        # while the cheaper but longer description still loses.
        for ordered in ([parmesan, tasty, cheddar], [cheddar, tasty, parmesan]):
            self.assertEqual(pricing.strongest_match('grated cheese', ordered), tasty)
            self.assertEqual(pricing.strongest_match('grated parmesan cheese', ordered), parmesan)

    def test_residual_price_cannot_override_explicit_words_or_cola_variant(self):
        classic = dict(store_item_id='3', name='Classic Cola', size='2L', price=6)
        diet = dict(store_item_id='1', name='Diet Cola', size='2L', price=1)
        self.assertEqual(pricing.strongest_match('2l cola', [diet, classic]), classic)
        self.assertEqual(pricing.strongest_match('2l diet cola', [classic, diet]), diet)
        self.assertIsNone(pricing.strongest_match('2l cherry cola', [classic, diet]))
        self.assertIsNone(pricing.strongest_match('shaved ham', [classic, diet]))

    def test_home_brand_only_settles_one_remaining_category_choice(self):
        home = dict(store_item_id='1', name='Woolworths Whole Milk Full Cream Milk',
                    brand='Woolworths', size='1L')
        named = dict(store_item_id='2', name='Devondale Full Cream Milk',
                     brand='Devondale', size='1L')
        candidates = [named, home]
        # Equal extra-word counts and missing prices settle by stable ID.
        self.assertEqual(pricing.strongest_match('milk', candidates)['store_item_id'], '1')
        self.assertEqual(pricing.strongest_match('milk', candidates, store='woolworths'), home)
        # Without a matching house label, equal titles settle by stable ID.
        self.assertEqual(pricing.strongest_match('milk', candidates, store='aldi')['store_item_id'], '1')
        # Nearby house sizes remain valid; equal missing prices settle by ID.
        self.assertEqual(pricing.strongest_match('milk', candidates +
            [dict(home, store_item_id='3', size='2L')], store='woolworths')['store_item_id'], '1')
        self.assertEqual(pricing.strongest_match('devondale milk', candidates,
                         store='woolworths'), named)
        self.assertIsNone(pricing.strongest_match('milk', candidates,
                         preferred_type={'skim'}, store='woolworths'))
        # A premium line is not the plain house label.
        # Macro and Devondale tie on extra words; missing prices settle by ID.
        self.assertEqual(pricing.strongest_match('milk', [named,
            dict(home, brand='Macro', name='Macro Full Cream Milk')], store='woolworths')['store_item_id'], '1')

    def test_aldi_plain_sauce_uses_confirmed_house_label(self):
        self.db.executescript("""
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('370683','Tomato Sauce 2L','COLWAY','2 l'),
                ('370278','Tomato Pasta Sauce 500g','REMANO','500 g'),
                ('364938','Spaghetti in Tomato Sauce 420g','CORALE','420 g'),
                ('261610','Baked Beans in Tomato Sauce 420g','CORALE','420 g');
        """)
        self.assertEqual(self.match('aldi', 'tomato sauce')['store_item_id'], '370683')
        # Regal also labels salmon and pet food outside Aldi; its soft
        # drink substitution must remain category-specific.
        self.assertNotIn('REGAL', pricing.HOME_BRANDS['aldi'])
        self.db.execute("UPDATE aldi_catalogue SET brand='Other' WHERE item_id='370683'")
        # The shortest real sauce title remains valid without a house label.
        self.assertEqual(self.match('aldi', 'tomato sauce')['store_item_id'], '370683')

    def test_home_brand_bulk_gap_uses_only_same_brand_finalists(self):
        home = dict(store_item_id='7413417', name='Tomato Sauce',
                    brand='Coles Simply', size='500mL')
        bulk = dict(home, store_item_id='8849054', size='2L')
        named = dict(store_item_id='3574040', name='Tomato Sauce',
                     brand='Rosella', size='580mL')
        for candidates in ([named, bulk, home], [home, bulk, named]):
            self.assertEqual(pricing.strongest_match('tomato sauce', candidates,
                             store='coles'), home)
            self.assertEqual(pricing.strongest_match('tomato sauce 2L', candidates,
                             store='coles'), bulk)
            for size in ('1L', '', '2kg'):
                # Rosella adds one brand word; Coles Simply adds two.
                self.assertEqual(pricing.strongest_match('tomato sauce',
                    [named, home, dict(bulk, size=size)], store='coles')['store_item_id'], '3574040')
        # A bulk gap cannot decide between separate house labels.
        # Separate house labels cannot settle size; Rosella has fewer extra words.
        self.assertEqual(pricing.strongest_match('tomato sauce',
            [named, home, dict(bulk, brand='Coles Finest')], store='coles')['store_item_id'], '3574040')

    def test_same_brand_squeeze_container_uses_everyday_size(self):
        self.db.executescript("""
            INSERT INTO woolworths_catalogue(stockcode,name,brand,size) VALUES
                (18040,'Woolworths Tomato Sauce','Woolworths','2L'),
                (230519,'Woolworths Tomato Sauce Squeeze','Woolworths','500mL');
        """)
        self.assertEqual(self.match('woolworths', 'tomato sauce')['store_item_id'], '230519')
        self.assertEqual(self.match('woolworths', 'tomato sauce 2L')['store_item_id'], '18040')
        self.assertEqual(self.match('woolworths', 'tomato sauce squeeze')['store_item_id'], '230519')
        candidates = pricing.search('woolworths', 'tomato sauce', conn=self.db)
        for ordered in (candidates, list(reversed(candidates))):
            self.assertEqual(pricing.strongest_match('tomato sauce', ordered,
                             store='woolworths')['store_item_id'], '230519')
        for size in ('1L', '', '500g'):
            self.db.execute("UPDATE woolworths_catalogue SET size=? WHERE stockcode=230519", (size,))
            self.assertEqual(self.match('woolworths', 'tomato sauce')['store_item_id'], '18040')

    def test_same_brand_flavour_is_not_a_container_difference(self):
        bulk = dict(store_item_id='18040', name='Woolworths Tomato Sauce',
                    brand='Woolworths', size='2L')
        spicy = dict(store_item_id='2', name='Woolworths Spicy Tomato Sauce Squeeze',
                     brand='Woolworths', size='500mL')
        self.assertEqual(pricing.strongest_match('tomato sauce', [spicy, bulk],
                         store='woolworths'), bulk)
        self.assertEqual(pricing.strongest_match('spicy tomato sauce', [bulk, spicy],
                         store='woolworths'), spicy)
        other = dict(spicy, name='Other Tomato Sauce Squeeze', brand='Other')
        self.assertEqual(pricing.strongest_match('tomato sauce', [other, bulk],
                         store='woolworths'), bulk)
        for brand in ('', 'Other'):
            plain = dict(bulk, brand=brand)
            squeeze = dict(other, brand=brand, name='Tomato Sauce Squeeze')
            if brand:
                squeeze['brand'] = 'Another'
            self.assertEqual(pricing.strongest_match('tomato sauce', [plain, squeeze],
                             store='woolworths'), plain)

    def test_grated_and_shredded_are_the_same_word(self):
        self.db.executescript("""
            INSERT INTO woolworths_catalogue(stockcode,name,brand,size) VALUES
                (17497,'Dairyworks Tasty Cheese Grated','Dairyworks','500g'),
                (6000143,'Bega Tasty Grated Cheese','Bega','250g'),
                (85587,'Woolworths Shredded Parmesan Cheese','Woolworths','250g');
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('334960','Grana Padano Grated Cheese 150g','EMPORIUM SELECTION','150 g'),
                ('399737','Long-life Grated Parmesan Cheese 100g','REMANO','100 g');
        """)
        # Retrieval includes shredded house lines; residual type choices now
        # resolve by extra words rather than abstaining.
        candidates = pricing.search('woolworths', 'grated cheese', conn=self.db)
        self.assertIn('85587', [c['store_item_id'] for c in candidates])
        # Parmesan has fewer extra brand words than the named tasty lines.
        self.assertEqual(self.match('woolworths', 'grated cheese')['store_item_id'], '85587')
        self.assertEqual(self.match('woolworths', 'grated parmesan cheese')['store_item_id'], '85587')
        # The Parmesan title has fewer extra words than Grana Padano.
        self.assertEqual(self.match('aldi', 'grated cheese')['store_item_id'], '399737')

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

    def test_plain_coke_means_classic_and_preserves_typed_variants(self):
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (1,'Classic Soft Drink Bottle','Coca-Cola','2L'),
                (2,'Diet Soft Drink Bottle','Coca-Cola','2L'),
                (3,'Zero Sugar Soft Drink Bottle','Coca-Cola','2L');
        """)
        self.assertEqual(self.match('coles', '2l coke')['store_item_id'], '1')
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
        # Identical full-cream titles and missing prices settle by stable ID.
        self.assertEqual(pricing.strongest_match('milk', [full, dict(full, store_item_id='4')])['store_item_id'], '1')
        # White and wholemeal tie on words and missing prices; stable ID decides.
        self.assertEqual(pricing.strongest_match('bread', [
            dict(store_item_id='1', name='White Bread'),
            dict(store_item_id='2', name='Wholemeal Bread')])['store_item_id'], '1')

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
                # The bulk pack loses first; missing/comparability gaps then settle by ID.
                self.assertEqual(pricing.strongest_match('Water', candidates + [unknown])['store_item_id'], '1')

    def test_shared_normalization_retrieves_and_selects_across_stores(self):
        for store, (table, key) in pricing._CATALOGUE_TABLES.items():
            with self.subTest(store=store):
                self.db.execute(f"INSERT INTO {table}({key},name,brand,size) VALUES(?,?,?,?)",
                                ('700', 'Hi-Lo Fresh Dairy Milk Carton', 'Dairy', '1 L'))
                if store == 'iga':
                    self.db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'local','piara waters')")
                candidates = pricing.search(store, '1l hilo milk', 'Piara Waters', conn=self.db)
                self.assertIn('700', [c['store_item_id'] for c in candidates])
                self.assertEqual(pricing.strongest_match('1l hilo milk', candidates, store=store)['store_item_id'], '700')
                self.assertIsNone(pricing.strongest_match('1l hilo milk vanilla', candidates, store=store))
        self.assertEqual(pricing.search('iga', '1l hilo milk', 'Other Location', conn=self.db), [])

    def test_apostrophe_brand_and_conjunction_retrieval_preserve_flavour(self):
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (1,'Crinkle Cut Cheese & Onion Potato Chips', 'Smith''s','170g'),
                (2,'Potato Chips Lunchbox Multipack Cheese & Onion 8 Pack','Smith''s','152g'),
                (3,'Crinkle Cut Sour Cream & Onion Potato Chips','Smith''s','170g'),
                (4,'Cheese & Onion Potato Chips','Other','170g');
        """)
        for query in ('Smiths cheese and onion', "Smith’s cheese & onion"):
            candidates = pricing.search('coles', query, conn=self.db)
            self.assertIn('1', [c['store_item_id'] for c in candidates])
            self.assertEqual(pricing.strongest_match(query, candidates)['store_item_id'], '1')
        self.assertIsNone(self.match('coles', 'smiths cheese and onion chilli'))

    def test_synonym_retrieval_and_remembered_type_select_parmesan(self):
        self.db.executescript("""
            INSERT INTO woolworths_catalogue(stockcode,name,brand,size) VALUES
                (1,'Woolworths Shredded Parmesan Cheese','Woolworths','250g'),
                (2,'Woolworths Shredded Mozzarella Cheese','Woolworths','250g');
        """)
        for query in ('grated cheese', 'shredded cheese'):
            candidates = pricing.search('woolworths', query, conn=self.db)
            self.assertEqual({c['store_item_id'] for c in candidates}, {'1', '2'})
            # Equal Parmesan and mozzarella titles have no prices; stable ID decides.
            self.assertEqual(pricing.strongest_match(query, candidates, store='woolworths')['store_item_id'], '1')
            self.assertEqual(pricing.strongest_match(query, candidates,
                preferred_type={'parmesan'}, store='woolworths')['store_item_id'], '1')
            self.assertEqual(pricing.strongest_match(query + ' mozzarella', candidates,
                preferred_type={'parmesan'}, store='woolworths')['store_item_id'], '2')

    def test_size_dimensions_pack_formats_and_explicit_size_priority(self):
        self.assertEqual(pricing.pack_size('19g x 8 pack'), pricing.pack_size('8 x 19 g'))
        self.db.executescript("""
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('1','Full Cream Milk 2L','FARMDALE','2 l'),
                ('2','Instant Full Cream Milk Powder','FARMDALE','2kg'),
                ('3','Chocolate Milk','FARMDALE','2 l'),
                ('4','Full Cream Milk 1L','FARMDALE','1 l');
        """)
        candidates = pricing.search('aldi', '2l milk', conn=self.db)
        self.assertEqual(pricing.strongest_match('2l milk', candidates,
            preferred_size={'volume': 1000}, store='aldi')['store_item_id'], '1')
        self.assertIsNone(pricing.strongest_match('2l milk', [c for c in candidates if c['store_item_id'] in {'2', '3'}], store='aldi'))
        self.assertIsNone(self.match('aldi', '1l hilo milk'))

    def test_verified_aldi_equivalent_keeps_all_flavour_words(self):
        self.db.executescript("""
            INSERT INTO aldi_catalogue(item_id,name,brand,size) VALUES
                ('1','Cheese & Onion Crinkle Cut Chips 230g','SPRINTERS','230 g'),
                ('2','Sour Cream & Onion Chips','SPRINTERS','230 g'),
                ('3','Cheese & Onion Frozen Chips','Other','230 g'),
                ('4','Snack Faves 20 Pack','SMITH''S','392g');
        """)
        self.assertEqual(self.match('aldi', 'Smiths cheese and onion')['store_item_id'], '1')
        self.assertIsNone(self.match('aldi', 'smiths cheese and onion chilli'))
        self.db.execute("DELETE FROM aldi_catalogue WHERE item_id='1'")
        self.assertIsNone(self.match('aldi', 'smiths cheese and onion'))

    def test_history_type_excludes_retailer_brand_without_changing_links(self):
        self.db.executescript("""
            INSERT INTO products(id,name,unit_id) VALUES(1,'grated cheese',1);
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (1,'Grated Parmesan Cheese','Kraft','125g');
            INSERT INTO product_store_links(product_id,store,store_item_id,store_name) VALUES
                (1,'coles','1','Kraft Grated Parmesan Cheese');
        """)
        before = list(self.db.execute('SELECT * FROM product_store_links'))
        self.assertEqual(pricing.type_preference(self.db, 1, 'aldi'), {'parmesan'})
        self.assertEqual(before, list(self.db.execute('SELECT * FROM product_store_links')))

    def test_plain_cola_never_uses_diet_line_as_only_choice(self):
        self.db.executescript("""
            INSERT INTO coles_catalogue(coles_id,name,brand,size) VALUES
                (1,'Zero Sugar Soft Drink Bottle','Coca-Cola','2L'),
                (2,'Diet Soft Drink Bottle','Coca-Cola','2L');
        """)
        self.assertIsNone(self.match('coles', '2l coke'))
        self.assertEqual(self.match('coles', '2l coke zero sugar')['store_item_id'], '1')
        self.assertEqual(self.match('coles', '2l coke diet')['store_item_id'], '2')
