"""Optional local language model (Ollama, e.g. Qwen 2.5 3B) that fills in missing product details.

What it does: for every bottle with a known name it adds what is still empty - brand, country,
a short English description and, when the categoriser gave up ('Overig'), the category.

Grounded: before asking, BarManager collects sources about the bottle (product database text,
Wikipedia, web search results) and the model may only use facts from those sources. Without
sources it only writes a generic description of the kind of drink - no names, places or years.
Exact numbers such as the alcohol percentage are left alone. It never overwrites what a user or
a product database filled in, and every field it fills is remembered so the product page can
mark it as "AI".

What it does not do: invent a product for an unknown barcode. A small model does not know
barcodes; asked anyway it makes up a plausible bottle, which is worse than "unknown".
"""

import json
import logging
import re
import threading
import time

import httpx

from . import categories
from .db import Database
from .lookup import _ABV, name_matches

log = logging.getLogger("barmanager.ai")

MAX_ATTEMPTS = 3
FIELDS_LABEL = {"brand": "merk", "country": "land", "description": "omschrijving", "category": "categorie",
                "abv": "alcohol", "extra": "valt ook onder"}

SYSTEM = (
    "You help a bar keep its inventory. You get one bottle of drink and SOURCES found about it "
    "(product databases, Wikipedia, web search results). Sources can be about other products; "
    "ignore those. Use ONLY facts that the sources state about this bottle or its brand. "
    "Do not use your own memory for facts. Never invent anything.\n"
    "- brand: the brand name exactly as it appears in the product name (e.g. 'Jim Beam'), nothing added.\n"
    "- country: the country the drink comes from, in English, as stated or clearly implied by the "
    "sources (e.g. 'Orkney' or 'Speyside' means Scotland, 'Norwegian' means Norway); otherwise empty.\n"
    "- category: what kind of drink it is.\n"
    "- description: ONE or TWO short sentences in English, max 200 characters. If the sources "
    "describe this bottle: summarise only what they say (kind of drink, origin, main taste). If they "
    "do not: describe only the kind of drink the name says it is, WITHOUT any names, places, years "
    "or numbers. Always write English, also when the sources are in another language. "
    "No marketing, no repetition, no remarks about the text itself.\n"
    "- abv: alcohol percentage of this bottle as stated in the sources; 0 if no source states it.\n"
    "- sure: true only if the sources clearly are about this product or its brand."
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
            "abv": {"type": "number"},
            "sure": {"type": "boolean"},
        },
        "required": ["brand", "country", "category", "also", "description", "abv", "sure"],
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

    def _chat(self, system: str, prompt: str, schema: dict, num_predict: int = 300) -> dict:
        body = {
            "model": self.model, "stream": False, "format": schema, "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": num_predict, "repeat_penalty": 1.15,
                        "num_thread": 3},
            **({"think": False} if self.thinking_model else {}),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        with self.client() as c:
            r = c.post("/api/chat", json=body)
            r.raise_for_status()
            return json.loads((r.json().get("message") or {}).get("content") or "{}")

    def identify(self, barcode: str, results: list[dict]) -> dict:
        """Clean product name + brand from search results that mention the barcode."""
        lines = "\n".join(f"- {x['title']}: {x['text'][:300]}" for x in results[:8])
        a = self._chat(IDENTIFY_SYSTEM, f"Barcode: {barcode}\n\nSearch results:\n{lines}", _identify_schema(), 120)
        name = a.get("name") if isinstance(a.get("name"), str) else ""
        brand = a.get("brand") if isinstance(a.get("brand"), str) else ""
        return {"name": name.strip()[:150], "brand": brand.strip()[:100], "sure": bool(a.get("sure"))}

    @property
    def thinking_model(self) -> bool:
        m = self.model.lower()
        return any(x in m for x in ("qwen3", "deepseek-r1", "phi4-mini-reasoning", "magistral", "gpt-oss"))

    def ask(self, product, sources: dict[str, list[str]]) -> dict:
        """Ask the model about one product, given {source name: [texts]}. Returns the cleaned-up answer."""
        cat = product["category"]
        facts = {
            "name": product["name"],
            "brand": product["brand"] or "unknown",
            "volume_ml": product["volume_ml"] or "unknown",
            "abv_percent": product["abv"] if product["abv"] is not None else "unknown",
            "category": cat if cat != categories.DEFAULT else "unknown",
        }
        prompt = "Bottle:\n" + json.dumps(facts, ensure_ascii=False, indent=1) + "\n\nSOURCES:"
        budget, any_source = 2400, False
        for label, texts in sources.items():
            texts = [t.strip() for t in texts if t and t.strip()]
            if not texts or budget <= 0:
                continue
            block = "\n".join(f"- {t[:400]}" for t in texts)[:budget]
            prompt += f"\n[{label}]\n{block}"
            budget -= len(block)
            any_source = True
        if not any_source:
            prompt += "\n(none found)"
        prompt += ("\n\nCategories: " + ", ".join(f"{k} ({CATEGORY_EN.get(k, k)})" for k in categories.KEYS)
                   + ". 'also' is only for a flavoured liqueur made from a spirit (e.g. whisky liqueur: "
                   "category likeur, also [whisky]); otherwise leave it empty.")
        body = {
            "model": self.model, "stream": False, "format": _schema(),
            "keep_alive": "30m",
            "options": {"temperature": 0.1, "num_ctx": 4096, "num_predict": 300, "repeat_penalty": 1.15,
                        "num_thread": 3},
            # Reasoning models think first; with structured output we want only the answer.
            **({"think": False} if self.thinking_model else {}),
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        }
        with self.client() as c:
            r = c.post("/api/chat", json=body)
            r.raise_for_status()
            content = (r.json().get("message") or {}).get("content") or "{}"
        return clean_answer(json.loads(content))


IDENTIFY_SYSTEM = (
    "You identify a bottle of drink from web search results for its barcode. The results mention the "
    "barcode, but can be shop pages, lists or noise. Use ONLY what the results say. Never invent.\n"
    "- name: the product name as a shop would write it, in its original language, with brand and type "
    "but without bottle size, price or shop name (e.g. 'Canadian Club Classic 12 Year Old').\n"
    "- brand: the brand name.\n"
    "- sure: true only if the results clearly agree on what product this barcode is."
)


def _identify_schema() -> dict:
    return {"type": "object",
            "properties": {"name": {"type": "string", "maxLength": 90}, "brand": {"type": "string", "maxLength": 40},
                           "sure": {"type": "boolean"}},
            "required": ["name", "brand", "sure"]}


def clean_answer(a: dict) -> dict:
    def text(key, n):
        v = a.get(key)
        v = v.strip() if isinstance(v, str) else ""
        return "" if v.lower() in ("", "onbekend", "unknown", "n/a", "-", "geen", "none") else v[:n]

    def tidy(desc: str) -> str:
        # Small models sometimes add remarks like "(180 characters)" or a stray clock time.
        desc = re.sub(r"\s*\(\s*\d+\s*(characters|chars|tekens)\s*\)", "", desc, flags=re.I)
        parts = [x for x in re.split(r"(?<=[.!?])\s+", desc) if x and not re.search(r"\b\d{1,2}:\d{2}\b", x)]
        out = " ".join(parts).strip()
        if out and out[-1] not in ".!?":
            cut = max(out.rfind(". "), out.rfind("! "), out.rfind("? "))
            out = out[:cut + 1] if cut >= 20 else out      # drop a sentence the length limit cut off
        return out

    cat = a.get("category") if categories.valid(a.get("category", "")) else categories.DEFAULT
    also = [c for c in (a.get("also") or []) if isinstance(c, str) and categories.valid(c)
            and c not in (cat, categories.DEFAULT)] if cat == "likeur" else []
    abv = a.get("abv")
    abv = float(abv) if isinstance(abv, (int, float)) and not isinstance(abv, bool) and 0.5 <= abv <= 80 else None
    return {"brand": text("brand", 100), "country": country_en(text("country", 60)), "category": cat, "abv": abv,
            "also": list(dict.fromkeys(also))[:2], "description": tidy(text("description", 300)),
            "sure": bool(a.get("sure"))}


# Small models sometimes answer with the country's own name ("norge", "Shqipëri").
COUNTRY_EN = {
    "norge": "Norway", "noreg": "Norway", "nederland": "Netherlands", "holland": "Netherlands",
    "the netherlands": "Netherlands", "deutschland": "Germany", "duitsland": "Germany", "shqiperi": "Albania",
    "shqiperia": "Albania", "espana": "Spain", "italia": "Italy", "eire": "Ireland", "sverige": "Sweden",
    "danmark": "Denmark", "suomi": "Finland", "polska": "Poland", "hrvatska": "Croatia", "schweiz": "Switzerland",
    "suisse": "Switzerland", "osterreich": "Austria", "mexico": "Mexico", "magyarorszag": "Hungary",
    "cesko": "Czechia", "ceska republika": "Czechia", "belgie": "Belgium", "belgique": "Belgium",
    "schotland": "Scotland", "ierland": "Ireland", "frankrijk": "France", "usa": "United States",
    "us": "United States", "united states of america": "United States", "uk": "United Kingdom",
    "hellas": "Greece", "ellada": "Greece", "portugal": "Portugal", "brasil": "Brazil", "nippon": "Japan",
}


def country_en(name: str) -> str:
    key = categories.normalize(name)
    if not key:
        return ""
    if key in COUNTRY_EN:
        return COUNTRY_EN[key]
    return name[:1].upper() + name[1:] if name.islower() else name


NON_ALCOHOLIC = {"mixer", "sap", "siroop"}


def abv_in(text: str, abv: float) -> bool:
    """Is this alcohol percentage literally in the source text (e.g. '40%', '40 % vol', '15,5%')?"""
    for m in _ABV.finditer(text or ""):
        if abs(float(m.group(1).replace(",", ".")) - abv) < 0.05:
            return True
    return False


def apply_answer(db: Database, product_id: int, ans: dict, source_text: str = "") -> list[str]:
    """Fill only empty fields. Returns the names of the fields that were filled."""
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if not p:
        return []
    filled, sets, args = [], [], []
    # Facts only when the sources were about this product; a generic description and the
    # category it may always give.
    name_words = set(categories.normalize(p["name"]).split())
    if ans["sure"]:
        # The brand must be taken from the name itself, not made up.
        if not p["brand"] and ans["brand"] and set(categories.normalize(ans["brand"]).split()) <= name_words:
            filled.append("brand"); sets.append("brand=?"); args.append(ans["brand"])
        if not p["country"] and ans["country"]:
            filled.append("country"); sets.append("country=?"); args.append(ans["country"])
        # A number only when it is really in the sources, never from the model's memory.
        if p["abv"] is None and ans.get("abv") and abv_in(source_text, ans["abv"]):
            filled.append("abv"); sets.append("abv=?"); args.append(ans["abv"])
    if not p["description"] and ans["description"]:
        filled.append("description"); sets.append("description=?"); args.append(ans["description"])
    # A drink with alcohol is never a soft drink, juice or syrup (Kesbeke Zure bom shot, 14.9%).
    alcoholic = (p["abv"] or ans.get("abv") or 0) >= 1.5
    if not p["category_locked"] and p["category"] == categories.DEFAULT and ans["category"] != categories.DEFAULT \
            and not (alcoholic and ans["category"] in NON_ALCOHOLIC):
        filled.append("category"); sets.append("category=?, extra_categories=?")
        args += [ans["category"], ",".join(ans["also"])]
    old = [f for f in (p["ai_fields"] or "").split(",") if f]
    sets.append("ai_fields=?"); args.append(",".join(dict.fromkeys(old + filled)))
    if filled:
        sets.append("source = CASE WHEN source='' THEN 'AI' WHEN source LIKE '%AI%' THEN source "
                    "ELSE source || ', AI' END")
    sets.append("ai_for=?, ai_attempts=0"); args.append(p["name"])
    db.run(f"UPDATE products SET {', '.join(sets)} WHERE id=?", *args, product_id)
    if add_extra_categories(db, product_id):
        filled.append("extra")
    return filled


def add_extra_categories(db: Database, product_id: int) -> bool:
    """'Valt ook onder': run the categoriser over name + (new) description and add what it finds,
    e.g. 'whiskey liqueur' -> also whisky. Only when it agrees on the main category."""
    p = db.one("SELECT * FROM products WHERE id=?", product_id)
    if not p or p["category_locked"] or p["category"] == categories.DEFAULT:
        return False
    cats = categories.classify_all(texts=[p["name"], p["description"]], brand=p["brand"])
    if cats[0] != p["category"]:
        return False
    have = [c for c in (p["extra_categories"] or "").split(",") if c]
    new = [c for c in cats[1:] if c not in have and c != p["category"]]
    if not new:
        return False
    extra = sorted(have + new, key=lambda c: categories.ORDER.get(c, 99))
    old = [f for f in (p["ai_fields"] or "").split(",") if f]
    db.run("UPDATE products SET extra_categories=?, ai_fields=? WHERE id=?",
           ",".join(extra), ",".join(dict.fromkeys(old + ["extra"])), product_id)
    return True


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

    def sources(self, p) -> dict[str, list[str]]:
        """Everything we can find about this bottle, for the model to work from."""
        out: dict[str, list[str]] = {}
        ai_fields = (p["ai_fields"] or "").split(",")
        if p["description"] and "description" not in ai_fields:
            out["Product database"] = [p["description"]]
        out["Wikipedia"] = self.lookup.wikipedia_texts(p["name"], p["brand"], langs=("en",))
        out["Web search"] = self.lookup.web_texts(p["name"], p["brand"])
        return {k: v for k, v in out.items() if v}

    def identify(self, p) -> bool:
        """Barcode only found by a web search: let the model read the results and name it properly."""
        results = self.lookup.barcode_results(p["barcode"])
        marked = ",".join(dict.fromkeys([f for f in (p["ai_fields"] or "").split(",") if f] + ["name"]))
        if not results:
            self.db.run("UPDATE products SET ai_fields=? WHERE id=?", marked, p["id"])
            return False
        ans = self.enricher.identify(p["barcode"], results)
        seen = " ".join(x["title"] + " " + x["text"] for x in results)
        # Only accept a name that is really in the search results, not one the model knows or made up.
        ok = ans["sure"] and ans["name"] and name_matches(ans["name"], seen, need=0.75)
        if ok:
            self.db.run("UPDATE products SET name=?, brand = CASE WHEN brand='' THEN ? ELSE brand END, ai_fields=?, "
                        "updated_at=? WHERE id=? AND lookup_status='guess'",
                        ans["name"], ans["brand"] if name_matches(ans["brand"], seen, need=0.99) else "",
                        marked, time.time(), p["id"])
            log.info("AI: barcode %s is '%s' (was '%s')", p["barcode"], ans["name"], p["name"])
        else:
            self.db.run("UPDATE products SET ai_fields=? WHERE id=?", marked, p["id"])
        return bool(ok)

    def process(self, product_id: int) -> list[str]:
        p = self.db.one("SELECT * FROM products WHERE id=?", product_id)
        if not p or not p["name"]:
            return []
        if p["lookup_status"] == "guess" and "name" not in (p["ai_fields"] or "").split(","):
            self.busy_with = product_id
            try:
                if self.identify(p):
                    p = self.db.one("SELECT * FROM products WHERE id=?", product_id)
            except (httpx.ReadTimeout, json.JSONDecodeError, ValueError) as exc:
                log.info("AI kon barcode %s niet benoemen: %s", p["barcode"], exc)
        self.busy_with = product_id
        try:
            sources = self.sources(p)
            started = time.time()
            ans = self.enricher.ask(p, sources)
        except (httpx.ReadTimeout, json.JSONDecodeError, ValueError) as exc:
            # Model answered badly or too slowly for this product: count it, move on.
            self.db.run("UPDATE products SET ai_attempts = ai_attempts + 1 WHERE id=?", product_id)
            log.info("AI kon product %s niet aanvullen: %s", product_id, exc)
            return []
        finally:
            self.busy_with = None
        filled = apply_answer(self.db, product_id, ans, "\n".join(t for v in sources.values() for t in v))
        log.info("AI %s (%.0fs, bronnen: %s): %s", p["name"], time.time() - started,
                 ", ".join(sources) or "geen", ", ".join(FIELDS_LABEL[f] for f in filled) or "niets aangevuld")
        return filled
