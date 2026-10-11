import secrets
from urllib.parse import urljoin, urlsplit

import httpx

from .db import UPLOAD_DIR
from .netcheck import check_host

TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
MAX_SIZE = 8 * 1024 * 1024

# tests swap this for an httpx.MockTransport
_transport = None


def _sniff(data):
    """What the bytes actually are, whatever the upload claimed."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def save_picture(data, content_type):
    ext = TYPES.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise ValueError("That doesn't look like an image (jpg, png, webp or gif)")
    if len(data) > MAX_SIZE:
        raise ValueError("Picture is too big, 8 MB max")
    # name the file after what's really in it, so it's always served as an image
    ext = _sniff(data)
    if not ext:
        raise ValueError("That doesn't look like an image (jpg, png, webp or gif)")
    name = secrets.token_hex(8) + ext
    (UPLOAD_DIR / name).write_bytes(data)
    return name


def _check_url(url):
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ValueError("Bad picture url")
    check_host(u.hostname, u.port or (443 if u.scheme == "https" else 80))


def fetch_picture(url):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise ValueError("Bad picture url")
    # follow redirects by hand so every hop gets the same address check
    with httpx.Client(transport=_transport, timeout=15, follow_redirects=False,
                      headers={"User-Agent": "Larder/0.1"}) as client:
        for _ in range(4):
            _check_url(url)
            with client.stream("GET", url) as r:
                if r.is_redirect and r.headers.get("location"):
                    url = urljoin(url, r.headers["location"])
                    continue
                r.raise_for_status()
                data = bytearray()
                for chunk in r.iter_bytes():
                    data += chunk
                    if len(data) > MAX_SIZE:
                        raise ValueError("Picture is too big, 8 MB max")
                return save_picture(bytes(data), r.headers.get("content-type", ""))
    raise ValueError("Too many redirects")


def remove_picture(name):
    if name and "/" not in name and "\\" not in name and name not in (".", ".."):
        try:
            (UPLOAD_DIR / name).unlink()
        except (FileNotFoundError, IsADirectoryError):
            pass
