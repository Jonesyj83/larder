import re
import json
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ShoppingPrices(unittest.TestCase):
    def run_logic(self, template, program):
        source = (ROOT / 'app/templates' / template).read_text()
        script = (ROOT / 'app/static/app.js').read_text().split('function drawHistory(svg, chart)')[1]
        script = 'function drawHistory(svg, chart)' + script + re.findall(r'<script>(.*?)</script>', source, re.S)[0]
        script = script.replace("{{ user.portion_size }}", 'regular').replace("{{ 'true' if user.is_admin else 'false' }}", 'false')
        result = subprocess.run(['node'], input="const assert = require('node:assert/strict');\n" + script + program,
                                text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_shopping_prices_and_inline_history(self):
        template = (ROOT / 'app/templates/shopping.html').read_text()
        for removed in ('Match this item', 'Auto-match all', 'openMatch(', 'redoMatch(', 'i.sources', 'origin_unknown', "'/products?id='"):
            self.assertNotIn(removed, template)
        self.assertIn('@click="openPicker(i, store.store)"', template)
        self.assertIn('@submit.prevent="searchMatches(i)"', template)
        self.assertIn('@click="pickMatch(i, store.store, candidate)"', template)
        self.assertIn('x-for="store in enabledStores"', template)
        self.assertIn('x-effect="drawHistory($el, i.historyChart)"', template)
        self.run_logic('shopping.html', r'''
const calls = [];
const oops = e => { throw e; };
let fail = false;
const api = async (path, options) => {
  calls.push([path, options]);
  if (path === '/api/shopping/lists') return [{id:1}];
  if (path === '/api/shopping?list_id=1') return [{id:1, product_id:10, label:'Milk'}, {id:2, label:'Loose'}];
  if (path === '/api/pricing/stores') return [{store:'coles',enabled:1},{store:'aldi',enabled:1},{store:'iga',enabled:0}];
  if (fail) throw Error('offline');
  if (path.endsWith('/history')) return {stores:[{store:'coles',enabled:1,history:[{price:3,checked_at:'2026-10-01'}]}],purchases:[]};
  if (path === '/api/pricing/products/10') return [{store:'coles',store_name:'Milk 2L',latest_check:{price:3}}, {store:'iga',latest_check:{price:1}}];
  if (path === '/api/shopping/1?list_id=1') {assert.deepEqual(options.body, {amount:2}); return {};}
  throw Error('Unexpected ' + path);
};
(async () => {
  const page = shoppingPage();
  await page.load();
  await page.loadPrices(page.items);
  const item = page.items[0];
  assert.equal(page.priceLinks(item).length, 1);
  assert.equal(page.badgePrice(item, 'coles'), '$3.00 · 2L');
  assert.equal(page.badgePrice(item, 'aldi'), '—');
  item.links[0].size = '3L';
  assert.equal(page.badgePrice(item, 'coles'), '$3.00 · 3L');
  assert.ok(page.cheapest(item, page.storeLink(item, 'coles')));
  assert.ok(item.historyChart);
  assert.ok(!calls.some(([path]) => path.includes('undefined')));
  item.amount = 2;
  await page.updateAmount(item);
  fail = true;
  await page.loadPrice(item);
  assert.ok(item.pricingError);
  assert.equal(item.historyChart, null);
  assert.equal(page.priceText(0), '$0.00');
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
        self.assertIn('x-model.number="i.amount"', template)
        helpers = (ROOT / 'app/static/app.js').read_text()
        helpers = helpers[helpers.index('function num('):helpers.index('function shelfLabel(')] + helpers[helpers.index('const MASS ='):]
        self.run_logic('shopping.html', "const window = {}; const preferences = () => ({measurement_system:'metric'});\n" + helpers +
            "\nconst items = " + json.dumps(by_id) + ";\n" + r'''
const page = shoppingPage();
assert.equal(page.amountText(items[1]), '60 ml needed (sold as 2 l)');
assert.equal(page.amountText(items[2]), '1 bottle');
assert.equal(page.amountText(items[4]), '1 piece');
''')

    def test_add_matches_only_added_product_and_keeps_item_on_failure(self):
        from app import db as database, pricing
        from app.routes import shopping
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(database.SCHEMA)
        db.executescript("""
            INSERT INTO units(id,name) VALUES(1,'piece');
            INSERT INTO products(id,name,unit_id) VALUES(1,'Woolworths Chilli Ground',1),(2,'Other',1);
            INSERT INTO shopping_lists(id,name) VALUES(1,'Home');
            INSERT INTO store_settings(store,enabled) VALUES('coles',1),('iga',0);
        """)
        with patch.object(pricing, 'auto_match') as match:
            shopping.add(shopping.ItemIn(product_id=1), db=db, user={'id':None}, list_id=1)
            match.assert_called_once_with(db, 1, 'Woolworths Chilli Ground')
        with patch.object(pricing, 'auto_match', side_effect=RuntimeError('offline')) as match, self.assertLogs(level='WARNING'):
            result = shopping.add(shopping.ItemIn(product_id=1), db=db, user={'id':None}, list_id=1)
            self.assertEqual(result['amount'], 2)
        db.execute("INSERT INTO product_store_links(product_id,store,store_item_id,store_name) VALUES(1,'coles','1','Chilli Ground')")
        db.commit()
        with patch.object(pricing, 'auto_match') as match:
            shopping.add(shopping.ItemIn(product_id=1), db=db, user={'id':None}, list_id=1)
            shopping.add(shopping.ItemIn(name='Loose'), db=db, user={'id':None}, list_id=1)
            match.assert_not_called()

    def test_estimates_convert_units_round_packs_and_scale_loose_produce(self):
        from app import db as database
        from app.routes import shopping, pricing
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(database.SCHEMA)
        db.executescript("""
            INSERT INTO units(id,name) VALUES(1,'mL'),(2,'bottle'),(3,'L'),(4,'kg'),(5,'g');
            INSERT INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(3,1,1000),(4,5,1000);
            INSERT INTO products(id,name,unit_id,purchase_unit_id,purchase_factor)
                VALUES(1,'Stock',1,2,1000),(2,'Onions',4,NULL,1);
            INSERT INTO shopping_lists(id,name) VALUES(1,'Home');
            INSERT INTO shopping_list(id,list_id,product_id,amount,unit_id)
                VALUES(1,1,1,100,1),(2,1,2,1.5,4);
        """)
        items = shopping.list_items(db=db, user={'id':1}, list_id=1)
        links = []
        for product_id, name, size, price, measure in [(1,'Stock 1L','1L',1.9,'1L'),(2,'Brown Onions per kg','',2,'kg')]:
            link_id = pricing.pricing.link_product(db, product_id, 'aldi', str(product_id), name, size=size)
            pricing.pricing.record_price(db, link_id, {'price':price,'unit_price':price,'unit_measure':measure})
            links.append(pricing.product_prices(product_id, db=db))
        self.assertEqual(links[0][0]['price_quantity'], {'stock_units':1000, 'whole_packs':True})
        self.assertEqual(links[1][0]['price_quantity'], {'stock_units':1, 'whole_packs':False})
        self.run_logic('shopping.html', 'const items = ' + json.dumps(items) + '; const links = ' + json.dumps(links) + ';' + r'''
const page = shoppingPage();
page.stores = [{store:'aldi',enabled:1}];
page.items = items.map(i => ({...i, links:links[i.product_id - 1]}));
assert.equal(page.splitTotal, 4.9);
assert.equal(page.storeTotals[0].total, 4.9);
const stock = page.items.find(i => i.product_id === 1);
stock.amount = 1001;
assert.equal(page.itemCost(stock, stock.links[0]), 3.8);
stock.amount = 2000;
assert.equal(page.itemCost(stock, stock.links[0]), 3.8);
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
