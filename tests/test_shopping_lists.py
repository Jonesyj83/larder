import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from test_hardening import MemoryDB, USER, product
from app.routes import shopping as shop


class ShoppingLists(MemoryDB):
    def test_lists_and_scoped_items(self):
        default = shop.lists(self.db, USER)[0]['id']
        work = shop.create_list(shop.ListIn(name='Work'), self.db, USER)['id']
        self.assertEqual(shop.rename_list(work, shop.ListIn(name='Picnic'), self.db, USER)['name'], 'Picnic')
        pid = product(self.db, 'Bread', 'piece', min_stock=3)
        a = shop.add(shop.ItemIn(product_id=pid), self.db, USER)
        b = shop.add(shop.ItemIn(product_id=pid, amount=2), self.db, USER, list_id=work)
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(shop.listed_amount(self.db, pid), 1)
        self.assertEqual(shop.listed_amount(self.db, pid, work), 2)
        self.assertEqual([i['id'] for i in shop.list_items(self.db, USER)], [a['id']])
        for action in (
            lambda: shop.edit(b['id'], shop.ItemEdit(done=True), self.db, USER),
            lambda: shop.bought(b['id'], None, self.db, USER),
            lambda: shop.make_product(b['id'], shop.MakeProductIn(), self.db, USER),
        ):
            with self.assertRaises(HTTPException) as error:
                action()
            self.assertEqual(error.exception.status_code, 404)
            self.db.rollback()
        shop.remove(b['id'], self.db, USER)
        self.assertEqual(len(shop.list_items(self.db, USER, list_id=work)), 1)
        shop.edit(b['id'], shop.ItemEdit(done=True), self.db, USER, list_id=work)
        self.assertEqual(next(l for l in shop.lists(self.db, USER) if l['id']==work)['item_count'], 0)
        shop.clear_done(self.db, USER)
        self.assertEqual(len(shop.list_items(self.db, USER, list_id=work)), 1)
        shop.clear_done(self.db, USER, list_id=work)
        shop.add_missing(self.db, USER, list_id=work)
        self.assertEqual(len(shop.list_items(self.db, USER, list_id=work)), 1)
        other = shop.create_list(shop.ListIn(name='Other'), self.db, USER)['id']
        ids = [i['id'] for i in shop.list_items(self.db, USER, list_id=work)]
        shop.delete_list(work, self.db, USER)
        self.assertTrue(set(ids).issubset({i['id'] for i in shop.list_items(self.db, USER, list_id=other)}))
        with self.assertRaises(HTTPException) as error:
            shop.delete_list(default, self.db, USER)
        self.assertEqual(error.exception.status_code, 409)
        self.db.rollback()
        with self.assertRaises(HTTPException):
            shop.add(shop.ItemIn(name='Invalid'), self.db, USER, list_id=999)

    def test_free_text_product_defaults_and_existing_name(self):
        pid = product(self.db, 'Milk', 'ml', purchase_unit='pack', factor=2000)
        loose = shop.add(shop.ItemIn(name='milk', amount=2), self.db, USER)
        made = shop.make_product(loose['id'], shop.MakeProductIn(), self.db, USER)
        self.assertEqual(made['product']['id'], pid)
        self.assertEqual(made['product']['in_stock'], 4000)
        for name in ('Paper towels', 'Soap'):
            loose = shop.add(shop.ItemIn(name=name, amount=2), self.db, USER)
            made = shop.make_product(loose['id'], None, self.db, USER)
            unit = self.db.execute('SELECT name FROM units WHERE id=?', (made['product']['unit_id'],)).fetchone()[0]
            self.assertIn(unit, ('each', 'piece'))
            self.assertEqual(made['product']['in_stock'], 2)
        self.assertEqual(shop.list_items(self.db, USER), [])

    def test_migration_of_copied_old_database(self):
        from app import db as module
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = sqlite3.connect(root/'original.db')
            original.executescript("""CREATE TABLE shopping_list (
                id INTEGER PRIMARY KEY, product_id INTEGER, name TEXT NOT NULL DEFAULT '',
                amount REAL NOT NULL DEFAULT 1, unit_id INTEGER, note TEXT NOT NULL DEFAULT '',
                done INTEGER NOT NULL DEFAULT 0, added_by INTEGER,
                created_at TEXT NOT NULL DEFAULT (datetime('now')));
                INSERT INTO shopping_list(id,name,amount,note,done) VALUES
                (7,'Bread',2,'Keep me',0),(12,'Milk',3,'Also keep',1);""")
            before = original.execute('SELECT * FROM shopping_list ORDER BY id').fetchall()
            copied = sqlite3.connect(root/'larder.db')
            original.backup(copied)
            copied.close()
            with patch.multiple(module, DATA_DIR=root, DB_PATH=root/'larder.db', UPLOAD_DIR=root/'uploads'):
                module.init_db()
                module.init_db()
                db = module.connect()
                after = db.execute('SELECT * FROM shopping_list ORDER BY id').fetchall()
                self.assertEqual([tuple(r)[:-1] for r in after], before)
                self.assertEqual(len({r['list_id'] for r in after}), 1)
                self.assertEqual(tuple(db.execute('SELECT name,kind FROM shopping_lists').fetchone()), ('Shopping', 'order'))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM shopping_list WHERE list_id IS NULL').fetchone()[0], 0)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('UPDATE shopping_list SET list_id=NULL WHERE id=7')
                db.close()
            self.assertEqual(original.execute('SELECT * FROM shopping_list ORDER BY id').fetchall(), before)
            original.close()

    def test_move_existing_and_named_lists(self):
        pid = product(self.db, 'Milk', 'ml', purchase_unit='pack', factor=1000)
        order = shop.resolve_list(self.db)
        target = shop.create_list(shop.ListIn(name='Weekend'), self.db, USER)['id']
        for body in (shop.MoveIn(list_id=target), shop.MoveIn(name='New list'), shop.MoveIn(name='weekend')):
            item = shop.add(shop.ItemIn(product_id=pid, amount=3), self.db, USER)
            moved = shop.move(item['id'], body, self.db, USER)
            self.assertNotEqual(moved['list_id'], order)
            self.assertEqual(moved['product_id'], pid)
            self.assertEqual(moved['amount'], 3)
            self.assertEqual(shop.list_items(self.db, USER), [])
            self.assertIn(item['id'], [i['id'] for i in shop.list_items(self.db, USER, moved['list_id'])])
        self.assertEqual(len(shop.lists(self.db, USER, kind='plain')), 2)
        result = shop.bought(item['id'], None, self.db, USER, moved['list_id'])
        self.assertIsNotNone(result['stock'])
        self.assertEqual(self.db.execute('SELECT SUM(amount) FROM stock WHERE product_id=?', (pid,)).fetchone()[0], 3000)

    def test_kind_migration_existing_lists(self):
        from app import db as module
        for multiple in (False, True):
            with self.subTest(multiple=multiple), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                db = sqlite3.connect(root/'larder.db')
                db.executescript("""CREATE TABLE shopping_lists (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL,
                    created_by INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now')));
                    INSERT INTO shopping_lists(id,name) VALUES(1,'Original');""")
                db.executescript(module.SCHEMA)
                db.execute("INSERT INTO shopping_list(list_id,name) VALUES(1,'Bread')")
                if multiple:
                    db.execute("INSERT INTO shopping_lists(id,name) VALUES(2,'Busy')")
                    db.executemany("INSERT INTO shopping_list(list_id,name) VALUES(2,?)", [('Milk',), ('Tea',)])
                db.commit()
                db.close()
                with patch.multiple(module, DATA_DIR=root, DB_PATH=root/'larder.db', UPLOAD_DIR=root/'uploads'):
                    module.init_db()
                    module.init_db()
                    db = module.connect()
                    self.assertEqual([r['id'] for r in db.execute("SELECT id FROM shopping_lists WHERE kind='order'")], [2 if multiple else 1])
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM shopping_lists').fetchone()[0], 2 if multiple else 1)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM shopping_list').fetchone()[0], 3 if multiple else 1)
                    if multiple:
                        self.assertEqual(db.execute('SELECT kind FROM shopping_lists WHERE id=1').fetchone()[0], 'plain')
                    db.close()

    def test_move_validation_preserves_source(self):
        item = shop.add(shop.ItemIn(name='Bread', amount=2), self.db, USER)
        for body in (shop.MoveIn(), shop.MoveIn(name=' '), shop.MoveIn(list_id=999),
                     shop.MoveIn(list_id=shop.resolve_list(self.db))):
            with self.assertRaises(HTTPException):
                shop.move(item['id'], body, self.db, USER)
            self.db.rollback()
            self.assertEqual([i['id'] for i in shop.list_items(self.db, USER)], [item['id']])
