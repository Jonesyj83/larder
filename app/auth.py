import hashlib
import hmac
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
    path.write_text(key)
    path.chmod(0o600)
    return key


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
        return row(db.execute("SELECT * FROM users WHERE id = ?", (uid,)))
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
