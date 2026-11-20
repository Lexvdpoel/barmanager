import pytest

from barmanager.categories import classify


@pytest.mark.parametrize("tags,expected", [
    (["en:alcoholic-beverages", "en:eaux-de-vie", "en:whisky", "en:scotch-whisky"], "whisky"),
    (["en:liqueurs", "en:cream-liqueurs"], "likeur"),
    (["en:wines", "en:fortified-wines", "en:port-wines"], "port"),
    (["en:wines", "en:sparkling-wines", "en:champagnes"], "bubbels"),
    (["en:beers", "en:lagers"], "bier"),
    (["en:sodas", "en:tonic-waters"], "mixer"),
    (["en:gins"], "gin"),
    (["nl:jenevers"], "jenever"),
])
def test_tags(tags, expected):
    assert classify(tags=tags) == expected


@pytest.mark.parametrize("name,brand,expected", [
    ("Bacardi Carta Blanca", "", "rum"),
    ("Red Label", "Johnnie Walker", "whisky"),
    ("Hendrick's Gin 70cl", "", "gin"),
    ("Fever-Tree Premium Indian Tonic Water", "", "mixer"),
    ("Ginger Beer", "Fentimans", "mixer"),
    ("Baileys Original Irish Cream", "", "likeur"),
    ("Aperol", "", "aperitief"),
    ("Campari Bitter", "", "bitter"),
    ("Jägermeister", "", "bitter"),
    ("Hooghoudt Jonge Graanjenever", "", "jenever"),
    ("Absolut Vodka", "", "vodka"),
    ("Hennessy V.S", "", "brandy"),
    ("Don Julio Blanco", "", "tequila"),
    ("Monin Siroop Vanille", "", "siroop"),
    ("Heineken", "", "bier"),
    ("Iets heel anders", "", "overig"),
])
def test_names(name, brand, expected):
    assert classify(texts=[name], brand=brand) == expected


def test_web_texts_vote():
    texts = ["Glenfarclas 12 years single malt whisky", "Single malt scotch whisky 70cl",
             "Shop: whisky, rum, gin"]
    assert classify(web_texts=texts) == "whisky"
