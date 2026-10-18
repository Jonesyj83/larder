# Changelog

What's changed, newest first. Not bothering with version numbers yet, just dates.

## 2026-10-19

- Added `update.sh` - backs up the database, pulls the latest code, restarts.
  One command to update instead of doing it by hand.

## 2026-10-18

- Cooking a recipe can now ask how many portions are left over and where
  they're going (fridge or freezer). Each batch gets a short code (B1, B2...)
  to write on the container.
- Stock page shows what's stored and waiting to be eaten, with a button to
  use a portion or take it off the list when it's gone.
- Meal plan can pull a stored meal straight into a day instead of cooking
  fresh - no stock gets taken twice, that already happened when it was cooked.
- Diet options now include gastric sleeve, gastric bypass, high protein and
  low sugar, spelled out in real cooking terms (small portions, protein
  first, no fizzy drinks) rather than just handed to the model as a label.

## 2026-10-15

- Recipes page is a card grid now instead of a flat list, with a real filter
  (all / can make / missing a few) instead of one checkbox.
- You can rate a recipe now, 1 to 5 stars, so you can look back and know which
  ones are actually worth making again.
- Running low on something now shows up in the same summary line at the top of
  Stock as the going-off-soon/past-date counts, as long as you've set a "keep
  at least" amount on the product.

## 2026-10-14

- Prices are optional now when you buy or scan something in, and you can turn on
  showing them back in Settings (off by default).

## 2026-10-12

- Scanning a new barcode sizes the product off its real pack size now (pulled from
  OpenFoodFacts) instead of just defaulting everything to "piece".
- Fixed a bunch of pantry items that got imported as a generic "piece" instead of
  their real unit (tins, bottles), and made it clearer when a suggested amount
  can't be checked against what's in stock.
- A suggested meal can go straight into the meal plan now, not just show up as
  text you have to type in yourself.
- Rewrote the README - why this exists next to Grocy, fixed the broken API
  examples, added a license and a rough roadmap.
- Went through the whole app fixing stock-math edge cases, unit handling, and
  spots that just went blank instead of saying something useful.
- Meal ideas can cover any date range with a day-by-meal grid now, not stuck to
  one week.
- Food preferences (diet, dislikes, notes) are shared across the house and get
  sent with every suggestion.
- Suggestions know not to tell you to boil frozen crumbed chicken, and to use a
  ready-made spice mix instead of piling raw spices on top of it.
- Redesigned the stock page - flat list, colour-coded dates, moved the overdue/
  use-by breakdown to its own page.
- Fixed a crash comparing a suggested recipe against stock.
- A suggested recipe shows what you have and what's missing now, with a button
  to add the missing stuff to the shopping list.
- Suggestions use real dish names ("chilli con carne") instead of making one up.
- "Test it" on a provider connection checks the key you just typed, not only a
  saved one.
- Suggestions are a plain, direct connection to whatever provider you pick now.
  Locked down logins, writes, uploads and provider URLs while I was in there.

## 2026-10-11

First working version. Stock, locations, products, barcode scanning, shopping
list, recipes, meal plan, optional AI suggestions if you bring your own key.
Charcoal and amber theme, works fine behind a reverse proxy.
