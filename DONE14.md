# Task 14 completed

- Removed Woolworths, Coles, Aldi and IGA tokens from scoring words, case-insensitively wherever they occur. Stored/displayed names and third-party brand guards remain intact.
- Shopping additions now auto-match only the added product when enabled stores lack links. The item is committed first; pricing failures cannot undo the addition. Existing links are preserved.
- Removed shopping match/search/change-match controls, the bulk matching button and its frontend call, and source-history text. The source table and API remain intact.
- Rebuilt shopping rows with shared CSS grid columns for checkbox/thumbnail, actual product name, store badges, inline history, editable quantity and right-aligned actions. Enabled unmatched stores retain muted placeholders; cheapest prices are highlighted. Narrow layouts use consistent grid rows. Purchase-size context is confined to the quantity tooltip.
- Moved the existing SVG history helpers into app.js for both shopping and product editing. Empty histories and failed history requests have quiet fallback text.
- Retained search-result pack sizes on store links, including an idempotent migration for existing databases. Legacy links can use sizes present in their store names. Updated asset versions.
- Used text and colour store badges. No real store logo images were used because there is no clean, legitimate source for the trademarked logos.

Verification:

- Required unittest discovery: **120 tests passed** (33.048 seconds), including cross-store scoring, real brand locking, add-time matching and failure recovery, quantity persistence, shared chart rendering and pack-size migration/preservation.
- `node --check app/static/app.js`: passed.
- `git diff --check`: passed.

Browser screenshot verification could not run: both installed Chrome and Chromium headless shell were blocked at startup by sandbox restrictions. Responsive styles were reviewed, but rendered visual verification remains outstanding.

Stale price-check data was not backfilled; that remains the separate follow-up specified in TASK14.md. No commits were created.
