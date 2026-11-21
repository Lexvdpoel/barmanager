from barmanager.lookup import barcode_variants, clean_title, name_matches, parse_abv, parse_volume
from barmanager.service import normalize_barcode

from .conftest import FakeLookup


def test_parse_volume():
    assert parse_volume("70cl") == 700
    assert parse_volume("0,7 l") == 700
    assert parse_volume("1L") == 1000
    assert parse_volume("330 ml") == 330
    assert parse_volume("geen") is None


def test_parse_abv():
    assert parse_abv("Gin 47,3% vol") == 47.3
    assert parse_abv("100% agave") is None


def test_clean_title():
    assert clean_title("Johnnie Walker Red Label 70cl - Drankdozijn") == "Johnnie Walker Red Label 70cl"
    assert clean_title("EAN 8712345678906 | Gall & Gall", "8712345678906") == ""


def test_barcodes():
    assert normalize_barcode(" 012345678905 ") == "0012345678905"
    assert barcode_variants("0012345678905") == ["0012345678905", "012345678905"]


def test_off_lookup(settings):
    res = FakeLookup(settings).lookup("5000267014203")
    assert res.found and res.name == "Johnnie Walker Red Label"
    assert res.volume_ml == 700 and res.abv == 40
    assert res.image_urls[0].endswith(".10.full.jpg")


def test_upcitemdb_lookup(settings):
    res = FakeLookup(settings).lookup("8712345678906")
    assert res.name == "Bombay Sapphire London Dry Gin 1L"
    assert res.volume_ml == 1000
    assert res.sources == ["UPCitemdb"]


def test_unknown(settings):
    assert not FakeLookup(settings).lookup("1111111111116").found


def test_name_matches():
    assert name_matches("Jim Beam Peach", "Jim Beam Peach Bourbon")
    assert not name_matches("Jim Beam Peach", "Jim Beam Original")
    assert not name_matches("Tullamore Honey 70cl", "Tullamore D.E.W")


def test_opengtindb(settings):
    settings.opengtindb_id = "123"
    res = FakeLookup(settings).lookup("4306188410409")
    assert res.name == "Kirschlikör 0,7L" and res.brand == "Testmarke"
    assert res.volume_ml == 700 and "OpenGTINDB" in res.sources


def test_transient_failure(settings):
    res = FakeLookup(settings).lookup("3333333333333")
    assert not res.found and res.transient
    assert not FakeLookup(settings).lookup("1111111111116").transient


def test_image_search_filters_wrong_products(settings):
    urls = FakeLookup(settings).image_search("Bacardi Carta Blanca")
    assert any("bacardi" in u for u in urls) and not any("ham" in u for u in urls)


def test_wikipedia_hint(settings):
    texts = FakeLookup(settings).wikipedia_texts("Glenfarclas 12 Years", "")
    assert any("Scotch whisky" in t for t in texts)
    assert FakeLookup(settings).wikipedia_texts("Iets onbekends", "") == []


def test_searxng_barcode_and_texts(settings):
    settings.searxng_url = "http://searxng.test"
    lk = FakeLookup(settings)
    res = lk.lookup("8001500009892")
    assert res.name == "Limoncello di Sorrento 70cl" and res.guessed and res.abv == 30
    texts = lk.web_texts("Glenfarclas 12 Years", "")
    assert len(texts) == 1 and "Speyside" in texts[0]
    urls = lk.image_search("Glenfarclas 12 Years")
    assert "https://images.example.com/gf.png" in urls and not any("ham2" in u for u in urls)
