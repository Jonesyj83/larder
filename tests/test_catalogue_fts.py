"""Search indexes follow catalogue transactions without changing product intent."""
import sqlite3
import unittest

from app import db as database, pricing
from scripts.catalogue_crawl import crawl


class CatalogueSearchIndex(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)
        self.db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'local','piara waters')")
        self.db.commit()

    def ids(self, store, query):
        return {c['store_item_id'] for c in pricing.search(store, query, 'Piara Waters', conn=self.db)}

    def postings(self, table, suffix, query):
        index = f'{table}_{suffix}'
        return list(self.db.execute(f'SELECT rowid FROM {index} WHERE {index} MATCH ?', (query,)))

    def test_crawl_insert_upsert_replacement_delete_and_rollback(self):
        for store, (table, key) in pricing._CATALOGUE_TABLES.items():
            with self.subTest(store=store):
                def run(name, brand, price):
                    self.db.execute(f'DELETE FROM {store}_crawl_progress')
                    product = {key: 1, 'name': name, 'brand': brand, 'size': '500g',
                               'price': price, 'was_price': None, 'unit_price': None, 'unit_measure': ''}
                    crawl(self.db, catalogue_table=table, history_table=f'{table}_history',
                          progress_table=f'{store}_crawl_progress', id_field=key,
                          categories=['pantry'], fetch=lambda category, page: ([product], 1),
                          sleep=lambda seconds: None, log=lambda message: None)
                run('Carrots', 'Garden', 2)
                self.assertEqual(self.ids(store, 'carrot'), {'1'})
                run('Potatoes', 'Farm', 3)
                self.assertEqual(self.ids(store, 'potato'), {'1'})
                self.assertEqual(pricing.search(store, 'potato', 'Piara Waters', conn=self.db)[0]['price'], 3)
                for suffix, old, new in [('fts', 'carrot', 'potato'), ('trigrams', 'carr', 'pota')]:
                    self.assertFalse(self.postings(table, suffix, old))
                    self.assertTrue(self.postings(table, suffix, new))
                self.db.execute(f"INSERT OR REPLACE INTO {table}({key},name,brand) VALUES(1,'Cabbage','Field')")
                self.db.commit()
                for suffix, old, new in [('fts', 'potato', 'cabbage'), ('trigrams', 'pota', 'cabb')]:
                    self.assertFalse(self.postings(table, suffix, old))
                    self.assertTrue(self.postings(table, suffix, new))
                self.db.execute(f"UPDATE {table} SET {key}=2,name='Turnip' WHERE {key}=1")
                self.assertEqual(self.ids(store, 'turnip'), {'2'})
                self.db.rollback()
                self.assertEqual(self.ids(store, 'cabbage'), {'1'})
                self.db.execute(f'DELETE FROM {table}')
                self.db.commit()
                for suffix in ('fts', 'trigrams'):
                    self.assertFalse(self.postings(table, suffix, 'cabbage'))
                    self.db.execute(f"INSERT INTO {table}_{suffix}({table}_{suffix}) VALUES('integrity-check')")

    def test_existing_catalogues_are_populated_once_on_schema_upgrade(self):
        with sqlite3.connect(':memory:') as conn:
            base = database.SCHEMA.split('CREATE VIRTUAL TABLE IF NOT EXISTS coles_catalogue_fts')[0]
            conn.executescript(base)
            for table, key in pricing._CATALOGUE_TABLES.values():
                conn.execute(f"INSERT INTO {table}({key},name,brand) VALUES(1,'Carrots','Garden')")
            conn.commit()
            for _ in range(2):
                conn.executescript(database.SCHEMA)
                for table, _key in pricing._CATALOGUE_TABLES.values():
                    for suffix in ('fts', 'trigrams'):
                        index = f'{table}_{suffix}'
                        self.assertEqual(conn.execute(f'SELECT count(*) FROM {index}').fetchone()[0], 1)
                        self.assertEqual(conn.execute(f'SELECT count(*) FROM {index} WHERE {index} MATCH ?',
                                                      ('carrot',)).fetchone()[0], 1)

    def test_stemming_word_order_and_typo_retrieval_keep_explicit_words_safe(self):
        self.db.executemany('INSERT INTO coles_catalogue(coles_id,name,brand) VALUES(?,?,?)',
                            [(1, 'Frozen Cherries', 'Orchard'), (2, 'Cheese', 'Dairy'),
                             (3, 'Onion Cheese Chips', 'Smiths')])
        for query in ('cherry', 'cherries', 'frozen cherry'):
            self.assertIn('1', self.ids('coles', query))
        for query in ('smiths cheese and onion', 'onion cheese smiths'):
            candidates = pricing.search('coles', query, conn=self.db)
            self.assertEqual(pricing.strongest_match(query, candidates)['store_item_id'], '3')
        candidates = pricing.search('coles', 'chese', conn=self.db)
        self.assertIn('2', {c['store_item_id'] for c in candidates})
        self.assertIsNone(pricing.strongest_match('chese', candidates))
        self.assertEqual(self.ids('coles', '" OR * ( )'), set())

    def test_literal_aliases_are_retrieved_without_a_separate_brand(self):
        self.db.execute("INSERT INTO coles_catalogue(coles_id,name,size) VALUES(1,'Coke','2L')")
        candidates = pricing.search('coles', '2l coke', conn=self.db)
        self.assertEqual(pricing.strongest_match('2l coke', candidates)['store_item_id'], '1')

    def test_bm25_orders_candidates_within_the_preserved_window(self):
        self.db.executemany('INSERT INTO coles_catalogue(coles_id,name,brand) VALUES(?,?,?)',
                            [(1, 'Milk', ''), (2, 'Milk Milk Milk', '')])
        ranked = pricing.search('coles', 'milk', conn=self.db)
        expected = [str(r[0]) for r in self.db.execute(
            "SELECT rowid FROM coles_catalogue_fts WHERE coles_catalogue_fts MATCH 'milk' "
            'ORDER BY bm25(coles_catalogue_fts), rowid')]
        self.assertEqual([c['store_item_id'] for c in ranked], expected)

    def test_iga_location_and_read_only_search(self):
        self.db.execute("INSERT INTO iga_catalogue(item_id,name) VALUES('1','Carrots')")
        self.db.commit()
        self.db.execute('PRAGMA query_only=ON')
        self.assertEqual(self.ids('iga', 'carrot'), {'1'})
        self.assertEqual(pricing.search('iga', 'carrot', 'other', conn=self.db), [])
