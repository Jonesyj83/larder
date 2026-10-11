from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
import unittest
from jinja2 import Environment, FileSystemLoader

ROOT = Path(__file__).resolve().parents[1]
VOID = {'input','img','link','meta','br','hr','source','wbr'}

class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack=[]
        self.controls=[]
        self.labels=set()
        self.loops=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if self.stack:
            self.stack[-1]['children']+=1
        if tag=='label' and attrs.get('for'): self.labels.add(attrs['for'])
        if tag in ('input','select','textarea'):
            self.controls.append((attrs, any(e['tag']=='label' for e in self.stack)))
        if tag not in VOID:
            self.stack.append({'tag':tag,'attrs':attrs,'children':0})
    def handle_endtag(self, tag):
        if self.stack and self.stack[-1]['tag']==tag:
            item=self.stack.pop()
            if tag=='template' and 'x-for' in item['attrs']: self.loops.append(item)
        elif tag not in VOID:
            raise AssertionError('Unexpected closing '+tag+' in '+str([e['tag'] for e in self.stack]))

class FormStructure(unittest.TestCase):
    def test_all_page_controls_and_loop_roots(self):
        env=Environment(loader=FileSystemLoader(ROOT/'app/templates'))
        env.globals['base_path']=''
        user={'id':1,'username':'test','portion_size':'regular','is_admin':True}
        for path in (ROOT/'app/templates').glob('*.html'):
            if path.name.startswith('_'): continue
            with self.subTest(page=path.name):
                html=env.get_template(path.name).render(user=user, title='Test',request=SimpleNamespace(url=SimpleNamespace(path='/')))
                parser=Forms(); parser.feed(html)
                self.assertFalse(parser.stack)
                for item in parser.loops:
                    self.assertEqual(item['children'],1,item['attrs'])
                for attrs,nested in parser.controls:
                    self.assertTrue(nested or attrs.get('aria-label') or attrs.get(':aria-label') or attrs.get('id') in parser.labels, attrs)

    def test_initialization_runs_once(self):
        for path in (ROOT/'app/templates').glob('*.html'):
            self.assertNotIn('x-init="init()"',path.read_text(),path.name)

    def test_ingredient_cancel_retry_and_meal_edit_javascript(self):
        import subprocess
        helper='function selectIngredient'+(ROOT/'app/static/app.js').read_text().split('function selectIngredient')[1]
        recipe=(ROOT/'app/templates/recipes.html').read_text().split('<script>')[1].split('</script>')[0]
        plan=(ROOT/'app/templates/mealplan.html').read_text().split('<script>')[1].split('</script>')[0].replace('{{ user.id }}','1')
        script="""
const assert=require('node:assert/strict'); const window={};
const today=()=> '2026-10-12', addDays=x=>x, calls=[], errors=[];
const oops=e=>errors.push(e.message), toast=()=>{}; let fail=true;
async function api(path,opts={}) {
 calls.push({path,...opts});
 if (path==='/api/recipes/8') return {ingredients:[{product_id:1,amount:2,unit_id:1}]};
 if (opts.method==='POST') {if(fail) throw Error('Request failed'); return {id:8};}
 return [];
}
"""+helper+recipe+plan+"""
(async()=>{
 const r=recipesPage(); r.open=async()=>{}; r.load=()=>{};
 r.edit(null); r.form.name='Toast'; r.form.ingredients[0].name='Bread';
 r.form.ingredients[0].unit_id=1; r.form=null;
 assert.equal(calls.length,0);
 r.edit(null); r.form.name='Toast'; r.form.ingredients[0].name='Bread';
 r.form.ingredients[0].unit_id=1; await r.save();
 assert.ok(r.form); assert.equal(r.busy,false);
 const token=calls[0].body.request_id; assert.ok(token);
 assert.deepEqual(calls[0].body.ingredients,[{product_id:null,name:'Bread',amount:1,unit_id:1}]);
 fail=false; await r.save(); assert.equal(calls[1].body.request_id,token); assert.equal(r.form,null);
 const p=planPage(); p.load=()=>{};
 await p.openEdit({id:7,recipe_id:8,simple_meal:1,day:'2026-10-12',slot:'breakfast',note:'Toast',servings:1});
 assert.equal(p.adding.recipe_id,''); assert.equal(p.adding.ingredients[0].amount,2);
 await p.add(); const call=calls.find(c=>c.path==='/api/mealplan/7');
 assert.equal(call.method,'PUT'); assert.equal(call.body.ingredients[0].amount,2);
 assert.equal(call.body.note,'Toast');
})().catch(e=>{console.error(e);process.exit(1)});
"""
        result=subprocess.run(['node'],input=script,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
