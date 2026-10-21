import sqlite3
import unittest

from app import stock as st
from app.db import BUILTIN_CONVERSIONS, SCHEMA

UNITS = ['piece', 'pack', 'g', 'kg']


def prepare(db):
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    db.execute("INSERT INTO users(id,username,password_hash,portion_size) VALUES(1,'test','unused','regular')")
    db.executemany('INSERT INTO units(id,name) VALUES(?,?)', list(enumerate(UNITS, 1)))
    ids = {name: i for i, name in enumerate(UNITS, 1)}
    db.executemany('INSERT OR IGNORE INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(?,?,?)',
                    [(ids[a], ids[b], f) for a, b, f in BUILTIN_CONVERSIONS if a in ids and b in ids])
    db.execute("INSERT INTO locations(id,name) VALUES(1,'Pantry')")
    db.commit()
    return db


def product(db, name, unit_id):
    cur = db.execute("INSERT INTO products(name, unit_id) VALUES (?, ?)", (name, unit_id))
    db.commit()
    return cur.lastrowid


class ReceiptLinking(unittest.TestCase):
    def setUp(self):
        self.db = prepare(sqlite3.connect(':memory:'))

    def tearDown(self):
        self.db.close()

    def test_purchase_can_attach_to_a_receipt_and_the_receipt_keeps_its_own_total(self):
        cur = self.db.execute(
            "INSERT INTO receipts (store, purchased_on, total, note, added_by) VALUES (?, ?, ?, ?, ?)",
            ('Woolworths', '2026-10-21', 127.30, 'Harrisdale eReceipt', 1))
        self.db.commit()
        receipt_id = cur.lastrowid

        pid1 = product(self.db, 'Milk 2L', 1)
        pid2 = product(self.db, 'Bread', 1)
        st.purchase(self.db, pid1, 1, price=3.40, user_id=1, receipt_id=receipt_id)
        st.purchase(self.db, pid2, 1, price=4.00, user_id=1, receipt_id=receipt_id)
        # a purchase with no receipt_id must still work exactly as before
        st.purchase(self.db, pid1, 1, price=3.40, user_id=1)

        linked = self.db.execute(
            "SELECT COUNT(*) FROM stock_log WHERE receipt_id = ?", (receipt_id,)).fetchone()[0]
        self.assertEqual(linked, 2)
        items_total = self.db.execute(
            "SELECT SUM(price) FROM stock_log WHERE receipt_id = ?", (receipt_id,)).fetchone()[0]
        self.assertAlmostEqual(items_total, 7.40)
        receipt = self.db.execute("SELECT total FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        # the receipt's own total (with whole-receipt discounts) can differ from
        # the sum of its line items - both are preserved, neither overwrites the other
        self.assertAlmostEqual(receipt['total'], 127.30)

    def test_purchase_rejects_an_unknown_receipt_id(self):
        pid = product(self.db, 'Milk 2L', 1)
        with self.assertRaises(st.StockError):
            st.purchase(self.db, pid, 1, receipt_id=9999)

    def test_deleting_a_receipt_leaves_the_purchase_log_in_place(self):
        cur = self.db.execute(
            "INSERT INTO receipts (store, purchased_on, total, note, added_by) VALUES (?, ?, ?, ?, ?)",
            ('Coles', '2026-10-20', 10.0, '', 1))
        self.db.commit()
        receipt_id = cur.lastrowid
        pid = product(self.db, 'Milk 2L', 1)
        st.purchase(self.db, pid, 1, price=3.40, user_id=1, receipt_id=receipt_id)
        self.db.execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
        self.db.commit()
        row = self.db.execute("SELECT receipt_id FROM stock_log WHERE product_id = ?", (pid,)).fetchone()
        self.assertIsNone(row['receipt_id'])
