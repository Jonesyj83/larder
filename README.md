# Larder

Keeps track of what's in the fridge, freezer and cupboards. I wanted something like Grocy
but smaller, so this only does the bits we actually use at home: stock, a shopping list,
barcode scanning, recipes and a meal plan.

It's a FastAPI app with a single SQLite file. The frontend is plain HTML with Alpine.js,
so there's no build step.

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
  "Best by" or a "Use by". Use by dates going past show in red, best by in orange.
- **Stock** - purchase adds stock, consume takes it away (oldest date first). Ticking
  "spoiled" logs it as thrown out instead of eaten.
- **Shopping list** - add products or just free text. "Got it" ticks it off and adds it
  to stock. Products with a minimum amount set can be added automatically when they run low.
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

## Suggestions (optional)

The Meal plan and Recipes pages have "Meal ideas" and "Suggest a recipe" buttons. These
send your stock list to a text generation API and show what comes back, so they need an
account with one of: OpenAI, Anthropic, OpenRouter, DeepSeek, or a local Ollama. Put the
key in Settings. Without one the buttons just stay greyed out and nothing gets sent
anywhere. A key can be shared with the rest of the household with the tickbox under it.

Keys are stored as plain text in the database, since the server needs them to make the
calls. Bear that in mind with your backups.

Everything else (the recipe check, shopping list maths and so on) is just sums and works
without any of this.

## API

Everything the web pages do goes through the JSON API under `/api`, and FastAPI's docs
for it are at `/docs`. Make a key in Settings and send it as `X-API-Key: <key>` (or
`Authorization: Bearer <key>`):

    curl -H "X-API-Key: lk_..." http://localhost:8000/api/stock

    curl -X POST -H "X-API-Key: lk_..." -H "Content-Type: application/json" \
         -d '{"product_id": 3, "amount": 1}' http://localhost:8000/api/stock/consume

    # barcode lookup, encode the barcode since QR codes can have slashes in them
    curl -H "X-API-Key: lk_..." http://localhost:8000/api/scan/5000157024671

## Things to know

- Uploaded pictures under `/uploads` don't need a login to view. Didn't seem worth the
  hassle for photos of tins.
- Logging out clears the cookie. Sessions last 60 days otherwise. Changing a password (or
  an admin resetting it) logs that account out everywhere else.
- Scripts should use an API key. Changes made with just the login cookie are refused
  unless they come from the app's own pages.
- Ten wrong passwords for the same username from the same address locks that login for
  15 minutes (it's kept in memory, so a restart clears it).
- A custom base URL for suggestions can't point at this machine or the home network,
  except for Ollama, which is meant to run there.
- There's no import from Grocy, at least not yet.
