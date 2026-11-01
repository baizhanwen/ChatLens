#!/usr/bin/env python3
"""Local Mac Web workspace with automatic active-account onboarding."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from queue import SimpleQueue, Empty
import secrets
import signal
import stat
import sys
import threading
import time
import webbrowser

PROJECT = Path(__file__).resolve().parents[1]
for directory in (PROJECT / 'scripts', PROJECT / 'scripts/windows-proof'):
    sys.path.insert(0, str(directory))

from collector_private import ensure_private_dir, read_private_json, write_private_json_new
from macos_active_account import detect_active_account
from macos_collector import MacCollector, MacNativeProbe, prepare_session
from macos_reader_setup import ReaderInitializer
from macos_web_server import make_macos_server
from web_data import CollectorWebData, WebDataError
from windows_selected_engine import _open, ID


def atomic_private(path, value):
    ensure_private_dir(path.parent)
    if path.exists() or path.is_symlink():
        read_private_json(path)
    temporary = path.with_name('.web-' + secrets.token_hex(12) + '.json')
    write_private_json_new(temporary, value)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def account_identity(account):
    info = account.db_root.stat()
    return hashlib.sha256(json.dumps([str(account.db_root), info.st_dev, info.st_ino],
                                    separators=(',', ':')).encode()).hexdigest()


def acquire_runtime(workspace):
    ensure_private_dir(workspace)
    descriptor = os.open(workspace / '.runtime.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('macos_runtime_lock_unsafe')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class MacWebManager:
    """One owner thread mutates the collector; HTTP threads enqueue actions."""
    def __init__(self, project, workspace, probe, *, detector=detect_active_account,
                 initializer_factory=ReaderInitializer):
        self.project, self.workspace, self.probe = Path(project), Path(workspace), Path(probe)
        self.detector, self.initializer_factory = detector, initializer_factory
        self.lock, self.commands = threading.RLock(), SimpleQueue()
        self.account = self.directory = self.data_dir = self.key_file = None
        self.initializer = self.collector = self.provider = self.pending_session = None
        self.assistant = None
        self.closed = False
        self.contacts, self.catalog = [], {}
        self.stage, self.reason = 'needs_initialization', None
        self.description = '正在自动识别当前登录的微信。'
        self.queued = False
        self.last_detection = self.last_poll = 0.0

    def start(self):
        ensure_private_dir(self.workspace)
        ensure_private_dir(self.workspace / 'accounts', create=True)
        try:
            self._detect_first()
        except Exception:
            self.stage, self.reason = 'error', 'macos_saved_reader_unavailable'
            self.description = '已有读取配置暂不可用，私人资料已保留。请确认微信登录与本机权限后重试。'

    def _detect_first(self):
        try:
            self.account = self.detector(self.probe)
        except Exception as error:
            code = getattr(error, 'code', '')
            messages = {
                'macos_active_account_ambiguous': '发现多个活动账号，暂不能唯一确认。请仅保留要接入的微信账号后重试。',
                'macos_active_open_files_unavailable': '暂时无法核对微信的活动账号，请检查当前运行应用的本机读取权限。',
                'macos_active_path_unavailable': '暂时无法访问微信资料目录，请检查当前运行应用的完全磁盘访问权限。',
                'macos_active_process_changed': '微信进程发生变化，正在重新识别当前登录账号。',
            }
            with self.lock:
                self.reason = code if code in messages else 'macos_active_account_not_ready'
                self.description = messages.get(code, '请先登录微信；本页会自动识别当前账号。')
            return
        self.directory = self.workspace / 'accounts' / account_identity(self.account)[:24]
        ensure_private_dir(self.directory, create=True)
        self.data_dir = self.directory / 'collector-data'
        reader = self.directory / 'reader.json'
        if self.data_dir.exists():
            self.pending_session = prepare_session(self.data_dir, db_root=self.account.db_root)
            self.stage, self.reason = 'starting', None
            self.description = '正在恢复已选择会话的采集位置，请保持微信登录并切回网页。'
        elif reader.exists():
            value = read_private_json(reader)
            if value.get('account_identity') != account_identity(self.account):
                raise ValueError('macos_cached_reader_account_mismatch')
            self.key_file = Path(value['key_file'])
            self._load_contacts()
        else:
            self.reason = None
            self.description = '已识别当前登录账号。首次使用需要在本机初始化读取能力。'

    def setup(self):
        with self.lock:
            init = self.initializer
            phase = 'baseline' if self.stage == 'starting' else getattr(init, 'phase', None)
            state = 'working' if self.queued or self.stage == 'starting' else getattr(init, 'state', 'idle')
            if state in ('ready', 'error'):
                state = 'idle'
            return {'stage': self.stage, 'reason': self.reason,
                    'account': {'detected': self.account is not None, 'display_name': None},
                    'checks': {'wechat_running': self.account is not None,
                               'access_ready': self.account is not None,
                               'reader_ready': self.key_file is not None or self.collector is not None},
                    'progress': {'phase': phase, 'completed': None, 'total': None,
                                 'elapsed_seconds': int(getattr(init, 'elapsed_seconds', 0))},
                    'initialization': {'state': state, 'action': getattr(init, 'action', 'none'),
                                       'description': self.description},
                    'scope_limits': {'private': 1, 'group': 1}, 'workspace_path': '/'}

    def request_initialize(self):
        with self.lock:
            if self.account is None:
                return self.setup()
            if self.stage in ('ready_to_select', 'starting', 'running'):
                return self.setup()
            if not self.queued and (self.initializer is None or self.initializer.state in ('error', 'idle', 'ready')):
                self.queued = True
                self.commands.put(('initialize', None))
            return self.setup()

    def list_conversations(self, q=''):
        with self.lock:
            if self.stage != 'ready_to_select':
                return {'conversations': [], 'has_more': False}
            matches = [row for row in self.contacts if q.casefold() in row['name'].casefold()]
            return {'conversations': matches[:200], 'has_more': len(matches) > 200}

    def select(self, private_id, group_id):
        with self.lock:
            if self.stage != 'ready_to_select' or self.queued:
                raise ValueError('macos_selection_not_ready')
            private, group = self.catalog.get(private_id), self.catalog.get(group_id)
            if not private or not group or private['type'] != 'private' or group['type'] != 'group':
                raise ValueError('macos_selection_invalid')
            self.queued, self.stage = True, 'starting'
            self.description = '正在保存所选范围，建立启用后的新消息起点。'
            self.commands.put(('select', (private['chat_id'], group['chat_id'])))
            return self.setup()

    def _load_contacts(self):
        """Read conversation table names and contact metadata; no chat bodies."""
        import sqlcipher3.dbapi2 as driver
        from macos_collector import _authenticate_keys, _keys
        document = read_private_json(self.key_file)
        keys = _keys(document, messages=True)
        keys.update(_keys(document, messages=False))
        _authenticate_keys(self.account.db_root, keys)
        tables = set()
        for relative in keys:
            if not relative.startswith('message/'):
                continue
            connection = _open(driver, self.account.db_root / relative, keys[relative])
            try:
                deadline = time.monotonic() + 1.5
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
                rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*' LIMIT 10001").fetchall()
                if len(rows) > 10000:
                    raise ValueError('macos_contact_catalog_limit')
                tables.update(row[0] for row in rows)
            finally:
                connection.close()
        connection = _open(driver, self.account.db_root / 'contact/contact.db', keys['contact/contact.db'])
        try:
            deadline = time.monotonic() + 1.5
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = connection.execute('SELECT username,substr(remark,1,256),substr(nick_name,1,256) FROM contact LIMIT 10001').fetchall()
            if len(rows) > 10000:
                raise ValueError('macos_contact_catalog_limit')
        finally:
            connection.close()
        contacts, catalog = [], {}
        for username, remark, nickname in rows:
            if not isinstance(username, str) or ID.fullmatch(username) is None:
                continue
            if 'Msg_' + hashlib.md5(username.encode()).hexdigest() not in tables:
                continue
            name = next((v.strip() for v in (remark, nickname) if isinstance(v, str) and v.strip()), '未命名会话')
            name = ''.join(c for c in name if c >= ' ' and c != '\x7f')[:256]
            identifier = secrets.token_urlsafe(18)
            kind = 'group' if username.endswith('@chatroom') else 'private'
            contacts.append({'id': identifier, 'name': name or '未命名会话', 'type': kind})
            catalog[identifier] = {'chat_id': username, 'type': kind}
        contacts.sort(key=lambda row: (row['type'], row['name'].casefold()))
        with self.lock:
            self.contacts, self.catalog = contacts, catalog
            self.stage, self.reason = 'ready_to_select', None
            self.description = '读取能力已准备好。只会采集你接下来选定的会话新文字。'

    def _native(self):
        source = self.native_probe()
        if not source.get('probe_succeeded'):
            return source
        try:
            current = self.detector(self.probe)
            if (account_identity(current) != account_identity(self.account)
                    or current.pid != self.account.pid or current.started_at != self.account.started_at):
                raise ValueError
        except Exception:
            return {'probe_succeeded': False, 'running': None, 'frontmost': None, 'screen_locked': None}
        return source

    def tick(self):
        try:
            self._tick()
        except Exception:
            if self.collector is not None:
                self.collector.close(reason='macos_setup_operation_failed')
                self.collector = None
            with self.lock:
                self.stage, self.reason = 'error', 'macos_setup_operation_failed'
                self.queued = False
                self.description = '本次接入未完成，已有配置和采集位置已保留。请检查微信登录与本机读取权限。'

    def _tick(self):
        if self.account is None:
            if time.monotonic() - self.last_detection > 3:
                self.last_detection = time.monotonic()
                self._detect_first()
            return
        try:
            command, value = self.commands.get_nowait()
        except Empty:
            command = None
        if command == 'initialize':
            current = self.detector(self.probe)
            if account_identity(current) != account_identity(self.account):
                raise ValueError('macos_account_changed')
            self.account = current
            self.initializer = self.initializer_factory(self.account, self.directory, project_root=self.project)
            self.initializer.start()
            self.queued, self.stage, self.reason = False, 'needs_initialization', None
        elif command == 'select':
            selected = self.directory / ('selection-' + secrets.token_hex(8) + '.json')
            write_private_json_new(selected, {'conversations': [
                {'alias': 'private_test', 'chat_id': value[0]},
                {'alias': 'group_test', 'chat_id': value[1]}]})
            current = self.detector(self.probe)
            if account_identity(current) != account_identity(self.account):
                raise ValueError('macos_account_changed')
            self.account = current
            self.pending_session = prepare_session(self.data_dir, db_root=current.db_root,
                selected_config=selected, key_file=self.key_file, client_version=current.client_version)
            self.queued = False
        if self.initializer is not None and self.stage == 'needs_initialization':
            self.initializer.step()
            action = self.initializer.action
            with self.lock:
                self.description = {'exit_wechat': '读取准备已完成。请保存草稿，从微信菜单正常退出；本页会自动继续。',
                    'confirm_login': '请在刚打开的临时微信完成本机登录；如需手机确认，请在手机操作。完成后会恢复原版微信。',
                    'none': '正在准备本机读取能力，请保持本页打开。'}.get(action, '正在初始化。')
            if self.initializer.state == 'ready':
                self.key_file = self.initializer.key_file
                atomic_private(self.directory / 'reader.json', {'account_identity': account_identity(self.account), 'key_file': str(self.key_file)})
                self._load_contacts()
            elif self.initializer.state == 'error':
                self.stage, self.reason = 'error', 'macos_reader_initialization_failed'
                self.description = '本机读取初始化未完成。原版微信和私人资料保留，请完成微信登录后重试。'
        if self.pending_session is not None:
            try:
                try:
                    current = self.detector(self.probe)
                except Exception:
                    self.description = '请完成原版微信登录，再切回网页。准备好后会自动开始。'
                    return
                if account_identity(current) != account_identity(self.account):
                    raise ValueError
                probe = MacNativeProbe(self.probe, expected_version=self.pending_session.client_version)
                sample = probe()
                if not (sample.get('probe_succeeded') and sample.get('running') is True
                        and sample.get('frontmost') is False and sample.get('screen_locked') is False):
                    self.description = '请完成原版微信登录，再切回网页。准备好后会自动开始。'
                    return
                self.account, self.native_probe = current, probe
                self.collector = MacCollector(self.pending_session, self._native)
            except Exception as error:
                if getattr(error, 'code', '') in ('continuous_native_state_not_ready', 'continuous_native_state_unavailable'):
                    return
                raise
            self.pending_session = None
            self.provider = CollectorWebData(self.data_dir)
            self.stage, self.reason = 'running', None
            self.description = '正在采集你选择的会话新文字。'
        if self.collector is not None and time.monotonic() - self.last_poll >= 1:
            self.collector.poll()
            self.last_poll = time.monotonic()

    def _data(self):
        with self.lock:
            if self.provider is None:
                raise WebDataError('web_store_unavailable')
            return self.provider

    def conversations(self):
        return self._data().conversations()

    def messages(self, *args, **kwargs):
        return self._data().messages(*args, **kwargs)

    def status(self):
        return self._data().status()

    def close(self):
        with self.lock:
            self.closed = True
            assistant = self.assistant
        if assistant is not None:
            assistant.close()
        if self.initializer is not None:
            self.initializer.close()
        if self.collector is not None:
            self.collector.close()

    def ai_service(self):
        from ai_service import AiAssistant, AiError
        with self.lock:
            if self.closed:
                raise AiError('ai_service_stopped', 'AI 服务正在关闭。', 503)
            if self.provider is None or self.directory is None:
                raise AiError('ai_knowledge_not_ready', '请先完成微信接入并选择会话。', 409)
            if self.assistant is None:
                self.assistant = AiAssistant(self.provider.data_dir, self.directory / 'assistant')
            return self.assistant


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=PROJECT / 'artifacts/private/macos-web')
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin' or os.geteuid() == 0:
        print('reason=macos_user_session_required', flush=True)
        return 2
    os.umask(0o077)
    guard = None
    manager = server = thread = None
    stopping, handlers = threading.Event(), {}
    try:
        ensure_private_dir(args.workspace, create=True)
        guard = acquire_runtime(args.workspace)
        manager = MacWebManager(PROJECT, args.workspace, PROJECT / 'scripts/native/bin/macos-probe-web')
        manager.start()
        server = make_macos_server(manager, PROJECT / 'apps/web')
        from ai_web_server import attach_ai_routes
        attach_ai_routes(server, manager.ai_service)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        thread.start()
        atomic_private(args.workspace / 'session.json', {'launch_url': server.launch_url,
                                                        'base_url': server.base_url, 'pid': os.getpid()})
        print('ChatLens 本机网页已启动，将自动识别当前登录的微信账号。', flush=True)
        print('首次接入和会话范围请在网页完成；按 Ctrl+C 正常停止。', flush=True)
        if not args.no_open:
            webbrowser.open(server.launch_url)
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, lambda *_: stopping.set())
        while not stopping.wait(.2):
            if not thread.is_alive():
                raise ValueError('macos_web_worker_stopped')
            manager.tick()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        print('reason=macos_web_start_or_runtime_failed', flush=True)
        return 2
    finally:
        if manager is not None:
            manager.close()
        if server is not None:
            if thread is not None and thread.is_alive():
                server.shutdown()
                thread.join(timeout=2)
            server.server_close()
        for signum, previous in handlers.items():
            signal.signal(signum, previous)
        if guard is not None:
            os.close(guard)


if __name__ == '__main__':
    raise SystemExit(main())
