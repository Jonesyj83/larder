"""Recent shopping quantities and pack choices."""
import logging


def recent(db, product_id, store=None):
    return [dict(r) for r in db.execute("""SELECT h.*, u.name AS unit FROM order_history h
        LEFT JOIN units u ON u.id=h.unit_id WHERE h.product_id=?
        AND (? IS NULL OR h.store=?) ORDER BY h.created_at DESC,h.id DESC LIMIT 20""",
        (product_id, store, store))]


def typical(entries, fields=("amount", "unit_id")):
    if not entries:
        return None
    # Entries arrive newest first, so equally frequent choices favour the latest.
    counts = {}
    for entry in entries:
        key = tuple(entry[f] for f in fields)
        counts[key] = counts.get(key, 0) + 1
    key = max(counts, key=counts.get)
    return next(e for e in entries if tuple(e[f] for f in fields) == key)


def record(db, item, store=None):
    if not item["product_id"]:
        return
    try:
        db.execute("SAVEPOINT order_history_insert")
        link = db.execute("SELECT size,store_item_id,store_name,is_substitute FROM product_store_links WHERE product_id=? AND store=?",
                          (item["product_id"], store)).fetchone() if store else None
        brand = ''
        if link:
            from .pricing import _CATALOGUE_TABLES
            table, key = _CATALOGUE_TABLES[store]
            candidate = db.execute(f"SELECT brand FROM {table} WHERE {key}=?", (link['store_item_id'],)).fetchone()
            brand = candidate['brand'] if candidate else ''
        db.execute("""INSERT INTO order_history(product_id,amount,unit_id,store,size,store_item_id,store_name,store_brand,is_substitute)
            VALUES(?,?,?,?,?,?,?,?,?)""", (item["product_id"], item["amount"], item["unit_id"],
                                   store, link["size"] if link else "",
                                   link["store_item_id"] if link else "", link["store_name"] if link else "",
                                   brand, link["is_substitute"] if link else 0))
        db.execute("RELEASE order_history_insert")
    except Exception:
        try:
            db.execute("ROLLBACK TO order_history_insert")
            db.execute("RELEASE order_history_insert")
        except Exception:
            pass
        logging.getLogger(__name__).warning("Shopping history could not be saved", exc_info=True)
