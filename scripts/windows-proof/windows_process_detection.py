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
MAX_FAMILY_PROCESSES = 16
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
            'IsWindowVisible': (user, [C.c_void_p], C.c_int32),
            'GetWindow': (user, [C.c_void_p, C.c_uint32], C.c_void_p),
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
    if not isinstance(candidates, (tuple, list)):
        return result('unavailable', 'windows_process_candidates_invalid')
    if not candidates:
        return result('not_running', 'windows_process_not_running')
    if (len(candidates) > 128
            or any(not isinstance(row, dict) or type(row.get('pid')) is not int
                   or not 0 < row['pid'] <= 0xffffffff or type(row.get('parent_pid')) is not int
                   or not 0 <= row['parent_pid'] <= 0xffffffff or type(row.get('creation_time')) is not int
                   or not 0 < row['creation_time'] < 2**64 for row in candidates)
            or len({row['pid'] for row in candidates}) != len(candidates)):
        return result('unavailable', 'windows_process_candidates_invalid')
    identifiers = {row['pid'] for row in candidates}
    window_identifiers = set(window_pids)
    windows = [row for row in candidates if row['pid'] in window_identifiers]
    if len(windows) == 1:
        return result('ready', 'windows_process_candidate_ready', windows[0])
    if windows:
        # Multiple verified window processes cannot be disambiguated by
        # preferring their launcher parent, which may own no account DB.
        return result('ambiguous', 'windows_process_multiple_candidates')
    roots = [row for row in candidates if row['parent_pid'] not in identifiers]
    if len(roots) == 1:
        # Only an absence of candidate windows permits the hidden/tray root
        # fallback. A same-executable launcher is not stronger than a window.
        return result('ready', 'windows_process_candidate_ready', roots[0])
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


def process_entries(api, native, *, clock=time.monotonic, deadline=None):
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
        rows, local_deadline = [], clock() + 2
        if deadline is not None:
            local_deadline = min(local_deadline, deadline)
        for _ in range(MAX_PROCESSES):
            if clock() > local_deadline:
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


def verified_process_candidates(expected_executable, *, api=None, native=None, clock=time.monotonic, deadline=None):
    """Private process metadata shared by main-window and family discovery."""
    executable = _path(expected_executable)
    if ntpath.basename(executable).casefold() not in ('weixin.exe', 'wechat.exe') or not path_is_local(executable):
        raise PlatformError('windows_process_path_unavailable')
    native, api = native or _api(), api or _DetectionAPI()
    end = min(clock() + 2, deadline) if deadline is not None else clock() + 2
    def budget():
        if clock() > end:
            raise PlatformError('windows_process_snapshot_deadline')
    budget()
    if executable_version(api, executable) != SUPPORTED_VERSION:
        raise PlatformError('windows_process_version_unsupported')
    candidates = []
    for row in process_entries(api, native, clock=clock, deadline=end):
        budget()
        handle = native.OpenProcess(0x1000, False, row['pid'])
        if not handle:
            raise PlatformError('windows_process_access_unavailable')
        try:
            image, size = C.create_unicode_buffer(32768), C.c_uint32(32768)
            if not native.QueryFullProcessImageNameW(handle, 0, image, C.byref(size)):
                raise PlatformError('windows_process_access_unavailable')
            if ntpath.normcase(image.value) != ntpath.normcase(executable):
                continue
            machine, host = C.c_uint16(), C.c_uint16()
            if not native.IsWow64Process2(handle, C.byref(machine), C.byref(host)) or machine.value != 0 or host.value != 0x8664:
                raise PlatformError('windows_process_architecture_unsupported')
            times = [FILETIME() for _ in range(4)]
            if not native.GetProcessTimes(handle, *(C.byref(item) for item in times)):
                raise PlatformError('windows_process_access_unavailable')
            birth = times[0].low | times[0].high << 32
            if birth <= 0:
                raise PlatformError('windows_process_access_unavailable')
            candidates.append(dict(row, creation_time=birth))
        finally:
            native.CloseHandle(handle)
        budget()
    budget()
    return candidates


def validate_process_family(candidates, pid, creation_time):
    """One bounded, connected tree with an exact member identity; no guessing."""
    if (type(pid) is not int or not 0 < pid <= 0xffffffff or type(creation_time) is not int
            or not 0 < creation_time < 2**64 or not isinstance(candidates, (list, tuple))
            or not 1 <= len(candidates) <= MAX_FAMILY_PROCESSES
            or any(not isinstance(row, dict) or set(row) != {'pid', 'parent_pid', 'creation_time'}
                   or type(row['pid']) is not int or not 0 < row['pid'] <= 0xffffffff
                   or type(row['parent_pid']) is not int or not 0 <= row['parent_pid'] <= 0xffffffff
                   or type(row['creation_time']) is not int or not 0 < row['creation_time'] < 2**64 for row in candidates)):
        raise PlatformError('windows_process_family_invalid')
    by_pid = {row['pid']: dict(row) for row in candidates}
    if len(by_pid) != len(candidates) or pid not in by_pid or by_pid[pid]['creation_time'] != creation_time:
        raise PlatformError('windows_process_family_identity_changed')
    roots = [row for row in candidates if row['parent_pid'] not in by_pid]
    if len(roots) != 1:
        raise PlatformError('windows_process_family_ambiguous')
    for row in candidates:
        seen, current = set(), row
        while current['parent_pid'] in by_pid:
            if current['pid'] in seen:
                raise PlatformError('windows_process_family_cycle')
            seen.add(current['pid'])
            parent = by_pid[current['parent_pid']]
            if parent['creation_time'] > current['creation_time']:
                raise PlatformError('windows_process_family_parent_reused')
            current = parent
        if current['pid'] != roots[0]['pid']:
            raise PlatformError('windows_process_family_disconnected')
    return [by_pid[identifier] for identifier in sorted(by_pid)]


def verified_process_family(expected_executable, pid, creation_time, *, api=None, native=None,
                            clock=time.monotonic, deadline=None):
    """Return only a verified same-executable tree, kept inside the probe child."""
    candidates = verified_process_candidates(expected_executable, api=api, native=native,
                                             clock=clock, deadline=deadline)
    return validate_process_family(candidates, pid, creation_time)


def detect_wechat_process(expected_executable, *, api=None, native=None):
    try:
        native, api = native or _api(), api or _DetectionAPI()
        candidates = verified_process_candidates(expected_executable, api=api, native=native)
        windows = set()
        verified_pids = {row['pid'] for row in candidates}
        window_metadata_failed = [False]
        @CALLBACK
        def visit(window, _):
            try:
                pid = C.c_uint32()
                if not api.GetWindowThreadProcessId(window, C.byref(pid)) or pid.value not in verified_pids:
                    return 1
                # Match the earlier .NET MainWindowHandle filter: hidden helper
                # windows and owned popups cannot override the main window PID.
                if not api.IsWindowVisible(window) or api.GetWindow(window, 4):  # GW_OWNER.
                    return 1
                windows.add(pid.value)
                return 1
            except Exception:
                window_metadata_failed[0] = True
                return 0
        # Root-process evidence still works with no main window (e.g. tray).
        if not api.EnumWindows(visit, 0) or window_metadata_failed[0]:
            return result('unavailable', 'windows_process_detection_unavailable')
        return select_candidate(candidates, windows)
    except Exception as error:
        reason = getattr(error, 'code', '')
        allowed = {'windows_process_version_unavailable', 'windows_process_snapshot_deadline',
                   'windows_process_snapshot_unavailable', 'windows_process_snapshot_limit',
                   'windows_process_sdk_layout_invalid', 'windows_process_path_unavailable',
                   'windows_process_version_unsupported', 'windows_process_access_unavailable',
                   'windows_process_architecture_unsupported'}
        return result('unavailable', reason if reason in allowed else 'windows_process_detection_unavailable')


class WindowsProcessDetector:
    def __init__(self, expected_executable):
        self.expected_executable = str(expected_executable)

    def __call__(self):
        return detect_wechat_process(self.expected_executable)
