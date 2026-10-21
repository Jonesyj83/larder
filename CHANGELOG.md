# Changelog

What's changed, newest first. Not bothering with version numbers yet, just dates.

## 2026-10-21

- Added a receipts table. A receipt now has its own total recorded, separate
  from the sum of its line items - a whole-receipt discount or a combo deal
  doesn't force the two numbers to match. `purchase` can tag a stock entry
  with the receipt it came from; deleting a receipt leaves the purchase
  history exactly where it was.

- Rough edges knocked off the whole look - softer depth, a real type scale,
  numbers that actually stand out when they matter, nicer buttons and
  tables. Dialled the biggest numbers back down after the first pass ran
  a bit large.
- Moved the whole app's navigation into a sidebar, then settled on a plain
  top bar instead - the sidebar looked flashier but a horizontal bar suits
  this app better day to day. Memory's eight categories are a dropdown off
  the top bar now.
- Took the Overview page back out. It looked nice but added a whole extra
  page between opening the app and seeing what's actually in the kitchen.
  Stock is the front page again, with its own quiet "N past date" line at
  the top doing the same job Overview was trying to do.
- Memory can open one category at a time from the nav instead of showing
  every category section together. Search still covers all notes.
- Straightened out a batch of shopping list rows that were showing
  "undefined" instead of where they came from - leftover labelling from
  the old suggestion feature.

- Taken out the built-in meal-idea and recipe-suggestion feature (the provider
  connections in Settings, the "Meal ideas" and "Suggest a recipe" buttons) -
  planning is better done by an outside agent talking to the app directly now.
- Added a Memory page: likes, dislikes, maybes, conditions, habits, buying
  patterns, recipe notes and cost notes, searchable, grouped and collapsed so
  it doesn't turn into a wall of text as it fills up.
- Added a "Work" location by default, for things kept away from home.
- Purchase prices now stick around in the stock log after the stock itself is
  used up, so price history and a spending summary are both possible per
  product.
- Added a small standalone MCP server (`mcp_server/`) so an outside agent can
  read and write stock, products, recipes, the meal plan, shopping list and
  the new memory notes through the same API keys already used for scripts.

## 2026-10-20

- Meal ideas now plans cook events, not one meal per slot - cook a proper batch once,
  eat some fresh, and the rest gets spread across other days in the week (same dish again,
  or with a twist, like chilli con carne turning into chilli jaffles a few nights later).
  One "Add to plan" per cook event fills in every slot it covers at once.
- Water and ice don't get flagged as missing or "can't compare" any more - obviously always
  on hand.
- Fixed a chunk of sauces, spices, oil and milk that were still stocked as generic bottles/
  pieces from the original import - "can't compare that amount" should show up a lot less.
- Products moved off the main menu - it's reachable from a link on the Stock page now, Stock
  stays focused on what's actually in the kitchen.
- Suggestions got a timeout bump and a visible warning that a reply can take a couple of
  minutes - and if one fails, the actual reason now shows on the page instead of nothing
  happening.

## 2026-10-19

- Suggested meals that match a recipe you already have now link to it instead of
  creating a near-duplicate every time - ratings and edits actually build up on
  one recipe over time now.
- Ratings feed back into meal ideas - well-rated saved recipes get preferred,
  poorly-rated ones get avoided.
- Cooking a recipe now works out how many portions are left over for you
  automatically, based on your portion size in Settings, instead of asking you
  to do the maths.
- Stored meals (fridge/freezer leftovers) show up front and centre when
  planning meals, before generating new ideas - use what's already cooked
  first.
- Suggest a recipe and Meal ideas both got a "how much should it stick to what
  we've got" choice (all / missing a few / can make), same as the recipes
  filter.
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
