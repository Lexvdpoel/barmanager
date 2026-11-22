"""Cocktails, shopping list, statistics and the public bar menu."""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from . import bar, categories, cocktails, i18n, service
from .i18n import _, _n
from .auth import check_csrf, db_of, require_admin, require_user
from .web import _int, get_location, get_product, redirect, render, safe_back

router = APIRouter()

SHOW = {"kan": "Kan ik maken", "bijna": "Mist er één", "alles": "Alle cocktails"}
TYPES = {"alcohol": "Met alcohol", "alcoholvrij": "Zonder alcohol", "alles": "Met en zonder alcohol"}
SPIRIT_CATS = ["gin", "rum", "vodka", "whisky", "tequila", "brandy", "likeur", "aperitief", "bitter", "bubbels",
               "wijn", "port", "bier"]


def _location_ids(request: Request, user, loc: int | None) -> tuple[list[int], object]:
    db = db_of(request)
    if loc:
        location = get_location(request, loc, user)
        return [loc], location
    return [l["id"] for l in bar.visible_locations(db, user)], None


def _drink_cats(c: dict) -> set[str]:
    out = set()
    for ing in c["ingredients"]:
        spec = cocktails.spec_for(ing["name"])
        if spec.kind == "drank":
            out |= set(spec.cats) | set(spec.sub_cats)
            if not spec.cats and not spec.sub_cats:      # named liqueurs: Cointreau, Galliano, ...
                out.add("likeur")
    return out & set(SPIRIT_CATS)


# ---- cocktails -----------------------------------------------------------------------------------
@router.get("/cocktails", response_class=HTMLResponse)
def cocktails_page(request: Request, user=Depends(require_user), loc: int | None = None, q: str = "",
                   toon: str = "kan", soort: str = "alcohol", sap: str = "1", fles: int | None = None, iba: str = "",
                   met: str = ""):
    db = db_of(request)
    toon = toon if toon in SHOW else "kan"
    soort = soort if soort in TYPES else "alcohol"
    lids, location = _location_ids(request, user, loc)
    stock = cocktails.stock(db, lids)
    assume = sap != "0"
    all_c = cocktails.evaluate_all(db, stock, assume)
    fles_p = None
    if fles:
        fles_p = get_product(request, fles)
        keys = cocktails.ingredients_for_product(db, fles_p)
        all_c = [c for c in all_c if any(cocktails.norm(i["name"]) in keys for i in c["ingredients"])]
        if toon == "kan" and not any(c["can"] for c in all_c):
            toon = "alles"
    if soort == "alcohol":
        all_c = [c for c in all_c if c["boozy"]]
    elif soort == "alcoholvrij":
        all_c = [c for c in all_c if not c["boozy"]]
    if iba:
        all_c = [c for c in all_c if c["iba"]]
    if q.strip():
        terms = categories.normalize(q).split()
        all_c = [c for c in all_c if all(t in categories.normalize(
            c["name"] + " " + " ".join(i["name"] for i in c["ingredients"])) for t in terms)]
    counts = {"kan": sum(c["can"] for c in all_c), "bijna": sum(c["almost"] for c in all_c), "alles": len(all_c)}
    shown = [c for c in all_c if toon == "alles" or (toon == "kan" and c["can"]) or (toon == "bijna" and c["almost"])]
    met_counts: dict[str, int] = {}
    for c in shown:
        c["drink_cats"] = _drink_cats(c)
        for k in c["drink_cats"]:
            met_counts[k] = met_counts.get(k, 0) + 1
    if met in SPIRIT_CATS:
        shown = [c for c in shown if met in c["drink_cats"]]
    else:
        met = ""
    if toon == "alles":
        shown.sort(key=lambda c: (not c["can"], not c["almost"], c["name"].lower()))
    # Which missing bottle would unlock the most cocktails?
    unlock: dict[str, int] = {}
    if toon == "bijna":
        for c in shown:
            # "Light rum", "White rum" and "Rum" are all just a bottle of rum.
            spec = cocktails.spec_for(c["missing"][0])
            name = _(categories.label(spec.cats[0])) if spec.all_cats and spec.cats else c["missing"][0]
            unlock[name] = unlock.get(name, 0) + 1

    def url_with(**kw):
        params = {"loc": loc or "", "q": q, "toon": toon, "soort": soort, "sap": "" if assume else "0",
                  "fles": fles or "", "iba": iba, "met": met}
        params.update(kw)
        params = {k: v for k, v in params.items() if v not in ("", None) and not (k == "toon" and v == "kan")
                  and not (k == "soort" and v == "alcohol")}
        return "/cocktails" + ("?" + urlencode(params) if params else "")

    st = request.app.state
    return render(request, "cocktails.html", items=shown, counts=counts, toon=toon, SHOW=SHOW, soort=soort,
                  TYPES=TYPES, q=q, loc=loc, location=location, locations=bar.visible_locations(db, user),
                  assume=assume, fles_p=fles_p, iba=iba, met=met, met_counts=met_counts, url_with=url_with,
                  unlock=sorted(unlock.items(), key=lambda x: -x[1])[:8],
                  n_cocktails=db.one("SELECT COUNT(*) AS n FROM cocktails")["n"], last_sync=cocktails.last_sync(db),
                  syncing=st.cocktail_sync.busy, sync_error=st.cocktail_sync.error, n_stock=len(stock),
                  pantry=cocktails.pantry_mode(db))


def _cocktail(db, cid: str) -> dict:
    row = db.one("SELECT * FROM cocktails WHERE id=?", cid)
    if not row:
        raise HTTPException(404, "Cocktail niet gevonden")
    c = dict(row)
    c["ingredients"] = [dict(r) for r in db.all("SELECT * FROM cocktail_ingredients WHERE cocktail_id=? "
                                                "ORDER BY pos", cid)]
    return c


def _ingredient_rows(db, c: dict, matcher: cocktails.Matcher) -> list[dict]:
    rows = []
    for ing in c["ingredients"]:
        st = matcher.resolve(ing["name"])
        rows.append({"name": ing["name"], "key": cocktails.canon(cocktails.norm(ing["name"])),
                     "nl": cocktails.NL.get(cocktails.canon(cocktails.norm(ing["name"])), "")
                     if st.kind != "drank" and i18n.get_lang() == "nl" else "",
                     "measure": cocktails.measure_local(ing["measure"]), "measure_orig": ing["measure"],
                     "state": st.state, "kind": st.kind, "products": st.products})
    return rows


@router.get("/cocktails/{cid}", response_class=HTMLResponse)
def cocktail_page(request: Request, cid: str, user=Depends(require_user), loc: int | None = None, sap: str = "1"):
    db = db_of(request)
    c = _cocktail(db, cid)
    lids, location = _location_ids(request, user, loc)
    m = cocktails.Matcher(db, cocktails.stock(db, lids), sap != "0")
    rows = _ingredient_rows(db, c, m)
    return render(request, "cocktail.html", c=c, rows=rows, loc=loc, location=location, sap=sap,
                  missing=[r for r in rows if r["state"] == "mist"])


@router.post("/cocktails/bijwerken")
def cocktails_sync(request: Request, user=Depends(require_admin), csrf: str = Form("")):
    check_csrf(request, csrf)
    request.app.state.cocktail_sync.now()
    return redirect("/cocktails?toon=alles", msg=_("Cocktails worden opgehaald; dat duurt een paar minuten."))


@router.get("/cocktails-ingredient", response_class=HTMLResponse)
def ingredient_page(request: Request, naam: str, user=Depends(require_user), loc: int | None = None,
                    back: str = ""):
    db = db_of(request)
    key = cocktails.norm(naam)
    if not key:
        raise HTTPException(404, "Onbekend ingrediënt")
    lids, location = _location_ids(request, user, loc)
    stock = cocktails.stock(db, lids)
    spec = cocktails.spec_for(key)
    pref = db.one("SELECT always FROM ingredient_prefs WHERE ingredient=?", key)
    linked = {r["product_id"] for r in db.all("SELECT product_id FROM ingredient_links WHERE ingredient=?", key)}
    for p in stock:
        p["auto"] = cocktails.match(spec, p)
        p["linked"] = p["id"] in linked
    stock.sort(key=lambda p: (not p["linked"], p["auto"] != "exact", p["auto"] != "sub", (p["name"] or "~").lower()))
    used_in = db.one("SELECT COUNT(DISTINCT cocktail_id) AS n FROM cocktail_ingredients WHERE name=? COLLATE NOCASE",
                     naam)["n"]
    return render(request, "ingredient.html", naam=naam, key=key, spec=spec, always=bool(pref and pref["always"]),
                  stock=stock, loc=loc, location=location, used_in=used_in,
                  back=safe_back(back, "/cocktails"))


@router.post("/cocktails-ingredient")
def ingredient_save(request: Request, user=Depends(require_user), csrf: str = Form(""), naam: str = Form(""),
                    always: str = Form(""), products: list[int] = Form([]), shown: list[int] = Form([]),
                    back: str = Form(""), loc: str = Form("")):
    check_csrf(request, csrf)
    db = db_of(request)
    key = cocktails.norm(naam)
    if not key:
        return redirect("/cocktails", err=_("Onbekend ingrediënt."))
    with db.tx():
        db.run("INSERT INTO ingredient_prefs(ingredient, always) VALUES (?,?) "
               "ON CONFLICT(ingredient) DO UPDATE SET always=excluded.always", key, 1 if always else 0)
        # Only products that were on the form change; links to bottles elsewhere stay.
        for pid in shown[:1000]:
            if pid in products:
                db.run("INSERT OR IGNORE INTO ingredient_links(ingredient, product_id) "
                       "SELECT ?, id FROM products WHERE id=?", key, pid)
            else:
                db.run("DELETE FROM ingredient_links WHERE ingredient=? AND product_id=?", key, pid)
    params = {"naam": naam, "back": safe_back(back, "")}
    if _int(loc):
        params["loc"] = _int(loc)
    return redirect("/cocktails-ingredient?" + urlencode({k: v for k, v in params.items() if v}),
                    msg=_("Opgeslagen."))


@router.post("/cocktails/{cid}/op-lijst")
def cocktail_missing_to_list(request: Request, cid: str, user=Depends(require_user), csrf: str = Form(""),
                             naam: list[str] = Form([]), back: str = Form("")):
    check_csrf(request, csrf)
    db = db_of(request)
    c = _cocktail(db, cid)
    have = {r["name"].lower() for r in db.all("SELECT name FROM shopping_items WHERE product_id IS NULL "
                                              "AND kind='handmatig' AND created_by=?", user["id"])}
    added = 0
    for n in naam[:15]:
        label = _("{item} (voor {cocktail})", item=n, cocktail=c["name"])
        if n.strip() and label.lower() not in have:
            bar.add_item(db, user, None, None, label)
            added += 1
    return redirect(safe_back(back, f"/cocktails/{cid}"),
                    msg=_("{n} op de boodschappenlijst gezet.", n=added) if added else _("Stond al op de lijst."))


# ---- kitchen & mixers ----------------------------------------------------------------------------
@router.get("/keuken", response_class=HTMLResponse)
def pantry_page(request: Request, user=Depends(require_user)):
    db = db_of(request)
    items = cocktails.pantry_items(db)
    stock = cocktails.stock(db, [l["id"] for l in bar.visible_locations(db, user)])
    for d in items:
        spec = cocktails.spec_for(d["key"])
        d["bottles"] = [p["name"] for p in stock if cocktails.match(spec, p) == "exact"]
    groups = [(key, label, [d for d in items if d["group"] == key]) for key, label in cocktails.PANTRY_GROUPS]
    return render(request, "pantry.html", groups=[g for g in groups if g[2]], mode=cocktails.pantry_mode(db),
                  n_have=sum(d["have"] for d in items), n=len(items))


@router.post("/keuken")
def pantry_save(request: Request, user=Depends(require_user), csrf: str = Form(""), have: list[str] = Form([]),
                actie: str = Form("")):
    check_csrf(request, csrf)
    db = db_of(request)
    if actie == "uit":
        db.run("DELETE FROM kv WHERE key='pantry'")
        return redirect("/keuken", msg=_("Uitgezet: verse ingrediënten, sap en frisdrank tellen weer als aanwezig."))
    n, total = _save_pantry(db, have)
    return redirect("/keuken", msg=_("Opgeslagen: {n} van de {total} dingen in huis.", n=n, total=total))


def _save_pantry(db, have: list[str]) -> tuple[int, int]:
    keys = {d["key"] for d in cocktails.pantry_items(db)}
    have_set = {cocktails.norm(h) for h in have}
    with db.tx():
        for k in keys:
            db.run("INSERT INTO ingredient_prefs(ingredient, always) VALUES (?,?) "
                   "ON CONFLICT(ingredient) DO UPDATE SET always=excluded.always", k, 1 if k in have_set else 0)
        db.run("INSERT INTO kv(key, value) VALUES ('pantry', '1') ON CONFLICT(key) DO UPDATE SET value='1'")
    return len(have_set & keys), len(keys)


@router.post("/keuken/op-lijst")
def pantry_to_list(request: Request, user=Depends(require_user), csrf: str = Form(""), naam: str = Form(""),
                   have: list[str] = Form([])):
    check_csrf(request, csrf)
    db = db_of(request)
    _save_pantry(db, have)         # the cart button submits the whole form: keep the ticks too
    if naam.strip():
        label = (cocktails.NL.get(cocktails.canon(cocktails.norm(naam))) if i18n.get_lang() == "nl" else None) or naam
        label = label[:1].upper() + label[1:]
        exists = db.one("SELECT 1 FROM shopping_items WHERE product_id IS NULL AND kind='handmatig' "
                        "AND created_by=? AND lower(name)=lower(?)", user["id"], label)
        if not exists:
            bar.add_item(db, user, None, None, label)
    return redirect("/keuken", msg=_("'{name}' staat op de boodschappenlijst.", name=naam))


# ---- shopping list -------------------------------------------------------------------------------
@router.get("/boodschappen", response_class=HTMLResponse)
def shopping_page(request: Request, user=Depends(require_user)):
    data = bar.shopping_list(db_of(request), user)
    return render(request, "shopping.html", **data)


@router.post("/boodschappen")
def shopping_add(request: Request, user=Depends(require_user), csrf: str = Form(""), name: str = Form("")):
    check_csrf(request, csrf)
    if not name.strip():
        return redirect("/boodschappen", err=_("Typ wat er op de lijst moet."))
    bar.add_item(db_of(request), user, None, None, name)
    return redirect("/boodschappen")


@router.post("/boodschappen/weg")
def shopping_remove(request: Request, user=Depends(require_user), csrf: str = Form(""), item: str = Form(""),
                    loc: str = Form(""), pid: str = Form(""), back: str = Form("")):
    check_csrf(request, csrf)
    db = db_of(request)
    item_id, lid, product_id = _int(item), _int(loc), _int(pid)
    if lid:
        get_location(request, lid, user)
    if item_id:
        row = db.one("SELECT * FROM shopping_items WHERE id=?", item_id)
        if not row:
            item_id = None
        elif row["location_id"]:
            get_location(request, row["location_id"], user)
        elif row["created_by"] != user["id"] and not user["is_admin"]:
            raise HTTPException(404, "Niet gevonden")
    bar.remove_item(db, lid, product_id, item_id)
    return redirect(safe_back(back, "/boodschappen"))


@router.post("/producten/{pid}/boodschappen")
def product_to_list(request: Request, pid: int, user=Depends(require_user), csrf: str = Form(""),
                    loc: str = Form(""), weg: str = Form("")):
    check_csrf(request, csrf)
    get_product(request, pid)
    db = db_of(request)
    lid = _int(loc)
    if lid:
        get_location(request, lid, user)
    back = f"/producten/{pid}" + (f"?loc={lid}" if lid else "")
    if weg:
        db.run("DELETE FROM shopping_items WHERE kind='handmatig' AND product_id=? AND location_id IS ?", pid, lid)
        return redirect(back, msg=_("Van de boodschappenlijst gehaald."))
    bar.add_item(db, user, lid, pid)
    return redirect(back, msg=_("Op de boodschappenlijst gezet."))


# ---- fill levels & statistics --------------------------------------------------------------------
@router.post("/locaties/{lid}/items/{pid}/niveau")
def set_level(request: Request, lid: int, pid: int, user=Depends(require_user), csrf: str = Form(""),
              level: str = Form(""), back: str = Form("")):
    check_csrf(request, csrf)
    get_location(request, lid, user)
    get_product(request, pid)
    db = db_of(request)
    if not service.tracks_levels(db, lid):
        return redirect(f"/producten/{pid}?loc={lid}", err=_("Flesinhoud bijhouden staat uit voor deze locatie."))
    value = None if level == "dicht" else _int(level, None, 0, 100)
    if value is None and level != "dicht":
        return redirect(f"/producten/{pid}?loc={lid}", err=_("Ongeldige inhoud."))
    msg = service.set_level(db, lid, pid, value, user["id"])
    return redirect(safe_back(back, f"/producten/{pid}?loc={lid}"), msg=msg)


@router.post("/locaties/{lid}/bijhouden")
def toggle_tracking(request: Request, lid: int, user=Depends(require_user), csrf: str = Form(""),
                    aan: str = Form("")):
    check_csrf(request, csrf)
    get_location(request, lid, user)
    db_of(request).run("UPDATE locations SET track_levels=? WHERE id=?", 1 if aan else 0, lid)
    return redirect(f"/locaties/{lid}/instellingen",
                    msg=_("Flesinhoud bijhouden staat aan.") if aan else _("Flesinhoud bijhouden staat uit."))


@router.get("/locaties/{lid}/statistieken", response_class=HTMLResponse)
def stats_page(request: Request, lid: int, user=Depends(require_user), maanden: int = 6):
    loc = get_location(request, lid, user)
    if not loc["track_levels"]:
        return redirect(f"/locaties/{lid}/instellingen",
                        err=_("Statistieken werken samen met 'Flesinhoud bijhouden'; zet dat eerst aan."))
    maanden = maanden if maanden in (3, 6, 12, 24) else 6
    db = db_of(request)
    return render(request, "stats.html", loc=loc, s=bar.stats(db, lid, maanden), maanden=maanden,
                  value_now=service.location_value(db, lid))


# ---- public bar menu -----------------------------------------------------------------------------
def _managed(request: Request, lid: int, user, csrf: str):
    check_csrf(request, csrf)
    loc = get_location(request, lid, user)
    if not (user["is_admin"] or loc["owner_id"] == user["id"]):
        raise HTTPException(403, "Alleen de eigenaar kan dit wijzigen")
    return loc


@router.post("/locaties/{lid}/barkaart")
def menu_toggle(request: Request, lid: int, user=Depends(require_user), csrf: str = Form(""),
                actie: str = Form("")):
    _managed(request, lid, user, csrf)
    db = db_of(request)
    if actie == "uit":
        db.run("UPDATE locations SET menu_token=NULL WHERE id=?", lid)
        msg = _("Barkaart uitgezet; de oude link werkt niet meer.")
    else:
        db.run("UPDATE locations SET menu_token=? WHERE id=?", secrets.token_urlsafe(12), lid)
        msg = _("Nieuwe link voor de barkaart gemaakt.") if actie == "nieuw" else _("Barkaart staat aan.")
    return redirect(f"/locaties/{lid}/instellingen#barkaart", msg=msg)


def menu_url(request: Request, token: str) -> str:
    return str(request.base_url).rstrip("/") + f"/kaart/{token}"


@router.get("/locaties/{lid}/barkaart.svg")
def menu_qr(request: Request, lid: int, user=Depends(require_user)):
    loc = get_location(request, lid, user)
    if not loc["menu_token"]:
        raise HTTPException(404, "Barkaart staat uit")
    import io

    import segno
    buf = io.BytesIO()
    segno.make(menu_url(request, loc["menu_token"]), error="m").save(buf, kind="svg", scale=8, border=2,
                                                                     dark="#1d2026", light="#ffffff")
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


def _menu_location(request: Request, token: str):
    if not token or len(token) > 64:
        raise HTTPException(404, "Deze barkaart bestaat niet (meer)")
    loc = db_of(request).one("SELECT * FROM locations WHERE menu_token=?", token)
    if not loc:
        raise HTTPException(404, "Deze barkaart bestaat niet (meer)")
    return loc


@router.get("/kaart/{token}", response_class=HTMLResponse)
def menu_page(request: Request, token: str, cat: str = ""):
    loc = _menu_location(request, token)
    db = db_of(request)
    items = [it for it in service.inventory_rows(db, loc["id"]) if it["quantity"] > 0 and it["name"]]
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it["category"], []).append(it)
    for g in groups.values():
        g.sort(key=lambda it: it["name"].lower())
    stock = cocktails.stock(db, [loc["id"]])
    drinks = [c for c in cocktails.evaluate_all(db, stock, True) if c["can"] and c["uses"] and c["boozy"]]
    return render(request, "menu.html", user=None, csrf="", loc=loc, token=token,
                  groups=sorted(groups.items(), key=lambda g: categories.ORDER.get(g[0], 99)),
                  drinks=drinks, n_items=len(items))


@router.get("/kaart/{token}/cocktail/{cid}", response_class=HTMLResponse)
def menu_cocktail(request: Request, token: str, cid: str):
    loc = _menu_location(request, token)
    db = db_of(request)
    c = _cocktail(db, cid)
    m = cocktails.Matcher(db, cocktails.stock(db, [loc["id"]]), True)
    return render(request, "menu_cocktail.html", user=None, csrf="", loc=loc, token=token, c=c,
                  rows=_ingredient_rows(db, c, m))
