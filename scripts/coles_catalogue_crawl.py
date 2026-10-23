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

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.pricing import stores
from scripts.catalogue_crawl import crawl as _crawl


def crawl(db, categories=None, delay=12.0, page_size=48, fetch=None, sleep=None, log=print, max_retries=2):
    categories = categories if categories is not None else stores.CATALOGUE_CATEGORIES
    # One cookied session for the whole run -- a fresh client with no
    # cookies on every request looks like a brand-new visitor every time,
    # which trips the rate limiter far sooner than a real browsing session
    # would. Only built when nothing's already passed a fetch (tests).
    session = None
    if fetch is None:
        session = stores.coles_browse_session()
        fetch = lambda category, page: stores.browse_coles_category(category, page, session=session)
    try:
        return _crawl(db, catalogue_table="coles_catalogue", history_table="coles_catalogue_history",
                      progress_table="coles_crawl_progress", id_field="coles_id", categories=categories,
                      classification=True, fetch=fetch, delay=delay, page_size=page_size, sleep=sleep, log=log, max_retries=max_retries)
    finally:
        if session is not None:
            session.close()


def main():
    database.init_db()
    db = database.connect()
    try:
        result = crawl(db)
        status = "hit a block, will resume next run" if result["blocked"] else "finished a full pass"
        print(f"Coles catalogue crawl: {result['products']} new/updated products this run, {status}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
