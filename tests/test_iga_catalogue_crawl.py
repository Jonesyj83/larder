import sqlite3
import unittest
from unittest.mock import MagicMock, patch

from app import db as database, pricing
from scripts.iga_catalogue_crawl import crawl


def product(item_id, price=2):
    return dict(item_id=item_id, name='Milk', brand='', size='1L', price=price,
                was_price=None, unit_price=None, unit_measure='')


class IgaCatalogueCrawl(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)
        self.db.execute("INSERT INTO store_settings(store,postcode) VALUES('iga','Piara Waters')")

    def run_crawl(self, fetch):
        return crawl(self.db, categories=['Dairy'], fetch=fetch, delay=0,
                     sleep=lambda _: None, log=lambda *a: None, max_retries=1)

    def test_resume_and_weekly_history(self):
        def interrupted(category, page):
            if page == 2:
                raise ValueError('unavailable')
            return [product(str(i)) for i in range(36)], 37
        self.assertTrue(self.run_crawl(interrupted)['blocked'])
        calls = []
        def resumed(category, page):
            calls.append(page)
            return [product('36')], 37
        self.assertFalse(self.run_crawl(resumed)['blocked'])
        self.assertEqual(calls, [2])
        self.run_crawl(lambda category, page: ([product('36', 3)], 1))
        self.assertEqual([r[0] for r in self.db.execute(
            "SELECT price FROM iga_catalogue_history WHERE item_id='36' ORDER BY id")], [2, 3])
        self.assertEqual(self.db.execute("SELECT price FROM iga_catalogue WHERE item_id='36'").fetchone()[0], 3)

    def test_store_binding_and_local_lookup(self):
        session = MagicMock()
        with patch.object(pricing.stores, 'client', return_value=session), \
             patch.object(pricing.stores, '_iga_store_for_location', return_value='48323'), \
             patch.object(pricing.stores, 'iga_categories', return_value=['Dairy']), \
             patch.object(pricing.stores, 'browse_iga_category', return_value=([product('1')], 1)) as browse:
            crawl(self.db, delay=0, log=lambda *a: None)
        browse.assert_called_once_with('48323', 'Dairy', 1, 36, session=session)
        session.close.assert_called_once()
        with patch.object(pricing.stores, 'client', side_effect=AssertionError('local lookup')):
            self.assertEqual(pricing.search('iga', 'milk', 'Piara Waters', conn=self.db)[0]['store_item_id'], '1')
            self.assertEqual(pricing.search('iga', 'milk', '6000', conn=self.db), [])
        with patch.object(pricing.stores, 'client', return_value=MagicMock()), \
             patch.object(pricing.stores, '_iga_store_for_location', return_value='other'):
            with self.assertRaisesRegex(ValueError, 'store changed'):
                crawl(self.db)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM iga_catalogue_history').fetchone()[0], 1)

    def test_missing_location_stops_before_network(self):
        self.db.execute("UPDATE store_settings SET postcode=''")
        with patch.object(pricing.stores, 'client') as client:
            with self.assertRaisesRegex(ValueError, 'requires an area or postcode'):
                crawl(self.db)
        client.assert_not_called()
