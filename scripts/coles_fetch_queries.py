#!/usr/bin/env python3
"""Step 2 of the weekly Coles refresh: actually ask Coles.

Meant to run somewhere with a fresh IP, not the home server. Doesn't touch
the real database at all -- just takes the query list from step 1 and
writes back whatever Coles said, paced the same as the rest of the app.
"""
import json
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pricing import stores


def fetch_all(queries, search=None, sleep=None, delay=1.5):
    search = search or stores.search_coles
    sleep = sleep or time.sleep
    out = {"refresh": [], "match": []}
    requested = False
    for item in queries.get("refresh", []):
        if requested:
            sleep(delay)
        requested = True
        try:
            results = search(item["query"])
        except Exception as e:
            print(f"refresh query {item['query']!r} failed: {type(e).__name__}: {e}", file=sys.stderr)
            results = []
        out["refresh"].append({"link_id": item["link_id"], "results": results})
    for item in queries.get("match", []):
        if requested:
            sleep(delay)
        requested = True
        try:
            results = search(item["query"])
        except Exception as e:
            print(f"match query {item['query']!r} failed: {type(e).__name__}: {e}", file=sys.stderr)
            results = []
        out["match"].append({"product_id": item["product_id"], "query": item["query"], "results": results})
        if not results:
            print(f"match query {item['query']!r} returned no results", file=sys.stderr)
    return out


def main():
    in_path = sys.argv[1] if len(sys.argv) > 1 else "coles-queries.json"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "coles-results.json"
    queries = json.loads(Path(in_path).read_text())
    results = fetch_all(queries)
    Path(out_path).write_text(json.dumps(results))
    found = sum(1 for r in results["refresh"] + results["match"] if r["results"])
    print(f"got results for {found} of {len(results['refresh']) + len(results['match'])} queries, wrote {out_path}")


if __name__ == "__main__":
    main()
