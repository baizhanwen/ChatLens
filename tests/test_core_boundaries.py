"""Boundary regressions: concurrent claims, stale asynchronous results and opt-out."""
import json
import tempfile
import threading
import unittest
from services.worker.core import Store, normalize
from tests.test_core import message

class BoundaryTests(unittest.TestCase):
    def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def prepare(self,content='我会明天提交报告。'):
        m=normalize(message(content=content));self.s.ingest([m],schedule=False);return m
    def result(self,m):return {'items':[{'content':m['content'],'evidence_ids':[m['id']],'owner':'alice','due_at':'2026-10-27'}]}
    def test_async_edit_cannot_publish_old_todo_as_valid(self):
        m=self.prepare();jid=self.s.enqueue(m['conversation_id'],'todos');job=self.s.next_job()
        self.s.ingest([normalize(message(revision=1,content='取消提交安排。'))],schedule=False)
        self.s.save_analysis(job,[m],self.result(m))
        self.assertEqual(self.s.analyses()[0]['status'],'stale');self.assertEqual(self.s.tasks()['tasks'],[])
    def test_confirmed_task_is_invalid_after_source_recall(self):
        m=self.prepare();self.s.enqueue(m['conversation_id'],'todos');job=self.s.next_job();self.s.save_analysis(job,[m],self.result(m))
        todo=self.s.tasks()['tasks'][0];self.s.update_task({'id':todo['id'],'status':'confirmed'})
        self.s.ingest([normalize(message(revision=1,deleted=True,content=''))],schedule=False)
        updated=self.s.tasks()['tasks'][0]
        self.assertEqual(updated['status'],'stale');self.assertEqual(updated['evidence_status'],'invalid')
        with self.assertRaises(ValueError):self.s.context(m['id'])
    def test_deleted_conversation_cannot_reappear_from_inflight_analysis(self):
        m=self.prepare();self.s.enqueue(m['conversation_id'],'todos');job=self.s.next_job()
        self.s.delete_conversation(m['conversation_id']);self.s.save_analysis(job,[m],self.result(m))
        self.assertEqual(self.s.conversations(),[]);self.assertEqual(self.s.analyses(),[]);self.assertEqual(self.s.tasks(),{'tasks':[],'jobs':[]})
    def test_newest_snapshot_after_ten_thousand_stays_valid(self):
        self.s.ingest([normalize(message(i)) for i in range(10001)],schedule=False)
        m=self.s.messages(limit=1,newest=True)['messages'][0];self.s.enqueue(m['conversation_id'],'summary');job=self.s.next_job()
        self.s.save_analysis(job,[m],{'items':[{'claim':m['content'],'evidence_ids':[m['id']]}]})
        self.assertEqual(self.s.analyses()[0]['status'],'valid')
    def test_timeout_lease_rejects_late_output_and_allows_new_attempt(self):
        m=self.prepare();jid=self.s.enqueue(m['conversation_id'],'todos');old=self.s.next_job()
        # Fault injection models a provider that exceeded its persisted deadline.
        with self.s.lock,self.s.conn:self.s.conn.execute("UPDATE jobs SET updated_at='2000-01-01T00:00:00+00:00',timeout=1 WHERE id=?",(jid,))
        self.assertIsNone(self.s.next_job());self.assertIsNone(self.s.save_analysis(old,[m],self.result(m)));self.assertEqual(self.s.analyses(),[])
        with self.s.lock,self.s.conn:self.s.conn.execute('UPDATE jobs SET available_at=0 WHERE id=?',(jid,))
        current=self.s.next_job();self.assertEqual(current['attempts'],2)
        self.assertIsNone(self.s.save_analysis(old,[m],self.result(m)))
        self.s.save_analysis(current,[m],self.result(m));self.assertEqual(self.s.analyses()[0]['status'],'valid');self.assertEqual(len(self.s.tasks()['tasks']),1)
    def test_distinct_process_connections_claim_one_job_once(self):
        m=self.prepare();jid=self.s.enqueue(m['conversation_id'],'todos')
        second=Store(self.tmp.name);barrier=threading.Barrier(2);results=[];errors=[]
        def claim(store):
            try:barrier.wait();results.append(store.next_job())
            except Exception as exc:errors.append(exc)
        a=threading.Thread(target=claim,args=(self.s,));b=threading.Thread(target=claim,args=(second,));a.start();b.start();a.join();b.join();second.close()
        self.assertEqual(errors,[]);self.assertEqual(sum(r is not None for r in results),1);self.assertEqual([r['id'] for r in results if r],[jid])
    def test_new_message_during_analysis_preserves_followup_job(self):
        m=self.prepare();self.s.enqueue(m['conversation_id'],'todos');old=self.s.next_job()
        n=normalize(message(2,content='我会后天发送附件。'));self.s.ingest([n])
        self.s.save_analysis(old,[m],self.result(m))
        pending=[j for j in self.s.tasks()['jobs'] if j['kind']=='todos' and j['state']=='pending']
        self.assertEqual(len(pending),1);self.assertNotEqual(pending[0]['id'],old['id'])
    def test_capture_opt_out_is_transactional_and_never_reenabled_by_ingest(self):
        m=normalize(message(capture_mode='vision_window'));cid=self.s.ensure_capture_conversation('wechat','demo-account','test','受控测试')
        self.s.ingest([m],require_whitelisted=True);self.s.update_conversation({'id':cid,'whitelisted':False})
        with self.assertRaises(ValueError):self.s.ingest([normalize(message(2,capture_mode='vision_window'))],checkpoint={'id':'vision:x','cursor':2},require_whitelisted=True)
        self.assertFalse(self.s.conversation(cid)['whitelisted']);self.assertEqual(self.s.messages()['total'],1);self.assertIsNone(self.s.checkpoint('vision:x'))
    def test_claim_without_evidence_cannot_be_saved_or_corrected(self):
        m=self.prepare();self.s.enqueue(m['conversation_id'],'summary');job=self.s.next_job()
        with self.assertRaises(ValueError):self.s.save_analysis(job,[m],{'items':[{'claim':'没有证据的结论'}]})
        self.s.save_analysis(job,[m],{'items':[{'claim':'有原文支持','evidence_ids':[m['id']]}]})
        with self.assertRaises(ValueError):self.s.correct_analysis(job['id'],{'items':[{'claim':'擅自添加的结论'}]})
    def test_model_urls_cannot_store_credentials_in_query_or_fragment(self):
        for key in ('model_url','vision_url'):
            for url in ('https://example.invalid/v1?api_key=synthetic-secret','https://example.invalid/v1#token=synthetic-secret'):
                with self.assertRaises(ValueError):self.s.update_settings({key:url})
        self.assertEqual(self.s.settings()['model_url'],'');self.assertEqual(self.s.settings()['vision_url'],'')
    def test_visual_crop_settings_validate_real_bounds(self):
        self.s.update_settings({'vision_crop':[.25,0,.75,.85],'vision_crop_acknowledged':True})
        for bad in ([.4,0,.8,.5],[0,0,0,.5],[0,0,float('nan'),1],[True,0,1,1]):
            with self.assertRaises(ValueError):self.s.update_settings({'vision_crop':bad})

    def test_optional_bad_fields_are_reported_as_invalid_lines(self):
        text='\n'.join(json.dumps(message(i,**kw)) for i,kw in enumerate([{'source_conversation_id':[]},{'timezone':'No/SuchZone'},{'metadata':'not-object'},{'revision':1.2}]))
        r=self.s.import_text(text);self.assertEqual(r['inserted'],0);self.assertEqual([e['line'] for e in r['errors']],[1,2,3,4])

if __name__=='__main__':unittest.main()
