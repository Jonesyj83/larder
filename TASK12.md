# Larder: price history graph + fake-discount flag (phase 4, final)

Last piece from the original brainstorm: a small line graph on a
product's price history so a rising/falling trend is visible, and a flag
when a "special" isn't actually a real discount (price was quietly raised
beforehand, or the "special" price is just the normal everyday price).

## Hard constraints

- No AI-authorship trace anywhere.
- No new frontend dependency - hand-roll a small inline SVG line chart,
  don't pull in a charting library. This needs to stay a simple personal
  app, not grow a build step.
- Don't touch `app/pricing/` internals or the shopping list matching UI -
  additive only.
- Match existing visual style/CSS variables, no new colours.

## What data already exists

- `GET /api/stock/price-history/{product_id}` (already in
  `app/routes/stock.py`) - every real purchase of this product, from
  `stock_log` where `kind='purchase'`, each row has `price` and `at`.
  This is money actually spent, at whatever store it was bought at
  (no store attribution currently, just a date + price).
- `store_price_checks` (added in an earlier phase) - every nightly check
  of a matched store item, with `price`, `was_price`, `on_special` and
  `checked_at`. This is what the store currently says the price is,
  whether or not anything was bought.

These are two different kinds of history (what you paid vs what a store
currently shows) and both matter - the first is ground truth for your own
spending, the second is what catches a store quietly moving prices around
specials.

## New endpoint

`GET /api/pricing/products/{product_id}/history` in
`app/routes/pricing.py` - for every store link on this product, return
its full `store_price_checks` history (ordered by `checked_at`), plus a
`typical_price` computed as the median of that store's non-null prices
over the stored history (or over the last 90 days if there's a lot of
history - use whatever window keeps this cheap, this is a personal
household DB, not optimizing for scale). Also include this product's
`stock_log` purchase history (reuse the existing query/logic from
`app/routes/stock.py` rather than duplicating it) so the frontend can
plot both series.

A check is a "fake special" when `on_special` is true but its `price` is
within a small tolerance (e.g. 5%, or exactly equal) of that store's
`typical_price` - i.e. the "special" price is basically the normal price,
not a real saving. Flag each check with this boolean rather than making
the frontend recompute it.

## Frontend

In the product edit modal (`app/templates/products.html`, inside
`<template x-if="form.id">` since this only makes sense for an existing
product), add a "Price history" section:

- A small inline SVG line chart - plot each enabled store's price-check
  series as its own line/colour, plus your own purchase price points as
  markers (different visual treatment, e.g. small dots, since purchases
  are sparse events not a continuous line). Use the app's existing CSS
  custom properties for colour rather than inventing a new palette - if
  more distinct colours than the existing variables are genuinely needed
  for multiple store lines, derive subtle variations rather than bright
  new hues that clash with the rest of the design.
- Hand-roll this as a `<svg>` with `<polyline>`/`<circle>` elements built
  from the fetched data in JS - straightforward enough not to need a
  library at this data volume (a household's price history, not a stock
  market feed).
- If a check is flagged as a fake special, show a small, plainly-worded
  note near that point (e.g. "not really cheaper than usual" as a tiny
  label or tooltip on hover) rather than a loud warning banner - this
  should read as a quietly useful detail, not a dramatic alert.
- If there's no history at all for a product (nothing bought, nothing
  checked yet), just don't show the section - no empty chart, no
  "no data" placeholder clutter.
- Fetch this lazily only when the edit modal opens for an existing
  product (not for every product in the list), same performance
  discipline as the shopping list's per-item pricing lookups in the
  earlier phase.

## Tests

Extend the pricing test file with: the new endpoint's shape (store
history + purchase history + computed `typical_price` + `on_special`
fake-discount flag under realistic mocked data, including a case that
should and shouldn't be flagged), and a products-page JS-logic test
(matching this repo's existing test-the-Alpine-logic-object pattern)
confirming the history section loads lazily on modal open and doesn't
render anything when there's no history.

## Rules

- Don't `git commit` - leave the worktree dirty for review.

## Verification

```
cd /home/jonesy/larder-control/price-history-worktree
LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q
node --check app/static/app.js
git diff --check
```

Write `./DONE12.md` with what was built, the test result, and anything
left unfinished. This is the last phase of this feature set - if
anything about the fake-discount tolerance or chart approach needed a
judgment call, say what you picked and why.
