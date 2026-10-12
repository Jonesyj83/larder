import json
import sqlite3
import unittest
from unittest.mock import patch
from fastapi import HTTPException
import test_planning
from app.routes import suggestions as sg
from app.routes.mealplan import MealEdit, edit_meal, needs, plan_to_shopping
from app import providers

class Discovery(unittest.TestCase):
    setUp = test_planning.Planning.setUp
    tearDown = test_planning.Planning.tearDown
    meal = test_planning.Planning.meal
    count = test_planning.Planning.count

    def test_preview_reject_accept_and_provenance(self):
        self.db.execute("UPDATE units SET name_plural='slices' WHERE name='slice'")
        meal = self.meal([])
        before = self.db.total_changes
        with patch.object(sg, '_run', return_value=json.dumps({'name':'Toast','servings':1,'ingredients':[{'name':'Bread','amount':2,'unit':'slices'}]})):
            draft = sg.discover_ingredients(sg.DiscoveryIn(idea='Toast',servings=1,generate=True),self.db,self.user)
        self.assertEqual(self.db.total_changes,before)
        self.assertEqual(self.count('products'),0)
        i = draft['recipe']['ingredients'][0]
        edit_meal(meal['id'],MealEdit(ingredients=[{'name':i['name'],'amount':i['amount'],'unit_id':i['unit_id']}]),self.db,self.user)
        n = needs('2026-10-12','2026-10-18',self.db,self.user)['items'][0]
        self.assertEqual((n['need'],n['have'],n['missing']),(2,0,2))
        self.assertEqual(self.count('shopping_list'),0)
        plan_to_shopping('2026-10-12','2026-10-18',self.db,self.user)
        from app.routes.shopping import list_items
        self.assertEqual(list_items(self.db,self.user)[0]['sources'][0]['kind'],'meal_plan')

    def test_recipe_idea_avoids_repeating_recent_suggestions_unless_asked(self):
        with patch.object(sg,'_run',return_value=json.dumps({'name':'Chilli con carne','servings':2,'ingredients':[{'name':'Beef','amount':500,'unit':'g'}]})):
            sg.recipe_idea(sg.RecipeIdeaIn(idea='',use_expiring=True),self.db,self.user)
        captured = {}
        def fake_run(db,user,prompt,system):
            captured['prompt'] = prompt
            return sg.GeneratedText(json.dumps({'name':'Fried rice','servings':2,'ingredients':[{'name':'Rice','amount':200,'unit':'g'}]}))
        with patch.object(sg,'_run',side_effect=fake_run):
            sg.recipe_idea(sg.RecipeIdeaIn(idea='',use_expiring=True),self.db,self.user)
        self.assertIn('Chilli con carne',captured['prompt'])
        names = [r['name'] for r in sg.rows(self.db.execute('SELECT name FROM recent_suggestions ORDER BY id'))]
        self.assertEqual(names,['Chilli con carne','Fried rice'])
        # an explicit request isn't suppressed by recent history
        captured.clear()
        with patch.object(sg,'_run',side_effect=fake_run):
            sg.recipe_idea(sg.RecipeIdeaIn(idea='chilli con carne again please',use_expiring=False),self.db,self.user)
        self.assertNotIn('Already suggested recently',captured['prompt'])
        # only the last 10 are kept
        for _ in range(12):
            with patch.object(sg,'_run',return_value=json.dumps({'name':'X','servings':1,'ingredients':[{'name':'Y','amount':1,'unit':'piece'}]})):
                sg.recipe_idea(sg.RecipeIdeaIn(idea='',use_expiring=False),self.db,self.user)
        self.assertEqual(self.count('recent_suggestions'),10)

    def test_meal_ideas_cell_validation(self):
        user = {'id': 1, 'username': 'test', 'portion_size': 'regular'}
        with self.assertRaises(HTTPException):
            sg.meal_ideas(sg.MealIdeasIn(cells=[]), self.db, user)
        with self.assertRaises(HTTPException):
            sg.meal_ideas(sg.MealIdeasIn(cells=[{'day': '2026-02-30', 'slot': 'dinner'}]), self.db, user)
        with self.assertRaises(HTTPException):
            sg.meal_ideas(sg.MealIdeasIn(cells=[{'day': '2026-10-12', 'slot': 'brunch'}]), self.db, user)
        with self.assertRaises(HTTPException):
            sg.meal_ideas(sg.MealIdeasIn(cells=[{'day': '2026-10-12', 'slot': 'dinner'}] * 61), self.db, user)

    def test_meal_ideas_prompt_groups_by_day_and_keeps_context(self):
        user = {'id': 1, 'username': 'test', 'portion_size': 'regular'}
        sg.save_preferences(sg.PreferencesIn(diet=['Vegetarian'], dislikes=['Mushrooms'], other='no rice'), self.db, user)
        self.db.execute("INSERT INTO locations (id,name) VALUES (1,'Freezer')")
        self.db.execute("INSERT INTO products (id,name,unit_id) VALUES (1,'Chicken Tenders',2)")
        self.db.commit()
        from app.stock import purchase
        purchase(self.db, 1, 500, 2, location_id=1, user_id=1)
        captured = {}
        def fake_run(db, user, prompt, system):
            captured['prompt'] = prompt
            return sg.GeneratedText('ideas')
        with patch.object(sg, '_run', side_effect=fake_run):
            sg.meal_ideas(sg.MealIdeasIn(cells=[
                {'day': '2026-10-21', 'slot': 'dinner'},
                {'day': '2026-10-20', 'slot': 'lunch'},
                {'day': '2026-10-20', 'slot': 'dinner'},
            ], note='nothing spicy'), self.db, user)
        prompt = captured['prompt']
        self.assertIn('Tue 20 Oct: lunch, dinner', prompt)
        self.assertIn('Wed 21 Oct: dinner', prompt)
        self.assertLess(prompt.index('Tue 20 Oct'), prompt.index('Wed 21 Oct'))
        self.assertIn('Vegetarian', prompt)
        self.assertIn('Mushrooms', prompt)
        self.assertIn('Chicken Tenders', prompt)
        self.assertIn('nothing spicy', prompt)

    def test_kitchen_text_tags_the_location_for_frozen_items(self):
        self.db.execute("INSERT INTO locations (id,name) VALUES (1,'Freezer'),(2,'Pantry')")
        self.db.execute("INSERT INTO products (id,name,unit_id) VALUES (1,'Chicken Tenders',2),(2,'Rice',2)")
        self.db.commit()
        from app.stock import purchase
        purchase(self.db,1,500,2,location_id=1,user_id=1)
        purchase(self.db,2,1,2,location_id=2,user_id=1)
        text = sg._kitchen(self.db)
        self.assertIn('Chicken Tenders: 500 piece [Freezer]',text)
        self.assertIn('Rice: 1 piece [Pantry]',text)

    def test_household_preferences_saved_and_fed_into_every_prompt(self):
        saved = sg.save_preferences(sg.PreferencesIn(diet=['Vegetarian',' ','Vegetarian'],
            dislikes=['Mushrooms',' olives ',''],other=' no rice, use Doritos '),self.db,self.user)
        self.assertEqual(saved['diet'],['Vegetarian','Vegetarian'])  # exact dupes aren't deduped, that's fine - UI sends checkboxes
        self.assertEqual(saved['dislikes'],['Mushrooms','olives'])
        self.assertEqual(saved['other'],'no rice, use Doritos')
        self.assertIn('Vegetarian',sg._preferences_text(self.db))
        self.assertIn('Mushrooms',sg._preferences_text(self.db))
        self.assertIn('Doritos',sg._preferences_text(self.db))
        # it reaches an actual recipe prompt, not just the helper
        captured = {}
        def fake_run(db,user,prompt,system):
            captured['prompt'] = prompt
            return sg.GeneratedText(json.dumps({'name':'X','servings':1,'ingredients':[{'name':'Y','amount':1,'unit':'piece'}]}))
        with patch.object(sg,'_run',side_effect=fake_run):
            sg.recipe_idea(sg.RecipeIdeaIn(idea='',use_expiring=False),self.db,self.user)
        self.assertIn('Vegetarian',captured['prompt'])
        self.assertIn('Mushrooms',captured['prompt'])
        # with nothing set, the prompt doesn't carry an empty preferences block
        sg.save_preferences(sg.PreferencesIn(diet=[],dislikes=[],other=''),self.db,self.user)
        self.assertEqual(sg._preferences_text(self.db),'')
        # a long list is capped by the request schema, not silently truncated into garbage
        with self.assertRaises(Exception):
            sg.PreferencesIn(dislikes=['x']*51)
        tags = sg.get_preferences(self.db,self.user)['diet_tags']
        self.assertIn('No seafood',tags)
        self.assertGreaterEqual(len(tags),10)

    def test_saved_search_without_provider_and_bad_payloads(self):
        self.meal([{'name':'Bread','amount':2,'unit_id':1}])
        with patch.object(sg,'_run',side_effect=AssertionError('no generation')):
            found=sg.discover_ingredients(sg.DiscoveryIn(idea='Toast',servings=3),self.db,self.user)
        self.assertEqual(found['matches'][0]['ingredients'][0]['amount'],6)
        before=self.db.total_changes
        for bad in ({'servings':1,'ingredients':[{'name':'Bread','unit':'slice'}]}, {'servings':float('nan'),'ingredients':[]}, {'servings':1,'ingredients':[{'name':'Bread','amount':-1,'unit':'slice'}]}):
            with patch.object(sg,'_run',return_value=json.dumps(bad)), self.assertRaises(HTTPException):
                sg.discover_ingredients(sg.DiscoveryIn(idea='Toast',generate=True),self.db,self.user)
        self.assertEqual(self.db.total_changes,before)
        draft=sg.review_recipe(self.db,{'servings':1,'ingredients':[{'name':'Flour','amount':2,'unit':'handful'}]},1)
        self.assertIsNone(draft['ingredients'][0]['unit_id'])

    def test_review_recipe_shows_what_is_short_and_add_missing(self):
        from app.stock import purchase
        from app.routes.shopping import list_items
        self.db.execute("INSERT INTO units(id,name) VALUES(3,'pack')")
        self.db.execute("INSERT INTO products(id,name,unit_id,purchase_unit_id,purchase_factor) VALUES(1,'Bread',1,3,6)")
        self.db.commit()
        purchase(self.db,1,3,1,user_id=1)  # 3 slices on hand
        draft = sg.review_recipe(self.db,{'servings':1,'ingredients':[
            {'name':'Bread','amount':1,'unit':'pack'}, {'name':'Chicken','amount':500,'unit':'piece'}]})
        bread, chicken = draft['ingredients']
        self.assertEqual((bread['have'],bread['short']),(3,3))  # 1 pack = 6 slices, have 3, short 3
        self.assertEqual((bread['short_unit'],bread['short_unit_id']),('slice',1))  # short is in the product's stock unit, not the requested one
        self.assertIsNone(chicken['product'])
        self.assertEqual(chicken['short'],500)  # don't have it at all - short the full amount
        added = sg.add_missing_from_suggestion(sg.AddMissingIn(name=draft['name'],ingredients=[
            sg.MissingIngredientIn(name=i['name'],amount=i['short'],unit=i['short_unit'],unit_id=i['short_unit_id'],
                                    suggested_product_id=i['suggested_product_id'])
            for i in draft['ingredients'] if not i['product'] or i['short'] > 0]),self.db,self.user)
        self.assertEqual(added['added'],2)
        items = {i['label']: i for i in list_items(self.db,self.user)}
        self.assertEqual(items['Bread']['amount'],3)
        self.assertEqual(items['Chicken']['amount'],500)
        # asking again shouldn't double it up - listed_amount already covers it
        again = sg.add_missing_from_suggestion(sg.AddMissingIn(name=draft['name'],ingredients=[
            sg.MissingIngredientIn(name='Bread',amount=3,unit='slice',unit_id=1,suggested_product_id=1)]),self.db,self.user)
        self.assertEqual(again['added'],0)
        self.assertEqual({i['label']: i['amount'] for i in list_items(self.db,self.user)}['Bread'],3)

    def test_unknown_unit_does_not_guess_stock_and_bad_shopping_is_atomic(self):
        from app.stock import purchase
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        purchase(self.db, 1, 20, 1, user_id=1)
        draft = sg.review_recipe(self.db, {'servings':1, 'ingredients':[
            {'name':'Bread','amount':2,'unit':'handful'}]})
        i = draft['ingredients'][0]
        self.assertIsNone(i['have'])
        self.assertIsNone(i['short'])
        for bad in ({'name':'Bread','amount':2,'unit':'handful','suggested_product_id':1},
                    {'name':'Other','amount':1,'unit_id':999}):
            with self.assertRaises(HTTPException) as error:
                sg.add_missing_from_suggestion(sg.AddMissingIn(ingredients=[
                    {'name':'Milk','amount':1}, bad]), self.db, self.user)
            self.assertEqual(error.exception.status_code, 400)
            self.assertEqual(self.count('shopping_list'), 0)

    def test_repeated_suggestion_lines_and_free_text_retry(self):
        from app.stock import purchase
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        purchase(self.db, 1, 1, 1, user_id=1)
        draft = sg.review_recipe(self.db, {'servings':1, 'ingredients':[
            {'name':'Bread','amount':2,'unit':'slice'},
            {'name':'Bread','amount':2,'unit':'slice'}]})
        self.assertEqual([i['short'] for i in draft['ingredients']], [1, 2])
        data = sg.AddMissingIn(ingredients=[
            {'name':'Bread','amount':i['short'],'unit_id':1,'suggested_product_id':1}
            for i in draft['ingredients']] + [{'name':'Milk','amount':2,'unit':'bottle'}])
        self.assertEqual(sg.add_missing_from_suggestion(data, self.db, self.user)['added'], 2)
        self.assertEqual(sg.add_missing_from_suggestion(data, self.db, self.user)['added'], 0)
        amounts = [r[0] for r in self.db.execute('SELECT amount FROM shopping_list ORDER BY id')]
        self.assertEqual(amounts, [3, 2])
        self.assertEqual(self.db.execute('SELECT note FROM shopping_list WHERE product_id IS NULL').fetchone()[0],
                         'Proposed unit: bottle')

class ProviderSafety(unittest.TestCase):
    def test_sanitized_upstream_errors(self):
        import httpx
        transport = httpx.MockTransport(lambda request: httpx.Response(401,text='secret-token'))
        with patch.object(providers,'_transport',transport):
            with self.assertRaises(providers.ProviderError) as e: providers._post('https://example.invalid',{}, {})
        self.assertNotIn('secret-token',str(e.exception))
    def test_standalone_and_configuration(self):
        self.assertEqual({p['slug'] for p in providers.catalogue()},set(providers.PROVIDERS))
        with self.assertRaises(providers.ProviderError): providers.validate_settings('openai','m','https://user:secret@example.invalid')
    def test_opencode_go_sends_session_header(self):
        import httpx
        captured = {}
        def fake_post(request):
            captured['headers'] = request.headers
            return httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}]})
        with patch.object(providers,'_transport',httpx.MockTransport(fake_post)):
            text = providers.opencode_go('hi','key','deepseek-v4.1-flash',None)
        self.assertEqual(text,'ok')
        self.assertIn('x-opencode-session',captured['headers'])

class Provenance(unittest.TestCase):
    setUp = test_planning.Planning.setUp
    tearDown = test_planning.Planning.tearDown
    def test_legacy_unknown_and_multiple_sources(self):
        from app.routes.shopping import list_items, add_item
        self.db.execute("INSERT INTO shopping_list(name,amount) VALUES('Bread',1)")
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
    def test_preview_reject_review_and_explicit_save_javascript(self):
        import subprocess
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        script=(root/'app/templates/mealplan.html').read_text().split('<script>')[1].split('</script>')[0].replace('{{ user.id }}','1')
        helpers='function selectIngredient'+(root/'app/static/app.js').read_text().split('function selectIngredient')[1]
        pre="""
const assert=require('node:assert/strict'), window={}, calls=[];
const today=()=> '2026-10-12', addDays=x=>x, toast=()=>{}, oops=e=>{throw e};
async function api(path,opts={}) {
 calls.push({path,...opts});
 if(path==='/api/suggestions/ingredients') return {matches:[],recipe:{ingredients:[{name:'Bread',amount:2,unit_id:1}]},message:'Draft only'};
 return [];
}
"""
        body="""
(async()=>{
 const p=planPage(); p.load=()=>{};
 await p.openEdit({id:7,day:'2026-10-12',slot:'breakfast',note:'Toast',servings:1,recipe_id:null});
 await p.findIngredients(true);
 assert.equal(p.adding.ingredients.length,0);
 p.discovery.recipe=null; assert.equal(calls.filter(c=>c.method==='PUT').length,0);
 await p.findIngredients(true); p.reviewDraft(p.discovery.recipe);
 assert.equal(p.adding.ingredients[0].amount,2); assert.equal(p.adding.note,'Toast');
 assert.equal(p.adding.day,'2026-10-12'); assert.equal(p.adding.servings,1);
 assert.equal(calls.filter(c=>c.method==='PUT').length,0);
 await p.add(); assert.equal(calls.filter(c=>c.method==='PUT').length,1);
 assert.equal(calls.some(c=>c.path.includes('shopping')),false);
 const r=recipesPage(); r.idea.result={recipe:{name:'Toast',servings:1,instructions:'Toast',ingredients:[{name:'Bread',amount:2,unit_id:1}]}};
 await r.saveIdea(); assert.equal(r.form.ingredients.length,1); assert.equal(r.form.ingredients[0].name,'Bread');
 assert.equal(calls.filter(c=>c.path==='/api/recipes' && c.method==='POST').length,0);
})().catch(e=>{console.error(e);process.exit(1)});
"""
        recipes=(root/'app/templates/recipes.html').read_text().split('<script>')[1].split('</script>')[0]
        run=subprocess.run(['node'],input=pre+helpers+script+recipes+body,text=True,capture_output=True,timeout=15)
        self.assertEqual(run.returncode,0,run.stderr)
