# Larder: smarter matching + shopping list polish (phase 5)

Real feedback after using the price-matching feature: a genuine bug where
a just-picked match loses its price until the next nightly refresh, the
auto-matcher is too strict to catch everyday staples, and the shopping
list display needs a cleaner layout plus a couple of missing bits of
context (already-in-stock, purchase-size clarity, a way to actually find
the price history chart).

## Hard constraints

- No AI-authorship trace anywhere.
- Match existing visual style, no new colours/design language.
- Don't break any existing pricing/matching/multi-list behavior - this
  is refinement, not a rewrite.

## Bug fix: manual match loses its price

`POST /api/pricing/products/{product_id}/link` (`app/routes/pricing.py`,
`LinkIn`) currently only saves `store`, `store_item_id`, `store_name` - it
never calls `record_price` (see `app/pricing/__init__.py`), so a
just-picked match shows "price not checked yet" until the next nightly
run, even though the picker just showed a real price seconds earlier.

Fix: extend `LinkIn` with optional `price`, `was_price`, `unit_price`,
`unit_measure`, `on_special` fields, and after `link_product()` succeeds,
call `record_price(db, link_id, data.model_dump())` (or equivalent) so a
fresh observation is recorded immediately from whatever the frontend
already has about the picked candidate. Update `pickMatch()` in
`app/templates/shopping.html` to send the full candidate object's price
fields in that POST body, not just the identity fields.

## Smarter matching

Current `auto_match()` in `app/pricing/__init__.py` only auto-links when
exactly one candidate has an **identical** normalized name - this is why
a generic item like "Pork Chops" or "Capsicum" or "Milk" won't auto-match
a store's "Pork Loin Chops" or "Red Capsicum" even though it obviously
should. Replace the exact-equality check with a scored match:

- Tokenize both the product name and each candidate's name (reuse/extend
  `normalized_name`'s tokenizing, or split into a word set).
- Score by word overlap (e.g. Jaccard-style: shared words / union of
  words), not exact string equality.
- If the product name also appears to carry a brand (heuristic: the
  first word of the product's own name matches a `brand` value seen
  among the candidates - e.g. "Vegemite" product name and a candidate
  with `brand == "Vegemite"`), require that brand to match too before
  counting a candidate as strong - a household's branded staple ("Vegemite")
  shouldn't silently swap to a home-brand spread, but a generic
  commodity ("Pork Chops", "Capsicum", "Milk", with no such brand
  signal) should match on word overlap alone.
- Auto-link only the single strongest candidate per store when its score
  clears a reasonable bar (e.g. >= 0.5 overlap) and no other candidate is
  close behind it (e.g. the next-best candidate's score is clearly
  lower) - still cautious, just not requiring a perfect string match.
- This same scoring function should back a new bulk endpoint (below), not
  just the new-product-creation hook.

## Bulk auto-match

`POST /api/pricing/products/auto-match` - runs the above matching for
every product that has at least one enabled store without an existing
link for that store (i.e. everything still missing a match), across all
currently-enabled stores. Returns something like
`{"matched": [{"product_id", "name", "store"}], "unmatched": [{"product_id", "name"}]}`
so the caller can show what worked and what still needs a manual pick.
Pace requests the same way the refresh script does (don't hammer every
store back-to-back with no delay) and keep going past one product's
failure rather than aborting the whole batch.

Add an "Auto-match all" button somewhere sensible on the shopping list
page (near the list switcher, or above the item list) that calls this and
shows a short result summary (e.g. "Matched 6, couldn't match 2: Chilli
Flakes, Blue Cheese" - a toast or small inline summary, whatever reads
cleanest, not a blocking alert).

## Shopping list layout: price display

Replace the current stacked small-text price lines (`app/templates/shopping.html`,
the `priceLinks(i)` loop) with a tidy inline row of compact price chips -
store name + price together as one small chip/pill per store, the
cheapest one visually distinguished (your existing `.pill` treatment is
fine, just apply it per-chip rather than as a lone highlighted line among
plain ones), a small "special" marker on a chip that's currently on
special, and "Change match"/"Match this item" staying as compact actions
rather than full-width buttons. This needs to still read clearly with
three or four store chips sitting next to a product name on one row,
wrapping sensibly on a narrower screen rather than breaking the layout.

Add a small icon/text link next to a matched item (something like a tiny
"price history" link) that goes to `/products?id={product_id}` - that
route already opens the product edit modal directly (see `products.html`'s
existing `?id=` handling) where the price history chart lives. This is
the simplest way to surface it from the shopping list without duplicating
the chart.

## Stock-awareness on shopping list items

For an item with a `product_id`, if the product's current `in_stock`
(use the same stock-sum logic as `app/stock.py`'s `in_stock()`, converted
to the item's listed unit the same way `app/routes/shopping.py`'s
`listed_amount()` already converts units) already covers the amount
needed, show a small note like "already have some in stock" next to the
item (exact wording your call, keep it low-key, not a warning). Don't
auto-remove or auto-tick the item - just surface the fact so a decision
can be made.

## Purchase-size clarity for small recipe-derived amounts

When a shopping-list item's amount is in the product's plain stock unit
(e.g. "60 mL") but the product has a `purchase_unit_id` different from
its stock unit (e.g. sold as a 2 L bottle, `purchase_factor` capturing
the conversion), show that alongside the raw amount - e.g. "60 mL needed
(sold as 2 L)" - so it's clear a recipe's small requirement doesn't mean
buying exactly that much; you're buying whatever unit the store actually
sells. Only show this when the purchase unit genuinely differs from the
stock unit and would otherwise look like a strange thing to shop for.

## Tests

Extend `tests/test_pricing.py` and `tests/test_shopping_prices.py`
covering: the link endpoint now recording an initial price observation,
the relaxed scored-matching function (a case that should match despite
differing names, a branded case that should NOT cross-match to a
different brand, a close-call case that should stay unmatched), the bulk
auto-match endpoint's matched/unmatched shape, and the shopping page's
stock-awareness note + purchase-unit clarity text under realistic mocked
data.

## Rules

- Don't `git commit` - leave the worktree dirty for review.

## Verification

```
cd /home/jonesy/larder/larder-control/pricing-polish-worktree
LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q
node --check app/static/app.js
git diff --check
```

Write `./DONE13.md` with what was built, the test result, and anything
left unfinished or any judgment calls made (especially on the matching
score threshold - say what you picked and why).
