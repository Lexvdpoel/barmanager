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
    if "/cgi/search.pl" in url:
        return httpx.Response(200, json={"products": []})
    if url.startswith("https://images.example.com/"):
        return httpx.Response(200, content=png_bytes(), headers={"content-type": "image/png"})
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
