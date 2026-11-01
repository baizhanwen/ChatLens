"""Synthetic SDK handle tables and subprocess boundaries; no Windows access."""
import ctypes as C
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
import windows_account_probe as probe

ROOT = r'C:\Users\synthetic\wxid_fixture_0123\db_storage'
OTHER = r'C:\Users\synthetic\wxid_other_5678\db_storage'


class Snapshot:
    def __init__(self, paths=(), failure=None):
        self.paths, self.failure, self.index = list(paths), failure, 0
        self.calls, self.buffers = [], []

    def PssCaptureSnapshot(self, process, flags, context, target):
        self.calls.append(('capture', process, flags, context))
        if self.failure == 'capture':
            return 5
        C.cast(target, C.POINTER(C.c_void_p))[0] = C.c_void_p(7)
        return 0

    def PssWalkMarkerCreate(self, allocator, target):
        self.calls.append(('marker', allocator))
        if self.failure == 'marker':
            return 5
        C.cast(target, C.POINTER(C.c_void_p))[0] = C.c_void_p(8)
        return 0

    def PssWalkSnapshot(self, snapshot, information, marker, target, size):
        self.calls.append(('walk', information, size))
        if self.failure == 'walk':
            return 24
        if self.index == len(self.paths):
            return 259
        kind, path = self.paths[self.index]
        self.index += 1
        entry = C.cast(target, C.POINTER(probe.HandleEntry)).contents
        typename, name = C.create_unicode_buffer(kind), C.create_unicode_buffer(path)
        self.buffers.extend([typename, name])
        entry.Flags = 2
        entry.TypeNameLength, entry.TypeName = len(kind) * 2, C.addressof(typename)
        entry.ObjectNameLength, entry.ObjectName = len(path) * 2, C.addressof(name)
        return 0

    def PssWalkMarkerFree(self, marker):
        self.calls.append(('free_marker', marker.value))

    def PssFreeSnapshot(self, process, snapshot):
        self.calls.append(('free_snapshot', process, snapshot.value))


class WindowsAccountProbeTests(unittest.TestCase):
    def test_only_unique_matching_root_is_accepted_and_paths_are_never_returned(self):
        paths = [ROOT + r'\message\message_0.db', ROOT + r'\contact\contact.db-wal']
        result = probe.classify_paths(paths, ROOT.upper())
        self.assertTrue(result['matched'])
        self.assertNotIn('Users', json.dumps(result))
        for actual in ([OTHER + r'\message\message_0.db'], paths + [OTHER + r'\message\message_1.db']):
            self.assertEqual(probe.classify_paths(actual, ROOT)['state'], 'mismatch')

    def test_unresolved_device_empty_or_malformed_metadata_never_matches(self):
        for paths in ([], ['synthetic-non-db'], [None], [ROOT + '\0'], [r'\Device\Unmapped\db_storage\message\message_0.db']):
            self.assertEqual(probe.classify_paths(paths, ROOT)['state'], 'unknown')
        self.assertEqual(probe.classify_paths([ROOT + r'\message\message_0.db'], 'relative/db_storage')['state'], 'unknown')

    def test_native_device_and_long_path_mapping_does_not_confuse_prefixes(self):
        device = r'\Device\HarddiskVolume3'
        path = device + ROOT[2:] + r'\message\message_0.db-shm'
        self.assertTrue(probe.classify_paths([path], ROOT, {device: 'C:'})['matched'])
        self.assertTrue(probe.classify_paths(['\\\\?\\' + ROOT + r'\message\message_0.db'], ROOT)['matched'])
        bad = r'\Device\HarddiskVolume30' + ROOT[2:] + r'\message\message_0.db'
        self.assertFalse(probe.classify_paths([bad], ROOT, {device: 'C:'})['matched'])

    def snapshot(self, api, **options):
        with patch.object(probe, '_api', return_value=SimpleNamespace(GetCurrentProcess=lambda: 99)):
            return probe.snapshot_paths(api, 42, **options)

    def test_sdk_layout_flags_and_file_names_only_and_local_resources_freed(self):
        api = Snapshot([('File', ROOT + r'\message\message_0.db'), ('Mutant', 'SYNTHETIC_NAMED_MUTEX')])
        self.assertEqual(C.sizeof(probe.HandleEntry), 136)
        self.assertEqual(self.snapshot(api), [ROOT + r'\message\message_0.db'])
        self.assertEqual(api.calls[0], ('capture', 42, 12, 0))
        self.assertNotEqual(api.calls[0][2] & 1, 1, 'VA clone is never requested')
        self.assertEqual(api.calls[-2:], [('free_marker', 8), ('free_snapshot', 99, 7)])
        self.assertTrue(all(item[1:] == (2, 136) for item in api.calls if item[0] == 'walk'))

    def test_partial_capture_errors_never_return_a_partial_account_result(self):
        for failure, freed in [('capture', []), ('marker', [('free_snapshot', 99, 7)]),
                               ('walk', [('free_marker', 8), ('free_snapshot', 99, 7)])]:
            api = Snapshot(failure=failure)
            with self.assertRaises(probe.PlatformError):
                self.snapshot(api)
            self.assertEqual([row for row in api.calls if row[0].startswith('free')], freed)

    def test_budget_or_deadline_does_not_accept_an_incomplete_handle_table(self):
        with patch.object(probe, 'MAX_HANDLES', 1):
            with self.assertRaisesRegex(probe.PlatformError, 'handle_limit'):
                self.snapshot(Snapshot([('File', ROOT + r'\message\message_0.db')]))
        ticks = iter([0, 4])
        with self.assertRaisesRegex(probe.PlatformError, 'deadline'):
            self.snapshot(Snapshot(), clock=lambda: next(ticks))

    def test_owned_probe_has_fixed_local_argv_timeout_no_shell_and_redacted_result(self):
        calls = []
        def runner(arguments, **options):
            calls.append((arguments, options))
            return subprocess.CompletedProcess(arguments, 0, json.dumps(probe._result('matched', 'windows_account_open_database_matched')).encode())
        check = probe.WindowsAccountProbe(12, 34, r'C:\synthetic\Weixin.exe', ROOT, runner=runner)
        self.assertTrue(check()['matched'])
        arguments, options = calls[0]
        self.assertEqual(arguments[:6], [sys.executable, '-I', '-S', '-B', '-X', 'utf8'])
        self.assertEqual(Path(arguments[6]).resolve(), Path(probe.__file__).resolve())
        self.assertEqual(options['timeout'], 6)
        self.assertFalse(options['shell'])
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(options['stdin'], subprocess.DEVNULL)

    def test_native_outer_revalidates_full_buffer_and_creation_time_around_snapshot(self):
        api = Snapshot([('File', ROOT + r'\message\message_0.db')])
        executable = r'C:\synthetic\Weixin.exe'
        capacities, opened, closed = [], [], []
        def query(handle, flags, image, size):
            capacity = C.cast(size, C.POINTER(C.c_uint32))
            capacities.append(capacity.contents.value)
            if capacity.contents.value <= len(executable):
                return 0
            image.value = executable
            capacity.contents.value = len(executable)
            return 1
        def process_times(handle, created, *unused):
            C.cast(created, C.POINTER(probe.FILETIME)).contents.low = 34
            return 1
        native = SimpleNamespace(OpenProcess=lambda *args: opened.append(args) or 42,
            CloseHandle=lambda handle: closed.append(handle) or 1,
            QueryFullProcessImageNameW=query, GetProcessTimes=process_times,
            GetCurrentProcess=lambda: 99)
        api.GetLogicalDrives = lambda: 0
        with patch.object(probe, '_api', return_value=native), patch.object(probe, '_SnapshotAPI', return_value=api), \
             patch.object(probe, 'path_is_local', return_value=True):
            self.assertTrue(probe.probe_direct(12, 34, executable, ROOT)['matched'])
            self.assertEqual(capacities, [32768, 32768])
            self.assertEqual(opened, [(0x450, False, 12)])
            self.assertEqual(closed, [42])
            api.calls.clear()
            self.assertEqual(probe.probe_direct(12, 35, executable, ROOT)['state'], 'unknown')
            self.assertFalse(any(row[0] == 'capture' for row in api.calls))
            self.assertEqual(closed, [42, 42])

    def test_child_timeout_failure_or_untrusted_output_fails_closed(self):
        bad = [b'not-json', b'{}', json.dumps({'state': 'matched', 'matched': True, 'reason': ROOT}).encode(),
               json.dumps(dict(probe._result('matched', 'windows_account_fake'), private=ROOT)).encode(), b' ' * 1025,
               json.dumps({'state': 'matched', 'matched': 1, 'reason': 'windows_account_fake'}).encode()]
        for data in bad:
            check = probe.WindowsAccountProbe(12, 34, 'SYNTHETIC_EXE', ROOT,
                runner=lambda *a, **k: subprocess.CompletedProcess([], 0, data))
            self.assertEqual(check()['state'], 'unknown')
        def timeout(*a, **k):
            raise subprocess.TimeoutExpired('SYNTHETIC_PRIVATE_COMMAND', 6)
        self.assertEqual(probe.WindowsAccountProbe(12, 34, 'SYNTHETIC_EXE', ROOT, runner=timeout)()['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
