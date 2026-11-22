"""Shopping list and drinking statistics."""

from __future__ import annotations

import time
from datetime import datetime

from . import categories
from .db import Database
from .service import accessible_location_ids, product_categories

MONTHS_NL = ["jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
LOW_LEVEL = 25          # last bottle at or below this % -> on the shopping list


# ---- shopping list -------------------------------------------------------------------------------
def visible_locations(db: Database, user) -> list:
    allowed = accessible_location_ids(db, user)
    rows = db.all("SELECT * FROM locations ORDER BY name COLLATE NOCASE")
    return [r for r in rows if allowed is None or r["id"] in allowed]


def shopping_list(db: Database, user) -> dict:
    """{'groups': [(location, [entries])], 'free': [entries], 'count': n, 'cost': euros}"""
    locs = visible_locations(db, user)
    ids = [l["id"] for l in locs]
    groups, count, cost = [], 0, 0.0
    if ids:
        marks = ",".join("?" * len(ids))
        hidden = {(r["location_id"], r["product_id"]) for r in db.all(
            f"SELECT location_id, product_id FROM shopping_items WHERE kind='verborgen' AND location_id IN ({marks})",
            *ids)}
        manual = {}
        for r in db.all(f"""SELECT s.*, u.username FROM shopping_items s LEFT JOIN users u ON u.id = s.created_by
                            WHERE s.kind='handmatig' AND s.product_id IS NOT NULL AND s.location_id IN ({marks})""",
                        *ids):
            manual[(r["location_id"], r["product_id"])] = r
        rows = db.all(f"""
            SELECT i.*, p.name, p.brand, p.barcode, p.category, p.extra_categories, p.volume_ml, l.track_levels,
                   (SELECT filename FROM product_images pi WHERE pi.product_id = p.id
                    ORDER BY position, id LIMIT 1) AS image
            FROM inventory i JOIN products p ON p.id = i.product_id JOIN locations l ON l.id = i.location_id
            WHERE i.location_id IN ({marks})""", *ids)
        per_loc: dict[int, list] = {}
        for r in rows:
            key = (r["location_id"], r["product_id"])
            reason = auto_reason(r)
            entry = None
            if key in manual:
                m = manual[key]
                entry = {"kind": "handmatig", "id": m["id"],
                         "reason": f"door {m['username']}" if m["username"] else "handmatig toegevoegd"}
                if reason:
                    entry["reason"] = reason
            elif reason and key not in hidden:
                entry = {"kind": "auto", "id": None, "reason": reason}
            if not entry:
                continue
            entry.update(product_id=r["product_id"], location_id=r["location_id"],
                         name=r["name"] or f"Onbekend product ({r['barcode']})", image=r["image"],
                         quantity=r["quantity"], price=r["price"], shop=r["shop"],
                         cats=product_categories(r))
            per_loc.setdefault(r["location_id"], []).append(entry)
            count += 1
            cost += r["price"] or 0
        for l in locs:
            entries = per_loc.get(l["id"])
            if entries:
                entries.sort(key=lambda e: (categories.ORDER.get(e["cats"][0], 99), e["name"].lower()))
                groups.append((l, entries))
    free = [dict(r) for r in db.all(
        """SELECT s.*, u.username FROM shopping_items s LEFT JOIN users u ON u.id = s.created_by
           WHERE s.kind='handmatig' AND s.product_id IS NULL AND (s.created_by=? OR ?)
           ORDER BY s.created_at""", user["id"], 1 if user["is_admin"] else 0)]
    count += len(free)
    return {"groups": groups, "free": free, "count": count, "cost": round(cost, 2)}


def auto_reason(r) -> str:
    q, mn = r["quantity"], r["min_quantity"]
    if mn and q <= mn:
        return "op" if q <= 0 else f"onder het minimum ({q} van {mn})"
    if r["track_levels"]:
        if q <= 0:
            return "op"
        if q == 1 and r["open_level"] is not None and r["open_level"] <= LOW_LEVEL:
            return f"laatste fles bijna leeg ({r['open_level']}%)"
    return ""


def on_list(db: Database, location_id: int | None, product_id: int) -> bool:
    return bool(db.one("SELECT 1 FROM shopping_items WHERE kind='handmatig' AND product_id=? "
                       "AND (location_id=? OR (? IS NULL AND location_id IS NULL))",
                       product_id, location_id, location_id))


def add_item(db: Database, user, location_id: int | None, product_id: int | None, name: str = "") -> None:
    if product_id and on_list(db, location_id, product_id):
        return
    if product_id:
        db.run("DELETE FROM shopping_items WHERE kind='verborgen' AND product_id=? AND location_id IS ?",
               product_id, location_id)
    db.run("INSERT INTO shopping_items(location_id, product_id, name, kind, created_by, created_at) "
           "VALUES (?,?,?,?,?,?)", location_id, product_id, name.strip()[:150], "handmatig", user["id"], time.time())


def remove_item(db: Database, location_id: int | None, product_id: int | None, item_id: int | None) -> None:
    """Take something off the list. Automatic entries are hidden until the stock goes up again."""
    if item_id:
        db.run("DELETE FROM shopping_items WHERE id=?", item_id)
    if product_id and location_id:
        db.run("DELETE FROM shopping_items WHERE kind='handmatig' AND product_id=? AND location_id=?",
               product_id, location_id)
        r = db.one("""SELECT i.*, l.track_levels FROM inventory i JOIN locations l ON l.id = i.location_id
                      WHERE i.location_id=? AND i.product_id=?""", location_id, product_id)
        if r and auto_reason(r):
            db.run("INSERT INTO shopping_items(location_id, product_id, kind, created_at) VALUES (?,?,?,?)",
                   location_id, product_id, "verborgen", time.time())


# ---- statistics ----------------------------------------------------------------------------------
def _month_key(ts: float) -> tuple[int, int]:
    d = datetime.fromtimestamp(ts)
    return d.year, d.month


def stats(db: Database, location_id: int, months: int = 6) -> dict:
    now = datetime.now()
    y, m = now.year, now.month
    keys = []
    for _ in range(months):
        keys.append((y, m))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    keys.reverse()
    since = datetime(keys[0][0], keys[0][1], 1).timestamp()

    rows = db.all("""SELECT g.*, p.name, p.brand, p.barcode, p.category, p.extra_categories
                     FROM level_log g JOIN products p ON p.id = g.product_id
                     WHERE g.location_id=? AND g.created_at >= ? ORDER BY g.created_at""", location_id, since)
    per_month = {k: {"ml": 0.0, "bottles": 0, "value": 0.0, "bought": 0, "spent": 0.0} for k in keys}
    per_cat: dict[str, float] = {}
    per_prod: dict[int, dict] = {}
    for r in rows:
        k = _month_key(r["created_at"])
        if k in per_month:
            per_month[k]["ml"] += r["ml"]
            per_month[k]["bottles"] += r["bottles"]
            per_month[k]["value"] += r["value"]
        per_cat[r["category"]] = per_cat.get(r["category"], 0) + r["ml"]
        pp = per_prod.setdefault(r["product_id"], {"id": r["product_id"], "name": r["name"] or r["barcode"],
                                                   "category": r["category"], "ml": 0.0, "bottles": 0,
                                                   "value": 0.0})
        pp["ml"] += r["ml"]
        pp["bottles"] += r["bottles"]
        pp["value"] += r["value"]

    for r in db.all("""SELECT mv.delta, mv.created_at, i.price FROM movements mv
                       LEFT JOIN inventory i ON i.location_id = mv.location_id AND i.product_id = mv.product_id
                       WHERE mv.location_id=? AND mv.created_at >= ? AND mv.delta > 0
                       AND mv.reason IN ('scan', 'handmatig')""", location_id, since):
        k = _month_key(r["created_at"])
        if k in per_month:
            per_month[k]["bought"] += r["delta"]
            per_month[k]["spent"] += r["delta"] * (r["price"] or 0)

    # How long does a bottle last: from opening to finished, per product.
    durations = []
    opened: dict[int, float] = {}
    for r in db.all("SELECT product_id, event, created_at FROM level_log WHERE location_id=? ORDER BY created_at",
                    location_id):
        if r["event"] == "open":
            opened[r["product_id"]] = r["created_at"]
        elif r["event"] == "leeg" and r["product_id"] in opened:
            t0 = opened.pop(r["product_id"])
            if r["created_at"] >= since:
                durations.append((r["created_at"] - t0) / 86400)

    open_now = db.one("SELECT COUNT(*) AS n FROM inventory WHERE location_id=? AND open_level IS NOT NULL "
                      "AND quantity > 0", location_id)["n"]
    month_list = [{"label": f"{MONTHS_NL[k[1] - 1]}" + (f" '{k[0] % 100:02d}" if k[1] == 1 or i == 0 else ""),
                   "liters": v["ml"] / 1000, **v} for i, (k, v) in enumerate(per_month.items())]
    total_ml = sum(v["ml"] for v in per_month.values())
    return {
        "months": month_list,
        "max_liters": max([x["liters"] for x in month_list] + [0]),
        "max_bought": max([x["bought"] for x in month_list] + [0]),
        "liters": total_ml / 1000,
        "bottles": sum(v["bottles"] for v in per_month.values()),
        "value": sum(v["value"] for v in per_month.values()),
        "spent": sum(v["spent"] for v in per_month.values()),
        "bought": sum(v["bought"] for v in per_month.values()),
        "open_now": open_now,
        "avg_days": (sum(durations) / len(durations)) if durations else None,
        "per_cat": sorted(((c, ml / 1000) for c, ml in per_cat.items() if ml > 0), key=lambda x: -x[1]),
        "top": sorted((p for p in per_prod.values() if p["ml"] > 0), key=lambda p: -p["ml"])[:10],
        "has_data": bool(rows),
    }
