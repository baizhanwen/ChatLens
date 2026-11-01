"""Private, transactional selected-message storage and restart checkpoints.

No network, source database access or console output lives here. The caller owns
scope selection and, on Windows, must protect ``directory`` with its user ACL
before construction. Bodies exist only in this private local SQLite database.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


VERSION = 1
APPLICATION_ID = 0x434C434F
DB_NAME = 'collector.sqlite3'
LOCK_NAME = '.collector.lock'
IDENTITY_NAME = 'collector.identity.json'
_ALLOWED = {DB_NAME, DB_NAME + '-journal', LOCK_NAME, IDENTITY_NAME}
_EVENT_KEYS = {'source_key', 'alias', 'source_local_id', 'sent_at', 'sender_id', 'body', 'observed_at'}


class StoreError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(data.encode('utf-8')) > 1024 * 1024:
            raise ValueError
        return data
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise StoreError('collector_state_value_invalid') from None


def _json_map(value):
    if not isinstance(value, dict):
        raise StoreError('collector_checkpoint_map_invalid')
    return _canonical(value)


def _regular(info):
    return stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not getattr(info, 'st_file_attributes', 0) & 0x400


def _safe_file(path):
    try:
        info = path.lstat()
    except OSError:
        raise StoreError('collector_state_file_unavailable') from None
    if not _regular(info):
        raise StoreError('collector_state_file_unsafe')
    if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
        raise StoreError('collector_state_permissions_unsafe')
    return info


def _write_exclusive(path, content):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, 'wb') as target:
            target.write(content)
            target.flush()
            os.fsync(target.fileno())
    except OSError:
        raise StoreError('collector_state_create_failed') from None


def _read_identity(path):
    before = _safe_file(path)
    if before.st_size > 4096:
        raise StoreError('collector_state_identity_invalid')
    try:
        flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, 'rb') as source:
            opened = os.fstat(source.fileno())
            if (not _regular(opened)
                    or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
                raise StoreError('collector_state_identity_invalid')
            raw = source.read(4097)
            if len(raw) > 4096:
                raise StoreError('collector_state_identity_invalid')
        return json.loads(raw.decode('utf-8'))
    except (OSError, ValueError, UnicodeError):
        raise StoreError('collector_state_identity_invalid') from None


class CollectorStore:
    """One privately locked store; commit texts and their cursors together.

    Once identity/database files exist they must both remain present. A missing
    database is an error, never an invitation to establish a new source baseline.
    ``fault_hook`` is an in-process synthetic-test seam, not a user CLI setting.
    """
    def __init__(self, directory, binding, *, fault_hook=None):
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        self._connection = None
        self._lock = None
        self._lock_acquired = False
        self._lock_created = False
        self._closed = False
        self._fault_hook = fault_hook
        self.resumed = False
        self._binding = hashlib.sha256(_json_map(binding).encode('utf-8')).hexdigest()
        try:
            self._prepare_directory()
            self._acquire_lock()
            self._open_database()
        except StoreError:
            self.close()
            raise
        except Exception:
            self.close()
            raise StoreError('collector_store_open_failed') from None

    def _prepare_directory(self):
        try:
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or self.directory.is_symlink()
                    or getattr(info, 'st_file_attributes', 0) & 0x400):
                raise StoreError('collector_state_directory_unsafe')
            if os.name != 'nt' and stat.S_IMODE(info.st_mode) & 0o077:
                raise StoreError('collector_state_permissions_unsafe')
            names = {item.name for item in self.directory.iterdir()}
            if names - _ALLOWED:
                raise StoreError('collector_state_unexpected_file')
            for name in names:
                _safe_file(self.directory / name)
        except OSError:
            raise StoreError('collector_state_directory_unavailable') from None

    def _acquire_lock(self):
        path = self.directory / LOCK_NAME
        if not path.exists():
            try:
                _write_exclusive(path, b'1')
                self._lock_created = True
            except StoreError:
                if not path.exists():
                    raise
        before = _safe_file(path)
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
            opened = os.fstat(fd)
            if (not _regular(opened) or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or opened.st_size != 1):
                os.close(fd)
                raise StoreError('collector_lock_file_unsafe')
            self._lock = os.fdopen(fd, 'r+b', buffering=0)
            if os.name == 'nt':
                import msvcrt
                self._lock.seek(0)
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_acquired = True
        except StoreError:
            raise
        except OSError:
            raise StoreError('collector_already_running_or_lock_unavailable') from None
        self._lock_identity = (opened.st_dev, opened.st_ino)

    def _check_files(self):
        self._require_open()
        for name in (DB_NAME, LOCK_NAME, IDENTITY_NAME):
            info = _safe_file(self.directory / name)
            expected = self._db_identity if name == DB_NAME else self._lock_identity if name == LOCK_NAME else self._marker_identity
            if (info.st_dev, info.st_ino) != expected:
                raise StoreError('collector_state_file_identity_changed')
        if self.directory.is_symlink():
            raise StoreError('collector_state_directory_unsafe')

    def _open_database(self):
        database, marker = self.directory / DB_NAME, self.directory / IDENTITY_NAME
        has_database, has_marker = database.exists(), marker.exists()
        if has_database != has_marker:
            raise StoreError('collector_state_pair_incomplete')
        if not has_database:
            # A surviving lock identifies an established/reserved workspace.
            # Deleting both checkpoint files must not silently rebaseline it.
            if not self._lock_created:
                raise StoreError('collector_state_pair_incomplete')
            if (self.directory / (DB_NAME + '-journal')).exists():
                raise StoreError('collector_state_pair_incomplete')
            _write_exclusive(marker, (_canonical({'schema_version': VERSION, 'binding_digest': self._binding}) + '\n').encode())
            _write_exclusive(database, b'')
        else:
            value = _read_identity(marker)
            if value != {'schema_version': VERSION, 'binding_digest': self._binding}:
                raise StoreError('collector_state_binding_mismatch')
        db_stat, marker_stat = _safe_file(database), _safe_file(marker)
        self._db_identity = (db_stat.st_dev, db_stat.st_ino)
        self._marker_identity = (marker_stat.st_dev, marker_stat.st_ino)
        try:
            connection = sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=0, isolation_level=None)
            self._connection = connection
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA foreign_keys=ON')
            # A short Web read may overlap our commit; keep message/cursor atomic.
            connection.execute('PRAGMA busy_timeout=3000')
            connection.execute('PRAGMA temp_store=MEMORY')
            connection.execute('PRAGMA journal_mode=DELETE')
            connection.execute('PRAGMA synchronous=FULL')
            if not has_database:
                connection.execute('BEGIN IMMEDIATE')
                try:
                    connection.execute('PRAGMA application_id=%d' % APPLICATION_ID)
                    connection.execute('PRAGMA user_version=%d' % VERSION)
                    connection.execute('CREATE TABLE checkpoint (id INTEGER PRIMARY KEY CHECK(id=1), binding_digest TEXT NOT NULL, initialized INTEGER NOT NULL CHECK(initialized IN(0,1)), cursors TEXT NOT NULL, epochs TEXT NOT NULL, baseline_completed_at TEXT, last_commit_at TEXT, commits INTEGER NOT NULL)')
                    connection.execute('CREATE TABLE messages (source_key TEXT PRIMARY KEY, alias TEXT NOT NULL, source_local_id INTEGER NOT NULL, sent_at INTEGER, sender_id TEXT, body TEXT NOT NULL, observed_at TEXT NOT NULL)')
                    connection.execute('INSERT INTO checkpoint VALUES (1,?,0,\'{}\',\'{}\',NULL,NULL,0)', (self._binding,))
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
            if connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or connection.execute('PRAGMA user_version').fetchone()[0] != VERSION:
                raise StoreError('collector_state_schema_invalid')
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if tables != {'checkpoint', 'messages'}:
                raise StoreError('collector_state_schema_invalid')
            objects = list(connection.execute("SELECT type,name FROM sqlite_master WHERE type!='table'"))
            if objects != [('index', 'sqlite_autoindex_messages_1')]:
                raise StoreError('collector_state_schema_invalid')
            for table, expected in {
                'checkpoint': ['id', 'binding_digest', 'initialized', 'cursors', 'epochs', 'baseline_completed_at', 'last_commit_at', 'commits'],
                'messages': ['source_key', 'alias', 'source_local_id', 'sent_at', 'sender_id', 'body', 'observed_at'],
            }.items():
                if [row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)] != expected:
                    raise StoreError('collector_state_schema_invalid')
            if connection.execute('PRAGMA quick_check').fetchone() != ('ok',):
                raise StoreError('collector_state_integrity_failed')
            state = self.snapshot()
            self.resumed = state['initialized']
        except StoreError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError):
            raise StoreError('collector_state_database_invalid') from None

    def _require_open(self):
        if self._closed or self._connection is None:
            raise StoreError('collector_store_closed')

    def _fault(self, stage):
        if self._fault_hook is not None:
            self._fault_hook(stage)

    def snapshot(self):
        self._require_open()
        try:
            rows = self._connection.execute('SELECT binding_digest,initialized,cursors,epochs,baseline_completed_at,last_commit_at,commits FROM checkpoint').fetchall()
            if len(rows) != 1:
                raise StoreError('collector_checkpoint_invalid')
            binding, initialized, cursors, epochs, baseline, last, commits = rows[0]
            if binding != self._binding:
                raise StoreError('collector_state_binding_mismatch')
            if initialized not in (0, 1) or type(commits) is not int or commits < 0:
                raise StoreError('collector_checkpoint_invalid')
            cursor_map, epoch_map = json.loads(cursors), json.loads(epochs)
            _json_map(cursor_map); _json_map(epoch_map)
            if initialized and (not isinstance(baseline, str) or not isinstance(last, str)):
                raise StoreError('collector_checkpoint_invalid')
            if not initialized and (cursor_map or epoch_map or baseline is not None or last is not None or commits):
                raise StoreError('collector_checkpoint_invalid')
            return {'initialized': bool(initialized), 'cursors': cursor_map, 'epochs': epoch_map,
                    'baseline_completed_at': baseline, 'last_commit_at': last}
        except StoreError:
            raise
        except (sqlite3.Error, ValueError, TypeError, UnicodeError):
            raise StoreError('collector_checkpoint_invalid') from None

    def initialize(self, cursors, epochs):
        self._check_files()
        cursor_json, epoch_json = _json_map(cursors), _json_map(epochs)
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            current = self.snapshot()
            if current['initialized']:
                raise StoreError('collector_baseline_already_initialized')
            when = _now()
            self._connection.execute('UPDATE checkpoint SET initialized=1,cursors=?,epochs=?,baseline_completed_at=?,last_commit_at=? WHERE id=1', (cursor_json, epoch_json, when, when))
            self._fault('before_baseline_commit')
            self._connection.commit()
        except BaseException as error:
            self._connection.rollback()
            if isinstance(error, StoreError):
                raise
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise StoreError('collector_baseline_commit_failed') from None
        self._fault('after_baseline_commit')
        return self.snapshot()

    @staticmethod
    def _event(event):
        if not isinstance(event, dict) or set(event) != _EVENT_KEYS:
            raise StoreError('collector_event_invalid')
        key, alias, local_id = event['source_key'], event['alias'], event['source_local_id']
        if (not isinstance(key, str) or re.fullmatch(r'[0-9a-f]{64}', key) is None
                or not isinstance(alias, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', alias) is None
                or type(local_id) is not int or not 0 < local_id < 2**63):
            raise StoreError('collector_event_identity_invalid')
        sent_at, sender, body, observed = event['sent_at'], event['sender_id'], event['body'], event['observed_at']
        if sent_at is not None and (type(sent_at) is not int or not -2**63 <= sent_at < 2**63):
            raise StoreError('collector_event_time_invalid')
        try:
            if (sender is not None and (not isinstance(sender, str) or len(sender.encode('utf-8')) > 2048)
                    or not isinstance(body, str) or len(body.encode('utf-8')) > 1024 * 1024
                    or not isinstance(observed, str) or len(observed) > 64
                    or datetime.fromisoformat(observed).utcoffset() != timezone.utc.utcoffset(None)):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise StoreError('collector_event_content_invalid') from None
        return key, alias, local_id, sent_at, sender, body, observed

    def commit(self, expected_cursors, new_cursors, events, epochs):
        self._check_files()
        _json_map(expected_cursors)
        next_json, epoch_json = _json_map(new_cursors), _json_map(epochs)
        if not isinstance(events, (tuple, list)) or len(events) > 10000:
            raise StoreError('collector_event_batch_invalid')
        rows = [self._event(event) for event in events]
        inserted = duplicates = 0
        try:
            self._connection.execute('BEGIN IMMEDIATE')
            state = self.snapshot()
            if not state['initialized']:
                raise StoreError('collector_baseline_required')
            if _canonical(state['cursors']) != _canonical(expected_cursors):
                raise StoreError('collector_checkpoint_conflict')
            if _canonical(state['epochs']) != epoch_json:
                raise StoreError('collector_source_epoch_changed')
            for row in rows:
                prior = self._connection.execute('SELECT source_key,alias,source_local_id,sent_at,sender_id,body,observed_at FROM messages WHERE source_key=?', (row[0],)).fetchone()
                if prior is not None:
                    if tuple(prior[:6]) != row[:6]:
                        raise StoreError('collector_source_message_conflict')
                    duplicates += 1
                else:
                    self._connection.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', row)
                    inserted += 1
            self._fault('after_message_inserts')
            self._connection.execute('UPDATE checkpoint SET cursors=?,last_commit_at=?,commits=commits+1 WHERE id=1', (next_json, _now()))
            self._fault('before_commit')
            self._connection.commit()
        except BaseException as error:
            self._connection.rollback()
            if isinstance(error, StoreError):
                raise
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise StoreError('collector_batch_commit_failed') from None
        self._fault('after_commit')
        return {'inserted': inserted, 'duplicates': duplicates, 'total_messages': self.stats()['total_messages']}

    def stats(self):
        self._check_files()
        try:
            return {'total_messages': self._connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0],
                    'commits': self._connection.execute('SELECT commits FROM checkpoint WHERE id=1').fetchone()[0]}
        except (sqlite3.Error, TypeError, IndexError):
            raise StoreError('collector_stats_unavailable') from None

    @property
    def binding_digest(self):
        return self._binding

    def sender_ids_by_alias(self):
        """Bounded metadata for senders already observed inside the selected scope."""
        self._check_files()
        result = {'private_test': [], 'group_test': []}
        try:
            for alias in result:
                rows = self._connection.execute(
                    'SELECT DISTINCT sender_id FROM messages WHERE alias=? AND sender_id IS NOT NULL LIMIT 256',
                    (alias,)).fetchall()
                result[alias] = [row[0] for row in rows if isinstance(row[0], str)]
            return result
        except sqlite3.Error:
            raise StoreError('collector_sender_metadata_unavailable') from None

    def controlled_summary(self, tokens, expected_senders):
        """Return counts/match flags for local controlled tests, never texts/IDs.

        Every selected text is stored independently of these tokens. This query
        only evaluates already committed destination rows and changes no cursor.
        Substring hits are counted so accidental extra text cannot falsely pass.
        """
        self._check_files()
        if not isinstance(tokens, dict) or not isinstance(expected_senders, dict) or len(tokens) > 16:
            raise StoreError('collector_controlled_test_configuration_invalid')
        all_tokens, normalized = set(), []
        for alias, values in tokens.items():
            if (not isinstance(alias, str) or re.fullmatch(r'[A-Za-z0-9_-]{1,64}', alias) is None
                    or not isinstance(values, list) or not 1 <= len(values) <= 256):
                raise StoreError('collector_controlled_test_configuration_invalid')
            for value in values:
                if (not isinstance(value, str) or not 8 <= len(value) <= 128
                        or re.fullmatch(r'[A-Za-z0-9_-]+', value) is None or value in all_tokens):
                    raise StoreError('collector_controlled_test_configuration_invalid')
                all_tokens.add(value)
            sender = expected_senders.get(alias)
            if sender is not None and (not isinstance(sender, str) or len(sender.encode('utf-8')) > 2048):
                raise StoreError('collector_controlled_test_configuration_invalid')
            normalized.append((alias, values, sender))
        complete, failed, observed_events = bool(normalized), False, 0
        conversations, unique = [], set()
        try:
            for alias, values, sender in normalized:
                outcomes = []
                for index, value in enumerate(values, 1):
                    rows = self._connection.execute(
                        'SELECT source_key,sent_at,observed_at,body=?,sender_id=? FROM messages WHERE alias=? AND instr(body,?)>0',
                        (value, sender, alias, value)).fetchall()
                    count = len(rows)
                    verified = count == 1 and rows[0][3] == 1 and rows[0][4] == 1
                    complete = complete and count == 1
                    failed = failed or count > 1 or bool(count) and not verified
                    observed_events += count
                    unique.update(row[0] for row in rows)
                    outcomes.append({'token_index': index, 'matched_count': count, 'verified': verified,
                                     'source_sent_at': rows[0][1] if count == 1 else None,
                                     'observed_at': rows[0][2] if count == 1 else None})
                conversations.append({'alias': alias, 'tokens': outcomes})
        except sqlite3.Error:
            raise StoreError('collector_controlled_summary_unavailable') from None
        passed = complete and not failed
        return {'status': '通过' if passed else '失败' if failed else '未验证',
                'reason': 'controlled_selected_text_verified' if passed else 'controlled_selected_text_mismatch' if failed else 'controlled_selected_text_incomplete',
                'all_expected_tokens_observed': complete, 'observed_events': observed_events,
                'unique_source_messages': len(unique), 'conversations': conversations}

    def close(self):
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                pass
            self._connection = None
        if self._lock is not None:
            try:
                if self._lock_acquired and os.name == 'nt':
                    import msvcrt
                    self._lock.seek(0)
                    msvcrt.locking(self._lock.fileno(), msvcrt.LK_UNLCK, 1)
                elif self._lock_acquired:
                    import fcntl
                    fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            self._lock.close()
            self._lock = None
            self._lock_acquired = False
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
