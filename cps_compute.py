"""Per-shipment freight (CPS) calculation for the B2C shipment export.

Appends a computed freight cost to every row of the BigQuery shipment
DataFrame using the exact same rate-card engine as the B2C cost-calculator
web app (see pricing.py + data/lanes.db + data/b2c_data.json in this
project).

Drop this file next to pricing.py and the data/ folder on the server, then:

    from cps_compute import compute_cps
    df = compute_cps(df)

Added columns
-------------
whid_resolved          integer rate-card warehouse id matched for the row
carrier_id_mapped      rate-card carrier id matched for the row
movement_used          "Fwd" / "RTO" (auto-detected)
chargeable_weight_kg   billing weight = max(weight, vol_weight), in kg
calculated_freight     Rs per shipment per the rate card (None if not quotable)
carrier_zone_used      zone string used for the rate lookup
freight_rate_basis     verbose basis (the "rate_basis" string from pricing.py)
freight_status         ok / machine-readable reason when calculated_freight is None

Defaults used (same as the web app's out-of-the-box behaviour):
* movement -> Fwd (RTO auto-detected from status columns)
* volume tier -> each carrier's lowest tier (no monthly-volume discounts)
* elastic service -> standard == auto (local Rs 37 / regional Rs 40, only
  for WH 2/4/10/12/28; other warehouses are not served -- matching the app)
* Delhivery <1 Lakh forward is intentionally NOT quoted on A/B lanes
  (C1..F etc. still quote) -- identical to the app.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from functools import lru_cache

import pandas as pd

import pricing

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(THIS_DIR, "data")
LANES_DB = os.path.join(DATA_DIR, "lanes.db")
B2C_JSON = os.path.join(DATA_DIR, "b2c_data.json")

# Purplle stores shipment weights in grams (0.5 kg = 500).
# Set to False if the export already provides charges in kg.
WEIGHT_IS_GRAMS = True

# carrier_id -> lanes.db column holding that carrier's zone (elastic is WH-based).
LANE_COLUMN = {
    "delhivery": "delhivery",
    "bluedart": "bluedart_plus",
    "dtdc": "dtdc",
    "ekart": "ekart",
    "shadowfax": "shadowfax",
    "velocity": "velocity",
    "amazon": "amazon",
    "elastic": None,
}

# Purplle shipment carrier-name fragments -> rate-card carrier id.
# Matched on lowercased, non-alphanumeric-stripped text (substring).
CARRIER_ALIASES = (
    ("ekart", "ekart"),
    ("delhivery", "delhivery"),
    ("blue dart", "bluedart"),
    ("dart plus", "bluedart"),
    ("dtdc", "dtdc"),
    ("shadowfax", "shadowfax"),
    ("velocity", "velocity"),
    ("amazon", "amazon"),
    ("elastic", "elastic"),
)

# Lowest tier per carrier = no monthly-volume discount (matches app defaults).
# Only carriers whose pricing actually consumes a volume tier are listed;
# others are priced at their base rate automatically by pricing.py.
DEFAULT_VOLUME = {
    "delhivery": "<1 Lakh",
    "bluedart": "Up to 1,50,000",
    "ekart": "Base rates (Rs 41)",
    "amazon": "<2 Lakh",
}

# Manual override for warehouse matching when the export's warehouse name does
# not resolve. Format: {"warehouse_name as it appears in the CSV": whid}
WH_NAME_MAP = {}


def _norm(value):
    """Lowercase + strip to digits/letters for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


@lru_cache(maxsize=1)
def _data():
    with open(B2C_JSON, encoding="utf-8") as fh:
        return json.load(fh)


def _kg(value):
    """Convert the export's weight to kg. None-safe; ignores non-positive/NaN."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value != value or value <= 0:  # NaN guard
        return None
    return value / 1000.0 if WEIGHT_IS_GRAMS else value


def _clean_pin(value):
    try:
        text = str(value).strip()
        if not text:
            return None
        return int(text.replace(",", "").split(".")[0])
    except (TypeError, ValueError):
        return None


def carrier_id_for(raw_name):
    """Map the export's carrier_name to a rate-card carrier id (or None)."""
    n = _norm(raw_name)
    if not n:
        return None
    for key, cid in CARRIER_ALIASES:
        if _norm(key) in n:
            return cid
    return None


def _build_wh_index():
    """Index the rate-card warehouses by id / origin pin / normalized name."""
    by_id, by_pin, by_name = {}, {}, {}
    for w in _data()["warehouses"]:
        by_id[int(w["whid"])] = w["whid"]
        if w.get("origin_pin"):
            by_pin[int(w["origin_pin"])] = w["whid"]
            by_name[_norm(w["origin_pin"])] = w["whid"]
        for key in ("code", "address"):
            if w.get(key):
                by_name[_norm(w[key])] = w["whid"]
    return by_id, by_pin, by_name


def resolve_whid(row, wh_by_id, wh_by_pin, wh_by_name):
    """Return (rate-card whid, how-matched) for a shipment row."""
    raw_id = row.get("warehouse_id")
    try:
        wid = int(raw_id)
        if wid in wh_by_id:
            return wid, "by id"
    except (TypeError, ValueError):
        pass

    name = _norm(row.get("warehouse_name"))
    for key, whid in WH_NAME_MAP.items():
        if _norm(key) and (_norm(key) in name or name in _norm(key)):
            return whid, "manual override"

    if name:
        if name in wh_by_name:
            return wh_by_name[name], "by name"
        for key, whid in wh_by_name.items():  # partial containment match
            if key and (name in key or key in name):
                return whid, "by name (partial)"

    pin = _clean_pin(row.get("postal_code"))
    if pin and pin in wh_by_pin:
        return wh_by_pin[pin], "by origin pin"

    return None, f"unmatched whid={raw_id!r} name={row.get('warehouse_name')!r}"


def _movement_for(row):
    sec = str(row.get("latest_secondary_status") or "").lower()
    st = str(row.get("shipment_status") or "").lower()
    if "rto" in sec or "rto" in st or "return" in st:
        return "RTO"
    rto_dt = row.get("rto_marked_on")
    if rto_dt is not None and str(rto_dt) not in ("", "NaT", "nan") and not pd.isna(rto_dt):
        return "RTO"
    return "Fwd"


@lru_cache(maxsize=1)
def _load_lanes():
    con = sqlite3.connect(LANES_DB)
    con.row_factory = sqlite3.Row
    lanes = {
        (r["whid"], r["pin"]): {key: r[key] for key in LANE_COLUMN.values() if key}
        for r in con.execute(
            "SELECT whid, pin, delhivery, bluedart_plus, dtdc, ekart, "
            "shadowfax, velocity, amazon FROM lanes"
        )
    }
    con.close()
    return lanes


def zones_for(whid, pin):
    return _load_lanes().get((whid, pin)) or {}


def freight_for_row(carrier_id, whid, pin, weight_kg, movement):
    """Compute the rate-card freight for one shipment.

    Returns (cost, zone, rate_basis, status). cost is None when the shipment
    cannot be priced; status gives a machine-readable reason.
    """
    if carrier_id is None:
        return None, None, "carrier not modelled", "no carrier"
    zone = None
    if carrier_id != "elastic":
        zones = zones_for(whid, pin)
        col = LANE_COLUMN[carrier_id]
        if zones.get(col) is None:
            if not zones:
                return None, None, "lane (whid, pin) not in zone master", "no lane"
            return None, None, "carrier not served on this lane", "no zone"
        zone = zones[col]
    carrier = {"id": carrier_id, "name": carrier_id}
    opts = {"whid": whid, "pin": pin, "volume": dict(DEFAULT_VOLUME), "elastic_service": "standard"}
    try:
        res = pricing.quote_carrier(carrier, zone, weight_kg, movement, opts, _data())
    except Exception as exc:  # defensive: never break the whole export
        return None, zone, f"quote error: {exc}", "quote error"
    if res.get("cost") is None:
        return None, zone, res.get("rate_basis") or "not served", "no quote"
    return round(float(res["cost"]), 2), zone, res.get("rate_basis") or "", "ok"


def compute_cps(df):
    """Append the calculated-freight columns to the shipment DataFrame."""
    df = df.copy()
    wh_by_id, wh_by_pin, wh_by_name = _build_wh_index()

    whids, how_matched, cids, movements = [], [], [], []
    chargeable, freights, zones_used, bases, statuses = [], [], [], [], []

    for _, row in df.iterrows():
        whid, how = resolve_whid(row, wh_by_id, wh_by_pin, wh_by_name)
        whids.append(whid)
        how_matched.append(how)

        carrier_name = row.get("carrier_name")
        if not carrier_name:
            carrier_name = row.get("picklist_carrier_name")
        cid = carrier_id_for(carrier_name)
        cids.append(cid)

        mv = _movement_for(row)
        movements.append(mv)

        weight_kg = _kg(row.get("weight"))
        vol_kg = _kg(row.get("vol_weight"))
        if weight_kg is None and vol_kg is None:
            cw = None
        elif weight_kg is None:
            cw = vol_kg
        elif vol_kg is None:
            cw = weight_kg
        else:
            cw = max(weight_kg, vol_kg)  # billing weight
        chargeable.append(cw)

        pin = _clean_pin(row.get("ship_postal_code")) or _clean_pin(row.get("pincode"))
        if whid is None or cw is None or pin is None:
            freights.append(None)
            zones_used.append(None)
            if whid is None:
                bases.append("warehouse not resolved"); statuses.append("no wh")
            elif cw is None:
                bases.append("no valid weight/vol_weight"); statuses.append("no weight")
            else:
                bases.append("no valid destination pincode"); statuses.append("no pin")
            continue

        cost, zone, basis, status = freight_for_row(cid, whid, pin, cw, mv)
        freights.append(cost)
        zones_used.append(zone)
        bases.append(basis)
        statuses.append(status)

    df["whid_resolved"] = whids
    df["wh_matched_by"] = how_matched
    df["carrier_id_mapped"] = cids
    df["movement_used"] = movements
    df["chargeable_weight_kg"] = chargeable
    df["calculated_freight"] = freights
    df["carrier_zone_used"] = zones_used
    df["freight_rate_basis"] = bases
    df["freight_status"] = statuses
    return df


def freight_summary(df):
    """One-line + per-status counts so the driver can log a summary."""
    ok = df["calculated_freight"].notna()
    text = [
        f"[CPS] freight computed for {ok.mean()*100:.1f}% of rows",
        f"(mean Rs {df.loc[ok, 'calculated_freight'].mean():,.2f})",
    ]
    for status, count in df["freight_status"].value_counts().items():
        text.append(f"{status}: {count:,}")
    return " | ".join(text)


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        out = pd.read_csv(sys.argv[1])
        out = compute_cps(out)
        out.to_csv(sys.argv[2] if len(sys.argv) > 2 else sys.argv[1], index=False)
        print(freight_summary(out))
    else:
        print("usage: python cps_compute.py <shipments.csv> [out.csv]")