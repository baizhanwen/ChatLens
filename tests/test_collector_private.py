"""Synthetic private-file boundaries; never opens user configuration or chats."""
from contextlib import contextmanager
import importlib.util
import ctypes as C
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULES = ROOT / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
SPEC = importlib.util.spec_from_file_location('collector_private_tested', MODULES / 'collector_private.py')
private = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(private)


class CollectorPrivateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-private-')
        # macOS /var is a system symlink; exercise the actual non-redirected path.
        self.root = Path(self.temp.name).resolve()
        self.directory = self.root / 'private'
        self.directory.mkdir(mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def new_json(self, name='synthetic.json', raw=b'{"synthetic": true}', mode=0o600):
        path = self.directory / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, 'wb') as target:
            target.write(raw)
        return path

    def test_creates_only_one_absent_private_directory(self):
        path = self.root / 'new-private'
        self.assertEqual(private.ensure_private_dir(path, create=True), path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        self.assertEqual(private.ensure_private_dir(path, create=True), path)
        with self.assertRaises(private.PrivateError):
            private.ensure_private_dir(self.root / 'missing' / 'child', create=True)

    def test_existing_permissions_are_rejected_without_repair(self):
        self.directory.chmod(0o755)
        with self.assertRaisesRegex(private.PrivateError, 'collector_private_permissions_invalid'):
            private.ensure_private_dir(self.directory, create=True)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o755)

    def test_private_json_round_trip_and_existing_file_preserved(self):
        path = self.directory / 'scope.json'
        document = {'synthetic': '本地虚构', 'items': [1, 2]}
        private.write_private_json_new(path, document)
        self.assertEqual(private.read_private_json(path), document)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        original = path.read_bytes()
        with self.assertRaisesRegex(private.PrivateError, 'collector_private_file_already_exists'):
            private.write_private_json_new(path, {'replacement': True})
        self.assertEqual(path.read_bytes(), original)

    def test_loose_file_mode_rejected_before_json_parse(self):
        path = self.new_json(raw=b'PRIVATE_INVALID_JSON', mode=0o644)
        with mock.patch.object(private, '_parse_json') as parser:
            with self.assertRaisesRegex(private.PrivateError, 'collector_private_permissions_invalid'):
                private.read_private_json(path)
        parser.assert_not_called()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_parent_permissions_rejected_before_open(self):
        path = self.new_json()
        self.directory.chmod(0o755)
        with mock.patch.object(private, '_parse_json') as parser:
            with self.assertRaisesRegex(private.PrivateError, 'collector_private_permissions_invalid'):
                private.read_private_json(path)
        parser.assert_not_called()

    def test_redirected_parent_file_and_hard_link_are_refused(self):
        path = self.new_json()
        linked_dir = self.root / 'redirect'
        linked_dir.symlink_to(self.directory, target_is_directory=True)
        for candidate in (linked_dir / path.name, self.directory / 'redirect.json'):
            if candidate.parent == self.directory:
                candidate.symlink_to(path)
            with self.assertRaises(private.PrivateError):
                private.read_private_json(candidate)
        hard = self.directory / 'hard.json'
        os.link(path, hard)
        with self.assertRaises(private.PrivateError):
            private.read_private_json(hard)

    def test_local_source_validation_does_not_require_private_modes(self):
        path = self.new_json(mode=0o644)
        self.directory.chmod(0o755)
        self.assertEqual(private.validate_local_path(path), path)
        self.assertEqual(private.validate_local_path(self.directory, True), self.directory)

    def test_relative_parent_traversal_and_missing_paths_refused(self):
        for candidate in ('relative.json', self.directory / '..' / 'scope.json', self.root / 'missing.json'):
            with self.assertRaises(private.PrivateError):
                private.validate_local_path(candidate)

    def test_bounded_json_and_untrusted_json_forms(self):
        cases = (b'{"a": 1, "a": 2}', b'{"a": NaN}', b'"SCALAR"', b'{PRIVATE_RAW_TEXT}')
        for index, content in enumerate(cases):
            path = self.new_json(name='bad-%d.json' % index, raw=content)
            with self.assertRaises(private.PrivateError) as context:
                private.read_private_json(path)
            self.assertNotIn('PRIVATE_RAW_TEXT', str(context.exception))
            self.assertNotIn(str(path), str(context.exception))
        path = self.new_json(name='large.json', raw=b'{"synthetic": "' + b'x' * 100 + b'"}')
        with mock.patch.object(private, '_parse_json') as parser:
            with self.assertRaisesRegex(private.PrivateError, 'collector_private_file_size_or_links_invalid'):
                private.read_private_json(path, maxbytes=50)
        parser.assert_not_called()
        for maximum in (True, 0, 1, 2 * 1024 * 1024 + 1):
            with self.assertRaises(private.PrivateError):
                private.read_private_json(path, maxbytes=maximum)

    def test_write_failure_removes_only_new_file(self):
        path = self.directory / 'incomplete.json'
        with mock.patch.object(private.os, 'fsync', side_effect=OSError('SYNTHETIC_PRIVATE_DIAGNOSTIC')):
            with self.assertRaises(private.PrivateError) as context:
                private.write_private_json_new(path, {'synthetic': True})
        self.assertFalse(path.exists())
        self.assertNotIn('SYNTHETIC_PRIVATE_DIAGNOSTIC', str(context.exception))

    def test_windows_existing_directory_is_verified_without_acl_change(self):
        api = mock.Mock()
        api.CreateFileW.return_value = 123

        @contextmanager
        def parents(*_):
            yield 456

        with mock.patch.object(private, '_windows', return_value=True), \
             mock.patch.object(private, 'validate_local_path', side_effect=lambda p, *_: p), \
             mock.patch.object(private.platform, '_api', return_value=api), \
             mock.patch.object(private.platform, '_path', return_value='C:\\synthetic\\private'), \
             mock.patch.object(private.platform, '_parents', parents), \
             mock.patch.object(private.platform, '_checked') as checked, \
             mock.patch.object(private.platform, '_verify_private') as verify, \
             mock.patch.object(private.platform, 'private_dir') as create:
            self.assertEqual(private.ensure_private_dir(self.directory, create=True), self.directory)
        checked.assert_called_once_with(api, 123, 'C:\\synthetic\\private', True)
        verify.assert_called_once_with(api, 123, True)
        create.assert_not_called()
        api.CloseHandle.assert_called_once_with(123)

    def test_windows_acl_failure_closes_handle_without_repair(self):
        api = mock.Mock()
        api.CreateFileW.return_value = 123

        @contextmanager
        def parents(*_):
            yield 456

        with mock.patch.object(private, '_windows', return_value=True), \
             mock.patch.object(private, 'validate_local_path', side_effect=lambda p, *_: p), \
             mock.patch.object(private.platform, '_api', return_value=api), \
             mock.patch.object(private.platform, '_path', return_value='C:\\synthetic\\private'), \
             mock.patch.object(private.platform, '_parents', parents), \
             mock.patch.object(private.platform, '_checked'), \
             mock.patch.object(private.platform, '_verify_private', side_effect=RuntimeError('SYNTHETIC_SECRET')):
            with self.assertRaisesRegex(private.PrivateError, '^collector_private_directory_validation_failed$'):
                private.ensure_private_dir(self.directory)
        api.CloseHandle.assert_called_once_with(123)

    def windows_read_patches(self, payload=b'{"synthetic":true}', links=1):
        api, calls = mock.Mock(), []
        api.CreateFileW.return_value = 123

        @contextmanager
        def parents(*_):
            yield 456

        def information(handle, pointer):
            info = C.cast(pointer, C.POINTER(private.platform.FILEINFO)).contents
            info.volume, info.index_low, info.links, info.size_low = 9, 7, links, len(payload)
            return 1

        def read(handle, buffer, maximum, count, _):
            calls.append('read')
            C.memmove(buffer, payload, len(payload))
            C.cast(count, C.POINTER(private.platform.U32)).contents.value = len(payload)
            return 1

        api.GetFileInformationByHandle.side_effect = information
        api.ReadFile.side_effect = read
        patches = [
            mock.patch.object(private, '_windows', return_value=True),
            mock.patch.object(private, 'ensure_private_dir', side_effect=lambda p, *_: p),
            mock.patch.object(private.platform, 'path_is_local', return_value=True),
            mock.patch.object(private.platform, '_api', return_value=api),
            mock.patch.object(private.platform, '_path', return_value='C:\\synthetic\\private\\scope.json'),
            mock.patch.object(private.platform, '_parents', parents),
            mock.patch.object(private.platform, '_checked', side_effect=lambda *_: calls.append('checked')),
            mock.patch.object(private.platform, '_verify_private', side_effect=lambda *_: calls.append('acl')),
        ]
        return api, calls, patches

    def test_windows_bounded_single_handle_read_verifies_acl_before_bytes(self):
        from contextlib import ExitStack
        api, calls, patches = self.windows_read_patches()
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            self.assertEqual(private.read_private_json(self.directory / 'scope.json', maxbytes=128), {'synthetic': True})
        self.assertEqual(calls, ['acl', 'checked', 'acl', 'read'])
        self.assertEqual(api.CreateFileW.call_args.args[2], 1)  # No WRITE/DELETE sharing during read.
        self.assertEqual(api.ReadFile.call_args.args[2], 129)
        api.CloseHandle.assert_called_once_with(123)

    def test_windows_hard_link_refused_before_read_and_handle_closed(self):
        from contextlib import ExitStack
        api, calls, patches = self.windows_read_patches(links=2)
        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            with self.assertRaisesRegex(private.PrivateError, 'collector_private_file_size_or_links_invalid'):
                private.read_private_json(self.directory / 'scope.json')
        self.assertNotIn('read', calls)
        api.ReadFile.assert_not_called()
        api.CloseHandle.assert_called_once_with(123)


if __name__ == '__main__':
    unittest.main()
