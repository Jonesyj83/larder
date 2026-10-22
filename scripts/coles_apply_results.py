#!/usr/bin/env python3
"""Step 3 of the weekly Coles refresh: write the answers into the real data.

Runs back on the home server. Takes whatever step 2 fetched from a clean
IP and applies it the same way the nightly refresh would -- an exact-ID
lookup for items we already track, strongest_match for anything still
unmatched. Never talks to Coles itself.
"""
import json
import logging
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.db import row
from app.pricing import link_product, record_price, size_preference, strongest_match

log = logging.getLogger(__name__)


def apply_results(db, results):
    checked = updated = matched = 0
    for item in results.get("refresh", []):
        checked += 1
        link = row(db.execute("SELECT store_item_id FROM product_store_links WHERE id=?", (item["link_id"],)))
        if not link:
            continue
        candidate = next((p for p in item["results"] if str(p.get("store_item_id")) == link["store_item_id"]), None)
        with db:
            record_price(db, item["link_id"], candidate)
        if candidate and candidate.get("price") is not None:
            updated += 1
    for item in results.get("match", []):
        if not item["results"]:
            continue
        candidate = strongest_match(item["query"], item["results"], size_preference(db, item["product_id"], "coles"))
        if not candidate:
            continue
        with db:
            link_id = link_product(db, item["product_id"], "coles", str(candidate["store_item_id"]),
                                    candidate["name"], size=candidate.get("size"))
            record_price(db, link_id, candidate)
        matched += 1
    return {"products_checked": checked, "prices_updated": updated, "new_matches": matched}


def main():
    in_path = sys.argv[1] if len(sys.argv) > 1 else "coles-results.json"
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    results = json.loads(Path(in_path).read_text())
    database.init_db()
    db = database.connect()
    try:
        summary = apply_results(db, results)
    finally:
        db.close()
    log.info("Coles weekly refresh applied: %s", summary)


if __name__ == "__main__":
    main()
