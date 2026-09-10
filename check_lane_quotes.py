import json, urllib.request

lanes = [
    (2, 421302, 'Intracity / Local'),
    (2, 413513, 'Intrastate / Intrastate'),
    (2, 380003, 'Within Zone / Regional'),
    (2, 191112, 'Special Zone / (vel none)'),
]
for whid, pin, tag in lanes:
    req = urllib.request.Request('http://127.0.0.1:8000/api/quote',
                                 data=json.dumps({'whid': whid, 'pin': pin, 'weight_kg': 1.0,
                                                  'movement': 'Fwd', 'zones': {}, 'volume': {}}).encode(),
                                 headers={'Content-Type': 'application/json'})
    q = json.load(urllib.request.urlopen(req))
    for c in q['carriers']:
        if c['id'] in ('shadowfax', 'velocity'):
            print(f"WH {whid} pin {pin} [{tag}] {c['id']}: served={c['served']} cost={c['cost']} basis={c['rate_basis']}")
    print()