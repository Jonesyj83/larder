# Meal plan: configurable length + day/slot picker for Meal ideas

Read every file this touches in full before changing anything:
app/routes/mealplan.py, app/routes/suggestions.py, app/templates/mealplan.html,
app/static/app.js, app/static/style.css, app/db.py (SCHEMA section), and the
existing tests in tests/test_planning.py, tests/test_discovery.py,
tests/test_usability.py so you understand current behavior before changing it.

## Background (don't re-litigate this, it's settled)

The "Meal ideas" feature (POST /api/suggestions/meals in suggestions.py,
opened via the "Meal ideas" button in mealplan.html) currently takes a
`days` count (capped 1-7) and a flat list of `slots` (e.g. ["breakfast",
"dinner"]) applied the same way to every day, and returns one block of
free-text prose describing meal ideas. It does NOT write anything into the
calendar - the user reads the text and manually adds meals one at a time via
the existing "+" button / add-meal modal.

The calendar itself (GET /api/mealplan in mealplan.py, the week() function)
already supports showing any `days` up to 42 from any `start` date - that
part is fine as-is for an arbitrary date range. Don't rebuild it.

The owner (household end user, not technical) wants:

1. The 1-7 day cap on meal ideas is too short - he's moving to a 2-on/2-off
   FIFO roster and wants to plan ~2 weeks of shopping at once. Needs this
   usable for 1 week / 2 weeks / 4 weeks (common cases) or an arbitrary
   custom date range (he said "almost 2 weeks" - his roster doesn't land on
   exact week boundaries).

2. Right now the slot list is a flat "which meal types apply to every day",
   which doesn't match how people actually eat (e.g. breakfast is usually
   routine and doesn't need planning every day, lunch might only be wanted on
   2 of the 14 days, dinner every day). He explicitly chose: "A fuller
   tick-box grid of every day x every slot (breakfast/lunch/dinner/snack) to
   explicitly include/exclude each one" over a simpler "just skip slots"
   option. Build a real day-by-day, slot-by-slot grid of checkboxes (like a
   calendar grid: rows or columns = days, the other axis = slots), not a
   single flat list.

3. He separately already has a household-wide "Food preferences" feature
   (diet tags, avoid-list, other notes - see suggestions.py DIET_TAGS,
   _preferences_text()). That's done and working; Meal ideas already calls
   _preferences_text(db) and includes it in the prompt (check meal_ideas()).
   Don't duplicate this or build a second preferences UI - just make sure
   the existing one still gets used when you change the prompt/payload shape.

## What to actually build

Backend (app/routes/suggestions.py):
- Replace `days: int` + flat `slots: list[str]` in MealIdeasIn with a
  structure that carries an explicit list of (day, slot) pairs the user
  ticked, e.g. `cells: list[dict]` each `{"day": "2026-10-20", "slot":
  "dinner"}`, validated against a real date and SLOTS, capped at a sane
  total (e.g. 60 cells - two weeks x 4-5 slots is well under that; refuse
  anything larger with a clear 400, don't silently truncate).
- The prompt sent to the provider needs to actually describe which days/
  slots are wanted (group by day for readability in the prompt, e.g. "Mon
  21 Oct: dinner" / "Tue 22 Oct: lunch, dinner" rather than one flat list),
  still includes _people(db), _preferences_text(db), _kitchen(db), known
  saved recipes - keep all of that, you're changing what's being asked for,
  not removing context that already works.
- Keep the response as free text for now (text + provenance) unless you can
  cleanly also return something structured without over-engineering it -
  don't attempt to auto-insert the suggestions into the calendar as real
  meal_plan rows in this task; that's a separate, bigger feature (parsing
  arbitrary free text into dated meals reliably is its own problem). Scope
  is: get a good prompt out, asking for exactly the day/slot cells picked.
- Add real tests for: cell validation (bad date, bad slot, too many cells,
  empty list -> clear error, not a crash), the prompt actually contains the
  cells grouped sensibly, preferences and kitchen context still present.

Frontend (app/templates/mealplan.html, "Meal ideas" modal):
- Replace the single "How many days" number input + flat slot checkboxes
  with:
  - A length control: dropdown with "1 week", "2 weeks", "4 weeks", and a
    "Custom range" option that reveals two date pickers (from/to) using the
    existing date_field() component pattern used elsewhere in this file and
    in stock.html - look at how {{ date_field(...) }} is used for a working
    example, match that style, respect the user's chosen date format.
  - Once a length/range is chosen, render a real grid: one row or column
    per day in the range, one column or row per slot (use the existing
    SLOTS list exposed from the page - check how `slots` already reaches
    this template), with a checkbox at each day/slot intersection. Default
    state: a sensible starting tick pattern is fine (e.g. dinner ticked for
    every day, everything else unticked) since most households plan dinner
    every day and plan other meals occasionally - but it must be fully
    editable, every box togglable independently.
  - A "tick whole day" / "tick whole slot column" quick toggle (click a day
    label to toggle its whole row, click a slot label to toggle its whole
    column) is a nice-to-have IF it's trivial to add cleanly; skip it if it
    adds real complexity, the per-cell checkboxes alone satisfy the request.
  - For 14-28 day ranges this grid must stay usable and genuinely clean on
    both desktop and a 390px-wide phone screen - it cannot just overflow
    sideways with no scroll handling or shrink into unreadable tiny
    checkboxes. Use a horizontally scrollable table/grid container with
    sticky day or slot labels if that's what it takes; this is explicitly
    a "needs to be really clean and neat" requirement from the owner, not
    a minor nice-to-have. Test it visually.
  - Send the ticked (day, slot) pairs to the new backend shape.
  - Keep the "Anything else?" free-text note field - that's unrelated to
    this task and still useful per-request.

Database: no schema change should be needed for this task (nothing is being
saved yet, it's a request payload + prompt shape). If you find you genuinely
need one, explain why in your report rather than assuming.

Styling: match the existing "Evening kitchen" theme already in style.css
(charcoal/slate, amber accents, Trebuchet typography) - don't introduce new
colors, fonts, or a different visual language. No node_modules, no new JS
frameworks - plain Alpine.js and vanilla CSS like the rest of the app.

## Hard rules (same as every other task in this codebase)

- Work only inside this worktree. Never touch /home/jonesy/larder/data or
  any file outside this directory. No commits, no push, no deploy - the
  owner's agent reviews your diff, runs tests itself, and merges.
- Zero mention anywhere (code comments, UI copy, docs) that AI, an agent,
  Claude, or Codex was involved. It has to read like a person wrote it.
- Run the actual test suite yourself before calling this done:
  `PYTHONPATH=$(pwd) LARDER_DATA=<scratch dir> python -m unittest discover -s tests`
  (python3 if `python` isn't on PATH). If some sandbox restriction stops you
  from running commands, say so explicitly and clearly at the very top of
  your report - don't claim success you didn't verify. The parent will
  re-run the suite regardless, but an honest "I couldn't run it" saves
  everyone time over a false "all tests pass."
- Check for other places the old MealIdeasIn shape (days/slots) might be
  referenced (other templates, other tests) before changing its shape, and
  update all of them consistently - don't leave a stale caller broken.

## When you're done

Write a plain-text report to ./MEALPLAN_REPORT.md (inside this worktree)
covering: what you built, what you decided and why for the open design
choices above (grid orientation, default tick pattern, how you grouped days
in the prompt), what tests you added and their count, the full test run
output (or an honest explanation of why you couldn't run it), and anything
you think still needs a human decision or a follow-up task (e.g. actually
parsing suggestions into real calendar entries is explicitly out of scope
here - flag it as a natural next step, don't attempt it).
