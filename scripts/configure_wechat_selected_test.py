#!/usr/bin/env python3
"""Resolve two explicitly named test chats using contact metadata only.

No message bodies, historical messages, session previews, cloud services or UI
are used. Contact names, usernames, tokens and keys stay in private files.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import sys


def local_module(name, filename):
    specification = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / filename)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


watcher = local_module("selected_config_watcher", "watch_wechat_test_messages.py")
preparation = local_module("selected_config_preparation", "prepare_wechat_read_test.py")
CONTACT_RELATIVE = "contact/contact.db"
ALIASES = (("private_test", "private"), ("group_test", "group"))
TOKEN = re.compile(r"[A-Za-z0-9_-]{8,128}\Z")


class ResolverError(Exception):
    def __init__(self, reason, alias=None, status="待外部条件"):
        super().__init__(reason)
        self.reason, self.alias, self.status = reason, alias, status


def emit(event, **fields):
    print(json.dumps(dict(event=event, **fields), ensure_ascii=False), flush=True)


def load_request(path):
    try:
        if path.stat().st_size > 256 * 1024:
            raise ValueError()
        request = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError()
        run_id = request["run_id"]
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise ValueError()
        for prefix in ("private", "group"):
            name = request[prefix + "_name"]
            if not isinstance(name, str) or not name.strip() or len(name.encode("utf-8")) > 2048 or "\x00" in name:
                raise ValueError()
            tokens = request[prefix + "_tokens"]
            if not isinstance(tokens, list) or len(tokens) != 3:
                raise ValueError()
            if any(not isinstance(token, str) or not TOKEN.fullmatch(token) for token in tokens):
                raise ValueError()
            if len(set(tokens)) != len(tokens):
                raise ValueError()
        return request
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise ResolverError("private_request_unavailable_or_invalid") from None


def load_contact_key(path):
    """Read only the explicitly allowed contact-key entry from a merged map."""
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError()
        document = json.loads(path.read_text(encoding="utf-8"))
        key = document["keys"][CONTACT_RELATIVE]["enc_key"]
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", key):
            raise ValueError()
        return key.lower()
    except (OSError, UnicodeError, ValueError, KeyError, TypeError):
        raise ResolverError("contact_key_unavailable_or_invalid") from None


def check_contact_schema(connection):
    columns = {row[1] for row in connection.execute('PRAGMA table_info("contact")')}
    if not {"username", "nick_name", "remark", "alias"}.issubset(columns):
        raise ResolverError("contact_metadata_schema_unsupported")


def resolve_exact(connection, name, kind, alias):
    if kind not in ("private", "group"):
        raise ResolverError("contact_type_invalid", alias, "失败")
    # The operator is selected from this local enum, never from request text.
    suffix_operator = "=" if kind == "group" else "!="
    sql = (
        "SELECT DISTINCT username FROM contact WHERE ("
        "username COLLATE BINARY = ? OR nick_name COLLATE BINARY = ? "
        "OR remark COLLATE BINARY = ? OR alias COLLATE BINARY = ?) "
        "AND substr(username, -9) %s ? LIMIT 2" % suffix_operator)
    candidates = connection.execute(sql, (name, name, name, name, "@chatroom")).fetchall()
    if not candidates:
        raise ResolverError("contact_exact_name_not_found", alias)
    if len(candidates) != 1:
        raise ResolverError("contact_exact_name_ambiguous", alias)
    username = candidates[0][0]
    if not isinstance(username, str) or not username or len(username.encode("utf-8")) > 2048 or "\x00" in username:
        raise ResolverError("contact_identifier_invalid", alias)
    return username


def build_configuration(request, connection):
    check_contact_schema(connection)
    private = resolve_exact(connection, request["private_name"], "private", "private_test")
    group = resolve_exact(connection, request["group_name"], "group", "group_test")
    return {"run_id": request["run_id"], "conversations": [
        {"alias": "private_test", "chat_id": private,
         "tokens": request["private_tokens"], "expected_sender_id": private},
        {"alias": "group_test", "chat_id": group,
         "tokens": request["group_tokens"], "expected_sender_id": private},
    ]}


def save_private_configuration(path, configuration):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        os.fchmod(output.fileno(), 0o600)
        json.dump(configuration, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def run(arguments, *, driver=None, db_root=None):
    connection = None
    try:
        if arguments.output.exists() or arguments.output.is_symlink():
            raise ResolverError("private_configuration_already_exists")
        request = load_request(arguments.request)
        key = load_contact_key(arguments.key_file)
        root = Path(db_root) if db_root is not None else preparation.only_database_root()
        contact = root / CONTACT_RELATIVE
        if not contact.is_file():
            raise ResolverError("contact_database_unavailable")
        if driver is None:
            try:
                driver = watcher.load_driver()
            except watcher.CheckError:
                raise ResolverError("isolated_sqlcipher_runtime_missing") from None
        try:
            connection = watcher.open_readonly(contact, key, driver)
        except watcher.CheckError:
            raise ResolverError("contact_database_readonly_open_failed") from None
        configuration = build_configuration(request, connection)
        save_private_configuration(arguments.output, configuration)
        for alias, _ in ALIASES:
            emit("selected_contact_resolved", alias=alias, count=1)
        emit("private_configuration_written", count=2)
        return 0
    except ResolverError as failure:
        fields = {"status": failure.status, "reason": failure.reason}
        if failure.alias is not None:
            fields["alias"] = failure.alias
        emit("configuration_stopped", **fields)
    except FileExistsError:
        emit("configuration_stopped", status="待外部条件", reason="private_configuration_already_exists")
    except Exception as failure:
        emit("configuration_stopped", status="失败", reason="resolver_operation_failed",
             error_type=type(failure).__name__)
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    return 2


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--key-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    os.umask(0o077)
    try:
        sys.exit(run(parse_args()))
    except KeyboardInterrupt:
        emit("configuration_stopped", status="未验证", reason="interrupted")
        sys.exit(130)
