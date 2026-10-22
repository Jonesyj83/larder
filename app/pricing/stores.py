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


def coles_browse_session():
    """One persistent, cookied session for a whole crawl run -- a real
    browser picks up Incapsula's visitor cookie on the first page and
    carries it for the rest of the visit; a fresh client per request never
    gets that and reads as a new first-time visitor every single time."""
    session = client()
    try:
        session.get("https://www.coles.com.au/")
    except Exception:
        pass
    return session


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
    for attempt in range(2):
        try:
            with client() as session:
                response = session.get("https://www.coles.com.au/search/products", params={"q": query})
                response.raise_for_status()
            data = payload(response.text, "__NEXT_DATA__")
            search = data.get("props", {}).get("pageProps", {}).get("searchResults")
            if not isinstance(search, dict) or "results" not in search:
                raise ValueError("Missing Coles search results")
            break
        except (ValueError, httpx.HTTPError) as exc:
            if attempt:
                raise ValueError("Coles search unavailable after retry; the store may be blocking requests") from exc
            time.sleep(0.5)
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


def _coles_products_from_next_data(raw_json, limit):
    data = json.loads(raw_json)
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


# The categories listed on coles.com.au/browse, excluding tobacco (we're
# not pricing that) -- walked page by page to build the full catalogue.
CATALOGUE_CATEGORIES = (
    "fruit-vegetables", "dairy-eggs-fridge", "pantry", "meat-seafood", "bakery",
    "frozen", "drinks", "chips-chocolates-snacks", "cleaning-laundry",
    "health-beauty", "health-dietary", "deli", "pet", "baby",
    "international-foods", "home-garden", "liquorland", "big-pack-value",
)


def browse_coles_category(category: str, page: int = 1, session=None):
    """One page of a Coles browse-category listing. Unlike search_coles(),
    these pages aren't behind the bot wall at all -- just plain product
    listings, same as any shopper scrolling the category in a browser.

    Pass a shared session (from coles_browse_session()) when fetching many
    pages in a row -- a fresh client with no cookies on every single
    request looks like a different first-time visitor every time, which
    trips the rate limiter far sooner than a normal browsing session would."""
    owns_session = session is None
    session = session or client()
    try:
        response = session.get(f"https://www.coles.com.au/browse/{category}", params={"page": page})
        response.raise_for_status()
    finally:
        if owns_session:
            session.close()
    data = payload(response.text, "__NEXT_DATA__")
    search = data.get("props", {}).get("pageProps", {}).get("searchResults")
    if not isinstance(search, dict) or "results" not in search:
        raise ValueError("Missing Coles browse results")
    out = []
    for p in search.get("results") or []:
        if p.get("_type") != "PRODUCT" or not p.get("id"):
            continue
        pricing = p.get("pricing") or {}
        unit = pricing.get("unit") or {}
        out.append({"coles_id": p["id"], "name": p["name"], "brand": p.get("brand", ""),
                    "size": p.get("size", ""), "price": pricing.get("now"), "was_price": pricing.get("was") or None,
                    "unit_price": unit.get("price"),
                    "unit_measure": f"{unit.get('ofMeasureQuantity') or ''}{unit.get('ofMeasureType') or ''}"})
    return out, search.get("noOfResults") or 0


def search_coles_browser(query: str, limit: int = 12):
    """Coles sits behind a bot-detection challenge that a plain HTTP request
    can't get past (even a real browser gets served a blank decoy page
    unless it's patched to hide the usual automation fingerprints). This is
    the fallback path for when search_coles() above comes back empty-handed:
    a real, stealth-patched browser tab, used only when it's actually
    needed since it's much slower than a plain request."""
    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth
    from urllib.parse import quote
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=HEADERS["User-Agent"])
                page = context.new_page()
                Stealth().apply_stealth_sync(page)
                page.goto(f"https://www.coles.com.au/search/products?q={quote(query)}",
                           wait_until="domcontentloaded", timeout=20000)
                # The challenge resolves itself client-side within a couple of
                # seconds once a real browser passes it; there's nothing to
                # poll for before then, so a flat wait is simplest.
                page.wait_for_timeout(3000)
                raw = page.evaluate("document.getElementById('__NEXT_DATA__')?.textContent")
                if not raw:
                    raise ValueError("No __NEXT_DATA__ on the page")
            finally:
                browser.close()
    except Exception as exc:
        raise ValueError("Coles search unavailable (bot check didn't clear)") from exc
    return _coles_products_from_next_data(raw, limit)


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
                          "potatoes": {"potato"}, "milk": {"dairy"}, "cream": {"dairy"},
                          "onion": {"onions", "fruit", "vegetables"}, "onions": {"onion", "fruit", "vegetables"},
                          "garlic": {"vegetables", "fruit"}, "capsicum": {"vegetables", "pepper"},
                          "pepper": {"vegetables", "capsicum"}, "carrot": {"carrots", "vegetables"},
                          "carrots": {"carrot", "vegetables"}, "tomato": {"tomatoes", "vegetables"},
                          "tomatoes": {"tomato", "vegetables"}, "chicken": {"meat", "poultry"},
                          "beef": {"meat"}, "mince": {"meat"}, "cheese": {"dairy", "fridge"},
                          "bread": {"bakery"}, "butter": {"dairy", "fridge"},
                          "egg": {"eggs", "dairy"}, "eggs": {"egg", "dairy"}}.items():
        if word in words:
            expanded.update(aliases)
    try:
        with client() as session:
            links = _categories(session)
            if words & {"onion", "onions", "garlic", "capsicum", "pepper", "carrot", "carrots", "tomato", "tomatoes", "potato", "potatoes"}:
                expanded.update({"fresh", "vegetables"})
            scored = sorted(((len(expanded & set(re.findall(r"[a-z]+", path))), path) for path in links), reverse=True)
            paths = [path for score, path in scored if score][:3]
            if not paths:
                # Broad departments cover more ground than eight unrelated leaf pages.
                broad = [path for _, path in scored if len(path.strip("/").split("/")) == 4]
                preferred = ("fruits-vegetables", "pantry", "dairy-eggs-fridge", "meat-seafood", "bakery", "cleaning-household")
                broad.sort(key=lambda path: next((i for i, word in enumerate(preferred) if word in path), len(preferred)))
                paths = (broad + [path for _, path in scored if path not in broad])[:8]
            out = {}
            for path in paths:
                response = session.get("https://www.aldi.com.au" + path)
                response.raise_for_status()
                data = _unflatten(payload(response.text, "__NUXT_DATA__"))
                for p in _product_dicts(data):
                    name = p.get("name") or p.get("displayName")
                    if not isinstance(name, str):
                        continue
                    name_words = set(re.findall(r"[a-z]+", re.sub(r"\bper\s+kg\b", "", name.casefold())))
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
                                    "was_price": p.get("wasPrice"), "unit_price": price if re.search(r"\bper\s+kg\b", name, re.I) else None,
                                    "unit_measure": "kg" if re.search(r"\bper\s+kg\b", name, re.I) else "",
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
        raise ValueError("IGA matching requires an area or postcode in Settings")
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
