from barmanager.lookup import barcode_variants, clean_title, parse_abv, parse_volume
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
