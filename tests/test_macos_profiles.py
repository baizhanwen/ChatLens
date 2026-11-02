"""Synthetic multi-scope runtime boundaries; no WeChat/source/model access."""
from contextlib import ExitStack
import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import macos_web as web
from macos_active_account import ActiveAccount, ActiveAccountError
from macos_collector import MacNativeProbe as RealMacNativeProbe
from ai_service import AiError
from collector_private import ensure_private_dir, read_private_json, write_private_json_new
from conversation_scope import alias_for, normalize_conversations
from web_data import WebDataError


class FakeCollector:
    def __init__(self, session, native, **options):
        self.session, self.native, self.options = session, native, options
        self.closed, self.polls = [], 0

    def close(self, **options):
        self.closed.append(options)

    def poll(self):
        self.polls += 1


class FakeProvider:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)

    def conversations(self):
        return {'conversations': [{'id': 'synthetic-current-scope', 'name': '合成当前范围'}]}

    def messages(self, *args, **options):
        return {'messages': [], 'has_more': False}

    def status(self):
        return {'state': 'collecting', 'total_messages': 0}


class ScopeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-profiles-')
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'project'; self.project.mkdir()
        self.workspace = self.root / 'workspace'; self.workspace.mkdir(mode=0o700)
        first = self.root / 'account-first/db_storage'
        second = self.root / 'account-second/db_storage'
        first.mkdir(parents=True); second.mkdir(parents=True)
        self.account = ActiveAccount(first, 123, 1700000000, '4.1.15')
        self.other_account = ActiveAccount(second, 456, 1700000010, '4.1.15')
        self.current = self.account
        self.created, self.prepared = [], []
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(web, 'prepare_session', side_effect=self.prepare))
        self.stack.enter_context(patch.object(web, 'MacNativeProbe', return_value=lambda: {
            'probe_succeeded': True, 'running': True, 'frontmost': False, 'screen_locked': False}))
        self.stack.enter_context(patch.object(web, 'MacCollector', side_effect=self.create))
        self.stack.enter_context(patch.object(web, 'CollectorWebData', FakeProvider))
        self.manager = self.new_manager()
        self.manager.start()
        self.manager.key_file = self.manager.directory / 'synthetic-keys.json'
        write_private_json_new(self.manager.key_file, {'synthetic_only': True})
        self.choices = {'opaque-a': 'synthetic-peer-a', 'opaque-b': 'synthetic-peer-b',
                        'opaque-c': 'synthetic-group-c@chatroom', 'opaque-d': 'synthetic-group-d@chatroom'}
        self.ready()

    def tearDown(self):
        self.manager.close()
        self.stack.close()
        self.temp.cleanup()

    def new_manager(self):
        return web.MacWebManager(self.project, self.workspace, self.root / 'synthetic-probe',
                                 detector=lambda _: self.current)

    def ready(self):
        self.manager.contacts = [{'id': token, 'name': '合成会话 ' + token[-1],
                                  'type': 'group' if chat.endswith('@chatroom') else 'private'}
                                 for token, chat in self.choices.items()]
        self.manager.catalog = {token: {'chat_id': chat, 'type': 'group' if chat.endswith('@chatroom') else 'private'}
                                for token, chat in self.choices.items()}
        self.manager.catalog_ready = True
        if self.manager.collector is None:
            self.manager.stage = 'ready_to_select'

    def prepare(self, data_dir, **options):
        data_dir = Path(data_dir)
        self.prepared.append((data_dir, dict(options)))
        if data_dir.exists():
            saved = read_private_json(data_dir / 'private/scope.json')
            config = saved['config']
        else:
            config = {'conversations': normalize_conversations(read_private_json(options['selected_config']))}
            ensure_private_dir(data_dir, create=True)
            private = ensure_private_dir(data_dir / 'private', create=True)
            ensure_private_dir(private / 'store', create=True)
            write_private_json_new(private / 'scope.json', {'config': config, 'db_root': str(self.current.db_root)})
            write_private_json_new(private / 'synthetic-checkpoint.json', {'baseline': 'SYNTHETIC_PRESERVED_BASELINE', 'events': []})
        return SimpleNamespace(config=config, client_version='4.1.15', data_dir=data_dir)

    def create(self, session, native, **options):
        collector = FakeCollector(session, native, **options)
        self.created.append(collector)
        return collector

    def select(self, tokens):
        self.ready()
        response = self.manager.select_many(tokens, expected_generation=self.manager.scope_generation)
        self.assertTrue(response['scope_switching'])
        self.manager.tick()
        self.assertEqual(self.manager.stage, 'running')
        return self.manager.active_scope_id

    def test_any_mixed_multiple_private_or_group_only_selection_is_supported(self):
        identifier = self.select(list(self.choices))
        config = self.manager.active_config['conversations']
        self.assertEqual(len(config), 4)
        self.assertEqual({row['alias'] for row in config}, {alias_for(chat) for chat in self.choices.values()})
        self.assertEqual(self.manager.setup()['active_scope']['count'], 4)
        self.assertEqual(self.manager.data_dir, self.manager.directory / 'profiles' / identifier / 'collector-data')
        self.assertEqual(self.manager.setup()['scope_limits']['total'], 50)
        second = self.select(['opaque-c', 'opaque-d'])
        self.assertNotEqual(identifier, second)
        self.assertEqual(self.manager.setup()['active_scope']['private_count'], 0)
        self.assertEqual(self.manager.setup()['active_scope']['group_count'], 2)

    def test_runtime_ambiguous_account_pauses_and_recovers_without_reusing_old_account_keys(self):
        self.select(['opaque-a', 'opaque-c'])
        old_collector = self.manager.collector
        old_generation, old_directory = self.manager.scope_generation, self.manager.directory
        old_key = self.manager.key_file.read_bytes()
        old_checkpoint = self.manager.data_dir / 'private/synthetic-checkpoint.json'
        checkpoint = old_checkpoint.read_bytes()
        self.manager.detector = Mock(side_effect=ActiveAccountError('macos_active_account_ambiguous', wechat_running=True))
        self.manager.last_detection = self.manager.last_poll = 0
        self.manager.tick()
        setup = self.manager.setup()
        self.assertFalse(setup['account']['detected'])
        self.assertTrue(setup['checks']['wechat_running'])
        self.assertFalse(setup['checks']['reader_ready'])
        self.assertEqual(setup['reason'], 'macos_active_account_ambiguous')
        self.assertNotEqual(setup['stage'], 'running')
        self.assertTrue(setup['detection']['collector_paused'])
        self.assertEqual(setup['profiles'], [])
        self.assertIsNone(setup['active_scope'])
        self.assertEqual(setup['catalog']['state'], 'idle')
        self.assertEqual(self.manager.list_conversations()['conversations'], [])
        self.assertFalse(self.manager._native()['probe_succeeded'])
        self.assertEqual(self.manager.status()['state'], 'paused')
        self.assertEqual(self.manager.status()['reason'], 'macos_active_account_ambiguous')
        self.manager.request_initialize()
        self.assertFalse(self.manager.queued)
        self.assertEqual(self.manager.directory, old_directory)
        self.assertEqual(self.manager.key_file.read_bytes(), old_key)
        self.assertEqual(old_checkpoint.read_bytes(), checkpoint)

        # The original account can recover without changing its stored domain.
        self.manager.detector = lambda _: self.account
        self.manager.last_detection = 0
        self.manager.tick()
        self.assertTrue(self.manager.setup()['account']['detected'])
        self.assertEqual(self.manager.status()['state'], 'collecting')
        self.assertEqual(self.manager.scope_generation, old_generation)

        # A later unique new account gets a fresh namespace, never the old key.
        self.current = self.other_account
        self.manager.detector = lambda _: self.current
        self.manager.last_detection = 0
        self.manager.tick()
        self.assertEqual(self.manager.account, self.other_account)
        self.assertNotEqual(self.manager.directory, old_directory)
        self.assertNotEqual(self.manager.scope_generation, old_generation)
        self.assertEqual(self.manager.stage, 'needs_initialization')
        self.assertIsNone(self.manager.key_file)
        self.assertEqual(self.manager.contacts, [])
        self.assertTrue(old_collector.closed)
        self.assertEqual(old_checkpoint.read_bytes(), checkpoint)

    def test_same_account_process_restart_recovers_with_fresh_pid_and_original_checkpoint(self):
        self.select(['opaque-a', 'opaque-c'])
        collector = self.manager.collector
        generation = self.manager.scope_generation
        checkpoint = self.manager.data_dir / 'private/synthetic-checkpoint.json'
        before = checkpoint.read_bytes()
        self.manager.probe.write_bytes(b'SYNTHETIC_EXECUTABLE_NEVER_RUN')
        self.manager.probe.chmod(0o700)
        def synthetic_run(*arguments, **options):
            payload = {'matching_applications': 1, 'running': True, 'screen_state_available': True,
                       'frontmost': False, 'screen_locked': False, 'process_id': self.current.pid,
                       'process_started_at': self.current.started_at, 'version': self.current.client_version}
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload).encode())
        def make_probe(path, *, expected_version):
            return RealMacNativeProbe(path, expected_version=expected_version, run=synthetic_run)
        original_probe = make_probe(self.manager.probe, expected_version=self.account.client_version)
        self.assertTrue(original_probe()['probe_succeeded'])
        self.manager.native_probe = original_probe
        restarted = ActiveAccount(self.account.db_root, self.account.pid + 100,
                                  self.account.started_at + 50, self.account.client_version)
        self.current = restarted
        self.assertFalse(original_probe()['probe_succeeded'])
        self.manager.last_detection = 0
        with patch.object(web, 'MacNativeProbe', side_effect=make_probe):
            self.manager.tick()
        self.assertEqual(self.manager.account, restarted)
        self.assertIsNot(self.manager.native_probe, original_probe)
        self.assertEqual(self.manager.native_probe.process_identity,
                         (restarted.pid, restarted.started_at, restarted.client_version))
        self.assertTrue(self.manager._native()['probe_succeeded'])
        self.assertTrue(self.manager.setup()['account']['detected'])
        self.assertEqual(self.manager.status()['state'], 'collecting')
        self.assertEqual(self.manager.scope_generation, generation)
        self.assertIs(self.manager.collector, collector)
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_same_account_new_process_version_must_match_saved_reader_session(self):
        self.select(['opaque-a', 'opaque-c'])
        original = self.manager.account
        collector, directory = self.manager.collector, self.manager.directory
        self.current = ActiveAccount(original.db_root, original.pid + 100,
                                     original.started_at + 50, '9.9.9')
        self.manager.last_detection = 0
        self.manager.tick()
        result = self.manager.setup()
        self.assertFalse(result['account']['detected'])
        self.assertEqual(result['reason'], 'macos_active_client_version_changed')
        self.assertEqual(self.manager.status()['state'], 'paused')
        self.assertFalse(self.manager._native()['probe_succeeded'])
        self.assertEqual(self.manager.account, original)
        self.assertEqual(self.manager.directory, directory)
        self.assertIs(self.manager.collector, collector)

        self.current = original
        self.manager.last_detection = 0
        self.manager.tick()
        self.assertTrue(self.manager.setup()['account']['detected'])
        self.assertIsNone(self.manager.setup()['reason'])

    def test_switch_preserves_old_checkpoint_and_has_independent_assistant_and_message_dirs(self):
        first = self.select(['opaque-a', 'opaque-c'])
        first_data = self.manager.data_dir
        checkpoint = first_data / 'private/synthetic-checkpoint.json'
        before = checkpoint.read_bytes()
        old_collector = self.manager.collector
        old_assistant = SimpleNamespace(close=lambda: setattr(old_assistant, 'closed', True), closed=False)
        self.manager.assistant = old_assistant
        old_generation = self.manager.scope_generation
        self.ready()
        self.manager.select_many(['opaque-b', 'opaque-d'], expected_generation=old_generation)
        self.assertNotEqual(self.manager.scope_generation, old_generation)
        with self.assertRaisesRegex(WebDataError, 'scope_changed'):
            self.manager.conversations(expected_generation=old_generation)
        with self.assertRaises(WebDataError):
            self.manager.conversations(expected_generation=self.manager.scope_generation)
        self.manager.tick()
        self.assertTrue(old_assistant.closed)
        self.assertEqual(len(old_collector.closed), 1)
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertNotEqual(self.manager.data_dir, first_data)
        self.assertIsNone(self.manager.assistant)
        self.assertEqual(self.manager._assistant_directory(), self.manager.directory / 'profiles' / self.manager.active_scope_id / 'assistant')
        self.assertEqual(len(self.manager.setup()['profiles']), 2)
        self.manager.restore_profile(first, expected_generation=self.manager.scope_generation)
        self.manager.tick()
        self.assertEqual(self.manager.data_dir, first_data)
        self.assertEqual(checkpoint.read_bytes(), before)
        self.assertEqual(self.manager.active_scope_id, first)

    def test_model_config_can_follow_same_account_but_threads_never_copy(self):
        self.select(['opaque-a'])
        source = self.manager._assistant_directory()
        ensure_private_dir(source, create=True)
        ensure_private_dir(source / 'threads', create=True)
        value = {'synthetic_only': 'SYNTHETIC_NOT_A_REAL_KEY'}
        write_private_json_new(source / 'provider.json', value)
        write_private_json_new(source / 'threads/synthetic.json', {'SYNTHETIC_PRIVATE_THREAD': True})
        self.select(['opaque-b', 'opaque-c'])
        target = self.manager._assistant_directory()
        self.assertEqual(read_private_json(target / 'provider.json'), value)
        self.assertFalse((target / 'threads').exists())
        self.assertTrue((source / 'threads/synthetic.json').exists())
        self.assertEqual((target / 'provider.json').stat().st_mode & 0o777, 0o600)

    def test_active_pointer_resumes_exact_saved_profile_without_rebaselining(self):
        identifier = self.select(['opaque-b', 'opaque-d'])
        data = self.manager.data_dir
        marker = data / 'private/synthetic-checkpoint.json'
        before = marker.read_bytes()
        self.manager.close()
        self.manager = self.new_manager()
        self.manager.start()
        self.assertEqual(self.manager.active_scope_id, identifier)
        self.assertEqual(self.manager.data_dir, data)
        self.assertEqual(self.manager.stage, 'starting')
        self.manager.tick()
        self.assertEqual(self.manager.stage, 'running')
        self.assertEqual(marker.read_bytes(), before)
        self.assertNotIn('selected_config', self.prepared[-1][1])

    def test_profile_paths_and_model_data_do_not_cross_account(self):
        self.select(['opaque-a', 'opaque-c'])
        old_directory = self.manager.directory
        self.manager.close()
        self.current = self.other_account
        self.manager = self.new_manager()
        self.manager.start()
        self.assertNotEqual(self.manager.directory, old_directory)
        self.assertEqual(self.manager.setup()['profiles'], [])
        self.assertIsNone(self.manager.provider)
        self.assertIsNone(self.manager.key_file)
        self.assertFalse((self.manager.directory / 'active-scope.json').exists())

    def test_running_account_roundtrip_invalidates_old_scope_and_restores_only_own_files(self):
        first_key = self.manager.key_file
        write_private_json_new(self.manager.directory / 'reader.json', {
            'account_identity': web.account_identity(self.account), 'key_file': str(first_key)})
        first_scope = self.select(['opaque-a', 'opaque-c'])
        first_directory, first_data = self.manager.directory, self.manager.data_dir
        checkpoint = first_data / 'private/synthetic-checkpoint.json'
        checkpoint_before = checkpoint.read_bytes()
        first_ai = self.manager._assistant_directory()
        ensure_private_dir(first_ai, create=True)
        ensure_private_dir(first_ai / 'threads', create=True)
        write_private_json_new(first_ai / 'provider.json', {'synthetic_account': 'A_MODEL_ONLY'})
        write_private_json_new(first_ai / 'threads/first.json', {'synthetic_thread': 'A_HISTORY_ONLY'})
        first_files = {path: path.read_bytes() for path in first_ai.rglob('*.json')}
        first_collector = self.manager.collector
        first_assistant = SimpleNamespace(closed=False)
        first_assistant.close = lambda: setattr(first_assistant, 'closed', True)
        self.manager.assistant = first_assistant
        self.ready()
        first_generation = self.manager.scope_generation
        before_count = len(self.prepared)
        self.manager.pending_identity_hint = {'synthetic_private_proof': 'ACCOUNT_A_ONLY'}
        self.manager.pending_display_names = ['ACCOUNT_A_ONLY']
        self.manager.catalog_loading = self.manager.queued = True
        self.manager.commands.put(('select', ('synthetic-peer-a', 'synthetic-group-c@chatroom')))

        self.current = self.other_account
        self.manager.tick()
        second_directory = self.manager.directory
        self.assertNotEqual(second_directory, first_directory)
        self.assertEqual(self.manager.account, self.other_account)
        self.assertNotEqual(self.manager.scope_generation, first_generation)
        self.assertEqual(self.manager.stage, 'needs_initialization')
        self.assertTrue(first_assistant.closed)
        self.assertEqual(len(first_collector.closed), 1)
        self.assertIsNone(self.manager.provider)
        self.assertIsNone(self.manager.assistant)
        self.assertIsNone(self.manager.initializer)
        self.assertIsNone(self.manager.key_file)
        self.assertIsNone(self.manager.pending_session)
        self.assertIsNone(self.manager.pending_identity_hint)
        self.assertEqual(self.manager.pending_display_names, [])
        self.assertEqual(self.manager.active_config, {'conversations': []})
        self.assertEqual(self.manager.contacts, [])
        self.assertEqual(self.manager.catalog, {})
        self.assertFalse(self.manager.catalog_loading)
        self.assertTrue(self.manager.commands.empty())
        self.assertEqual(self.manager.setup()['profiles'], [])
        self.assertEqual(self.manager.list_conversations()['conversations'], [])
        self.assertEqual(len(self.prepared), before_count)
        self.assertEqual(list(second_directory.iterdir()), [])
        for read in (self.manager.conversations, self.manager.status, self.manager.list_conversations):
            with self.assertRaises(WebDataError) as raised:
                read(expected_generation=first_generation)
            self.assertEqual((raised.exception.code, raised.exception.status), ('scope_changed', 409))
        with self.assertRaises(AiError) as raised:
            self.manager.ai_service(expected_generation=first_generation)
        self.assertEqual((raised.exception.code, raised.exception.status), ('scope_changed', 409))
        with self.assertRaises(ValueError):
            self.manager.restore_profile(first_scope, expected_generation=self.manager.scope_generation)

        # B must initialize with B's own reader before choosing any scope. No
        # automatic source preparation or copied A credential is permitted.
        second_key = second_directory / 'synthetic-b-keys.json'
        write_private_json_new(second_key, {'synthetic_account': 'B_KEYS_ONLY'})
        write_private_json_new(second_directory / 'reader.json', {
            'account_identity': web.account_identity(self.other_account), 'key_file': str(second_key)})
        self.manager.key_file = second_key
        self.choices = {'opaque-b-only': 'synthetic-b-only-peer'}
        second_scope = self.select(['opaque-b-only'])
        self.assertEqual(self.prepared[-1][1]['db_root'], self.other_account.db_root)
        self.assertEqual(self.prepared[-1][1]['key_file'], second_key)
        self.assertEqual(self.created[-1].options, {})
        second_ai = self.manager._assistant_directory()
        self.assertFalse(second_ai.exists())
        ensure_private_dir(second_ai, create=True)
        ensure_private_dir(second_ai / 'threads', create=True)
        write_private_json_new(second_ai / 'provider.json', {'synthetic_account': 'B_MODEL_ONLY'})
        write_private_json_new(second_ai / 'threads/second.json', {'synthetic_thread': 'B_HISTORY_ONLY'})
        second_files = {path: path.read_bytes() for path in second_ai.rglob('*.json')}
        second_generation, second_collector = self.manager.scope_generation, self.manager.collector

        self.current = self.account
        self.manager.last_detection = 0
        self.manager.tick()
        self.assertEqual(self.manager.account, self.account)
        self.assertEqual(self.manager.directory, first_directory)
        self.assertEqual(self.manager.stage, 'starting')
        self.assertEqual(self.manager.key_file, first_key)
        self.assertEqual(self.manager.active_scope_id, first_scope)
        self.assertEqual(len(second_collector.closed), 1)
        self.assertNotEqual(self.manager.scope_generation, second_generation)
        with self.assertRaises(WebDataError) as raised:
            self.manager.status(expected_generation=second_generation)
        self.assertEqual(raised.exception.status, 409)
        self.manager.tick()
        self.assertEqual(self.manager.stage, 'running')
        self.assertEqual(self.manager.data_dir, first_data)
        self.assertEqual(checkpoint.read_bytes(), checkpoint_before)
        self.assertEqual(self.prepared[-1][1], {'db_root': self.account.db_root})
        self.assertEqual({row['id'] for row in self.manager.setup()['profiles']}, {first_scope})
        self.assertNotEqual(first_scope, second_scope)
        for path, before in (first_files | second_files).items():
            self.assertEqual(path.read_bytes(), before)

    def test_temporarily_unavailable_detection_keeps_offline_scope_and_periodically_retries(self):
        self.select(['opaque-a'])
        generation = self.manager.scope_generation
        provider, collector = self.manager.provider, self.manager.collector
        self.manager.last_detection = 0
        with patch.object(self.manager, 'detector', side_effect=ValueError('SYNTHETIC_NOT_LOGGED_IN')) as detect:
            self.manager.tick()
            self.manager.tick()
        self.assertEqual(detect.call_count, 1)
        self.assertEqual(self.manager.scope_generation, generation)
        self.assertIs(self.manager.provider, provider)
        self.assertIs(self.manager.collector, collector)
        self.assertEqual(collector.closed, [])
        self.assertEqual(self.manager.conversations(expected_generation=generation)['scope_generation'], generation)

    def test_native_observed_account_change_disconnects_after_poll_without_waiting_for_timer(self):
        self.select(['opaque-a'])
        collector = self.manager.collector
        generation = self.manager.scope_generation
        self.manager.last_detection = web.time.monotonic()
        def poll():
            self.current = self.other_account
            self.assertFalse(self.manager._native()['probe_succeeded'])
            # The collector is still in its own poll; shutdown waits for the
            # owner safe point immediately after this call returns.
            self.assertEqual(collector.closed, [])
        collector.poll = poll
        self.manager.last_poll = 0
        self.manager.tick()
        self.assertEqual(self.manager.account, self.other_account)
        self.assertNotEqual(self.manager.scope_generation, generation)
        self.assertEqual(len(collector.closed), 1)
        self.assertIsNone(self.manager.provider)

    def test_initializer_owned_logout_is_not_interrupted_by_account_poll(self):
        self.manager.stage = 'needs_initialization'
        initializer = SimpleNamespace(state='needs_user_action', action='confirm_login', closes=0, steps=0)
        initializer.close = lambda: setattr(initializer, 'closes', initializer.closes + 1)
        initializer.step = lambda: setattr(initializer, 'steps', initializer.steps + 1)
        self.manager.initializer = initializer
        self.manager.last_detection = 0
        generation = self.manager.scope_generation
        with patch.object(self.manager, 'detector', side_effect=AssertionError('owned initialization must not redetect')) as detect:
            self.manager.tick()
        detect.assert_not_called()
        self.assertEqual(initializer.steps, 1)
        self.assertEqual(initializer.closes, 0)
        self.assertEqual(self.manager.scope_generation, generation)
        initializer.state = 'error'
        self.current = self.other_account
        self.manager.tick()
        self.assertEqual(initializer.closes, 1)
        self.assertEqual(self.manager.account, self.other_account)
        self.assertIsNone(self.manager.initializer)

    def test_picker_keeps_all_fifty_current_selections_visible_among_large_catalog(self):
        self.manager.contacts = [{'id': f'opaque-{index:03d}', 'name': f'合成会话 {index:03d}',
                                  'type': 'private', 'selected': index >= 210}
                                 for index in range(260)]
        before = list(self.manager.contacts)
        result = self.manager.list_conversations(expected_generation=self.manager.scope_generation)
        self.assertTrue(result['has_more'])
        self.assertEqual(len(result['conversations']), 200)
        self.assertEqual({row['id'] for row in result['conversations'] if row['selected']},
                         {f'opaque-{index:03d}' for index in range(210, 260)})
        self.assertTrue(all(row['selected'] for row in result['conversations'][:50]))
        self.assertEqual(self.manager.contacts, before)
        matches = self.manager.list_conversations('会话 2', expected_generation=self.manager.scope_generation)
        self.assertEqual([row['id'] for row in matches['conversations']],
                         [f'opaque-{index:03d}' for index in range(200, 260)])

    def test_http_rejects_old_account_nonce_for_data_picker_and_ai_after_runtime_switch(self):
        self.select(['opaque-a'])
        self.manager.provider.conversations = lambda: {'conversations': [{'id': 'A_DATA_ONLY'}]}
        assistant = SimpleNamespace(closed=False, list_threads=lambda: {'threads': [{'title': 'A_HISTORY_ONLY'}]})
        assistant.close = lambda: setattr(assistant, 'closed', True)
        self.manager.assistant = assistant
        static = self.project / 'apps/web'; static.mkdir(parents=True)
        (static / 'index.html').write_text('SYNTHETIC_ONLY')
        server = web.make_macos_server(self.manager, static)
        from ai_web_server import attach_ai_routes
        attach_ai_routes(server, self.manager.ai_service)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        worker.start()
        cookie = None
        def request(method, path, value=None, generation=None, bootstrap=False):
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            headers = {}
            if cookie:
                headers['Cookie'] = cookie
            if generation:
                headers['X-ChatLens-Scope'] = generation
            if bootstrap:
                headers['Authorization'] = 'Bearer ' + server.token
            raw = None if value is None else json.dumps(value).encode()
            if raw is not None:
                headers.update({'Content-Type': 'application/json', 'Origin': server.base_url})
            connection.request(method, path, body=raw, headers=headers)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), json.loads(response.read())
            connection.close()
            return result
        try:
            code, headers, session = request('GET', '/api/session', bootstrap=True)
            self.assertEqual(code, 200)
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            stale = session['scope_generation']
            self.assertIn('A_DATA_ONLY', json.dumps(request('GET', '/api/conversations', generation=stale)[2]))
            self.assertIn('A_HISTORY_ONLY', json.dumps(request('GET', '/api/ai/threads', generation=stale)[2]))
            self.current = self.other_account
            self.manager.last_detection = 0
            count = len(self.prepared)
            self.manager.tick()
            self.assertTrue(assistant.closed)
            for path in ('/api/conversations', '/api/status', '/api/messages?conversation_id=private_test',
                         '/api/setup/conversations', '/api/ai/threads', '/api/ai/config'):
                with self.subTest(path=path):
                    code, _, body = request('GET', path, generation=stale)
                    self.assertEqual(code, 409)
                    self.assertEqual(body['error']['code'], 'scope_changed')
                    self.assertNotIn('A_DATA_ONLY', json.dumps(body))
                    self.assertNotIn('A_HISTORY_ONLY', json.dumps(body))
            code, _, body = request('POST', '/api/setup/select', {'conversation_ids': ['opaque-a']}, generation=stale)
            self.assertEqual((code, body['error']['code']), (409, 'scope_changed'))
            code, _, setup = request('GET', '/api/setup')
            self.assertEqual(code, 200)
            self.assertNotEqual(setup['scope_generation'], stale)
            self.assertEqual(setup['profiles'], [])
            self.assertFalse(setup['checks']['reader_ready'])
            self.assertEqual(request('GET', '/api/setup/conversations', generation=setup['scope_generation'])[2]['conversations'], [])
            code, _, body = request('GET', '/api/ai/threads', generation=setup['scope_generation'])
            self.assertEqual((code, body['error']['code']), (409, 'ai_knowledge_not_ready'))
            self.assertEqual(len(self.prepared), count)
        finally:
            server.shutdown()
            worker.join(timeout=2)
            server.server_close()

    def test_restore_rejects_stale_nonce_foreign_profile_and_redirected_directory(self):
        identifier = self.select(['opaque-a'])
        stale = self.manager.scope_generation
        self.select(['opaque-b'])
        with self.assertRaisesRegex(WebDataError, 'scope_changed'):
            self.manager.restore_profile(identifier, expected_generation=stale)
        with self.assertRaises(ValueError):
            self.manager.restore_profile('0' * 64, expected_generation=self.manager.scope_generation)
        for value in ('../other', '/tmp/other', 'f' * 63):
            with self.assertRaises(ValueError):
                self.manager._profile_root(value)

    def test_account_change_before_owner_commit_never_prepares_old_scope_against_new_account(self):
        self.select(['opaque-a'])
        count = len(self.prepared)
        self.ready()
        self.manager.select_many(['opaque-b'], expected_generation=self.manager.scope_generation)
        self.current = self.other_account
        self.manager.tick()
        self.assertEqual(len(self.prepared), count)
        self.assertEqual(self.manager.stage, 'needs_initialization')
        self.assertEqual(self.manager.account, self.other_account)
        self.assertIsNone(self.manager.key_file)
        self.assertIsNone(self.manager.provider)
        self.assertIsNone(self.manager.collector)

    def test_catalog_loading_does_not_stop_current_collector(self):
        self.select(['opaque-a'])
        collector = self.manager.collector
        with patch.object(self.manager, '_load_contacts', side_effect=self.ready) as load:
            self.manager.request_catalog(expected_generation=self.manager.scope_generation)
            self.assertEqual(collector.closed, [])
            self.manager.tick()
        load.assert_called_once()
        self.assertEqual(collector.closed, [])
        self.assertEqual(self.manager.stage, 'running')

    def test_selection_rejects_unselected_ids_duplicates_empty_and_over_limit(self):
        for values in ([], ['opaque-a', 'opaque-a'], ['synthetic-peer-a'], ['unknown'], ['x'] * 51):
            with self.subTest(values=len(values)):
                with self.assertRaises(ValueError):
                    self.manager.select_many(values, expected_generation=self.manager.scope_generation)
                self.assertFalse(self.manager.switching)

    def test_failed_new_scope_keeps_old_pointer_and_can_restore_its_original_data(self):
        original = self.select(['opaque-a'])
        data = self.manager.data_dir
        pointer = (self.manager.directory / 'active-scope.json').read_bytes()
        marker = (data / 'private/synthetic-checkpoint.json').read_bytes()
        self.ready()
        self.manager.select_many(['opaque-b'], expected_generation=self.manager.scope_generation)
        with patch.object(web, 'prepare_session', side_effect=ValueError('SYNTHETIC_FAILURE')):
            self.manager.tick()
        self.assertEqual(self.manager.stage, 'error')
        self.assertIsNone(self.manager.provider)
        self.assertEqual((self.manager.directory / 'active-scope.json').read_bytes(), pointer)
        self.assertEqual((data / 'private/synthetic-checkpoint.json').read_bytes(), marker)
        self.manager.restore_profile(original, expected_generation=self.manager.scope_generation)
        self.manager.tick()
        self.assertEqual(self.manager.data_dir, data)
        self.assertEqual(self.manager.stage, 'running')

    def test_changed_legacy_pair_creates_new_scope_without_mutating_original_legacy_store(self):
        self.manager.select('opaque-a', 'opaque-c', expected_generation=self.manager.scope_generation)
        self.manager.tick()
        self.assertEqual(self.manager.active_scope_id, 'legacy')
        legacy_data = self.manager.data_dir
        before = (legacy_data / 'private/scope.json').read_bytes()
        self.ready()
        self.manager.select('opaque-b', 'opaque-d', expected_generation=self.manager.scope_generation)
        self.manager.tick()
        self.assertNotEqual(self.manager.active_scope_id, 'legacy')
        self.assertNotEqual(self.manager.data_dir, legacy_data)
        self.assertEqual({row['chat_id'] for row in self.manager.active_config['conversations']},
                         {self.choices['opaque-b'], self.choices['opaque-d']})
        self.assertEqual((legacy_data / 'private/scope.json').read_bytes(), before)

    def test_profile_manifest_and_persisted_scope_must_agree_before_activation(self):
        original = self.select(['opaque-a'])
        path = self.manager.data_dir / 'private/scope.json'
        changed = read_private_json(path)
        changed['config'] = {'conversations': normalize_conversations({'conversations': [
            {'alias': alias_for(self.choices['opaque-b']), 'chat_id': self.choices['opaque-b']}]})}
        web.atomic_private(path, changed)
        self.select(['opaque-c'])
        self.manager.restore_profile(original, expected_generation=self.manager.scope_generation)
        self.manager.tick()
        self.assertEqual(self.manager.stage, 'error')
        self.assertIsNone(self.manager.provider)

    def test_nonce_guard_on_http_and_new_selection_payload(self):
        static = self.project / 'apps/web'; static.mkdir(parents=True)
        (static / 'index.html').write_text('SYNTHETIC_ONLY')
        server = web.make_macos_server(self.manager, static)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True);worker.start()
        cookie = None
        def request(method, path, value=None, generation=None, bootstrap=False):
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            headers = {}
            if cookie:headers['Cookie'] = cookie
            if generation:headers['X-ChatLens-Scope'] = generation
            if bootstrap:headers['Authorization'] = 'Bearer ' + server.token
            raw = None if value is None else json.dumps(value).encode()
            if raw is not None:headers.update({'Content-Type': 'application/json', 'Origin': server.base_url})
            connection.request(method,path,body=raw,headers=headers)
            response=connection.getresponse();result=response.status,dict(response.getheaders()),json.loads(response.read());connection.close();return result
        try:
            code,headers,session=request('GET','/api/session',bootstrap=True)
            self.assertEqual(code,200);cookie=headers['Set-Cookie'].split(';',1)[0]
            old=session['scope_generation']
            self.assertEqual(request('GET','/api/setup')[0],200)
            self.assertEqual(request('GET','/api/conversations')[0],409)
            self.assertEqual(request('POST','/api/setup/select',{'conversation_ids':['opaque-a']})[0],409)
            code,_,selected=request('POST','/api/setup/select',{'conversation_ids':['opaque-a','opaque-c'],'scope_generation':old},generation=old)
            self.assertEqual(code,200)
            self.assertNotEqual(selected['scope_generation'],old)
            self.assertEqual(request('GET','/api/setup/conversations',generation=old)[0],409)
            self.assertEqual(request('POST','/api/setup/profile',{'profile_id':'legacy'},generation=old)[0],409)
            self.assertEqual(len(self.prepared),0)
        finally:server.shutdown();worker.join(timeout=2);server.server_close()


if __name__ == '__main__':
    unittest.main()
