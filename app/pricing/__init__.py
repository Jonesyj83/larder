"""Product matching and append-only store price observations."""
import json
import logging
import math
import re
import time
import unicodedata
from statistics import median

from . import stores
from ..db import rows

log = logging.getLogger(__name__)


# Only retry an absent brand with a narrow category, since an unrelated
# substitute is worse than leaving the item unmatched.
GENERIC_EQUIVALENTS = {
    "pepsi": ("cola",),
    "pepsi max": ("no", "sugar", "cola"),
    "coke": ("cola",),
    "coca-cola": ("cola",),
    "coke zero": ("no", "sugar", "cola"),
    "coke zero sugar": ("no", "sugar", "cola"),
    "coca-cola zero": ("no", "sugar", "cola"),
    "coca-cola zero sugar": ("no", "sugar", "cola"),
    "sprite": ("lemonade",),
    "7up": ("lemonade",),
    "fanta": ("orange", "soft", "drink"),
    "monster": ("energy", "drink"),
    "mother": ("energy", "drink"),
    "red bull": ("energy", "drink"),
    "vegemite": ("yeast", "spread"),
    "nutella": ("choc", "hazelnut", "spread"),
    "smiths": ("chips",),
    "doritos": ("corn", "chips"),
    "kettle": ("deli", "style", "potato", "chips"),
    "red rock deli": ("deli", "style", "potato", "chips"),
    "pringles": ("snack", "stack", "chips"),
    "weet-bix": ("wheat", "biscuits"),
    "coco pops cereal": ("choco", "rice", "cereal"),
    "kelloggs corn flakes": ("corn", "flakes"),
    "kellogg's corn flakes": ("corn", "flakes"),
    "skippy": ("peanut", "butter"),
    "kraft peanut butter": ("peanut", "butter"),
    "heinz baked beans": ("baked", "beans"),
    "maggi chicken noodles": ("chicken", "noodles"),
    "maggi beef noodles": ("beef", "noodles"),
    "indomie mi goreng": ("mi", "goreng", "noodles"),
}

# Category names and private labels confirmed in the stored Aldi range.
# Requiring both keeps soft drinks away from chocolate, and snack chips
# away from frozen chips. A label leaving the range means no substitute.
ALDI_CATEGORY_BRANDS = {
    ("cola",): "REGAL",
    ("no", "sugar", "cola"): "REGAL",
    ("lemonade",): "FIZZI",
    ("orange", "soft", "drink"): "SUNLIT",
    ("energy", "drink"): "FLYING POWER",
    ("choc", "hazelnut", "spread"): "BRAMWELLS",
    ("chips",): "SPRINTERS",
    ("corn", "chips"): "SPRINTERS",
    ("deli", "style", "potato", "chips"): "BLACKSTONE GOURMET SNACK CO.",
    ("snack", "stack", "chips"): "SPRINTERS",
    ("wheat", "biscuits"): "HILLCREST",
    ("choco", "rice", "cereal"): "HILLCREST",
    ("corn", "flakes"): "HILLCREST",
    ("peanut", "butter"): "BRAMWELLS",
    ("baked", "beans"): "CORALE",
    ("chicken", "noodles"): "SIMPLEE",
    ("beef", "noodles"): "SIMPLEE",
    ("mi", "goreng", "noodles"): "SIMPLEE",
}

# House labels confirmed in the stored ranges. Organic and dietary lines
# are not plain defaults; shared names stay category-specific.
HOME_BRANDS = {
    "coles": ("Coles", "Coles Simply", "Coles Kitchen", "Coles Finest",
              "Coles Bakery", "Coles Ultra", "Coles Asia", "Coles Made Easy", "Coles Deli"),
    "woolworths": ("Woolworths", "Woolworths Essentials"),
    "aldi": ("FARMDALE", "COLWAY", "REMANO", "CORALE", "SPRINTERS",
             "BRAMWELLS", "FIZZI", "SUNLIT", "FLYING POWER", "HILLCREST",
             "SIMPLEE", "BLACKSTONE GOURMET SNACK CO.", "NORTHBROOK", "BERG", "BAKERS LIFE",
             "YOGURI", "WESTACRE", "WESTACRE DAIRY", "CHOCEUR", "CUCINA",
             "DELI ORIGINALS", "OCEAN ROYALE", "PURE VALLEY", "BEAUTIFULLY BUTTERFULLY",
             "EMPORIUM SELECTION"),
    "iga": ("Community Co", "Black & Gold", "G-FRESH"),
}


def _aldi_equivalent(query):
    # Match the longest named product after removing sizes. Keep all
    # remaining flavour/dietary words and the original size for scoring.
    sizes = [m.group() for m in re.finditer(SIZE_TOKEN, query, re.IGNORECASE)]
    tokens = re.findall(r"\w+", re.sub(SIZE_TOKEN, " ", query, flags=re.IGNORECASE).casefold().replace("’", "").replace("'", ""))
    for alias in sorted(GENERIC_EQUIVALENTS, key=lambda a: len(re.findall(r"\w+", a)), reverse=True):
        prefix = re.findall(r"\w+", alias)
        if tokens[:len(prefix)] == prefix:
            category = GENERIC_EQUIVALENTS[alias]
            brand = ALDI_CATEGORY_BRANDS.get(category, "")
            fallback = " ".join((*category, brand, *(t for t in tokens[len(prefix):] if t != "and"), *sizes)).strip()
            return alias, fallback, brand
    return None


# Only aliases backed by a stored catalogue brand belong here.
BRAND_ALIASES = {"coke": "coca-cola"}
# Only household-confirmed defaults belong here.
DEFAULT_VARIANT = {"milk": "full cream"}

QUERY_CORRECTIONS = {"redbull": "red bull"}

# These qualifiers change the product rather than its brand or pack size.
# A category alone must not acquire one through title or price preferences.
CATEGORY_QUALIFIERS = {
    frozenset({'cola', 'pepsi'}): frozenset({'diet', 'zero', 'max', 'no', 'free'}),
    frozenset({'bread'}): frozenset({
        'banana', 'garlic', 'mix', 'crumb', 'naan', 'roti', 'souvlaki',
        'lebanese', 'roll', 'choc', 'chocolate', 'brownie',
    }),
    frozenset({'ham'}): frozenset({
        'soup', 'pizza', 'sandwich', 'toastie', 'quiche', 'bean', 'spread',
    }),
    frozenset({'noodle'}): frozenset({'cup', 'pot', 'bowl'}),
    frozenset({'water'}): frozenset({
        'tonic', 'soda', 'coconut', 'flavoured', 'infused', 'lemon', 'lime', 'berry',
        'apple', 'orange', 'mango', 'pineapple', 'passionfruit', 'peach',
        'lychee', 'yuzu', 'raspberry', 'strawberry', 'watermelon',
    }),
}

# Plain water titles use source and carbonation descriptions. Requiring
# only these words after brand removal also excludes non-drink water goods.
# Soda water sits in CATEGORY_QUALIFIERS instead -- it's a mixer, not a
# source/carbonation description of plain water, same reasoning as tonic.
PLAIN_CATEGORY_WORDS = {
    'water': frozenset({'water', 'spring', 'natural', 'still', 'sparkling',
                        'lightly', 'mineral', 'pure', 'artesian',
                        'australian', 'italian', 'bottle', 'can', 'single', 'pack'}),
}


_CATALOGUE_TABLES = {
    "coles": ("coles_catalogue", "coles_id"),
    "woolworths": ("woolworths_catalogue", "stockcode"),
    "aldi": ("aldi_catalogue", "item_id"),
    "iga": ("iga_catalogue", "item_id"),
}


def _fetch(store, query, postcode, conn=None):
    if store in _CATALOGUE_TABLES:
        return _catalogue_lookup(store, query, conn=conn, postcode=postcode)
    function = getattr(stores, "search_" + store)
    return function(query, postcode=postcode) if store == "iga" else function(query)


def _catalogue_lookup(store, query, limit=None, conn=None, postcode=""):
    """Match against each store's locally crawled range.

    conn lets a caller that already has the right connection (the current
    request, a test's own db) use it directly; otherwise this opens and
    closes its own, since auto_match_live can call this from a worker
    thread that has no connection of its own to hand in."""
    table, id_field = _CATALOGUE_TABLES[store]
    query, _ = _query_pack(query)
    words = sorted(name_words(query, singular=False))
    scoring_words = name_words(query)
    if not words:
        return []
    if limit is None:
        limit = 200
    classification_sql = "c.classification"
    owns_conn = conn is None
    if owns_conn:
        from .. import db as database
        conn = database.connect()
    try:
        if store == "iga":
            source = conn.execute("SELECT location FROM iga_catalogue_store WHERE id=1").fetchone()
            if not source or source['location'] != postcode.strip().casefold():
                return []
        basis_sql = "c.price_basis" if 'price_basis' in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')} else "'{}'"
        # Expand canonical synonyms back to their catalogue spellings. Porter
        # handles regular plurals; phrases preserve retailer token boundaries.
        alternatives = {"hilo": ("hilo", "hi lo"),
                        "coca": ("coca", "coke"), "cola": ("cola", "coke")}
        for alias, canonical in SYNONYMS.items():
            alternatives[canonical] = (*alternatives.get(canonical, (canonical,)), alias)
        for word in words:
            if '&' in word:
                alternatives[word] = (word, word + 's', word + "'s")
        def quoted(word):
            return '"' + word.replace('"', '""') + '"'
        terms = ["(" + " OR ".join(quoted(term) for term in alternatives.get(word, (word,))) + ")"
                 for word in words]
        index = f"{table}_fts"
        # Keep the established coverage and title-length window. Partial
        # matches can carry brand guards used by selection, so changing that
        # window can turn an abstention into a different product choice.
        conn.create_function("matching_length", 1,
                             lambda text: len(" " + " ".join(sorted(name_words(text or ""))) + " "))
        conn.create_function("matching_coverage", 2,
                             lambda name, brand: len(scoring_words &
                                 (name_words(name or "") | name_words(brand or ""))))
        found = conn.execute(
            f"WITH candidates AS MATERIALIZED (SELECT c.{id_field} AS item_id, c.name, c.brand, c.size, c.price, "
            f"c.was_price, c.unit_price, c.unit_measure, {basis_sql} AS price_basis, {classification_sql} AS classification, matching_coverage(c.name,c.brand) AS coverage, "
            f"bm25({index}) AS retrieval_rank FROM {index} "
            f"JOIN {table} c ON c.rowid={index}.rowid "
            f"WHERE {index} MATCH ? ORDER BY coverage DESC, matching_length(c.name), "
            f"c.rowid LIMIT ?) SELECT * FROM candidates ORDER BY retrieval_rank, item_id", (" OR ".join(terms), limit)).fetchall()
        if not found and not (store == "aldi" and _aldi_equivalent(query)):
            # Porter is not a spelling corrector. Trigrams can surface a
            # misspelled title, but never change the words selection checks.
            fragments = sorted({word[i:i + 3] for word in words if len(word) >= 4
                                for i in range(len(word) - 2)})
            if fragments:
                index = f"{table}_trigrams"
                found = conn.execute(
                    f"SELECT c.{id_field} AS item_id, c.name, c.brand, c.size, c.price, "
                    f"c.was_price, c.unit_price, c.unit_measure, {basis_sql} AS price_basis, {classification_sql} AS classification FROM {index} "
                    f"JOIN {table} c ON c.rowid={index}.rowid WHERE {index} MATCH ? "
                    f"ORDER BY bm25({index}), c.rowid LIMIT ?",
                    (" OR ".join(quoted(fragment) for fragment in fragments), limit)).fetchall()
    finally:
        if owns_conn:
            conn.close()
    return [{"store_item_id": str(r["item_id"]), "name": r["name"], "brand": r["brand"], "size": r["size"],
             "price": r["price"], "was_price": r["was_price"], "unit_price": r["unit_price"],
             "unit_measure": r["unit_measure"], "price_basis": json.loads(r["price_basis"] or "{}"), "on_special": bool(r["was_price"]),
             "classification": _classification_data(r["classification"])} for r in found]


def search(store, query, postcode="", conn=None):
    query = QUERY_CORRECTIONS.get(query.casefold().strip(), query)
    equivalent = _aldi_equivalent(query) if store == "aldi" else None
    results = _fetch(store, query, postcode, conn=conn)
    if not equivalent:
        return results
    alias, fallback_query, brand = equivalent
    # OR-based retrieval can return onions for Red Bull or chocolate for
    # cola. Check the named product itself before deciding it is stocked.
    # A real direct line keeps precedence even if its size doesn't fit.
    identity = name_words(query)
    direct = results
    if not any(identity <= (name_words(c['name']) | name_words(c.get('brand') or '')) for c in direct):
        direct = _catalogue_lookup(store, alias, limit=200, conn=conn)
    if any(identity <= (name_words(c['name']) | name_words(c.get('brand') or '')) for c in direct):
        return results
    try:
        found = _fetch(store, fallback_query, postcode, conn=conn)
        wanted = name_words(fallback_query)
        return [dict(c, _match_query=fallback_query) for c in found
                if (not brand or normalized_name(c.get('brand') or '') == normalized_name(brand))
                and wanted <= (name_words(c['name']) | name_words(c.get('brand') or ''))] or results
    except Exception:
        return results


def catalogue_brands(db):
    return {normalized_name(row[0]) for table, _ in _CATALOGUE_TABLES.values()
            for row in db.execute(f"SELECT DISTINCT brand FROM {table} WHERE brand!=''")}


def substitute_pick(query, store, candidate, known_brands=()):
    # Search only rewrites a request when the named line is absent. An
    # actual stocked national brand, or a generic category, stays normal.
    if candidate.get('_match_query') or candidate.get('_substitute'):
        return True
    equivalent = _aldi_equivalent(query) if store == 'aldi' else None
    identity = name_words(candidate['name']) | name_words(candidate.get('brand') or '')
    if equivalent and not name_words(query) <= identity:
        return True
    if name_words(query) <= identity:
        return False
    # Reuse stored brand labels to recognize requests outside the verified
    # substitution aliases. Detection does not invent a replacement mapping.
    query = QUERY_CORRECTIONS.get(query.casefold().strip(), query)
    tokens = re.findall(r"\w+", re.sub(SIZE_TOKEN, ' ', query, flags=re.IGNORECASE).casefold())
    brands = [normalized_name(' '.join(tokens[:n])) for n in range(1, len(tokens) + 1)]
    requested = next((brand for brand in reversed(brands) if brand in known_brands), None)
    house = {normalized_name(b) for labels in HOME_BRANDS.values() for b in labels}
    if requested and requested not in house:
        return requested != normalized_name(candidate.get('brand') or '') and not any(
            requested == normalized_name(' '.join(re.findall(r"\w+", candidate['name'])[:n]))
            for n in range(1, len(re.findall(r"\w+", candidate['name'])) + 1))
    return False


def household_match(db, product_id, name, store, candidates):
    from .. import order_history
    history = [h for h in order_history.recent(db, product_id, store) if h['store_item_id']]
    remembered = order_history.typical(history, fields=('store_item_id',))
    missing_identity = False
    if remembered:
        table, key = _CATALOGUE_TABLES[store]
        item = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (remembered['store_item_id'],)).fetchone()
        if item:
            direct = dict(item, store_item_id=str(item[key]))
            remembered_type = name_words(item['name']) - name_words(name) - name_words(item['brand'])
            picked = strongest_match(name, [direct], preferred_type=remembered_type, store=store)
            if picked:
                return dict(picked, _substitute=True) if remembered["is_substitute"] else picked
        else:
            missing_identity = True
    preferred_type = type_preference(db, product_id, store)
    if remembered:
        preferred_type = (name_words(remembered['store_name']) - name_words(name)
                          - name_words(remembered['store_brand']))
    words = name_words(name)
    if len(words) == 1 and next(iter(words)) in PLAIN_CATEGORY_WORDS:
        preferred_type -= PLAIN_CATEGORY_WORDS[next(iter(words))] - {'sparkling', 'mineral', 'lightly'}
    preferred_class = classification_preference(db, product_id, store)

    def remembered_result(picked):
        other = name_words(picked['name']) | name_words(picked.get('brand') or '')
        missing_type = len(words) == 1 and preferred_type and not preferred_type <= other
        return dict(picked, _substitute=True) if missing_identity or missing_type else picked

    # National-brand agreement across confirmed choices can supply a brand
    # even when the household calls the item by its generic category.
    brands = {normalized_name(remembered['store_brand'])} if remembered and remembered['store_brand'] else set()
    links = [] if brands else db.execute("SELECT * FROM product_store_links WHERE product_id=? AND needs_review=0 AND is_substitute=0",
                                        (product_id,))
    for link in links:
        table, key = _CATALOGUE_TABLES[link['store']]
        item = db.execute(f"SELECT brand FROM {table} WHERE {key}=?", (link['store_item_id'],)).fetchone()
        if item and item['brand']:
            brand = normalized_name(item['brand'])
            if brand not in {normalized_name(b) for b in HOME_BRANDS.get(link['store'], ())}:
                brands.add(brand)
    if len(brands) == 1:
        preferred = [c for c in candidates if normalized_name(c.get('brand') or '') in brands]
        picked = strongest_match(name, preferred, size_preference(db, product_id, store), preferred_type,
                                 store=store, preferred_classification=preferred_class)
        if picked:
            return remembered_result(picked)
        missing_identity = True
    picked = strongest_match(name, candidates, size_preference(db, product_id, store), preferred_type,
                             store=store, preferred_classification=preferred_class)
    if picked is None and (missing_identity or preferred_type or preferred_class):
        picked = strongest_match(name, candidates, store=store)
        missing_identity = True
    return remembered_result(picked) if picked else None


def link_product(db, product_id, store, item_id, name, matched_by=None, size="", *, needs_review=False, is_substitute=None):
    existing = db.execute("SELECT id,store_item_id,is_substitute FROM product_store_links WHERE product_id=? AND store=?",
                          (product_id, store)).fetchone()
    if is_substitute is None:
        product = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
        table, key = _CATALOGUE_TABLES[store]
        item = db.execute(f"SELECT brand FROM {table} WHERE {key}=?", (item_id,)).fetchone()
        is_substitute = substitute_pick(product['name'], store, {'name': name, 'brand': item['brand'] if item else ''}, catalogue_brands(db))
        if existing and existing['store_item_id'] == item_id:
            is_substitute |= bool(existing['is_substitute'])
    if existing and existing["store_item_id"] != item_id:
        # A replacement identity must not inherit the previous item's history.
        db.execute("DELETE FROM product_store_links WHERE id=?", (existing["id"],))
    db.execute("""INSERT INTO product_store_links(product_id,store,store_item_id,store_name,matched_by,size,needs_review,is_substitute)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(product_id,store) DO UPDATE SET
        store_name=excluded.store_name, matched_by=excluded.matched_by,
        needs_review=excluded.needs_review, is_substitute=excluded.is_substitute,
        size=CASE WHEN excluded.size != '' THEN excluded.size ELSE product_store_links.size END""",
        (product_id, store, item_id, name, matched_by, size or "", int(needs_review), int(is_substitute)))
    return db.execute("SELECT id FROM product_store_links WHERE product_id=? AND store=?",
                      (product_id, store)).fetchone()[0]


def record_price(db, link_id, candidate=None):
    candidate = candidate or {}
    values = []
    for field in ("price", "was_price", "unit_price"):
        value = candidate.get(field)
        if value is not None:
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError("Invalid store price")
        values.append(value)
    basis = candidate.get('price_basis') or {}
    if isinstance(basis, str):
        basis = json.loads(basis)
    columns = 'link_id,price,was_price,unit_price,unit_measure,on_special'
    values = [link_id, *values, candidate.get('unit_measure') or '', int(bool(candidate.get('on_special')))]
    # Read-only snapshot reproductions can still use the pre-upgrade schema.
    if 'price_basis' in {r[1] for r in db.execute('PRAGMA table_info(store_price_checks)')}:
        columns += ',price_basis'
        values.append(json.dumps(basis))
    db.execute(f"INSERT INTO store_price_checks({columns}) VALUES({','.join('?' for _ in values)})", values)



def normalized_name(name):
    return "".join(re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold()))


# A size token anywhere in a name -- "1.25L Pepsi Max", "Milk 2L", "Water
# 12x600mL" -- never appears in what a store calls the product itself, so
# it's pure noise for word-overlap scoring and has to come out first.
SIZE_UNIT = r"(?:ml|kg|grams?|g|litres?|l)"
SIZE_COUNT = r"(?:pack|pk|pac|bottles?|pieces?|each|ea|pcs?)"
SIZE_TOKEN = (rf"\d+(?:\.\d+)?\s*{SIZE_UNIT}\s*x\s*\d+\s*(?:pack|pk|case)?\b"
              rf"|\d+(?:\.\d+)?\s*x\s*\d+(?:\.\d+)?\s*{SIZE_UNIT}\s*(?:pack|pk|case)?\b"
              rf"|\d+(?:\.\d+)?\s*{SIZE_UNIT}\b"
              rf"|\d+(?:\.\d+)?\s*[- ]?\s*{SIZE_COUNT}\b")


# Australian supermarkets swap these words for the same thing -- both
# sides have to collapse onto one form or they anti-match each other.
# Reduced-fat milk in particular has three real spellings in the wild:
# Coles/Woolworths call it Lite, IGA's Brownes/Country Dairy call it
# Hi-Lo, Aldi's Farmdale calls it Light -- same product, same intent.
SYNONYMS = {
    "multipack": "pack", "shredded": "grated", "sach": "sachet", "hilo": "lite", "light": "lite",
    "yogurt": "yoghurt", "chocolate": "choc", "bbq": "barbecue", "barbeque": "barbecue",
    "feta": "fetta", "fettucine": "fettuccine", "lasagna": "lasagne",
    "soya": "soy", "mayonnaise": "mayo", "smokey": "smoky", "chili": "chilli",
    "aluminum": "aluminium", "specialty": "speciality", "squeezy": "squeezie",
    "licorice": "liquorice", "masaman": "massaman", "panaeng": "panang",
    "café": "cafe", "qualità": "qualita", "choco": "choc",
    "doughnut": "donut", "doughnuts": "donuts", "bottl": "bottle",
    "crispibread": "crispbread", "mueli": "muesli", "four": "4", "five": "5",
    "brasied": "braised", "cheescake": "cheesecake", "cranbery": "cranberry",
    "glentle": "gentle", "invorating": "invigorating", "lisio": "liscio",
    "matifying": "mattifying", "maxium": "maximum", "monoploy": "monopoly",
    "proetin": "protein", "sald": "salad", "sauekraut": "sauerkraut",
    "sheperd": "shepherd", "sherbert": "sherbet", "shitake": "shiitake",
    "shortbrtead": "shortbread", "strengh": "strength", "traditonal": "traditional",
    "tumeric": "turmeric", "vanlla": "vanilla", "jalepeno": "jalapeno",
    "chedder": "cheddar", "defense": "defence",
    # Keep plural retrieval and singular scoring on the same product word.
    "anchovies": "anchovy", "anchovie": "anchovy", "blackberries": "blackberry",
    "blackberrie": "blackberry", "chillies": "chilli", "chillie": "chilli",
    "mangoes": "mango", "mangoe": "mango", "pastries": "pastry",
    "pastrie": "pastry", "patties": "patty", "pattie": "patty",
    "lollies": "lolly", "lollie": "lolly", "jellies": "jelly",
    "jellie": "jelly", "gummies": "gummy", "gummie": "gummy",
    "batteries": "battery", "batterie": "battery", "nappies": "nappy",
    "nappie": "nappy", "patches": "patch", "patche": "patch",
    "brushes": "brush", "brushe": "brush", "toothbrushes": "toothbrush",
    "toothbrushe": "toothbrush", "pouches": "pouch", "pouche": "pouch",
    "sandwiches": "sandwich", "sandwiche": "sandwich", "matches": "match",
    "matche": "match", "mixes": "mix", "mixe": "mix",
    "berries": "berry",
    "berrie": "berry", "blueberries": "blueberry", "blueberrie": "blueberry",
    "cranberries": "cranberry", "cranberrie": "cranberry", "cherries": "cherry",
    "cherrie": "cherry", "strawberries": "strawberry", "strawberrie": "strawberry",
    "raspberries": "raspberry", "raspberrie": "raspberry", "peaches": "peach",
    "peache": "peach",
    # Singular scoring drops the final s; retrieval keeps the stored spelling.
    "hummus": "hommus", "hummu": "hommu",
}


def name_words(name, *, singular=True):
    # Keep size tokens consistent: "2 L" and "2L" mean the same thing.
    name = unicodedata.normalize("NFKC", name).casefold().replace("×", "x")
    name = re.sub(r"\b(\w+)[’']n\b", r"\1 n", name)
    name = re.sub(r"[’']", "", name)
    # Initials joined by an ampersand form one identity, even when stores
    # add spaces or a possessive/plural suffix. Ordinary word pairs keep
    # their existing words (Ben & Jerry's, salt & vinegar).
    name = re.sub(r"\b([a-z](?:\s*&\s*[a-z])+)s?\b",
                  lambda m: re.sub(r"\s+", "", m[1]), name)
    name = re.sub(r"\bhi[ -]+lo\b", "hilo", name)
    name = re.sub(r"\bww\b", "woolworths", name)
    for alias, formal in BRAND_ALIASES.items():
        name = re.sub(r"\b" + re.escape(alias) + r"\b", formal, name)
    name = re.sub(r"(\d)\s+(?=(?:ml|kg|g|l)\b)", r"\1", name)
    name = re.sub(SIZE_TOKEN, " ", name, flags=re.IGNORECASE)
    # Supermarket names are scoring noise; keep stored names and brand guards intact.
    # Keep decimal measurements whole even when their unit is not a food size.
    words = set(re.findall(r"[a-z](?:&[a-z])+|\d+(?:\.\d+)+(?:[^\W\d_]+)?|\w+", name)) - {"woolworths", "coles", "aldi", "iga", "and"}
    # A plain trailing "s" shouldn't count as a different word -- "carrot"
    # and "carrots" are the same item, not a mismatch.
    if singular:
        words = {w[:-2] if w in {"potatoes", "tomatoes"} else
                 w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}
    # Collapse swapped naming onto one canonical word.
    return {SYNONYMS.get(w, w) for w in words}


def pack_size(size):
    text = size.casefold().strip().replace('×', 'x')
    # Coles spells out some measures; Aldi and Woolworths abbreviate counts.
    text = re.sub(r"(?<=\d)\s*grams?\b", 'g', text)
    text = re.sub(r"(?<=\d)\s*litres?\b", 'l', text)
    # Retailers also write the measure before the pack count.
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(ml|kg|g|l)\s*x\s*(\d+)\s*(?:pack|pk)?\b", r"\3 x \1\2 ", text)
    multi = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*(ml|kg|g|l)\b", text)
    if multi:
        count, amount, unit = multi.groups()
        factor = 1000 if unit in ('kg', 'l') else 1
        return {'count': float(count), 'volume' if unit in ('ml', 'l') else 'mass': float(count) * float(amount) * factor}
    count = re.search(rf"(\d+(?:\.\d+)?)\s*[- ]?\s*{SIZE_COUNT}\b", text)
    measure = re.search(r"(\d+(?:\.\d+)?)\s*(ml|kg|g|l)\b", text)
    result = {'count': float(count[1])} if count else {}
    if measure:
        unit = measure[2]
        result['volume' if unit in ('ml', 'l') else 'mass'] = float(measure[1]) * (1000 if unit in ('kg', 'l') else 1)
        result.setdefault('count', 1)
    return result


def _query_pack(name, candidates=()):
    # A lone edge number is household pack shorthand. Leave numbers that
    # belong to a catalogue description ("2 Minute Noodles") searchable.
    if pack_size(name):
        return name, False
    match = re.fullmatch(r"\s*(?:(\d+)\s+(.+?)|(.+?)\s+(\d+))\s*", name)
    if not match:
        return name, False
    number = match[1] or match[4]
    item = match[2] or match[3]
    if not name_words(item) or any(w.isdigit() for w in name_words(item)):
        return name, False
    adjacent = (number + ' ' + item.split()[0] if match[1]
                else item.split()[-1] + ' ' + number)
    for candidate in candidates:
        if number not in name_words(candidate['name']):
            continue
        if name_words(name) == name_words(candidate['name']):
            return name, False
        tokens = re.findall(r'\w+', candidate['name'].casefold())
        if any(name_words(' '.join(tokens[i:i + 2])) == name_words(adjacent)
               for i in range(len(tokens) - 1)):
            return name, False
    return f"{item} {number} pack", True


def size_preference(db, product_id, store):
    from .. import order_history
    entries = [e for e in order_history.recent(db, product_id, store) if pack_size(e['size'])]
    entry = order_history.typical(entries, ('size',))
    if entry:
        return pack_size(entry['size'])
    entry = order_history.typical(order_history.recent(db, product_id))
    if not entry:
        return {}
    unit = (entry['unit'] or '').casefold()
    if unit in ('g', 'kg', 'ml', 'l'):
        return {'volume' if unit in ('ml', 'l') else 'mass': entry['amount'] * (1000 if unit in ('kg', 'l') else 1)}
    if unit in ('bottle', 'bottles', 'piece', 'pieces', 'each', 'count'):
        return {'count': entry['amount']}
    return {}


def type_preference(db, product_id, store):
    product = db.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
    if not product:
        return set()
    # A provisional choice must not teach later matches its guessed type.
    links = db.execute("""SELECT store,store_item_id,store_name FROM product_store_links
        WHERE product_id=? AND needs_review=0 AND is_substitute=0""", (product_id,)).fetchall()
    choices = []
    all_choices = []
    for link in links:
        table, key = _CATALOGUE_TABLES[link['store']]
        item = db.execute(f"SELECT brand FROM {table} WHERE {key}=?", (link['store_item_id'],)).fetchone()
        brand_words = name_words(item['brand'] or '') if item else set()
        choice = (name_words(link['store_name']) - name_words(product['name']) - brand_words
                  - {'bottle', 'can', 'single'})
        all_choices.append(choice)
        if link['store'] != store:
            choices.append(choice)
    # Conflicting store choices give no reliable preference. Only carry
    # words shared by every remembered choice across to another store.
    if not choices:
        return set()
    # Agreement may contain harmless extra packaging words. Incomparable
    # descriptions (e.g. full cream versus skim) are conflicting evidence.
    common = set.intersection(*all_choices)
    if len(all_choices) > 1 and not any(choice == common for choice in all_choices):
        return set()
    return set.intersection(*choices)


def _classification_data(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def _classification_paths(value):
    value = _classification_data(value)
    for key in ('primary', 'secondary'):
        paths = value.get(key)
        if not isinstance(paths, list):
            continue
        paths = [[name_words(label) if isinstance(label, str) else set() for label in path]
                 for path in paths if isinstance(path, list)]
        paths = [path for path in paths if any(path)]
        if paths:
            return paths
    return []


def classification_preference(db, product_id, store):
    """Only compare real labels, never assume retailer IDs share a taxonomy.

    Agreement across remembered stores can identify a finer class. Broad
    ancestors are retained in paths but cannot win over a more specific
    shared label. Conflicting choices supply no structural preference.
    """
    choices = []
    for link in db.execute("SELECT store,store_item_id FROM product_store_links WHERE product_id=? AND needs_review=0 AND is_substitute=0",
                           (product_id,)):
        if link['store'] == store:
            continue
        table, key = _CATALOGUE_TABLES[link['store']]
        item = db.execute(f"SELECT classification FROM {table} WHERE {key}=?", (link['store_item_id'],)).fetchone()
        if not item:
            continue
        paths = _classification_paths(item['classification'])
        labels = {frozenset(label) for path in paths for label in path if label}
        if labels:
            choices.append(labels)
    return set.intersection(*choices) if choices else set()


# Department labels are retailer vocabulary, not product-name exceptions.
# Require an explicit context before crossing from groceries into these aisles.
DEPARTMENT_CONTEXTS = (
    ('Pets|Dog and Puppy|Cat and Kitten|Birds, Fish and Small Pets|Dog Food|Cat Food|Bird Food|Fresh Pet Food DY|Pet Accessories',
     'pet dog cat kitten puppy bird animal'),
    ('Baby|Baby Food|Baby Formula|Baby Toiletries|Baby Accessories|Nappies and Wipes|Babywear|Infant Food|Infant Formula|Infant Nappies|Infant Personal',
     'baby infant toddler nappy nappies wipe formula'),
    ('Health Beauty|Health Care|Toiletries|Cosmetics|Makeup|Dental Health|Dental Care|Hair Care|Skin Care|Bath and Soap|Soaps & Body Wash|Sanitary Protection|Female Deodorants and Bodysprays|Mens Deos & Grooming|Womens Deos & Groom|Personal Care and Hygiene|Men\'s Grooming|Vitamins|Medicinal Products|First Aid and Medicinal|Beauty/Trial Travel',
     'soap shampoo conditioner hair skin dental toothpaste toothbrush cosmetic makeup deodorant sanitary period vitamin supplement medicinal medicine health beauty hygiene sunscreen wash grooming'),
    ('Cleaning Household|Cleaning Products|Cleaning Goods|Household Gloves|Laundry|Laundry Accessories|Dishwashing|Aircare|Pest Control|Brushware|Toilet Paper|Facial Tissues|Paper Towels|Bathroom',
     'cleaning cleaner detergent laundry dishwashing soap disinfectant bleach household pest insect toilet paper tissue towel freshener bathroom'),
    ('Liquor|Liquor RTD|Beer|Wine|Red Wine|White Wine|Spirits|Champagne & Sparkling|Cider|Premix Drinks',
     'liquor beer wine spirit champagne cider premix alcoholic alcohol vodka gin rum whisky whiskey'),
    ('Homewares|Kitchen|Kitchenware|Kitchen Needs/Bags|Electrical|Hardware|Stationery|Hosiery|Family Underwear|Family Socks|Seasonal Apparel|Toys & Hobbies|Garden Non Greens|Garden and Lawn Care|Auto|Car Care|Telco|Tech Accessories|Disposable Tableware|Picnic Pool BBQ Acc|Picnic and Party|Party Goods|Shoe Care|Household Appliances|Heating & Cooling|Floral|Floral & Hampers|Shopping Bags',
     'homeware kitchen electrical hardware stationery sock underwear toy hobby garden auto car telco accessory tableware picnic party shoe heating cooling flower floral shopping bag utensil bottle container'),
)
DEPARTMENT_WORDS = tuple(({frozenset(name_words(label)) for label in labels.split('|')}, name_words(context))
                         for labels, context in DEPARTMENT_CONTEXTS)


def _department_allowed(words, candidate):
    paths = _classification_paths(candidate.get('classification'))
    if not paths:
        return True
    def allowed(path):
        for departments, context in DEPARTMENT_WORDS:
            if frozenset(path[0]) in departments:
                return bool(words & context)
        return True
    return any(allowed(path) for path in paths)


def _classification_filter(words, candidates, preferred=(), plain_category=None):
    paths = {str(c['store_item_id']): _classification_paths(c.get('classification')) for c in candidates}

    def classified(c):
        return paths[str(c['store_item_id'])]

    # The existing plain-category check also applies to classification:
    # soda water is a segment of Mixers, not the retailer's Water branch.
    # An exact terminal label still allows water sold as lunchbox drinks.
    eligible = [c for c in candidates if not plain_category or not classified(c) or any(
        len(path) == 1 or any(words <= label for label in path[:-1]) or path[-1] == words
        for path in classified(c))]

    # Reuse the established format guards when a title omits a detail
    # that the retailer still declares (for example NOODLES - CUP).
    eligible = [c for c in eligible if not any(
        words & category and not words & qualifiers and
        any(label & category and label & qualifiers for path in classified(c) for label in path)
        for category, qualifiers in CATEGORY_QUALIFIERS.items())]

    # Typed words must identify a complete real label before a class can
    # narrow the pool. Flavour words absent from the taxonomy leave all
    # existing title, brand, size and history decisions alone.
    for depth in range(max((len(p) for ps in paths.values() for p in ps), default=0)):
        def labels(c):
            return [p[depth] for p in classified(c) if len(p) > depth and p[depth]]
        agree = [c for c in eligible if any(words <= label for label in labels(c))]
        disagree = [c for c in eligible if labels(c) and not any(words <= label for label in labels(c))]
        if agree:
            eligible = [c for c in eligible if c not in disagree]
            # Once the query identifies a shared branch, its children are
            # alternatives, not evidence that one is a better category.
            break

    # Cross-store labels must match exactly after normal word normalization.
    # A shared broad ancestor cannot distinguish its own sibling classes.
    preferred = set(preferred)
    for label in sorted(preferred, key=lambda label: (-len(label), sorted(label))):
        agree = [c for c in eligible if any(label == part for path in classified(c) for part in path)]
        if agree:
            return agree
    return eligible


def strongest_match(name, candidates, preferred_size=None, preferred_type=None, *, store=None, preferred_classification=None):
    if candidates and all(c.get('_match_query') == candidates[0].get('_match_query') for c in candidates):
        name = candidates[0].get('_match_query') or name
    name = QUERY_CORRECTIONS.get(name.casefold().strip(), name)
    name, bare_count = _query_pack(name, candidates)
    words = name_words(name)
    if not words:
        return None
    candidates = [c for c in candidates if _department_allowed(words, c)]
    default_type = name_words(DEFAULT_VARIANT.get(next(iter(words)), "")) if len(words) == 1 else set()
    remembered = set(preferred_type or ())
    if len(words) == 1 and next(iter(words)) in PLAIN_CATEGORY_WORDS:
        remembered -= PLAIN_CATEGORY_WORDS[next(iter(words))] - {'sparkling', 'mineral', 'lightly'}
    if len(words) > 1 and not any(words | remembered <= (name_words(c['name']) | name_words(c.get('brand') or '')) for c in candidates):
        remembered = set()
    type_words = (remembered if not words & {"cola", "pepsi"} else set()) or default_type
    # Explicit query words still take precedence over a remembered type.
    generic_type = bool(type_words) and len(words) == 1
    def type_overlap(candidate):
        other = name_words(candidate['name'])
        if remembered:
            return len(type_words & other)
        return len(default_type) if default_type <= other else 0

    # A size claim right there in what was typed ("1.25L Pepsi") is real
    # specificity, same as typing an extra descriptor word -- it's not
    # "just a brand name" with nothing else to go on, even though the
    # size token itself gets stripped out of word-overlap scoring below.
    size_in_name = bool(pack_size(name))
    preferred_size = pack_size(name) or preferred_size
    # The leading word decides the brand check -- but it has to be the
    # first REAL word, not a size token's leftover digits ("1" out of
    # "1.25L Pepsi Max").
    stripped = re.sub(SIZE_TOKEN, " ", re.sub(r"(\d)\s+(?=[a-zA-Z])", r"\1", name), flags=re.IGNORECASE)
    leading = re.findall(r"\w+", unicodedata.normalize("NFKC", stripped).casefold())
    first = leading[0] if leading else ""
    brands = {normalized_name(p.get("brand") or "") for p in candidates}
    brand = normalized_name(BRAND_ALIASES.get(first, first))
    required_brand = brand if brand in brands and first not in DEFAULT_VARIANT else None
    if not required_brand:
        required_brand = next((normalized_name(" ".join(leading[:n]))
                               for n in range(len(leading), 1, -1)
                               if normalized_name(" ".join(leading[:n])) in brands), None)
    required_brands = {required_brand} if required_brand else set()
    if required_brand and any(normalized_name(" ".join(leading[:n])) == required_brand
                              for n in range(1, len(leading) + 1)):
        # A real shorter label can share the typed phrase with a longer
        # stored brand. Keep both eligible; recall and size still decide.
        # Only whole-word prefixes of brands in this retrieval qualify.
        for candidate in candidates:
            label = re.findall(r"\w+", unicodedata.normalize(
                "NFKC", candidate.get("brand") or "").casefold())
            if any(normalized_name(" ".join(label[:n])) == required_brand
                   for n in range(1, len(label))):
                required_brands.add(normalized_name(candidate["brand"]))
    def candidate_words(c):
        # Some stores (Coles especially) strip the brand clean out of the
        # name itself ("Soft Drink Cola Bottle" with brand="Pepsi" sitting
        # separately) -- count the brand's own words too everywhere a
        # candidate's words get checked, or a brand-only query like
        # "Pepsi" could never recall a word it's genuinely claiming, just
        # not inside the name text.
        return name_words(c["name"]) | name_words(c.get("brand") or "")
    plain_category = PLAIN_CATEGORY_WORDS.get(next(iter(words))) if len(words) == 1 else None
    class_words = words - name_words(required_brand or '')
    # Explicit descriptors outrank remembered classes. Only use history
    # when the complete typed request can describe that class as well.
    class_history = [label for label in (preferred_classification or ()) if class_words <= label]
    scored = {}
    exact_sizes = set()
    for candidate in candidates:
        if required_brands and normalized_name(candidate.get("brand") or "") not in required_brands:
            continue
        other = candidate_words(candidate)
        score = len(words & other) / len(words | other)
        # Every explicitly requested detail must survive. Catalogue verbosity
        # is harmless when all requested words are present.
        if not words <= other:
            continue
        if type_words and not type_words <= other:
            if not remembered and not other <= words | default_type:
                continue
        if any(words & category and not (words | remembered) & qualifiers and other & qualifiers
               for category, qualifiers in CATEGORY_QUALIFIERS.items()):
            continue
        # A noodle soup sachet is not a packet of instant noodles either.
        if 'noodle' in words and 'soup' not in words and 'soup' in other:
            continue
        if plain_category:
            plain_title = name_words(candidate['name']) - name_words(candidate.get('brand') or '')
            if not plain_title <= plain_category:
                continue
        # A confirmed brand match is strong enough on its own that a long,
        # marketing-heavy store title ("Pepsi Max Cola No Sugar Soft Drink
        # Bottle") shouldn't sink a candidate just for having extra words
        # we didn't type -- as long as every word we DID type shows up.
        recall = len(words & other) / len(words)
        # Full recall clears the name floor. Extra title words settle ties
        # only after intent and size have been checked.
        if required_brand and (len(words) > 1 or size_in_name) and recall == 1:
            score = .5
        elif plain_category and recall == 1:
            score = .5
        elif type_words and recall == 1 and type_overlap(candidate):
            score = .5
        elif len(words) > 1 and recall == 1:
            score = .5
        elif recall == 1 and not required_brand:
            score = max(score, .5)
        elif score < .5:
            continue
        if preferred_size:
            size = pack_size(candidate.get('size') or '')
            embedded = pack_size(candidate['name'])
            requested_count = bare_count or pack_size(name).get('count', 1) > 1
            # A numeric retailer size can answer an explicit count request;
            # it supplies no mass or volume preference on its own.
            raw_size = (candidate.get('size') or '').strip()
            if requested_count and not size and re.fullmatch(r'\d+', raw_size):
                size = pack_size(raw_size + ' pack')
            if embedded and (not size or ('count' in size and not {'mass', 'volume'} & size.keys())
                             or requested_count and embedded.get('count', 1) > size.get('count', 1)):
                size = embedded
            if requested_count and size.get('count') != preferred_size['count']:
                continue
            # A shared single-item count cannot make powder measured in
            # kilograms a size match for milk requested in litres.
            dimensions = {'mass', 'volume'}
            if (dimensions & preferred_size.keys() and dimensions & size.keys()
                    and not dimensions & preferred_size.keys() & size.keys()):
                continue
            similarities = [min(value, size[key]) / max(value, size[key])
                            for key, value in preferred_size.items()
                            if key in size and value > 0 and size[key] > 0]
            if similarities:
                # Same name doesn't mean same product -- a 170g tub, a 1kg
                # tub, a 1.25L bottle and a 2L bottle of the exact same
                # drink can all share every word. Only abstain (no opinion)
                # when the candidate makes no size claim at all; anything
                # that isn't close to the size we know we buy (within 25%)
                # has to lose outright, not just lose a bonus -- a near
                # miss on name shouldn't out-rank the actually right size.
                if min(similarities) < .75:
                    continue
                score += 1 + sum(similarities) / len(similarities)
                if len(similarities) == len(preferred_size) and min(similarities) == 1:
                    exact_sizes.add(str(candidate["store_item_id"]))
        scored[str(candidate["store_item_id"])] = (score, candidate)
    eligible = _classification_filter(class_words or words, [pair[1] for pair in scored.values()],
                                      class_history, plain_category)
    eligible_ids = {str(c['store_item_id']) for c in eligible}
    remembered_ids = {str(c['store_item_id']) for c in eligible
                      if remembered and remembered <= candidate_words(c)}
    if remembered_ids:
        eligible_ids &= remembered_ids
    ranked = sorted((pair for key, pair in scored.items() if key in eligible_ids),
                    key=lambda pair: pair[0], reverse=True)
    if not ranked or ranked[0][0] < .5:
        return None
    tied = [pair for pair in ranked if ranked[0][0] - pair[0] < .15]
    if len(tied) > 1 and exact_sizes:
        exact = [pair for pair in tied if str(pair[1]["store_item_id"]) in exact_sizes]
        if exact:
            tied = exact
        if len(tied) == 1:
            return tied[0][1]
    if len(tied) > 1:
        # Only full-recall products can participate in title preferences;
        # a missing typed word is not evidence of a plainer match.
        if all(words <= candidate_words(pair[1]) for pair in tied):
            # Container words are packaging, not flavours. Embedded pack
            # sizes have already disappeared from the same comparison.
            def extra_words(pair):
                extra = candidate_words(pair[1]) - words - {'can', 'bottle', 'single', 'loaf'}
                if plain_category:
                    extra -= plain_category - {'sparkling', 'mineral', 'lightly'}
                # A bare household default favours everyday milk over
                # premium lines. Keep dietary words intact, and never
                # apply this preference to an explicit or remembered type.
                premium = 0
                if default_type and not preferred_type:
                    premium = len(extra & {'organic', 'biodynamic', 'premium'})
                    premium += int({'free', 'range'} <= extra)
                    premium += int({'high', 'protein'} <= extra)
                if words & {'cola', 'pepsi'}:
                    extra -= {'classic', 'original', 'regular', 'taste'}
                parsed = pack_size(pair[1].get('size') or '')
                embedded = pack_size(pair[1]['name'])
                if embedded.get('count', 1) > parsed.get('count', 1):
                    parsed = embedded
                multipack = int(not preferred_size and parsed.get('count', 1) > 1)
                if not preferred_size and 'single' in name_words(pair[1]['name']):
                    if any(pack_size(other[1].get('size') or '').get(dimension, 0) >= 4 * amount
                           for dimension, amount in parsed.items() if dimension in {'mass', 'volume'} and amount > 0
                           for other in tied):
                        multipack = -1
                return premium, multipack, len(extra)
            priority = min(extra_words(pair)[:2] for pair in tied)
            tied = [pair for pair in tied if extra_words(pair)[:2] == priority]
            # Try shared types and true plain subsets before the final
            # title/price preference. Compare residual descriptions after
            # removing owned brand words and packaging.
            def variant_words(pair):
                c = pair[1]
                extra = name_words(c['name']) - words - name_words(c.get('brand') or '') - remembered
                extra -= {'can', 'bottle', 'single', 'pack', 'squeeze', 'loaf', 'fresh', 'classic', 'original', 'regular'}
                if plain_category:
                    extra -= plain_category - {'sparkling', 'mineral', 'lightly'}
                category = GENERIC_EQUIVALENTS.get(name.casefold().strip())
                category_words = name_words(' '.join(category or ()))
                if category_words and category_words <= name_words(c['name']):
                    extra -= category_words
                if default_type <= extra and default_type:
                    extra -= default_type | {'whole'}
                if words & {'cola', 'pepsi'}:
                    extra -= {'classic', 'original', 'regular', 'taste', 'soft', 'drink', 'soda', 'cola'}
                return extra
            variants = [variant_words(pair) for pair in tied]
            plain = [pair for pair, variant in zip(tied, variants)
                     if all(variant <= other for other in variants)]
            different_variants = (not required_brand and not plain
                                  and len({frozenset(variant) for variant in variants}) > 1)

            def resolved(candidate):
                return dict(candidate, _ambiguous=True) if different_variants else candidate

            if not plain and remembered:
                plain = [pair for pair in tied if remembered <= candidate_words(pair[1])]
            # Without a shared plain subset, retain the real alternatives
            # for the final closest-title and price preference.
            tied = plain or tied
            if len(tied) == 1:
                return resolved(tied[0][1])
            if generic_type or remembered:
                overlaps = [(type_overlap(pair[1]), pair) for pair in tied]
                best = max(overlap for overlap, _ in overlaps)
                if best:
                    tied = [pair for overlap, pair in overlaps if overlap == best]
                    if len(tied) == 1:
                        return resolved(tied[0][1])
                else:
                    # A type absent from this store is no reason to guess
                    # another one or fall back from a remembered choice.
                    return None
            # A genuine plain subset and remembered types still govern the
            # product. Ownership precedes the remaining title/price guess.
            if not required_brand and not preferred_type and not preferred_classification:
                labels = {normalized_name(label) for label in HOME_BRANDS.get(store, ())}
                home = [pair for pair in tied if normalized_name(pair[1].get('brand') or '') in labels]
                tied = home or tied
            by_extra = sorted(tied, key=extra_words)
            fewest_extra = extra_words(by_extra[0])
            plainest = [pair for pair in by_extra if extra_words(pair) == fewest_extra]
            def everyday_size(finalists):
                if preferred_size:
                    return None
                # A fourfold gap separates an everyday item from a bulk
                # quantity without deciding between nearby everyday sizes.
                # Require comparable sizes for every finalist; a missing
                # size must not silently lose the tie.
                def total_size(c):
                    parsed = pack_size(c.get('size') or '')
                    return next((v for k, v in parsed.items() if k in ('volume', 'mass')), None)
                sized = [pair for pair in finalists if total_size(pair[1]) is not None]
                by_size = sorted(sized, key=lambda pair: total_size(pair[1]))
                dimensions = {key for pair in finalists
                              for key in pack_size(pair[1].get('size') or '')
                              if key in ('volume', 'mass')}
                if len(by_size) == len(finalists) and len(by_size) > 1 and len(dimensions) == 1:
                    smallest = total_size(by_size[0][1])
                    # An explicit single can also identifies the intended
                    # end of a bulk-size spread when another can sits between.
                    explicit_single = 'single' in name_words(by_size[0][1]['name'])
                    comparison = by_size[-1] if explicit_single else by_size[1]
                    if total_size(comparison[1]) >= 4 * smallest > 0 and total_size(by_size[1][1]) > smallest:
                        return by_size[0][1]
            # A squeeze title can describe another container of the same
            # product. Require the same nonempty brand and every remaining
            # product word before letting a bulk gap precede title length.
            def container_identity(pair):
                c = pair[1]
                return (normalized_name(c.get('brand') or ''),
                        frozenset(name_words(c['name']) - name_words(c.get('brand') or '')
                                  - {'can', 'bottle', 'single', 'pack', 'squeeze'}))
            identity = container_identity(plainest[0])
            if identity[0] and all(container_identity(pair) == identity for pair in plainest):
                packs = [pair for pair in tied if container_identity(pair) == identity]
                everyday = everyday_size(packs)
                if everyday is not None:
                    return resolved(everyday)
            if len(plainest) == 1:
                return resolved(plainest[0][1])
            everyday = everyday_size(plainest)
            if everyday is not None:
                return resolved(everyday)
        # All remaining products have survived explicit intent and size
        # checks. Prefer fewer unrequested words, then the lowest valid
        # shelf price: it is the smaller household spend for an equal title.
        # Missing prices lose; stable IDs settle equal or absent prices so
        # catalogue retrieval order cannot change the result.
        def closest(pair):
            candidate = pair[1]
            price = candidate.get('price')
            valid_price = (isinstance(price, (int, float)) and not isinstance(price, bool)
                           and math.isfinite(price) and price > 0)
            return (extra_words(pair)[2], price if valid_price else math.inf,
                    str(candidate['store_item_id']))
        # Price has settled a remaining tie, not established intent. Return
        # a copy because search results can be reused for another query.
        return dict(min(plainest, key=closest)[1], _ambiguous=True)
    return ranked[0][1]


def _new_match(db, product_id):
    from .. import order_history
    return (not db.execute("SELECT 1 FROM product_store_links WHERE product_id=?", (product_id,)).fetchone()
            and not order_history.recent(db, product_id))


def auto_match(db, product_id, name, before_search=None, search_fn=None, excluded_stores=()):
    matched = []
    settings = db.execute("""SELECT * FROM store_settings s WHERE enabled=1
        AND NOT EXISTS (SELECT 1 FROM product_store_links l
            WHERE l.product_id=? AND l.store=s.store) ORDER BY store""", (product_id,)).fetchall()
    pending = [s for s in settings if s['store'] not in excluded_stores]
    propagate = _new_match(db, product_id)
    if propagate:
        named_stores = re.findall(r'\b(?:coles|woolworths|aldi|iga)\b', name.casefold())
        pending.sort(key=lambda s: named_stores.index(s['store']) if s['store'] in named_stores else len(named_stores))
    query = name
    seeded = False
    deferred = []
    known_brands = catalogue_brands(db)

    def save(setting, candidate):
        with db:
            link_id = link_product(db, product_id, setting["store"], str(candidate["store_item_id"]), candidate["name"], size=candidate.get("size"),
                                   needs_review=candidate.get("_ambiguous", False),
                                   is_substitute=substitute_pick(name, setting["store"], candidate, known_brands))
            record_price(db, link_id, candidate)
        matched.append({"product_id": product_id, "name": name, "store": setting["store"]})

    while pending:
        setting = pending.pop(0)
        try:
            if before_search and before_search() is False:
                break
            if search_fn:
                candidates = search_fn(setting["store"], query, setting["postcode"])
            else:
                candidates = search(setting["store"], query, setting["postcode"], conn=db)
            if query != name:
                original_words = name_words(name)
                candidates = _classification_filter(original_words, candidates,
                    plain_category=PLAIN_CATEGORY_WORDS.get(next(iter(original_words))) if len(original_words) == 1 else None)
                candidates = [c for c in candidates if _department_allowed(original_words, c)]
            # Retrieval uses the seed description; original explicit intent,
            # history and classification still govern every selected result.
            candidate = household_match(db, product_id, query, setting["store"], candidates)
            if candidate is not None and query != name:
                if strongest_match(name, [candidate], size_preference(db, product_id, setting['store']),
                                   type_preference(db, product_id, setting['store']), store=setting['store'],
                                   preferred_classification=classification_preference(db, product_id, setting['store'])) is None:
                    candidate = None
            if candidate is None and propagate and seeded and query != name:
                # A seed adds a possible description, not a new household
                # requirement. Stores without that wording retry the request.
                original_query, _ = _query_pack(name)
                if search_fn:
                    original = search_fn(setting["store"], original_query, setting["postcode"])
                else:
                    original = search(setting["store"], original_query, setting["postcode"], conn=db)
                candidate = household_match(db, product_id, name, setting["store"], original)
            if propagate and not seeded:
                if (candidate is None or candidate.get('_ambiguous')
                        or substitute_pick(name, setting['store'], candidate, known_brands)):
                    deferred.append((setting, candidate))
                    continue
                generic = name_words(candidate['name']) - name_words(candidate.get('brand') or '')
                if generic:
                    seeded = True
                    explicit, _ = _query_pack(name, candidates)
                    sizes = [m.group() for m in re.finditer(SIZE_TOKEN, explicit, re.IGNORECASE)]
                    # Pack wording belongs to the count constraint, not the
                    # description: other stores omit it from their titles.
                    if pack_size(explicit).get('count', 1) > 1:
                        generic -= {'pack'}
                    query = ' '.join([*sorted(generic), *sizes])
                    pending = [s for s, _ in deferred] + pending
                    deferred = []
            if candidate is not None:
                save(setting, candidate)
        except Exception:
            db.rollback()
            log.warning("Product lookup failed for %s", setting["store"], exc_info=True)
    for setting, candidate in deferred:
        if candidate is not None:
            try:
                save(setting, candidate)
            except Exception:
                db.rollback()
                log.warning("Saving a match failed for %s", setting['store'], exc_info=True)
    return matched


def auto_match_live(db, product_id, name, excluded_stores=()):
    """New items establish a seed before searching the remaining stores.
    Existing items with household history can search concurrently; paced
    nightly/weekly jobs continue to use auto_match directly."""
    if _new_match(db, product_id):
        return auto_match(db, product_id, name, excluded_stores=excluded_stores)

    from concurrent.futures import ThreadPoolExecutor

    settings = [s for s in db.execute("""SELECT * FROM store_settings s WHERE enabled=1
        AND NOT EXISTS (SELECT 1 FROM product_store_links l
            WHERE l.product_id=? AND l.store=s.store) ORDER BY store""", (product_id,)).fetchall()
        if s["store"] not in excluded_stores]
    if not settings:
        return []

    def fetch(setting):
        try:
            return setting["store"], search(setting["store"], name, setting["postcode"]), None
        except Exception as exc:
            return setting["store"], None, exc

    matched = []
    with ThreadPoolExecutor(max_workers=len(settings)) as pool:
        for store, candidates, exc in pool.map(fetch, settings):
            if exc is not None:
                log.warning("Product lookup failed for %s", store, exc_info=exc)
                continue
            candidate = household_match(db, product_id, name, store, candidates)
            if candidate is None:
                continue
            try:
                with db:
                    link_id = link_product(db, product_id, store, str(candidate["store_item_id"]), candidate["name"], size=candidate.get("size"),
                                           needs_review=candidate.get("_ambiguous", False),
                                           is_substitute=substitute_pick(name, store, candidate, catalogue_brands(db)))
                    record_price(db, link_id, candidate)
                matched.append({"product_id": product_id, "name": name, "store": store})
            except Exception:
                db.rollback()
                log.warning("Saving a match failed for %s", store, exc_info=True)
    return matched


def auto_match_all(db, *, search_fn=None, before_search=None, excluded_stores=(), max_searches=None):
    requested = False
    searches = 0

    def pace():
        nonlocal requested, searches
        if max_searches is not None and searches >= max_searches:
            return False
        searches += 1
        if before_search:
            return before_search()
        if requested:
            time.sleep(1.5)
        requested = True

    result = {"matched": [], "unmatched": []}
    products = db.execute("""SELECT id,name FROM products p WHERE EXISTS (
        SELECT 1 FROM store_settings s WHERE enabled=1 AND NOT EXISTS (
            SELECT 1 FROM product_store_links l WHERE l.product_id=p.id AND l.store=s.store))
        ORDER BY p.id""").fetchall()
    for product in products:
        result["matched"].extend(auto_match(db, product["id"], product["name"], pace, search_fn, excluded_stores))
        if db.execute("""SELECT 1 FROM store_settings s WHERE enabled=1 AND NOT EXISTS (
                SELECT 1 FROM product_store_links l WHERE l.product_id=? AND l.store=s.store)""",
                (product["id"],)).fetchone():
            result["unmatched"].append({"product_id": product["id"], "name": product["name"]})
    return result


def coles_catalogue_history(db, coles_id):
    """Every price the crawl has ever seen for one Coles product, oldest
    first, plus whether each "was $X" claim actually matches what the
    product has really sold for -- same 5%-of-typical rule the household's
    own price-history graph uses, just against the product's own full
    history instead of one household's purchases."""
    checks = rows(db.execute("""SELECT * FROM coles_catalogue_history WHERE coles_id=?
        ORDER BY checked_at, id""", (coles_id,)))
    prices = [c["price"] for c in checks if c["price"] is not None]
    typical = median(prices) if prices else None
    for check in checks:
        claimed_special = check["was_price"] is not None and check["price"] is not None and check["was_price"] > check["price"]
        check["fake_special"] = bool(claimed_special and typical is not None
            and abs(check["price"] - typical) <= typical * .05 + 1e-9)
    return {"typical_price": typical, "history": checks}
