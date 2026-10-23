import sqlite3
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from scripts.coles_catalogue_crawl import crawl


def product(coles_id, name, price=1.0):
    return {"coles_id": coles_id, "name": name, "brand": "", "size": "1kg",
            "price": price, "was_price": None, "unit_price": None, "unit_measure": ""}


class ColesCatalogueCrawl(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)
        self.addCleanup(self.db.close)

    def test_a_category_that_runs_out_of_results_gets_marked_done(self):
        def fetch(category, page):
            if page <= 2:
                return [product(page * 100 + i, f"Item {page}-{i}") for i in range(12)], 24
            return [], 24
        result = crawl(self.db, categories=["drinks"], delay=0, page_size=12, sleep=lambda s: None, fetch=fetch, log=lambda *a: None)
        self.assertEqual(result["products"], 24)
        self.assertFalse(result["blocked"])
        row = self.db.execute("SELECT done FROM coles_crawl_progress WHERE category='drinks'").fetchone()
        self.assertEqual(row["done"], 1)

    def test_every_run_appends_a_history_row_instead_of_only_overwriting(self):
        def fetch(category, page):
            return ([product(1, "Rice", price=4.0)], 1) if page == 1 else ([], 1)
        crawl(self.db, categories=["pantry"], delay=0, page_size=1, sleep=lambda s: None, fetch=fetch, log=lambda *a: None)
        # Run it again later with a different price -- the old observation
        # must still be there, not overwritten.
        def fetch2(category, page):
            return ([product(1, "Rice", price=5.0)], 1) if page == 1 else ([], 1)
        self.db.execute("UPDATE coles_crawl_progress SET next_page=1, done=0 WHERE category='pantry'")
        self.db.commit()
        crawl(self.db, categories=["pantry"], delay=0, page_size=1, sleep=lambda s: None, fetch=fetch2, log=lambda *a: None)
        prices = [r["price"] for r in self.db.execute(
            "SELECT price FROM coles_catalogue_history WHERE coles_id=1 ORDER BY id").fetchall()]
        self.assertEqual(prices, [4.0, 5.0])
        # The catalogue itself only ever shows the latest snapshot.
        latest = self.db.execute("SELECT price FROM coles_catalogue WHERE coles_id=1").fetchone()
        self.assertEqual(latest["price"], 5.0)

    def test_repeated_failure_stops_the_whole_run_and_saves_the_spot(self):
        calls = []
        def fetch(category, page):
            calls.append((category, page))
            raise ValueError("blocked")
        result = crawl(self.db, categories=["drinks", "pantry"], delay=0, sleep=lambda s: None,
                        fetch=fetch, log=lambda *a: None, max_retries=2)
        self.assertTrue(result["blocked"])
        self.assertEqual(result["products"], 0)
        # Never even touched pantry -- no point hammering a second category
        # when the block is global, not per-category.
        self.assertTrue(all(c == "drinks" for c, _ in calls))
        row = self.db.execute("SELECT next_page, done FROM coles_crawl_progress WHERE category='drinks'").fetchone()
        self.assertEqual((row["next_page"], row["done"]), (1, 0))

    def test_a_later_run_resumes_from_the_saved_page_not_page_one(self):
        self.db.execute("INSERT INTO coles_crawl_progress(category, next_page, done) VALUES('drinks', 3, 0)")
        self.db.commit()
        seen_pages = []
        def fetch(category, page):
            seen_pages.append(page)
            return ([], 0)
        crawl(self.db, categories=["drinks"], delay=0, sleep=lambda s: None, fetch=fetch, log=lambda *a: None)
        self.assertEqual(seen_pages, [3])

    def test_a_category_already_marked_done_is_skipped_entirely(self):
        self.db.execute("INSERT INTO coles_crawl_progress(category, next_page, done) VALUES('drinks', 9, 1)")
        self.db.commit()
        def fetch(category, page):
            raise AssertionError("should not be called")
        result = crawl(self.db, categories=["drinks"], delay=0, sleep=lambda s: None, fetch=fetch, log=lambda *a: None)
        self.assertEqual(result["products"], 0)

    def test_default_run_shares_one_cookied_session_across_every_request(self):
        from unittest.mock import patch
        from app.pricing import stores
        calls = []
        class FakeSession:
            closed = False
            def close(self):
                self.closed = True
        fake = FakeSession()
        def fake_browse(category, page, session=None):
            calls.append(session)
            return [], 0
        with patch.object(stores, 'coles_browse_session', return_value=fake) as opener, \
             patch.object(stores, 'browse_coles_category', side_effect=fake_browse):
            crawl(self.db, categories=["drinks", "pantry"], delay=0, sleep=lambda s: None, log=lambda *a: None)
        opener.assert_called_once()
        # Every single page request used the SAME session object, not a
        # fresh one each time.
        self.assertTrue(all(s is fake for s in calls))
        self.assertTrue(fake.closed)


if __name__ == "__main__":
    unittest.main()
