"""B2C logistics cost web app."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys

from flask import Flask, jsonify, request, send_from_directory

from pricing import quote_carrier, resolve_zones, CARRIER_BILLING_NAME, price_shadowfax_rvp

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
LANES_DB = os.path.join(DATA_DIR, "lanes.db")
B2C_JSON = os.path.join(DATA_DIR, "b2c_data.json")
STATIC = os.path.join(ROOT, "static")

app = Flask(__name__, static_folder=STATIC)

_DATA = {"d": None}


def load_data():
    with open(B2C_JSON, encoding="utf-8") as f:
        _DATA["d"] = json.load(f)
    return _DATA["d"]


DATA = load_data()
WH_BY_ID = {w["whid"]: w for w in DATA["warehouses"]}
MASTER_COL = DATA["master_col_by_id"]


def _conn():
    return sqlite3.connect(LANES_DB)


def _lanes_sha256():
    h = hashlib.sha256()
    with open(LANES_DB, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


LANES_SHA256 = _lanes_sha256()


@app.route("/api/health")
def health():
    """Liveness + data fingerprint so a stale deploy is obvious."""
    con = _conn()
    rows = con.execute("SELECT COUNT(*) FROM lanes").fetchone()[0]
    whids = [r[0] for r in con.execute("SELECT DISTINCT whid FROM lanes ORDER BY whid")]
    max_pin = con.execute("SELECT MAX(pin) FROM lanes").fetchone()[0]
    con.close()
    return jsonify({
        "status": "ok",
        "lanes_db": {
            "sha256": LANES_SHA256,
            "rows": rows,
            "whids": whids,
            "max_pin": max_pin,
        },
        "b2c_data": {
            "warehouses": len(DATA["warehouses"]),
            "active_carriers": len(DATA["active_carriers"]),
            "price_cards": len(DATA["price_cards"]),
        },
    })


def _norm(v):
    return v


def _sort_carriers(carriers):
    """Cheapest served first, 'not served' rows pushed to the bottom."""
    def key(c):
        return (
            0 if c.get("served") else 1,
            c.get("cost") if c.get("cost") is not None else float("inf"),
        )
    return sorted(carriers, key=key)


@app.route("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.route("/api/bootstrap")
def bootstrap():
    carriers = []
    for c in DATA["active_carriers"]:
        pc = DATA["price_cards"][c["id"]]
        volume_options = []
        volume_key = None
        if "xy" in pc:
            volume_key, volume_options = "xy", list(pc["xy"].keys())
        elif "vol_discount" in pc:
            volume_key, volume_options = "vol_discount", list(pc["vol_discount"].keys())
        elif "cps" in pc:
            volume_key, volume_options = "cps", list(pc["cps"].keys())
        elif "vol_rates" in pc:
            volume_key, volume_options = "vol_rates", list(pc["vol_rates"].keys())
        elif c["id"] == "shadowfax":
            volume_key, volume_options = "rates", ["<4L", ">=4L"]
        carriers.append({
            "id": c["id"],
            "name": c["name"],
            "movements": pc.get("movements") or ["Fwd"],
            "volume_options": volume_options,
            "volume_key": volume_key,
            "defaults": {
                "volume": (volume_options[0] if volume_options else None),
                "movement": (pc.get("movements") or ["Fwd"])[0],
            },
        })
    return jsonify({
        "warehouses": DATA["warehouses"],
        "carriers": carriers,
    })


@app.route("/api/zones")
def lane_zones():
    whid = request.args.get("whid", type=int)
    pin = request.args.get("pin", type=int)
    if whid is None or pin is None:
        return jsonify({"error": "whid and pin required"}), 400
    con = _conn()
    row = con.execute(
        "SELECT origin_pin, city, state, lane, ecom, delhivery, delhivery_mm, shadowfax, "
        "bluedart_plus, dtdc, delhivery_ndd, ekart, amazon "
        "FROM lanes WHERE whid=? AND pin=?",
        (whid, pin),
    ).fetchone()
    con.close()
    if row is None:
        return jsonify({"found": False, "lane": {"whid": whid, "pin": pin}, "carriers": []})

    (origin_pin, city, state, lane, ecom, delhivery, delhivery_mm, shadowfax,
     bluedart_plus, dtdc, delhivery_ndd, ekart, amazon) = row

    master = {
        "delhivery": delhivery,
        "bluedart": bluedart_plus,
        "dtdc": dtdc,
        "ekart": ekart,
        "shadowfax": shadowfax,
        "amazon": amazon,
        "elastic": None,
    }
    carriers = []
    for c in DATA["active_carriers"]:
        zone = master.get(c["id"])
        carriers.append(quote_carrier(c, zone, 0, "Fwd", {"whid": whid}, DATA))
    return jsonify({
        "found": True,
        "lane": {
            "whid": whid,
            "pin": pin,
            "origin_pin": origin_pin,
            "city": city,
            "state": state,
            "lane": lane,
            "wh_code": WH_BY_ID.get(whid, {}).get("code"),
            "wh_state": WH_BY_ID.get(whid, {}).get("state"),
        },
        "carriers": _sort_carriers(carriers),
    })


@app.route("/api/quote", methods=["POST"])
def quote():
    body = request.get_json(force=True) or {}
    whid = body.get("whid")
    pin = body.get("pin")
    weight = float(body.get("weight_kg") or 0)
    movement = (body.get("movement") or "Fwd").upper()
    zone_overrides = body.get("zones") or {}
    volume = body.get("volume") or {}
    elastic_service = body.get("elastic_service") or "standard"

    # Default zones from the lane master (unless user overrode them).
    default_zones = {}
    rec = None
    if whid and pin:
        con = _conn()
        row = con.execute(
            "SELECT whid, origin_pin, pin, city, state, lane, ecom, delhivery, "
            "delhivery_mm, shadowfax, bluedart_plus, dtdc, delhivery_ndd, ekart, amazon "
            "FROM lanes WHERE whid=? AND pin=?", (whid, pin)).fetchone()
        con.close()
        if row:
            cols = ["whid", "origin_pin", "pin", "city", "state", "lane", "ecom", "delhivery",
                    "delhivery_mm", "shadowfax", "bluedart_plus", "dtdc",
                    "delhivery_ndd", "ekart", "amazon"]
            rec = dict(zip(cols, row))
            default_zones = {
                "delhivery": rec["delhivery"],
                "bluedart": rec["bluedart_plus"],
                "dtdc": rec["dtdc"],
                "ekart": rec["ekart"],
                "shadowfax": rec["shadowfax"],
                "amazon": rec["amazon"],
                "elastic": None,
            }

    opts = {"whid": whid, "pin": pin, "volume": volume, "elastic_service": elastic_service}
    results = []
    for c in DATA["active_carriers"]:
        # Explicit override (incl. null = "force not served") wins over the lane default.
        if c["id"] in zone_overrides:
            zone = zone_overrides[c["id"]] or ""
        else:
            zone = default_zones.get(c["id"])
        results.append(quote_carrier(c, zone, weight, movement, opts, DATA))
    results = _sort_carriers(results)
    cheapest = next((c for c in results if c.get("cost") is not None), None)

    # Optional Shadowfax RVP (reverse pickup) quote on the same lane.
    rvp = None
    if body.get("rvp") and rec:
        zone = rec["shadowfax"]
        cost, detail = price_shadowfax_rvp(zone, DATA["price_cards"]["shadowfax"])
        rvp = {"served": cost is not None, "zone": zone, "cost": cost, "rate_basis": detail}

    return jsonify({"carriers": results, "cheapest": cheapest,
                    "movement": movement, "rvp": rvp})


@app.route("/api/rebuild", methods=["POST"])
def rebuild():
    r = subprocess.run([sys.executable, os.path.join(ROOT, "builder.py")],
                       capture_output=True, text=True, timeout=1200)
    if r.returncode != 0:
        return jsonify({"ok": False, "error": r.stderr[-2000:]}), 500
    global DATA, WH_BY_ID, MASTER_COL
    DATA = load_data()
    WH_BY_ID = {w["whid"]: w for w in DATA["warehouses"]}
    MASTER_COL = DATA["master_col_by_id"]
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=False)