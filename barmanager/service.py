"""Products, inventory and the background lookup worker."""

import logging
import queue
import re
import threading
import time

from . import categories, images
from .db import Database
from .i18n import _
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
    now = time.time()
    attempts = p["lookup_attempts"] + 1
    if res.found:
        status, next_at = ("guess" if res.guessed else "found"), None
    elif res.transient and attempts <= len(RETRY_DELAYS):
        # A source was down or too slow: not "unknown" yet, try again soon.
        status, next_at = "pending", now + RETRY_DELAYS[attempts - 1]
    else:
        # Really unknown: the databases grow every day, so look again tomorrow (for a month).
        status, next_at = "not_found", (now + 86400 if attempts < 30 else None)
    if p["lookup_status"] == "manual" or (not res.found and p["name"]):
        status, next_at = ("manual" if p["lookup_status"] == "manual" else p["lookup_status"]), None
    category, extra = p["category"], p["extra_categories"]
    if not p["category_locked"] and category == categories.DEFAULT:
        cats = categories.classify_all(res.tags, [res.name, *res.texts], res.brand, res.web_texts)
        category, extra = cats[0], ",".join(cats[1:])
    db.run("""UPDATE products SET
                name = CASE WHEN name='' THEN ? ELSE name END,
                brand = CASE WHEN brand='' THEN ? ELSE brand END,
                volume_ml = COALESCE(volume_ml, ?),
                abv = COALESCE(abv, ?),
                description = CASE WHEN description='' THEN ? ELSE description END,
                category = ?, extra_categories = ?, source = CASE WHEN ? = '' THEN source ELSE ? END,
                lookup_status = ?, lookup_attempts = ?, next_lookup_at = ?, updated_at = ?
              WHERE id = ?""",
           res.name, res.brand, res.volume_ml, res.abv, res.description, category, extra,
           ", ".join(res.sources), ", ".join(res.sources), status, attempts, next_at, now, product_id)


RETRY_DELAYS = [120, 600, 3600, 6 * 3600]


def prefer_web_photos(db: Database, settings, lookup: Lookup, product_id: int) -> int:
    """Product only has (amateur) Open Food Facts photos: add web photos and put them first."""
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if not p or not p["name"]:
        return 0
    before = {r["id"] for r in product_images(db, product_id)}
    urls = lookup.web_image_search(image_query(p["name"], p["brand"]))
    added = download_images(db, settings, lookup, product_id, urls, limit=2)
    if added:
        rows = product_images(db, product_id)
        ordered = [r for r in rows if r["id"] not in before] + [r for r in rows if r["id"] in before]
        for pos, r in enumerate(ordered):
            db.run("UPDATE product_images SET position=? WHERE id=?", pos, r["id"])
        log.info("product %s: %d webfoto('s) toegevoegd als hoofdfoto", product_id, added)
    return added


def accessible_location_ids(db: Database, user) -> set[int] | None:
    """Locations the user may see; None means all (admin)."""
    if user["is_admin"]:
        return None
    rows = db.all("SELECT id FROM locations WHERE owner_id=? "
                  "UNION SELECT location_id FROM location_members WHERE user_id=?", user["id"], user["id"])
    return {r["id"] for r in rows}


def can_delete_product(db: Database, user, product_id: int) -> bool:
    """Admins always; others only if the product is in no location they cannot access."""
    allowed = accessible_location_ids(db, user)
    if allowed is None:
        return True
    used = {r["location_id"] for r in db.all("SELECT location_id FROM inventory WHERE product_id=?", product_id)}
    return used <= allowed


def delete_product(db: Database, settings, product_id: int) -> bool:
    """Remove a product everywhere: all locations, history and its photos."""
    photos = product_images(db, product_id)
    with db.tx():
        if not db.one("SELECT 1 FROM products WHERE id=?", product_id):
            return False
        db.run("DELETE FROM products WHERE id=?", product_id)   # cascades to inventory, movements, images
    for ph in photos:
        images.remove(ph["filename"], settings.image_dir)
    return True


def reclassify_all(db: Database) -> int:
    """Re-run the categoriser for named products still in 'Overig' (after classifier updates)."""
    rows = db.all("SELECT id FROM products WHERE category = ? AND category_locked = 0 AND name != ''",
                  categories.DEFAULT)
    for r in rows:
        reclassify(db, r["id"])
    return len(rows)


def reclassify(db: Database, product_id: int) -> None:
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if p and not p["category_locked"]:
        cats = categories.classify_all(texts=[p["name"], p["description"]], brand=p["brand"])
        db.run("UPDATE products SET category=?, extra_categories=? WHERE id=?",
               cats[0], ",".join(cats[1:]), product_id)


def product_categories(p) -> list[str]:
    """Main category first, then the extra ones."""
    extra = [c for c in (p["extra_categories"] or "").split(",") if categories.valid(c) and c != p["category"]]
    return [p["category"], *extra]


# ---- inventory ----------------------------------------------------------------------
def adjust(db: Database, location_id: int, product_id: int, delta: int, user_id: int | None,
           reason: str = "") -> int:
    """Change the stock of a product at a location. Never goes below 0. Returns new quantity.

    More bottles: the product leaves the shopping list. Fewer bottles at a location that tracks
    fill levels: the bottles count as finished (the open one first) for the statistics."""
    now = time.time()
    with db.tx():
        row = db.one("SELECT * FROM inventory WHERE location_id=? AND product_id=?", location_id, product_id)
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
        if new > old:
            db.run("DELETE FROM shopping_items WHERE product_id=? AND (location_id=? OR location_id IS NULL)",
                   product_id, location_id)
        elif new < old and row:
            _finish_bottles(db, row, old - new, new, user_id, reason, now)
    return new


DEFAULT_VOLUME_ML = 700
LEVELS = [(100, "Vol"), (75, "¾"), (50, "½"), (25, "¼"), (10, "Bijna leeg")]     # labels go through _()


def tracks_levels(db: Database, location_id: int) -> bool:
    r = db.one("SELECT track_levels FROM locations WHERE id=?", location_id)
    return bool(r and r["track_levels"])


def _volume(db: Database, product_id: int) -> float:
    r = db.one("SELECT volume_ml FROM products WHERE id=?", product_id)
    return float(r["volume_ml"] or DEFAULT_VOLUME_ML) if r else DEFAULT_VOLUME_ML


def _finish_bottles(db: Database, row, n: int, left: int, user_id, reason: str, now: float) -> None:
    lid, pid, level = row["location_id"], row["product_id"], row["open_level"]
    counted = reason == "geteld"          # a stock count is a correction, not drinking
    if level is not None and (not counted or left == 0):
        db.run("UPDATE inventory SET open_level=NULL, opened_at=NULL WHERE id=?", row["id"])
    if counted or not tracks_levels(db, lid):
        return
    pct = (level if level is not None else 100) + 100 * (n - 1)
    vol = _volume(db, pid)
    db.run("INSERT INTO level_log(location_id, product_id, user_id, event, level_before, level_after, bottles, "
           "ml, value, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", lid, pid, user_id, "leeg", level, 0, n,
           vol * pct / 100, (row["price"] or 0) * pct / 100, now)


def set_level(db: Database, location_id: int, product_id: int, level: int | None, user_id: int | None) -> str:
    """Open bottle: set how full it still is (100..1), None = still closed, 0 = finished."""
    row = db.one("SELECT * FROM inventory WHERE location_id=? AND product_id=?", location_id, product_id)
    if not row or row["quantity"] <= 0:
        return _("Er staat geen fles van dit product op deze locatie.")
    if level is not None and level <= 0:
        adjust(db, location_id, product_id, -1, user_id, "leeg")
        return _("Fles is op; voorraad één minder.")
    now = time.time()
    if level is None:
        db.run("UPDATE inventory SET open_level=NULL, opened_at=NULL WHERE id=?", row["id"])
        return _("Fles staat weer als dicht.")
    level = max(1, min(100, int(level)))
    before = row["open_level"]
    poured = max(0, (100 if before is None else before) - level)
    vol = _volume(db, product_id)
    with db.tx():
        db.run("UPDATE inventory SET open_level=?, opened_at=COALESCE(opened_at, ?) WHERE id=?",
               level, now, row["id"])
        db.run("INSERT INTO level_log(location_id, product_id, user_id, event, level_before, level_after, bottles, "
               "ml, value, created_at) VALUES (?,?,?,?,?,?,0,?,?,?)", location_id, product_id, user_id,
               "open" if before is None else "niveau", before, level, vol * poured / 100,
               (row["price"] or 0) * poured / 100, now)
    return _("Fles geopend.") if before is None else _("Inhoud bijgewerkt.")


def shelf_days(p) -> int | None:
    from .cocktails import default_shelf_days
    if p["shelf_days"] is not None:
        return p["shelf_days"] or None       # 0 = keeps forever
    return default_shelf_days(p)


def open_info(item: dict, now: float | None = None) -> dict | None:
    """For an item with an open bottle: days open, shelf life and whether it is past it."""
    if item.get("open_level") is None:
        return None
    now = now or time.time()
    days = int((now - (item.get("opened_at") or now)) // 86400)
    limit = shelf_days(item)
    state = "ok"
    if limit:
        if days > limit:
            state = "over"
        elif days >= limit * 0.8:
            state = "bijna"
    return {"level": item["open_level"], "days": days, "limit": limit, "state": state,
            "left": limit - days if limit else None}


def item_value(item: dict, track: bool) -> float:
    if not item.get("price"):
        return 0.0
    bottles = item["quantity"]
    if track and item.get("open_level") is not None and bottles > 0:
        bottles -= 1 - item["open_level"] / 100
    return round(item["price"] * max(0, bottles), 2)


def location_value(db: Database, location_id: int) -> float:
    track = tracks_levels(db, location_id)
    rows = db.all("SELECT quantity, open_level, price FROM inventory WHERE location_id=? AND price IS NOT NULL",
                  location_id)
    return round(sum(item_value(dict(r), track) for r in rows), 2)


def inventory_rows(db: Database, location_id: int) -> list[dict]:
    rows = db.all("""
        SELECT p.*, i.quantity, i.min_quantity, i.notes, i.updated_at AS item_updated,
               i.open_level, i.opened_at, i.price, i.bought_at, i.shop, i.location_id,
               (SELECT filename FROM product_images pi WHERE pi.product_id = p.id
                ORDER BY position, id LIMIT 1) AS image
        FROM inventory i JOIN products p ON p.id = i.product_id
        WHERE i.location_id = ?""", location_id)
    out = []
    for r in rows:
        d = dict(r)
        d["cats"] = product_categories(r)
        out.append(d)
    return out


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
        self._queued: set = set()
        self._last_due_check = 0.0

    def start(self) -> None:
        if self.settings.lookup_inline:
            return
        self._thread = threading.Thread(target=self._run, name="lookup-worker", daemon=True)
        self._thread.start()
        for row in self.db.all("SELECT id FROM products WHERE lookup_status='pending' "
                               "AND (next_lookup_at IS NULL OR next_lookup_at <= ?)", time.time()):
            self.enqueue(row["id"])
        if self.lookup.can_web_search:
            for row in self.db.all("""SELECT p.id FROM products p WHERE p.name != '' AND EXISTS (
                    SELECT 1 FROM product_images i WHERE i.product_id = p.id) AND NOT EXISTS (
                    SELECT 1 FROM product_images i WHERE i.product_id = p.id
                    AND i.source_url NOT LIKE '%openfoodfacts%')"""):
                self.enqueue(row["id"], "webphotos")

    def enqueue_due(self) -> int:
        """Queue products whose retry time has come (temporary failures, daily re-check)."""
        rows = self.db.all("SELECT id FROM products WHERE next_lookup_at IS NOT NULL AND next_lookup_at <= ? "
                           "AND lookup_status IN ('pending', 'not_found') ORDER BY next_lookup_at LIMIT 50",
                           time.time())
        for r in rows:
            self.enqueue(r["id"])
        return len(rows)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def enqueue(self, product_id: int, job: str = "lookup") -> None:
        if self.settings.lookup_inline:
            self.process(product_id, job)
        elif (product_id, job) not in self._queued:
            self._queued.add((product_id, job))
            self._q.put((product_id, job))

    def _run(self) -> None:
        while not self._stop.is_set():
            if time.time() - self._last_due_check > 60:
                self._last_due_check = time.time()
                try:
                    self.enqueue_due()
                except Exception:  # noqa: BLE001
                    log.exception("geplande opzoekingen ophalen mislukt")
            try:
                product_id, job = self._q.get(timeout=1)
            except queue.Empty:
                continue
            self._queued.discard((product_id, job))
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
            log.info("barcode %s: %s (%s)", p["barcode"],
                     res.name or ("bron onbereikbaar, later opnieuw" if res.transient else "niet gevonden"),
                     ", ".join(res.sources) or "-")
            if res.image_urls:
                download_images(self.db, self.settings, self.lookup, product_id, res.image_urls)
            if res.extra_image_urls:
                # Open Food Facts / UPCitemdb photos: kept as extra photos behind the web photos.
                download_images(self.db, self.settings, self.lookup, product_id, res.extra_image_urls, limit=2)
            self.wiki_classify(product_id)
        elif job == "images":
            urls = self.lookup.image_search(image_query(p["name"], p["brand"]))
            download_images(self.db, self.settings, self.lookup, product_id, urls)
        elif job == "classify":
            self.wiki_classify(product_id)
        elif job == "webphotos":
            prefer_web_photos(self.db, self.settings, self.lookup, product_id)

    def wiki_classify(self, product_id: int) -> None:
        """Name known but category still 'Overig': ask Wikipedia what kind of drink it is."""
        p = self.db.one("SELECT * FROM products WHERE id=?", product_id)
        if not p or p["category_locked"] or p["category"] != categories.DEFAULT or not p["name"]:
            return
        texts = self.lookup.wikipedia_texts(p["name"], p["brand"])
        if not texts:
            return
        cats = categories.classify_all(texts=[p["name"], p["description"]], brand=p["brand"], web_texts=texts)
        if cats[0] != categories.DEFAULT:
            self.db.run("UPDATE products SET category=?, extra_categories=?, "
                        "source = CASE WHEN source='' THEN 'Wikipedia' ELSE source || ', Wikipedia' END "
                        "WHERE id=? AND category_locked=0", cats[0], ",".join(cats[1:]), product_id)
            log.info("product %s via Wikipedia ingedeeld als %s", product_id, cats[0])
