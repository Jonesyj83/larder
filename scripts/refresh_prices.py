#!/usr/bin/env python3
"""Refresh this household's matched items and retry missing store matches, outside the web process.

Run from a checkout with its virtual environment and LARDER_DATA set to the
same directory as the web service. Example cron entry (nightly at 03:15):
15 3 * * * cd /opt/larder && LARDER_DATA=/var/lib/larder .venv/bin/python scripts/refresh_prices.py >> /var/log/larder-prices.log 2>&1

Requests are paced by 1.5 seconds. Three consecutive failures disable a store
for this run. Missing IDs and unavailable prices append an unknown observation;
old observations remain available as history. Missing matches get up to 100 paced
searches per nightly run; existing links are preserved.

Coles no longer needs special handling here -- matching against it is just a
local lookup against the catalogue crawled on a schedule (see
scripts/coles_catalogue_crawl.py), not a live request, so it runs alongside
every other store.
"""
import logging
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.pricing import auto_match_all, record_price, search

log = logging.getLogger(__name__)


def refresh_prices(db, fetch=None, sleep=None, delay=1.5, match_limit=0, only_stores=None, exclude_stores=()):
    fetch = fetch or search
    sleep = sleep or time.sleep
    failures = {}
    skipped = set(exclude_stores)
    if only_stores:
        enabled = {r["store"] for r in db.execute("SELECT store FROM store_settings WHERE enabled=1")}
        skipped |= enabled - set(only_stores)
    broken = set(skipped)  # one shared, mutable set -- auto_match_all below needs to see breaker trips live
    checked = updated = 0
    requested = False
    links = db.execute("""SELECT l.*,s.postcode FROM product_store_links l
        JOIN store_settings s ON s.store=l.store WHERE s.enabled=1 ORDER BY l.store,l.id""").fetchall()
    for link in links:
        store = link["store"]
        if store in broken:
            continue
        if requested:
            sleep(delay)
        requested = True
        checked += 1
        candidate = None
        try:
            results = fetch(store, link["store_name"], link["postcode"])
            if getattr(results, "failed", False):
                raise ValueError("Store lookup failed")
            if not isinstance(results, list) or any(not isinstance(p, dict) or "store_item_id" not in p for p in results):
                raise ValueError("Malformed store results")
            candidate = next((p for p in results if str(p["store_item_id"]) == link["store_item_id"]), None)
            with db:
                record_price(db, link["id"], candidate)
            if candidate and candidate.get("price") is not None:
                updated += 1
            failures[store] = 0
        except Exception:
            db.rollback()
            failures[store] = failures.get(store, 0) + 1
            log.warning("Price check failed for %s item %s", store, link["store_item_id"], exc_info=True)
            with db:
                record_price(db, link["id"])
            if failures[store] >= 3:
                broken.add(store)
                log.warning("Circuit breaker: %s stopped after three consecutive failures", store)
    if match_limit:
        def pace_match():
            nonlocal requested
            if requested:
                sleep(delay)
            requested = True

        def match_search(store, name, postcode):
            try:
                results = fetch(store, name, postcode)
                if getattr(results, "failed", False) or not isinstance(results, list) or any(
                        not isinstance(p, dict) or "store_item_id" not in p for p in results):
                    raise ValueError("Store lookup failed or returned malformed results")
                failures[store] = 0
                return results
            except Exception:
                failures[store] = failures.get(store, 0) + 1
                if failures[store] >= 3:
                    broken.add(store)
                    log.warning("Circuit breaker: %s stopped after three consecutive failures", store)
                raise

        matches = auto_match_all(db, search_fn=match_search, before_search=pace_match,
                                 excluded_stores=broken, max_searches=match_limit)
        log.info("Missing store matches added: %s", len(matches["matched"]))
    tripped = sorted(broken - skipped)
    summary = {"products_checked": checked, "prices_updated": updated, "circuit_breakers": tripped}
    log.info("Products checked: %s; prices updated: %s; circuit breakers: %s", checked, updated, ", ".join(tripped) or "none")
    return summary


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(asctime)s %(levelname)s %(message)s")
    database.init_db()
    db = database.connect()
    try:
        # Coles no longer needs special-casing here -- matching against it
        # is just a local lookup against the crawled catalogue now (see
        # scripts/coles_catalogue_crawl.py), same cost as any other store.
        refresh_prices(db, match_limit=100)
    finally:
        db.close()


if __name__ == "__main__":
    main()
