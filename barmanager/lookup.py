"""Looking up unknown barcodes on the internet.

Sources (all official APIs):
- Open Food Facts   (free, no key; covers most drinks sold in Europe)
- UPCitemdb trial   (free, no key, ~100 lookups/day)
- OpenGTINDB        (free, optional BM_OPENGTINDB_ID; German/Dutch market)
- Brave Search API  (optional, BM_BRAVE_API_KEY): web search on the barcode when the
                    product databases do not know it, and image search on the product name.
- Wikipedia         (free): not for barcodes (articles do not list them), but to find out what
                    kind of drink a name/brand is when the categoriser cannot tell.
"""

import html
import logging
import re
from dataclasses import dataclass, field

import httpx

from . import __version__

log = logging.getLogger("barmanager.lookup")

USER_AGENT = f"BarManager/{__version__} (self-hosted bar inventory)"
OFF_URL = "https://world.openfoodfacts.org/api/v2/product/{code}.json"
OFF_SEARCH = "https://search.openfoodfacts.org/search"
OPENGTINDB = "https://opengtindb.org/"
OFF_FIELDS = ("product_name,product_name_nl,product_name_en,generic_name,generic_name_nl,brands,"
              "categories_tags,categories,quantity,nutriments,image_front_url,image_url,"
              "selected_images")
UPC_LOOKUP = "https://api.upcitemdb.com/prod/trial/lookup"
UPC_SEARCH = "https://api.upcitemdb.com/prod/trial/search"
WIKIPEDIA = "https://{lang}.wikipedia.org/w/api.php"
BRAVE_WEB = "https://api.search.brave.com/res/v1/web/search"
BRAVE_IMAGES = "https://api.search.brave.com/res/v1/images/search"


@dataclass
class LookupResult:
    name: str = ""
    brand: str = ""
    volume_ml: int | None = None
    abv: float | None = None
    description: str = ""
    tags: list[str] = field(default_factory=list)       # Open Food Facts category tags
    texts: list[str] = field(default_factory=list)      # category/description text
    web_texts: list[str] = field(default_factory=list)  # noisy search-result text
    image_urls: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    guessed: bool = False                               # name comes from a web search
    transient: bool = False                             # a source failed (timeout, 5xx): try again later

    @property
    def found(self) -> bool:
        return bool(self.name)

    def add_images(self, urls) -> None:
        for u in urls:
            if u and u.startswith(("http://", "https://")) and u not in self.image_urls:
                self.image_urls.append(u)


def barcode_variants(code: str) -> list[str]:
    """EAN-13 with a leading 0 is the same product as the 12-digit UPC-A code."""
    out = [code]
    if code.isdigit():
        if len(code) == 13 and code.startswith("0"):
            out.append(code[1:])
        elif len(code) == 12:
            out.append("0" + code)
    return out


_VOL = re.compile(r"(\d+(?:[.,]\d+)?)\s*(ml|cl|dl|ltr|liter|litre|l)(?![a-z])", re.I)
_ABV = re.compile(r"(\d{1,2}(?:[.,]\d{1,2})?)\s*%(?:\s*(?:vol|alc|abv))?", re.I)


def parse_volume(text: str | None) -> int | None:
    m = _VOL.search(text or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    factor = {"ml": 1, "cl": 10, "dl": 100}.get(m.group(2).lower(), 1000)
    ml = round(value * factor)
    return ml if 20 <= ml <= 30000 else None


def parse_abv(text: str | None) -> float | None:
    for m in _ABV.finditer(text or ""):
        v = float(m.group(1).replace(",", "."))
        if 0 < v <= 96:
            return v
    return None


def clean_title(title: str, barcode: str = "") -> str:
    """'Johnnie Walker Red Label 70cl - Drankdozijn' -> 'Johnnie Walker Red Label 70cl'."""
    t = html.unescape(re.sub(r"<[^>]+>", "", title or ""))
    t = re.split(r"\s[-|–—•·:]\s|\s\|\s?", t)[0]
    if barcode:
        t = t.replace(barcode, "")
    t = re.sub(r"\b(EAN|UPC|GTIN|barcode)\b[:\s]*\d*", "", t, flags=re.I)
    t = re.sub(r"\b(kopen|bestellen|online|buy|kaufen)\b", "", t, flags=re.I)
    t = " ".join(t.split()).strip(" -,.")
    return t[:100]


class Lookup:
    def __init__(self, settings):
        self.settings = settings

    def client(self) -> httpx.Client:
        return httpx.Client(timeout=self.settings.lookup_timeout,
                            headers={"User-Agent": USER_AGENT})

    @property
    def brave_key(self) -> str:
        return getattr(self.settings, "brave_api_key", "") or ""

    # ---- main entry -------------------------------------------------------
    def lookup(self, barcode: str) -> LookupResult:
        res = LookupResult()
        with self.client() as c:
            def safe(fn, *args):
                out = self._safe(fn, *args)
                if out is None:
                    res.transient = True
                return out

            for code in barcode_variants(barcode):
                if safe(self._off, c, code, res):
                    break
            if not res.found and getattr(self.settings, "opengtindb_id", ""):
                safe(self._opengtindb, c, barcode, res)
            if not res.found or not res.image_urls:
                if self.settings.use_upcitemdb:
                    for code in barcode_variants(barcode):
                        if safe(self._upcitemdb, c, code, res):
                            break
            if not res.found and self.brave_key:
                safe(self._brave_web, c, barcode, res)
            if res.found and not res.image_urls:
                res.add_images(self._image_search(c, image_query(res.name, res.brand)))
        if res.found:
            res.transient = False
        return res

    def image_search(self, query: str) -> list[str]:
        with self.client() as c:
            return self._image_search(c, query)

    def _image_search(self, c: httpx.Client, query: str) -> list[str]:
        if not query.strip():
            return []
        urls: list[str] = []
        if self.brave_key:
            urls += self._safe(self._brave_images, c, query) or []
        if len(urls) < self.settings.max_images:
            urls += self._safe(self._off_search_images, c, query) or []
        if len(urls) < self.settings.max_images and self.settings.use_upcitemdb:
            urls += self._safe(self._upc_search_images, c, query) or []
        seen: list[str] = []
        for u in urls:
            if u not in seen:
                seen.append(u)
        return seen

    @staticmethod
    def _safe(fn, *args):
        """Run one source; None means the source itself failed (network, 5xx, bad data)."""
        try:
            return fn(*args)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            log.info("%s mislukt: %s", fn.__name__.lstrip("_"), exc)
            return None

    # ---- Wikipedia ------------------------------------------------------------------
    def wikipedia_texts(self, name: str, brand: str = "", langs=("nl", "en")) -> list[str]:
        """Short description + intro of the Wikipedia article about this drink or its brand."""
        query = image_query(re.sub(r"\d+([.,]\d+)?\s*(cl|ml|l|ltr|%)\b", " ", name or "", flags=re.I), brand)
        if not _tokens(query):
            return []
        with self.client() as c:
            for lang in langs:
                texts = self._safe(self._wikipedia, c, lang, query) or []
                if texts:
                    return texts
        return []

    def _wikipedia(self, c: httpx.Client, lang: str, query: str) -> list[str]:
        r = c.get(WIKIPEDIA.format(lang=lang), params={
            "action": "query", "format": "json", "generator": "search", "gsrsearch": query,
            "gsrlimit": 3, "prop": "description|extracts", "exintro": 1, "explaintext": 1,
            "exsentences": 3, "redirects": 1})
        r.raise_for_status()
        pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(),
                       key=lambda pg: pg.get("index", 99))
        for pg in pages:
            # The article title (brand or product) must be part of what we searched for.
            if name_matches(pg.get("title", ""), query, need=0.99):
                return [pg.get("description", ""), pg.get("extract", "")]
        return []

    # ---- Open Food Facts ----------------------------------------------------
    def _off(self, c: httpx.Client, code: str, res: LookupResult) -> bool:
        r = c.get(OFF_URL.format(code=code), params={"fields": OFF_FIELDS})
        if r.status_code == 404:
            return False
        r.raise_for_status()
        data = r.json()
        p = data.get("product") or {}
        if data.get("status") != 1 or not p:
            return False
        name = (p.get("product_name_nl") or p.get("product_name") or p.get("product_name_en")
                or p.get("generic_name_nl") or p.get("generic_name") or "").strip()
        if not name and not p.get("brands"):
            return False
        res.sources.append("Open Food Facts")
        brand = (p.get("brands") or "").split(",")[0].strip()
        res.name = res.name or name or brand
        res.brand = res.brand or brand
        res.tags += p.get("categories_tags") or []
        res.texts += [p.get("categories") or "", p.get("generic_name") or ""]
        res.volume_ml = res.volume_ml or parse_volume(p.get("quantity")) or parse_volume(name)
        nutr = p.get("nutriments") or {}
        abv = nutr.get("alcohol_100g", nutr.get("alcohol_value", nutr.get("alcohol")))
        try:
            abv = float(abv) if abv not in (None, "") else None
        except (TypeError, ValueError):
            abv = None
        res.abv = res.abv or (abv if abv and 0 < abv <= 96 else None) or parse_abv(name)
        imgs = [p.get("image_front_url"), p.get("image_url")]
        front = ((p.get("selected_images") or {}).get("front") or {}).get("display") or {}
        imgs += [front.get(k) for k in ("nl", "en", "fr", "de")] + list(front.values())
        # The 400px "display" version is small; ask for the full resolution one.
        res.add_images(re.sub(r"\.(\d+)\.400\.jpg$", r".\1.full.jpg", u) for u in imgs if u)
        return True

    def _off_search_images(self, c: httpx.Client, query: str) -> list[str]:
        """Search Open Food Facts by name, only among alcoholic drinks, and only keep
        products whose name really matches (otherwise 'Tullamore Honey' finds honey ham)."""
        q = query.replace('"', " ")
        r = c.get(OFF_SEARCH, params={"q": f'{q} categories_tags:"en:alcoholic-beverages"', "page_size": 10,
                                      "fields": "product_name,brands,image_front_url"})
        r.raise_for_status()
        out = []
        for p in r.json().get("hits", []):
            brands = p.get("brands") or []
            label = " ".join([p.get("product_name") or "", *(brands if isinstance(brands, list) else [brands])])
            if p.get("image_front_url") and name_matches(query, label):
                out.append(re.sub(r"\.(\d+)\.400\.jpg$", r".\1.full.jpg", p["image_front_url"]))
        return out

    # ---- OpenGTINDB ---------------------------------------------------------------------
    def _opengtindb(self, c: httpx.Client, code: str, res: LookupResult) -> bool:
        if not code.isdigit():
            return False
        r = c.get(OPENGTINDB, params={"ean": code, "cmd": "query", "queryid": self.settings.opengtindb_id})
        r.raise_for_status()
        text = r.content.decode("utf-8", "replace") if b"\xc3" in r.content else r.content.decode("latin-1")
        data = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        err = data.get("error", "").strip()
        if err == "1":          # not found
            return False
        if err != "0":
            raise ValueError(f"OpenGTINDB error {err}")
        name = (data.get("detailname") or data.get("name") or "").strip()
        if not name:
            return False
        res.sources.append("OpenGTINDB")
        res.name = res.name or clean_title(name)
        res.brand = res.brand or (data.get("vendor") or "").strip()
        res.texts += [data.get("maincat", ""), data.get("subcat", ""), name, data.get("descr", "")]
        res.description = res.description or (data.get("descr") or "").strip()[:1000]
        res.volume_ml = res.volume_ml or parse_volume(name) or parse_volume(data.get("pack"))
        res.abv = res.abv or parse_abv(name) or parse_abv(data.get("descr"))
        return True

    # ---- UPCitemdb ------------------------------------------------------------
    def _upcitemdb(self, c: httpx.Client, code: str, res: LookupResult) -> bool:
        r = c.get(UPC_LOOKUP, params={"upc": code})
        if r.status_code in (400, 404, 429):
            return False
        r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            return False
        it = items[0]
        title = (it.get("title") or "").strip()
        if not title:
            return False
        res.sources.append("UPCitemdb")
        res.name = res.name or clean_title(title)
        res.brand = res.brand or (it.get("brand") or "").strip()
        res.texts += [it.get("category") or "", title]
        res.description = res.description or (it.get("description") or "").strip()[:1000]
        res.volume_ml = res.volume_ml or parse_volume(it.get("size")) or parse_volume(title)
        res.abv = res.abv or parse_abv(title) or parse_abv(it.get("description"))
        res.add_images(it.get("images") or [])
        return True

    def _upc_search_images(self, c: httpx.Client, query: str) -> list[str]:
        r = c.get(UPC_SEARCH, params={"s": query, "match_mode": 0, "type": "product"})
        if r.status_code in (400, 404, 429):
            return []
        r.raise_for_status()
        out: list[str] = []
        for it in (r.json().get("items") or [])[:5]:
            if name_matches(query, f"{it.get('title', '')} {it.get('brand', '')}"):
                out += it.get("images") or []
        return out

    # ---- Brave Search API -------------------------------------------------------
    def _brave_headers(self) -> dict:
        return {"X-Subscription-Token": self.brave_key, "Accept": "application/json"}

    def _brave_web(self, c: httpx.Client, barcode: str, res: LookupResult) -> bool:
        r = c.get(BRAVE_WEB, params={"q": barcode, "count": 10}, headers=self._brave_headers())
        r.raise_for_status()
        results = (r.json().get("web") or {}).get("results") or []
        titles = [x.get("title", "") for x in results]
        snippets = [re.sub(r"<[^>]+>", "", x.get("description", "")) for x in results]
        name = ""
        for t in titles:
            cleaned = clean_title(t, barcode)
            if len(cleaned) >= 4 and not cleaned.isdigit():
                name = cleaned
                break
        if not name:
            return False
        res.sources.append("webzoektocht")
        res.guessed = True
        res.name = name
        res.web_texts += titles + snippets
        joined = " ".join(titles[:3] + snippets[:3])
        res.volume_ml = res.volume_ml or parse_volume(name) or parse_volume(joined)
        res.abv = res.abv or parse_abv(joined)
        return True

    def _brave_images(self, c: httpx.Client, query: str) -> list[str]:
        r = c.get(BRAVE_IMAGES, params={"q": query, "count": 10, "safesearch": "strict"},
                  headers=self._brave_headers())
        r.raise_for_status()
        out: list[str] = []
        for x in r.json().get("results") or []:
            url = (x.get("properties") or {}).get("url") or (x.get("thumbnail") or {}).get("src")
            if url:
                out.append(url)
        return out


_STOP = {"de", "het", "een", "the", "and", "en", "of", "with", "met", "la", "le", "les", "di", "da"}


def _tokens(text: str) -> set[str]:
    from .categories import normalize
    out = set()
    for t in normalize(text).split():
        if t in _STOP or len(t) < 2 or re.fullmatch(r"\d+(cl|ml|l|ltr)?|\d+%?", t):
            continue
        out.add(t)
    return out


def name_matches(query: str, label: str, need: float = 0.75) -> bool:
    """At least `need` of the words of the query appear in the label."""
    q = _tokens(query)
    if not q:
        return False
    return len(q & _tokens(label)) / len(q) >= need


def image_query(name: str, brand: str = "") -> str:
    name = (name or "").strip()
    if brand and brand.lower() not in name.lower():
        name = f"{brand} {name}"
    return name
