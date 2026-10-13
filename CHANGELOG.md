# Changelog

What's changed, newest first. Not bothering with version numbers yet, just dates.

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
