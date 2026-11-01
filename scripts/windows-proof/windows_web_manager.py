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


def load_contact_catalog(manager):
    """Enumerate contact names and source table names, never chat bodies."""
    import sqlcipher3.dbapi2 as driver
    import windows_keyproof_core as proof
    import windows_keyproof_platform as platform
    scope, keys = manager._source_documents()
    root, _, values, _ = validate_scope(scope, keys)
    tables = set()
    for relative, value in values.items():
        if not proof.page_authenticates(bytes.fromhex(value), platform.read_page(root / relative)):
            raise ValueError('windows_scope_key_authentication_failed')
        if not relative.startswith('message/'):
            continue
        connection = _open(driver, root / relative, value)
        try:
            deadline = time.monotonic() + 1.5
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'Msg_*' LIMIT 10001").fetchall()
            if len(rows) > 10000:
                raise ValueError('windows_scope_catalog_limit')
            tables.update(row[0] for row in rows)
        finally:
            connection.close()
    connection = _open(driver, root / 'contact/contact.db', values['contact/contact.db'])
    try:
        deadline = time.monotonic() + 1.5
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        rows = connection.execute('SELECT username,substr(remark,1,256),substr(nick_name,1,256) FROM contact LIMIT 10001').fetchall()
        if len(rows) > 10000:
            raise ValueError('windows_scope_catalog_limit')
    finally:
        connection.close()
    result = []
    seen = set()
    for username, remark, nickname in rows:
        if not isinstance(username, str) or not ID.fullmatch(username) or username in seen:
            continue
        if 'Msg_' + hashlib.md5(username.encode()).hexdigest() not in tables:
            continue
        seen.add(username)
        value = next((value.strip() for value in (remark, nickname) if isinstance(value, str) and value.strip()), '未命名会话')
        name = ''.join(char for char in value if char >= ' ' and char != '\x7f')[:256] or '未命名会话'
        result.append({'chat_id': username, 'name': name,
                       'type': 'group' if username.endswith('@chatroom') else 'private'})
    return result


class WindowsWebManager:
    def __init__(self, data_dir, package, output, *, pid=None, creation_time=None, read_only=False,
                 start_collector=None, stop_collector=None, catalog_loader=None, account_probe=None,
                 clock=time.monotonic):
        self.legacy_data = Path(data_dir)
        self.package, self.output = Path(package), Path(output)
        self.private = self.legacy_data / 'private'
        self.pid, self.creation_time, self.read_only = pid, creation_time, bool(read_only)
        self.start_collector, self.stop_collector = start_collector, stop_collector
        self.catalog_loader = catalog_loader or load_contact_catalog
        self.account_probe = account_probe
        self.clock, self.last_probe_at = clock, float('-inf')
        self.lock, self.commands = threading.RLock(), SimpleQueue()
        self.scope_generation, self.active_scope_id = secrets.token_hex(16), 'legacy'
        self.provider = self.assistant = self.child = self.child_output = None
        self.active_config, self.contacts, self.catalog = {'conversations': []}, [], {}
        self.catalog_ready = self.catalog_loading = self.queued = self.switching = False
        self.closed, self.account_matched, self.account_blocked = False, bool(read_only), False
        self.account_probe_reason = 'windows_account_not_checked'
        self.stage, self.reason = 'starting', None
        self.description = '正在恢复已有关注范围；原消息和采集位置保留。'
        self.account_identity = None
        self.scope = self.data_dir = None
        self.children_started, self.last_child_exit, self.cleanup_confirmed = 0, None, True
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
        if not self.read_only:
            prepare_session(self.legacy_data)  # Validate only; never reset an existing checkpoint.
            if self.account_probe is None:
                from windows_account_probe import WindowsAccountProbe
                self.account_probe = WindowsAccountProbe(self.pid, self.creation_time,
                                                         self.scope['exe_path'], self.scope['db_root'])
            if not self._verify_account(force=True):
                return
        identifier = self._active_pointer()
        self._activate(identifier, initial=True)

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
        public_reasons = {'windows_account_root_invalid', 'windows_account_handle_invalid',
            'windows_account_device_unresolved', 'windows_account_open_database_unavailable',
            'windows_account_changed_or_ambiguous', 'windows_account_open_database_matched',
            'windows_account_probe_unavailable'}
        self.account_probe_reason = reason if reason in public_reasons else 'windows_account_probe_unavailable'
        self.account_matched = matched
        if not matched:
            if not self.account_blocked:
                self.scope_generation = secrets.token_hex(16)
                self.account_blocked, self.switching = True, True
                self.catalog_ready = self.catalog_loading = self.queued = False
                self.contacts, self.catalog, self.commands = [], {}, SimpleQueue()
                self._stop_active()
            self.stage, self.reason = 'error', 'windows_active_account_not_verified'
            self.description = '无法确认当前微信仍是此资料对应的账号，已停止采集并隔离旧页面。请登录原账号，或关闭后选择另一账号自己的资料目录。'
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
            self.cleanup_confirmed = True
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

    def setup(self):
        with self.lock:
            profiles = self._saved_profiles() if self.scope and not self.account_blocked else []
            detected = not self.account_blocked
            return {'stage': self.stage, 'reason': self.reason, 'platform': 'windows', 'read_only': self.read_only,
                'account': {'detected': detected, 'display_name': None},
                'checks': {'wechat_running': self.account_matched and not self.read_only,
                           'access_ready': detected, 'reader_ready': detected and not self.read_only},
                'progress': {'phase': 'baseline' if self.stage == 'starting' else None, 'completed': None, 'total': None, 'elapsed_seconds': 0},
                'initialization': {'state': 'working' if self.queued or self.stage == 'starting' else 'idle',
                                   'action': 'none', 'description': self.description},
                'scope_limits': {'total': MAX_CONVERSATIONS, 'private': MAX_CONVERSATIONS, 'group': MAX_CONVERSATIONS},
                **self.public_scope_context(), 'catalog': {'state': 'loading' if self.catalog_loading else 'ready' if self.catalog_ready else 'idle'},
                'active_scope': next((row for row in profiles if row['active']), None),
                'profiles': profiles, 'workspace_path': '/'}

    def request_initialize(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            raise ValueError('windows_existing_reader_configuration_required')

    def request_catalog(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self.read_only or self.switching or not self._verify_account(force=True):
                raise ValueError('windows_scope_catalog_not_ready')
            if not self.catalog_loading:
                self.catalog_loading = True
                self.commands.put(('catalog', None))
            return self.setup()

    def list_conversations(self, q='', *, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if not isinstance(q, str) or len(q) > 256 or '\0' in q:
                raise ValueError('windows_scope_query_invalid')
            rows = [dict(row) for row in self.contacts if q.casefold() in row['name'].casefold()] if self.catalog_ready else []
            if not q:
                rows.sort(key=lambda row: (not row['selected'], row['type'], row['name'].casefold()))
            return {'conversations': rows[:200], 'has_more': len(rows) > 200}

    def _begin_switch(self):
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
            if not self.read_only and not self._verify_account(force=True):
                raise ValueError('windows_scope_account_not_ready')
            self._begin_switch()
            self.commands.put(('profile', identifier))
            return self.setup()

    def _load_contacts(self):
        rows = self.catalog_loader(self)
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
                if not self.read_only:
                    if not self._verify_account(force=self.queued):
                        return
                    if self.account_blocked:
                        self.scope_generation = secrets.token_hex(16)
                        self._activate(self._active_pointer())
                        return
                try:
                    command, value = self.commands.get_nowait()
                except Empty:
                    command = None
                if command == 'catalog':
                    try:
                        self._load_contacts()
                    except Exception:
                        self.catalog_ready = self.catalog_loading = False
                        self.reason = 'windows_scope_catalog_unavailable'
                        self.description = '会话名称暂不可用，原关注范围继续保留；请确认微信账号与读取权限后重试。'
                elif command == 'select':
                    self._activate(value[0], value[1], value[2])
                elif command == 'profile':
                    self._activate(value)
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
            self.scope_generation = secrets.token_hex(16)
            self.switching = True
            try:
                self._stop_active()
            finally:
                if self.runtime_lock is not None:
                    os.close(self.runtime_lock)
                    self.runtime_lock = None
