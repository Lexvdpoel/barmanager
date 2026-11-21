"""Optional local language model (Ollama, e.g. Qwen 2.5 3B) that fills in missing product details.

What it does: for every bottle with a known name it adds what is still empty - brand, country,
a short English description and, when the categoriser gave up ('Overig'), the category. Exact
numbers such as the alcohol percentage are left alone: a 3B model gets those wrong too often. It never overwrites what a user or a product database filled in, and every
field it fills is remembered so the product page can mark it as "AI".

What it does not do: invent a product for an unknown barcode. A small model does not know
barcodes; asked anyway it makes up a plausible bottle, which is worse than "unknown".
"""

import json
import logging
import threading
import time

import httpx

from . import categories
from .db import Database

log = logging.getLogger("barmanager.ai")

MAX_ATTEMPTS = 3
FIELDS_LABEL = {"brand": "merk", "country": "land", "description": "omschrijving", "category": "categorie"}

SYSTEM = (
    "You help a bar keep its inventory. You get one bottle of drink and fill in missing details. "
    "Only state facts you are sure of; leave a field empty when unsure. Never invent anything.\n"
    "- brand: the brand name exactly as it appears in the product name (e.g. 'Jim Beam'), nothing added.\n"
    "- country: country of origin in English (e.g. 'Scotland', 'United States').\n"
    "- category: what kind of drink it is.\n"
    "- description: ONE or TWO short sentences in English, max 200 characters: what kind of drink, "
    "from where, main taste. No marketing, no repetition.\n"
    "- sure: true only if you really know this product or brand."
)


CATEGORY_EN = {
    "vodka": "vodka", "jenever": "Dutch genever", "likeur": "liqueur", "bitter": "herbal bitter / amaro",
    "aperitief": "aperitif, vermouth", "port": "port, sherry, fortified wine", "wijn": "wine",
    "bubbels": "sparkling wine, champagne", "bier": "beer", "mixer": "soft drink, tonic, mixer",
    "siroop": "syrup", "sap": "juice", "additief": "cocktail bitters, garnish", "overig": "other",
}


def _schema() -> dict:
    keys = list(categories.KEYS)
    return {
        "type": "object",
        "properties": {
            "brand": {"type": "string", "maxLength": 40},
            "country": {"type": "string", "maxLength": 30},
            "category": {"type": "string", "enum": keys},
            "also": {"type": "array", "items": {"type": "string", "enum": keys}, "maxItems": 2},
            "description": {"type": "string", "maxLength": 220},
            "sure": {"type": "boolean"},
        },
        "required": ["brand", "country", "category", "also", "description", "sure"],
    }


class Enricher:
    def __init__(self, settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport
        self._model_ready = False

    @property
    def enabled(self) -> bool:
        return bool(self.settings.ollama_url)

    @property
    def model(self) -> str:
        return self.settings.ollama_model

    def client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(base_url=self.settings.ollama_url.rstrip("/"), transport=self.transport,
                            timeout=timeout or self.settings.ollama_timeout)

    def ensure_model(self) -> bool:
        """Download the model into Ollama the first time (a few minutes, ~2 GB)."""
        if self._model_ready:
            return True
        with self.client() as c:
            tags = c.get("/api/tags", timeout=15)
            tags.raise_for_status()
            names = {m.get("name") for m in tags.json().get("models", [])}
            if self.model not in names and f"{self.model}:latest" not in names:
                log.info("AI-model %s wordt gedownload in Ollama…", self.model)
                r = c.post("/api/pull", json={"model": self.model, "stream": False}, timeout=3600)
                r.raise_for_status()
                log.info("AI-model %s staat klaar.", self.model)
        self._model_ready = True
        return True

    def ask(self, product, hints: list[str]) -> dict:
        """Ask the model about one product. Returns the cleaned-up answer."""
        cat = product["category"]
        facts = {
            "name": product["name"],
            "brand": product["brand"] or "unknown",
            "volume_ml": product["volume_ml"] or "unknown",
            "abv_percent": product["abv"] if product["abv"] is not None else "unknown",
            "category": cat if cat != categories.DEFAULT else "unknown",
        }
        prompt = "Bottle:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
        hints = [h.strip() for h in hints if h and h.strip()]
        if hints:
            prompt += "\n\nWikipedia about it:\n" + "\n".join(hints)[:1200]
        prompt += ("\n\nCategories: " + ", ".join(f"{k} ({CATEGORY_EN.get(k, k)})" for k in categories.KEYS)
                   + ". 'also' is only for a flavoured liqueur made from a spirit (e.g. whisky liqueur: "
                   "category likeur, also [whisky]); otherwise leave it empty.")
        body = {
            "model": self.model, "stream": False, "format": _schema(),
            "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 300, "repeat_penalty": 1.15,
                        "num_thread": 3},
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        }
        with self.client() as c:
            r = c.post("/api/chat", json=body)
            r.raise_for_status()
            content = (r.json().get("message") or {}).get("content") or "{}"
        return clean_answer(json.loads(content))


def clean_answer(a: dict) -> dict:
    def text(key, n):
        v = a.get(key)
        v = v.strip() if isinstance(v, str) else ""
        return "" if v.lower() in ("", "onbekend", "unknown", "n/a", "-", "geen", "none") else v[:n]

    cat = a.get("category") if categories.valid(a.get("category", "")) else categories.DEFAULT
    also = [c for c in (a.get("also") or []) if isinstance(c, str) and categories.valid(c)
            and c not in (cat, categories.DEFAULT)] if cat == "likeur" else []
    return {"brand": text("brand", 100), "country": text("country", 60), "category": cat,
            "also": list(dict.fromkeys(also))[:2], "description": text("description", 300),
            "sure": bool(a.get("sure"))}


def apply_answer(db: Database, product_id: int, ans: dict) -> list[str]:
    """Fill only empty fields. Returns the names of the fields that were filled."""
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if not p:
        return []
    filled, sets, args = [], [], []
    # Facts only when the model says it knows the product; the category it may always guess.
    name_words = set(categories.normalize(p["name"]).split())
    if ans["sure"]:
        # The brand must be taken from the name itself, not made up.
        if not p["brand"] and ans["brand"] and set(categories.normalize(ans["brand"]).split()) <= name_words:
            filled.append("brand"); sets.append("brand=?"); args.append(ans["brand"])
        if not p["country"] and ans["country"]:
            filled.append("country"); sets.append("country=?"); args.append(ans["country"])
        if not p["description"] and ans["description"]:
            filled.append("description"); sets.append("description=?"); args.append(ans["description"])
    if not p["category_locked"] and p["category"] == categories.DEFAULT and ans["category"] != categories.DEFAULT:
        filled.append("category"); sets.append("category=?, extra_categories=?")
        args += [ans["category"], ",".join(ans["also"])]
    old = [f for f in (p["ai_fields"] or "").split(",") if f]
    sets.append("ai_fields=?"); args.append(",".join(dict.fromkeys(old + filled)))
    if filled:
        sets.append("source = CASE WHEN source='' THEN 'AI' WHEN source LIKE '%AI%' THEN source "
                    "ELSE source || ', AI' END")
    sets.append("ai_for=?, ai_attempts=0"); args.append(p["name"])
    db.run(f"UPDATE products SET {', '.join(sets)} WHERE id=?", *args, product_id)
    return filled


class AiWorker:
    """Goes through all named products one by one in the background (a Pi needs ~1 min each)."""

    def __init__(self, db: Database, settings, enricher: Enricher, lookup):
        self.db, self.settings, self.enricher, self.lookup = db, settings, enricher, lookup
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.busy_with: int | None = None
        self.last_error = ""

    def start(self) -> None:
        if not self.enricher.enabled or self.settings.lookup_inline:
            return
        self._thread = threading.Thread(target=self._run, name="ai-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def wake(self) -> None:
        self._wake.set()

    def todo(self) -> list:
        return self.db.all("SELECT id FROM products WHERE name != '' AND ai_for != name AND ai_attempts < ? "
                           "AND lookup_status != 'pending' ORDER BY updated_at DESC", MAX_ATTEMPTS)

    def _run(self) -> None:
        backoff = 60
        while not self._stop.is_set():
            try:
                self.enricher.ensure_model()
                rows = self.todo()
                for r in rows:
                    if self._stop.is_set():
                        break
                    self.process(r["id"])
                backoff = 60
                self.last_error = ""
            except (httpx.HTTPError, ValueError) as exc:
                # Ollama down, still starting or model still downloading: try again later.
                self.last_error = str(exc) or exc.__class__.__name__
                log.info("AI niet bereikbaar (%s); over %ds opnieuw.", self.last_error, backoff)
                self._wake.wait(backoff)
                backoff = min(backoff * 2, 1800)
                self._wake.clear()
                continue
            except Exception:  # noqa: BLE001
                log.exception("AI-aanvulling mislukt")
            self.busy_with = None
            self._wake.wait(60)
            self._wake.clear()

    def process(self, product_id: int) -> list[str]:
        p = self.db.one("SELECT * FROM products WHERE id=?", product_id)
        if not p or not p["name"]:
            return []
        self.busy_with = product_id
        try:
            hints = self.lookup.wikipedia_texts(p["name"], p["brand"], langs=("en",))
            started = time.time()
            ans = self.enricher.ask(p, hints)
        except (httpx.ReadTimeout, json.JSONDecodeError, ValueError) as exc:
            # Model answered badly or too slowly for this product: count it, move on.
            self.db.run("UPDATE products SET ai_attempts = ai_attempts + 1 WHERE id=?", product_id)
            log.info("AI kon product %s niet aanvullen: %s", product_id, exc)
            return []
        finally:
            self.busy_with = None
        filled = apply_answer(self.db, product_id, ans)
        log.info("AI %s (%.0fs): %s", p["name"], time.time() - started,
                 ", ".join(FIELDS_LABEL[f] for f in filled) or "niets aangevuld")
        return filled
