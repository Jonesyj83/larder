"""Product matching and append-only store price observations."""
import logging
import math
import re
import unicodedata

from . import stores

log = logging.getLogger(__name__)


def search(store, query, postcode=""):
    function = getattr(stores, "search_" + store)
    if store == "iga":
        return function(query, postcode=postcode)
    return function(query)


def link_product(db, product_id, store, item_id, name, matched_by=None):
    existing = db.execute("SELECT id,store_item_id FROM product_store_links WHERE product_id=? AND store=?",
                          (product_id, store)).fetchone()
    if existing and existing["store_item_id"] != item_id:
        # A replacement identity must not inherit the previous item's history.
        db.execute("DELETE FROM product_store_links WHERE id=?", (existing["id"],))
    db.execute("""INSERT INTO product_store_links(product_id,store,store_item_id,store_name,matched_by)
        VALUES(?,?,?,?,?) ON CONFLICT(product_id,store) DO UPDATE SET
        store_name=excluded.store_name, matched_by=excluded.matched_by""",
        (product_id, store, item_id, name, matched_by))
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


def auto_match(db, product_id, name):
    for setting in db.execute("SELECT * FROM store_settings WHERE enabled=1").fetchall():
        try:
            candidates = search(setting["store"], name, setting["postcode"])
            matches = {str(p["store_item_id"]): p for p in candidates
                       if normalized_name(p["name"]) == normalized_name(name)}
            if len(matches) != 1:
                continue
            candidate = next(iter(matches.values()))
            with db:
                link_id = link_product(db, product_id, setting["store"], str(candidate["store_item_id"]), candidate["name"])
                record_price(db, link_id, candidate)
        except Exception:
            db.rollback()
            log.warning("New product lookup failed for %s", setting["store"], exc_info=True)
