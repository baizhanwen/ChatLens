"""Real authenticated loopback routing over a temporary store and fake model.

No WeChat files, existing private data, credentials or external network are used.
"""
from datetime import datetime, timedelta, timezone
import hashlib
from http.client import HTTPConnection
import json
from pathlib import Path
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ai_service as fixtures
from ai_web_server import attach_ai_routes
from web_data import CollectorWebData
from web_server import make_server


class RoutingHttpTests(unittest.TestCase):
    add = fixtures.AiAssistantTests.add

    def setUp(self):
        fixtures.AiAssistantTests.setUp(self)
        self.server = attach_ai_routes(make_server(CollectorWebData(self.data),
            Path(__file__).resolve().parents[1] / 'apps' / 'web'), lambda: self.assistant)
        self.worker = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': .01}, daemon=True)
        self.worker.start()
        status, _, headers = self.request('GET', '/api/session', headers={
            'Authorization': 'Bearer ' + self.server.token}, authenticated=False)
        self.assertEqual(status, 200)
        self.cookie = headers['Set-Cookie'].split(';', 1)[0]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=3)
        fixtures.AiAssistantTests.tearDown(self)

    def request(self, method, route, value=None, *, headers=None, authenticated=True):
        actual = dict(headers or {})
        if authenticated:
            actual.setdefault('Cookie', self.cookie)
        if method == 'POST':
            actual.setdefault('Origin', self.server.base_url)
            actual.setdefault('Content-Type', 'application/json')
        raw = json.dumps(value, ensure_ascii=False).encode() if value is not None else None
        connection = HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=5)
        try:
            connection.request(method, route, raw, actual)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def question(self, question, **extra):
        return {'question': question, 'scope': ['private_test', 'group_test'],
                'time_range': '7d', **extra}

    def completed(self, value):
        code, launched, _ = self.request('POST', '/api/ai/questions', value)
        self.assertEqual(code, 200, launched)
        self.assistant.jobs[launched['job_id']]['worker'].join(timeout=4)
        code, result, _ = self.request('GET', '/api/ai/jobs/' + launched['job_id'])
        self.assertEqual(code, 200)
        self.assertEqual(result['stage'], 'done', result)
        return result

    def test_preview_requires_session_and_same_origin_without_history_or_database_writes(self):
        self.add('明日交货。')
        value = self.question('今天都谁给我发信息了')
        before = hashlib.sha256(self.assistant.data.database.read_bytes()).hexdigest()
        for authenticated, headers, expected in [
                (False, {}, 401), (True, {'Origin': 'https://synthetic.invalid'}, 403)]:
            code, _, _ = self.request('POST', '/api/ai/plan', value,
                                      authenticated=authenticated, headers=headers)
            self.assertEqual(code, expected)
        code, result, headers = self.request('POST', '/api/ai/plan', value)
        self.assertEqual(code, 200)
        self.assertEqual(result['plan']['route'], 'local_stats')
        self.assertFalse(result['plan']['requires_model'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.assistant.list_threads()['threads'], [])
        self.assertEqual(hashlib.sha256(self.assistant.data.database.read_bytes()).hexdigest(), before)
        self.assertEqual(self.provider.calls, [])
        self.assertNotIn('synthetic_raw_sender_id', json.dumps(result))

    def test_statistics_and_followup_work_without_model_and_recount_committed_rows(self):
        self.assistant.update_config({'base_url': 'https://synthetic.invalid/v1', 'model': 'synthetic',
                                     'key_action': 'delete', 'api_key': ''})
        self.add('今天第一条。')
        self.add('昨日第二条。', 'group_test')
        yesterday = datetime.now(timezone(timedelta(hours=8))).replace(hour=12) - timedelta(days=1)
        self.store._connection.execute('UPDATE messages SET sent_at=? WHERE rowid=2',
                                       (int(yesterday.timestamp()),))
        first = self.completed(self.question('今天共有多少条消息'))
        self.assertEqual(first['response_kind'], 'statistics')
        self.assertIsNone(first['model'])
        self.assertEqual(first['statistics']['message_count'], 1)
        second = self.completed(self.question('那昨天呢', thread_id=first['thread_id']))
        self.assertEqual(second['plan']['time_label'], '昨天')
        self.assertEqual(second['statistics']['message_count'], 1)
        self.add('今天新增的一条。')
        third = self.completed(self.question('再统计今天共有多少条消息'))
        self.assertEqual(third['statistics']['message_count'], 2)
        code, history, _ = self.request('GET', '/api/ai/threads/' + first['thread_id'])
        self.assertEqual(code, 200)
        self.assertEqual(history['messages'][-1]['response_kind'], 'statistics')
        self.assertEqual(history['messages'][-1]['plan']['time_label'], '昨天')
        self.assertEqual(self.provider.calls, [])

    def test_server_replans_questions_and_clarifies_outside_ui_window_without_model(self):
        self.add('仅合成数据。')
        for extra, expected in [({'plan': {'route': 'local_stats'}}, 'ai_question_fields_invalid'),
                                ({'scope': ['unselected']}, 'ai_scope_invalid')]:
            code, value, _ = self.request('POST', '/api/ai/questions', self.question('今天有哪些重点', **extra))
            self.assertEqual(code, 400)
            self.assertEqual(value['error']['code'], expected)
        result = self.completed(self.question('上个月一共有多少条消息', time_range='today'))
        self.assertEqual(result['plan']['route'], 'clarify')
        self.assertEqual(result['response_kind'], 'system')
        self.assertIsNone(result['model'])
        self.assertEqual(self.provider.calls, [])

    def test_mixed_analysis_enforces_cloud_confirmation_and_sends_exact_aggregates(self):
        for index in range(35):
            self.add('交付进度已更新%d。' % index, 'private_test' if index % 2 else 'group_test')
        value = self.question('统计今天各会话消息数量并分析重点')
        code, result, _ = self.request('POST', '/api/ai/plan', value)
        self.assertEqual(code, 200)
        self.assertEqual(result['plan']['route'], 'mixed')
        fingerprint = self.assistant.config()['target_fingerprint']
        code, result, _ = self.request('POST', '/api/ai/questions',
                                        dict(value, target_fingerprint=fingerprint))
        self.assertEqual(code, 409)
        self.assertEqual(result['error']['code'], 'ai_cloud_scope_confirmation_required')
        self.assertEqual(self.provider.calls, [])
        result = self.completed(dict(value, target_fingerprint=fingerprint, confirmed_target=fingerprint))
        self.assertEqual(result['statistics']['message_count'], 35)
        self.assertEqual(result['statistics']['group_by'], 'conversation')
        self.assertEqual(len(result['statistics']['rows']), 2)
        self.assertEqual(sorted(row['message_count'] for row in result['statistics']['rows']), [17, 18])
        sent = json.loads(self.provider.calls[0]['messages'][-1]['content'])
        self.assertEqual(sent['local_statistics']['message_count'], 35)
        self.assertEqual(sent['local_statistics']['group_by'], 'conversation')
        self.assertLessEqual(len(sent['evidence']['messages']), 30)
        self.assertIn('35', result['answer'])


if __name__ == '__main__':
    unittest.main()
