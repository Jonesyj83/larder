"""Execute Scan's actual JavaScript, then persist its requests in scratch SQLite."""
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / 'app/templates/scan.html').read_text().split('<script>')[1].split('</script>')[0]
PRELUDE = """
const assert = require('node:assert/strict');
const localStorage = {getItem: () => null, setItem: () => {}};
const window = {isSecureContext: true};
const navigator = {mediaDevices: {getUserMedia: async () => {throw Error('unexpected camera');}}};
const errors = [], calls = [];
const oops = e => errors.push(e.message), toast = () => {};
const pic = x => x || '', qty = () => '', num = String;
const today = () => '2026-10-11', addDays = () => '2026-10-18';
let failPurchase = false;
async function api(path, opts = {}) {
  calls.push({path, body: opts.body});
  if (path.startsWith('/api/scan/')) return {found: false, off: null, matches: []};
  if (path === '/api/products') return {...opts.body, id: 17, unit: 'piece', unit_plural: 'pieces'};
  if (path === '/api/stock/purchase') {
    if (failPurchase) throw Error('purchase failed');
    return {added: 12};
  }
  if (path === '/api/products/9/barcodes') return {id: 9, name: 'Linked', unit_id: 1};
  if (path === '/api/products/9/picture-url') return {id: 9, name: 'Linked', unit_id: 1, picture: 'saved.jpg'};
  if (path === '/api/stock/consume') return {removed: 1, in_stock: 11};
  throw Error(path);
}
async function fresh() {
  const s = scanPage(); s.units = [{id: 1, name: 'piece'}];
  await s.lookup('123456789'); calls.length = 0;
  Object.assign(s.np, {name: 'Packet', unit_id: 1, purchase_unit_id: 2,
    purchase_factor: 6, location_id: 1, shelf_life_kind: 'use_by', default_days: 7});
  s.amount = 2; s.expires_on = '2026-11-03'; s.price = '4.50';
  return s;
}
"""


class ScanRegression(unittest.TestCase):
    def run_js(self, body):
        result = subprocess.run(['node'], input=PRELUDE + SCRIPT +
            '\n(async () => {' + body + '})().catch(e => {console.error(e); process.exit(1)});',
            text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_create_and_purchase_packet(self):
        output = self.run_js("""
          const s = await fresh(); await s.create();
          assert.deepEqual(calls.map(c => c.path), ['/api/products', '/api/stock/purchase']);
          const b = calls[1].body;
          assert.equal(b.amount, 2); assert.equal(b.unit_id, 2);
          assert.equal(b.expires_on, '2026-11-03'); assert.equal(b.price, 4.5);
          assert.equal(b.location_id, 1); assert.equal(s.unknown, null);
          console.log(JSON.stringify(calls));
        """)
        from app.db import SCHEMA
        from app.routes.products import ProductIn, add_product
        from app.routes.stock import PurchaseIn, purchase
        calls = json.loads(output)
        with tempfile.TemporaryDirectory(prefix='larder-scan-') as scratch:
            db = sqlite3.connect(str(Path(scratch) / 'test.db'))
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys = ON')
            db.executescript(SCHEMA)
            db.executemany('INSERT INTO units(id,name) VALUES (?,?)', [(1, 'piece'), (2, 'pack')])
            db.execute("INSERT INTO locations(id,name) VALUES (1,'Pantry')")
            db.execute("INSERT INTO users(id,username,password_hash) VALUES (1,'scratch','unused')")
            p = add_product(ProductIn(**calls[0]['body']), db)
            body = dict(calls[1]['body'], product_id=p['id'])
            self.assertEqual(purchase(PurchaseIn(**body), db, {'id': 1})['added'], 12)
            entry = dict(db.execute('SELECT * FROM stock').fetchone())
            self.assertEqual((entry['expires_on'], entry['price'], entry['location_id']),
                             ('2026-11-03', 4.5, 1))
            self.assertEqual(p['shelf_life_kind'], 'use_by')
            self.assertEqual(p['barcodes'][0]['code'], '123456789')
            db.close()

    def test_purchase_fields_and_advanced_setting(self):
        from html.parser import HTMLParser

        class FormFields(HTMLParser):
            def __init__(self):
                super().__init__()
                self.in_create = False
                self.advanced = False
                self.fields = {}

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'form':
                    self.in_create = attrs.get('@submit.prevent') == 'create()'
                if not self.in_create:
                    return
                if tag == 'details':
                    self.advanced = True
                    self.assert_closed = 'open' not in attrs
                model = attrs.get('x-model', attrs.get('x-model.number'))
                if model:
                    self.fields[model] = (tag, attrs, self.advanced)

            def handle_endtag(self, tag):
                if tag == 'form':
                    self.in_create = False
                if tag == 'details':
                    self.advanced = False

        parsed = FormFields()
        from jinja2 import Environment, FileSystemLoader
        from types import SimpleNamespace
        html = Environment(loader=FileSystemLoader(ROOT / 'app/templates')).get_template('scan.html').render(
            user={'id':1,'username':'test','portion_size':'regular','is_admin':True},
            request=SimpleNamespace(url=SimpleNamespace(path='/scan')), base_path='', title='Scan')
        parsed.feed(html)
        for model in ('amount', 'expires_on', 'price', 'np.location_id'):
            self.assertIn(model, parsed.fields)
            self.assertFalse(parsed.fields[model][2])
        self.assertEqual(parsed.fields['expires_on'][1]['x-modelable'], 'iso')
        self.assertIn('data-date-input type="text"', html)
        self.assertTrue(parsed.fields['np.default_days'][2])
        self.assertTrue(parsed.assert_closed)
        self.assertIn('shelfLabel(np.shelf_life_kind)',
                      (ROOT / 'app/templates/scan.html').read_text())

    def test_retry_purchase_without_duplicate_product(self):
        self.run_js("""
          const s = await fresh(); failPurchase = true; await s.create();
          assert.equal(s.busy, false); assert.ok(s.unknown);
          failPurchase = false; await s.create();
          assert.equal(calls.filter(c => c.path === '/api/products').length, 1);
          assert.equal(calls.filter(c => c.path === '/api/stock/purchase').length, 2);
          assert.deepEqual(calls[1].body, calls[2].body);
          assert.equal(s.unknown, null);
        """)

    def test_unknown_consume_does_not_create(self):
        self.run_js("const s = await fresh(); s.setMode('consume'); await s.create(); assert.equal(calls.length, 0);")

    def test_camera_guard_both_engines(self):
        self.run_js("""
          for (const engine of ['native', 'zxing']) {
            for (const [secure, media] of [[false, {getUserMedia: () => {throw Error('must not open camera');}}], [true, undefined], [true, {}]]) {
              window.isSecureContext = secure; navigator.mediaDevices = media;
              const s = scanPage(); s.engine = engine;
              s.startZxing = () => {throw Error('must not load scanner');};
              await s.start(); assert.match(s.camErr, /HTTPS/); assert.match(s.camErr, /manual|typ/i);
              assert.equal(s.camOn, false); assert.equal(s.scanning, false);
            }
          }
        """)

    def test_picture_link_and_cancel(self):
        self.run_js("""
          const s = await fresh();
          s.unknown.off = {image_url: 'https://example.invalid/packet.jpg'};
          s.np.use_pic = true; failPurchase = true; await s.create();
          assert.equal(calls[0].body.picture_url, s.unknown.off.image_url);
          s.reset(false); assert.equal(s.createdProduct, null);
          assert.equal(s.expires_on, ''); assert.equal(s.price, '');
          assert.equal(s.amount, 1);
          await s.lookup('another/barcode'); calls.length = 0;
          s.mode = 'consume'; s.unknown.off = {image_url: 'https://example.invalid/packet.jpg'};
          await s.link(9);
          assert.deepEqual(calls.map(c => c.path), ['/api/products/9/barcodes', '/api/products/9/picture-url']);
          assert.equal(calls[0].body.code, 'another/barcode');
          assert.equal(s.product.id, 9); assert.equal(s.picture, 'saved.jpg');
          assert.equal(s.unknown, null);
        """)

    def test_known_purchase_conversion_defaults(self):
        self.run_js("""
          const s = await fresh();
          s.showProduct({id: 9, unit_id: 1, purchase_unit_id: 2, default_days: 7}, null);
          assert.equal(s.amount, 1); assert.equal(s.unit_id, 2);
          assert.equal(s.expires_on, '2026-10-18');
          await s.submit(); assert.equal(calls[0].path, '/api/stock/purchase');
          assert.equal(calls[0].body.unit_id, 2);
          assert.equal(calls[0].body.expires_on, '2026-10-18');
        """)

    def test_known_consume_spoiled(self):
        self.run_js("""
          const s = await fresh(); s.showProduct({id: 9, unit_id: 1}, 3);
          s.setMode('consume'); s.spoiled = true; await s.submit();
          assert.equal(calls[0].path, '/api/stock/consume');
          assert.equal(calls[0].body.spoiled, true); assert.equal(calls[0].body.amount, 3);
        """)

    def test_malformed_lookup_reply_is_an_unknown_barcode(self):
        from unittest.mock import patch
        from app.routes.scan import lookup_off
        import httpx
        for reply in ([], None, {'status':1,'product':[]},
                      {'status':1,'product':{'product_name':42}},
                      {'status':1,'product':{'brands':['Brand']}}):
            with self.subTest(reply=reply), patch('app.routes.scan.httpx.get',
                    return_value=httpx.Response(200, json=reply)):
                self.assertIsNone(lookup_off('123456789'))

if __name__ == '__main__':
    unittest.main()
