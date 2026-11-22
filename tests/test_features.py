import re
import time

from fastapi.testclient import TestClient

from barmanager import bar, cocktails, service
from barmanager.db import Database

from .conftest import FakeLookup


def make_location(c, csrf, name="Thuisbar") -> int:
    r = c.post("/locaties", data={"csrf": csrf, "name": name}, follow_redirects=False)
    return int(r.headers["location"].split("?")[0].rsplit("/", 1)[-1])


def add_product(db, name, category, qty, lid, brand="", country="", volume=700, extra=""):
    now = time.time()
    pid = db.run("INSERT INTO products(barcode, name, brand, country, category, extra_categories, volume_ml, "
                 "lookup_status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 f"99{abs(hash(name)) % 10**10:010d}", name, brand, country, category, extra, volume, "manual",
                 now, now)
    service.adjust(db, lid, pid, qty, None, "scan")
    return pid


def synced(c) -> Database:
    db = c.app.state.db
    c.app.state.cocktail_sync.now()
    return db


def test_measures_in_cl():
    assert cocktails.measure_nl("1 1/2 oz") == "4,5 cl"
    assert cocktails.measure_nl("3/4 oz ") == "2,5 cl"      # 2.25 rounded to half cl
    assert cocktails.measure_nl("2-3 oz") == "6–9 cl"
    assert cocktails.measure_nl("1 shot") == "4 cl"
    assert cocktails.measure_nl("2 tsp") == "2 tl"
    assert cocktails.measure_nl("Juice of 1") == "Juice of 1"


def test_sync_fetches_all_cocktails_and_photos(admin):
    c, csrf = admin
    db = synced(c)
    rows = db.all("SELECT * FROM cocktails ORDER BY id")
    # Gin And Tonic is not in the (capped) letter search, only via filter + lookup.
    assert [r["name"] for r in rows] == ["Margarita", "Mojito", "Gin And Tonic", "Rob Roy"]
    assert all(r["image"].startswith("cocktails/") for r in rows)
    assert db.one("SELECT COUNT(*) AS n FROM cocktail_ingredients WHERE cocktail_id='1'")["n"] == 4
    assert cocktails.last_sync(db)


def test_what_can_i_make(admin):
    c, csrf = admin
    db = synced(c)
    lid = make_location(c, csrf)
    add_product(db, "Olmeca Tequila Blanco", "tequila", 1, lid)
    add_product(db, "Cointreau", "likeur", 1, lid)
    add_product(db, "Bombay Sapphire", "gin", 1, lid)
    add_product(db, "Highland Park 12 Year Old", "whisky", 1, lid, country="Scotland")
    res = {x["name"]: x for x in cocktails.evaluate_all(db, cocktails.stock(db, [lid]))}
    assert res["Margarita"]["can"] and res["Gin And Tonic"]["can"]
    assert res["Mojito"]["missing"] == ["Light rum"]
    assert res["Rob Roy"]["missing"] == ["Sweet Vermouth", "Angostura bitters"]
    # Mixers must be in stock when the user says so.
    strict = {x["name"]: x for x in cocktails.evaluate_all(db, cocktails.stock(db, [lid]), assume_mixers=False)}
    assert strict["Gin And Tonic"]["missing"] == ["Tonic water"]

    page = c.get(f"/cocktails?loc={lid}").text
    assert "Margarita" in page and "Gin And Tonic" in page and "Mojito" not in page
    page = c.get(f"/cocktails?loc={lid}&toon=bijna").text
    assert "Mojito" in page and "Light rum" in page
    page = c.get(f"/cocktails/1?loc={lid}").text
    assert "4,5 cl" in page and "Cointreau" in page and "Olmeca" in page


def test_substitute_and_manual_link(admin):
    c, csrf = admin
    db = synced(c)
    lid = make_location(c, csrf)
    jw = add_product(db, "Glen Moray", "whisky", 1, lid, country="Scotland")
    add_product(db, "Martini Rosso", "aperitief", 1, lid)
    bitters = add_product(db, "Huisgemaakte bitter", "overig", 1, lid)
    m = cocktails.Matcher(db, cocktails.stock(db, [lid]))
    assert m.resolve("Scotch").state == "ok" and m.resolve("Bourbon").state == "vervanger"
    assert m.resolve("Sweet Vermouth").state == "ok"
    assert m.resolve("Angostura bitters").state == "mist"
    # Link the unknown bottle to the ingredient by hand.
    page = c.get(f"/cocktails-ingredient?naam=Angostura+bitters&loc={lid}").text
    assert "Huisgemaakte bitter" in page
    c.post("/cocktails-ingredient", data={"csrf": csrf, "naam": "Angostura bitters", "products": [bitters],
                                          "shown": [bitters, jw], "loc": lid})
    res = {x["name"]: x for x in cocktails.evaluate_all(db, cocktails.stock(db, [lid]))}
    assert res["Rob Roy"]["can"]
    assert "cocktail" in c.get(f"/producten/{jw}?loc={lid}").text
    # "Always in the house"
    c.post("/cocktails-ingredient", data={"csrf": csrf, "naam": "Light rum", "always": "1", "loc": lid})
    res = {x["name"]: x for x in cocktails.evaluate_all(db, cocktails.stock(db, [lid]))}
    assert res["Mojito"]["can"]


def test_fill_levels_shopping_and_stats(admin):
    c, csrf = admin
    db = c.app.state.db
    lid = make_location(c, csrf)
    pid = add_product(db, "Baileys Original Irish Cream", "likeur", 2, lid)
    # Tracking off: no level panel, but a shopping-list button.
    page = c.get(f"/producten/{pid}?loc={lid}").text
    assert "Geopende fles" not in page and "Zet op boodschappenlijst" in page
    c.post(f"/locaties/{lid}/bijhouden", data={"csrf": csrf, "aan": "1"})
    page = c.get(f"/producten/{pid}?loc={lid}").text
    assert "Geopende fles" in page and "Zet op boodschappenlijst" not in page

    c.post(f"/locaties/{lid}/items/{pid}", data={"csrf": csrf, "quantity": "2", "min_quantity": "0", "notes": "",
                                                 "price": "20,00", "bought_at": "2026-11-01", "shop": "Gall"})
    c.post(f"/locaties/{lid}/items/{pid}/niveau", data={"csrf": csrf, "level": "50"})
    item = db.one("SELECT * FROM inventory WHERE location_id=? AND product_id=?", lid, pid)
    assert item["open_level"] == 50 and item["price"] == 20 and item["shop"] == "Gall"
    assert service.location_value(db, lid) == 30.0         # 1 full + half a bottle
    log = db.one("SELECT * FROM level_log WHERE event='open'")
    assert log["ml"] == 350 and log["value"] == 10

    # Last bottle almost empty -> on the shopping list.
    c.post(f"/locaties/{lid}/items/{pid}/niveau", data={"csrf": csrf, "level": "0"})
    assert db.one("SELECT quantity, open_level FROM inventory WHERE product_id=?", pid)["quantity"] == 1
    c.post(f"/locaties/{lid}/items/{pid}/niveau", data={"csrf": csrf, "level": "10"})
    user = db.one("SELECT * FROM users WHERE username='admin'")
    lst = bar.shopping_list(db, user)
    assert lst["count"] == 1 and "bijna leeg" in lst["groups"][0][1][0]["reason"]
    assert "Baileys" in c.get("/boodschappen").text

    # Opened long ago: past its shelf life (cream liqueur ~6 months).
    db.run("UPDATE inventory SET opened_at=? WHERE product_id=?", time.time() - 200 * 86400, pid)
    assert "over datum" in c.get(f"/locaties/{lid}").text

    stats = bar.stats(db, lid, 6)
    # 350 ml (opened at 50%) + 350 ml (finished) + 630 ml (next bottle opened, 10% left)
    assert stats["bottles"] == 1 and round(stats["liters"], 2) == 1.33
    assert stats["top"][0]["value"] == 10 + 10 + 18
    assert "Statistieken" in c.get(f"/locaties/{lid}/statistieken").text

    # Scanning a bottle in takes it off the list.
    service.adjust(db, lid, pid, 1, None, "scan")
    assert bar.shopping_list(db, user)["count"] == 0


def test_manual_shopping_list(admin):
    c, csrf = admin
    db = c.app.state.db
    lid = make_location(c, csrf)
    pid = add_product(db, "Hendrick's Gin", "gin", 1, lid)
    c.post(f"/producten/{pid}/boodschappen", data={"csrf": csrf, "loc": lid})
    assert "Staat op de" in c.get(f"/producten/{pid}?loc={lid}").text
    c.post("/boodschappen", data={"csrf": csrf, "name": "Limoenen"})
    page = c.get("/boodschappen").text
    assert "Hendrick" in page and "Limoenen" in page
    user = db.one("SELECT * FROM users WHERE username='admin'")
    assert bar.shopping_list(db, user)["count"] == 2
    # Below minimum: automatic entry; removing it hides it until stock goes up.
    pid2 = add_product(db, "Campari", "bitter", 1, lid)
    db.run("UPDATE inventory SET min_quantity=2 WHERE product_id=?", pid2)
    assert bar.shopping_list(db, user)["count"] == 3
    c.post("/boodschappen/weg", data={"csrf": csrf, "loc": lid, "pid": pid2})
    assert bar.shopping_list(db, user)["count"] == 2


def test_public_bar_menu(admin, settings):
    c, csrf = admin
    db = synced(c)
    lid = make_location(c, csrf, "Kelderbar")
    add_product(db, "Bombay Sapphire", "gin", 1, lid)
    c.post(f"/locaties/{lid}/barkaart", data={"csrf": csrf, "actie": "aan"})
    token = db.one("SELECT menu_token FROM locations WHERE id=?", lid)["menu_token"]
    assert token
    page = c.get(f"/locaties/{lid}/instellingen").text
    assert f"/kaart/{token}" in page
    assert c.get(f"/locaties/{lid}/barkaart.svg").text.lstrip().startswith("<?xml")
    with TestClient(c.app) as guest:                       # not logged in
        page = guest.get(f"/kaart/{token}").text
        assert "Kelderbar" in page and "Bombay Sapphire" in page and "Gin And Tonic" in page
        assert "Uitloggen" not in page
        assert "Make a Gin And Tonic" in guest.get(f"/kaart/{token}/cocktail/3").text
        c.post(f"/locaties/{lid}/barkaart", data={"csrf": csrf, "actie": "uit"})
        assert guest.get(f"/kaart/{token}").status_code == 404


def test_shelf_life_defaults():
    def p(name, cat, extra="", desc=""):
        return {"name": name, "brand": "", "description": desc, "category": cat, "extra_categories": extra,
                "shelf_days": None}
    assert cocktails.default_shelf_days(p("Baileys Original Irish Cream", "likeur")) == 180
    assert cocktails.default_shelf_days(p("Cointreau", "likeur")) is None
    assert cocktails.default_shelf_days(p("Martini Rosso", "aperitief")) == 60
    assert cocktails.default_shelf_days(p("Highland Park", "whisky")) is None


def test_csv_has_value_columns(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    pid = add_product(c.app.state.db, "Aperol", "aperitief", 2, lid)
    c.post(f"/locaties/{lid}/items/{pid}", data={"csrf": csrf, "quantity": "2", "price": "15,5"})
    text = c.get(f"/locaties/{lid}/export.csv").text
    assert "prijs_per_fles" in text and "15,50" in text and "31,00" in text
    assert re.search(r"waarde € 31,00", c.get(f"/locaties/{lid}").text)


def test_dark_rum_is_not_light_rum(settings):
    db = Database(settings.db_path)
    db.run("INSERT INTO users(username, password_hash, created_at) VALUES ('a', 'x', 0)")
    lid = db.run("INSERT INTO locations(name, owner_id, created_at) VALUES ('bar', 1, 0)")
    add_product(db, "Havana Club Añejo 7 Años", "rum", 1, lid, brand="Havana Club")
    m = cocktails.Matcher(db, cocktails.stock(db, [lid]))
    assert m.resolve("Dark rum").state == "ok"
    assert m.resolve("Light rum").state == "vervanger" and m.resolve("White rum").state == "vervanger"
    assert m.resolve("Rum").state == "ok"
    add_product(db, "Bacardi Carta Blanca", "rum", 1, lid, brand="Bacardi")
    m = cocktails.Matcher(db, cocktails.stock(db, [lid]))
    assert [p["name"] for p in m.resolve("Light rum").products] == ["Bacardi Carta Blanca"]
    assert [p["name"] for p in m.resolve("Dark rum").products] == ["Havana Club Añejo 7 Años"]
