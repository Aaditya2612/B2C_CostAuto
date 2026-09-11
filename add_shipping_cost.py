from __future__ import annotations

import argparse

import pandas as pd

import pricing
from cps_compute import DEFAULT_VOLUME, LANE_COLUMN, _data, _load_lanes, compute_cps

SRC = "bc_sales_export.csv"
OUT = "bc_sales_export_with_cost.csv"
QUOTE_WEIGHT = 0.5  # kg — lane-level representative weight
RTO_CARRIERS = ("delhivery", "bluedart", "ekart")

DATA = _data()
DISPLAY = {c["id"]: c["name"] for c in DATA["active_carriers"]}
ZONES = _load_lanes()          # {(whid, pin): {carrier_col: zone}}
WHIDS = {w for (w, _) in ZONES}
_ORDER = {cid: i for i, cid in enumerate(DISPLAY)}


def resolve_carrier(name):
    """Map a user-supplied carrier to its active-carrier dict (id or display
    name, case-insensitive). Returns None if nothing matches."""
    if not name:
        return None
    key = str(name).strip().lower()
    for c in DATA["active_carriers"]:
        if c["id"].lower() == key or c["name"].lower() == key:
            return c
    return None


def _pick(quotes: dict):
    """(cost, carrier_display) of the cheapest quote (deterministic ties)."""
    if not quotes:
        return None, None
    best = min(quotes.items(), key=lambda kv: (kv[1], _ORDER[kv[0]]))
    return best[1], DISPLAY[best[0]]


def _quote_at(whid: int, pin: int, carrier, movement: str, weight_kg: float):
    """quote_carrier result cost for ONE carrier at a given weight (None-safe)."""
    cid = carrier["id"]
    if cid != "elastic":
        zones = ZONES.get((whid, pin)) or {}
        zone = zones.get(LANE_COLUMN[cid])
        if not zone:
            return None
    else:
        zone = None
    res = pricing.quote_carrier(
        carrier, zone, weight_kg, movement,
        {"whid": whid, "pin": pin, "volume": dict(DEFAULT_VOLUME), "elastic_service": "standard"},
        DATA,
    )
    return round(float(res["cost"]), 2) if res.get("cost") is not None else None


def _quote(whid: int, pin: int, carrier, movement: str):
    """quote_carrier result cost for one carrier at QUOTE_WEIGHT (None-safe)."""
    return _quote_at(whid, pin, carrier, movement, QUOTE_WEIGHT)


def _lane_quotes(whid: int, pin: int):
    """({cid: cost_fwd}, {cid: cost_rto}) for the lane at QUOTE_WEIGHT."""
    fwd, rto = {}, {}
    for carrier in DATA["active_carriers"]:
        cost = _quote(whid, pin, carrier, "Fwd")
        if cost is not None:
            fwd[carrier["id"]] = cost
            if carrier["id"] in RTO_CARRIERS:
                r = _quote(whid, pin, carrier, "RTO")
                if r is not None:
                    rto[carrier["id"]] = r
    return fwd, rto


def _carrier_zone(whid: int, pin: int, cid: str):
    """The zone-master value for a carrier on a lane (None for elastic / none)."""
    if cid == "elastic":
        return None
    return (ZONES.get((whid, pin)) or {}).get(LANE_COLUMN[cid])


def _is_rto(df):
    """Row-level RTO flag: shipment status / latest courier status / RTO dates."""
    return (
        df["shipment_status"].astype(str).eq("Returned")
        | df["latest_secondary_status"].astype(str).str.contains("RTO", case=False, na=False)
        | df["rto_marked_date"].notna()
        | df["rto_received_date"].notna()
    )


def _column_values(df, lane_fwd, lane_rto, carrier=None):
    """Return (costs, carriers, notes[, zones]) for every shipment row.

    carrier=None            -> cheapest carrier per unique (warehouse_id, pincode)
    carrier=<active dict>   -> that one carrier's lane charge (unique (WH, pin, carrier))
    """
    rto_flag = _is_rto(df)

    cid = carrier["id"] if carrier is not None else None
    costs, carriers, notes, zones = [], [], [], []
    for whid, pin, is_rto in zip(
        df["warehouse_id"].astype("Int64"), df["pincode"].astype("Int64"), rto_flag
    ):
        if pd.isna(whid) or pd.isna(pin):
            costs.append(None); carriers.append(None); zones.append(None)
            notes.append("missing warehouse_id/pincode")
            continue
        whid, pin = int(whid), int(pin)
        if whid not in WHIDS:
            costs.append(None); carriers.append(None); zones.append(None)
            notes.append("warehouse not on rate card")
            continue
        key = (whid, pin)
        quotes = (lane_rto.get(key, {}) if is_rto else lane_fwd.get(key, {}))
        has_fwd = lane_fwd.get(key, {})
        had_any = lane_fwd.get(key, {}) or lane_rto.get(key, {})
        zone = None
        if carrier is None:
            cost, disp = _pick(quotes)
            if cost is None:
                if is_rto and has_fwd:
                    note = "RTO not quoted on any carrier"
                else:
                    note = "no servicable carrier on this lane"
            else:
                note = "ok"
        else:
            cost = quotes.get(cid)
            disp = DISPLAY[cid]
            zone = _carrier_zone(whid, pin, cid)
            if cost is None:
                if is_rto and cid in has_fwd:
                    note = "RTO not quoted on this carrier"
                else:
                    note = "no servicable rate for this carrier on this lane"
            else:
                note = "ok"
        costs.append(cost); carriers.append(disp); notes.append(note); zones.append(zone)
    return costs, carriers, notes, zones if cid is not None else None


def add_shipping_cost_columns(df, carrier=None):
    """Shipping cost per shipment.

    carrier=None -> cheapest carrier per unique lane (shipping_cost + shipping_carrier).
    carrier=<dict> -> that one carrier's lane charge; also appends its lane zone.

    The lane identity spans (warehouse_id, pincode) and, when a carrier is an
    input, that carrier as well: every unique combination resolves to the
    charge for exactly that carrier.
    """
    df = df.copy()
    lanes = (
        df.loc[df["warehouse_id"].isin(WHIDS), ["warehouse_id", "pincode"]]
        .drop_duplicates()
    )
    lane_fwd, lane_rto = {}, {}
    for whid, pin in lanes.itertuples(index=False):
        fwd, rto = _lane_quotes(int(whid), int(pin))
        lane_fwd[(int(whid), int(pin))] = fwd
        lane_rto[(int(whid), int(pin))] = rto

    costs, carriers, notes, zones = _column_values(df, lane_fwd, lane_rto, carrier)
    df["shipping_cost"] = costs
    df["shipping_carrier"] = carriers
    df["shipping_cost_note"] = notes
    if zones is not None:
        df["carrier_zone"] = zones
    return df


def add_carrier_cost_columns(df, carrier, weight_col="chargeable_weight_kg"):
    """One chosen carrier, priced PER SHIPMENT at that shipment's own chargeable
    weight.  Correct per-carrier weight slabs therefore apply to every row
    (e.g. DTDC flat first-5000g, Ekart 0-5kg base then per-500g, Bluedart min
    1kg then 500g slabs, Amazon +15/kg above 2kg, Delhivery/Shadowfax flat),
    instead of the lane-level QUOTE_WEIGHT used by the cheapest-carrier mode.

    Appends:
      shipping_cost / shipping_carrier / shipping_cost_note  (same names as the
        cheapest mode, but shipping_cost is that carrier's charge for this row)
      carrier_zone            -> zone used for the carrier on this lane
      <carrier_id>_cost       -> duplicated as an explicit carrier-named column
      <carrier_id>_zone       -> explicit zone column
      <carrier_id>_note       -> explicit note column
    """
    df = df.copy()
    cid = carrier["id"]
    cname = DISPLAY[cid]
    if weight_col not in df.columns:
        df[weight_col] = QUOTE_WEIGHT

    costs, carriers, notes, zones = [], [], [], []
    for whid, pin, w, is_rto in zip(
        df["warehouse_id"].astype("Int64"),
        df["pincode"].astype("Int64"),
        df[weight_col],
        _is_rto(df),
    ):
        if pd.isna(whid) or pd.isna(pin):
            costs.append(None); carriers.append(cname); zones.append(None)
            notes.append("missing warehouse_id/pincode")
            continue
        whid, pin = int(whid), int(pin)
        if whid not in WHIDS:
            costs.append(None); carriers.append(cname); zones.append(None)
            notes.append("warehouse not on rate card")
            continue
        zone = _carrier_zone(whid, pin, cid)
        if w is None or pd.isna(w):
            costs.append(None); carriers.append(cname); zones.append(zone)
            notes.append("no valid weight/vol_weight")
            continue
        movement = "RTO" if is_rto else "Fwd"
        cost = _quote_at(whid, pin, carrier, movement, float(w))
        if cost is None:
            if is_rto:
                note = "RTO not quoted on this carrier"
            else:
                note = "no servicable rate for this carrier at this weight"
        else:
            note = "ok"
        costs.append(cost); carriers.append(cname); notes.append(note); zones.append(zone)

    df["shipping_cost"] = costs
    df["shipping_carrier"] = carriers
    df["shipping_cost_note"] = notes
    df["carrier_zone"] = zones
    df[f"{cid}_cost"] = costs
    df[f"{cid}_zone"] = zones
    df[f"{cid}_note"] = notes
    return df


def add_cost_columns(df, carrier=None):
    df = df.copy()
    if "rto_marked_on" not in df.columns and "rto_marked_date" in df.columns:
        df["rto_marked_on"] = df["rto_marked_date"]
    df = compute_cps(df)
    if carrier is None:
        df = add_shipping_cost_columns(df)
    else:
        df = add_carrier_cost_columns(df, carrier)
    return df


def main():
    parser = argparse.ArgumentParser(description="Compute per-shipment B2C shipping costs.")
    parser.add_argument("in_csv", nargs="?", default=SRC, help="input shipment CSV")
    parser.add_argument("out_csv", nargs="?", default=OUT, help="output CSV")
    parser.add_argument("--carrier", default=None,
                        help="only this carrier (id or name, e.g. delhivery); "
                             "default prices the cheapest carrier per lane")
    args = parser.parse_args()
    carrier = resolve_carrier(args.carrier)
    if args.carrier and carrier is None:
        print(f"Unknown carrier {args.carrier!r}. Known: "
              + ", ".join(f"{c['id']} ({c['name']})" for c in DATA["active_carriers"]))
        raise SystemExit(2)

    print(f"Reading {args.in_csv} ...", flush=True)
    df = pd.read_csv(args.in_csv, low_memory=False)
    print(f"Loaded {len(df):,} rows. Computing lane quotes ...", flush=True)

    df = add_cost_columns(df, carrier)
    df.to_csv(args.out_csv, index=False)

    scope = f"single carrier: {carrier['name']}" if carrier else "cheapest carrier per lane"
    priced = df["shipping_cost"].notna().sum()
    print(f"rows: {len(df):,}  priced: {priced:,} ({priced / len(df) * 100:.1f}%)  scope: {scope}")
    print()
    print("blank reasons:")
    print(df["shipping_cost_note"].value_counts().to_string())
    print()
    print("carrier share:")
    print(df["shipping_carrier"].value_counts().to_string())
    print()
    print("Preview (top rows of the output dataframe):")
    _cols = [
        "order_id", "awb", "carrier_name", "warehouse_id", "warehouse_name",
        "pincode", "shipment_status", "chargeable_weight_kg",
        "carrier_zone", "shipping_cost", "shipping_carrier", "shipping_cost_note",
    ]
    if carrier is not None:
        _cols += [f"{carrier['id']}_cost", f"{carrier['id']}_zone"]
    show = [c for c in _cols if c in df.columns]
    print(df[show].head(10).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()