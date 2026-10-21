import re
import subprocess
import unittest
from pathlib import Path


class MemoryNavigation(unittest.TestCase):
    def test_category_links_filter_notes_to_one_group(self):
        template = (Path(__file__).resolve().parents[1] / 'app/templates/memory.html').read_text()
        script = re.findall(r'<script>(.*?)</script>', template, re.S)[0]
        program = """
const assert = require('node:assert/strict');
const location = {search: ''};
const api = async (path, options) => {
  if (options?.method === 'POST') return {id: 4, kind: options.body.kind, content: options.body.content};
  const all = [
    {id: 1, kind: 'habit', content: 'Lunch'},
    {id: 2, kind: 'like', content: 'Apples'},
    {id: 3, kind: 'habit', content: 'Coffee'},
  ];
  const q = new URLSearchParams(path.split('?')[1] || '').get('q');
  return q ? all.filter(note => note.content.toLowerCase().includes(q.toLowerCase())) : all;
};
const oops = e => {throw e;};
const toast = () => {};
""" + script + """
(async () => {
  location.search = '?kind=habit';
  const page = memoryPage();
  await page.init();
  assert.equal(page.activeGroup.label, 'Habits');
  assert.deepEqual(page.visibleNotes.map(note => note.content), ['Lunch', 'Coffee']);
  assert.equal(page.kinds.filter(group => group.kind === page.activeKind).length, 1);
  location.search = '';
  const all = memoryPage();
  await all.init();
  assert.equal(all.activeGroup, null);
  assert.equal(all.visibleNotes.length, 3);
  all.newContent = 'No seafood';
  await all.add('dislike');
  assert.equal(all.notes[0].kind, 'dislike');
  all.query = 'Coffee';
  await all.load();
  assert.deepEqual(all.visibleNotes.map(note => note.content), ['Coffee']);
})().catch(e => {console.error(e); process.exit(1);});
"""
        result = subprocess.run(['node'], input=program, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
