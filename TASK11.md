# Larder: multiple shopping lists (phase 3)

Right now there's exactly one shopping list, backed by a single
`shopping_list` table with no list concept at all. This phase adds real
multiple lists (Work/Home/Picnic was the example given) while keeping
every existing list behavior working exactly as it does now.

## Hard constraints

- No AI-authorship trace anywhere.
- Additive-only migration - every row in the current `shopping_list`
  table must end up attached to a real list after migration, nothing
  silently orphaned or dropped.
- Don't touch `app/pricing/` or the price-matching UI added in the
  previous phase - this just needs to keep working once items live under
  a list_id instead of globally.
- Match existing visual style, no new colours/design language.

## Schema (`app/db.py`)

```sql
CREATE TABLE IF NOT EXISTS shopping_lists (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
```

Add `list_id INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE
CASCADE` to `shopping_list` via the usual additive `ALTER TABLE` +
`PRAGMA table_info` guard pattern already used throughout this file.
SQLite's `ALTER TABLE ADD COLUMN` can't add a `NOT NULL` column without a
default when the table already has rows, so: add it nullable first (or
with a temporary default), then in the same init path backfill every
existing row to a default list (create one row in `shopping_lists` named
"Shopping" if it doesn't already exist, point every pre-existing
`shopping_list` row with a null `list_id` at it). Verify this leaves zero
null `list_id` rows afterward. Follow whatever exact mechanism this
codebase's existing migrations already use for an additive NOT NULL
backfill (check how `receipt_id` or similar was done) rather than
inventing a new approach.

Seed one default list ("Shopping") on fresh init too, so a brand new
install always has at least one list to land items on.

## API (`app/routes/shopping.py`)

- `GET /api/shopping/lists` - all lists with an item count (open items,
  not done) per list.
- `POST /api/shopping/lists` - body `{name: str}`, creates a list.
- `PUT /api/shopping/lists/{id}` - rename.
- `DELETE /api/shopping/lists/{id}` - only if there's more than one list
  left (don't allow deleting the last list - there must always be
  somewhere for an item to land); reassign or block, your call, but don't
  leave the household with zero lists.
- Every existing item endpoint (`GET /api/shopping`, `POST`, `PUT`,
  `/add-missing`, `/make-product`, `/bought`, `/done`) needs a `list_id`
  query param or body field (whichever fits the existing pattern per
  endpoint) so items are scoped to a list. Default to the household's
  first/default list when a `list_id` isn't supplied, so nothing that
  currently calls these endpoints without a list concept breaks.
- `add_item`/`listed_amount` in `app/routes/shopping.py` need a list_id
  parameter threaded through - check call sites (`add-missing`,
  `make_product`, recipe/meal-plan code that calls `add_item` if any -
  search for `add_item(` across the repo) and give those a sensible
  default list rather than breaking them.

## Frontend (`app/templates/shopping.html`)

- A simple list switcher at the top of the page - tabs or a dropdown,
  whichever reads cleaner with 2-4 lists, matching existing nav patterns
  in this app rather than inventing a new widget style. Remember the last
  selected list in `localStorage` so reloading the page doesn't always
  dump you back on the default.
- "New list" action (name prompt, same style as other create-flows in
  this app, e.g. the recipe/product forms), rename, and delete (with the
  existing "can't delete the last list" rule surfaced as a real error,
  not a silent failure).
- Everything else on the page (add item, match-to-store-price, got it,
  make product, etc.) now operates against whichever list is currently
  selected.
- "Add what's running low" should add into the currently selected list.

## Tests

Extend the existing shopping test file(s) covering: migration backfill
(an old-shape DB ends up with every item on exactly one default list, zero
nulls), creating/renaming/deleting lists, the last-list-can't-be-deleted
rule, items scoping correctly to their list, and that omitting list_id on
an existing call still works against the default list (backward
compatibility for anything hitting the API without knowing about lists).

## Rules

- Don't `git commit` - leave the worktree dirty for review.

## Verification

```
cd /home/jonesy/larder-control/multi-lists-worktree
LARDER_DATA=$(mktemp -d) PYTHONPATH="$PWD" ./.venv/bin/python -m unittest discover -s tests -q
node --check app/static/app.js
git diff --check
```

Also manually sanity-check the migration against a *copy* of a database
with existing shopping items if a sample is available in the repo/tests,
so backfill correctness isn't just asserted by a fresh-DB test.

Write `./DONE11.md` with what was built, the test result, and anything
left unfinished.
