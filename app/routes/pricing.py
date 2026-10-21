import logging
from statistics import median

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth import current_user
from ..db import get_db, row, rows
from .. import pricing
from ..pricing.stores import STORES
from ..stock import purchase_history

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/pricing", tags=["pricing"], dependencies=[Depends(current_user)])


class StoreIn(BaseModel):
    enabled: bool
    postcode: str = Field(default="", max_length=20)


class LinkIn(BaseModel):
    store: str
    store_item_id: str = Field(min_length=1, max_length=200)
    store_name: str = Field(min_length=1, max_length=500)


def check_store(store):
    if store not in STORES:
        raise HTTPException(404, "Unknown store")


def check_product(db, product_id):
    if not db.execute("SELECT 1 FROM products WHERE id=?", (product_id,)).fetchone():
        raise HTTPException(404, "Product not found")


@router.get("/stores")
def list_stores(db=Depends(get_db)):
    return rows(db.execute("SELECT * FROM store_settings ORDER BY store"))


@router.put("/stores/{store}")
def set_store(store: str, data: StoreIn, db=Depends(get_db)):
    check_store(store)
    db.execute("""INSERT INTO store_settings(store,enabled,postcode) VALUES(?,?,?)
        ON CONFLICT(store) DO UPDATE SET enabled=excluded.enabled,postcode=excluded.postcode""",
        (store, int(data.enabled), data.postcode.strip()))
    db.commit()
    return row(db.execute("SELECT * FROM store_settings WHERE store=?", (store,)))


@router.get("/search")
def search_store(store: str, q: str = Query(min_length=1, max_length=200), db=Depends(get_db)):
    check_store(store)
    if not q.strip():
        raise HTTPException(400, "Search can't be empty")
    setting = db.execute("SELECT * FROM store_settings WHERE store=?", (store,)).fetchone()
    try:
        return pricing.search(store, q.strip(), setting["postcode"] if setting else "")
    except Exception:
        log.warning("Store search failed for %s", store, exc_info=True)
        raise HTTPException(502, "Store search unavailable")


@router.post("/products/{product_id}/link")
def set_link(product_id: int, data: LinkIn, db=Depends(get_db), user=Depends(current_user)):
    check_store(data.store)
    check_product(db, product_id)
    if not data.store_item_id.strip() or not data.store_name.strip():
        raise HTTPException(400, "Item ID and store name can't be empty")
    with db:
        link_id = pricing.link_product(db, product_id, data.store, data.store_item_id.strip(),
                                       data.store_name.strip(), user["id"])
    return row(db.execute("SELECT * FROM product_store_links WHERE id=?", (link_id,)))


@router.delete("/products/{product_id}/link/{store}")
def delete_link(product_id: int, store: str, db=Depends(get_db)):
    check_store(store)
    check_product(db, product_id)
    db.execute("DELETE FROM product_store_links WHERE product_id=? AND store=?", (product_id, store))
    db.commit()
    return {"ok": True}


@router.get("/products/{product_id}")
def product_prices(product_id: int, db=Depends(get_db)):
    check_product(db, product_id)
    links = rows(db.execute("SELECT * FROM product_store_links WHERE product_id=? ORDER BY store", (product_id,)))
    for link in links:
        link["latest_check"] = row(db.execute("""SELECT * FROM store_price_checks WHERE link_id=?
            ORDER BY checked_at DESC,id DESC LIMIT 1""", (link["id"],)))
    return links


@router.get("/products/{product_id}/history")
def product_history(product_id: int, db=Depends(get_db)):
    check_product(db, product_id)
    links = rows(db.execute("""SELECT l.*, COALESCE(s.enabled,0) AS enabled
        FROM product_store_links l LEFT JOIN store_settings s ON s.store=l.store
        WHERE l.product_id=? ORDER BY l.store""", (product_id,)))
    for link in links:
        checks = rows(db.execute("""SELECT * FROM store_price_checks WHERE link_id=?
            ORDER BY checked_at,id""", (link["id"],)))
        prices = [check["price"] for check in checks if check["price"] is not None]
        typical = median(prices) if prices else None
        link["typical_price"] = typical
        for check in checks:
            check["fake_special"] = bool(check["on_special"] and check["price"] is not None
                and typical is not None and abs(check["price"] - typical) <= typical * .05 + 1e-9)
        link["history"] = checks
    return {"stores": links, "purchases": purchase_history(db, product_id)}
