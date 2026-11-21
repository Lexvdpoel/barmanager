import csv
import io
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import __version__, categories, images, service
from .auth import (check_csrf, create_session, current_user, db_of, logout, require_admin,
                   require_user, set_cookie)
from .security import LoginLimiter, hash_password, verify_password

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(
    version=__version__,
    CATEGORIES=categories.CATEGORIES,
    cat_label=categories.label,
    cat_color=lambda k: categories.COLORS.get(k, categories.COLORS["overig"]),
    stock_state=service.stock_state,
)
limiter = LoginLimiter()

SORTS = {"categorie": "Categorie", "naam": "Naam", "aantal": "Aantal", "recent": "Laatst gewijzigd"}
STATES = {"": "Alles", "voorraad": "Op voorraad", "bijna": "Bijna op", "op": "Op"}


# ---- helpers ------------------------------------------------------------------------------
def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("csrf", getattr(request.state, "csrf", ""))
    ctx.setdefault("msg", request.query_params.get("msg"))
    ctx.setdefault("err", request.query_params.get("err"))
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def redirect(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("msg", msg), ("err", err)) if v}
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    return RedirectResponse(url, status_code=303)


def get_location(request: Request, lid: int, user):
    db = db_of(request)
    loc = db.one("SELECT * FROM locations WHERE id=?", lid)
    if not loc:
        raise HTTPException(404, "Locatie niet gevonden")
    if user["is_admin"] or loc["owner_id"] == user["id"]:
        return loc
    if db.one("SELECT 1 FROM location_members WHERE location_id=? AND user_id=?", lid, user["id"]):
        return loc
    raise HTTPException(404, "Locatie niet gevonden")


def can_manage(loc, user) -> bool:
    return bool(user["is_admin"] or loc["owner_id"] == user["id"])


def get_product(request: Request, pid: int):
    p = db_of(request).one("SELECT * FROM products WHERE id=?", pid)
    if not p:
        raise HTTPException(404, "Product niet gevonden")
    return p


def safe_back(url: str | None, default: str) -> str:
    return url if url and url.startswith("/") and not url.startswith("//") else default


def _int(value, default=None, lo=None, hi=None):
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


def _float(value):
    try:
        v = float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None
    return v


# ---- login ----------------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return redirect("/")
    return render(request, "login.html", user=None)


@router.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form("")):
    key = (request.client.host if request.client else "?") + ":" + username.lower()
    if limiter.blocked(key):
        return render(request, "login.html", user=None, username=username,
                      err="Te veel mislukte pogingen, probeer het over een paar minuten opnieuw.")
    row = db_of(request).one("SELECT * FROM users WHERE username=?", username.strip())
    if not row or not verify_password(password, row["password_hash"]):
        limiter.fail(key)
        return render(request, "login.html", user=None, username=username,
                      err="Onjuiste gebruikersnaam of wachtwoord.")
    limiter.reset(key)
    resp = redirect("/")
    set_cookie(request, resp, create_session(request, row["id"]))
    return resp


@router.post("/logout")
def do_logout(request: Request, user=Depends(require_user), csrf: str = Form("")):
    check_csrf(request, csrf)
    logout(request)
    resp = redirect("/login")
    resp.delete_cookie("bm_session", path="/")
    return resp


# ---- locations ---------------------------------------------------------------------------------
@router.get("/")
def home(request: Request, user=Depends(require_user)):
    return redirect("/locaties")


@router.get("/locaties", response_class=HTMLResponse)
def locations(request: Request, user=Depends(require_user)):
    db = db_of(request)
    if user["is_admin"]:
        locs = db.all("SELECT l.*, u.username AS owner FROM locations l JOIN users u ON u.id=l.owner_id "
                      "ORDER BY l.name COLLATE NOCASE")
    else:
        locs = db.all("""SELECT l.*, u.username AS owner FROM locations l JOIN users u ON u.id=l.owner_id
                         WHERE l.owner_id=? OR l.id IN (SELECT location_id FROM location_members WHERE user_id=?)
                         ORDER BY l.name COLLATE NOCASE""", user["id"], user["id"])
    stats = {r["location_id"]: r for r in db.all("""
        SELECT location_id, COUNT(*) AS products, COALESCE(SUM(quantity),0) AS bottles,
               SUM(CASE WHEN quantity <= 0 THEN 1 ELSE 0 END) AS empty,
               SUM(CASE WHEN quantity > 0 AND min_quantity > 0 AND quantity <= min_quantity
                   THEN 1 ELSE 0 END) AS low
        FROM inventory GROUP BY location_id""")}
    return render(request, "locations.html", locations=locs, stats=stats)


@router.post("/locaties")
def create_location(request: Request, user=Depends(require_user), csrf: str = Form(""),
                    name: str = Form(""), description: str = Form("")):
    check_csrf(request, csrf)
    name = name.strip()[:100]
    if not name:
        return redirect("/locaties", err="Geef de locatie een naam.")
    lid = db_of(request).run("INSERT INTO locations(name, description, owner_id, created_at) VALUES (?,?,?,?)",
                             name, description.strip()[:500], user["id"], time.time())
    return redirect(f"/locaties/{lid}", msg=f"Locatie '{name}' aangemaakt. Begin met scannen!")


def _filtered_items(request: Request, lid: int, q: str, cat: str, state: str, sort: str):
    items = service.inventory_rows(db_of(request), lid)
    for it in items:
        it["state"] = service.stock_state(it)
    if q:
        terms = categories.normalize(q).split()
        def hit(it):
            hay = categories.normalize(" ".join([it["name"], it["brand"], it["barcode"], it["notes"],
                                                 *[categories.label(c) for c in it["cats"]]]))
            return all(t in hay for t in terms)
        items = [it for it in items if hit(it)]
    if state == "voorraad":
        items = [it for it in items if it["quantity"] > 0]
    elif state == "bijna":
        items = [it for it in items if it["state"] == "bijna"]
    elif state == "op":
        items = [it for it in items if it["state"] == "op"]
    counts: dict[str, int] = {"_all": len(items)}
    for it in items:
        for c in it["cats"]:
            counts[c] = counts.get(c, 0) + 1
    if cat:
        items = [it for it in items if cat in it["cats"]]
    key_name = lambda it: ((it["name"] or "~").lower(), it["barcode"])
    if sort == "naam":
        items.sort(key=key_name)
    elif sort == "aantal":
        items.sort(key=lambda it: (-it["quantity"], key_name(it)))
    elif sort == "recent":
        items.sort(key=lambda it: -(it["item_updated"] or 0))
    else:
        items.sort(key=lambda it: (categories.ORDER.get(it["category"], 99), key_name(it)))
    return items, counts


@router.get("/locaties/{lid}", response_class=HTMLResponse)
def location_page(request: Request, lid: int, user=Depends(require_user), q: str = "",
                  cat: str = "", staat: str = "", sort: str = "categorie"):
    loc = get_location(request, lid, user)
    cat = cat if categories.valid(cat) else ""
    staat = staat if staat in STATES else ""
    sort = sort if sort in SORTS else "categorie"
    items, counts = _filtered_items(request, lid, q.strip(), cat, staat, sort)
    groups = []
    if sort == "categorie":
        # A drink with two categories (e.g. whisky + liqueur) shows up in both groups.
        by_cat: dict[str, list] = {}
        for it in items:
            for c in it["cats"]:
                if not cat or c == cat:
                    by_cat.setdefault(c, []).append(it)
        groups = sorted(by_cat.items(), key=lambda g: categories.ORDER.get(g[0], 99))
    total = db_of(request).one("SELECT COUNT(*) AS n, COALESCE(SUM(quantity),0) AS b FROM inventory "
                               "WHERE location_id=?", lid)

    def url_with(**kw):
        params = {"q": q, "cat": cat, "staat": staat, "sort": sort}
        params.update(kw)
        params = {k: v for k, v in params.items() if v and not (k == "sort" and v == "categorie")}
        return f"/locaties/{lid}" + ("?" + urlencode(params) if params else "")

    return render(request, "location.html", loc=loc, items=items, groups=groups, counts=counts,
                  q=q, cat=cat, staat=staat, sort=sort, SORTS=SORTS, STATES=STATES,
                  url_with=url_with, total=total, manage=can_manage(loc, user))


@router.get("/locaties/{lid}/scannen", response_class=HTMLResponse)
def scan_page(request: Request, lid: int, user=Depends(require_user)):
    loc = get_location(request, lid, user)
    return render(request, "scan.html", loc=loc)


@router.get("/locaties/{lid}/export.csv")
def export_csv(request: Request, lid: int, user=Depends(require_user)):
    loc = get_location(request, lid, user)
    items, _ = _filtered_items(request, lid, "", "", "", "categorie")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["categorie", "ook_in", "naam", "merk", "barcode", "inhoud_ml", "alcohol_pct", "aantal",
                "minimum", "notities"])
    for it in items:
        w.writerow([categories.label(it["category"]), ", ".join(categories.label(c) for c in it["cats"][1:]),
                    it["name"], it["brand"], it["barcode"],
                    it["volume_ml"] or "", it["abv"] if it["abv"] is not None else "",
                    it["quantity"], it["min_quantity"], it["notes"]])
    fname = "".join(ch if ch.isalnum() else "-" for ch in loc["name"]).strip("-") or "voorraad"
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})


@router.post("/locaties/{lid}/items/{pid}/aanpassen")
def adjust_item(request: Request, lid: int, pid: int, user=Depends(require_user),
                csrf: str = Form(""), delta: int = Form(0), back: str = Form("")):
    check_csrf(request, csrf)
    get_location(request, lid, user)
    get_product(request, pid)
    service.adjust(db_of(request), lid, pid, max(-1000, min(1000, delta)), user["id"], "handmatig")
    return redirect(safe_back(back, f"/locaties/{lid}"))


@router.post("/locaties/{lid}/items/{pid}")
def update_item(request: Request, lid: int, pid: int, user=Depends(require_user), csrf: str = Form(""),
                quantity: str = Form("0"), min_quantity: str = Form("0"), notes: str = Form("")):
    check_csrf(request, csrf)
    get_location(request, lid, user)
    get_product(request, pid)
    db = db_of(request)
    row = db.one("SELECT quantity FROM inventory WHERE location_id=? AND product_id=?", lid, pid)
    current = row["quantity"] if row else 0
    target = _int(quantity, current, 0, 100000)
    service.adjust(db, lid, pid, target - current, user["id"], "geteld")
    db.run("UPDATE inventory SET min_quantity=?, notes=? WHERE location_id=? AND product_id=?",
           _int(min_quantity, 0, 0, 100000), notes.strip()[:500], lid, pid)
    return redirect(f"/producten/{pid}?loc={lid}", msg="Voorraad opgeslagen.")


@router.post("/locaties/{lid}/items/{pid}/verwijderen")
def remove_item(request: Request, lid: int, pid: int, user=Depends(require_user), csrf: str = Form("")):
    check_csrf(request, csrf)
    get_location(request, lid, user)
    db_of(request).run("DELETE FROM inventory WHERE location_id=? AND product_id=?", lid, pid)
    return redirect(f"/locaties/{lid}", msg="Product uit deze locatie verwijderd.")


@router.get("/locaties/{lid}/instellingen", response_class=HTMLResponse)
def location_settings(request: Request, lid: int, user=Depends(require_user)):
    loc = get_location(request, lid, user)
    db = db_of(request)
    members = db.all("SELECT u.id, u.username FROM location_members m JOIN users u ON u.id=m.user_id "
                     "WHERE m.location_id=? ORDER BY u.username", lid)
    owner = db.one("SELECT username FROM users WHERE id=?", loc["owner_id"])
    return render(request, "location_settings.html", loc=loc, members=members,
                  owner=owner["username"] if owner else "?", manage=can_manage(loc, user))


def _managed_location(request: Request, lid: int, user, csrf: str):
    check_csrf(request, csrf)
    loc = get_location(request, lid, user)
    if not can_manage(loc, user):
        raise HTTPException(403, "Alleen de eigenaar kan dit wijzigen")
    return loc


@router.post("/locaties/{lid}/instellingen")
def save_location(request: Request, lid: int, user=Depends(require_user), csrf: str = Form(""),
                  name: str = Form(""), description: str = Form("")):
    _managed_location(request, lid, user, csrf)
    if not name.strip():
        return redirect(f"/locaties/{lid}/instellingen", err="Naam mag niet leeg zijn.")
    db_of(request).run("UPDATE locations SET name=?, description=? WHERE id=?",
                       name.strip()[:100], description.strip()[:500], lid)
    return redirect(f"/locaties/{lid}/instellingen", msg="Opgeslagen.")


@router.post("/locaties/{lid}/leden")
def add_member(request: Request, lid: int, user=Depends(require_user), csrf: str = Form(""),
               username: str = Form("")):
    loc = _managed_location(request, lid, user, csrf)
    db = db_of(request)
    member = db.one("SELECT id FROM users WHERE username=?", username.strip())
    if not member:
        return redirect(f"/locaties/{lid}/instellingen", err=f"Gebruiker '{username}' bestaat niet.")
    if member["id"] != loc["owner_id"]:
        db.run("INSERT OR IGNORE INTO location_members(location_id, user_id) VALUES (?,?)", lid, member["id"])
    return redirect(f"/locaties/{lid}/instellingen", msg=f"{username} heeft nu toegang.")


@router.post("/locaties/{lid}/leden/{uid}/verwijderen")
def remove_member(request: Request, lid: int, uid: int, user=Depends(require_user), csrf: str = Form("")):
    _managed_location(request, lid, user, csrf)
    db_of(request).run("DELETE FROM location_members WHERE location_id=? AND user_id=?", lid, uid)
    return redirect(f"/locaties/{lid}/instellingen", msg="Toegang ingetrokken.")


@router.post("/locaties/{lid}/verwijderen")
def delete_location(request: Request, lid: int, user=Depends(require_user), csrf: str = Form(""),
                    confirm: str = Form("")):
    loc = _managed_location(request, lid, user, csrf)
    if confirm.strip() != loc["name"]:
        return redirect(f"/locaties/{lid}/instellingen", err="Typ de naam van de locatie om te bevestigen.")
    db_of(request).run("DELETE FROM locations WHERE id=?", lid)
    return redirect("/locaties", msg=f"Locatie '{loc['name']}' verwijderd.")


# ---- products -------------------------------------------------------------------------------------
@router.get("/producten", response_class=HTMLResponse)
def products_page(request: Request, user=Depends(require_user), q: str = "", cat: str = "", merk: str = "",
                  staat: str = ""):
    db = db_of(request)
    allowed = service.accessible_location_ids(db, user)
    rows = db.all("""
        SELECT p.*, (SELECT filename FROM product_images pi WHERE pi.product_id = p.id
                     ORDER BY position, id LIMIT 1) AS image,
               (SELECT GROUP_CONCAT(location_id) FROM inventory WHERE product_id = p.id) AS loc_ids,
               (SELECT COALESCE(SUM(quantity), 0) FROM inventory WHERE product_id = p.id) AS total
        FROM products p ORDER BY p.name = '', p.name COLLATE NOCASE, p.barcode""")
    names = {r["id"]: r["name"] for r in db.all("SELECT id, name FROM locations")}
    items, brands, counts = [], {}, {"_all": 0}
    terms = categories.normalize(q).split()
    for r in rows:
        d = dict(r)
        locs = [int(x) for x in (d["loc_ids"] or "").split(",") if x]
        # Non-admins see products from their own locations, plus products that are in no location.
        if allowed is not None and locs and not (set(locs) & allowed):
            continue
        d["locs"] = [(lid, names.get(lid, "?")) for lid in locs if allowed is None or lid in allowed]
        d["cats"] = service.product_categories(r)
        d["deletable"] = allowed is None or set(locs) <= allowed
        if terms:
            hay = categories.normalize(" ".join([d["name"], d["brand"], d["barcode"],
                                                 *[categories.label(c) for c in d["cats"]]]))
            if not all(t in hay for t in terms):
                continue
        if staat == "onbekend" and d["name"]:
            continue
        if staat == "los" and locs:
            continue
        if merk and d["brand"] != merk:
            continue
        for c in d["cats"]:
            counts[c] = counts.get(c, 0) + 1
        counts["_all"] += 1
        if d["brand"]:
            brands[d["brand"]] = brands.get(d["brand"], 0) + 1
        if cat and cat not in d["cats"]:
            continue
        items.append(d)

    def url_with(**kw):
        params = {"q": q, "cat": cat, "merk": merk, "staat": staat}
        params.update(kw)
        params = {k: v for k, v in params.items() if v}
        return "/producten" + ("?" + urlencode(params) if params else "")

    return render(request, "products.html", items=items, counts=counts, q=q, cat=cat, merk=merk, staat=staat,
                  brands=sorted(brands.items(), key=lambda b: b[0].lower()), url_with=url_with)


@router.post("/producten-verwijderen")
def delete_products(request: Request, user=Depends(require_user), csrf: str = Form(""),
                    ids: list[int] = Form([]), back: str = Form("")):
    check_csrf(request, csrf)
    db = db_of(request)
    done = skipped = 0
    for pid in ids[:500]:
        if service.can_delete_product(db, user, pid) and service.delete_product(db, request.app.state.settings, pid):
            done += 1
        else:
            skipped += 1
    back = safe_back(back, "/producten")
    if not done:
        return redirect(back, err="Niets verwijderd." if not skipped else
                        "Deze producten staan ook in locaties van anderen; alleen een beheerder kan ze verwijderen.")
    msg = f"{done} product(en) verwijderd."
    if skipped:
        msg += f" {skipped} overgeslagen (staan ook in locaties van anderen)."
    return redirect(back, msg=msg)


@router.post("/producten/{pid}/verwijderen")
def delete_product(request: Request, pid: int, user=Depends(require_user), csrf: str = Form(""),
                   loc: str = Form("")):
    check_csrf(request, csrf)
    p = get_product(request, pid)
    db = db_of(request)
    if not service.can_delete_product(db, user, pid):
        return redirect(_product_url(pid, loc), err="Dit product staat ook in locaties van anderen; "
                                                     "alleen een beheerder kan het verwijderen.")
    service.delete_product(db, request.app.state.settings, pid)
    back = f"/locaties/{_int(loc)}" if _int(loc) else "/producten"
    return redirect(back, msg=f"'{p['name'] or p['barcode']}' verwijderd.")


@router.get("/producten/{pid}", response_class=HTMLResponse)
def product_page(request: Request, pid: int, user=Depends(require_user), loc: int | None = None):
    p = get_product(request, pid)
    db = db_of(request)
    location = item = None
    if loc:
        location = get_location(request, loc, user)
        item = db.one("SELECT * FROM inventory WHERE location_id=? AND product_id=?", loc, pid)
    history = []
    if location:
        history = db.all("""SELECT m.*, u.username FROM movements m LEFT JOIN users u ON u.id=m.user_id
                            WHERE m.location_id=? AND m.product_id=? ORDER BY m.created_at DESC LIMIT 15""",
                         loc, pid)
    return render(request, "product.html", p=p, photos=service.product_images(db, pid),
                  cats=service.product_categories(p),
                  can_delete=service.can_delete_product(db, user, pid),
                  location=location, item=item, history=history,
                  brave=bool(request.app.state.settings.brave_api_key),
                  ai=request.app.state.ai, ai_fields=[f for f in (p["ai_fields"] or "").split(",") if f])


def _product_url(pid: int, loc: str | int | None) -> str:
    loc = _int(loc)
    return f"/producten/{pid}" + (f"?loc={loc}" if loc else "")


@router.post("/producten/{pid}")
def save_product(request: Request, pid: int, user=Depends(require_user), csrf: str = Form(""),
                 loc: str = Form(""), name: str = Form(""), brand: str = Form(""),
                 category: str = Form("overig"), extra: list[str] = Form([]), volume_ml: str = Form(""),
                 abv: str = Form(""), description: str = Form(""), country: str = Form("")):
    check_csrf(request, csrf)
    p = get_product(request, pid)
    db = db_of(request)
    category = category if categories.valid(category) else p["category"]
    extra_s = ",".join(c for c in categories.KEYS if c in extra and c != category)
    changed = category != p["category"] or extra_s != (p["extra_categories"] or "")
    locked = 1 if (p["category_locked"] or changed) else 0
    abv_v = _float(abv)
    abv_v = abv_v if abv_v is not None and 0 <= abv_v <= 100 else None
    vol_v = _int(volume_ml, None, 1, 100000)
    new = {"brand": brand.strip()[:100], "country": country.strip()[:60], "abv": abv_v,
           "description": description.strip()[:2000], "category": category}
    # A field the user changed is no longer "filled in by AI".
    new["name"] = name.strip()[:150]
    new["extra"] = extra_s
    p = {**dict(p), "extra": p["extra_categories"] or ""}
    # 'name' stays as marker that the barcode was already identified, so it never runs again.
    ai_fields = ",".join(f for f in (p["ai_fields"] or "").split(",") if f in new and (f == "name" or new[f] == p[f]))
    db.run("""UPDATE products SET name=?, brand=?, category=?, extra_categories=?, category_locked=?,
              volume_ml=?, abv=?, country=?, ai_fields=?,
              description=?, lookup_status=CASE WHEN lookup_status='pending' THEN 'pending' ELSE 'manual' END,
              updated_at=? WHERE id=?""",
           name.strip()[:150], new["brand"], category, extra_s, locked, vol_v, abv_v, new["country"], ai_fields,
           new["description"], time.time(), pid)
    if not locked and category == categories.DEFAULT:
        service.reclassify(db, pid)
        if db.one("SELECT category FROM products WHERE id=?", pid)["category"] == categories.DEFAULT:
            request.app.state.worker.enqueue(pid, "classify")
    msg = "Product opgeslagen."
    request.app.state.ai.wake()
    if name.strip() and not service.product_images(db, pid):
        request.app.state.worker.enqueue(pid, "images")
        msg += " Er wordt op de achtergrond naar foto's gezocht."
    return redirect(_product_url(pid, loc), msg=msg)


@router.post("/producten/{pid}/ai")
def ai_again(request: Request, pid: int, user=Depends(require_user), csrf: str = Form(""), loc: str = Form("")):
    check_csrf(request, csrf)
    get_product(request, pid)
    if not request.app.state.ai.enricher.enabled:
        return redirect(_product_url(pid, loc), err="Er is geen AI ingesteld (BM_OLLAMA_URL).")
    db_of(request).run("UPDATE products SET ai_for='', ai_attempts=0 WHERE id=?", pid)
    request.app.state.ai.wake()
    return redirect(_product_url(pid, loc), msg="De AI vult lege velden aan; dat duurt ongeveer een minuut.")


@router.post("/producten/{pid}/opnieuw-opzoeken")
def relookup(request: Request, pid: int, user=Depends(require_user), csrf: str = Form(""), loc: str = Form("")):
    check_csrf(request, csrf)
    get_product(request, pid)
    db_of(request).run("UPDATE products SET lookup_status='pending' WHERE id=? AND lookup_status!='manual'", pid)
    request.app.state.worker.enqueue(pid, "lookup")
    return redirect(_product_url(pid, loc), msg="Product wordt opnieuw opgezocht; ververs de pagina zo.")


@router.post("/producten/{pid}/fotos")
async def add_photo(request: Request, pid: int, user=Depends(require_user), csrf: str = Form(""),
                    loc: str = Form(""), url: str = Form(""), file: UploadFile | None = File(None)):
    check_csrf(request, csrf)
    get_product(request, pid)
    settings = request.app.state.settings
    db = db_of(request)
    try:
        if file is not None and file.filename:
            data = await file.read(settings.max_image_bytes + 1)
            if len(data) > settings.max_image_bytes:
                raise images.ImageError("bestand te groot")
            service.add_image(db, pid, images.store(data, settings.image_dir), "upload")
        elif url.strip():
            from starlette.concurrency import run_in_threadpool

            def _download():
                with request.app.state.lookup.client() as c:
                    return images.fetch(c, url.strip(), settings.max_image_bytes)
            data = await run_in_threadpool(_download)
            service.add_image(db, pid, images.store(data, settings.image_dir), url.strip())
        else:
            return redirect(_product_url(pid, loc), err="Kies een bestand of vul een adres in.")
    except Exception as exc:  # noqa: BLE001
        return redirect(_product_url(pid, loc), err=f"Foto toevoegen mislukt: {exc}")
    return redirect(_product_url(pid, loc), msg="Foto toegevoegd.")


@router.post("/producten/{pid}/fotos/zoeken")
def search_photos(request: Request, pid: int, user=Depends(require_user), csrf: str = Form(""),
                  loc: str = Form("")):
    check_csrf(request, csrf)
    p = get_product(request, pid)
    if not p["name"]:
        return redirect(_product_url(pid, loc), err="Vul eerst een naam in.")
    st = request.app.state
    urls = st.lookup.image_search(service.image_query(p["name"], p["brand"]))
    # An explicit search may add a few more, on top of what is already there.
    added = service.download_images(st.db, st.settings, st.lookup, pid, urls, limit=st.settings.max_images)
    if not added:
        return redirect(_product_url(pid, loc), err="Geen (nieuwe) foto's gevonden.")
    return redirect(_product_url(pid, loc), msg=f"{added} foto('s) toegevoegd.")


@router.post("/producten/{pid}/fotos/{iid}/hoofdfoto")
def primary_photo(request: Request, pid: int, iid: int, user=Depends(require_user), csrf: str = Form(""),
                  loc: str = Form("")):
    check_csrf(request, csrf)
    db = db_of(request)
    photos = service.product_images(db, pid)
    order = [ph["id"] for ph in photos if ph["id"] != iid]
    if len(order) != len(photos):
        order.insert(0, iid)
    with db.tx():
        for pos, img_id in enumerate(order):
            db.run("UPDATE product_images SET position=? WHERE id=?", pos, img_id)
    return redirect(_product_url(pid, loc), msg="Hoofdfoto ingesteld.")


@router.post("/producten/{pid}/fotos/{iid}/verwijderen")
def delete_photo(request: Request, pid: int, iid: int, user=Depends(require_user), csrf: str = Form(""),
                 loc: str = Form("")):
    check_csrf(request, csrf)
    db = db_of(request)
    ph = db.one("SELECT * FROM product_images WHERE id=? AND product_id=?", iid, pid)
    if ph:
        db.run("DELETE FROM product_images WHERE id=?", iid)
        images.remove(ph["filename"], request.app.state.settings.image_dir)
    return redirect(_product_url(pid, loc), msg="Foto verwijderd.")


# ---- users & account -------------------------------------------------------------------------------
@router.get("/gebruikers", response_class=HTMLResponse)
def users_page(request: Request, user=Depends(require_admin)):
    users = db_of(request).all("""SELECT u.*, (SELECT COUNT(*) FROM locations WHERE owner_id=u.id) AS locs
                                  FROM users u ORDER BY username COLLATE NOCASE""")
    return render(request, "users.html", users=users)


@router.post("/gebruikers")
def create_user(request: Request, user=Depends(require_admin), csrf: str = Form(""),
                username: str = Form(""), password: str = Form(""), is_admin: str = Form("")):
    check_csrf(request, csrf)
    username = username.strip()
    if not username or len(username) > 50 or len(password) < 8:
        return redirect("/gebruikers", err="Gebruikersnaam verplicht, wachtwoord minstens 8 tekens.")
    db = db_of(request)
    if db.one("SELECT 1 FROM users WHERE username=?", username):
        return redirect("/gebruikers", err=f"'{username}' bestaat al.")
    db.run("INSERT INTO users(username, password_hash, is_admin, created_at) VALUES (?,?,?,?)",
           username, hash_password(password), 1 if is_admin else 0, time.time())
    return redirect("/gebruikers", msg=f"Gebruiker '{username}' aangemaakt.")


@router.post("/gebruikers/{uid}/wachtwoord")
def reset_password(request: Request, uid: int, user=Depends(require_admin), csrf: str = Form(""),
                   password: str = Form("")):
    check_csrf(request, csrf)
    if len(password) < 8:
        return redirect("/gebruikers", err="Wachtwoord moet minstens 8 tekens zijn.")
    db = db_of(request)
    db.run("UPDATE users SET password_hash=? WHERE id=?", hash_password(password), uid)
    db.run("DELETE FROM sessions WHERE user_id=?", uid)
    return redirect("/gebruikers", msg="Wachtwoord gewijzigd.")


@router.post("/gebruikers/{uid}/beheerder")
def toggle_admin(request: Request, uid: int, user=Depends(require_admin), csrf: str = Form("")):
    check_csrf(request, csrf)
    if uid == user["id"]:
        return redirect("/gebruikers", err="Je kunt je eigen beheerdersrechten niet afnemen.")
    db_of(request).run("UPDATE users SET is_admin = 1 - is_admin WHERE id=?", uid)
    return redirect("/gebruikers", msg="Rechten aangepast.")


@router.post("/gebruikers/{uid}/verwijderen")
def delete_user(request: Request, uid: int, user=Depends(require_admin), csrf: str = Form("")):
    check_csrf(request, csrf)
    if uid == user["id"]:
        return redirect("/gebruikers", err="Je kunt jezelf niet verwijderen.")
    db_of(request).run("DELETE FROM users WHERE id=?", uid)
    return redirect("/gebruikers", msg="Gebruiker verwijderd, inclusief de eigen locaties.")


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, user=Depends(require_user)):
    return render(request, "account.html")


@router.post("/account/wachtwoord")
def change_password(request: Request, user=Depends(require_user), csrf: str = Form(""),
                    current: str = Form(""), new: str = Form(""), repeat: str = Form("")):
    check_csrf(request, csrf)
    if not verify_password(current, user["password_hash"]):
        return redirect("/account", err="Huidig wachtwoord klopt niet.")
    if len(new) < 8 or new != repeat:
        return redirect("/account", err="Nieuw wachtwoord moet minstens 8 tekens zijn en twee keer gelijk.")
    db_of(request).run("UPDATE users SET password_hash=? WHERE id=?", hash_password(new), user["id"])
    return redirect("/account", msg="Wachtwoord gewijzigd.")
