import json
import sqlite3
import unittest
from unittest.mock import patch
from fastapi import HTTPException
import test_planning
class Provenance(unittest.TestCase):
    setUp = test_planning.Planning.setUp
    tearDown = test_planning.Planning.tearDown
    def test_legacy_unknown_and_multiple_sources(self):
        from app.routes.shopping import list_items, add_item
        self.db.execute("INSERT INTO shopping_list(name,amount,list_id) VALUES('Bread',1,1)")
        item=list_items(self.db,self.user)[0]
        self.assertTrue(item['origin_unknown']); self.assertEqual(item['sources'],[])
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Egg',2)")
        first=add_item(self.db,1,1,2,user_id=1)
        second=add_item(self.db,1,2,2,user_id=1,source='recipe',source_ref=9,source_label='Egg toast')
        self.assertEqual(first,second)
        item=next(i for i in list_items(self.db,self.user) if i['id']==first)
        self.assertFalse(item['origin_unknown'])
        self.assertEqual([s['kind'] for s in item['sources']],['manual','recipe'])
        self.assertEqual(item['amount'],3)
    def test_additive_migration_preserves_legacy_bread(self):
        import tempfile
        from pathlib import Path
        from app import db as module
        with tempfile.TemporaryDirectory(prefix='larder-source-migration-') as path:
            root=Path(path)
            db=sqlite3.connect(root/'larder.db')
            db.executescript("CREATE TABLE shopping_list(id INTEGER PRIMARY KEY,product_id INTEGER,name TEXT NOT NULL DEFAULT '',amount REAL NOT NULL DEFAULT 1,unit_id INTEGER,note TEXT NOT NULL DEFAULT '',done INTEGER NOT NULL DEFAULT 0,added_by INTEGER,created_at TEXT NOT NULL DEFAULT (datetime('now')));")
            db.execute("INSERT INTO shopping_list(id,name,amount,created_at) VALUES(1,'Bread',1,'2026-10-11 10:41:14')")
            db.commit(); db.close()
            with patch.multiple(module,DATA_DIR=root,DB_PATH=root/'larder.db',UPLOAD_DIR=root/'uploads'):
                module.init_db(); module.init_db()
                db=module.connect()
                self.assertEqual(db.execute('SELECT id,name,product_id,amount,created_at FROM shopping_list').fetchone()[:],(1,'Bread',None,1,'2026-10-11 10:41:14'))
                self.assertEqual(db.execute('SELECT COUNT(*) FROM shopping_sources').fetchone()[0],0)
                db.close()

class BrowserReview(unittest.TestCase):
    def test_recipe_filter_and_rating_javascript(self):
        import subprocess
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        recipes=(root/'app/templates/recipes.html').read_text().split('<script>')[1].split('</script>')[0]
        body="""
const assert=require('node:assert/strict'), window={}, calls=[];
const today=()=> '2026-10-12', addDays=x=>x, toast=()=>{}, oops=e=>{throw e};
async function api(path,opts={}) { calls.push({path,...opts}); return {}; }
(async()=>{
const r = recipesPage();
r.recipes = [
  {id:1, name:'Chilli', can_make:true, ingredient_count:5, rating:4},
  {id:2, name:'Soup', can_make:false, ingredient_count:3, rating:0},
  {id:3, name:'Draft idea', can_make:false, ingredient_count:0, rating:0},
];
assert.equal(r.shown().length, 3);
r.filter = 'ready'; assert.deepEqual(r.shown().map(x=>x.id), [1]);
r.filter = 'missing'; assert.deepEqual(r.shown().map(x=>x.id), [2]);
r.filter = 'all'; assert.equal(r.shown().length, 3);
assert.equal(r.stars(4), '\\u2605\\u2605\\u2605\\u2605');
assert.equal(r.stars(0), '');
assert.equal(r.stars(undefined), '');
r.current = {id:1, rating:4};
await r.rate(2);
assert.equal(r.current.rating, 2);
assert.equal(r.recipes.find(x=>x.id==1).rating, 2);
assert.equal(calls[0].path, '/api/recipes/1/rating');
assert.equal(calls[0].method, 'PUT');
assert.deepEqual(calls[0].body, {rating: 2});
})().catch(e=>{console.error(e);process.exit(1)});
"""
        run=subprocess.run(['node'],input=recipes+body,text=True,capture_output=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
