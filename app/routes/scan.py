from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends

from ..auth import current_user
from ..db import get_db, row, rows
from .products import load_product

router = APIRouter(prefix="/api/scan", tags=["scanning"], dependencies=[Depends(current_user)])

OFF_URL = "https://world.openfoodfacts.org/api/v0/product/{}.json"


def lookup_off(code):
    """Ask OpenFoodFacts about a barcode. Returns None if it doesn't know it."""
    # quote everything, scanned QR codes can have slashes and colons in them
    url = OFF_URL.format(quote(code, safe=""))
    try:
        r = httpx.get(url, timeout=10, headers={"User-Agent": "Larder/0.1 (home pantry app)"})
        data = r.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("status") != 1:
        return None
    p = data.get("product")
    if p is None:
        p = {}
    if not isinstance(p, dict):
        return None
    if any(p.get(field) is not None and not isinstance(p[field], str) for field in
           ("product_name", "generic_name", "brands", "quantity", "image_front_small_url", "image_small_url", "image_url")):
        return None
    name = (p.get("product_name") or p.get("generic_name") or "").strip()
    brand = (p.get("brands") or "").split(",")[0].strip()
    # "Heinz" + "Baked Beans" -> "Heinz Baked Beans", but don't double up if the name already has it
    if brand and brand.lower() not in name.lower():
        name = f"{brand} {name}".strip()
    return {
        "name": name,
        "brand": brand,
        "quantity": p.get("quantity") or "",
        "image_url": p.get("image_front_small_url") or p.get("image_small_url") or p.get("image_url") or "",
    }


@router.get("/{code:path}")
def scan(code: str, db=Depends(get_db)):
    """Look a barcode up. Known ones come back with the product, unknown ones with
    whatever OpenFoodFacts has so the product can be made on the spot."""
    code = code.strip()
    if not code:
        return {"found": False, "code": code, "off": None}
    b = row(db.execute("SELECT * FROM barcodes WHERE code = ?", (code,)))
    if b:
        return {"found": True, "code": code, "barcode_amount": b["amount"], "product": load_product(db, b["product_id"])}
    # not a barcode we know, but maybe the scan was a product name typed in by hand
    by_name = rows(db.execute("SELECT id, name, picture FROM products WHERE name LIKE ? ORDER BY name LIMIT 8",
                              (f"%{code}%",)))
    off = lookup_off(code) if len(code) >= 6 else None
    return {"found": False, "code": code, "off": off, "matches": by_name}
