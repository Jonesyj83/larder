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
    "pepsi": ("cola", "soft", "drink"),
    "coke": ("cola", "soft", "drink"),
    "coca-cola": ("cola", "soft", "drink"),
    "vegemite": ("yeast", "spread"),
    "nutella": ("hazelnut", "chocolate", "spread"),
}

# Only aliases backed by a stored catalogue brand belong here.
BRAND_ALIASES = {"coke": "coca-cola"}
QUERY_CORRECTIONS = {"redbull": "red bull"}


_CATALOGUE_TABLES = {
    "coles": ("coles_catalogue", "coles_id"),
    "woolworths": ("woolworths_catalogue", "stockcode"),
    "aldi": ("aldi_catalogue", "item_id"),
}


def _fetch(store, query, postcode, conn=None):
    if store in _CATALOGUE_TABLES:
        return _catalogue_lookup(store, query, conn=conn)
    function = getattr(stores, "search_" + store)
    return function(query, postcode=postcode) if store == "iga" else function(query)


def _catalogue_lookup(store, query, limit=None, conn=None):
    """Coles, Woolworths and Aldi all get matched against a locally
    crawled copy of their whole range instead of a live request per item
    (see scripts/*_catalogue_crawl.py) -- Coles because its live search is
    bot-walled, the other two just because asking live never builds real
    history for anything beyond the household's own shortlist. Matching
    an item just means searching the local copy, same shape of result as
    any store's live search would give strongest_match to rank.

    conn lets a caller that already has the right connection (the current
    request, a test's own db) use it directly; otherwise this opens and
    closes its own, since auto_match_live can call this from a worker
    thread that has no connection of its own to hand in."""
    table, id_field = _CATALOGUE_TABLES[store]
    words = [w for w in re.findall(r"\w+", query.casefold()) if len(w) > 2]
    if not words:
        return []
    # Broad staples need room for real variants beyond the first few rows.
    # Keep the window bounded; ambiguity still belongs to strongest_match.
    if limit is None:
        limit = 200 if len(words) == 1 else 30
    owns_conn = conn is None
    if owns_conn:
        from .. import db as database
        conn = database.connect()
    try:
        # Match a word against either column -- Coles in particular often
        # strips the brand clean out of the name itself ("Soft Drink Cola
        # Bottle" with brand="Pepsi" sitting separately), so a brand-only
        # query like "Pepsi" would otherwise never find it by name alone.
        #
        # Order by how many of the typed words actually matched, not just
        # whichever rows the table happens to hit first -- a common word
        # like "red" alone matches hundreds of unrelated rows (Red Onion,
        # Red Wine, Red Capsicum...), and with no ranking those can fill
        # the whole result before a real "Red Bull" row ever gets seen.
        match_clauses = ["(name LIKE ? OR brand LIKE ?)" for _ in words]
        relevance = " + ".join(f"CASE WHEN {clause} THEN 1 ELSE 0 END" for clause in match_clauses)
        params = [p for w in words for p in (f"%{w}%", f"%{w}%")]
        clause = " OR ".join(match_clauses)
        found = conn.execute(
            f"SELECT {id_field} AS item_id, name, brand, size, price, was_price, unit_price, unit_measure, "
            f"({relevance}) AS relevance FROM {table} "
            f"WHERE {clause} ORDER BY relevance DESC LIMIT ?", (*params, *params, limit)).fetchall()
    finally:
        if owns_conn:
            conn.close()
    return [{"store_item_id": str(r["item_id"]), "name": r["name"], "brand": r["brand"], "size": r["size"],
             "price": r["price"], "was_price": r["was_price"], "unit_price": r["unit_price"],
             "unit_measure": r["unit_measure"], "on_special": bool(r["was_price"])} for r in found]


def search(store, query, postcode="", conn=None):
    query = QUERY_CORRECTIONS.get(query.casefold().strip(), query)
    results = _fetch(store, query, postcode, conn=conn)
    if results:
        return results
    # A plain singular/plural miss shouldn't come back empty -- "carrot" has
    # to find "Carrots" just as easily as it finds "Carrot". Best effort only:
    # if the retry itself can't run (store genuinely has nothing, or a test
    # double runs out of canned responses), fall back to what we already have.
    variant = query[:-1] if query.endswith("s") and len(query) > 3 else query + "s"
    if variant != query:
        try:
            results = _fetch(store, variant, postcode, conn=conn) or results
        except Exception:
            pass
    if results or store != "aldi":
        return results
    # Named brands can exist in Aldi's range too. Only an empty search
    # permits a category substitute; direct stock always takes precedence.
    first = query.casefold().split()
    generic = GENERIC_EQUIVALENTS.get(first[0]) if first else None
    if not generic:
        return results
    fallback_query = " ".join(generic) + " " + " ".join(first[1:])
    try:
        found = _fetch(store, fallback_query, postcode, conn=conn)
        # OR-based retrieval alone is too broad for a category substitute.
        # Preserve the category query for scoring instead of comparing the
        # substitute against a brand it deliberately does not claim.
        wanted = name_words(fallback_query)
        return [dict(c, _match_query=fallback_query) for c in found
                if wanted <= (name_words(c['name']) | name_words(c.get('brand') or ''))] or results
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
SIZE_TOKEN = (r"\d+(?:\.\d+)?\s*x\s*\d+(?:\.\d+)?\s*(?:ml|kg|g|l)\b"
              r"|\d+(?:\.\d+)?\s*(?:ml|kg|g|l)\b"
              r"|\d+(?:\.\d+)?\s*[- ]?\s*(?:pack|pk|bottles?|pieces?|each)\b")


def name_words(name):
    # Keep size tokens consistent: "2 L" and "2L" mean the same thing.
    name = re.sub(r"(\d)\s+(?=[a-zA-Z])", r"\1", name)
    name = re.sub(SIZE_TOKEN, " ", name, flags=re.IGNORECASE)
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
    if candidates and all(c.get('_match_query') == candidates[0].get('_match_query') for c in candidates):
        name = candidates[0].get('_match_query') or name
    name = QUERY_CORRECTIONS.get(name.casefold().strip(), name)
    words = name_words(name)
    if not words:
        return None
    # A size claim right there in what was typed ("1.25L Pepsi") is real
    # specificity, same as typing an extra descriptor word -- it's not
    # "just a brand name" with nothing else to go on, even though the
    # size token itself gets stripped out of word-overlap scoring below.
    size_in_name = bool(pack_size(name))
    if not preferred_size:
        preferred_size = pack_size(name)
    # The leading word decides the brand check -- but it has to be the
    # first REAL word, not a size token's leftover digits ("1" out of
    # "1.25L Pepsi Max").
    stripped = re.sub(SIZE_TOKEN, " ", re.sub(r"(\d)\s+(?=[a-zA-Z])", r"\1", name), flags=re.IGNORECASE)
    leading = re.findall(r"\w+", unicodedata.normalize("NFKC", stripped).casefold())
    first = leading[0] if leading else ""
    brands = {normalized_name(p.get("brand") or "") for p in candidates}
    brand = normalized_name(BRAND_ALIASES.get(first, first))
    required_brand = brand if brand in brands else None
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
    for candidate in candidates:
        if required_brand and normalized_name(candidate.get("brand") or "") != required_brand:
            continue
        other = candidate_words(candidate)
        score = len(words & other) / len(words | other)
        # A confirmed brand match is strong enough on its own that a long,
        # marketing-heavy store title ("Pepsi Max Cola No Sugar Soft Drink
        # Bottle") shouldn't sink a candidate just for having extra words
        # we didn't type -- as long as every word we DID type shows up.
        recall = len(words & other) / len(words)
        # The bonus only kicks in when there's a real word beyond the brand
        # itself to check recall against -- "Vegemite" alone matching every
        # word of itself proves nothing, it's the whole query. Clamped up
        # to the floor, never past it -- a messy full-recall name must
        # never outrank a cleaner one that already cleared the floor on
        # its own ("Pepsi Max Cola Drink" vs "...Vanilla...").
        if required_brand and (len(words) > 1 or size_in_name) and recall == 1:
            score = max(score, .5)
        elif score < .5:
            continue
        if preferred_size:
            size = pack_size(candidate.get('size') or '')
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
        scored[str(candidate["store_item_id"])] = (score, candidate)
    ranked = sorted(scored.values(), key=lambda pair: pair[0], reverse=True)
    if not ranked or ranked[0][0] < .5:
        return None
    tied = [pair for pair in ranked if ranked[0][0] - pair[0] < .15]
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
                return candidate_words(pair[1]) - words - {'can', 'bottle', 'single'}
            by_extra = sorted(tied, key=lambda pair: len(extra_words(pair)))
            fewest_extra = len(extra_words(by_extra[0]))
            plainest = [pair for pair in by_extra if len(extra_words(pair)) == fewest_extra]
            if len(plainest) == 1:
                return plainest[0][1]
            if not preferred_size:
                # A fourfold gap separates an everyday item from a bulk
                # quantity without deciding between nearby everyday sizes.
                # Require comparable sizes for every finalist; a missing
                # size must not silently lose the tie.
                def total_size(c):
                    parsed = pack_size(c.get('size') or '')
                    return next((v for k, v in parsed.items() if k in ('volume', 'mass')), None)
                sized = [pair for pair in plainest if total_size(pair[1]) is not None]
                by_size = sorted(sized, key=lambda pair: total_size(pair[1]))
                dimensions = {key for pair in plainest
                              for key in pack_size(pair[1].get('size') or '')
                              if key in ('volume', 'mass')}
                if len(by_size) == len(plainest) and len(by_size) > 1 and len(dimensions) == 1:
                    smallest = total_size(by_size[0][1])
                    # An explicit single can also identifies the intended
                    # end of a bulk-size spread when another can sits between.
                    explicit_single = 'single' in name_words(by_size[0][1]['name'])
                    comparison = by_size[-1] if explicit_single else by_size[1]
                    if total_size(comparison[1]) >= 4 * smallest > 0 and total_size(by_size[1][1]) > smallest:
                        return by_size[0][1]
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
            candidate = strongest_match(name, candidates, size_preference(db, product_id, store))
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
