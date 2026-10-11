import secrets

import httpx

from .db import UPLOAD_DIR

TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_SIZE = 8 * 1024 * 1024


def save_picture(data, content_type):
    ext = TYPES.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise ValueError("That doesn't look like an image (jpg, png, webp or gif)")
    if len(data) > MAX_SIZE:
        raise ValueError("Picture is too big, 8 MB max")
    name = secrets.token_hex(8) + ext
    (UPLOAD_DIR / name).write_bytes(data)
    return name


def fetch_picture(url):
    if not url.startswith(("http://", "https://")):
        raise ValueError("Bad picture url")
    r = httpx.get(url, timeout=15, follow_redirects=True, headers={"User-Agent": "Larder/0.1"})
    r.raise_for_status()
    return save_picture(r.content, r.headers.get("content-type", ""))


def remove_picture(name):
    if name and "/" not in name:
        try:
            (UPLOAD_DIR / name).unlink()
        except FileNotFoundError:
            pass
