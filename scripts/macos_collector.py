#!/usr/bin/env python3
"""Selected-scope macOS collector using a new, private durable checkpoint.

Only explicit legacy configuration/key inputs are imported. The former finite
test's HMAC checkpoint is never read or modified. First use establishes a new
current-source baseline; later runs resume this new store without rebaselining.
No application launch, attachment, message sending, model or network call lives
in this module. The native helper is invoked in metadata-only ``wechat`` mode.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import time

WINDOWS_MODULES = Path(__file__).resolve().parent / 'windows-proof'
if str(WINDOWS_MODULES) not in sys.path:
    sys.path.insert(0, str(WINDOWS_MODULES))

from collector_private import ensure_private_dir, read_private_json, validate_local_path, write_private_json_new
from collector_web_meta import WebMetadata
from continuous_reader import ContinuousReader
from continuous_store import CollectorStore
from prepare_wechat_read_test import key_validates_page
from windows_selected_engine import ALIASES, ID

MESSAGE_RELATIVE = re.compile(r'message/message_([0-7])\.db\Z')
HEX_KEY = re.compile(r'[0-9a-fA-F]{64}\Z')
VERSION_TEXT = re.compile(r'[0-9A-Za-z ._-]{1,40}\Z')
SAFE_CODE = re.compile(r'[a-z0-9_]{1,100}\Z')
CONTACT = 'contact/contact.db'


class MacCollectorError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _safe_code(error):
    value = getattr(error, 'code', None)
    return value if isinstance(value, str) and SAFE_CODE.fullmatch(value) else 'macos_collector_operation_failed'


def _config(source):
    try:
        rows = source['conversations']
        if not isinstance(rows, list) or len(rows) != 2:
            raise ValueError
        result = []
        for alias in ALIASES:
            found = [row for row in rows if isinstance(row, dict) and row.get('alias') == alias]
            if len(found) != 1:
                raise ValueError
            chat_id = found[0]['chat_id']
            if (not isinstance(chat_id, str) or ID.fullmatch(chat_id) is None
                    or (alias == 'group_test') != chat_id.endswith('@chatroom')):
                raise ValueError
            table = 'Msg_' + hashlib.md5(chat_id.encode()).hexdigest()
            if found[0].get('table', table) != table:
                raise ValueError
            result.append({'alias': alias, 'chat_id': chat_id, 'table': table})
        if result[0]['chat_id'] == result[1]['chat_id']:
            raise ValueError
        return {'conversations': result}
    except (KeyError, TypeError, ValueError):
        raise MacCollectorError('macos_selected_scope_invalid') from None


def _keys(document, *, messages):
    try:
        rows = document['keys']
        if not isinstance(rows, dict):
            raise ValueError
        if messages and any(not isinstance(relative, str)
                            or relative.startswith('message/') and MESSAGE_RELATIVE.fullmatch(relative) is None
                            for relative in rows):
            raise ValueError
        selected = sorted(relative for relative in rows if MESSAGE_RELATIVE.fullmatch(relative)) if messages else [CONTACT]
        if messages and not 1 <= len(selected) <= 8:
            raise ValueError
        result = {}
        for relative in selected:
            key = rows[relative]['enc_key']
            if not isinstance(key, str) or HEX_KEY.fullmatch(key) is None:
                raise ValueError
            result[relative] = key.lower()
        return result
    except (KeyError, TypeError, ValueError):
        raise MacCollectorError('macos_selected_keys_invalid') from None


def _root_identity(root):
    validate_local_path(root, require_dir=True)
    info = root.stat()
    return [int(info.st_dev), int(info.st_ino)]


def _authenticate_keys(root, keys):
    """Bind explicitly supplied keys to this source, reading page one only."""
    directory = validate_local_path(root / 'message', require_dir=True)
    entries = list(itertools.islice(directory.iterdir(), 65))
    if len(entries) > 64:
        raise MacCollectorError('macos_source_directory_entry_limit')
    present = {'message/' + entry.name for entry in entries if re.fullmatch(r'message_[0-9]+\.db', entry.name)}
    if present != set(keys) - {CONTACT}:
        # Discovery never expands authorization or imports an extra key. A
        # missing/extra source shard requires an explicitly initialized map.
        raise MacCollectorError('macos_source_shards_mismatch')
    for relative, key in keys.items():
        path = validate_local_path(root / relative)
        before = path.stat()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            page = stream.read(4096)
        after = path.stat()
        if ((before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or not key_validates_page(bytes.fromhex(key), page)):
            raise MacCollectorError('macos_selected_database_key_authentication_failed')


@dataclass(frozen=True, repr=False)
class MacSession:
    data_dir: Path
    root: Path
    config: dict
    keys: dict
    binding: dict
    shards: tuple
    client_version: str


def _session(data_dir, scope, document):
    try:
        if (not isinstance(scope, dict) or type(scope.get('schema_version')) is not int
                or scope['schema_version'] != 1 or scope.get('platform') != 'macos'
                or not isinstance(scope.get('session_id'), str)
                or re.fullmatch(r'[0-9a-f]{32}', scope['session_id']) is None):
            raise MacCollectorError('macos_session_scope_invalid')
        root = validate_local_path(Path(scope['db_root']), require_dir=True)
        identity = _root_identity(root)
        if scope.get('root_identity') != identity:
            raise MacCollectorError('macos_session_account_identity_changed')
        config = _config(scope['config'])
        version = scope['client_version']
        if not isinstance(version, str) or VERSION_TEXT.fullmatch(version) is None:
            raise MacCollectorError('macos_session_version_invalid')
        if (not isinstance(document, dict) or type(document.get('schema_version')) is not int
                or document['schema_version'] != 1 or document.get('platform') != 'macos'):
            raise MacCollectorError('macos_session_keys_invalid')
        keys = _keys(document, messages=True)
        keys.update(_keys(document, messages=False))
        if set(document['keys']) != set(keys) or scope.get('shards') != sorted(set(keys) - {CONTACT}):
            raise MacCollectorError('macos_session_shards_mismatch')
        _authenticate_keys(root, keys)
        binding = {'schema_version': 1, 'platform': 'macos', 'session_id': scope['session_id'],
                   'root': str(root), 'root_identity': identity, 'config': config, 'shards': scope['shards']}
        return MacSession(data_dir, root, config, keys, binding, tuple(scope['shards']), version)
    except MacCollectorError:
        raise
    except Exception:
        raise MacCollectorError('macos_session_inputs_unavailable') from None


def prepare_session(data_dir, *, db_root=None, selected_config=None, key_file=None,
                    message_key_file=None, contact_key_file=None, client_version=None):
    """Import one explicit legacy selection once; never import its checkpoint.

    ``key_file`` accepts the old merged ``{keys:{relative:{enc_key}}}`` shape.
    Separate message/contact files are also accepted. Once data_dir exists its
    private scope and keys are authoritative; missing files fail without reset.
    """
    data_dir = Path(data_dir)
    try:
        if data_dir.exists() or data_dir.is_symlink():
            ensure_private_dir(data_dir)
            private = ensure_private_dir(data_dir / 'private')
            ensure_private_dir(private / 'store')
            session = _session(data_dir, read_private_json(private / 'scope.json'),
                               read_private_json(private / 'keys.json'))
            if db_root is not None and validate_local_path(Path(db_root), require_dir=True) != session.root:
                raise MacCollectorError('macos_session_account_mismatch')
            return session
        if db_root is None or selected_config is None or not (message_key_file or key_file) or not (contact_key_file or key_file):
            raise MacCollectorError('macos_initial_configuration_required')
        root = validate_local_path(Path(db_root), require_dir=True)
        config = _config(read_private_json(Path(selected_config)))
        message_document = read_private_json(Path(message_key_file or key_file))
        contact_path = Path(contact_key_file or key_file)
        contact_document = message_document if contact_path == Path(message_key_file or key_file) else read_private_json(contact_path)
        keys = _keys(message_document, messages=True)
        keys.update(_keys(contact_document, messages=False))
        version = client_version if client_version is not None else 'unknown'
        scope = {'schema_version': 1, 'platform': 'macos', 'session_id': secrets.token_hex(16),
                 'db_root': str(root), 'root_identity': _root_identity(root), 'config': config,
                 'shards': sorted(set(keys) - {CONTACT}), 'client_version': version}
        document = {'schema_version': 1, 'platform': 'macos',
                    'keys': {relative: {'enc_key': value} for relative, value in keys.items()}}
        session = _session(data_dir, scope, document)  # Verify before any persistent creation.
        ensure_private_dir(data_dir, create=True)
        private = ensure_private_dir(data_dir / 'private', create=True)
        ensure_private_dir(private / 'store', create=True)
        write_private_json_new(private / 'keys.json', document)
        write_private_json_new(private / 'scope.json', scope)
        return session
    except MacCollectorError:
        raise
    except Exception:
        raise MacCollectorError('macos_session_prepare_failed') from None


class MacNativeProbe:
    """Fail-closed, metadata-only subprocess probe of the original Mac client."""
    def __init__(self, probe, *, expected_version, run=subprocess.run):
        self.path = validate_local_path(Path(probe))
        if not os.access(self.path, os.X_OK):
            raise MacCollectorError('macos_native_probe_not_executable')
        info = self.path.stat()
        self.identity = (info.st_dev, info.st_ino)
        if not isinstance(expected_version, str) or VERSION_TEXT.fullmatch(expected_version) is None or expected_version == 'unknown':
            raise MacCollectorError('macos_native_probe_expected_version_required')
        self.expected_version = expected_version
        self.process_identity = None
        self.run = run

    def __call__(self):
        result = {'probe_succeeded': False, 'running': None, 'frontmost': None, 'screen_locked': None}
        try:
            validate_local_path(self.path)
            info = self.path.stat()
            if (info.st_dev, info.st_ino) != self.identity:
                return result
            call = self.run([str(self.path), 'wechat'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            timeout=5, check=False, shell=False)
            if call.returncode != 0 or not isinstance(call.stdout, bytes) or len(call.stdout) > 128 * 1024:
                return result
            source = json.loads(call.stdout)
            if not isinstance(source, dict):
                return result
            process_id, started = source.get('process_id'), source.get('process_started_at')
            version = source.get('version')
            if (type(source.get('matching_applications')) is not int or source['matching_applications'] != 1
                    or type(process_id) is not int or process_id <= 0
                    or type(started) not in (int, float) or not math.isfinite(started) or started <= 0
                    or source.get('screen_state_available') is not True or source.get('running') is not True
                    or type(source.get('frontmost')) is not bool or type(source.get('screen_locked')) is not bool
                    or version != self.expected_version):
                return result
            identity = (process_id, started, version)
            if self.process_identity is not None and identity != self.process_identity:
                return result
            self.process_identity = identity
            result['probe_succeeded'] = True
            for field in ('running', 'frontmost', 'screen_locked'):
                if type(source.get(field)) is bool:
                    result[field] = source[field]
            result['client_version'] = version
            return result
        except Exception:
            return result


class MacCollector:
    def __init__(self, session, native_state_callable, driver=None):
        self.session, self.native = session, native_state_callable
        self.store = self.reader = self.metadata = None
        self.resumed = False
        self.baseline_completed_at = None
        self.added = self.duplicates = self.busy_retries = 0
        self.closed = False
        try:
            if not isinstance(session, MacSession) or not callable(native_state_callable):
                raise MacCollectorError('macos_collector_inputs_invalid')
            if driver is None:
                try:
                    import sqlcipher3.dbapi2 as driver
                    import zstandard  # Required by the bounded shared decoder.
                except ImportError:
                    raise MacCollectorError('macos_isolated_sqlcipher_runtime_missing') from None
            self.driver = driver
            self._preflight()
            self.store = CollectorStore(session.data_dir / 'private' / 'store', session.binding)
            self.metadata = WebMetadata(session.data_dir / 'private', self.store.binding_digest, _now())
            self._heartbeat('starting')
            self.reader = ContinuousReader(driver, session.root, session.keys, session.config, self.store,
                                           self.native, shards=session.shards)
            self.resumed = self.reader.resumed
            self.baseline_completed_at = self.reader.baseline_completed_at
            self._catalog()
            self._heartbeat('collecting')
        except BaseException as error:
            self.close(reason=_safe_code(error))
            raise

    def _preflight(self):
        for directory in (self.session.data_dir, self.session.data_dir / 'private', self.session.data_dir / 'private' / 'store'):
            ensure_private_dir(directory)
        if _root_identity(self.session.root) != self.session.binding['root_identity']:
            raise MacCollectorError('macos_session_account_identity_changed')

    def _catalog(self):
        try:
            self.metadata.refresh_catalog(self.driver, self.session.root, self.session.keys, self.session.config,
                                          self.store.sender_ids_by_alias())
        except Exception:
            self.metadata.failed = True  # Optional labels never stop durable message commits.

    def _heartbeat(self, stage, reason=None):
        try:
            self.metadata.status(stage, self.store.stats()['total_messages'], self.added, self.resumed, reason)
        except Exception:
            self.metadata.failed = True

    def poll(self):
        if self.closed:
            raise MacCollectorError('macos_collector_closed')
        state = 'collecting'
        try:
            self._preflight()
            sample = self.native()
            if sample.get('probe_succeeded') is True and sample.get('running') is False:
                raise MacCollectorError('macos_wechat_not_running_restart_required')
            result = self.reader.poll()
            self.busy_retries = 0
            self.added += result['new_messages']
            self.duplicates += result['duplicates']
            if result['new_messages']:
                self._catalog()
        except Exception as error:
            code = _safe_code(error)
            if code in ('continuous_native_state_not_ready', 'continuous_native_state_unavailable'):
                state = 'paused'
            elif code == 'continuous_source_busy' and self.busy_retries < 5:
                self.busy_retries += 1
                state = 'paused'
            else:
                self.close(reason=code)
                raise
            result = {'new_messages': 0, 'duplicates': 0, 'total_messages': self.store.stats()['total_messages']}
        self._heartbeat(state)
        return {'state': state, 'new_messages': result['new_messages'], 'duplicates': result['duplicates'],
                'total_messages': result['total_messages'], 'resumed_from_checkpoint': self.resumed}

    def close(self, reason=None):
        if self.closed:
            return
        self.closed = True
        if self.reader is not None:
            self.reader.close()
        if self.store is not None:
            if self.metadata is not None:
                self._heartbeat('error' if reason else 'stopped', reason)
            self.store.close()  # Final status belongs to this writer while its lock is still held.

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        self.close(reason=_safe_code(error) if error is not None else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--db-root', type=Path)
    parser.add_argument('--selected-config', type=Path)
    parser.add_argument('--key-file', type=Path)
    parser.add_argument('--message-key-file', type=Path)
    parser.add_argument('--contact-key-file', type=Path)
    parser.add_argument('--client-version')
    parser.add_argument('--native-probe', type=Path, default=Path(__file__).resolve().parent / 'native/bin/macos-probe')
    args = parser.parse_args(argv)
    collector, handlers, stopping = None, {}, False
    def stop(signum, frame):
        nonlocal stopping
        stopping = True
    try:
        if sys.platform != 'darwin':
            raise MacCollectorError('macos_collector_launcher_required')
        session = prepare_session(args.data_dir, db_root=args.db_root, selected_config=args.selected_config,
                                  key_file=args.key_file, message_key_file=args.message_key_file,
                                  contact_key_file=args.contact_key_file, client_version=args.client_version)
        for signum in (signal.SIGINT, signal.SIGTERM):
            handlers[signum] = signal.signal(signum, stop)
        collector = MacCollector(session, MacNativeProbe(args.native_probe, expected_version=session.client_version))
        print('READY：仅持续采集原先选择的两个会话的新文字；按 Ctrl+C 正常停止。', flush=True)
        while not stopping:
            result = collector.poll()
            print('state=%s total_messages=%d' % (result['state'], result['total_messages']), flush=True)
            time.sleep(1)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print('reason=' + _safe_code(error), flush=True)
        return 2
    finally:
        if collector is not None:
            collector.close()
        for signum, previous in handlers.items():
            signal.signal(signum, previous)


if __name__ == '__main__':
    raise SystemExit(main())
