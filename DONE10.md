# Shopping list prices and store settings

Built:
- Added household store toggles for Woolworths, Coles, Aldi and IGA to Settings for all users, using the existing store settings API. IGA's postcode field is disabled while the store is off.
- Added parallel pricing lookups after shopping list rendering, skipping entries without a product. Enabled stores show their latest prices, cheapest-price emphasis, special indicators and an explicit message for unchecked prices.
- Added inline store search and candidate selection for unmatched products, plus unlinking and rematching for incorrect matches. Disabled stores are excluded from prices and matching choices.
- Added lookup error messages and retry actions without blocking the shopping list.
- Added Node-based page logic tests covering pricing calls, linked and unmatched products, disabled stores, matching, rematching, missing checks, failure isolation and settings updates.

Verification:
- `LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q`: passed, 108 tests.
- `node --check app/static/app.js`: passed.
- `git diff --check`: passed.

Nothing left unfinished. Existing pricing internals and shopping actions were retained. No commit was created.
