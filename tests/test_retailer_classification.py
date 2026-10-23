import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from app import db as database, pricing
from app.pricing import stores
from scripts import coles_catalogue_crawl, woolworths_catalogue_crawl, iga_catalogue_crawl


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/retailer_classification.json').read_text())


def browse(store, products):
    if store == 'coles':
        data = {'props': {'pageProps': {'searchResults': {'results': products, 'noOfResults': len(products)}}}}
        response = httpx.Response(200, text='<script id="__NEXT_DATA__">' + json.dumps(data) + '</script>')
    elif store == 'woolworths':
        response = httpx.Response(200, json={'Bundles': [{'Products': products}], 'TotalRecordCount': len(products)})
    else:
        response = httpx.Response(200, json={'items': products, 'total': len(products)})
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as session:
        if store == 'coles':
            return stores.browse_coles_category('drinks', session=session)[0]
        if store == 'woolworths':
            return stores.browse_woolworths_category('1_5AF3A0A', session=session)[0]
        return stores.browse_iga_category('48108', 'Drinks', session=session)[0]


def candidates(store):
    key = {'coles': 'coles_id', 'woolworths': 'stockcode', 'iga': 'item_id'}[store]
    return [dict(p, store_item_id=str(p[key])) for p in browse(store, FIXTURE[store])]


class RetailerClassification(unittest.TestCase):
    def test_browse_keeps_merchandise_paths_and_secondary_online_assignments(self):
        coles = candidates('coles')[0]['classification']
        self.assertEqual(coles['primary'], [['WATER', 'STILL WATER', 'MULTI PACK']])
        self.assertEqual(coles['secondary'], [['Water', 'Still Water'], ['Water', 'Sparkling Water']])
        self.assertEqual(coles['retailer']['onlineHeirs'][0]['aisleId'], '98635')
        woolworths = candidates('woolworths')[1]['classification']
        self.assertEqual(woolworths['primary'], [['CARBONATED SOFT DRINKS', 'SOFT DRINKS - MIXERS', 'SODA WATER']])
        self.assertEqual(woolworths['retailer']['sap']['sapsubcategoryno'], '93')
        self.assertEqual(woolworths['secondary'], [['Mixers']])
        iga = candidates('iga')[0]['classification']
        self.assertEqual(iga['primary'], [['Water', 'Still Water']])
        self.assertEqual(iga['retailer']['defaultCategory'][0]['categoryId'], '161adac3-b621-4ac9-8923-32009107cbb3')

    def test_missing_or_malformed_classification_does_not_drop_a_product(self):
        fields = {'coles': ['onlineHeirs', 'merchandiseHeir'],
                  'woolworths': ['AdditionalAttributes'], 'iga': ['categories', 'defaultCategory']}
        for store in fields:
            for value in (None, '', 42, [], [None], {'unexpected': None}):
                with self.subTest(store=store, value=value):
                    product = copy.deepcopy(FIXTURE[store][0])
                    product.update({field: value for field in fields[store]})
                    found = browse(store, [product])
                    self.assertEqual(len(found), 1)
                    self.assertEqual(found[0]['classification'], {})
        product = copy.deepcopy(FIXTURE['woolworths'][0])
        product['AdditionalAttributes'] = {'piessubcategorynamesjson': 'not json'}
        self.assertEqual(browse('woolworths', [product])[0]['classification'], {})

    def test_secondary_paths_are_available_when_primary_assignment_is_missing(self):
        for store, field in [('coles', 'merchandiseHeir'), ('iga', 'defaultCategory')]:
            product = copy.deepcopy(FIXTURE[store][0])
            product.pop(field)
            result = browse(store, [product])[0]['classification']
            self.assertTrue(pricing._classification_paths(result))
        product = copy.deepcopy(FIXTURE['woolworths'][0])
        product['AdditionalAttributes'] = {'piessubcategorynamesjson': '["Still Water", "Water"]'}
        self.assertEqual(pricing._classification_paths(browse('woolworths', [product])[0]['classification']),
                         [[{'still', 'water'}], [{'water'}]])

    def test_plain_water_rejects_mixer_classification_even_with_a_plain_title(self):
        for store in ('coles', 'woolworths', 'iga'):
            pool = candidates(store)[:2]
            for p in pool:
                p.update(name='Water', brand='', size='1L')
            pool[0]['price'], pool[1]['price'] = 3, 1
            with self.subTest(store=store):
                self.assertEqual(pricing.strongest_match('water', pool, store=store)['store_item_id'], pool[0]['store_item_id'])
                self.assertIsNone(pricing.strongest_match('water', pool[1:], store=store))

    def test_bare_category_uses_real_branches_before_plainest_title(self):
        spring, soda = candidates('woolworths')[:2]
        spring.update(name='Natural Water', brand='', size='1L', price=3)
        soda.update(name='Water', brand='', size='1L', price=1)
        self.assertEqual(pricing.strongest_match('water', [soda, spring], store='woolworths')['store_item_id'], spring['store_item_id'])

    def test_explicit_variant_overrides_query_and_remembered_classification_preferences(self):
        spring, soda, tonic = candidates('woolworths')[:3]
        remembered = {frozenset({'still', 'water'}), frozenset({'water'})}
        for query, wanted in [('soda water', soda), ('schweppes indian tonic water', tonic)]:
            self.assertEqual(pricing.strongest_match(query, [spring, soda, tonic], store='woolworths',
                             preferred_classification=remembered)['store_item_id'], wanted['store_item_id'])

    def test_missing_classification_preserves_existing_selection_and_partial_backfill(self):
        pool = candidates('woolworths')[:2]
        for p in pool:
            p.pop('classification')
        before = pricing.strongest_match('water', pool, store='woolworths')
        self.assertEqual(before['store_item_id'], pool[1]['store_item_id'])
        for p in pool:
            p['classification'] = {}
        self.assertEqual(pricing.strongest_match('water', pool, store='woolworths'), before | {'classification': {}})
        pool[0]['classification'] = candidates('woolworths')[0]['classification']
        self.assertEqual(pricing.strongest_match('water', pool, store='woolworths')['store_item_id'], before['store_item_id'])

    def test_noodle_segment_supplies_format_guard_when_title_omits_cup(self):
        packet, cup = candidates('woolworths')[3:5]
        cup['name'] = packet['name']
        cup['size'], cup['price'] = packet['size'], 0.1
        self.assertEqual(pricing.strongest_match('maggi chicken noodles', [cup, packet], store='woolworths')['store_item_id'], packet['store_item_id'])
        # A retailer classification adds protection; it does not replace the
        # title guard for rows that have not been crawled again yet.
        cup = candidates('woolworths')[4]
        cup.pop('classification')
        self.assertEqual(pricing.strongest_match('maggi chicken noodles', [cup, packet], store='woolworths')['store_item_id'], packet['store_item_id'])
        self.assertEqual(pricing.strongest_match('maggi chicken cup noodles', [cup, packet], store='woolworths')['store_item_id'], cup['store_item_id'])

    def test_shared_chip_flavour_classes_do_not_replace_explicit_words(self):
        sour, original = candidates('woolworths')[5:7]
        self.assertEqual(sour['classification']['primary'], original['classification']['primary'])
        self.assertEqual(pricing.strongest_match('pringles sour cream onion', [original, sour], store='woolworths')['store_item_id'], sour['store_item_id'])

    def test_shared_branch_does_not_make_a_named_chip_brand_lose_to_corn_chips(self):
        sour, original = candidates('woolworths')[5:7]
        corn = dict(original, store_item_id='340997', name='Woolworths Original Salted Corn Chips',
                    brand='Woolworths', classification={'primary': [['SNACKS', 'CHIPS - SHARING', 'CORN CHIPS']]})
        self.assertEqual(pricing.strongest_match('pringles chips', [corn, original, sour], store='woolworths')['store_item_id'], original['store_item_id'])

    def test_matching_uses_only_candidates_that_survive_typed_brand_and_size(self):
        still, other = candidates('iga')
        still.update(name='Aqua Pura Water', brand='Aqua Pura', size='1L',
                     classification={'primary': [['Water', 'Sparkling Water']]})
        other.update(name='Other Water', brand='Other', size='1L',
                     classification={'primary': [['Water', 'Still Water']]})
        preferred = {frozenset({'still', 'water'})}
        self.assertEqual(pricing.strongest_match('aqua pura water', [other, still], preferred_classification=preferred)['store_item_id'], still['store_item_id'])
        other.update(name=still['name'], brand=still['brand'], size='10L')
        self.assertEqual(pricing.strongest_match('1l aqua pura water', [other, still], preferred_classification=preferred)['store_item_id'], still['store_item_id'])

    def test_frozen_dishes_share_classification_and_still_require_dish_words(self):
        spaghetti, lasagne = candidates('woolworths')[7:9]
        self.assertEqual(spaghetti['classification']['primary'], lasagne['classification']['primary'])
        self.assertEqual(pricing.strongest_match('beef lasagne', [spaghetti, lasagne], store='woolworths')['store_item_id'], lasagne['store_item_id'])

    def test_marketing_tags_and_invalid_saved_json_do_not_invent_a_variant(self):
        cola = {'store_item_id': '1', 'name': 'Cola', 'brand': '', 'price': 1,
                'classification': {'secondary': [['Gluten Free']]}}
        self.assertEqual(pricing.strongest_match('cola', [cola])['store_item_id'], '1')
        for value in ('invalid', None, 'null', '[]', {'primary': [None, 42]}):
            cola['classification'] = value
            self.assertEqual(pricing.strongest_match('cola', [cola])['store_item_id'], '1')

    def test_remembered_classification_is_read_from_other_store_catalogues(self):
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        db.executescript(database.SCHEMA)
        db.execute("INSERT INTO units(id,name,name_plural) VALUES(1,'piece','pieces')")
        db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'water',1)")
        for store, p, key in [('coles', candidates('coles')[0], 'coles_id'), ('iga', candidates('iga')[0], 'item_id')]:
            db.execute(f'INSERT INTO {store}_catalogue({key},name,classification) VALUES(?,?,?)',
                       (p['store_item_id'], p['name'], json.dumps(p['classification'])))
            db.execute('INSERT INTO product_store_links(product_id,store,store_item_id,store_name) VALUES(1,?,?,?)',
                       (store, p['store_item_id'], p['name']))
        preferred = pricing.classification_preference(db, 1, 'woolworths')
        self.assertIn(frozenset({'still', 'water'}), preferred)
        still, sparkling = candidates('iga')
        sparkling.update(name=still['name'], brand=still['brand'], size=still['size'], price=0.1,
                         classification={'primary': [['Water', 'Sparkling Water']]})
        self.assertEqual(pricing.strongest_match('water', [sparkling, still], preferred_classification=preferred)['store_item_id'], still['store_item_id'])
        db.execute("UPDATE iga_catalogue SET classification=?", (json.dumps({'primary': [['Water', 'Sparkling Water']]}),))
        self.assertNotIn(frozenset({'still', 'water'}), pricing.classification_preference(db, 1, 'woolworths'))

    def test_upsert_backfills_existing_ids_and_replaces_classification_on_next_pass(self):
        for store, module, key in [('coles', coles_catalogue_crawl, 'coles_id'),
                                   ('woolworths', woolworths_catalogue_crawl, 'stockcode'),
                                   ('iga', iga_catalogue_crawl, 'item_id')]:
            with self.subTest(store=store):
                db = sqlite3.connect(':memory:')
                db.row_factory = sqlite3.Row
                self.addCleanup(db.close)
                db.executescript(database.SCHEMA)
                db.execute("INSERT INTO store_settings(store,postcode) VALUES('iga','Piara Waters')")
                product = browse(store, FIXTURE[store][:1])[0]
                db.execute(f'INSERT INTO {store}_catalogue({key},name,category) VALUES(?,?,?)', (product[key], 'Old name', 'old bucket'))
                def run(p):
                    db.execute(f'DELETE FROM {store}_crawl_progress')
                    return module.crawl(db, categories=['drinks'], fetch=lambda category, page: ([p], 1),
                                        sleep=lambda _: None, log=lambda *args: None)
                run(product)
                row = db.execute(f'SELECT * FROM {store}_catalogue').fetchone()
                self.assertEqual(json.loads(row['classification']), product['classification'])
                self.assertEqual(row['category'], 'drinks')
                found = pricing.search(store, 'water', 'Piara Waters', conn=db)
                if store == 'iga':
                    db.execute("INSERT INTO iga_catalogue_store(id,store_id,location) VALUES(1,'48108','piara waters')")
                    found = pricing.search(store, 'water', 'Piara Waters', conn=db)
                self.assertEqual(found[0]['classification'], product['classification'])
                product['classification'] = {'primary': [['Water', 'Sparkling Water']]}
                run(product)
                self.assertEqual(db.execute(f'SELECT count(*) FROM {store}_catalogue').fetchone()[0], 1)
                self.assertEqual(json.loads(db.execute(f'SELECT classification FROM {store}_catalogue').fetchone()[0]), product['classification'])
                self.assertEqual(db.execute(f'SELECT count(*) FROM {store}_catalogue_history').fetchone()[0], 2)
                product.pop('classification')
                run(product)
                self.assertEqual(db.execute(f'SELECT classification FROM {store}_catalogue').fetchone()[0], '{}')

    def test_additive_migration_preserves_rows_and_resets_old_pass_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(database, 'DATA_DIR', root), patch.object(database, 'UPLOAD_DIR', root / 'uploads'), \
                    patch.object(database, 'DB_PATH', root / 'larder.db'):
                db = database.connect()
                db.executescript(database.SCHEMA.replace("    classification TEXT NOT NULL DEFAULT '{}',\n", ''))
                for store, key in [('coles', 'coles_id'), ('woolworths', 'stockcode'), ('iga', 'item_id')]:
                    db.execute(f"INSERT INTO {store}_catalogue({key},name,price,category) VALUES(1,'Water',3,'drinks')")
                    db.execute(f"INSERT INTO {store}_catalogue_history({key},price) VALUES(1,3)")
                    db.execute(f"INSERT INTO {store}_crawl_progress(category,next_page,done) VALUES('drinks',9,1)")
                db.commit()
                db.close()
                database.init_db()
                db = database.connect()
                for store in ('coles', 'woolworths', 'iga'):
                    row = db.execute(f'SELECT * FROM {store}_catalogue').fetchone()
                    self.assertEqual((row['name'], row['price'], row['category'], row['classification']), ('Water', 3, 'drinks', '{}'))
                    self.assertEqual(db.execute(f'SELECT count(*) FROM {store}_catalogue_history').fetchone()[0], 1)
                    self.assertEqual(tuple(db.execute(f'SELECT next_page,done FROM {store}_crawl_progress').fetchone()), (1, 0))
                    db.execute(f'UPDATE {store}_crawl_progress SET next_page=4')
                db.commit()
                db.close()
                database.init_db()
                db = database.connect()
                self.assertEqual(db.execute('SELECT next_page FROM coles_crawl_progress').fetchone()[0], 4)
                db.close()
