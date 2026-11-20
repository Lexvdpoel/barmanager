import pytest

from barmanager.categories import classify, classify_all


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


@pytest.mark.parametrize("name,brand,expected", [
    ("Jim Beam Peach", "", ["whisky", "likeur"]),
    ("Tennessee Honey", "Jack Daniel's", ["whisky", "likeur"]),
    ("Johnnie Walker Red Label", "", ["whisky"]),
    ("Baileys Irish Cream Liqueur with Irish whiskey", "", ["likeur", "whisky"]),
    ("Angostura Aromatic Bitters", "", ["additief"]),
    ("Luxardo Maraschino Cherries", "", ["additief"]),
    ("Taylor's Fine Ruby Port", "", ["port", "wijn"]),
    ("Bacardi Carta Blanca", "", ["rum"]),
])
def test_multi(name, brand, expected):
    assert classify_all(texts=[name], brand=brand) == expected


def test_generic_tags_no_second_category():
    assert classify_all(tags=["en:eaux-de-vie", "en:whisky", "en:scotch-whisky"]) == ["whisky"]
