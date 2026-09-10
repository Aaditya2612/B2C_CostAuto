import urllib.request

html = urllib.request.urlopen('http://127.0.0.1:8000/').read().decode('utf-8')
checks = {
    'logo img (purplle svg)': 'class="logo" aria-hidden="true"><img src="static/purplle_icon.svg" alt="" /></div>' in html,
    'subtitle removed': 'Freight estimate per carrier built from' not in html,
    'volset present & not hidden': 'id="volrow" class="volset"' in html,
    'volgrid': 'id="volgrid"' in html,
    'assumptions bold removed': '<b>not printed in the workbook</b>' not in html,
    'button own centered row': 'class="row ctrl"' in html,
}
for k, v in checks.items():
    print(k, '->', v)