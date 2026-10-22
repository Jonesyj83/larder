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
        self.assertIn('@click.stop="openPicker(i, store.store)"', template)
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
  if (path === '/api/shopping/lists') return [{id:1,kind:'order'}];
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
        self.run_logic('shopping_lists.html', r'''
const saved = {'shopping-list': '2'};
const localStorage = {getItem: key => saved[key], setItem: (key, value) => saved[key] = String(value)};
const calls = [];
const oops = e => {throw e;};
const toast = () => {};
const api = async (path, options) => {
  calls.push([path, options]);
  if (path === '/api/shopping/lists?kind=plain') return [{id:1, name:'Shopping', item_count:0}, {id:2, name:'Work', item_count:0}];
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
  saved['shopping-list'] = '999';
  const reloaded = shoppingPage();
  await reloaded.init();
  assert.equal(reloaded.list_id, 1);
})().catch(e => {console.error(e); process.exit(1);});
''')

    def test_stock_context_with_legacy_rows(self):
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
        for item in items:
            self.assertNotIn('stock_per_amount', item)
            self.assertNotIn('purchase_size', item)
        self.assertFalse(by_id[6]['stock_covers_need'])
        template = (ROOT / 'app/templates/shopping.html').read_text()
        self.assertIn('x-show="i.stock_covers_need">Already have enough in stock', template)
        self.assertIn('x-model.number="i.amount"', template)
        for removed in ('unitChoice', 'unit-mini', 'unit-choice', 'qty-unit', 'amountText', '/api/units'):
            self.assertNotIn(removed, template)

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
            INSERT INTO shopping_lists(id,name,kind) VALUES(1,'Home','order');
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
            match.assert_not_called()
        # A brand-new free-text name on the order list gets its own product so it
        # can still be matched to stores, not left as inert text.
        with patch.object(pricing, 'auto_match') as match:
            shopping.add(shopping.ItemIn(name='Loose'), db=db, user={'id':None}, list_id=1)
            match.assert_called_once()
            self.assertEqual(match.call_args[0][2], 'Loose')
        created = db.execute("SELECT id FROM products WHERE name='Loose'").fetchone()
        self.assertIsNotNone(created)

    def test_estimates_multiply_quantity_by_store_price(self):
        self.run_logic('shopping.html', r'''
const page = shoppingPage();
page.stores = [{store:'aldi',enabled:1}, {store:'coles',enabled:1}];
const pack = {store:'aldi', size:'1.1kg-1.8kg', latest_check:{price:3.50, unit_price:1.75}};
const other = {store:'coles', latest_check:{price:4}};
page.items = [{product_id:1, amount:2, links:[pack, other]}];
assert.equal(page.itemCost(page.items[0], pack), 7);
assert.equal(page.splitTotal, 7);
assert.equal(page.storeTotals[0].total, 7);
assert.equal(page.storeTotals[1].total, 8);
assert.equal(page.bestSingleStore.total, 7);
assert.equal(page.itemCost({amount:1.5}, pack), 5.25);
assert.equal(page.itemCost({amount:2}, {latest_check:{price:0}}), 0);
assert.equal(page.itemCost({amount:2}, {latest_check:{price:null}}), null);
const onions = {store_name:'Brown Onions per kg', latest_check:{price:2.49, unit_price:2.49}};
assert.equal(page.itemCost({amount:2}, onions), 4.98);
''')

    def test_price_response_has_no_quantity_conversion(self):
        from app import db as database
        from app.routes import pricing
        db = sqlite3.connect(':memory:')
        self.addCleanup(db.close)
        db.row_factory = sqlite3.Row
        db.executescript(database.SCHEMA)
        db.executescript("""
            INSERT INTO units(id,name) VALUES(1,'g');
            INSERT INTO products(id,name,unit_id) VALUES(1,'Chicken',1);
        """)
        link = pricing.pricing.link_product(db, 1, 'aldi', '1', 'Chicken 1.1kg-1.8kg', size='1.1kg-1.8kg')
        pricing.pricing.record_price(db, link, {'price':3.5})
        response = pricing.product_prices(1, db=db)[0]
        self.assertEqual(response['latest_check']['price'], 3.5)
        self.assertNotIn('price_quantity', response)

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

    def test_plain_page_has_only_checklist_controls(self):
        template = (ROOT / 'app/templates/shopping_lists.html').read_text()
        for absent in ('store-chip', 'pricing', 'historyChart', 'openPicker', 'unit_id', 'makeProduct'):
            self.assertNotIn(absent, template)
        for present in ('Got it', 'qty-stepper', 'type="checkbox"', 'newList()', 'renameList()', 'deleteList()'):
            self.assertIn(present, template)
        order = (ROOT / 'app/templates/shopping.html').read_text()
        self.assertNotIn('list-switcher', order)
        self.assertNotIn('Got it', order)
        self.assertIn('Add to list', order)

    def test_order_ignores_saved_plain_selection_and_moves(self):
        self.run_logic('shopping.html', r'''
const oops = e => {throw e;};
const calls = [];
const api = async (path, options) => {
  calls.push([path, options]);
  if (path === '/api/shopping/lists') return [{id:2,kind:'plain'}, {id:7,kind:'order'}];
  return [];
};
(async () => {
  const page = shoppingPage();
  page.list_id = 2;
  await page.init();
  assert.equal(page.list_id, 7);
  await page.moveItem({id:10, moveStore:'woolworths'}, {list_id:2});
  assert.ok(calls.some(([p,o]) => p === '/api/shopping/10/move?list_id=7' && o.method === 'POST' && o.body.list_id === 2));
})().catch(e => {console.error(e); process.exit(1);});
''')

    def test_live_suggestions_and_store_choice(self):
        self.run_logic('shopping.html', r'''
const oops = e => {throw e;};
const calls = [];
const api = async (path, options) => {
  calls.push([path, options]);
  if (path.includes('/pricing/search')) {
    const store = new URL(path, 'http://x').searchParams.get('store');
    return [{store_item_id: store + '-42', name: 'Salt', size: '500g', price: 2}];
  }
  return [];
};
(async () => {
  const page = shoppingPage();
  page.stores = [{store:'coles',enabled:1},{store:'woolworths',enabled:1}];
  page.text = 'salt';
  await page.suggestStore('salt', 0);
  assert.equal(calls.length, 2);
  assert.ok(calls.some(c => c[0].includes('store=woolworths')));
  assert.ok(calls.some(c => c[0].includes('store=coles')));
  assert.equal(page.liveSuggestions.length, 2);
  assert.ok(page.liveSuggestions.some(c => c.store === 'coles'));
  assert.ok(page.liveSuggestions.some(c => c.store === 'woolworths'));
  await page.suggestStore('salt', 0);
  assert.equal(calls.length, 2);
  page.resetSuggestions();
  await page.suggestStore('salt', 0);
  assert.equal(page.liveSuggestions.length, 0);
  page.liveSearchCount = 5;
  page.text = 'saffron';
  await page.suggestStore('saffron', page.suggestionVersion);
  assert.equal(calls.length, 2);
  assert.ok(page.suggestionMessage.includes('limit'));
  page.liveSearchCount = 0;
  page.stores[1].enabled = 0;
  await page.suggestStore('saffron', page.suggestionVersion);
  assert.equal(calls.length, 3);
  assert.ok(calls[2][0].includes('store=coles'));
  const item = {links:[{store:'coles'}, {store:'woolworths'}]};
  page.openMove(item);
  assert.equal(item.moveStore, null);
  assert.equal(item.moveOpen, true);
})().catch(e => {console.error(e); process.exit(1);});
''')

    def test_quantity_hint_requires_acceptance(self):
        self.run_logic('shopping.html', r'''
const oops = e => {throw e;};
const calls = [];
const api = async (path, options) => {
  calls.push([path, options]);
  if (options?.method === 'POST') return {id:4, product_id:2, label:'Water', amount:3, typical_amount:24, typical_unit_id:1, typical_unit:'bottle'};
  return {};
};
(async () => {
  const page = shoppingPage();
  page.list_id = 1; page.text = 'Water'; page.amount = 3;
  page.load = async () => {};
  await page.add();
  assert.equal(calls.length, 1);
  assert.equal(calls[0][1].body.amount, 3);
  assert.equal(page.quantityHint.typical_amount, 24);
  await page.acceptQuantityHint();
  assert.deepEqual(calls[1], ['/api/shopping/4?list_id=1', {method:'PUT', body:{amount:24, unit_id:1}}]);
  assert.equal(page.quantityHint, null);
})().catch(e => {console.error(e); process.exit(1);});
''')
