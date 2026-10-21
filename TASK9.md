# Larder: store price matching + nightly refresh (phase 1 - backend)

Full feature from household discussion: compare Woolworths/Coles/Aldi/IGA
prices per product, match once per product (not per shopping-list-add),
refresh nightly by known item ID (not a live search every time), one
immediate single-item lookup only when a genuinely new product is
created. This phase is backend + schema only - no shopping list UI yet,
that's phase 2. Build this to actually work against the real sites; every
technique below was tested live and confirmed working minutes before this
brief was written, so implement it as specified rather than guessing at a
different approach.

## Hard constraints

- Everything lives inside this repo (`app/pricing/` package) - no external
  service, no paid scraping API. The user explicitly wants this
  self-contained so a Claude-MCP-only user (not just this specific
  household's Hermes setup) gets the same feature by cloning the repo.
- Zero AI-authorship trace - no comments/commits/copy mentioning AI/
  Claude/Codex/agent/LLM anywhere.
- Don't touch the design/CSS or existing page templates in this phase.
- Use `httpx` (already a dependency) for all HTTP calls, not urllib/requests.
- Every outbound request needs a realistic browser User-Agent header and a
  sane timeout (10-15s). Never retry a failed request in a tight loop.
- This is real traffic to real third-party sites. Keep requests to what's
  actually needed - one request per product per store per refresh cycle,
  never a broad "pull everything" sweep. A single household's product
  list is the only data this ever indexes, not a general grocery catalogue.

## Confirmed working techniques (use these exactly)

**Woolworths** - cookie handshake then POST to their search API:

```python
import httpx
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

def search_woolworths(query: str, limit: int = 12) -> list[dict]:
    with httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        client.get("https://www.woolworths.com.au/")  # sets required cookies
        resp = client.post(
            "https://www.woolworths.com.au/apis/ui/Search/products",
            json={"SearchTerm": query, "PageNumber": 1, "PageSize": min(limit, 36)},
        )
        resp.raise_for_status()
        data = resp.json()
    flat = [p for block in data.get("Products") or [] for p in (block.get("Products") or [block])]
    return [{
        "store_item_id": str(p["Stockcode"]), "name": p["Name"], "brand": p.get("Brand", ""),
        "size": p.get("PackageSize", ""), "price": p.get("Price"), "was_price": p.get("WasPrice"),
        "unit_price": p.get("CupPrice"), "unit_measure": p.get("CupMeasure"),
        "on_special": bool(p.get("IsOnSpecial")),
    } for p in flat if p.get("Stockcode")]
```

Refreshing a single already-known Stockcode: same POST, search by the
stored product name is fine for a refresh since Woolworths returns the
same Stockcode in results and this module should match on `store_item_id`
in the returned list rather than assuming position; if the known
Stockcode isn't found in that response, treat the price as stale/unknown
for this cycle rather than guessing.

**Coles** - plain GET, parse the `__NEXT_DATA__` script tag, no cookie
handshake needed:

```python
import json, re

def search_coles(query: str, limit: int = 12) -> list[dict]:
    with httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        resp = client.get("https://www.coles.com.au/search/products", params={"q": query})
        resp.raise_for_status()
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
    if not m:
        return []
    data = json.loads(m.group(1))
    results = (data.get("props", {}).get("pageProps", {}).get("searchResults", {}) or {}).get("results") or []
    out = []
    for p in results[:limit]:
        if p.get("_type") != "PRODUCT":
            continue
        pricing = p.get("pricing") or {}
        out.append({
            "store_item_id": str(p["id"]), "name": p["name"], "brand": p.get("brand", ""),
            "size": p.get("size", ""), "price": pricing.get("now"), "was_price": pricing.get("was") or None,
            "unit_price": (pricing.get("unit") or {}).get("price"),
            "unit_measure": f"{(pricing.get('unit') or {}).get('ofMeasureQuantity')}{(pricing.get('unit') or {}).get('ofMeasureType')}",
            "on_special": bool(pricing.get("onlineSpecial")),
        })
    return out
```

**Aldi** - plain GET on a category page (NOT the `api.aldi.com.au` AJAX
endpoint - that one sits behind Akamai and returns 403; the rendered page
itself is not blocked), parse the `__NUXT_DATA__` script tag. This is
Nuxt's "devalue" flattened-array format, not plain nested JSON: index 0 of
the parsed array is the root value, every other slot is a value that
earlier slots reference by integer index. Write a small `_unflatten(arr)`
helper: walk the structure recursively, and whenever you hit a bare `int`
in a position where a nested value is expected, replace it with
`arr[that_int]` (recursing, with a seen-set to avoid infinite loops on
self-referential slots - skip/leave-as-is on a repeat). Confirmed real
product entries exist in this payload (tested against
`https://www.aldi.com.au/products/dairy-eggs-fridge/k/960000000` type
category URLs - category IDs are visible in the rendered page's own
category nav links, scrape that nav once at startup/rarely and cache it,
don't hardcode a fixed list since Aldi's range rotates). Given the parser
is genuinely more work than the other two, it's fine for this module to
come back with fewer/no results on a parse failure (log and return `[]`)
rather than crashing the whole refresh cycle - treat Aldi as best-effort,
not a hard dependency the whole feature breaks without.

**IGA** - plain GET, same `__NEXT_DATA__` pattern as Coles, but
`igashop.com.au` (not `iga.com.au`) and pricing is per-store: a plain
search without a store selected returns no results at all (confirmed -
the page loads fine, embeds `__NEXT_DATA__`, but has no `products`/
`results` key in it). This module needs a store/postcode selected first -
check the rendered homepage or a store-locator endpoint for how the
selection is passed (a cookie, most likely, matching the Coles/Woolworths
pattern of a session cookie carrying state) and thread a configured
postcode through. Same best-effort treatment as Aldi: log and return `[]`
on failure rather than crash the cycle. If this one genuinely can't be
made to work within reasonable effort, it's fine to ship the other three
and leave a clear `# not yet working` note rather than forcing something
broken - don't fake a result.

## Schema additions (`app/db.py`)

Add to `SCHEMA` plus matching additive migrations (check-then-`ALTER
TABLE`/`CREATE TABLE IF NOT EXISTS`, same pattern as every existing
migration in this file):

```sql
CREATE TABLE IF NOT EXISTS store_settings (
    store TEXT PRIMARY KEY,  -- 'woolworths', 'coles', 'aldi', 'iga'
    enabled INTEGER NOT NULL DEFAULT 1,
    postcode TEXT NOT NULL DEFAULT ''  -- used by iga, ignored by the others
);

CREATE TABLE IF NOT EXISTS product_store_links (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store TEXT NOT NULL,
    store_item_id TEXT NOT NULL,
    store_name TEXT NOT NULL DEFAULT '',  -- the matched item's name at that store, for display
    matched_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product_id, store)
);

CREATE TABLE IF NOT EXISTS store_price_checks (
    id INTEGER PRIMARY KEY,
    link_id INTEGER NOT NULL REFERENCES product_store_links(id) ON DELETE CASCADE,
    price REAL,
    was_price REAL,
    unit_price REAL,
    unit_measure TEXT NOT NULL DEFAULT '',
    on_special INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Seed `store_settings` with all four stores enabled by default on first
init (same pattern as `DEFAULT_LOCATIONS` seeding) - this is what phase 2's
Settings toggle will read/write, build the table now even though the
toggle UI doesn't exist yet.

`store_price_checks` is deliberately an append-only ledger (never
updated/deleted except via cascade) - this is what phase 3's price-history
line graph and fake-discount detection will read from. Don't collapse it
down to "latest price only" on `product_store_links` - both are needed,
same reasoning as the existing `stock` vs `stock_log` split in this
codebase.

## API surface (`app/routes/pricing.py`, new file, register in `main.py`
same way `receipts` was just added)

- `GET /api/pricing/stores` - returns `store_settings` rows.
- `PUT /api/pricing/stores/{store}` - body `{enabled: bool, postcode: str
  = ''}`, upserts into `store_settings`. 404 on an unknown store name.
- `GET /api/pricing/search?store=woolworths&q=milk` - calls the matching
  module's `search_*` function live, returns candidates for a human to
  pick from (this is the ONLY place a live search happens outside the
  one-time new-product match - never call this automatically from a
  refresh cycle).
- `POST /api/pricing/products/{product_id}/link` - body `{store: str,
  store_item_id: str, store_name: str}`, upserts a `product_store_links`
  row (replace on the existing `UNIQUE(product_id, store)` conflict so
  re-matching an item doesn't duplicate).
- `DELETE /api/pricing/products/{product_id}/link/{store}` - removes a
  link (e.g. matched the wrong item, want to redo it).
- `GET /api/pricing/products/{product_id}` - returns every store link for
  a product plus its most recent `store_price_checks` row per store, so a
  future shopping-list page can render "Woolworths $3.40 (special),
  Coles $3.60" straight from this one call.

## New-product auto-match hook

In `app/routes/products.py`'s product-creation path, after a product is
successfully created, for each enabled store in `store_settings`: call
that store's `search_*` with the product's name, and if there's exactly
one very close name match (exact-ish string match on name, not just "top
result") auto-link it and record one `store_price_checks` row from that
same response - this is the "new product gets a price right away" case
from the brief. If there's no confident single match, leave it unlinked;
the UI (phase 2) will show it as unmatched and let a human pick from
`/api/pricing/search`. Don't block product creation on this - wrap the
whole block in a try/except that logs and continues, a pricing-site hiccup
should never stop someone adding a product to their pantry.

## Refresh script (`scripts/refresh_prices.py`, repo root)

Standalone script (same shape as `update.sh` - documented, runnable via
cron/systemd timer, not built into the live web process):

- Loads enabled stores from `store_settings`.
- For every `product_store_links` row whose store is enabled, re-fetch
  that exact `store_item_id`'s current price (via that store's search
  function, matching the stored `store_item_id` in the results - same
  "don't guess by position" rule as the Woolworths refresh note above)
  and insert one `store_price_checks` row.
- **Circuit breaker**: track consecutive failures per store within one
  run; after 3 consecutive failures (timeout, non-200, or a response that
  doesn't parse as expected) for a given store, stop making further
  requests to that store for the rest of this run and log why, instead of
  continuing to hammer it.
- **Pacing**: a short delay between requests (1-2s), not a tight loop.
- Logs a short summary at the end (products checked, prices updated,
  which stores hit the circuit breaker if any) to stdout, so it's visible
  in a cron log.
- Takes `LARDER_DATA` env var the same way the app does, uses `app.db`
  directly rather than going through HTTP - it's a local maintenance
  script, no need to round-trip through the API on localhost.

Document how to schedule it (cron line and/or a systemd timer unit
alongside the existing `larder.service` pattern) in a short new
`## Keeping store prices fresh` section - don't write the full section
yet, just leave the script's own module docstring clear; the README
section is phase 2/3's job once the UI exists to explain it against.

## Tests

Unit-test what doesn't need the network: schema/migration correctness
(same `prepare()`-against-`:memory:` pattern used elsewhere), the
`product_store_links` UNIQUE-per-store-upsert behavior, the API routes
with the actual HTTP calls mocked out (don't let the test suite make real
network calls to Woolworths/Coles/Aldi/IGA - patch the `search_*`
functions), and the circuit-breaker logic in `refresh_prices.py` with a
fake failing fetch function. A short manual note in `DONE9.md` on which
store modules you were able to verify against the live site during this
session (if sandboxed network access allows it) vs which are
implemented-but-unverified is fine and expected - say plainly which is
which.

## Rules

- Don't attempt `git commit` in this worktree - leave changes in the
  working tree.
- No backend changes outside pricing/product-creation-hook/schema
  described above.

## Verification

```
cd /home/jonesy/larder-control/store-pricing-worktree
LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q
node --check app/static/app.js
git diff --check
```

Write `./DONE9.md` (not inside `app/`) covering: what was built, which
store modules you verified live vs implemented-only, the final test
result, and anything you deliberately left unfinished (IGA's store
selection if it didn't pan out, say so plainly).
