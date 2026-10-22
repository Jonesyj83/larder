"""The whole app over HTTP: cross-site writes, who can see and change what,
uploads, error bodies, odd input. Runs in a subprocess with LARDER_DATA pointed
at a throwaway folder, the same way test_pages does."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]

PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class HttpSafety(unittest.TestCase):
    def test_app_over_http(self):
        with tempfile.TemporaryDirectory(prefix='larder-http-') as data:
            env = dict(os.environ, LARDER_DATA=data, LARDER_BASE_PATH='',
                       PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, __file__, '--check'],
                                    cwd=ROOT, env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@patch.multiple("app.pricing.stores", **{
    "search_" + store: MagicMock(return_value=[])
    for store in ("woolworths", "coles", "aldi", "iga")
})
async def check():
    from unittest.mock import patch
    from httpx import AsyncClient, ASGITransport
    from app.db import DATA_DIR, UPLOAD_DIR
    from app.main import app
    from app.routes import account

    page = {'X-Requested-With': 'fetch'}

    def client(**kw):
        return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False),
                           base_url='http://testserver', **kw)

    async with client(headers=page) as admin, client(headers=page) as member, \
            client(headers=page) as member_other_tab, client() as bare:
        # --- first run ---
        r = await admin.post('/api/auth/setup', json={'username': 'admin-test', 'password': 'admin-password'})
        assert r.status_code == 200, r.text
        assert r.headers['x-content-type-options'] == 'nosniff'
        assert r.headers['x-frame-options'] == 'SAMEORIGIN'
        r = await bare.post('/api/auth/setup', json={'username': 'second', 'password': 'second-password'},
                            headers=page)
        assert r.status_code == 400, r.text
        r = await admin.post('/api/users', json={'username': 'member-test', 'password': 'member-password'})
        assert r.status_code == 201, r.text
        member_id = r.json()['id']
        admin_id = (await admin.get('/api/me')).json()['id']
        for c in (member, member_other_tab):
            r = await c.post('/api/auth/login', json={'username': 'member-test', 'password': 'member-password'})
            assert r.status_code == 200, r.text

        # --- writes riding on the cookie have to come from our own pages ---
        async with client(cookies=admin.cookies) as no_header:
            assert (await no_header.get('/api/stock')).status_code == 200
            r = await no_header.post('/api/shopping', json={'name': 'Milk'})
            assert r.status_code == 403, r.text
            r = await no_header.post('/api/shopping', json={'name': 'Milk'}, headers={'Origin': 'http://evil.example'})
            assert r.status_code == 403, r.text
            r = await no_header.post('/api/shopping', json={'name': 'Milk'}, headers={'Origin': 'null'})
            assert r.status_code == 403, r.text
            r = await no_header.delete('/api/shopping/done')
            assert r.status_code == 403, r.text
            r = await no_header.post('/api/products/1/picture', files={'file': ('a.png', PNG, 'image/png')})
            assert r.status_code == 403, r.text
            r = await no_header.post('/api/recipes/1/cook')
            assert r.status_code == 403, r.text
            # same origin (e.g. the /docs page) is fine
            r = await no_header.post('/api/shopping', json={'name': 'Milk'}, headers={'Origin': 'http://testserver'})
            assert r.status_code == 201, r.text

        # --- API keys don't need the header, and are per person ---
        r = await admin.post('/api/me/keys', json={'name': 'scanner'})
        admin_key, admin_key_id = r.json()['key'], r.json()['id']
        r = await bare.post('/api/shopping', json={'name': 'Bread'}, headers={'X-API-Key': admin_key})
        assert r.status_code == 201, r.text
        r = await bare.get('/api/me', headers={'Authorization': 'Bearer ' + admin_key})
        assert r.json()['username'] == 'admin-test'
        r = await bare.post('/api/shopping', json={'name': 'Bread'}, headers={'X-API-Key': 'lk_wrong'})
        assert r.status_code == 401, r.text
        assert all(admin_key not in str(k) for k in (await admin.get('/api/me/keys')).json())
        assert (await member.delete(f'/api/me/keys/{admin_key_id}')).status_code == 200
        assert (await bare.get('/api/me', headers={'X-API-Key': admin_key})).status_code == 200
        assert (await member.get('/api/me/keys')).json() == []

        # --- admin-only means admin-only on the server ---
        assert (await member.get('/api/users')).status_code == 403
        assert (await member.post('/api/users', json={'username': 'x', 'password': 'xxxxxxxx'})).status_code == 403
        assert (await member.put(f'/api/users/{member_id}', json={'is_admin': True})).status_code == 403
        assert (await member.put(f'/api/users/{admin_id}', json={'password': 'taken-over'})).status_code == 403
        assert (await member.delete(f'/api/users/{admin_id}')).status_code == 403
        assert not (await member.get('/api/me')).json()['is_admin']
        r = await bare.get('/api/users', headers={'X-API-Key': admin_key})
        assert r.status_code == 200 and all('password_hash' not in u for u in r.json())

        # --- odd numbers and text ---
        units = {u['name']: u['id'] for u in (await admin.get('/api/units')).json()}
        r = await admin.post('/api/products', json={'name': 'Crème brûlée 🍮', 'unit_id': units['g']})
        assert r.status_code == 201 and r.json()['name'] == 'Crème brûlée 🍮', r.text
        pid = r.json()['id']
        found = (await admin.get('/api/products', params={'q': '🍮'})).json()
        assert [p['id'] for p in found] == [pid], found
        for body in ('{"product_id": %d, "amount": NaN}', '{"product_id": %d, "amount": Infinity}',
                     '{"product_id": %d, "amount": -1}', '{"product_id": %d, "amount": 1e309}',
                     '{"product_id": %d, "amount": 0}', '{"product_id": %d, "amount": 1, "price": -2}',
                     '{"product_id": %d, "amount": 1, "expires_on": "31/12/2026"}'):
            r = await admin.post('/api/stock/purchase', content=body % pid, headers={'Content-Type': 'application/json'})
            assert r.status_code == 422, (body, r.status_code, r.text)
        r = await admin.post('/api/stock/purchase', json={'product_id': pid, 'amount': 1, 'location_id': 9999})
        assert r.status_code == 400, r.text
        assert (await admin.get('/api/stock')).json() == []
        for bad in ({'name': '   ', 'unit_id': units['g']}, {'name': 'X', 'unit_id': 9999},
                    {'name': 'X', 'unit_id': units['g'], 'default_days': 10 ** 12},
                    {'name': 'X', 'unit_id': units['g'], 'purchase_factor': 0},
                    {'name': 'X', 'unit_id': units['g'], 'min_stock': -1}):
            r = await admin.post('/api/products', json=bad)
            assert r.status_code in (400, 422), (bad, r.text)
        assert (await admin.post('/api/locations', json={'name': 'Shed', 'sort': 10 ** 30})).status_code == 422
        assert (await admin.post('/api/locations', json={'name': '  '})).status_code == 400
        assert (await admin.post('/api/conversions', json={'from_unit_id': units['g'], 'to_unit_id': units['kg'], 'factor': 0})).status_code == 422
        assert (await admin.post('/api/shopping', json={'name': 'Milk', 'amount': 0})).status_code == 422
        assert (await admin.post('/api/shopping', json={'name': 'Milk', 'unit_id': 9999})).status_code == 400
        assert (await admin.get('/api/stock/expiring', params={'days': 10 ** 15})).status_code == 200
        assert len((await admin.get('/api/stock/log', params={'limit': -1})).json()) <= 1
        assert (await admin.get('/api/mealplan', params={'start': '9999-12-31'})).status_code == 400
        assert (await admin.get('/api/recipes/1', params={'servings': 'nan'})).status_code in (400, 404)

        # --- quotes and wildcards are just text ---
        r = await admin.post('/api/products', json={'name': "Robert'); DROP TABLE products;--", 'unit_id': units['g']})
        assert r.status_code == 201, r.text
        assert (await admin.get('/api/products', params={'q': "%' OR '1'='1"})).json() == []
        assert len((await admin.get('/api/products')).json()) == 4

        # --- barcodes ---
        r = await admin.post('/api/products', json={'name': 'Beans', 'unit_id': units['piece'], 'barcodes': ['5000157024671']})
        assert r.status_code == 201, r.text
        r = await admin.post('/api/products', json={'name': 'Other beans', 'unit_id': units['piece'], 'barcodes': ['5000157024671']})
        assert r.status_code == 400 and 'Beans' in r.json()['detail'], r.text
        assert not any(p['name'] == 'Other beans' for p in (await admin.get('/api/products')).json())
        beans = (await admin.get('/api/scan/5000157024671')).json()
        assert beans['found'] and beans['product']['name'] == 'Beans'

        # --- "Got it" twice adds the stock once ---
        item = (await admin.post('/api/shopping', json={'product_id': beans['product']['id'], 'amount': 2})).json()
        assert (await admin.post(f"/api/shopping/{item['id']}/bought", json={})).status_code == 200
        assert (await admin.post(f"/api/shopping/{item['id']}/bought", json={})).status_code == 404
        lists = (await admin.get('/api/shopping/lists')).json()
        home = await admin.post('/api/shopping/lists', json={'name': 'Home'})
        assert home.status_code == 201
        home_id = home.json()['id']
        loose = (await admin.post(f'/api/shopping?list_id={home_id}', json={'name': 'Home item'})).json()
        assert loose['id'] not in [i['id'] for i in (await admin.get('/api/shopping')).json()]
        assert (await admin.post(f"/api/shopping/{loose['id']}/bought", json={})).status_code == 404
        assert (await admin.post(f"/api/shopping/{loose['id']}/bought?list_id={home_id}", json={})).status_code == 200
        renamed = await admin.put(f'/api/shopping/lists/{home_id}', json={'name': 'Work'})
        assert renamed.json()['name'] == 'Work'
        assert (await admin.delete(f'/api/shopping/lists/{home_id}')).status_code == 409
        assert (await admin.post('/api/shopping/lists', json={'name': 'Other'})).status_code == 201
        assert (await admin.delete(f'/api/shopping/lists/{home_id}')).status_code == 200
        assert (await admin.delete('/api/shopping/lists/' + str(lists[0]['id']))).status_code == 409
        assert (await admin.post('/api/shopping/lists', json={'name': '  '})).status_code == 400

        stock = (await admin.get('/api/stock')).json()
        assert [s['amount'] for s in stock if s['product_id'] == beans['product']['id']] == [2], stock

        # --- pictures ---
        before = sorted(os.listdir(UPLOAD_DIR))
        r = await admin.post(f'/api/products/{pid}/picture', files={'file': ('x.png', b'<html><script>alert(1)</script>', 'image/png')})
        assert r.status_code == 400, r.text
        r = await admin.post(f'/api/products/{pid}/picture', files={'file': ('x.html', PNG, 'text/html')})
        assert r.status_code == 400, r.text
        r = await admin.post(f'/api/products/{pid}/picture', files={'file': ('big.png', PNG + b'\x00' * (8 * 1024 * 1024), 'image/png')})
        assert r.status_code == 400, r.text
        r = await admin.post('/api/products/99999/picture', files={'file': ('x.png', PNG, 'image/png')})
        assert r.status_code == 404, r.text
        for url in ('http://169.254.169.254/latest/meta-data/', 'http://127.0.0.1:8000/', 'file:///etc/passwd'):
            r = await admin.post(f'/api/products/{pid}/picture-url', json={'url': url})
            assert r.status_code == 400, (url, r.text)
        r = await admin.post('/api/products', json={'name': 'Pic', 'unit_id': units['g'], 'picture_url': 'http://169.254.169.254/x.png'})
        assert r.status_code == 201 and r.json()['picture'] is None, r.text
        assert sorted(os.listdir(UPLOAD_DIR)) == before
        r = await admin.post(f'/api/products/{pid}/picture', files={'file': ('x.png', PNG, 'image/png')})
        assert r.status_code == 200, r.text
        name = r.json()['picture']
        assert name.endswith('.png')
        r = await bare.get('/uploads/' + name)
        assert r.status_code == 200 and r.headers['content-type'] == 'image/png'
        assert r.headers['x-content-type-options'] == 'nosniff'
        secret_key = (DATA_DIR / 'secret.key').read_text().strip()
        for path in ('/uploads/%2e%2e/secret.key', '/uploads/..%2fsecret.key', '/uploads/%2e%2e/larder.db',
                     '/static/..%2f..%2fdb.py'):
            r = await bare.get(path)
            assert r.status_code == 404 and secret_key not in r.text and 'SQLite' not in r.text, (path, r.status_code)

        # --- nothing about the server leaks in an error ---
        with patch('app.stock.in_stock', side_effect=RuntimeError('/very/secret/path/larder.db is broken')):
            r = await admin.post('/api/stock/purchase', json={'product_id': pid, 'amount': 1})
        assert r.status_code == 500, r.text
        assert '/very/secret' not in r.text and 'Traceback' not in r.text and 'RuntimeError' not in r.text, r.text
        assert r.json()['detail']

        # --- names are escaped wherever the server renders them ---
        r = await admin.post('/api/users', json={'username': '<img src=x onerror=alert(1)>', 'password': 'xss-password'})
        assert r.status_code == 201, r.text
        async with client(headers=page) as odd:
            r = await odd.post('/api/auth/login', json={'username': '<img src=x onerror=alert(1)>', 'password': 'xss-password'})
            assert r.status_code == 200, r.text
            for path in ('/', '/settings', '/mealplan'):
                html = (await odd.get(path)).text
                assert '<img src=x' not in html and '&lt;img src=x onerror=alert(1)&gt;' in html, path
        assert (await admin.post('/api/users', json={'username': 'bad\nname', 'password': 'xxxxxxxx'})).status_code == 400

        # --- changing a password ends the other sessions ---
        r = await member.post('/api/me/password', json={'old_password': 'member-password', 'new_password': 'member-password-2'})
        assert r.status_code == 200, r.text
        assert (await member.get('/api/me')).status_code == 200
        assert (await member_other_tab.get('/api/me')).status_code == 401
        # and so does an admin resetting it
        r = await admin.put(f'/api/users/{member_id}', json={'password': 'reset-by-admin'})
        assert r.status_code == 200, r.text
        assert (await member.get('/api/me')).status_code == 401
        r = await admin.put(f'/api/users/{admin_id}', json={'password': 'admin-password-2'})
        assert r.status_code == 200 and (await admin.get('/api/me')).status_code == 200

        # --- too many wrong passwords ---
        account._failures.clear()
        async with client(headers=page) as guesser:
            for _ in range(account.MAX_FAILURES):
                r = await guesser.post('/api/auth/login', json={'username': 'admin-test', 'password': 'wrong'})
                assert r.status_code == 401, r.text
            r = await guesser.post('/api/auth/login', json={'username': 'admin-test', 'password': 'admin-password-2'})
            assert r.status_code == 429, r.text
            # other accounts aren't caught up in it
            r = await guesser.post('/api/auth/login', json={'username': 'member-test', 'password': 'reset-by-admin'})
            assert r.status_code == 200, r.text
            r = await guesser.post('/api/auth/login', json={'username': 'nobody', 'password': 'whatever'})
            assert r.status_code == 401 and r.json()['detail'] == 'Wrong username or password'
        account._failures.clear()
    print('HTTP safety checks passed')


if __name__ == '__main__':
    if '--check' in sys.argv:
        import asyncio

        async def run_checks():
            async def tick():
                while True:
                    await asyncio.sleep(.01)
            # same trick as test_pages: keep the loop busy while sync routes run in threads
            timer = asyncio.create_task(tick())
            try:
                await asyncio.wait_for(check(), timeout=150)
            finally:
                timer.cancel()
        asyncio.run(run_checks())
    else:
        unittest.main()
