import json, urllib.request, re

src = open('builder.py', encoding='utf-8').read()

# what keys do the shadowfax / velocity rates dicts use?
for m in re.finditer(r'"(rates|overrides|cps)":\s*(\{[^}]*\})', src):
    chunk = m.group(0)
    if 'LM' in chunk:
        print(chunk, '\n')
for m in re.finditer(r"['\"](?:Only LM|Kerala \(Only LM\))['\"]\s*:\s*\d", src):
    print('KEY FOUND:', m.group(0))

# does the master mapping ever produce "Only LM"?
i = src.find('shadowfax')
j = src.find('velocity')
print('\n--- shadowfax mapping context ---')
print(src[i-200:i+700])