import unittest

from app import db as database
from app import stock
from app.routes import stock as routes
import test_memory


class Prices(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = test_memory.Memory.asyncTearDown
    async def asyncSetUp(self):
        await test_memory.Memory.asyncSetUp(self)
        self.app.include_router(routes.router)
        db = database.connect()
        db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'test','unused')")
        unit = db.execute("SELECT id FROM units WHERE name='piece'").fetchone()[0]
        db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Eggs',?)", (unit,))
        db.execute("INSERT INTO products(id,name,unit_id) VALUES(2,'Bread',?)", (unit,))
        db.commit()
        db.close()

    async def test_ledger_survives_consumption_and_spending_window(self):
        c = self.client
        for pid, price in ((1, 5), (1, None), (1, 0), (2, 8)):
            response = await c.post('/api/stock/purchase', json={'product_id': pid, 'amount': 1, 'price': price})
            self.assertEqual(response.status_code, 200)
        await c.post('/api/stock/consume', json={'product_id': 1, 'amount': 3})
        db = database.connect()
        self.assertEqual(db.execute('SELECT COUNT(*) FROM stock WHERE product_id=1').fetchone()[0], 0)
        db.execute("INSERT INTO stock_log(product_id,kind,amount,price,at) VALUES(1,'purchase',1,100,datetime('now','-40 days'))")
        db.execute("INSERT INTO stock_log(product_id,kind,amount,price) VALUES(1,'adjust',1,200)")
        db.commit()
        db.close()
        history = (await c.get('/api/stock/price-history/1')).json()
        self.assertEqual([entry['price'] for entry in history], [100, 5, None, 0])
        self.assertTrue(all(entry['at'] for entry in history))
        summary = (await c.get('/api/stock/spending')).json()
        self.assertEqual(summary['total'], 13)
        self.assertEqual([item['product_id'] for item in summary['products']], [2, 1])
        self.assertEqual((await c.get('/api/stock/spending?days=60')).json()['total'], 113)
        self.assertEqual((await c.get('/api/stock/spending?days=-1')).status_code, 400)

    async def test_existing_locations_and_log_migrate_without_data_loss(self):
        db = database.connect()
        db.execute("DELETE FROM locations WHERE name='Work'")
        db.execute("UPDATE locations SET sort=17 WHERE name='Pantry'")
        db.execute('ALTER TABLE stock_log DROP COLUMN price')
        db.execute("INSERT INTO stock_log(product_id,kind,amount) VALUES(1,'purchase',2)")
        db.commit()
        db.close()
        database.init_db()
        database.init_db()
        db = database.connect()
        self.assertEqual(tuple(db.execute("SELECT is_freezer,sort FROM locations WHERE name='Work'").fetchone()), (0, 3))
        self.assertEqual(db.execute("SELECT COUNT(*) FROM locations WHERE name='Work'").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT sort FROM locations WHERE name='Pantry'").fetchone()[0], 17)
        self.assertEqual(tuple(db.execute('SELECT amount,price FROM stock_log').fetchone()), (2, None))
        db.close()
