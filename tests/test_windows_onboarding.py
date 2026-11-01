"""First-use production coordinator with explicit synthetic native adapters.

Workspace files, manager, HTTP guards and stores are real local fixtures. No
installed WeChat, real process memory, account database or cloud is touched.
"""
from __future__ import annotations

import ctypes as C
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'scripts/windows-proof')]
import collector_service
from collector_private import ensure_private_dir, read_private_json, write_private_json_new
from collector_web_meta import WebMetadata
from continuous_store import CollectorStore
from conversation_scope import alias_for
from web_data import WebDataError
from windows_onboarding import WindowsOnboardingManager, _owned_process
from windows_onboarding_native import (OnboardingError, initialize_direct, mapped_roots,
                                       publish_json_once, validate_candidate, discover_direct)
from windows_workspace import Workspace
from windows_web_server import make_windows_server
from tests.test_windows_selected_runner import Memory, page, KEYS


class Child:
    def __init__(self, store):
        self.store, self.returncode = store, None
    def poll(self):
        return self.returncode


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-onboarding-')
        self.root = Path(self.temp.name).resolve()
        self.exe = self.root / 'Weixin.exe'
        self.exe.write_bytes(b'SYNTHETIC_NOT_EXECUTED')
        self.package = self.root / 'package'
        self.package.mkdir()
        self.output = self.root / 'results'
        self.output.mkdir()
        self.source = self.root / 'synthetic-one/db_storage'
        self.source.mkdir(parents=True)
        self.current = self.candidate(self.source)
        self.initializations, self.children, self.managers = [], [], []
        self.choices = [{'chat_id': 'synthetic-peer', 'name': '合成私聊', 'type': 'private'},
                        {'chat_id': 'synthetic-group@chatroom', 'name': '合成群聊', 'type': 'group'}]

    def tearDown(self):
        for manager in self.managers:
            manager.close()
        for child in self.children:
            if child.returncode is None:
                child.store.close()
                child.returncode = 0
        self.temp.cleanup()

    def candidate(self, source):
        return {'state': 'ready', 'reason': 'windows_onboarding_account_detected',
                'pid': 123, 'creation_time': 456, 'exe_path': str(self.exe), 'db_root': str(source)}

    def documents(self, candidate, target, *, selected=False):
        target = ensure_private_dir(target, create=True)
        private = ensure_private_dir(target / 'private', create=True)
        ensure_private_dir(private / 'store', create=True)
        scope = {'schema_version': 1, 'session_id': 'a' * 32,
                 'scope_kind': 'selected_conversations' if selected else 'unselected_reader',
                 'db_root': candidate['db_root'], 'exe_path': candidate['exe_path'],
                 'config': {'conversations': [{'alias': alias_for('synthetic-peer'), 'chat_id': 'synthetic-peer'}]
                            if selected else []}, 'test_tokens': {}}
        keys = {'schema_version': 1, 'db_root': candidate['db_root'], 'exe_path': candidate['exe_path'],
                'keys': {relative: {'enc_key': '01' * 32} for relative in collector_service.RELATIVES}}
        write_private_json_new(private / 'scope.json', scope)
        write_private_json_new(private / 'keys.json', keys)
        return scope, keys

    def initialize(self, candidate, target, cancel, progress):
        self.initializations.append(candidate['db_root'])
        progress('validating_keys')
        self.documents(candidate, target)
        return {'state': 'ready', 'reason': 'windows_onboarding_reader_initialized'}

    def start_child(self, target, output):
        output.mkdir()
        scope, _, _, _, _, binding = collector_service.prepare_session(target)
        store = CollectorStore(target / 'private/store', binding)
        if not store.resumed:
            cursors = {'message/message_0.db': {row['alias']: {'local_id': 0, 'present': True}
                                               for row in scope['config']['conversations']}}
            store.initialize(cursors, {})
        metadata = WebMetadata(target / 'private', store.binding_digest, '2026-11-01T00:00:00+00:00')
        metadata.ensure_catalog(scope['config'])
        metadata.status('collecting', store.stats()['total_messages'], 0, store.resumed)
        collector_service.Heartbeat(output).emit('collecting', store.stats()['total_messages'], 0, store.resumed)
        child = Child(store)
        self.children.append(child)
        return child

    @staticmethod
    def stop_child(child, output):
        child.store.close()
        child.returncode = 0
        return 0, True

    def manager(self, *, workspace=None, **options):
        workspace = workspace or Workspace(self.root / 'workspace', self.package,
                                           fingerprint='a' * 64, discovery_roots=[self.package])
        defaults = {'workspace': workspace,
                    'discoverer': lambda cancel, progress: dict(self.current),
                    'initializer': self.initialize,
                    'start_collector': self.start_child, 'stop_collector': self.stop_child,
                    'process_detector': lambda: {'state': 'ready', 'pid': 123, 'creation_time': 456,
                                                 'reason': 'windows_process_candidate_ready'},
                    'account_probe_factory': lambda pid, birth, exe, root: lambda: {
                        'state': 'matched' if root == self.current.get('db_root') else 'mismatch',
                        'matched': root == self.current.get('db_root'),
                        'reason': 'windows_account_mapped_database_matched' if root == self.current.get('db_root')
                                  else 'windows_account_changed_or_ambiguous'},
                    'catalog_loader': lambda job: list(self.choices)}
        defaults.update(options)
        manager = WindowsOnboardingManager(workspace.root, self.package, self.output, **defaults)
        self.managers.append(manager)
        manager.start()
        self.settle(manager)
        return manager

    def settle(self, manager):
        deadline = time.monotonic() + 3
        while manager._task is not None and time.monotonic() < deadline:
            manager._task.thread.join(timeout=.02)
            manager.tick()
        self.assertIsNone(manager._task, 'synthetic task must complete')

    def ready(self, manager):
        manager.request_initialize(expected_generation=manager.scope_generation)
        self.settle(manager)
        self.assertEqual(manager.setup()['onboarding']['state'], 'ready')
        return manager

    def select(self, manager):
        manager.request_catalog(expected_generation=manager.scope_generation)
        manager.tick()
        job = manager._delegate.catalog_job
        if job is not None:
            job.thread.join(timeout=2)
        manager.tick()
        # The workspace picker is search-first; it never dumps the catalog on
        # an empty query. Explicit search returns the matching opaque IDs.
        rows = manager.list_conversations('合成', expected_generation=manager.scope_generation)['conversations']
        manager.select_many([row['id'] for row in rows], expected_generation=manager.scope_generation)
        manager.tick()

    def test_new_workspace_does_not_initialize_or_collect_without_explicit_click(self):
        manager = self.manager()
        setup = manager.setup()
        self.assertEqual(setup['onboarding']['state'], 'needs_initialization')
        self.assertTrue(setup['onboarding']['can_initialize'])
        self.assertFalse(setup['connection']['can_read_saved'])
        self.assertEqual(self.initializations, [])
        self.assertEqual(self.children, [])
        self.assertFalse((manager.workspace.private / 'accounts').exists())

    def test_initialize_empty_reader_then_explicit_selection_creates_first_blank_baseline(self):
        manager = self.manager()
        generation = manager.scope_generation
        self.ready(manager)
        self.assertNotEqual(generation, manager.scope_generation)
        setup = manager.setup()
        self.assertEqual(setup['stage'], 'ready_to_select')
        self.assertTrue(setup['connection']['can_modify_scope'])
        self.assertFalse(setup['connection']['can_start'])
        self.assertEqual(self.children, [])
        with self.assertRaisesRegex(collector_service.CollectorError, 'collector_explicit_selection_required'):
            collector_service.prepare_session(manager._delegate.legacy_data)
        self.select(manager)
        self.assertEqual(manager.setup()['stage'], 'running')
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.children[0].store.stats()['total_messages'], 0)
        self.assertEqual(len(manager.conversations()['conversations']), 2)
        self.assertTrue(manager._delegate.search_first)

    def test_relaunch_reuses_account_config_and_selected_scope_without_second_initialization(self):
        manager = self.ready(self.manager())
        self.select(manager)
        directory = manager._delegate.legacy_data
        scope_before = (directory / 'private/scope.json').read_bytes()
        keys_before = (directory / 'private/keys.json').read_bytes()
        selected = manager._delegate.active_scope_id
        manager.close()
        replacement = self.manager()
        self.assertEqual(replacement.setup()['onboarding']['mode'], 'reused')
        self.assertEqual(replacement._delegate.active_scope_id, selected)
        self.assertEqual(self.initializations, [str(self.source)])
        self.assertEqual((directory / 'private/scope.json').read_bytes(), scope_before)
        self.assertEqual((directory / 'private/keys.json').read_bytes(), keys_before)
        self.assertFalse(replacement.setup()['connection']['collector_running'])

    def test_account_change_clears_old_domain_and_new_account_does_not_copy_model_or_history(self):
        manager = self.ready(self.manager())
        self.select(manager)
        old = manager.scope_generation
        private = manager._delegate._assistant_directory()
        ensure_private_dir(private, create=True)
        write_private_json_new(private / 'provider.json', {'synthetic_old_account': True})
        write_private_json_new(private / 'threads.json', {'synthetic_old_history': True})
        other_source = self.root / 'synthetic-two/db_storage'
        other_source.mkdir(parents=True)
        self.current = self.candidate(other_source)
        manager._delegate.last_process_at = float('-inf')
        manager.tick()
        self.settle(manager)
        self.assertNotEqual(manager.scope_generation, old)
        self.assertEqual(manager.setup()['onboarding']['state'], 'needs_initialization')
        with self.assertRaisesRegex(WebDataError, 'scope_changed'):
            manager.messages(alias_for('synthetic-peer'), expected_generation=old)
        with self.assertRaises(WebDataError):
            manager.conversations(expected_generation=manager.scope_generation)
        self.ready(manager)
        self.assertFalse((manager._delegate.private / 'assistant/provider.json').exists())
        self.assertFalse((manager._delegate.private / 'assistant/threads.json').exists())
        self.assertTrue((private / 'provider.json').exists())
        self.assertEqual(len(self.initializations), 2)

    def test_account_changed_between_page_and_initialize_is_not_scanned_or_registered(self):
        manager = self.manager()
        other = self.root / 'synthetic-two/db_storage'
        other.mkdir(parents=True)
        self.current = self.candidate(other)
        manager.request_initialize(expected_generation=manager.scope_generation)
        self.settle(manager)
        self.assertEqual(self.initializations, [])
        self.assertEqual(manager.setup()['onboarding']['reason'], 'windows_onboarding_account_changed')
        self.assertEqual(manager.workspace._read()['accounts'], {})

    def test_recheck_of_uninitialized_account_rotates_scope_before_detecting_another_account(self):
        manager = self.manager()
        previous = manager.scope_generation
        self.assertEqual(manager.setup()['onboarding']['state'], 'needs_initialization')
        other = self.root / 'synthetic-two/db_storage'
        other.mkdir(parents=True)
        self.current = self.candidate(other)
        response = manager.request_recheck(expected_generation=previous)
        self.assertNotEqual(response['scope_generation'], previous)
        self.settle(manager)
        self.assertEqual(manager.setup()['onboarding']['state'], 'needs_initialization')
        self.assertEqual(manager._candidate['db_root'], str(other))
        with self.assertRaisesRegex(WebDataError, 'scope_changed'):
            manager.request_initialize(expected_generation=previous)
        self.assertEqual(self.initializations, [])
        self.assertEqual(manager.workspace._read()['accounts'], {})
        self.ready(manager)
        self.assertEqual(self.initializations, [str(other)])

    def test_cancel_does_not_commit_late_result_or_start_parallel_initializer(self):
        started, release = threading.Event(), threading.Event()
        def initialize(candidate, target, cancel, progress):
            started.set()
            release.wait(2)
            if cancel.is_set():
                raise OnboardingError('windows_onboarding_cancelled')
            self.initialize(candidate, target, cancel, progress)
        manager = self.manager(initializer=initialize)
        generation = manager.scope_generation
        manager.request_initialize(expected_generation=generation)
        self.assertTrue(started.wait(1))
        self.assertTrue(manager.setup()['onboarding']['can_cancel'])
        manager.request_cancel(expected_generation=generation)
        self.assertNotEqual(manager.scope_generation, generation)
        self.assertFalse(manager.setup()['onboarding']['can_initialize'])
        with self.assertRaises(WebDataError):
            manager.request_initialize(expected_generation=manager.scope_generation)
        release.set()
        self.settle(manager)
        self.assertEqual(manager.workspace._read()['accounts'], {})
        self.assertEqual(self.children, [])
        self.assertTrue(manager.setup()['onboarding']['can_initialize'])

    def test_waiting_and_ambiguous_accounts_never_initialize(self):
        for state, reason in [('waiting_for_wechat', 'windows_process_not_running'),
                              ('ambiguous', 'windows_onboarding_account_ambiguous')]:
            with self.subTest(state=state):
                self.current = {'state': state, 'reason': reason}
                manager = self.manager()
                self.assertEqual(manager.setup()['onboarding']['state'], state)
                self.assertFalse(manager.setup()['onboarding']['can_initialize'])
                self.assertFalse(manager.setup()['connection']['checks']['account_verified'])
                manager.close()
        self.assertEqual(self.initializations, [])

    def test_explicit_import_is_local_once_and_reuses_matching_account_only(self):
        (self.root / 'manual').mkdir()
        legacy = self.root / 'manual/collector-data'
        self.documents(self.current, legacy)
        imported = []
        manager = self.manager(importer=lambda cancel, progress: imported.append(True) or legacy)
        manager.request_import(expected_generation=manager.scope_generation)
        self.settle(manager)
        self.assertEqual(imported, [True])
        self.assertEqual(manager.setup()['onboarding']['state'], 'ready')
        self.assertEqual(manager._delegate.legacy_data, legacy)
        self.assertEqual(self.initializations, [])

    def test_diagnostics_never_contains_paths_candidate_ids_or_keys(self):
        manager = self.manager()
        document = manager.connection_diagnostics(expected_generation=manager.scope_generation)
        text = json.dumps(document)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn('exe_path', text)
        self.assertNotIn('db_root', text)
        self.assertNotIn('enc_key', text)
        self.assertNotIn('creation_time', text)
        self.assertEqual(document['selected_conversation_count'], 0)

    def test_process_found_without_account_mapping_is_not_reported_as_logged_in_or_absent(self):
        self.current = {'state': 'waiting_for_wechat', 'reason': 'windows_onboarding_account_not_observed',
                        'process_running': True}
        manager = self.manager()
        setup = manager.setup()
        self.assertTrue(setup['connection']['checks']['process_running'])
        self.assertFalse(setup['connection']['checks']['account_verified'])
        self.assertEqual(setup['connection']['state'], 'account_unverified')
        self.assertFalse(setup['onboarding']['can_initialize'])
        self.assertIn('已找到微信程序', setup['onboarding']['description'])

    def test_unsupported_version_never_exposes_initialize_or_assumes_login_failure(self):
        self.current = {'state': 'waiting_for_wechat', 'reason': 'windows_process_version_unsupported',
                        'process_running': True}
        manager = self.manager()
        self.assertEqual(manager.setup()['onboarding']['state'], 'error')
        self.assertIn('4.1.15.56', manager.setup()['onboarding']['description'])
        with self.assertRaises(WebDataError):
            manager.request_initialize(expected_generation=manager.scope_generation)
        self.assertEqual(self.initializations, [])

    def test_new_executable_is_rediscovered_after_original_process_disappears(self):
        from windows_web_manager import WindowsWebManager
        def factory(data, package, output, **options):
            scope = read_private_json(Path(data) / 'private/scope.json')
            expected_exe = scope['exe_path']
            options['process_detector'] = lambda: (
                {'state': 'ready', 'pid': 123, 'creation_time': 456, 'reason': 'windows_process_candidate_ready'}
                if expected_exe == self.current['exe_path'] else
                {'state': 'not_running', 'pid': None, 'creation_time': None, 'reason': 'windows_process_not_running'})
            return WindowsWebManager(data, package, output, **options)
        manager = self.ready(self.manager(manager_factory=factory))
        old = manager.scope_generation
        old_directory = manager._delegate.legacy_data
        source = self.root / 'new-install-account/db_storage'; source.mkdir(parents=True)
        executable = self.root / 'NewInstall/Weixin.exe'; executable.parent.mkdir(); executable.write_bytes(b'SYNTHETIC')
        self.current = dict(self.candidate(source), exe_path=str(executable))
        manager._delegate.last_process_at = float('-inf')
        manager.tick(); self.settle(manager)
        self.assertEqual(manager.setup()['onboarding']['state'], 'needs_initialization')
        self.assertEqual(manager._candidate['exe_path'], str(executable))
        self.assertNotEqual(manager.scope_generation, old)
        self.assertTrue((old_directory / 'private/keys.json').exists())

    def test_cancel_during_import_metadata_never_registers_reference(self):
        (self.root / 'manual').mkdir()
        legacy = self.root / 'manual/collector-data'; self.documents(self.current, legacy)
        manager = self.manager(importer=lambda cancel, progress: legacy)
        entered, release = threading.Event(), threading.Event()
        original = manager.workspace.validate_import
        def validate(path):
            entered.set(); release.wait(2)
            return original(path)
        with patch.object(manager.workspace, 'validate_import', side_effect=validate):
            generation = manager.scope_generation
            manager.request_import(expected_generation=generation)
            self.assertTrue(entered.wait(1))
            manager.request_cancel(expected_generation=generation)
            release.set(); self.settle(manager)
        self.assertEqual(manager.workspace._read()['accounts'], {})
        self.assertIsNone(manager._delegate)

    def test_cancel_during_registry_commit_metadata_does_not_activate_or_register(self):
        manager = self.manager()
        manager.request_initialize(expected_generation=manager.scope_generation)
        manager._task.thread.join(timeout=2)
        entered, release = threading.Event(), threading.Event()
        original = manager.workspace.register_account
        def register(*args, **kwargs):
            entered.set(); release.wait(2)
            return original(*args, **kwargs)
        with patch.object(manager.workspace, 'register_account', side_effect=register):
            tick = threading.Thread(target=manager.tick)
            tick.start(); self.assertTrue(entered.wait(1))
            manager.request_cancel(expected_generation=manager.scope_generation)
            release.set(); tick.join(timeout=2)
            self.assertFalse(tick.is_alive())
        self.assertEqual(manager.workspace._read()['accounts'], {})
        self.assertIsNone(manager._delegate)

    def test_close_cancels_owned_task_and_releases_workspace_only_after_it_exits(self):
        entered = threading.Event()
        def discover(cancel, progress):
            entered.set()
            cancel.wait(2)
            return dict(self.current)
        workspace = Workspace(self.root / 'workspace', self.package, fingerprint='a' * 64, discovery_roots=[])
        manager = WindowsOnboardingManager(workspace.root, self.package, self.output,
                                          workspace=workspace, discoverer=discover)
        self.managers.append(manager)
        manager.start(); self.assertTrue(entered.wait(1))
        before = time.monotonic(); manager.close()
        self.assertLess(time.monotonic() - before, .5)
        self.assertFalse(manager._task.thread.is_alive())
        self.assertTrue(workspace.closing.is_set())
        self.assertIsNone(workspace.descriptor)
        self.assertFalse(workspace.registry.exists())

    def blocking_attach(self):
        from windows_web_manager import WindowsWebManager
        entered, release = threading.Event(), threading.Event()
        attached = []
        def factory(*args, **options):
            result = WindowsWebManager(*args, **options)
            original = result.start
            def start():
                original()
                entered.set()
                if not release.wait(2):
                    raise AssertionError('synthetic attach barrier was not released')
            result.start = start
            attached.append(result)
            return result
        manager = self.manager(manager_factory=factory)
        manager.request_initialize(expected_generation=manager.scope_generation)
        manager._task.thread.join(timeout=2)
        owner = threading.Thread(target=manager.tick)
        owner.start()
        self.assertTrue(entered.wait(1))
        return manager, owner, release, attached

    def test_cancel_at_attach_commit_boundary_cannot_publish_cancelled_domain(self):
        manager, owner, release, attached = self.blocking_attach()
        previous = manager.scope_generation
        manager.request_cancel(expected_generation=previous)
        release.set(); owner.join(timeout=2)
        self.assertFalse(owner.is_alive())
        self.assertIsNone(manager._delegate)
        self.assertTrue(attached[0].closed)
        self.assertNotEqual(previous, manager.scope_generation)
        self.assertEqual(manager.setup()['onboarding']['reason'], 'windows_onboarding_cancelled')
        self.assertTrue(manager._commit_done.is_set())

    def test_concurrent_close_keeps_workspace_lease_until_pending_attach_has_closed(self):
        manager, owner, release, attached = self.blocking_attach()
        errors = []
        def close():
            try:
                manager.close()
            except BaseException as error:
                errors.append(error)
        closer = threading.Thread(target=close)
        closer.start()
        self.assertTrue(manager.workspace.closing.wait(1))
        self.assertIsNotNone(manager.workspace.descriptor)
        self.assertTrue(closer.is_alive())
        self.assertFalse(manager._commit_done.is_set())
        release.set(); owner.join(timeout=2); closer.join(timeout=2)
        self.assertFalse(owner.is_alive())
        self.assertFalse(closer.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(attached[0].closed)
        self.assertIsNone(manager._delegate)
        self.assertTrue(manager._commit_done.is_set())
        self.assertIsNone(manager.workspace.descriptor)

    def test_http_initialize_and_cancel_require_current_scope_cookie_and_origin(self):
        manager = self.manager()
        server = make_windows_server(manager, ROOT / 'apps/web', token='SYNTHETIC_SESSION_TOKEN_12345678901234567890')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = http.client.HTTPConnection(host, port, timeout=2)
            connection.request('GET', '/api/session', headers={'Authorization': 'Bearer ' + server.token})
            response = connection.getresponse(); response.read()
            cookie = response.getheader('Set-Cookie').split(';', 1)[0]
            def post(route, **overrides):
                headers = {'Cookie': cookie, 'Content-Type': 'application/json', 'Origin': server.base_url,
                           'X-ChatLens-Scope': manager.scope_generation}
                headers.update(overrides)
                connection.request('POST', route, '{}', headers=headers)
                response = connection.getresponse()
                value = json.loads(response.read())
                return response.status, value
            self.assertEqual(post('/api/setup/initialize', Origin='http://127.0.0.1:1')[0], 403)
            self.assertEqual(post('/api/setup/initialize', **{'X-ChatLens-Scope': 'stale'})[0], 409)
            self.assertEqual(self.initializations, [])
            self.assertEqual(post('/api/setup/initialize')[0], 200)
            self.settle(manager)
            self.assertEqual(manager.setup()['onboarding']['state'], 'ready')
            self.assertEqual(post('/api/setup/cancel')[0], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)


class NativeOnboardingContracts(unittest.TestCase):
    def mapped_discovery(self, paths, *, incomplete=None, changed=False):
        from windows_keyproof_platform import FILETIME
        executable = r'C:\Synthetic\Weixin.exe'
        family = [{'pid': 12, 'parent_pid': 8, 'creation_time': 34},
                  {'pid': 13, 'parent_pid': 12, 'creation_time': 35}]
        evidence = {'opened': [], 'closed': [], 'queries': []}
        def opened(access, inherit, pid):
            evidence['opened'].append((access, pid)); return pid
        def image(pid, flags, target, size):
            target.value = executable
            C.cast(size, C.POINTER(C.c_uint32)).contents.value = len(executable)
            return 1
        def stamp(pid, timestamp, *unused):
            C.cast(timestamp, C.POINTER(FILETIME)).contents.low = {12: 34, 13: 35}[pid]
            return 1
        visits = {}
        def mappings(api, handle, *, diagnostics, **options):
            visits[handle] = visits.get(handle, 0) + 1
            evidence['queries'].append((handle, visits[handle], options['max_regions']))
            diagnostics.update(query_completed=handle != incomplete, mapped_regions_queried=1)
            return list(paths(handle, visits[handle]))
        native = SimpleNamespace(OpenProcess=opened, QueryFullProcessImageNameW=image,
            GetProcessTimes=stamp, CloseHandle=lambda pid: evidence['closed'].append(pid) or True)
        api = SimpleNamespace(GetLogicalDrives=lambda: 0)
        after = [dict(row) for row in family]
        if changed:
            after[1]['creation_time'] += 1
        candidate = {'state': 'ready', 'pid': 12, 'creation_time': 34, 'exe_path': executable}
        with patch('windows_onboarding_native.discover_executable', return_value=candidate), \
             patch('windows_onboarding_native._api', return_value=native), \
             patch('windows_onboarding_native.path_is_local', return_value=True), \
             patch('windows_process_detection.verified_process_family', side_effect=[family, after]), \
             patch('windows_mapped_files.MappedFilesAPI', return_value=api), \
             patch('windows_mapped_files.mapped_paths', side_effect=mappings):
            result = discover_direct(clock=lambda: 0)
        return result, evidence

    def test_native_discovery_requires_complete_stable_family_and_two_unique_root_passes(self):
        root = r'C:\Synthetic\db_storage'
        result, evidence = self.mapped_discovery(lambda pid, visit: [root + r'\message\message_0.db'])
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['db_root'], root.casefold())
        self.assertEqual(evidence['queries'], [(12, 1, 32768), (13, 1, 32767),
                                               (12, 2, 32766), (13, 2, 32765)])
        self.assertTrue(all(access == 0x400 for access, pid in evidence['opened']))
        self.assertEqual(evidence['closed'], [12, 13, 12, 13])
        with self.assertRaisesRegex(OnboardingError, 'mapping_unavailable'):
            self.mapped_discovery(lambda pid, visit: [root + r'\message\message_0.db'], incomplete=13)
        with self.assertRaisesRegex(OnboardingError, 'process_changed'):
            self.mapped_discovery(lambda pid, visit: [root + r'\message\message_0.db'], changed=True)

    def test_native_discovery_does_not_guess_between_accounts_or_different_rounds(self):
        one, two = r'C:\One\db_storage', r'C:\Two\db_storage'
        result, _ = self.mapped_discovery(lambda pid, visit: [(one if pid == 12 else two) + r'\message\message_0.db'])
        self.assertEqual(result['state'], 'ambiguous')
        result, _ = self.mapped_discovery(lambda pid, visit: [(one if visit == 1 else two) + r'\message\message_0.db'])
        self.assertEqual(result['state'], 'ambiguous')
        result, _ = self.mapped_discovery(lambda pid, visit: [])
        self.assertEqual(result['state'], 'waiting_for_wechat')
        self.assertTrue(result['process_running'])

    def test_database_path_discovery_resolves_device_paths_and_keeps_account_ambiguity(self):
        roots = mapped_roots([r'\Device\VolumeA\Users\Synthetic\db_storage\message\message_0.db',
                              r'C:\Users\Synthetic\db_storage\contact\contact.db-wal'],
                             {r'\Device\VolumeA': 'c:'})
        self.assertEqual(roots, {r'c:\users\synthetic\db_storage'})
        roots = mapped_roots([r'C:\One\db_storage\message\message_0.db',
                              r'C:\Two\db_storage\message\message_1.db'], {})
        self.assertEqual(len(roots), 2)
        with self.assertRaisesRegex(OnboardingError, 'mapping_unavailable'):
            mapped_roots([r'\Device\Unknown\One\db_storage\message\message_0.db'], {})

    def test_candidate_rejects_extra_fields_nonlocal_roots_and_wrong_process(self):
        value = {'state': 'ready', 'reason': 'windows_onboarding_account_detected', 'pid': 1,
                 'creation_time': 2, 'exe_path': r'C:\WeChat\Weixin.exe', 'db_root': r'C:\Synthetic\db_storage'}
        self.assertEqual(validate_candidate(value), value)
        for changed in [dict(value, pid=True), dict(value, db_root=r'\\server\share\db_storage'),
                        dict(value, exe_path=r'C:\WeChat\Other.exe'), dict(value, secret='forbidden')]:
            with self.subTest(changed=set(changed)):
                with self.assertRaises((OnboardingError, ValueError)):
                    validate_candidate(changed)

    def test_initialize_reuses_bounded_real_key_parser_and_authenticates_three_synthetic_pages(self):
        with tempfile.TemporaryDirectory(prefix='chatlens-synthetic-native-init-') as temporary:
            root = Path(temporary).resolve()
            source = root / 'db_storage'; source.mkdir()
            executable = root / 'Weixin.exe'; executable.write_bytes(b'SYNTHETIC_NOT_EXECUTED')
            target = ensure_private_dir(root / 'data', create=True)
            candidate = {'state': 'ready', 'reason': 'windows_onboarding_account_detected', 'pid': 123,
                         'creation_time': 456, 'exe_path': str(executable), 'db_root': str(source)}
            pages = {relative: page(index) for index, relative in enumerate(collector_service.RELATIVES)}
            for relative, content in pages.items():
                path = source / relative; path.parent.mkdir(exist_ok=True); path.write_bytes(content)
            phases = []
            with patch('windows_onboarding_native.validate_candidate', lambda value: value), \
                 patch('windows_account_probe.probe_direct', return_value={'state': 'matched'}), \
                 patch('windows_selected_runner.fixed_shards'), \
                 patch('windows_keyproof_platform.read_page', lambda path: pages[path.relative_to(source).as_posix()]), \
                 patch('windows_keyproof_platform.WindowsProcess', Memory) as memory:
                result = initialize_direct(candidate, target, progress=phases.append)
            self.assertEqual(result['state'], 'ready')
            keys = read_private_json(target / 'private/keys.json')
            self.assertEqual({name: item['enc_key'] for name, item in keys['keys'].items()},
                             {relative: KEYS[index].hex() for index, relative in enumerate(collector_service.RELATIVES)})
            scope = read_private_json(target / 'private/scope.json')
            self.assertEqual(scope['scope_kind'], 'unselected_reader')
            self.assertEqual(scope['config'], {'conversations': []})
            self.assertIn('memory_markers', phases)
            self.assertIn('memory_references', phases)
            self.assertIn('validating_keys', phases)
            with self.assertRaisesRegex(collector_service.CollectorError, 'explicit_selection_required'):
                collector_service.prepare_session(target)
            # Existing verified keys are reused without reading process memory
            # again, even after an interrupted final scope publication.
            (target / 'private/scope.json').unlink()
            with patch('windows_onboarding_native.validate_candidate', lambda value: value), \
                 patch('windows_account_probe.probe_direct', return_value={'state': 'matched'}), \
                 patch('windows_selected_runner.fixed_shards'), \
                 patch('windows_keyproof_platform.read_page', lambda path: pages[path.relative_to(source).as_posix()]), \
                 patch('windows_keyproof_platform.WindowsProcess', side_effect=AssertionError('must reuse authenticated keys')):
                initialize_direct(candidate, target)

    def test_atomic_initialization_publication_never_overwrites_an_existing_configuration(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = ensure_private_dir(Path(temporary).resolve() / 'private', create=True)
            path = directory / 'keys.json'
            publish_json_once(path, {'synthetic_existing': True})
            before = path.read_bytes()
            with self.assertRaises(OnboardingError):
                publish_json_once(path, {'synthetic_new': True})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(directory.glob('.reader-init-*')), [])

    def test_missing_database_reports_layout_without_scanning_or_publishing_partial_keys(self):
        with tempfile.TemporaryDirectory(prefix='chatlens-synthetic-missing-db-') as temporary:
            root = Path(temporary).resolve()
            source = root / 'db_storage'; source.mkdir()
            executable = root / 'Weixin.exe'; executable.write_bytes(b'SYNTHETIC')
            target = ensure_private_dir(root / 'data', create=True)
            candidate = {'state': 'ready', 'reason': 'windows_onboarding_account_detected', 'pid': 123,
                         'creation_time': 456, 'exe_path': str(executable), 'db_root': str(source)}
            with patch('windows_onboarding_native.validate_candidate', lambda value: value), \
                 patch('windows_account_probe.probe_direct', return_value={'state': 'matched'}), \
                 patch('windows_selected_runner.fixed_shards'), \
                 patch('windows_keyproof_platform.WindowsProcess', side_effect=AssertionError('no memory scan for missing database')):
                with self.assertRaisesRegex(OnboardingError, 'windows_onboarding_database_layout_unsupported'):
                    initialize_direct(candidate, target)
            self.assertFalse((target / 'private/keys.json').exists())
            self.assertFalse((target / 'private/scope.json').exists())

    def test_owned_worker_cancellation_terminates_only_our_synthetic_child(self):
        cancel = threading.Event()
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        try:
            with self.assertRaisesRegex(OnboardingError, 'cancelled'):
                _owned_process([sys.executable, '-c', 'import sys,time;sys.stdin.buffer.read();time.sleep(10)'],
                               {}, cancel=cancel, progress=lambda _: None, timeout=2)
        finally:
            timer.cancel()

    def test_owned_worker_cancel_remains_responsive_when_child_never_reads_stdin(self):
        cancel = threading.Event()
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        before = time.monotonic()
        try:
            with self.assertRaisesRegex(OnboardingError, 'cancelled'):
                _owned_process([sys.executable, '-c', 'import time;time.sleep(10)'],
                               {'synthetic_input': 'x' * 24000}, cancel=cancel,
                               progress=lambda _: None, timeout=2)
            self.assertLess(time.monotonic() - before, 1.5)
        finally:
            timer.cancel()


if __name__ == '__main__':
    unittest.main()
