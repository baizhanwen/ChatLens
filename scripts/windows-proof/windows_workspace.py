"""Current-user workspace and metadata-only discovery of existing local data.

Paths in the registry are private references, never copied chat/config payloads.
No recursive drive search, source SQL, model request or key extraction occurs.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from collector_private import ensure_private_dir, read_private_json, validate_local_path, write_private_json_new

TOKEN = re.compile(r'[0-9a-f]{64}\Z')
MAX_ACCOUNTS = 100
MAX_ENTRIES = 4096
MAX_DIRECTORIES = 512
DISCOVERY_SECONDS = 10


class WorkspaceError(ValueError):
    """A fixed reason safe to report without a private path."""


def machine_binding():
    if os.name != 'nt':
        raise WorkspaceError('windows_workspace_windows_required')
    try:
        import winreg
        from windows_keyproof_platform import _api
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            guid, kind = winreg.QueryValueEx(key, 'MachineGuid')
        sid = _api().current_sid()
        if kind != winreg.REG_SZ or not isinstance(guid, str) or not 16 <= len(guid) <= 128 or not sid:
            raise ValueError
        return hashlib.sha256(('ChatLens-local-user-v1\0' + guid + '\0' + sid).encode()).hexdigest()
    except Exception:
        raise WorkspaceError('windows_workspace_machine_identity_unavailable') from None


def default_workspace_root():
    value = os.environ.get('LOCALAPPDATA')
    if not value or not Path(value).is_absolute():
        raise WorkspaceError('windows_workspace_local_appdata_unavailable')
    parent = validate_local_path(Path(value), require_dir=True)
    return parent / 'ChatLens'


def _canonical(path):
    return os.path.normcase(str(Path(path).resolve()))


def _fixed_drives():
    if os.name != 'nt':
        return []
    try:
        import ctypes as C
        kernel = C.WinDLL('kernel32', use_last_error=True)
        kernel.GetLogicalDrives.argtypes, kernel.GetLogicalDrives.restype = [], C.c_uint32
        kernel.GetDriveTypeW.argtypes, kernel.GetDriveTypeW.restype = [C.c_wchar_p], C.c_uint32
        mask = kernel.GetLogicalDrives()
        return [Path(chr(65 + i) + ':\\') for i in range(26)
                if mask & (1 << i) and kernel.GetDriveTypeW(chr(65 + i) + ':\\') == 3]
    except Exception:
        return []


class Workspace:
    def __init__(self, root, package, *, fingerprint=None, discovery_roots=None):
        self.root = Path(root)
        self.package = validate_local_path(Path(package), require_dir=True)
        self.root = ensure_private_dir(self.root, create=True)
        self.private = ensure_private_dir(self.root / 'private', create=True)
        self.registry = self.private / 'workspace.json'
        self.fingerprint = fingerprint if fingerprint is not None else machine_binding()
        if not isinstance(self.fingerprint, str) or TOKEN.fullmatch(self.fingerprint) is None:
            raise WorkspaceError('windows_workspace_machine_identity_unavailable')
        self.discovery_roots = discovery_roots
        self.lock = threading.RLock()
        self.lease_lock = threading.RLock()
        self.closing = threading.Event()
        self.descriptor = None
        self._discovered = None
        self._read()

    def acquire(self):
        with self.lease_lock:
            if self.closing.is_set():
                raise WorkspaceError('windows_workspace_closed')
            if self.descriptor is not None:
                return
            path = self.private / 'workspace-lock.json'
            if not path.exists() and not path.is_symlink():
                try:
                    write_private_json_new(path, {'schema_version': 1, 'kind': 'chatlens_windows_workspace_lock'})
                except FileExistsError:
                    pass
            descriptor = None
            try:
                read_private_json(path)
                descriptor = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NOINHERIT', 0))
                before, opened = path.lstat(), os.fstat(descriptor)
                if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                        or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise ValueError
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.descriptor = descriptor
            except Exception:
                if descriptor is not None:
                    os.close(descriptor)
                raise WorkspaceError('windows_workspace_already_in_use') from None

    def begin_close(self):
        self.closing.set()

    def close(self):
        self.begin_close()
        with self.lease_lock:
            if self.descriptor is not None:
                os.close(self.descriptor)
                self.descriptor = None

    def _read(self):
        if not self.registry.exists() and not self.registry.is_symlink():
            return {'schema_version': 1, 'kind': 'chatlens_windows_workspace',
                    'machine_binding': self.fingerprint, 'accounts': {}}
        value = read_private_json(self.registry)
        if (not isinstance(value, dict) or set(value) != {'schema_version', 'kind', 'machine_binding', 'accounts'}
                or value['schema_version'] != 1 or value['kind'] != 'chatlens_windows_workspace'
                or value['machine_binding'] != self.fingerprint or not isinstance(value['accounts'], dict)
                or len(value['accounts']) > MAX_ACCOUNTS):
            raise WorkspaceError('windows_workspace_registry_or_machine_mismatch')
        for token, entry in value['accounts'].items():
            if (not isinstance(token, str) or TOKEN.fullmatch(token) is None
                    or not isinstance(entry, dict) or set(entry) != {'data_directory', 'previous_directories'}
                    or not isinstance(entry['data_directory'], str) or not Path(entry['data_directory']).is_absolute()
                    or not isinstance(entry['previous_directories'], list) or len(entry['previous_directories']) > 10
                    or any(not isinstance(p, str) or not Path(p).is_absolute() for p in entry['previous_directories'])):
                raise WorkspaceError('windows_workspace_registry_invalid')
        return value

    def _write(self, value, guard=None):
        self._read()  # Verify existing owner-only target before atomic replacement.
        temporary = self.private / ('.workspace-' + secrets.token_hex(12) + '.json')
        write_private_json_new(temporary, value)
        try:
            with self.lease_lock:
                if self.closing.is_set() or self.descriptor is None:
                    raise WorkspaceError('windows_workspace_closed')
                if guard is not None and not guard():
                    raise WorkspaceError('windows_workspace_commit_cancelled')
                os.replace(temporary, self.registry)
        finally:
            if temporary.exists():
                temporary.unlink()

    def account_token(self, db_root, exe):
        root = validate_local_path(Path(db_root), require_dir=True)
        executable = validate_local_path(Path(exe))
        info = root.stat()
        return hashlib.sha256(json.dumps([self.fingerprint, _canonical(root), info.st_dev,
            info.st_ino, _canonical(executable)], separators=(',', ':')).encode()).hexdigest()

    def _reader(self, path):
        from collector_service import validate_scope
        candidate = ensure_private_dir(Path(path))
        private = ensure_private_dir(candidate / 'private')
        scope = read_private_json(private / 'scope.json')
        keys = read_private_json(private / 'keys.json')
        root, exe, _, _ = validate_scope(scope, keys)
        return candidate, root, exe

    def _scope_reference(self, path):
        """Discover a reference before touching another account's key file."""
        candidate = ensure_private_dir(Path(path))
        private = ensure_private_dir(candidate / 'private')
        scope = read_private_json(private / 'scope.json')
        if (not isinstance(scope, dict) or scope.get('schema_version') != 1
                or not isinstance(scope.get('db_root'), str) or not isinstance(scope.get('exe_path'), str)
                or not (private / 'keys.json').is_file() or (private / 'keys.json').is_symlink()):
            raise WorkspaceError('windows_workspace_import_invalid')
        root = validate_local_path(Path(scope['db_root']), require_dir=True)
        exe = validate_local_path(Path(scope['exe_path']))
        if exe.name.casefold() not in ('weixin.exe', 'wechat.exe'):
            raise WorkspaceError('windows_workspace_import_invalid')
        return candidate, root, exe

    def _default_roots(self):
        roots = [self.package, self.package.parent, self.package.parent.parent]
        home = Path.home()
        roots += [home / name for name in ('Downloads', 'Desktop', 'Documents')]
        roots += _fixed_drives()
        return roots

    def _discover(self):
        if self._discovered is not None:
            return self._discovered
        roots = self._default_roots() if self.discovery_roots is None else list(self.discovery_roots)
        queue, visited, candidates = [], set(), {}
        deadline = time.monotonic() + DISCOVERY_SECONDS
        def check():
            if self.closing.is_set():
                raise WorkspaceError('windows_workspace_closed')
            if time.monotonic() >= deadline:
                raise WorkspaceError('windows_workspace_discovery_limit')
        for root in roots:
            try:
                root = validate_local_path(Path(root), require_dir=True)
            except Exception:
                continue
            queue.append((root, 0))
        directories = 0
        while queue:
            check()
            directory, depth = queue.pop(0)
            key = _canonical(directory)
            if key in visited:
                continue
            visited.add(key)
            directories += 1
            if directories > MAX_DIRECTORIES:
                raise WorkspaceError('windows_workspace_discovery_limit')
            # Only fixed collector-data locations and ChatLens-named descendants
            # are checked. Never descend into databases, private or other projects.
            for possible in (directory / 'collector-data',):
                if possible.is_dir() and not possible.is_symlink():
                    try:
                        path, root, exe = self._scope_reference(possible)
                        candidates[_canonical(path)] = (path, root, exe)
                    except Exception:
                        pass
            if depth >= 3:
                continue
            try:
                with os.scandir(directory) as stream:
                    for index, entry in enumerate(stream):
                        check()
                        if index >= MAX_ENTRIES:
                            raise WorkspaceError('windows_workspace_discovery_limit')
                        if (entry.name.casefold().startswith('chatlens') and entry.is_dir(follow_symlinks=False)
                                and not entry.is_symlink()):
                            try:
                                child = validate_local_path(Path(entry.path), require_dir=True)
                                queue.append((child, depth + 1))
                            except Exception:
                                continue
            except WorkspaceError:
                raise
            except OSError:
                continue
        self._discovered = list(candidates.values())
        return self._discovered

    def find_account(self, db_root, exe):
        with self.lock:
            self.acquire()
            token = self.account_token(db_root, exe)
            registry = self._read()
            entry = registry['accounts'].get(token)
            if entry is not None:
                try:
                    path, root, executable = self._reader(Path(entry['data_directory']))
                    if self.account_token(root, executable) != token:
                        raise ValueError
                    return path
                except Exception:
                    raise WorkspaceError('windows_workspace_registered_data_unavailable') from None
            matches = []
            for path, root, executable in self._discover():
                try:
                    candidate_token = self.account_token(root, executable)
                except Exception:
                    continue
                if candidate_token != token:
                    continue
                try:
                    target, root, executable = self._reader(path)
                    if self.account_token(root, executable) == token:
                        matches.append(target)
                except Exception:
                    continue
            if len(matches) > 1:
                raise WorkspaceError('windows_workspace_legacy_ambiguous')
            if matches:
                self.register_account(db_root, exe, matches[0])
                return matches[0]
            return None

    def register_account(self, db_root, exe, data_dir, *, explicit_import=False, guard=None):
        with self.lock:
            self.acquire()
            if guard is not None and not guard():
                raise WorkspaceError('windows_workspace_commit_cancelled')
            token = self.account_token(db_root, exe)
            path, root, executable = self._reader(Path(data_dir))
            if self.account_token(root, executable) != token:
                raise WorkspaceError('windows_workspace_account_mismatch')
            value = self._read()
            old = value['accounts'].get(token)
            previous = list(old['previous_directories']) if old else []
            if old and _canonical(old['data_directory']) != _canonical(path):
                if not explicit_import:
                    raise WorkspaceError('windows_workspace_legacy_ambiguous')
                if old['data_directory'] not in previous:
                    previous.append(old['data_directory'])
                if len(previous) > 10:
                    raise WorkspaceError('windows_workspace_saved_data_limit')
            if token not in value['accounts'] and len(value['accounts']) >= MAX_ACCOUNTS:
                raise WorkspaceError('windows_workspace_account_limit')
            value['accounts'][token] = {'data_directory': str(path), 'previous_directories': previous}
            self._write(value, guard=guard)
            return path

    def create_account_directory(self, db_root, exe):
        with self.lock:
            self.acquire()
            token = self.account_token(db_root, exe)
            parent = ensure_private_dir(self.private / 'accounts', create=True)
            account = ensure_private_dir(parent / token, create=True)
            target = account / 'collector-data'
            ensure_private_dir(target, create=True)
            return target

    def import_existing(self, path, *, guard=None):
        with self.lock:
            try:
                target, root, exe = self._reader(Path(path))
                return self.register_account(root, exe, target, explicit_import=True, guard=guard)
            except WorkspaceError:
                raise
            except Exception:
                raise WorkspaceError('windows_workspace_import_invalid') from None

    def validate_import(self, path):
        """Validate in an owned task; register only after its commit guard."""
        try:
            if self.closing.is_set():
                raise WorkspaceError('windows_workspace_closed')
            target, _, _ = self._reader(Path(path))
            if self.closing.is_set():
                raise WorkspaceError('windows_workspace_closed')
            return target
        except WorkspaceError:
            raise
        except Exception:
            raise WorkspaceError('windows_workspace_import_invalid') from None
