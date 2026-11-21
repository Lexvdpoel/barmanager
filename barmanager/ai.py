"""Optional local language model (Ollama, e.g. Qwen 2.5 3B) that fills in missing product details.

What it does: for every bottle with a known name it adds what is still empty - brand, country,
alcohol percentage, a short Dutch description and, when the categoriser gave up ('Overig'),
the category. It never overwrites what a user or a product database filled in, and every
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
FIELDS_LABEL = {"brand": "merk", "country": "land", "abv": "alcohol", "description": "omschrijving",
                "category": "categorie"}

SYSTEM = (
    "Je bent een drankenkenner die een voorraadsysteem voor een bar helpt. Je krijgt gegevens van één "
    "fles drank en vult ontbrekende gegevens aan. Antwoord alleen met feiten die je zeker weet over dit "
    "specifieke product of merk; laat een veld leeg (lege tekst of 0) als je het niet zeker weet. "
    "Verzin niets. Schrijf de omschrijving in het Nederlands, maximaal drie korte zinnen: wat voor drank "
    "het is, waar het vandaan komt en hoe het smaakt of gebruikt wordt."
)


def _schema() -> dict:
    keys = list(categories.KEYS)
    return {
        "type": "object",
        "properties": {
            "brand": {"type": "string"},
            "country": {"type": "string"},
            "abv": {"type": "number"},
            "category": {"type": "string", "enum": keys},
            "also": {"type": "array", "items": {"type": "string", "enum": keys}},
            "description": {"type": "string"},
            "sure": {"type": "boolean"},
        },
        "required": ["brand", "country", "abv", "category", "also", "description", "sure"],
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
            "naam": product["name"],
            "merk": product["brand"] or "onbekend",
            "inhoud_ml": product["volume_ml"] or "onbekend",
            "alcohol_procent": product["abv"] if product["abv"] is not None else "onbekend",
            "categorie": categories.label(cat) if cat != categories.DEFAULT else "onbekend",
            "omschrijving": product["description"] or "",
        }
        prompt = "Fles:\n" + json.dumps(facts, ensure_ascii=False, indent=1)
        hints = [h.strip() for h in hints if h and h.strip()]
        if hints:
            prompt += "\n\nWat Wikipedia hierover zegt:\n" + "\n".join(hints)[:1500]
        prompt += ("\n\nKies 'category' uit: " + ", ".join(f"{k} ({categories.label(k)})" for k in categories.KEYS)
                   + ". Zet in 'also' extra categorieën als de drank er ook onder valt (bijv. een whisky-likeur: "
                   "category likeur, also [whisky]). 'country' is het land van herkomst in het Nederlands. "
                   "'abv' is het alcoholpercentage. 'sure' is true als je dit product of merk echt kent.")
        body = {
            "model": self.model, "stream": False, "format": _schema(),
            "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 400},
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
        return "" if v.lower() in ("", "onbekend", "unknown", "n/a", "-", "geen") else v[:n]

    abv = a.get("abv")
    abv = float(abv) if isinstance(abv, (int, float)) and 0.5 <= abv <= 80 else None
    cat = a.get("category") if categories.valid(a.get("category", "")) else categories.DEFAULT
    also = [c for c in (a.get("also") or []) if isinstance(c, str) and categories.valid(c)
            and c not in (cat, categories.DEFAULT)]
    return {"brand": text("brand", 100), "country": text("country", 60), "abv": abv, "category": cat,
            "also": list(dict.fromkeys(also))[:3], "description": text("description", 600),
            "sure": bool(a.get("sure"))}


def apply_answer(db: Database, product_id: int, ans: dict) -> list[str]:
    """Fill only empty fields. Returns the names of the fields that were filled."""
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if not p:
        return []
    filled, sets, args = [], [], []
    # Facts about the bottle only when the model says it knows it; a description it may always try.
    if ans["sure"]:
        if not p["brand"] and ans["brand"]:
            filled.append("brand"); sets.append("brand=?"); args.append(ans["brand"])
        if not p["country"] and ans["country"]:
            filled.append("country"); sets.append("country=?"); args.append(ans["country"])
        if p["abv"] is None and ans["abv"] is not None:
            filled.append("abv"); sets.append("abv=?"); args.append(ans["abv"])
    if not p["description"] and ans["description"] and ans["sure"]:
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
            hints = self.lookup.wikipedia_texts(p["name"], p["brand"])
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
