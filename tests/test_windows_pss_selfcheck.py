"""Self-owned PSS canary contracts; native Windows behavior is not simulated proof."""
from contextlib import redirect_stdout
import ctypes as C
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/windows-proof'))
import windows_pss_selfcheck as check

CANARY = r'C:\synthetic-selfcheck\fixture.synthetic'
DEVICE = r'\Device\HarddiskVolume77'
NT_CANARY = DEVICE + CANARY[2:]


class Snapshot:
    def __init__(self, rows=()):
        self.rows, self.index, self.buffers = list(rows), 0, []
        self.captures, self.marker_frees, self.snapshot_frees, self.drives = [], [], [], []
        self.capture_code = self.marker_free_code = self.snapshot_free_code = 0
        self.walk_error_after = None

    def GetCurrentProcess(self):
        return 444

    def QueryDosDeviceW(self, drive, buffer, length):
        self.drives.append(drive)
        buffer.value = DEVICE
        return len(DEVICE) + 1

    def PssCaptureSnapshot(self, process, flags, context, target):
        self.captures.append((process, flags, context))
        if not self.capture_code:
            C.cast(target, C.POINTER(C.c_void_p))[0] = 111
        return self.capture_code

    def PssWalkMarkerCreate(self, allocator, target):
        C.cast(target, C.POINTER(C.c_void_p))[0] = 222
        return 0

    def _buffer(self, value):
        data = value.encode('utf-16-le') if isinstance(value, str) else value
        buffer = C.create_string_buffer(data)
        self.buffers.append(buffer)
        return C.addressof(buffer), len(data)

    def PssWalkSnapshot(self, snapshot, information_class, marker, target, length):
        assert information_class == 2
        assert length == 136
        if self.walk_error_after is not None and self.index >= self.walk_error_after:
            return 24
        if self.index == len(self.rows):
            return 259
        row = self.rows[self.index]; self.index += 1
        entry = C.cast(target, C.POINTER(check.HandleEntry)).contents
        entry.Handle = row.get('handle', 99)
        entry.Flags = row.get('flags', 2)
        if row.get('type') is not None:
            entry.TypeName, entry.TypeNameLength = self._buffer(row['type'])
        if row.get('name') is not None:
            entry.ObjectName, entry.ObjectNameLength = self._buffer(row['name'])
        return 0

    def PssWalkMarkerFree(self, marker):
        self.marker_frees.append(marker.value)
        return self.marker_free_code

    def PssFreeSnapshot(self, process, snapshot):
        self.snapshot_frees.append((process, snapshot.value))
        return self.snapshot_free_code


class Mapped:
    def __init__(self):
        self.calls = []
        self.name = NT_CANARY
        self.returned_length = None
        self.device_ok = self.create_ok = self.view_ok = self.unmap_ok = self.close_ok = True
        self.query_error = False

    def QueryDosDeviceW(self, drive, buffer, length):
        self.calls.append(('device', drive))
        if self.device_ok:
            buffer.value = DEVICE
            return len(DEVICE) + 1
        return 0

    def CreateFileMappingW(self, *arguments):
        self.calls.append(('create', arguments))
        return 111 if self.create_ok else None

    def MapViewOfFile(self, *arguments):
        self.calls.append(('view', arguments))
        return 222 if self.view_ok else None

    def GetCurrentProcess(self):
        self.calls.append(('self',))
        return 444

    def K32GetMappedFileNameW(self, process, address, buffer, capacity):
        self.calls.append(('query', process, address, capacity))
        if self.query_error:
            raise RuntimeError('PRIVATE_NATIVE_PATH')
        buffer.value = self.name[:capacity - 1]
        return len(self.name) if self.returned_length is None else self.returned_length

    def UnmapViewOfFile(self, address):
        self.calls.append(('unmap', address))
        return self.unmap_ok

    def CloseHandle(self, handle):
        self.calls.append(('close', handle))
        return self.close_ok


class SelfcheckTests(unittest.TestCase):
    def observe(self, rows=(), **options):
        api = Snapshot(rows)
        result = check.observe_self(api, CANARY, 99, **options)
        return api, result

    def completed(self):
        _, result = self.observe([{'type': 'File', 'name': NT_CANARY}])
        result['diagnostics']['fixture_closed'] = True
        return result

    def test_sdk_byte_lengths_accept_nonterminated_unicode_and_do_not_halve_characters(self):
        raw = 'File'.encode('utf-16-le')
        buffer = C.create_string_buffer(raw, len(raw))
        self.assertEqual(check.decode_name(C.addressof(buffer), 8), 'File')
        self.assertEqual(C.sizeof(check.HandleEntry), 136)
        self.assertEqual(check.HandleEntry.TypeName.offset, 64)
        self.assertEqual(check.HandleEntry.ObjectName.offset, 80)

    def test_single_terminal_nul_and_embedded_nul_are_distinct_observations(self):
        self.assertEqual(check.name_shape('File'), ('exact', 'File'))
        self.assertEqual(check.name_shape('File\0'), ('single_trailing_nul', 'File'))
        for text in ('Fi\0le', 'File\0\0', '\0File'):
            self.assertEqual(check.name_shape(text), ('embedded_nul', None))
        buffer = C.create_string_buffer('File\0'.encode('utf-16-le'), 10)
        self.assertEqual(check.decode_name(C.addressof(buffer), 10), 'File\0')

    def test_invalid_pointer_or_byte_count_is_rejected_before_buffer_read(self):
        with patch.object(check.C, 'string_at', side_effect=AssertionError('must not read')):
            for pointer, length in ((0, 8), (100, 0), (100, 3), (100, 65534), (100, True), (100, 8.0)):
                with self.subTest(pointer=pointer, length=length), self.assertRaises(ValueError):
                    check.decode_name(pointer, length)
        buffer = C.create_string_buffer(b'\x00\xd8', 2)
        with self.assertRaises(UnicodeDecodeError):
            check.decode_name(C.addressof(buffer), 2)

    def test_owned_file_handle_and_nt_path_are_seen_without_publishing_identity(self):
        api, result = self.observe([{'type': 'File', 'name': NT_CANARY}])
        detail = result['diagnostics']
        self.assertEqual(result['status'], '通过')
        for key in ('query_completed', 'cleanup_succeeded', 'drive_mapping_succeeded',
                    'self_canary_handle_seen', 'self_canary_type_exact_file', 'self_canary_exact_path_seen'):
            self.assertTrue(detail[key], key)
        self.assertFalse(detail['self_canary_trimmed_path_seen'])
        self.assertEqual(api.captures, [(444, 0x0c, 0)])
        self.assertEqual(api.drives, ['C:'])
        self.assertEqual(api.marker_frees, [222])
        self.assertEqual(api.snapshot_frees, [(444, 111)])
        text = json.dumps(result)
        for secret in (CANARY, NT_CANARY, DEVICE, '444', '111', '222', '"pid"', '"Handle"'):
            self.assertNotIn(secret, text)

    def test_tail_nul_canary_is_reported_as_a_separate_diagnostic_without_changing_production(self):
        _, result = self.observe([{'type': 'File\0', 'name': NT_CANARY + '\0'}])
        detail = result['diagnostics']
        self.assertEqual(result['status'], '通过')
        self.assertEqual(detail['type_exact_file'], 0)
        self.assertEqual(detail['type_single_trailing_nul_file'], 1)
        self.assertTrue(detail['self_canary_type_single_trailing_nul_file'])
        self.assertFalse(detail['self_canary_exact_path_seen'])
        self.assertTrue(detail['self_canary_trimmed_path_seen'])

    def test_file_with_same_leaf_or_another_handle_cannot_impersonate_owned_canary(self):
        cases = ([{'handle': 98, 'type': 'File', 'name': NT_CANARY}],
                 [{'type': 'File', 'name': r'\Device\HarddiskVolume77\another\fixture.synthetic'}],
                 [{'type': 'File', 'name': NT_CANARY + '\0suffix'}],
                 [{'type': 'Fi\0le', 'name': NT_CANARY}])
        for rows in cases:
            with self.subTest(rows=rows):
                _, result = self.observe(rows)
                self.assertEqual(result['status'], '失败')
                self.assertTrue(result['diagnostics']['query_completed'])

    def test_missing_file_metadata_and_unknown_types_are_counted_not_exported_raw(self):
        rows = [{'type': 'PRIVATE_SYNTHETIC_TYPE', 'name': NT_CANARY, 'flags': 0},
                {'handle': 91, 'type': b'\x00\xd8', 'name': b'\x01'},
                {'handle': 92, 'type': 'Event', 'name': 'embedded\0name'}]
        _, result = self.observe(rows)
        detail = result['diagnostics']
        self.assertEqual(result['status'], '失败')
        self.assertEqual(detail['type_invalid_utf16_or_length'], 1)
        self.assertEqual(detail['object_invalid_utf16_or_length'], 1)
        self.assertEqual(detail['object_embedded_nul'], 1)
        self.assertEqual(detail['known_type_histogram']['Other'], 1)
        self.assertEqual(detail['known_type_histogram']['Event'], 1)
        self.assertNotIn('PRIVATE_SYNTHETIC_TYPE', json.dumps(result))

    def test_partial_capture_deadline_and_shared_handle_bound_never_claim_completion(self):
        api = Snapshot([{'type': 'File', 'name': NT_CANARY}]);api.walk_error_after = 1
        result = check.observe_self(api, CANARY, 99)
        self.assertFalse(result['diagnostics']['query_completed'])
        self.assertEqual(result['status'], '未验证')
        self.assertEqual(api.marker_frees, [222])
        clock = iter((0, 0, 0, 4))
        api, result = self.observe([{'type': 'File', 'name': NT_CANARY}], clock=lambda: next(clock))
        self.assertFalse(result['diagnostics']['query_completed'])
        self.assertEqual(api.snapshot_frees, [(444, 111)])
        with patch.object(check, 'MAX_HANDLES', 1):
            _, result = self.observe([{'type': 'File', 'name': NT_CANARY}])
        self.assertFalse(result['diagnostics']['query_completed'])
        self.assertEqual(result['diagnostics']['handles_seen'], 1)

    def test_capture_and_cleanup_errors_do_not_become_canary_success(self):
        for field in ('capture_code', 'marker_free_code', 'snapshot_free_code'):
            api = Snapshot([{'type': 'File', 'name': NT_CANARY}]);setattr(api, field, 5)
            with self.subTest(field=field):
                result = check.observe_self(api, CANARY, 99)
                self.assertEqual(result['status'], '未验证')
                if field != 'capture_code':
                    self.assertFalse(result['diagnostics']['cleanup_succeeded'])
                    self.assertEqual(api.snapshot_frees, [(444, 111)])

    def test_nonwindows_returns_explicit_unverified_without_native_or_worker_attempt(self):
        runner = lambda *a, **k: self.fail('unsupported platform must not spawn')
        result = check.run_selfcheck(runner=runner, platform_check=lambda: False)
        self.assertEqual(result['status'], '未验证')
        self.assertEqual(result['reason'], 'pss_selfcheck_windows_x64_required')
        self.assertFalse(result['diagnostics']['native_api_attempted'])
        result = check.worker('UNUSED', platform_check=lambda: False,
                              api_factory=lambda: self.fail('must not load native API'))
        self.assertEqual(result['reason'], 'pss_selfcheck_windows_x64_required')

    def test_parent_runs_only_its_owned_worker_and_removes_temporary_artifact(self):
        seen = []
        def runner(arguments, **options):
            self.assertEqual(arguments[:7], [sys.executable, '-I', '-S', '-B', '-X', 'utf8', str(Path(check.__file__).resolve())])
            self.assertEqual(arguments[-2], '--worker')
            self.assertFalse(options['shell']);self.assertEqual(options['timeout'], 6)
            self.assertEqual(options['stderr'], subprocess.DEVNULL)
            self.assertEqual(options['stdin'], subprocess.DEVNULL)
            folder = Path(arguments[-1]);seen.append(folder)
            (folder / 'synthetic-worker-owned').write_bytes(b'SYNTHETIC')
            return subprocess.CompletedProcess(arguments, 0, json.dumps(self.completed()).encode())
        result = check.run_selfcheck(runner=runner, platform_check=lambda: True)
        self.assertEqual(result['status'], '通过')
        self.assertTrue(result['diagnostics']['parent_fixture_removed'])
        self.assertTrue(all(not path.exists() for path in seen))

    def test_owned_worker_timeout_discards_partial_output_and_cleans_fixture(self):
        seen = []
        def runner(arguments, **_):
            folder = Path(arguments[-1]);seen.append(folder)
            (folder / 'synthetic-worker-owned').write_bytes(b'SYNTHETIC')
            raise subprocess.TimeoutExpired(arguments, 6, output=b'PRIVATE_RAW_PATH')
        result = check.run_selfcheck(runner=runner, platform_check=lambda: True)
        self.assertEqual(result['reason'], 'pss_selfcheck_worker_timeout')
        self.assertFalse(result['diagnostics']['query_completed'])
        self.assertNotIn('PRIVATE_RAW_PATH', json.dumps(result))
        self.assertTrue(all(not path.exists() for path in seen))

    def test_parent_rejects_unknown_fields_invalid_counts_duplicate_keys_and_partial_pass(self):
        good = self.completed()
        invalid = []
        for key in ('native_api_attempted', 'fixture_created', 'fixture_closed', 'query_completed', 'cleanup_succeeded'):
            value = copy.deepcopy(good);value['diagnostics'][key] = False;invalid.append(value)
        value = copy.deepcopy(good);value['diagnostics']['handles_seen'] = True;invalid.append(value)
        value = copy.deepcopy(good);value['diagnostics']['raw_path'] = CANARY;invalid.append(value)
        value = copy.deepcopy(good);value['diagnostics']['known_type_histogram']['PRIVATE_TYPE'] = 1;invalid.append(value)
        value = copy.deepcopy(good);value['pid'] = 99;invalid.append(value)
        value = copy.deepcopy(good);value['tool_version'] = 'unrecognized';invalid.append(value)
        invalid.extend((b'{}' + b' ' * 16384, b'{"kind":"a","kind":"b"}'))
        for value in invalid:
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            with self.subTest(value=value):
                result = check.run_selfcheck(platform_check=lambda: True,
                    runner=lambda *a, **k: subprocess.CompletedProcess([], 0, raw))
                self.assertEqual(result['reason'], 'pss_selfcheck_worker_output_invalid')
                self.assertNotIn(CANARY, json.dumps(result))

    @unittest.skipIf(sys.platform == 'win32', 'Non-Windows standalone smoke only; native Windows needs its own result.')
    def test_single_file_works_in_isolated_python_without_repository_imports(self):
        completed = subprocess.run([sys.executable, '-I', '-S', '-B', '-X', 'utf8', check.__file__],
                                   capture_output=True, timeout=3, check=False)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b'')
        result = check._validated_report(completed.stdout)
        self.assertEqual(result['reason'], 'pss_selfcheck_windows_x64_required')
        self.assertEqual(len(completed.stdout.splitlines()), 1)

    def test_invalid_cli_does_not_offer_another_process_or_print_arguments(self):
        output = io.StringIO()
        with redirect_stdout(output):
            check.main(['--pid', 'PRIVATE_ARGUMENT_123'])
        result = json.loads(output.getvalue())
        self.assertEqual(result['reason'], 'pss_selfcheck_invalid_arguments')
        self.assertNotIn('PRIVATE_ARGUMENT_123', output.getvalue())


class MappedSelfcheckTests(unittest.TestCase):
    def observe(self, api=None, **options):
        api = api or Mapped()
        return api, check.observe_mapped_self(api, CANARY, 99, **options)

    def completed(self):
        _, value = self.observe()
        value['diagnostics']['fixture_closed'] = True
        return value

    def test_owned_file_mapping_queries_only_its_known_view_and_releases_every_resource(self):
        api, result = self.observe()
        self.assertEqual(result['kind'], check.MAPPED_KIND)
        self.assertEqual(result['tool_version'], '20261101-02')
        self.assertEqual(result['status'], '通过')
        detail = result['diagnostics']
        self.assertEqual(detail['mapped_regions_created'], 1)
        self.assertEqual(detail['mapped_name_characters'], len(NT_CANARY))
        for key in ('map_created', 'map_closed', 'cleanup_succeeded', 'mapped_query_completed', 'self_canary_exact_path_seen'):
            self.assertTrue(detail[key], key)
        self.assertEqual(api.calls, [('device', 'C:'), ('create', (99, None, 4, 0, 4096, None)),
            ('view', (111, 2, 0, 0, 4096)), ('self',), ('query', 444, 222, 32768),
            ('unmap', 222), ('close', 111)])
        for forbidden in (CANARY, NT_CANARY, DEVICE, '"pid"', '"address"', '"fd"', '444', '222', '111'):
            self.assertNotIn(forbidden, json.dumps(result))

    def test_api_character_count_is_not_treated_as_pss_byte_count(self):
        path = r'C:\合成自检\映射文件.synthetic'
        api = Mapped();api.name = DEVICE + path[2:]
        result = check.observe_mapped_self(api, path, 99)
        self.assertEqual(result['status'], '通过')
        self.assertEqual(result['diagnostics']['mapped_name_characters'], len(api.name))

    def test_zero_truncated_or_oversized_return_never_becomes_path_success(self):
        for length in (0, 32768, 40000):
            api = Mapped();api.returned_length = length
            with self.subTest(length=length):
                _, result = self.observe(api)
                detail = result['diagnostics']
                self.assertNotEqual(result['status'], '通过')
                self.assertFalse(detail['mapped_query_completed'])
                self.assertFalse(detail['self_canary_exact_path_seen'])
                self.assertTrue(detail['map_closed'])
                if length:
                    self.assertTrue(detail['mapped_query_truncated'])
                    self.assertEqual(result['status'], '未验证')
                else:
                    self.assertEqual(result['status'], '失败')
                    self.assertEqual(result['reason'], 'mapped_selfcheck_path_unavailable')

    def test_wrong_full_path_and_unresolved_device_are_not_a_matching_canary(self):
        api = Mapped();api.name = DEVICE + r'\another\fixture.synthetic'
        _, result = self.observe(api)
        self.assertEqual(result['status'], '失败')
        self.assertEqual(result['reason'], 'mapped_selfcheck_path_mismatch')
        api = Mapped();api.device_ok = False
        _, result = self.observe(api)
        self.assertEqual(result['status'], '未验证')
        self.assertEqual(result['reason'], 'mapped_selfcheck_device_mapping_unavailable')
        api.name = CANARY
        _, result = self.observe(api)
        self.assertEqual(result['status'], '通过', 'An exact DOS path does not require a device alias.')

    def test_mapping_query_and_cleanup_failures_still_release_remaining_resources(self):
        for field in ('create_ok', 'view_ok', 'unmap_ok', 'close_ok', 'query_error'):
            api = Mapped();setattr(api, field, True if field == 'query_error' else False)
            with self.subTest(field=field):
                _, result = self.observe(api)
                self.assertEqual(result['status'], '未验证')
                self.assertNotIn('PRIVATE_NATIVE_PATH', json.dumps(result))
                if field != 'create_ok':
                    self.assertIn(('close', 111), api.calls)
                if field not in ('create_ok', 'view_ok'):
                    self.assertIn(('unmap', 222), api.calls)
                if field in ('unmap_ok', 'close_ok'):
                    self.assertFalse(result['diagnostics']['cleanup_succeeded'])

    def test_elapsed_budget_or_embedded_null_name_never_claims_complete_mapping_query(self):
        clock = iter((0, 0, 0, 0, 4))
        api, result = self.observe(clock=lambda: next(clock))
        self.assertEqual(result['status'], '未验证')
        self.assertFalse(result['diagnostics']['mapped_query_completed'])
        self.assertTrue(result['diagnostics']['map_closed'])
        api = Mapped();api.name = NT_CANARY + '\0suffix'
        _, result = self.observe(api)
        self.assertEqual(result['status'], '未验证')
        self.assertFalse(result['diagnostics']['mapped_query_completed'])

    def test_native_bindings_use_pointer_sized_handles_and_wchar_character_capacity(self):
        class Function:
            pass
        class DLL:
            def __getattr__(self, name):
                function = Function();setattr(self, name, function);return function
        with patch.object(check.C, 'WinDLL', create=True, return_value=DLL()):
            api = check.MappedAPI()
        self.assertEqual(api.CreateFileMappingW.restype, C.c_void_p)
        self.assertEqual(api.MapViewOfFile.restype, C.c_void_p)
        self.assertEqual(api.MapViewOfFile.argtypes[-1], C.c_size_t)
        self.assertEqual(api.K32GetMappedFileNameW.argtypes, [C.c_void_p, C.c_void_p, C.c_wchar_p, C.c_uint32])
        self.assertEqual(api.K32GetMappedFileNameW.restype, C.c_uint32)

    def test_worker_creates_only_a_new_4096_zero_byte_file_and_closes_it(self):
        observed = []
        completed_result = self.completed()
        def own_query(api, path, handle):
            fixture = Path(path);observed.append(fixture)
            self.assertEqual(fixture.read_bytes(), bytes(4096))
            self.assertIsInstance(handle, int)
            return copy.deepcopy(completed_result)
        with tempfile.TemporaryDirectory(prefix='synthetic-mapping-selfcheck-test-') as folder:
            with patch.dict(sys.modules, {'msvcrt': SimpleNamespace(get_osfhandle=lambda fd: fd)}), \
                    patch.object(check, 'observe_mapped_self', side_effect=own_query):
                result = check.mapped_worker(folder, platform_check=lambda: True, api_factory=lambda: Mapped())
            self.assertEqual(result['status'], '通过')
            self.assertTrue(result['diagnostics']['fixture_closed'])
            self.assertTrue(observed and all(not item.exists() for item in observed))

    def test_mapped_parent_keeps_kind_separate_uses_owned_timeout_and_removes_temp(self):
        seen = []
        def runner(arguments, **options):
            self.assertEqual(arguments[-2], '--worker-mapped')
            self.assertEqual(options['timeout'], 6);self.assertFalse(options['shell'])
            folder = Path(arguments[-1]);seen.append(folder)
            (folder / 'synthetic-owned').write_bytes(b'SYNTHETIC')
            return subprocess.CompletedProcess(arguments, 0, json.dumps(self.completed()).encode())
        result = check.run_selfcheck(mode='mapped-path', runner=runner, platform_check=lambda: True)
        self.assertEqual(result['status'], '通过')
        self.assertEqual(result['kind'], check.MAPPED_KIND)
        self.assertTrue(result['diagnostics']['parent_fixture_removed'])
        self.assertTrue(all(not path.exists() for path in seen))
        def timeout(arguments, **options):
            folder = Path(arguments[-1]);seen.append(folder)
            (folder / 'synthetic-owned').write_bytes(b'SYNTHETIC')
            raise subprocess.TimeoutExpired(arguments, options['timeout'], output=b'PRIVATE_RAW_PATH')
        result = check.run_selfcheck(mode='mapped-path', runner=timeout, platform_check=lambda: True)
        self.assertEqual(result['reason'], 'mapped_selfcheck_worker_timeout')
        self.assertNotIn('PRIVATE_RAW_PATH', json.dumps(result))
        self.assertTrue(all(not path.exists() for path in seen))

    def test_parent_rejects_other_mode_private_fields_truncated_positive_and_incomplete_cleanup(self):
        invalid = [check._report('未验证', 'pss_selfcheck_worker_unavailable')]
        for key in ('native_api_attempted', 'fixture_created', 'fixture_closed', 'map_created',
                    'map_closed', 'mapped_query_completed', 'cleanup_succeeded', 'self_canary_exact_path_seen'):
            value = self.completed();value['diagnostics'][key] = False;invalid.append(value)
        value = self.completed();value['diagnostics']['mapped_query_truncated'] = True;invalid.append(value)
        value = self.completed();value['diagnostics']['mapped_name_characters'] = 32768;invalid.append(value)
        value = self.completed();value['diagnostics']['mapped_regions_created'] = True;invalid.append(value)
        value = self.completed();value['diagnostics']['path'] = CANARY;invalid.append(value)
        for document in invalid:
            with self.subTest(document=document):
                result = check.run_selfcheck(mode='mapped-path', platform_check=lambda: True,
                    runner=lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps(document).encode()))
                self.assertEqual(result['reason'], 'mapped_selfcheck_worker_output_invalid')
                self.assertNotIn(CANARY, json.dumps(result))

    @unittest.skipIf(sys.platform == 'win32', 'Isolated non-Windows mode dispatch only.')
    def test_isolated_mapped_cli_reports_unverified_without_running_pss_or_windows_api(self):
        completed = subprocess.run([sys.executable, '-I', '-S', '-B', '-X', 'utf8', check.__file__, '--mapped-path'],
                                   capture_output=True, timeout=3, check=False)
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b'')
        result = check._validated_report(completed.stdout, kind=check.MAPPED_KIND)
        self.assertEqual(result['kind'], check.MAPPED_KIND)
        self.assertEqual(result['status'], '未验证')
        self.assertEqual(result['reason'], 'mapped_selfcheck_windows_x64_required')
        self.assertFalse(result['diagnostics']['native_api_attempted'])


if __name__ == '__main__':
    unittest.main()
