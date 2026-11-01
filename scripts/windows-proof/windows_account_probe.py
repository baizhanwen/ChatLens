"""Bounded, metadata-only check of the selected Windows process's DB account.

Uses documented Process Snapshotting handle-name capture (flags 0x0c), never
VA cloning, process-memory reads, database reads, process writes or discovery
of another process. Run in an owned short-lived child so a native API stall
cannot hold the Web manager indefinitely. No paths leave the child.

SDK references: Microsoft Learn processsnapshot PssCaptureSnapshot,
PssWalkSnapshot, PSS_HANDLE_ENTRY and PSS_CAPTURE_FLAGS.
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

HANDLE_NAME_FLAGS = 0x04 | 0x08
PROCESS_METADATA_ACCESS = 0x400 | 0x40 | 0x10
MAX_HANDLES = 32768


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


def _result(state, reason):
    return {'state': state, 'matched': state == 'matched', 'reason': reason}


def classify_paths(paths, expected_root, device_map=None):
    """Pure classifier; ambiguous/unrecognized evidence never means matched."""
    expected = ntpath.normcase(ntpath.normpath(expected_root))
    if not re.fullmatch(r'[a-z]:\\[^\x00:]*\\db_storage', expected):
        return _result('unknown', 'windows_account_root_invalid')
    roots = set()
    for item in paths:
        if not isinstance(item, str) or '\0' in item or len(item) > 32768:
            return _result('unknown', 'windows_account_handle_invalid')
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
        match = re.fullmatch(r'(.+\\db_storage)\\(?:message\\message_[0-9]+\.db|contact\\contact\.db)(?:-wal|-shm)?', value)
        if match:
            # An unmatched device path is relevant evidence we cannot resolve.
            root = ntpath.normcase(ntpath.normpath(match[1]))
            if not re.fullmatch(r'[a-z]:\\[^\x00:]*', root):
                return _result('unknown', 'windows_account_device_unresolved')
            roots.add(root)
    if not roots:
        return _result('unknown', 'windows_account_open_database_unavailable')
    if roots != {expected}:
        return _result('mismatch', 'windows_account_changed_or_ambiguous')
    return _result('matched', 'windows_account_open_database_matched')


def _wide(pointer, length):
    if not pointer or not 0 < length <= 65532 or length % 2:
        raise PlatformError('windows_account_handle_name_invalid')
    return C.wstring_at(pointer, length // 2)


def snapshot_paths(api, process_handle, *, clock=time.monotonic):
    """Read SDK handle names only; always free this process's snapshot/marker."""
    snapshot, marker = C.c_void_p(), C.c_void_p()
    if C.sizeof(HandleEntry) != 136 or HandleEntry.ObjectName.offset != 80:
        raise PlatformError('windows_account_sdk_layout_invalid')
    try:
        if api.PssCaptureSnapshot(process_handle, HANDLE_NAME_FLAGS, 0, C.byref(snapshot)) or not snapshot:
            raise PlatformError('windows_account_snapshot_unavailable')
        if api.PssWalkMarkerCreate(None, C.byref(marker)) or not marker:
            raise PlatformError('windows_account_marker_unavailable')
        paths, deadline = [], clock() + 3
        for _ in range(MAX_HANDLES):
            if clock() > deadline:
                raise PlatformError('windows_account_snapshot_deadline')
            entry = HandleEntry()
            status = api.PssWalkSnapshot(snapshot, 2, marker, C.byref(entry), C.sizeof(entry))
            if status == 259:  # ERROR_NO_MORE_ITEMS: a complete snapshot only.
                return paths
            if status:
                raise PlatformError('windows_account_snapshot_walk_failed')
            # Some named non-file objects also contain paths. Require the SDK
            # type name and the name-present flag, then classify fixed DB paths.
            if not entry.Flags & 2 or not entry.TypeNameLength or not entry.TypeName:
                continue
            if _wide(entry.TypeName, entry.TypeNameLength).casefold() != 'file':
                continue
            paths.append(_wide(entry.ObjectName, entry.ObjectNameLength))
        raise PlatformError('windows_account_snapshot_handle_limit')
    finally:
        if marker:
            api.PssWalkMarkerFree(marker)
        if snapshot:
            # Snapshot is in this caller, not inside the selected process.
            api.PssFreeSnapshot(_api().GetCurrentProcess(), snapshot)


def probe_direct(pid, creation_time, executable, db_root):
    """Child-only native operation. Returns no path, username or file bytes."""
    handle = None
    try:
        if type(pid) is not int or not 0 < pid <= 0xffffffff or type(creation_time) is not int or not 0 < creation_time < 2**64:
            raise PlatformError('windows_account_process_identity_invalid')
        executable, db_root = _path(executable), _path(db_root)
        if not path_is_local(executable) or not path_is_local(db_root, require_dir=True):
            raise PlatformError('windows_account_path_unsafe')
        native, api = _api(), _SnapshotAPI()
        handle = native.OpenProcess(PROCESS_METADATA_ACCESS, False, pid)
        if not handle:
            raise PlatformError('windows_account_process_unavailable')
        image, size = C.create_unicode_buffer(32768), C.c_uint32(32768)
        timestamps = [FILETIME() for _ in range(4)]
        def verify():
            size.value = len(image)  # QueryFullProcessImageNameW writes length back.
            if not native.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)):
                raise PlatformError('windows_account_process_query_failed')
            if ntpath.normcase(image.value) != ntpath.normcase(executable):
                raise PlatformError('windows_account_executable_mismatch')
            if not native.GetProcessTimes(handle, *(C.byref(x) for x in timestamps)):
                raise PlatformError('windows_account_process_time_unavailable')
            if timestamps[0].low | timestamps[0].high << 32 != creation_time:
                raise PlatformError('windows_account_process_reused')
        verify()
        device_map = {}
        drives = api.GetLogicalDrives()
        for index in range(26):
            if drives & (1 << index):
                drive, target = chr(65 + index) + ':', C.create_unicode_buffer(32768)
                if api.QueryDosDeviceW(drive, target, len(target)):
                    device_map[target.value.casefold()] = drive.casefold()
        paths = snapshot_paths(api, handle)
        verify()
        return classify_paths(paths, db_root, device_map)
    except Exception:
        return _result('unknown', 'windows_account_probe_unavailable')
    finally:
        if handle:
            _api().CloseHandle(handle)


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
            if completed.returncode != 0 or len(completed.stdout) > 1024:
                raise ValueError
            result = json.loads(completed.stdout)
            if (not isinstance(result, dict) or set(result) != {'state', 'matched', 'reason'}
                    or result['state'] not in ('matched', 'mismatch', 'unknown')
                    or type(result['matched']) is not bool or result['matched'] != (result['state'] == 'matched')
                    or not isinstance(result['reason'], str) or not re.fullmatch(r'windows_account_[a-z_]{1,80}', result['reason'])):
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
    print(json.dumps(probe_direct(args.pid, args.creation_time, args.exe, args.root)))


if __name__ == '__main__':
    main()
