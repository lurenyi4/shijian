"""Real local API + MinerU integration check on generated samples only."""
import json
import time
from pathlib import Path

import httpx
from PIL import Image

base='http://127.0.0.1:8765'
qa=Path(__file__).parent/'.qa'
with Image.open(qa/'printed-sample.png') as image:
    image.resize((800,1000)).save(qa/'printed-sample-two.png')
with httpx.Client(base_url=base,timeout=120) as client:
    data=client.get('/api/state').json()
    client.headers['X-Shijian-Session']=data['session']
    ids=[]
    for filename in ('printed-sample.png','printed-sample-two.png'):
        with (qa/filename).open('rb') as f:
            res=client.post('/api/import?collection=流程测试',files={'file':(filename,f,'image/png')})
        res.raise_for_status()
        ids.append(res.json()['document']['id'])
    result=client.post('/api/recognize',json={'ids':ids,'force':True})
    result.raise_for_status()
    assert len(result.json()['accepted'])==2,result.text
    started=time.monotonic()
    while time.monotonic()-started < 600:
        docs=[client.get('/api/documents/'+doc_id).json() for doc_id in ids]
        if all(d['status'] not in ('queued','running') for d in docs):
            break
        time.sleep(2)
    assert all(d['status']=='done' for d in docs),[(d['status'],d['error']) for d in docs]
    assert all('床前明月光' in d['text'] and '低头思故乡' in d['text'] for d in docs)
    assert all(not d['reviewed'] and d['text']==d['raw_text'] for d in docs)
    result={'batch':len(docs),'seconds':round(time.monotonic()-started,2),'status':'passed',
            'documents':[{'id':d['id'],'text':d['text'],'reviewed':d['reviewed']} for d in docs]}
    (qa/'integration-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'passed','batch':2,'seconds':result['seconds']}))
