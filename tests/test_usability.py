"""Scratch-only usability regressions; no services or network."""
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

class Usability(unittest.TestCase):
    def setUp(self):
        from app.db import SCHEMA
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'test','unused')")
        self.db.execute("INSERT INTO units(id,name) VALUES(1,'piece')")
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.user = {'id': 1, 'portion_size': 'regular'}

    def tearDown(self):
        self.db.close()

    def test_preferences_persist_and_validate(self):
        from app.routes.account import PrefsIn, update_me
        from pydantic import ValidationError
        u = update_me(PrefsIn(date_format='MM/DD/YYYY', measurement_system='imperial'), self.user, self.db)
        self.assertEqual((u['date_format'], u['measurement_system'], u['portion_size']), ('MM/DD/YYYY', 'imperial', 'regular'))
        self.assertEqual(u['show_prices'], 0)  # off by default
        update_me(PrefsIn(portion_size='small'), self.user, self.db)
        u = dict(self.db.execute('SELECT * FROM users').fetchone())
        self.assertEqual(u['date_format'], 'MM/DD/YYYY')
        with self.assertRaises(ValidationError):
            PrefsIn(date_format='DD/YY/MM')
        # turning it on and back off both stick - False isn't mistaken for "not sent"
        u = update_me(PrefsIn(show_prices=True), self.user, self.db)
        self.assertEqual(u['show_prices'], 1)
        u = update_me(PrefsIn(show_prices=False), self.user, self.db)
        self.assertEqual(u['show_prices'], 0)

    def test_stock_entry_price_is_optional_and_editable(self):
        from app.routes.stock import EntryEdit, edit_entry
        from app.stock import purchase
        from app.routes.products import load_product
        stock_id = purchase(self.db, 1, 2, price=4.5, user_id=self.user['id'])['stock_id']
        p = load_product(self.db, 1)
        self.assertEqual(p['stock'][0]['price'], 4.5)
        # editing without clearing price keeps it
        edit_entry(stock_id, EntryEdit(amount=2, price=4.5), self.db, self.user)
        p = load_product(self.db, 1)
        self.assertEqual(p['stock'][0]['price'], 4.5)
        # clearing it is a real action, not ignored
        edit_entry(stock_id, EntryEdit(amount=2, price=None), self.db, self.user)
        p = load_product(self.db, 1)
        self.assertIsNone(p['stock'][0]['price'])

    def test_note_edit_to_ingredients_and_shopping(self):
        from app.routes.mealplan import MealIn, MealEdit, add_meal, edit_meal, needs, plan_to_shopping
        m = add_meal(MealIn(day='2026-10-12', slot='breakfast', note='Toast'), self.db, self.user)
        n = needs('2026-10-12', '2026-10-18', self.db, self.user)
        self.assertEqual(n['unmapped'][0]['note'], 'Toast')
        self.assertEqual(n['items'], [])
        m = edit_meal(m['id'], MealEdit(day='2026-10-13', slot='lunch', note='Toast', servings=2,
            ingredients=[{'product_id': 1, 'amount': 4, 'unit_id': 1}]), self.db, self.user)
        self.assertIsNotNone(m['recipe_id'])
        self.assertEqual(m['note'], 'Toast')
        n = needs('2026-10-12', '2026-10-18', self.db, self.user)
        self.assertEqual(n['unmapped'], [])
        self.assertEqual(n['items'][0]['need'], 4)
        edit_meal(m['id'], MealEdit(servings=4), self.db, self.user)
        self.assertEqual(needs('2026-10-12','2026-10-18',self.db,self.user)['items'][0]['need'], 8)
        self.assertEqual(plan_to_shopping('2026-10-12','2026-10-18',self.db,self.user)['added'], 1)
        self.assertEqual(plan_to_shopping('2026-10-12','2026-10-18',self.db,self.user)['added'], 0)
        edit_meal(m['id'], MealEdit(recipe_id=None), self.db, self.user)
        self.assertEqual(len(needs('2026-10-12','2026-10-18',self.db,self.user)['unmapped']), 1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM stock').fetchone()[0], 0)

    def test_strict_backend_dates(self):
        from app.routes.stock import PurchaseIn, EntryEdit
        from app.routes.mealplan import MealIn, add_meal
        from pydantic import ValidationError
        from fastapi import HTTPException
        for bad in ('2026-02-30', '12/10/2026', '20261012', '2026-1-2'):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    PurchaseIn(product_id=1, amount=1, expires_on=bad)
                with self.assertRaises(ValidationError):
                    EntryEdit(amount=1, expires_on=bad)
                with self.assertRaises(HTTPException):
                    add_meal(MealIn(day=bad, slot='lunch', note='test'), self.db, self.user)

    def test_migration_and_builtin_arithmetic(self):
        import app.db as module
        from app.stock import to_stock_units
        with tempfile.TemporaryDirectory(prefix='migration-') as scratch:
            path = Path(scratch)
            old = sqlite3.connect(path / 'larder.db')
            old.executescript(module.SCHEMA.replace("    date_format TEXT NOT NULL DEFAULT 'DD/MM/YYYY',\n", '').replace("    measurement_system TEXT NOT NULL DEFAULT 'metric',\n", ''))
            old.execute("INSERT INTO users(id,username,password_hash) VALUES(9,'old','unused')")
            old.execute("INSERT INTO meal_plan(id,day,slot,note,user_id) VALUES(8,'2026-10-12','breakfast','Toast',9)")
            old.execute("INSERT INTO units(id,name) VALUES(1,'g')")
            old.execute("INSERT INTO products(id,name,unit_id) VALUES(7,'Existing stock',1)")
            old.execute("INSERT INTO stock(product_id,amount,expires_on) VALUES(7,123.456789,'2026-12-31')")
            old.execute("INSERT INTO units(id,name) VALUES(2,'kg')")
            old.execute("INSERT INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(2,1,999)")
            old.execute("INSERT INTO locations(name,is_freezer) VALUES('Freezer',1)")
            old.commit(); old.close()
            with patch.multiple(module, DATA_DIR=path, DB_PATH=path/'larder.db', UPLOAD_DIR=path/'uploads'):
                module.init_db(); module.init_db()
                db = module.connect()
                self.assertEqual(dict(db.execute('SELECT * FROM users').fetchone())['date_format'], 'DD/MM/YYYY')
                self.assertEqual(db.execute('SELECT note FROM meal_plan').fetchone()[0], 'Toast')
                self.assertEqual(db.execute('SELECT password_hash FROM users').fetchone()[0], 'unused')
                self.assertEqual(db.execute('SELECT amount,expires_on FROM stock').fetchone()[:], (123.456789,'2026-12-31'))
                self.assertEqual(db.execute('SELECT factor FROM unit_conversions WHERE from_unit_id=2 AND to_unit_id=1').fetchone()[0], 999)
                db.execute('UPDATE unit_conversions SET factor=1000 WHERE from_unit_id=2 AND to_unit_id=1')
                ids = {r['name']:r['id'] for r in db.execute('SELECT * FROM units')}
                p = {'name':'Mass','unit_id':ids['kg'],'purchase_unit_id':None,'purchase_factor':1}
                self.assertAlmostEqual(to_stock_units(db,p,1,ids['lb']), .45359237)
                self.assertAlmostEqual(to_stock_units(db,p,16,ids['oz']), .45359237)
                self.assertAlmostEqual(to_stock_units(db,p,1000,ids['g']), 1)
                p['unit_id'] = ids['ml']
                self.assertAlmostEqual(to_stock_units(db,p,1,ids['fl oz (US)']), 29.5735295625)
                self.assertAlmostEqual(to_stock_units(db,p,1,ids['l']), 1000)
                p['unit_id'] = ids['l']
                self.assertAlmostEqual(to_stock_units(db,p,1,ids['fl oz (US)']), .0295735295625)
                p['unit_id'] = ids['piece']
                with self.assertRaises(Exception): to_stock_units(db,p,1,ids['oz'])
                p['purchase_unit_id'] = ids['pack']; p['purchase_factor'] = 6
                self.assertEqual(to_stock_units(db,p,2,ids['pack']), 12)
                self.assertEqual(db.execute('SELECT is_freezer FROM locations').fetchone()[0], 1)
                db.execute("INSERT INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES(1,2,0.002)")
                p['unit_id'] = ids['g']; p['purchase_unit_id'] = None
                self.assertEqual(to_stock_units(db,p,1,ids['kg']), 1000)
                # A reverse household rule survives restart without a conflicting seed.
                db.execute('DELETE FROM unit_conversions WHERE from_unit_id=2 AND to_unit_id=1')
                db.commit(); db.close()
                module.init_db()
                db = module.connect()
                self.assertEqual(db.execute('SELECT COUNT(*) FROM unit_conversions WHERE from_unit_id=2 AND to_unit_id=1').fetchone()[0], 0)
                self.assertEqual(db.execute('SELECT factor FROM unit_conversions WHERE from_unit_id=1 AND to_unit_id=2').fetchone()[0], .002)
                db.close()

    def test_visible_controls(self):
        manage = (ROOT/'app/templates/manage.html').read_text()
        self.assertNotIn('type="checkbox"', manage)
        self.assertIn('Automatic conversions', manage)
        self.assertIn('pack', manage)
        for page in ('products', 'stock'):
            html = (ROOT/f'app/templates/{page}.html').read_text()
            self.assertNotIn('thumb blank', html)
            self.assertIn('pic(', html)
        for page in ('scan','stock','_stock_modal','mealplan','recipes'):
            self.assertNotIn('type="date"', (ROOT/f'app/templates/{page}.html').read_text())
        self.assertIn('openEdit(m)', (ROOT/'app/templates/mealplan.html').read_text())

    def test_failed_meal_edit_is_atomic_and_reports_conversion_problem(self):
        from app.routes.mealplan import MealIn, MealEdit, add_meal, edit_meal, needs
        from fastapi import HTTPException
        m = add_meal(MealIn(day='2026-10-12', slot='breakfast', note='Toast'), self.db, self.user)
        with self.assertRaises(HTTPException):
            edit_meal(m['id'], MealEdit(note='Changed', ingredients=[{'product_id':999,'amount':2}]), self.db, self.user)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM recipes').fetchone()[0], 0)
        self.assertEqual(self.db.execute('SELECT note FROM meal_plan').fetchone()[0], 'Toast')
        self.db.execute("INSERT INTO units(id,name) VALUES(2,'g')")
        edit_meal(m['id'], MealEdit(ingredients=[{'product_id':1,'amount':2,'unit_id':2}]), self.db, self.user)
        n = needs('2026-10-12', '2026-10-18', self.db, self.user)
        self.assertTrue(n['problems'])
        self.assertFalse(n['items'])

    def test_actual_edit_and_stock_javascript_preserves_quantity(self):
        plan = (ROOT/'app/templates/mealplan.html').read_text().split('<script>')[1].split('</script>')[0].replace('{{ user.id }}','1')
        stock = (ROOT/'app/templates/stock.html').read_text().split('<script>')[1].split('</script>')[0]
        pre = """
const assert=require('node:assert/strict'); const today=()=> '2026-10-12';
const addDays=x=>x, oops=e=>{throw e;}, toast=()=>{};
const preferences=()=>({show_prices:false});
const window={}; const calls=[]; async function api(path,opts={}) {calls.push({path,...opts}); return [];}
"""
        body = """
(async()=> {
 const p=planPage(); p.load=()=>{};
 p.openEdit({id:7,day:'2026-10-12',slot:'breakfast',note:'Toast',servings:2,recipe_id:null});
 p.adding.day='2026-10-13'; p.adding.slot='lunch';
 p.adding.ingredients=[{product_id:1,amount:4,unit_id:1}]; await p.add();
 assert.equal(calls[0].method,'PUT'); assert.equal(calls[0].path,'/api/mealplan/7');
 assert.deepEqual(calls[0].body.ingredients,[{product_id:1,amount:4,unit_id:1}]);
 assert.equal(calls[0].body.day,'2026-10-13'); assert.equal(calls[0].body.note,'Toast');
 const s=stockPage(); s.load=()=>{};
 const entry={id:9,amount:453.59237,location_id:null,expires_on:'2026-11-03',opened:false};
 for(let i=0;i<10;i++) await s.saveEntry(entry);
 for(const call of calls.filter(c=>c.path==='/api/stock/entries/9')) {
  assert.equal(call.body.amount,453.59237); assert.equal(call.body.expires_on,'2026-11-03');
 }
})().catch(e=>{console.error(e);process.exit(1)});
"""
        r = subprocess.run(['node'], input=pre+"function selectIngredient"+(ROOT/'app/static/app.js').read_text().split('function selectIngredient')[1]+plan+stock+body,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_date_and_quantity_javascript(self):
        script = (ROOT/'app/static/app.js').read_text()
        body = r"""
const assert = require('node:assert/strict');
assert.equal(parseDate('03/11/2026', 'DD/MM/YYYY'), '2026-11-03');
assert.equal(parseDate('03/11/2026', 'MM/DD/YYYY'), '2026-03-11');
assert.equal(parseDate('2026-11-03', 'YYYY-MM-DD'), '2026-11-03');
for(const f of ['DD/MM/YYYY','MM/DD/YYYY','YYYY-MM-DD']) {
 for(const iso of ['2024-02-29','2026-10-12']) assert.equal(parseDate(formatDate(iso,f),f),iso);
}
for(const bad of ['31/02/2026','29/02/2025','1/2/2026','2026-02-28']) assert.throws(()=>parseDate(bad,'DD/MM/YYYY'));
assert.equal(formatDate('2026-11-03','DD/MM/YYYY'),'03/11/2026');
window.USER_PREFS.measurement_system='imperial';
assert.match(qty(453.59237,'g'), /^1 lb/);
assert.equal(qty(6,'piece','pieces'),'6 pieces');
assert.match(qty(.1,'g'), /0\.00353 oz/);
window.UNIT_RULES=[{source:'kg',target:'g',factor:999}];
window.USER_PREFS.measurement_system='metric';
assert.equal(qty(1998,'g'),'2 kg');
window.UNIT_RULES=null; window.USER_PREFS.measurement_system='imperial';
const units=[{id:1,name:'g'},{id:2,name:'oz'},{id:3,name:'piece'},{id:4,name:'pack'}];
assert.equal(preferredUnits(units,1)[0].name,'oz');
assert.ok(preferredUnits(units,1).some(u=>u.id===1));
const field=dateField('2026-11-03'); field.$refs={input:{setCustomValidity(x){this.error=x;},reportValidity(){}}};
field.sync('2026-11-03'); assert.equal(field.text,'03/11/2026');
field.text='31/02/2026'; field.accept(); assert.ok(field.$refs.input.error); assert.equal(field.iso,'2026-11-03');
field.text='04/11/2026'; field.accept(); assert.equal(field.iso,'2026-11-04');
"""
        pre = "const window={USER_PREFS:{date_format:'DD/MM/YYYY',measurement_system:'metric'}}; const document={addEventListener(){}}; const navigator={};\n"
        r = subprocess.run(['node'], input=pre+script+'\n'+body,text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_portion_factor_drives_the_default_leftover_amount(self):
        script = (ROOT/'app/static/app.js').read_text()
        body = """
const assert = require('node:assert/strict');
window.USER_PREFS = {date_format: 'DD/MM/YYYY', measurement_system: 'metric', portion_size: 'small'};
assert.equal(portionFactor(), 0.5);
const servings = 4;
assert.equal(Math.max(0, Math.round((servings - portionFactor()) * 100) / 100), 3.5);
window.USER_PREFS.portion_size = 'large';
assert.equal(portionFactor(), 1.5);
window.USER_PREFS.portion_size = 'unknown-value';
assert.equal(portionFactor(), 1.0);
"""
        pre = "const window={}; const document={addEventListener(){}}; const navigator={};\n"
        r = subprocess.run(['node'], input=pre+script+'\n'+body, text=True, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stock_shortcut_asks_for_quantity_in_the_stock_unit(self):
        text = (ROOT / 'app/templates/stock.html').read_text()
        self.assertIn('How much ' + "' + s.name", text)
        self.assertIn('unit_id: s.unit_id', text)
        self.assertIn('Number.isFinite(amount)', text)

    def test_stock_page_counts_and_colour_coding_javascript(self):
        script = (ROOT/'app/static/app.js').read_text()
        stock = (ROOT/'app/templates/stock.html').read_text().split('<script>')[1].split('</script>')[0]
        pre = "const window={USER_PREFS:{date_format:'DD/MM/YYYY',measurement_system:'metric'}}; const document={addEventListener(){}}; const navigator={};\n"
        body = r"""
const assert = require('node:assert/strict');
const REAL_DATE = Date;
class FixedDate extends REAL_DATE {
  constructor(...a) { super(...(a.length ? a : ['2026-10-12T00:00:00'])); }
  static now() { return new FixedDate().getTime(); }
}
Date = FixedDate;
const p = stockPage();
p.items = [
  {product_id: 1, name: 'Old milk', next_expiry: '2026-10-05'},   // 7 days overdue - red
  {product_id: 2, name: 'Soon bread', next_expiry: '2026-10-13'}, // tomorrow - yellow
  {product_id: 3, name: 'Fresh beans', next_expiry: '2027-01-01'}, // green
  {product_id: 4, name: 'No date item', next_expiry: null},
];
assert.equal(p.overdueCount, 1);
assert.equal(p.soonCount, 1);
assert.equal(expiry(p.items[0].next_expiry).cls, 'bad');
assert.equal(expiry(p.items[1].next_expiry).cls, 'warn');
assert.equal(expiry(p.items[2].next_expiry).cls, 'ok');
assert.equal(expiry(p.items[3].next_expiry).cls, '');  // no date - no colour
// the main list is plain and flat now, no grouping/collapsing
assert.equal(typeof p.groups, 'undefined');
assert.equal(p.filtered().length, 4);

// stored meals - using one calls the right endpoint with the right body
const calls = [];
globalThis.api = async (path, opts={}) => { calls.push({path,...opts});
  if (path.includes('/use')) return {portions_left: 2};
  return {}; };
globalThis.toast = () => {};
p.load = () => {};
await p.useBatch({id: 7, name: 'Chilli con carne', code: 'B7'});
assert.equal(calls[0].path, '/api/cooked-batches/7/use');
assert.equal(calls[0].method, 'PUT');
assert.deepEqual(calls[0].body, {amount: 1});
"""
        r = subprocess.run(['node'], input=pre+script+'\n'+stock+'\n'+'(async()=>{\n'+body+'\n})().catch(e=>{console.error(e);process.exit(1)});',text=True,capture_output=True)
        self.assertEqual(r.returncode,0,r.stderr)

    def test_meal_idea_cards_save_shop_retry_and_reset_javascript(self):
        plan = (ROOT/'app/templates/mealplan.html').read_text().split('<script>')[1].split('</script>')[0].replace('{{ user.id }}','1')
        helpers = (ROOT/'app/static/app.js').read_text()
        pre = """
const assert=require('node:assert/strict');
const window={}, document={addEventListener(){}}, navigator={};
const calls=[]; let failSave=true, pendingResolve=null, deferIdeas=false;
const ingredients=[{name:'Bread',amount:1,unit:'pack',unit_id:3,product:'Bread',short:3,have:3,short_unit:'slice',short_unit_id:1,suggested_product_id:1},
 {name:'Butter',amount:1,unit:'piece',unit_id:2,product:null,short:1,suggested_product_id:null}];
const response={cook_events:[{name:'Toast',servings:2,ingredients,instructions:'Toast.',
  uses:[{day:'2026-10-12',slot:'dinner',portions:2,twist:''}]}],missing_cells:[]};
async function api(path,opts={}) {
 calls.push({path,...opts});
 if(path==='/api/suggestions/meals') return deferIdeas ? new Promise(resolve=>pendingResolve=resolve) : response;
 if(path==='/api/mealplan/from-cook-event') {
   if(failSave) {failSave=false; throw Error('temporary failure');}
   return {meals:[{id:7}],not_added:[{...ingredients[1],reason:'not added - no matching product'}]};
 }
 return {added:1};
}
"""
        helpers = helpers[helpers.index('function num'):].replace("function toast(msg, bad = false) { Alpine.store('toast').show(msg, bad); }", 'function toast() {}').replace("function oops(e) { toast(e.message || String(e), true); }", 'function oops() {}')
        body = """
(async()=>{
 const p=planPage(); p.load=()=>{}; p.openIdeas(); await p.getIdeas();
 const e=p.ideas.cookEvents[0]; assert.equal(e.added,false);
 assert.equal(p.leftover(e),0);
 await p.addIdeaToPlan(e); assert.equal(e.added,false); assert.equal(e.adding,false);
 await p.addIdeaToPlan(e); assert.equal(e.added,true); assert.equal(e.notAdded[0].name,'Butter');
 const saves=calls.filter(c=>c.path==='/api/mealplan/from-cook-event');
 assert.equal(saves[0].body.request_id,saves[1].body.request_id);
 assert.equal(saves[1].body.uses[0].day,'2026-10-12'); assert.equal(saves[1].body.name,'Toast');
 await p.addIdeaToPlan(e); assert.equal(calls.filter(c=>c.path==='/api/mealplan/from-cook-event').length,2);
 assert.equal(p.ideas.cookEvents.length,1);
 p.ideasOpen=false; p.openIdeas(); assert.deepEqual(p.ideas.cookEvents,[]);
 deferIdeas=true; const old=p.ideas; const pending=p.getIdeas();
 p.ideasOpen=false; p.openIdeas(); pendingResolve(response); await pending;
 assert.deepEqual(p.ideas.cookEvents,[]); assert.equal(old.busy,false);
})().catch(e=>{console.error(e);process.exit(1)});
"""
        run=subprocess.run(['node'],input=pre+helpers+plan+body,text=True,capture_output=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        html=(ROOT/'app/templates/mealplan.html').read_text()
        self.assertIn('e.added ? \'Added\'',html)
        self.assertIn("- don't have this yet",html)
        self.assertIn("qty(i.have, i.short_unit)",html)

    def test_retry_idea_replaces_just_that_cook_event_javascript(self):
        plan = (ROOT/'app/templates/mealplan.html').read_text().split('<script>')[1].split('</script>')[0].replace('{{ user.id }}','1')
        helpers = (ROOT/'app/static/app.js').read_text()
        pre = """
const assert=require('node:assert/strict');
const window={}, document={addEventListener(){}}, navigator={};
const calls=[];
const first={cook_events:[{name:'Toast',servings:2,ingredients:[],instructions:'',
  uses:[{day:'2026-10-12',slot:'dinner',portions:2,twist:''}]}],missing_cells:[{day:'2026-10-13',slot:'lunch'}]};
const second={cook_events:[{name:'Pancakes',servings:2,ingredients:[],instructions:'',
  uses:[{day:'2026-10-12',slot:'dinner',portions:2,twist:''}]}],missing_cells:[]};
let ideaCalls=0;
async function api(path,opts={}) {
 calls.push({path,...opts});
 if(path==='/api/suggestions/meals') { ideaCalls++; return ideaCalls===1 ? first : second; }
 return {added:1};
}
"""
        helpers = helpers[helpers.index('function num'):].replace("function toast(msg, bad = false) { Alpine.store('toast').show(msg, bad); }", 'function toast() {}').replace("function oops(e) { toast(e.message || String(e), true); }", 'function oops() {}')
        body = """
(async()=>{
 const p=planPage(); p.load=()=>{}; p.openIdeas(); await p.getIdeas();
 assert.equal(p.ideas.cookEvents[0].name,'Toast');
 assert.equal(p.ideas.missingCells.length,1);
 const e=p.ideas.cookEvents[0];
 await p.retryIdea(e);
 assert.equal(p.ideas.cookEvents.length,1);
 assert.equal(p.ideas.cookEvents[0].name,'Pancakes');
 assert.equal(p.ideas.cookEvents[0].added,false);
 // the retry request asked only for this event's own day/slot, not the whole week
 const retryCall=calls.filter(c=>c.path==='/api/suggestions/meals').at(-1);
 assert.deepEqual(retryCall.body.cells,[{day:'2026-10-12',slot:'dinner'}]);
 // the original missing cell is untouched by a retry on a different cell
 assert.equal(p.ideas.missingCells.length,1);
})().catch(e=>{console.error(e);process.exit(1)});
"""
        run=subprocess.run(['node'],input=pre+helpers+plan+body,text=True,capture_output=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
        html=(ROOT/'app/templates/mealplan.html').read_text()
        self.assertIn('Try something different',html)
