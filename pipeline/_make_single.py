"""Builds bc_sales_pipeline_single.py - a fully standalone, single-file version of
the B2C cost pipeline.

It embeds:
  * the exact BigQuery SQL (taken from pipeline/bc_sales_pipeline.py)
  * the complete rate-card engine (pricing.py inlined; cps_compute + lane-cost
    logic inlined with identical behaviour)
  * data/b2c_data.json as an embedded JSON document
  * data/lanes.db as a zlib/base64-compressed CSV of the zone columns the engine uses

The recipient only needs this one .py file, `pip install pandas google-cloud-bigquery
google-api-python-client google-auth`, and their BigQuery credentials
(GOOGLE_APPLICATION_CREDENTIALS). Running it executes the query, builds the
dataframe, appends the cost columns and prints a preview.

Regenerate whenever the engine/data change:  python pipeline/_make_single.py
"""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import sqlite3
import zlib

ROOT = r"C:\Users\Administrator\Documents\0909costAuto"
OUT = rf"{ROOT}\b2c_cost_calc.py"
DATA_DIR = rf"{ROOT}\data"

LANE_KEYS = ["delhivery", "bluedart_plus", "dtdc", "ekart", "shadowfax", "amazon"]


def embed_lanes():
    """Compress the zone columns the engine needs (the data of lanes.db)."""
    con = sqlite3.connect(rf"{DATA_DIR}\lanes.db")
    cols = ", ".join(LANE_KEYS)
    rows = con.execute(f"SELECT whid, pin, {cols} FROM lanes").fetchall()
    con.close()

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for whid, pin, *zones in rows:
        writer.writerow([whid, pin] + ["" if z is None else z for z in zones])
    raw = buf.getvalue().encode("utf-8")
    b64 = base64.b64encode(zlib.compress(raw, 9)).decode("ascii")
    print(f"lanes: {len(rows):,} rows | raw {len(raw)/1e6:.1f} MB | "
          f"embedded {len(b64)/1e6:.2f} MB")
    return b64


def extract_sql():
    src = open(rf"{ROOT}\pipeline\bc_sales_pipeline.py", encoding="utf-8").read()
    m = re.search(r'sql_query = """(.*?)"""', src, re.S)
    if not m:
        raise SystemExit("SQL block not found in pipeline/bc_sales_pipeline.py")
    return m.group(1).strip("\n")


def inline_pricing():
    with open(rf"{ROOT}\pricing.py", encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r"from __future__ import annotations.*$", "", src, count=1, flags=re.M)
    src = re.sub(r"^import math.*$", "", src, count=1, flags=re.M)
    return src


def strip_cps_defs(src):
    """Remove the file-based _data/_load_lanes + WEIGHT_IS_GRAMS=True from cps
    text: they are re-defined later against the embedded data."""
    def remove_defs(src, names):
        def starts_def(line, n):
            return line.lstrip().startswith("def " + n + "(")

        lines = src.splitlines()
        out = []
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if (stripped == "@lru_cache(maxsize=1)" and i + 1 < len(lines) and
                    any(starts_def(lines[i + 1], n) for n in names)):
                i += 2
                while i < len(lines) and lines[i].strip() and lines[i][0] in " \t":
                    i += 1
                continue
            if any(starts_def(lines[i], n) for n in names):
                i += 1
                while i < len(lines) and (lines[i].strip() == "" or lines[i][0] in " \t"):
                    i += 1
                continue
            out.append(lines[i])
            i += 1
        return "\n".join(out)

    src = remove_defs(src, ("_data", "_load_lanes"))
    src = re.sub(r"^WEIGHT_IS_GRAMS = True.*$", "", src, count=1, flags=re.M)
    src = re.sub(r"^from __future__ import annotations.*$", "", src, count=1, flags=re.M)
    src = re.sub(r"^import pricing.*$", "", src, count=1, flags=re.M)
    src = re.sub(r"^import sqlite3.*$", "", src, count=1, flags=re.M)
    src = re.sub(r"pricing\.", "", src)
    return src.split('if __name__ == "__main__":')[0]


def inline_lane_cost():
    with open(rf"{ROOT}\add_shipping_cost.py", encoding="utf-8") as fh:
        src = fh.read()
    src = re.sub(r"^from __future__ import annotations.*$", "", src, count=1, flags=re.M)
    src = re.sub(r"^import pricing.*$", "", src, count=1, flags=re.M)
    src = re.sub(r"^from cps_compute import .*$", "", src, count=1, flags=re.M)
    src = re.sub(r"pricing\.", "", src)
    # strip module-load-time data/lane loaders: re-defined after the embed.
    for line in ("DATA = _data()", "DISPLAY = {c[\"id\"]: c[\"name\"] for c in DATA[\"active_carriers\"]}",
                 "ZONES = _load_lanes()",
                 "WHIDS = {w for (w, _) in ZONES}",
                 "_ORDER = {cid: i for i, cid in enumerate(DISPLAY)}"):
        src = re.sub(rf"^{re.escape(line)}\s*$", "", src, count=1, flags=re.M)
    src = re.sub(r"\ndef main\(\):.*$", "", src, flags=re.S)   # drop dead CSV main()
    return src.split('if __name__ == "__main__":')[0]


def build():
    data_json = open(rf"{DATA_DIR}\b2c_data.json", encoding="utf-8").read().strip()
    lanes_b64 = embed_lanes()
    sql = extract_sql()

    header = f'''"""bc_sales_pipeline_single.py - standalone B2C cost pipeline.

Fetches the B2C shipment data from BigQuery using the exact SQL below, builds a
dataframe and appends the calculated cost columns with the same rate-card engine
as the B2C cost-calculator web app.  Everything the engine needs (rate cards,
zone-lane data) is embedded in this file, so it is fully standalone.

Requirements:
    pip install pandas google-cloud-bigquery google-api-python-client google-auth

Run (daily pull + cost columns):
    python bc_sales_pipeline_single.py [out.csv] [--carrier <carrier>]
    # --carrier picks ONE carrier (id or name, e.g. delhivery): every shipment
    # is priced for that carrier at ITS OWN chargeable weight (correct weight
    # slabs per carrier), and the explicit <carrier>_cost column carries the charge.
    # GOOGLE_APPLICATION_CREDENTIALS must point at your service-account JSON

Prepared from: pricing.py / cps_compute.py / add_shipping_cost.py /
data/b2c_data.json / data/lanes.db   (project: 0909costAuto).
"""
from __future__ import annotations
import base64, csv, io, json, math, zlib
from functools import lru_cache

import pandas as pd

DATA_JSON = json.loads(r"""{data_json}""")

# Zone-lane data embedded (lanes.db) as zlib+base64 CSV:  whid,pin,<6 zones>
LANES_B64 = """{lanes_b64}"""
LANES_KEYS = {LANE_KEYS!r}
'''
    body = []
    A = body.append
    A("")
    A("def _data():")
    A("    return DATA_JSON")
    A("")
    A("@lru_cache(maxsize=1)")
    A("def _load_lanes():")
    A('    text = zlib.decompress(base64.b64decode(LANES_B64)).decode("utf-8")')
    A("    lanes = {}")
    A("    for row in csv.reader(io.StringIO(text)):")
    A("        whid, pin = int(row[0]), int(row[1])")
    A('        vals = [None if v == "" else v for v in row[2:]]')
    A("        lanes[(whid, pin)] = dict(zip(LANES_KEYS, vals))")
    A("    return lanes")
    A("")
    A("")
    A("WEIGHT_IS_GRAMS = False   # bc_sales_export weights are already in kg")
    A("")
    A("# ---- pricing.py (inlined) ---------------------------------------------")
    A(inline_pricing())
    A("")
    A("# ---- cps_compute logic (inlined) ---------------------------------------")
    A(strip_cps_defs(inline_cps_source()))
    A("")
    A("# ---- lane shipping-cost logic (inlined: add_shipping_cost.py) ----------")
    A(inline_lane_cost())
    A("")
    A("# ---- data/lane sources re-bound to the embedded copies -----------------")
    A("DATA = _data()")
    A('DISPLAY = {c["id"]: c["name"] for c in DATA["active_carriers"]}')
    A("ZONES = _load_lanes()")
    A("WHIDS = {w for (w, _) in ZONES}")
    A("_ORDER = {cid: i for i, cid in enumerate(DISPLAY)}")
    A("")
    A("")
    A(f'SQL_QUERY = """{sql}"""')
    A("")
    A('''def build_df_from_query(carrier=None):
    """Run the SQL query and return the dataframe with the cost columns appended.

    carrier=None prices the cheapest carrier per unique lane;
    carrier=<dict> prices that one carrier on every (warehouse_id, pincode, carrier).
    """
    from google.cloud import bigquery
    client = bigquery.Client()
    print("Executing query...")
    df = client.query(SQL_QUERY).to_dataframe()
    print("Computing cost columns...")
    df = add_cost_columns(df, carrier)
    total, priced = len(df), df["calculated_freight"].notna().sum()
    sh = df["shipping_cost"].notna().sum()
    print(f"[COST] rows {total:,} | freight priced {priced:,} "
          f"({priced / total * 100:.1f}%) | shipping_cost present {sh:,} "
          f"({sh / total * 100:.1f}%)")
    if carrier is not None:
        print(f"scope: single carrier = {carrier['name']}")
    preview = [c for c in ["order_id", "awb", "carrier_name", "warehouse_id",
                           "warehouse_name", "pincode", "shipment_status",
                           "chargeable_weight_kg", "calculated_freight",
                           "carrier_zone", "shipping_cost", "shipping_carrier",
                           "shipping_cost_note"]
               if c in df.columns]
    if carrier is not None:
        preview += [c for c in [f"{carrier['id']}_cost", f"{carrier['id']}_zone"]
                    if c in df.columns]
    print("\\nPreview (top rows of the dataframe with the cost columns):")
    print(df[preview].head(10).to_string(index=False))
    print()
    return df''')
    A("")
    A('''def main():
    import argparse
    parser = argparse.ArgumentParser(description="B2C cost pipeline (standalone).")
    parser.add_argument("out", nargs="?", default="bc_sales_export_with_cost.csv")
    parser.add_argument("--carrier", default=None,
                        help="price only this one carrier (id or name, e.g. "
                             "delhivery / 'Delhivery'); default prices the "
                             "cheapest carrier per unique lane")
    args = parser.parse_args()
    carrier = resolve_carrier(args.carrier)
    if args.carrier and carrier is None:
        print("Unknown carrier %r. Known: %s"
              % (args.carrier, ", ".join("%s (%s)" % (c["id"], c["name"])
                                         for c in DATA["active_carriers"])))
        raise SystemExit(2)
    df = build_df_from_query(carrier)
    df.to_csv(args.out, index=False)
    print(f"Saved {len(df):,} rows to {args.out}")''')
    A("")
    A('if __name__ == "__main__":')
    A("    main()")

    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(header)
        fh.write("\n".join(body))
        fh.write("\n")

    print(f"Wrote {OUT}")


def inline_cps_source():
    with open(rf"{ROOT}\cps_compute.py", encoding="utf-8") as fh:
        return fh.read()


if __name__ == "__main__":
    build()