import sqlite3
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from scripts.aldi_catalogue_crawl import crawl, PAGE_SIZE


def product(item_id, name, price=1.0):
    return {"item_id": item_id, "name": name, "brand": "", "size": "1kg",
            "price": price, "was_price": None, "unit_price": None, "unit_measure": ""}


class AldiCatalogueCrawl(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)
        self.addCleanup(self.db.close)

    def test_a_category_page_is_captured_whole_in_a_single_pass(self):
        def fetch(category, page):
            items = [product(str(i), f"Item {i}") for i in range(5)]
            return items, len(items)
        result = crawl(self.db, categories=["/products/fresh/k/1"], delay=0, fetch=fetch, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(result["products"], 5)
        self.assertFalse(result["blocked"])
        row = self.db.execute("SELECT done FROM aldi_crawl_progress WHERE category='/products/fresh/k/1'").fetchone()
        self.assertEqual(row["done"], 1)

    def test_every_run_appends_a_history_row_instead_of_only_overwriting(self):
        def fetch(category, page):
            return [product("1", "Bananas", price=2.0)], 1
        crawl(self.db, categories=["/products/fresh/k/1"], delay=0, fetch=fetch, sleep=lambda s: None, log=lambda *a: None)
        def fetch2(category, page):
            return [product("1", "Bananas", price=2.5)], 1
        self.db.execute("UPDATE aldi_crawl_progress SET next_page=1, done=0 WHERE category='/products/fresh/k/1'")
        self.db.commit()
        crawl(self.db, categories=["/products/fresh/k/1"], delay=0, fetch=fetch2, sleep=lambda s: None, log=lambda *a: None)
        prices = [r["price"] for r in self.db.execute(
            "SELECT price FROM aldi_catalogue_history WHERE item_id='1' ORDER BY id").fetchall()]
        self.assertEqual(prices, [2.0, 2.5])
        latest = self.db.execute("SELECT price FROM aldi_catalogue WHERE item_id='1'").fetchone()
        self.assertEqual(latest["price"], 2.5)

    def test_discovers_real_categories_and_shares_one_session_when_not_overridden(self):
        from unittest.mock import patch, MagicMock
        from app.pricing import stores
        fake_session = MagicMock()
        with patch.object(stores, 'client', return_value=fake_session), \
             patch.object(stores, '_categories', return_value=["/products/fresh/k/1"]) as cats, \
             patch.object(stores, 'browse_aldi_category', return_value=([], 0)) as browse:
            crawl(self.db, delay=0, sleep=lambda s: None, log=lambda *a: None)
        cats.assert_called_once_with(fake_session)
        browse.assert_called_once_with("/products/fresh/k/1", session=fake_session)
        fake_session.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
