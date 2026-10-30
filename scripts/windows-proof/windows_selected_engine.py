"""Finite, selected-conversation SQLCipher reads; no native/UI/network actions.

Keys, identifiers, names and message bodies are caller-owned private inputs.
Only sanitized controlled-token observations leave this module. State is RAM
only; this verification engine does not implement restart recovery.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import hmac
import io
from pathlib import Path
import re
import secrets


MAX_BODY = 1024 * 1024
SHARDS = ('message/message_0.db', 'message/message_1.db')
ALIASES = ('private_test', 'group_test')
ID = re.compile(r'[A-Za-z0-9_@.-]{1,256}\Z')
TOKEN = re.compile(r'[A-Za-z0-9_-]{8,128}\Z')


class EngineError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _key(keys, relative):
    value = keys.get(relative) if isinstance(keys, dict) else None
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{64}', value) is None:
        raise EngineError('selected_database_key_unavailable')
    return value.lower()


def _open(driver, path, key):
    connection = None
    try:
        with Path(path).open('rb') as stream:
            salt = stream.read(16)
        if len(salt) != 16:
            raise EngineError('selected_database_header_invalid')
        connection = driver.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=2)
        version = connection.execute('PRAGMA cipher_version').fetchone()
        if not version or not isinstance(version[0], str) or not version[0]:
            raise EngineError('sqlcipher_runtime_required')
        connection.execute('PRAGMA key = "x\'%s%s\'"' % (key, salt.hex()))
        for pragma in ('cipher_compatibility = 4', 'cipher_page_size = 4096',
                       'cipher_hmac_algorithm = HMAC_SHA512', 'cipher_kdf_algorithm = PBKDF2_HMAC_SHA512',
                       'query_only = ON', 'trusted_schema = OFF', 'temp_store = MEMORY'):
            connection.execute('PRAGMA ' + pragma)
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        return connection
    except Exception as error:
        if connection is not None:
            connection.close()
        if isinstance(error, EngineError):
            raise
        raise EngineError('selected_database_readonly_open_failed') from None


def _columns(connection, table):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return None
    return {row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)}


def resolve(driver, db_root, keys, private_name, group_name):
    """Exact BINARY contact metadata lookup; no message database is opened."""
    if any(not isinstance(value, str) or not 1 <= len(value) <= 256 for value in (private_name, group_name)):
        raise EngineError('selected_contact_name_invalid')
    connection = _open(driver, Path(db_root) / 'contact/contact.db', _key(keys, 'contact/contact.db'))
    try:
        columns = _columns(connection, 'contact')
        if columns is None or not {'username', 'nick_name', 'remark', 'alias'}.issubset(columns):
            raise EngineError('contact_metadata_schema_unsupported')
        chats = []
        for alias, name in zip(ALIASES, (private_name, group_name)):
            operator = '=' if alias == 'group_test' else '<>'
            rows = connection.execute(
                'SELECT DISTINCT username FROM contact WHERE ('
                'username COLLATE BINARY=? OR nick_name COLLATE BINARY=? OR '
                'remark COLLATE BINARY=? OR alias COLLATE BINARY=?) '
                "AND substr(username,-9) COLLATE BINARY %s '@chatroom' LIMIT 2" % operator,
                (name, name, name, name)).fetchall()
            if len(rows) != 1 or not isinstance(rows[0][0], str) or ID.fullmatch(rows[0][0]) is None:
                raise EngineError(alias + '_contact_not_unique')
            chat_id = rows[0][0]
            chats.append({'alias': alias, 'chat_id': chat_id,
                          'table': 'Msg_' + hashlib.md5(chat_id.encode()).hexdigest()})
        for chat in chats:
            chat['expected_sender_id'] = chats[0]['chat_id']
        return {'conversations': chats}
    except EngineError:
        raise
    except Exception:
        raise EngineError('contact_metadata_lookup_failed') from None
    finally:
        connection.close()


def _decode(value, compression):
    if compression in (2, 3):
        raise EngineError('dictionary_compression_unsupported')
    if compression not in (None, 0, 1, 4, 5):
        raise EngineError('message_compression_type_unsupported')
    if compression in (4, 5) and isinstance(value, str):
        raise EngineError('compressed_message_storage_type_unsupported')
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_BODY:
            raise EngineError('message_body_size_limit')
        return value
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise EngineError('message_body_type_unsupported')
    data = bytes(value)
    if len(data) > MAX_BODY:
        raise EngineError('message_body_size_limit')
    if compression in (4, 5) or data.startswith(b'\x28\xb5\x2f\xfd'):
        try:
            import zstandard
            decoder = zstandard.ZstdDecompressor(max_window_size=MAX_BODY // 1024)
            with decoder.stream_reader(io.BytesIO(data), read_across_frames=True) as stream:
                data = stream.read(MAX_BODY + 1)
        except Exception:
            raise EngineError('message_decompression_failed') from None
        if len(data) > MAX_BODY:
            raise EngineError('message_body_size_limit')
    try:
        return data.decode('utf-8')
    except UnicodeError:
        raise EngineError('message_encoding_unsupported') from None


def _background(before, after):
    return all(isinstance(state, dict) and state.get('probe_succeeded') is True and
               state.get('running') is True and state.get('frontmost') is False and
               state.get('screen_locked') is False for state in (before, after))


class Poller:
    def __init__(self, driver, db_root, keys, config, tokens):
        self._connections, self._cursors, self._observations = {}, {}, []
        self._initial_missing = set()
        self._scope, self._digest_key = secrets.token_bytes(32), bytes.fromhex(_key(keys, SHARDS[0]))
        try:
            chats = config['conversations']
            if len(chats) != 2 or {chat['alias'] for chat in chats} != set(ALIASES):
                raise ValueError
            self._chats = copy.deepcopy(chats)
            self._tokens = copy.deepcopy(tokens)
            for chat in self._chats:
                alias, chat_id = chat['alias'], chat['chat_id']
                if (not isinstance(chat_id, str) or ID.fullmatch(chat_id) is None or
                        (alias == 'group_test') != chat_id.endswith('@chatroom') or
                        chat['table'] != 'Msg_' + hashlib.md5(chat_id.encode()).hexdigest() or
                        not isinstance(chat.get('expected_sender_id'), str) or
                        ID.fullmatch(chat['expected_sender_id']) is None or
                        not isinstance(tokens[alias], list) or len(tokens[alias]) != 2 or
                        any(not isinstance(token, str) or TOKEN.fullmatch(token) is None for token in tokens[alias])):
                    raise ValueError
            if len({token for alias in ALIASES for token in tokens[alias]}) != 4:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise EngineError('selected_poll_configuration_invalid') from None
        try:
            found = {alias: False for alias in ALIASES}
            for relative in SHARDS:
                connection = _open(driver, Path(db_root) / relative, _key(keys, relative))
                self._connections[relative] = connection
                for chat in self._chats:
                    pair = (relative, chat['alias'])
                    columns = _columns(connection, chat['table'])
                    if columns is None:
                        self._initial_missing.add(pair)
                        continue
                    if not {'local_id', 'local_type', 'create_time', 'real_sender_id', 'message_content'}.issubset(columns):
                        raise EngineError('selected_message_schema_unsupported')
                    found[chat['alias']] = True
                    self._cursors[pair] = int(connection.execute(
                        'SELECT COALESCE(MAX(local_id),0) FROM "%s"' % chat['table']).fetchone()[0])
            if not all(found.values()):
                raise EngineError('selected_message_table_unavailable')
            self.baseline_completed_at = _now()
        except Exception as error:
            self.close()
            if isinstance(error, EngineError):
                raise
            raise EngineError('selected_message_baseline_failed') from None

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

    @property
    def observations(self):
        return copy.deepcopy(self._observations)

    def _sender(self, connection, rowid):
        if not isinstance(rowid, int) or isinstance(rowid, bool) or rowid <= 0:
            return None
        columns = _columns(connection, 'Name2Id')
        if columns is None or 'user_name' not in columns:
            return None
        row = connection.execute('SELECT user_name FROM Name2Id WHERE rowid=?', (rowid,)).fetchone()
        return row[0] if row and isinstance(row[0], str) and ID.fullmatch(row[0]) else None

    def poll(self, native_state_callback):
        """Advance RAM cursors only after every selected snapshot and state probe succeed."""
        if not self._connections:
            raise EngineError('selected_reader_closed')
        pending, cursors = [], dict(self._cursors)
        try:
            before = native_state_callback()
            for relative, connection in self._connections.items():
                for chat in self._chats:
                    pair = (relative, chat['alias'])
                    connection.execute('BEGIN')
                    try:
                        columns = _columns(connection, chat['table'])
                        if columns is None:
                            if pair in cursors:
                                raise EngineError('selected_message_table_disappeared')
                            continue
                        if not {'local_id', 'local_type', 'create_time', 'real_sender_id', 'message_content'}.issubset(columns):
                            raise EngineError('selected_message_schema_unsupported')
                        if pair not in cursors:
                            if pair not in self._initial_missing:
                                raise EngineError('selected_message_table_baseline_unknown')
                            cursors[pair] = 0
                        upper = int(connection.execute('SELECT COALESCE(MAX(local_id),0) FROM "%s"' % chat['table']).fetchone()[0])
                        if upper < cursors[pair]:
                            raise EngineError('selected_cursor_regressed')
                        compression = 'WCDB_CT_message_content' if 'WCDB_CT_message_content' in columns else 'NULL'
                        query = ('SELECT local_id,create_time,real_sender_id,%s,message_content '
                                 'FROM "%s" WHERE local_id>? AND local_id<=? '
                                 'AND (local_type & 4294967295)=1 ORDER BY local_id ASC LIMIT 500')
                        rows = connection.execute(query % (compression, chat['table']), (cursors[pair], upper))
                        for local_id, sent_at, sender_id, compressed, value in rows:
                            body = _decode(value, compressed)
                            sender = self._sender(connection, sender_id)
                            if chat['alias'] == 'group_test' and sender:
                                prefix = sender + ':\n'
                                if body.startswith(prefix):
                                    body = body[len(prefix):]
                            for index, token in enumerate(self._tokens[chat['alias']], 1):
                                if token not in body:
                                    continue
                                identity = ('%s\0%s\0%d' % (relative, chat['alias'], local_id)).encode()
                                observation = {'alias': chat['alias'], 'token_index': index,
                                               'body_exact_match': body == token,
                                               'expected_sender_match': sender == chat['expected_sender_id'] if sender else None,
                                               'event_id_digest': hmac.new(self._digest_key, self._scope + identity, hashlib.sha256).hexdigest()}
                                if isinstance(sent_at, int) and not isinstance(sent_at, bool):
                                    observation['source_sent_at'] = sent_at
                                pending.append(observation)
                            cursors[pair] = int(local_id)
                    finally:
                        connection.rollback()
            after = native_state_callback()
        except EngineError:
            raise
        except Exception:
            raise EngineError('selected_poll_or_native_state_failed') from None
        confirmed = _background(before, after)
        for observation in pending:
            observation['background_confirmed'] = confirmed
        self._cursors, self._observations = cursors, self._observations + pending
        return copy.deepcopy(pending)

    def summary(self):
        complete, failed, conversations = True, False, []
        for alias in ALIASES:
            tokens = []
            for index in (1, 2):
                matches = [row for row in self._observations if row['alias'] == alias and row['token_index'] == index]
                complete = complete and len(matches) == 1
                valid = len(matches) == 1 and all(matches[0][field] is True for field in
                                                 ('body_exact_match', 'expected_sender_match', 'background_confirmed'))
                failed = failed or len(matches) > 1 or bool(matches) and not valid
                tokens.append({'token_index': index, 'matched_count': len(matches), 'verified': valid})
            conversations.append({'alias': alias, 'tokens': tokens})
        passed = complete and not failed
        return {'status': '通过' if passed else '失败' if failed else '未验证',
                'reason': 'controlled_selected_text_verified' if passed else 'controlled_selected_text_mismatch' if failed else 'controlled_selected_text_incomplete',
                'all_expected_tokens_observed': complete, 'observed_events': len(self._observations),
                'unique_source_messages': len({row['event_id_digest'] for row in self._observations}),
                'baseline_completed_at': self.baseline_completed_at, 'conversations': conversations}
