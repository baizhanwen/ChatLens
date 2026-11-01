"""Mapped-file metadata contracts with explicitly synthetic Windows APIs."""
import ctypes as C
from contextlib import redirect_stdout
import io
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
import windows_mapped_files as mapped


FILE = r'\Device\HarddiskVolume3\Users\synthetic\客户\db_storage\message\message_0.db-shm'


def put(pointer, kind, value):
    C.cast(pointer, C.POINTER(kind))[0] = kind(value)


def region(base, size=0x1000, *, state=mapped.MEM_COMMIT, kind=mapped.MEM_MAPPED,
           allocation=None, flag='MappedDataFile', name=FILE):
    return {'base': base, 'size': size, 'state': state, 'kind': kind,
            'allocation': base if allocation is None else allocation,
            'flag': flag, 'name': name}


class SyntheticAPI:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            region(0x1000, state=mapped.MEM_FREE),
            region(0x2000, kind=mapped.MEM_PRIVATE),
            region(0x3000, kind=mapped.MEM_IMAGE),
            region(0x4000),
            region(0x5000, flag='MappedPageFile'),
            region(0x6000, flag='MappedPhysical'),
            region(0x7000, size=0x3000, state=mapped.MEM_FREE)]
        self.minimum, self.maximum, self.page, self.architecture = 0x1000, 0x9fff, 0x1000, 9
        self.calls, self.basic_counts, self.allocation_counts = [], {}, {}
        self.basic_size, self.allocation_size = C.sizeof(mapped.MBI64), C.sizeof(mapped.MemoryRegionInformation)
        self.after_basic = lambda address, count, info: None
        self.after_allocation = lambda address, count, info: None
        self.after_name = lambda address, buffer: None
        self.name_result = None

    def GetNativeSystemInfo(self, target):
        self.calls.append(('system',))
        value = C.cast(target, C.POINTER(mapped.SystemInfo)).contents
        value.wProcessorArchitecture = self.architecture
        value.dwPageSize = self.page
        value.lpMinimumApplicationAddress, value.lpMaximumApplicationAddress = self.minimum, self.maximum

    def _row(self, address):
        return next(row for row in self.rows if row['base'] <= address < row['base'] + row['size'])

    def VirtualQueryEx(self, handle, address, target, size):
        self.calls.append(('basic', handle, address, size))
        row = self._row(address)
        value = C.cast(target, C.POINTER(mapped.MBI64)).contents
        value.BaseAddress, value.RegionSize = row['base'], row['size']
        value.AllocationBase, value.AllocationProtect = row['allocation'], 4
        value.State, value.Protect, value.Type = row['state'], 4, row['kind']
        self.basic_counts[address] = self.basic_counts.get(address, 0) + 1
        self.after_basic(address, self.basic_counts[address], value)
        return self.basic_size

    def QueryVirtualMemoryInformation(self, handle, address, kind, target, size, returned):
        self.calls.append(('allocation', handle, address, kind, size))
        row = self._row(address)
        value = C.cast(target, C.POINTER(mapped.MemoryRegionInformation)).contents
        value.AllocationBase, value.AllocationProtect = row['allocation'], 4
        value.RegionSize, value.CommitSize = row['size'], row['size']
        if row['flag']:
            setattr(value, row['flag'], 1)
        self.allocation_counts[address] = self.allocation_counts.get(address, 0) + 1
        self.after_allocation(address, self.allocation_counts[address], value)
        put(returned, C.c_size_t, self.allocation_size)
        return True

    def K32GetMappedFileNameW(self, handle, address, buffer, capacity):
        self.calls.append(('name', handle, address, capacity))
        name = self._row(address)['name']
        if name is not None:
            buffer.value = name
        self.after_name(address, buffer)
        return self.name_result if self.name_result is not None else (len(name) if name is not None else 0)

    def __getattr__(self, name):
        if name in ('ReadProcessMemory', 'WriteProcessMemory', 'OpenProcess', 'CloseHandle',
                    'DuplicateHandle', 'PssCaptureSnapshot', 'ReadFile'):
            raise AssertionError('Metadata enumeration must not call ' + name)
        raise AttributeError(name)


class MappedFilesTests(unittest.TestCase):
    def run_query(self, api=None, **options):
        api = api or SyntheticAPI()
        detail = {}
        paths = mapped.mapped_paths(api, 42, diagnostics=detail, clock=lambda: 0, **options)
        return api, detail, paths

    def assert_incomplete(self, api, *, expected=None, **options):
        detail, output = {}, io.StringIO()
        with redirect_stdout(output), self.assertRaises(mapped.PlatformError) as raised:
            mapped.mapped_paths(api, 42, diagnostics=detail, clock=lambda: 0, **options)
        self.assertFalse(detail['query_completed'])
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn(FILE, str(raised.exception))
        if expected is not None:
            self.assertEqual(raised.exception.code, expected)
        self.assertEqual(set(detail), {*mapped.MAPPED_COUNTS, 'query_completed'})
        return detail

    def test_complete_traversal_returns_unicode_name_and_skips_only_classified_nonfiles(self):
        api, detail, paths = self.run_query()
        self.assertEqual(paths, [FILE])
        self.assertTrue(detail['query_completed'])
        self.assertEqual(detail['mapped_data_regions_seen'], 1)
        self.assertEqual(detail['mapped_pagefile_regions_skipped'], 1)
        self.assertEqual(detail['mapped_names_seen'], 1)
        self.assertEqual(detail['mapped_name_query_failures'], 0)
        self.assertEqual(detail['mapped_regions_queried'], 10)
        self.assertEqual([row[2] for row in api.calls if row[0] == 'name'], [0x4000])
        self.assertTrue(all(row[1] == 42 for row in api.calls if row[0] != 'system'))
        self.assertTrue(all(row[3] == 0 for row in api.calls if row[0] == 'allocation'))

    def test_sdk_uses_native_bounds_and_counts_before_and_after_queries(self):
        api = SyntheticAPI([region(0x9000, state=mapped.MEM_RESERVE)])
        api.minimum, api.maximum = 0x9000, 0x9fff
        _, detail, paths = self.run_query(api)
        self.assertEqual(paths, [FILE])
        self.assertEqual(detail['mapped_regions_queried'], 2)
        self.assertEqual([row[2] for row in api.calls if row[0] == 'basic'], [0x9000, 0x9000])
        self.assertEqual(api.calls[0], ('system',))

    def test_direct_mapped_data_file_is_not_silently_treated_as_anonymous(self):
        for flags in (('DirectMapped',), ('DirectMapped', 'MappedDataFile')):
            with self.subTest(flags=flags):
                api = SyntheticAPI([region(0x1000, flag=flags[0])])
                api.maximum = 0x1fff
                if len(flags) == 2:
                    api.after_allocation = lambda address, count, info: setattr(info, 'MappedDataFile', 1)
                self.assertEqual(self.run_query(api)[2], [FILE])

    def test_data_mapping_with_no_name_rejects_entire_result_even_after_a_valid_file(self):
        api = SyntheticAPI([region(0x1000), region(0x2000, name=None)])
        api.maximum = 0x2fff
        detail = self.assert_incomplete(api, expected='windows_mapped_name_unavailable')
        self.assertEqual(detail['mapped_names_seen'], 1)
        self.assertEqual(detail['mapped_name_query_failures'], 1)
        self.assertEqual(detail['mapped_data_regions_seen'], 2)

    def test_length_is_wchar_count_and_invalid_or_truncated_names_do_not_pass(self):
        self.assertEqual(self.run_query()[2], [FILE])
        for length in (0, mapped.NAME_CAPACITY, mapped.NAME_CAPACITY + 1, True, -1):
            with self.subTest(length=length):
                api = SyntheticAPI()
                api.name_result = length
                detail = self.assert_incomplete(api, expected='windows_mapped_name_unavailable')
                self.assertEqual(detail['mapped_name_query_failures'], 1)
        for value in (FILE + '\0', FILE[:10] + '\0' + FILE[10:]):
            with self.subTest(value=value):
                api = SyntheticAPI([region(0x1000, name=value)])
                api.maximum = 0x1fff
                self.assert_incomplete(api, expected='windows_mapped_name_invalid')
        api = SyntheticAPI()
        def not_terminated(address, buffer):
            buffer[len(FILE)] = 'X'
        api.after_name = not_terminated
        self.assert_incomplete(api, expected='windows_mapped_name_invalid')

    def test_uncertain_or_inconsistent_allocation_classes_cannot_skip_filename_evidence(self):
        for flag, extra in ((None, None), ('Private', None), ('MappedImage', None),
                            ('MappedDataFile', 'MappedPageFile'), ('MappedPageFile', 'MappedPhysical')):
            with self.subTest(flag=flag, extra=extra):
                api = SyntheticAPI([region(0x1000, flag=flag)])
                api.maximum = 0x1fff
                if extra:
                    api.after_allocation = lambda address, count, info: setattr(info, extra, 1)
                self.assert_incomplete(api, expected='windows_mapped_allocation_kind_unknown')
        api = SyntheticAPI()
        api.after_allocation = lambda address, count, info: setattr(info, 'Reserved', 1)
        self.assert_incomplete(api, expected='windows_mapped_allocation_invalid')

    def test_changes_in_region_protection_or_backing_kind_invalidate_observation(self):
        api = SyntheticAPI()
        api.after_basic = lambda address, count, info: setattr(info, 'Protect', 2) if address == 0x4000 and count == 2 else None
        self.assert_incomplete(api, expected='windows_mapped_allocation_changed')
        api = SyntheticAPI()
        def different_backing(address, count, info):
            if address == 0x4000 and count == 2:
                info.MappedDataFile, info.MappedPageFile = 0, 1
        api.after_allocation = different_backing
        self.assert_incomplete(api, expected='windows_mapped_allocation_changed')
        api = SyntheticAPI()
        def pagefile_becomes_file(address, count, info):
            if address == 0x5000 and count == 2:
                info.MappedDataFile, info.MappedPageFile = 1, 0
        api.after_allocation = pagefile_becomes_file
        self.assert_incomplete(api, expected='windows_mapped_allocation_changed')

    def test_copy_on_write_commit_charge_can_change_without_changing_file_identity(self):
        api = SyntheticAPI()
        api.after_allocation = lambda address, count, info: setattr(info, 'CommitSize', 0) if count == 2 else None
        self.assertEqual(self.run_query(api)[2], [FILE])

    def test_whole_allocation_can_cover_multiple_basic_regions_without_losing_names(self):
        api = SyntheticAPI([region(0x1000), region(0x2000, allocation=0x1000)])
        api.maximum = 0x2fff
        api.after_allocation = lambda address, count, info: setattr(info, 'RegionSize', 0x2000)
        self.assertEqual(self.run_query(api)[2], [FILE, FILE])

    def test_query_structures_and_success_flags_must_be_complete(self):
        for size in (0, 8, C.sizeof(mapped.MBI64) + 8, True):
            with self.subTest(basic_size=size):
                api = SyntheticAPI();api.basic_size = size
                self.assert_incomplete(api, expected='windows_mapped_region_invalid')
        for size in (0, 8, C.sizeof(mapped.MemoryRegionInformation) + 8):
            with self.subTest(allocation_size=size):
                api = SyntheticAPI();api.allocation_size = size
                self.assert_incomplete(api, expected='windows_mapped_allocation_invalid')
        api = SyntheticAPI()
        api.QueryVirtualMemoryInformation = lambda *_: False
        self.assert_incomplete(api, expected='windows_mapped_allocation_unavailable')

    def test_malformed_regions_or_native_bounds_cannot_finish_enumeration(self):
        for field, value in (('BaseAddress', 0x5000), ('RegionSize', 0),
                             ('RegionSize', 1), ('RegionSize', 2**64 - 0x1000),
                             ('AllocationBase', 0), ('State', 999), ('Type', 999)):
            with self.subTest(field=field):
                api = SyntheticAPI([region(0x1000)])
                api.maximum = 0x1fff
                api.after_basic = lambda address, count, info: setattr(info, field, value)
                self.assert_incomplete(api)
        for field, value in (('page', 3), ('minimum', 0), ('maximum', 0x9001), ('architecture', 12)):
            with self.subTest(system=field):
                api = SyntheticAPI();setattr(api, field, value)
                self.assert_incomplete(api, expected='windows_mapped_system_bounds_invalid')

    def test_large_free_tail_is_clipped_to_system_limit_not_guessed_by_an_error_code(self):
        api = SyntheticAPI([region(0x1000, size=0x100000000, state=mapped.MEM_FREE)])
        _, detail, paths = self.run_query(api)
        self.assertEqual(paths, [])
        self.assertTrue(detail['query_completed'])
        self.assertEqual(detail['mapped_regions_queried'], 1)
        api = SyntheticAPI()
        api.VirtualQueryEx = lambda *_: 0
        self.assert_incomplete(api, expected='windows_mapped_region_invalid')

    def test_budget_counts_rechecks_and_does_not_return_a_partial_path(self):
        api = SyntheticAPI([region(0x1000)])
        api.maximum = 0x1fff
        detail = self.assert_incomplete(api, max_regions=1, expected='windows_mapped_region_limit')
        self.assertEqual(detail['mapped_regions_queried'], 1)
        self.assertEqual(detail['mapped_names_seen'], 0)
        self.assert_incomplete(SyntheticAPI(), max_regions=0, expected='windows_mapped_region_limit')
        self.assert_incomplete(SyntheticAPI(), deadline=0, expected='windows_mapped_query_deadline')
        for value in (True, -1, 32769):
            self.assert_incomplete(SyntheticAPI(), max_regions=value, expected='windows_mapped_budget_invalid')
        for value in (float('nan'), float('inf'), 'private-value'):
            self.assert_incomplete(SyntheticAPI(), deadline=value, expected='windows_mapped_budget_invalid')

    def test_clock_budget_includes_system_and_name_metadata_calls(self):
        current = [0]
        api = SyntheticAPI()
        original = api.K32GetMappedFileNameW
        def slow_name(*arguments):
            result = original(*arguments)
            current[0] = 3
            return result
        api.K32GetMappedFileNameW = slow_name
        detail = {}
        with self.assertRaises(mapped.PlatformError) as raised:
            mapped.mapped_paths(api, 42, diagnostics=detail, clock=lambda: current[0], deadline=3)
        self.assertEqual(raised.exception.code, 'windows_mapped_query_deadline')
        self.assertFalse(detail['query_completed'])

    def test_unexpected_native_errors_never_escape_as_paths_or_messages(self):
        api = SyntheticAPI()
        def unavailable(*_):
            raise OSError(FILE)
        api.K32GetMappedFileNameW = unavailable
        detail = self.assert_incomplete(api, expected='windows_mapped_name_unavailable')
        self.assertEqual(detail['mapped_name_query_failures'], 1)

    def test_api_signatures_and_sdk_layout_are_explicit_and_load_no_read_functions(self):
        class Function:
            pass
        class DLL:
            def __getattr__(self, name):
                method = Function();setattr(self, name, method);return method
        loads = []
        def dll(name, **options):
            loads.append((name, options));return DLL()
        with patch.object(mapped.os, 'name', 'nt'), patch.object(mapped.C, 'WinDLL', create=True, side_effect=dll):
            api = mapped.MappedFilesAPI()
        self.assertEqual([row[0] for row in loads], ['kernel32', 'api-ms-win-core-memory-l1-1-4.dll'])
        self.assertEqual(C.sizeof(mapped.SystemInfo), 48)
        self.assertEqual(C.sizeof(mapped.MBI64), 48)
        self.assertEqual(C.sizeof(mapped.MemoryRegionInformation), 32)
        self.assertEqual(mapped.MemoryRegionInformation.RegionSize.offset, 16)
        self.assertEqual(api.GetNativeSystemInfo.argtypes, [C.POINTER(mapped.SystemInfo)])
        self.assertEqual(api.QueryVirtualMemoryInformation.argtypes[-1], C.POINTER(C.c_size_t))
        self.assertEqual(api.K32GetMappedFileNameW.argtypes[-1], C.c_uint32)
        self.assertFalse(hasattr(api, 'ReadProcessMemory'))
        self.assertFalse(hasattr(api, 'OpenProcess'))
        self.assertFalse(hasattr(api, 'DuplicateHandle'))


if __name__ == '__main__':
    unittest.main()
