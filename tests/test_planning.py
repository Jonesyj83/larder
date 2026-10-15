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
        cook(r['id'], None, self.db, self.user)
        self.assertEqual(check(self.db, r['id'])['missing_count'], 2)
        missing_to_shopping(r['id'], None, self.db, self.user)
        self.assertEqual(self.db.execute('SELECT amount FROM shopping_list').fetchone()[0], 4)

    def test_from_suggestion_only_existing_products_and_retry(self):
        from app.routes.mealplan import SuggestedMealIn, meal_from_suggestion
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        data = SuggestedMealIn(request_id='suggestion-retry', day='2026-10-12',slot='breakfast',
            name='Toast',servings=2,instructions='Toast the bread.',ingredients=[
                {'name':'BREAD','amount':4,'unit':'slice'},
                {'name':'Butter','amount':1,'unit':'piece'}])
        result = meal_from_suggestion(data,self.db,self.user)
        self.assertEqual(meal_from_suggestion(data,self.db,self.user),result)
        self.assertEqual((self.count('products'),self.count('meal_plan'),self.count('recipes')),(1,1,1))
        m = result['meal']
        self.assertEqual((m['day'],m['slot'],m['note'],m['servings'],m['simple_meal']),
                         ('2026-10-12','breakfast','Toast',2,1))
        self.assertEqual(self.db.execute('SELECT product_id,amount,unit_id FROM recipe_ingredients').fetchone()[:],(1,4,1))
        self.assertEqual(self.db.execute('SELECT instructions FROM recipes').fetchone()[0],'Toast the bread.')
        self.assertEqual(result['not_added'][0]['name'],'Butter')
        self.assertEqual(result['not_added'][0]['reason'],'not added - no matching product')
        self.assertEqual(needs('2026-10-12','2026-10-18',self.db,self.user)['items'][0]['need'],4)
        self.assertEqual((self.count('shopping_list'),self.count('stock')),(0,0))
        from app.routes.suggestions import AddMissingIn, add_missing_from_suggestion
        self.assertEqual(add_missing_from_suggestion(AddMissingIn(name='Toast',ingredients=result['not_added']),self.db,self.user)['added'],1)
        self.assertEqual(self.count('products'),1)
        self.assertIsNone(self.db.execute('SELECT product_id FROM shopping_list').fetchone()[0])

    def test_from_suggestion_unknown_and_invalid_units_are_reported(self):
        from app.routes.mealplan import SuggestedMealIn, meal_from_suggestion
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        result = meal_from_suggestion(SuggestedMealIn(day='2026-10-12',slot='lunch',name='Toast',servings=1,
            ingredients=[{'name':'Bread','amount':1,'unit':'handful'},
                         {'name':'Bread','amount':1,'unit':'piece'},
                         {'name':'Butter','amount':1,'unit':'piece'}]),self.db,self.user)
        self.assertEqual(len(result['not_added']),3)
        self.assertIn('valid unit',result['not_added'][0]['reason'])
        self.assertIsNone(result['meal']['recipe_id'])
        self.assertEqual((self.count('products'),self.count('recipe_ingredients')),(1,0))

    def test_from_suggestion_failure_rolls_back(self):
        from app.routes.mealplan import SuggestedMealIn, meal_from_suggestion
        self.db.execute("INSERT INTO products(id,name,unit_id) VALUES(1,'Bread',1)")
        self.db.commit()
        self.db.execute("CREATE TRIGGER fail_suggestion BEFORE INSERT ON meal_plan BEGIN SELECT RAISE(ABORT,'failed'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            meal_from_suggestion(SuggestedMealIn(day='2026-10-12',slot='lunch',name='Toast',servings=1,
                ingredients=[{'name':'Bread','amount':1,'unit':'slice'}]),self.db,self.user)
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
