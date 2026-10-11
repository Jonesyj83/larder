import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("LARDER_DATA", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "larder.db"
UPLOAD_DIR = DATA_DIR / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    portion_size TEXT NOT NULL DEFAULT 'regular',
    date_format TEXT NOT NULL DEFAULT 'DD/MM/YYYY',
    measurement_system TEXT NOT NULL DEFAULT 'metric',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    key_hash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used TEXT
);

-- per-user provider settings for the suggestions feature
CREATE TABLE IF NOT EXISTS suggestion_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT 'openai',
    api_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    shared INTEGER NOT NULL DEFAULT 0  -- let the rest of the household use it too
);

CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    is_freezer INTEGER NOT NULL DEFAULT 0,
    sort INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS units (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    name_plural TEXT NOT NULL DEFAULT ''
);

-- generic conversions, e.g. 1 kg = 1000 g. product specific purchase
-- conversions live on the product itself (purchase_unit_id + purchase_factor)
CREATE TABLE IF NOT EXISTS unit_conversions (
    id INTEGER PRIMARY KEY,
    from_unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    to_unit_id INTEGER NOT NULL REFERENCES units(id) ON DELETE CASCADE,
    factor REAL NOT NULL,
    UNIQUE(from_unit_id, to_unit_id)
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    unit_id INTEGER NOT NULL REFERENCES units(id),
    purchase_unit_id INTEGER REFERENCES units(id),
    purchase_factor REAL NOT NULL DEFAULT 1,
    shelf_life_kind TEXT NOT NULL DEFAULT 'best_by',
    default_days INTEGER,
    min_stock REAL NOT NULL DEFAULT 0,
    picture TEXT,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS barcodes (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    code TEXT NOT NULL UNIQUE,
    -- how many stock units one scan means, defaults to the product's purchase amount
    amount REAL
);

CREATE TABLE IF NOT EXISTS stock (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    location_id INTEGER REFERENCES locations(id) ON DELETE SET NULL,
    amount REAL NOT NULL,
    expires_on TEXT,
    purchased_on TEXT NOT NULL DEFAULT (date('now')),
    price REAL,
    opened INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stock_log (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind TEXT NOT NULL, -- purchase, consume, spoiled, adjust
    amount REAL NOT NULL,
    location_id INTEGER,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shopping_list (
    id INTEGER PRIMARY KEY,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 1,
    unit_id INTEGER REFERENCES units(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    servings REAL NOT NULL DEFAULT 2,
    instructions TEXT NOT NULL DEFAULT '',
    picture TEXT,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recipe_ingredients (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    amount REAL NOT NULL,
    unit_id INTEGER REFERENCES units(id),
    note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meal_plan (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    slot TEXT NOT NULL,
    recipe_id INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
    note TEXT NOT NULL DEFAULT '',
    servings REAL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS meal_plan_day ON meal_plan(day);
"""

DEFAULT_LOCATIONS = [("Fridge", 0), ("Pantry", 0), ("Freezer", 1)]
DEFAULT_UNITS = [
    ("piece", "pieces"), ("pack", "packs"), ("g", "g"), ("kg", "kg"),
    ("ml", "ml"), ("l", "l"), ("tin", "tins"), ("bottle", "bottles"),
]


# Fluid ounces here always mean US volume; oz/lb mean mass.
BUILTIN_CONVERSIONS = [("kg", "g", 1000), ("l", "ml", 1000),
                       ("oz", "g", 28.349523125), ("lb", "g", 453.59237),
                       ("fl oz (US)", "ml", 29.5735295625)]


def connect():
    # fastapi may run the dependency and the endpoint on different threads
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
        for i, (name, freezer) in enumerate(DEFAULT_LOCATIONS):
            conn.execute("INSERT INTO locations (name, is_freezer, sort) VALUES (?, ?, ?)", (name, freezer, i))
    if conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0:
        conn.executemany("INSERT INTO units (name, name_plural) VALUES (?, ?)", DEFAULT_UNITS)
        ids = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM units")}
        conn.execute("INSERT INTO unit_conversions (from_unit_id, to_unit_id, factor) VALUES (?, ?, 1000)", (ids["kg"], ids["g"]))
        conn.execute("INSERT INTO unit_conversions (from_unit_id, to_unit_id, factor) VALUES (?, ?, 1000)", (ids["l"], ids["ml"]))
    # Additive migration: preserve users, meals and existing conversion overrides.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for name, default in (("date_format", "DD/MM/YYYY"), ("measurement_system", "metric")):
        if name not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
    conn.executemany("INSERT OR IGNORE INTO units(name, name_plural) VALUES (?, ?)",
                     DEFAULT_UNITS + [("oz", "oz"), ("lb", "lb"), ("fl oz (US)", "fl oz (US)")])
    ids = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM units")}
    for source, target, factor in BUILTIN_CONVERSIONS:
        source_id, target_id = ids[source.lower()], ids[target.lower()]
        existing = conn.execute("""SELECT 1 FROM unit_conversions
            WHERE (from_unit_id=? AND to_unit_id=?) OR (from_unit_id=? AND to_unit_id=?)""",
            (source_id, target_id, target_id, source_id)).fetchone()
        if not existing:
            conn.execute("INSERT INTO unit_conversions(from_unit_id,to_unit_id,factor) VALUES (?,?,?)",
                         (source_id, target_id, factor))
    conn.commit()
    conn.close()


def get_db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def row(cur):
    r = cur.fetchone()
    return dict(r) if r else None
