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
