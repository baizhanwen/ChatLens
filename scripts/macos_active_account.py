"""Identify one running WeChat account from open-file metadata only.

No chat database, encryption key, account catalog or message body is read.
Errors contain fixed codes; the returned private path is deliberately omitted
from the dataclass representation.
"""
from dataclasses import dataclass, field
import ctypes as C
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


BUNDLE = 'com.tencent.xinWeChat'
DEFAULT_PROBE = Path(__file__).resolve().parent / 'native/bin/macos-probe-web'
ACCOUNT = re.compile(r'[A-Za-z0-9_@.-]{1,256}\Z')
VERSION = re.compile(r'[0-9][0-9A-Za-z ._-]{0,39}\Z')
MAX_PROBE_BYTES = 128 * 1024
MAX_LSOF_BYTES = 2 * 1024 * 1024


class ActiveAccountError(ValueError):
    def __init__(self, code):
        self.code = self.reason = code
        super().__init__(code)


@dataclass(frozen=True)
class ActiveAccount:
    db_root: Path = field(repr=False)
    pid: int
    started_at: float
    client_version: str


class _StatFS64(C.Structure):
    # Darwin SDK sys/mount.h __DARWIN_STRUCT_STATFS64; MAXPATHLEN=1024.
    _fields_ = [('bsize', C.c_uint32), ('iosize', C.c_int32),
                ('blocks', C.c_uint64), ('bfree', C.c_uint64), ('bavail', C.c_uint64),
                ('files', C.c_uint64), ('ffree', C.c_uint64), ('fsid', C.c_int32 * 2),
                ('owner', C.c_uint32), ('type', C.c_uint32), ('flags', C.c_uint32),
                ('subtype', C.c_uint32), ('type_name', C.c_char * 16),
                ('mounted_on', C.c_char * 1024), ('mounted_from', C.c_char * 1024),
                ('extended_flags', C.c_uint32), ('reserved', C.c_uint32 * 7)]


def _local_volume(path):
    """Inspect mount flags on a pinned directory; never read file content."""
    if sys.platform != 'darwin' or C.sizeof(C.c_void_p) != 8:
        raise ActiveAccountError('macos_active_native_platform_required')
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        library = C.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
        function = getattr(library, 'fstatfs$INODE64', None) or library.fstatfs
        function.argtypes, function.restype = [C.c_int, C.POINTER(_StatFS64)], C.c_int
        value = _StatFS64()
        if function(descriptor, C.byref(value)) != 0:
            raise ActiveAccountError('macos_active_mount_metadata_unavailable')
        return bool(value.flags & 0x1000)  # Darwin MNT_LOCAL.
    except ActiveAccountError:
        raise
    except Exception:
        raise ActiveAccountError('macos_active_mount_metadata_unavailable') from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_path(path, directory):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or '\0' in os.fspath(path):
        raise ActiveAccountError('macos_active_path_unsafe')
    try:
        current = Path(path.anchor)
        for index, part in enumerate(path.parts[1:], 1):
            current /= part
            info = current.lstat()
            last = index == len(path.parts) - 1
            if stat.S_ISLNK(info.st_mode):
                raise ActiveAccountError('macos_active_path_redirected')
            if not last or directory:
                if not stat.S_ISDIR(info.st_mode):
                    raise ActiveAccountError('macos_active_directory_required')
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ActiveAccountError('macos_active_file_unsafe')
        return path.lstat()
    except ActiveAccountError:
        raise
    except Exception:
        raise ActiveAccountError('macos_active_path_unavailable') from None


def _execute(runner, command, limit, failure):
    try:
        result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                        timeout=5, check=False)
        if (result.returncode != 0 or not isinstance(result.stdout, bytes)
                or not 1 <= len(result.stdout) <= limit):
            raise ActiveAccountError(failure)
        return result.stdout.decode('utf-8', errors='strict')
    except ActiveAccountError:
        raise
    except Exception:
        raise ActiveAccountError(failure) from None


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _identity(source):
    try:
        value = json.loads(source, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if (not isinstance(value, dict) or value.get('bundle_id') != BUNDLE or value.get('running') is not True
                or type(value.get('matching_applications')) is not int or value['matching_applications'] != 1
                or type(value.get('process_id')) is not int or not 1 <= value['process_id'] <= 2**31 - 1
                or type(value.get('process_started_at')) not in (int, float)
                or not math.isfinite(value['process_started_at']) or value['process_started_at'] <= 0
                or not isinstance(value.get('version'), str) or VERSION.fullmatch(value['version']) is None):
            raise ValueError
        for name in ('frontmost', 'screen_state_available', 'screen_locked'):
            if type(value.get(name)) is not bool:
                raise ValueError
        return value['process_id'], float(value['process_started_at']), value['version']
    except Exception:
        raise ActiveAccountError('macos_active_process_metadata_invalid') from None


def detect_active_account(probe_path=None, *, runner=None, home=None):
    """Return one private active root, pinned by metadata samples around lsof.

    Test seams accept a synthetic runner and synthetic home. The production
    caller supplies neither and never scans account folders for activity.
    """
    runner = subprocess.run if runner is None else runner
    probe = DEFAULT_PROBE if probe_path is None else Path(probe_path)
    _safe_path(probe, False)
    user_home = Path.home() if home is None else Path(home)
    base = user_home / 'Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files'
    first = _identity(_execute(runner, [str(probe), 'wechat'], MAX_PROBE_BYTES,
                               'macos_active_process_probe_failed'))
    pid, started, version = first
    listing = _execute(runner, ['/usr/sbin/lsof', '-nP', '-p', str(pid), '-Fn'], MAX_LSOF_BYTES,
                       'macos_active_open_files_unavailable')
    lines = listing.split('\n')
    if len(lines) > 16384:
        raise ActiveAccountError('macos_active_open_file_limit')
    process_fields = [line[1:] for line in lines if line.startswith('p')]
    if not process_fields or any(value != str(pid) for value in process_fields):
        raise ActiveAccountError('macos_active_open_file_process_mismatch')
    roots = {}
    for line in lines:
        if not line.startswith('n'):
            continue
        name = line[1:]
        candidate = Path(name)
        try:
            relative = candidate.relative_to(base)
        except ValueError:
            continue
        if len(relative.parts) < 3 or relative.parts[1] != 'db_storage':
            continue
        if (not ACCOUNT.fullmatch(relative.parts[0]) or relative.parts[0] in ('.', '..')
                or '..' in relative.parts or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            raise ActiveAccountError('macos_active_open_file_path_invalid')
        root = base / relative.parts[0] / 'db_storage'
        _safe_path(root, True)
        info = _safe_path(candidate, candidate.is_dir())
        if not stat.S_ISREG(info.st_mode):
            continue
        if root not in roots:
            if not _local_volume(root):
                raise ActiveAccountError('macos_active_local_volume_required')
            info = root.lstat()
            roots[root] = (info.st_dev, info.st_ino)
    if len(roots) != 1:
        raise ActiveAccountError('macos_active_account_ambiguous' if roots else 'macos_active_account_not_identified')
    second = _identity(_execute(runner, [str(probe), 'wechat'], MAX_PROBE_BYTES,
                                'macos_active_process_probe_failed'))
    if second != first:
        raise ActiveAccountError('macos_active_process_changed')
    root = next(iter(roots))
    current = _safe_path(root, True)
    if (current.st_dev, current.st_ino) != roots[root]:
        raise ActiveAccountError('macos_active_account_root_changed')
    return ActiveAccount(root, pid, started, version)
