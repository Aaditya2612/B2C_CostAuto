

from __future__ import annotations

import pandas as pd

import pricing
from cps_compute import DEFAULT_VOLUME, LANE_COLUMN, _data, _load_lanes

SRC = "bc_sales_export.csv"
OUT = "bc_sales_export_with_cost.csv"
QUOTE_WEIGHT = 0.5  # kg — lane-level representative weight
RTO_CARRIERS = ("delhivery", "bluedart", "ekart")

DATA = _data()
DISPLAY = {c["id"]: c["name"] for c in DATA["active_carriers"]}
ZONES = _load_lanes()          # {(whid, pin): {carrier_col: zone}}
WHIDS = {w for (w, _) in ZONES}
_ORDER = {cid: i for i, cid in enumerate(DISPLAY)}


def _pick(quotes: dict):
    """(cost, carrier_display) of the cheapest quote (deterministic ties)."""
    if not quotes:
        return None, None
    best = min(quotes.items(), key=lambda kv: (kv[1], _ORDER[kv[0]]))
    return best[1], DISPLAY[best[0]]


def _quote(whid: int, pin: int, carrier, movement: str):
    """quote_carrier result cost for one carrier at QUOTE_WEIGHT (None-safe)."""
    cid = carrier["id"]
    if cid != "elastic":
        zones = ZONES.get((whid, pin)) or {}
        zone = zones.get(LANE_COLUMN[cid])
        if not zone:
            return None
    else:
        zone = None
    res = pricing.quote_carrier(
        carrier, zone, QUOTE_WEIGHT, movement,
        {"whid": whid, "pin": pin, "volume": dict(DEFAULT_VOLUME), "elastic_service": "standard"},
        DATA,
    )
    return round(float(res["cost"]), 2) if res.get("cost") is not None else None


def _lane_quotes(whid: int, pin: int):
    """(fwd_quotes, rto_quotes) for the lane at QUOTE_WEIGHT."""
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


def main():
    print(f"Reading {SRC} ...", flush=True)
    df = pd.read_csv(SRC, low_memory=False)
    print(f"Loaded {len(df):,} rows. Computing lane quotes ...", flush=True)

    lanes = (
        df.loc[df["warehouse_id"].isin(WHIDS), ["warehouse_id", "pincode"]]
        .drop_duplicates()
    )
    lane_fwd, lane_rto = {}, {}
    n_lanes = len(lanes)
    for i, (whid, pin) in enumerate(lanes.itertuples(index=False), start=1):
        fwd, rto = _lane_quotes(int(whid), int(pin))
        lane_fwd[(int(whid), int(pin))] = _pick(fwd)
        lane_rto[(int(whid), int(pin))] = _pick(rto)
        if i % 5000 == 0 or i == n_lanes:
            print(f"  lanes priced: {i:,}/{n_lanes:,}", flush=True)
    print("Applying lane costs to shipments ...", flush=True)

    rto_flag = (
        df["shipment_status"].astype(str).eq("Returned")
        | df["latest_secondary_status"].astype(str).str.contains("RTO", case=False, na=False)
        | df["rto_marked_date"].notna()
        | df["rto_received_date"].notna()
    )

    costs, carriers, notes = [], [], []
    for whid, pin, is_rto in zip(
        df["warehouse_id"].astype("Int64"), df["pincode"].astype("Int64"), rto_flag
    ):
        if pd.isna(whid) or pd.isna(pin):
            costs.append(None); carriers.append(None)
            notes.append("missing warehouse_id/pincode")
            continue
        whid, pin = int(whid), int(pin)
        if whid not in WHIDS:
            costs.append(None); carriers.append(None)
            notes.append("warehouse not on rate card")
            continue
        key = (whid, pin)
        if is_rto:
            cost, carrier = lane_rto.get(key, (None, None))
            note = ("ok" if cost is not None
                    else ("RTO not quoted on any carrier" if lane_fwd.get(key, (None, None))[0] is not None
                          else "no servicable carrier on this lane"))
        else:
            cost, carrier = lane_fwd.get(key, (None, None))
            note = "ok" if cost is not None else "no servicable carrier on this lane"
        costs.append(cost); carriers.append(carrier); notes.append(note)

    df["shipping_cost"] = costs
    df["shipping_carrier"] = carriers
    df["shipping_cost_note"] = notes

    print("Writing CSV ...", flush=True)
    df.to_csv(OUT, index=False)
    priced = df["shipping_cost"].notna().sum()
    print(f"rows: {len(df):,}  priced: {priced:,} ({priced/len(df)*100:.1f}%)")
    print()
    print("blank reasons:")
    print(df["shipping_cost_note"].value_counts().to_string())
    print()
    print("carrier share of lowest:")
    print(df["shipping_carrier"].value_counts().to_string())
    print()
    print("Preview (top rows of the output dataframe):")
    _cols = [
        "order_id", "awb", "carrier_name", "warehouse_id", "warehouse_name",
        "pincode", "shipment_status",
        "shipping_cost", "shipping_carrier", "shipping_cost_note",
    ]
    show = [c for c in _cols if c in df.columns]
    print(df[show].head(10).to_string(index=False), flush=True)


if __name__ == "__main__":
    main()