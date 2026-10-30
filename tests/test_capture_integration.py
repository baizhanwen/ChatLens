import tempfile
import unittest
from unittest.mock import patch
from services.worker.core import Store, opaque
from services.worker.server import Runtime


class FakeAdapter:
    def __init__(self,*args,**kwargs):self._options={};self.saved={};self.stopped=False
    def start(self,**kwargs):self._options=kwargs;return self.health()
    def health(self):return {'state':'stopped' if self.stopped else 'waiting','status':'受限','gaps':[{'at':'2026-10-26T10:00:00Z','reason':'synthetic_gap'}]}
    def checkpoint(self):return {'cursor':0,'previous':[]}
    def stop(self):self.stopped=True


class CaptureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name);self.runtime=Runtime(self.store)
        self.payload={'platform':'wechat','account_id':'synthetic','conversation_id':'controlled','window_id':123,
                      'expected_conversation':'虚构会话','acknowledged_scope':True,'crop_acknowledged':True,'watch_changes':True}
    def tearDown(self):self.runtime.close();self.temp.cleanup()
    def configure(self):self.store.update_settings({'vision_mode':'local','vision_url':'http://127.0.0.1:12345/v1','vision_name':'synthetic-mock'})
    def event(self):return {'platform':'wechat','account_id':'synthetic','conversation_id':'controlled','source_id':'v1',
        'sender_id':'unknown','sent_at':'2026-10-26T18:00:00+08:00','type':'text','content':'完全虚构','capture_mode':'vision_window'}
    def test_missing_model_does_not_create_capture_whitelist(self):
        with self.assertRaises(ValueError):self.runtime.start_capture(self.payload)
        self.assertEqual(self.store.conversations(),[])
    def test_batch_cursor_white_list_and_deletion_boundary(self):
        self.configure()
        with patch('services.worker.adapters.WindowVisionAdapter',FakeAdapter):self.runtime.start_capture(self.payload)
        adapter=self.runtime.capture;callback=adapter._options['on_batch']
        cp={'cursor':1,'previous':['synthetic-overlap'],'adapter_version':'mock'}
        callback([self.event()],cp)
        cid=opaque('wechat','synthetic','controlled')
        self.assertEqual(self.store.checkpoint('vision:'+cid)['metadata']['previous'],['synthetic-overlap'])
        self.assertTrue(adapter._options['watch_changes'])
        self.runtime.capture_health()
        self.assertEqual(len(self.store.conversations()[0]['gaps']),1)
        self.store.update_conversation({'id':cid,'whitelisted':False})
        with self.assertRaises(ValueError):callback([dict(self.event(),source_id='v2')],dict(cp,cursor=2))
        self.assertFalse(self.store.conversations()[0]['whitelisted'])
        self.assertEqual(self.store.messages(cid)['total'],1)
        self.store.delete_conversation(cid)
        self.assertIsNone(self.store.checkpoint('vision:'+cid))
        with self.assertRaises(ValueError):callback([dict(self.event(),source_id='v3')],dict(cp,cursor=3))
        self.assertEqual(self.store.conversations(),[])
        self.runtime.capture_options=None
    def test_stopping_twice_after_delete_does_not_restore_sensitive_checkpoint(self):
        self.configure()
        with patch('services.worker.adapters.WindowVisionAdapter',FakeAdapter):self.runtime.start_capture(self.payload)
        cid=opaque('wechat','synthetic','controlled')
        self.runtime.stop_capture()
        self.store.delete_conversation(cid)
        self.runtime.stop_capture()
        self.assertIsNone(self.store.checkpoint('vision:'+cid))
    def test_crop_and_cloud_consent_gate_before_adapter(self):
        self.configure()
        with self.assertRaises(ValueError):self.runtime.start_capture(dict(self.payload,crop_acknowledged=False))
        self.store.update_settings({'vision_mode':'cloud','vision_url':'https://example.com/v1'})
        with self.assertRaises(ValueError):self.runtime.start_capture(self.payload)


if __name__=='__main__':unittest.main()
