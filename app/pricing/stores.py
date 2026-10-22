"""Small, household-scoped adapters for the stores' rendered product data."""
import json
import logging
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
STORES = ("woolworths", "coles", "aldi", "iga")


class FailedResults(list):
    """An empty result with a failure marker for the refresh circuit breaker."""
    failed = True


def client():
    return httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True)


class PageData(HTMLParser):
    def __init__(self, script_id):
        super().__init__(convert_charrefs=True)
        self.script_id = script_id
        self.inside = False
        self.parts = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self.inside = attrs.get("id") == self.script_id
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag == "script":
            self.inside = False

    def handle_data(self, data):
        if self.inside:
            self.parts.append(data)


def payload(text, script_id):
    page = PageData(script_id)
    page.feed(text)
    if not page.parts:
        raise ValueError(f"Missing {script_id} data")
    return json.loads("".join(page.parts))


def search_woolworths(query: str, limit: int = 12):
    with client() as session:
        session.get("https://www.woolworths.com.au/").raise_for_status()
        response = session.post("https://www.woolworths.com.au/apis/ui/Search/products",
                                json={"SearchTerm": query, "PageNumber": 1, "PageSize": max(1, min(limit, 36))})
        response.raise_for_status()
        data = response.json()
    if "Products" not in data:
        raise ValueError("Missing Woolworths Products")
    flat = [p for block in data.get("Products") or [] for p in (block.get("Products") or [block])]
    return [{"store_item_id": str(p["Stockcode"]), "name": p["Name"], "brand": p.get("Brand", ""),
             "size": p.get("PackageSize", ""), "price": p.get("Price"), "was_price": p.get("WasPrice"),
             "unit_price": p.get("CupPrice"), "unit_measure": p.get("CupMeasure") or "",
             "on_special": bool(p.get("IsOnSpecial"))} for p in flat if p.get("Stockcode")]


def search_coles(query: str, limit: int = 12):
    with client() as session:
        response = session.get("https://www.coles.com.au/search/products", params={"q": query})
        response.raise_for_status()
    data = payload(response.text, "__NEXT_DATA__")
    search = data.get("props", {}).get("pageProps", {}).get("searchResults")
    if not isinstance(search, dict) or "results" not in search:
        raise ValueError("Missing Coles search results")
    out = []
    for p in (search.get("results") or [])[:max(1, min(limit, 36))]:
        if p.get("_type") != "PRODUCT":
            continue
        pricing = p.get("pricing") or {}
        unit = pricing.get("unit") or {}
        out.append({"store_item_id": str(p["id"]), "name": p["name"], "brand": p.get("brand", ""),
                    "size": p.get("size", ""), "price": pricing.get("now"), "was_price": pricing.get("was") or None,
                    "unit_price": unit.get("price"),
                    "unit_measure": f"{unit.get('ofMeasureQuantity') or ''}{unit.get('ofMeasureType') or ''}",
                    "on_special": bool(pricing.get("onlineSpecial"))})
    return out


def _unflatten(arr):
    """Resolve devalue references, keeping recursive slots from looping forever."""
    if not isinstance(arr, list) or not arr:
        raise ValueError("Invalid Nuxt array")
    cache = {}

    def resolve(value, seen):
        if type(value) is int:
            if value < 0:
                return None
            if value >= len(arr):
                raise ValueError("Invalid Nuxt reference")
            if value in seen:
                return None
            if value not in cache:
                cache[value] = walk(arr[value], seen | {value})
            return cache[value]
        return walk(value, seen)

    def walk(value, seen):
        if isinstance(value, dict):
            return {k: resolve(v, seen) for k, v in value.items()}
        if isinstance(value, list):
            if len(value) == 2 and value[0] in ("Reactive", "ShallowReactive", "Ref", "ShallowRef"):
                return resolve(value[1], seen)
            return [resolve(v, seen) for v in value]
        return value

    return resolve(0, set())


_category_cache = (0, [])


def _categories(session):
    global _category_cache
    if time.time() - _category_cache[0] < 86400:
        return _category_cache[1]
    from ..db import DATA_DIR
    cache_path = DATA_DIR / "aldi_categories.json"
    try:
        saved = json.loads(cache_path.read_text())
        if time.time() - saved["at"] < 86400:
            _category_cache = (saved["at"], saved["links"])
            return saved["links"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    response = session.get("https://www.aldi.com.au/")
    response.raise_for_status()
    page = PageData("__NUXT_DATA__")
    page.feed(response.text)
    links = sorted({urlsplit(href).path for href in page.links
                    if urlsplit(href).netloc in ("", "www.aldi.com.au")
                    and re.fullmatch(r"/products/[a-z0-9/-]+/k/\d+/?", urlsplit(href).path)})
    if not links:
        raise ValueError("No Aldi category navigation")
    _category_cache = (time.time(), links)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"at": _category_cache[0], "links": links}))
    except OSError:
        log.warning("Could not persist Aldi category navigation")
    return links


def _product_dicts(value):
    if isinstance(value, dict):
        if (value.get("sku") or value.get("productId") or value.get("code")) and (value.get("name") or value.get("displayName")):
            yield value
        for nested in value.values():
            yield from _product_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _product_dicts(nested)


def search_aldi(query: str, limit: int = 12):
    words = set(re.findall(r"[a-z]+", query.lower()))
    expanded = set(words)
    for word, aliases in {"pork": {"meat"}, "chops": {"pork"}, "potato": {"potatoes"},
                          "potatoes": {"potato"}, "milk": {"dairy"}, "cream": {"dairy"}}.items():
        if word in words:
            expanded.update(aliases)
    try:
        with client() as session:
            links = _categories(session)
            scored = sorted(((len(expanded & set(re.findall(r"[a-z]+", path))), path) for path in links), reverse=True)
            paths = [path for score, path in scored if score][:3]
            out = {}
            for path in paths:
                response = session.get("https://www.aldi.com.au" + path)
                response.raise_for_status()
                data = _unflatten(payload(response.text, "__NUXT_DATA__"))
                for p in _product_dicts(data):
                    name = p.get("name") or p.get("displayName")
                    if not isinstance(name, str):
                        continue
                    name_words = set(re.findall(r"[a-z]+", name.casefold()))
                    overlap = len(words & name_words)
                    if overlap < max(1, len(words) - 1):
                        continue
                    score = overlap / len(words | name_words)
                    price = p.get("price")
                    if isinstance(price, dict):
                        price = price.get("amount", price.get("value"))
                    if isinstance(price, int) and not isinstance(price, bool):
                        price = price / 100
                    item_id = str(p.get("sku") or p.get("productId") or p.get("code"))
                    out[item_id] = {"store_item_id": item_id, "name": name, "brand": p.get("brand") or "",
                                    "size": p.get("size") or "", "price": price,
                                    "was_price": p.get("wasPrice"), "unit_price": None, "unit_measure": "",
                                    "on_special": bool(p.get("wasPrice")), "_score": score}
            return sorted(out.values(), key=lambda item: item["_score"], reverse=True)[:max(1, min(limit, 36))]
    except Exception:
        log.warning("Aldi lookup failed", exc_info=True)
        return FailedResults()


_IGA_STORE_CACHE = {}
_IGA_CACHE_TTL = 604800
_IGA_MODE_ID = "11111111-1111-1111-1111-111111111111"


def _iga_store_for_location(session, location):
    key = location.strip().casefold()
    cached = _IGA_STORE_CACHE.get(key)
    if cached and cached[0] > time.time():
        return cached[1]
    response = session.get("https://nominatim.openstreetmap.org/search",
                           params={"q": location + ", Western Australia, Australia", "format": "json", "limit": 1},
                           headers={"User-Agent": "Larder grocery app"})
    response.raise_for_status()
    places = response.json()
    if not places:
        raise ValueError("Could not locate that suburb or postcode")
    lat, lon = places[0]["lat"], places[0]["lon"]
    response = session.get(f"https://www.igashop.com.au/api/storefront/near/{lat}/{lon}/100/10/stores",
                           params={"shoppingModeId": _IGA_MODE_ID})
    response.raise_for_status()
    stores = response.json().get("items") or []
    store = next((item for item in stores if item.get("retailerStoreId")), None)
    if not store:
        raise ValueError("No IGA store found near that location")
    store_id = str(store["retailerStoreId"])
    _IGA_STORE_CACHE[key] = (time.time() + _IGA_CACHE_TTL, store_id)
    return store_id


def search_iga(query: str, limit: int = 12, postcode: str = ""):
    if not postcode.strip():
        return FailedResults()
    try:
        with client() as session:
            store_id = _iga_store_for_location(session, postcode)
            response = session.get(f"https://www.igashop.com.au/api/storefront/stores/{store_id}/search",
                                   params={"q": query, "take": max(1, min(limit, 36))})
            response.raise_for_status()
            products = response.json().get("items") or []
        results = []
        for p in products:
            size_info = p.get("unitOfSize") or {}
            size = " ".join(str(size_info.get(key)).strip() for key in ("size", "abbreviation") if size_info.get(key))
            results.append({"store_item_id": str(p.get("productId") or p.get("sku") or ""),
                            "name": p.get("name", ""), "brand": p.get("brand") or "", "size": size,
                            "price": p.get("priceNumeric"), "was_price": p.get("wasPriceNumeric"),
                            "unit_price": p.get("wholePrice"),
                            "unit_measure": (p.get("unitOfPrice") or {}).get("abbreviation", ""),
                            "on_special": bool(p.get("tprPrice"))})
        return [item for item in results if item["store_item_id"] and item["name"]]
    except Exception:
        log.warning("IGA lookup failed for location %s", postcode, exc_info=True)
        return FailedResults()
