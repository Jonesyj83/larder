import json
import sqlite3
import unittest
import tempfile
from unittest.mock import Mock, patch
from pathlib import Path

from app import pricing, db as database
from app.pricing import stores
from app.routes.pricing import LinkIn
from test_pricing import prepare
import test_shopping_prices

FIXTURE = Path(__file__).parent / 'fixtures/weighed_products.json'


class WeighedPrices(unittest.TestCase):
    def test_selling_basis_survives_link_and_price_observation(self):
        db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(db.close)
        for store, raw in json.loads(FIXTURE.read_text()).items():
            basis = stores._weight_basis(store, raw)
            self.assertGreater(basis['per_kg'], 0)
            candidate = LinkIn(store=store, store_item_id='1', store_name='Meat',
                               price=raw.get('Price', raw.get('priceNumeric', 10)), price_basis=basis)
            link = pricing.link_product(db, 1, store, '1', 'Meat')
            pricing.record_price(db, link, candidate.model_dump())
            saved = db.execute('SELECT price_basis FROM store_price_checks WHERE link_id=?', (link,)).fetchone()[0]
            self.assertEqual(json.loads(saved), basis)
        self.assertEqual(stores._weight_basis('coles', {'pricing': {'unit': {'price': 19.9, 'isWeighted': False}}}), {})
        self.assertEqual(stores._weight_basis('woolworths', {'Unit': 'Each', 'CupMeasure': '1KG', 'CupPrice': 19.9}), {})

    def test_browse_crawl_retrieval_and_link_retain_selling_basis(self):
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
        from catalogue_crawl import crawl
        db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(db.close)
        raw = json.loads(FIXTURE.read_text())
        for store, product in raw.items():
            session = Mock()
            if store == 'woolworths':
                session.get.return_value.json.return_value = {'Bundles': [{'Products': [product]}], 'TotalRecordCount': 1}
                products, total = stores.browse_woolworths_category('deli', session=session)
            elif store == 'coles':
                product = dict(product, _type='PRODUCT')
                data = {'props': {'pageProps': {'searchResults': {'results': [product], 'noOfResults': 1}}}}
                session.get.return_value.text = '<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>'
                products, total = stores.browse_coles_category('meat', session=session)
            elif store == 'aldi':
                with patch.object(stores, 'payload', return_value={}), patch.object(stores, '_unflatten', return_value=[product]):
                    products, total = stores.browse_aldi_category('/meat', session=session)
            else:
                session.get.return_value.json.return_value = {'items': [product], 'total': 1}
                products, total = stores.browse_iga_category('test', 'meat', session=session)
                db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'test','')")
            basis = products[0]['price_basis']
            self.assertTrue(basis)
            crawl(db, catalogue_table=store + '_catalogue', history_table=store + '_catalogue_history',
                  progress_table=store + '_crawl_progress', id_field=pricing._CATALOGUE_TABLES[store][1],
                  categories=['meat'], fetch=lambda category, page: (products, total), log=lambda message: None)
            saved = db.execute(f'SELECT price_basis FROM {store}_catalogue_history').fetchone()[0]
            self.assertEqual(json.loads(saved), basis)
            candidates = pricing.search(store, products[0]['name'], conn=db)
            item = next(c for c in candidates if c['store_item_id'] == str(products[0][pricing._CATALOGUE_TABLES[store][1]]))
            self.assertEqual(item['price_basis'], basis)
            link = pricing.link_product(db, 1, store, item['store_item_id'], item['name'], size=item['size'])
            pricing.record_price(db, link, item)
            saved = db.execute('SELECT price_basis FROM store_price_checks WHERE link_id=?', (link,)).fetchone()[0]
            self.assertEqual(json.loads(saved), basis)

    def test_existing_price_rows_survive_repeatable_schema_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = prepare(sqlite3.connect(root / 'larder.db'))
            link = pricing.link_product(db, 1, 'woolworths', '130276', 'Leg Ham', size='per 100g')
            pricing.record_price(db, link, {'price': 19.9, 'unit_price': 19.9, 'unit_measure': '1KG'})
            tables = ['store_price_checks'] + [store + suffix for store in pricing._CATALOGUE_TABLES
                                               for suffix in ('_catalogue', '_catalogue_history')]
            for table in tables:
                db.execute(f'ALTER TABLE {table} DROP COLUMN price_basis')
            db.commit()
            db.close()
            with patch.multiple(database, DATA_DIR=root, DB_PATH=root / 'larder.db', UPLOAD_DIR=root / 'uploads'):
                for _ in range(2):
                    database.init_db()
                    db = database.connect()
                    for table in tables:
                        self.assertIn('price_basis', {r['name'] for r in db.execute(f'PRAGMA table_info({table})')})
                    self.assertEqual(db.execute('SELECT price FROM store_price_checks').fetchall()[0][0], 19.9)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                    db.close()

    def test_weight_rates_portions_and_totals(self):
        fixture = json.loads(FIXTURE.read_text())
        offers = []
        for store, raw in fixture.items():
            basis = stores._weight_basis(store, raw)
            price = {'woolworths': 19.9, 'coles': 13.6, 'aldi': 9.99, 'iga': 9.495}[store]
            offers.append(dict(store=store, size=raw.get('PackageSize', raw.get('size', raw.get('sellingSize', '500 g'))),
                               latest_check=dict(price=price, price_basis=json.dumps(basis))))
        check = test_shopping_prices.ShoppingPrices()
        check.run_logic('shopping.html', 'const offers = ' + json.dumps(offers) + r''';
const page = shoppingPage();
page.stores = offers.map(o => ({store:o.store, enabled:1}));
const ham = offers[0];
assert.equal(page.offerPrice(ham), '$19.90/kg');
assert.equal(page.offerSize(ham), '100g increments');
assert.equal(page.offerPrice(offers[1]), '$13.60 approx. · $8.50/kg');
assert.equal(page.offerPrice(offers[2]), '$9.99/kg');
assert.equal(page.offerPrice(offers[3]), '$9.49 approx. · $18.99/kg');
assert.equal(page.badgePrice({links:[ham]}, 'woolworths'), '$19.90/kg · 100g increments');
const near = (a,b) => assert.ok(Math.abs(a-b)<.00001, `${a} != ${b}`);
near(page.itemCost({amount:100,unit:'g'}, ham), 1.99);
near(page.itemCost({amount:.1,unit:'kg'}, ham), 1.99);
near(page.itemCost({amount:1,unit:'piece'}, ham), 1.99);
near(page.itemCost({amount:2,unit:'piece'}, ham), 3.98);
near(page.itemCost({amount:1}, offers[1]), 13.60);
near(page.itemCost({amount:100,unit:'g'}, offers[1]), .85);
near(page.itemCost({amount:1}, offers[2]), 14.97);
near(page.itemCost({amount:100,unit:'g'}, offers[2]), .999);
near(page.itemCost({amount:1}, offers[3]), 9.495);
near(page.itemCost({amount:100,unit:'g'}, offers[3]), 1.899);
page.items = [{product_id:1,amount:100,unit:'g',links:[ham]}];
near(page.splitTotal, 1.99);
near(page.storeTotals[0].total, 1.99);
near(page.bestSingleStore.total, 1.99);
const legacy = {store:'woolworths',size:'per 100g',latest_check:{price:19.9,unit_price:19.9,unit_measure:'1KG'}};
near(page.itemCost({amount:1},legacy),1.99);
assert.equal(page.offerPrice(legacy), '$19.90/kg');
const packed = {store:'woolworths',size:'100g',latest_check:{price:1.99,unit_price:19.9,unit_measure:'1KG'}};
near(page.itemCost({amount:2},packed),3.98);
assert.equal(page.offerPrice(packed),'$1.99');
const unknownWeight = {store:'coles',latest_check:{price:10,price_basis:{per_kg:10,rate_price:true}}};
assert.equal(page.itemCost({amount:1}, unknownWeight), null);
page.items = [{product_id:1,amount:1,links:[unknownWeight]}];
assert.equal(page.storeTotals.length, 0);
assert.equal(page.splitTotal, 0);
''')
