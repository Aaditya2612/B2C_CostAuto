import sqlite3, json, urllib.request

con = sqlite3.connect(r'data/lanes.db')
cols = ['whid', 'pin', 'city', 'state', 'delhivery', 'delhivery_mm', 'shadowfax', 'bluedart_plus', 'dtdc', 'velocity', 'delhivery_ndd', 'ekart', 'amazon']
row = con.execute('SELECT ' + ', '.join(cols) + ' FROM lanes WHERE whid=29 AND pin=380003').fetchone()
print('== Zone Master row for WH 29 / 380003 ==')
if row:
    for k, v in zip(cols, row):
        print(f'  {k:14s} = {v!r}')
else:
    print('  NOT FOUND')
con.close()

print()
print('== /api/quote 1kg Fwd (default tiers) ==')
req = urllib.request.Request('http://127.0.0.1:8000/api/quote',
                             data=json.dumps({'whid': 29, 'pin': 380003, 'weight_kg': 1.0,
                                              'movement': 'Fwd', 'zones': {}, 'volume': {}}).encode(),
                             headers={'Content-Type': 'application/json'})
q = json.load(urllib.request.urlopen(req))
for c in q['carriers']:
    print(f'  {c["id"]:11s} served={c["served"]!s:5s} cost={c["cost"]} master={c["master_zone"]!r} final={c["final_zone"]!r} | {c["rate_basis"][:70]}')