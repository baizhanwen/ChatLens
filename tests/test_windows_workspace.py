"""Private synthetic workspace fixtures; no real account, chat or Windows IO."""
import hashlib
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/windows-proof'))
from collector_private import ensure_private_dir, read_private_json, write_private_json_new
from conversation_scope import alias_for
from windows_workspace import Workspace, WorkspaceError
import windows_workspace as workspace_module


class WindowsWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.package = self.root / 'ChatLens-r8'
        self.package.mkdir()
        self.source = self.root / 'synthetic-source-a'
        self.source.mkdir()
        self.other = self.root / 'synthetic-source-b'
        self.other.mkdir()
        self.exe = self.root / 'Weixin.exe'
        self.exe.write_bytes(b'SYNTHETIC_NOT_EXECUTABLE')
        self.workspaces = []

    def workspace(self, **kwargs):
        args = dict(fingerprint='a' * 64, discovery_roots=[self.root])
        args.update(kwargs)
        result = Workspace(self.root / 'workspace', self.package, **args)
        self.workspaces.append(result)
        return result

    def tearDown(self):
        for workspace in self.workspaces:
            workspace.close()

    def reader(self, parent, source=None):
        parent.mkdir(exist_ok=True)
        data = ensure_private_dir(parent / 'collector-data', create=True)
        private = ensure_private_dir(data / 'private', create=True)
        source = source or self.source
        chat = 'synthetic-workspace-peer'
        scope = {'schema_version': 1, 'scope_kind': 'selected_conversations',
                 'db_root': str(source), 'exe_path': str(self.exe), 'session_id': 'a' * 32,
                 'config': {'conversations': [{'alias': alias_for(chat), 'chat_id': chat}]}, 'test_tokens': {}}
        keys = {'schema_version': 1, 'db_root': str(source), 'exe_path': str(self.exe),
                'keys': {name: {'enc_key': '1' * 64} for name in
                         ('message/message_0.db', 'message/message_1.db', 'contact/contact.db')}}
        write_private_json_new(private / 'scope.json', scope)
        write_private_json_new(private / 'keys.json', keys)
        write_private_json_new(private / 'synthetic-history-marker.json', {'value': 'SYNTHETIC_HISTORY_KEEP'})
        return data

    def snapshot(self, path):
        return {str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in path.rglob('*') if p.is_file()}

    def test_new_user_gets_own_directory_without_fake_reader_or_message_baseline(self):
        workspace = self.workspace()
        self.assertIsNone(workspace.find_account(self.source, self.exe))
        target = workspace.create_account_directory(self.source, self.exe)
        self.assertEqual(target.parent.parent, workspace.private / 'accounts')
        self.assertEqual(list(target.iterdir()), [])
        self.assertFalse(workspace.registry.exists())

    def test_one_matching_legacy_is_reused_in_place_and_remembered_across_relaunch(self):
        data = self.reader(self.root / 'ChatLens-Windows-Collector')
        before = self.snapshot(data)
        workspace = self.workspace()
        self.assertEqual(workspace.find_account(self.source, self.exe), data)
        self.assertEqual(self.snapshot(data), before)
        registry = workspace.registry.read_text()
        self.assertNotIn('SYNTHETIC_HISTORY_KEEP', registry)
        self.assertNotIn('enc_key', registry)
        workspace.close()
        again = self.workspace(discovery_roots=[])
        self.assertEqual(again.find_account(self.source, self.exe), data)
        self.assertEqual(self.snapshot(data), before)

    def test_other_account_cannot_reuse_registered_keys_or_data(self):
        data = self.reader(self.root / 'ChatLens-old')
        workspace = self.workspace()
        workspace.register_account(self.source, self.exe, data)
        self.assertIsNone(workspace.find_account(self.other, self.exe))
        self.assertNotEqual(workspace.create_account_directory(self.source, self.exe),
                            workspace.create_account_directory(self.other, self.exe))
        with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_account_mismatch'):
            workspace.register_account(self.other, self.exe, data)

    def test_discovery_does_not_read_keys_for_another_account(self):
        own = self.reader(self.root / 'ChatLens-own')
        other = self.reader(self.root / 'ChatLens-other', source=self.other)
        original = workspace_module.read_private_json
        def read(path, *args, **kwargs):
            self.assertNotEqual(Path(path), other / 'private/keys.json')
            return original(path, *args, **kwargs)
        with mock.patch.object(workspace_module, 'read_private_json', side_effect=read):
            self.assertEqual(self.workspace().find_account(self.source, self.exe), own)

    def test_ambiguous_old_data_requires_explicit_import_and_preserves_both(self):
        one = self.reader(self.root / 'ChatLens-old-one')
        two = self.reader(self.root / 'ChatLens-old-two')
        before = (self.snapshot(one), self.snapshot(two))
        workspace = self.workspace()
        with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_legacy_ambiguous'):
            workspace.find_account(self.source, self.exe)
        workspace.import_existing(two)
        self.assertEqual(workspace.find_account(self.source, self.exe), two)
        workspace.import_existing(one)
        self.assertEqual(workspace.find_account(self.source, self.exe), one)
        entry = read_private_json(workspace.registry)['accounts'][workspace.account_token(self.source, self.exe)]
        self.assertIn(str(two), entry['previous_directories'])
        self.assertEqual((self.snapshot(one), self.snapshot(two)), before)

    def test_registered_missing_data_is_not_silently_replaced_with_empty_workspace(self):
        data = self.reader(self.root / 'ChatLens-old')
        workspace = self.workspace()
        workspace.register_account(self.source, self.exe, data)
        shutil.rmtree(data)
        with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_registered_data_unavailable'):
            workspace.find_account(self.source, self.exe)

    def test_no_recursive_search_through_unrelated_projects(self):
        self.reader(self.root / 'unrelated-project')
        workspace = self.workspace()
        self.assertIsNone(workspace.find_account(self.source, self.exe))

    def test_symlink_import_is_rejected_without_changing_real_data(self):
        data = self.reader(self.root / 'ChatLens-old')
        link = self.root / 'linked-collector-data'
        try:
            link.symlink_to(data, target_is_directory=True)
        except OSError:
            self.skipTest('symlink creation is unavailable for this test user')
        before = self.snapshot(data)
        with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_import_invalid'):
            self.workspace().import_existing(link)
        self.assertEqual(self.snapshot(data), before)

    def test_workspace_machine_binding_rejects_copied_registry(self):
        data = self.reader(self.root / 'ChatLens-old')
        workspace = self.workspace()
        workspace.register_account(self.source, self.exe, data)
        workspace.close()
        with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_registry_or_machine_mismatch'):
            self.workspace(fingerprint='b' * 64)

    def test_workspace_single_owner_lock_releases_normally(self):
        first, second = self.workspace(), self.workspace()
        first.acquire()
        with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_already_in_use'):
            second.acquire()
        first.close()
        second.acquire()

    def test_cancelled_commit_during_metadata_read_does_not_register(self):
        data = self.reader(self.root / 'ChatLens-old')
        workspace = self.workspace()
        allowed = [True]
        original = workspace._reader
        def read(path):
            value = original(path)
            allowed[0] = False
            return value
        with mock.patch.object(workspace, '_reader', side_effect=read):
            with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_commit_cancelled'):
                workspace.register_account(self.source, self.exe, data, guard=lambda: allowed[0])
        self.assertFalse(workspace.registry.exists())
        self.assertFalse(list(workspace.private.glob('.workspace-*.json')))

    def test_discovery_guard_is_explicit_instead_of_hiding_existing_data(self):
        for name in ['ChatLens-one', 'ChatLens-two', 'ChatLens-three']:
            (self.root / name).mkdir()
        with mock.patch.object(workspace_module, 'MAX_DIRECTORIES', 1):
            with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_discovery_limit'):
                self.workspace().find_account(self.source, self.exe)

    def test_close_does_not_wait_for_slow_discovery_or_allow_late_registration(self):
        self.reader(self.root / 'ChatLens-old')
        workspace = self.workspace()
        started, release = threading.Event(), threading.Event()
        original, result = workspace._scope_reference, []
        def slow(path):
            started.set()
            release.wait(2)
            return original(path)
        def action():
            try:
                workspace.find_account(self.source, self.exe)
            except Exception as error:
                result.append(str(error))
        with mock.patch.object(workspace, '_scope_reference', side_effect=slow):
            thread = threading.Thread(target=action)
            thread.start()
            try:
                self.assertTrue(started.wait(1))
                begin = time.monotonic()
                workspace.close()
                self.assertLess(time.monotonic() - begin, .2)
                self.assertIsNone(workspace.descriptor)
            finally:
                release.set()
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ['windows_workspace_closed'])
        self.assertFalse(workspace.registry.exists())

    def test_import_metadata_account_mismatch_is_rejected_without_registry(self):
        data = self.reader(self.root / 'ChatLens-old')
        path = data / 'private/keys.json'
        keys = read_private_json(path)
        path.unlink()
        keys['db_root'] = str(self.other)
        write_private_json_new(path, keys)
        workspace = self.workspace()
        with self.assertRaisesRegex(WorkspaceError, 'windows_workspace_import_invalid'):
            workspace.import_existing(data)
        self.assertFalse(workspace.registry.exists())
