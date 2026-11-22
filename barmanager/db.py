import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS locations(
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at REAL NOT NULL,
    track_levels INTEGER NOT NULL DEFAULT 0,     -- keep track of open bottles / fill levels
    menu_token TEXT                              -- public bar menu link, NULL = off
);
CREATE TABLE IF NOT EXISTS location_members(
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY(location_id, user_id)
);
CREATE TABLE IF NOT EXISTS products(
    id INTEGER PRIMARY KEY,
    barcode TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'overig',
    category_locked INTEGER NOT NULL DEFAULT 0,
    extra_categories TEXT NOT NULL DEFAULT '',   -- comma separated, e.g. 'likeur'
    volume_ml INTEGER,
    abv REAL,
    description TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    -- pending | found | guess | not_found | manual
    lookup_status TEXT NOT NULL DEFAULT 'pending',
    lookup_attempts INTEGER NOT NULL DEFAULT 0,
    next_lookup_at REAL,                         -- when to try the internet again
    country TEXT NOT NULL DEFAULT '',
    ai_for TEXT NOT NULL DEFAULT '',             -- name the AI last filled this product in for
    ai_attempts INTEGER NOT NULL DEFAULT 0,
    ai_fields TEXT NOT NULL DEFAULT '',          -- fields the AI filled, e.g. 'brand,description'
    shelf_days INTEGER,                          -- keeps N days after opening (NULL = default by type)
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS product_images(
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_images_product ON product_images(product_id, position);
CREATE TABLE IF NOT EXISTS inventory(
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    quantity INTEGER NOT NULL DEFAULT 0,
    min_quantity INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT '',
    updated_at REAL NOT NULL,
    open_level INTEGER,                          -- % left in the open bottle, NULL = none open
    opened_at REAL,
    price REAL,                                  -- purchase price per bottle
    bought_at TEXT NOT NULL DEFAULT '',
    shop TEXT NOT NULL DEFAULT '',
    UNIQUE(location_id, product_id)
);
CREATE TABLE IF NOT EXISTS movements(
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_movements_location ON movements(location_id, created_at);
-- Open bottles: how much was poured, for statistics (only for locations that track fill levels).
CREATE TABLE IF NOT EXISTS level_log(
    id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event TEXT NOT NULL,                         -- open | niveau | leeg
    level_before INTEGER,
    level_after INTEGER,
    bottles INTEGER NOT NULL DEFAULT 0,          -- bottles finished by this event
    ml REAL NOT NULL DEFAULT 0,                  -- millilitres poured
    value REAL NOT NULL DEFAULT 0,               -- euros poured (when a price is known)
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_level_log ON level_log(location_id, created_at);
-- Shopping list: manual entries, free text, and auto entries the user hid ('verborgen').
CREATE TABLE IF NOT EXISTS shopping_items(
    id INTEGER PRIMARY KEY,
    location_id INTEGER REFERENCES locations(id) ON DELETE CASCADE,
    product_id INTEGER REFERENCES products(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'handmatig',     -- handmatig | verborgen
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cocktails(
    id TEXT PRIMARY KEY,                         -- TheCocktailDB idDrink
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    alcoholic TEXT NOT NULL DEFAULT '',
    glass TEXT NOT NULL DEFAULT '',
    iba TEXT NOT NULL DEFAULT '',
    instructions TEXT NOT NULL DEFAULT '',
    thumb_url TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL DEFAULT '',              -- local file under images/cocktails/
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cocktail_ingredients(
    cocktail_id TEXT NOT NULL REFERENCES cocktails(id) ON DELETE CASCADE,
    pos INTEGER NOT NULL,
    name TEXT NOT NULL,
    measure TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(cocktail_id, pos)
);
-- User choices per ingredient: 'always' in the house, or linked to specific products.
CREATE TABLE IF NOT EXISTS ingredient_prefs(
    ingredient TEXT PRIMARY KEY,                 -- normalised ingredient name
    always INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ingredient_links(
    ingredient TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    PRIMARY KEY(ingredient, product_id)
);
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    """Small thread-safe wrapper around one SQLite connection (autocommit)."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(products)")}
        if "extra_categories" not in cols:
            self._conn.execute("ALTER TABLE products ADD COLUMN extra_categories TEXT NOT NULL DEFAULT ''")
        if "next_lookup_at" not in cols:
            self._conn.execute("ALTER TABLE products ADD COLUMN lookup_attempts INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("ALTER TABLE products ADD COLUMN next_lookup_at REAL")
            # Unknown products from before this version: look them up again soon.
            self._conn.execute("UPDATE products SET next_lookup_at = 0 "
                               "WHERE lookup_status = 'not_found' AND name = ''")
        if "country" not in cols:
            self._conn.execute("ALTER TABLE products ADD COLUMN country TEXT NOT NULL DEFAULT ''")
        if "ai_for" not in cols:
            self._conn.execute("ALTER TABLE products ADD COLUMN ai_for TEXT NOT NULL DEFAULT ''")
            self._conn.execute("ALTER TABLE products ADD COLUMN ai_attempts INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("ALTER TABLE products ADD COLUMN ai_fields TEXT NOT NULL DEFAULT ''")
        if "shelf_days" not in cols:
            self._conn.execute("ALTER TABLE products ADD COLUMN shelf_days INTEGER")
        add = {
            "locations": [("track_levels", "INTEGER NOT NULL DEFAULT 0"), ("menu_token", "TEXT")],
            "inventory": [("open_level", "INTEGER"), ("opened_at", "REAL"), ("price", "REAL"),
                          ("bought_at", "TEXT NOT NULL DEFAULT ''"), ("shop", "TEXT NOT NULL DEFAULT ''")],
        }
        for table, columns in add.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def one(self, sql: str, *args):
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args) -> list:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def run(self, sql: str, *args) -> int:
        with self._lock:
            return self._conn.execute(sql, args).lastrowid

    @contextmanager
    def tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()
