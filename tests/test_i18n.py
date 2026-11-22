"""Every text the user sees has an English translation, and English pages contain no Dutch."""

import ast
import re
from pathlib import Path

from barmanager import categories, cocktails, i18n
from barmanager.locale_en import EN, JS

PKG = Path(__file__).resolve().parent.parent / "barmanager"
_TPL = re.compile(r"""\b_n?\(\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')(?:\s*,\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'))?""")
_JS = re.compile(r"""\bt\(\s*"((?:[^"\\]|\\.)*)\"""")


def template_keys() -> set[str]:
    keys = set()
    for f in (PKG / "templates").glob("*.html"):
        for m in _TPL.finditer(f.read_text(encoding="utf-8")):
            for g in m.groups():
                if g:
                    keys.add(g.replace('\\"', '"').replace("\\'", "'"))
    return keys


def python_keys() -> set[str]:
    keys = set()
    for f in PKG.glob("*.py"):
        if f.name in ("locale_en.py", "i18n.py"):
            continue
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "_n"):
                for a in node.args[:2]:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str):
                        keys.add(a.value)
    return keys


def js_keys() -> set[str]:
    keys = set()
    for f in (PKG / "static").glob("*.js"):
        keys |= set(_JS.findall(f.read_text(encoding="utf-8")))
    for f in (PKG / "templates").glob("*.html"):
        keys |= set(_JS.findall(f.read_text(encoding="utf-8")))
    return keys


def dynamic_keys() -> set[str]:
    """Texts that reach _() through a variable: category and pantry group labels, levels, lists of choices."""
    from barmanager import pages, service, web
    keys = {label for _k, label, _c in categories.CATEGORIES}
    keys |= {label for _k, label in cocktails.PANTRY_GROUPS}
    keys |= {label for _v, label in service.LEVELS}
    keys |= set(pages.SHOW.values()) | set(pages.TYPES.values()) | set(web.SORTS.values()) | set(web.STATES.values())
    keys |= {"scan", "handmatig", "geteld", "leeg"}          # movement reasons
    keys |= {"Locatie niet gevonden", "Product niet gevonden", "Niet ingelogd", "Alleen voor beheerders",
             "Ongeldig formulier, laad de pagina opnieuw", "Ongeldige barcode", "Cocktail niet gevonden",
             "Onbekend ingrediënt", "Niet gevonden", "Alleen de eigenaar kan dit wijzigen", "Barkaart staat uit",
             "Deze barkaart bestaat niet (meer)"}
    return keys


def test_every_text_has_an_english_translation():
    keys = template_keys() | python_keys() | js_keys() | dynamic_keys()
    missing = sorted(k for k in keys if k not in EN)
    assert not missing, "No English for:\n" + "\n".join(missing)


def test_js_texts_are_shipped_to_the_browser():
    assert js_keys() <= set(JS), sorted(js_keys() - set(JS))


def test_placeholders_match():
    for nl, en in EN.items():
        assert set(re.findall(r"\{(\w+)\}", nl)) == set(re.findall(r"\{(\w+)\}", en)), (nl, en)


def test_pick_language():
    assert i18n.pick_lang(None, "nl-NL,nl;q=0.9,en;q=0.8") == "nl"
    assert i18n.pick_lang(None, "en-US,en;q=0.9") == "en"
    assert i18n.pick_lang(None, "de-DE") == "en"
    assert i18n.pick_lang("nl", "en-US") == "nl"
    assert i18n.pick_lang(None, None) == "nl"


DUTCH = re.compile(r"\b(Opslaan|Locaties|Voorraad|Toevoegen|Verwijderen|Zoeken|flessen|Instellingen|Boodschappen"
                   r"|Uitloggen|Terug|Gebruikersnaam|Wachtwoord|Statistieken|bijhouden|Omschrijving)\b")


def _visible(html: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    html = re.sub(r'(?:href|src|action|value|name|id|class|data-[\w-]+|for|formaction)="[^"]*"', " ", html)
    return re.sub(r"<[^>]+>", " ", html)


def test_english_pages_have_no_dutch(admin):
    c, csrf = admin
    lid = int(c.post("/locaties", data={"csrf": csrf, "name": "Bar"}, follow_redirects=False)
              .headers["location"].split("?")[0].rsplit("/", 1)[-1])
    c.post(f"/api/locaties/{lid}/scan", json={"barcode": "5000267014203"}, headers={"X-CSRF-Token": csrf})
    c.post(f"/locaties/{lid}/bijhouden", data={"csrf": csrf, "aan": "1"})
    c.cookies.set("bm_lang", "en")
    pid = c.app.state.db.one("SELECT id FROM products")["id"]
    for url in ["/locaties", f"/locaties/{lid}", f"/locaties/{lid}/instellingen", f"/locaties/{lid}/scannen",
                f"/locaties/{lid}/statistieken", f"/producten/{pid}?loc={lid}", "/producten", "/cocktails",
                "/boodschappen", "/keuken", "/gebruikers", "/account"]:
        r = c.get(url)
        assert r.status_code == 200, url
        assert '<html lang="en">' in r.text
        found = DUTCH.findall(_visible(r.text))
        assert not found, (url, found)
    assert "Locations" in c.get("/locaties").text
    c.cookies.set("bm_lang", "nl")
    assert "Locaties" in c.get("/locaties").text


def test_language_switch_sets_cookie(client):
    r = client.get("/taal/en?terug=/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert "bm_lang=en" in r.headers["set-cookie"]
    assert "Log in" in client.get("/login").text
