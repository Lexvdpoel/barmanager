import io
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from barmanager import images
from barmanager.app import create_app
from barmanager.config import Settings
from barmanager.lookup import Lookup


def png_bytes(color=(200, 120, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (300, 400), color).save(buf, "PNG")
    return buf.getvalue()


OFF_PRODUCTS = {
    "5000267014203": {
        "product_name": "Johnnie Walker Red Label",
        "brands": "Johnnie Walker",
        "categories_tags": ["en:beverages", "en:alcoholic-beverages", "en:eaux-de-vie", "en:whisky",
                            "en:scotch-whisky"],
        "quantity": "70cl",
        "nutriments": {"alcohol_100g": 40},
        "image_front_url": "https://images.example.com/jw/front_en.10.400.jpg",
    },
}


def fake_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "/api/v2/product/3333333333333" in url:
        raise httpx.ConnectTimeout("timeout", request=request)
    m = re.search(r"/api/v2/product/(\w+)\.json", url)
    if m:
        p = OFF_PRODUCTS.get(m.group(1))
        if p:
            return httpx.Response(200, json={"status": 1, "product": p})
        return httpx.Response(404, json={"status": 0})
    if "upcitemdb.com/prod/trial/lookup" in url:
        if request.url.params.get("upc") == "8712345678906":
            return httpx.Response(200, json={"code": "OK", "items": [{
                "title": "Bombay Sapphire London Dry Gin 1L - Drinks Shop",
                "brand": "Bombay Sapphire", "category": "Food, Beverages & Tobacco > Beverages",
                "images": ["https://images.example.com/bombay.png"]}]})
        return httpx.Response(200, json={"code": "OK", "items": []})
    if "upcitemdb.com/prod/trial/search" in url:
        return httpx.Response(200, json={"items": [{"images": ["https://images.example.com/search.png"]}]})
    if "search.openfoodfacts.org/search" in url:
        return httpx.Response(200, json={"hits": [
            {"product_name": "Honey Roast Ham", "image_front_url": "https://images.example.com/ham.png"},
            {"product_name": "Bacardi Carta Blanca", "brands": ["Bacardi"],
             "image_front_url": "https://images.example.com/bacardi.png"}]})
    if "opengtindb.org" in url:
        if request.url.params.get("ean") == "4306188410409":
            body = "error=0\n---\nname=\ndetailname=Kirschlikör 0,7L\nvendor=Testmarke\nmaincat=Getränke\n---\n"
            return httpx.Response(200, content=body.encode("latin-1"))
        return httpx.Response(200, content=b"error=1\n---\n")
    if "upcitemdb.com" in url and request.url.params.get("upc") == "3333333333333":
        return httpx.Response(503)
    if "wikipedia.org" in url:
        q = request.url.params.get("gsrsearch", "")
        if "Glenfarclas" in q:
            return httpx.Response(200, json={"query": {"pages": {"1": {
                "title": "Glenfarclas", "index": 1, "description": "Scotch whisky distillery",
                "extract": "Glenfarclas is a Speyside distillery producing single malt Scotch whisky."}}}})
        return httpx.Response(200, json={"batchcomplete": True})
    if url.startswith("http://searxng.test/search"):
        q = request.url.params.get("q", "")
        if request.url.params.get("categories") == "images":
            return httpx.Response(200, json={"results": [
                {"title": "Glenfarclas 12 Years bottle", "img_src": "https://images.example.com/gf.png"},
                {"title": "Johnnie Walker Red Label 70cl", "img_src": "https://images.example.com/jw-web.png"},
                {"title": "Random ham", "img_src": "https://images.example.com/ham2.png"}]})
        if q == "8001500009892":
            return httpx.Response(200, json={"results": [
                {"title": "Unrelated page", "url": "https://x.example/1", "content": "nothing here"},
                {"title": "Limoncello di Sorrento 70cl - Shop", "url": "https://shop.example/p",
                 "content": "EAN 8001500009892. Limoncello 30% vol."}]})
        if "Glenfarclas" in q:
            return httpx.Response(200, json={"results": [
                {"title": "Glenfarclas 12 Year Old", "url": "https://w.example/gf",
                 "content": "Glenfarclas 12 is a Speyside single malt from Scotland, sherry cask matured."},
                {"title": "Weather in Speyside", "url": "https://w.example/w", "content": "Rain."}]})
        return httpx.Response(200, json={"results": []})
    if url.startswith("https://www.thecocktaildb.com/api/"):
        return cocktaildb(request)
    if url.startswith("https://images.example.com/"):
        return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})
    return httpx.Response(404)


def drink(i, name, ings, iba="", alcoholic="Alcoholic"):
    d = {"idDrink": i, "strDrink": name, "strCategory": "Cocktail", "strAlcoholic": alcoholic,
         "strGlass": "Cocktail glass", "strIBA": iba, "strInstructions": f"Make a {name}.",
         "strDrinkThumb": f"https://images.example.com/drink/{i}.jpg"}
    for n, (ing, measure) in enumerate(ings, 1):
        d[f"strIngredient{n}"], d[f"strMeasure{n}"] = ing, measure
    return d


DRINKS = {
    "1": drink("1", "Margarita", [("Tequila", "1 1/2 oz"), ("Triple sec", "1/2 oz"), ("Lime juice", "1 oz"),
                                  ("Salt", "")], iba="Contemporary Classics"),
    "2": drink("2", "Mojito", [("Light rum", "2-3 oz"), ("Lime", "Juice of 1"), ("Sugar", "2 tsp"),
                               ("Mint", "2-4"), ("Soda water", "")]),
    "3": drink("3", "Gin And Tonic", [("Gin", "2 oz"), ("Tonic water", "5 oz"), ("Lime", "")]),
    "4": drink("4", "Rob Roy", [("Scotch", "1 1/2 oz"), ("Sweet Vermouth", "3/4 oz"), ("Angostura bitters", "dash")]),
}


def cocktaildb(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, request.url.params
    if path.endswith("/search.php"):
        f = params.get("f", "")
        hits = [d for d in DRINKS.values() if d["strDrink"].lower().startswith(f) and d["idDrink"] != "3"]
        return httpx.Response(200, json={"drinks": hits or None})
    if path.endswith("/list.php"):
        if params.get("c") == "list":
            return httpx.Response(200, json={"drinks": [{"strCategory": "Cocktail"}]})
        return httpx.Response(200, json={"drinks": []})
    if path.endswith("/filter.php"):
        return httpx.Response(200, json={"drinks": [{"idDrink": i, "strDrink": d["strDrink"]} for i, d in DRINKS.items()]})
    if path.endswith("/lookup.php"):
        d = DRINKS.get(params.get("i", ""))
        return httpx.Response(200, json={"drinks": [d] if d else None})
    return httpx.Response(404)


class FakeLookup(Lookup):
    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(fake_handler))


@pytest.fixture
def settings(tmp_path):
    return Settings(db_path=str(tmp_path / "bm.db"), image_dir=str(tmp_path / "images"),
                    admin_username="admin", admin_password="geheim123", lookup_inline=True)


@pytest.fixture
def client(settings, monkeypatch):
    monkeypatch.setattr(images, "is_public_url", lambda url: url.startswith("https://"))
    app = create_app(settings, lookup=FakeLookup(settings))
    with TestClient(app) as c:
        yield c


def login(c: TestClient, username="admin", password="geheim123") -> str:
    r = c.post("/login", data={"username": username, "password": password}, follow_redirects=False)
    assert r.status_code == 303, r.text
    page = c.get("/locaties").text
    return re.search(r'data-csrf="([^"]+)"', page).group(1)


@pytest.fixture
def admin(client):
    return client, login(client)
