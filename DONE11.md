# Multiple shopping lists

Implemented household shopping lists with open-item counts, creation, rename, and deletion. Deleting a list moves its items to the first remaining list, preserving item IDs and source records. The last list cannot be deleted.

Added a guarded, additive migration that creates a default Shopping list and backfills existing items in the startup transaction. Existing tables retain their rows and structure, with insert/update guards enforcing non-null list IDs after backfill; fresh tables declare the column NOT NULL. Foreign keys and an index support list ownership.

All shopping item endpoints accept a list_id query parameter and default to the first list when omitted. Product merging, listed amounts, and low-stock additions operate within a list. Existing recipe and meal-plan callers continue using the default list.

The shopping page now has a list selector, new/rename/delete actions, and localStorage selection memory. Item actions use the selected list. Existing pricing controls and app/pricing/ are unchanged.

Verification:

- Full unittest discovery: 111 tests passed.
- node --check app/static/app.js: passed.
- git diff --check: passed.
- Migration sanity check on a copied old-schema database: two items (open and completed) and their source record survived two initialization runs; zero null list IDs and no foreign-key violations. No sample database was present in the repository, so a populated legacy database was constructed and copied for this check.
- Added coverage for list management, last-list protection, reassignment, item isolation, backward compatibility, HTTP query scoping, migration preservation, selection persistence, and existing pricing behavior.

Nothing left unfinished. Changes are uncommitted for review.
