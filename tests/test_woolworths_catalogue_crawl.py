import sqlite3
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from scripts.woolworths_catalogue_crawl import crawl


def product(stockcode, name, price=1.0):
    return {"stockcode": stockcode, "name": name, "brand": "", "size": "1kg",
            "price": price, "was_price": None, "unit_price": None, "unit_measure": ""}


class WoolworthsCatalogueCrawl(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)
        self.addCleanup(self.db.close)

    def test_a_department_that_runs_out_of_results_gets_marked_done(self):
        def fetch(category, page):
            if page <= 2:
                return [product(page * 100 + i, f"Item {page}-{i}") for i in range(12)], 24
            return [], 24
        result = crawl(self.db, categories=["1_39FD49C"], delay=0, page_size=12, sleep=lambda s: None, fetch=fetch, log=lambda *a: None)
        self.assertEqual(result["products"], 24)
        self.assertFalse(result["blocked"])
        row = self.db.execute("SELECT done FROM woolworths_crawl_progress WHERE category='1_39FD49C'").fetchone()
        self.assertEqual(row["done"], 1)

    def test_every_run_appends_a_history_row_instead_of_only_overwriting(self):
        def fetch(category, page):
            return ([product(1, "Rice", price=4.0)], 1) if page == 1 else ([], 1)
        crawl(self.db, categories=["1_39FD49C"], delay=0, page_size=1, sleep=lambda s: None, fetch=fetch, log=lambda *a: None)
        def fetch2(category, page):
            return ([product(1, "Rice", price=5.0)], 1) if page == 1 else ([], 1)
        self.db.execute("UPDATE woolworths_crawl_progress SET next_page=1, done=0 WHERE category='1_39FD49C'")
        self.db.commit()
        crawl(self.db, categories=["1_39FD49C"], delay=0, page_size=1, sleep=lambda s: None, fetch=fetch2, log=lambda *a: None)
        prices = [r["price"] for r in self.db.execute(
            "SELECT price FROM woolworths_catalogue_history WHERE stockcode=1 ORDER BY id").fetchall()]
        self.assertEqual(prices, [4.0, 5.0])
        latest = self.db.execute("SELECT price FROM woolworths_catalogue WHERE stockcode=1").fetchone()
        self.assertEqual(latest["price"], 5.0)

    def test_repeated_failure_stops_the_whole_run_and_saves_the_spot(self):
        calls = []
        def fetch(category, page):
            calls.append((category, page))
            raise ValueError("blocked")
        result = crawl(self.db, categories=["1_39FD49C", "1_5AF3A0A"], delay=0, sleep=lambda s: None,
                        fetch=fetch, log=lambda *a: None, max_retries=2)
        self.assertTrue(result["blocked"])
        self.assertTrue(all(c == "1_39FD49C" for c, _ in calls))

    def test_default_run_shares_one_cookied_session_across_every_request(self):
        from unittest.mock import patch
        from app.pricing import stores
        calls = []
        class FakeSession:
            closed = False
            def close(self):
                self.closed = True
        fake = FakeSession()
        def fake_browse(category, page, page_size, session=None):
            calls.append(session)
            return [], 0
        with patch.object(stores, 'woolworths_browse_session', return_value=fake) as opener, \
             patch.object(stores, 'browse_woolworths_category', side_effect=fake_browse):
            crawl(self.db, categories=["1_39FD49C", "1_5AF3A0A"], delay=0, sleep=lambda s: None, log=lambda *a: None)
        opener.assert_called_once()
        self.assertTrue(all(s is fake for s in calls))
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
