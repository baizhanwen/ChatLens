"""Authenticated AI HTTP surface; synthetic manager, no provider calls."""
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'scripts/windows-proof'))
from ai_web_server import attach_ai_routes
from ai_service import AiError
from ai_provider import ProviderError
from web_server import make_server


class SyntheticAi:
    def __init__(self):
        self.calls = []
        self.error = None

    def status(self):
        if self.error:
            raise self.error
        return {'provider': {'configured': False}, 'knowledge': {'message_count': 0}}

    def config(self):
        return {'base_url': '', 'model': '', 'has_key': False, 'configured': False}

    def update_config(self, value):
        self.calls.append(('config', value))
        return self.config()

    def ask(self, value):
        self.calls.append(('ask', value))
        return {'job_id': 'a' * 32, 'thread_id': 'b' * 32}

    def test(self, value):
        self.calls.append(('test', value))
        return {'job_id': 'a' * 32}

    def list_threads(self):
        return {'threads': []}

    def get_thread(self, identifier):
        return {'id': identifier, 'messages': []}

    def new_thread(self):
        self.calls.append(('thread', {}))
        return {'id': 'b' * 32, 'messages': []}

    def get_job(self, identifier):
        return {'id': identifier, 'stage': 'retrieving'}

    def cancel(self, identifier):
        self.calls.append(('cancel', identifier))
        return {'id': identifier, 'stage': 'cancelled'}

    def evidence(self, alias, identifier):
        self.calls.append(('evidence', alias, identifier))
        return {'target_id': identifier, 'messages': []}


class SyntheticData:
    def conversations(self):
        return {'conversations': []}

    def messages(self, *args, **kwargs):
        return {'messages': []}

    def status(self):
        return {'state': 'stopped'}


class AiWebServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-ai-http-')
        self.addCleanup(self.temp.cleanup)
        self.ai = SyntheticAi()
        self.server = make_server(SyntheticData(), Path(self.temp.name).resolve())
        attach_ai_routes(self.server, lambda: self.ai)
        self.worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.worker.start()
        self.cookie = None

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(timeout=2)

    def request(self, method, path, body=None, headers=None, *, auth=True):
        fields = {'Origin': self.server.base_url}
        if auth:
            fields['Cookie'] = self.cookie or 'chatlens_session=' + self.server.token
        if isinstance(body, dict):
            body = json.dumps(body).encode();fields['Content-Type'] = 'application/json'
        fields.update(headers or {})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        connection.request(method, path, body=body, headers=fields)
        response = connection.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        connection.close()
        return result

    def test_authenticated_status_is_honest_and_existing_message_api_remains_available(self):
        code, result, headers = self.request('GET', '/api/ai/status')
        self.assertEqual(code, 200)
        self.assertFalse(result['provider']['configured'])
        self.assertEqual(result['knowledge']['message_count'], 0)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(self.request('GET', '/api/messages?conversation=private_test')[1], {'messages': []})
        self.assertEqual(self.ai.calls, [])

    def test_no_cookie_cross_origin_and_wrong_host_cannot_mutate(self):
        cases = [({'Origin': 'https://example.invalid'}, True),
                 ({'Host': 'example.invalid'}, True), ({}, False)]
        for headers, authenticated in cases:
            with self.subTest(headers=headers):
                code, _, _ = self.request('POST', '/api/ai/config', {}, headers=headers, auth=authenticated)
                self.assertIn(code, (401, 403))
        self.assertEqual(self.ai.calls, [])

    def test_mutations_require_origin_and_do_not_accept_unbounded_or_ambiguous_json(self):
        cases = [(b'{}', {'Content-Type': 'application/json', 'Origin': ''}),
                 (b'{"model":"a","model":"b"}', {'Content-Type': 'application/json'}),
                 (b'{"x":NaN}', {'Content-Type': 'application/json'}),
                 (b'[]', {'Content-Type': 'application/json'}),
                 (b'{}', {'Content-Type': 'text/plain'}),
                 (b'{}', {'Content-Type': 'application/json', 'Content-Length': '32769'}),
                 (b'{}', {'Content-Type': 'application/json', 'Transfer-Encoding': 'chunked'})]
        for body, headers in cases:
            with self.subTest(headers=headers):
                self.assertIn(self.request('POST', '/api/ai/questions', body, headers)[0], (400, 403))
        self.assertEqual(self.ai.calls, [])

    def test_question_and_key_use_body_only_and_public_reply_has_no_credential(self):
        secret = 'SYNTHETIC_API_KEY_ONLY'
        body = {'base_url': 'https://example.invalid/v1', 'model': 'synthetic', 'key_action': 'replace', 'api_key': secret}
        code, result, _ = self.request('POST', '/api/ai/config', body)
        self.assertEqual(code, 200)
        self.assertEqual(self.ai.calls, [('config', body)])
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(self.request('POST', '/api/ai/questions', {'question': '合成问题'})[0], 200)
        self.assertEqual(self.ai.calls[-1], ('ask', {'question': '合成问题'}))

    def test_job_thread_and_evidence_routes_have_explicit_identifier_parsing(self):
        self.assertEqual(self.request('GET', '/api/ai/jobs/' + 'a' * 32)[1]['stage'], 'retrieving')
        self.assertEqual(self.request('GET', '/api/ai/threads/' + 'b' * 32)[1]['messages'], [])
        self.assertEqual(self.request('POST', '/api/ai/jobs/' + 'a' * 32 + '/cancel', {})[1]['stage'], 'cancelled')
        self.assertEqual(self.request('GET', '/api/ai/evidence?conversation_id=private_test&message_id=7')[1]['target_id'], 7)
        self.assertEqual(self.ai.calls[-1], ('evidence', 'private_test', 7))
        for path in ('/api/ai/jobs/../config', '/api/ai/jobs/not-an-id', '/api/ai/threads?api_key=secret'):
            self.assertIn(self.request('GET', path)[0], (400, 404))
        for query in ('conversation_id=private_test&message_id=-1', 'conversation_id=private_test&message_id=7&message_id=8',
                      'conversation_id=private_test&message_id=7&secret=foo'):
            self.assertEqual(self.request('GET', '/api/ai/evidence?' + query)[0], 400)

    def test_unsafe_or_unknown_exceptions_never_echo_private_provider_details(self):
        secret = 'SYNTHETIC_PRIVATE_ROOT_KEY_CHAT'
        for error in (RuntimeError(secret), ProviderError(secret)):
            self.ai.error = error
            code, value, _ = self.request('GET', '/api/ai/status')
            self.assertIn(code, (400, 503))
            self.assertNotIn(secret, json.dumps(value))
        self.ai.error = AiError('ai_target_changed', '请重新核对目标。', 409)
        code, value, _ = self.request('GET', '/api/ai/status')
        self.assertEqual(code, 409)
        self.assertEqual(value['error']['code'], 'ai_target_changed')


if __name__ == '__main__':
    unittest.main()
