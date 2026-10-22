#!/usr/bin/env python3
"""Rebuilds the local Coles catalogue by walking its browse-category pages.

Coles' search box is behind a bot wall, but its plain category listing
pages aren't -- they're just a normal paginated product grid, same thing
a shopper sees scrolling the site. So instead of asking Coles about one
item at a time and hoping it answers, this walks every category once,
page by page, and keeps the whole lot locally. Matching an item to Coles
then becomes a local lookup, same as it would be if we already knew their
full range -- because now we do.

Meant to run on a schedule (weekly to start with, cheap enough to go more
often later): python scripts/coles_catalogue_crawl.py
"""
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.pricing import stores


def crawl(db, categories=None, delay=12.0, page_size=48, fetch=None, sleep=None, log=print, max_retries=3):
    fetch = fetch or stores.browse_coles_category
    sleep = sleep or time.sleep
    categories = categories if categories is not None else stores.CATALOGUE_CATEGORIES
    seen = set()
    total_written = 0
    requested = False
    for category in categories:
        page = 1
        misses = 0
        while True:
            if requested:
                sleep(delay)
            requested = True
            try:
                products, total = fetch(category, page)
            except Exception as exc:
                misses += 1
                # A soft block eases off after a cooldown, but hitting it
                # again right away just extends it -- back off hard rather
                # than hammering the same wall.
                log(f"{category} page {page} failed: {type(exc).__name__}: {exc}")
                if misses >= max_retries:
                    log(f"{category}: giving up for this run after {misses} misses")
                    break
                sleep(delay * (3 ** misses))
                continue
            misses = 0
            if not products:
                break
            for p in products:
                if p["coles_id"] in seen:
                    continue
                seen.add(p["coles_id"])
                db.execute("""INSERT INTO coles_catalogue
                        (coles_id, name, brand, size, price, was_price, unit_price, unit_measure, category, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,datetime('now'))
                    ON CONFLICT(coles_id) DO UPDATE SET
                        name=excluded.name, brand=excluded.brand, size=excluded.size,
                        price=excluded.price, was_price=excluded.was_price, unit_price=excluded.unit_price,
                        unit_measure=excluded.unit_measure, category=excluded.category, updated_at=excluded.updated_at""",
                    (p["coles_id"], p["name"], p["brand"], p["size"], p["price"], p["was_price"],
                     p["unit_price"], p["unit_measure"], category))
                total_written += 1
            db.commit()
            log(f"{category} page {page}: {len(products)} products ({total} total in category)")
            if page * page_size >= total or len(products) < 10:
                break
            page += 1
    return {"categories": len(categories), "products": total_written}


def main():
    database.init_db()
    db = database.connect()
    try:
        result = crawl(db)
        print(f"Coles catalogue crawl done: {result['products']} products across {result['categories']} categories")
    finally:
        db.close()


if __name__ == "__main__":
    main()
