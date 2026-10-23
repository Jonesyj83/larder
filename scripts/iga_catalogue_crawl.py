#!/usr/bin/env python3
"""Walk the configured IGA store's departments into a local catalogue.

A store change needs a separate catalogue: national IGA prices do not
exist, and retaining another owner's prices would give false comparisons.
"""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.pricing import stores
from scripts.catalogue_crawl import crawl as _crawl


def crawl(db, categories=None, delay=1.0, page_size=36, fetch=None, sleep=None, log=print, max_retries=2):
    setting = db.execute("SELECT postcode FROM store_settings WHERE store='iga'").fetchone()
    location = setting['postcode'].strip().casefold() if setting else ""
    if not location:
        raise ValueError("IGA matching requires an area or postcode in Settings")
    session = None
    try:
        if fetch is None or categories is None:
            session = stores.client()
            store_id = stores._iga_store_for_location(session, location)
            source = db.execute("SELECT store_id FROM iga_catalogue_store WHERE id=1").fetchone()
            if source and source['store_id'] != store_id:
                raise ValueError("IGA store changed; use a separate catalogue for the new store")
            db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET location=excluded.location", (store_id, location))
            db.commit()
            if categories is None:
                categories = stores.iga_categories(session)
            if fetch is None:
                fetch = lambda category, page: stores.browse_iga_category(store_id, category, page, page_size, session=session)
        # Resume an interrupted pass, but begin again after a completed one
        # so weekly runs keep appending price observations.
        if categories and all(db.execute("SELECT 1 FROM iga_crawl_progress WHERE category=? AND done=1",
                                         (category,)).fetchone() for category in categories):
            db.execute("UPDATE iga_crawl_progress SET next_page=1, done=0")
            db.commit()
        return _crawl(db, catalogue_table="iga_catalogue", history_table="iga_catalogue_history",
                      progress_table="iga_crawl_progress", id_field="item_id", categories=categories,
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
        print(f"IGA catalogue crawl: {result['products']} new/updated products this run, {status}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
