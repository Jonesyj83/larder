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


if __name__ == "__main__":
    unittest.main()
