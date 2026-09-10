import json, urllib.request

def post(body):
    req = urllib.request.Request('http://127.0.0.1:8000/api/quote',
                                 data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.urlopen(req))

def row(r, cid):
    for c in r['carriers']:
        if c['id'] == cid:
            return c
    return None

# Delhivery DTO on zone A, 2kg
r = post({'whid':2,'pin':421302,'weight_kg':2.0,'movement':'DTO','zones':{},'volume':{}})
print('Delhivery DTO 2kg A:', row(r,'delhivery')['cost'], '|', row(r,'delhivery')['rate_basis'])

# Delhivery RTO
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'RTO','zones':{},'volume':{}})
print('Delhivery RTO:', row(r,'delhivery')['cost'], '|', row(r,'delhivery')['rate_basis'])

# Delhivery forward A at <1 Lakh must NOT be fabricated
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'Fwd','zones':{},'volume':{}})
d = row(r,'delhivery')
print('Delhivery Fwd A <1 Lakh: served=%s cost=%s | %s' % (d['served'], d['cost'], d['rate_basis']))

# Delhivery >=3 Lakh slab
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'Fwd','zones':{},'volume':{'delhivery':'>=3 Lakh'}})
print('Delhivery >=3L A:', row(r,'delhivery')['cost'], '|', row(r,'delhivery')['rate_basis'])

# Delhivery override zone B with an explicit 1-3 Lakh slab (Y=42)
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'Fwd','zones':{'delhivery':'B'},'volume':{'delhivery':'1-3 Lakh'}})
print('Override Delhivery B (1-3L):', row(r,'delhivery')['cost'], row(r,'delhivery')['rate_basis'])

# Movement gate: DTDC on RTO must not silently quote forward rate
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'RTO','zones':{},'volume':{}})
d = row(r,'dtdc')
print('DTDC RTO (should be gated): served=%s cost=%s | %s' % (d['served'], d['cost'], d['rate_basis']))
b = row(r,'bluedart')
print('Bluedart RTO 1kg:', b['cost'], '|', b['rate_basis'])

# Ekart volume plan (>=4L/mo from Jul'26) on >5kg
r = post({'whid':2,'pin':421302,'weight_kg':6.0,'movement':'Fwd','zones':{},'volume':{'ekart':'>=4,00,000 /mo (Rs 37)'}})
print('Ekart 6kg >=4L plan:', row(r,'ekart')['cost'], '|', row(r,'ekart')['rate_basis'])

# Amazon addl weight rounded to full kg
r = post({'whid':2,'pin':421302,'weight_kg':2.5,'movement':'Fwd','zones':{},'volume':{'amazon':'<2 Lakh'}})
print('Amazon 2.5kg (full-kg rounding):', row(r,'amazon')['cost'], '|', row(r,'amazon')['rate_basis'])
r = post({'whid':2,'pin':421302,'weight_kg':3.0,'movement':'Fwd','zones':{},'volume':{'amazon':'2-3 Lakh'}})
print('Amazon 3kg 2-3L slab:', row(r,'amazon')['cost'], '|', row(r,'amazon')['rate_basis'])

# Shadowfax 'Regional' lanes now priced as category B2 (was wrongly not-served)
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'Fwd','zones':{'shadowfax':'Regional'},'volume':{'shadowfax':'<4L'}})
print('Shadowfax override Regional:', row(r,'shadowfax')['cost'], '|', row(r,'shadowfax')['rate_basis'])

# Zone override + force not-served
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'Fwd','zones':{'shadowfax':'Metro','dtdc':None},'volume':{}})
print('Override Shadowfax Metro:', row(r,'shadowfax')['cost'], row(r,'shadowfax')['rate_basis'])
print('Force DTDC off:', row(r,'dtdc')['served'])

# Lane not found
r = post({'whid':2,'pin':999999,'weight_kg':1.0,'movement':'Fwd','zones':{},'volume':{}})
print('Lane 2_999999 delhivery served:', row(r,'delhivery')['served'])

# Elastic NDD regional
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'Fwd','zones':{},'volume':{},'elastic_service':'ndd_regional'})
print('Elastic NDD:', row(r,'elastic')['cost'], '|', row(r,'elastic')['rate_basis'])

# Sorting: served (cheapest first) then not-served at bottom
r = post({'whid':2,'pin':421302,'weight_kg':1.0,'movement':'Fwd','zones':{},'volume':{}})
order = [(c['id'], c['served'], c['cost']) for c in r['carriers']]
print('Quote order:', order)
seen_not = False
ok = True
for cid, served, cost in order:
    if served:
        if seen_not: ok = False
    else:
        seen_not = True
print('Sorted correctly (served first, then not-served):', ok)