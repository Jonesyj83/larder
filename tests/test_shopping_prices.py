import re
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
  if (path.endsWith('/link')) { assert.deepEqual(options.body, {store:'coles', store_item_id:'123', store_name:'Milk 2L'}); linked = true; return {}; }
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
