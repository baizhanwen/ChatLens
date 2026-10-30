"""Protocol mock only: does not assert any real provider or model quality."""
import base64
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from services.worker.analysis import analyze
from services.worker.vision import extract_messages
from services.worker.server import bounded_analysis


class ModelProtocolTests(unittest.TestCase):
    def setUp(self):
        self.requests=[];self.reply={'items':[{'claim':'预算原文','classification':'fact','category':'budget','evidence_ids':['m1']} ]}
        self.delay=0
        outer=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.requests.append(body)
                time.sleep(outer.delay)
                data=json.dumps({'choices':[{'message':{'content':json.dumps(outer.reply,ensure_ascii=False)}}]}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers()
                try:self.wfile.write(data)
                except BrokenPipeError:pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.config={'mode':'local','url':'http://127.0.0.1:'+str(self.server.server_port)+'/v1','name':'protocol-mock-no-real-model'}
        self.rows=[{'id':'m1','sender_id':'虚构甲','content':'预算是5000元','sent_at':'2026-10-26T16:00:00+08:00'}]
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
    def test_text_mock_protocol_evidence_and_no_tools(self):
        result=analyze(self.rows,'customer',model_config=self.config)
        self.assertEqual(result['mode'],'local')
        self.assertEqual(result['items'][0]['evidence_ids'],['m1'])
        self.assertNotIn('tools',self.requests[0])
        self.assertIn('untrusted_messages',self.requests[0]['messages'][1]['content'])
    def test_invalid_provider_citation_not_saved(self):
        self.reply['items'][0]['evidence_ids']=['outside']
        with self.assertRaises(ValueError):analyze(self.rows,'summary',model_config=self.config)
    def test_vision_synthetic_image_payload_and_missing_timestamp(self):
        self.reply={'messages':[{'content':'完全虚构测试','confidence':.85,'sender_id':'unknown','sent_at':None,'type':'text'}],
                    'coverage':{'conversation_matches':True,'visible_title':'测试会话'},'uncertain':['时间未见']}
        synthetic=b'\x89PNG\r\n\x1a\n' + b'NOT_A_REAL_SCREENSHOT'
        result=extract_messages(synthetic,self.config,{'expected_conversation':'测试会话'})
        self.assertEqual(result['messages'][0]['timestamp_quality'],'observed_only_not_original')
        image=self.requests[0]['messages'][1]['content'][1]['image_url']['url']
        self.assertEqual(base64.b64decode(image.split(',')[1]),synthetic)
    def test_hung_model_is_terminated_without_blocking_queue(self):
        self.delay=3
        started=time.monotonic()
        with self.assertRaises(TimeoutError):bounded_analysis(self.rows,'summary','Asia/Shanghai',None,self.config,timeout=.3)
        self.assertLess(time.monotonic()-started,2)
        result=bounded_analysis(self.rows,'summary','Asia/Shanghai',None,{'mode':'rules'},timeout=3)
        self.assertEqual(result['mode'],'rules')


if __name__=='__main__':unittest.main()
