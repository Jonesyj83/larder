# Phase 1 store pricing

Implemented the pricing schema, additive initialization and four enabled store
settings. Product links are unique per product/store; observations append to a
separate price ledger. Replacing a matched item cascades its previous ledger so
prices from a different item cannot appear under the new match.

Added authenticated store settings, candidate search, product link/unlink and
latest-price endpoints. Newly created products search enabled stores once and
only auto-link a unique name match after case, spacing and punctuation
normalization. The initial observation comes from that same response. Lookup
failures leave product creation successful; product edits do not search again.

Implemented Woolworths' cookie handshake and search POST, Coles' rendered
`__NEXT_DATA__` parsing, and best-effort Aldi category discovery/cache plus
`__NUXT_DATA__` devalue parsing. Aldi requests one relevant discovered category,
never a catalogue sweep; unrecognized categories or payloads can yield no
candidates. Navigation is cached in memory and under LARDER_DATA for 24 hours.
All HTTP uses httpx, a browser User-Agent, redirects and a 15-second timeout.

Added `scripts/refresh_prices.py`, using the local database and LARDER_DATA.
It fetches enabled linked items, selects results by stored item ID, appends an
unknown observation on a missing item or failed lookup, spaces lookups by 1.5
seconds, and stops each store after three consecutive failures. Its module
docstring includes an example nightly cron entry. No schedule was installed.
README and shopping-list UI work remain for later phases.

## Live verification and unfinished work

No store adapter was verified end-to-end against live prices this session.
Direct httpx requests to Woolworths, Coles, Aldi and IGA all failed with temporary
DNS resolution errors in the execution environment. Woolworths, Coles and Aldi
are implemented and tested against mocked HTTP payloads, but remain unverified
against current live payloads.

A separate rendered-page inspection reached Aldi's dairy category and the IGA
homepage. This confirmed category navigation and IGA's “No store selected” page,
but did not verify the adapters or expose a verified IGA store-selection
mechanism.

IGA store selection is deliberately unfinished. Its adapter logs that limitation
and returns empty failed results; configured postcodes are stored and passed
through, but no store cookie or endpoint is invented and no prices are fabricated.
The refresh circuit breaker recognizes these failures. The source includes the
requested `# not yet working` note.

## Verification

- `LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q`: **106 tests passed** (27.167 seconds).
- `node --check app/static/app.js`: passed.
- `git diff --check`: passed.
- Standalone refresh with a fresh temporary LARDER_DATA: passed; zero linked
  products, zero updates, no circuit breakers.

Tests cover migrations, settings preservation, foreign keys, link upserts and
replacement, append-only observations, latest-price selection, authenticated
routes, mocked store parsers, new-product matching/failure isolation, missing-ID
refresh behavior, pacing, disabled stores, circuit isolation and failure reset.
Existing full-app workflows now mock store search functions to avoid real store
traffic during the test suite. No templates, design or CSS were changed. No
commit was created.
