import sys
import pandas as pd

sys.path.insert(0, r"C:\Users\Administrator\Documents\0909costAuto\pipeline")
import bc_sales_pipeline as p

if "--local" in sys.argv:
    COST_CSV = r"C:\Users\Administrator\Documents\0909costAuto\bc_sales_export_with_cost.csv"
    df = pd.read_csv(COST_CSV, low_memory=False)   # optional fast path: read saved CSV
    print("loaded saved CSV:", COST_CSV)
else:
    df = p.build_df_from_query()                    # dataframe built from the SQL query

print("shape:", df.shape)
print("columns:", df.columns.tolist())
print("has the 2 new columns:", "shipping_cost" in df.columns and "shipping_carrier" in df.columns)
print(df.head(10).to_string())

if "--save" in sys.argv:
    out = r"C:\Users\Administrator\Documents\0909costAuto\bc_sales_export_with_cost.csv"
    df.to_csv(out, index=False)
    print("saved ->", out)