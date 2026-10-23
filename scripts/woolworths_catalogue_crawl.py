#!/usr/bin/env python3
"""Rebuilds the local Woolworths catalogue by walking its department
listing pages.

Woolworths' search box already works live, but asking it one item at a
time means never having real history for anything beyond the household's
own shortlist, and never catching a fake "was/now" special on a product
nobody's added yet. So this walks every department once, page by page,
the same way the Coles crawler does -- matching then becomes a local
lookup and every product gets a timestamped history entry on every run.

Meant to run on a schedule: python scripts/woolworths_catalogue_crawl.py
"""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.pricing import stores
from scripts.catalogue_crawl import crawl as _crawl


def crawl(db, categories=None, delay=1.0, page_size=36, fetch=None, sleep=None, log=print, max_retries=2):
    categories = categories if categories is not None else stores.WOOLWORTHS_CATALOGUE_CATEGORIES
    session = None
    if fetch is None:
        session = stores.woolworths_browse_session()
        fetch = lambda category, page: stores.browse_woolworths_category(category, page, page_size, session=session)
    try:
        return _crawl(db, catalogue_table="woolworths_catalogue", history_table="woolworths_catalogue_history",
                      progress_table="woolworths_crawl_progress", id_field="stockcode", categories=categories,
                      fetch=fetch, delay=delay, page_size=page_size, sleep=sleep, log=log, max_retries=max_retries)
    finally:
        if session is not None:
            session.close()


def main():
    database.init_db()
    db = database.connect()
    try:
        result = crawl(db)
        status = "hit a block, will resume next run" if result["blocked"] else "finished a full pass"
        print(f"Woolworths catalogue crawl: {result['products']} new/updated products this run, {status}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
