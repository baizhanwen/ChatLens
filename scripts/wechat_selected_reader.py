#!/usr/bin/env python3
"""Controlled selected-chat DB/WAL reader with private, atomic restart state.

This module has no daemon, model, network, message-sending, or UI entry point.
Only selected Msg_<md5(chat_id)> tables are queried for new text. The caller
supplies the private configuration and existing local database keys.
"""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile

try:
    from scripts import watch_wechat_test_messages as _base
except ModuleNotFoundError:
    import watch_wechat_test_messages as _base


CheckError = _base.CheckError
open_readonly = _base.open_readonly
decode_body = _base.decode_body
native_state = _base.native_state
confirmed_background = _base.confirmed_background
load_keys = _base.load_keys
discover_shards = _base.discover_shards
utc_now = _base.utc_now

_ALIASES = {"private", "group", "private_test", "group_test"}
_ID = re.compile(r"[A-Za-z0-9_@.-]{1,256}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")
_VERSION = 1


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _normalize_config(config):
    if not isinstance(config, dict) or not isinstance(config.get("run_id"), str) or not config["run_id"]:
        raise CheckError("selected_run_configuration_invalid", "待外部条件")
    supplied = config.get("conversations")
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= 2:
        raise CheckError("selected_conversation_configuration_invalid", "待外部条件")
    rows, aliases, chats = [], set(), set()
    for source in supplied:
        if not isinstance(source, dict):
            raise CheckError("selected_conversation_configuration_invalid", "待外部条件")
        alias, chat_id, tokens = source.get("alias"), source.get("chat_id"), source.get("tokens")
        expected = source.get("expected_sender_id")
        if alias not in _ALIASES or alias in aliases or not isinstance(chat_id, str) or not _ID.fullmatch(chat_id) or chat_id in chats:
            raise CheckError("selected_conversation_identity_invalid", "待外部条件")
        group = alias in ("group", "group_test")
        if group != chat_id.endswith("@chatroom"):
            raise CheckError("selected_conversation_kind_mismatch", "待外部条件")
        if not isinstance(tokens, list) or not 1 <= len(tokens) <= 256 or any(not isinstance(t, str) or not _TOKEN.fullmatch(t) for t in tokens):
            raise CheckError("selected_test_tokens_invalid", "待外部条件")
        if len(tokens) != len(set(tokens)):
            raise CheckError("selected_test_tokens_ambiguous", "待外部条件")
        if expected is not None and (not isinstance(expected, str) or not _ID.fullmatch(expected)):
            raise CheckError("selected_sender_expectation_invalid", "待外部条件")
        aliases.add(alias)
        chats.add(chat_id)
        rows.append({"alias": alias, "chat_id": chat_id, "tokens": list(tokens),
                     "expected_sender_id": expected, "is_group": group,
                     "table": "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()})
    return {"run_id": config["run_id"], "conversations": rows}


def _columns(connection, table):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return None
    columns = {row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)}
    if not {"local_id", "local_type", "create_time", "message_content"}.issubset(columns):
        raise CheckError("selected_message_schema_unsupported")
    return columns


def _maximum(connection, table):
    return int(connection.execute('SELECT COALESCE(MAX(local_id),0) FROM "%s"' % table).fetchone()[0])


def _baseline(connection, table):
    return _maximum(connection, table) if _columns(connection, table) is not None else 0


def _new_rows(connection, table, cursor):
    connection.execute("BEGIN")
    try:
        columns = _columns(connection, table)
        if columns is None:
            if cursor:
                raise CheckError("selected_message_table_disappeared")
            return [], 0
        maximum = _maximum(connection, table)
        if maximum < cursor:
            raise CheckError("selected_message_cursor_regressed")
        server = '"server_id"' if "server_id" in columns else "NULL"
        sender = '"real_sender_id"' if "real_sender_id" in columns else "NULL"
        compression = '"WCDB_CT_message_content"' if "WCDB_CT_message_content" in columns else "NULL"
        sql = ('SELECT local_id, create_time, %s, %s, %s, message_content FROM "%s" '
               'WHERE local_id > ? AND local_id <= ? AND (local_type & 4294967295)=1 '
               'ORDER BY local_id LIMIT 500') % (server, sender, compression, table)
        rows = connection.execute(sql, (cursor, maximum)).fetchall()
        return rows, int(rows[-1][0]) if len(rows) == 500 else maximum
    finally:
        connection.rollback()


def _sender(connection, real_sender_id):
    if not isinstance(real_sender_id, int) or isinstance(real_sender_id, bool) or real_sender_id <= 0:
        return None
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='Name2Id'").fetchone()
    if exists is None:
        return None
    columns = {row[1] for row in connection.execute('PRAGMA table_info("Name2Id")')}
    if "user_name" not in columns:
        return None
    row = connection.execute('SELECT user_name FROM "Name2Id" WHERE rowid=?', (real_sender_id,)).fetchone()
    if row and isinstance(row[0], str) and _ID.fullmatch(row[0]):
        return row[0]
    return None


class SelectedReader:
    """One finite selected-chat read step; each successful commit is restartable.

    ``step()`` returns only new sanitized token observations after their cursors
    and observations were atomically committed together. A failed commit leaves
    both in-memory and on-disk progress unchanged. Repeated physical messages
    with identical bodies retain distinct event digests.
    """

    def __init__(self, db_root, key_file, config, state_file, native_probe, driver=None):
        self.db_root = Path(db_root).resolve()
        self.key_file, self.state_file = Path(key_file), Path(state_file)
        self.native_probe = Path(native_probe)
        self.driver = driver or _base.load_driver()
        self._connections, self._state, self._secret = {}, None, None
        self._lock_handle, self._closed = None, False
        self.resumed, self.restored_observations = False, 0
        try:
            self._config = _normalize_config(config)
            self._conversations = self._config["conversations"]
            self._aliases = [row["alias"] for row in self._conversations]
            stat = self.db_root.stat()
            if not self.db_root.is_dir():
                raise CheckError("selected_account_root_unavailable", "待外部条件")
            account = _sha(_canonical([str(self.db_root), stat.st_dev, stat.st_ino]))
            self._binding = {"account_root_fingerprint": account,
                             "run_id_digest": _sha(self._config["run_id"].encode()),
                             "configuration_digest": _sha(_canonical(self._config))}
            self._prepare_private_state()
            keys, shards = self._current_sources()
            if self.state_file.exists():
                try:
                    source = json.loads(self.state_file.read_text(encoding="utf-8"))
                except Exception:
                    raise CheckError("selected_private_state_invalid") from None
                if not isinstance(source, dict) or source.get("version") != _VERSION or source.get("binding") != self._binding:
                    raise CheckError("selected_private_state_binding_mismatch")
                anchor = source.get("integrity_anchor")
                if anchor not in shards or anchor not in keys:
                    raise CheckError("selected_state_anchor_unavailable")
                self._secret = self._derive_secret(keys[anchor], shards[anchor])
                checksum = source.get("integrity")
                unsigned = {k: v for k, v in source.items() if k != "integrity"}
                if not isinstance(checksum, str) or not hmac.compare_digest(checksum, self._digest(_canonical(unsigned))):
                    raise CheckError("selected_private_state_integrity_failed")
                self._validate_state(source)
                self._state = source
                self._validate_epochs(shards, keys, source)
                for relative in source["key_epochs"]:
                    self._connections[relative] = open_readonly(shards[relative], keys[relative], self.driver)
                self.resumed = True
                self.restored_observations = len(source["observations"])
            else:
                anchor = sorted(shards)[0]
                self._secret = self._derive_secret(keys[anchor], shards[anchor])
                initial = {"version": _VERSION, "binding": self._binding,
                           "integrity_anchor": anchor, "baseline_completed_at": None,
                           "last_commit_at": None, "key_epochs": {}, "cursors": {},
                           "observations": []}
                for relative, path in shards.items():
                    connection = open_readonly(path, keys[relative], self.driver)
                    self._connections[relative] = connection
                    initial["key_epochs"][relative] = self._epoch(keys[relative], path)
                    initial["cursors"][relative] = {row["alias"]: _baseline(connection, row["table"])
                                                     for row in self._conversations}
                initial["baseline_completed_at"] = utc_now()
                initial["last_commit_at"] = initial["baseline_completed_at"]
                self._state = self._persist(initial)
        except CheckError:
            self.close()
            raise
        except Exception:
            self.close()
            raise CheckError("selected_reader_initialization_failed") from None

    def _prepare_private_state(self):
        if self.state_file.is_symlink() or self.state_file.parent.is_symlink():
            raise CheckError("selected_private_state_symlink_refused")
        self.state_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(self.state_file.parent), 0o700)
        if self.state_file.exists():
            os.chmod(str(self.state_file), 0o600)
        # A process lock prevents two consumers from racing the same cursors;
        # flock is released by the OS even if a test process crashes.
        try:
            import fcntl
            self._lock_handle = open(str(self.state_file) + ".lock", "a+b")
            os.chmod(str(self.state_file) + ".lock", 0o600)
            fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            raise CheckError("selected_private_state_process_lock_unavailable", "待外部条件") from None
        except OSError:
            raise CheckError("selected_private_state_in_use", "待外部条件") from None

    def _current_sources(self):
        stat = self.db_root.stat()
        account = _sha(_canonical([str(self.db_root), stat.st_dev, stat.st_ino]))
        if account != self._binding["account_root_fingerprint"]:
            raise CheckError("selected_account_root_changed")
        keys = load_keys(self.key_file)
        shards = discover_shards(self.db_root)
        if not shards or not set(shards).issubset(keys):
            raise CheckError("selected_message_shard_keys_incomplete", "待外部条件")
        expected_parent = (self.db_root / "message").resolve()
        if any(path.is_symlink() or path.resolve().parent != expected_parent for path in shards.values()):
            raise CheckError("selected_message_shard_path_refused")
        return keys, shards

    def _epoch(self, key, path):
        with path.open("rb") as source:
            salt = source.read(16)
        if len(salt) != 16:
            raise CheckError("selected_message_shard_header_invalid")
        return _sha(bytes.fromhex(key) + salt)

    def _derive_secret(self, key, path):
        context = b"chatlens-selected-reader-v1\0" + _canonical(self._binding)
        return hmac.new(bytes.fromhex(key), context + self._epoch(key, path).encode(), hashlib.sha256).digest()

    def _digest(self, content):
        return hmac.new(self._secret, content, hashlib.sha256).hexdigest()

    def _validate_state(self, state):
        epochs, cursors, observations = state.get("key_epochs"), state.get("cursors"), state.get("observations")
        if not isinstance(epochs, dict) or not epochs or not isinstance(cursors, dict) or set(cursors) != set(epochs) or not isinstance(observations, list):
            raise CheckError("selected_private_state_structure_invalid")
        for relative, positions in cursors.items():
            if not _base.SHARD_RE.fullmatch(relative) or not isinstance(positions, dict) or set(positions) != set(self._aliases):
                raise CheckError("selected_private_state_cursor_invalid")
            if any(type(value) is not int or value < 0 for value in positions.values()):
                raise CheckError("selected_private_state_cursor_invalid")
        if any(not isinstance(row, dict) or row.get("alias") not in self._aliases for row in observations):
            raise CheckError("selected_private_state_observations_invalid")

    def _validate_epochs(self, shards, keys, state):
        if not set(state["key_epochs"]).issubset(shards):
            raise CheckError("selected_message_shard_disappeared")
        for relative, epoch in state["key_epochs"].items():
            if not hmac.compare_digest(epoch, self._epoch(keys[relative], shards[relative])):
                raise CheckError("selected_message_shard_key_or_salt_changed")

    def _persist(self, state):
        candidate = {key: copy.deepcopy(value) for key, value in state.items() if key != "integrity"}
        candidate["integrity"] = self._digest(_canonical(candidate))
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".selected-state-", dir=str(self.state_file.parent))
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as target:
                json.dump(candidate, target, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                target.write("\n")
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.state_file)
            temporary = None
            # The committed file is already fsynced. Directory fsync is best
            # effort, so an unsupported directory fsync cannot report a false
            # rollback after the atomic replace has actually succeeded.
            try:
                directory = os.open(str(self.state_file.parent), os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except OSError:
                pass
            return candidate
        except Exception:
            raise CheckError("selected_private_state_commit_failed") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    @property
    def observations(self):
        return copy.deepcopy(self._state["observations"]) if self._state else []

    def step(self):
        if self._closed:
            raise CheckError("selected_reader_closed")
        added_connections = {}
        try:
            keys, shards = self._current_sources()
            self._validate_epochs(shards, keys, self._state)
            candidate = copy.deepcopy(self._state)
            for relative in sorted(set(shards) - set(candidate["key_epochs"])):
                connection = open_readonly(shards[relative], keys[relative], self.driver)
                added_connections[relative] = connection
                candidate["key_epochs"][relative] = self._epoch(keys[relative], shards[relative])
                # New keyed shards appeared after this run's initial baseline.
                # Starting at zero preserves post-baseline messages in them.
                candidate["cursors"][relative] = {alias: 0 for alias in self._aliases}
            before = native_state(self.native_probe)
            pending = []
            seen = {(row["event_id_digest"], row["alias"], row["token_index"])
                    for row in candidate["observations"]}
            connections = dict(self._connections, **added_connections)
            for relative, connection in sorted(connections.items()):
                for conversation in self._conversations:
                    alias = conversation["alias"]
                    rows, cursor = _new_rows(connection, conversation["table"], candidate["cursors"][relative][alias])
                    for local_id, created_at, server_id, real_sender_id, compression, value in rows:
                        body = decode_body(value, compression)
                        matching = [index for index, token in enumerate(conversation["tokens"]) if token in body]
                        if not matching:
                            continue
                        sender = _sender(connection, real_sender_id)
                        prefix_removed = False
                        if conversation["is_group"] and sender is not None and body.startswith(sender + ":\n"):
                            body = body[len(sender) + 2:]
                            prefix_removed = True
                        event_id = self._digest(_canonical([alias, relative, local_id]))
                        for index in matching:
                            identity = (event_id, alias, index + 1)
                            if identity in seen:
                                continue
                            seen.add(identity)
                            expected = conversation["expected_sender_id"]
                            pending.append({"alias": alias, "token_index": index + 1,
                                "event_id_digest": event_id,
                                "source_id_kind": "message_shard_local_id",
                                "server_id_available": isinstance(server_id, int) and server_id != 0,
                                "sender_digest": self._digest(sender.encode()) if sender is not None else None,
                                "sender_source": "name2id_rowid" if sender is not None else "unavailable",
                                "expected_sender_match": sender == expected if sender is not None and expected is not None else None,
                                "group_prefix_removed": prefix_removed,
                                "body_exact_match": body == conversation["tokens"][index],
                                "source_create_time": created_at if isinstance(created_at, (int, float)) else None,
                                "observed_at": utc_now()})
                    candidate["cursors"][relative][alias] = cursor
            after = native_state(self.native_probe)
            for observation in pending:
                observation.update(background_confirmed=confirmed_background(before, after),
                                   native_before=before, native_after=after)
            candidate["observations"].extend(pending)
            changed = any(candidate.get(key) != self._state.get(key) for key in ("cursors", "key_epochs", "observations"))
            if changed:
                candidate["last_commit_at"] = utc_now()
                committed = self._persist(candidate)
                self._state = committed
                self._connections.update(added_connections)
                added_connections = {}
            return copy.deepcopy(pending)
        except CheckError:
            raise
        except Exception:
            raise CheckError("selected_reader_step_failed") from None
        finally:
            for connection in added_connections.values():
                connection.close()

    def summary(self):
        conversations = []
        all_complete, any_observed, any_failed, sender_unverified = True, False, False, False
        for conversation in self._conversations:
            alias, tokens = conversation["alias"], []
            for index in range(len(conversation["tokens"])):
                matches = [row for row in self._state["observations"] if row["alias"] == alias and row["token_index"] == index + 1]
                complete = bool(matches)
                all_complete = all_complete and complete
                any_observed = any_observed or complete
                exact = all(row["body_exact_match"] for row in matches) if matches else None
                background = all(row["background_confirmed"] for row in matches) if matches else None
                sender_results = [row["expected_sender_match"] for row in matches]
                expected = (all(sender_results) if sender_results and all(value is not None for value in sender_results) else None)
                if exact is False or background is False or expected is False:
                    any_failed = True
                if complete and conversation["expected_sender_id"] is not None and expected is None:
                    sender_unverified = True
                tokens.append({"token_index": index + 1, "matched_count": len(matches),
                               "body_exact_match": exact, "background_confirmed": background,
                               "expected_sender_match": expected})
            conversations.append({"alias": alias, "tokens": tokens})
        status = "失败" if any_failed else "通过" if all_complete and not sender_unverified else "未验证"
        reason = ("controlled_message_check_failed" if any_failed else
                  "controlled_selected_text_verified" if status == "通过" else
                  "configured_sender_identity_unverified" if sender_unverified else
                  "waiting_for_controlled_test_messages")
        return {"status": status, "scope": "selected_controlled_new_text_only",
                "reason": reason,
                "resumed": self.resumed, "restored_observations": self.restored_observations,
                "baseline_completed_at": self._state["baseline_completed_at"],
                "last_commit_at": self._state["last_commit_at"],
                "message_shards": len(self._state["key_epochs"]),
                "observed_events": len(self._state["observations"]),
                "unique_source_messages": len({row["event_id_digest"] for row in self._state["observations"]}),
                "all_expected_tokens_observed": all_complete,
                "any_test_message_observed": any_observed,
                "configured_sender_expectation_unverified": sender_unverified,
                "conversations": conversations}

    def close(self):
        if self._closed:
            return
        self._closed = True
        for connection in self._connections.values():
            try:
                connection.close()
            except Exception:
                pass
        self._connections = {}
        if self._lock_handle is not None:
            try:
                self._lock_handle.close()
            except Exception:
                pass
            self._lock_handle = None
