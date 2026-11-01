"""Read-only discovery of the original WeChat executable, not a login claim.

Documented Toolhelp process metadata, top-level window PIDs (no titles), fixed
version resources and process creation time only. No process memory, messages,
keys, shell, privilege changes or automatic path/WeChat-version migration.
"""
from __future__ import annotations

import ctypes as C
import ntpath
import time

from windows_keyproof_platform import FILETIME, PlatformError, _api, _path, path_is_local

SUPPORTED_VERSION = (4, 1, 15, 56)
MAX_PROCESSES = 32768
CALLBACK = getattr(C, 'WINFUNCTYPE', C.CFUNCTYPE)(C.c_int32, C.c_void_p, C.c_ssize_t)


class ProcessEntry(C.Structure):
    _fields_ = [('dwSize', C.c_uint32), ('cntUsage', C.c_uint32), ('th32ProcessID', C.c_uint32),
                ('th32DefaultHeapID', C.c_uint64), ('th32ModuleID', C.c_uint32),
                ('cntThreads', C.c_uint32), ('th32ParentProcessID', C.c_uint32),
                ('pcPriClassBase', C.c_int32), ('dwFlags', C.c_uint32),
                ('szExeFile', C.c_uint16 * 260)]


class FixedVersion(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in (
        'signature', 'structure_version', 'file_ms', 'file_ls', 'product_ms',
        'product_ls', 'flags_mask', 'flags', 'os', 'type', 'subtype', 'date_ms', 'date_ls')]


class _DetectionAPI:
    def __init__(self):
        _api()
        kernel = C.WinDLL('kernel32', use_last_error=True)
        user = C.WinDLL('user32', use_last_error=True)
        version = C.WinDLL('version', use_last_error=True)
        signatures = {
            'CreateToolhelp32Snapshot': (kernel, [C.c_uint32, C.c_uint32], C.c_void_p),
            'Process32FirstW': (kernel, [C.c_void_p, C.POINTER(ProcessEntry)], C.c_int32),
            'Process32NextW': (kernel, [C.c_void_p, C.POINTER(ProcessEntry)], C.c_int32),
            'EnumWindows': (user, [CALLBACK, C.c_ssize_t], C.c_int32),
            'GetWindowThreadProcessId': (user, [C.c_void_p, C.POINTER(C.c_uint32)], C.c_uint32),
            'GetFileVersionInfoSizeW': (version, [C.c_wchar_p, C.POINTER(C.c_uint32)], C.c_uint32),
            'GetFileVersionInfoW': (version, [C.c_wchar_p, C.c_uint32, C.c_uint32, C.c_void_p], C.c_int32),
            'VerQueryValueW': (version, [C.c_void_p, C.c_wchar_p, C.POINTER(C.c_void_p), C.POINTER(C.c_uint32)], C.c_int32),
        }
        for name, (library, arguments, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)


def result(state, reason, candidate=None):
    return {'state': state, 'reason': reason,
            'pid': candidate['pid'] if candidate else None,
            'creation_time': candidate['creation_time'] if candidate else None}


def select_candidate(candidates, window_pids=()):
    """Hidden/tray WeChat can have a main process without a visible window."""
    if not candidates:
        return result('not_running', 'windows_process_not_running')
    if len(candidates) > 128 or len({row['pid'] for row in candidates}) != len(candidates):
        return result('unavailable', 'windows_process_candidates_invalid')
    identifiers = {row['pid'] for row in candidates}
    roots = [row for row in candidates if row['parent_pid'] not in identifiers]
    if len(roots) == 1:
        return result('ready', 'windows_process_candidate_ready', roots[0])
    windows = [row for row in candidates if row['pid'] in set(window_pids)]
    if len(windows) == 1:
        return result('ready', 'windows_process_candidate_ready', windows[0])
    if len(candidates) == 1:
        return result('ready', 'windows_process_candidate_ready', candidates[0])
    return result('ambiguous', 'windows_process_multiple_candidates')


def executable_version(api, executable):
    ignored = C.c_uint32()
    size = api.GetFileVersionInfoSizeW(executable, C.byref(ignored))
    if not 52 <= size <= 1024 * 1024:
        raise PlatformError('windows_process_version_unavailable')
    buffer = C.create_string_buffer(size)
    if not api.GetFileVersionInfoW(executable, 0, size, buffer):
        raise PlatformError('windows_process_version_unavailable')
    pointer, length = C.c_void_p(), C.c_uint32()
    if not api.VerQueryValueW(buffer, '\\', C.byref(pointer), C.byref(length)) or not pointer or length.value < C.sizeof(FixedVersion):
        raise PlatformError('windows_process_version_unavailable')
    # The returned pointer must refer to the supplied version resource buffer.
    begin = C.addressof(buffer)
    if not begin <= pointer.value <= begin + size - C.sizeof(FixedVersion):
        raise PlatformError('windows_process_version_unavailable')
    value = C.cast(pointer, C.POINTER(FixedVersion)).contents
    if value.signature != 0xfeef04bd:
        raise PlatformError('windows_process_version_unavailable')
    return (value.product_ms >> 16, value.product_ms & 0xffff,
            value.product_ls >> 16, value.product_ls & 0xffff)


def process_entries(api, native, *, clock=time.monotonic):
    if C.sizeof(ProcessEntry) != 568 or ProcessEntry.szExeFile.offset != 44:
        raise PlatformError('windows_process_sdk_layout_invalid')
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot in (None, 0, C.c_void_p(-1).value):
        raise PlatformError('windows_process_snapshot_unavailable')
    try:
        entry = ProcessEntry()
        entry.dwSize = C.sizeof(entry)
        if not api.Process32FirstW(snapshot, C.byref(entry)):
            if C.get_last_error() == 18:  # ERROR_NO_MORE_FILES.
                return []
            raise PlatformError('windows_process_snapshot_unavailable')
        rows, deadline = [], clock() + 2
        for _ in range(MAX_PROCESSES):
            if clock() > deadline:
                raise PlatformError('windows_process_snapshot_deadline')
            name = bytes(entry.szExeFile).decode('utf-16-le').split('\0', 1)[0].casefold()
            if name in ('weixin.exe', 'wechat.exe'):
                rows.append({'pid': int(entry.th32ProcessID), 'parent_pid': int(entry.th32ParentProcessID)})
                if len(rows) > 128:
                    raise PlatformError('windows_process_snapshot_limit')
            if not api.Process32NextW(snapshot, C.byref(entry)):
                if C.get_last_error() != 18:
                    raise PlatformError('windows_process_snapshot_unavailable')
                return rows
        raise PlatformError('windows_process_snapshot_limit')
    finally:
        native.CloseHandle(snapshot)


def detect_wechat_process(expected_executable, *, api=None, native=None):
    try:
        executable = _path(expected_executable)
        if ntpath.basename(executable).casefold() not in ('weixin.exe', 'wechat.exe') or not path_is_local(executable):
            return result('unavailable', 'windows_process_path_unavailable')
        native, api = native or _api(), api or _DetectionAPI()
        if executable_version(api, executable) != SUPPORTED_VERSION:
            return result('unavailable', 'windows_process_version_unsupported')
        candidates = []
        for row in process_entries(api, native):
            handle = native.OpenProcess(0x1000, False, row['pid'])  # Query-only.
            if not handle:
                return result('unavailable', 'windows_process_access_unavailable')
            try:
                image, size = C.create_unicode_buffer(32768), C.c_uint32(32768)
                if not native.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)):
                    return result('unavailable', 'windows_process_access_unavailable')
                if ntpath.normcase(image.value) != ntpath.normcase(executable):
                    continue
                machine, host = C.c_uint16(), C.c_uint16()
                if not native.IsWow64Process2(handle, C.byref(machine), C.byref(host)) or machine.value != 0 or host.value != 0x8664:
                    return result('unavailable', 'windows_process_architecture_unsupported')
                times = [FILETIME() for _ in range(4)]
                if not native.GetProcessTimes(handle, *(C.byref(item) for item in times)):
                    return result('unavailable', 'windows_process_access_unavailable')
                birth = times[0].low | times[0].high << 32
                if birth <= 0:
                    return result('unavailable', 'windows_process_access_unavailable')
                candidates.append(dict(row, creation_time=birth))
            finally:
                native.CloseHandle(handle)
        windows = set()
        @CALLBACK
        def visit(window, _):
            pid = C.c_uint32()
            if api.GetWindowThreadProcessId(window, C.byref(pid)):
                windows.add(pid.value)
            return 1
        # Root-process evidence still works with no main window (e.g. tray).
        api.EnumWindows(visit, 0)
        return select_candidate(candidates, windows)
    except Exception as error:
        reason = getattr(error, 'code', '')
        allowed = {'windows_process_version_unavailable', 'windows_process_snapshot_deadline',
                   'windows_process_snapshot_unavailable', 'windows_process_snapshot_limit',
                   'windows_process_sdk_layout_invalid'}
        return result('unavailable', reason if reason in allowed else 'windows_process_detection_unavailable')


class WindowsProcessDetector:
    def __init__(self, expected_executable):
        self.expected_executable = str(expected_executable)

    def __call__(self):
        return detect_wechat_process(self.expected_executable)
