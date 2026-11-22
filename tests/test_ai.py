import json
import time

import httpx

from barmanager.ai import AiWorker, Enricher, apply_answer, clean_answer
from barmanager.db import Database

from .conftest import FakeLookup

CALLS = []
PROMPTS = []


def ollama_handler(request: httpx.Request) -> httpx.Response:
    CALLS.append(request.url.path)
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen3:4b"}]})
    if request.url.path == "/api/chat":
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        PROMPTS.append(prompt)
        if body["messages"][0]["content"].startswith("You identify"):
            ans = {"name": "Limoncello di Sorrento", "brand": "Limoncello", "sure": True} \
                if "8001500009892" in prompt else {"name": "Made Up Whisky", "brand": "Fake", "sure": True}
        elif "Apenshot" in prompt:
            ans = {"brand": "", "country": "", "category": "likeur", "also": [], "description": "",
                   "sure": False}
        else:
            ans = {"brand": "Glenfarclas", "country": "Scotland", "category": "whisky", "also": [],
                   "description": "Single malt Scotch whisky uit de Speyside.", "sure": True}
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(ans)}})
    return httpx.Response(404)


def setup(settings):
    settings.ollama_url = "http://ollama.test:11434"
    db = Database(settings.db_path)
    now = time.time()
    a = db.run("INSERT INTO products(barcode, name, brand, abv, created_at, updated_at, lookup_status) "
               "VALUES ('1', 'Glenfarclas 12 Years', '', 40, ?, ?, 'manual')", now, now)
    b = db.run("INSERT INTO products(barcode, name, created_at, updated_at, lookup_status) "
               "VALUES ('2', 'Apenshot', ?, ?, 'manual')", now, now)
    worker = AiWorker(db, settings, Enricher(settings, httpx.MockTransport(ollama_handler)), FakeLookup(settings))
    return db, worker, a, b


def test_fills_only_empty_fields(settings):
    db, worker, a, _ = setup(settings)
    assert [r["id"] for r in worker.todo()]
    assert worker.enricher.ensure_model()
    filled = worker.process(a)
    p = db.one("SELECT * FROM products WHERE id=?", a)
    assert set(filled) == {"brand", "country", "description", "category"}
    assert p["abv"] == 40
    assert p["country"] == "Scotland" and p["category"] == "whisky"
    assert p["ai_for"] == p["name"] and "AI" in p["source"]
    assert a not in [r["id"] for r in worker.todo()]


def test_unsure_answer_adds_no_facts(settings):
    db, worker, _, b = setup(settings)
    filled = worker.process(b)
    p = db.one("SELECT * FROM products WHERE id=?", b)
    assert filled == ["category"] and p["brand"] == "" and p["description"] == ""


def test_locked_category_is_kept(settings):
    db, worker, a, _ = setup(settings)
    db.run("UPDATE products SET category='rum', category_locked=1 WHERE id=?", a)
    worker.process(a)
    assert db.one("SELECT category FROM products WHERE id=?", a)["category"] == "rum"


def test_clean_answer():
    c = clean_answer({"brand": "onbekend", "country": " Ireland ", "category": "nope",
                      "also": ["whisky", "nope", "whisky"], "description": "x", "sure": 1})
    assert c["brand"] == "" and c["country"] == "Ireland"
    assert c["category"] == "overig" and c["also"] == [] and c["sure"] is True
    assert clean_answer({"category": "likeur", "also": ["whisky", "whisky"]})["also"] == ["whisky"]


def test_user_edit_removes_ai_mark(admin, settings):
    c, csrf = admin
    db = c.app.state.db
    now = time.time()
    pid = db.run("INSERT INTO products(barcode, name, created_at, updated_at, lookup_status) "
                 "VALUES ('9', 'Merk Test', ?, ?, 'manual')", now, now)
    apply_answer(db, pid, clean_answer({"brand": "Merk", "country": "France", "category": "brandy",
                                        "also": [], "description": "Cognac.", "sure": True}))
    page = c.get(f"/producten/{pid}").text
    assert page.count('class="ai-tag"') == 4
    c.post(f"/producten/{pid}", data={"csrf": csrf, "name": "Merk Test", "brand": "Ander merk", "category": "brandy",
                                      "country": "France", "abv": "40", "description": "Cognac."})
    p = db.one("SELECT * FROM products WHERE id=?", pid)
    assert p["brand"] == "Ander merk" and "brand" not in p["ai_fields"] and "country" in p["ai_fields"]


def test_brand_must_come_from_name(settings):
    db, worker, a, _ = setup(settings)
    filled = apply_answer(db, a, clean_answer({"brand": "Highland Park Distillery", "country": "Scotland",
                                               "category": "whisky", "description": "x", "sure": True}))
    assert "brand" not in filled and "country" in filled


def test_sources_are_given_to_the_model(settings):
    db, worker, a, _ = setup(settings)
    settings.searxng_url = "http://searxng.test"
    PROMPTS.clear()
    worker.process(a)
    prompt = PROMPTS[-1]
    assert "[Wikipedia]" in prompt and "Speyside distillery" in prompt
    assert "[Web search]" in prompt and "sherry cask" in prompt and "Rain." not in prompt


def test_description_tidy():
    d = clean_answer({"description": "Fruit-flavored whiskey liqueur. Produced since 1:00 PM. (247 characters)",
                      "category": "likeur"})["description"]
    assert d == "Fruit-flavored whiskey liqueur."
    d = clean_answer({"description": "Single malt from Orkney with honey notes. Aged in sherry casks with an"})
    assert d["description"] == "Single malt from Orkney with honey notes."


def test_identify_unknown_barcode_from_search(settings):
    db, worker, _, _ = setup(settings)
    settings.searxng_url = "http://searxng.test"
    now = time.time()
    pid = db.run("INSERT INTO products(barcode, name, created_at, updated_at, lookup_status) "
                 "VALUES ('8001500009892', 'Limoncello di Sorrento 70cl', ?, ?, 'guess')", now, now)
    worker.process(pid)
    p = db.one("SELECT * FROM products WHERE id=?", pid)
    assert p["name"] == "Limoncello di Sorrento" and "name" in p["ai_fields"]


def test_identify_rejects_name_not_in_results(settings):
    db, worker, _, _ = setup(settings)
    settings.searxng_url = "http://searxng.test"
    now = time.time()
    # Search results for this code exist in the fake only for 8001500009892; make some that do not match.
    pid = db.run("INSERT INTO products(barcode, name, created_at, updated_at, lookup_status) "
                 "VALUES ('8001500009892', 'Iets', ?, ?, 'guess')", now, now)
    worker.enricher.identify = lambda code, res: {"name": "Made Up Whisky", "brand": "Fake", "sure": True}
    worker.process(pid)
    p = db.one("SELECT * FROM products WHERE id=?", pid)
    assert p["name"] == "Iets" and "name" in p["ai_fields"]


def test_abv_only_when_in_sources_and_extra_categories(settings):
    db, worker, a, _ = setup(settings)
    db.run("UPDATE products SET abv=NULL, name='Southern Comfort', category='likeur' WHERE id=?", a)
    ans = clean_answer({"brand": "Southern Comfort", "country": "United States", "category": "likeur",
                        "description": "Fruit-flavoured whiskey liqueur from New Orleans.", "abv": 35,
                        "sure": True})
    filled = apply_answer(db, a, ans, "Southern Comfort Original 35% vol, 70cl")
    p = db.one("SELECT * FROM products WHERE id=?", a)
    assert p["abv"] == 35 and "abv" in filled
    assert "whisky" in p["extra_categories"] and "extra" in filled
    db.run("UPDATE products SET abv=NULL WHERE id=?", a)
    assert "abv" not in apply_answer(db, a, {**ans, "abv": 40.0}, "Southern Comfort 35% vol")


def test_country_in_english_and_no_soft_drink_with_alcohol(settings):
    from barmanager.ai import country_en
    assert country_en("norge") == "Norway" and country_en("Shqipëri") == "Albania"
    assert country_en("netherlands") == "Netherlands" and country_en("Scotland") == "Scotland"
    db, worker, a, _ = setup(settings)
    db.run("UPDATE products SET category='overig', abv=14.9, name='Kesbeke Zure bom shot' WHERE id=?", a)
    ans = clean_answer({"brand": "", "country": "", "category": "mixer", "also": [], "description": "Sour shot.",
                        "abv": 0, "sure": False})
    assert "category" not in apply_answer(db, a, ans)
