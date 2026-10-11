import sqlite3
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import (admin_user, check_password, current_user, hash_key, hash_password,
                    new_api_key)
from ..db import get_db, row, rows

router = APIRouter(prefix="/api", tags=["account"])

PORTIONS = Literal["small", "regular", "large"]


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str
    new_password: str


class PrefsIn(BaseModel):
    portion_size: Optional[PORTIONS] = None
    date_format: Optional[Literal["DD/MM/YYYY", "MM/DD/YYYY", "YYYY-MM-DD"]] = None
    measurement_system: Optional[Literal["metric", "imperial"]] = None


class KeyIn(BaseModel):
    name: str = ""


class NewUserIn(BaseModel):
    username: str
    password: str
    is_admin: bool = False
    portion_size: PORTIONS = "regular"


class UserEdit(BaseModel):
    is_admin: Optional[bool] = None
    password: Optional[str] = None


def public(user):
    return {k: user[k] for k in ("id", "username", "is_admin", "portion_size", "date_format", "measurement_system", "created_at")}


def _check_new(username, password):
    if not username.strip():
        raise HTTPException(400, "Username can't be empty")
    if len(password) < 6:
        raise HTTPException(400, "Password needs at least 6 characters")


@router.get("/auth/status")
def status(db=Depends(get_db)):
    """Lets the login page know whether this is a fresh install."""
    count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {"needs_setup": count == 0}


@router.post("/auth/setup")
def setup(data: LoginIn, request: Request, db=Depends(get_db)):
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        raise HTTPException(400, "Already set up")
    _check_new(data.username, data.password)
    cur = db.execute("INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, 1)",
                     (data.username.strip(), hash_password(data.password)))
    db.commit()
    request.session["user_id"] = cur.lastrowid
    return public(row(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,))))


@router.post("/auth/login")
def login(data: LoginIn, request: Request, db=Depends(get_db)):
    user = row(db.execute("SELECT * FROM users WHERE username = ?", (data.username.strip(),)))
    if not user or not check_password(data.password, user["password_hash"]):
        raise HTTPException(401, "Wrong username or password")
    request.session.clear()
    request.session["user_id"] = user["id"]
    return public(user)


@router.post("/auth/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
def me(user=Depends(current_user)):
    return public(user)


@router.put("/me")
def update_me(data: PrefsIn, user=Depends(current_user), db=Depends(get_db)):
    for field in ("portion_size", "date_format", "measurement_system"):
        value = getattr(data, field)
        if value is not None:
            db.execute(f"UPDATE users SET {field} = ? WHERE id = ?", (value, user["id"]))
    db.commit()
    return public(row(db.execute("SELECT * FROM users WHERE id = ?", (user["id"],))))


@router.post("/me/password")
def change_password(data: PasswordIn, user=Depends(current_user), db=Depends(get_db)):
    if not check_password(data.old_password, user["password_hash"]):
        raise HTTPException(400, "Current password is wrong")
    _check_new(user["username"], data.new_password)
    db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(data.new_password), user["id"]))
    db.commit()
    return {"ok": True}


@router.get("/me/keys")
def list_keys(user=Depends(current_user), db=Depends(get_db)):
    return rows(db.execute("SELECT id, name, prefix, created_at, last_used FROM api_keys WHERE user_id = ? ORDER BY id",
                           (user["id"],)))


@router.post("/me/keys", status_code=201)
def create_key(data: KeyIn, user=Depends(current_user), db=Depends(get_db)):
    key = new_api_key()
    cur = db.execute("INSERT INTO api_keys (user_id, name, key_hash, prefix) VALUES (?, ?, ?, ?)",
                     (user["id"], data.name.strip(), hash_key(key), key[:8]))
    db.commit()
    # only time the full key is ever shown
    return {"id": cur.lastrowid, "name": data.name.strip(), "key": key}


@router.delete("/me/keys/{key_id}")
def delete_key(key_id: int, user=Depends(current_user), db=Depends(get_db)):
    db.execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user["id"]))
    db.commit()
    return {"ok": True}


@router.get("/users")
def list_users(user=Depends(admin_user), db=Depends(get_db)):
    return [public(u) for u in rows(db.execute("SELECT * FROM users ORDER BY username"))]


@router.post("/users", status_code=201)
def add_user(data: NewUserIn, user=Depends(admin_user), db=Depends(get_db)):
    _check_new(data.username, data.password)
    try:
        cur = db.execute("INSERT INTO users (username, password_hash, is_admin, portion_size) VALUES (?, ?, ?, ?)",
                         (data.username.strip(), hash_password(data.password), int(data.is_admin), data.portion_size))
    except sqlite3.IntegrityError:
        raise HTTPException(400, "That username is taken")
    db.commit()
    return public(row(db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,))))


@router.put("/users/{user_id}")
def edit_user(user_id: int, data: UserEdit, user=Depends(admin_user), db=Depends(get_db)):
    target = row(db.execute("SELECT * FROM users WHERE id = ?", (user_id,)))
    if not target:
        raise HTTPException(404, "No such user")
    if data.is_admin is not None:
        if user_id == user["id"] and not data.is_admin:
            raise HTTPException(400, "You can't take admin away from yourself")
        db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(data.is_admin), user_id))
    if data.password:
        _check_new(target["username"], data.password)
        db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(data.password), user_id))
    db.commit()
    return public(row(db.execute("SELECT * FROM users WHERE id = ?", (user_id,))))


@router.delete("/users/{user_id}")
def delete_user(user_id: int, user=Depends(admin_user), db=Depends(get_db)):
    if user_id == user["id"]:
        raise HTTPException(400, "You can't delete yourself")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return {"ok": True}
