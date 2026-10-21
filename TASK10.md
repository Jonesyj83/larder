# Larder: shopping list prices + store settings (phase 2)

Phase 1 (merged, `app/pricing/`) built the store search/match modules, the
`product_store_links` / `store_price_checks` tables, and the pricing API.
This phase wires that into the two pages a household actually looks at.
Multiple shopping lists (Work/Home/Picnic) are a separate, later phase -
don't build that here, this phase keeps the single existing shopping list
and just adds prices + matching to it.

## Hard constraints

- No AI-authorship trace anywhere.
- Don't touch `app/pricing/` internals - call the existing API endpoints
  from the frontend (`/api/pricing/...`, see `app/routes/pricing.py`) or
  call the python functions directly from a route handler if that's
  cleaner for a new backend endpoint; don't re-implement the matching
  logic.
- Keep existing shopping-list behavior (add/toggle/bought/make-product/
  remove/add-missing) exactly as it works now - this is additive only.
- Match the existing visual style (`style.css` custom properties, `.pill`,
  `.card`, `.small`, `.muted` classes already used throughout) - no new
  colours, no new design language, this should look like it always
  belonged.

## Settings page: store toggles

Add a new section to `app/templates/settings.html` (visible to any user,
not just admin - same visibility as "Food preferences"), something like:

```
<details class="space-top">
  <summary>Store prices</summary>
  <div class="card">
    <p class="muted small">Turn a store off if you don't shop there - it stops
    being searched or shown anywhere.</p>
    <!-- one row per store: woolworths, coles, aldi, iga -->
    <!-- toggle + for iga only, a postcode field (disabled/greyed until iga is on) -->
  </div>
</details>
```

Backed by the existing `GET /api/pricing/stores` and
`PUT /api/pricing/stores/{store}` endpoints. Store names should display
properly capitalized (Woolworths, Coles, Aldi, IGA) even though the API
uses lowercase keys.

## Shopping list: prices per item

For each shopping-list row that has a `product_id` (skip the "not a
product yet" rows entirely - they can't be matched):

- Call `GET /api/pricing/products/{product_id}` (already returns every
  store link plus its latest `store_price_checks` row - see phase 1's
  `app/routes/pricing.py`) when the list loads.
- If the product has at least one link, show a compact price row under
  the item: each linked, enabled store's name and latest price, cheapest
  one visually emphasized (e.g. bold or a `.pill` like the existing
  "not a product" pill, not a loud color), on-special items get a small
  "special" indicator. If a store has no check yet (circuit breaker /
  never refreshed), show "price not checked yet" rather than nothing or
  a zero.
- If the product has **no** links at all for currently-enabled stores,
  show a small "Match this item" action instead of a price row.
- "Match this item" opens a small inline/modal picker: pick a store (only
  enabled ones), type a search term (prefilled with the product name),
  call `GET /api/pricing/search?store=...&q=...`, show the returned
  candidates (name, brand, size, price) as a clickable list, and on pick
  call `POST /api/pricing/products/{product_id}/link` with that
  candidate's `store_item_id`/`name`. Let someone redo a match (call the
  existing unlink endpoint, then match again) if they picked wrong.
- None of this should block the list from loading or make the whole page
  feel slow - fetch each product's pricing data after the list itself has
  rendered (e.g. `Promise.all` per visible item, not sequentially), and
  don't fail the whole page if one pricing lookup errors.

## Tests

Extend `tests/test_pages.py` or add `tests/test_shopping_prices.py` -
whichever fits this codebase's existing pattern best - covering: the
settings page renders the store toggle section, the pricing endpoints get
called from the shopping page's JS logic (mock `api()` same as existing
shopping/memory tests do), and a product with no links shows the match
action while a product with a link shows a price. Keep using the existing
JS-logic-under-node-via-jsdom-or-similar test style already present in
this repo for `app.js` - look at how `tests/test_memory_navigation.py` or
similar already tests Alpine page logic before inventing a new pattern.

## Rules

- Don't `git commit` - leave the worktree dirty for review.
- Don't restructure routes/products.py or app/pricing - additive frontend
  + settings wiring only, plus whatever small new endpoint is genuinely
  needed (if any) in `app/routes/pricing.py`.

## Verification

```
cd /home/jonesy/larder-control/shopping-prices-worktree
LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q
node --check app/static/app.js
git diff --check
```

Write `./DONE10.md` with what was built, the test result, and anything
left unfinished.
