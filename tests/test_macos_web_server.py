"""Synthetic onboarding manager only; never access real private/source files."""
import contextlib
import http.client
import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlencode

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from macos_web_server import make_macos_server
from web_server import COOKIE, CSP

TOKEN = 'SYNTHETIC_MAC_WEB_TOKEN_' + 'x' * 32


class SyntheticManager:
    def __init__(self):
        self.calls = []
        self.failure = None

    def setup(self):
        self.calls.append(('setup',))
        if self.failure:
            raise self.failure
        return {'stage': 'ready_to_select', 'reason': None,
                'account': {'detected': True, 'display_name': '合成账号'}, 'scope_limits': {'private': 1, 'group': 1}}

    def list_conversations(self, q):
        self.calls.append(('list', q))
        return {'conversations': [{'id': 'synthetic-private', 'name': '合成客户', 'type': 'private'}], 'has_more': False}

    def request_initialize(self):
        # The real manager must enqueue work on its own thread; HTTP routes
        # only call this method and never import source/SQLite code themselves.
        self.calls.append(('enqueue_initialize',))
        if self.failure:
            raise self.failure
        return {'accepted': True}

    def select(self, private_id, group_id):
        self.calls.append(('enqueue_selection', private_id, group_id))
        return {'stage': 'starting', 'reason': None}

    def conversations(self):
        self.calls.append(('reader_conversations',))
        return {'conversations': []}

    def messages(self, conversation, **parameters):
        self.calls.append(('reader_messages', conversation))
        return {'messages': [], 'has_more': False}

    def status(self):
        self.calls.append(('reader_status',))
        return {'state': 'stopped'}


class MacWebServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-macos-web-')
        self.static = Path(self.tmp.name).resolve()
        for name in ('index.html', 'app.js', 'styles.css', 'connect.html', 'connect.js', 'connect.css'):
            (self.static / name).write_text('SYNTHETIC_PUBLIC_ASSET ' + name, encoding='utf-8')
        (self.static / 'private.json').write_text('SYNTHETIC_PRIVATE_FILE_MUST_NOT_LEAK', encoding='utf-8')
        self.manager = SyntheticManager()
        self.server = make_macos_server(self.manager, self.static, token=TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path, *, method='GET', body=None, authenticated=True, origin=True, headers=None):
        current = dict(headers or {})
        if authenticated:
            current.setdefault('Cookie', COOKIE + '=' + TOKEN)
        if method == 'POST':
            current.setdefault('Content-Type', 'application/json')
            if origin:
                current.setdefault('Origin', self.server.base_url)
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=current)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def raw(self, lines, body=b''):
        with socket.create_connection(self.server.server_address, timeout=5) as connection:
            connection.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('ascii') + body)
            response = b''
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    return response
                response += chunk

    def test_connect_launch_and_fixed_assets_reuse_parent_security_headers(self):
        self.assertEqual(self.server.launch_url, self.server.base_url + '/connect.html#token=' + TOKEN)
        for path in ('/connect.html', '/connect.js', '/connect.css', '/index.html', '/app.js', '/styles.css'):
            status, headers, raw = self.request(path, authenticated=False)
            self.assertEqual(status, 200)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
            self.assertEqual(headers['Content-Security-Policy'], CSP)
            self.assertNotIn(TOKEN.encode(), raw)
            self.assertNotIn('Access-Control-Allow-Origin', headers)
        for path in ('/private.json', '/private/scope.json', '/assets/../private.json', '/%2e%2e/private.json'):
            status, _, raw = self.request(path)
            self.assertEqual(status, 404)
            self.assertNotIn(b'SYNTHETIC_PRIVATE_FILE', raw)

    def test_session_bootstrap_and_base_reader_routes_still_work(self):
        status, headers, raw = self.request('/api/session', authenticated=False,
                                             headers={'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['mode'], 'live')
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertEqual(self.request('/api/conversations')[0], 200)
        self.assertEqual(self.request('/api/status')[0], 200)
        self.assertEqual(self.manager.calls, [('reader_conversations',), ('reader_status',)])

    def test_setup_gets_require_cookie_and_fixed_query_shape(self):
        self.assertEqual(self.request('/api/setup', authenticated=False)[0], 401)
        self.assertEqual(self.request('/api/setup/conversations', authenticated=False)[0], 401)
        self.assertEqual(self.manager.calls, [])
        status, _, raw = self.request('/api/setup')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['stage'], 'ready_to_select')
        query = urlencode({'q': "合成%' OR 1=1 --"})
        self.assertEqual(self.request('/api/setup/conversations?' + query)[0], 200)
        self.assertEqual(self.manager.calls[-1], ('list', "合成%' OR 1=1 --"))
        for query in ('q=a&q=b', 'private_root=secret', urlencode({'q': '长' * 257}), 'q=%00', 'a=1&b=2&c=3'):
            self.assertEqual(self.request('/api/setup/conversations?' + query)[0], 400)
        self.assertEqual(self.request('/api/setup?unexpected=1')[0], 400)
        self.assertEqual(len(self.manager.calls), 2)

    def test_mutations_use_authenticated_same_origin_enqueue_contract(self):
        status, _, raw = self.request('/api/setup/initialize', method='POST', body=b'{}')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)['accepted'])
        payload = json.dumps({'private_id': 'synthetic-private', 'group_id': 'synthetic-group'}).encode()
        status, _, raw = self.request('/api/setup/select', method='POST', body=payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['stage'], 'starting')
        self.assertEqual(self.manager.calls, [('enqueue_initialize',),
                                             ('enqueue_selection', 'synthetic-private', 'synthetic-group')])

    def test_cross_origin_host_and_missing_post_origin_rejected_before_manager(self):
        for headers, origin in [({'Origin': 'https://evil.example'}, True), ({}, False),
                                ({'Origin': 'null'}, True), ({'Host': 'evil.example'}, True),
                                ({'Sec-Fetch-Site': 'cross-site'}, True)]:
            self.assertEqual(self.request('/api/setup/initialize', method='POST', body=b'{}', origin=origin,
                                          headers=headers)[0], 403)
        self.assertEqual(self.request('/api/setup/initialize', method='POST', body=b'{}', authenticated=False)[0], 401)
        self.assertEqual(self.request('/api/setup', headers={'Origin': 'https://evil.example'})[0], 403)
        self.assertEqual(self.manager.calls, [])

    def test_json_body_duplicates_unknown_fields_bad_encoding_and_size_rejected(self):
        for path, body in [('/api/setup/initialize', b'{"unexpected":true}'), ('/api/setup/initialize', b'[]'),
                           ('/api/setup/initialize', b'{"x":1,"x":2}'), ('/api/setup/initialize', b'\xff\xff'),
                           ('/api/setup/initialize', b'NaN'), ('/api/setup/initialize', b'x' * 4097),
                           ('/api/setup/select', b'{"private_id":"a","group_id":"b","private_id":"c"}'),
                           ('/api/setup/select', b'{"private_id":"a"}'),
                           ('/api/setup/select', b'{"private_id":true,"group_id":"b"}'),
                           ('/api/setup/select', b'{"private_id":"","group_id":"b"}'),
                           ('/api/setup/select', json.dumps({'private_id': 'x' * 257, 'group_id': 'b'}).encode())]:
            with self.subTest(path=path, length=len(body)):
                self.assertEqual(self.request(path, method='POST', body=body)[0], 400)
        self.assertEqual(self.request('/api/setup/initialize?unexpected=1', method='POST', body=b'{}')[0], 400)
        self.assertEqual(self.request('/api/setup/initialize', method='POST', body=b'{}',
                                      headers={'Content-Type': 'text/plain'})[0], 400)
        self.assertEqual(self.manager.calls, [])

    def test_duplicate_lengths_content_types_and_transfer_encoding_are_rejected(self):
        fixed = ['POST /api/setup/initialize HTTP/1.1', 'Host: ' + self.server.expected_host,
                 'Origin: ' + self.server.base_url, 'Cookie: ' + COOKIE + '=' + TOKEN]
        for extra in (['Content-Type: application/json', 'Content-Length: 2', 'Content-Length: 2'],
                      ['Content-Type: application/json', 'Content-Type: application/json', 'Content-Length: 2'],
                      ['Content-Type: application/json', 'Content-Length: 2', 'Transfer-Encoding: chunked'],
                      ['Content-Type: application/json'],
                      ['Content-Type: application/json', 'Content-Length: -1']):
            reply = self.raw(fixed + extra, b'{}')
            self.assertIn(b' 400 ', reply.split(b'\r\n', 1)[0])
        self.assertEqual(self.manager.calls, [])

    def test_failures_do_not_echo_manager_exception_detail_or_code(self):
        class PrivateError(Exception):
            code = 'synthetic_private_account_path_token'
        self.manager.failure = PrivateError('SYNTHETIC_PRIVATE_ACCOUNT_PATH_KEY')
        logs = io.StringIO()
        with contextlib.redirect_stdout(logs), contextlib.redirect_stderr(logs):
            for path, method, body in [('/api/setup', 'GET', None), ('/api/setup/initialize', 'POST', b'{}')]:
                status, headers, raw = self.request(path, method=method, body=body)
                self.assertEqual(status, 503)
                self.assertEqual(json.loads(raw), {'error': {'code': 'macos_setup_operation_failed'}})
                self.assertNotIn(TOKEN.encode(), raw)
                self.assertNotIn(b'SYNTHETIC_PRIVATE', raw)
                self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(logs.getvalue(), '')

    def test_unlisted_mutations_remain_disabled_and_static_symlinks_refused(self):
        self.assertEqual(self.request('/api/status', method='POST', body=b'{}')[0], 405)
        self.assertEqual(self.request('/api/setup/initialize', method='PUT', body=b'{}')[0], 405)
        self.assertEqual(self.request('/api/setup/select', method='OPTIONS')[0], 405)
        path = self.static / 'connect.js'
        path.unlink()
        path.symlink_to(self.static / 'private.json')
        self.assertEqual(self.request('/connect.js')[0], 404)
        self.assertEqual(self.manager.calls, [])


if __name__ == '__main__':
    unittest.main()
