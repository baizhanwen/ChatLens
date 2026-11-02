"""Actual Mac manager/HTTP/store/AI with entirely synthetic source adapters.

Only source preparation, contact enumeration, native state and source polling
are replaced. Profiles, SQLite, checkpoints, AI persistence, nonce checks and
HTTP handlers are production implementations. No existing private data is read.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import http.client
import json
from pathlib import Path
from queue import Queue, Empty
import secrets
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/windows-proof'), str(ROOT / 'tests')]
import ai_provider
import ai_service
from ai_web_server import attach_ai_routes
from collector_private import ensure_private_dir, read_private_json, write_private_json_new
from continuous_store import CollectorStore
from scope_history import apply_history_plan
from conversation_scope import normalize_conversations
from macos_active_account import ActiveAccount
import macos_web as web
from wechat_identity import establish_identity, unknown_identity


def now():
    return datetime.now(timezone.utc).isoformat()


class SyntheticProvider:
    public_config = staticmethod(ai_provider.public_config)
    load_config = staticmethod(ai_provider.load_config)
    update_config = staticmethod(ai_provider.update_config)

    def __init__(self):
        self.calls = []
        self.raw_identity_leaks = 0
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.slow = False

    def chat(self, messages, config, *, cancel_event):
        if any(value in json.dumps(messages) for value in ('synthetic-profile-self', 'synthetic-peer-',
                'synthetic-group-', 'self_sender_id', 'private_peer_id', 'source_key', 'binding_digest')):
            self.raw_identity_leaks += 1
        context = json.loads(messages[-1]['content'])
        self.calls.append(context)
        self.entered.set()
        if self.slow and not self.release.wait(10):
            raise RuntimeError('synthetic_model_gate_timeout')
        evidence = context['evidence']['messages']
        return {'text': '合成档案模型回答 [' + evidence[0]['citation'] + ']。',
                'model': config.model, 'usage': {'total_tokens': 10}}

    def test_connection(self, config, *, cancel_event):
        return {'ok': True}


class SyntheticCollector:
    """A source adapter that uses the real single-writer durable store."""
    def __init__(self, runtime, session, native, **options):
        self.runtime, self.session, self.native = runtime, session, native
        self.closed, self.polls = False, 0
        self.pending_messages = []
        self.private = session.data_dir / 'private'
        self.store = CollectorStore(self.private / 'store', session.binding)
        self.epochs = runtime.source_epochs()
        baseline = {'message/message_0.db': {row['alias']: {
            'local_id': len(runtime.source_messages(row['chat_id'])), 'present': True}
            for row in session.config['conversations']}}
        # Consume the same production history plan as ContinuousReader. Only
        # source MAX discovery and source polling are synthetic in this fixture.
        current = self.store.snapshot()
        snapshot = apply_history_plan(self.store, current['cursors'] if self.store.resumed else baseline, self.epochs,
                                      initialize=not self.store.resumed)
        self.cursors = snapshot['cursors']
        own = runtime.self_id()
        private = next((row for row in session.config['conversations']
                        if not row['chat_id'].endswith('@chatroom')), None)
        identity = (establish_identity(self.store.binding_digest, [own], [own],
            session.config['conversations'], {private['alias']: [private['chat_id'], own]})
            if private else unknown_identity(self.store.binding_digest))
        catalog = {'schema_version': 1, 'binding_digest': self.store.binding_digest,
            'identity': identity, 'conversations': [{'id': row['alias'],
                'name': runtime.name_for(row['chat_id']),
                'kind': 'group' if row['chat_id'].endswith('@chatroom') else 'private',
                'sender_names': {own: '合成本人', row['chat_id']: '合成联系人',
                                 'synthetic-group-member': '合成群成员'}}
                for row in session.config['conversations']]}
        web.atomic_private(self.private / 'catalog.json', catalog)
        for row in session.config['conversations']:
            source = (runtime.accounts.index(runtime.current), row['chat_id'])
            if source not in runtime.previously_watched:
                runtime.previously_watched.add(source)
                for direction in ('incoming', 'outgoing', 'unknown'):
                    self.commit(row['alias'], direction=direction)
        self._flush_source()
        self.heartbeat()

    def commit(self, alias=None, *, direction='incoming', body=None):
        chats = self.session.config['conversations']
        selected = next(row for row in chats if row['alias'] == (alias or chats[0]['alias']))
        self.runtime.append_source(selected['chat_id'], direction=direction, body=body)
        self._flush_source()
        return self.store.stats()['total_messages']

    def _flush_source(self):
        before = self.cursors
        after = json.loads(json.dumps(before))
        events = []
        for chat in self.session.config['conversations']:
            cursor = after['message/message_0.db'][chat['alias']]
            fresh = [event for event in self.runtime.source_messages(chat['chat_id'])
                     if event['source_local_id'] > cursor['local_id']]
            for event in fresh:
                events.append({**event, 'alias': chat['alias']})
                cursor['local_id'] = event['source_local_id']
        if events:
            self.store.commit(before, after, events, self.epochs)
            self.cursors = after

    def heartbeat(self, stage='collecting'):
        web.atomic_private(self.private / 'web-status.json', {'schema_version': 1,
            'binding_digest': self.store.binding_digest, 'stage': stage,
            'recorded_at_utc': now(), 'started_at_utc': self.store.snapshot()['baseline_completed_at'],
            'resumed_from_checkpoint': self.store.resumed, 'reason': None})

    def poll(self):
        self.polls += 1
        while self.pending_messages:
            self.commit(**self.pending_messages.pop(0))
        self._flush_source()
        self.heartbeat()

    def close(self, **options):
        if not self.closed:
            self.heartbeat('stopped')
            self.store.close()
            self.closed = True


class SyntheticRuntime:
    def __init__(self, *, automatic=False):
        self.temporary = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-multi-')
        self.root = Path(self.temporary.name).resolve()
        self.workspace = self.root / 'workspace'
        ensure_private_dir(self.workspace, create=True)
        self.accounts = []
        for index in range(2):
            path = self.root / ('synthetic-account-%d' % index) / 'db_storage'
            path.mkdir(parents=True)
            self.accounts.append(ActiveAccount(path, 100 + index, 1700000000 + index, '4.1.11'))
        self.current = self.accounts[0]
        self.synthetic_source = {}
        self.previously_watched = set()
        self.next_scope_gap = []
        self.choices = []
        private_names = ['合成青禾客户', '合成星桥客户', '合成远帆客户']
        group_names = ['合成交付讨论群', '合成订单确认群']
        for index in range(60):
            kind = 'private' if index < 35 else 'group'
            name = (private_names[index] if index < 3 else '合成客户%02d' % index) if kind == 'private' else (
                group_names[index - 35] if index < 37 else '合成群聊%02d' % index)
            self.choices.append({'id': 'choice_%03d' % index, 'name': name, 'type': kind,
                'chat_id': 'synthetic-peer-%d' % index if kind == 'private' else 'synthetic-group-%d@chatroom' % index})
        self.provider, self.services, self.collectors = SyntheticProvider(), [], []
        self.transitions = []
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(web, 'prepare_session', side_effect=self.prepare))
        self.stack.enter_context(patch.object(web, 'MacNativeProbe', return_value=lambda: {
            'probe_succeeded': True, 'running': True, 'frontmost': False, 'screen_locked': False}))
        self.stack.enter_context(patch.object(web, 'MacCollector', side_effect=self.collect))
        self.stack.enter_context(patch.object(web.MacWebManager, '_load_contacts', self.populate))
        original = ai_service.AiAssistant
        def assistant(*args, **kwargs):
            value = original(*args, provider=self.provider, **kwargs)
            self.services.append(value)
            return value
        self.stack.enter_context(patch.object(ai_service, 'AiAssistant', side_effect=assistant))
        self.manager = self.make_manager()
        self.manager.start()
        self.make_ready()
        self.server = attach_ai_routes(web.make_macos_server(self.manager, ROOT / 'apps/web'),
                                      lambda **kwargs: self.manager.ai_service(**kwargs))
        self.http_worker = threading.Thread(target=self.server.serve_forever,
            kwargs={'poll_interval': .01}, daemon=True)
        self.http_worker.start()
        self.cookie = None
        self.jobs, self.stopping = Queue(), threading.Event()
        self.automatic = automatic
        self.owner = None
        if automatic:
            self.owner = threading.Thread(target=self.loop, daemon=True)
            self.owner.start()

    def self_id(self):
        return 'synthetic-profile-self-%d' % self.accounts.index(self.current)

    def name_for(self, chat_id):
        return next(row['name'] for row in self.choices if row['chat_id'] == chat_id)

    def source_epochs(self):
        info = self.current.db_root.stat()
        index = self.accounts.index(self.current)
        return {'message/message_0.db': {'file_identity': [info.st_dev, info.st_ino],
            'salt_sha256': hashlib.sha256(('synthetic-salt-%d' % index).encode()).hexdigest(),
            'key_sha256': hashlib.sha256(('synthetic-key-%d' % index).encode()).hexdigest()}}

    def source_messages(self, chat_id):
        return self.synthetic_source.setdefault((self.accounts.index(self.current), chat_id), [])

    def append_source(self, chat_id, *, direction='incoming', body=None):
        rows = self.source_messages(chat_id)
        serial = len(rows) + 1
        sender = self.self_id() if direction == 'outgoing' else '' if direction == 'unknown' else (
            'synthetic-group-member' if chat_id.endswith('@chatroom') else chat_id)
        text = body or '合成账号%d · %s · %s · %d' % (
            self.accounts.index(self.current), self.name_for(chat_id), direction, serial)
        # The production source identity is independent of profile and alias.
        identity = ['message/message_0.db', chat_id, serial, self.source_epochs()['message/message_0.db']]
        canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        event = {'source_key': hashlib.sha256(canonical).hexdigest(), 'source_local_id': serial,
            'sent_at': int(datetime.now(timezone.utc).timestamp()), 'sender_id': sender,
            'body': text, 'observed_at': now()}
        rows.append(event)
        return event

    def make_manager(self):
        return web.MacWebManager(ROOT, self.workspace, self.root / 'synthetic-not-executed-probe',
                                 detector=lambda _: self.current)

    def populate(self, manager=None):
        manager = manager or self.manager
        selected = {row['chat_id'] for row in manager.active_config.get('conversations', [])}
        manager.contacts = [{key: row[key] for key in ('id', 'name', 'type')} |
                            {'selected': row['chat_id'] in selected} for row in self.choices]
        manager.catalog = {row['id']: {'chat_id': row['chat_id'], 'type': row['type']} for row in self.choices}
        manager.catalog_ready, manager.catalog_loading = True, False
        if manager.collector is None and not manager.switching:
            manager.stage = 'ready_to_select'

    def make_ready(self):
        self.manager.key_file = self.manager.directory / 'synthetic-reader.json'
        if not self.manager.key_file.exists():
            write_private_json_new(self.manager.key_file, {'synthetic_only': True})
        web.atomic_private(self.manager.directory / 'reader.json', {
            'account_identity': web.account_identity(self.current), 'key_file': str(self.manager.key_file)})
        self.populate()

    def prepare(self, data_dir, **options):
        data_dir = Path(data_dir)
        if data_dir.exists():
            scope = read_private_json(data_dir / 'private/scope.json')
            config = scope['config']
        else:
            config = {'conversations': normalize_conversations(read_private_json(options['selected_config']))}
            ensure_private_dir(data_dir, create=True)
            private = ensure_private_dir(data_dir / 'private', create=True)
            ensure_private_dir(private / 'store', create=True)
            info = self.current.db_root.stat()
            scope = {'schema_version': 1, 'platform': 'macos', 'session_id': secrets.token_hex(16),
                'db_root': str(self.current.db_root), 'root_identity': [info.st_dev, info.st_ino],
                'config': config, 'shards': ['message/message_0.db'],
                'client_version': self.current.client_version}
            write_private_json_new(private / 'scope.json', scope)
            # Source-only timing seam: these arrive after the old collector
            # stopped but before the new target discovers its source MAX.
            for event in self.next_scope_gap:
                self.append_source(event['chat_id'], body=event['body'])
            self.next_scope_gap = []
        binding = {'schema_version': 1, 'platform': 'macos', 'session_id': scope['session_id'],
                   'root': scope['db_root'], 'root_identity': scope['root_identity'],
                   'config': config, 'shards': scope['shards']}
        return SimpleNamespace(data_dir=data_dir, config=config, client_version=self.current.client_version,
            binding=binding)

    def collect(self, session, native, **options):
        value = SyntheticCollector(self, session, native, **options)
        self.collectors.append(value)
        return value

    def tick(self):
        self.manager.last_poll = 0
        self.manager.tick()

    def loop(self):
        while not self.stopping.wait(.025):
            try:
                function, event, result = self.jobs.get_nowait()
            except Empty:
                self.manager.tick()
                self.observe()
                continue
            try:
                result['value'] = function()
            except BaseException as error:
                result['error'] = error
            finally:
                self.observe()
                event.set()

    def observe(self):
        current = {'stage': self.manager.stage, 'switching': self.manager.switching,
            'provider_ready': self.manager.provider is not None, 'pending': self.manager.pending_session is not None,
            'account_index': self.accounts.index(self.current), 'closed': self.manager.closed}
        if not self.transitions or self.transitions[-1] != current:
            self.transitions.append(current)
            self.transitions = self.transitions[-12:]
        return current

    def own(self, function):
        if not self.automatic:
            return function()
        event, result = threading.Event(), {}
        self.jobs.put((function, event, result))
        if not event.wait(6):
            raise RuntimeError('synthetic_owner_timeout')
        if 'error' in result:
            raise result['error']
        return result.get('value')

    def request(self, method, path, value=None, *, generation='current', bootstrap=False):
        headers = {'Cookie': self.cookie} if self.cookie else {}
        if generation == 'current':
            generation = self.manager.scope_generation
        if generation is not None:
            headers['X-ChatLens-Scope'] = generation
        if bootstrap:
            headers['Authorization'] = 'Bearer ' + self.server.token
        raw = None if value is None else json.dumps(value, ensure_ascii=False).encode()
        if raw is not None:
            headers.update({'Content-Type': 'application/json', 'Origin': self.server.base_url})
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, raw, headers)
            response = connection.getresponse()
            document = json.loads(response.read())
            if response.getheader('Set-Cookie'):
                self.cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            return response.status, document
        finally:
            connection.close()

    def snapshot(self, profile_id=None):
        path = self.manager._profile_data(profile_id or self.manager.active_scope_id) / 'private/store/collector.sqlite3'
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        try:
            checkpoint = connection.execute('SELECT cursors,epochs,baseline_completed_at,last_commit_at,commits FROM checkpoint').fetchone()
            messages = connection.execute('SELECT rowid,* FROM messages ORDER BY rowid').fetchall()
        finally:
            connection.close()
        return {'checkpoint_sha256': hashlib.sha256(json.dumps(checkpoint).encode()).hexdigest(),
                'database_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                'messages_sha256': hashlib.sha256(json.dumps(messages).encode()).hexdigest(),
                'baseline': checkpoint[2], 'count': len(messages),
                'cursors': json.loads(checkpoint[0]),
                'unique_source_keys': len({row[1] for row in messages}),
                'row_fingerprints': {str(row[0]): hashlib.sha256(json.dumps(row).encode()).hexdigest()
                                     for row in messages}}

    def switch_account(self):
        self.current = self.accounts[1] if self.current == self.accounts[0] else self.accounts[0]
        self.manager.last_detection = 0
        self.manager.tick()
        return self.manager.setup()

    def command(self, value):
        command = value['command']
        if command == 'tick':
            self.own(self.tick)
        elif command == 'commit':
            self.own(lambda: self.manager.collector.commit(value.get('alias'),
                direction=value.get('direction', 'incoming'), body=value.get('body')))
        elif command == 'queue_message':
            self.own(lambda: self.manager.collector.pending_messages.append({
                'alias': value.get('alias'), 'direction': value.get('direction', 'incoming'),
                'body': value.get('body', '合成编辑名单期间收到的新文字')}))
        elif command == 'preload_source':
            row = next(row for row in self.choices if row['id'] == value['choice_id'])
            self.own(lambda: self.append_source(row['chat_id'],
                body=value.get('body', '合成未关注时的旧源文字，不应被新会话导入')))
        elif command == 'gap_on_next_scope':
            row = next(row for row in self.choices if row['id'] == value['choice_id'])
            self.own(lambda: self.next_scope_gap.append({'chat_id': row['chat_id'],
                'body': value.get('body', '合成切换间隙测试群文字')}))
        elif command == 'slow':
            self.provider.entered.clear(); self.provider.release.clear(); self.provider.slow = True
        elif command == 'release':
            self.provider.slow = False; self.provider.release.set()
        elif command == 'account':
            return self.own(self.switch_account)
        elif command == 'ready':
            self.own(self.make_ready)
        elif command == 'snapshot':
            return self.own(lambda: self.snapshot(value.get('profile_id')))
        return {'provider_calls': len(self.provider.calls), 'provider_entered': self.provider.entered.is_set(),
            'raw_identity_leaks': self.provider.raw_identity_leaks,
            'polls': self.manager.collector.polls if self.manager.collector else 0,
            'owner_state': self.own(self.observe), 'transitions': list(self.transitions),
            **self.manager.public_scope_context()}

    def close(self):
        self.own(self.manager.close)
        self.provider.release.set()
        for service in self.services:
            service.close()
        self.stopping.set()
        if self.owner:
            self.owner.join(timeout=3)
        self.server.shutdown(); self.server.server_close(); self.http_worker.join(timeout=3)
        self.stack.close()
        self.temporary.cleanup()


def main():
    from web_ai_fixture import install_loopback_guard
    guard = install_loopback_guard()
    runtime = SyntheticRuntime(automatic=True)
    print(json.dumps({'ready': True, 'launch_url': runtime.server.launch_url, 'network_guard': guard}), flush=True)
    try:
        for line in sys.stdin:
            value = json.loads(line)
            if value['command'] == 'exit':
                print(json.dumps({'closed': True}), flush=True)
                break
            result = runtime.command(value)
            print(json.dumps({**result, 'network_guard': guard}), flush=True)
    finally:
        runtime.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(json.dumps({'error': 'synthetic_multi_fixture_failed'}), flush=True)
        sys.exit(1)
