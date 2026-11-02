"""Synthetic Mac onboarding manager and authenticated queue-only HTTP actions."""
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import macos_web as web
from macos_active_account import ActiveAccount, ActiveAccountError
import macos_collector
from collector_private import read_private_json, write_private_json_new


PRIVATE_CHAT = 'SYNTHETIC_RAW_PRIVATE_ID'
GROUP_CHAT = 'SYNTHETIC_RAW_GROUP_ID@chatroom'
OPAQUE_PRIVATE = 'synthetic_opaque_private'
OPAQUE_GROUP = 'synthetic_opaque_group'


class Initializer:
    def __init__(self, account, directory, *, project_root):
        self.account, self.directory, self.project_root = account, directory, project_root
        self.key_file = directory / 'SYNTHETIC_PRIVATE_KEY_MAP.json'
        self.state, self.phase, self.action = 'idle', 'preparing', 'none'
        self.elapsed_seconds = 2
        self.starts = self.steps = self.closes = 0

    def start(self):
        self.starts += 1
        self.state = 'working'

    def step(self):
        self.steps += 1

    def close(self):
        self.closes += 1


class Collector:
    def __init__(self, session, native):
        self.session, self.native = session, native
        self.polls, self.closes = 0, []

    def poll(self):
        self.polls += 1

    def close(self, **options):
        self.closes.append(options)


class MacWebManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-macos-manager-')
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root / 'synthetic-project'
        self.project.mkdir()
        self.workspace = self.root / 'synthetic-workspace'
        self.workspace.mkdir(mode=0o700)
        self.probe = self.root / 'synthetic-probe'
        self.probe.write_bytes(b'SYNTHETIC_NOT_EXECUTED')
        first = self.root / 'synthetic-account-a/db_storage'
        second = self.root / 'synthetic-account-b/db_storage'
        first.mkdir(parents=True); second.mkdir(parents=True)
        self.first = ActiveAccount(first, 100, 1700000000.0, '4.1.15')
        self.second = ActiveAccount(second, 200, 1700000010.0, '4.1.15')
        self.current = self.first
        self.initializers = []
        self.manager = self.new_manager()

    def tearDown(self):
        self.manager.close()
        self.tmp.cleanup()

    def detector(self, probe):
        self.assertEqual(probe, self.probe)
        return self.current

    def factory(self, *arguments, **options):
        value = Initializer(*arguments, **options)
        self.initializers.append(value)
        return value

    def new_manager(self, **options):
        return web.MacWebManager(self.project, self.workspace, self.probe,
                                 detector=options.get('detector', self.detector),
                                 initializer_factory=options.get('factory', self.factory))

    def contacts(self):
        self.manager.contacts = [{'id': OPAQUE_PRIVATE, 'name': '合成客户', 'type': 'private'},
                                 {'id': OPAQUE_GROUP, 'name': '合成工作群', 'type': 'group'}]
        self.manager.catalog = {OPAQUE_PRIVATE: {'chat_id': PRIVATE_CHAT, 'type': 'private'},
                                OPAQUE_GROUP: {'chat_id': GROUP_CHAT, 'type': 'group'}}
        self.manager.stage, self.manager.reason = 'ready_to_select', None

    def ready_to_select(self):
        self.manager.start()
        self.manager.key_file = self.manager.directory / 'SYNTHETIC_PRIVATE_KEY_MAP.json'
        self.contacts()

    def assert_public(self, document):
        raw = json.dumps(document, ensure_ascii=False)
        for value in (str(self.root), str(self.current.db_root), 'SYNTHETIC_PRIVATE_KEY_MAP',
                      PRIVATE_CHAT, GROUP_CHAT, 'SYNTHETIC_SECRET', 'launch_url', '#token='):
            self.assertNotIn(value, raw)

    @contextmanager
    def http(self):
        static = self.project / 'apps/web'
        static.mkdir(parents=True, exist_ok=True)
        (static / 'index.html').write_bytes(b'<!doctype html><title>Synthetic manager</title>')
        server = web.make_macos_server(self.manager, static)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
        worker.start()
        def request(method, route, value=None, *, headers=None):
            connection = http.client.HTTPConnection(*server.server_address, timeout=2)
            extra = {} if headers is None else dict(headers)
            if value is not None:
                raw = json.dumps(value).encode()
                extra.update({'Content-Type': 'application/json', 'Origin': server.base_url})
            else:
                raw = None
            connection.request(method, route, body=raw, headers=extra)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), json.loads(response.read())
            connection.close()
            return result
        try:
            status, headers, session = request('GET', '/api/session', headers={'Authorization': 'Bearer ' + server.token})
            self.assertEqual(status, 200)
            cookie = headers['Set-Cookie'].split(';', 1)[0]
            scope_generation = session.get('scope_generation')
            def authenticated(method, route, value=None):
                nonlocal scope_generation
                headers = {'Cookie': cookie}
                if scope_generation:
                    headers['X-ChatLens-Scope'] = scope_generation
                result = request(method, route, value, headers=headers)
                scope_generation = result[2].get('scope_generation', scope_generation)
                return result
            yield authenticated
        finally:
            server.shutdown(); worker.join(timeout=2); server.server_close()

    def test_start_only_identifies_current_account_and_never_collects_without_scope(self):
        forbidden = mock.Mock(side_effect=AssertionError('scope must be selected before source collection'))
        with mock.patch.object(web, 'prepare_session', forbidden), \
                mock.patch.object(web, 'MacCollector', forbidden), \
                mock.patch.object(self.manager, '_load_contacts', forbidden):
            self.manager.start()
            for _ in range(3):
                self.manager.tick()
        forbidden.assert_not_called()
        self.assertIsNone(self.manager.collector)
        self.assertIsNone(self.manager.pending_session)
        self.assertEqual(self.manager.list_conversations(), {'conversations': [], 'selected_conversations': [], 'has_more': False})
        self.assertEqual(self.manager.setup()['stage'], 'needs_initialization')
        self.assertTrue(self.manager.setup()['account']['detected'])
        self.assert_public(self.manager.setup())

    def test_search_seed_keeps_already_selected_conversations_outside_query(self):
        self.ready_to_select()
        self.manager.contacts[1]['selected'] = True
        self.manager.contacts[0]['selected'] = False
        result = self.manager.list_conversations('客户')
        self.assertEqual([row['id'] for row in result['conversations']], [OPAQUE_PRIVATE])
        self.assertEqual([row['id'] for row in result['selected_conversations']], [OPAQUE_GROUP])
        self.assert_public(result)

    def test_http_initialize_only_enqueues_and_duplicate_clicks_do_not_initialize_twice(self):
        self.manager.start()
        with self.http() as request, mock.patch.object(self.manager, '_load_contacts') as contacts:
            for _ in range(2):
                status, _, document = request('POST', '/api/setup/initialize', {})
                self.assertEqual(status, 200)
                self.assert_public(document)
            self.assertEqual(self.initializers, [])
            contacts.assert_not_called()
            self.manager.tick()
            self.assertEqual(len(self.initializers), 1)
            self.assertEqual(self.initializers[0].starts, 1)
            self.assertEqual(self.initializers[0].steps, 1)
            request('POST', '/api/setup/initialize', {})
            self.manager.tick()
            self.assertEqual(len(self.initializers), 1)
            contacts.assert_not_called()

    def test_http_select_only_enqueues_opaque_tokens_until_owner_tick(self):
        self.ready_to_select()
        forbidden = mock.Mock(side_effect=AssertionError('HTTP thread cannot prepare or collect'))
        with self.http() as request, mock.patch.object(web, 'prepare_session', forbidden), \
                mock.patch.object(web, 'MacCollector', forbidden), \
                mock.patch.object(self.manager, '_load_contacts', forbidden):
            status, _, document = request('POST', '/api/setup/select',
                                           {'private_id': OPAQUE_PRIVATE, 'group_id': OPAQUE_GROUP})
            self.assertEqual(status, 200)
            self.assertEqual(document['stage'], 'starting')
            self.assert_public(document)
            forbidden.assert_not_called()
        self.assertIsNone(self.manager.pending_session)
        self.assertFalse(self.manager.data_dir.exists())
        command, value = self.manager.commands.get_nowait()
        self.assertEqual((command, value), ('select', (PRIVATE_CHAT, GROUP_CHAT)))

    def test_selection_rejects_raw_ids_wrong_types_unknown_or_stale_tokens(self):
        self.ready_to_select()
        for private, group in ((PRIVATE_CHAT, GROUP_CHAT), (OPAQUE_GROUP, OPAQUE_PRIVATE),
                               (OPAQUE_PRIVATE, OPAQUE_PRIVATE), ('unknown', OPAQUE_GROUP)):
            with self.subTest(kind=(private == PRIVATE_CHAT, group == OPAQUE_PRIVATE)):
                with self.assertRaisesRegex(ValueError, 'macos_selection_invalid'):
                    self.manager.select(private, group)
                self.assertEqual(self.manager.stage, 'ready_to_select')
                self.assertFalse(self.manager.queued)
        self.manager.select(OPAQUE_PRIVATE, OPAQUE_GROUP)
        with self.assertRaisesRegex(ValueError, 'macos_selection_not_ready'):
            self.manager.select(OPAQUE_PRIVATE, OPAQUE_GROUP)

    def test_initializer_ready_loads_contacts_on_owner_thread_and_preserves_no_scope_boundary(self):
        self.manager.start()
        self.manager.request_initialize()
        self.manager.tick()
        initializer = self.initializers[0]
        initializer.state = 'ready'
        write_private_json_new(initializer.key_file, {'synthetic_only': 'SYNTHETIC_SECRET'})
        forbidden = mock.Mock(side_effect=AssertionError('contact readiness does not start collection'))
        with mock.patch.object(self.manager, '_load_contacts', side_effect=self.contacts) as load, \
                mock.patch.object(web, 'prepare_session', forbidden), mock.patch.object(web, 'MacCollector', forbidden):
            self.manager.tick()
        load.assert_called_once_with()
        forbidden.assert_not_called()
        self.assertEqual(self.manager.key_file, initializer.key_file)
        self.assertEqual(self.manager.setup()['stage'], 'ready_to_select')
        self.assertEqual(len(self.manager.list_conversations()['conversations']), 2)
        self.assert_public(self.manager.setup())
        reader = read_private_json(self.manager.directory / 'reader.json')
        self.assertEqual(reader['account_identity'], web.account_identity(self.first))

    def test_new_account_namespace_does_not_reuse_other_accounts_reader_or_selection(self):
        self.ready_to_select()
        directory_a = self.manager.directory
        write_private_json_new(directory_a / 'reader.json',
                               {'account_identity': web.account_identity(self.first),
                                'key_file': str(self.manager.key_file)})
        self.current = self.second
        second = self.new_manager()
        forbidden = mock.Mock(side_effect=AssertionError('new account must not reuse old sources'))
        try:
            with mock.patch.object(second, '_load_contacts', forbidden), mock.patch.object(web, 'prepare_session', forbidden):
                second.start()
            forbidden.assert_not_called()
            self.assertNotEqual(second.directory, directory_a)
            self.assertEqual(second.stage, 'needs_initialization')
            self.assertIsNone(second.key_file)
            self.assertEqual(second.catalog, {})
            self.assertEqual(second.contacts, [])
        finally:
            second.close()

    def test_old_selected_checkpoint_is_never_opened_or_modified(self):
        old = self.project / 'artifacts/private/wechat-selected/2026-10-27/state.json'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'SYNTHETIC_OLD_CONTROLLED_CHECKPOINT_MUST_STAY_UNREAD')
        before, modified = old.read_bytes(), old.stat().st_mtime_ns
        real_open = Path.open
        def safe_open(path, *args, **options):
            if path == old:
                raise AssertionError('old token-only checkpoint is not a new collector store')
            return real_open(path, *args, **options)
        with mock.patch.object(Path, 'open', safe_open):
            self.manager.start()
            self.manager.request_initialize()
            self.manager.tick()
        self.assertEqual(old.read_bytes(), before)
        self.assertEqual(old.stat().st_mtime_ns, modified)
        self.assertNotIn('wechat-selected', str(self.manager.directory))

    def test_start_error_still_returns_sanitized_setup_to_http(self):
        self.manager.start()
        self.manager.data_dir.mkdir(mode=0o700)
        second = self.new_manager()
        try:
            with mock.patch.object(web, 'prepare_session', side_effect=ValueError('SYNTHETIC_SECRET_ROOT_KEY')):
                second.start()
            self.manager = second
            self.assertEqual(second.setup()['stage'], 'error')
            self.assert_public(second.setup())
            with self.http() as request:
                status, _, document = request('GET', '/api/setup')
            self.assertEqual(status, 200)
            self.assertEqual(document['stage'], 'error')
            self.assert_public(document)
        finally:
            second.close()

    def test_no_current_account_shows_setup_and_retries_detection_without_source_access(self):
        def missing(_):
            raise OSError('SYNTHETIC_SECRET_ACCOUNT_PATH')
        self.manager = self.new_manager(detector=missing)
        self.manager.start()
        self.assertFalse(self.manager.setup()['account']['detected'])
        self.manager.request_initialize()
        self.assertFalse(self.manager.queued)
        self.assert_public(self.manager.setup())
        self.manager.detector = self.detector
        self.manager.last_detection = 0
        self.manager.tick()
        self.assertTrue(self.manager.setup()['account']['detected'])
        self.assertEqual(self.manager.stage, 'needs_initialization')
        self.assertEqual(self.initializers, [])

    def test_current_service_metadata_failure_is_not_reported_as_logged_out(self):
        failure = ActiveAccountError('macos_active_process_metadata_invalid',
                                     wechat_running=True, metadata_issue='client_version')
        self.manager = self.new_manager(detector=mock.Mock(side_effect=failure))
        self.manager.start()
        with self.http() as request:
            status, _, document = request('GET', '/api/setup')
        self.assertEqual(status, 200)
        self.assertEqual(document['reason'], 'macos_active_process_metadata_invalid')
        self.assertFalse(document['account']['detected'])
        self.assertIs(document['checks']['wechat_running'], True)
        self.assertFalse(document['checks']['access_ready'])
        self.assertEqual(document['detection']['metadata_issue'], 'client_version')
        self.assertIn('无法核验客户端版本', document['initialization']['description'])
        self.assertNotIn('请先登录', document['initialization']['description'])
        self.assert_public(document)

    def test_detection_reports_permission_multiple_roots_absent_and_unknown_separately(self):
        cases = [
            (ActiveAccountError('macos_active_process_not_running', wechat_running=False), False, '打开微信'),
            (ActiveAccountError('macos_active_account_ambiguous', wechat_running=True), True, '多个账号'),
            (ActiveAccountError('macos_active_path_unavailable', wechat_running=True), True, '完全磁盘访问权限'),
            (ActiveAccountError('macos_active_account_not_identified', wechat_running=True), True, '暂时无法'),
            (OSError('SYNTHETIC_SECRET_ACCOUNT_PATH'), None, '暂时无法确认'),
            (ActiveAccountError('SYNTHETIC_SECRET_UNKNOWN', wechat_running=True), None, '暂时无法确认'),
        ]
        for failure, running, description in cases:
            with self.subTest(code=getattr(failure, 'code', None)):
                manager = self.new_manager(detector=mock.Mock(side_effect=failure))
                try:
                    manager.start()
                    result = manager.setup()
                    self.assertIs(result['checks']['wechat_running'], running)
                    self.assertIn(description, result['initialization']['description'])
                    expected = failure.code if running is not None else 'macos_active_account_not_ready'
                    self.assertEqual(result['reason'], expected)
                    self.assertFalse(result['account']['detected'])
                    self.assert_public(result)
                finally:
                    manager.close()

    def test_detection_failure_retries_after_three_seconds_and_recovers_current_account(self):
        detector = mock.Mock(side_effect=[ActiveAccountError('macos_active_account_ambiguous', wechat_running=True),
                                          self.second])
        self.manager = self.new_manager(detector=detector)
        self.manager.start()
        self.manager.last_detection = 100
        with mock.patch.object(web.time, 'monotonic', return_value=102.99):
            self.manager.tick()
        self.assertEqual(detector.call_count, 1)
        with mock.patch.object(web.time, 'monotonic', return_value=103.01):
            self.manager.tick()
        self.assertEqual(detector.call_count, 2)
        result = self.manager.setup()
        self.assertTrue(result['account']['detected'])
        self.assertIs(result['checks']['wechat_running'], True)
        self.assertIsNone(result['reason'])
        self.assertIsNone(result['detection']['metadata_issue'])
        self.assertEqual(self.manager.account, self.second)
        self.assertIsNone(self.manager.key_file)
        self.assertEqual(self.initializers, [])
        self.assert_public(result)

    def test_failed_initializer_factory_clears_queue_so_a_retry_can_progress(self):
        self.manager.start()
        self.manager.initializer_factory = mock.Mock(side_effect=ValueError('SYNTHETIC_SECRET_FACTORY_PATH'))
        self.manager.request_initialize()
        self.manager.tick()
        self.assertEqual(self.manager.stage, 'error')
        self.assertFalse(self.manager.queued)
        self.assert_public(self.manager.setup())
        self.manager.initializer_factory = self.factory
        self.manager.request_initialize()
        self.manager.tick()
        self.assertEqual(len(self.initializers), 1)
        self.assertEqual(self.initializers[0].steps, 1)
        self.assertEqual(self.manager.stage, 'needs_initialization')

    def test_failed_initialization_can_be_retried_and_new_initializer_is_stepped(self):
        self.manager.start()
        self.manager.request_initialize()
        self.manager.tick()
        self.initializers[0].state = 'error'
        self.manager.tick()
        self.assertEqual(self.manager.stage, 'error')
        self.manager.request_initialize()
        self.manager.tick()
        self.assertEqual(len(self.initializers), 2)
        self.assertEqual(self.initializers[1].starts, 1)
        self.assertEqual(self.initializers[1].steps, 1)
        self.assertEqual(self.manager.stage, 'needs_initialization')

    def test_owner_select_prepares_only_chosen_scope_then_starts_new_collector_after_native_gate(self):
        self.ready_to_select()
        self.manager.select(OPAQUE_PRIVATE, OPAQUE_GROUP)
        sessions, collectors = [], []
        def prepare(data_dir, **options):
            self.assertEqual(data_dir, self.manager.data_dir)
            self.assertEqual(options['db_root'], self.first.db_root)
            self.assertEqual(options['key_file'], self.manager.key_file)
            selected = read_private_json(options['selected_config'])
            self.assertEqual(selected, {'conversations': [
                {'alias': 'private_test', 'chat_id': PRIVATE_CHAT},
                {'alias': 'group_test', 'chat_id': GROUP_CHAT}]})
            session = SimpleNamespace(client_version=options['client_version'], config=selected)
            sessions.append(session)
            return session
        def create(session, native):
            collector = Collector(session, native)
            collectors.append(collector)
            return collector
        native = mock.Mock(return_value={'probe_succeeded': True, 'running': True,
                                         'frontmost': True, 'screen_locked': False})
        provider = SimpleNamespace(synthetic_only=True)
        with mock.patch.object(web, 'prepare_session', side_effect=prepare) as prepared, \
                mock.patch.object(web, 'MacNativeProbe', return_value=native), \
                mock.patch.object(web, 'MacCollector', side_effect=create), \
                mock.patch.object(web, 'CollectorWebData', return_value=provider):
            self.manager.tick()
            self.assertEqual(prepared.call_count, 1)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(collectors, [])
            self.assertEqual(self.manager.stage, 'starting')
            native.return_value['frontmost'] = False
            self.manager.tick()
        self.assertEqual(len(collectors), 1)
        self.assertIs(collectors[0].session, sessions[0])
        self.assertEqual(collectors[0].polls, 1)
        self.assertEqual(self.manager.stage, 'running')
        self.assertIs(self.manager.provider, provider)
        self.assert_public(self.manager.setup())

    def test_account_change_before_select_preparation_is_rejected_without_source_body_access(self):
        self.ready_to_select()
        self.manager.select(OPAQUE_PRIVATE, OPAQUE_GROUP)
        self.current = self.second
        forbidden = mock.Mock(side_effect=AssertionError('account switch cannot use selected old scope'))
        with mock.patch.object(web, 'prepare_session', forbidden), mock.patch.object(web, 'MacCollector', forbidden):
            self.manager.tick()
        forbidden.assert_not_called()
        self.assertEqual(self.manager.stage, 'needs_initialization')
        self.assertEqual(self.manager.account, self.second)
        self.assertIsNone(self.manager.key_file)
        self.assert_public(self.manager.setup())

    def test_contacts_are_metadata_only_and_public_rows_use_opaque_identifiers(self):
        self.manager.start()
        key = self.manager.directory / 'SYNTHETIC_PRIVATE_KEY_MAP.json'
        write_private_json_new(key, {'synthetic_only': 'SYNTHETIC_SECRET'})
        self.manager.key_file = key
        queries, opened = [], []
        class Connection:
            def __init__(self, kind):
                self.kind = kind
            def set_progress_handler(self, *_):
                pass
            def execute(self, sql):
                queries.append(sql)
                rows = ([('Msg_' + hashlib.md5(PRIVATE_CHAT.encode()).hexdigest(),),
                         ('Msg_' + hashlib.md5(GROUP_CHAT.encode()).hexdigest(),)] if self.kind == 'message' else
                        [(PRIVATE_CHAT, '合成客户', '合成昵称'), (GROUP_CHAT, '', '合成工作群'),
                         ('SYNTHETIC_NO_MESSAGE_TABLE', '无会话', ''), ('invalid username', '无效', '')])
                return SimpleNamespace(fetchall=lambda: rows)
            def close(self):
                pass
        def opening(driver, path, raw_key):
            opened.append(path)
            return Connection('message' if path.name == 'message_0.db' else 'contact')
        driver = ModuleType('sqlcipher3.dbapi2')
        package = ModuleType('sqlcipher3'); package.dbapi2 = driver; package.__path__ = []
        def keys(document, *, messages):
            return {'message/message_0.db' if messages else 'contact/contact.db': '0' * 64}
        with mock.patch.dict(sys.modules, {'sqlcipher3': package, 'sqlcipher3.dbapi2': driver}), \
                mock.patch.object(web, '_open', side_effect=opening), \
                mock.patch.object(macos_collector, '_keys', side_effect=keys), \
                mock.patch.object(macos_collector, '_authenticate_keys') as authenticate:
            self.manager._load_contacts()
        authenticate.assert_called_once()
        self.assertEqual(opened, [self.first.db_root / 'message/message_0.db', self.first.db_root / 'contact/contact.db'])
        self.assertTrue(all('message_content' not in sql.lower() and 'from msg_' not in sql.lower() for sql in queries))
        self.assertTrue(any('sqlite_master' in sql for sql in queries))
        self.assertTrue(any('FROM contact ' in sql for sql in queries))
        public = self.manager.list_conversations()
        self.assertEqual(len(public['conversations']), 2)
        self.assertEqual({row['type'] for row in public['conversations']}, {'private', 'group'})
        self.assertTrue(all(len(row['id']) == 24 for row in public['conversations']))
        self.assert_public(public)
        self.assertEqual({row['chat_id'] for row in self.manager.catalog.values()}, {PRIVATE_CHAT, GROUP_CHAT})
        self.assertEqual([row['name'] for row in self.manager.list_conversations('客户')['conversations']], ['合成客户'])


class RuntimeLockTests(unittest.TestCase):
    def test_only_one_own_runtime_can_lock_private_workspace(self):
        with tempfile.TemporaryDirectory(prefix='chatlens-synthetic-runtime-lock-') as temporary:
            root = Path(temporary).resolve()
            descriptor = web.acquire_runtime(root)
            try:
                with self.assertRaises(BlockingIOError):
                    web.acquire_runtime(root)
            finally:
                os.close(descriptor)
            descriptor = web.acquire_runtime(root)
            os.close(descriptor)

    def test_redirected_or_multiply_linked_lock_is_not_modified(self):
        with tempfile.TemporaryDirectory(prefix='chatlens-synthetic-runtime-lock-links-') as temporary:
            root = Path(temporary).resolve()
            other = root / 'other';other.write_bytes(b'SYNTHETIC_UNTOUCHED')
            other.chmod(0o600)
            target = root / '.runtime.lock'
            target.symlink_to(other)
            with self.assertRaises(OSError):
                web.acquire_runtime(root)
            target.unlink()
            os.link(other, target)
            with self.assertRaises(ValueError):
                web.acquire_runtime(root)
            self.assertEqual(other.read_bytes(), b'SYNTHETIC_UNTOUCHED')


if __name__ == '__main__':
    unittest.main()
