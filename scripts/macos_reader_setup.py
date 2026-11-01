#!/usr/bin/env python3
"""Nonblocking, private initialization of one explicitly detected Mac account.

Only fixed project-owned helpers are launched. The user quits original WeChat;
the existing capture helper owns its temporary app and original-app restoration.
No helper output, encryption key or account path enters a public state object.
"""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import secrets
import signal
import stat
import subprocess
import time

from macos_collector import _authenticate_keys, _keys, _root_identity
from collector_private import ensure_private_dir, read_private_json, validate_local_path, write_private_json_new

SYSTEM_PYTHON = Path('/usr/bin/python3')


def current_application(account, *, run=subprocess.run):
    """Resolve the detected running process, never guess an installed copy."""
    call = run(['/bin/ps', '-p', str(account.pid), '-o', 'comm='], stdout=subprocess.PIPE,
               stderr=subprocess.DEVNULL, timeout=3, check=False, shell=False)
    if call.returncode != 0 or not isinstance(call.stdout, bytes) or not 1 <= len(call.stdout) <= 4096:
        raise ValueError('macos_running_application_unavailable')
    value = call.stdout.decode('utf-8').strip()
    if '\n' in value or '\r' in value or not value.endswith('/Contents/MacOS/WeChat'):
        raise ValueError('macos_running_application_invalid')
    executable = validate_local_path(Path(value))
    app = validate_local_path(executable.parents[2], require_dir=True)
    info = validate_local_path(app / 'Contents/Info.plist')
    if info.stat().st_size > 1024 * 1024:
        raise ValueError('macos_running_application_invalid')
    document = plistlib.loads(info.read_bytes())
    if (document.get('CFBundleIdentifier') != 'com.tencent.xinWeChat'
            or document.get('CFBundleShortVersionString') != account.client_version):
        raise ValueError('macos_running_application_version_mismatch')
    return app


class ReaderInitializer:
    def __init__(self, account, private_account_dir, *, project_root,
                 app_path=None, process_factory=subprocess.Popen,
                 runner=subprocess.run, clock=time.monotonic, signal_group=None):
        self.account = account
        self.private_account_dir = Path(private_account_dir)
        self.project_root, self.app_path = Path(project_root), Path(app_path) if app_path is not None else None
        self.process_factory, self.runner, self.clock = process_factory, runner, clock
        self.signal_group = signal_group if signal_group is not None else os.killpg
        self.state, self.action, self.phase, self.reason = 'idle', 'none', 'idle', None
        self._key_file, self._child, self._state_dir = None, None, None
        self._started = self._phase_started = None
        self._root_identity = None
        self._closed = False

    @property
    def key_file(self):
        return self._key_file

    @property
    def elapsed_seconds(self):
        return max(0, int(self.clock() - self._started)) if self._started is not None else 0

    def _publish(self):
        if self._state_dir is None:
            return
        target = self._state_dir / 'initializer-status.json'
        if target.exists() or target.is_symlink():
            read_private_json(target)
        temporary = self._state_dir / ('.initializer-' + secrets.token_hex(8) + '.json')
        value = {'schema_version': 1, 'state': self.state, 'action': self.action, 'phase': self.phase,
                 'elapsed_seconds': self.elapsed_seconds, 'reason': self.reason}
        write_private_json_new(temporary, value)
        try:
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _set(self, state, action, phase, reason=None):
        self.state, self.action, self.phase, self.reason = state, action, phase, reason
        try:
            self._publish()
        except Exception:
            self.state, self.action, self.phase, self.reason = 'error', 'none', 'error', 'macos_reader_private_state_unavailable'
            self._stop_child()

    def _preflight(self):
        ensure_private_dir(self.private_account_dir)
        ensure_private_dir(self._state_dir)
        if _root_identity(Path(self.account.db_root)) != self._root_identity:
            raise ValueError('macos_reader_account_changed')

    def _common(self):
        return ['--state-dir', str(self._state_dir), '--app', str(self.app_path),
                '--database-scope', 'message_and_contact', '--db-root', str(self.account.db_root)]

    def _launch(self, phase):
        if phase == 'prepare':
            interpreter = (self.project_root / '.build/wechat-read-venv/bin/python').resolve(strict=True)
            helper = self.project_root / 'scripts/prepare_wechat_read_test.py'
        else:
            interpreter = SYSTEM_PYTHON
            helper = self.project_root / 'scripts/macos_capture_keys.py'
        if interpreter == Path('/usr/bin/python3'):
            # Apple's command-line dispatcher is intentionally hard-linked
            # under many names. Private data rules must not reject that fixed,
            # root-owned system executable. Parent redirects remain forbidden.
            validate_local_path(interpreter.parent, require_dir=True)
            info = interpreter.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) & 0o022 or not os.access(interpreter, os.X_OK)):
                raise ValueError('macos_reader_system_python_unsafe')
        else:
            validate_local_path(interpreter)
        validate_local_path(helper)
        command = [str(interpreter), str(helper), phase, *self._common()]
        if phase == 'capture':
            command += ['--timeout', '300']
        self._child = self.process_factory(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                           stderr=subprocess.DEVNULL, cwd=str(self.project_root),
                                           shell=False, start_new_session=True)
        if type(self._child.pid) is not int or self._child.pid <= 1:
            raise ValueError('macos_reader_owned_process_invalid')
        self._phase_started = self.clock()

    def start(self):
        if self._closed or self.state != 'idle':
            return
        self._started = self.clock()
        try:
            validate_local_path(self.project_root, require_dir=True)
            if self.app_path is None:
                self.app_path = current_application(self.account, run=self.runner)
            validate_local_path(self.app_path, require_dir=True)
            self._root_identity = _root_identity(Path(self.account.db_root))
            ensure_private_dir(self.private_account_dir, create=True)
            # The child state is always new. Existing initialization evidence,
            # successful or partial, is never overwritten or silently reused.
            self._state_dir = self.private_account_dir / ('reader-init-' + secrets.token_hex(12))
            ensure_private_dir(self._state_dir, create=True)
            self._launch('prepare')
            self._set('working', 'none', 'preparing_copy')
        except Exception:
            self._stop_child()
            self._set('error', 'none', 'error', 'macos_reader_prepare_start_failed')

    def _original_exited(self):
        call = self.runner(['/usr/bin/pgrep', '-x', 'WeChat'], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=3, check=False, shell=False)
        if call.returncode not in (0, 1):
            raise ValueError('macos_reader_original_process_state_unavailable')
        return call.returncode == 1

    def _validated_key_file(self):
        target = self._state_dir / 'keys.json'
        document = read_private_json(target)
        keys = _keys(document, messages=True)
        keys.update(_keys(document, messages=False))
        if set(document['keys']) != set(keys):
            raise ValueError('macos_reader_extra_key_refused')
        _authenticate_keys(Path(self.account.db_root), keys)
        self._preflight()
        return target

    def step(self):
        if self._closed or self.state in ('idle', 'ready', 'error'):
            return self.state
        try:
            self._preflight()
            if self.phase == 'waiting_for_exit':
                if not self._original_exited():
                    return self.state
                self._launch('capture')
                self._set('needs_user_action', 'confirm_login', 'waiting_for_login')
                return self.state
            exit_code = self._child.poll()
            if exit_code is None:
                deadline = 600 if self.phase == 'preparing_copy' else 330
                if self.clock() - self._phase_started > deadline:
                    self._stop_child()
                    self._set('error', 'none', 'error', 'macos_reader_helper_deadline')
                return self.state
            self._child = None
            if exit_code != 0:
                self._set('error', 'none', 'error', 'macos_reader_prepare_failed' if self.phase == 'preparing_copy'
                          else 'macos_reader_capture_failed')
                return self.state
            if self.phase == 'preparing_copy':
                self._set('needs_user_action', 'exit_wechat', 'waiting_for_exit')
                return self.state
            self._set('working', 'none', 'validating_keys')
            try:
                target = self._validated_key_file()
            except Exception:
                self._set('error', 'none', 'error', 'macos_reader_key_validation_failed')
                return self.state
            self._key_file = target
            self._set('ready', 'none', 'ready')
            if self.state != 'ready':
                self._key_file = None
            return self.state
        except Exception:
            self._stop_child()
            self._set('error', 'none', 'error', 'macos_reader_initialization_failed')
            return self.state

    def _stop_child(self):
        child, self._child = self._child, None
        if child is None:
            return True
        try:
            if child.poll() is not None:
                return True
            if type(child.pid) is not int or child.pid <= 1:
                raise ValueError
            for signum, timeout in ((signal.SIGINT, 10), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
                try:
                    # start_new_session=True makes only this owned helper's
                    # process group addressable. Restored original WeChat starts
                    # its own session in capture()'s finally and is excluded.
                    self.signal_group(child.pid, signum)
                    child.wait(timeout=timeout)
                    return True
                except subprocess.TimeoutExpired:
                    continue
                except ProcessLookupError:
                    child.wait(timeout=2)
                    return True
            self.reason = 'macos_reader_owned_helper_cleanup_unconfirmed'
            return False
        except Exception:
            self.reason = 'macos_reader_owned_helper_cleanup_unconfirmed'
            return False

    def close(self):
        if self._closed:
            return
        self._closed = True
        active = self._child is not None or self.state == 'needs_user_action'
        cleaned = self._stop_child()
        if active and self.state != 'ready':
            self._key_file = None
            self._set('error', 'none', 'cancelled', 'macos_reader_initialization_cancelled' if cleaned
                      else 'macos_reader_owned_helper_cleanup_unconfirmed')
