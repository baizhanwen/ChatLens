#!/usr/bin/env python3
"""Narrow, local-only verification of new filehelper text while WeChat is behind.

No other conversation body is queried. Old bodies are never read. Credentials,
account paths, tokens, and unmatched text are never printed or written.

The SQLCipher raw-key + explicit-salt, DB-and-WAL reading approach was informed
by acmerfight's MIT-licensed local toolkit:
https://gist.github.com/acmerfight/0a01249ef72970d07a2603dbb629e80f
This test watcher is independently authored and does not execute that toolkit.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone


CHAT_ID = "filehelper"
TABLE = "Msg_" + hashlib.md5(CHAT_ID.encode()).hexdigest()
SHARD_RE = re.compile(r"message/message_[0-9]+\.db\Z")
MAX_BODY_BYTES = 1024 * 1024


class CheckError(Exception):
    def __init__(self, reason, status="失败"):
        super().__init__(reason)
        self.reason, self.status = reason, status


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields), ensure_ascii=False), flush=True)


def load_keys(path):
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        rows = obj["keys"]
        if not isinstance(rows, dict):
            raise ValueError()
        result = {}
        for relative, entry in rows.items():
            if not SHARD_RE.fullmatch(relative):
                continue
            value = entry["enc_key"]
            if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                raise ValueError()
            result[relative] = value.lower()
        if not result:
            raise ValueError()
        return result
    except (OSError, ValueError, KeyError, TypeError):
        raise CheckError("message_key_file_unavailable_or_invalid", "待外部条件") from None


def discover_shards(root):
    try:
        paths = sorted((root / "message").glob("message_*.db"))
        return {"message/" + path.name: path for path in paths
                if SHARD_RE.fullmatch("message/" + path.name) and path.is_file()}
    except OSError:
        raise CheckError("message_shard_discovery_failed", "待外部条件") from None


def load_driver():
    try:
        import sqlcipher3.dbapi2 as driver
        return driver
    except ImportError:
        raise CheckError("isolated_sqlcipher_runtime_missing", "待外部条件") from None


def open_readonly(path, raw_key, driver):
    connection = None
    try:
        with path.open("rb") as source:
            salt = source.read(16)
        if len(salt) != 16:
            raise CheckError("message_database_header_invalid")
        connection = driver.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        # A 48-byte SQLCipher raw key supplies the 32-byte encryption key and
        # original 16-byte salt; no password or derived key enters the argv.
        connection.execute('PRAGMA key = "x\'%s%s\'"' % (raw_key, salt.hex()))
        connection.execute("PRAGMA cipher_compatibility = 4")
        connection.execute("PRAGMA cipher_page_size = 4096")
        connection.execute("PRAGMA cipher_hmac_algorithm = HMAC_SHA512")
        connection.execute("PRAGMA cipher_kdf_algorithm = PBKDF2_HMAC_SHA512")
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA trusted_schema = OFF")
        connection.execute("PRAGMA temp_store = MEMORY")
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return connection
    except CheckError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise CheckError("message_database_readonly_open_failed") from None


def table_columns(connection):
    # The sole permitted message table is a constant derived from filehelper.
    exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone()
    if not exists:
        return None
    columns = {row[1] for row in connection.execute('PRAGMA table_info("%s")' % TABLE)}
    if not {"local_id", "local_type", "create_time", "message_content"}.issubset(columns):
        raise CheckError("filehelper_message_schema_unsupported")
    return columns


def table_max(connection):
    return int(connection.execute('SELECT COALESCE(MAX(local_id),0) FROM "%s"' % TABLE).fetchone()[0])


def baseline(connection):
    columns = table_columns(connection)
    return table_max(connection) if columns is not None else 0


def read_new_text(connection, cursor):
    """Read a bounded snapshot of new text from this one conversation only."""
    connection.execute("BEGIN")
    try:
        columns = table_columns(connection)
        if columns is None:
            return [], cursor
        upper = table_max(connection)
        server = '"server_id"' if "server_id" in columns else "NULL"
        compression = '"WCDB_CT_message_content"' if "WCDB_CT_message_content" in columns else "NULL"
        sql = ('SELECT local_id, create_time, %s, %s, message_content FROM "%s" '
               'WHERE local_id > ? AND local_id <= ? AND (local_type & 4294967295)=1 '
               'ORDER BY local_id LIMIT 500') % (server, compression, TABLE)
        rows = connection.execute(sql, (cursor, upper)).fetchall()
        next_cursor = int(rows[-1][0]) if len(rows) == 500 else upper
        return rows, next_cursor
    finally:
        connection.rollback()


def decode_body(value, compression):
    if isinstance(value, str):
        if len(value.encode("utf-8")) > MAX_BODY_BYTES:
            raise CheckError("new_text_body_exceeds_test_limit")
        return value
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise CheckError("new_text_body_type_unsupported")
    data = bytes(value)
    if len(data) > MAX_BODY_BYTES:
        raise CheckError("new_text_body_exceeds_test_limit")
    if data.startswith(b"\x28\xb5\x2f\xfd") or compression == 4:
        try:
            import zstandard
            data = zstandard.ZstdDecompressor().decompress(data, max_output_size=MAX_BODY_BYTES)
        except ImportError:
            raise CheckError("isolated_zstandard_runtime_missing", "待外部条件") from None
        except Exception:
            raise CheckError("new_text_decompression_failed") from None
    try:
        return data.decode("utf-8")
    except UnicodeError:
        raise CheckError("new_text_encoding_unsupported") from None


def native_state(probe):
    result = {"probe_succeeded": False, "running": None, "frontmost": None,
              "hidden": None, "screen_locked": None, "all_ax_windows_minimized": None}
    try:
        call = subprocess.run([str(probe), "wechat"], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=5, check=False)
        if call.returncode != 0 or len(call.stdout) > 128 * 1024:
            return result
        source = json.loads(call.stdout)
        result["probe_succeeded"] = True
        for field in ("running", "frontmost", "hidden", "screen_locked"):
            if isinstance(source.get(field), bool):
                result[field] = source[field]
        windows = source.get("windows")
        if isinstance(windows, list) and windows and all(isinstance(w.get("minimized"), bool) for w in windows):
            result["all_ax_windows_minimized"] = all(w["minimized"] for w in windows)
        version = source.get("version")
        if isinstance(version, str) and re.fullmatch(r"[0-9A-Za-z ._-]{1,40}", version):
            result["client_version"] = version
        return result
    except Exception:
        return result


def confirmed_background(before, after):
    return all(s.get("probe_succeeded") and s.get("running") is True and
               s.get("frontmost") is False and s.get("screen_locked") is False
               for s in (before, after))


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        json.dump(report, target, ensure_ascii=False, indent=2)
        target.write("\n")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-root", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--tokens", nargs="+", required=True)
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--native-probe", type=Path, required=True)
    parser.add_argument("--stop-when-complete", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.seconds <= 300:
        parser.error("seconds must be between 1 and 300")
    if not 1 <= len(args.tokens) <= 10 or len(set(args.tokens)) != len(args.tokens):
        parser.error("provide between 1 and 10 distinct synthetic test tokens")
    if any(not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token) for token in args.tokens):
        parser.error("test tokens must contain 8-128 ASCII letters, digits, underscores or hyphens")
    return args


def run(args, driver=None):
    report = {"recorded_at": utc_now(), "scope": "filehelper_new_text_test_tokens_only",
              "status": "未验证", "reason": "test_messages_not_observed",
              "capture_mode": "sqlcipher_database_and_wal_readonly",
              "baseline_completed_at": None, "message_shards": None,
              "requested_seconds": args.seconds, "elapsed_seconds": None,
              "background_verified": None, "duplicate_count": None,
              "tokens": [{"index": i + 1, "matched_count": 0, "body_exact_match": None,
                          "observations": []} for i in range(len(args.tokens))],
              "errors": []}
    connections, cursors, seen = {}, {}, set()
    digest_key = secrets.token_bytes(32)
    started = time.monotonic()
    stop = {"requested": False}
    old_handlers = {}

    def request_stop(signum, frame):
        stop["requested"] = True

    try:
        if args.output.exists():
            raise CheckError("verification_output_already_exists", "待外部条件")
        driver = driver or load_driver()
        keys = load_keys(args.key_file)
        shards = discover_shards(args.db_root)
        if not shards:
            raise CheckError("message_databases_unavailable", "待外部条件")
        if not set(shards).issubset(keys):
            raise CheckError("message_shard_keys_incomplete", "待外部条件")
        for relative, path in shards.items():
            con = open_readonly(path, keys[relative], driver)
            connections[relative] = con
            cursors[relative] = baseline(con)
        report["message_shards"] = len(connections)
        report["baseline_completed_at"] = utc_now()
        report["initial_native_state"] = native_state(args.native_probe)
        for signum in (signal.SIGINT, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, request_stop)
        emit("ready", baseline_completed_at=report["baseline_completed_at"],
             message_shards=len(connections), token_count=len(args.tokens),
             requested_seconds=args.seconds, scope=report["scope"])
        watch_started = time.monotonic()
        while not stop["requested"] and time.monotonic() - watch_started < args.seconds:
            before = native_state(args.native_probe)
            current = discover_shards(args.db_root)
            if set(connections) - set(current):
                raise CheckError("message_shard_disappeared_during_test")
            for relative in set(current) - set(connections):
                keys = load_keys(args.key_file)
                if relative not in keys:
                    raise CheckError("new_message_shard_key_missing", "待外部条件")
                connections[relative] = open_readonly(current[relative], keys[relative], driver)
                # This shard did not exist when ready was emitted, so all its
                # filehelper rows belong to the post-baseline observation.
                cursors[relative] = 0
            pending = []
            for relative, con in connections.items():
                rows, cursor = read_new_text(con, cursors[relative])
                for local_id, created_at, server_id, compression, value in rows:
                    body = decode_body(value, compression)
                    matching = [i for i, token in enumerate(args.tokens) if token in body]
                    if not matching:
                        continue
                    identity = "%s:%s:%s" % (relative, local_id, server_id)
                    digest = hmac.new(digest_key, identity.encode(), hashlib.sha256).hexdigest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    pending.append((matching, body, digest, created_at))
                cursors[relative] = cursor
            after = native_state(args.native_probe)
            for matching, body, digest, created_at in pending:
                for index in matching:
                    row = report["tokens"][index]
                    row["matched_count"] += 1
                    exact = body == args.tokens[index]
                    row["body_exact_match"] = exact if row["body_exact_match"] is None else row["body_exact_match"] and exact
                    row["observations"].append({"observed_at": utc_now(),
                        "source_create_time": created_at if isinstance(created_at, (int, float)) else None,
                        "event_id_digest": digest, "body_exact_match": exact,
                        "background_confirmed": confirmed_background(before, after),
                        "native_before": before, "native_after": after})
                emit("test_token_observed", matched_indexes=[i + 1 for i in matching],
                     background_confirmed=confirmed_background(before, after))
            if args.stop_when_complete and all(row["matched_count"] for row in report["tokens"]):
                break
            time.sleep(1)
        report["watch_elapsed_seconds"] = round(time.monotonic() - watch_started, 3)
        observed = [o for row in report["tokens"] for o in row["observations"]]
        if observed:
            report["background_verified"] = all(o["background_confirmed"] for o in observed)
            report["duplicate_count"] = sum(max(row["matched_count"] - 1, 0) for row in report["tokens"])
        complete = all(row["matched_count"] for row in report["tokens"])
        if complete and all(row["body_exact_match"] for row in report["tokens"]) and report["background_verified"] and report["duplicate_count"] == 0:
            report.update(status="通过", reason="controlled_new_text_read_while_client_background")
        elif observed:
            report.update(status="失败", reason="controlled_sample_incomplete_or_not_exact_or_background_unconfirmed")
    except CheckError as error:
        report.update(status=error.status, reason=error.reason)
        report["errors"].append({"type": type(error).__name__, "reason": error.reason})
    except Exception as error:
        report.update(status="失败", reason="watcher_operation_failed")
        report["errors"].append({"type": type(error).__name__, "reason": "watcher_operation_failed"})
    finally:
        for con in connections.values():
            try:
                con.close()
            except Exception:
                pass
        for signum, previous in old_handlers.items():
            signal.signal(signum, previous)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    try:
        save_report(args.output, report)
    except Exception as error:
        emit("report_write_failed", status="失败", error_type=type(error).__name__)
        return 2
    emit("complete", status=report["status"], reason=report["reason"],
         matched_counts=[row["matched_count"] for row in report["tokens"]],
         background_verified=report["background_verified"])
    return 0 if report["status"] == "通过" else 1


if __name__ == "__main__":
    sys.exit(run(parse_args()))
