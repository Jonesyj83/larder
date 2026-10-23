from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import unittest

from app import db as database, pricing


SPELLINGS = json.loads((Path(__file__).parent / 'fixtures' / 'catalogue_spellings.json').read_text())


class CatalogueSpellings(unittest.TestCase):
    def test_stored_measure_and_count_abbreviations(self):
        # Coles 1002165/3529486, Woolworths 68823 and Aldi 648262/649583.
        for stored, common in (('160 GRAM', '160g'), ('1.1 LITRE', '1100ml'),
                               ('1 pc', '1 Piece'), ('25 PAC', '25 Pack'),
                               ('110 PAC', '110 Pack'), ('2EA', '2 each'),
                               ('1kg', '1000g'), ('375mL x 24 case', '24x375ml')):
            with self.subTest(stored=stored):
                self.assertEqual(pricing.pack_size(stored), pricing.pack_size(common))
                self.assertEqual(pricing.name_words('Product ' + stored),
                                 pricing.name_words('Product'))
        # A bare IGA number does not identify packs, sheets or individual pieces.
        for unknown in ('110', '95 sheets', '30m'):
            self.assertEqual(pricing.pack_size(unknown), {})

    def test_spelled_measure_selects_the_requested_size(self):
        candidates = [dict(store_item_id='1', name='Spearmint Leaves Lollies',
                           brand="Allen's", size='160 GRAM'),
                      dict(store_item_id='2', name='Spearmint Leaves Lollies',
                           brand="Allen's", size='1kg')]
        for query in ("Allens Spearmint Leaves Lollies 160g",
                      "Allens Spearmint Leaves Lollies 160 GRAM"):
            self.assertEqual(pricing.strongest_match(query, candidates)['store_item_id'], '1')
        self.assertIsNone(pricing.strongest_match('Spearmint Leaves Lollies 160ml', candidates))

    def test_verified_spellings_share_words_and_preserve_product_details(self):
        for case in SPELLINGS:
            alias, canonical = case['alias'], case['canonical']
            with self.subTest(alias=alias):
                for singular in (False, True):
                    self.assertEqual(pricing.name_words(alias, singular=singular),
                                     pricing.name_words(canonical, singular=singular))
                self.assertNotEqual(pricing.name_words(alias + ' vanilla'),
                                    pricing.name_words(canonical + ' strawberry'))

    def test_catalogue_spellings_retrieve_and_select_in_both_directions(self):
        for case in SPELLINGS:
            alias, canonical = case['alias'], case['canonical']
            for store, spellings in case['examples'].items():
                for spelling, examples in spellings.items():
                    for example in examples:
                        with self.subTest(alias=alias, store=store, title=example['name']):
                            with closing(sqlite3.connect(':memory:')) as db:
                                db.row_factory = sqlite3.Row
                                db.executescript(database.SCHEMA)
                                table, key = pricing._CATALOGUE_TABLES[store]
                                db.execute(f'INSERT INTO {table}({key},name,brand,size) VALUES(?,?,?,?)',
                                           ('1', example['name'], example['brand'], example['size']))
                                if store == 'iga':
                                    db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'local','piara waters')")
                                replacement = canonical if spelling == alias else alias
                                query = re.sub(r'\b' + re.escape(spelling) + r'\b', replacement,
                                               example['name'], flags=re.IGNORECASE)
                                query = re.sub(pricing.SIZE_TOKEN, ' ', query, flags=re.IGNORECASE)
                                if example['brand']:
                                    query = example['brand'] + ' ' + query
                                candidates = pricing.search(store, query, 'Piara Waters', conn=db)
                                self.assertIn('1', [r['store_item_id'] for r in candidates])
                                selected = pricing.strongest_match(query, candidates, store=store)
                                self.assertIsNotNone(selected)
                                self.assertEqual(selected['store_item_id'], '1')
                                self.assertIsNone(pricing.strongest_match(query + ' unlisted', candidates, store=store))

    def test_synonym_only_retrieval_survives_the_candidate_window(self):
        for case in SPELLINGS:
            for source, target in ((case['alias'], case['canonical']),
                                   (case['canonical'], case['alias'])):
                with self.subTest(source=source, target=target):
                    with closing(sqlite3.connect(':memory:')) as db:
                        db.row_factory = sqlite3.Row
                        db.executescript(database.SCHEMA)
                        db.executemany('INSERT INTO coles_catalogue(coles_id,name) VALUES(?,?)',
                                       [(i, 'Unrelated grocery') for i in range(1, 210)])
                        db.execute('INSERT INTO coles_catalogue(coles_id,name) VALUES(500,?)', (target,))
                        self.assertEqual([r['store_item_id'] for r in pricing.search('coles', source, conn=db)], ['500'])
