

from __future__ import annotations

import os
import sys
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Make the project root (which holds pricing.py + data/) importable.
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from googleapiclient.discovery import build                       # noqa: E402
    from googleapiclient.http import MediaFileUpload                  # noqa: E402
    from google.cloud import bigquery                                 # noqa: E402
except ImportError:  # heavy Google libs optional at import time (offline smoke tests)
    build = MediaFileUpload = bigquery = None
from google.oauth2 import service_account                             # noqa: E402
import pandas as pd                                               # noqa: E402

import pricing  # noqa: E402,F401  (engine - single copy, loaded once)
import cps_compute                                                # noqa: E402
import add_shipping_cost as asc                                   # noqa: E402

# This BigQuery export stores weights in KILOGRAMS (verified).
cps_compute.WEIGHT_IS_GRAMS = False

# ==============================================================================
# CONFIGURATION & AUTHENTICATION
# ==============================================================================
SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/home/ubuntu/purplle_data_science/marketing/dev/vishal/bc_sales/data.json",
)
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = SERVICE_ACCOUNT_FILE

DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID", "1MaJzsNAcdVHcIpb5vfrfKAF0N1vCjrji")
EXPORT_CSV = os.environ.get("EXPORT_CSV", "bc_sales_export.csv")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "25"))
HEAD_ROWS = int(os.environ.get("HEAD_ROWS", "10"))

SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "dipanshu@purplle.com")
SENDER_PASSWORD = os.environ.get("SENDER_PASSWORD", "cwwe rwex vbmj vozr")

RECIPIENTS_TO = [
    s.strip()
    for s in os.environ.get(
        "RECIPIENTS_TO",
        "aditi.s@purplle.com,subrat.d@purplle.com,srivibhav.b@purplle.com,logisticsteam@purplle.com",
    ).split(",")
    if s.strip()
]
RECIPIENTS_CC = [s.strip() for s in os.environ.get("RECIPIENTS_CC", "").split(",") if s.strip()]


# ==============================================================================
# RATE-CARD COST COLUMNS (same engine as the web app)
# ==============================================================================
def add_shipping_cost_columns(df):
    """Cheapest-carrier shipping cost per unique (warehouse_id, pincode) lane.

    Reproduces add_shipping_cost.main() in-memory so the SAME dataframe keeps
    its cost columns when exported. Lane quote = representative 0.5 kg at each
    carrier's default (lowest) volume tier; RTO rows use RTO rates where a
    carrier quotes RTO.
    """
    lanes = (
        df.loc[df["warehouse_id"].isin(asc.WHIDS), ["warehouse_id", "pincode"]]
        .drop_duplicates()
    )
    lane_fwd, lane_rto = {}, {}
    for whid, pin in lanes.itertuples(index=False):
        fwd, rto = asc._lane_quotes(int(whid), int(pin))
        lane_fwd[(int(whid), int(pin))] = asc._pick(fwd)
        lane_rto[(int(whid), int(pin))] = asc._pick(rto)

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
        if whid not in asc.WHIDS:
            costs.append(None); carriers.append(None)
            notes.append("warehouse not on rate card")
            continue
        key = (whid, pin)
        if is_rto:
            cost, carrier = lane_rto.get(key, (None, None))
            note = ("ok" if cost is not None
                    else ("RTO not quoted on any carrier"
                          if lane_fwd.get(key, (None, None))[0] is not None
                          else "no servicable carrier on this lane"))
        else:
            cost, carrier = lane_fwd.get(key, (None, None))
            note = "ok" if cost is not None else "no servicable carrier on this lane"
        costs.append(cost); carriers.append(carrier); notes.append(note)

    df["shipping_cost"] = costs
    df["shipping_carrier"] = carriers
    df["shipping_cost_note"] = notes
    return df


def add_cost_columns(df):
    """Append per-shipment freight + lane shipping-cost columns."""
    df = df.copy()
    if "rto_marked_on" not in df.columns and "rto_marked_date" in df.columns:
        df["rto_marked_on"] = df["rto_marked_date"]   # movement auto-detect compat
    df = cps_compute.compute_cps(df)
    df = add_shipping_cost_columns(df)
    total = len(df)
    priced = df["calculated_freight"].notna().sum()
    sh = df["shipping_cost"].notna().sum()
    print(f"[COST] rows {total:,} | per-shipment freight priced {priced:,} "
          f"({priced / total * 100:.1f}%) | lane shipping_cost present {sh:,} "
          f"({sh / total * 100:.1f}%)")

    # 2 new calculated columns always present after this function.
    _preview_cols = [
        "order_id", "awb", "carrier_name", "warehouse_id", "warehouse_name",
        "pincode", "shipment_status", "movement_used", "chargeable_weight_kg",
        "calculated_freight", "carrier_zone_used",
        "shipping_cost", "shipping_carrier", "shipping_cost_note",
    ]
    show = [c for c in _preview_cols if c in df.columns]
    print("\nPreview (head of the dataframe with the new columns):")
    print(df[show].head(HEAD_ROWS).to_string(index=False))
    print()
    return df


# ==============================================================================
# 1. RUN QUERY & SAVE TO CSV
# ==============================================================================
def _query_df():
    sql_query = """\
WITH filtered_base AS (
  SELECT 
    sho.id AS child_shipment_id,
    sho.order_id,
    sho.awb AS sho_awb,
    sho.status AS shipment_status,
    sho.collection_amount AS invoice_value,
    sho.weight,
    sho.vol_weight,
    sho.warehouse_id,
    sho.carrier_id AS sho_carrier_id,
    sho.dispatch_time,
    sho.delivery_timestamp,
    so.id AS shop_order_id,
    so.contact_id,
    so.fulfillment_type,
    so.ship_postal_code,
    so.firstorder_timestamp,
    so.time_stamp AS order_timestamp,
    so.order_type,
    so.method AS payment_method,
    so.tenant,
    so.sub_tenant,
    msm.master_shipment_id
  FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shipping_order` sho
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shop_order` so 
    ON sho.order_id = so.id
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_master_shipment_mapping` msm 
    ON msm.child_shipment_id = sho.id
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_master_shipment_detail` msd 
    ON msd.id = msm.master_shipment_id
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shipping_carrier` sc 
    ON sc.id = COALESCE(sho.carrier_id, msd.carrier_id)
  WHERE 
    so.order_type = 'b2c'
    AND so.tenant = 'PURPLLE_COM'
    AND so.sub_tenant IN ('MAIN_SITE', 'QUICK')
    AND sc.id IN (96, 29, 26, 88, 101, 99, 2, 12, 75, 100, 25, 3)
    AND DATE(TIMESTAMP_SECONDS(CAST(sho.dispatch_time AS INT64)), 'Asia/Kolkata') 
        BETWEEN DATE_SUB(CURRENT_DATE('Asia/Kolkata'), INTERVAL 25 DAY) 
            AND CURRENT_DATE('Asia/Kolkata')
),
order_items_agg AS (
  SELECT 
    shoi.shiporder_id,
    SUM(shoi.qty) AS shipment_quantity,
    MAX(SAFE_CAST(soid.edd_max_tat AS INT64)) AS edd_max_tat,
    MAX(SAFE_CAST(soid.update_max_tat AS INT64)) AS update_max_tat
  FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shipping_orderitem` shoi
  JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shop_orderitem_details` soid 
    ON soid.orderitem_id = shoi.orderitem_id
  JOIN filtered_base fb 
    ON fb.child_shipment_id = shoi.shiporder_id
  GROUP BY shoi.shiporder_id
),
hub_mapping AS (
  SELECT 
    order_id,
    MAX(hub_id) AS hub_id
  FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_hub_orderitem_map`
  WHERE order_id IN (SELECT order_id FROM filtered_base)
  GROUP BY order_id
),
consolidated_status_logs AS (
  SELECT 
    module,
    clean_module_id,
    MAX(CASE WHEN LOWER(status) = 'handover' THEN clean_timestamp END) AS handover_ts,
    MAX(CASE WHEN LOWER(status) = 'dispatched' THEN clean_timestamp END) AS dispatched_ts,
    MAX(CASE WHEN LOWER(status) = 'delivered' THEN clean_timestamp END) AS delivered_ts,
    ARRAY_AGG(STRUCT(status, clean_timestamp) ORDER BY clean_timestamp DESC LIMIT 1)[OFFSET(0)] AS latest_log
  FROM (
    SELECT 
      module,
      status,
      SAFE_CAST(REGEXP_REPLACE(CAST(module_id AS STRING), r'[^0-9]', '') AS INT64) AS clean_module_id,
      SAFE_CAST(REGEXP_REPLACE(CAST(time_stamp AS STRING), r'[^0-9]', '') AS INT64) AS clean_timestamp
    FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_log_statuschange`
    WHERE module IN ('shipment', 'order')
  )
  GROUP BY module, clean_module_id
),
master_details AS (
  SELECT 
    id, 
    carrier_id, 
    awb 
  FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_master_shipment_detail`
),
picklist_info AS (
  SELECT 
    pps.shipment_id,
    MAX(pp.carrier_id) AS picklist_carrier_id
  FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_picking_picklistshipment` pps
  JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_picking_picklist` pp 
    ON pp.id = pps.picklist_id
  WHERE pps.shipment_id IN (SELECT child_shipment_id FROM filtered_base)
  GROUP BY pps.shipment_id
),
latest_courier_status AS (
  SELECT awb, primary_status, secondary_status, status_date_time
  FROM (
    SELECT 
      awb, primary_status, secondary_status, status_date_time,
      ROW_NUMBER() OVER (PARTITION BY awb ORDER BY status_date_time DESC) AS rn
    FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_courier_shipment_status`
  )
  WHERE rn = 1
),
rto_marked_info AS (
  SELECT awb, MIN(time) AS rto_marked_time
  FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_courier_shipment_status`
  WHERE secondary_status = 'RTO-Marked'
  GROUP BY awb
),
calculated_metrics AS (
  SELECT
    fb.master_shipment_id,
    fb.child_shipment_id,
    COALESCE(fb.master_shipment_id, fb.child_shipment_id, fb.shop_order_id) AS effective_shipment_id,
    fb.order_id,
    COALESCE(fb.sho_awb, msd.awb) AS awb,
    fb.shipment_status,
    fb.invoice_value,
    fb.weight,
    fb.vol_weight,
    SAFE_CAST(cpt.cpt_date AS DATE) AS cpt_date,
    DATETIME(cpt.handover_date, 'Asia/Kolkata') AS cpt_handover_date,
    COALESCE(lsc_ship.latest_log.status, lsc_ord.latest_log.status) AS latest_log_status,
    DATETIME(TIMESTAMP_SECONDS(COALESCE(lsc_ship.latest_log.clean_timestamp, lsc_ord.latest_log.clean_timestamp)), 'Asia/Kolkata') AS latest_log_time,
    lcs.primary_status AS latest_primary_status,
    lcs.secondary_status AS latest_secondary_status,
    fb.fulfillment_type,
    fb.warehouse_id,
    wh.name AS warehouse_name,
    wh.postal_code,
    hub.hub_id,
    dim_region.city_name AS ship_city,
    fb.ship_postal_code AS pincode,
    dim_region.state_name AS state,
    COALESCE(fb.sho_carrier_id, msd.carrier_id) AS carrier_id,
    sc.name AS carrier_name,
    pp.picklist_carrier_id,
    IF(fb.firstorder_timestamp = fb.order_timestamp, 'FT', 'RB') AS FT_RB_FLAG,
    IF(
      COALESCE(CAST(COALESCE(fb.sho_carrier_id, msd.carrier_id) AS STRING), '') <> COALESCE(CAST(pp.picklist_carrier_id AS STRING), ''),
      1, 0
    ) AS carrier_switch,
    fb.order_type,
    IF(cc.is_retailer = 1, 'Retailer', 'Non-Retailer') AS is_retailer,
    fb.payment_method,
    DATETIME(TIMESTAMP_SECONDS(CAST(fb.order_timestamp AS INT64)), 'Asia/Kolkata') AS order_time,
    DATETIME(TIMESTAMP_SECONDS(lsc_ship.handover_ts), 'Asia/Kolkata') AS lsc_handover_time,
    DATETIME(TIMESTAMP_SECONDS(CAST(wtd.pickedup_time AS INT64)), 'Asia/Kolkata') AS pickup_time,
    IF(fb.dispatch_time IS NULL OR fb.dispatch_time = 0, NULL, DATETIME(TIMESTAMP_SECONDS(CAST(fb.dispatch_time AS INT64)), 'Asia/Kolkata')) AS dispatch_time,
    DATETIME(TIMESTAMP_SECONDS(lsc_ship.dispatched_ts), 'Asia/Kolkata') AS lsc_dispatched_time,
    DATETIME(TIMESTAMP_SECONDS(CAST(wtd.intransit_time AS INT64)), 'Asia/Kolkata') AS intransit_time,
    DATETIME(TIMESTAMP_SECONDS(CAST(wtd.OFD1_timestamp AS INT64)), 'Asia/Kolkata') AS ofd1_time,
    DATETIME(TIMESTAMP_SECONDS(CAST(wtd.OFD2_timestamp AS INT64)), 'Asia/Kolkata') AS ofd2_time,
    DATETIME(TIMESTAMP_SECONDS(CAST(wtd.OFD3_timestamp AS INT64)), 'Asia/Kolkata') AS ofd3_time,
    IF(fb.delivery_timestamp IS NULL OR fb.delivery_timestamp = 0, NULL, DATETIME(TIMESTAMP_SECONDS(CAST(fb.delivery_timestamp AS INT64)), 'Asia/Kolkata')) AS delivery_time,
    DATETIME(TIMESTAMP_SECONDS(lsc_ship.delivered_ts), 'Asia/Kolkata') AS lsc_delivered_time,
    DATETIME(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') AS edd_max_time,
    DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') AS edd_date,
    DATE(TIMESTAMP_SECONDS(oi.update_max_tat), 'Asia/Kolkata') AS updated_edd_max,
    DATE(TIMESTAMP_SECONDS(CAST(rmi.rto_marked_time AS INT64)), 'Asia/Kolkata') AS rto_marked_date,
    DATE(TIMESTAMP_SECONDS(CAST(ril.created_on AS INT64)), 'Asia/Kolkata') AS rto_received_date,
    DATETIME(TIMESTAMP_SECONDS(CAST(ril.created_on AS INT64)), 'Asia/Kolkata') AS rto_received_time,
    DATE(TIMESTAMP_SECONDS(CAST(riil.inwarded_at AS INT64)), 'Asia/Kolkata') AS rto_inward_date,
    DATETIME(TIMESTAMP_SECONDS(CAST(riil.inwarded_at AS INT64)), 'Asia/Kolkata') AS rto_inward_time,
    wtd.no_of_attempts AS total_attempts,
    fb.tenant,
    fb.sub_tenant,
    oi.shipment_quantity,
    CASE
      WHEN DATETIME(cpt.handover_date, 'Asia/Kolkata') > DATETIME(cpt.handover_cutoff_datetime, 'Asia/Kolkata') THEN 1
      WHEN DATETIME(cpt.handover_date, 'Asia/Kolkata') <= DATETIME(cpt.handover_cutoff_datetime, 'Asia/Kolkata') THEN 0
      WHEN cpt.handover_date IS NULL 
           AND DATETIME(cpt.handover_cutoff_datetime, 'Asia/Kolkata') < CURRENT_DATETIME('Asia/Kolkata') THEN 1
      ELSE 0
    END AS cpt_breach,
    CASE
      WHEN (fb.delivery_timestamp IS NULL OR fb.delivery_timestamp = 0) 
           AND CURRENT_DATE('Asia/Kolkata') > DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') THEN 1
      WHEN DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') > CURRENT_DATE('Asia/Kolkata') THEN 0
      WHEN fb.shipment_status IN ('Delivered', 'Lost', 'Returned', 'In Transit')
           AND COALESCE(
                 DATE(TIMESTAMP_SECONDS(CAST(wtd.OFD1_timestamp AS INT64)), 'Asia/Kolkata'), 
                 DATE(TIMESTAMP_SECONDS(CAST(fb.delivery_timestamp AS INT64)), 'Asia/Kolkata')
               ) <= DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata')
           AND COALESCE(NULLIF(wtd.OFD1_timestamp, 0), NULLIF(fb.delivery_timestamp, 0)) IS NOT NULL THEN 0
      WHEN fb.shipment_status IN ('Returned', 'In Transit')
           AND (wtd.OFD1_timestamp IS NULL OR wtd.OFD1_timestamp = 0)
           AND DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') > CURRENT_DATE('Asia/Kolkata') THEN 0
      ELSE 1
    END AS edd_breach,
    CASE
      WHEN (fb.delivery_timestamp IS NULL OR fb.delivery_timestamp = 0) 
           AND CURRENT_DATE('Asia/Kolkata') > DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') THEN 1
      WHEN DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') > CURRENT_DATE('Asia/Kolkata') THEN 0
      WHEN fb.delivery_timestamp IS NOT NULL 
           AND fb.delivery_timestamp > 0
           AND DATE(TIMESTAMP_SECONDS(CAST(fb.delivery_timestamp AS INT64)), 'Asia/Kolkata') <= DATE(TIMESTAMP_SECONDS(oi.edd_max_tat), 'Asia/Kolkata') THEN 0
      ELSE 1
    END AS otif_breach
  FROM filtered_base fb
  LEFT JOIN order_items_agg oi 
    ON oi.shiporder_id = fb.child_shipment_id
  LEFT JOIN master_details msd 
    ON msd.id = fb.master_shipment_id
  LEFT JOIN hub_mapping hub 
    ON hub.order_id = fb.order_id
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shipping_carrier` sc 
    ON sc.id = COALESCE(fb.sho_carrier_id, msd.carrier_id)
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_warehouse_warehouse` wh
    ON fb.warehouse_id = wh.id
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_contact_contact` cc 
    ON cc.id = fb.contact_id
  LEFT JOIN picklist_info pp 
    ON pp.shipment_id = fb.child_shipment_id
  LEFT JOIN consolidated_status_logs lsc_ship 
    ON lsc_ship.module = 'shipment' AND lsc_ship.clean_module_id = fb.child_shipment_id
  LEFT JOIN consolidated_status_logs lsc_ord 
    ON lsc_ord.module = 'order' AND lsc_ord.clean_module_id = fb.shop_order_id
  LEFT JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_webhook_tracking_details` wtd 
    ON wtd.shipment_id = COALESCE(fb.master_shipment_id, fb.child_shipment_id, fb.shop_order_id)
  LEFT JOIN latest_courier_status lcs 
    ON lcs.awb = COALESCE(fb.sho_awb, msd.awb)
  LEFT JOIN rto_marked_info rmi 
    ON rmi.awb = COALESCE(fb.sho_awb, msd.awb)
  LEFT JOIN (
    SELECT shipment_id, MAX(CAST(created_on AS INT64)) AS created_on
    FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_rto_inward_log`
    GROUP BY shipment_id
  ) ril ON ril.shipment_id = COALESCE(fb.master_shipment_id, fb.child_shipment_id, fb.shop_order_id)
  LEFT JOIN (
    SELECT shipment_id, MAX(CAST(inwarded_at AS INT64)) AS inwarded_at
    FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_rto_inward_item_logs`
    GROUP BY shipment_id
  ) riil ON riil.shipment_id = COALESCE(fb.master_shipment_id, fb.child_shipment_id, fb.shop_order_id)
  LEFT JOIN (
    SELECT 
      sa.postal_code,
      MAX(city_name) AS city_name, 
      MAX(ss.name) AS state_name
    FROM `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shipping_areas` sa
    JOIN `datapipelineproduction.datos_deposito_banco.purplle_purplle2_shipping_state` ss ON sa.state_id = ss.id
    GROUP BY sa.postal_code
  ) dim_region ON SAFE_CAST(REGEXP_REPLACE(CAST(fb.ship_postal_code AS STRING), r'[^0-9]', '') AS INT64) = dim_region.postal_code
  LEFT JOIN `datapipelineproduction.datos_studios.fc_cpt_new_automation_fk` cpt 
    ON SAFE_CAST(REGEXP_REPLACE(CAST(cpt.shipment_id AS STRING), r'[^0-9]', '') AS INT64) = fb.child_shipment_id
)
SELECT
  master_shipment_id,
  child_shipment_id,
  effective_shipment_id,
  order_id,
  awb,
  shipment_status,
  invoice_value,
  weight,
  vol_weight,
  cpt_date,
  cpt_handover_date,
  latest_log_status,
  latest_primary_status,
  latest_secondary_status,
  fulfillment_type,
  warehouse_id,
  warehouse_name,
  postal_code,
  hub_id,
  ship_city,
  pincode,
  state,
  carrier_id,
  carrier_name,
  picklist_carrier_id,
  FT_RB_FLAG,
  carrier_switch,
  order_type,
  is_retailer,
  payment_method,
  order_time,
  lsc_handover_time,
  pickup_time,
  dispatch_time,
  intransit_time,
  ofd1_time,
  ofd2_time,
  ofd3_time,
  delivery_time,
  edd_max_time,
  edd_date,
  updated_edd_max,
  rto_marked_date,
  rto_received_date,
  rto_inward_date,
  total_attempts,
  tenant,
  sub_tenant,
  shipment_quantity,
  cpt_breach,
  edd_breach,
  otif_breach,
  CASE 
    WHEN cpt_breach = 1 AND edd_breach = 0 THEN 'cpt_breach'
    WHEN cpt_breach = 0 AND edd_breach = 1 THEN 'ops_breach'
    WHEN cpt_breach = 1 AND edd_breach = 1 THEN 'cpt_breach'
    ELSE 'no_breach'
  END AS breach_type
FROM calculated_metrics
ORDER BY edd_date DESC, effective_shipment_id;
"""
    print("Executing query...")
    if bigquery is None:
        raise ImportError("google-cloud-bigquery not installed - run: pip install -r pipeline/requirements.txt")
    client = bigquery.Client()
    df = client.query(sql_query).to_dataframe()

    print("Computing cost columns...")
    df = add_cost_columns(df)
    return df


def build_df_from_query():
    """Run the SQL query and RETURN the dataframe with the cost columns appended.

    The dataframe is built entirely from the SQL query in this script
    (no reading from the exported CSV/spreadsheet).
    """
    df = _query_df()
    print(f"Dataframe built from the SQL query: {len(df):,} rows.")
    return df


def run_query():
    """Execute the SQL query, build the dataframe, and export it to CSV.

    Returns the dataframe AND writes EXPORT_CSV for the Drive/email step.
    """
    df = _query_df()
    df.to_csv(EXPORT_CSV, index=False)
    print(f"Exported {len(df)} rows to {EXPORT_CSV}")
    return EXPORT_CSV


# ==============================================================================
# 2. UPLOAD TO GOOGLE DRIVE
# ==============================================================================
def upload_to_drive(file_path):
    if build is None or MediaFileUpload is None:
        raise ImportError("google-api-python-client not installed - run: pip install -r pipeline/requirements.txt")
    SCOPES = ["https://www.googleapis.com/auth/drive"]
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    drive_service = build("drive", "v3", credentials=creds)

    file_metadata = {
        "name": os.path.basename(file_path),
        "parents": [DRIVE_FOLDER_ID],
    }
    media = MediaFileUpload(file_path, mimetype="text/csv", resumable=False)

    uploaded_file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, fields="id, webViewLink")
        .execute()
    )
    file_id = uploaded_file.get("id")
    drive_link = uploaded_file.get("webViewLink")

    drive_service.permissions().create(
        fileId=file_id, body={"type": "anyone", "role": "reader", "allowFileDiscovery": False}
    ).execute()

    print(f"Uploaded to Drive: {drive_link}")
    return drive_link


# ==============================================================================
# 3. EMAIL LINK (MULTIPLE TO & CC)
# ==============================================================================
def send_email(drive_link):
    today_dt = datetime.now()
    past_25_dt = today_dt - timedelta(days=LOOKBACK_DAYS)

    today_str = today_dt.strftime("%Y-%m-%d")
    past_25_str = past_25_dt.strftime("%Y-%m-%d")

    subject = f"B2C shipment Raw data from {past_25_str} - {today_str}"

    body = f"""Hi Team,

This is the RAW data for B2C shipment from {today_str}

Access the CSV file here:
{drive_link}

Best regards,
Dipanshu
"""

    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(RECIPIENTS_TO)
    if RECIPIENTS_CC:
        msg["Cc"] = ", ".join(RECIPIENTS_CC)
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    all_recipients = RECIPIENTS_TO + RECIPIENTS_CC

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, all_recipients, msg.as_string())

    print(f"Email successfully sent to {len(all_recipients)} recipient(s)!")


# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def build_local_df(csv_path=None, out_csv=None):
    """Read the existing dataframe, append the 2 new calculated columns
    (shipping_cost, shipping_carrier) and RETURN the augmented dataframe.

    Saves a copy to <out_csv> so the result is usable afterwards.
    """
    csv_path = csv_path or os.environ.get("LOCAL_CSV", "bc_sales_export.csv")
    out_csv = out_csv or os.environ.get("LOCAL_OUT", "bc_sales_export_with_cost.csv")
    print(f"Loading existing dataframe: {csv_path} ...")
    df = pd.read_csv(csv_path, low_memory=False)
    print(f"Loaded {len(df):,} rows.")
    df = add_cost_columns(df)          # returns df with the 2 new columns appended
    df.to_csv(out_csv, index=False)
    print(f"Saved augmented dataframe -> {out_csv} ({len(df):,} rows)")
    return df


if __name__ == "__main__":
    if "--local" in sys.argv:                    # local mode: reuse existing CSV, no GCP
        build_local_df()
    else:                                        # full mode: BigQuery -> Drive -> email
        output_csv = run_query()
        drive_url = upload_to_drive(output_csv)
        send_email(drive_url)

        if os.path.exists(output_csv):
            os.remove(output_csv)
            print("Local temp CSV removed.")