import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport
from starlette.middleware.sessions import SessionMiddleware

from app import db as database
from app.auth import current_user
from app.routes import memory


class Memory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        async def tick():
            while True:
                await asyncio.sleep(.01)
        self.timer = asyncio.create_task(tick())
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = patch.multiple(database, DATA_DIR=root, DB_PATH=root/'larder.db', UPLOAD_DIR=root/'uploads')
        self.paths.start()
        database.init_db()
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="test")
        app.include_router(memory.router)
        self.app = app
        app.dependency_overrides[current_user] = lambda: {'id': 1}
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        self.timer.cancel()
        await self.client.aclose()
        self.paths.stop()
        self.temp.cleanup()

    async def test_crud_filters_and_search_sync(self):
        c = self.client
        created = await c.post('/api/memory', json={'kind': 'like', 'content': 'Crispy potatoes'})
        self.assertEqual(created.status_code, 201)
        note = created.json()
        self.assertEqual(note['source'], 'chat')
        await c.post('/api/memory', json={'kind': 'habit', 'content': 'Lunch at work', 'source': 'manual'})
        self.assertEqual(len((await c.get('/api/memory')).json()), 2)
        self.assertEqual((await c.get('/api/memory?kind=like')).json(), [note])
        self.assertEqual((await c.get('/api/memory?q=potatoes')).json(), [note])
        url = '/api/memory/' + str(note['id'])
        changed = (await c.put(url, json={'content': 'Roast carrots', 'kind': 'maybe'})).json()
        self.assertEqual(changed['kind'], 'maybe')
        self.assertEqual(changed['source'], 'chat')
        self.assertEqual((await c.get('/api/memory?q=potatoes')).json(), [])
        self.assertEqual((await c.get('/api/memory?q=carrots')).json(), [changed])
        inactive = (await c.put(url, json={'active': False})).json()
        self.assertEqual(len((await c.get('/api/memory')).json()), 1)
        self.assertEqual((await c.get('/api/memory?active=false')).json(), [inactive])
        self.assertEqual((await c.delete(url)).status_code, 200)
        self.assertEqual((await c.get('/api/memory?active=false&q=carrots')).json(), [])
        self.assertEqual((await c.put(url, json={'active': True})).status_code, 404)
        self.assertEqual((await c.delete(url)).status_code, 404)

    async def test_validation_and_authentication(self):
        c = self.client
        for body in ({'kind': 'invalid', 'content': 'x'}, {'kind': 'like', 'content': ''}):
            self.assertEqual((await c.post('/api/memory', json=body)).status_code, 422)
        self.assertEqual((await c.post('/api/memory', json={'kind': 'like', 'content': '  '})).status_code, 400)
        self.assertEqual((await c.get('/api/memory?q=%22')).status_code, 400)
        self.app.dependency_overrides.clear()
        self.assertEqual((await c.get('/api/memory')).status_code, 401)

    async def test_migration_backfills_search_and_is_repeatable(self):
        db = database.connect()
        for name in ('ai', 'au', 'ad'):
            db.execute('DROP TRIGGER household_notes_' + name)
        db.execute("INSERT INTO household_notes(kind,content) VALUES('like','Noodles')")
        db.commit()
        db.close()
        database.init_db()
        database.init_db()
        self.assertEqual(len((await self.client.get('/api/memory?q=Noodles')).json()), 1)
