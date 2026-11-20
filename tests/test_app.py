from pathlib import Path

from .conftest import login, png_bytes


def scan(c, csrf, lid, code, mode="add", qty=1):
    return c.post(f"/api/locaties/{lid}/scan", json={"barcode": code, "mode": mode, "qty": qty},
                  headers={"X-CSRF-Token": csrf})


def make_location(c, csrf, name="Thuisbar"):
    r = c.post("/locaties", data={"csrf": csrf, "name": name}, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].split("/locaties/")[1].split("?")[0])


def test_login_required(client):
    r = client.get("/locaties", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/healthz").json()["ok"]


def test_wrong_password(client):
    r = client.post("/login", data={"username": "admin", "password": "fout"})
    assert "Onjuiste" in r.text


def test_scan_flow(admin, settings):
    c, csrf = admin
    lid = make_location(c, csrf)

    r = scan(c, csrf, lid, "5000267014203")
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["created"] and p["quantity"] == 1

    # Lookup ran inline: name, category and photo are filled in.
    p = c.get(f"/api/producten/{p['id']}?loc={lid}").json()
    assert p["name"] == "Johnnie Walker Red Label"
    assert p["category"] == "whisky" and p["status"] == "found"
    assert p["image"] and (Path(settings.image_dir) / Path(p["image"]).name).exists()
    assert c.get(p["image"]).status_code == 200

    p = scan(c, csrf, lid, "5000267014203", qty=2).json()
    assert not p["created"] and p["quantity"] == 3

    p = scan(c, csrf, lid, "5000267014203", mode="remove").json()
    assert p["quantity"] == 2 and p["applied"] == -1

    gin = scan(c, csrf, lid, "8712345678906").json()
    unknown = scan(c, csrf, lid, "1111111111116").json()
    assert c.get(f"/api/producten/{gin['id']}").json()["category"] == "gin"
    assert c.get(f"/api/producten/{unknown['id']}").json()["status"] == "not_found"

    page = c.get(f"/locaties/{lid}").text
    assert "Johnnie Walker Red Label" in page and "Onbekend product" in page
    assert "Whisky" in page and "Gin" in page

    filtered = c.get(f"/locaties/{lid}?cat=gin").text
    assert "Bombay" in filtered and "Johnnie Walker Red Label" not in filtered
    searched = c.get(f"/locaties/{lid}?q=johnnie").text
    assert "Johnnie Walker Red Label" in searched and "Bombay" not in searched

    csv = c.get(f"/locaties/{lid}/export.csv").text
    assert "Johnnie Walker Red Label" in csv and ";2;" in csv


def test_remove_unknown_barcode(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    assert scan(c, csrf, lid, "9999999999994", mode="remove").status_code == 404


def test_edit_unknown_product(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    pid = scan(c, csrf, lid, "1111111111116").json()["id"]
    r = c.post(f"/producten/{pid}", data={"csrf": csrf, "loc": str(lid), "name": "Bacardi Carta Blanca",
                                          "brand": "", "category": "overig"}, follow_redirects=False)
    assert r.status_code == 303
    p = c.get(f"/api/producten/{pid}").json()
    assert p["category"] == "rum" and p["status"] == "manual"
    assert p["images"] == 1        # photo search ran after the name was filled in
    # A manually chosen category sticks.
    c.post(f"/producten/{pid}", data={"csrf": csrf, "name": "Bacardi Carta Blanca", "category": "likeur"})
    assert c.get(f"/api/producten/{pid}").json()["category"] == "likeur"
    assert c.get(f"/producten/{pid}?loc={lid}").status_code == 200


def test_adjust_and_counts(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    pid = scan(c, csrf, lid, "5000267014203").json()["id"]
    r = c.post(f"/api/locaties/{lid}/items/{pid}/aanpassen", json={"delta": -5},
               headers={"X-CSRF-Token": csrf})
    assert r.json()["quantity"] == 0
    c.post(f"/locaties/{lid}/items/{pid}",
           data={"csrf": csrf, "quantity": "4", "min_quantity": "5", "notes": "kast"})
    assert "bijna op" in c.get("/locaties").text
    assert "Johnnie" in c.get(f"/locaties/{lid}?staat=bijna").text


def test_csrf_required(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    assert scan(c, "fout", lid, "5000267014203").status_code == 403
    assert c.post("/locaties", data={"csrf": "fout", "name": "x"}).status_code == 403


def test_access_between_users(admin):
    c, csrf = admin
    lid = make_location(c, csrf, "Van admin")
    c.post("/gebruikers", data={"csrf": csrf, "username": "piet", "password": "wachtwoord1"})
    c.cookies.clear()
    csrf2 = login(c, "piet", "wachtwoord1")
    assert c.get(f"/locaties/{lid}").status_code == 404
    assert scan(c, csrf2, lid, "5000267014203").status_code == 404
    assert c.get("/gebruikers").status_code == 403

    c.cookies.clear()
    csrf = login(c)
    c.post(f"/locaties/{lid}/leden", data={"csrf": csrf, "username": "piet"})
    c.cookies.clear()
    csrf2 = login(c, "piet", "wachtwoord1")
    assert c.get(f"/locaties/{lid}").status_code == 200
    assert scan(c, csrf2, lid, "5000267014203").status_code == 200
    # Members cannot delete the location.
    r = c.post(f"/locaties/{lid}/verwijderen", data={"csrf": csrf2, "confirm": "Van admin"})
    assert r.status_code == 403


def test_photo_upload(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    pid = scan(c, csrf, lid, "1111111111116").json()["id"]
    r = c.post(f"/producten/{pid}/fotos", data={"csrf": csrf, "loc": str(lid)},
               files={"file": ("foto.png", png_bytes(), "image/png")}, follow_redirects=False)
    assert "Foto+toegevoegd" in r.headers["location"]
    r = c.post(f"/producten/{pid}/fotos", data={"csrf": csrf},
               files={"file": ("x.png", b"<svg/>", "image/png")}, follow_redirects=False)
    assert "err=" in r.headers["location"]
