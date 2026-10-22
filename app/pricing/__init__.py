"""Product matching and append-only store price observations."""
import logging
import math
import re
import time
import unicodedata

from . import stores

log = logging.getLogger(__name__)


def _fetch(store, query, postcode):
    function = getattr(stores, "search_" + store)
    if store != "coles":
        return function(query, postcode=postcode) if store == "iga" else function(query)
    try:
        return function(query)
    except Exception as exc:
        # The plain request almost always hits Coles's bot check. Only pay
        # for a real browser tab when the quick path has actually failed.
        try:
            return stores.search_coles_browser(query)
        except Exception:
            raise exc from None


def search(store, query, postcode=""):
    results = _fetch(store, query, postcode)
    if results:
        return results
    # A plain singular/plural miss shouldn't come back empty -- "carrot" has
    # to find "Carrots" just as easily as it finds "Carrot". Best effort only:
    # if the retry itself can't run (store genuinely has nothing, or a test
    # double runs out of canned responses), fall back to what we already have.
    variant = query[:-1] if query.endswith("s") and len(query) > 3 else query + "s"
    if variant == query:
        return results
    try:
        return _fetch(store, variant, postcode) or results
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


def name_words(name):
    # Keep size tokens consistent: "2 L" and "2L" mean the same thing.
    name = re.sub(r"(\d)\s+(?=[a-zA-Z])", r"\1", name)
    # Supermarket names are scoring noise; keep stored names and brand guards intact.
    words = set(re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold())) - {"woolworths", "coles", "aldi", "iga"}
    # A plain trailing "s" shouldn't count as a different word -- "carrot"
    # and "carrots" are the same item, not a mismatch.
    return {w[:-1] if w.endswith("s") and len(w) > 3 else w for w in words}


def pack_size(size):
    text = size.casefold().strip().replace('×', 'x')
    multi = re.search(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*(ml|kg|g|l)\b", text)
    if multi:
        count, amount, unit = multi.groups()
        factor = 1000 if unit in ('kg', 'l') else 1
        return {'count': float(count), 'volume' if unit in ('ml', 'l') else 'mass': float(count) * float(amount) * factor}
    count = re.search(r"(\d+(?:\.\d+)?)\s*[- ]?\s*(?:pack|pk|bottles?|pieces?|each)\b", text)
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


def strongest_match(name, candidates, preferred_size=None):
    words = name_words(name)
    if not words:
        return None
    first = re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold())[0]
    brands = {normalized_name(p.get("brand") or "") for p in candidates}
    required_brand = first if first in brands else None
    scored = {}
    for candidate in candidates:
        if required_brand and normalized_name(candidate.get("brand") or "") != required_brand:
            continue
        other = name_words(candidate["name"])
        score = len(words & other) / len(words | other)
        if score < .5:
            continue
        if preferred_size:
            size = pack_size(candidate.get('size') or '')
            similarities = [min(value, size[key]) / max(value, size[key])
                            for key, value in preferred_size.items()
                            if key in size and value > 0 and size[key] > 0]
            if similarities:
                # Same name doesn't mean same product -- a 170g tub and a 1kg
                # tub can share every word. Only abstain (no opinion) when the
                # candidate makes no size claim at all; a real claim that's
                # wildly off (less than 40% the size we know we buy) has to
                # lose outright, not just lose a bonus.
                if min(similarities) < .4:
                    continue
                score += .35 * sum(similarities) / len(similarities)
        scored[str(candidate["store_item_id"])] = (score, candidate)
    ranked = sorted(scored.values(), key=lambda pair: pair[0], reverse=True)
    if not ranked or ranked[0][0] < .5:
        return None
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < .15:
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
            candidates = (search_fn or search)(setting["store"], name, setting["postcode"])
            candidate = strongest_match(name, candidates, size_preference(db, product_id, setting["store"]))
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
