import sqlite3
import unittest
from fastapi import HTTPException
from pydantic import ValidationError
from app.db import SCHEMA
from app.routes.mealplan import MealIn, MealEdit, add_meal, edit_meal, needs, plan_to_shopping
from app.routes.recipes import RecipeIn, add_recipe
from app.stock import purchase

class Planning(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript(SCHEMA)
        self.db.execute("INSERT INTO shopping_lists(id,name,kind) VALUES(1,'Shopping','order')")
        self.db.execute("INSERT INTO users(id,username,password_hash) VALUES(1,'test','unused')")
        self.db.executemany('INSERT INTO units(id,name) VALUES(?,?)', [(1,'slice'),(2,'piece')])
        self.db.commit()
        self.user = {'id':1}
    def tearDown(self): self.db.close()
    def meal(self, ingredients, **kw):
        return add_meal(MealIn(day='2026-10-12',slot='breakfast',note='Toast',servings=1,ingredients=ingredients,**kw),self.db,self.user)
    def count(self, table): return self.db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
    def test_plan_before_purchase_and_edit(self):
        m = self.meal([{'name':'Bread','amount':2,'unit_id':1}])
        n = needs('2026-10-12','2026-10-18',self.db,self.user)['items'][0]
        self.assertEqual((n['need'],n['have'],n['missing']), (2,0,2))
        self.assertEqual((self.count('stock'),self.count('stock_log')), (0,0))
        for expected in (1,0): self.assertEqual(plan_to_shopping('2026-10-12','2026-10-18',self.db,self.user)['added'],expected)
        purchase(self.db,n['id'],2,1,user_id=1)
        self.assertEqual(needs('2026-10-12','2026-10-18',self.db,self.user)['items'][0]['missing'],0)
        for _ in range(3):
            edited = edit_meal(m['id'],MealEdit(ingredients=[{'product_id':n['id'],'amount':3,'unit_id':1}]),self.db,self.user)
            self.assertEqual(edited['recipe_id'],m['recipe_id'])
        self.assertEqual(self.count('recipes'),1)
        edit_meal(m['id'],MealEdit(servings=2),self.db,self.user)
        self.assertEqual(needs('2026-10-12','2026-10-18',self.db,self.user)['items'][0]['need'],6)
    def test_atomic_retry_duplicate_and_validation(self):
        for _ in range(2):
            with self.assertRaises(HTTPException):
                self.meal([{'name':'Bread','amount':2,'unit_id':1},{'name':'Butter','amount':1,'unit_id':999}])
            self.assertEqual((self.count('products'),self.count('recipes'),self.count('meal_plan')),(0,0,0))
        self.meal([{'name':'Bread','amount':2,'unit_id':1}])
        with self.assertRaises(HTTPException): self.meal([{'name':'  BREAD  ','amount':1,'unit_id':1}])
        self.assertEqual(self.count('products'),1)
        self.meal([{'product_id':1,'amount':1,'unit_id':2}])
        self.assertTrue(needs('2026-10-12','2026-10-18',self.db,self.user)['problems'])
        for amount in (0,-1,float('inf'),float('nan')):
            with self.assertRaises(ValidationError): RecipeIn(name='Bad',servings=amount)
            with self.assertRaises(ValidationError): RecipeIn(name='Bad',ingredients=[{'name':'Bad','unit_id':1,'amount':amount}])
    def test_recipe_new_ingredient_and_note(self):
        r=add_recipe(RecipeIn(name='Toast',servings=1,ingredients=[{'name':'Bread','amount':2,'unit_id':1}]),self.db,self.user)
        self.assertEqual(r['ingredients'][0]['have'],0)
        m=self.meal([])
        self.assertEqual(needs('2026-10-12','2026-10-18',self.db,self.user)['unmapped'][0]['id'],m['id'])
        self.assertEqual(self.count('products'),1)

    def test_lost_response_retry_and_failed_request(self):
        draft=[{'name':'Bread','amount':2,'unit_id':1}]
        m=self.meal(draft,request_id='retry')
        self.assertEqual(self.meal(draft,request_id='retry'),m)
        self.assertEqual((self.count('products'),self.count('recipes'),self.count('meal_plan')),(1,1,1))
        self.db.execute("CREATE TRIGGER fail_meal BEFORE INSERT ON meal_plan BEGIN SELECT RAISE(ABORT,'failed request'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.meal([{'name':'Butter','amount':1,'unit_id':1}],request_id='failed')
        self.assertEqual((self.count('products'),self.count('recipes'),self.count('planning_requests')),(1,1,1))
        self.db.execute('DROP TRIGGER fail_meal')
        self.meal([{'name':'Butter','amount':1,'unit_id':1}],request_id='failed')
        self.assertEqual(self.count('products'),2)

    def test_recipe_rollback_duplicate_and_shared_meal_edit(self):
        from app.routes.recipes import edit_recipe, missing_to_shopping, check
        r=add_recipe(RecipeIn(name='Toast',servings=1,ingredients=[{'name':'Bread','amount':2,'unit_id':1}]),self.db,self.user)
        for expected in (1,0): self.assertEqual(missing_to_shopping(r['id'],None,self.db,self.user)['added'],expected)
        with self.assertRaises(HTTPException):
            edit_recipe(r['id'],RecipeIn(name='Changed',ingredients=[{'name':'Butter','amount':1,'unit_id':1},{'product_id':999,'amount':1}]),self.db,self.user)
        self.assertEqual(check(self.db,r['id'])['name'],'Toast')
        self.assertEqual(self.count('products'),1)
        m=add_meal(MealIn(day='2026-10-12',slot='breakfast',recipe_id=r['id'],servings=1),self.db,self.user)
        edited=edit_meal(m['id'],MealEdit(note='Different toast',ingredients=[{'product_id':1,'amount':5,'unit_id':1}]),self.db,self.user)
        self.assertNotEqual(edited['recipe_id'],r['id'])
        self.assertEqual(check(self.db,r['id'])['ingredients'][0]['amount'],2)
        for bad in (0,-1,float('inf'),float('nan')):
            with self.assertRaises(HTTPException): check(self.db,r['id'],bad)

    def test_invalid_date_name_unit_and_duplicate_rows(self):
        for day in ('2026-02-30','12/10/2026'):
            with self.assertRaises(HTTPException): add_meal(MealIn(day=day,slot='lunch',note='Toast',ingredients=[{'name':'Bread','amount':2,'unit_id':1}]),self.db,self.user)
        for ingredient in ({'name':'','unit_id':1,'amount':1},{'name':'Bread','amount':1},{'product_id':999,'amount':1}):
            with self.assertRaises(HTTPException): self.meal([ingredient])
        self.assertEqual((self.count('products'),self.count('recipes')),(0,0))
        self.meal([{'name':'Bread','amount':1,'unit_id':1},{'name':' bread ','amount':1,'unit_id':1}])
        self.assertEqual(self.count('products'),1)
        self.assertEqual(needs('2026-10-12','2026-10-18',self.db,self.user)['items'][0]['need'],2)

    def test_repeated_product_uses_stock_once_and_shops_total_shortfall(self):
        from app.routes.recipes import check, missing_to_shopping, cook
        r = add_recipe(RecipeIn(name='Toast twice', servings=1, ingredients=[
            {'name':'Bread','amount':2,'unit_id':1},
            {'name':'Bread','amount':2,'unit_id':1}]), self.db, self.user)
        pid = r['ingredients'][0]['product_id']
        purchase(self.db, pid, 3, 1, user_id=1)
        checked = check(self.db, r['id'])
        self.assertFalse(checked['can_make'])
        self.assertEqual([i['missing'] for i in checked['ingredients']], [0, 1])
        self.assertEqual(missing_to_shopping(r['id'], None, self.db, self.user)['added'], 1)
        self.assertEqual(self.db.execute('SELECT amount FROM shopping_list').fetchone()[0], 1)
        self.assertEqual(missing_to_shopping(r['id'], None, self.db, self.user)['added'], 0)
        cook(r['id'], None, db=self.db, user=self.user)
        self.assertEqual(check(self.db, r['id'])['missing_count'], 2)
        missing_to_shopping(r['id'], None, self.db, self.user)
        self.assertEqual(self.db.execute('SELECT amount FROM shopping_list').fetchone()[0], 4)

    def test_from_cook_event_only_existing_products_and_retry(self):
        from app.routes.mealplan import CookEventIn, meal_from_cook_event
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        data = CookEventIn(request_id='cook_event-retry',
            name='Toast',servings=2,instructions='Toast the bread.',ingredients=[
                {'name':'BREAD','amount':4,'unit':'slice'},
                {'name':'Butter','amount':1,'unit':'piece'}],
            uses=[{'day':'2026-10-12','slot':'breakfast','portions':2,'twist':''}])
        result = meal_from_cook_event(data,self.db,self.user)
        self.assertEqual(meal_from_cook_event(data,self.db,self.user),result)
        # Butter had no matching product but a real unit, so it got its own product
        # created on the spot rather than falling out of the recipe silently
        self.assertEqual((self.count('products'),self.count('meal_plan'),self.count('recipes')),(2,1,1))
        m = result['meals'][0]
        self.assertEqual((m['day'],m['slot'],m['note'],m['servings'],m['simple_meal']),
                         ('2026-10-12','breakfast','Toast',2,1))
        self.assertEqual(self.db.execute('SELECT product_id,amount,unit_id FROM recipe_ingredients WHERE product_id=1').fetchone()[:],(1,4,1))
        self.assertEqual(self.db.execute('SELECT instructions FROM recipes').fetchone()[0],'Toast the bread.')
        self.assertEqual(result['not_added'],[])
        # Bread matched but had zero stock, so its shortfall is queued too - not just
        # the brand-new Butter product; "Add to plan" now covers everything needed
        self.assertEqual(tuple(self.db.execute('SELECT amount FROM shopping_list WHERE product_id=1').fetchone()),(4,))
        butter = self.db.execute("SELECT id,unit_id FROM products WHERE name='Butter'").fetchone()
        self.assertEqual(tuple(self.db.execute('SELECT amount,unit_id FROM recipe_ingredients WHERE product_id=?',(butter[0],)).fetchone()),(1,butter[1]))
        # it starts at zero stock, so the full amount landed straight on the shopping list
        self.assertEqual(tuple(self.db.execute('SELECT amount FROM shopping_list WHERE product_id=?',(butter[0],)).fetchone()),(1,))

    def test_single_slot_cook_event_keeps_batch_servings_separate_from_portions(self):
        from app.routes.mealplan import CookEventIn, meal_from_cook_event
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Eggs',2)")
        self.db.commit()
        data = CookEventIn(name='Omelette', servings=4, instructions='Cook four servings.',
            ingredients=[{'name':'Eggs','amount':8,'unit':'piece'}],
            uses=[{'day':'2026-10-21','slot':'lunch','portions':0.5,'twist':''}])
        result = meal_from_cook_event(data,self.db,self.user)
        meal = result['meals'][0]
        recipe = self.db.execute('SELECT servings FROM recipes WHERE id=?',(meal['recipe_id'],)).fetchone()
        need = needs('2026-10-21','2026-10-21',self.db,self.user)['items'][0]['need']
        self.assertEqual(recipe['servings'],4)
        self.assertEqual(meal['servings'],0.5)
        self.assertEqual(need,8)
        event_ids = {r['cook_event_id'] for r in self.db.execute('SELECT cook_event_id FROM meal_plan')}
        self.assertEqual(len(event_ids),1)
        self.assertIsNotNone(next(iter(event_ids)))

    def test_cook_event_does_not_reuse_different_recipe_just_by_name(self):
        from app.routes.mealplan import CookEventIn, meal_from_cook_event
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Beef Mince',2)")
        self.db.commit()
        saved = add_recipe(RecipeIn(name='Chilli con carne', servings=4,
            ingredients=[{'product_id':1,'amount':500,'unit_id':2}]),self.db,self.user)
        data = CookEventIn(name='Chilli con carne',servings=2,instructions='Brown the mince.',
            ingredients=[{'name':'Beef Mince','amount':2,'unit':'piece'}],
            uses=[{'day':'2026-10-20','slot':'dinner','portions':2,'twist':''}])
        result = meal_from_cook_event(data,self.db,self.user)
        self.assertFalse(result['reused_recipe'])
        self.assertNotEqual(result['meals'][0]['recipe_id'],saved['id'])
        self.assertEqual(self.count('recipes'),2)
        self.assertEqual(self.db.execute('SELECT amount FROM recipe_ingredients WHERE recipe_id=?',
                         (result['meals'][0]['recipe_id'],)).fetchone()[0],2)

    def test_cook_event_covers_several_uses_from_one_recipe(self):

        from app.routes.mealplan import CookEventIn, meal_from_cook_event
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Beef Mince',2)")
        self.db.commit()
        data = CookEventIn(name='Chilli con carne', servings=4, instructions='Brown it.',
            ingredients=[{'name': 'Beef Mince', 'amount': 4, 'unit': 'piece'}],
            uses=[{'day': '2026-10-20', 'slot': 'dinner', 'portions': 2, 'twist': ''},
                  {'day': '2026-10-22', 'slot': 'lunch', 'portions': 1, 'twist': 'as jaffles'}])
        result = meal_from_cook_event(data, self.db, self.user)
        self.assertEqual(self.count('recipes'), 1)  # one batch, not two separate dishes
        self.assertEqual(len(result['meals']), 2)
        fresh, twist = result['meals']
        self.assertEqual((fresh['day'], fresh['slot'], fresh['note'], fresh['servings']),
                         ('2026-10-20', 'dinner', 'Chilli con carne', 2))
        self.assertEqual((twist['day'], twist['slot'], twist['note'], twist['servings']),
                         ('2026-10-22', 'lunch', 'as jaffles (Chilli con carne)', 1))
        self.assertEqual(fresh['recipe_id'], twist['recipe_id'])  # same dish, same batch

    def test_from_cook_event_unknown_and_invalid_units_are_reported(self):
        from app.routes.mealplan import CookEventIn, meal_from_cook_event
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        result = meal_from_cook_event(CookEventIn(name='Toast',servings=1,
            ingredients=[{'name':'Bread','amount':1,'unit':'handful'},
                         {'name':'Bread','amount':1,'unit':'piece'},
                         {'name':'Butter','amount':1,'unit':'piece'}],
            uses=[{'day':'2026-10-12','slot':'lunch','portions':1,'twist':''}]),self.db,self.user)
        # the two Bread lines matched a product but used a unit that doesn't convert to
        # its stock unit - those still get reported. Butter had no product at all but a
        # real, recognised unit, so it got created rather than falling out of the recipe
        self.assertEqual(len(result['not_added']),2)
        self.assertIn('valid unit',result['not_added'][0]['reason'])
        self.assertIsNotNone(result['meals'][0]['recipe_id'])
        self.assertEqual((self.count('products'),self.count('recipe_ingredients')),(2,1))

    def test_from_cook_event_failure_rolls_back(self):
        from app.routes.mealplan import CookEventIn, meal_from_cook_event
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        self.db.execute("CREATE TRIGGER fail_cook_event BEFORE INSERT ON meal_plan BEGIN SELECT RAISE(ABORT,'failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            meal_from_cook_event(CookEventIn(name='Toast',servings=1,
                ingredients=[{'name':'Bread','amount':1,'unit':'slice'}],
                uses=[{'day':'2026-10-12','slot':'lunch','portions':1,'twist':''}]),self.db,self.user)
        self.assertEqual((self.count('products'),self.count('recipes'),self.count('meal_plan')),(1,0,0))

    def test_recipe_rating(self):
        from app.routes.recipes import RatingIn, rate_recipe
        r = add_recipe(RecipeIn(name='Toast', servings=1, ingredients=[]), self.db, self.user)
        self.assertEqual(r['rating'], 0)  # unrated by default
        rate_recipe(r['id'], RatingIn(rating=4), self.db, self.user)
        self.assertEqual(self.db.execute('SELECT rating FROM recipes WHERE id=?', (r['id'],)).fetchone()[0], 4)
        # clearing it back to 0 is a real action
        rate_recipe(r['id'], RatingIn(rating=0), self.db, self.user)
        self.assertEqual(self.db.execute('SELECT rating FROM recipes WHERE id=?', (r['id'],)).fetchone()[0], 0)
        with self.assertRaises(ValidationError):
            RatingIn(rating=6)
        with self.assertRaises(ValidationError):
            RatingIn(rating=-1)
        with self.assertRaises(HTTPException):
            rate_recipe(99999, RatingIn(rating=3), self.db, self.user)

    def test_cooked_batches_stored_used_and_pulled_into_the_plan(self):
        from app.routes.recipes import cook
        from app.routes.batches import list_batches, use_batch, remove_batch, UseIn
        from app.routes.mealplan import FromBatchIn, meal_from_batch
        self.db.execute("INSERT INTO locations(id,name,is_freezer) VALUES(1,'Freezer',1),(2,'Fridge',0)")
        self.db.commit()
        pid = self.db.execute("INSERT INTO products(name,unit_id) VALUES('Beef Mince',2)").lastrowid
        purchase(self.db, pid, 4, 2, user_id=1)
        r = add_recipe(RecipeIn(name='Chilli con carne', servings=4,
            ingredients=[{'product_id': pid, 'amount': 4, 'unit_id': 2}]), self.db, self.user)
        # cooking it with no leftovers specified: no batch, same as before
        plain = cook(r['id'], None, db=self.db, user=self.user)
        self.assertIsNone(plain['batch'])
        self.assertEqual(self.count('cooked_batches'), 0)
        # must say where the leftovers are going
        purchase(self.db, pid, 4, 2, user_id=1)
        with self.assertRaises(HTTPException):
            cook(r['id'], None, leftover_portions=3, db=self.db, user=self.user)
        result = cook(r['id'], None, leftover_portions=3, location_id=1, db=self.db, user=self.user)
        batch = result['batch']
        self.assertEqual(batch['portions_left'], 3)
        self.assertEqual(batch['name'], 'Chilli con carne')
        self.assertEqual(batch['location'], 'Freezer')
        self.assertTrue(batch['is_freezer'])
        self.assertTrue(batch['code'].startswith('B'))
        self.assertEqual(len(list_batches(self.db, self.user)), 1)
        # use it in the meal plan - no stock gets touched, it was already taken when cooked
        before = self.db.execute('SELECT amount FROM stock').fetchall()
        used = meal_from_batch(FromBatchIn(day='2026-10-12', slot='dinner', batch_id=batch['id']), self.db, self.user)
        self.assertEqual(used['portions_left'], 2)
        self.assertEqual(used['meal']['note'], 'Chilli con carne')
        self.assertEqual(used['meal']['batch_id'], batch['id'])
        self.assertEqual(self.db.execute('SELECT amount FROM stock').fetchall(), before)
        # using it down to zero clears the batch entirely, and un-links the meal that used the last of it
        use_batch(batch['id'], UseIn(amount=1), self.db, self.user)
        last = meal_from_batch(FromBatchIn(day='2026-10-13', slot='dinner', batch_id=batch['id']), self.db, self.user)
        self.assertEqual(last['portions_left'], 0)
        self.assertEqual(self.count('cooked_batches'), 0)
        self.assertEqual(len(list_batches(self.db, self.user)), 0)
        self.assertIsNone(self.db.execute('SELECT batch_id FROM meal_plan WHERE id=?', (last['meal']['id'],)).fetchone()[0])
        # gone batches can't be used again
        with self.assertRaises(HTTPException):
            meal_from_batch(FromBatchIn(day='2026-10-14', slot='dinner', batch_id=batch['id']), self.db, self.user)
        with self.assertRaises(HTTPException):
            use_batch(batch['id'], UseIn(amount=1), self.db, self.user)
        # a leftover batch can be thrown out manually too
        another = cook(r['id'], None, leftover_portions=2, location_id=2, db=self.db, user=self.user)['batch']
        remove_batch(another['id'], self.db, self.user)
        self.assertEqual(self.count('cooked_batches'), 0)
