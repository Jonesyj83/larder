import json
import sqlite3
import unittest
from unittest.mock import patch

from app import pricing
from app.routes import pricing as routes
from test_pricing import prepare


class MatchReview(unittest.TestCase):
    def setUp(self):
        self.db = prepare(sqlite3.connect(':memory:'))
        self.addCleanup(self.db.close)
        self.db.execute("UPDATE products SET name='Grated Cheese' WHERE id=1")
        self.db.execute("UPDATE store_settings SET enabled=(store='coles')")
        self.tasty = dict(store_item_id='1', name='Grated Tasty Cheese', brand='', size='500g', price=3)
        self.parmesan = dict(self.tasty, store_item_id='2', name='Grated Parmesan Cheese', price=5)
        self.pool = [self.tasty, self.parmesan]
        for c in self.pool:
            self.db.execute('INSERT INTO coles_catalogue(coles_id,name,brand,size,price) VALUES(?,?,?,?,?)',
                            (c['store_item_id'], c['name'], c['brand'], c['size'], c['price']))
        self.db.commit()

    def test_explicit_size_brand_and_type_resolve_cleanly(self):
        wanted = dict(self.tasty, brand='Dairy', name='Dairy Grated Tasty Cheese')
        other = dict(self.parmesan, brand='Dairy', name='Dairy Grated Parmesan Cheese')
        selected = pricing.strongest_match('500g Dairy Grated Tasty Cheese', [wanted, other],
                                          preferred_type={'tasty'}, store='coles')
        self.assertEqual(selected, wanted)
        self.assertNotIn('_ambiguous', selected)

    def test_residual_choice_is_flagged_without_mutating_candidates(self):
        selected = pricing.strongest_match('Grated Cheese', self.pool, store='coles')
        self.assertEqual(selected, dict(self.tasty, _ambiguous=True))
        self.assertTrue(all('_ambiguous' not in c for c in self.pool))
        self.assertEqual(pricing.strongest_match('Grated Tasty Cheese', self.pool), self.tasty)

    def test_confirmation_and_replacement_clear_review_and_remember_choice(self):
        for candidate in self.pool:
            with self.subTest(choice=candidate['store_item_id']):
                self.db.execute('DELETE FROM product_store_links')
                pricing.auto_match(self.db, 1, 'Grated Cheese', search_fn=lambda *args: self.pool)
                link = self.db.execute('SELECT * FROM product_store_links').fetchone()
                self.assertEqual(link['needs_review'], 1)
                self.assertEqual(pricing.type_preference(self.db, 1, 'woolworths'), set())
                saved = routes.set_link(1, routes.LinkIn(store='coles', store_name=candidate['name'],
                                       **{k: v for k, v in candidate.items() if k not in ('name', 'brand')}),
                                       db=self.db, user={'id': 1})
                self.assertEqual(saved['needs_review'], 0)
                self.assertEqual(saved['matched_by'], 1)
                preference = pricing.type_preference(self.db, 1, 'woolworths')
                self.assertIn('tasty' if candidate is self.tasty else 'parmesan', preference)
                selected = pricing.strongest_match('Grated Cheese', self.pool, preferred_type=preference)
                self.assertEqual(selected, candidate)
                with patch.object(pricing, 'search', side_effect=AssertionError('confirmed store searched')):
                    self.assertEqual(pricing.auto_match(self.db, 1, 'Grated Cheese'), [])
                    self.assertEqual(pricing.auto_match_live(self.db, 1, 'Grated Cheese'), [])
                self.assertEqual(self.db.execute('SELECT needs_review FROM product_store_links').fetchone()[0], 0)

    def test_classification_resolves_cleanly_and_pending_choices_are_not_learned(self):
        water = dict(store_item_id='3', name='Aqua Water', brand='Aqua', size='1L',
                     classification={'primary': [['Drinks', 'Water']]})
        mixer = dict(water, store_item_id='4', classification={'primary': [['Drinks', 'Mixers']]})
        self.assertEqual(pricing.strongest_match('aqua water', [mixer, water]), water)
        self.db.execute('UPDATE coles_catalogue SET classification=? WHERE coles_id=1',
                        (json.dumps({'primary': [['Dairy', 'Cheese']]}),))
        pricing.link_product(self.db, 1, 'coles', '1', self.tasty['name'], needs_review=True)
        self.assertEqual(pricing.classification_preference(self.db, 1, 'woolworths'), set())
        pricing.link_product(self.db, 1, 'coles', '1', self.tasty['name'], matched_by=1)
        self.assertIn(frozenset({'cheese'}), pricing.classification_preference(self.db, 1, 'woolworths'))

    def test_live_matching_persists_review_flag(self):
        with patch.object(pricing, 'search', return_value=self.pool):
            pricing.auto_match_live(self.db, 1, 'Grated Cheese')
        self.assertEqual(self.db.execute('SELECT needs_review FROM product_store_links').fetchone()[0], 1)
