"""Bounded, metadata-only check of the selected Windows process's DB account.

Uses two complete passes of documented mapped-file metadata queries, never
VA cloning, process-memory reads, database reads or process writes. A strictly
verified same-executable process family stays inside the owned short-lived child,
so a native API stall cannot hold the Web manager indefinitely. No paths leave
the child. Legacy PSS name parsing remains a low-level diagnostic only.

SDK references: Microsoft Learn VirtualQueryEx, QueryVirtualMemoryInformation,
GetMappedFileNameW, and processsnapshot PSS_HANDLE_ENTRY for legacy diagnostics.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from windows_keyproof_platform import FILETIME, PlatformError, _api, _path, path_is_local
from windows_process_detection import verified_process_family
from windows_mapped_files import MappedFilesAPI, MAPPED_COUNTS, mapped_paths

HANDLE_NAME_FLAGS = 0x04 | 0x08
PROCESS_METADATA_ACCESS = 0x400  # PROCESS_QUERY_INFORMATION only.
MAX_HANDLES = 32768
MAX_REGIONS = 32768
SNAPSHOT_COUNTS = (
    'handles_seen', 'handles_with_name', 'type_names_present', 'file_type_handles',
    'file_names_retained', 'all_named_db_candidates', 'database_names_seen',
    'matching_root_paths', 'other_root_paths', 'unresolved_database_paths')
DIAGNOSTIC_COUNTS = SNAPSHOT_COUNTS + MAPPED_COUNTS + (
    'family_process_count', 'family_processes_completed',
    'main_database_names_seen', 'helper_database_names_seen', 'mapped_verification_passes')
DIAGNOSTIC_BOOLEANS = ('query_completed', 'mapped_file_query_used')
DIAGNOSTIC_KEYS = {'schema_version', *DIAGNOSTIC_BOOLEANS, *DIAGNOSTIC_COUNTS}


def sanitize_diagnostics(value):
    """Public counts only: permit subsets, never names, IDs or native details."""
    if (not isinstance(value, dict) or not set(value).issubset(DIAGNOSTIC_KEYS)
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or any(type(value[key]) is not bool for key in DIAGNOSTIC_BOOLEANS if key in value)
            or any(type(value[key]) is not int or not 0 <= value[key] <= 32768
                   for key in DIAGNOSTIC_COUNTS if key in value)):
        return {}
    return {key: value[key] for key in ('schema_version', *DIAGNOSTIC_COUNTS, *DIAGNOSTIC_BOOLEANS) if key in value}


def _prepare_diagnostics(value, *, reset=False):
    if not isinstance(value, dict):
        return None
    clean = sanitize_diagnostics(value)
    if reset:
        clean = {}
    value.clear()
    value.update({'schema_version': 1, **{key: 0 for key in DIAGNOSTIC_COUNTS},
                  **{key: False for key in DIAGNOSTIC_BOOLEANS}})
    value.update(clean)
    return value


def _count(diagnostics, key):
    if diagnostics is not None:
        diagnostics[key] = min(32768, diagnostics[key] + 1)


def _database_path_shape(value):
    return re.fullmatch(r'(.+\\db_storage)\\(?:message\\message_[0-9]+\.db|contact\\contact\.db)(?:-wal|-shm)?',
                        ntpath.normcase(value.replace('/', '\\')))


class HandleEntry(C.Structure):
    # Windows x64 SDK layout. No type-specific fields are requested or used;
    # its largest union member (Thread) occupies 48 bytes, aligned to 8.
    _fields_ = [('Handle', C.c_void_p), ('Flags', C.c_uint32), ('ObjectType', C.c_uint32),
                ('CaptureTime', FILETIME), ('Attributes', C.c_uint32),
                ('GrantedAccess', C.c_uint32), ('HandleCount', C.c_uint32),
                ('PointerCount', C.c_uint32), ('PagedPoolCharge', C.c_uint32),
                ('NonPagedPoolCharge', C.c_uint32), ('CreationTime', FILETIME),
                ('TypeNameLength', C.c_uint16), ('TypeName', C.c_void_p),
                ('ObjectNameLength', C.c_uint16), ('ObjectName', C.c_void_p),
                ('TypeSpecificInformation', C.c_uint64 * 6)]


class _SnapshotAPI:
    def __init__(self):
        _api()  # Enforce Windows x64 before loading native symbols.
        kernel = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'PssCaptureSnapshot': ([C.c_void_p, C.c_uint32, C.c_uint32, C.POINTER(C.c_void_p)], C.c_uint32),
            'PssFreeSnapshot': ([C.c_void_p, C.c_void_p], C.c_uint32),
            'PssWalkMarkerCreate': ([C.c_void_p, C.POINTER(C.c_void_p)], C.c_uint32),
            'PssWalkMarkerFree': ([C.c_void_p], C.c_uint32),
            'PssWalkSnapshot': ([C.c_void_p, C.c_uint32, C.c_void_p, C.c_void_p, C.c_uint32], C.c_uint32),
            'QueryDosDeviceW': ([C.c_wchar_p, C.c_wchar_p, C.c_uint32], C.c_uint32),
            'GetLogicalDrives': ([], C.c_uint32),
        }
        for name, (arguments, result) in signatures.items():
            method = getattr(kernel, name)
            method.argtypes, method.restype = arguments, result
            setattr(self, name, method)


def _result(state, reason, diagnostics=None):
    value = {'state': state, 'matched': state == 'matched', 'reason': reason}
    clean = sanitize_diagnostics(diagnostics)
    if clean:
        value['diagnostics'] = clean
    return value


def classify_paths(paths, expected_root, device_map=None, *, diagnostics=None):
    """Pure classifier; ambiguous/unrecognized evidence never means matched."""
    diagnostics = _prepare_diagnostics(diagnostics)
    expected = ntpath.normcase(ntpath.normpath(expected_root))
    if not re.fullmatch(r'[a-z]:\\[^\x00:]*\\db_storage', expected):
        return _result('unknown', 'windows_account_root_invalid', diagnostics)
    roots = set()
    for item in paths:
        if not isinstance(item, str) or '\0' in item or len(item) > 32768:
            return _result('unknown', 'windows_account_handle_invalid', diagnostics)
        value = ntpath.normcase(item.replace('/', '\\'))
        if value.startswith('\\\\?\\'):
            value = value[4:]
        if value.startswith('\\device\\'):
            mapped = None
            for device, drive in (device_map or {}).items():
                if value.startswith(device.casefold() + '\\'):
                    mapped = drive + value[len(device):]
                    break
            value = mapped or value
        match = _database_path_shape(value)
        if match:
            _count(diagnostics, 'database_names_seen')
            # An unmatched device path is relevant evidence we cannot resolve.
            root = ntpath.normcase(ntpath.normpath(match[1]))
            if not re.fullmatch(r'[a-z]:\\[^\x00:]*', root):
                _count(diagnostics, 'unresolved_database_paths')
                return _result('unknown', 'windows_account_device_unresolved', diagnostics)
            _count(diagnostics, 'matching_root_paths' if root == expected else 'other_root_paths')
            roots.add(root)
    if not roots:
        return _result('unknown', 'windows_account_open_database_unavailable', diagnostics)
    if roots != {expected}:
        return _result('mismatch', 'windows_account_changed_or_ambiguous', diagnostics)
    return _result('matched', 'windows_account_open_database_matched', diagnostics)


def _wide(pointer, length):
    if not pointer or not 0 < length <= 65532 or length % 2:
        raise PlatformError('windows_account_handle_name_invalid')
    # SDK lengths remain byte counts. A captured name may include one terminal
    # UTF-16 NUL; remove exactly that terminator without accepting embedded NULs.
    value = C.wstring_at(pointer, length // 2)
    if value.endswith('\0'):
        value = value[:-1]
    if not value or '\0' in value:
        raise PlatformError('windows_account_handle_name_invalid')
    return value


def snapshot_paths(api, process_handle, *, clock=time.monotonic, diagnostics=None, deadline=None, max_handles=None):
    """Read SDK handle names only; always free this process's snapshot/marker."""
    snapshot, marker = C.c_void_p(), C.c_void_p()
    diagnostics = _prepare_diagnostics(diagnostics, reset=True)
    handle_limit = MAX_HANDLES if max_handles is None else max_handles
    if type(handle_limit) is not int or not 0 <= handle_limit <= MAX_HANDLES:
        raise PlatformError('windows_account_snapshot_handle_limit')
    end = min(clock() + 3, deadline) if deadline is not None else clock() + 3
    if C.sizeof(HandleEntry) != 136 or HandleEntry.ObjectName.offset != 80:
        raise PlatformError('windows_account_sdk_layout_invalid')
    try:
        if clock() > end:
            raise PlatformError('windows_account_snapshot_deadline')
        if api.PssCaptureSnapshot(process_handle, HANDLE_NAME_FLAGS, 0, C.byref(snapshot)) or not snapshot:
            raise PlatformError('windows_account_snapshot_unavailable')
        if api.PssWalkMarkerCreate(None, C.byref(marker)) or not marker:
            raise PlatformError('windows_account_marker_unavailable')
        paths = []
        for _ in range(handle_limit):
            if clock() > end:
                raise PlatformError('windows_account_snapshot_deadline')
            entry = HandleEntry()
            status = api.PssWalkSnapshot(snapshot, 2, marker, C.byref(entry), C.sizeof(entry))
            if status == 259:  # ERROR_NO_MORE_ITEMS: a complete snapshot only.
                if diagnostics is not None:
                    diagnostics['query_completed'] = True
                return paths
            if status:
                raise PlatformError('windows_account_snapshot_walk_failed')
            _count(diagnostics, 'handles_seen')
            if entry.TypeNameLength and entry.TypeName:
                _count(diagnostics, 'type_names_present')
            type_name = None
            if diagnostics is not None and entry.TypeNameLength and entry.TypeName:
                try:
                    type_name = _wide(entry.TypeName, entry.TypeNameLength).casefold()
                except PlatformError:
                    pass
                if type_name == 'file':
                    _count(diagnostics, 'file_type_handles')
            if entry.Flags & 2:
                _count(diagnostics, 'handles_with_name')
            object_name = None
            if diagnostics is not None and entry.Flags & 2 and entry.ObjectNameLength and entry.ObjectName:
                try:
                    object_name = _wide(entry.ObjectName, entry.ObjectNameLength)
                except PlatformError:
                    # A diagnostic for a non-File object must not alter the
                    # original File-only proof. File parsing below stays strict.
                    object_name = None
                if object_name is not None and _database_path_shape(object_name):
                    _count(diagnostics, 'all_named_db_candidates')
            # Some named non-file objects also contain paths. Require the SDK
            # type name and the name-present flag, then classify fixed DB paths.
            if not entry.Flags & 2 or not entry.TypeNameLength or not entry.TypeName:
                continue
            if (type_name if type_name is not None else _wide(entry.TypeName, entry.TypeNameLength).casefold()) != 'file':
                continue
            paths.append(object_name if object_name is not None else _wide(entry.ObjectName, entry.ObjectNameLength))
            _count(diagnostics, 'file_names_retained')
        raise PlatformError('windows_account_snapshot_handle_limit')
    finally:
        if marker:
            api.PssWalkMarkerFree(marker)
        if snapshot:
            # Snapshot is in this caller, not inside the selected process.
            api.PssFreeSnapshot(_api().GetCurrentProcess(), snapshot)


def _mapped_failure_reason(error):
    """Coarse fixed categories only; native details and arbitrary codes stay private."""
    code = getattr(error, 'code', None) if isinstance(error, PlatformError) else None
    categories = {
        'windows_account_mapped_api_unavailable': {
            'windows_mapped_api_unavailable', 'windows_x64_python_required'},
        'windows_account_mapped_name_unavailable': {
            'windows_mapped_name_invalid', 'windows_mapped_name_unavailable',
            'windows_account_handle_invalid'},
        'windows_account_mapped_budget_exceeded': {
            'windows_mapped_budget_invalid', 'windows_mapped_query_deadline',
            'windows_mapped_region_limit', 'windows_account_mapped_deadline',
            'windows_account_mapped_region_limit', 'windows_process_snapshot_deadline',
            'windows_process_snapshot_limit'},
        'windows_account_mapped_process_changed': {
            'windows_account_process_identity_invalid', 'windows_account_process_unavailable',
            'windows_account_process_query_failed', 'windows_account_executable_mismatch',
            'windows_account_process_time_unavailable', 'windows_account_process_reused',
            'windows_account_process_family_changed', 'windows_mapped_process_unavailable',
            'windows_mapped_allocation_changed', 'windows_process_access_unavailable',
            'windows_process_family_ambiguous', 'windows_process_family_cycle',
            'windows_process_family_disconnected', 'windows_process_family_identity_changed',
            'windows_process_family_invalid', 'windows_process_family_parent_reused',
            'windows_process_path_unavailable', 'windows_process_snapshot_unavailable',
            'windows_process_version_unavailable', 'windows_process_version_unsupported',
            'windows_process_architecture_unsupported'},
        'windows_account_mapped_region_unavailable': {
            'windows_mapped_allocation_invalid', 'windows_mapped_allocation_kind_unknown',
            'windows_mapped_allocation_unavailable', 'windows_mapped_metadata_unavailable',
            'windows_mapped_region_invalid', 'windows_mapped_region_kind_unknown',
            'windows_mapped_sdk_layout_invalid', 'windows_mapped_system_bounds_invalid',
            'windows_process_sdk_layout_invalid', 'windows_account_mapped_incomplete'},
    }
    if isinstance(code, str):
        for public, codes in categories.items():
            if code in codes:
                return public
    return 'windows_account_mapped_query_unavailable'


def probe_direct(pid, creation_time, executable, db_root, *, diagnostics=None, clock=time.monotonic):
    """Only two complete, account-bound mapped-file passes may establish a match."""
    public_diagnostics = diagnostics
    totals = _prepare_diagnostics({}, reset=True)
    totals['mapped_file_query_used'] = True
    deadline = clock() + 3

    def budget():
        if clock() > deadline:
            raise PlatformError('windows_account_mapped_deadline')

    def finish(state, reason):
        if isinstance(public_diagnostics, dict):
            public_diagnostics.clear()
            public_diagnostics.update(totals)
        return _result(state, reason, totals if public_diagnostics is not None else None)

    try:
        if type(pid) is not int or not 0 < pid <= 0xffffffff or type(creation_time) is not int or not 0 < creation_time < 2**64:
            raise PlatformError('windows_account_process_identity_invalid')
        executable, db_root = _path(executable), _path(db_root)
        if not path_is_local(executable) or not path_is_local(db_root, require_dir=True):
            raise PlatformError('windows_account_path_unsafe')
        family = verified_process_family(executable, pid, creation_time, clock=clock, deadline=deadline)
        totals['family_process_count'] = len(family)
        budget()
        native, api = _api(), MappedFilesAPI()
        device_map = {}
        drives = api.GetLogicalDrives()
        for index in range(26):
            budget()
            if drives & (1 << index):
                drive, target = chr(65 + index) + ':', C.create_unicode_buffer(32768)
                if api.QueryDosDeviceW(drive, target, len(target)):
                    device_map[target.value.casefold()] = drive.casefold()

        classifications = []
        for _ in range(2):
            pass_paths = []
            totals['family_processes_completed'] = 0
            for member in sorted(family, key=lambda row: (row['pid'] != pid, row['pid'])):
                budget()
                handle = native.OpenProcess(PROCESS_METADATA_ACCESS, False, member['pid'])
                if not handle:
                    raise PlatformError('windows_account_process_unavailable')
                try:
                    image, size = C.create_unicode_buffer(32768), C.c_uint32(32768)
                    timestamps = [FILETIME() for _ in range(4)]

                    def verify():
                        budget()
                        size.value = len(image)
                        if not native.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)):
                            raise PlatformError('windows_account_process_query_failed')
                        if (not 0 < size.value < len(image)
                                or ntpath.normcase(image.value) != ntpath.normcase(executable)):
                            raise PlatformError('windows_account_executable_mismatch')
                        if not native.GetProcessTimes(handle, *(C.byref(x) for x in timestamps)):
                            raise PlatformError('windows_account_process_time_unavailable')
                        if timestamps[0].low | timestamps[0].high << 32 != member['creation_time']:
                            raise PlatformError('windows_account_process_reused')
                        budget()

                    verify()
                    member_diagnostics = {}
                    try:
                        member_paths = mapped_paths(api, handle, diagnostics=member_diagnostics, clock=clock,
                            deadline=deadline, max_regions=MAX_REGIONS - totals['mapped_regions_queried'])
                    finally:
                        for key in MAPPED_COUNTS:
                            count = member_diagnostics.get(key, 0)
                            if type(count) is not int or not 0 <= count <= MAX_REGIONS:
                                raise PlatformError('windows_account_mapped_diagnostic_invalid')
                            accumulated = totals[key] + count
                            totals[key] = min(32768, accumulated)
                            if key == 'mapped_regions_queried' and accumulated > MAX_REGIONS:
                                raise PlatformError('windows_account_mapped_region_limit')
                    verify()
                    if member_diagnostics.get('query_completed') is not True:
                        raise PlatformError('windows_account_mapped_incomplete')
                    totals['family_processes_completed'] += 1
                    key = 'main_database_names_seen' if member['pid'] == pid else 'helper_database_names_seen'
                    for path in member_paths:
                        if not isinstance(path, str) or '\0' in path or len(path) > 32768:
                            raise PlatformError('windows_account_handle_invalid')
                        if _database_path_shape(path):
                            _count(totals, key)
                    pass_paths.extend(member_paths)
                finally:
                    if not native.CloseHandle(handle):
                        raise PlatformError('windows_account_process_close_failed')
            budget()
            classified = classify_paths(pass_paths, db_root, device_map, diagnostics=totals)
            classifications.append((classified['state'], classified['reason']))
            totals['mapped_verification_passes'] += 1

        budget()
        after = verified_process_family(executable, pid, creation_time, clock=clock, deadline=deadline)
        if after != family:
            raise PlatformError('windows_account_process_family_changed')
        budget()
        totals['query_completed'] = True
        if any(state == 'mismatch' for state, _ in classifications):
            return finish('mismatch', 'windows_account_changed_or_ambiguous')
        if all(state == 'matched' for state, _ in classifications):
            return finish('matched', 'windows_account_mapped_database_matched')
        reason = next(reason for state, reason in classifications if state != 'matched')
        if reason == 'windows_account_open_database_unavailable':
            reason = 'windows_account_mapped_database_unavailable'
        return finish('unknown', reason)
    except Exception as error:
        totals['query_completed'] = False
        return finish('unknown', _mapped_failure_reason(error))


class WindowsAccountProbe:
    def __init__(self, pid, creation_time, executable, db_root, *, runner=subprocess.run):
        self.pid, self.creation_time = pid, creation_time
        self.executable, self.db_root = str(executable), str(db_root)
        self.runner = runner

    def __call__(self):
        arguments = [sys.executable, '-I', '-S', '-B', '-X', 'utf8', str(Path(__file__).resolve()),
                     '--pid', str(self.pid), '--creation-time', str(self.creation_time),
                     '--exe', self.executable, '--root', self.db_root]
        try:
            options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.PIPE,
                       'stderr': subprocess.DEVNULL, 'timeout': 6, 'check': False, 'shell': False}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NO_WINDOW
            completed = self.runner(arguments, **options)
            if completed.returncode != 0 or len(completed.stdout) > 4096:
                raise ValueError
            def pairs(items):
                parsed = {}
                for key, value in items:
                    if key in parsed:
                        raise ValueError
                    parsed[key] = value
                return parsed
            result = json.loads(completed.stdout, object_pairs_hook=pairs)
            if (not isinstance(result, dict) or set(result) not in (
                    {'state', 'matched', 'reason'}, {'state', 'matched', 'reason', 'diagnostics'})
                    or result['state'] not in ('matched', 'mismatch', 'unknown')
                    or type(result['matched']) is not bool or result['matched'] != (result['state'] == 'matched')
                    or not isinstance(result['reason'], str) or not re.fullmatch(r'windows_account_[a-z_]{1,80}', result['reason'])):
                raise ValueError
            if 'diagnostics' in result:
                clean = sanitize_diagnostics(result['diagnostics'])
                if not clean:
                    raise ValueError
                if result['matched'] and clean.get('query_completed') is not True:
                    raise ValueError
                result['diagnostics'] = clean
            if result['matched'] and result['reason'] == 'windows_account_mapped_database_matched':
                clean = result.get('diagnostics', {})
                if (clean.get('mapped_file_query_used') is not True
                        or clean.get('mapped_verification_passes') != 2
                        or type(clean.get('family_process_count')) is not int
                        or not 1 <= clean['family_process_count'] <= 16
                        or clean.get('family_processes_completed') != clean['family_process_count']):
                    raise ValueError
            return result
        except Exception:
            return _result('unknown', 'windows_account_probe_unavailable')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--creation-time', type=int, required=True)
    parser.add_argument('--exe', required=True)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    print(json.dumps(probe_direct(args.pid, args.creation_time, args.exe, args.root, diagnostics={})))


if __name__ == '__main__':
    main()
