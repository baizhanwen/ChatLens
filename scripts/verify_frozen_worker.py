"""Standalone frozen executable acceptance, synthetic data only."""
import json
import os
import secrets
import select
import subprocess
import tempfile
import time
import urllib.request
import urllib.parse
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def main():
    binary=ROOT/'dist/worker'/('chatlens-worker.exe' if os.name=='nt' else 'chatlens-worker')
    token=secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix='chatlens-frozen-test-') as directory:
        proc=subprocess.Popen([str(binary),'--port','0','--data-dir',directory],stdout=subprocess.PIPE,stderr=subprocess.PIPE,
             env=dict(os.environ,CHATLENS_TOKEN=token,CHATLENS_NATIVE_DIR=str(ROOT/'scripts/native'),CHATLENS_COMPATIBILITY_PATH=str(ROOT/'docs/compatibility.json')),text=True,cwd=str(ROOT))
        try:
            if os.name!='nt' and not select.select([proc.stdout],[],[],30)[0]:raise RuntimeError('frozen worker readiness timed out')
            ready=json.loads(proc.stdout.readline());port=ready['port']
            def api(route,payload=None):
                data=None if payload is None else json.dumps(payload).encode()
                req=urllib.request.Request('http://127.0.0.1:'+str(port)+'/api'+urllib.parse.quote(route,safe='/?=&'),data=data,headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=15) as response:return json.load(response)
            lines=(ROOT/'fixtures/demo-customer.jsonl').read_text().splitlines()[:30]
            result=api('/import',{'text':'\n'.join(lines),'format':'jsonl'})
            assert result['inserted']==30,result
            conversation=api('/conversations')['conversations'][0]['id']
            deadline=time.monotonic()+30
            while True:
                analyses=api('/analyses?conversation_id='+conversation)['analyses']
                kinds={a['kind'] for a in analyses if a['status']=='valid'}
                if {'summary','todos','customer','communication'}.issubset(kinds):break
                if time.monotonic()>deadline:raise RuntimeError('frozen child analysis did not complete: '+str([(j['kind'],j['state'],j['error']) for j in api('/tasks')['jobs']]))
                time.sleep(.2)
            assert api('/tasks')['tasks']
            assert api('/messages?conversation_id='+conversation+'&q=预算')['total']>0
            report=api('/export',{'conversation_id':conversation,'kind':'customer','format':'json'})
            assert json.loads(report['content'])['brand']=='利明智耀'
            assert api('/capabilities')['capabilities']['release_status']
            api('/conversations/delete',{'id':conversation})
            assert not api('/conversations')['conversations']
            assert not api('/tasks')['tasks']
            evidence={'status':'passed','tested_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'),'binary':str(binary.relative_to(ROOT)),
                 'input':'30 completely fictional messages','checks':['frozen worker starts','spawned analysis subprocesses complete','four analysis kinds valid','todos persisted','search','branded JSON report','bundled compatibility route','delete cascades'],
                 'real_client_test':False,'real_model_test':False,'messages_uploaded':False}
            (ROOT/'docs/evidence/frozen-worker.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))
            print(json.dumps(evidence,ensure_ascii=False))
        finally:
            proc.terminate()
            try:proc.wait(timeout=5)
            except subprocess.TimeoutExpired:proc.kill();proc.wait()

if __name__=='__main__':main()
