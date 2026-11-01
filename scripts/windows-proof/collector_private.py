"""Small private-file boundary for the local continuous collector.

Existing permissions are verified and never repaired. No private values or
native exception text are printed. Windows APIs are loaded only on Windows.
"""
from contextlib import contextmanager
import ctypes as C
import json
import os
from pathlib import Path
import stat

import windows_keyproof_platform as platform


MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_NEW_JSON_BYTES = 1024 * 1024


class PrivateError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _windows():
    return os.name == 'nt'


def _absolute(path):
    try:
        value = os.fspath(path)
        candidate = Path(value)
        if not isinstance(value, str) or '\0' in value or not candidate.is_absolute() or '..' in candidate.parts:
            raise ValueError
        return candidate
    except (TypeError, ValueError):
        raise PrivateError('collector_absolute_local_path_required') from None


def _identity(info):
    return info.st_dev, info.st_ino


def _regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not getattr(info, 'st_file_attributes', 0) & 0x400


def _posix_local(path, require_dir):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise PrivateError('collector_redirected_path_refused')
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise PrivateError('collector_local_path_invalid')
    info = path.lstat()
    if require_dir:
        if not stat.S_ISDIR(info.st_mode):
            raise PrivateError('collector_local_directory_required')
    elif not _regular(info):
        raise PrivateError('collector_local_regular_file_required')
    return info


def validate_local_path(path, require_dir=False):
    """Verify an existing absolute source path, without requiring private ACLs."""
    try:
        candidate = _absolute(path)
        if _windows():
            if not platform.path_is_local(candidate, require_dir):
                raise PrivateError('collector_local_non_reparse_path_required')
            info = candidate.lstat()
            if not require_dir and not _regular(info):
                raise PrivateError('collector_local_regular_file_required')
        else:
            _posix_local(candidate, require_dir)
        return candidate
    except PrivateError:
        raise
    except Exception:
        raise PrivateError('collector_local_path_validation_failed') from None


def _private_mode(info, directory):
    expected = 0o700 if directory else 0o600
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != expected:
        raise PrivateError('collector_private_permissions_invalid')


def ensure_private_dir(path, create=False):
    """Create one absent private directory or strictly verify an existing one."""
    try:
        candidate = _absolute(path)
        if create and not candidate.exists() and not candidate.is_symlink():
            validate_local_path(candidate.parent, True)
            if _windows():
                platform.private_dir(candidate)
            else:
                candidate.mkdir(mode=0o700)
        validate_local_path(candidate, True)
        if _windows():
            api, value = platform._api(), platform._path(candidate)
            with platform._parents(api, value):
                handle = api.CreateFileW(value, 0x20080, 3, None, 3, 0x02200000, None)
                if handle in (None, C.c_void_p(-1).value):
                    raise PrivateError('collector_private_directory_open_failed')
                try:
                    platform._checked(api, handle, value, True)
                    platform._verify_private(api, handle, True)
                finally:
                    api.CloseHandle(handle)
        else:
            _private_mode(candidate.lstat(), True)
        return candidate
    except PrivateError:
        raise
    except Exception:
        raise PrivateError('collector_private_directory_validation_failed') from None


@contextmanager
def _posix_parent(path):
    parent = ensure_private_dir(path.parent)
    before = parent.lstat()
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before) or not stat.S_ISDIR(opened.st_mode):
            raise PrivateError('collector_private_parent_identity_changed')
        _private_mode(opened, True)
        yield descriptor
        if _identity(parent.lstat()) != _identity(opened):
            raise PrivateError('collector_private_parent_identity_changed')
    finally:
        os.close(descriptor)


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _parse_json(raw):
    def invalid_constant(_):
        raise ValueError
    value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_json_pairs,
                       parse_constant=invalid_constant)
    if not isinstance(value, (dict, list)):
        raise ValueError
    return value


def _windows_read(path, maximum):
    ensure_private_dir(path.parent)
    api, value = platform._api(), platform._path(path)
    with platform._parents(api, value) as parent:
        platform._verify_private(api, parent, True)
        handle = api.CreateFileW(value, 0x80020080, 1, None, 3, 0x00200000, None)
        if handle in (None, C.c_void_p(-1).value):
            raise PrivateError('collector_private_file_open_failed')
        try:
            platform._checked(api, handle, value, False)
            platform._verify_private(api, handle, False)
            before, after = platform.FILEINFO(), platform.FILEINFO()
            if not api.GetFileInformationByHandle(handle, C.byref(before)):
                raise PrivateError('collector_private_file_identity_unavailable')
            length = before.size_low | before.size_high << 32
            if before.links != 1 or not 2 <= length <= maximum:
                raise PrivateError('collector_private_file_size_or_links_invalid')
            buffer, count = C.create_string_buffer(maximum + 1), platform.U32()
            if not api.ReadFile(handle, buffer, maximum + 1, C.byref(count), None) or count.value != length:
                raise PrivateError('collector_private_file_read_failed')
            if not api.GetFileInformationByHandle(handle, C.byref(after)):
                raise PrivateError('collector_private_file_identity_unavailable')
            identity = lambda info: (info.volume, info.index_high, info.index_low, info.size_high, info.size_low,
                                     info.write.high, info.write.low, info.links)
            if identity(before) != identity(after):
                raise PrivateError('collector_private_file_identity_changed')
            return buffer.raw[:count.value]
        finally:
            api.CloseHandle(handle)


def read_private_json(path, maxbytes=MAX_JSON_BYTES):
    """Bounded read after owner-only parent/file validation; reject duplicates."""
    try:
        if type(maxbytes) is not int or not 2 <= maxbytes <= MAX_JSON_BYTES:
            raise PrivateError('collector_private_json_limit_invalid')
        candidate = _absolute(path)
        if _windows():
            if not platform.path_is_local(candidate):
                raise PrivateError('collector_local_non_reparse_path_required')
            raw = _windows_read(candidate, maxbytes)
        else:
            validate_local_path(candidate)
            with _posix_parent(candidate) as parent:
                before = candidate.lstat()
                _private_mode(before, False)
                descriptor = os.open(candidate.name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0), dir_fd=parent)
                with os.fdopen(descriptor, 'rb') as stream:
                    opened = os.fstat(stream.fileno())
                    if not _regular(opened) or _identity(before) != _identity(opened):
                        raise PrivateError('collector_private_file_identity_changed')
                    _private_mode(opened, False)
                    if not 2 <= opened.st_size <= maxbytes:
                        raise PrivateError('collector_private_file_size_or_links_invalid')
                    raw = stream.read(maxbytes + 1)
                    after = os.fstat(stream.fileno())
                    _private_mode(after, False)
                    if (not _regular(after) or len(raw) != opened.st_size or len(raw) > maxbytes
                            or _identity(candidate.lstat()) != _identity(opened)
                            or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns)):
                        raise PrivateError('collector_private_file_identity_changed')
        return _parse_json(raw)
    except PrivateError:
        raise
    except Exception:
        raise PrivateError('collector_private_json_read_failed') from None


def write_private_json_new(path, data):
    """Write a new owner-only JSON file, never replacing existing material."""
    try:
        candidate = _absolute(path)
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if not 2 <= len(raw) <= MAX_NEW_JSON_BYTES or not isinstance(data, (dict, list)):
            raise PrivateError('collector_private_json_size_invalid')
        ensure_private_dir(candidate.parent)
        if candidate.exists() or candidate.is_symlink():
            raise PrivateError('collector_private_file_already_exists')
        if _windows():
            platform.private_json(candidate, data)
            return
        with _posix_parent(candidate) as parent:
            descriptor = os.open(candidate.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                                 0o600, dir_fd=parent)
            complete = False
            try:
                with os.fdopen(descriptor, 'wb') as stream:
                    info = os.fstat(stream.fileno())
                    if not _regular(info):
                        raise PrivateError('collector_private_file_identity_changed')
                    _private_mode(info, False)
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
                    if _identity(os.stat(candidate.name, dir_fd=parent, follow_symlinks=False)) != _identity(info):
                        raise PrivateError('collector_private_file_identity_changed')
                complete = True
                os.fsync(parent)
            finally:
                if not complete:
                    # This name was created exclusively above; never remove another file.
                    try:
                        current = os.stat(candidate.name, dir_fd=parent, follow_symlinks=False)
                        if _identity(current) == _identity(info):
                            os.unlink(candidate.name, dir_fd=parent)
                    except (OSError, UnboundLocalError):
                        pass
    except PrivateError:
        raise
    except Exception:
        raise PrivateError('collector_private_json_write_failed') from None
