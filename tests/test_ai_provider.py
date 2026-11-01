"""Synthetic credentials and local fake HTTP only; never call a cloud model."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import socket
import ssl
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import ai_provider as provider

KEY = 'SYNTHETIC_API_KEY_NOT_A_REAL_CREDENTIAL'
MESSAGES = [{'role': 'system', 'content': '合成系统提示'}, {'role': 'user', 'content': '合成问题'}]


def answer(**extra):
    value = {'choices': [{'message': {'content': '合成回答', 'reasoning_content': 'SYNTHETIC_REASONING_NEVER_RETURN'},
                          'finish_reason': 'stop'}], 'model': 'SYNTHETIC_UNTRUSTED_UPSTREAM_MODEL',
             'usage': {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13,
                       'untrusted_secret_field': KEY}}
    value.update(extra)
    return value


@contextmanager
def fake_server(*, document=None, raw=None, status=200, headers=None, delay=None, omit_length=False):
    record = {'requests': [], 'received': threading.Event(), 'release': threading.Event()}
    payload = raw if raw is not None else json.dumps(answer() if document is None else document).encode()
    class Server(ThreadingHTTPServer):
        daemon_threads = True
        block_on_close = False
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            record['requests'].append({'path': self.path, 'headers': dict(self.headers), 'body': json.loads(body)})
            if delay == 'headers':
                record['received'].set(); record['release'].wait(3)
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            if not omit_length:
                self.send_header('Content-Length', str(len(payload)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if delay == 'body':
                self.wfile.flush(); record['received'].set(); record['release'].wait(3)
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
    server = Server(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    worker.start()
    record['base_url'] = 'http://127.0.0.1:%d/v1' % server.server_address[1]
    record['address'] = server.server_address
    try:
        yield record
    finally:
        record['release'].set()
        server.shutdown(); worker.join(timeout=2); server.server_close()


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-ai-provider-')
        self.directory = Path(self.tmp.name).resolve() / 'assistant'
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / provider.CONFIG_NAME

    def tearDown(self):
        self.tmp.cleanup()

    def configure(self, base_url='https://provider.example/v1', model='synthetic-model', **options):
        return provider.update_config(self.directory, base_url=base_url, model=model, **options)

    def config(self, base_url, model='synthetic-model', key=KEY):
        return provider.ProviderConfig(base_url, model, key, 'a' * 32)

    def assert_error(self, code, function, *arguments, **options):
        with self.assertRaises(provider.ProviderError) as failure:
            function(*arguments, **options)
        self.assertEqual(failure.exception.code, code)
        self.assertEqual(str(failure.exception), code)
        self.assertNotIn(KEY, repr(failure.exception))
        self.assertNotIn(str(self.directory), repr(failure.exception))

    def test_default_is_empty_and_config_operations_never_connect(self):
        with mock.patch.object(socket, 'socket', side_effect=AssertionError('configuration must not connect')):
            config = provider.load_config(self.directory)
            public = provider.public_config(self.directory)
            self.assertEqual(public, dict(base_url='', model='', has_key=False, configured=False,
                                          target_fingerprint=None, is_local=False))
            self.assertFalse(self.path.exists())
            self.assertEqual(config.get('api_key'), '')
            self.configure(model='MiniMax-M3.1-Flash')
            self.assertFalse(provider.public_config(self.directory)['configured'])

    def test_private_key_is_never_in_public_view_or_config_repr(self):
        result = self.configure(key_action='replace', api_key=KEY)
        config = provider.load_config(self.directory)
        self.assertTrue(result['configured'])
        self.assertTrue(result['has_key'])
        self.assertFalse(result['is_local'])
        self.assertEqual(config['api_key'], KEY)
        self.assertNotIn(KEY, repr(config))
        self.assertNotIn(config.credential_revision, repr(config))
        public = json.dumps(result)
        self.assertNotIn(KEY, public)
        self.assertNotIn(KEY[-5:], public)
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(self.path.stat().st_uid, os.geteuid())

    def test_keep_replace_delete_rotate_confirmation_when_credentials_change(self):
        first = self.configure(key_action='replace', api_key=KEY)
        kept = self.configure(key_action='keep', api_key='')
        self.assertEqual(first['target_fingerprint'], kept['target_fingerprint'])
        self.assertEqual(provider.load_config(self.directory).api_key, KEY)
        replaced = self.configure(key_action='replace', api_key=KEY)
        self.assertNotEqual(first['target_fingerprint'], replaced['target_fingerprint'])
        deleted = self.configure(key_action='delete')
        self.assertNotEqual(replaced['target_fingerprint'], deleted['target_fingerprint'])
        self.assertFalse(deleted['has_key'])
        self.assertFalse(deleted['configured'])
        self.assertEqual(provider.load_config(self.directory).api_key, '')

    def test_changed_api_address_clears_old_key_and_fingerprint(self):
        first = self.configure(key_action='replace', api_key=KEY)
        result = self.configure('https://other.example/v1', 'synthetic-model')
        self.assertFalse(result['has_key'])
        self.assertFalse(result['configured'])
        self.assertNotEqual(result['target_fingerprint'], first['target_fingerprint'])
        self.assertEqual(provider.load_config(self.directory).api_key, '')
        rebound = self.configure('https://new.example/v1', 'new-model', key_action='replace', api_key=KEY)
        self.assertTrue(rebound['configured'])

    def test_same_api_model_switch_preserves_key_but_invalidates_old_confirmation(self):
        first = self.configure(key_action='replace', api_key=KEY)
        first_revision = provider.load_config(self.directory).credential_revision
        result = self.configure(model='different-model', key_action='keep', api_key='')
        current = provider.load_config(self.directory)
        self.assertTrue(result['has_key'])
        self.assertTrue(result['configured'])
        self.assertEqual(current.api_key, KEY)
        self.assertEqual(current.base_url, 'https://provider.example/v1')
        self.assertEqual(current.model, 'different-model')
        self.assertNotEqual(current.credential_revision, first_revision)
        self.assertNotEqual(result['target_fingerprint'], first['target_fingerprint'])

    def test_canonical_target_does_not_change_on_case_default_port_or_trailing_slash(self):
        first = self.configure('https://PROVIDER.example:443/v1/', key_action='replace', api_key=KEY)
        second = self.configure('https://provider.example/v1')
        self.assertEqual(first['base_url'], 'https://provider.example/v1')
        self.assertEqual(first['target_fingerprint'], second['target_fingerprint'])
        self.assertTrue(second['has_key'])

    def test_key_actions_are_explicit_and_invalid_updates_preserve_old_file(self):
        self.configure(key_action='replace', api_key=KEY)
        before = self.path.read_bytes()
        cases = [dict(key_action='replace', api_key=''), dict(key_action='replace', api_key=None),
                 dict(key_action='keep', api_key=KEY), dict(key_action='delete', api_key=KEY),
                 dict(key_action='unknown'), dict(key_action='replace', api_key='bad\r\nInjected: value')]
        for options in cases:
            with self.subTest(action=options['key_action']):
                with self.assertRaises(provider.ProviderError):
                    self.configure(**options)
                self.assertEqual(self.path.read_bytes(), before)

    def test_unsafe_urls_and_models_cannot_save_or_connect(self):
        urls = ['http://example.com/v1', 'http://localhost/v1', 'http://192.168.1.2/v1',
                'http://0.0.0.0/v1', 'https://user:pass@example.com/v1',
                'https://example.com/v1?api_key=secret', 'https://example.com/v1#secret',
                'https://example.com/v1?', 'https://example.com\\@evil.example/v1',
                'https://example.com/../v1', 'https://example.com/%2e%2e/v1',
                'file:///tmp/private', 'https://example.com:99999/v1', 'https://example.com/\nvalue',
                'https://%65xample.com/v1']
        for url in urls:
            with self.subTest(kind=urls.index(url)):
                self.assert_error('ai_provider_url_invalid', self.configure, url)
        for model in ('bad model', '\ud800', 'x' * 257):
            self.assert_error('ai_provider_model_invalid', self.configure, model=model)
        self.assert_error('ai_provider_configuration_invalid', self.configure, None)
        self.assertFalse(self.path.exists())

    def test_only_explicit_loopback_http_is_local_and_does_not_require_key(self):
        result = self.configure('http://127.0.0.1:12345/v1')
        self.assertTrue(result['configured'])
        self.assertTrue(result['is_local'])
        self.assertFalse(result['has_key'])
        self.assertTrue(provider._url('http://[::1]:12345/v1')[1])
        self.assertTrue(provider._url('https://127.0.0.1/v1')[1])

    @unittest.skipIf(os.name == 'nt', 'POSIX private permissions and links')
    def test_loose_permissions_symlink_or_hardlink_config_are_rejected_without_repair(self):
        self.configure(key_action='replace', api_key=KEY)
        self.path.chmod(0o644)
        self.assert_error('ai_provider_configuration_unavailable', provider.load_config, self.directory)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o644)
        self.path.chmod(0o600)
        link = self.directory / 'synthetic-hardlink'
        os.link(self.path, link)
        self.assert_error('ai_provider_configuration_unavailable', provider.load_config, self.directory)
        link.unlink()
        moved = self.directory / 'synthetic-original.json'
        self.path.rename(moved); self.path.symlink_to(moved)
        self.assert_error('ai_provider_configuration_unavailable', provider.load_config, self.directory)

    def test_generic_chat_sends_bounded_text_and_returns_only_final_answer_requested_model_and_usage(self):
        with fake_server() as server:
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = provider.chat(MESSAGES, self.config(server['base_url']))
            request = server['requests'][0]
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['headers']['Authorization'], 'Bearer ' + KEY)
        self.assertEqual(request['headers']['Content-Type'], 'application/json')
        self.assertEqual(request['body'], dict(model='synthetic-model', messages=MESSAGES, stream=False, max_tokens=2048))
        self.assertEqual(result, dict(text='合成回答', model='synthetic-model',
                                      usage=dict(prompt_tokens=10, completion_tokens=3, total_tokens=13)))
        self.assertNotIn('SYNTHETIC_REASONING', json.dumps(result))
        self.assertNotIn(KEY, json.dumps(result))
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual(stderr.getvalue(), '')

    def test_local_without_key_omits_authorization_and_ignores_proxy_environment_without_changes(self):
        environment = dict(os.environ)
        with fake_server() as server, mock.patch.dict(os.environ, {'HTTP_PROXY': 'http://127.0.0.1:1',
                                                                  'HTTPS_PROXY': 'http://127.0.0.1:1', 'ALL_PROXY': 'http://127.0.0.1:1'}):
            configured_environment = dict(os.environ)
            result = provider.chat(MESSAGES, self.config(server['base_url'], key=''))
            self.assertEqual(os.environ, configured_environment)
            self.assertNotIn('Authorization', server['requests'][0]['headers'])
            self.assertEqual(result['text'], '合成回答')
        self.assertEqual(os.environ, environment)

    def test_minimax_presets_and_parameters_match_verified_official_compatible_protocol(self):
        self.assertEqual([row['base_url'] for row in provider.MINIMAX_PRESETS],
                         ['https://api.minimax.cn/v1', 'https://api.minimax.io/v1'])
        for row in provider.MINIMAX_PRESETS:
            raw = provider._payload(self.config(row['base_url'], row['model']), MESSAGES, 512)
            body = json.loads(raw)
            self.assertEqual(body['max_completion_tokens'], 512)
            self.assertTrue(body['reasoning_split'])
            self.assertEqual(body['reasoning_effort'], 'low')
            self.assertNotIn('max_tokens', body)
            self.assertNotIn('service_tier', body)
            self.assertNotIn('temperature', body)
        body = json.loads(provider._payload(self.config('https://api.minimax.cn/v1', 'MiniMax-M3'), MESSAGES, 512))
        self.assertEqual(body['thinking'], {'type': 'disabled'})

    def test_connection_uses_only_a_fixed_non_chat_prompt_and_low_output_limit(self):
        with fake_server() as server:
            result = provider.test_connection(self.config(server['base_url']))
            request = server['requests'][0]['body']
        self.assertEqual(request['messages'], [{'role': 'user', 'content': 'This is a connection test. Reply only OK.'}])
        self.assertEqual(request['max_tokens'], 512)
        self.assertEqual(result['model'], 'synthetic-model')

    def test_invalid_or_oversized_context_and_unconfigured_remote_fail_before_any_network(self):
        cases = [[], MESSAGES * 33, [{'role': 'developer', 'content': 'x'}],
                 [{'role': 'user', 'content': ['unsupported multimodal']}],
                 [{'role': 'user', 'content': 'x', 'tool_calls': []}],
                 [{'role': 'user', 'content': 'x' * (provider.MAX_MESSAGE_BYTES + 1)}],
                 [{'role': 'user', 'content': 'x' * provider.MAX_MESSAGE_BYTES}] * 5,
                 [{'role': 'user', 'content': '\ud800'}]]
        with mock.patch.object(provider, '_addresses', side_effect=AssertionError('must not connect')):
            for messages in cases:
                with self.subTest(case=cases.index(messages)):
                    self.assert_error('ai_provider_messages_invalid', provider.chat, messages,
                                      self.config('http://127.0.0.1:12345/v1'))
            self.assert_error('ai_provider_not_configured', provider.chat, MESSAGES,
                              self.config('https://provider.example/v1', key=''))

    def test_redirect_is_rejected_and_key_is_never_forwarded_to_another_server(self):
        with fake_server() as destination, fake_server(status=307, headers={'Location': destination['base_url'] + '/chat/completions'}) as source:
            self.assert_error('ai_provider_redirect_refused', provider.chat, MESSAGES, self.config(source['base_url']))
            self.assertEqual(len(source['requests']), 1)
            self.assertEqual(destination['requests'], [])

    def test_upstream_errors_are_fixed_codes_without_raw_body_key_or_model(self):
        codes = [(401, 'ai_provider_authentication_failed'), (403, 'ai_provider_authentication_failed'),
                 (429, 'ai_provider_rate_limited'), (500, 'ai_provider_upstream_failed')]
        for status, code in codes:
            with self.subTest(status=status), fake_server(status=status, raw=KEY.encode()) as server:
                self.assert_error(code, provider.chat, MESSAGES, self.config(server['base_url']))
                self.assertEqual(len(server['requests']), 1)

    def test_invalid_responses_reasoning_only_and_declared_error_are_never_returned_as_answers(self):
        raw_values = [b'not-json:' + KEY.encode(), b'{"choices":[],"choices":[]}',
                      json.dumps({'choices': [{'message': {'content': None, 'reasoning_content': KEY}}]}).encode(),
                      json.dumps({'base_resp': {'status_code': 1000, 'status_msg': KEY}, **answer()}).encode()]
        for raw in raw_values:
            with fake_server(raw=raw) as server:
                self.assert_error('ai_provider_response_invalid', provider.chat, MESSAGES, self.config(server['base_url']))
        limited = answer(); limited['choices'][0]['finish_reason'] = 'length'
        with fake_server(document=limited) as server:
            self.assert_error('ai_provider_output_limit', provider.chat, MESSAGES, self.config(server['base_url']))

    def test_response_limit_applies_even_without_content_length_and_encoded_body_is_rejected(self):
        with fake_server(raw=b'x' * (provider.MAX_BODY_BYTES + 2), omit_length=True) as server:
            self.assert_error('ai_provider_response_too_large', provider.chat, MESSAGES, self.config(server['base_url']))
        with fake_server(raw=b'x' * (provider.MAX_BODY_BYTES + 2)) as server:
            self.assert_error('ai_provider_response_too_large', provider.chat, MESSAGES, self.config(server['base_url']))
        with fake_server(headers={'Content-Encoding': 'gzip'}) as server:
            self.assert_error('ai_provider_response_encoding_invalid', provider.chat, MESSAGES, self.config(server['base_url']))

    def test_response_cannot_echo_own_key_or_untrusted_usage_fields(self):
        document = answer()
        document['choices'][0]['message']['content'] = 'Echo: ' + KEY
        document['usage'] = {'prompt_tokens': True, 'completion_tokens': -1, 'total_tokens': KEY, 'raw_secret': KEY}
        with fake_server(document=document) as server:
            result = provider.chat(MESSAGES, self.config(server['base_url']))
        self.assertEqual(result['text'], 'Echo: [凭据已隐藏]')
        self.assertEqual(result['usage'], {})
        self.assertNotIn(KEY, json.dumps(result))

    def test_cancellation_interrupts_delayed_headers_and_delayed_body_on_own_socket(self):
        for stage in ('headers', 'body'):
            with self.subTest(stage=stage), fake_server(delay=stage) as server:
                cancelled, finished, failures = threading.Event(), threading.Event(), []
                def work():
                    try:
                        provider.chat(MESSAGES, self.config(server['base_url']), cancelled)
                    except provider.ProviderError as error:
                        failures.append(error.code)
                    finally:
                        finished.set()
                worker = threading.Thread(target=work)
                worker.start()
                self.assertTrue(server['received'].wait(2))
                started = time.monotonic(); cancelled.set()
                self.assertTrue(finished.wait(1.5))
                worker.join(timeout=.2)
                self.assertLess(time.monotonic() - started, 1.5)
                self.assertEqual(failures, ['ai_request_cancelled'])
                self.assertFalse(worker.is_alive())

    def test_cancelled_before_request_does_not_open_any_connection(self):
        cancelled = threading.Event(); cancelled.set()
        with mock.patch.object(provider, '_addresses', side_effect=AssertionError('cancelled request cannot resolve')):
            self.assert_error('ai_request_cancelled', provider.chat, MESSAGES,
                              self.config('http://127.0.0.1:12345/v1'), cancelled)

    def test_one_total_timeout_budget_stops_a_delayed_response(self):
        with fake_server(delay='headers') as server, mock.patch.object(provider, 'REQUEST_TIMEOUT', .15):
            started = time.monotonic()
            self.assert_error('ai_provider_timeout', provider.chat, MESSAGES, self.config(server['base_url']))
            self.assertLess(time.monotonic() - started, 1)

    def test_cancelled_dns_resolution_never_later_sends_credentials_or_body(self):
        entered, release, resolver_done, request_done = (threading.Event() for _ in range(4))
        cancel, failures = threading.Event(), []
        with fake_server() as server:
            def resolving(*_, **__):
                entered.set()
                try:
                    release.wait(2)
                    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', server['address'])]
                finally:
                    resolver_done.set()
            def request():
                try:
                    provider.chat(MESSAGES, self.config('https://provider.invalid/v1'), cancel)
                except provider.ProviderError as error:
                    failures.append(error.code)
                finally:
                    request_done.set()
            with mock.patch.object(provider.socket, 'getaddrinfo', side_effect=resolving):
                worker = threading.Thread(target=request)
                worker.start()
                self.assertTrue(entered.wait(1))
                cancel.set()
                self.assertTrue(request_done.wait(1))
                self.assertEqual(failures, ['ai_request_cancelled'])
                release.set()
                self.assertTrue(resolver_done.wait(1))
                worker.join(timeout=1)
            self.assertEqual(server['requests'], [])

    def test_cancel_watcher_only_closes_this_request_socket(self):
        unrelated_left, unrelated_right = socket.socketpair()
        try:
            with fake_server(delay='headers') as server:
                cancel, done, errors = threading.Event(), threading.Event(), []
                def request():
                    try:
                        provider.chat(MESSAGES, self.config(server['base_url']), cancel)
                    except provider.ProviderError as error:
                        errors.append(error.code)
                    finally:
                        done.set()
                worker = threading.Thread(target=request)
                worker.start()
                self.assertTrue(server['received'].wait(1))
                cancel.set(); self.assertTrue(done.wait(1)); worker.join(timeout=1)
                self.assertEqual(errors, ['ai_request_cancelled'])
                unrelated_left.sendall(b'SYNTHETIC_UNRELATED_CONNECTION_INTACT')
                self.assertEqual(unrelated_right.recv(128), b'SYNTHETIC_UNRELATED_CONNECTION_INTACT')
        finally:
            unrelated_left.close(); unrelated_right.close()

    def test_watcher_thread_launch_failure_is_fixed_error_without_request_or_secret(self):
        with fake_server() as server, mock.patch.object(provider.threading.Thread, 'start', side_effect=RuntimeError(KEY)):
            self.assert_error('ai_provider_network_failed', provider.chat, MESSAGES, self.config(server['base_url']))
            self.assertEqual(server['requests'], [])

    def test_tls_verification_uses_default_context_and_does_not_send_body_on_certificate_error(self):
        context = ssl.create_default_context()
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        with fake_server() as server, mock.patch.object(provider, '_addresses', return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', server['address'])]), \
                mock.patch.object(provider.ssl, 'create_default_context', return_value=context), \
                mock.patch.object(context, 'wrap_socket', side_effect=ssl.SSLCertVerificationError(KEY)):
            self.assert_error('ai_provider_tls_verification_failed', provider.chat, MESSAGES,
                              self.config('https://provider.invalid/v1'))
            self.assertEqual(server['requests'], [])


if __name__ == '__main__':
    unittest.main()
