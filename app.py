"""B2C logistics cost web app."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
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

_LANE_COLS = ["whid", "origin_pin", "pin", "city", "state", "lane", "ecom", "delhivery",
              "delhivery_mm", "shadowfax", "bluedart_plus", "dtdc", "delhivery_ndd",
              "ekart", "amazon"]
_LANE_SQL = ("SELECT whid, origin_pin, pin, city, state, lane, ecom, delhivery, "
             "delhivery_mm, shadowfax, bluedart_plus, dtdc, delhivery_ndd, ekart, amazon "
             "FROM lanes WHERE ")


def _row_to_dict(row):
    return dict(zip(_LANE_COLS, row)) if row else None


def _fetch_lane(whid, pin):
    if not whid or pin is None:
        return None
    con = _conn()
    row = con.execute(_LANE_SQL + "whid=? AND pin=?", (whid, pin)).fetchone()
    con.close()
    return _row_to_dict(row)


def _compute_carriers(rec, weight, movement, volume, elastic_service, zone_overrides=None):
    """Run every active carrier through the pricing engine for one lane row.

    `rec` is the lanes-table dict or None (lane unknown); returns sorted results.
    """
    zone_overrides = zone_overrides or {}
    default_zones = {}
    if rec:
        default_zones = {
            "delhivery": rec["delhivery"],
            "bluedart": rec["bluedart_plus"],
            "dtdc": rec["dtdc"],
            "ekart": rec["ekart"],
            "shadowfax": rec["shadowfax"],
            "amazon": rec["amazon"],
            "elastic": None,
        }
    opts = {"whid": rec["whid"] if rec else None, "pin": rec["pin"] if rec else None,
            "volume": volume, "elastic_service": elastic_service}
    results = []
    for c in DATA["active_carriers"]:
        if c["id"] in zone_overrides:
            zone = zone_overrides[c["id"]] or ""
        else:
            zone = default_zones.get(c["id"])
        results.append(quote_carrier(c, zone, weight, movement, opts, DATA))
    return _sort_carriers(results)


def _digit6(pin):
    s = re.sub(r"\D", "", str(pin or ""))
    return s if len(s) == 6 else None


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

    rec = _fetch_lane(whid, pin)
    results = _compute_carriers(rec, weight, movement, volume, elastic_service, zone_overrides)
    cheapest = next((c for c in results if c.get("cost") is not None), None)

    # Optional Shadowfax RVP (reverse pickup) quote on the same lane.
    rvp = None
    if body.get("rvp") and rec:
        zone = rec["shadowfax"]
        cost, detail = price_shadowfax_rvp(zone, DATA["price_cards"]["shadowfax"])
        rvp = {"served": cost is not None, "zone": zone, "cost": cost, "rate_basis": detail}

    return jsonify({"carriers": results, "cheapest": cheapest,
                    "movement": movement, "rvp": rvp})


@app.route("/api/bulk_quote", methods=["POST"])
def bulk_quote():
    """Quote many lanes at once, shared volume/weight/movement for all of them.

    mode "pins": one warehouse (whid) x many destination pincodes.
    mode "whs":  many warehouses x one destination pincode.
    """
    body = request.get_json(force=True) or {}
    mode = body.get("mode") or "pins"
    weight = float(body.get("weight_kg") or 0)
    movement = (body.get("movement") or "Fwd").upper()
    volume = body.get("volume") or {}
    elastic_service = body.get("elastic_service") or "standard"
    warnings = []

    con = _conn()
    by_lane = {}  # (whid, pin) -> lane row dict
    lanes = []

    if mode == "whs":
        pin6 = _digit6(body.get("pin"))
        if pin6 is None:
            con.close()
            return jsonify({"error": "Destination pincode must be 6 digits"}), 400
        pin = int(pin6)
        whids = []
        for w in body.get("whids") or []:
            try:
                wid = int(w)
            except (TypeError, ValueError):
                warnings.append(f"skipping non-numeric warehouse {w!r}")
                continue
            if wid in WH_BY_ID:
                whids.append(wid)
            else:
                warnings.append(f"skipping unknown warehouse WH {wid}")
        if not whids:
            con.close()
            return jsonify({"error": "No valid warehouses selected"}), 400
        ins = ",".join("?" * len(whids))
        rows = con.execute(_LANE_SQL + f"pin=? AND whid IN ({ins})", [pin] + whids).fetchall()
        con.close()
        for r in rows:
            by_lane[(r[0], r[2])] = _row_to_dict(r)
        for wid in whids:
            rec = by_lane.get((wid, pin))
            lanes.append(_bulk_lane(rec, wid, pin, pin6, weight, movement, volume, elastic_service))
        fixed = {"kind": "pin", "pin": pin6}
    else:
        try:
            whid = int(body.get("whid"))
        except (TypeError, ValueError):
            con.close()
            return jsonify({"error": "Warehouse required"}), 400
        if whid not in WH_BY_ID:
            con.close()
            return jsonify({"error": f"Unknown warehouse WH {whid}"}), 400
        tokens = body.get("pins") or []
        if isinstance(tokens, str):
            tokens = re.split(r"[\s,;]+", tokens)
        pins = []
        seen = set()
        for t in tokens:
            d = _digit6(t)
            if d is None:
                warnings.append(f"skipping invalid pin {str(t).strip()!r}")
                continue
            i = int(d)
            if i in seen:
                continue
            seen.add(i)
            pins.append((d, i))
        if not pins:
            con.close()
            return jsonify({"error": "No valid 6-digit pincodes provided"}), 400
        ins = ",".join("?" * len(pins))
        rows = con.execute(_LANE_SQL + f"whid=? AND pin IN ({ins})",
                           [whid] + [i for _, i in pins]).fetchall()
        con.close()
        for r in rows:
            by_lane[(r[0], r[2])] = _row_to_dict(r)
        for d, i in pins:
            rec = by_lane.get((whid, i))
            lanes.append(_bulk_lane(rec, whid, i, d, weight, movement, volume, elastic_service))
        fixed = {"kind": "whid", "whid": whid, "code": WH_BY_ID[whid]["code"],
                 "state": WH_BY_ID[whid]["state"]}

    return jsonify({
        "mode": mode,
        "fixed": fixed,
        "carrier_order": [c["id"] for c in DATA["active_carriers"]],
        "lanes": lanes,
        "warnings": warnings,
        "weight": weight,
        "movement": movement,
    })


def _bulk_lane(rec, whid, pin, pin6, weight, movement, volume, elastic_service):
    wh = WH_BY_ID.get(whid, {})
    lane = {
        "whid": whid,
        "wh_code": wh.get("code"),
        "wh_state": wh.get("state"),
        "pin": pin,
        "pin_6": pin6,
        "city": rec["city"] if rec else None,
        "state": rec["state"] if rec else None,
        "found": bool(rec),
        "carriers": [],
        "cheapest": None,
    }
    if rec:
        lane["carriers"] = _compute_carriers(rec, weight, movement, volume, elastic_service)
        lane["cheapest"] = next((c for c in lane["carriers"] if c.get("cost") is not None), None)
    return lane


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