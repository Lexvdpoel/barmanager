"""JSON endpoints used by the scan page and the +/- buttons."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from . import categories, service
from .auth import check_csrf, db_of, require_api_user
from .web import get_location, get_product

router = APIRouter(prefix="/api")


class ScanIn(BaseModel):
    barcode: str = Field(max_length=100)
    mode: str = "add"           # add | remove
    qty: int = Field(1, ge=1, le=1000)


class AdjustIn(BaseModel):
    delta: int = Field(ge=-1000, le=1000)


def product_json(request: Request, p, location_id: int | None = None) -> dict:
    db = db_of(request)
    imgs = service.product_images(db, p["id"])
    out = {
        "id": p["id"],
        "barcode": p["barcode"],
        "name": p["name"],
        "display_name": p["name"] or "Onbekend product",
        "brand": p["brand"],
        "category": p["category"],
        "category_label": categories.label(p["category"]),
        "category_color": categories.COLORS.get(p["category"], categories.COLORS["overig"]),
        "categories": [{"key": c, "label": categories.label(c), "color": categories.COLORS[c]}
                       for c in service.product_categories(p)],
        "volume_ml": p["volume_ml"],
        "abv": p["abv"],
        "status": p["lookup_status"],
        "source": p["source"],
        "image": f"/images/{imgs[0]['filename']}" if imgs else None,
        "images": len(imgs),
        "url": f"/producten/{p['id']}" + (f"?loc={location_id}" if location_id else ""),
    }
    if location_id:
        row = db.one("SELECT quantity FROM inventory WHERE location_id=? AND product_id=?",
                     location_id, p["id"])
        out["quantity"] = row["quantity"] if row else 0
    return out


@router.post("/locaties/{lid}/scan")
def scan(request: Request, lid: int, body: ScanIn, user=Depends(require_api_user)):
    check_csrf(request, request.headers.get("x-csrf-token"))
    get_location(request, lid, user)
    code = service.normalize_barcode(body.barcode)
    if not service.valid_barcode(code):
        raise HTTPException(400, "Ongeldige barcode")
    db = db_of(request)
    if body.mode == "remove":
        p = db.one("SELECT * FROM products WHERE barcode=?", code)
        if not p:
            raise HTTPException(404, f"Barcode {code} is onbekend, er valt niets af te boeken")
        row = db.one("SELECT quantity FROM inventory WHERE location_id=? AND product_id=?", lid, p["id"])
        if not row or row["quantity"] <= 0:
            out = product_json(request, p, lid)
            out.update(created=False, applied=0, warning="Niet (meer) op voorraad op deze locatie")
            return out
        before = row["quantity"]
        after = service.adjust(db, lid, p["id"], -body.qty, user["id"], "scan")
        out = product_json(request, p, lid)
        out.update(created=False, applied=after - before)
        return out
    p, created = service.get_or_create_product(db, code)
    service.adjust(db, lid, p["id"], body.qty, user["id"], "scan")
    if created:
        request.app.state.worker.enqueue(p["id"], "lookup")
        p = db.one("SELECT * FROM products WHERE id=?", p["id"])
    out = product_json(request, p, lid)
    out.update(created=created, applied=body.qty)
    return out


@router.post("/locaties/{lid}/items/{pid}/aanpassen")
def adjust(request: Request, lid: int, pid: int, body: AdjustIn, user=Depends(require_api_user)):
    check_csrf(request, request.headers.get("x-csrf-token"))
    get_location(request, lid, user)
    p = get_product(request, pid)
    service.adjust(db_of(request), lid, pid, body.delta, user["id"], "handmatig")
    return product_json(request, p, lid)


@router.get("/producten/{pid}")
def product(request: Request, pid: int, loc: int | None = None, user=Depends(require_api_user)):
    p = get_product(request, pid)
    if loc:
        get_location(request, loc, user)
    return product_json(request, p, loc)
