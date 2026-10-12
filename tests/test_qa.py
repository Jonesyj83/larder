"""Run page actions and their visible labels without a browser or network."""
from html.parser import HTMLParser
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]


def page_script(name):
    return (ROOT / f'app/templates/{name}.html').read_text().split('<script>')[1].split('</script>')[0].replace('{{ user.id }}', '1')


def expressions(name):
    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.attrs = []

        def handle_starttag(self, tag, attrs):
            self.attrs.append(dict(attrs))

    parser = Tags()
    parser.feed((ROOT / f'app/templates/{name}.html').read_text())
    return parser.attrs


class PageSeams(unittest.TestCase):
    def run_js(self, script):
        pre = """
const assert = require('node:assert/strict');
const window = {USER_PREFS: {date_format: 'DD/MM/YYYY', measurement_system: 'metric'}};
const document = {addEventListener() {}}, navigator = {};
const location = {search: '?q=Oat%20milk'}, BASE = '';
const localStorage = {getItem() {return null;}, setItem() {}};
const oops = e => {throw e;}, toast = () => {}, calls = [];
async function api(path, opts={}) {
  calls.push({path, ...opts});
  if (path === '/api/recipes/8') return {servings: 1, ingredients: [{product_id: 3, amount: 2, unit_id: 1}]};
  return [];
}
"""
        helpers = (ROOT / 'app/static/app.js').read_text().replace(
            "function toast(msg, bad = false) { Alpine.store('toast').show(msg, bad); }", '')
        helpers = helpers.replace("function oops(e) { toast(e.message || String(e), true); }", '')
        helpers = helpers[helpers.index('function num'):]
        result = subprocess.run(['node'], input=pre + helpers + script,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_stock_link_search_and_zero_day_defaults(self):
        self.run_js(page_script('stock') + page_script('scan') + page_script('_stock_modal') + """
const stock = stockPage(); stock.init(); assert.equal(stock.q, 'Oat milk');
stock.items = [{name: 'Oat milk'}, {name: 'Bread'}];
assert.equal(stock.filtered().length, 1);
const scan = scanPage(); scan.product = {unit_id: 1, default_days: 0};
scan.fillDefaults(); assert.equal(scan.expires_on, today());
const modal = stockModal(); modal.p = {unit_id: 1, default_days: 0};
modal.setMode('purchase'); assert.equal(modal.expires_on, today());
""")

    def test_simple_meal_edit_keeps_scaled_ingredients(self):
        self.run_js(page_script('mealplan') + """
(async () => {
  const p = planPage(); p.load = () => {};
  await p.openEdit({id: 7, recipe_id: 8, simple_meal: 1, day: '2026-10-12',
    slot: 'breakfast', note: 'Toast', servings: 3});
  assert.equal(p.adding.ingredients[0].amount, 6);
  await p.add();
  const saved = calls.find(c => c.path === '/api/mealplan/7');
  assert.equal(saved.body.servings, 3);
  assert.equal(saved.body.ingredients[0].amount, 6);
})().catch(e => {console.error(e); process.exit(1);});
""")

    def test_suggestion_preview_uses_stock_units_and_unknown_is_not_green(self):
        attrs = expressions('recipes')
        label = next(a['x-text'] for a in attrs if a.get('x-show') == 'i.product && i.short > 0')
        green = next(a['x-show'] for a in attrs if a.get('class') == 'ok' and 'i.product' in a.get('x-show', ''))
        self.run_js("const label = i => (" + label + "); const green = i => (" + green + ");" + """
const i = {product: 'Bread', unit: 'pack', short_unit: 'slice', have: 3, short: 3};
assert.equal(label(i), '- have 3 slice, short 3 slice');
assert.equal(green({...i, short: null}), false);
assert.equal(green({...i, short: 0}), true);
""")

    def test_shopping_suggestion_origin_has_a_label(self):
        label = next(a['x-text'] for a in expressions('shopping') if 'source.kind' in a.get('x-text', ''))
        self.run_js("const label = source => (" + label + ");" + """
assert.equal(label({kind: 'suggestion', label: 'Toast', amount: 2}), 'Suggested recipe: Toast (2) ');
""")

    def test_meal_ideas_grid_lengths_toggles_and_incomplete_range(self):
        self.run_js(page_script('mealplan') + """
const p = planPage(); p.openIdeas();
for (const [length, days] of [['week', 7], ['2weeks', 14], ['4weeks', 28]]) {
  p.ideas.length = length; p.buildIdeasGrid();
  assert.equal(p.ideasDays().length, days); assert.equal(p.ideasCells().length, days);
}
p.toggleSlotColumn('lunch'); assert.equal(p.ideasCells().length, 56);
p.toggleSlotColumn('lunch'); assert.equal(p.ideasCells().length, 28);
const first = p.ideasDays()[0]; p.toggleDay(first);
assert.equal(p.ideasCells().length, 32); p.toggleDay(first);
assert.equal(p.ideasCells().length, 27);
p.ideas.length = 'custom';
for (const [from, to] of [['', '2026-10-14'], ['2026-10-12', ''],
                         ['2026-10-14', '2026-10-12']]) {
  p.ideas.rangeFrom = from; p.ideas.rangeTo = to; p.buildIdeasGrid();
  assert.deepEqual(p.ideasDays(), []); assert.deepEqual(p.ideasCells(), []);
}
p.ideas.rangeFrom = '2026-10-12'; p.ideas.rangeTo = '2026-10-14'; p.buildIdeasGrid();
assert.equal(p.ideasCells().length, 3);
""")


if __name__ == '__main__':
    unittest.main()
