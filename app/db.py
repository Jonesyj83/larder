import os
import sqlite3
from pathlib import Path

DATA_DIR = Path(os.environ.get("LARDER_DATA", Path(__file__).resolve().parent.parent / "data"))
DB_PATH = DATA_DIR / "larder.db"
UPLOAD_DIR = DATA_DIR / "uploads"

SCHEMA = """

CREATE TABLE IF NOT EXISTS household_notes (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('like','dislike','maybe','condition','habit','buying_pattern','recipe_note','cost_note')),
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'chat',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS household_notes_fts USING fts5(content, content='household_notes', content_rowid='id');

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    portion_size TEXT NOT NULL DEFAULT 'regular',
    date_format TEXT NOT NULL DEFAULT 'DD/MM/YYYY',
    measurement_system TEXT NOT NULL DEFAULT 'metric',
    show_prices INTEGER NOT NULL DEFAULT 0,
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

-- Retained legacy settings.
CREATE TABLE IF NOT EXISTS suggestion_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    reuse_account INTEGER NOT NULL DEFAULT 0,
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


CREATE TABLE IF NOT EXISTS store_settings (
    store TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    postcode TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS product_store_links (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    store TEXT NOT NULL,
    store_item_id TEXT NOT NULL,
    store_name TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    matched_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product_id, store)
);
CREATE TABLE IF NOT EXISTS store_price_checks (
    id INTEGER PRIMARY KEY,
    link_id INTEGER NOT NULL REFERENCES product_store_links(id) ON DELETE CASCADE,
    price REAL,
    was_price REAL,
    unit_price REAL,
    unit_measure TEXT NOT NULL DEFAULT '',
    on_special INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS store_price_checks_latest ON store_price_checks(link_id, checked_at DESC, id DESC);

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

CREATE TABLE IF NOT EXISTS receipts (
    id INTEGER PRIMARY KEY,
    store TEXT NOT NULL DEFAULT '',
    purchased_on TEXT NOT NULL DEFAULT (date('now')),
    total REAL,
    note TEXT NOT NULL DEFAULT '',
    added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS stock_log (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    kind TEXT NOT NULL, -- purchase, consume, spoiled, adjust
    amount REAL NOT NULL,
    location_id INTEGER,
    price REAL,
    receipt_id INTEGER REFERENCES receipts(id) ON DELETE SET NULL,
    at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shopping_lists (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'plain',
    name TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shopping_list (
    id INTEGER PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES shopping_lists(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 1,
    unit_id INTEGER REFERENCES units(id) ON DELETE SET NULL,
    note TEXT NOT NULL DEFAULT '',
    done INTEGER NOT NULL DEFAULT 0,
    added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shopping_sources (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES shopping_list(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL,
    added_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    servings REAL NOT NULL DEFAULT 2,
    instructions TEXT NOT NULL DEFAULT '',
    picture TEXT,
    rating INTEGER NOT NULL DEFAULT 0 CHECK (rating BETWEEN 0 AND 5),
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

CREATE TABLE IF NOT EXISTS planned_cooks (
    id INTEGER PRIMARY KEY,
    recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
    servings REAL NOT NULL,
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meal_plan (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    slot TEXT NOT NULL,
    recipe_id INTEGER REFERENCES recipes(id) ON DELETE CASCADE,
    simple_meal INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    servings REAL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    batch_id INTEGER REFERENCES cooked_batches(id) ON DELETE SET NULL,
    cook_event_id INTEGER REFERENCES planned_cooks(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS meal_plan_day ON meal_plan(day);
CREATE TABLE IF NOT EXISTS cooked_batches (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
    recipe_id INTEGER REFERENCES recipes(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE RESTRICT,
    portions_total REAL NOT NULL CHECK (portions_total > 0),
    portions_left REAL NOT NULL,
    cooked_at TEXT NOT NULL DEFAULT (date('now')),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS planning_requests (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    response TEXT NOT NULL,
    PRIMARY KEY(user_id, kind, request_id)
);

-- Retained legacy history.
CREATE TABLE IF NOT EXISTS recent_suggestions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Shared household diet and taste preferences.
CREATE TABLE IF NOT EXISTS household_preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    diet TEXT NOT NULL DEFAULT '[]',      -- json list of tags, e.g. ["vegetarian","no seafood"]
    dislikes TEXT NOT NULL DEFAULT '[]',  -- json list of free-typed ingredient/dish tags to avoid
    other TEXT NOT NULL DEFAULT ''        -- anything that doesn't fit a tag
);
"""

DEFAULT_STORES = ("woolworths", "coles", "aldi", "iga")

DEFAULT_LOCATIONS = [("Fridge", 0), ("Pantry", 0), ("Freezer", 1), ("Work", 0)]
DEFAULT_UNITS = [
    ("piece", "pieces"), ("slice", "slices"), ("pack", "packs"), ("g", "g"), ("kg", "kg"),
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
    # all of it under one write lock: two workers starting at once shouldn't both
    # try to add the same column or seed the same rows
    conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
    if "size" not in {r["name"] for r in conn.execute("PRAGMA table_info(product_store_links)")}:
        conn.execute("ALTER TABLE product_store_links ADD COLUMN size TEXT NOT NULL DEFAULT ''")
    if "list_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(shopping_list)")}:
        conn.execute("ALTER TABLE shopping_list ADD COLUMN list_id INTEGER REFERENCES shopping_lists(id) ON DELETE CASCADE")
    needs_backfill = conn.execute("SELECT 1 FROM shopping_list WHERE list_id IS NULL").fetchone()
    if needs_backfill or not conn.execute("SELECT 1 FROM shopping_lists").fetchone():
        default = conn.execute("SELECT id FROM shopping_lists WHERE name='Shopping' ORDER BY id LIMIT 1").fetchone()
        default_id = default["id"] if default else conn.execute(
            "INSERT INTO shopping_lists(name) VALUES ('Shopping')").lastrowid
        conn.execute("UPDATE shopping_list SET list_id=? WHERE list_id IS NULL", (default_id,))
    if "kind" not in {r["name"] for r in conn.execute("PRAGMA table_info(shopping_lists)")}:
        conn.execute("ALTER TABLE shopping_lists ADD COLUMN kind TEXT NOT NULL DEFAULT 'plain'")
    if not conn.execute("SELECT 1 FROM shopping_lists WHERE kind='order'").fetchone():
        chosen = conn.execute("""SELECT l.id FROM shopping_lists l LEFT JOIN shopping_list i ON i.list_id=l.id
            GROUP BY l.id ORDER BY COUNT(i.id) DESC, l.id LIMIT 1""").fetchone()
        conn.execute("UPDATE shopping_lists SET kind='order' WHERE id=?", (chosen['id'],))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS shopping_lists_single_order ON shopping_lists(kind) WHERE kind='order'")
    if conn.execute("SELECT 1 FROM shopping_list WHERE list_id IS NULL").fetchone():
        raise RuntimeError("Shopping list backfill incomplete")
    # Older tables retain their shape; guards enforce the same non-null rule.
    for event in ("INSERT", "UPDATE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS shopping_list_list_id_{event.lower()}
            BEFORE {event} ON shopping_list WHEN NEW.list_id IS NULL
            BEGIN SELECT RAISE(ABORT, 'shopping_list.list_id must not be null'); END""")
    conn.execute("CREATE INDEX IF NOT EXISTS shopping_list_by_list ON shopping_list(list_id, done)")
    conn.executemany("INSERT OR IGNORE INTO store_settings(store) VALUES (?)",
                     [(store,) for store in DEFAULT_STORES])
    triggers = {
        "household_notes_ai": "AFTER INSERT ON household_notes BEGIN INSERT INTO household_notes_fts(rowid,content) VALUES(new.id,new.content); END",
        "household_notes_ad": "AFTER DELETE ON household_notes BEGIN INSERT INTO household_notes_fts(household_notes_fts,rowid,content) VALUES('delete',old.id,old.content); END",
        "household_notes_au": "AFTER UPDATE ON household_notes BEGIN INSERT INTO household_notes_fts(household_notes_fts,rowid,content) VALUES('delete',old.id,old.content); INSERT INTO household_notes_fts(rowid,content) VALUES(new.id,new.content); END",
    }
    rebuild = False
    for name, body in triggers.items():
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone():
            conn.execute(f"CREATE TRIGGER {name} {body}")
            rebuild = True
    if rebuild:
        conn.execute("INSERT INTO household_notes_fts(household_notes_fts) VALUES('rebuild')")
    if conn.execute("SELECT COUNT(*) FROM locations").fetchone()[0] == 0:
        for i, (name, freezer) in enumerate(DEFAULT_LOCATIONS):
            conn.execute("INSERT INTO locations (name, is_freezer, sort) VALUES (?, ?, ?)", (name, freezer, i))
    if not conn.execute("SELECT 1 FROM locations WHERE name='Work' COLLATE NOCASE").fetchone():
        conn.execute("INSERT OR IGNORE INTO locations(name,is_freezer,sort) VALUES('Work',0,3)")
    if "price" not in {r["name"] for r in conn.execute("PRAGMA table_info(stock_log)")}:
        conn.execute("ALTER TABLE stock_log ADD COLUMN price REAL")
    if "receipt_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(stock_log)")}:
        conn.execute("ALTER TABLE stock_log ADD COLUMN receipt_id INTEGER REFERENCES receipts(id) ON DELETE SET NULL")
    if conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0:
        conn.executemany("INSERT INTO units (name, name_plural) VALUES (?, ?)", DEFAULT_UNITS)
        ids = {r["name"].lower(): r["id"] for r in conn.execute("SELECT id, name FROM units")}
        conn.execute("INSERT INTO unit_conversions (from_unit_id, to_unit_id, factor) VALUES (?, ?, 1000)", (ids["kg"], ids["g"]))
        conn.execute("INSERT INTO unit_conversions (from_unit_id, to_unit_id, factor) VALUES (?, ?, 1000)", (ids["l"], ids["ml"]))
    if "simple_meal" not in {r["name"] for r in conn.execute("PRAGMA table_info(meal_plan)")}:
        conn.execute("ALTER TABLE meal_plan ADD COLUMN simple_meal INTEGER NOT NULL DEFAULT 0")
    # Additive migration: preserve users, meals and existing conversion overrides.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    for name, default in (("date_format", "DD/MM/YYYY"), ("measurement_system", "metric")):
        if name not in columns:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
    if "show_prices" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN show_prices INTEGER NOT NULL DEFAULT 0")
    if "rating" not in {r["name"] for r in conn.execute("PRAGMA table_info(recipes)")}:
        conn.execute("ALTER TABLE recipes ADD COLUMN rating INTEGER NOT NULL DEFAULT 0")
    if "batch_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(meal_plan)")}:
        conn.execute("ALTER TABLE meal_plan ADD COLUMN batch_id INTEGER REFERENCES cooked_batches(id) ON DELETE SET NULL")
    if "cook_event_id" not in {r["name"] for r in conn.execute("PRAGMA table_info(meal_plan)")}:
        conn.execute("ALTER TABLE meal_plan ADD COLUMN cook_event_id INTEGER REFERENCES planned_cooks(id) ON DELETE SET NULL")
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


def begin(db):
    """Take the write lock before reading, so a read-then-write can't interleave
    with the same thing happening in another tab."""
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def row(cur):
    r = cur.fetchone()
    return dict(r) if r else None
