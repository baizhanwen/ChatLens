"""Read-only view of the already authorized local collector store.

No source WeChat database, decryption key or writer lock is opened here.
Only committed rows belonging to the two fixed selected aliases leave this
boundary. Names come from a private, producer-written catalog, never guesses.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
import ctypes as C
import os
from pathlib import Path
import sqlite3
import stat
import time

from collector_private import ensure_private_dir, read_private_json, validate_local_path
from continuous_store import APPLICATION_ID, VERSION


ALIASES = ('private_test', 'group_test')
KINDS = {'private_test': 'private', 'group_test': 'group'}
MAX_PAGE = 100
MAX_QUERY_BYTES = 1024
MAX_RESPONSE_BODY_BYTES = 4 * 1024 * 1024
MAX_MESSAGE_BYTES = 1024 * 1024
STAGES = {'starting', 'collecting', 'paused', 'stopping', 'stopped', 'error'}
_STORE_FILES = {'collector.sqlite3', 'collector.sqlite3-journal', '.collector.lock', 'collector.identity.json'}
# Explicit public vocabulary: a syntactically valid lowercase secret in a
# damaged metadata file must not become a browser-visible "reason".
_PUBLIC_REASONS = {
    'collector_stopped_with_committed_checkpoint', 'collector_final_checkpoint_query_failed',
    'collector_source_process_unavailable_restart_required', 'collector_database_key_authentication_failed',
    'collector_operation_failed', 'collector_batch_commit_failed', 'collector_source_epoch_changed',
    'collector_source_message_conflict', 'collector_checkpoint_conflict',
    'collector_state_binding_mismatch', 'collector_state_file_identity_changed',
    'collector_source_read_timeout', 'collector_source_database_busy',
}


class WebDataError(Exception):
    def __init__(self, code, status=503):
        self.code, self.status = code, status
        super().__init__(code)


def _utc_now():
    return datetime.now(timezone.utc)


def _iso(value):
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _identity(info):
    return info.st_dev, info.st_ino


def _public_time(value):
    parsed = _iso(value)
    return parsed.isoformat() if parsed else None


def _regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not getattr(info, 'st_file_attributes', 0) & 0x400


def _windows_storage_acl(path):
    """A collector DB inherits its SID-only parent ACL; do not repair it."""
    import windows_keyproof_platform as platform
    api, value = platform._api(), platform._path(path)
    handle = api.CreateFileW(value, 0x20080, 7, None, 3, 0x00200000, None)
    if handle in (None, C.c_void_p(-1).value):
        raise WebDataError('web_store_permissions_unavailable')
    owner, acl, descriptor = platform.PTR(), platform.PTR(), platform.PTR()
    try:
        platform._checked(api, handle, value, False)
        if api.GetSecurityInfo(handle, 1, 5, C.byref(owner), None, C.byref(acl), None, C.byref(descriptor)):
            raise WebDataError('web_store_permissions_unavailable')
        info, ace_pointer = platform.ACLINFO(), platform.PTR()
        if (not owner or not acl or api.sid_text(owner) != api.current_sid()
                or not api.GetAclInformation(acl, C.byref(info), C.sizeof(info), 2)
                or info.count != 1 or not api.GetAce(acl, 0, C.byref(ace_pointer))):
            raise WebDataError('web_store_permissions_invalid')
        ace = C.cast(ace_pointer, C.POINTER(platform.ACE)).contents
        if (ace.kind != 0 or ace.flags not in (0, 16) or ace.mask != 0x1f01ff or ace.size < 16
                or api.sid_text(ace_pointer.value + 8) != api.current_sid()):
            raise WebDataError('web_store_permissions_invalid')
    finally:
        if descriptor:
            api.LocalFree(descriptor)
        api.CloseHandle(handle)


class CollectorWebData:
    def __init__(self, data_dir, *, now=None):
        self.data_dir = Path(data_dir)
        self.private = self.data_dir / 'private'
        self.store_dir = self.private / 'store'
        self.database = self.store_dir / 'collector.sqlite3'
        self._now = now or _utc_now
        try:
            ensure_private_dir(self.data_dir)
            ensure_private_dir(self.private)
        except Exception:
            raise WebDataError('web_private_data_unavailable') from None

    def _file(self, path):
        try:
            validate_local_path(path)
            info = path.lstat()
            if not _regular(info):
                raise WebDataError('web_store_file_unsafe')
            if os.name == 'nt':
                _windows_storage_acl(path)
            elif info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise WebDataError('web_store_permissions_invalid')
            return info
        except WebDataError:
            raise
        except Exception:
            raise WebDataError('web_store_unavailable') from None

    def _store_files(self):
        # The producer uses DELETE journaling. Never let SQLite follow an
        # unexpected WAL/SHM or a redirected hot journal beside the main file.
        for path in self.store_dir.iterdir():
            if path.name not in _STORE_FILES:
                raise WebDataError('web_store_contents_invalid')
            try:
                self._file(path)
            except WebDataError:
                # A normal short writer transaction may remove its journal
                # between directory enumeration and verification.
                if path.name == 'collector.sqlite3-journal' and not path.exists() and not path.is_symlink():
                    continue
                raise

    @contextmanager
    def _open(self, *, allow_uninitialized=False):
        connection = None
        try:
            ensure_private_dir(self.data_dir)
            ensure_private_dir(self.private)
            ensure_private_dir(self.store_dir)
            self._store_files()
            before = self._file(self.database)
            if os.name == 'nt':
                import windows_keyproof_platform as platform
                pinned = platform._parents(platform._api(), platform._path(self.database))
            else:
                pinned = nullcontext()
            with pinned:
                connection = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=0.15)
                connection.row_factory = sqlite3.Row
                connection.execute('PRAGMA query_only=ON')
                connection.execute('PRAGMA trusted_schema=OFF')
                connection.execute('PRAGMA temp_store=MEMORY')
                connection.execute('PRAGMA busy_timeout=150')
                deadline = time.monotonic() + 1.5
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
                if (connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                        or connection.execute('PRAGMA user_version').fetchone()[0] != VERSION):
                    raise WebDataError('web_store_schema_invalid')
                objects = connection.execute('SELECT type,name FROM sqlite_master ORDER BY type,name LIMIT 4').fetchall()
                if [tuple(row) for row in objects] != [
                        ('index', 'sqlite_autoindex_messages_1'), ('table', 'checkpoint'), ('table', 'messages')]:
                    raise WebDataError('web_store_schema_invalid')
                for table, columns in {
                    'messages': ['source_key', 'alias', 'source_local_id', 'sent_at', 'sender_id', 'body', 'observed_at'],
                    'checkpoint': ['id', 'binding_digest', 'initialized', 'cursors', 'epochs', 'baseline_completed_at', 'last_commit_at', 'commits'],
                }.items():
                    kind = connection.execute('SELECT type FROM sqlite_master WHERE name=?', (table,)).fetchone()
                    if kind is None or kind[0] != 'table':
                        raise WebDataError('web_store_schema_invalid')
                    if [row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)] != columns:
                        raise WebDataError('web_store_schema_invalid')
                if _identity(before) != _identity(self._file(self.database)):
                    raise WebDataError('web_store_identity_changed')
                connection.execute('BEGIN')
                state = connection.execute('SELECT id,initialized,binding_digest,last_commit_at FROM checkpoint LIMIT 2').fetchall()
                if (len(state) != 1 or state[0]['id'] != 1 or state[0]['initialized'] not in (0, 1)
                        or not allow_uninitialized and state[0]['initialized'] != 1):
                    raise WebDataError('web_store_not_initialized')
                yield connection, state[0]
                if _identity(before) != _identity(self._file(self.database)):
                    raise WebDataError('web_store_identity_changed')
        except WebDataError:
            raise
        except sqlite3.Error as error:
            code = getattr(error, 'sqlite_errorcode', None)
            if isinstance(code, int) and code & 255 in (5, 6):
                raise WebDataError('web_store_busy') from None
            raise WebDataError('web_store_read_failed') from None
        except Exception:
            raise WebDataError('web_store_unavailable') from None
        finally:
            if connection is not None:
                try:
                    connection.rollback()
                finally:
                    connection.close()

    def _metadata(self, filename):
        path = self.private / filename
        if not path.exists() and not path.is_symlink():
            return None
        for attempt in range(3):
            try:
                return read_private_json(path, maxbytes=1024 * 1024)
            except Exception:
                if attempt < 2:
                    time.sleep(0.005)
        raise WebDataError('web_metadata_unavailable') from None

    def _catalog(self, binding_digest, source):
        if source is None:
            return {alias: {'name': '所选私聊' if alias == 'private_test' else '所选群聊',
                            'kind': KINDS[alias], 'sender_names': {}} for alias in ALIASES}
        try:
            if (not isinstance(source, dict) or type(source.get('schema_version')) is not int
                    or source['schema_version'] != 1 or not isinstance(source.get('conversations'), list)
                    or len(source['conversations']) != 2):
                raise ValueError
            if source.get('binding_digest') is not None and source['binding_digest'] != binding_digest:
                raise WebDataError('web_catalog_binding_mismatch')
            result = {}
            for row in source['conversations']:
                alias, name = row['id'], row['name']
                if alias not in ALIASES or alias in result or row['kind'] != KINDS[alias]:
                    raise ValueError
                if not isinstance(name, str) or not name.strip() or len(name) > 256 or '\0' in name:
                    raise ValueError
                names = row.get('sender_names', {})
                if (not isinstance(names, dict) or len(names) > 4096
                        or any(not isinstance(k, str) or len(k) > 256 or not isinstance(v, str)
                               or not v.strip() or len(v) > 256 or '\0' in v for k, v in names.items())):
                    raise ValueError
                result[alias] = {'name': name, 'kind': row['kind'], 'sender_names': names}
            if set(result) != set(ALIASES):
                raise ValueError
            return result
        except WebDataError:
            raise
        except (KeyError, TypeError, ValueError):
            raise WebDataError('web_catalog_invalid') from None

    def conversations(self):
        # Metadata ACL checks and atomic-replace retries happen before opening
        # the SQLite snapshot, so they never extend the writer's lock wait.
        source = self._metadata('catalog.json')
        with self._open() as (connection, state):
            catalog = self._catalog(state['binding_digest'], source)
            rows = []
            for alias in ALIASES:
                count, latest = connection.execute('SELECT COUNT(*),MAX(rowid) FROM messages WHERE alias=?', (alias,)).fetchone()
                last = connection.execute('SELECT observed_at,substr(body,1,200) AS preview FROM messages WHERE rowid=? AND alias=?',
                                          (latest, alias)).fetchone() if latest is not None else None
                rows.append({'id': alias, 'alias': alias, 'name': catalog[alias]['name'], 'type': catalog[alias]['kind'],
                             'count': count, 'latest_id': latest, 'latest_at': _public_time(last['observed_at']) if last else None,
                             'latest_preview': last['preview'] if last else ''})
            return {'conversations': rows}

    def messages(self, conversation, *, before=None, after=None, limit=100, q=''):
        if conversation not in ALIASES:
            raise WebDataError('web_conversation_not_selected', 400)
        if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
            raise WebDataError('web_message_limit_invalid', 400)
        if before is not None and after is not None:
            raise WebDataError('web_message_cursor_invalid', 400)
        for value in (before, after):
            if value is not None and (type(value) is not int or not 0 <= value < 2**63):
                raise WebDataError('web_message_cursor_invalid', 400)
        try:
            query_valid = isinstance(q, str) and '\0' not in q and len(q.encode('utf-8')) <= MAX_QUERY_BYTES
        except UnicodeError:
            query_valid = False
        if not query_valid:
            raise WebDataError('web_message_query_invalid', 400)
        source_catalog = self._metadata('catalog.json')
        with self._open() as (connection, state):
            catalog = self._catalog(state['binding_digest'], source_catalog)
            where, params = ['alias=?'], [conversation]
            if before is not None:
                where.append('rowid<?'); params.append(before)
            if after is not None:
                where.append('rowid>?'); params.append(after)
            if q:
                where.append('instr(body,?)>0'); params.append(q)
            ascending = after is not None
            sql = ('SELECT rowid,body,sender_id,sent_at,observed_at FROM messages WHERE ' + ' AND '.join(where)
                   + ' ORDER BY rowid ' + ('ASC' if ascending else 'DESC') + ' LIMIT ?')
            source = connection.execute(sql, params + [limit + 1])
            result, body_bytes = [], 0
            has_more = False
            for row in source:
                if len(result) == limit:
                    has_more = True
                    break
                body = row['body']
                if not isinstance(body, str) or len(body.encode('utf-8')) > MAX_MESSAGE_BYTES:
                    raise WebDataError('web_message_body_invalid')
                size = len(body.encode('utf-8'))
                if result and body_bytes + size > MAX_RESPONSE_BODY_BYTES:
                    has_more = True
                    break
                body_bytes += size
                label = catalog[conversation]['sender_names'].get(row['sender_id'])
                result.append({'id': row['rowid'], 'conversation_id': conversation, 'conversation': conversation,
                               'body': body, 'sender': label if label else '未知发送人', 'sender_known': bool(label),
                               'sent_at': row['sent_at'] if type(row['sent_at']) is int else None,
                               'observed_at': _public_time(row['observed_at']), 'own': False})
            if not ascending:
                result.reverse()
            return {'messages': result, 'has_more': has_more,
                    'before_cursor': result[0]['id'] if result else None,
                    'after_cursor': result[-1]['id'] if result else None}

    def status(self):
        current = self._now()
        if isinstance(current, (int, float)):
            current = datetime.fromtimestamp(current, timezone.utc)
        if current.tzinfo is None:
            raise WebDataError('web_clock_invalid')
        with self._open(allow_uninitialized=True) as (connection, checkpoint):
            total = connection.execute('SELECT COUNT(*) FROM messages WHERE alias IN (?,?)', ALIASES).fetchone()[0]
            last_commit = _public_time(checkpoint['last_commit_at'])
            binding = checkpoint['binding_digest']
        source = self._metadata('web-status.json')
        result = {'state': 'unavailable', 'reason': 'collector_status_unavailable', 'last_update': last_commit,
                  'total_messages': total, 'heartbeat_at': None, 'started_at': None,
                  'updated_at': current.astimezone(timezone.utc).isoformat(), 'resumed_from_checkpoint': None,
                  'stale': True}
        if source is None:
            return result
        if (not isinstance(source, dict) or type(source.get('schema_version')) is not int
                or source['schema_version'] != 1 or source.get('stage') not in STAGES):
            raise WebDataError('web_collector_status_invalid')
        if source.get('binding_digest') is not None and source['binding_digest'] != binding:
            raise WebDataError('web_status_binding_mismatch')
        recorded = _iso(source.get('recorded_at_utc'))
        if recorded is None:
            raise WebDataError('web_collector_status_invalid')
        stage = source['stage']
        age = (current - recorded).total_seconds()
        stale = stage in ('starting', 'collecting', 'paused', 'stopping') and (age > 15 or age < -5)
        reason = source.get('reason')
        if reason is not None and (not isinstance(reason, str) or reason not in _PUBLIC_REASONS):
            reason = 'collector_status_reason_unavailable'
        started = _iso(source.get('started_at_utc'))
        result.update(state='stale' if stale else stage, reason='collector_heartbeat_stale' if stale else reason,
                      heartbeat_at=recorded.isoformat(), started_at=started.isoformat() if started else None,
                      resumed_from_checkpoint=source.get('resumed_from_checkpoint') if type(source.get('resumed_from_checkpoint')) is bool else None,
                      stale=stale)
        return result

    # A convenience alias for callers that describe the operation as get_status.
    get_status = status
