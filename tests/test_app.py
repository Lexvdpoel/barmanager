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


def test_two_categories(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    pid = scan(c, csrf, lid, "1111111111116").json()["id"]
    c.post(f"/producten/{pid}", data={"csrf": csrf, "name": "Jim Beam Peach", "category": "overig"})
    p = c.get(f"/api/producten/{pid}").json()
    assert [x["key"] for x in p["categories"]] == ["whisky", "likeur"]
    assert "Jim Beam Peach" in c.get(f"/locaties/{lid}?cat=whisky").text
    assert "Jim Beam Peach" in c.get(f"/locaties/{lid}?cat=likeur").text
    page = c.get(f"/locaties/{lid}").text
    assert page.count("Jim Beam Peach</a>") == 2      # in the whisky and the liqueur group
    # Manually remove the second category.
    c.post(f"/producten/{pid}", data={"csrf": csrf, "name": "Jim Beam Peach", "category": "whisky"})
    assert [x["key"] for x in c.get(f"/api/producten/{pid}").json()["categories"]] == ["whisky"]
    c.post(f"/producten/{pid}", data={"csrf": csrf, "name": "Jim Beam Peach", "category": "whisky",
                                      "extra": ["likeur", "additief"]})
    keys = [x["key"] for x in c.get(f"/api/producten/{pid}").json()["categories"]]
    assert keys == ["whisky", "likeur", "additief"]
    assert "Alle <span>1</span>" in page


def test_transient_failure_is_retried(admin, client):
    c, csrf = admin
    lid = make_location(c, csrf)
    pid = scan(c, csrf, lid, "3333333333333").json()["id"]
    db = client.app.state.db
    p = db.one("SELECT * FROM products WHERE id=?", pid)
    assert p["lookup_status"] == "pending" and p["next_lookup_at"] and p["lookup_attempts"] == 1
    # After all retries it becomes 'not_found', with a daily re-check.
    db.run("UPDATE products SET lookup_attempts=4 WHERE id=?", pid)
    client.app.state.worker.process(pid)
    p = db.one("SELECT * FROM products WHERE id=?", pid)
    assert p["lookup_status"] == "not_found" and p["next_lookup_at"] > p["updated_at"] + 80000
    db.run("UPDATE products SET next_lookup_at=0 WHERE id=?", pid)
    assert client.app.state.worker.enqueue_due() == 1


def test_likorette_category():
    from barmanager.categories import classify
    assert classify(texts=["Apenshot - Likorette - 500ML"]) == "likeur"


def test_wikipedia_helps_categorise(admin):
    c, csrf = admin
    lid = make_location(c, csrf)
    pid = scan(c, csrf, lid, "1111111111116").json()["id"]
    c.post(f"/producten/{pid}", data={"csrf": csrf, "name": "Glenfarclas 12 Years", "category": "overig"})
    keys = [x["key"] for x in c.get(f"/api/producten/{pid}").json()["categories"]]
    assert keys[0] == "whisky"


def test_delete_products(admin, settings):
    c, csrf = admin
    lid = make_location(c, csrf)
    jw = scan(c, csrf, lid, "5000267014203").json()["id"]
    a = scan(c, csrf, lid, "1111111111116").json()["id"]
    b = scan(c, csrf, lid, "2222222222222").json()["id"]
    page = c.get("/producten").text
    assert "Johnnie Walker Red Label" in page and "Onbekend product" in page
    assert "Johnnie" in c.get("/producten?merk=Johnnie+Walker").text
    assert "Johnnie" not in c.get("/producten?staat=onbekend").text

    photo = Path(settings.image_dir) / Path(c.get(f"/api/producten/{jw}").json()["image"]).name
    assert photo.exists()
    r = c.post(f"/producten/{jw}/verwijderen", data={"csrf": csrf, "loc": str(lid)}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/locaties/{lid}")
    assert c.get(f"/api/producten/{jw}").status_code == 404
    assert not photo.exists()
    assert "Johnnie" not in c.get(f"/locaties/{lid}").text

    r = c.post("/producten-verwijderen", data={"csrf": csrf, "ids": [str(a), str(b)]}, follow_redirects=False)
    assert "2+product" in r.headers["location"]
    assert "Onbekend product" not in c.get("/producten").text


def test_delete_needs_access_to_all_locations(admin):
    c, csrf = admin
    lid = make_location(c, csrf, "Van admin")
    pid = scan(c, csrf, lid, "5000267014203").json()["id"]
    c.post("/gebruikers", data={"csrf": csrf, "username": "piet", "password": "wachtwoord1"})
    c.cookies.clear()
    csrf2 = login(c, "piet", "wachtwoord1")
    lid2 = make_location(c, csrf2, "Van piet")
    scan(c, csrf2, lid2, "5000267014203")
    own = scan(c, csrf2, lid2, "1111111111116").json()["id"]
    r = c.post(f"/producten/{pid}/verwijderen", data={"csrf": csrf2}, follow_redirects=False)
    assert "err=" in r.headers["location"]
    assert c.get(f"/api/producten/{pid}").status_code == 200
    # Admin's location is not visible in piet's catalogue, his own unknown product is deletable.
    page = c.get("/producten").text
    assert "Van admin" not in page
    r = c.post(f"/producten/{own}/verwijderen", data={"csrf": csrf2}, follow_redirects=False)
    assert "msg=" in r.headers["location"]
