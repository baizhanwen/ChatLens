"""Windows selected scopes: preserve the original store and isolate new profiles.

HTTP threads enqueue changes; the launcher owner tick stops and starts only its
own supervised collectors. No account is inferred from a nickname or old PID.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from queue import Empty, SimpleQueue
import re
import secrets
import threading
import time

from collector_private import ensure_private_dir, read_private_json, write_private_json_new, validate_local_path
from collector_service import prepare_session, validate_scope
from conversation_scope import MAX_CONVERSATIONS, alias_for, normalize_conversations
from web_data import CollectorWebData, WebDataError
from windows_selected_engine import ID, _open

PROFILE = re.compile(r'[0-9a-f]{64}\Z')


def atomic_private(path, document):
    path = Path(path)
    ensure_private_dir(path.parent)
    if path.exists() or path.is_symlink():
        read_private_json(path)
    temporary = path.with_name('.scope-' + secrets.token_hex(12) + '.json')
    write_private_json_new(temporary, document)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def scope_fingerprint(chats):
    ids = sorted(row['chat_id'] for row in normalize_conversations({'conversations': chats}))
    return hashlib.sha256(json.dumps(ids, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def source_account_identity(scope):
    root = Path(scope['db_root'])
    info = root.stat()
    return hashlib.sha256(json.dumps([str(root), info.st_dev, info.st_ino, str(scope['exe_path'])],
                                    separators=(',', ':')).encode()).hexdigest()


CATALOG_SECONDS = 30
CATALOG_LIMIT = 10000
CATALOG_PHASES = {'idle', 'queued', 'validating_sources', 'opening_message', 'reading_message_tables',
                  'opening_contacts', 'reading_contacts', 'preparing_choices', 'complete'}
CATALOG_REASONS = {'windows_scope_catalog_timeout', 'windows_scope_catalog_cancelled',
                   'windows_scope_catalog_limit', 'windows_scope_key_authentication_failed',
                   'windows_scope_catalog_source_changed', 'windows_scope_catalog_unavailable'}
CATALOG_COUNTS = ('databases_checked', 'tables_seen', 'contacts_seen', 'conversations_found')


class CatalogReadError(ValueError):
    """A fixed public code, never a driver exception or database path."""


class CatalogRead:
    """One cooperative, bounded read with an immutable source/domain snapshot.

    The worker owns connections and closes them. Cancellation may call SQLite's
    thread-safe interrupt(), but never closes a connection from another thread.
    Native/open calls cannot be forcibly killed: a still-live worker prevents a
    replacement task even after its deadline or cancellation has been reported.
    """
    def __init__(self, documents, generation, account_identity, *, clock=time.monotonic):
        self.documents = documents
        self.generation, self.account_identity = generation, account_identity
        self.clock, self.started = clock, clock()
        self.deadline = self.started + CATALOG_SECONDS
        self.lock, self.cancelled = threading.Lock(), threading.Event()
        self.cancel_reason = None
        self.connections = set()
        self.thread = None
        self.result = None
        self.error = None
        self.phase = 'queued'
        self.counts = dict.fromkeys(CATALOG_COUNTS, 0)

    def _source_documents(self):
        return self.documents

    def stop_reason(self):
        if self.cancelled.is_set():
            return self.cancel_reason or 'windows_scope_catalog_cancelled'
        if self.clock() >= self.deadline:
            return 'windows_scope_catalog_timeout'
        return None

    def check(self):
        reason = self.stop_reason()
        if reason:
            raise CatalogReadError(reason)

    def update(self, phase, **counts):
        self.check()
        if phase not in CATALOG_PHASES or any(key not in CATALOG_COUNTS or type(value) is not int
                or not 0 <= value <= CATALOG_LIMIT * 3 for key, value in counts.items()):
            raise CatalogReadError('windows_scope_catalog_unavailable')
        with self.lock:
            self.phase = phase
            self.counts.update(counts)

    def cancel(self, reason='windows_scope_catalog_cancelled'):
        with self.lock:
            if not self.cancelled.is_set():
                self.cancel_reason = reason
            self.cancelled.set()
            for connection in self.connections:
                try:
                    connection.interrupt()
                except Exception:
                    pass

    def open(self, driver, path, key):
        job = self
        class ProgressDriver:
            def connect(self, *args, **kwargs):
                job.check()
                connection = driver.connect(*args, **kwargs)
                try:
                    # Install before _open's PRAGMAs and first schema query, not
                    # after expensive first-page decryption has already happened.
                    connection.set_progress_handler(lambda: int(job.stop_reason() is not None), 1000)
                    with job.lock:
                        job.connections.add(connection)
                    job.check()
                    return connection
                except Exception:
                    with job.lock:
                        job.connections.discard(connection)
                    connection.close()
                    raise
        self.check()
        connection = _open(ProgressDriver(), path, key)
        self.check()
        return connection

    def release(self, connection):
        with self.lock:
            self.connections.discard(connection)
        connection.close()

    def finish(self):
        with self.lock:
            connections, self.connections = self.connections, set()
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass

    def snapshot(self):
        with self.lock:
            return {'schema_version': 1, 'phase': self.phase,
                    'elapsed_seconds': min(86400, max(0, int(self.clock() - self.started))),
                    'worker_active': bool(self.thread is not None and self.thread.is_alive()), **self.counts}


def load_contact_catalog(manager):
    """Read bounded source/contact metadata only; no message-body SQL."""
    import sqlcipher3.dbapi2 as driver
    import windows_keyproof_core as proof
    import windows_keyproof_platform as platform
    scope, keys = manager._source_documents()
    job = manager if isinstance(manager, CatalogRead) else CatalogRead((scope, keys), None, None)
    root, _, values, _ = validate_scope(scope, keys)
    tables = set()
    try:
        job.update('validating_sources')
        checked = 0
        for relative, value in values.items():
            job.check()
            if not proof.page_authenticates(bytes.fromhex(value), platform.read_page(root / relative)):
                raise CatalogReadError('windows_scope_key_authentication_failed')
            checked += 1
            job.update('validating_sources', databases_checked=checked)
        for relative, value in values.items():
            if not relative.startswith('message/'):
                continue
            job.update('opening_message')
            connection = job.open(driver, root / relative, value)
            try:
                job.update('reading_message_tables')
                cursor = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*' LIMIT 10001")
                total = 0
                while True:
                    job.check()
                    rows = cursor.fetchmany(128)
                    if not rows:
                        break
                    total += len(rows)
                    if total > CATALOG_LIMIT:
                        raise CatalogReadError('windows_scope_catalog_limit')
                    tables.update(row[0] for row in rows)
                    job.update('reading_message_tables', tables_seen=len(tables))
            finally:
                job.release(connection)
        job.update('opening_contacts')
        connection = job.open(driver, root / 'contact/contact.db', values['contact/contact.db'])
        rows = []
        try:
            job.update('reading_contacts')
            cursor = connection.execute('SELECT substr(username,1,257),substr(remark,1,256),substr(nick_name,1,256) FROM contact LIMIT 10001')
            while True:
                job.check()
                batch = cursor.fetchmany(128)
                if not batch:
                    break
                rows.extend(batch)
                if len(rows) > CATALOG_LIMIT:
                    raise CatalogReadError('windows_scope_catalog_limit')
                job.update('reading_contacts', contacts_seen=len(rows))
        finally:
            job.release(connection)
        result, seen = [], set()
        job.update('preparing_choices')
        for username, remark, nickname in rows:
            job.check()
            if not isinstance(username, str) or not ID.fullmatch(username) or username in seen:
                continue
            if 'Msg_' + hashlib.md5(username.encode()).hexdigest() not in tables:
                continue
            seen.add(username)
            value = next((value.strip() for value in (remark, nickname) if isinstance(value, str) and value.strip()), '未命名会话')
            name = ''.join(char for char in value if char >= ' ' and char != '\x7f')[:256] or '未命名会话'
            result.append({'chat_id': username, 'name': name,
                           'type': 'group' if username.endswith('@chatroom') else 'private'})
        job.update('complete', conversations_found=len(result))
        return result
    finally:
        job.finish()


class WindowsWebManager:
    def __init__(self, data_dir, package, output, *, pid=None, creation_time=None, read_only=False,
                 start_collector=None, stop_collector=None, catalog_loader=None, account_probe=None,
                 clock=time.monotonic, wait_for_wechat=False, process_detector=None, account_probe_factory=None):
        self.legacy_data = Path(data_dir)
        self.package, self.output = Path(package), Path(output)
        self.private = self.legacy_data / 'private'
        self.pid, self.creation_time, self.read_only = pid, creation_time, bool(read_only)
        self.wait_for_wechat = bool(wait_for_wechat)
        if self.read_only and self.wait_for_wechat:
            raise ValueError('windows_wait_and_explicit_read_only_conflict')
        self.process_detector = process_detector
        self.account_probe_factory = account_probe_factory
        self.provided_account_probe = account_probe
        self.process_state = 'unavailable'
        self.process_reason = 'windows_process_not_checked'
        self.last_process_at = float('-inf')
        self.connection_state = 'explicit_read_only' if self.read_only else 'checking_account'
        self.collection_requested = not self.read_only and not self.wait_for_wechat
        self.reader_config_ready = False
        self.start_collector, self.stop_collector = start_collector, stop_collector
        self.catalog_loader = catalog_loader or load_contact_catalog
        self.account_probe = account_probe
        self.clock, self.last_probe_at = clock, float('-inf')
        self.lock, self.commands = threading.RLock(), SimpleQueue()
        self.scope_generation, self.active_scope_id = secrets.token_hex(16), 'legacy'
        self.provider = self.assistant = self.child = self.child_output = None
        self.active_config, self.contacts, self.catalog = {'conversations': []}, [], {}
        self.catalog_ready = self.catalog_loading = self.queued = self.switching = False
        self.catalog_job, self.catalog_error = None, None
        self.catalog_progress = {"schema_version": 1, "phase": "idle", "elapsed_seconds": 0,
                                 "worker_active": False, **dict.fromkeys(CATALOG_COUNTS, 0)}
        self.closed, self.account_matched, self.account_blocked = False, bool(read_only), False
        self.account_probe_reason = 'windows_account_not_checked'
        self.account_diagnostics = {}
        self.stage, self.reason = 'starting', None
        self.description = '正在恢复已有关注范围；原消息和采集位置保留。'
        self.account_identity = None
        self.scope = self.data_dir = None
        self.children_started, self.last_child_exit, self.cleanup_confirmed = 0, None, True
        self.child_started_at = None
        self.cloud_request_attempts = self.model_request_attempts = 0
        self.runtime_lock = None

    def _check_generation(self, expected_generation):
        if expected_generation is not None and expected_generation != self.scope_generation:
            raise WebDataError('scope_changed', 409)

    def public_scope_context(self):
        with self.lock:
            return {'scope_generation': self.scope_generation, 'scope_id': self.active_scope_id,
                    'scope_switching': self.switching}

    def _source_documents(self):
        source = read_private_json(self.private / 'scope.json')
        if source_account_identity(source) != self.account_identity:
            raise ValueError('windows_scope_source_account_changed')
        return source, read_private_json(self.private / 'keys.json')

    def _profile_root(self, identifier):
        if identifier == 'legacy':
            return self.private
        if not isinstance(identifier, str) or not PROFILE.fullmatch(identifier):
            raise ValueError('windows_scope_profile_invalid')
        parent = self.private / 'profiles'
        if parent.exists() or parent.is_symlink():
            ensure_private_dir(parent)
        target = parent / identifier
        if target.exists() or target.is_symlink():
            ensure_private_dir(target)
        return target

    def _profile_data(self, identifier):
        return self.legacy_data if identifier == 'legacy' else self._profile_root(identifier) / 'collector-data'

    def _assistant_directory(self, identifier=None):
        return self._profile_root(identifier or self.active_scope_id) / 'assistant'

    def _manifest(self, identifier):
        if identifier == 'legacy':
            return {'conversations': normalize_conversations(self.scope['config']), 'display_names': ['原关注范围']}
        value = read_private_json(self._profile_root(identifier) / 'profile.json')
        chats = normalize_conversations({'conversations': value.get('conversations')})
        if (value.get('schema_version') != 1 or value.get('account_identity') != self.account_identity
                or value.get('profile_id') != identifier or scope_fingerprint(chats) != identifier):
            raise ValueError('windows_scope_profile_account_mismatch')
        return value

    def _saved_profiles(self):
        identifiers = ['legacy']
        parent = self.private / 'profiles'
        if parent.exists() or parent.is_symlink():
            ensure_private_dir(parent)
            paths = list(parent.iterdir())
            if len(paths) > 100:
                raise ValueError('windows_scope_profile_limit')
            identifiers += [path.name for path in sorted(paths) if PROFILE.fullmatch(path.name)]
        result = []
        for identifier in identifiers:
            try:
                value = self._manifest(identifier)
                chats = normalize_conversations({'conversations': value['conversations']})
                names = value.get('display_names', [])
                label = '、'.join(name[:48] for name in names[:3] if isinstance(name, str))[:140]
                result.append({'id': identifier, 'label': label or '已保存关注范围', 'count': len(chats),
                    'private_count': sum(not row['chat_id'].endswith('@chatroom') for row in chats),
                    'group_count': sum(row['chat_id'].endswith('@chatroom') for row in chats),
                    'active': identifier == self.active_scope_id})
            except Exception:
                if identifier == 'legacy':
                    raise
        return result

    def _active_pointer(self):
        path = self.private / 'active-scope.json'
        if not path.exists() and not path.is_symlink():
            return 'legacy'
        value = read_private_json(path)
        identifier = value.get('profile_id')
        if value.get('schema_version') != 1 or value.get('account_identity') != self.account_identity:
            raise ValueError('windows_scope_active_account_mismatch')
        self._manifest(identifier)
        return identifier

    def start(self):
        ensure_private_dir(self.legacy_data)
        ensure_private_dir(self.private)
        self._acquire_runtime()
        self.scope = read_private_json(self.private / 'scope.json')
        self.active_config = {'conversations': normalize_conversations(self.scope['config'])}
        self.account_identity = source_account_identity(self.scope)
        self._verify_previous_child()
        if self.wait_for_wechat:
            if self.process_detector is None:
                from windows_process_detection import WindowsProcessDetector
                self.process_detector = WindowsProcessDetector(self.scope['exe_path'])
            self.active_scope_id = self._active_pointer()
            self._tick_connection(force=True)
            return
        if not self.read_only:
            prepare_session(self.legacy_data)  # Validate only; never reset an existing checkpoint.
            self.reader_config_ready = True
            if self.account_probe is None:
                from windows_account_probe import WindowsAccountProbe
                self.account_probe = WindowsAccountProbe(self.pid, self.creation_time,
                                                         self.scope['exe_path'], self.scope['db_root'])
            if not self._verify_account(force=True):
                return
        identifier = self._active_pointer()
        self._activate(identifier, initial=True)

    def _open_saved(self, identifier):
        """Open only the user's chosen, already-saved local scope; no keys/source."""
        self._manifest(identifier)
        target = self._profile_data(identifier)
        scope = read_private_json(target / 'private/scope.json')
        if source_account_identity(scope) != self.account_identity:
            raise ValueError('windows_scope_source_account_mismatch')
        if identifier != 'legacy' and scope_fingerprint(scope['config']['conversations']) != identifier:
            raise ValueError('windows_scope_profile_config_mismatch')
        provider = CollectorWebData(target)
        provider.status()
        self.active_scope_id, self.data_dir = identifier, target
        self.active_config = {'conversations': normalize_conversations(scope['config'])}
        self.provider = provider
        self._refresh_selected()

    def _disconnect_waiting(self, state, reason, description, *, allow_saved=False):
        changed = self.connection_state != state or self.child is not None or self.account_matched
        if changed:
            self.scope_generation = secrets.token_hex(16)
            self.switching = True
            self._stop_active()
            self.contacts, self.catalog = [], {}
            self._cancel_catalog(reset=True)
            self.catalog_ready = self.catalog_loading = False
            self.commands, self.queued = SimpleQueue(), False
        self.account_matched = False
        self.reader_config_ready = False
        self.account_blocked = not allow_saved
        self.stage, self.reason = 'needs_connection', reason
        self.connection_state, self.description = state, description
        self.switching = not self.cleanup_confirmed
        if allow_saved and self.provider is None and self.cleanup_confirmed:
            self._open_saved(self.active_scope_id)

    def _tick_connection(self, *, force=False):
        """Process metadata is a candidate; handles establish the data binding."""
        if not self.wait_for_wechat or self.read_only:
            return self.account_matched
        if not force and self.clock() - self.last_process_at < 3:
            return self.account_matched
        self.last_process_at = self.clock()
        try:
            result = self.process_detector()
            state = result.get('state') if isinstance(result, dict) else None
            pid = result.get('pid') if isinstance(result, dict) else None
            birth = result.get('creation_time') if isinstance(result, dict) else None
            if state not in ('ready', 'not_running', 'ambiguous', 'unavailable'):
                raise ValueError
            if state == 'ready' and (type(pid) is not int or not 0 < pid <= 0xffffffff
                                     or type(birth) is not int or not 0 < birth < 2**64):
                raise ValueError
            reason = result.get('reason')
            self.process_reason = reason if reason in {
                'windows_process_not_running', 'windows_process_candidate_ready',
                'windows_process_candidates_ambiguous', 'windows_process_detection_unavailable',
                'windows_process_executable_unavailable', 'windows_process_version_unsupported',
                'windows_process_architecture_unsupported', 'windows_process_path_unavailable',
                'windows_process_access_unavailable', 'windows_process_version_unavailable',
                'windows_process_multiple_candidates', 'windows_process_snapshot_unavailable',
                'windows_process_snapshot_deadline'} else (
                    'windows_process_not_running' if state == 'not_running' else
                    'windows_process_candidates_ambiguous' if state == 'ambiguous' else
                    'windows_process_candidate_ready' if state == 'ready' else 'windows_process_detection_unavailable')
        except Exception:
            state, pid, birth = 'unavailable', None, None
            self.process_reason = 'windows_process_detection_unavailable'
        self.process_state = state
        if state != 'ready':
            self.pid = self.creation_time = None
            self.account_probe = self.provided_account_probe
            self.account_probe_reason = 'windows_account_not_checked'
            self.account_diagnostics = {}
            if state == 'not_running':
                self._disconnect_waiting('waiting_for_wechat', self.process_reason,
                    '尚未检测到原路径的微信。请打开微信并完成登录，再点击重新检测；也可以只浏览本机已保存消息。', allow_saved=True)
            else:
                messages = {
                    'windows_process_path_unavailable': '原微信程序路径暂不可访问。请确认程序未被移动，恢复原程序后重新检测。',
                    'windows_process_access_unavailable': '微信程序的只读身份信息暂不可访问。请保持原微信运行并检查当前用户权限后重新检测。',
                    'windows_process_version_unavailable': '暂时无法核对微信版本。请保持原微信运行，稍后重新检测。',
                    'windows_process_version_unsupported': '检测到的微信版本尚不在本包验证范围内。当前不会采集，请使用此前已验证的版本或反馈此固定状态。',
                    'windows_process_architecture_unsupported': '微信程序架构与此 x64 测试包不符，当前不会启动采集。',
                    'windows_process_snapshot_deadline': '本次程序信息检测超时，尚未确认微信状态。请稍后重新检测。',
                    'windows_process_snapshot_unavailable': '暂时无法获取程序元信息，尚未确认微信状态。请稍后重新检测。'}
                self._disconnect_waiting('account_unverified', self.process_reason,
                    messages.get(self.process_reason, '暂时不能唯一确认微信程序。请只保留原账号对应的微信，完成登录后重新检测。'))
            return False
        if (pid, birth) != (self.pid, self.creation_time):
            self._disconnect_waiting('checking_account', None, '已找到原路径的微信程序，正在核对当前打开的账号资料。')
            self.pid, self.creation_time = pid, birth
            self.account_probe = None
            self.account_diagnostics = {}
        if self.account_probe is None:
            factory = self.account_probe_factory
            if factory is None and self.provided_account_probe is not None:
                self.account_probe = self.provided_account_probe
            else:
                if factory is None:
                    from windows_account_probe import WindowsAccountProbe
                    factory = WindowsAccountProbe
                self.account_probe = factory(pid, birth, self.scope['exe_path'], self.scope['db_root'])
        if not self._verify_account(force=True):
            self.connection_state = ('wrong_account' if self.account_probe_reason == 'windows_account_changed_or_ambiguous'
                                     else 'account_unverified')
            self.stage = 'needs_connection'
            self.description = ('当前活动微信资料与所选本机资料不一致，已隔离旧范围。请登录原账号，或关闭入口后选择另一账号自己的资料目录。'
                if self.connection_state == 'wrong_account' else
                '已找到微信程序，当前账号读取尚未核验。请保持原账号微信运行并点击重新检测；已登录时仍可能因程序绑定或句柄访问条件而暂未取得核验依据。原数据和配置保持不变。')
            return False
        if self.account_blocked:
            # Returning to this account creates a fresh domain as well. A page
            # opened while the account was unverified cannot later read it.
            self.scope_generation = secrets.token_hex(16)
            self.contacts, self.catalog = [], {}
            self._cancel_catalog(reset=True)
            self.catalog_ready = self.catalog_loading = False
            self.commands = SimpleQueue()
        self.account_blocked = False
        self.switching = False
        self.connection_state, self.reason = 'connected', None
        if not self.reader_config_ready:
            try:
                prepare_session(self._profile_data(self.active_scope_id))
                self.reader_config_ready = True
            except Exception:
                self.connection_state, self.stage = 'account_unverified', 'needs_connection'
                self.reason = 'windows_reader_configuration_unavailable'
                self.description = '原账号读取已核验，但已有读取配置未能通过检查。原数据未改动，请确认选择的是此前测试成功的完整 collector-data。'
                return False
        if self.provider is None:
            self._open_saved(self.active_scope_id)
        if self.collection_requested and self.child is None:
            self._activate(self.active_scope_id)
        elif self.child is None:
            self.stage = 'ready_to_start'
            self.description = '原账号读取已核验。点击开始采集，将继续原关注范围和已保存的采集位置。'
        elif self.child.poll() is not None:
            self.collection_requested = False
            self.connection_state, self.stage = 'stopped', 'ready_to_start'
            self.description = '本轮采集已经结束，已保存消息仍可查看。可以重新检测后点击开始采集。'
        return True

    def _acquire_runtime(self):
        path = self.private / 'web-manager-lock.json'
        if not path.exists() and not path.is_symlink():
            try:
                write_private_json_new(path, {'schema_version': 1, 'kind': 'local_scope_owner_lock'})
            except Exception:
                if not path.exists():
                    raise
        read_private_json(path)
        descriptor = os.open(path, os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
        try:
            if os.name == 'nt':
                import msvcrt
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.runtime_lock = descriptor
        except BaseException:
            os.close(descriptor)
            raise ValueError('windows_scope_runtime_already_running') from None

    def _verify_previous_child(self):
        path = self.private / 'owned-collector.json'
        if not path.exists() and not path.is_symlink():
            return
        value = read_private_json(path)
        if value.get('schema_version') != 1 or value.get('account_identity') != self.account_identity:
            raise ValueError('windows_scope_owned_collector_binding_invalid')
        if value.get('state') == 'closed':
            return
        if value.get('state') not in ('starting', 'running') or not isinstance(value.get('output'), str):
            raise ValueError('windows_scope_owned_collector_state_invalid')
        report = validate_local_path(Path(value['output']) / 'supervisor.json')
        if report.stat().st_size > 16384:
            raise ValueError('windows_scope_previous_collector_not_confirmed')
        document = json.loads(report.read_bytes())
        if (document.get('schema_version') != 1 or document.get('kind') != 'chatlens_collector_supervisor'
                or document.get('child_cleanup_confirmed') is not True
                or type(document.get('child_exit_code')) is not int):
            raise ValueError('windows_scope_previous_collector_not_confirmed')
        self._record_child('closed', Path(value['output']))

    def _record_child(self, state, output):
        atomic_private(self.private / 'owned-collector.json', {'schema_version': 1,
            'account_identity': self.account_identity, 'state': state, 'output': str(output)})

    def _verify_account(self, *, force=False):
        if self.read_only:
            return False
        if not force and self.clock() - self.last_probe_at < 3:
            return self.account_matched
        self.last_probe_at = self.clock()
        try:
            result = self.account_probe()
            matched = isinstance(result, dict) and result.get('state') == 'matched' and result.get('matched') is True
            reason = result.get('reason') if isinstance(result, dict) else None
        except Exception:
            matched = False
            reason = None
            result = None
        from windows_account_probe import sanitize_diagnostics
        self.account_diagnostics = sanitize_diagnostics(result.get('diagnostics') if isinstance(result, dict) else None)
        public_reasons = {'windows_account_root_invalid', 'windows_account_handle_invalid',
            'windows_account_device_unresolved', 'windows_account_open_database_unavailable',
            'windows_account_changed_or_ambiguous', 'windows_account_open_database_matched',
            'windows_account_mapped_database_matched', 'windows_account_mapped_database_unavailable',
            'windows_account_mapped_query_unavailable',
            'windows_account_mapped_api_unavailable', 'windows_account_mapped_region_unavailable',
            'windows_account_mapped_name_unavailable', 'windows_account_mapped_budget_exceeded',
            'windows_account_mapped_process_changed',
            'windows_account_probe_unavailable'}
        self.account_probe_reason = reason if reason in public_reasons else 'windows_account_probe_unavailable'
        self.account_matched = matched
        if not matched:
            if not self.account_blocked:
                self.scope_generation = secrets.token_hex(16)
                self.account_blocked, self.switching = True, True
                self._cancel_catalog(reset=True)
                self.catalog_ready = self.catalog_loading = self.queued = False
                self.contacts, self.catalog, self.commands = [], {}, SimpleQueue()
                self._stop_active()
            self.stage, self.reason = 'error', 'windows_active_account_not_verified'
            if self.wait_for_wechat:
                self.connection_state = ('wrong_account' if self.account_probe_reason == 'windows_account_changed_or_ambiguous'
                                         else 'account_unverified')
            self.description = ('当前活动微信资料与所选资料不一致，已停止采集并隔离旧页面。请切回原账号，或关闭后选择另一账号自己的资料目录。'
                if self.account_probe_reason == 'windows_account_changed_or_ambiguous' else
                '当前账号读取尚未核验，已停止采集并保留原数据。请保持原账号微信运行后重新检测；已登录时也可能暂时无法取得可靠的账号打开状态。')
            return False
        return True

    def _copy_model_config(self, source, target):
        if source == target or not (source / 'provider.json').exists():
            return
        ensure_private_dir(source)
        ensure_private_dir(target.parent, create=True)
        ensure_private_dir(target, create=True)
        destination = target / 'provider.json'
        if not destination.exists() and not destination.is_symlink():
            write_private_json_new(destination, read_private_json(source / 'provider.json'))

    def _identity_hint(self):
        if self.provider is None:
            return None
        try:
            from collector_web_meta import account_context
            from wechat_identity import trusted_self_identity
            catalog = self.provider._metadata('catalog.json')
            with self.provider._open() as (_, snapshot):
                binding = snapshot['binding_digest']
            if trusted_self_identity(catalog, binding):
                return {'catalog': catalog, 'binding_digest': binding,
                        'account_context': account_context(self.scope['db_root'])}
        except Exception:
            pass
        return None

    def _stop_active(self):
        assistant, child, output = self.assistant, self.child, self.child_output
        self.assistant = self.provider = None
        try:
            if assistant is not None:
                assistant.close()
                self.cloud_request_attempts += assistant.cloud_request_attempts
                self.model_request_attempts += assistant.model_request_attempts
        finally:
            if child is not None:
                try:
                    code, clean = self.stop_collector(child, output)
                except BaseException:
                    self.cleanup_confirmed = False
                    self.switching = True
                    raise
                self.last_child_exit = code
                self.cleanup_confirmed = clean
                if not clean:
                    self.switching = True
                    raise ValueError('windows_scope_collector_stop_not_confirmed')
                self.child = self.child_output = None
                self._record_child('closed', output)

    def _activate(self, identifier, chats=None, names=None, *, initial=False):
        if not self.read_only and not self._verify_account(force=True):
            return
        if chats is not None and scope_fingerprint(chats) == scope_fingerprint(self.scope['config']['conversations']):
            identifier = 'legacy'
        source_assistant = self._assistant_directory()
        hint = self._identity_hint()
        self._stop_active()
        root = self._profile_root(identifier)
        target = self._profile_data(identifier)
        if identifier != 'legacy':
            ensure_private_dir(root.parent, create=True)
            ensure_private_dir(root, create=True)
            if not (root / 'profile.json').exists() and chats is not None:
                if self.read_only:
                    raise ValueError('windows_scope_read_only')
                source, keys = self._source_documents()
                dynamic = {'schema_version': 1, 'scope_kind': 'selected_conversations',
                           'session_id': secrets.token_hex(16), 'db_root': source['db_root'],
                           'exe_path': source['exe_path'], 'config': {'conversations': normalize_conversations({'conversations': chats})},
                           'test_tokens': {}}
                validate_scope(dynamic, keys)
                ensure_private_dir(target, create=True)
                private = ensure_private_dir(target / 'private', create=True)
                ensure_private_dir(private / 'store', create=True)
                write_private_json_new(private / 'scope.json', dynamic)
                write_private_json_new(private / 'keys.json', keys)
                if hint is not None:
                    write_private_json_new(private / 'identity-hint.json', hint)
                write_private_json_new(root / 'profile.json', {'schema_version': 1, 'account_identity': self.account_identity,
                    'profile_id': identifier, 'conversations': dynamic['config']['conversations'],
                    'display_names': list(names or []), 'created_at_utc': datetime.now(timezone.utc).isoformat()})
            self._manifest(identifier)
            if not self.read_only:
                # Only a first same-account configuration copy. History is never copied.
                self._copy_model_config(source_assistant, root / 'assistant')
        scope = read_private_json(target / 'private/scope.json')
        if source_account_identity(scope) != self.account_identity:
            raise ValueError('windows_scope_source_account_mismatch')
        if identifier != 'legacy' and scope_fingerprint(scope['config']['conversations']) != identifier:
            raise ValueError('windows_scope_profile_config_mismatch')
        if not self.read_only:
            prepare_session(target)
            self.reader_config_ready = True
        self.active_scope_id, self.data_dir = identifier, target
        self.active_config = {'conversations': normalize_conversations(scope['config'])}
        self.switching, self.queued, self.account_blocked = False, False, False
        self.reason = None
        self._refresh_selected()
        if not self.read_only:
            child_output = self.output / 'collector-run'
            if not initial or child_output.exists() or child_output.is_symlink():
                child_output = self.output / ('collector-run-' + secrets.token_hex(8))
            # Persist intent before Popen: a crash between process creation and
            # assigning its handle must not lose ownership of a live collector.
            self._record_child('starting', child_output)
            self.cleanup_confirmed = False
            self.child = self.start_collector(target, child_output)
            self.child_output = child_output
            self.child_started_at = self.clock()
            self.cleanup_confirmed = True
            self.last_child_exit = None
            self.children_started += 1
            self._record_child('running', child_output)
            atomic_private(self.private / 'active-scope.json', {'schema_version': 1,
                           'account_identity': self.account_identity, 'profile_id': identifier})
        self.stage = 'starting'
        self.description = '正在恢复原采集位置。' if initial or chats is None else '正在建立本次所选范围的新消息起点。'
        self._refresh_provider()

    def _refresh_provider(self):
        if self.account_blocked or self.data_dir is None:
            return
        try:
            if self.provider is None:
                candidate = CollectorWebData(self.data_dir)
                candidate.status()
                self.provider = candidate
            self.stage = 'running'
            self.description = '已有消息保存在本机；当前关注范围独立保存。'
        except WebDataError:
            if self.read_only or self.child is not None and self.child.poll() is not None:
                self.stage, self.reason = 'error', 'windows_scope_store_not_ready'

    def _refresh_selected(self):
        selected = {row['chat_id'] for row in self.active_config['conversations']}
        for row in self.contacts:
            row['selected'] = self.catalog.get(row['id'], {}).get('chat_id') in selected

    def _owned_collector_status(self):
        """Only this run's heartbeat can establish actual reader readiness."""
        child = self.child
        if child is None or child.poll() is not None:
            code = child.poll() if child is not None else self.last_child_exit
            return {'collector_running': False, 'collector_stage': 'error' if code not in (None, 0) else 'stopped',
                    'heartbeat_at': None, 'reason': 'collector_child_failed' if code not in (None, 0) else 'collector_not_started'}
        from collector_supervisor import read_heartbeat
        heartbeat = read_heartbeat(self.child_output / 'status.json') if self.child_output is not None else None
        if heartbeat is None:
            age = self.clock() - self.child_started_at if self.child_started_at is not None else 0
            stage = 'stale' if age > 15 else 'starting'
            return {'collector_running': True, 'collector_stage': stage, 'heartbeat_at': None,
                    'reason': 'collector_heartbeat_stale' if stage == 'stale' else 'collector_heartbeat_pending'}
        try:
            recorded = datetime.fromisoformat(heartbeat['recorded_at_utc'])
            if recorded.tzinfo is None or recorded.utcoffset() is None:
                raise ValueError
            age = (datetime.now(timezone.utc) - recorded.astimezone(timezone.utc)).total_seconds()
            if not -5 <= age <= 15:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            return {'collector_running': True, 'collector_stage': 'stale', 'heartbeat_at': None,
                    'reason': 'collector_heartbeat_stale'}
        return {'collector_running': True, 'collector_stage': heartbeat['stage'],
                'heartbeat_at': recorded.astimezone(timezone.utc).isoformat(), 'reason': None}

    def setup(self):
        with self.lock:
            profiles = self._saved_profiles() if self.scope and not self.account_blocked else []
            detected = not self.account_blocked
            owned = self._owned_collector_status() if self.wait_for_wechat else None
            collector_running = bool(self.child is not None and self.child.poll() is None)
            collecting = bool(not self.closed and self.account_matched and collector_running
                              and (owned is None or owned['collector_stage'] == 'collecting'))
            can_manage = bool(not self.closed and not self.read_only and self.account_matched and not self.account_blocked
                              and self.reader_config_ready and self.cleanup_confirmed and not self.switching and not self.queued)
            can_start = bool(can_manage and not collector_running)
            can_recheck = bool(not self.closed and not self.read_only and self.cleanup_confirmed and not self.queued)
            connection_state = ('cleanup_required' if not self.cleanup_confirmed else
                                'explicit_read_only' if self.read_only else
                                self.connection_state if self.wait_for_wechat else
                                'wrong_account' if self.account_blocked and self.account_probe_reason == 'windows_account_changed_or_ambiguous' else
                                'account_unverified' if self.account_blocked else
                                'stopped' if self.child is not None and self.child.poll() is not None else 'connected')
            connection = {'state': connection_state,
                'reason': self.account_probe_reason if self.account_blocked else self.reason,
                'description': ('当前为显式离线只读模式，不检测微信或启动采集。需要连接时请关闭此入口并以连接模式启动。'
                                if self.read_only else self.description),
                'actions': (['recheck'] if can_recheck else []) + (['start'] if can_start else []),
                'can_recheck': can_recheck, 'can_start': can_start, 'can_manage': can_manage,
                'can_read_saved': bool(self.provider is not None and not self.account_blocked and not self.switching),
                'can_modify_scope': can_manage,
                'checks': {'process_running': (self.process_state == 'ready' if self.wait_for_wechat else
                                               bool(self.pid and self.creation_time) if not self.read_only else None),
                    'account_verified': bool(self.account_matched and not self.read_only and not self.account_blocked),
                    'reader_ready': bool(self.reader_config_ready and self.account_matched and not self.read_only and not self.account_blocked),
                    'scope_selected': bool(self.active_config.get('conversations')), 'collecting': collecting}}
            if owned is not None:
                connection.update(collector_running=owned['collector_running'], collector_stage=owned['collector_stage'])
                if owned['collector_running']:
                    descriptions = {
                        'starting': '账号读取已核验，采集进程正在初始化；收到当前运行的就绪心跳后才会显示采集就绪。',
                        'paused': '采集进程暂时暂停，已保存的采集位置保持。请让微信回到后台并解锁屏幕，或查看采集状态等待恢复。',
                        'stale': '当前采集心跳未及时更新，暂不能确认仍在接收新消息。原消息和采集位置保持，请查看采集状态。',
                        'stopping': '本轮采集正在正常停止，正在保存位置；请等待结束。',
                        'stopped': '本轮采集已经保存位置，正在等待监督进程退出；请稍候。',
                        'error': '本轮采集报告错误，原消息和采集位置保持。请等待进程结束后重新检测。'}
                    if owned['collector_stage'] in descriptions:
                        connection['description'] = descriptions[owned['collector_stage']]
            return {'stage': self.stage, 'reason': self.reason, 'platform': 'windows', 'read_only': self.read_only,
                'connection': connection,
                'account': {'detected': self.account_matched if self.wait_for_wechat else detected, 'display_name': None},
                'checks': {'wechat_running': self.process_state == 'ready' if self.wait_for_wechat else self.account_matched and not self.read_only,
                           'access_ready': detected, 'reader_ready': detected and not self.read_only and self.reader_config_ready},
                'progress': {'phase': 'baseline' if self.stage == 'starting' else None, 'completed': None, 'total': None, 'elapsed_seconds': 0},
                'initialization': {'state': 'working' if self.queued or self.stage == 'starting' else 'idle',
                                   'action': 'none', 'description': self.description},
                'scope_limits': {'total': MAX_CONVERSATIONS, 'private': MAX_CONVERSATIONS, 'group': MAX_CONVERSATIONS},
                **self.public_scope_context(), 'catalog': self._catalog_status(),
                'active_scope': next((row for row in profiles if row['active']), None),
                'profiles': profiles, 'workspace_path': '/'}

    def connection_diagnostics(self, expected_generation=None):
        """Export cached fixed codes/counts only, never process or account identity."""
        with self.lock:
            self._check_generation(expected_generation)
            from windows_account_probe import sanitize_diagnostics
            connection = self.setup()['connection']
            state = connection['state']
            states = {'cleanup_required', 'explicit_read_only', 'waiting_for_wechat', 'checking_account',
                      'wrong_account', 'account_unverified', 'stopped', 'connected'}
            reasons = {'windows_account_not_checked', 'windows_account_root_invalid', 'windows_account_handle_invalid',
                       'windows_account_device_unresolved', 'windows_account_open_database_unavailable',
                       'windows_account_changed_or_ambiguous', 'windows_account_open_database_matched',
                       'windows_account_mapped_database_matched', 'windows_account_mapped_database_unavailable',
                       'windows_account_mapped_query_unavailable',
                       'windows_account_mapped_api_unavailable', 'windows_account_mapped_region_unavailable',
                       'windows_account_mapped_name_unavailable', 'windows_account_mapped_budget_exceeded',
                       'windows_account_mapped_process_changed',
                       'windows_account_probe_unavailable'}
            stage = connection.get('collector_stage')
            return {'schema_version': 1, 'kind': 'chatlens_windows_connection_diagnostics', 'platform': 'windows',
                'connection_state': state if state in states else 'account_unverified',
                'account_reason': self.account_probe_reason if self.account_probe_reason in reasons else 'windows_account_probe_unavailable',
                'checks': {key: connection['checks'][key] for key in
                           ('process_running', 'account_verified', 'reader_ready', 'scope_selected', 'collecting')},
                'account_diagnostics': sanitize_diagnostics(self.account_diagnostics),
                'selected_conversation_count': min(MAX_CONVERSATIONS, len(self.active_config.get('conversations', []))),
                'collector_running': bool(connection.get('collector_running', self.child is not None and self.child.poll() is None)),
                'collector_stage': stage if stage in {'starting', 'collecting', 'paused', 'stale', 'stopping', 'stopped', 'error'} else None,
                'catalog': self._catalog_status(description=False)}

    def request_recheck(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self.read_only or self.closed or not self.cleanup_confirmed:
                raise ValueError('windows_connection_recheck_unavailable')
            if not self.queued:
                self.queued = True
                self.connection_state = 'checking_account'
                self.description = '正在重新检测微信程序和原账号读取依据，检测完成后会显示下一步。'
                self.commands.put(('recheck', None))
            return self.setup()

    def request_start(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if (self.read_only or self.closed or not self.cleanup_confirmed or self.queued or self.switching
                    or not self.account_matched or self.account_blocked or not self.reader_config_ready
                    or self.wait_for_wechat and self.child is not None and self.child.poll() is None):
                raise ValueError('windows_connection_start_unavailable')
            self._begin_switch()
            self.connection_state = 'checking_account'
            self.description = '正在再次核验账号并恢复原采集位置；不会重新建立旧消息基线。'
            self.commands.put(('start', None))
            return self.setup()

    def request_initialize(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            raise ValueError('windows_existing_reader_configuration_required')

    def _catalog_status(self, *, description=True):
        job = self.catalog_job
        domain_valid = (job is not None and job.generation == self.scope_generation
                        and job.account_identity == self.account_identity
                        and not self.closed and not self.account_blocked and self.account_matched)
        progress = job.snapshot() if domain_valid else dict(self.catalog_progress)
        if job is not None and not domain_valid:
            progress['worker_active'] = bool(job.thread is not None and job.thread.is_alive())
        reason = self.catalog_error if self.catalog_error in CATALOG_REASONS else None
        state = 'error' if reason else 'loading' if self.catalog_loading else 'ready' if self.catalog_ready else 'idle'
        result = {**progress, 'state': state, 'reason': reason}
        if description:
            result['description'] = ({
                'windows_scope_catalog_timeout': '会话目录读取超过30秒预算。原采集继续保留；请在当前读取结束后重试。',
                'windows_scope_catalog_cancelled': '会话目录读取已取消，原消息和关注范围保留。',
                'windows_scope_catalog_limit': '会话元信息超出本轮读取上限，原关注范围保留。',
                'windows_scope_key_authentication_failed': '会话目录读取密钥校验未通过，原数据和配置保留。',
                'windows_scope_catalog_source_changed': '读取期间账号或资料依据发生变化，本次目录结果已丢弃。',
                'windows_scope_catalog_unavailable': '会话目录读取未完成，原采集和关注范围保留。可重新读取。',
            }.get(reason) if reason else
                '正在准备会话目录，原关注范围继续采集。' if state == 'loading' else
                '会话目录已就绪，可以搜索和选择。' if state == 'ready' else
                '上一次会话目录读取正在结束，请稍候再读取。' if progress['worker_active'] else
                '点击读取会话目录后，再选择需要关注的会话。')
        return result

    def _cancel_catalog(self, *, reset=False):
        if self.catalog_job is not None:
            self.catalog_job.cancel()
            if self.catalog_job.thread is None or not self.catalog_job.thread.is_alive():
                self.catalog_job = None
        self.catalog_loading = False
        if reset:
            self.catalog_ready, self.catalog_error = False, None
            self.catalog_progress = {'schema_version': 1, 'phase': 'idle', 'elapsed_seconds': 0,
                                     'worker_active': False, **dict.fromkeys(CATALOG_COUNTS, 0)}

    def request_catalog(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if (self.closed or self.read_only or self.switching or self.queued or not self.cleanup_confirmed
                    or self.account_blocked or not self.account_matched or not self.reader_config_ready):
                raise ValueError('windows_scope_catalog_not_ready')
            # Do not create replacement workers while a cancelled/slow read is
            # still unwinding, and do not invalidate a completed catalog for a
            # repeated click on the same ready picker.
            if self.catalog_job is not None and (self.catalog_job.thread is None or self.catalog_job.thread.is_alive()):
                return self.setup()
            if not self._verify_account(force=True):
                raise ValueError('windows_scope_catalog_not_ready')
            if self.catalog_ready and self.catalog_error is None:
                return self.setup()
            try:
                documents = self._source_documents()
            except Exception:
                self.catalog_error = 'windows_scope_catalog_source_changed'
                return self.setup()
            self.catalog_ready, self.catalog_loading, self.catalog_error = False, True, None
            self.catalog_job = CatalogRead(documents, self.scope_generation, self.account_identity)
            self.commands.put(('catalog', self.catalog_job))
            return self.setup()

    def _start_catalog(self, job):
        if job is not self.catalog_job or job.cancelled.is_set() or job.thread is not None:
            return
        loader = self.catalog_loader
        def run():
            try:
                rows = loader(job)
                job.check()
                if not isinstance(rows, list) or len(rows) > CATALOG_LIMIT:
                    raise CatalogReadError('windows_scope_catalog_limit')
                job.update('complete', conversations_found=len(rows))
                job.result = rows
            except Exception as problem:
                reason = job.stop_reason()
                if reason is None and isinstance(problem, CatalogReadError):
                    reason = str(problem)
                job.error = reason if reason in CATALOG_REASONS else 'windows_scope_catalog_unavailable'
            finally:
                job.finish()
        job.thread = threading.Thread(target=run, name='chatlens-contact-catalog', daemon=True)
        job.thread.start()

    def _poll_catalog(self):
        job = self.catalog_job
        if job is None or job.thread is None:
            return
        domain_valid = (not self.closed and job.generation == self.scope_generation
                        and job.account_identity == self.account_identity
                        and not self.account_blocked and self.account_matched and not self.switching)
        if job.thread.is_alive():
            if not domain_valid:
                job.cancel()
            elif job.stop_reason():
                job.cancel(job.stop_reason())
                self.catalog_error = job.stop_reason()
                self.catalog_loading = self.catalog_ready = False
            return
        self.catalog_job = None
        if not domain_valid:
            return
        self.catalog_progress = job.snapshot()
        self.catalog_progress['worker_active'] = False
        self.catalog_loading = False
        reason = job.error or job.stop_reason()
        if reason:
            self.catalog_ready = False
            self.catalog_error = reason if reason in CATALOG_REASONS else 'windows_scope_catalog_unavailable'
            return
        if not self._verify_account(force=True) or job.generation != self.scope_generation:
            return
        try:
            if self._source_documents() != job.documents:
                raise CatalogReadError('windows_scope_catalog_source_changed')
            self._load_contacts(job.result)
            self.catalog_error = None
        except Exception as problem:
            self.catalog_ready = False
            reason = str(problem) if isinstance(problem, CatalogReadError) else None
            self.catalog_error = reason if reason in CATALOG_REASONS else 'windows_scope_catalog_unavailable'

    def list_conversations(self, q='', *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if not isinstance(q, str) or len(q) > 256 or '\0' in q:
                raise ValueError('windows_scope_query_invalid')
            rows = [dict(row) for row in self.contacts if q.casefold() in row['name'].casefold()] if self.catalog_ready else []
            if not q:
                rows.sort(key=lambda row: (not row['selected'], row['type'], row['name'].casefold()))
            return {'conversations': rows[:200], 'has_more': len(rows) > 200, 'catalog': self._catalog_status()}

    def _begin_switch(self):
        if self.catalog_job is not None:
            self._cancel_catalog(reset=True)
        self.scope_generation = secrets.token_hex(16)
        self.switching = self.queued = True
        self.stage, self.reason = 'starting', None
        self.description = '正在切换独立关注范围，原消息和采集进度保留。'

    def select_many(self, identifiers, *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if (self.read_only or self.queued or self.switching or not self.catalog_ready or not self.cleanup_confirmed
                    or not self._verify_account(force=True)):
                raise ValueError('windows_scope_selection_not_ready')
            if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= MAX_CONVERSATIONS
                    or any(not isinstance(value, str) or value not in self.catalog for value in identifiers)
                    or len(set(identifiers)) != len(identifiers)):
                raise ValueError('windows_scope_selection_invalid')
            chats = [{'alias': alias_for(self.catalog[value]['chat_id']), 'chat_id': self.catalog[value]['chat_id']} for value in identifiers]
            identifier = scope_fingerprint(chats)
            profiles = self._saved_profiles()
            if len(profiles) >= 100 and not any(row['id'] == identifier for row in profiles):
                raise ValueError('windows_scope_profile_limit')
            names = {row['id']: row['name'] for row in self.contacts}
            self._begin_switch()
            self.commands.put(('select', (identifier, chats, [names[value] for value in identifiers])))
            return self.setup()

    def select(self, private_id, group_id, *, expected_generation=None):
        return self.select_many([private_id, group_id], expected_generation=expected_generation)

    def restore_profile(self, identifier, *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self.queued or self.switching or not self.cleanup_confirmed or not any(row['id'] == identifier for row in self._saved_profiles()):
                raise ValueError('windows_scope_profile_not_ready')
            if self.wait_for_wechat and (self.account_blocked or not self.account_matched or not self.reader_config_ready):
                raise ValueError('windows_scope_account_not_ready')
            if not self.read_only and not self._verify_account(force=True):
                raise ValueError('windows_scope_account_not_ready')
            self._begin_switch()
            self.commands.put(('profile', identifier))
            return self.setup()

    def _load_contacts(self, rows):
        old = {row['chat_id']: token for token, row in self.catalog.items()}
        contacts, catalog = [], {}
        selected = {row['chat_id'] for row in self.active_config['conversations']}
        for row in rows:
            value = row.get('chat_id')
            if not isinstance(value, str) or not ID.fullmatch(value):
                continue
            name = row.get('name')
            if not isinstance(name, str) or not name.strip():
                name = '未命名会话'
            token = old.get(value) or secrets.token_urlsafe(18)
            kind = 'group' if value.endswith('@chatroom') else 'private'
            catalog[token] = {'chat_id': value, 'type': kind}
            contacts.append({'id': token, 'name': name[:256], 'type': kind, 'selected': value in selected})
        self.contacts, self.catalog = contacts, catalog
        self.catalog_ready, self.catalog_loading = True, False

    def tick(self):
        with self.lock:
            if self.closed:
                return
            if not self.cleanup_confirmed:
                self.stage, self.reason = 'error', 'windows_scope_collector_stop_not_confirmed'
                return
            try:
                try:
                    command, value = self.commands.get_nowait()
                except Empty:
                    command = None
                if self.wait_for_wechat:
                    connected = self._tick_connection(force=command is not None)
                    if command == 'recheck':
                        self.queued = False
                        return
                    if command == 'start':
                        self.queued = False
                        if connected:
                            self.collection_requested = True
                            self._activate(self.active_scope_id)
                        return
                    if not connected:
                        return
                elif not self.read_only:
                    if not self._verify_account(force=self.queued):
                        return
                    if self.account_blocked:
                        self.scope_generation = secrets.token_hex(16)
                        self._activate(self._active_pointer())
                        return
                if command == 'recheck':
                    self.queued = False
                    if self.account_matched:
                        self.description = '原账号已核验，可以继续当前范围或开始采集。'
                    return
                if command == 'start':
                    self.queued = False
                    self.collection_requested = True
                    self._activate(self.active_scope_id)
                    return
                if command == 'catalog':
                    self._start_catalog(value)
                elif command == 'select':
                    self.collection_requested = True
                    self._activate(value[0], value[1], value[2])
                elif command == 'profile':
                    self._activate(value)
                self._poll_catalog()
                self._refresh_provider()
            except Exception:
                try:
                    self._stop_active()
                except Exception:
                    self.cleanup_confirmed = False
                self.stage, self.reason = 'error', 'windows_scope_operation_failed'
                self.switching = not self.cleanup_confirmed
                self.queued = self.catalog_loading = False
                self.description = '操作未完成，原数据和配置已保留。可以重新启动后恢复原范围。'

    def _current_provider(self, expected_generation=None):
        self._check_generation(expected_generation)
        if self.closed or self.switching or self.account_blocked or self.provider is None:
            raise WebDataError('web_data_unavailable', 503)
        return self.provider

    def conversations(self, *, expected_generation=None):
        with self.lock:
            return self._current_provider(expected_generation).conversations()

    def messages(self, *args, expected_generation=None, **kwargs):
        with self.lock:
            return self._current_provider(expected_generation).messages(*args, **kwargs)

    def status(self, *, expected_generation=None):
        with self.lock:
            result = self._current_provider(expected_generation).status()
            if self.read_only:
                result.update(state='stopped', reason='web_read_only_mode', stale=False)
            elif self.wait_for_wechat and (self.child is None or not self.account_matched):
                result.update(state='stopped', reason=self.reason or 'collector_not_started', stale=False)
            elif self.wait_for_wechat:
                owned = self._owned_collector_status()
                result.update(state=owned['collector_stage'], reason=owned['reason'],
                              heartbeat_at=owned['heartbeat_at'], stale=owned['collector_stage'] == 'stale')
            elif self.child is not None and self.child.poll() is not None:
                code = self.child.poll()
                result.update(state='stopped' if code == 0 else 'error',
                              reason='collector_stopped' if code == 0 else 'collector_child_failed', stale=False)
            return result

    def get_ai_service(self, *, expected_generation=None):
        with self.lock:
            self._current_provider(expected_generation)
            if self.assistant is None:
                import sys
                parent = Path(__file__).resolve().parent.parent
                if (parent / 'ai_service.py').is_file() and str(parent) not in sys.path:
                    sys.path.insert(0, str(parent))
                from ai_service import AiAssistant
                self.assistant = AiAssistant(self.data_dir, self._assistant_directory())
            return self.assistant

    def close(self):
        with self.lock:
            if self.closed and self.child is None:
                return
            self.closed = True
            catalog_job = self.catalog_job
            self._cancel_catalog(reset=True)
            self.scope_generation = secrets.token_hex(16)
            self.switching = True
            try:
                self._stop_active()
            finally:
                if self.runtime_lock is not None:
                    os.close(self.runtime_lock)
                    self.runtime_lock = None
        if catalog_job is not None and catalog_job.thread is not None:
            catalog_job.thread.join(timeout=.2)
