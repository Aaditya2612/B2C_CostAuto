import sqlite3

con = sqlite3.connect(r'data/lanes.db')
zones = ['Within Zone', 'Intracity', 'Intrastate', 'Regional', 'ROI', 'Special Zone', 'Metro']
q = 'SELECT whid, pin, city, state, shadowfax, velocity FROM lanes WHERE shadowfax IN (%s)' % ','.join('?' * len(zones))
rows = con.execute(q, zones).fetchall()
seen = set()
for w, p, c, s, sf, ve in rows:
    key = (sf, ve)
    if key in seen:
        continue
    seen.add(key)
    print(f'WH {w}  pin {p}  {c}, {s}  | sfx={sf!r} vel={ve!r}')
    if len(seen) >= 9:
        break
con.close()