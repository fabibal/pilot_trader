#!/usr/bin/env python3
"""Live, read-only dashboard callback smoke test; requires the running container.

Trade tabs may fetch market prices through their normal dashboard callbacks.
"""
import json,urllib.request
base='http://127.0.0.1:8051'
deps=json.load(urllib.request.urlopen(base+'/_dash-dependencies',timeout=10))
for tab in ['Consensus','BenCowen','JesseOlson','KiYoungJu','JoaoWedson','DorkChicken','DaanCrypto','DonAlt','CowenX','Glassnode','Truecrypto','GeoffKendrick','IncomeSharks','traderstewie']:
 key=next(d['output'] for d in deps if (d['output']=='consensus-panel.children' if tab=='Consensus' else 'influencer-signals.data' in d['output']))
 parts=key[2:-2].split('...') if key.startswith('..') else [key]
 outputs=[dict(zip(['id','property'],p.rsplit('.',1))) for p in parts]
 payload={'output':key,'outputs':outputs if key.startswith('..') else outputs[0], 'inputs':[{'id':'interval','property':'n_intervals','value':0},{'id':'influencer-subtabs','property':'value','value':tab}], 'state':[], 'changedPropIds':['influencer-subtabs.value']}
 req=urllib.request.Request(base+'/_dash-update-component',data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
 with urllib.request.urlopen(req,timeout=90) as response:
  data=response.read(); parsed=json.loads(data)['response']
  assert len(data)>1000,(tab,'Empty content response')
  if tab in ('IncomeSharks','traderstewie'):
   assert parsed['influencer-signals']['data'], 'No trade signals'
  print(tab,response.status,len(data),'bytes',flush=True)
