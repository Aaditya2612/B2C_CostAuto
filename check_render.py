import json, urllib.request, urllib.error

BASE = "https://b2c-cost-calculator.onrender.com"


def get(path, timeout=90, data=None):
    req = urllib.request.Request(BASE + path,
                                 data=json.dumps(data).encode() if data else None,
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


# warm-up (wakes the free instance) then real checks
try:
    s, ct, body = get("/", timeout=120)
    print(f"GET / -> {s} {ct} {len(body)} bytes (index.html: {'B2C Logistics Cost Calculator' in body.decode('utf-8', 'ignore')})")
except Exception as e:
    print("WARMUP FAIL:", e)

# static assets
for p in ("/static/style.css", "/static/app.js", "/static/purplle_icon.svg"):
    try:
        s, ct, body = get(p, timeout=60)
        print(f"GET {p} -> {s} {ct} {len(body)} bytes")
    except Exception as e:
        print(f"GET {p} FAIL:", e)

# bootstrap
try:
    s, ct, body = get("/api/bootstrap", timeout=60)
    b = json.loads(body)
    ids = [c["id"] for c in b["carriers"]]
    print(f"GET /api/bootstrap -> {s}; carriers: {ids}; warehouses: {len(b['warehouses'])}")
except Exception as e:
    print("bootstrap FAIL:", e)

# zones + quotes
for whid, pin in ((2, 411043), (29, 380003)):
    try:
        s, ct, body = get(f"/api/zones?whid={whid}&pin={pin}", timeout=60)
        z = json.loads(body)
        found = z.get("found")
        sfx = next((c for c in z.get("carriers", []) if c["id"] == "shadowfax"), {})
        print(f"zones WH{whid}/{pin} -> found={found} sfx_zone={sfx.get('master_zone')}")

        s, ct, body = get("/api/quote", timeout=60,
                          data={"whid": whid, "pin": pin, "weight_kg": 1.0,
                                "movement": "Fwd", "zones": {}, "volume": {}})
        q = json.loads(body)
        order = [c["id"] for c in q["carriers"]]
        cheapest = (q.get("cheapest") or {}).get("id")
        served = [c["id"] for c in q["carriers"] if c["served"]]
        print(f"quote WH{whid}/{pin} -> order={order} cheapest={cheapest} served={served}")
    except Exception as e:
        print(f"WH{whid}/{pin} FAIL:", e)