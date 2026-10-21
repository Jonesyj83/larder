# Price history and special-price context

Built `GET /api/pricing/products/{product_id}/history` with ordered checks for every linked store, enabled status, per-store median `typical_price`, and a `fake_special` boolean on each check. Purchase history uses the same shared query as the existing stock endpoint. Missing products return 404.

The existing-product edit modal now fetches history only when opened. A small inline SVG plots enabled store prices as lines and purchases as separate dots, using existing CSS colour variables. Point titles show dates and prices; hollow special-price points include “not really cheaper than usual”. The section stays absent when there are no plottable prices. Late responses from an earlier edit cannot populate another product's chart. No frontend dependency or build step was added; pricing internals and the shopping matching UI were left unchanged.

Judgment calls:

- Use the median of all stored non-null checks for each store. This keeps the household-sized history straightforward and limits the effect of occasional unusually high or low prices.
- Flag specials within 5% of that median, inclusive, with a small floating-point allowance. Null prices are never flagged; a zero median only matches a zero price. The advertised `was_price` does not establish the baseline.
- Use linear time and price axes, centering a single date or flat price series. Purchase markers explicitly represent the amount paid because a purchase may contain multiple items, while store lines represent shelf prices.
- Construct SVG elements directly with the SVG namespace and set tooltip text through `textContent`.

Verification:

- Full unittest discovery: 113 tests passed.
- `node --check app/static/app.js`: passed.
- `git diff --check`: passed.
- Added endpoint coverage for ordering, median, missing/empty history, purchases, disabled stores, null prices, true discounts and the 5% boundary. Added Alpine-object logic coverage for lazy loading, empty-chart suppression, plotting, SVG rendering, and stale responses.

Nothing left unfinished. Changes are uncommitted for review.
