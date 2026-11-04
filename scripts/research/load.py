# Сборка минутных свечей из кеша в один npz на бумагу (время — секунды UTC)
import json,glob,os,sys
import numpy as np
from datetime import datetime
out='npz';os.makedirs(out,exist_ok=True)
for d in sorted(glob.glob('cache/backfill_cache/*')):
    figi=os.path.basename(d);rows=[]
    for f in sorted(glob.glob(d+'/*.json')):
        for c in json.load(open(f)):
            if c.get('is_complete',True):
                rows.append((datetime.fromisoformat(c['time']).timestamp(),c['open'],c['high'],c['low'],c['close'],c['volume']))
    a=np.array(rows,dtype=float);a=a[np.argsort(a[:,0],kind='stable')]
    _,i=np.unique(a[:,0],return_index=True);a=a[i]
    np.savez_compressed(f'{out}/{figi}.npz',a=a);print(figi,len(a),datetime.utcfromtimestamp(a[0,0]),datetime.utcfromtimestamp(a[-1,0]),flush=True)
