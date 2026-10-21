# Task 13 completed

Manual matches now save an immediate price observation in the same transaction as the link. The picker sends the candidate's price, previous price, unit pricing and special status; optional missing prices remain unknown, and invalid numeric prices are rejected.

Creation and bulk matching share a word-overlap scorer. It normalizes case, Unicode and size spacing, deduplicates store item IDs, requires a Jaccard score of at least 0.5 and a lead of at least 0.15 over the runner-up. These thresholds allow straightforward staples such as Pork Chops / Pork Loin Chops and Capsicum / Red Capsicum while leaving ties and close calls for manual selection. When the product's first word identifies a candidate brand, only that brand is eligible.

The authenticated bulk endpoint fills missing links across enabled stores, preserves existing matches, records initial prices, waits 1.5 seconds between lookups and continues after failed searches. Results list each new store match and every product still missing an enabled-store match; a partially matched product can appear in both lists. The shopping page has an Auto-match all button with a nonblocking result summary.

Shopping prices are compact wrapping chips with the cheapest highlighted, special markers, compact match actions and a direct price-history link. Items show a quiet stock note when summed stock covers the listed need after unit conversion. Smaller amounts in the stock unit show their purchase size using existing quantity formatting, such as 60 ml needed (sold as 2 l). This note is limited to amounts smaller than one purchase unit with a different purchase unit ID. Items remain on the list with their existing tick state.

Verification:

- Full unittest discovery: 119 tests passed.
- `node --check app/static/app.js`: passed.
- `git diff --check`: passed.

Regression coverage includes initial manual prices, invalid prices, relaxed names, brand protection, close calls, bulk results/pacing/failure recovery, stock sums and unit conversion, purchase-size text, and the bulk page action.

Nothing left unfinished. Changes are uncommitted for review.
