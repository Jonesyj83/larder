import re
import subprocess
import unittest
from pathlib import Path


class MemoryNavigation(unittest.TestCase):
    def test_url_expansion_preserves_search_and_manual_toggles(self):
        template = (Path(__file__).resolve().parents[1] / 'app/templates/memory.html').read_text()
        script = re.findall(r'<script>(.*?)</script>', template, re.S)[0]
        program = """
const assert = require('node:assert/strict');
const location = {search: ''};
const api = async () => [{id: 1, kind: 'habit', content: 'Lunch'}];
const oops = e => {throw e;};
""" + script + """
(async () => {
  for (const kind of memoryPage().kinds.map(group => group.kind)) {
    location.search = '?kind=' + kind;
    const page = memoryPage();
    await page.init();
    assert.deepEqual(page.expanded, {[kind]: true});
    assert.equal(page.loaded, true);
    page.expanded[kind] = false;
    page.query = 'Lunch';
    await page.load();
    assert.equal(page.searching, true);
    assert.equal(page.expanded[kind], false);
    page.query = '';
    assert.equal(page.searching, false);
  }
  for (const query of ['', '?kind=invalid', '?kind=', '?kind=__proto__']) {
    location.search = query;
    const page = memoryPage();
    await page.init();
    assert.deepEqual(page.expanded, {});
  }
})().catch(e => {console.error(e); process.exit(1);});
"""
        result = subprocess.run(['node'], input=program, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
