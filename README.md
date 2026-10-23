# Larder

Keeps track of what's in the fridge, freezer and cupboards, what's on the shopping
list, the recipes and the meal plan for the week. I'd been running Grocy for a while
and it's genuinely solid software - properly thought out, does a lot, free. I only
ever touched about a third of it, and kept working around the rest: chores I wasn't
going to track, equipment I don't own, a barcode/unit setup that wanted more fiddling
than I had patience for on a weeknight. So I built Larder to do the parts I actually
use, in a way that fits how my house actually runs - small portions matter here more
than most households, I'm away every second week on a rotating roster, and meal
planning needed to work with what's already sitting in the freezer instead of
pretending the fridge is empty.

It's grown past "cut-down Grocy" since then. There's a household memory for the
likes/dislikes/habits/conditions that come up in the kitchen, meal planning that
thinks in batches (cook once, eat the leftovers across the week), purchase price
history, and a small MCP server so a chat assistant can read and write the same data
the web pages use. None of that lives inside a single page you have to go hunting
for - it's built into the same stock/shopping/recipes/meal plan flow as everything
else. If your household runs differently, Grocy's flexibility will probably suit you
better - this is still the smaller, opinionated version, just a bit less small than
it used to be.

It's a FastAPI app with a single SQLite file. The frontend is plain HTML with Alpine.js,
so there's no build step. [CHANGELOG.md](CHANGELOG.md) has what's new.

## Running it

You need Python 3.11 or newer.

    ./run.sh

The first run makes a `.venv` and installs everything from requirements.txt. After that
it just starts. Go to http://localhost:8000. The first account you make is the admin, and
you can add the rest of the house from Settings.

Port and host can be changed with `LARDER_PORT` and `LARDER_HOST`. Everything the app
stores (database, uploaded pictures, session secret) goes in `data/`, or wherever
`LARDER_DATA` points. Back that folder up and you've backed up everything.

If you'd rather do it by hand:

    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

## Connecting an AI assistant

Larder ships its own MCP server (`mcp_server/`) so Claude, Hermes or anything else that
speaks MCP can read and write your stock, shopping list, recipes, meal plan and household
memory - the same data the web pages use, through the same API key system.

    python3 -m venv .venv-mcp
    .venv-mcp/bin/pip install -r mcp_server/requirements.txt
    export LARDER_URL=http://127.0.0.1:8000
    export LARDER_API_KEY=your-personal-key   # made in Settings
    .venv-mcp/bin/python mcp_server/server.py

Point your MCP client's config at that command and those two environment variables.
Full setup, the JSON config example, and what each tool expects: [mcp_server/README.md](mcp_server/README.md).

## Keeping store prices fresh

An existing product/store link is kept until someone changes or removes it.
Automatic matching never searches that store again, even if its catalogue changes.
For an empty store slot, a remembered purchase at that store comes first, then
remembered brand/type and retailer classification from the household's other
choices. Explicit details in the item name still apply. With no usable history,
it picks the closest plain option and uses shelf price to settle a tie.

A quiet ≈ on a store chip means a substitute rather than the same product, such
as a private label standing in for a national brand. It is separate from Review,
which means several candidates remained tied. Keeping a match clears Review;
a substitute stays marked as a substitute. New shopping-history entries retain
the chosen store item so that removing a link does not erase the buying choice.
Older quantity-only history cannot supply an item identity it never recorded.

Matching a product to a store (via the shopping list) is a one-off search.
After that, `scripts/refresh_prices.py` re-checks every matched item's
price directly by its known ID - no searching, no browsing a catalogue,
just "is this exact item still this price." It's meant to run once a
night, not inside the web process.

If you're running this as a systemd user service alongside `larder.service`,
two more unit files do it:

    cp scripts/larder-price-refresh.service scripts/larder-price-refresh.timer ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user enable --now larder-price-refresh.timer

Or a plain cron line does the same thing - see the example and the pacing/
circuit-breaker notes in the script's own docstring.

## Updating

    ./update.sh

Backs up the database first (keeps the last 20 backups in `data/backups/`), pulls the
latest code with `git pull --ff-only` so it never overwrites a local change without
telling you, then restarts the server - `run.sh` picks up any new Python packages and
the app applies any new database columns/tables on its own when it starts. Nothing in
your data gets touched; schema changes are additive, never destructive.

If you run it under systemd instead of calling `run.sh` directly, set `LARDER_SERVICE`
to your unit's name and `update.sh` will restart that instead:

    LARDER_SERVICE=larder ./update.sh

If you changed something locally and `git pull --ff-only` refuses because of that,
either commit/stash your change first or just pull manually and sort it out - the
script won't guess for you.

### Phones and the camera

Browsers only allow the camera on https (localhost is the exception). On the LAN over
plain http the scan page still works, you just have to type barcodes in. Putting it behind
something like Caddy with a real certificate sorts that out. Once it's on https you can
use "Add to home screen" and it opens like an app.

The scanner uses the browser's built-in BarcodeDetector where there is one (Chrome on
Android is fine). Firefox and iOS Safari don't have it, so the page loads zxing from a CDN
instead. You can also pick zxing yourself for QR codes and Data Matrix.

## How things fit together

- **Locations** - Fridge, Pantry and Freezer to start with, add whatever else you've got.
- **Units** - each product is counted in one unit (pieces, g and so on). It can also have
  a "bought as" unit with a conversion, e.g. bought as 1 pack = 20 pieces for frozen
  chicken strips. General conversions like kg to g are on the Locations & units page.
- **Products** - name, default location, unit, picture, and whether the date on it is a
  "Best by" or a "Use by". Past dates show in red, dates within three days in amber,
  later dates in green.
- **Stock** - purchase adds stock, consume takes it away (oldest date first). Ticking
  "spoiled" logs it as thrown out instead of eaten.
- **Shopping list** - add products or just free text. "Got it" adds it to stock and removes it
  from the list. The checkbox just ticks it off. Products with a minimum amount set
  can be added automatically when they run low.
- **Recipes** - ingredients and amounts. Each recipe shows what you have and what's
  short, scaled to however many servings you want. "Put missing on shopping list" does
  what it says, and "Cooked it" takes the ingredients out of stock.
- **Meal plan** - a week at a time, breakfast / lunch / dinner / snack / dessert. Everyone
  has a portion size (small, regular, large) in Settings. Pick who's eating and the
  servings get worked out from that (small = half, large = one and a half). "What we need"
  adds up the week's ingredients against stock.

## Scanning

Scan or type a barcode on the Scan page. If it's a known barcode you get the product with
its picture and can add or use some straight away. If it isn't, it asks OpenFoodFacts and
fills in a name and picture, and you can either make a new product or attach the barcode
to one you already have. The location you last used for each barcode is remembered in the
browser.

## Household memory

The Memory page keeps household likes, dislikes, conditions, habits, buying patterns,
recipe notes and cost notes - things worth remembering about how the house eats that
don't belong on any one product or recipe. Edit notes inline or add and delete them
manually; search works across every category at once. This is also what makes the
MCP server (above) worth connecting day to day - an assistant reading stock and
writing down a new household preference keeps it useful, rather than something
you fill in by hand once and forget about.

Purchase prices stay in the stock log after stock is consumed; the API provides
product price history and spending totals. A receipt has its own record too
(`/api/receipts`) - its own total, store and date, separate from the individual
items bought on it, since whole-receipt discounts don't split evenly per product.
Work is included in the default locations.

## API

Everything the web pages do goes through the JSON API under `/api`, and FastAPI's docs
for it are at `/docs`. Make a key in Settings and send it as `X-API-Key: your-key-here` (or
`Authorization: Bearer your-key-here`).

    curl -H "X-API-Key: your-key-here" http://localhost:8000/api/stock

    curl -X POST -H "X-API-Key: your-key-here" -H "Content-Type: application/json" \
         -d '{"product_id": 3, "amount": 1}' http://localhost:8000/api/stock/consume

    # barcode lookup, encode the barcode since QR codes can have slashes in them
    curl -H "X-API-Key: your-key-here" http://localhost:8000/api/scan/5000157024671

## Things to know

- Uploaded pictures under `/uploads` don't need a login to view. Didn't seem worth the
  hassle for photos of tins.
- Logging out clears the cookie. Sessions last 60 days otherwise. Changing a password (or
  an admin resetting it) logs that account out everywhere else.
- Scripts should use an API key. Changes made with just the login cookie are refused
  unless they come from the app's own pages.
- Ten wrong passwords for the same username from the same address locks that login for
  15 minutes (it's kept in memory, so a restart clears it).
- There's no import from Grocy, at least not yet.

## What's next

Things I know are missing and might get to:

- A simple page for the spending data that's already being tracked - the API can
  already tell you what you've spent over the last N days and a product's price
  history, there's just no screen that shows it to a human yet.
- A proper receipt importer that actually handles Coles, Woolworths and Aldi
  PDFs/eReceipts on its own, instead of the one-off script I used to get my own
  data in. The matching-against-existing-products logic already works, it just
  needs a real upload page instead of me hand-typing a receipt in.
- A country and currency setting, with a list of supermarkets that updates based on which
  country you pick (Woolworths, Coles, Aldi for Australia, wherever makes sense elsewhere).
  Pick the ones you actually shop at and it pulls live prices for your shopping list from
  each one, side by side. Realistically this will roll out country by country as the price
  data becomes available rather than all at once - some places just don't publish anything
  usable yet.
- Proper import from Grocy's export, instead of the one-off script I used for my own data.

If you build any of these first, a pull request is very welcome.
