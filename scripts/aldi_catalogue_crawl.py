#!/usr/bin/env python3
"""Rebuilds the local Aldi catalogue by walking every category page.

search_aldi() already visits a handful of category pages per query and
throws away anything that isn't a good match for the search term -- this
walks every one of Aldi's real category pages (same ones their own site
links to) and keeps everything, so matching becomes a local lookup and
every product gets a timestamped history entry on every run, the same
idea as the Coles and Woolworths catalogue crawls.

Each Aldi category page renders its whole contents in one response (no
further pagination), so every category here only ever takes one page --
page_size is set high enough that the shared crawl loop always treats
page 1 as the last page.

Meant to run on a schedule: python scripts/aldi_catalogue_crawl.py
"""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.pricing import stores
from scripts.catalogue_crawl import crawl as _crawl

PAGE_SIZE = 9999


def crawl(db, categories=None, delay=1.0, fetch=None, sleep=None, log=print, max_retries=2):
    session = None
    if categories is None or fetch is None:
        session = stores.client()
    try:
        if categories is None:
            categories = stores._categories(session)
        if fetch is None:
            fetch = lambda category, page: stores.browse_aldi_category(category, session=session)
        return _crawl(db, catalogue_table="aldi_catalogue", history_table="aldi_catalogue_history",
                      progress_table="aldi_crawl_progress", id_field="item_id", categories=categories,
                      fetch=fetch, delay=delay, page_size=PAGE_SIZE, sleep=sleep, log=log, max_retries=max_retries, classification=True)
    finally:
        if session is not None:
            session.close()


def main():
    database.init_db()
    db = database.connect()
    try:
        result = crawl(db)
        status = "hit a block, will resume next run" if result["blocked"] else "finished a full pass"
        print(f"Aldi catalogue crawl: {result['products']} new/updated products this run, {status}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
