import json, urllib.request

b = json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/bootstrap'))
for c in b['carriers']:
    print(c['id'], '| movements:', c['movements'], '| vol:', c['volume_key'], c['volume_options'][:4])

z = json.load(urllib.request.urlopen('http://127.0.0.1:8000/api/zones?whid=2&pin=421302'))
print('zones found:', z['found'])
print('zones order:', [(c['id'], c['served'], c['cost']) for c in z['carriers']])

html = urllib.request.urlopen('http://127.0.0.1:8000/').read().decode('utf-8')
print('assumptions hidden default:', 'id="assumptions" class="notes" hidden' in html)
print('toggle btn:', 'id="assumptions-toggle"' in html)
print('zone dropdown select leftover:', 'zone-sel' in html)
print('has_zones leftover:', 'has_zones' in html)