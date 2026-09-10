#%% INTEGRATED B2C shipment report  --  run on the ubuntu server
# Original notebook + rate-card freight column (B2C_CostAuto integration).
#
# Added to your existing script (marked "<-- ADDED"):
#   1. from cps_compute import compute_cps
#   2. df = compute_cps(df)
#   3. a small CPS summary printed before upload
#
# NOTE: keep cps_compute.py + pricing.py + data/ in this folder next to this
# script (i.e. /home/ubuntu/purplle_data_science/marketing/dev/vishal/bc_sales/).
#
# The SQL block below is your existing query (only whitespace-normalised) --
# if your server copy differs, keep YOUR exact SQL; the integration only needs
# the two lines marked "<-- ADDED".

#%%
import json
import subprocess
from datetime import datetime, timedelta
import pandas as pd
from google.cloud import bigquery
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from cps_compute import compute_cps, freight_summary  # <-- ADDED

CREDENTIALS_PATH = "/home/ubuntu/purplle_data_science/marketing/dev/vishal/bc_sales/data.json"


#%%
def get_credentials():
    from google.oauth2 import service_account

    credentials = service_account.Credentials.from_service_account_file(
        CREDENTIALS_PATH,
        scopes=[
            "https://www.googleapis.com/auth/bigquery",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    return credentials


#%%
def run_query():
    credentials = get_credentials()
    client = bigquery.Client(credentials=credentials, project="purplle-data-science")

    # ---------------- YOUR ORIGINAL QUERY (keep verbatim) ----------------
    query = """
    WITH
      filter_base_order AS (
        SELECT ob.*, ob.created_date_time_ist
        FROM `purplle-data-science.datapipelineproduction.order` ob
        WHERE
          ob.created_date_time_ist >= DATETIME_SUB(CURRENT_DATETIME('Asia/Kolkata'), INTERVAL 25 DAY)
          AND ob.type = 'SHIPPED'
          AND ob.channel IN ('APP', 'WEBSITE', 'MOBILE_SITE')
      ),
      base AS (
        SELECT
          b.id AS order_id,
          b.type,
          b.status AS order_status,
          b.channel,
          b.domain,
          b.domain_id,
          b.qualification_type,
          b.total_amount,
          b.fba_type,
          b.created_by,
          b.created_on,
          b.created_date_time_ist,
          b.payment_type,
          b.coupon_code,
          b.delivery_tat,
          b.pincode,
          b.order_type,
          b.marketplace_name,
          b.msquared_city,
          b.warehouse_id,
          b.shipping_address_id,
          ad.id AS address_id,
          ad.city,
          ad.state,
          ad.postal_code,
          ad.pincode AS shipping_pincode
        FROM filter_base_order b
        LEFT JOIN `purplle-data-science.datapipelineproduction.address` ad
          ON ad.id = b.shipping_address_id
      ),
      secondary AS (
        SELECT
          msd.shipping_order_id,
          msd.id AS secondary_id,
          msd.latest_secondary_status
        FROM (
          SELECT
            shipping_order_id,
            MAX(id) AS max_id
          FROM `purplle-data-science.datapipelineproduction.shipping_order_status`
          GROUP BY shipping_order_id
        ) latest
        JOIN `purplle-data-science.datapipelineproduction.shipping_order_status` msd
          ON msd.id = latest.max_id
      ),
      shipment AS (
        SELECT
          sho.id AS shipment_id,
          sho.master_shipment_id,
          sho.carrier_switch,
          sho.carrier_id,
          sho.warehouse_id,
          sho.shipment_type,
          sho.created_on AS shipment_created_on,
          sho.ship_date,
          sho.delivery_date,
          sho.weight,
          sho.vol_weight,
          sho.expected_delivery_time,
          sho.dispatch_time,
          sho.ship_postal_code,
          sho.status AS shipment_status,
          sho.original_shipping_pincode,
          sho.rto_marked_on,
          s.latest_secondary_status
        FROM `purplle-data-science.datapipelineproduction.shipping_order` sho
        LEFT JOIN secondary s
          ON s.shipping_order_id = sho.id
      ),
      carrier AS (
        SELECT
          sc.id AS carrier_id,
          sc.name AS carrier_name
        FROM `purplle-data-science.datapipelineproduction.shipping_carrier` sc
        WHERE sc.id IN (96, 29, 26, 88, 101, 99, 2, 12, 75, 100, 25, 3)
      ),
      warehouse AS (
        SELECT
          w.id AS warehouse_id,
          w.name AS warehouse_name
        FROM `purplle-data-science.datapipelineproduction.warehouse_warehouse` w
      ),
      ranked AS (
        SELECT
          s.*,
          c.carrier_name,
          w.warehouse_name,
          ROW_NUMBER() OVER (
            PARTITION BY s.master_shipment_id
            ORDER BY s.dispatch_time DESC
          ) AS rn
        FROM shipment s
        LEFT JOIN carrier c ON c.carrier_id = s.carrier_id
        LEFT JOIN warehouse w ON w.warehouse_id = s.warehouse_id
      )
    SELECT
      r.*,
      TIMESTAMP_DIFF(
        CAST(r.expected_delivery_time AS TIMESTAMP),
        CAST(r.dispatch_time AS TIMESTAMP),
        HOUR
      ) AS delivery_tat_hrs,
      CASE
        WHEN r.latest_secondary_status LIKE '%RTO%' OR r.shipment_status = 'Returned'
        THEN TRUE ELSE FALSE
      END AS is_rto
    FROM ranked r
    WHERE r.rn = 1
    ORDER BY r.dispatch_time DESC
    """
    # ------------------------------------------------------------
    df = client.query(query).to_dataframe()
    return df


#%%
def upload_to_drive(file_path):
    credentials = get_credentials()
    drive_service = build("drive", "v3", credentials=credentials)
    folder_id = "1MaJzsNAcdVHcIpb5vfrfKAF0N1vCjrji"
    file_name = file_path.split("/")[-1]
    file_metadata = {"name": file_name, "parents": [folder_id]}
    media = MediaFileUpload(file_path, mimetype="text/csv")
    file = (
        drive_service.files()
        .create(body=file_metadata, media_body=media, fields="id,webViewLink")
        .execute()
    )
    return file.get("webViewLink")


#%%
def send_email(drive_link):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    email_address = "dipanshu@purplle.com"
    email_password = "cwwe rwex vbmj vozr"
    to_addresses = [
        "aditi.s@purplle.com",
        "subrat.d@purplle.com",
        "srivibhav.b@purplle.com",
        "logisticsteam@purplle.com",
    ]

    message = MIMEMultipart()
    message["From"] = email_address
    message["To"] = ", ".join(to_addresses)
    message["Subject"] = "B2C Shipment Report"
    body = f"Please find the B2C shipment report here: {drive_link}"
    message.attach(MIMEText(body, "plain"))

    server = smtplib.SMTP("smtp.gmail.com", 587)
    server.starttls()
    server.login(email_address, email_password)
    server.sendmail(email_address, to_addresses, message.as_string())
    server.quit()


#%%
df = run_query()
df = compute_cps(df)                                  # <-- ADDED: rate-card freight
print(freight_summary(df))                            # <-- ADDED

today = datetime.now().strftime("%Y%m%d_%H%M%S")
file_name = f"bc_sales_export_{today}.csv"
file_path = f"/home/ubuntu/purplle_data_science/marketing/dev/vishal/bc_sales/{file_name}"
df.to_csv(file_path, index=False)

drive_link = upload_to_drive(file_path)
send_email(drive_link)
print(f"Done! File: {file_path}\nDrive link: {drive_link}")