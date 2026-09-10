import sqlite3, json, urllib.request

con = sqlite3.connect(r'data/lanes.db')

# find a lane where Shadowfax master zone is 'Regional' (category B2)
rw = con.execute("SELECT whid, pin, shadowfax, delhivery, bluedart_plus, dtdc, ekart, velocity, amazon FROM lanes WHERE shadowfax='Regional' LIMIT 5").fetchall()
print('lanes with Shadowfax=Regional:', len(rw))
for r in rw[:3]:
    print('  lane:', r[:2], 'sfx=', r[2], 'del=', r[3])
    whid, pin = r[0], r[1]
    req = urllib.request.Request('http://127.0.0.1:8000/api/quote',
                                 data=json.dumps({'whid': whid, 'pin': pin, 'weight_kg': 1.0,
                                                  'movement': 'Fwd', 'zones': {}, 'volume': {}}).encode(),
                                 headers={'Content-Type': 'application/json'})
    q = json.load(urllib.request.urlopen(req))
    sfx = next(c for c in q['carriers'] if c['id'] == 'shadowfax')
    print(f"    quote -> shadowfax served={sfx['served']} cost={sfx['cost']} | {sfx['rate_basis']}")

# B_SPL lane check
rw = con.execute("SELECT whid, pin FROM lanes WHERE delhivery='B_SPL' LIMIT 1").fetchone()
print('B_SPL lane sample:', rw)
if rw:
    req = urllib.request.Request('http://127.0.0.1:8000/api/quote',
                                 data=json.dumps({'whid': rw[0], 'pin': rw[1], 'weight_kg': 1.0,
                                                  'movement': 'Fwd', 'zones': {}, 'volume': {'delhivery': '1-3 Lakh'}}).encode(),
                                 headers={'Content-Type': 'application/json'})
    q = json.load(urllib.request.urlopen(req))
    d = next(c for c in q['carriers'] if c['id'] == 'delhivery')
    print(f"    quote -> delhivery served={d['served']} cost={d['cost']} | {d['rate_basis']}")
con.close()