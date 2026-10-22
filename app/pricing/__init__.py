"""Product matching and append-only store price observations."""
import logging
import math
import re
import time
import unicodedata

from . import stores

log = logging.getLogger(__name__)


def search(store, query, postcode=""):
    function = getattr(stores, "search_" + store)
    if store == "iga":
        return function(query, postcode=postcode)
    return function(query)


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
    return set(re.findall(r"\w+", unicodedata.normalize("NFKC", name).casefold())) - {"woolworths", "coles", "aldi", "iga"}


def strongest_match(name, candidates):
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
            candidate = strongest_match(name, candidates)
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
