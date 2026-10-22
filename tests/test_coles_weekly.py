import sqlite3
import unittest
from unittest.mock import MagicMock

from app import db as database
from app import pricing
from scripts.coles_apply_results import apply_results
from scripts.coles_export_queries import collect_queries
from scripts.coles_fetch_queries import fetch_all

CANDIDATE = dict(store_item_id='123', name='Carrots', brand='', size='1kg', price=2.5,
                  was_price=None, unit_price=None, unit_measure='', on_special=False)


def prepare(db):
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    database.SCHEMA and db.executescript(database.SCHEMA)
    db.execute("INSERT INTO units(id,name) VALUES(1,'each')")
    db.execute("INSERT INTO store_settings(store,enabled,postcode) VALUES"
               "('woolworths',1,''),('coles',1,''),('aldi',0,''),('iga',0,'')")
    db.commit()
    return db


class ColesWeeklySplit(unittest.TestCase):
    def setUp(self):
        self.db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(self.db.close)

    def test_export_lists_existing_links_and_missing_matches_only_for_coles(self):
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Carrot',1),(2,'Bread',1)")
        pricing.link_product(self.db, 1, 'coles', '123', 'Carrots')
        pricing.link_product(self.db, 1, 'woolworths', '999', 'Carrot Fresh')
        self.db.commit()
        data = collect_queries(self.db)
        self.assertEqual(data['refresh'], [{'link_id': 1, 'query': 'Carrots'}])
        self.assertEqual(data['match'], [{'product_id': 2, 'query': 'Bread'}])

    def test_export_respects_match_limit(self):
        for i in range(3):
            self.db.execute("INSERT INTO products(name,unit_id) VALUES(?,1)", (f'Item {i}',))
        self.db.commit()
        data = collect_queries(self.db, match_limit=2)
        self.assertEqual(len(data['match']), 2)

    def test_fetch_paces_requests_and_tolerates_failures(self):
        queries = {'refresh': [{'link_id': 1, 'query': 'Carrots'}],
                   'match': [{'product_id': 2, 'query': 'Bread'}, {'product_id': 3, 'query': 'Milk'}]}
        search = MagicMock(side_effect=[[CANDIDATE], ValueError('blocked'), [CANDIDATE]])
        sleep = MagicMock()
        result = fetch_all(queries, search=search, sleep=sleep)
        self.assertEqual(result['refresh'], [{'link_id': 1, 'results': [CANDIDATE]}])
        self.assertEqual(result['match'], [{'product_id': 2, 'query': 'Bread', 'results': []},
                                            {'product_id': 3, 'query': 'Milk', 'results': [CANDIDATE]}])
        self.assertEqual(sleep.call_count, 2)

    def test_apply_updates_existing_links_and_creates_new_matches(self):
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Carrot',1),(2,'Milk',1)")
        link_id = pricing.link_product(self.db, 1, 'coles', '123', 'Carrots')
        self.db.commit()
        results = {
            'refresh': [{'link_id': link_id, 'results': [CANDIDATE]}],
            'match': [{'product_id': 2, 'query': 'Milk', 'results': [dict(CANDIDATE, name='Milk 2L')]},
                      {'product_id': 2, 'query': 'bogus', 'results': []}],
        }
        summary = apply_results(self.db, results)
        self.assertEqual(summary, {'products_checked': 1, 'prices_updated': 1, 'new_matches': 1})
        row = self.db.execute("SELECT store_item_id FROM product_store_links WHERE product_id=2 AND store='coles'").fetchone()
        self.assertEqual(row['store_item_id'], '123')
        check = self.db.execute("SELECT price FROM store_price_checks WHERE link_id=?", (link_id,)).fetchone()
        self.assertEqual(check['price'], 2.5)

    def test_apply_leaves_a_missing_link_untouched(self):
        results = {'refresh': [{'link_id': 999, 'results': [CANDIDATE]}], 'match': []}
        summary = apply_results(self.db, results)
        self.assertEqual(summary['products_checked'], 1)
        self.assertEqual(summary['prices_updated'], 0)


if __name__ == '__main__':
    unittest.main()
