"""Read-only process/desktop state and fixed-shard WAL prerequisites.

No window titles, screenshots, process memory or database contents are read.
SHM checks are preconditions; ordinary SQLite WAL locks can alter SHM metadata.
"""
import ctypes as C
import ntpath
import os

from windows_keyproof_platform import WindowsProcess, PlatformError, _api, _path, path_is_local


class _ProbeAPI:
    def __init__(self):
        try:
            kernel = C.WinDLL('kernel32', use_last_error=True)
            user = C.WinDLL('user32', use_last_error=True)
            signatures = {
                'GetExitCodeProcess': (kernel, [C.c_void_p, C.POINTER(C.c_uint32)], C.c_int32),
                'GetForegroundWindow': (user, [], C.c_void_p),
                'GetWindowThreadProcessId': (user, [C.c_void_p, C.POINTER(C.c_uint32)], C.c_uint32),
                'OpenInputDesktop': (user, [C.c_uint32, C.c_int32, C.c_uint32], C.c_void_p),
                'CloseDesktop': (user, [C.c_void_p], C.c_int32),
                'GetUserObjectInformationW': (user, [C.c_void_p, C.c_int32, C.c_void_p,
                                                       C.c_uint32, C.POINTER(C.c_uint32)], C.c_int32),
            }
            for name, (dll, arguments, result) in signatures.items():
                function = getattr(dll, name)
                function.argtypes, function.restype = arguments, result
                setattr(self, name, function)
        except (AttributeError, OSError):
            raise PlatformError('native_probe_api_unavailable') from None


_probe_native = None


def _probe_api():
    global _probe_native
    _api()  # Enforce the existing Windows x64 runtime contract before DLL load.
    if _probe_native is None:
        _probe_native = _ProbeAPI()
    return _probe_native


def _desktop(api):
    handle = api.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS only.
    if not handle:
        return None, False
    locked, verified = None, False
    try:
        needed = C.c_uint32()
        api.GetUserObjectInformationW(handle, 2, None, 0, C.byref(needed))  # UOI_NAME.
        if 0 < needed.value <= 4096 and needed.value % C.sizeof(C.c_wchar) == 0:
            name = C.create_unicode_buffer(needed.value // C.sizeof(C.c_wchar))
            if api.GetUserObjectInformationW(handle, 2, name, C.sizeof(name), C.byref(needed)):
                locked = name.value.casefold() != 'default'
                verified = locked is False
    finally:
        if not api.CloseDesktop(handle):
            verified = False
    return locked, verified


class NativeState:
    def __init__(self, pid, expected_exe, creation_time):
        self.pid = pid
        self.process = WindowsProcess(pid, expected_exe, creation_time)
        self.api = None

    def __enter__(self):
        self.process.__enter__()
        try:
            self.api = _probe_api()
            return self
        except BaseException:
            self.process.__exit__(None, None, None)
            raise

    def __exit__(self, *error):
        self.api = None
        self.process.__exit__(*error)

    def sample(self):
        result = {'probe_succeeded': False, 'running': False,
                  'frontmost': None, 'screen_locked': None}
        if self.api is None or not self.process.handle:
            return result
        try:
            code = C.c_uint32()
            if not self.api.GetExitCodeProcess(self.process.handle, C.byref(code)):
                return result
            result['running'] = code.value == 259  # STILL_ACTIVE.
            if not result['running']:
                return result
            window, foreground_pid = self.api.GetForegroundWindow(), C.c_uint32()
            foreground_known = bool(window and self.api.GetWindowThreadProcessId(window, C.byref(foreground_pid))
                                    and foreground_pid.value)
            if foreground_known:
                result['frontmost'] = foreground_pid.value == self.pid
            result['screen_locked'], desktop_ok = _desktop(self.api)
            result['probe_succeeded'] = foreground_known and desktop_ok
        except Exception:
            # Native error strings and desktop names must never escape.
            result['probe_succeeded'] = False
        return result


def _file_status(api, path):
    attributes = api.GetFileAttributesW(path)
    if attributes == 0xffffffff:
        if C.get_last_error() in (2, 3):
            return False, 0
        raise PlatformError('wal_pair_metadata_unavailable')
    if attributes & (0x400 | 16) or not path_is_local(path):
        raise PlatformError('wal_pair_unsafe_file')
    try:
        size = os.stat(path, follow_symlinks=False).st_size
    except OSError:
        raise PlatformError('wal_pair_metadata_unavailable') from None
    if type(size) is not int or size < 0:
        raise PlatformError('wal_pair_metadata_unavailable')
    return True, size


def detect_shm_preconditions(dbroot, bodyrels):
    """Check fixed message_0/1/contact pairs; never open their bytes."""
    if not isinstance(bodyrels, (list, tuple)) or not 1 <= len(bodyrels) <= 3:
        raise PlatformError('invalid_body_database_scope')
    if any(type(rel) is not str or rel not in ('message/message_0.db', 'message/message_1.db', 'contact/contact.db')
           for rel in bodyrels) or len(set(bodyrels)) != len(bodyrels):
        raise PlatformError('invalid_body_database_scope')
    api, root = _api(), _path(dbroot)
    if not path_is_local(root, require_dir=True) or ntpath.basename(root).casefold() != 'db_storage':
        raise PlatformError('unsafe_selected_database_root')
    status = {}
    for relative in bodyrels:
        path = ntpath.join(root, *relative.split('/'))
        database, _ = _file_status(api, path)
        wal, size = _file_status(api, path + '-wal')
        shm, _ = _file_status(api, path + '-shm')
        if not database:
            raise PlatformError('selected_body_database_missing')
        if wal and size > 0 and not shm:
            raise PlatformError('nonempty_wal_without_shm')
        status[relative] = {'database_exists': True, 'wal_exists': wal,
                            'wal_bytes': size, 'shm_exists': shm}
    return status
