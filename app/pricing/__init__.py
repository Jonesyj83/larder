"""Product matching and append-only store price observations."""
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
             "SIMPLEE", "BLACKSTONE GOURMET SNACK CO."),
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
    words = sorted(name_words(query, singular=False))
    scoring_words = name_words(query)
    if not words:
        return []
    if limit is None:
        limit = 200
    owns_conn = conn is None
    if owns_conn:
        from .. import db as database
        conn = database.connect()
    try:
        if store == "iga":
            source = conn.execute("SELECT location FROM iga_catalogue_store WHERE id=1").fetchone()
            if not source or source['location'] != postcode.strip().casefold():
                return []
        # Expand canonical synonyms back to their catalogue spellings. Porter
        # handles regular plurals; phrases preserve retailer token boundaries.
        alternatives = {"hilo": ("hilo", "hi lo"),
                        "coca": ("coca", "coke"), "cola": ("cola", "coke")}
        for alias, canonical in SYNONYMS.items():
            alternatives[canonical] = (*alternatives.get(canonical, (canonical,)), alias)
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
            f"c.was_price, c.unit_price, c.unit_measure, matching_coverage(c.name,c.brand) AS coverage, "
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
                    f"c.was_price, c.unit_price, c.unit_measure FROM {index} "
                    f"JOIN {table} c ON c.rowid={index}.rowid WHERE {index} MATCH ? "
                    f"ORDER BY bm25({index}), c.rowid LIMIT ?",
                    (" OR ".join(quoted(fragment) for fragment in fragments), limit)).fetchall()
    finally:
        if owns_conn:
            conn.close()
    return [{"store_item_id": str(r["item_id"]), "name": r["name"], "brand": r["brand"], "size": r["size"],
             "price": r["price"], "was_price": r["was_price"], "unit_price": r["unit_price"],
             "unit_measure": r["unit_measure"], "on_special": bool(r["was_price"])} for r in found]


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


def link_product(db, product_id, store, item_id, name, matched_by=None, size=""):
    existing = db.execute("SELECT id,store_item_id FROM product_store_links WHERE product_id=? AND store=?",
                          (product_id, store)).fetchone()
    if existing and existing["store_item_id"] != item_id:
        # A replacement identity must not inherit the previous item's history.
        db.execute("DELETE FROM product_store_links WHERE id=?", (existing["id"],))
    db.execute("""INSERT INTO product_store_links(product_id,store,store_item_id,store_name,matched_by,size)
        VALUES(?,?,?,?,?,?) ON CONFLICT(product_id,store) DO UPDATE SET
        store_name=excluded.store_name, matched_by=excluded.matched_by,
        size=CASE WHEN excluded.size != '' THEN excluded.size ELSE product_store_links.size END""",
        (product_id, store, item_id, name, matched_by, size or ""))
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
    db.execute("""INSERT INTO store_price_checks(link_id,price,was_price,unit_price,unit_measure,on_special)
        VALUES(?,?,?,?,?,?)""", (link_id, *values, candidate.get("unit_measure") or "",
                                 int(bool(candidate.get("on_special")))))


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
    "shredded": "grated", "sach": "sachet", "hilo": "lite", "light": "lite",
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
    name = re.sub(r"\bhi[ -]+lo\b", "hilo", name)
    name = re.sub(r"\bww\b", "woolworths", name)
    for alias, formal in BRAND_ALIASES.items():
        name = re.sub(r"\b" + re.escape(alias) + r"\b", formal, name)
    name = re.sub(r"(\d)\s+(?=(?:ml|kg|g|l)\b)", r"\1", name)
    name = re.sub(SIZE_TOKEN, " ", name, flags=re.IGNORECASE)
    # Supermarket names are scoring noise; keep stored names and brand guards intact.
    words = set(re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold())) - {"woolworths", "coles", "aldi", "iga", "and"}
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
    links = db.execute("""SELECT store,store_item_id,store_name FROM product_store_links
        WHERE product_id=?""", (product_id,)).fetchall()
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


def strongest_match(name, candidates, preferred_size=None, preferred_type=None, *, store=None):
    if candidates and all(c.get('_match_query') == candidates[0].get('_match_query') for c in candidates):
        name = candidates[0].get('_match_query') or name
    name = QUERY_CORRECTIONS.get(name.casefold().strip(), name)
    words = name_words(name)
    if not words:
        return None
    default_type = name_words(DEFAULT_VARIANT.get(next(iter(words)), "")) if len(words) == 1 else set()
    remembered = set(preferred_type or ())
    if len(words) > 1 and not any(words | remembered <= (name_words(c['name']) | name_words(c.get('brand') or '')) for c in candidates):
        remembered = set()
    type_words = (remembered if not words & {"cola", "pepsi"} else set()) or default_type
    # A bare category needs a real type signal before descriptive titles
    # can clear the name floor. Explicit query words still take precedence.
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
    def candidate_words(c):
        # Some stores (Coles especially) strip the brand clean out of the
        # name itself ("Soft Drink Cola Bottle" with brand="Pepsi" sitting
        # separately) -- count the brand's own words too everywhere a
        # candidate's words get checked, or a brand-only query like
        # "Pepsi" could never recall a word it's genuinely claiming, just
        # not inside the name text.
        return name_words(c["name"]) | name_words(c.get("brand") or "")
    scored = {}
    exact_sizes = set()
    for candidate in candidates:
        if required_brand and normalized_name(candidate.get("brand") or "") != required_brand:
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
        # Unqualified cola means the regular line, including Classic titles.
        if words & {'cola', 'pepsi'} and not words & {'diet', 'zero', 'max', 'no', 'free'}:
            if other & {'diet', 'zero', 'max', 'no', 'free'}:
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
        elif type_words and recall == 1 and type_overlap(candidate):
            score = .5
        elif len(words) > 1 and recall == 1:
            score = .5
        elif score < .5:
            continue
        if preferred_size:
            size = pack_size(candidate.get('size') or '')
            embedded = pack_size(candidate['name'])
            if embedded and (not size or ('count' in size and not {'mass', 'volume'} & size.keys())):
                size = embedded
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
    ranked = sorted(scored.values(), key=lambda pair: pair[0], reverse=True)
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
        # Several real products tie -- usually different flavour/variant
        # lines of the same thing ("Pepsi Max", "Pepsi Vanilla") that all
        # genuinely contain every word that was typed, just with extra
        # words tacked on. Only worth breaking the tie when that's true
        # for every one of them -- a candidate missing a word that was
        # actually typed ("Pork Chops" vs "Pork Loin Chops") is a real
        # gap, not just a plainer name, and must stay a toss-up.
        if all(words <= candidate_words(pair[1]) for pair in tied):
            # Container words are packaging, not flavours. Embedded pack
            # sizes have already disappeared from the same comparison.
            def extra_words(pair):
                extra = candidate_words(pair[1]) - words - {'can', 'bottle', 'single'}
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
            # Title length cannot decide between distinct unrequested types.
            # Compare residual descriptions after removing owned brand words
            # and packaging; only a shared type or a true plain subset wins.
            def variant_words(pair):
                c = pair[1]
                extra = name_words(c['name']) - words - name_words(c.get('brand') or '') - remembered
                extra -= {'can', 'bottle', 'single', 'pack', 'squeeze', 'fresh', 'classic', 'original', 'regular'}
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
            if not plain and remembered:
                plain = [pair for pair in tied if remembered <= candidate_words(pair[1])]
            if not plain:
                return None
            if (len(plain) == 1 and len(tied) > 1 and not required_brand
                    and not default_type and not remembered):
                candidate = plain[0][1]
                labels = {normalized_name(label) for label in HOME_BRANDS.get(store, ())}
                # An unspecified brand's short title is not a confirmed
                # category default when other real variants are present.
                if (candidate.get('brand') and normalized_name(candidate['brand']) not in labels
                        and not variant_words(plain[0])):
                    return None
            tied = plain
            if len(tied) == 1:
                return tied[0][1]
            if generic_type or remembered:
                overlaps = [(type_overlap(pair[1]), pair) for pair in tied]
                best = max(overlap for overlap, _ in overlaps)
                if best:
                    tied = [pair for overlap, pair in overlaps if overlap == best]
                    if len(tied) == 1:
                        return tied[0][1]
                else:
                    # A type absent from this store is no reason to guess
                    # another one or fall back from a remembered choice.
                    return None
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
                    return everyday
            if len(plainest) == 1:
                return plainest[0][1]
            everyday = everyday_size(plainest)
            if everyday is not None:
                return everyday
            # A house label only settles the remaining tie, never a named
            # brand, a remembered type, or nearby house sizes.
            if not required_brand:
                labels = {normalized_name(label) for label in HOME_BRANDS.get(store, ())}
                home = [pair for pair in plainest
                        if normalized_name(pair[1].get('brand') or '') in labels]
                if len(home) == 1:
                    return home[0][1]
                if len(home) > 1 and len({normalized_name(pair[1].get('brand') or '')
                                          for pair in home}) == 1:
                    # Ownership narrows the brand; the same bulk-size
                    # check can then settle its otherwise identical packs.
                    everyday = everyday_size(home)
                    if everyday is not None:
                        return everyday
        # Full Cream still leaves brands and nearby sizes undecided.
        # Equally valid brands and nearby sizes remain ambiguous.
        return None
    return ranked[0][1]


def auto_match(db, product_id, name, before_search=None, search_fn=None, excluded_stores=()):
    matched = []
    settings = db.execute("""SELECT * FROM store_settings s WHERE enabled=1
        AND NOT EXISTS (SELECT 1 FROM product_store_links l
            WHERE l.product_id=? AND l.store=s.store) ORDER BY store""", (product_id,)).fetchall()
    for setting in settings:
        if setting["store"] in excluded_stores:
            continue
        try:
            if before_search and before_search() is False:
                break
            if search_fn:
                candidates = search_fn(setting["store"], name, setting["postcode"])
            else:
                candidates = search(setting["store"], name, setting["postcode"], conn=db)
            candidate = strongest_match(name, candidates, size_preference(db, product_id, setting["store"]),
                                        type_preference(db, product_id, setting["store"]), store=setting["store"])
            if candidate is None:
                continue
            with db:
                link_id = link_product(db, product_id, setting["store"], str(candidate["store_item_id"]), candidate["name"], size=candidate.get("size"))
                record_price(db, link_id, candidate)
            matched.append({"product_id": product_id, "name": name, "store": setting["store"]})
        except Exception:
            db.rollback()
            log.warning("Product lookup failed for %s", setting["store"], exc_info=True)
    return matched


def auto_match_live(db, product_id, name, excluded_stores=()):
    """Same job as auto_match, but for the moment someone's actually sitting
    there waiting on an "add item" click -- asks every store at once instead
    of one after another, so it's one slow store's wait, not the sum of all
    of them. Never used for the paced nightly/weekly batch jobs; those stay
    sequential on purpose."""
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
            candidate = strongest_match(name, candidates, size_preference(db, product_id, store),
                                        type_preference(db, product_id, store), store=store)
            if candidate is None:
                continue
            try:
                with db:
                    link_id = link_product(db, product_id, store, str(candidate["store_item_id"]), candidate["name"], size=candidate.get("size"))
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
