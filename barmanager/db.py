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
    created_at REAL NOT NULL
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
