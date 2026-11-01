"""Actual local AI/collector HTTP stack with an explicitly synthetic provider.

Everything is created under a new temporary private directory. This harness
never reads WeChat, existing private material, API credentials or cloud APIs.
The launch fragment travels only through parent-process IPC, never evidence.
"""
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts' / 'windows-proof')]
from ai_service import AiAssistant
from ai_web_server import attach_ai_routes
from collector_private import ensure_private_dir, write_private_json_new
from continuous_store import CollectorStore
from web_data import CollectorWebData
from web_server import make_server


def now():
    return datetime.now(timezone.utc).isoformat()


class SyntheticProvider(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


def install_loopback_guard():
    """Fail before DNS/connect for every non-loopback fixture request.

    Saving a synthetic cloud target exercises the UI without granting this
    process a way to contact it, even if a regression submits unexpectedly.
    The audit hook remains confined to this short-lived fixture subprocess.
    """
    state = {'loopback_only': True, 'blocked_attempts': 0, 'self_test_passed': False}

    def audit(event, arguments):
        if event == 'socket.getaddrinfo':
            host = arguments[0]
        elif event == 'socket.connect':
            address = arguments[1]
            host = address[0] if isinstance(address, tuple) and len(address) >= 2 else None
        else:
            return
        try:
            permitted = ipaddress.ip_address(host).is_loopback
        except (ValueError, TypeError):
            permitted = False
        if not permitted:
            state['blocked_attempts'] += 1
            raise PermissionError('synthetic_fixture_external_network_blocked')

    sys.addaudithook(audit)
    # The documentation-only address must be rejected by the audit hook before
    # DNS or any socket connection; this does not send a network probe.
    try:
        socket.create_connection(('203.0.113.9', 443), timeout=.1)
    except PermissionError:
        state['self_test_passed'] = state['blocked_attempts'] == 1
    if not state['self_test_passed']:
        raise RuntimeError('synthetic_fixture_guard_failed')
    return state


def main():
    network_guard = install_loopback_guard()
    (ROOT / '.build').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='synthetic-ai-web-', dir=ROOT / '.build') as temporary:
        data = Path(temporary).resolve()
        ensure_private_dir(data)
        private = data / 'private'
        ensure_private_dir(private, create=True)
        directory = private / 'store'
        ensure_private_dir(directory, create=True)
        store = CollectorStore(directory, {'source': 'synthetic-ai-browser-fixture'})
        cursors = {'private_test': 0, 'group_test': 0}
        epochs = {'synthetic': 'one'}
        store.initialize(cursors, epochs)
        serial = 0
        stage = 'collecting'
        started = now()

        def status(value=None):
            nonlocal stage
            stage = value or stage
            pending = private / 'next-status.json'
            write_private_json_new(pending, {'schema_version': 1, 'stage': stage, 'reason': None,
                'recorded_at_utc': now(), 'started_at_utc': started, 'resumed_from_checkpoint': True})
            os.replace(pending, private / 'web-status.json')

        def commit(alias, body):
            nonlocal serial, cursors
            serial += 1
            after = dict(cursors)
            after[alias] += 1
            event = {'source_key': hashlib.sha256(('synthetic-ai-' + str(serial)).encode()).hexdigest(),
                'alias': alias, 'source_local_id': after[alias],
                'sent_at': int(datetime.now(timezone.utc).timestamp()), 'sender_id': 'synthetic-sender',
                'body': body, 'observed_at': now()}
            result = store.commit(cursors, after, [event], epochs)
            cursors = after
            status()
            return result['total_messages']

        write_private_json_new(private / 'catalog.json', {'schema_version': 1, 'conversations': [
            {'id': 'private_test', 'name': '合成客户·知秋', 'kind': 'private', 'sender_names': {'synthetic-sender': '合成客户'}},
            {'id': 'group_test', 'name': '合成订单测试群', 'kind': 'group', 'sender_names': {'synthetic-sender': '合成群成员'}}]})
        commit('private_test', '合成报价：客户询价 100 套，希望下周确认。<img src=x onerror=alert(1)> 仅为文字🙂')
        for index in range(2, 126):
            commit('private_test', '合成普通文字 ' + str(index))
        commit('group_test', '合成群内约定：交付时间待确认，付款前请先核对报价。')
        synthetic = {'mode': 'ok', 'calls': 0, 'test_calls': 0, 'question_calls': 0}

        class ProviderHandler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                request = json.loads(raw)
                synthetic['calls'] += 1
                content = request.get('messages', [{}])[-1].get('content', '')
                is_test = content == 'This is a connection test. Reply only OK.'
                synthetic['test_calls' if is_test else 'question_calls'] += 1
                mode = synthetic['mode']
                if mode == 'slow':
                    time.sleep(4)
                else:
                    time.sleep(.8)
                code = 401 if mode == 'auth' else 200
                if code != 200:
                    payload = {'error': {'message': 'synthetic authentication failure'}}
                else:
                    if is_test:
                        answer = 'OK'
                    elif mode == 'no_citations':
                        answer = '合成缺失引用回答，不应作为成功回答展示。'
                    elif mode == 'bad_citation':
                        answer = '合成无效引用回答 [E999]'
                    else:
                        context = json.loads(content)
                        evidence = context.get('evidence', {}).get('messages', [])
                        label = evidence[0].get('citation', 'E1') if evidence else 'E1'
                        answer = '这是本机合成供应商的测试回答。\n\n所选原文包含需要核对的沟通信息 [' + label + ']。\n这里只验证检索、API、引用和页面，不代表真实 MiniMax。'
                    payload = {'choices': [{'message': {'content': answer}}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}}
                encoded = json.dumps(payload, ensure_ascii=False).encode()
                try:
                    self.send_response(code)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        provider = SyntheticProvider(('127.0.0.1', 0), ProviderHandler)
        provider_thread = threading.Thread(target=provider.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        provider_thread.start()
        assistant = AiAssistant(data, private / 'assistant')
        server = attach_ai_routes(make_server(CollectorWebData(data), ROOT / 'apps' / 'web'), lambda: assistant)
        server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        server_thread.start()
        print(json.dumps({'ready': True, 'launch_url': server.launch_url,
                          'synthetic_provider_url': 'http://127.0.0.1:%d/v1' % provider.server_address[1],
                          'network_guard': network_guard}), flush=True)
        try:
            for line in sys.stdin:
                request = json.loads(line)
                command = request.get('command')
                if command == 'mode':
                    synthetic['mode'] = request['mode']
                elif command == 'commit':
                    commit(request.get('alias', 'private_test'), request['body'])
                elif command == 'status':
                    status(request['stage'])
                elif command == 'heartbeat':
                    status()
                elif command == 'exit':
                    print(json.dumps({'closed': True}), flush=True)
                    break
                print(json.dumps({**{key: value for key, value in synthetic.items() if key != 'mode'},
                                  'network_guard': network_guard}), flush=True)
        finally:
            assistant.close()
            server.shutdown()
            server.server_close()
            provider.shutdown()
            provider.server_close()
            server_thread.join(timeout=3)
            provider_thread.join(timeout=3)
            store.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'synthetic_ai_fixture_failed'}), flush=True)
        sys.exit(1)
