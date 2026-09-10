import re

js = open(r'static/app.js', encoding='utf-8').read()
html = open(r'static/index.html', encoding='utf-8').read()
ids = set(re.findall(r'\$\("([a-zA-Z_]+)"\)', js))
html_ids = set(re.findall(r'\bid="([a-zA-Z_]+)"', html))
missing = [i for i in ids if i not in html_ids]
print('IDs used in JS:', sorted(ids))
print('Missing in HTML:', missing)