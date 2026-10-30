"""Virtual Win32 probes and file metadata; never touch user data or windows."""
import contextlib
import ctypes as C
import importlib.util
import io
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
BASE_SPEC = importlib.util.spec_from_file_location('windows_keyproof_platform', DIRECTORY / 'windows_keyproof_platform.py')
BASE = importlib.util.module_from_spec(BASE_SPEC)
BASE_SPEC.loader.exec_module(BASE)
SPEC = importlib.util.spec_from_file_location('selected_native_test_target', DIRECTORY / 'windows_selected_native.py')
N = importlib.util.module_from_spec(SPEC)
with patch.dict(sys.modules, {'windows_keyproof_platform': BASE}):
    SPEC.loader.exec_module(N)


def put(pointer, kind, value):
    C.cast(pointer, C.POINTER(kind))[0] = value


class FakeProcess:
    instances = []

    def __init__(self, *arguments):
        self.arguments, self.handle, self.closed = arguments, None, False
        self.instances.append(self)

    def __enter__(self):
        self.handle = 101
        return self

    def __exit__(self, *_):
        self.handle, self.closed = None, True

    def read(self, *_):
        raise AssertionError('probe must never read process memory')

    def regions(self, *_):
        raise AssertionError('probe must never enumerate process memory')


class FakeProbe:
    def __init__(self):
        self.exit_code, self.foreground_pid, self.window = 259, 99, 999
        self.desktop_name, self.desktop, self.closed = 'Default', 777, []
        self.exit_ok, self.thread_ok, self.close_ok = True, True, True
        self.info_ok, self.input_arguments, self.exit_handles = True, [], []

    def GetExitCodeProcess(self, handle, code):
        self.exit_handles.append(handle)
        put(code, C.c_uint32, self.exit_code)
        return self.exit_ok

    def GetForegroundWindow(self):
        return self.window

    def GetWindowThreadProcessId(self, window, pid):
        put(pid, C.c_uint32, self.foreground_pid)
        return 555 if self.thread_ok else 0

    def OpenInputDesktop(self, *arguments):
        self.input_arguments.append(arguments)
        return self.desktop

    def GetUserObjectInformationW(self, desktop, index, buffer, length, needed):
        assert index == 2
        put(needed, C.c_uint32, (len(self.desktop_name) + 1) * C.sizeof(C.c_wchar))
        if buffer is None:
            return False  # Initial size query, not an operational failure.
        if self.info_ok:
            buffer.value = self.desktop_name
        return self.info_ok

    def CloseDesktop(self, handle):
        self.closed.append(handle)
        return self.close_ok


class NativeTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeProbe()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(N, 'WindowsProcess', FakeProcess))
        self.stack.enter_context(patch.object(N, '_probe_api', return_value=self.api))

    def native(self):
        return N.NativeState(42, r'C:\synthetic\Weixin.exe', 1234)

    def test_bound_handle_background_fields_and_desktop_closed(self):
        with self.native() as native:
            self.assertEqual(native.process.arguments, (42, r'C:\synthetic\Weixin.exe', 1234))
            self.assertEqual(native.sample(), {'probe_succeeded': True, 'running': True,
                                               'frontmost': False, 'screen_locked': False})
            self.assertEqual(self.api.exit_handles, [101])
            self.assertEqual(self.api.input_arguments, [(0, False, 1)])
            self.assertEqual(self.api.closed, [777])
        self.assertTrue(native.process.closed)
        self.assertFalse(native.sample()['probe_succeeded'])

    def test_popup_of_same_pid_is_foreground_regardless_window_handle(self):
        self.api.foreground_pid = 42
        with self.native() as native:
            for window in (9001, 9002):
                self.api.window = window
                state = native.sample()
                self.assertTrue(state['frontmost'])
                self.assertTrue(state['probe_succeeded'])

    def test_missing_foreground_and_failed_pid_query_never_confirm(self):
        with self.native() as native:
            self.api.window = None
            self.assertIsNone(native.sample()['frontmost'])
            self.assertFalse(native.sample()['probe_succeeded'])
            self.api.window, self.api.thread_ok = 9001, False
            self.assertIsNone(native.sample()['frontmost'])
            self.assertFalse(native.sample()['probe_succeeded'])

    def test_nondefault_inaccessible_bad_name_and_failed_close_never_confirm(self):
        with self.native() as native:
            self.api.desktop_name = 'Winlogon-SYNTHETIC-PRIVATE-DESKTOP'
            state = native.sample()
            self.assertTrue(state['screen_locked'])
            self.assertFalse(state['probe_succeeded'])
            self.assertNotIn(self.api.desktop_name, str(state))
            self.api.desktop = None
            self.assertIsNone(native.sample()['screen_locked'])
            self.assertFalse(native.sample()['probe_succeeded'])
            self.api.desktop, self.api.desktop_name, self.api.info_ok = 777, 'Default', False
            self.assertIsNone(native.sample()['screen_locked'])
            self.assertFalse(native.sample()['probe_succeeded'])
            self.api.info_ok, self.api.close_ok = True, False
            self.assertFalse(native.sample()['probe_succeeded'])

    def test_exit_failure_and_terminated_process_fail_closed(self):
        with self.native() as native:
            self.api.exit_ok = False
            self.assertFalse(native.sample()['probe_succeeded'])
            self.assertFalse(native.sample()['running'])
            self.api.exit_ok, self.api.exit_code = True, 0
            self.assertFalse(native.sample()['running'])
            self.assertEqual(self.api.input_arguments, [])

    def test_probe_load_failure_closes_bound_process_and_native_errors_are_silent(self):
        with patch.object(N, '_probe_api', side_effect=N.PlatformError('native_probe_api_unavailable')):
            with self.assertRaises(N.PlatformError):
                with self.native():
                    self.fail('must not enter')
        self.assertTrue(FakeProcess.instances[-1].closed)
        with self.native() as native, contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.api.GetForegroundWindow = lambda: (_ for _ in ()).throw(OSError('SYNTHETIC_PRIVATE_ERROR'))
            state = native.sample()
        self.assertFalse(state['probe_succeeded'])
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR', str(state))
        self.assertEqual(stdout.getvalue(), '')

    def test_explicit_pointer_sized_native_api_signatures(self):
        class Function:
            pass
        class DLL:
            def __getattr__(self, name):
                function = Function()
                setattr(self, name, function)
                return function
        with patch.object(N.C, 'WinDLL', create=True, side_effect=lambda *a, **k: DLL()):
            api = N._ProbeAPI()
        self.assertEqual(api.GetForegroundWindow.restype, C.c_void_p)
        self.assertEqual(api.OpenInputDesktop.restype, C.c_void_p)
        self.assertEqual(api.GetExitCodeProcess.argtypes, [C.c_void_p, C.POINTER(C.c_uint32)])
        self.assertEqual(api.GetWindowThreadProcessId.argtypes[-1], C.POINTER(C.c_uint32))
        self.assertEqual(api.GetUserObjectInformationW.argtypes[-2:], [C.c_uint32, C.POINTER(C.c_uint32)])


class ShmTests(unittest.TestCase):
    root = r'C:\synthetic\db_storage'
    relative = 'message/message_0.db'

    def setUp(self):
        self.files = {
            self.root + r'\message\message_0.db': (0, 4096),
            self.root + r'\message\message_0.db-wal': (0, 100),
            self.root + r'\message\message_0.db-shm': (0, 32768),
        }
        self.paths, self.stats = [], []
        self.api = SimpleNamespace(GetFileAttributesW=self.attributes)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(N, '_api', return_value=self.api))
        self.stack.enter_context(patch.object(N, 'path_is_local', return_value=True))
        self.stack.enter_context(patch.object(N.C, 'get_last_error', create=True, return_value=2))
        self.stack.enter_context(patch.object(N.os, 'stat', side_effect=self.stat))

    def attributes(self, path):
        self.paths.append(path)
        return self.files[path][0] if path in self.files else 0xffffffff

    def stat(self, path, *, follow_symlinks):
        self.stats.append(path)
        self.assertFalse(follow_symlinks)
        return SimpleNamespace(st_size=self.files[path][1])

    def test_matching_pairs_and_only_selected_metadata(self):
        result = N.detect_shm_preconditions(self.root, [self.relative])
        self.assertEqual(result, {self.relative: {'database_exists': True, 'wal_exists': True,
                                                'wal_bytes': 100, 'shm_exists': True}})
        self.assertEqual(len(self.stats), 3)
        self.assertEqual(set(self.paths), set(self.files))
        self.assertNotIn(self.root, str(result))

    def test_missing_shm_nonempty_wal_stops_but_empty_wal_is_allowed(self):
        del self.files[self.root + r'\message\message_0.db-shm']
        with self.assertRaisesRegex(N.PlatformError, '^nonempty_wal_without_shm$'):
            N.detect_shm_preconditions(self.root, [self.relative])
        self.files[self.root + r'\message\message_0.db-wal'] = (0, 0)
        self.assertFalse(N.detect_shm_preconditions(self.root, [self.relative])[self.relative]['shm_exists'])

    def test_fixed_three_databases_contact_nonempty_wal_requires_shm(self):
        message1, contact = self.root + r'\message\message_1.db', self.root + r'\contact\contact.db'
        self.files.update({message1: (0, 4096), contact: (0, 4096), contact + '-wal': (0, 500)})
        selected = [self.relative, 'message/message_1.db', 'contact/contact.db']
        with self.assertRaisesRegex(N.PlatformError, '^nonempty_wal_without_shm$'):
            N.detect_shm_preconditions(self.root, selected)
        self.files[contact + '-shm'] = (0, 32768)
        result = N.detect_shm_preconditions(self.root, selected)
        self.assertEqual(set(result), set(selected))
        self.assertTrue(result['contact/contact.db']['shm_exists'])
        self.assertEqual(result['contact/contact.db']['wal_bytes'], 500)
        self.assertFalse(result['message/message_1.db']['wal_exists'])

    def test_reparse_directory_permission_failure_and_bad_root_stop(self):
        path = self.root + r'\message\message_0.db-wal'
        for attributes in (0x400, 16):
            self.files[path] = (attributes, 100)
            with self.assertRaisesRegex(N.PlatformError, '^wal_pair_unsafe_file$'):
                N.detect_shm_preconditions(self.root, [self.relative])
        del self.files[path]
        with patch.object(N.C, 'get_last_error', return_value=5):
            with self.assertRaisesRegex(N.PlatformError, '^wal_pair_metadata_unavailable$'):
                N.detect_shm_preconditions(self.root, [self.relative])
        with patch.object(N, 'path_is_local', return_value=False):
            with self.assertRaisesRegex(N.PlatformError, '^unsafe_selected_database_root$'):
                N.detect_shm_preconditions(self.root, [self.relative])

    def test_scope_rejects_other_shards_sessions_traversal_duplicates_and_unhashable(self):
        for scope in ([], ['message/message_2.db'], ['session/session.db'], ['../message/message_0.db'],
                      [self.relative, self.relative], [[self.relative]], self.relative,
                      [self.relative, 'message/message_1.db', 'contact/contact.db', 'session/session.db']):
            with self.assertRaisesRegex(N.PlatformError, '^invalid_body_database_scope$'):
                N.detect_shm_preconditions(self.root, scope)
        self.assertEqual(self.paths, [])


if __name__ == '__main__':
    unittest.main()
