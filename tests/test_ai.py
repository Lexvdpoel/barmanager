import json
import time

import httpx

from barmanager.ai import AiWorker, Enricher, apply_answer, clean_answer
from barmanager.db import Database

from .conftest import FakeLookup

CALLS = []


def ollama_handler(request: httpx.Request) -> httpx.Response:
    CALLS.append(request.url.path)
    if request.url.path == "/api/tags":
        return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})
    if request.url.path == "/api/chat":
        body = json.loads(request.content)
        prompt = body["messages"][-1]["content"]
        if "Apenshot" in prompt:
            ans = {"brand": "", "country": "", "category": "likeur", "also": [], "description": "",
                   "sure": False}
        else:
            ans = {"brand": "Glenfarclas", "country": "Schotland", "category": "whisky", "also": [],
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
    assert p["country"] == "Schotland" and p["category"] == "whisky"
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
    c = clean_answer({"brand": "onbekend", "country": " Ierland ", "category": "nope",
                      "also": ["whisky", "nope", "whisky"], "description": "x", "sure": 1})
    assert c["brand"] == "" and c["country"] == "Ierland"
    assert c["category"] == "overig" and c["also"] == [] and c["sure"] is True
    assert clean_answer({"category": "likeur", "also": ["whisky", "whisky"]})["also"] == ["whisky"]


def test_user_edit_removes_ai_mark(admin, settings):
    c, csrf = admin
    db = c.app.state.db
    now = time.time()
    pid = db.run("INSERT INTO products(barcode, name, created_at, updated_at, lookup_status) "
                 "VALUES ('9', 'Merk Test', ?, ?, 'manual')", now, now)
    apply_answer(db, pid, clean_answer({"brand": "Merk", "country": "Frankrijk", "category": "brandy",
                                        "also": [], "description": "Cognac.", "sure": True}))
    page = c.get(f"/producten/{pid}").text
    assert page.count('class="ai-tag"') == 4
    c.post(f"/producten/{pid}", data={"csrf": csrf, "name": "Merk Test", "brand": "Ander merk", "category": "brandy",
                                      "country": "Frankrijk", "abv": "40", "description": "Cognac."})
    p = db.one("SELECT * FROM products WHERE id=?", pid)
    assert p["brand"] == "Ander merk" and "brand" not in p["ai_fields"] and "country" in p["ai_fields"]


def test_brand_must_come_from_name(settings):
    db, worker, a, _ = setup(settings)
    filled = apply_answer(db, a, clean_answer({"brand": "Highland Park Distillery", "country": "Schotland",
                                               "category": "whisky", "description": "x", "sure": True}))
    assert "brand" not in filled and "country" in filled
