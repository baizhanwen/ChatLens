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
import re
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
from macos_active_account import ActiveAccountError, METADATA_ISSUES, detect_active_account
from macos_collector import MacCollector, MacNativeProbe, prepare_session
from macos_reader_setup import ReaderInitializer
from macos_web_server import make_macos_server
from web_data import CollectorWebData, WebDataError
from windows_selected_engine import _open, ID
from conversation_scope import alias_for, normalize_conversations, MAX_CONVERSATIONS
from scope_history import write_history_plan

PROFILE = re.compile(r'[0-9a-f]{64}\Z')
ACTIVE_ACCOUNT_MESSAGES = {
    'macos_active_process_not_running': '没有检测到本机微信程序。请打开微信并由你本人完成登录，本页会自动重新检查。',
    'macos_active_process_ambiguous': '发现多个微信程序，暂时无法确认当前账号。请仅保留需要接入的微信后重新检查。',
    'macos_active_process_metadata_invalid': '微信进程信息未能完整核验。请检查运行入口的本机权限后重新检查。',
    'macos_active_process_probe_failed': '暂时无法取得微信运行状态。请检查运行入口的本机权限后重新检查。',
    'macos_active_account_ambiguous': '检测到多个账号的活动资料，暂时无法唯一确认当前账号。请从微信菜单正常退出后重新打开当前账号，再重新检查。',
    'macos_active_account_not_identified': '已找到微信，但暂时无法从已打开的资料确认当前账号。若刚切换账号，请等待微信完成登录后重新检查。',
    'macos_active_open_files_unavailable': '已找到微信，但暂时无法核对活动资料。请检查当前运行入口的本机读取权限。',
    'macos_active_path_unavailable': '暂时无法访问本机读取组件或微信资料目录。请检查当前运行入口的完全磁盘访问权限。',
    'macos_active_process_changed': '微信进程发生变化，正在重新识别当前登录账号。',
    'macos_active_process_rebind_unavailable': '已识别重新打开的微信，但采集状态核验暂未就绪，已暂停采集。请保持屏幕解锁后重新检查。',
    'macos_active_client_version_changed': '当前微信版本与已有读取配置不一致，已暂停采集。请保留本机资料并反馈版本变化。',
    'macos_active_account_root_changed': '微信资料目录发生变化，正在重新识别当前账号。',
    'macos_active_open_file_limit': '微信活动资料数量超过本次核验上限，未能确认当前账号。请重新检查或反馈状态详情。',
    'macos_active_open_file_process_mismatch': '微信活动资料的进程依据不一致，未能确认当前账号。请重新检查。',
    'macos_active_open_file_path_invalid': '微信活动资料路径不符合核验条件，未能确认当前账号。请反馈状态详情。',
    'macos_active_path_unsafe': '本机读取路径不符合安全核验条件。请反馈状态详情。',
    'macos_active_path_redirected': '本机读取路径存在重定向，未通过安全核验。请反馈状态详情。',
    'macos_active_directory_required': '本机资料目录类型不符合核验条件。请反馈状态详情。',
    'macos_active_file_unsafe': '本机读取文件不符合安全核验条件。请反馈状态详情。',
    'macos_active_local_volume_required': '微信资料需要位于本机磁盘，当前目录未通过核验。请反馈状态详情。',
    'macos_active_mount_metadata_unavailable': '无法核验微信资料所在磁盘的元信息。请检查本机权限后重新检查。',
    'macos_active_native_platform_required': '当前运行环境无法执行 macOS 本机磁盘核验。请使用本机 Mac 启动入口。',
}
METADATA_ISSUE_MESSAGES = {
    'client_version': '检测到微信程序，但无法核验客户端版本。请检查实际启动终端对微信所在目录的访问权限后重新检查。',
    'process_started_at': '检测到微信程序，但无法核验进程启动时间。请检查实际启动终端的本机权限后重新检查。',
    'process_id': '检测到微信程序，但无法核验进程身份。请检查实际启动终端的本机权限后重新检查。',
    'frontmost_state': '检测到微信程序，但无法核验前后台状态。请检查实际启动终端的本机权限后重新检查。',
    'screen_state': '检测到微信程序，但无法核验屏幕状态。请检查实际启动终端的本机权限后重新检查。',
}


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
        self.scope_generation = secrets.token_hex(16)
        self.active_scope_id = 'legacy'
        self.active_config = {'conversations': []}
        self.switching = False
        self.catalog_loading = False
        self.catalog_ready = False
        self.pending_scope_id = None
        self.pending_display_names = []
        self.pending_identity_hint = None
        self.observed_account = None
        self.active_account_identity = None
        self.current_account_detected = False
        self.wechat_running = None
        self.detection_reason = self.detection_metadata_issue = self.detection_description = None

    def _record_detection_failure(self, error):
        code = getattr(error, 'code', '')
        known = isinstance(error, ActiveAccountError) and code in ACTIVE_ACCOUNT_MESSAGES
        with self.lock:
            self.current_account_detected = False
            self.wechat_running = error.wechat_running if known and type(error.wechat_running) is bool else None
            issue = error.metadata_issue if known and error.metadata_issue in METADATA_ISSUES else None
            self.detection_metadata_issue = issue
            self.detection_reason = code if known else 'macos_active_account_not_ready'
            self.detection_description = ACTIVE_ACCOUNT_MESSAGES.get(code) if known else None
            if known and code == 'macos_active_process_metadata_invalid' and self.wechat_running is True:
                self.detection_description = METADATA_ISSUE_MESSAGES.get(issue, self.detection_description)
            if self.detection_description is None:
                self.detection_description = '暂时无法确认本机微信状态。请检查运行入口的本机权限后重新检查。'

    def _record_detection_success(self):
        with self.lock:
            self.current_account_detected = True
            self.wechat_running = True
            self.detection_reason = self.detection_metadata_issue = self.detection_description = None

    def public_scope_context(self):
        with self.lock:
            return {'scope_generation': self.scope_generation,
                    'scope_id': self.active_scope_id, 'scope_switching': self.switching}

    def _check_generation(self, expected_generation):
        if expected_generation is not None and expected_generation != self.scope_generation:
            raise WebDataError('scope_changed', 409)

    def _profile_root(self, identifier):
        if self.directory is None or identifier != 'legacy' and (not isinstance(identifier, str) or PROFILE.fullmatch(identifier) is None):
            raise ValueError('macos_profile_invalid')
        if identifier == 'legacy':
            return self.directory
        parent = self.directory / 'profiles'
        if parent.exists() or parent.is_symlink():
            ensure_private_dir(parent)
        path = parent / identifier
        if path.exists() or path.is_symlink():
            ensure_private_dir(path)
        return path

    def _profile_data(self, identifier):
        return self._profile_root(identifier) / 'collector-data'

    def _assistant_directory(self, identifier=None):
        return self._profile_root(identifier or self.active_scope_id) / 'assistant'

    @staticmethod
    def _scope_fingerprint(chats):
        identifiers = sorted(chat['chat_id'] for chat in chats)
        return hashlib.sha256(json.dumps(identifiers, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

    def _saved_profiles(self):
        if self.directory is None or self.account is None:
            return []
        entries = []
        legacy = self.directory / 'collector-data/private/scope.json'
        if legacy.exists() or legacy.is_symlink():
            try:
                value = read_private_json(legacy)
                chats = normalize_conversations(value['config'])
                if Path(value['db_root']) == self.account.db_root:
                    entries.append({'id': 'legacy', 'label': '原关注范围', 'count': len(chats),
                                    'private_count': sum(not row['chat_id'].endswith('@chatroom') for row in chats),
                                    'group_count': sum(row['chat_id'].endswith('@chatroom') for row in chats),
                                    'active': self.active_scope_id == 'legacy'})
            except Exception:
                pass
        parent = self.directory / 'profiles'
        if parent.exists() or parent.is_symlink():
            ensure_private_dir(parent)
            paths = list(parent.iterdir())
            if len(paths) > 100:
                raise ValueError('macos_profile_limit')
            for path in sorted(paths):
                if not PROFILE.fullmatch(path.name):
                    continue
                ensure_private_dir(path)
                try:
                    value = read_private_json(path / 'profile.json')
                    if (value.get('schema_version') != 1 or value.get('account_identity') != account_identity(self.account)
                            or value.get('profile_id') != path.name):
                        continue
                    chats = normalize_conversations({'conversations': value['conversations']})
                    if self._scope_fingerprint(chats) != path.name:
                        continue
                    names = value.get('display_names', [])
                    label = '、'.join(name for name in names[:3] if isinstance(name, str))[:140]
                    entries.append({'id': path.name, 'label': label or '已保存关注范围', 'count': len(chats),
                                    'private_count': sum(not row['chat_id'].endswith('@chatroom') for row in chats),
                                    'group_count': sum(row['chat_id'].endswith('@chatroom') for row in chats),
                                    'active': self.active_scope_id == path.name})
                except Exception:
                    continue
        return entries

    def _activate_pointer(self):
        atomic_private(self.directory / 'active-scope.json',
                       {'schema_version': 1, 'account_identity': account_identity(self.account),
                        'profile_id': self.active_scope_id})

    def _read_active_pointer(self):
        pointer = self.directory / 'active-scope.json'
        if not pointer.exists() and not pointer.is_symlink():
            return 'legacy'
        value = read_private_json(pointer)
        identifier = value.get('profile_id')
        if (value.get('schema_version') != 1 or value.get('account_identity') != account_identity(self.account)
                or identifier != 'legacy' and (not isinstance(identifier, str) or PROFILE.fullmatch(identifier) is None)):
            raise ValueError('macos_active_profile_invalid')
        return identifier

    def _begin_switch(self):
        self.scope_generation = secrets.token_hex(16)
        self.switching = True
        self.queued, self.stage, self.reason = True, 'starting', None
        self.description = '正在修改关注范围；仍被关注会话的已采消息和进度会保留。'

    def _stop_active(self):
        # Hold the manager lock so an HTTP handler cannot obtain the old
        # assistant/provider between invalidation and shutdown.
        with self.lock:
            assistant, collector = self.assistant, self.collector
            self.assistant = self.collector = self.provider = None
            self.pending_session = None
            try:
                if assistant is not None:
                    assistant.close()
            finally:
                if collector is not None:
                    collector.close(reason='macos_scope_switch')

    def _copy_model_config(self, source, target):
        if source == target or not (source / 'provider.json').exists():
            return
        ensure_private_dir(source)
        ensure_private_dir(target.parent, create=True)
        ensure_private_dir(target, create=True)
        if not (target / 'provider.json').exists() and not (target / 'provider.json').is_symlink():
            write_private_json_new(target / 'provider.json', read_private_json(source / 'provider.json'))

    def _prepare_history_plan(self, target, continuity_source=None):
        """Only current-account saved profiles may donate collected text."""
        sources = []
        for profile in self._saved_profiles():
            source = self._profile_data(profile['id'])
            if source == target:
                continue
            database = source / 'private/store/collector.sqlite3'
            if database.exists() or database.is_symlink():
                sources.append(source)
        if sources:
            write_history_plan(target, sources,
                               continuity_source=continuity_source if continuity_source in sources else None)

    def _capture_identity_hint(self):
        from wechat_identity import trusted_self_identity
        provider = self.provider
        if provider is None or not hasattr(provider, '_metadata'):
            return None
        try:
            catalog = provider._metadata('catalog.json')
            with provider._open() as (_, snapshot):
                binding = snapshot['binding_digest']
            if trusted_self_identity(catalog, binding) is None:
                return None
            return {'catalog': catalog, 'binding_digest': binding,
                    'account_context': account_identity(self.account)}
        except Exception:
            return None

    def _prepare_scope(self, identifier, chats=None, display_names=None):
        current = self.detector(self.probe)
        if account_identity(current) != self.active_account_identity:
            self._change_account(current)
            return
        if chats is not None:
            legacy_scope = self.directory / 'collector-data/private/scope.json'
            if legacy_scope.exists() or legacy_scope.is_symlink():
                legacy = read_private_json(legacy_scope)
                legacy_chats = normalize_conversations(legacy['config'])
                if self._scope_fingerprint(legacy_chats) == self._scope_fingerprint(chats):
                    identifier = 'legacy'
                elif identifier == 'legacy':
                    chats = normalize_conversations({'conversations': [
                        {'alias': alias_for(row['chat_id']), 'chat_id': row['chat_id']} for row in chats]})
                    identifier = self._scope_fingerprint(chats)
        source_assistant = self._assistant_directory()
        continuity_source = self.data_dir
        identity_hint = self._capture_identity_hint()
        self._stop_active()
        self.account = current
        target_root = self._profile_root(identifier)
        if identifier != 'legacy':
            ensure_private_dir(target_root.parent, create=True)
            ensure_private_dir(target_root, create=True)
        self.data_dir = self._profile_data(identifier)
        if chats is None:
            session = prepare_session(self.data_dir, db_root=current.db_root)
        else:
            selected = self.directory / ('selection-' + secrets.token_hex(8) + '.json')
            write_private_json_new(selected, {'conversations': chats})
            session = prepare_session(self.data_dir, db_root=current.db_root,
                selected_config=selected, key_file=self.key_file, client_version=current.client_version)
        if identifier != 'legacy' and self._scope_fingerprint(session.config['conversations']) != identifier:
            raise ValueError('macos_profile_scope_mismatch')
        if chats is not None and self._scope_fingerprint(session.config['conversations']) != self._scope_fingerprint(chats):
            raise ValueError('macos_selected_profile_scope_mismatch')
        self._prepare_history_plan(self.data_dir, continuity_source)
        self._copy_model_config(source_assistant, target_root / 'assistant')
        if identifier != 'legacy':
            manifest = target_root / 'profile.json'
            if not manifest.exists() and not manifest.is_symlink():
                canonical = getattr(session, 'config', {'conversations': chats or []})
                write_private_json_new(manifest, {'schema_version': 1,
                    'account_identity': account_identity(self.account), 'profile_id': identifier,
                    'conversations': canonical['conversations'], 'display_names': list(display_names or []),
                    'created_at': datetime.now(timezone.utc).isoformat()})
        self.pending_session = session
        self.pending_scope_id = identifier
        self.pending_identity_hint = identity_hint
        self.pending_display_names = list(display_names or [])
        self.queued = False

    def start(self):
        ensure_private_dir(self.workspace)
        ensure_private_dir(self.workspace / 'accounts', create=True)
        try:
            self._detect_first()
        except Exception:
            self.stage, self.reason = 'error', 'macos_saved_reader_unavailable'
            self.description = '已有读取配置暂不可用，私人资料已保留。请确认微信登录与本机权限后重试。'

    def _detect_first(self, detected=None):
        try:
            self.account = detected if detected is not None else self.detector(self.probe)
        except Exception as error:
            self._record_detection_failure(error)
            with self.lock:
                self.reason = self.detection_reason
                self.description = self.detection_description
            return
        self._record_detection_success()
        self.last_detection = time.monotonic()
        self.active_account_identity = account_identity(self.account)
        self.directory = self.workspace / 'accounts' / self.active_account_identity[:24]
        ensure_private_dir(self.directory, create=True)
        self.active_scope_id = self._read_active_pointer()
        self.data_dir = self._profile_data(self.active_scope_id)
        reader = self.directory / 'reader.json'
        if reader.exists() or reader.is_symlink():
            value = read_private_json(reader)
            if value.get('account_identity') != account_identity(self.account):
                raise ValueError('macos_cached_reader_account_mismatch')
            self.key_file = Path(value['key_file'])
        if self.data_dir.exists():
            self.pending_session = prepare_session(self.data_dir, db_root=self.account.db_root)
            self.active_config = getattr(self.pending_session, 'config', {'conversations': []})
            if (self.active_scope_id != 'legacy'
                    and self._scope_fingerprint(self.active_config['conversations']) != self.active_scope_id):
                raise ValueError('macos_active_profile_scope_mismatch')
            # Also repair already-created profiles from earlier builds: merge
            # their selected chats' previously saved rows without rewinding.
            self._prepare_history_plan(self.data_dir)
            self.pending_scope_id = self.active_scope_id
            if self.key_file is None:
                self.key_file = self.data_dir / 'private/keys.json'
            self.switching = True
            self.stage, self.reason = 'starting', None
            self.description = '正在恢复已选择会话的采集位置，请保持微信登录并切回网页。'
        elif self.active_scope_id != 'legacy':
            raise ValueError('macos_active_profile_missing')
        elif self.key_file is not None:
            self._load_contacts()
        else:
            self.reason = None
            self.description = '已识别当前登录账号。首次使用需要在本机初始化读取能力。'

    def _initializer_active(self):
        return (self.stage == 'needs_initialization' and self.initializer is not None
                and self.initializer.state not in ('idle', 'ready', 'error'))

    def _change_account(self, current):
        """Invalidate the old domain before discovering the new account's state."""
        with self.lock:
            self.scope_generation = secrets.token_hex(16)
            self.switching = True
            initializer, self.initializer = self.initializer, None
            try:
                self._stop_active()
            finally:
                # Nothing from the old account may be used for the new reader,
                # picker, assistant, or queued HTTP operation, even if shutdown
                # reports an error. Saved private files remain in their domain.
                self.account = self.directory = self.data_dir = self.key_file = None
                self.active_account_identity = None
                self.current_account_detected = False
                self.wechat_running = None
                self.detection_reason = self.detection_metadata_issue = self.detection_description = None
                self.contacts, self.catalog = [], {}
                self.commands = SimpleQueue()
                self.active_scope_id = 'legacy'
                self.active_config = {'conversations': []}
                self.pending_session = self.pending_scope_id = self.pending_identity_hint = None
                self.pending_display_names = []
                self.catalog_loading = self.catalog_ready = self.queued = False
                self.observed_account = None
                self.last_poll = 0.0
                self.native_probe = None
                self.stage, self.reason = 'needs_initialization', None
                self.switching = False
                self.description = '已切换微信账号，正在识别该账号自己的本机读取配置。'
                if initializer is not None:
                    initializer.close()
            self._detect_first(current)

    def _refresh_active_account(self, *, force=False):
        """Owner-thread safety point; a temporary detection failure is offline."""
        if self.account is None or self._initializer_active():
            return False
        current, self.observed_account = self.observed_account, None
        if current is None:
            now = time.monotonic()
            if not force and now - self.last_detection < 3:
                return not self.current_account_detected
            self.last_detection = now
            try:
                current = self.detector(self.probe)
                identity = account_identity(current)
            except Exception as error:
                self._record_detection_failure(error)
                return True
        else:
            identity = account_identity(current)
        if identity == self.active_account_identity:
            session = self.pending_session or getattr(self.collector, 'session', None)
            expected_version = getattr(session, 'client_version', None)
            if expected_version is None and self.key_file is not None:
                expected_version = self.account.client_version
            if expected_version is not None and current.client_version != expected_version:
                self._record_detection_failure(ActiveAccountError('macos_active_client_version_changed', wechat_running=True))
                return True
            if (self.collector is not None
                    and (current.pid, current.started_at) != (self.account.pid, self.account.started_at)):
                # Native probes pin one process. Recreate that pin only after a
                # complete same-root detection and compatible saved version,
                # then independently confirm its sample and root once more.
                try:
                    fresh_probe = MacNativeProbe(self.probe, expected_version=expected_version or current.client_version)
                    sample = fresh_probe()
                    if (sample.get('probe_succeeded') is not True
                            or fresh_probe.process_identity != (current.pid, current.started_at, current.client_version)):
                        raise ActiveAccountError('macos_active_process_rebind_unavailable', wechat_running=True)
                    verified = self.detector(self.probe)
                    if (account_identity(verified) != self.active_account_identity
                            or (verified.pid, verified.started_at, verified.client_version)
                            != (current.pid, current.started_at, current.client_version)):
                        raise ActiveAccountError('macos_active_process_changed', wechat_running=True)
                except Exception as error:
                    self._record_detection_failure(error)
                    return True
                self.native_probe = fresh_probe
            # A fresh complete detection pins the same source root to its new
            # process after a normal restart; keep the source/version guards.
            self.account = current
            self._record_detection_success()
            return False
        self._change_account(current)
        return True

    def setup(self):
        with self.lock:
            detected = self.account is not None and self.current_account_detected
            detection_blocked = self.account is not None and not detected
            stage = 'needs_initialization' if detection_blocked else self.stage
            init = self.initializer
            phase = None if detection_blocked else 'baseline' if self.stage == 'starting' else getattr(init, 'phase', None)
            state = 'idle' if detection_blocked else 'working' if self.queued or self.stage == 'starting' else getattr(init, 'state', 'idle')
            if state in ('ready', 'error'):
                state = 'idle'
            return {'stage': stage, 'reason': self.detection_reason or self.reason,
                    'account': {'detected': detected, 'display_name': None},
                    'checks': {'wechat_running': self.wechat_running,
                               'access_ready': detected,
                               'reader_ready': detected and (self.key_file is not None or self.collector is not None)},
                    'detection': {'metadata_issue': self.detection_metadata_issue,
                                  'collector_paused': self.collector is not None and not detected},
                    'progress': {'phase': phase, 'completed': None, 'total': None,
                                 'elapsed_seconds': int(getattr(init, 'elapsed_seconds', 0))},
                    'initialization': {'state': state, 'action': 'none' if detection_blocked else getattr(init, 'action', 'none'),
                                       'description': self.detection_description or self.description},
                    'scope_limits': {'total': MAX_CONVERSATIONS, 'private': MAX_CONVERSATIONS, 'group': MAX_CONVERSATIONS},
                    'scope_generation': self.scope_generation, 'scope_id': self.active_scope_id,
                    'scope_switching': self.switching,
                    'catalog': {'state': 'idle' if not detected else 'loading' if self.catalog_loading else 'ready' if self.catalog_ready else 'idle'},
                    'active_scope': next((row for row in self._saved_profiles() if row['active']), None) if detected else None,
                    'profiles': self._saved_profiles() if detected else [], 'workspace_path': '/'}

    def request_initialize(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self.account is None or not self.current_account_detected:
                return self.setup()
            if self.stage in ('ready_to_select', 'starting', 'running'):
                return self.setup()
            if not self.queued and (self.initializer is None or self.initializer.state in ('error', 'idle', 'ready')):
                self.queued = True
                self.commands.put(('initialize', None))
            return self.setup()

    def list_conversations(self, q='', *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if not self.current_account_detected or self.stage != 'ready_to_select' and not self.catalog_ready:
                return {'conversations': [], 'selected_conversations': [], 'has_more': False}
            matches = [row for row in self.contacts if q.casefold() in row['name'].casefold()]
            if not q:
                # Current selections must fit in the first picker page even
                # when the account has thousands of other conversations.
                matches.sort(key=lambda row: (not row.get('selected', False),
                                              row['type'], row['name'].casefold()))
            return {'conversations': matches[:200], 'has_more': len(matches) > 200,
                    'selected_conversations': [dict(row) for row in self.contacts if row.get('selected')][:50]}

    def request_catalog(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self.account is None or not self.current_account_detected or self.key_file is None or self.switching:
                raise ValueError('macos_selection_not_ready')
            if not self.catalog_loading:
                self.catalog_loading = True
                self.commands.put(('catalog', None))
            return self.setup()

    def select(self, private_id, group_id, *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if not self.current_account_detected or self.stage not in ('ready_to_select', 'running') or self.queued or self.switching:
                raise ValueError('macos_selection_not_ready')
            private, group = self.catalog.get(private_id), self.catalog.get(group_id)
            if not private or not group or private['type'] != 'private' or group['type'] != 'group':
                raise ValueError('macos_selection_invalid')
            self.queued, self.stage = True, 'starting'
            self.description = '正在保存所选范围，建立启用后的新消息起点。'
            self.commands.put(('select', (private['chat_id'], group['chat_id'])))
            self.scope_generation = secrets.token_hex(16)
            self.switching = True
            return self.setup()

    def select_many(self, conversation_ids, *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if (not self.current_account_detected or self.stage not in ('ready_to_select', 'running', 'error') or self.queued or self.switching
                    or not self.catalog_ready and self.stage != 'ready_to_select'):
                raise ValueError('macos_selection_not_ready')
            if (not isinstance(conversation_ids, list) or not 1 <= len(conversation_ids) <= MAX_CONVERSATIONS
                    or any(not isinstance(value, str) or value not in self.catalog for value in conversation_ids)
                    or len(set(conversation_ids)) != len(conversation_ids)):
                raise ValueError('macos_selection_invalid')
            rows = [self.catalog[value] for value in conversation_ids]
            chats = [{'alias': alias_for(row['chat_id']), 'chat_id': row['chat_id']} for row in rows]
            existing = self._saved_profiles()
            identifier = self._scope_fingerprint(chats)
            if len(existing) >= 100 and not any(row['id'] == identifier for row in existing):
                raise ValueError('macos_profile_limit')
            names = {row['id']: row['name'] for row in self.contacts}
            display = [names.get(value, '未命名会话') for value in conversation_ids]
            self._begin_switch()
            self.commands.put(('select_many', {'chats': chats, 'display_names': display}))
            return self.setup()

    def restore_profile(self, identifier, *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if not self.current_account_detected or self.queued or self.switching or not any(row['id'] == identifier for row in self._saved_profiles()):
                raise ValueError('macos_profile_not_ready')
            self._begin_switch()
            self.commands.put(('profile', identifier))
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
        with self.lock:
            previous_identifiers = {value['chat_id']: identifier for identifier, value in self.catalog.items()
                                    if isinstance(value, dict) and isinstance(value.get('chat_id'), str)}
        selected = {row['chat_id'] for row in self.active_config.get('conversations', [])}
        for username, remark, nickname in rows:
            if not isinstance(username, str) or ID.fullmatch(username) is None:
                continue
            if 'Msg_' + hashlib.md5(username.encode()).hexdigest() not in tables:
                continue
            name = next((v.strip() for v in (remark, nickname) if isinstance(v, str) and v.strip()), '未命名会话')
            name = ''.join(c for c in name if c >= ' ' and c != '\x7f')[:256]
            identifier = previous_identifiers.get(username) or secrets.token_urlsafe(18)
            kind = 'group' if username.endswith('@chatroom') else 'private'
            contacts.append({'id': identifier, 'name': name or '未命名会话', 'type': kind,
                             'selected': username in selected})
            catalog[identifier] = {'chat_id': username, 'type': kind}
        contacts.sort(key=lambda row: (row['type'], row['name'].casefold()))
        with self.lock:
            self.contacts, self.catalog = contacts, catalog
            self.catalog_ready, self.catalog_loading = True, False
            if self.collector is None and not self.switching:
                self.stage, self.reason = 'ready_to_select', None
                self.description = '读取能力已准备好。只会采集你接下来选定的会话新文字。'

    def _native(self):
        if not self.current_account_detected:
            return {'probe_succeeded': False, 'running': None, 'frontmost': None, 'screen_locked': None}
        source = self.native_probe()
        if not source.get('probe_succeeded'):
            return source
        try:
            current = self.detector(self.probe)
            if account_identity(current) != self.active_account_identity:
                self.observed_account = current
                raise ValueError
            if current.pid != self.account.pid or current.started_at != self.account.started_at:
                raise ActiveAccountError('macos_active_process_changed', wechat_running=True)
            if current.client_version != self.account.client_version:
                raise ActiveAccountError('macos_active_client_version_changed', wechat_running=True)
        except Exception as error:
            self._record_detection_failure(error)
            return {'probe_succeeded': False, 'running': None, 'frontmost': None, 'screen_locked': None}
        return source

    def tick(self):
        try:
            self._tick()
        except Exception:
            self._stop_active()
            with self.lock:
                self.stage, self.reason = 'error', 'macos_setup_operation_failed'
                self.queued = False
                self.switching = False
                self.description = '本次接入未完成，已有配置和采集位置已保留。请检查微信登录与本机读取权限。'

    def _tick(self):
        if self.account is None:
            if time.monotonic() - self.last_detection > 3:
                self.last_detection = time.monotonic()
                self._detect_first()
            return
        if self._refresh_active_account(force=self.queued or self.pending_session is not None):
            if (self.collector is not None and not self.current_account_detected
                    and time.monotonic() - self.last_poll >= 1):
                # Publish a paused heartbeat through the collector's existing
                # native-state guard. No account or source-read rule is relaxed.
                self.collector.poll()
                self.last_poll = time.monotonic()
            return
        try:
            command, value = self.commands.get_nowait()
        except Empty:
            command = None
        if command == 'initialize':
            current = self.detector(self.probe)
            if account_identity(current) != self.active_account_identity:
                self._change_account(current)
                return
            self.account = current
            self.initializer = self.initializer_factory(self.account, self.directory, project_root=self.project)
            self.initializer.start()
            self.queued, self.stage, self.reason = False, 'needs_initialization', None
        elif command == 'catalog':
            try:
                current = self.detector(self.probe)
                if account_identity(current) != self.active_account_identity:
                    self._change_account(current)
                    return
                self._load_contacts()
            except Exception:
                self.catalog_loading = self.catalog_ready = False
                self.reason = 'macos_contact_catalog_unavailable'
                self.description = '可选会话暂时未能载入，当前关注范围继续保留。请确认微信登录后重试。'
        elif command == 'select':
            self._prepare_scope('legacy', [
                {'alias': 'private_test', 'chat_id': value[0]},
                {'alias': 'group_test', 'chat_id': value[1]}])
        elif command == 'select_many':
            chats = normalize_conversations({'conversations': value['chats']})
            self._prepare_scope(self._scope_fingerprint(chats), chats, value['display_names'])
        elif command == 'profile':
            self._prepare_scope(value)
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
                if account_identity(current) != self.active_account_identity:
                    self._change_account(current)
                    return
                probe = MacNativeProbe(self.probe, expected_version=self.pending_session.client_version)
                sample = probe()
                if not (sample.get('probe_succeeded') and sample.get('running') is True
                        and sample.get('frontmost') is False and sample.get('screen_locked') is False):
                    self.description = '请完成原版微信登录，再切回网页。准备好后会自动开始。'
                    return
                self.account, self.native_probe = current, probe
                options = {'identity_hint': self.pending_identity_hint} if self.pending_identity_hint else {}
                self.collector = MacCollector(self.pending_session, self._native, **options)
            except Exception as error:
                if getattr(error, 'code', '') in ('continuous_native_state_not_ready', 'continuous_native_state_unavailable'):
                    return
                raise
            self.active_config = getattr(self.pending_session, 'config', self.active_config)
            self.active_scope_id = self.pending_scope_id or self.active_scope_id
            self.pending_scope_id = self.pending_identity_hint = None
            self.pending_session = None
            self.provider = CollectorWebData(self.data_dir)
            self._activate_pointer()
            self.switching = False
            self.catalog_ready = False
            self.stage, self.reason = 'running', None
            self.description = '正在采集你选择的会话新文字。'
        if self.collector is not None and time.monotonic() - self.last_poll >= 1:
            self.collector.poll()
            self.last_poll = time.monotonic()
            if self.observed_account is not None:
                self._refresh_active_account()

    def _data(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self.provider is None or self.switching:
                raise WebDataError('web_store_unavailable')
            return self.provider

    def conversations(self, *, expected_generation=None):
        with self.lock:
            return self._data(expected_generation).conversations() | self.public_scope_context()

    def messages(self, *args, **kwargs):
        expected = kwargs.pop('expected_generation', None)
        with self.lock:
            return self._data(expected).messages(*args, **kwargs) | self.public_scope_context()

    def status(self, *, expected_generation=None):
        with self.lock:
            result = self._data(expected_generation).status() | self.public_scope_context()
            if not self.current_account_detected and result.get('state') in ('starting', 'collecting', 'paused'):
                result.update(state='paused', reason=self.detection_reason or 'macos_active_account_not_ready')
            return result

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

    def ai_service(self, expected_generation=None):
        from ai_service import AiAssistant, AiError
        with self.lock:
            if expected_generation is not None and expected_generation != self.scope_generation:
                raise AiError('scope_changed', '关注范围已经改变，请重新打开工作台。', 409)
            if self.closed:
                raise AiError('ai_service_stopped', 'AI 服务正在关闭。', 503)
            if self.provider is None or self.directory is None or self.switching:
                raise AiError('ai_knowledge_not_ready', '请先完成微信接入并选择会话。', 409)
            if self.assistant is None:
                self.assistant = AiAssistant(self.provider.data_dir, self._assistant_directory())
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
