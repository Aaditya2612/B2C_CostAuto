"""Build ONE consolidated query so the chain
    warehouse_id + destination_pincode + carrier  ->  charge
works in a single BigQuery run.

Emits:
  pricing_lookup_all.sql  - ALL unique lanes x every carrier -> charge, in one
                            query (long format: one row per lane x carrier; the
                            lane->charges for all 7 carriers are embedded wide as
                            a compact UNNEST string array).  Search with a simple
                            WHERE warehouse_id=.. AND pincode=.. AND carrier='..'.
  queryGiven_joined.sql   - the query from desktop/queryGiven.txt (the shipment
                            query that carries a carrier per shipment) LEFT JOINed
                            with the same lookup; every shipment row gets
                            carrier_charge for ITS carrier on its lane.

Charges = the web app engine's Fwd quote at the representative weight
(default 0.5 kg) - identical to add_shipping_cost._quote_at (the rate-card +
zone-master rulesets the /api/quote endpoint uses).  NULL = carrier not served
on that lane or no servicable rate at that weight.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import add_shipping_cost as asc  # noqa: E402

CIDS = ["delhivery", "bluedart", "dtdc", "ekart", "shadowfax", "amazon", "elastic"]
RW = 0.5  # representative weight kg


def all_lanes(data_csv):
    df = pd.read_csv(data_csv, usecols=["warehouse_id", "pincode"], low_memory=False)
    lanes = df.dropna().drop_duplicates()
    recs = {}
    for whid, pin in lanes.itertuples(index=False):
        whid, pin = int(whid), int(pin)
        if whid not in asc.WHIDS:
            continue
        d = {}
        for c in asc.DATA["active_carriers"]:
            cost = asc._quote_at(whid, pin, c, "Fwd", RW)
            if cost is not None:
                d[c["id"]] = round(float(cost), 2)
        if d:
            recs[(whid, pin)] = d
    return recs


def encode_parts(wh, pin, d, include_zones):
    parts = [str(wh), str(pin)]
    for f in CIDS:
        c = d.get(f)
        z = asc._carrier_zone(wh, pin, f) if (include_zones and c is not None) else None
        if include_zones:
            parts.append("" if z is None else z)
        if c is None:
            parts.append("")
        else:
            parts.append(f"{c:.2f}")
    return parts


def wide_cte(recs, include_zones):
    rowstrs = [
        "'" + "|".join(encode_parts(w, p, d, include_zones)) + "'"
        for (w, p), d in sorted(recs.items())
    ]
    vals = ",\n".join(rowstrs)
    cols = [
        "CAST(SPLIT(v, '|')[OFFSET(0)] AS INT64) AS warehouse_id",
        "CAST(SPLIT(v, '|')[OFFSET(1)] AS INT64) AS pincode",
    ]
    idx = 2
    for f in CIDS:
        if include_zones:
            cols.append(f"SPLIT(v, '|')[OFFSET({idx})] AS {f}_zone")
            idx += 1
        cols.append(f"SAFE_CAST(SPLIT(v, '|')[OFFSET({idx})] AS FLOAT64) AS {f}_charge")
        idx += 1
    sel = ",\n".join(f"    {c}" for c in cols)
    return (
        "price_lookup AS (\n"
        "  SELECT\n" + sel + "\n"
        "  FROM UNNEST([\n" + vals + "\n"
        "  ]) v\n"
        ")"
    )


def write_search(recs, include_zones, out="pricing_lookup_all.sql"):
    cte = wide_cte(recs, include_zones)
    carriers_literal = ",\n".join(
        f"    STRUCT('{f}' AS carrier_id, '{asc.DISPLAY[f]}' AS carrier)"
        for f in CIDS)
    car = "CASE c.carrier_id\n" + "".join(
        f"    WHEN '{f}' THEN pl.{f}_charge\n" for f in CIDS) + "  END"
    sql = (
        "/* ONE self-contained BigQuery query: EVERY unique lane x every carrier "
        "-> the charge for that carrier on that lane.\n"
        f"   Lane->charges for all 7 carriers are embedded (wide; {len(recs):,} "
        "unique lanes, 0.5 kg Fwd representative, computed with the web app's\n"
        "   rate-card rulesets).  NULL charge = that carrier does not serve the lane.\n"
        "   To search: add  WHERE warehouse_id = .. AND pincode = ..\n"
        "               AND carrier_id = 'dtdc'   (at the end). */\n"
        "WITH " + cte + ",\n"
        "carriers AS (\n"
        "  SELECT * FROM UNNEST([\n" + carriers_literal + "\n"
        "  ]) c\n"
        ")\n"
        "SELECT\n"
        "  pl.warehouse_id,\n"
        "  pl.pincode,\n"
        "  c.carrier_id,\n"
        "  c.carrier,\n"
        "  " + car + " AS charge\n"
        "FROM price_lookup pl\n"
        "CROSS JOIN carriers c\n"
        "ORDER BY pl.warehouse_id, pl.pincode, c.carrier_id;\n"
    )
    with open(os.path.join(ROOT, out), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(sql)
    kb = len(sql.encode("utf-8")) / 1024
    print(f"wrote {out}: {len(recs):,} lane rows -> {len(recs) * len(CIDS):,} "
          f"lane x carrier rows, {kb:,.0f} KB")
    return kb


def write_joined(recs, include_zones, given, out="queryGiven_joined.sql"):
    src = open(given, encoding="utf-8").read()
    i = src.index("-- 10. Final Projection")
    selstart = src.index("SELECT", i)
    fromm = src.index("FROM calculated_metrics", selstart)
    selcols = src[selstart + len("SELECT"):fromm].rstrip()
    tail = src[fromm + len("FROM calculated_metrics"):].rstrip()
    tail = re.sub(r"ORDER BY edd_date DESC, effective_shipment_id;?\s*$", "", tail).rstrip()

    car_chg = ("CASE LOWER(COALESCE(carrier_name, ''))\n" + "".join(
        f"    WHEN '{f}' THEN pl.{f}_charge\n" for f in CIDS) + "    ELSE NULL\n  END")
    car_zn = ("CASE LOWER(COALESCE(carrier_name, ''))\n" + "".join(
        f"    WHEN '{f}' THEN pl.{f}_zone\n" for f in CIDS) + "    ELSE NULL\n  END"
        if include_zones else "NULL")

    cte = wide_cte(recs, include_zones)
    body = (
        ",\n" + cte + ",\n"
        "priced AS (\n"
        "  SELECT cm.*, " + car_chg + " AS carrier_charge,\n"
        "         " + car_zn + " AS carrier_zone\n"
        "  FROM calculated_metrics cm\n"
        "  LEFT JOIN price_lookup pl\n"
        "    ON pl.warehouse_id = cm.warehouse_id AND pl.pincode = cm.pincode\n"
        ")"
    )
    final = (
        "\n-- 10. Final Projection\nSELECT" + selcols + ",\n"
        "  carrier_charge,\n  carrier_zone\n"
        "FROM priced\n"
        "ORDER BY edd_date DESC, effective_shipment_id;\n"
    )
    out_sql = (
        "/* queryGiven.txt (shipment query; carries a carrier per shipment) joined\n"
        "   with the all-carrier lane lookup: every row resolves the charge for ITS\n"
        f"   carrier on its (warehouse_id, pincode) lane @0.5kg. Self-contained. */\n"
        + src[:i] + body + final
    )
    path = os.path.join(ROOT, out)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out_sql)
    kb = len(out_sql.encode("utf-8")) / 1024
    print(f"wrote {out}: {len(recs):,} lookup rows, {kb:,.0f} KB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="bc_sales_export.csv")
    ap.add_argument("--given", default=r"C:\Users\Administrator\Desktop\queryGiven.txt")
    ap.add_argument("--zones", action="store_true", help="include zone columns "
                                                         "(may exceed 1 MB)")
    args = ap.parse_args()
    if not os.path.isabs(args.data):
        args.data = os.path.join(ROOT, args.data)
    if not os.path.exists(args.given):
        raise SystemExit(f"--given file not found: {args.given}")

    recs = all_lanes(args.data)
    zones = args.zones
    kb = write_search(recs, zones)
    if kb > 950 and zones:
        print("--zones puts it over budget; dropping zone columns (charges only).")
        zones = False
        write_search(recs, False)
    write_joined(recs, zones, args.given)


if __name__ == "__main__":
    main()