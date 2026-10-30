import csv
import io
import json
import os
import tempfile
import threading
import unittest
from services.worker.core import Store, normalize, parse_text, now

def message(i=1,**kw):
    m={'platform':'wechat','account_id':'demo-account','conversation_id':'test','sender_id':'alice','source_id':str(i),'sent_at':'2026-10-26T10:00:00+08:00','type':'text','content':'相同的真实文本','demo':True}
    m.update(kw);return m

class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
    def tearDown(self):
        self.s.close();self.tmp.cleanup()
    def test_import_1000_idempotency_duplicates_and_invalid_lines(self):
        text='\n'.join(json.dumps(message(i)) for i in range(1000))+'\n{bad}\n'+json.dumps({'platform':'wechat'})
        result=self.s.import_text(text);self.assertEqual(result['inserted'],1000);self.assertEqual([e['line'] for e in result['errors']],[1001,1002])
        result=self.s.import_text(text);self.assertEqual(result['inserted'],0);self.assertEqual(result['unchanged'],1000)
        self.assertEqual(self.s.messages()['total'],1000)
    def test_account_identity_separation(self):
        self.s.ingest([normalize(message(1)),normalize(message(1,account_id='another'))]);self.assertEqual(len(self.s.conversations()),2)
    def test_required_timestamp_timezone(self):
        with self.assertRaises(ValueError):normalize(message(sent_at='2026-10-26T10:00:00'))
    def test_csv_multiline(self):
        out=io.StringIO();m=message(content='第一行\n第二行');writer=csv.DictWriter(out,fieldnames=list(m));writer.writeheader();writer.writerow(m)
        valid,errors=parse_text(out.getvalue(),'csv');self.assertEqual(errors,[]);self.assertEqual(valid[0][1]['content'],'第一行\n第二行')
    def test_unknown_media_kept(self):
        m=normalize(message(type='sticker'));self.s.ingest([m]);r=self.s.messages()['messages'][0];self.assertEqual(r['type'],'unknown');self.assertEqual(r['unsupported_type'],'sticker')
    def test_edit_revoke_stale_and_fts(self):
        m=normalize(message());self.s.ingest([m]);cid=m['conversation_id'];jid=self.s.enqueue(cid,'summary');job={'id':jid,'conversation_id':cid,'kind':'summary','payload':{}}
        self.s.save_analysis(job,[m],{'items':[{'claim':'测试','evidence_ids':[m['id']]}]})
        self.s.ingest([normalize(message(revision=1,content='新内容'))]);self.assertEqual(self.s.analyses()[0]['status'],'stale')
        self.assertEqual(self.s.messages(q='真实文本')['total'],0)
        self.s.ingest([normalize(message(revision=2,deleted=True,content=''))]);self.assertEqual(self.s.messages()['total'],0)
        self.assertEqual(self.s.conn.execute('SELECT COUNT(*) FROM messages_fts').fetchone()[0],0)
        with self.assertRaises(ValueError):self.s.context(m['id'])
        self.s.ingest([m]);self.assertEqual(self.s.messages()['total'],0)
    def test_order_pagination_and_filters(self):
        msgs=[normalize(message(i,sent_at='2026-10-26T10:%02d:00+08:00'%i,sender_id='a' if i%2 else 'b',content='100%_text')) for i in reversed(range(30))]
        self.s.ingest(msgs);a=self.s.messages(limit=10);b=self.s.messages(limit=10,offset=10)
        self.assertEqual(a['total'],30);self.assertLess(a['messages'][-1]['sent_at'],b['messages'][0]['sent_at']);self.assertEqual(self.s.messages(sender='a')['total'],15)
        self.assertEqual(self.s.messages(q='%_')['total'],30);self.assertEqual(self.s.messages(q='%x')['total'],0)
    def test_checkpoint_atomic_and_recovery(self):
        m=normalize(message());self.s.ingest([m],checkpoint={'id':'replay:x','cursor':1});cid=m['conversation_id'];job=self.s.next_job()
        self.assertIsNone(job) # merge window not closed
        jid=self.s.enqueue(cid,'qa',{'question':'x'});job=self.s.next_job();self.assertEqual(job['id'],jid)
        self.s.close();self.s=Store(self.tmp.name);self.assertEqual(self.s.checkpoint('replay:x')['cursor'],1)
        self.assertEqual(self.s.conn.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()[0],'pending')
    def test_transaction_rollback(self):
        good=normalize(message());bad=normalize(message(2));bad.pop('content')
        with self.assertRaises(KeyError):self.s.ingest([good,bad],checkpoint={'id':'replay:x','cursor':2})
        self.assertEqual(self.s.messages()['total'],0);self.assertIsNone(self.s.checkpoint('replay:x'))
    def test_invalid_evidence_rejected(self):
        m=normalize(message());self.s.ingest([m]);jid=self.s.enqueue(m['conversation_id'],'todos')
        with self.assertRaises(ValueError):self.s.save_analysis({'id':jid,'conversation_id':m['conversation_id'],'kind':'todos','payload':{}},[m],{'items':[{'content':'伪造','evidence_ids':['missing']}]})
    def test_todo_idempotency_and_correction(self):
        m=normalize(message());self.s.ingest([m]);cid=m['conversation_id'];result={'items':[{'content':'提交报告','owner':None,'due_at':None,'evidence_ids':[m['id']]}]}
        for i in range(2):
            jid=self.s.enqueue(cid,'todos',{'round':i});self.s.save_analysis({'id':jid,'conversation_id':cid,'kind':'todos','payload':{}},[m],result)
        todos=self.s.tasks()['tasks'];self.assertEqual(len(todos),1);self.assertIsNone(todos[0]['due_at']);self.s.update_task({'id':todos[0]['id'],'status':'done','owner':'user','due_at':'2026-10-27'});self.assertEqual(self.s.tasks()['tasks'][0]['status'],'done');self.assertEqual(self.s.tasks()['tasks'][0]['due_at'],'2026-10-27')
    def test_delete_cascades(self):
        m=normalize(message());self.s.ingest([m]);cid=m['conversation_id'];jid=self.s.enqueue(cid,'todos');self.s.save_analysis({'id':jid,'conversation_id':cid,'kind':'todos','payload':{}},[m],{'items':[{'content':'任务','evidence_ids':[m['id']]}]})
        self.assertEqual(self.s.delete_conversation(cid)['deleted_messages'],1)
        self.assertEqual(self.s.messages(q='真实文本')['total'],0);self.assertEqual(self.s.tasks(),{'tasks':[],'jobs':[]});self.assertEqual(self.s.analyses(),[])
        self.assertEqual(self.s.conn.execute('SELECT COUNT(*) FROM messages_fts').fetchone()[0],0)
    def test_permission_and_settings_no_secret(self):
        self.assertEqual(os.stat(self.s.path).st_mode&0o777,0o600)
        with self.assertRaises(ValueError):self.s.update_settings({'api_key':'secret'})
        with self.assertRaises(ValueError):self.s.update_settings({'model_url':'https://user:pass@example.com'})
        self.assertEqual(self.s.settings()['model_mode'],'rules');self.assertEqual(self.s.settings()['vision_mode'],'disabled')
    def test_retention_removes_original_and_derived(self):
        self.s.ingest([normalize(message(sent_at='2020-01-01T00:00:00+00:00'))]);self.s.update_settings({'retention_days':30})
        self.assertEqual(self.s.messages()['total'],0);self.assertEqual(self.s.conversations(),[]);self.assertEqual(self.s.tasks(),{'tasks':[],'jobs':[]})
    def test_all_messages_and_latest_window(self):
        self.s.ingest([normalize(message(i,sent_at='2026-10-26T10:00:00+08:00')) for i in range(10001)],schedule=False)
        self.assertEqual(len(self.s.all_messages()),10001)
        latest=self.s.messages(limit=1,newest=True)['messages'];allrows=self.s.all_messages();self.assertEqual(latest[0]['id'],allrows[-1]['id'])
    def test_context(self):
        self.s.ingest([normalize(message(i,sent_at='2026-10-26T10:%02d:00+08:00'%i)) for i in range(10)])
        mid=self.s.messages(offset=5,limit=1)['messages'][0]['id'];ctx=self.s.context(mid,radius=2);self.assertEqual(len(ctx['messages']),5);self.assertEqual(ctx['messages'][2]['id'],mid)

if __name__=='__main__':unittest.main()
