import logging
import os
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .auth import secret_key, user_from_request
from .db import UPLOAD_DIR, get_db, init_db
from .icons import make_png
from .routes import account, core, mealplan, products, recipes, scan, shopping, stock, suggestions

HERE = Path(__file__).resolve().parent
log = logging.getLogger("larder")

init_db()

# set this if the app is reverse-proxied under a sub-path, e.g. /larder
BASE_PATH = os.environ.get("LARDER_BASE_PATH", "").rstrip("/")

app = FastAPI(title="Larder", version="0.1", description="Household pantry tracker. "
              "Log in through the web UI, or send an API key from Settings as `X-API-Key` "
              "(or `Authorization: Bearer <key>`).")
app.add_middleware(SessionMiddleware, secret_key=secret_key(), session_cookie="larder_session",
                   max_age=60 * 60 * 24 * 60, same_site="lax")

app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.globals["base_path"] = BASE_PATH

for r in (account, core, products, stock, shopping, scan, recipes, mealplan, suggestions):
    app.include_router(r.router)


def _is_api(request):
    return request.url.path.startswith("/api/")


@app.exception_handler(StarletteHTTPException)
async def http_error(request: Request, exc: StarletteHTTPException):
    if not _is_api(request) and exc.status_code == 401:
        return RedirectResponse("/login")
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    # flatten pydantic's error list into something you can show a person
    msgs = []
    for e in exc.errors():
        field = ".".join(str(x) for x in e.get("loc", []) if x != "body")
        msgs.append(f"{field}: {e.get('msg')}" if field else e.get("msg", "invalid"))
    return JSONResponse({"detail": "; ".join(msgs)}, status_code=422)


@app.exception_handler(Exception)
async def anything_else(request: Request, exc: Exception):
    # the frontend always expects json back, never an html error page
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": f"Something went wrong: {exc}"}, status_code=500)


# --- pages ---

PAGES = {
    "/": ("stock.html", "Stock"),
    "/scan": ("scan.html", "Scan"),
    "/products": ("products.html", "Products"),
    "/shopping": ("shopping.html", "Shopping list"),
    "/manage": ("manage.html", "Locations & units"),
    "/recipes": ("recipes.html", "Recipes"),
    "/mealplan": ("mealplan.html", "Meal plan"),
    "/settings": ("settings.html", "Settings"),
}


def page(template, title):
    def view(request: Request, db=Depends(get_db)):
        user = user_from_request(request, db)
        if not user:
            return RedirectResponse(BASE_PATH + "/login")
        return templates.TemplateResponse(request, template, {"user": user, "title": title})
    return view


for path, (tpl, title) in PAGES.items():
    app.add_api_route(path, page(tpl, title), include_in_schema=False)


@app.get("/login", include_in_schema=False)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"user": None, "title": "Log in"})


@app.get("/manifest.webmanifest", include_in_schema=False)
def manifest():
    import json
    data = json.loads((HERE / "static" / "manifest.webmanifest").read_text())
    data["start_url"] = BASE_PATH + "/"
    data["scope"] = BASE_PATH + "/"
    for icon in data.get("icons", []):
        if icon["src"].startswith("/"):
            icon["src"] = BASE_PATH + icon["src"]
    return JSONResponse(data, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # served from the root so its scope covers the whole app
    return FileResponse(HERE / "static" / "sw.js", media_type="text/javascript")


_icons = {}


@app.get("/icon-{size}.png", include_in_schema=False)
def icon(size: int):
    if size not in (192, 512):
        raise StarletteHTTPException(404)
    if size not in _icons:
        _icons[size] = make_png(size)
    return Response(_icons[size], media_type="image/png", headers={"Cache-Control": "max-age=86400"})
