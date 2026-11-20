"""Products, inventory and the background lookup worker."""

import logging
import queue
import re
import threading
import time

from . import categories, images
from .db import Database
from .lookup import Lookup, LookupResult, image_query

log = logging.getLogger("barmanager.service")


def normalize_barcode(raw: str) -> str:
    code = re.sub(r"\s+", "", raw or "")
    if code.isdigit() and len(code) == 12:      # UPC-A -> EAN-13
        code = "0" + code
    return code


def valid_barcode(code: str) -> bool:
    return bool(code) and len(code) <= 64 and re.fullmatch(r"[0-9A-Za-z\-\.\+/]+", code) is not None


# ---- products ---------------------------------------------------------------------
def get_or_create_product(db: Database, barcode: str) -> tuple:
    now = time.time()
    with db.tx():
        row = db.one("SELECT * FROM products WHERE barcode=?", barcode)
        if row:
            return row, False
        pid = db.run("INSERT INTO products(barcode, created_at, updated_at) VALUES (?,?,?)",
                     barcode, now, now)
        return db.one("SELECT * FROM products WHERE id=?", pid), True


def product_images(db: Database, product_id: int) -> list:
    return db.all("SELECT * FROM product_images WHERE product_id=? ORDER BY position, id", product_id)


def add_image(db: Database, product_id: int, filename: str, source_url: str = "") -> int:
    pos = db.one("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM product_images WHERE product_id=?",
                 product_id)["p"]
    return db.run("INSERT INTO product_images(product_id, filename, source_url, position, created_at) "
                  "VALUES (?,?,?,?,?)", product_id, filename, source_url, pos, time.time())


def download_images(db: Database, settings, lookup: Lookup, product_id: int, urls: list[str],
                    limit: int | None = None) -> int:
    """Download new photos for a product (by default until it has max_images).

    Returns how many were added."""
    existing = product_images(db, product_id)
    have = {r["source_url"] for r in existing}
    room = limit if limit is not None else settings.max_images - len(existing)
    added = 0
    if room <= 0:
        return 0
    with lookup.client() as c:
        for url in urls:
            if added >= room:
                break
            if url in have:
                continue
            try:
                data = images.fetch(c, url, settings.max_image_bytes)
                name = images.store(data, settings.image_dir)
            except Exception as exc:  # noqa: BLE001 - one bad image must not stop the rest
                log.info("afbeelding %s overgeslagen: %s", url, exc)
                continue
            add_image(db, product_id, name, url)
            have.add(url)
            added += 1
    return added


def apply_lookup(db: Database, product_id: int, res: LookupResult) -> None:
    """Fill in what the lookup found, without overwriting what a user typed in."""
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if not p:
        return
    if res.found:
        status = "guess" if res.guessed else "found"
    else:
        status = "not_found"
    if p["lookup_status"] == "manual":
        status = "manual"
    category = p["category"]
    if not p["category_locked"] and category == categories.DEFAULT:
        category = categories.classify(res.tags, [res.name, *res.texts], res.brand, res.web_texts)
    db.run("""UPDATE products SET
                name = CASE WHEN name='' THEN ? ELSE name END,
                brand = CASE WHEN brand='' THEN ? ELSE brand END,
                volume_ml = COALESCE(volume_ml, ?),
                abv = COALESCE(abv, ?),
                description = CASE WHEN description='' THEN ? ELSE description END,
                category = ?, source = ?, lookup_status = ?, updated_at = ?
              WHERE id = ?""",
           res.name, res.brand, res.volume_ml, res.abv, res.description, category,
           ", ".join(res.sources), status, time.time(), product_id)


def reclassify(db: Database, product_id: int) -> None:
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if p and not p["category_locked"]:
        cat = categories.classify(texts=[p["name"], p["description"]], brand=p["brand"])
        db.run("UPDATE products SET category=? WHERE id=?", cat, product_id)


# ---- inventory ----------------------------------------------------------------------
def adjust(db: Database, location_id: int, product_id: int, delta: int, user_id: int | None,
           reason: str = "") -> int:
    """Change the stock of a product at a location. Never goes below 0. Returns new quantity."""
    now = time.time()
    with db.tx():
        row = db.one("SELECT quantity FROM inventory WHERE location_id=? AND product_id=?",
                     location_id, product_id)
        old = row["quantity"] if row else 0
        new = max(0, old + delta)
        if row:
            db.run("UPDATE inventory SET quantity=?, updated_at=? WHERE location_id=? AND product_id=?",
                   new, now, location_id, product_id)
        else:
            db.run("INSERT INTO inventory(location_id, product_id, quantity, updated_at) VALUES (?,?,?,?)",
                   location_id, product_id, new, now)
        if new != old:
            db.run("INSERT INTO movements(location_id, product_id, user_id, delta, reason, created_at) "
                   "VALUES (?,?,?,?,?,?)", location_id, product_id, user_id, new - old, reason, now)
    return new


def inventory_rows(db: Database, location_id: int) -> list[dict]:
    rows = db.all("""
        SELECT p.*, i.quantity, i.min_quantity, i.notes, i.updated_at AS item_updated,
               (SELECT filename FROM product_images pi WHERE pi.product_id = p.id
                ORDER BY position, id LIMIT 1) AS image
        FROM inventory i JOIN products p ON p.id = i.product_id
        WHERE i.location_id = ?""", location_id)
    return [dict(r) for r in rows]


def stock_state(item: dict) -> str:
    if item["quantity"] <= 0:
        return "op"
    if item["min_quantity"] and item["quantity"] <= item["min_quantity"]:
        return "bijna"
    return "ok"


# ---- background worker ------------------------------------------------------------------
class LookupWorker:
    """Looks up new barcodes in a background thread so scanning never has to wait."""

    def __init__(self, db: Database, settings, lookup: Lookup):
        self.db, self.settings, self.lookup = db, settings, lookup
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.settings.lookup_inline:
            return
        self._thread = threading.Thread(target=self._run, name="lookup-worker", daemon=True)
        self._thread.start()
        for row in self.db.all("SELECT id FROM products WHERE lookup_status='pending'"):
            self.enqueue(row["id"])

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, product_id: int, job: str = "lookup") -> None:
        if self.settings.lookup_inline:
            self.process(product_id, job)
        else:
            self._q.put((product_id, job))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                product_id, job = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.process(product_id, job)
            except Exception:  # noqa: BLE001
                log.exception("opzoeken van product %s mislukt", product_id)
                self.db.run("UPDATE products SET lookup_status='not_found' "
                            "WHERE id=? AND lookup_status='pending'", product_id)

    def process(self, product_id: int, job: str = "lookup") -> None:
        p = self.db.one("SELECT * FROM products WHERE id=?", product_id)
        if not p:
            return
        if job == "lookup":
            res = self.lookup.lookup(p["barcode"])
            apply_lookup(self.db, product_id, res)
            log.info("barcode %s: %s (%s)", p["barcode"], res.name or "niet gevonden",
                     ", ".join(res.sources) or "-")
            if res.image_urls:
                download_images(self.db, self.settings, self.lookup, product_id, res.image_urls)
        elif job == "images":
            urls = self.lookup.image_search(image_query(p["name"], p["brand"]))
            download_images(self.db, self.settings, self.lookup, product_id, urls)
