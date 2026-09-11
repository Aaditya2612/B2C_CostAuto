"""Emit a SELF-CONTAINED BigQuery query that resolves
(warehouse_id, destination_pincode, <carrier>) -> charge for every unique lane.

The charge is the web app engine's Fwd quote for that carrier at a fixed
representative weight (default 0.5 kg), identical to add_shipping_cost._quote_at
(the same engine the /api/quote web endpoint uses).  Lane zones + rate-card
results are pre-computed and embedded as a compact UNNEST string array, so the
generated query runs on its own in BigQuery with NO python afterwards.

Usage:
  python pipeline/_make_pricing_sql.py --carrier delhivery [--weight 0.5]
      [--data bc_sales_export.csv]
Out (in repo root): pricing_lookup.sql  (core ask: one row per lane x carrier)
                    pricing_joined.sql  (your fat shipment query, each row gets
                                         <carrier>_charge via LEFT JOIN)
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


def lane_rows(data_csv, carrier, weight):
    df = pd.read_csv(data_csv, usecols=["warehouse_id", "pincode"], low_memory=False)
    lanes = df.dropna().drop_duplicates()
    out, skipped = [], {"no_lane": 0, "no_cost": 0}
    for whid, pin in lanes.itertuples(index=False):
        whid, pin = int(whid), int(pin)
        if whid not in asc.WHIDS:
            skipped["no_lane"] += 1
            continue
        zone = asc._carrier_zone(whid, pin, carrier["id"])
        cost = asc._quote_at(whid, pin, carrier, "Fwd", weight)
        if cost is None:
            skipped["no_cost"] += 1
            continue
        out.append((whid, pin, zone, round(float(cost), 2)))
    out.sort()
    return out, skipped


def price_lookup_cte(rows):
    vals = "\n".join(
        f"    '{w}|{p}|{z}|{c:.2f}'," for w, p, z, c in rows)
    cte = (
        "price_lookup AS (\n"
        "  SELECT\n"
        "    CAST(REGEXP_EXTRACT(v, r'^([0-9]+)\\|') AS INT64) AS warehouse_id,\n"
        "    CAST(REGEXP_EXTRACT(v, r'\\|([0-9]+)\\|') AS INT64) AS pincode,\n"
        "    REGEXP_EXTRACT(v, r'\\|([^|]+)\\|([0-9.]+)$') AS carrier_zone,\n"
        "    SAFE_CAST(REGEXP_EXTRACT(v, r'\\|[^|]+\\|([0-9.]+)$') AS FLOAT64) AS charge\n"
        "  FROM UNNEST([\n"
        f"{vals}\n"
        "  ]) v\n"
        ")"
    )
    return cte


def write_lookup(rows, carrier, weight, csv_path, out):
    header = (
        "/* (warehouse_id, destination_pincode, <carrier>) -> charge for each unique lane.\n"
        f"   carrier            : {carrier['name']} ({carrier['id']})\n"
        f"   representative wt  : {weight} kg, Fwd (rate-card weight slabs applied; use\n"
        f"                        --weight to regenerate for another weight / RTO)\n"
        f"   source lanes       : {csv_path} ({len(rows):,} unique lanes served by {carrier['name']})\n"
        "   charge == the web app engine's quote for this carrier on this lane.\n"
        "   Self-contained BigQuery: run as-is, no python needed.\n*/\n"
    )
    sql = header + "WITH " + price_lookup_cte(rows) + (
        "\nSELECT\n"
        "  warehouse_id,\n"
        "  pincode,\n"
        f"  '{carrier['name']}' AS carrier,\n"
        "  carrier_zone,\n"
        "  charge\n"
        "FROM price_lookup\n"
        "ORDER BY warehouse_id, pincode;\n"
    )
    with open(os.path.join(ROOT, out), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(sql)
    kb = len(sql.encode("utf-8")) / 1024
    print(f"wrote {out}: {len(rows):,} lane x carrier rows, {kb:,.0f} KB "
          f"(BigQuery query-text limit is 1024 KB)")
    return kb


def write_joined(rows, carrier, weight, out="pricing_joined.sql"):
    src = open(os.path.join(ROOT, "pipeline", "bc_sales_pipeline.py"),
               encoding="utf-8").read()
    m = re.search(r"sql_query = \"\"\"(.*?)\"\"\"", src, re.S)
    if not m:
        raise SystemExit("SQL block not found")
    sql = m.group(1).strip("\n")

    i = sql.index("-- 10. Final Projection")
    selstart = sql.index("SELECT", i)
    fromm = sql.index("FROM calculated_metrics", selstart)
    selcols = sql[selstart + len("SELECT"):fromm].rstrip()
    tail = sql[fromm + len("FROM calculated_metrics"):].rstrip()
    tail = re.sub(r"ORDER BY edd_date DESC, effective_shipment_id;?\s*$", "", tail).rstrip()

    ctc = price_lookup_cte(rows)
    body = (
        ",\n" + ctc + ",\n"
        "priced AS (\n"
        "  SELECT cm.*," + " " +
        "pl.carrier_zone AS %s_zone, pl.charge AS %s_charge\n"
        "  FROM calculated_metrics cm\n"
        "  LEFT JOIN price_lookup pl\n"
        "    ON pl.warehouse_id = cm.warehouse_id AND pl.pincode = cm.pincode\n"
        ")" % (carrier["id"], carrier["id"])
    )
    final = (
        "\n-- 10. Final Projection\nSELECT" + selcols + ",\n"
        f"  {carrier['id']}_zone,\n  {carrier['id']}_charge\n"
        "FROM priced\n"
        "ORDER BY edd_date DESC, effective_shipment_id;\n"
    )
    out_sql = sql[:i] + body + final
    out_sql = "/* Your fat B2C shipment query + LEFT JOIN of the pricing lookup:\n" \
        f"   each shipment row carries {carrier['name']} charge for its (wh, pin) lane\n" \
        f"   at {weight} kg Fwd representative weight. Self-contained BigQuery. */\n" + out_sql
    path = os.path.join(ROOT, out)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out_sql)
    kb = len(out_sql.encode("utf-8")) / 1024
    print(f"wrote {out}: {len(rows):,} lookup rows in {kb:,.0f} KB total")
    return kb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--carrier", default="delhivery")
    ap.add_argument("--weight", type=float, default=0.5, help="representative kg (Fwd)")
    ap.add_argument("--data", default="bc_sales_export.csv")
    ap.add_argument("--lookup-only", action="store_true", help="skip the joined variant")
    ap.add_argument("--out", default=None,
                    help="output file (default: pricing_lookup_<carrier_id>.sql)")
    args = ap.parse_args()

    carrier = asc.resolve_carrier(args.carrier)
    if carrier is None:
        raise SystemExit(f"Unknown carrier {args.carrier!r}. Known: "
                         + ", ".join(f"{c['id']}" for c in asc.DATA["active_carriers"]))
    out = args.out or f"pricing_lookup_{carrier['id']}.sql"
    if not os.path.isabs(args.data):
        args.data = os.path.join(ROOT, args.data)
    rows, skipped = lane_rows(args.data, carrier, args.weight)
    print(f"lanes: {len(rows):,} served by {carrier['name']} "
          f"(skipped {skipped['no_lane']:,} no-lane, {skipped['no_cost']:,} not served)")
    write_lookup(rows, carrier, args.weight, args.data, out)
    if not args.lookup_only:
        write_joined(rows, carrier, args.weight)


if __name__ == "__main__":
    main()