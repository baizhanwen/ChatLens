"""Real subprocess/HTTP integration using synthetic data only."""
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]
TOKEN='test-local-session-token-0000000000000000'

def events_10000():
    base=datetime(2026,10,26,10,tzinfo=timezone(timedelta(hours=8)))
    events=[]
    for i in range(9000):
        events.append({'platform':'wechat','account_id':'fictional','conversation_id':'recovery','sender_id':'a' if i%2 else 'b','source_id':str(i),'sent_at':(base+timedelta(seconds=9000-i)).isoformat(),'type':'text','content':'相同文本' if i%7 else '请明天提交测试报告','capture_mode':'replay','demo':True})
    events.extend(dict(events[i]) for i in range(500))
    events.extend(dict(events[i],revision=1,content='请明天提交更新后的报告') for i in range(300))
    events.extend(dict(events[i+300],revision=1,deleted=True,content='') for i in range(200))
    return events

class WorkerHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.proc=None;self.start()
    def tearDown(self):
        if self.proc and self.proc.poll() is None:self.proc.terminate();self.proc.wait(timeout=8)
        if self.proc:
            self.proc.stdout.close();self.proc.stderr.close()
        self.tmp.cleanup()
    def start(self):
        if self.proc and self.proc.poll() is not None:
            self.proc.stdout.close();self.proc.stderr.close()
        env=dict(os.environ,CHATLENS_TOKEN=TOKEN)
        self.proc=subprocess.Popen([sys.executable,'-m','services.worker.server','--port','0','--data-dir',self.tmp.name],cwd=str(ROOT),env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        line=self.proc.stdout.readline();info=json.loads(line);self.base='http://127.0.0.1:'+str(info['port'])
    def request(self,path,payload=None,token=TOKEN,origin=None):
        headers={'Authorization':'Bearer '+token}
        if origin:headers['Origin']=origin
        data=None
        if payload is not None:data=json.dumps(payload).encode();headers['Content-Type']='application/json'
        request=urllib.request.Request(self.base+'/api/'+quote(path,safe='/?=&%'),data=data,headers=headers)
        with urllib.request.urlopen(request,timeout=20) as response:return json.load(response)
    def test_security_and_model_outage(self):
        self.assertTrue(self.request('health')['ok'])
        for kw,status in (({'token':'wrong'},401),({'origin':'https://untrusted.test'},403)):
            with self.assertRaises(urllib.error.HTTPError) as exc:self.request('state',**kw)
            self.assertEqual(exc.exception.code,status)
        event=events_10000()[0]
        r=self.request('import',{'text':json.dumps(event),'format':'jsonl'});self.assertEqual(r['inserted'],1)
        self.request('settings',{'model_mode':'cloud','model_url':'https://example.invalid/v1','model_name':'test'})
        cid=self.request('conversations')['conversations'][0]['id'];self.request('analysis',{'conversation_id':cid,'kind':'qa','question':'报告'})
        time.sleep(.3)
        self.assertEqual(self.request('messages?q=报告')['total'],1)
        self.assertFalse('api_key' in json.dumps(self.request('state')).lower())
    def test_10000_replay_process_kill_recovery(self):
        events=events_10000();text='\n'.join(json.dumps(m,ensure_ascii=False) for m in events)
        began=time.monotonic();self.request('settings',{'analysis_window_seconds':300})
        self.request('replay',{'text':text,'format':'jsonl','speed':10000})
        deadline=time.time()+20
        cursor=0
        while time.time()<deadline:
            state=self.request('state');cursor=state['replay']['cursor']
            if cursor>=500:break
            time.sleep(.05)
        self.assertGreaterEqual(cursor,500)
        self.proc.kill();self.proc.wait(timeout=5)
        # Cursor and message insert are committed atomically before process death.
        con=sqlite3.connect(str(Path(self.tmp.name)/'chatlens.sqlite3'))
        checkpoint=con.execute('SELECT cursor FROM checkpoints').fetchone()[0]
        actual=con.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        self.assertEqual(checkpoint,actual)
        con.close()
        self.start();resumed=self.request('replay',{'text':text,'format':'jsonl','speed':10000})
        self.assertGreaterEqual(resumed['cursor'],checkpoint)
        deadline=time.time()+50
        while time.time()<deadline:
            state=self.request('state')
            if not state['replay']['running']:break
            time.sleep(.1)
        self.assertEqual(state['replay']['cursor'],10000)
        r=self.request('messages?limit=10000');self.assertEqual(r['total'],8800)
        self.assertEqual(len({m['id'] for m in r['messages']}),8800)
        self.assertEqual(sum(m['content']=='请明天提交更新后的报告' for m in r['messages']),300)
        cid=self.request('conversations')['conversations'][0]['id']
        self.request('settings',{'analysis_window_seconds':0})
        self.request('analysis',{'conversation_id':cid,'kind':'todos'})
        deadline=time.time()+10
        while time.time()<deadline:
            tasks=self.request('tasks');relevant=[j for j in tasks['jobs'] if j['kind']=='todos']
            if relevant and all(j['state']=='done' for j in relevant):break
            time.sleep(.1)
        todos=tasks['tasks'];self.assertGreater(len(todos),0);self.assertEqual(len(todos),len({t['id'] for t in todos}))
        evidence={'dataset':'10000 synthetic events: 9000 originals,500 duplicate,300 revision,200 revoked; reversed sent_at order','started_cursor_before_kill':cursor,'committed_cursor_after_kill':checkpoint,'committed_messages_after_kill':actual,'final_cursor':state['replay']['cursor'],'visible_messages':r['total'],'expected_visible_messages':8800,'updated_visible':300,'todo_count':len(todos),'todo_ids_unique':len(todos)==len({t['id'] for t in todos}),'elapsed_seconds':round(time.monotonic()-began,3),'python':sys.version,'system':os.uname().sysname+' '+os.uname().release,'capture_mode':'synthetic replay; NOT real client capture'}
        output=Path(os.environ.get('CHATLENS_TEST_EVIDENCE_DIR',str(ROOT/'.build/test-evidence')));output.mkdir(parents=True,exist_ok=True);(output/'worker-10000-recovery.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2))

if __name__=='__main__':unittest.main()
