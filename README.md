# Larder

Keeps track of what's in the fridge, freezer and cupboards. I'd been running Grocy for a
while and it's a genuinely solid piece of software - properly thought out, does a lot, free.
But it's built to do everything, and I only ever touched about a third of it. The rest of
the screen was just stuff to work around: chores I wasn't going to track, equipment I don't
own, a barcode/unit setup that wanted more fiddling than I had patience for on a weeknight.
So I built Larder to just do the parts I actually use, in a way that fits how my house
actually runs - small portions matter here more than most households, I'm away from home
every second week on a rotating roster, and I wanted meal planning that works with what's
already sitting in the freezer instead of pretending the fridge is empty.
If your household is different, Grocy's flexibility might suit you better than this will -
this is deliberately the smaller, opinionated version.

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

## Suggestions (optional)

The Meal plan and Recipes pages have "Meal ideas" and "Suggest a recipe" buttons. These
send your stock list to a text generation API and show what comes back. Connect one of
Anthropic, OpenAI, local Ollama, OpenCode Go, Gemini, or OpenRouter in Settings. Ollama
usually just needs its address; the others need an API key. Without a connection the
buttons stay greyed out and nothing gets sent anywhere. A connection can be shared
with the rest of the household with the tickbox under it.

Keys are stored as plain text in the database, since the server needs them to make the
calls. Bear that in mind with your backups.

Everything else (the recipe check, shopping list maths and so on) is just sums and works
without any of this.

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
- A custom base URL for suggestions can't point at this machine or the home network,
  except for Ollama, which is meant to run there.
- There's no import from Grocy, at least not yet.

## What's next

Things I know are missing and might get to:

- A low-stock count somewhere visible on the Stock page, so you notice you're running low
  on something without having to go looking.
- A country and currency setting, with a list of supermarkets that updates based on which
  country you pick (Woolworths, Coles, Aldi for Australia, wherever makes sense elsewhere).
  Pick the ones you actually shop at and it pulls live prices for your shopping list from
  each one, side by side. Realistically this will roll out country by country as the price
  data becomes available rather than all at once - some places just don't publish anything
  usable yet.
- Proper import from Grocy's export, instead of the one-off script I used for my own data.
- A nicer recipe page - photo-forward card layout, a proper filter row instead of the one
  checkbox, and a better add/edit form with optional ingredient groupings.
- Importing recipes instead of typing them in by hand: paste a block of text, paste a link
  to a recipe site, or a photo of a cookbook page, and have it come back as a real recipe
  you review before saving - the same review screen suggestions already use. Paste-a-link
  can read structured recipe data straight off most recipe sites without needing a model at
  all; a photo needs a provider that can actually look at images.
- A quick "give me an idea" right on a single meal plan slot instead of only the whole-grid
  version - type what you feel like for just that one meal and get one answer back.
- Tracking cooked meals as their own thing, not just raw stock. Cook something for 4, eat 1,
  and the other 3 become portions sitting in the fridge or freezer that the meal plan can
  pull from directly instead of cooking fresh again. A short code written on the container
  (something like B102) ties a container back to its batch - reuses the barcode system
  already there, just handwritten instead of scanned.
- A rating on recipes - nothing fancy, just enough to look back and go "oh that one was good"
  or "never again."

If you build any of these first, a pull request is very welcome.
