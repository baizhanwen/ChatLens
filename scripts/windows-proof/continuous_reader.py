"""Incremental reads of explicitly selected conversations into a private store.

The source connections are SQLCipher read-only. The caller owns the private
configuration, keys and destination; public return values contain counts only.
A destination transaction commits messages together with source cursors. No
network, message-sending, scheduling or source checkpoint operation is present.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import os
import json
from pathlib import Path
import re
import stat

from windows_selected_engine import (
    ID, SHARDS, EngineError, _background, _columns, _decode, _key, _now, _open,
)
from conversation_scope import LEGACY_ALIASES, ScopeError, normalize_conversations


PAGE_SIZE = 128
MAX_BATCH_BODY_BYTES = 16 * 1024 * 1024
MAX_BATCH_MESSAGES = 10000
MAX_PAIR_BODY_BYTES = MAX_BATCH_BODY_BYTES // (len(SHARDS) * len(LEGACY_ALIASES))
REQUIRED_COLUMNS = {'local_id', 'local_type', 'create_time', 'real_sender_id', 'message_content'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def _unsafe(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _config(value):
    try:
        return normalize_conversations(value)
    except ScopeError:
        raise EngineError('continuous_configuration_invalid') from None


def _maximum(connection, table):
    value = connection.execute('SELECT COALESCE(MAX(local_id),0) FROM "%s"' % table).fetchone()[0]
    if type(value) is not int or value < 0:
        raise EngineError('continuous_source_local_id_invalid')
    return value


def _sender(connection, value):
    if type(value) is not int or value <= 0:
        return None
    columns = _columns(connection, 'Name2Id')
    if columns is None or 'user_name' not in columns:
        return None
    row = connection.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (value,)).fetchone()
    if row and isinstance(row[0], str) and ID.fullmatch(row[0]):
        return row[0]
    return None


class ContinuousReader:
    """One bounded read/commit step; restarting uses committed destination cursors.

    ``store`` supplies snapshot/initialize/commit/stats. A native state callback
    must confirm the selected client is running, in background and unlocked at
    both ends of a read. Unhealthy samples raise without advancing the state.
    """

    def __init__(self, driver, db_root, keys, config, store, native_state_callback, *, shards=None):
        self._connections = {}
        self._shards = tuple(SHARDS if shards is None else shards)
        if (not 1 <= len(self._shards) <= 8
                or any(not isinstance(relative, str) or re.fullmatch(r'message/message_[0-7]\.db', relative) is None
                       for relative in self._shards)
                or len(set(self._shards)) != len(self._shards)):
            raise EngineError('continuous_shard_configuration_invalid')
        self._store, self._native = store, native_state_callback
        self._root, self._keys = Path(db_root), {relative: _key(keys, relative) for relative in self._shards}
        self._chats = _config(config)
        self._aliases = tuple(chat['alias'] for chat in self._chats)
        pairs = len(self._shards) * len(self._chats)
        self._pair_body_budget = min(MAX_PAIR_BODY_BYTES, MAX_BATCH_BODY_BYTES // pairs)
        self._pair_page_size = min(PAGE_SIZE, max(1, MAX_BATCH_MESSAGES // pairs))
        self._pair_offset = 0
        self.resumed = False
        self.baseline_completed_at = None
        try:
            epochs = self._sources()
            snapshot = store.snapshot()
            if type(snapshot.get('initialized')) is not bool:
                raise EngineError('continuous_checkpoint_invalid')
            self.resumed = snapshot['initialized']
            if self.resumed:
                self._validate_snapshot(snapshot, epochs)
                from scope_history import apply_history_plan
                snapshot = apply_history_plan(store, snapshot['cursors'], epochs)
                self._validate_snapshot(snapshot, epochs)
            for relative in self._shards:
                self._connections[relative] = _open(driver, self._root / relative, self._keys[relative])
            if not self.resumed:
                before = self._sample()
                cursors = {relative: {} for relative in self._shards}
                found = {alias: False for alias in self._aliases}
                for relative, connection in self._connections.items():
                    for chat in self._chats:
                        connection.execute('BEGIN')
                        try:
                            columns = _columns(connection, chat['table'])
                            present = columns is not None
                            if present and not REQUIRED_COLUMNS.issubset(columns):
                                raise EngineError('continuous_message_schema_unsupported')
                            found[chat['alias']] = found[chat['alias']] or present
                            cursors[relative][chat['alias']] = {
                                'local_id': _maximum(connection, chat['table']) if present else 0,
                                'present': present,
                            }
                        finally:
                            connection.rollback()
                if not all(found.values()):
                    raise EngineError('continuous_selected_table_unavailable')
                after = self._sample()
                if not _background(before, after):
                    raise EngineError('continuous_native_state_not_ready')
                if self._sources() != epochs:
                    raise EngineError('continuous_source_epoch_changed')
                from scope_history import apply_history_plan
                snapshot = apply_history_plan(store, cursors, epochs, initialize=True)
                self._validate_snapshot(snapshot, epochs)
            self.baseline_completed_at = snapshot.get('baseline_completed_at')
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        for connection in self._connections.values():
            try:
                connection.close()
            except Exception:
                pass
        self._connections.clear()

    def _sample(self):
        try:
            value = self._native()
        except Exception:
            raise EngineError('continuous_native_state_unavailable') from None
        if not _background(value, value):
            raise EngineError('continuous_native_state_not_ready')
        return value

    def _sources(self):
        """Only identity, key fingerprint and the stable 16-byte salt are epochs."""
        try:
            if not self._root.is_absolute() or '..' in self._root.parts:
                raise EngineError('continuous_source_path_unsafe')
            directories = tuple(self._root.parents) + (self._root, self._root / 'message')
            for directory in directories:
                info = directory.lstat()
                if _unsafe(info) or not stat.S_ISDIR(info.st_mode):
                    raise EngineError('continuous_source_path_unsafe')
            entries = list(itertools.islice((self._root / 'message').iterdir(), 65))
            if len(entries) > 64:
                raise EngineError('continuous_source_directory_entry_limit')
            names = {path.name for path in entries if re.fullmatch(r'message_[0-9]+\.db', path.name)}
            if names != {Path(relative).name for relative in self._shards}:
                raise EngineError('continuous_source_shard_set_changed')
            result = {}
            for relative in self._shards:
                path = self._root / relative
                before = path.lstat()
                if _unsafe(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise EngineError('continuous_source_path_unsafe')
                with path.open('rb') as stream:
                    salt = stream.read(16)
                    opened = os.fstat(stream.fileno())
                after = path.lstat()
                identities = {(info.st_dev, info.st_ino) for info in (before, opened, after)}
                if len(identities) != 1 or _unsafe(after):
                    raise EngineError('continuous_source_epoch_changed')
                if len(salt) != 16:
                    raise EngineError('continuous_source_header_invalid')
                sidecars = {}
                for suffix in ('-wal', '-shm'):
                    sidecar = Path(str(path) + suffix)
                    try:
                        info = sidecar.lstat()
                    except FileNotFoundError:
                        continue
                    if _unsafe(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise EngineError('continuous_source_path_unsafe')
                    sidecars[suffix] = info
                if '-wal' in sidecars and sidecars['-wal'].st_size > 0 and '-shm' not in sidecars:
                    raise EngineError('continuous_source_wal_shm_unavailable')
                result[relative] = {
                    'file_identity': [int(before.st_dev), int(before.st_ino)],
                    'salt_sha256': hashlib.sha256(salt).hexdigest(),
                    'key_sha256': hashlib.sha256(bytes.fromhex(self._keys[relative])).hexdigest(),
                }
            return result
        except EngineError:
            raise
        except (OSError, ValueError):
            raise EngineError('continuous_source_unavailable') from None

    def _validate_snapshot(self, snapshot, epochs):
        if snapshot.get('epochs') != epochs:
            raise EngineError('continuous_source_epoch_changed')
        cursors = snapshot.get('cursors')
        if not isinstance(cursors, dict) or set(cursors) != set(self._shards):
            raise EngineError('continuous_checkpoint_invalid')
        for relative in self._shards:
            if not isinstance(cursors[relative], dict) or set(cursors[relative]) != set(self._aliases):
                raise EngineError('continuous_checkpoint_invalid')
            for alias in self._aliases:
                cursor = cursors[relative][alias]
                if (not isinstance(cursor, dict) or set(cursor) != {'local_id', 'present'}
                        or type(cursor['local_id']) is not int or cursor['local_id'] < 0
                        or type(cursor['present']) is not bool
                        or not cursor['present'] and cursor['local_id'] != 0):
                    raise EngineError('continuous_checkpoint_invalid')

    def poll(self):
        if not self._connections:
            raise EngineError('continuous_reader_closed')
        before = self._sample()
        epochs = self._sources()
        snapshot = self._store.snapshot()
        self._validate_snapshot(snapshot, epochs)
        cursors = copy.deepcopy(snapshot['cursors'])
        events, batch_bytes, batch_full = [], 0, False
        pairs = [(relative, connection, chat) for relative, connection in self._connections.items()
                 for chat in self._chats]
        next_offset = self._pair_offset
        try:
            for position in range(len(pairs)):
                index = (self._pair_offset + position) % len(pairs)
                if len(events) >= MAX_BATCH_MESSAGES or batch_bytes >= MAX_BATCH_BODY_BYTES:
                    next_offset = index
                    break
                relative, connection, chat = pairs[index]
                next_offset = (index + 1) % len(pairs)
                alias, table = chat['alias'], chat['table']
                cursor = cursors[relative][alias]
                connection.execute('BEGIN')
                rows = None
                try:
                    columns = _columns(connection, table)
                    if columns is None:
                        if cursor['present']:
                            raise EngineError('continuous_selected_table_disappeared')
                        continue
                    if not REQUIRED_COLUMNS.issubset(columns):
                        raise EngineError('continuous_message_schema_unsupported')
                    cursor['present'] = True
                    upper = _maximum(connection, table)
                    if upper < cursor['local_id']:
                        raise EngineError('continuous_source_cursor_regressed')
                    compression = 'WCDB_CT_message_content' if 'WCDB_CT_message_content' in columns else 'NULL'
                    sql = ('SELECT local_id,create_time,real_sender_id,%s,message_content '
                           'FROM "%s" WHERE local_id>? AND local_id<=? '
                           'AND (local_type & 4294967295)=1 ORDER BY local_id ASC LIMIT %d')
                    rows = connection.execute(sql % (compression, table, self._pair_page_size), (cursor['local_id'], upper))
                    count, body_bytes, capped = 0, 0, False
                    for local_id, sent_at, sender_id, compressed, value in rows:
                        if type(local_id) is not int or not cursor['local_id'] < local_id <= upper:
                            raise EngineError('continuous_source_local_id_invalid')
                        body = _decode(value, compressed)
                        size = len(body.encode('utf-8'))
                        # A first individually bounded message may exceed its
                        # fair share; otherwise a large text could block this
                        # conversation forever when many chats are selected.
                        if len(events) >= MAX_BATCH_MESSAGES or batch_bytes + size > MAX_BATCH_BODY_BYTES:
                            capped, batch_full, next_offset = True, True, index
                            break
                        if count and body_bytes + size > min(self._pair_body_budget, MAX_PAIR_BODY_BYTES):
                            capped = True
                            break
                        sender = _sender(connection, sender_id)
                        if chat['chat_id'].endswith('@chatroom') and sender and body.startswith(sender + ':\n'):
                            body = body[len(sender) + 2:]
                        identity = [relative, chat['chat_id'], local_id, epochs[relative]]
                        events.append({
                            'source_key': hashlib.sha256(_canonical(identity)).hexdigest(),
                            'alias': alias, 'source_local_id': local_id,
                            'sent_at': sent_at if type(sent_at) is int else None,
                            'sender_id': sender, 'body': body, 'observed_at': _now(),
                        })
                        body_bytes += size
                        batch_bytes += size
                        count += 1
                        cursor['local_id'] = local_id
                    # Fewer than one page means all remaining text was consumed.
                    # Advancing across trailing media reads no media bodies.
                    if not capped and count < self._pair_page_size:
                        cursor['local_id'] = upper
                finally:
                    if rows is not None:
                        rows.close()
                    connection.rollback()
                if batch_full:
                    break
            after = self._sample()
            if not _background(before, after):
                raise EngineError('continuous_native_state_not_ready')
            if self._sources() != epochs:
                raise EngineError('continuous_source_epoch_changed')
        except EngineError:
            raise
        except Exception as error:
            code = getattr(error, 'sqlite_errorcode', None)
            if type(code) is int and code & 255 in (5, 6):
                raise EngineError('continuous_source_busy') from None
            raise EngineError('continuous_source_read_failed') from None
        changed = cursors != snapshot['cursors']
        if changed or events:
            result = self._store.commit(snapshot['cursors'], cursors, events, epochs)
        else:
            stats = self._store.stats()
            result = {'inserted': 0, 'duplicates': 0, 'total_messages': stats['total_messages']}
        self._pair_offset = next_offset
        return {'new_messages': result['inserted'], 'duplicates': result['duplicates'],
                'total_messages': result['total_messages'], 'cursors_advanced': changed,
                'background_confirmed': True}
