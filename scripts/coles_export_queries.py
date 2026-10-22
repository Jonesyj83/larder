#!/usr/bin/env python3
"""Step 1 of the weekly Coles refresh: list what needs asking.

Runs on the home server (has the real database). Writes a small JSON file
of queries -- nothing else -- for a clean-IP runner to go fetch. Doesn't
touch Coles itself.
"""
import json
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db as database
from app.db import rows


def collect_queries(db, match_limit=40):
    refresh = rows(db.execute("""SELECT l.id AS link_id, l.store_item_id, l.store_name FROM product_store_links l
        JOIN store_settings s ON s.store=l.store WHERE l.store='coles' AND s.enabled=1 ORDER BY l.id"""))
    # Whatever's sitting on a list right now is what someone's actually
    # waiting on -- check those first, newest first, then work backwards
    # through the rest of the backlog so it still clears over time instead
    # of getting stuck forever behind old unused products.
    missing = rows(db.execute("""SELECT p.id AS product_id, p.name FROM products p WHERE EXISTS (
        SELECT 1 FROM store_settings s WHERE s.store='coles' AND s.enabled=1) AND NOT EXISTS (
        SELECT 1 FROM product_store_links l WHERE l.product_id=p.id AND l.store='coles')
        ORDER BY (EXISTS (SELECT 1 FROM shopping_list sl WHERE sl.product_id=p.id)) DESC, p.id DESC LIMIT ?""", (match_limit,)))
    return {"refresh": [{"link_id": r["link_id"], "query": r["store_name"]} for r in refresh],
            "match": [{"product_id": m["product_id"], "query": m["name"]} for m in missing]}


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else "coles-queries.json"
    database.init_db()
    db = database.connect()
    try:
        data = collect_queries(db)
    finally:
        db.close()
    Path(out_path).write_text(json.dumps(data))
    print(f"wrote {len(data['refresh'])} refresh + {len(data['match'])} match queries to {out_path}")


if __name__ == "__main__":
    main()
