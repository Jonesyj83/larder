import sqlite3
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from app import db as database, order_history, pricing
from app.routes import shopping


class OrderHistory(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(database.SCHEMA)
        self.db.executescript("""
            INSERT INTO units(id,name) VALUES(1,'bottle');
            INSERT INTO products(id,name,unit_id) VALUES(1,'Water',1);
            INSERT INTO shopping_lists(id,name,kind) VALUES(1,'Order','order'),(2,'Home','plain');
            INSERT INTO store_settings(store) VALUES('coles');
            INSERT INTO product_store_links(product_id,store,store_item_id,store_name,size)
                VALUES(1,'coles','24','Water','24x600mL');
        """)

    def add(self, amount=1):
        return shopping.add(shopping.ItemIn(name='Water', amount=amount), db=self.db,
                            user={'id': None}, list_id=1)

    def test_move_completion_and_hint(self):
        item = self.add(24)
        shopping.move(item['id'], shopping.MoveIn(store='coles', list_id=2),
                      db=self.db, user={'id': None}, list_id=1)
        history = shopping.history(1, db=self.db, user={})
        self.assertEqual((history[0]['amount'], history[0]['size'], history[0]['store']),
                         (24, '24x600mL', 'coles'))
        item = self.add(3)
        self.assertEqual(item['amount'], 3)
        self.assertEqual(item['typical_amount'], 24)
        self.assertEqual(item['typical_unit_id'], 1)
        shopping.edit(item['id'], shopping.ItemEdit(done=True, amount=2), db=self.db, user={}, list_id=1)
        shopping.edit(item['id'], shopping.ItemEdit(done=True), db=self.db, user={}, list_id=1)
        history = shopping.history(1, db=self.db, user={})
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['amount'], 2)
        self.assertIsNone(history[0]['store'])

    def test_history_failure_keeps_completion(self):
        item = self.add()
        self.db.execute("""CREATE TRIGGER reject_history BEFORE INSERT ON order_history
            BEGIN SELECT RAISE(ABORT, 'unavailable'); END""")
        self.db.commit()
        with self.assertLogs('app.order_history', level='WARNING'):
            shopping.edit(item['id'], shopping.ItemEdit(done=True), db=self.db, user={}, list_id=1)
        self.assertEqual(self.db.execute('SELECT done FROM shopping_list').fetchone()[0], 1)

    def test_recent_mode_and_store_size(self):
        for amount in [100] * 30 + [24] * 12 + [1] * 8:
            self.db.execute('INSERT INTO order_history(product_id,amount,unit_id) VALUES(1,?,1)', (amount,))
        self.assertEqual(order_history.typical(order_history.recent(self.db, 1))['amount'], 24)
        self.assertEqual(pricing.size_preference(self.db, 1, 'coles'), {'count': 24})
        self.db.execute("INSERT INTO order_history(product_id,amount,unit_id,store,size) VALUES(1,1,1,'coles','6x600mL')")
        self.assertEqual(pricing.size_preference(self.db, 1, 'coles')['count'], 6)

    def test_size_boost_and_name_floor(self):
        candidates = [dict(store_item_id='1', name='Water', size='600mL'),
                      dict(store_item_id='24', name='Water', size='24x600mL')]
        self.assertEqual(pricing.strongest_match('Water', candidates)['store_item_id'], '1')
        self.assertIsNone(pricing.strongest_match('Water', [candidates[0],
            dict(store_item_id='2', name='Water', size='1L')]))
        self.assertEqual(pricing.strongest_match('Water', candidates, {'count': 24})['store_item_id'], '24')
        self.assertIsNone(pricing.strongest_match('Water', [dict(store_item_id='x', name='Orange juice', size='24 pack')], {'count': 24}))
        self.db.execute('DELETE FROM product_store_links')
        self.db.execute('INSERT INTO order_history(product_id,amount,unit_id) VALUES(1,24,1)')
        self.db.commit()
        pricing.auto_match(self.db, 1, 'Water', search_fn=lambda *args: candidates)
        self.assertEqual(self.db.execute('SELECT store_item_id FROM product_store_links').fetchone()[0], '24')

    def test_bought_records_before_deletion(self):
        item = self.add(2)
        shopping.bought(item['id'], shopping.BoughtIn(store='coles'), db=self.db, user={'id': None}, list_id=1)
        self.assertEqual(shopping.history(1, db=self.db, user={})[0]['size'], '24x600mL')
        self.assertIsNone(self.db.execute('SELECT 1 FROM shopping_list').fetchone())

    def test_migration_backfills_once_and_preserves_rows(self):
        with tempfile.TemporaryDirectory() as scratch:
            path = Path(scratch)
            with patch.object(database, 'DATA_DIR', path), patch.object(database, 'DB_PATH', path / 'larder.db'), patch.object(database, 'UPLOAD_DIR', path / 'uploads'):
                database.init_db()
                conn = database.connect()
                conn.execute("DELETE FROM schema_migrations WHERE name='order_history_backfill'")
                conn.execute("INSERT INTO products(id,name,unit_id) SELECT 1,'Water',id FROM units LIMIT 1")
                list_id = conn.execute('SELECT id FROM shopping_lists LIMIT 1').fetchone()[0]
                conn.execute('INSERT INTO shopping_list(list_id,product_id,amount,done) VALUES(?,1,24,1)', (list_id,))
                conn.execute('INSERT INTO shopping_list(list_id,product_id,amount,done) VALUES(?,1,3,0)', (list_id,))
                conn.commit()
                conn.close()
                database.init_db()
                database.init_db()
                conn = database.connect()
                self.assertEqual(conn.execute('SELECT amount,store,size FROM order_history').fetchall()[0][:], (24, None, ''))
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM order_history').fetchone()[0], 1)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM shopping_list').fetchone()[0], 2)
                conn.execute('DELETE FROM order_history')
                conn.commit()
                conn.close()
                database.init_db()
                conn = database.connect()
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM order_history').fetchone()[0], 0)
                self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                conn.close()
