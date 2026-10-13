# Changelog

Plain-English notes on what's changed, newest first. No formal version numbers yet -
just what moved and when, so anyone updating their install knows what to expect.

## 2026-10-14

- Prices can be entered when buying stock or scanning a new item, and shown back to
  you afterward if you turn it on in Settings (off by default).

## 2026-10-12

- Scanning a new barcode now sizes the product from its real pack size (from
  OpenFoodFacts) instead of always defaulting to a generic "piece".
- Fixed pantry items that had been imported with a generic "piece" unit instead of
  their real one (tins, bottles), and made it clearer when a suggested amount can't
  be compared to what's in stock.
- Suggested meals can go straight into the meal plan as a real entry, instead of
  only showing as text you had to copy in by hand.
- README rewrite explaining why this exists next to Grocy, fixed broken API examples,
  added a license and a roadmap of what's planned.
- Full pass through the app fixing stock-math edge cases, unit handling, and blank
  screens where there should have been a plain message instead.
- Meal ideas can now cover any date range with a day-by-meal tick grid, not just a
  fixed week.
- Household food preferences (diet, dislikes, notes) are shared across the house and
  sent with every suggestion from here on.
- Suggestions taught not to treat frozen or crumbed food like it needs boiling, and
  to use a ready-made spice mix instead of piling raw spices on top of it.
- Stock page redesigned: a flat list with colour-coded dates, with the full
  overdue/use-by breakdown moved to its own page.
- Fixed a crash when comparing a suggested recipe's ingredients against stock.
- A suggested recipe now shows what you have and what's missing, with a button to
  add the missing bits to the shopping list.
- Suggestions use real dish names ("chilli con carne") instead of making one up.
- The "Test it" button on a provider connection now checks the key you just typed,
  not only a saved one.
- Suggestions moved to a plain, direct connection to whichever provider you choose -
  locked down logins, writes, uploads and provider URLs along the way.

## 2026-10-11

First working version: stock, locations, products, barcode scanning, shopping list,
recipes, meal plan, and optional AI-assisted meal/recipe suggestions behind your own
provider key. Charcoal-and-amber theme, works fine behind a reverse proxy path prefix.
