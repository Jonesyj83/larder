import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PageCompatibility(unittest.TestCase):
    def test_pages_and_workflows(self):
        for prefix in ('', '/larder', '/kitchen/larder'):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory(
                prefix='larder-pages-'
            ) as data:
                env = dict(os.environ, LARDER_DATA=data, LARDER_BASE_PATH=prefix,
                           PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE='1')
                result = subprocess.run([sys.executable, __file__, '--check'],
                                        cwd=ROOT, env=env, text=True, capture_output=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


async def check():
    import json
    import re
    from html.parser import HTMLParser
    from httpx import AsyncClient, ASGITransport
    from starlette.requests import Request
    from app.main import app, templates, PAGES, BASE_PATH

    class Tags(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags = []

        def handle_starttag(self, tag, attrs):
            self.tags.append((tag, dict(attrs)))

    def inspect(html):
        parsed = Tags()
        parsed.feed(html)
        for tag, attrs in parsed.tags:
            for key in ('href', 'src'):
                value = attrs.get(key, '')
                if value.startswith('/'):
                    assert value.startswith(BASE_PATH + '/'), (tag, key, value)
        return parsed.tags

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://testserver') as client:
        for route in PAGES:
            r = (await client.get(route, follow_redirects=False))
            assert r.status_code == 307
            assert r.headers['location'] == BASE_PATH + '/login'
        assert (await client.get('/api/stock')).status_code == 401
        login = (await client.get('/login'))
        assert login.status_code == 200
        inspect(login.text)
        assert 'loginPage()' in login.text and 'Main navigation' not in login.text
        assert (await client.get('/api/auth/status')).json()['needs_setup']
        r = (await client.post('/api/auth/setup', json={'username': 'render-test', 'password': 'temporary-test-password'}))
        assert r.status_code == 200, r.text
        me = (await client.get('/api/me')).json()
        assert me['date_format'] == 'DD/MM/YYYY' and me['measurement_system'] == 'metric'
        assert (await client.put('/api/me', json={'date_format':'invalid'})).status_code == 422
        assert (await client.put('/api/me', json={'measurement_system':'invalid'})).status_code == 422
        r = await client.put('/api/me', json={'date_format':'MM/DD/YYYY','measurement_system':'imperial'})
        assert r.status_code == 200 and r.json()['portion_size'] == 'regular'
        member = await client.post('/api/users', json={'username':'member-test','password':'temporary-test-password'})
        assert member.status_code == 201 and member.json()['date_format'] == 'DD/MM/YYYY'
        assert member.json()['measurement_system'] == 'metric'
        freezer = next(l for l in (await client.get('/api/locations')).json() if l['is_freezer'])
        r = await client.put('/api/locations/' + str(freezer['id']), json={'name':freezer['name'],'sort':2})
        assert r.status_code == 200 and r.json()['is_freezer'] == 1
        assert (await client.get('/api/products')).json() == []
        assert (await client.get('/api/stock')).json() == []
        for route, (template, title) in PAGES.items():
            response = (await client.get(route))
            assert response.status_code == 200, (route, response.text)
            tags = inspect(response.text)
            assert '<h1' in response.text and 'page-head' in response.text
            assert any(t == 'nav' and a.get('aria-label') == 'Main navigation' for t, a in tags)
            assert '{{' not in response.text and '{%' not in response.text
            # Render the public URL as seen behind a proxy, including active navigation.
            request = Request({'type': 'http', 'method': 'GET', 'path': BASE_PATH + route,
                               'headers': [], 'scheme': 'http', 'server': ('testserver', 80),
                               'query_string': b''})
            html = templates.env.get_template(template).render(request=request, title=title,
                user={'id': 1, 'username': 'render-test', 'portion_size': 'regular', 'is_admin': True})
            tags = inspect(html)
            selected = [a for t, a in tags if t == 'a' and 'on' in a.get('class', '').split()]
            assert len(selected) == 1 and selected[0]['href'] == BASE_PATH + route, (route, selected)
            if template == 'settings.html':
                regular = templates.env.get_template(template).render(request=request, title=title,
                    user={'id': 2, 'username': 'member', 'portion_size': 'small', 'is_admin': False})
                assert '<h2>People</h2>' not in regular
            # Compile every inline script and Alpine expression without executing browser actions.
            for script in re.findall(r'<script[^>]*>(.*?)</script>', html, re.S):
                subprocess.run(['node', '--check'], input=script, text=True, check=True, capture_output=True)
            expressions = []
            for _, attrs in tags:
                for name, value in attrs.items():
                    if value and (name.startswith(('@', ':')) or name in
                                  ('x-data', 'x-init', 'x-show', 'x-if', 'x-text', 'x-model', 'x-model.number')):
                        expressions.append('new Function(' + json.dumps(value) + ');')
            subprocess.run(['node'], input='\n'.join(expressions), text=True, check=True, capture_output=True)
        manifest = (await client.get('/manifest.webmanifest')).json()
        assert manifest['scope'] == BASE_PATH + '/'
        assert manifest['start_url'] == BASE_PATH + '/'
        assert all(i['src'].startswith(BASE_PATH + '/') for i in manifest['icons'])
        assert manifest['theme_color'] == '#171d25'
        for size in (192, 512):
            r = (await client.get(f'/icon-{size}.png'))
            assert r.status_code == 200 and r.content.startswith(b'\x89PNG')
        units = (await client.get('/api/units')).json()
        locations = (await client.get('/api/locations')).json()
        product = (await client.post('/api/products', json={'name': 'Compatibility item', 'unit_id': units[0]['id'],
                            'location_id': locations[0]['id'], 'shelf_life_kind': 'use_by',
                            'barcodes': ['test/code:123']}))
        assert product.status_code == 201, product.text
        pid = product.json()['id']
        scan = (await client.get('/api/scan/test%2Fcode%3A123')).json()
        assert scan['found'] and scan['product']['id'] == pid
        r = (await client.post('/api/stock/purchase', json={'product_id': pid, 'amount': 3}))
        assert r.status_code == 200, r.text
        assert (await client.get('/api/stock')).json()[0]['amount'] == 3
        assert (await client.get('/api/stock')).json()[0]['shelf_life_kind'] == 'use_by'
        r = (await client.post('/api/stock/consume', json={'product_id': pid, 'amount': 1}))
        assert r.status_code == 200, r.text
        assert (await client.get('/api/stock')).json()[0]['amount'] == 2
        r = (await client.post('/api/shopping', json={'product_id': pid}))
        assert r.status_code == 201, r.text
        assert (await client.get('/api/shopping')).json()[0]['product_id'] == pid
        meal = await client.post('/api/mealplan', json={'day':'2026-10-12','slot':'breakfast','note':'Simple breakfast'})
        assert meal.status_code == 201
        mid = meal.json()['id']
        n = (await client.get('/api/mealplan/needs?start=2026-10-12&end=2026-10-18')).json()
        assert n['unmapped'][0]['id'] == mid
        r = await client.put('/api/mealplan/' + str(mid), json={'day':'2026-10-13','slot':'lunch','servings':2,
            'ingredients':[{'product_id':pid,'amount':4}]})
        assert r.status_code == 200 and r.json()['recipe_id']
        n = (await client.get('/api/mealplan/needs?start=2026-10-12&end=2026-10-18')).json()
        assert n['items'][0]['need'] == 4 and n['items'][0]['missing'] == 2 and not n['unmapped']
        assert (await client.put('/api/mealplan/' + str(mid), json={'day':'13/10/2026'})).status_code == 400
        assert (await client.post('/api/stock/purchase', json={'product_id':pid,'amount':1,'expires_on':'2026-02-30'})).status_code == 422
        (await client.post('/api/auth/logout'))
        assert (await client.get('/api/stock')).status_code == 401
        r = (await client.post('/api/auth/login', json={'username': 'render-test', 'password': 'temporary-test-password'}))
        assert r.status_code == 200
        assert (await client.get('/')).status_code == 200
        me = (await client.get('/api/me')).json()
        assert me['date_format'] == 'MM/DD/YYYY' and me['measurement_system'] == 'imperial'
        assert 'MM/DD/YYYY' in (await client.get('/scan')).text
        await client.post('/api/auth/logout')
        await client.post('/api/auth/login', json={'username':'member-test','password':'temporary-test-password'})
        me = (await client.get('/api/me')).json()
        assert me['date_format'] == 'DD/MM/YYYY' and me['measurement_system'] == 'metric'
    print('Page rendering, bindings, auth, stock, shopping and prefix checks passed')


if __name__ == '__main__':
    if '--check' in sys.argv:
        import asyncio
        async def run_checks():
            async def tick():
                while True:
                    await asyncio.sleep(.01)
            # Keep a bounded timer active while worker threads finish synchronous routes.
            timer = asyncio.create_task(tick())
            try:
                await asyncio.wait_for(check(), timeout=30)
            finally:
                timer.cancel()
        asyncio.run(run_checks())
    else:
        unittest.main()
