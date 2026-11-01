"""Synthetic HTTP contract/security checks. No real chat, credentials or keys."""
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

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from web_data import WebDataError
from web_server import COOKIE, CSP, make_server

TOKEN = 'SYNTHETIC_SESSION_ONLY_' + 'x' * 32


class SyntheticData:
    def __init__(self):
        self.calls = []
        self.failure = None

    def conversations(self):
        self.calls.append(('conversations',))
        if self.failure:
            raise self.failure
        return {'conversations': [{'id': 'private_test', 'name': '合成私聊', 'type': 'private',
                                  'count': 0, 'latest_id': None, 'latest_at': None, 'latest_preview': ''}]}

    def status(self):
        self.calls.append(('status',))
        return {'state': 'stopped', 'reason': None, 'total_messages': 0}

    def messages(self, conversation, **parameters):
        self.calls.append(('messages', conversation, parameters))
        return {'messages': [], 'has_more': False, 'before_cursor': None, 'after_cursor': None}


class WebServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-web-server-')
        self.root = Path(self.tmp.name).resolve()
        self.static = self.root / 'static'
        self.static.mkdir()
        (self.static / 'assets').mkdir()
        for name, contents in [('index.html', '<!doctype html><title>Synthetic only</title>'),
                               ('app.js', '// synthetic only'), ('styles.css', 'body{}'),
                               ('assets/chatlens-mark.svg', '<svg xmlns="http://www.w3.org/2000/svg"/>')]:
            (self.static / name).write_text(contents, encoding='utf-8')
        (self.static / 'config.json').write_text('SYNTHETIC_MUST_NOT_BE_PUBLIC', encoding='utf-8')
        self.data = SyntheticData()
        self.server = make_server(self.data, self.static, token=TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.cookie = COOKIE + '=' + TOKEN

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self, path='/', method='GET', headers=None, authenticated=False):
        current = dict(headers or {})
        if authenticated:
            current.setdefault('Cookie', self.cookie)
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, headers=current)
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            connection.close()

    def raw_request(self, lines):
        with socket.create_connection(self.server.server_address, timeout=3) as source:
            source.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
            result = b''
            while True:
                chunk = source.recv(65536)
                if not chunk:
                    return result
                result += chunk

    def test_server_binds_only_loopback_and_bootstrap_uses_fragment(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        self.assertEqual(self.server.launch_url, self.server.base_url + '/#token=' + TOKEN)
        status, headers, raw = self.request('/api/session', headers={'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)['mode'], 'live')
        self.assertTrue(json.loads(raw)['capabilities']['read_messages'])
        self.assertFalse(json.loads(raw)['capabilities']['control_collection'])
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        self.assertIn('Path=/', headers['Set-Cookie'])
        self.assertNotIn(TOKEN.encode(), raw)
        self.assertEqual(self.request('/api/session', authenticated=True)[0], 200)

    def test_cookie_required_for_data_bearer_only_allowed_on_session(self):
        for path in ('/api/session', '/api/conversations', '/api/messages?conversation=private_test', '/api/status'):
            self.assertEqual(self.request(path)[0], 401)
        self.assertEqual(self.data.calls, [])
        self.assertEqual(self.request('/api/conversations', headers={'Authorization': 'Bearer ' + TOKEN})[0], 401)
        self.assertEqual(self.request('/api/conversations', authenticated=True)[0], 200)
        self.assertEqual(self.data.calls, [('conversations',)])

    def test_bad_or_duplicate_credentials_never_authorize_or_echo(self):
        for headers in ({'Cookie': COOKIE + '=wrong-secret'}, {'Cookie': 'broken;'},
                        {'Cookie': COOKIE + '=\xff'}, {'Authorization': 'Bearer wrong-secret'},
                        {'Authorization': 'Bearer \xff'}):
            with self.subTest(headers=repr(headers)):
                status, response_headers, raw = self.request('/api/session', headers=headers)
                self.assertEqual(status, 401)
                self.assertNotIn('Set-Cookie', response_headers)
                self.assertNotIn(b'wrong-secret', raw)
        reply = self.raw_request(['GET /api/session HTTP/1.1', 'Host: ' + self.server.expected_host,
                                  'Authorization: Bearer ' + TOKEN, 'Authorization: Bearer ' + TOKEN])
        self.assertIn(b' 401 ', reply.split(b'\r\n', 1)[0])

    def test_exact_host_and_same_origin_requirements_prevent_rebinding(self):
        rejected = [{'Host': 'evil.example'}, {'Host': 'localhost:%d' % self.server.server_address[1]},
                    {'Host': self.server.expected_host + '.evil.example'}, {'Origin': 'https://evil.example'},
                    {'Origin': 'null'}, {'Origin': self.server.base_url + '/'},
                    {'Sec-Fetch-Site': 'cross-site'}, {'Sec-Fetch-Site': 'same-site'}]
        for headers in rejected:
            with self.subTest(headers=headers):
                self.assertEqual(self.request('/api/conversations', headers=headers, authenticated=True)[0], 403)
        self.assertEqual(self.data.calls, [])
        headers = {'Origin': self.server.base_url, 'Sec-Fetch-Site': 'same-origin'}
        self.assertEqual(self.request('/api/status', headers=headers, authenticated=True)[0], 200)
        for duplicate in ('Host', 'Origin'):
            lines = ['GET /api/status HTTP/1.1', 'Host: ' + self.server.expected_host, 'Cookie: ' + self.cookie]
            if duplicate == 'Host':
                lines.append('Host: ' + self.server.expected_host)
            else:
                lines += ['Origin: ' + self.server.base_url, 'Origin: ' + self.server.base_url]
            self.assertIn(b' 403 ', self.raw_request(lines).split(b'\r\n', 1)[0])

    def test_all_responses_have_private_cache_and_content_security_headers(self):
        for path, authenticated in [('/', False), ('/api/session', False), ('/api/status', True), ('/missing', False)]:
            _, headers, _ = self.request(path, authenticated=authenticated)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
            self.assertEqual(headers['Content-Security-Policy'], CSP)
            self.assertNotIn('Access-Control-Allow-Origin', headers)

    def test_static_whitelist_without_directory_private_or_encoded_traversal_access(self):
        for path in ('/', '/index.html', '/app.js', '/styles.css', '/assets/chatlens-mark.svg'):
            self.assertEqual(self.request(path)[0], 200)
        for path in ('/config.json', '/private/catalog.json', '/assets/', '/../../config.json',
                     '/%2e%2e/config.json', '/assets/%2e%2e/config.json', '/.git/config', '/api/../config.json'):
            with self.subTest(path=path):
                status, _, raw = self.request(path, authenticated=True)
                self.assertEqual(status, 404)
                self.assertNotIn(b'SYNTHETIC_MUST_NOT_BE_PUBLIC', raw)
                self.assertNotIn(str(self.root).encode(), raw)
        (self.static / 'app.js').unlink()
        (self.static / 'app.js').symlink_to(self.static / 'config.json')
        self.assertEqual(self.request('/app.js')[0], 404)

    def test_mutation_methods_are_disabled_without_invoking_data(self):
        for method in ('POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD'):
            with self.subTest(method=method):
                self.assertEqual(self.request('/api/status', method=method, authenticated=True)[0], 405)
        self.assertEqual(self.data.calls, [])

    def test_message_query_parse_exact_contract_and_parameter_rejection(self):
        query = urlencode({'conversation': 'private_test', 'after': 0, 'limit': 25, 'q': '合成%_needle'})
        self.assertEqual(self.request('/api/messages?' + query, authenticated=True)[0], 200)
        self.assertEqual(self.data.calls, [('messages', 'private_test',
                                           {'before': None, 'after': 0, 'limit': 25, 'q': '合成%_needle'})])
        for suffix in ('conversation=private_test&conversation=group_test', 'conversation=private_test&after=NaN',
                       'conversation=private_test&before=-1', 'conversation=private_test&private_root=secret',
                       'conversation=private_test&limit=' + '9' * 20, '&'.join('q%d=x' % n for n in range(9))):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.request('/api/messages?' + suffix, authenticated=True)[0], 400)
        self.assertEqual(self.request('/api/status?anything=1', authenticated=True)[0], 400)
        self.assertEqual(self.request('/api/unknown', authenticated=True)[0], 404)
        self.assertEqual(len(self.data.calls), 1)

    def test_fixed_data_errors_hide_private_exception_values(self):
        self.data.failure = WebDataError('web_store_unavailable')
        status, _, raw = self.request('/api/conversations', authenticated=True)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)['error']['code'], 'web_store_unavailable')
        self.data.failure = RuntimeError('SYNTHETIC_PRIVATE_ROOT_AND_KEY')
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
            status, _, raw = self.request('/api/conversations', authenticated=True)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw)['error']['code'], 'web_data_unavailable')
        self.assertNotIn('SYNTHETIC_PRIVATE', stream.getvalue())
        self.assertNotIn(b'SYNTHETIC_PRIVATE', raw)

    def test_absolute_or_malformed_targets_rejected_and_url_secrets_not_logged(self):
        for path in ('http://127.0.0.1/api/status', 'http://[bad/', '/api/messages?' + 'x' * 4100):
            status, _, raw = self.request(path, headers={'Host': self.server.expected_host}, authenticated=True)
            self.assertIn(status, (400, 414))
            self.assertNotIn(TOKEN.encode(), raw)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            self.request('/private/SYNTHETIC_SECRET?token=' + TOKEN)
        self.assertEqual(captured.getvalue(), '')

    def test_factory_rejects_publicish_ports_and_bad_token(self):
        for port in (-1, 65536, True, '8787'):
            with self.assertRaisesRegex(ValueError, 'web_port_invalid'):
                make_server(self.data, self.static, port=port)
        for token in ('short', 'x' * 129, 'a b' * 20, '\xff' * 32):
            with self.assertRaisesRegex(ValueError, 'web_session_token_invalid'):
                make_server(self.data, self.static, token=token)


if __name__ == '__main__':
    unittest.main()
