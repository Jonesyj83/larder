"""Read-only linked-pair calibration; saved candidates allow identical replay."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', default=str(Path(__file__).resolve().parents[1]))
parser.add_argument('--db', required=True, help='Disposable catalogue snapshot, opened read-only')
parser.add_argument('--freeze')
parser.add_argument('--sample')
parser.add_argument('--refresh-preferences', action='store_true')
parser.add_argument('--refresh-retrieval', action='store_true')
parser.add_argument('--output', required=True)
parser.add_argument('--probes', action='store_true')
args = parser.parse_args()
sys.path.insert(0, args.repo)
from app import pricing

started = time.perf_counter()

def fingerprint():
    files = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
             (Path(args.db), Path(args.db + '-wal')) if p.exists()}
    db = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    schema = hashlib.sha256(repr(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()).encode()).hexdigest()
    links = hashlib.sha256(repr(db.execute('SELECT * FROM product_store_links ORDER BY id').fetchall()).encode()).hexdigest()
    db.close()
    return dict(files=files, schema=schema, links=links)

# Opening a WAL database read-only can create an empty WAL sidecar on a
# disposable snapshot. Open first so fingerprints compare stable file sets.
warm = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
warm.execute('PRAGMA query_only=ON')
warm.execute('SELECT count(*) FROM sqlite_master').fetchone()
before = fingerprint()
if args.sample:
    sample = json.loads(Path(args.sample).read_text())
    if isinstance(sample, dict):
        sample = sample['results']
else:
    db = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    sample = []
    products = db.execute('''SELECT id,name FROM products WHERE id IN
        (SELECT product_id FROM product_store_links GROUP BY product_id HAVING count(DISTINCT store)>1) ORDER BY id''').fetchall()
    if args.probes:
        products = []
    for p in products:
        for link in db.execute('SELECT * FROM product_store_links WHERE product_id=? ORDER BY store', (p['id'],)).fetchall():
            candidates = pricing.search(link['store'], p['name'], 'Piara Waters', conn=db)
            sample.append(dict(product_id=p['id'], query=p['name'], store=link['store'], link=dict(link),
                               preferred_size=pricing.pack_size(link['size'] or ''),
                               preferred_type=sorted(pricing.type_preference(db,p['id'],link['store'])), candidates=candidates))
    if args.probes:
        sample = []
        for query in ('1l milk','1l hilo milk','2l coke','smiths cheese and onion','grated cheese','redbull'):
            for store in pricing._CATALOGUE_TABLES:
                sample.append(dict(product_id=query,query=query,store=store,link=dict(store_item_id='',store_name='',size=''),
                    preferred_size={},preferred_type=[],candidates=pricing.search(store,query,'Piara Waters',conn=db)))
    db.close()
    if args.freeze:
        Path(args.freeze).write_text(json.dumps(sample, indent=2)+'\n')

if args.sample and (args.refresh_preferences or args.refresh_retrieval):
    db = sqlite3.connect(f'file:{args.db}?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    for row in sample:
        if args.refresh_preferences:
            row['preferred_type'] = sorted(pricing.type_preference(db,row['product_id'],row['store']))
        if args.refresh_retrieval:
            row['candidates'] = pricing.search(row['store'],row['query'],'Piara Waters',conn=db)
    db.close()

def query_class(row):
    words=pricing.name_words(row['query'])
    return ('explicit_size' if pricing.pack_size(row['query']) else 'bare' if len(words)==1 else 'qualified')

results=[]
counts=collections.Counter()
groups=collections.defaultdict(collections.Counter)
for row in sample:
    best=pricing.strongest_match(row['query'], row['candidates'],row['preferred_size'],row['preferred_type'],store=row['store'])
    outcome='abstain' if best is None else 'selected' if not row['link']['store_item_id'] else 'exact' if str(best['store_item_id'])==str(row['link']['store_item_id']) else 'different'
    counts[outcome]+=1
    for group in (row['store'],query_class(row)):
        groups[group][outcome]+=1
    results.append(dict(row, outcome=outcome, selected=best, linked_retrieved=any(str(c['store_item_id'])==str(row['link']['store_item_id']) for c in row['candidates'])))
after=fingerprint()
report=dict(elapsed_seconds=round(time.perf_counter()-started,3),products=len({r['product_id'] for r in sample}),pairs=len(sample),counts=dict(counts),groups={k:dict(v) for k,v in groups.items()},before=before,after=after,database_unchanged=before==after,results=results)
Path(args.output).write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps({k:v for k,v in report.items() if k not in ('results','before','after')},indent=2))
warm.close()
if before!=after:
    raise SystemExit('Database fingerprint changed during benchmark')
