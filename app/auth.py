import hashlib
import hmac
import os
import secrets

from fastapi import Depends, HTTPException, Request

from .db import DATA_DIR, get_db, row


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2${salt}${h.hex()}"


def check_password(password, stored):
    try:
        _, salt, h = stored.split("$")
    except ValueError:
        return False
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return hmac.compare_digest(test.hex(), h)


_dummy = []


def waste_a_check(password):
    """Same work as a real check, so a wrong username takes as long as a wrong password."""
    if not _dummy:
        _dummy.append(hash_password(secrets.token_hex(8)))
    check_password(password, _dummy[0])


def hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


def new_api_key():
    return "lk_" + secrets.token_urlsafe(32)


def secret_key():
    # keep the session secret on disk so logins survive a restart
    path = DATA_DIR / "secret.key"
    if path.exists():
        return path.read_text().strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    # written 0600 from the start, then linked into place whole, so nobody else can
    # read it and a second worker starting at the same moment never sees half a file
    tmp = DATA_DIR / f"secret.key.{secrets.token_hex(4)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    try:
        os.link(tmp, path)
    except FileExistsError:
        key = path.read_text().strip()  # another worker got there first
    except OSError:
        os.replace(tmp, path)  # filesystem without hard links
    finally:
        tmp.unlink(missing_ok=True)
    return key


def session_check(user):
    """Changes whenever the password does, so changing it logs out every other session."""
    return hashlib.sha256(("session:" + user["password_hash"]).encode()).hexdigest()[:24]


def start_session(request, user):
    request.session.clear()
    request.session["user_id"] = user["id"]
    request.session["check"] = session_check(user)


def user_from_request(request, db):
    key = request.headers.get("x-api-key")
    if not key:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
    if key:
        k = row(db.execute("SELECT id, user_id FROM api_keys WHERE key_hash = ?", (hash_key(key),)))
        if not k:
            return None
        db.execute("UPDATE api_keys SET last_used = datetime('now') WHERE id = ?", (k["id"],))
        db.commit()
        return row(db.execute("SELECT * FROM users WHERE id = ?", (k["user_id"],)))

    uid = request.session.get("user_id")
    if uid:
        user = row(db.execute("SELECT * FROM users WHERE id = ?", (uid,)))
        if user and hmac.compare_digest(str(request.session.get("check", "")), session_check(user)):
            return user
    return None


def current_user(request: Request, db=Depends(get_db)):
    user = user_from_request(request, db)
    if not user:
        raise HTTPException(401, "Not logged in")
    return user


def admin_user(user=Depends(current_user)):
    if not user["is_admin"]:
        raise HTTPException(403, "Admin only")
    return user
