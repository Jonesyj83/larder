import re
import json
import sqlite3
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ShoppingPrices(unittest.TestCase):
    def run_logic(self, template, program):
        source = (ROOT / 'app/templates' / template).read_text()
        script = re.findall(r'<script>(.*?)</script>', source, re.S)[0]
        script = script.replace("{{ user.portion_size }}", 'regular').replace("{{ 'true' if user.is_admin else 'false' }}", 'false')
        result = subprocess.run(['node'], input="const assert = require('node:assert/strict');\n" + script + program,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shopping_prices_and_matching(self):
        template = (ROOT / 'app/templates/shopping.html').read_text()
        self.assertIn('i.pricingLoaded && !priceLinks(i).length', template)
        self.assertIn('Match this item', template)
        self.assertIn('x-for="link in priceLinks(i)"', template)
        self.run_logic('shopping.html', r'''
const calls = [];
const oops = e => { throw e; };
let linked = false;
let fail = false;
const api = async (path, options) => {
  calls.push([path, options]);
  if (path === '/api/shopping/lists') return [{id: 1, name: 'Shopping', item_count: 1}];
  if (path === '/api/shopping?list_id=1') return [{id:1, product_id:10, label:'Milk'}, {id:2, label:'Loose'}, {id:3, product_id:11, label:'Bread'}];
  if (path === '/api/pricing/stores') return [{store:'coles', enabled:1}, {store:'iga', enabled:0}];
  if (path === '/api/pricing/products/11') throw Error('offline');
  if (path.endsWith('/link/coles')) { linked = false; return {}; }
  if (path.endsWith('/link')) { assert.equal(options.body.price, 3); assert.equal(options.body.store_name, 'Milk 2L'); assert.equal(options.body.store, 'coles'); linked = true; return {}; }
  if (path === '/api/pricing/products/10') {
    if (fail) throw Error('offline');
    return linked ? [{store:'coles', latest_check:{price:3, on_special:1}}, {store:'iga', latest_check:{price:1}}] : [];
  }
  if (path.startsWith('/api/pricing/search?')) return [{store_item_id:'123', name:'Milk 2L', brand:'Brand', size:'2L', price:3}];
  throw Error('Unexpected ' + path);
};
(async () => {
  const page = shoppingPage();
  page.products = [{id:10, name:'Milk'}];
  await page.load();
  assert.equal(page.items.length, 3);
  await page.loadPrices(page.items);
  const item = page.items[0];
  assert.equal(item.pricingLoaded, true);
  assert.equal(page.priceLinks(item).length, 0);
  assert.ok(page.items[2].pricingError);
  assert.ok(!calls.some(([path]) => path.includes('/products/undefined')));
  page.openMatch(item);
  assert.equal(item.picker.query, 'Milk');
  assert.equal(item.picker.store, 'coles');
  await page.searchMatches(item);
  assert.ok(calls.some(([path]) => path === '/api/pricing/search?store=coles&q=Milk'));
  await page.pickMatch(item, item.picker.candidates[0]);
  assert.equal(page.priceLinks(item).length, 1);
  assert.equal(page.priceText(page.priceLinks(item)[0].latest_check.price), '$3.00');
  assert.equal(page.cheapest(item, page.priceLinks(item)[0]), true);
  assert.equal(page.priceText(null), 'price not checked yet');
  assert.equal(page.priceText(0), '$0.00');
  assert.equal(page.cheapest({links:[{store:'coles',latest_check:null}]}, {store:'coles',latest_check:null}), false);
  await page.redoMatch(item, page.priceLinks(item)[0]);
  assert.equal(page.priceLinks(item).length, 0);
  assert.equal(item.picker.store, 'coles');
  fail = true;
  await page.loadPrice(item);
  assert.ok(item.pricingError);
})().catch(e => {console.error(e); process.exit(1);});
''')

    def test_selected_list_is_remembered_and_used(self):
        self.run_logic('shopping.html', r'''
const saved = {'shopping-list': '2'};
const localStorage = {getItem: key => saved[key], setItem: (key, value) => saved[key] = String(value)};
const calls = [];
const oops = e => {throw e;};
const toast = () => {};
const api = async (path, options) => {
  calls.push([path, options]);
  if (path === '/api/shopping/lists') return [{id:1, name:'Shopping', item_count:0}, {id:2, name:'Work', item_count:0}];
  if (path.startsWith('/api/shopping/add-missing')) return {added:1};
  return [];
};
(async () => {
  const page = shoppingPage();
  await page.init();
  assert.equal(page.list_id, 2);
  assert.ok(calls.some(([path]) => path === '/api/shopping?list_id=2'));
  page.list_id = 1;
  await page.selectList();
  assert.equal(saved['shopping-list'], '1');
  page.text = 'Bread';
  await page.add();
  assert.ok(calls.some(([path, opts]) => path === '/api/shopping?list_id=1' && opts?.method === 'POST'));
  await page.addMissing();
  assert.ok(calls.some(([path]) => path === '/api/shopping/add-missing?list_id=1'));
  saved['shopping-list'] = '999';
  const reloaded = shoppingPage();
  await reloaded.init();
  assert.equal(reloaded.list_id, 1);
})().catch(e => {console.error(e); process.exit(1);});
''')

    def test_stock_context_and_purchase_size_with_real_units(self):
        from app import db as database
        from app.routes import shopping
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(database.SCHEMA)
        db.executescript("""
            INSERT INTO units(id,name) VALUES(1,'mL'),(2,'bottle'),(3,'L'),(4,'piece');
            INSERT INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(3,1,1000);
            INSERT INTO products(id,name,unit_id,purchase_unit_id,purchase_factor)
                VALUES(1,'Milk',1,2,2000),(2,'Apples',4,4,1);
            INSERT INTO shopping_lists(id,name) VALUES(1,'Home'),(2,'Work');
            INSERT INTO stock(product_id,amount) VALUES(1,30),(1,70),(2,2);
            INSERT INTO shopping_list(id,list_id,product_id,amount,unit_id) VALUES
                (1,1,1,60,1),(2,1,1,1,2),(3,1,1,0.05,3),(4,1,2,1,4),(5,2,1,60,1);
            INSERT INTO shopping_list(id,list_id,name,amount) VALUES(6,1,'Loose item',1);
        """)
        items = shopping.list_items(db=db, user={'id':1}, list_id=1)
        by_id = {i['id']:i for i in items}
        self.assertNotIn(5, by_id)
        self.assertTrue(by_id[1]['stock_covers_need'])
        self.assertFalse(by_id[2]['stock_covers_need'])
        self.assertTrue(by_id[3]['stock_covers_need'])
        self.assertEqual(by_id[1]['purchase_size'], {'amount':2000,'unit':'mL'})
        for number in (2,3,4,6):
            self.assertIsNone(by_id[number]['purchase_size'])
        self.assertFalse(by_id[6]['stock_covers_need'])
        template = (ROOT / 'app/templates/shopping.html').read_text()
        self.assertIn('x-show="i.stock_covers_need">Already have enough in stock', template)
        self.assertIn("'/products?id=' + i.product_id", template)
        helpers = (ROOT / 'app/static/app.js').read_text()
        helpers = helpers[helpers.index('function num('):helpers.index('function shelfLabel(')] + helpers[helpers.index('const MASS ='):]
        self.run_logic('shopping.html', "const window = {}; const preferences = () => ({measurement_system:'metric'});\n" + helpers +
            "\nconst items = " + json.dumps(by_id) + ";\n" + r'''
const page = shoppingPage();
assert.equal(page.amountText(items[1]), '60 ml needed (sold as 2 l)');
assert.equal(page.amountText(items[2]), '1 bottle');
assert.equal(page.amountText(items[4]), '1 piece');
''')

    def test_bulk_action_summary_and_recovery(self):
        self.run_logic('shopping.html', r'''
let fail = false;
const api = async (path, opts) => {
  assert.equal(path, '/api/pricing/products/auto-match');
  assert.equal(opts.method, 'POST');
  if (fail) throw Error('offline');
  return {matched:[{product_id:1, name:'Milk', store:'coles'}], unmatched:[{product_id:2, name:'Blue Cheese'}]};
};
(async () => {
  const page = shoppingPage();
  let refreshed = 0;
  page.loadPrices = async () => {refreshed++;};
  await page.autoMatchAll();
  assert.equal(page.matchSummary, 'Matched 1, couldn’t match 1: Blue Cheese');
  assert.equal(page.matching, false);
  assert.equal(refreshed, 1);
  fail = true;
  await page.autoMatchAll();
  assert.match(page.matchSummary, /unavailable/);
  assert.equal(page.matching, false);
})().catch(e => {console.error(e); process.exit(1);});
''')

    def test_settings_store_controls(self):
        template = (ROOT / 'app/templates/settings.html').read_text()
        self.assertLess(template.index('<summary>Store prices</summary>'), template.index('{% if user.is_admin %}'))
        self.assertIn(':disabled="!store.enabled || store.saving"', template)
        self.run_logic('settings.html', r'''
const preferences = () => ({});
const toast = () => {};
const oops = e => {throw e;};
const calls = [];
const api = async (path, options) => {
  calls.push([path, options]);
  if (options) return {store:'iga', ...options.body};
  return [{store:'iga', enabled:0, postcode:''}];
};
(async () => {
  const page = settingsPage();
  await page.loadStores();
  assert.equal(page.stores.length, 1);
  const store = page.stores[0];
  store.postcode = '6000';
  await page.saveStore(store, true);
  assert.deepEqual(calls[1], ['/api/pricing/stores/iga', {method:'PUT', body:{enabled:true, postcode:'6000'}}]);
  assert.equal(store.enabled, true);
  assert.equal(store.saving, false);
  assert.deepEqual(['woolworths','coles','aldi','iga'].map(s => page.storeName(s)), ['Woolworths','Coles','Aldi','IGA']);
})().catch(e => {console.error(e); process.exit(1);});
''')
