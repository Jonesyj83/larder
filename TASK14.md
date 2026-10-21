# Larder: rebuild the shopping list row + fix cross-store matching (phase 6)

Direct, blunt feedback: the shopping list currently looks like stacked
boxes with nothing lining up, "Auto-match all" barely seemed to do
anything, a manually-matched item still showed "price not checked yet",
and clicking "price history" dumps you on an edit page instead of just
showing the graph. This phase is a real rebuild of the row, not another
tweak pass.

## Root causes already diagnosed - fix these exactly, don't re-diagnose

**Why auto-match only ever found Woolworths matches:** most of this
household's pantry items are named after the exact home-brand product
bought, e.g. the Larder product itself is literally named "Woolworths
Chilli Ground". When matching that name's words against a DIFFERENT
store's candidates (say Coles), the word "woolworths" never appears in
any Coles product name, so it sits in the Jaccard union as permanent dead
weight, dragging every cross-store score down below the 0.5 threshold
even when the actual product (ground chilli) matches fine. This is not a
brand-lock bug (`required_brand` correctly stays `None` here since no
Coles candidate has brand "Woolworths") - it's noise in the word set.

Fix in `app/pricing/__init__.py`'s `name_words()` (or `strongest_match`,
whichever is cleaner): strip a small known set of supermarket-own-brand
tokens - `woolworths`, `coles`, `aldi`, `iga` - out of the word set used
for *scoring* only (never touch what's stored or displayed), case-
insensitively, wherever they appear in the name. This lets "Woolworths
Chilli Ground" score correctly against Coles' or Aldi's own "Chilli
Ground"-type product, while a genuine third-party brand (Vegemite,
Devondale, Dolmio, etc.) is untouched and still brand-locks exactly as
before.

**Why a manually-matched item can still show "not checked yet":** that
specific case (a Woolworths Tomato Paste link with zero rows in
`store_price_checks`) predates the fix that made `/link` call
`record_price` immediately - it's stale data from before that fix shipped,
not a new bug. Nothing to change in code for this one; it'll be swept up
once prices are backfilled (see below, done separately after this build).

## Hard constraints

- No AI-authorship trace anywhere.
- No real store logo images - there's no clean, legitimate source for
  Woolworths/Coles/Aldi/IGA trademarked logos. Use clean text/colour
  badges instead (the existing per-store colour already used in the price
  history chart is a reasonable base). Say so plainly in `DONE14.md`,
  don't silently skip it.
- Match the app's existing visual language (CSS custom properties,
  spacing scale) - the goal is this row looking like it was designed on
  purpose, lining up cleanly across every item regardless of how many
  stores are matched or whether a chart has data.

## Remove entirely

- The "Match this item" button and its inline search/pick form.
- The "Auto-match all" button and its button-triggered bulk call.
- The per-item "origin not recorded" / source-history text block
  (`Manual addition` / `Meal plan` / `Recipe` / `Low stock` labels and
  the underlying `sources` fetch/render in `app/templates/shopping.html`
  if it's only used for that text - leave the backend `shopping_sources`
  table and API alone, this is a display-only removal).

## Auto-match on add, not as a manual bulk action

When an item is added to a shopping list (`POST /api/shopping` in
`app/routes/shopping.py`) and it has a `product_id` with at least one
enabled store missing a link, run `pricing.auto_match()` for just that
one product (not the whole household's product list - that's what made
the bulk button feel like it hung) before responding, or immediately
after via whatever approach keeps the add from feeling slow - a single
product against up to four stores is a handful of requests, this should
stay fast. Wrap it so a pricing hiccup never blocks adding the item
itself (same defensive pattern as the existing new-product-creation
hook).

## Row layout rebuild (`app/templates/shopping.html`)

Rebuild the per-item row as a real CSS grid (not stacked block elements)
with consistent columns so every row lines up regardless of content:

- Checkbox + thumbnail (existing).
- **Product name** - the actual product's name, never the raw
  recipe-requested text. If an item isn't linked to a real product yet
  (the "not a product" case already handled elsewhere), that's unchanged.
- **Store price badges** - one compact badge per *enabled* store the
  product is matched to, each showing the store name, its current price,
  and the item's pack size (e.g. "Woolworths $4.50 · 2L"), cheapest
  visually distinguished. A store with no match for this product gets a
  small, quiet placeholder (muted dash or similarly low-key, not a
  button, not empty space that breaks row alignment) rather than being
  omitted - the row's shape should stay predictable.
- **A small inline price-history chart** - reuse the existing hand-rolled
  SVG chart logic from `app/templates/products.html`
  (`buildHistoryChart`/`drawHistory`, both already pure functions with no
  `this` dependency - lift them into `app/static/app.js` as shared global
  functions and have both `products.html` and `shopping.html` call them,
  rather than duplicating the code). Keep it small enough to sit cleanly
  in a row - this doesn't need to be as large as the one in the product
  edit modal. If there's no price history at all for an item, leave this
  slot visually quiet (not empty/broken-looking) rather than erroring.
- **Quantity** - a plain number input bound to the item's amount, no
  unit-conversion explanation text next to it (the existing "sold as 2 L"
  clarity note from the last phase can stay as a small tooltip/title
  attribute if that's a clean way to keep the information without
  cluttering the row, otherwise it's fine to drop it here - the row needs
  to breathe).
- **Got it** / **Remove** buttons, right-aligned.

This needs actual visual care, not just "it compiles" - a consistent
grid-template-columns (or equivalent) that holds its shape with zero,
two, or four store badges present, a chart with or without data, and a
product name of varying length, without boxes drifting out of alignment
with each other.

## Tests

Update the existing shopping-prices test coverage for: the removed
match/auto-match-all UI paths, the add-time single-product auto-match
trigger, the shared chart functions now living in `app.js` (if this
repo's test harness for Alpine logic can exercise `app.js` directly,
otherwise cover it at the page-logic level), and the corrected
`name_words`/`strongest_match` behavior (a case proving a store-prefixed
product name like "Woolworths Chilli Ground" can now match a plain
"Chilli Ground"-type candidate at a different store, while a genuine
third-party brand like "Devondale Milk" still won't cross-match to a
different brand).

## Rules

- Don't `git commit` - leave the worktree dirty for review.

## Verification

```
cd /home/jonesy/larder/larder-control/shopping-rebuild-worktree
LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q
node --check app/static/app.js
git diff --check
```

Write `./DONE14.md` with what was built, the test result, the logo
limitation note, and anything left unfinished.
