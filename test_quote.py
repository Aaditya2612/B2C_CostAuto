import json, urllib.request

def quote(whid, pin, weight, movement='Fwd', volume=None, zones=None, eservice='standard'):
    body = {'whid': whid, 'pin': pin, 'weight_kg': weight, 'movement': movement,
            'zones': zones or {}, 'volume': volume or {}, 'elastic_service': eservice}
    req = urllib.request.Request('http://127.0.0.1:8000/api/quote',
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))

def show(r, title):
    print('='*100)
    print(title)
    for c in r['carriers']:
        z = str(c['master_zone']); sz = str(c['system_zone']); fz = str(c['final_zone'])
        print(f"{c['name']:22s} zone={z:24s} sys={sz:24s} fin={fz:20s} served={c['served']!s:5s} cost={c['cost']}  {c['rate_basis']}")
    if r['cheapest']:
        print('>>> cheapest:', r['cheapest']['name'], r['cheapest']['cost'], 'zone', r['cheapest']['master_zone'])

r = quote(2, 421302, 1.0)
show(r, 'MUM(2) -> 421302 Bhiwandi, 1.0kg, Fwd')

r = quote(2, 421302, 3.5)
show(r, 'MUM(2) -> 421302 Bhiwandi, 3.5kg, Fwd')

r = quote(12, 700007, 2.0)
show(r, 'CCU(12) -> 700007 Kolkata, 2.0kg, Fwd')

r = quote(2, 782441, 1.0)
show(r, 'MUM(2) -> 782441 Assamese NE pin, 1.0kg, Fwd')

r = quote(29, 683544, 1.0)
show(r, 'COK(29) -> 683544 Kochi, 1.0kg, Fwd')