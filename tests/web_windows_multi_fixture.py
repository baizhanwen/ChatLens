"""Windows manager/HTTP/store/AI integration with synthetic platform adapters.

No installed WeChat, existing collector-data, credentials or native API is
accessed. Only source/supervisor/account probing is replaced; scope selection,
profile files, durable stores, HTTP and AI persistence run production code.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import sqlite3
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/windows-proof'), str(ROOT / 'tests')]
import ai_provider
import ai_service
from ai_web_server import attach_ai_routes
from collector_private import ensure_private_dir, read_private_json, write_private_json_new
from collector_service import prepare_session
from collector_web_meta import WebMetadata
from continuous_store import CollectorStore
from wechat_identity import establish_identity
from windows_web_manager import WindowsWebManager
from windows_web_server import make_windows_server


def now():
    return datetime.now(timezone.utc).isoformat()


class SyntheticProvider:
    public_config = staticmethod(ai_provider.public_config)
    load_config = staticmethod(ai_provider.load_config)
    update_config = staticmethod(ai_provider.update_config)

    def __init__(self):
        self.calls, self.raw_identity_leaks = [], 0
        self.slow, self.release = False, threading.Event()
        self.release.set()

    def chat(self, messages, config, *, cancel_event):
        text = json.dumps(messages)
        if any(value in text for value in ('synthetic-windows-self', 'synthetic-windows-peer-',
                'synthetic-windows-group-', 'self_sender_id', 'binding_digest', 'source_key')):
            self.raw_identity_leaks += 1
        context = json.loads(messages[-1]['content'])
        self.calls.append(context)
        if self.slow and not self.release.wait(10):
            raise RuntimeError('synthetic_windows_model_gate_timeout')
        evidence = context['evidence']['messages']
        return {'text': '合成 Windows 分析结果 [' + evidence[0]['citation'] + ']。',
                'model': config.model, 'usage': {'total_tokens': 12}}

    def test_connection(self, config, *, cancel_event):
        return {'ok': True}


class SyntheticChild:
    def __init__(self, runtime, data_dir, output):
        self.runtime, self.data_dir, self.output = runtime, Path(data_dir), Path(output)
        self.output.mkdir()
        self.returncode, self.closed, self.polls = None, False, 0
        self.scope, _, _, _, _, binding = prepare_session(self.data_dir)
        self.config = self.scope['config']
        self.store = CollectorStore(self.data_dir / 'private/store', binding)
        self.cursors = self.store.snapshot()['cursors'] if self.store.resumed else {
            'message/message_0.db': {row['alias']: {'local_id': 0, 'present': True}
                                   for row in self.config['conversations']}}
        if not self.store.resumed:
            self.store.initialize(self.cursors, {})
        metadata = WebMetadata(self.data_dir / 'private', self.store.binding_digest, now())
        private = next(row for row in self.config['conversations'] if not row['chat_id'].endswith('@chatroom'))
        identity = establish_identity(self.store.binding_digest, ['synthetic-windows-self'],
            ['synthetic-windows-self'], self.config['conversations'],
            {private['alias']: [private['chat_id'], 'synthetic-windows-self']})
        metadata.write('catalog.json', {'schema_version': 1, 'binding_digest': self.store.binding_digest,
            'identity': identity, 'conversations': [{'id': row['alias'],
                'name': runtime.name(row['chat_id']), 'kind': 'group' if row['chat_id'].endswith('@chatroom') else 'private',
                'sender_names': {row['chat_id']: runtime.name(row['chat_id']),
                    'synthetic-windows-member': '合成群成员'}} for row in self.config['conversations']]})
        self.metadata = metadata
        self.heartbeat()

    def commit(self, *, direction='incoming', alias=None, body=None):
        row = next(row for row in self.config['conversations'] if row['alias'] == (alias or self.config['conversations'][0]['alias']))
        before, after = self.cursors, json.loads(json.dumps(self.cursors))
        after['message/message_0.db'][row['alias']]['local_id'] += 1
        serial = after['message/message_0.db'][row['alias']]['local_id']
        sender = 'synthetic-windows-self' if direction == 'outgoing' else '' if direction == 'unknown' else (
            'synthetic-windows-member' if row['chat_id'].endswith('@chatroom') else row['chat_id'])
        text = body or '合成 Windows %s · %s · %s · %d' % (
            '原范围' if self.data_dir == self.runtime.data else self.data_dir.parent.name[:8],
            self.runtime.name(row['chat_id']), direction, serial)
        event = {'source_key': hashlib.sha256((row['alias'] + ':' + str(serial)).encode()).hexdigest(),
            'alias': row['alias'], 'source_local_id': serial, 'sender_id': sender, 'body': text,
            'sent_at': int(datetime.now(timezone.utc).timestamp()), 'observed_at': now()}
        self.store.commit(before, after, [event], {})
        self.cursors = after
        self.heartbeat()

    def heartbeat(self, stage='collecting'):
        self.metadata.status(stage, self.store.stats()['total_messages'], 0, self.store.resumed)

    def seed(self):
        for row in self.config['conversations']:
            for direction in ('incoming', 'outgoing', 'unknown'):
                self.commit(alias=row['alias'], direction=direction)

    def poll(self):
        self.polls += 1
        return self.returncode

    def close(self):
        if not self.closed:
            self.heartbeat('stopped')
            self.store.close()
            self.returncode, self.closed = 0, True


class SyntheticWindowsRuntime:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-windows-multi-')
        self.root = Path(self.temporary.name).resolve()
        self.data, self.source = self.root / 'collector-data', self.root / 'synthetic-account/db_storage'
        self.source.mkdir(parents=True)
        executable = self.root / 'Weixin.exe'
        executable.write_bytes(b'SYNTHETIC_NOT_EXECUTABLE')
        ensure_private_dir(self.data, create=True)
        private = ensure_private_dir(self.data / 'private', create=True)
        ensure_private_dir(private / 'store', create=True)
        self.choices = []
        for index in range(60):
            group = index >= 35
            name = (['青禾客户', '星桥客户', '远帆客户'][index] if index < 3 else '合成客户%02d' % index) if not group else (
                ['交付讨论群', '订单确认群'][index - 35] if index < 37 else '合成群聊%02d' % index)
            self.choices.append({'chat_id': 'synthetic-windows-peer-%d' % index if not group else 'synthetic-windows-group-%d@chatroom' % index,
                'name': name, 'type': 'group' if group else 'private'})
        config = {'conversations': [{'alias': alias, 'chat_id': self.choices[index]['chat_id'],
                    'expected_sender_id': 'synthetic-windows-peer-0'} for alias,index in [('private_test',0),('group_test',35)]]}
        write_private_json_new(private / 'scope.json', {'schema_version': 1, 'session_id': 'd' * 32,
            'db_root': str(self.source), 'exe_path': str(executable), 'config': config,
            'test_tokens': {alias: ['CL-WINDOWS-%s-%d' % (alias.upper(), i) for i in range(3)]
                            for alias in ('private_test', 'group_test')}})
        write_private_json_new(private / 'keys.json', {'schema_version': 1, 'db_root': str(self.source),
            'exe_path': str(executable), 'keys': {rel: {'enc_key': '01' * 32} for rel in (
                'message/message_0.db','message/message_1.db','contact/contact.db')}})
        self.provider, self.services, self.children = SyntheticProvider(), [], []
        self.stack = ExitStack()
        original = ai_service.AiAssistant
        def assistant(*args, **kwargs):
            service = original(*args, provider=self.provider, **kwargs)
            self.services.append(service)
            return service
        self.stack.enter_context(patch.object(ai_service, 'AiAssistant', side_effect=assistant))
        self.account_state = 'matched'
        self.manager = WindowsWebManager(self.data, ROOT / 'scripts/windows-proof', self.root / 'output',
            pid=12, creation_time=34, start_collector=self.start_child, stop_collector=self.stop_child,
            catalog_loader=lambda _: self.choices,
            account_probe=lambda: {'state': self.account_state, 'matched': self.account_state == 'matched',
                'reason': 'windows_account_synthetic'})
        self.manager.output.mkdir()
        self.jobs, self.stopping = Queue(), threading.Event()
        self.owner = threading.Thread(target=self.loop, daemon=True)
        self.owner.start()
        # Simulate an existing completed old run before upgrading the manager.
        def old_run():
            old = self.start_child(self.data, self.root / 'old-controlled-run')
            old.seed(); self.stop_child(old, old.output)
        self.own(old_run)
        self.old_snapshot = self.snapshot('legacy')
        self.own(self.manager.start)
        self.server = attach_ai_routes(make_windows_server(self.manager, ROOT / 'apps/web'),
            lambda **kwargs: self.manager.get_ai_service(**kwargs))
        self.http_worker = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.http_worker.start()

    def name(self, identifier):
        return next(row['name'] for row in self.choices if row['chat_id'] == identifier)

    def start_child(self, data, output):
        child = SyntheticChild(self, data, output)
        self.children.append(child)
        return child

    def stop_child(self, child, output):
        child.close()
        return 0, True

    def loop(self):
        while not self.stopping.wait(.025):
            try:
                function, event, result = self.jobs.get_nowait()
            except Empty:
                if self.manager.scope is not None:
                    self.manager.tick()
                    child = self.manager.child
                    if child is not None and not child.closed and time.monotonic() - getattr(child, 'last_heartbeat', 0) > .5:
                        child.heartbeat()
                        child.last_heartbeat = time.monotonic()
                continue
            try:
                result['value'] = function()
            except BaseException as error:
                result['error'] = error
            finally:
                event.set()

    def own(self, function):
        event, result = threading.Event(), {}
        self.jobs.put((function, event, result))
        if not event.wait(8):
            raise RuntimeError('synthetic_windows_owner_timeout')
        if 'error' in result:
            raise result['error']
        return result.get('value')

    def snapshot(self, profile_id=None):
        data = self.manager._profile_data(profile_id or self.manager.active_scope_id)
        path = data / 'private/store/collector.sqlite3'
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            checkpoint = connection.execute('SELECT cursors,epochs,baseline_completed_at,last_commit_at,commits FROM checkpoint').fetchone()
            rows = connection.execute('SELECT * FROM messages ORDER BY rowid').fetchall()
        finally:
            connection.close()
        return {'checkpoint_sha256': hashlib.sha256(json.dumps(checkpoint).encode()).hexdigest(),
            'messages_sha256': hashlib.sha256(json.dumps(rows).encode()).hexdigest(), 'baseline': checkpoint[2], 'count':len(rows)}

    def command(self, value):
        name = value['command']
        if name == 'seed':
            self.own(lambda: self.manager.child.seed())
        elif name == 'commit':
            self.own(lambda: self.manager.child.commit(direction=value.get('direction','incoming'),alias=value.get('alias')))
        elif name == 'snapshot':
            return self.own(lambda: self.snapshot(value.get('profile_id')))
        elif name == 'slow':
            self.provider.release.clear(); self.provider.slow = True
        elif name == 'release':
            self.provider.slow = False; self.provider.release.set()
        elif name == 'account':
            def change():
                self.account_state = 'matched' if self.account_state != 'matched' else 'mismatch'
                self.manager.last_probe_at = float('-inf')
                self.manager.tick()
            self.own(change)
        return {'provider_calls':len(self.provider.calls),'raw_identity_leaks':self.provider.raw_identity_leaks,
            'account_state':self.account_state,'legacy_unchanged':self.snapshot('legacy') == self.old_snapshot,
            **self.manager.public_scope_context()}

    def close(self):
        self.own(self.manager.close)
        self.own(lambda: [child.close() for child in self.children])
        self.provider.release.set()
        self.stopping.set(); self.owner.join(timeout=3)
        self.server.shutdown(); self.server.server_close(); self.http_worker.join(timeout=3)
        self.stack.close(); self.temporary.cleanup()


def main():
    from web_ai_fixture import install_loopback_guard
    guard = install_loopback_guard()
    runtime = SyntheticWindowsRuntime()
    print(json.dumps({'ready':True,'launch_url':runtime.server.launch_url,'network_guard':guard}),flush=True)
    try:
        for line in sys.stdin:
            value=json.loads(line)
            if value['command']=='exit':
                print(json.dumps({'closed':True}),flush=True)
                break
            print(json.dumps({**runtime.command(value),'network_guard':guard}),flush=True)
    finally:
        runtime.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error':'synthetic_windows_multi_fixture_failed'}),flush=True)
        sys.exit(1)
