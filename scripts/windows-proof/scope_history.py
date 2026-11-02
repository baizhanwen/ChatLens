"""Recover same-account selected history without reading source chat or keys.

Plans contain private paths and fixed identity digests only. All message bodies
are streamed directly from validated, committed local stores into one target
transaction; neither plans nor public errors contain a message or account value.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

from collector_private import (ensure_private_dir, read_private_json,
                               validate_local_path, write_private_json_new)
from conversation_scope import normalize_conversations
from continuous_store import StoreError
from web_data import CollectorWebData


PLAN_NAME = 'history-plan.json'
MAX_SOURCES = 100
DIGEST = re.compile(r'[0-9a-f]{64}\Z')
NONCE = re.compile(r'[0-9a-f]{32}\Z')
SHARD = re.compile(r'message/message_[0-7]\.db\Z')
EVENT_COLUMNS = 'source_key,alias,source_local_id,sent_at,sender_id,body,observed_at'


class HistoryError(ValueError):
    def __init__(self, code='scope_history_operation_failed'):
        self.code = self.reason = code
        super().__init__(code)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode('utf-8')).hexdigest()


def _path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _data_directory(path):
    path = Path(path)
    ensure_private_dir(path)
    ensure_private_dir(path / 'private')
    return path


def _descriptor(data_dir):
    """Reconstruct the producer's binding from scope metadata, never keys."""
    directory = _data_directory(data_dir)
    scope = read_private_json(directory / 'private/scope.json')
    if (not isinstance(scope, dict) or type(scope.get('schema_version')) is not int
            or scope['schema_version'] != 1 or not NONCE.fullmatch(scope.get('session_id', ''))):
        raise HistoryError('scope_history_scope_invalid')
    root = validate_local_path(Path(scope['db_root']), require_dir=True)
    root_info = root.stat()
    identity = [root_info.st_dev, root_info.st_ino]
    chats = normalize_conversations(scope['config'])
    platform = scope.get('platform')
    if platform == 'macos':
        if scope.get('root_identity') != identity:
            raise HistoryError('scope_history_account_changed')
        shards = scope.get('shards')
        if (not isinstance(shards, list) or not 1 <= len(shards) <= 8
                or shards != sorted(set(shards)) or any(not SHARD.fullmatch(x) for x in shards)):
            raise HistoryError('scope_history_scope_invalid')
        binding = {'schema_version': 1, 'platform': 'macos', 'session_id': scope['session_id'],
                   'root': str(root), 'root_identity': identity,
                   'config': {'conversations': chats}, 'shards': shards}
        account = ('macos', _path_key(root), tuple(identity))
    elif platform in (None, 'windows'):
        executable = validate_local_path(Path(scope['exe_path']))
        if executable.name.lower() not in ('wechat.exe', 'weixin.exe'):
            raise HistoryError('scope_history_scope_invalid')
        if not isinstance(scope.get('test_tokens'), dict):
            raise HistoryError('scope_history_scope_invalid')
        binding = {'schema_version': 1, 'session_id': scope['session_id'],
                   'root': str(root), 'root_identity': identity, 'executable': str(executable),
                   'config': scope['config'], 'test_tokens': scope['test_tokens']}
        account = ('windows', _path_key(root), tuple(identity), _path_key(executable))
    else:
        raise HistoryError('scope_history_platform_invalid')
    return {'directory': directory, 'account': account, 'binding': _digest(binding),
            'chats': chats, 'by_alias': {row['alias']: row for row in chats}}


def _same_account(target, source):
    if target['account'] != source['account']:
        raise HistoryError('scope_history_account_mismatch')


def write_history_plan(target_data_dir, source_data_dirs, continuity_source=None):
    """Atomically plan history from internal same-account profile directories.

    The target's scope must exist, but its store need not have been created yet.
    Zero-overlap sources are omitted. No source SQLite, message or key is read.
    """
    temporary = None
    try:
        target = _descriptor(target_data_dir)
        if isinstance(source_data_dirs, (str, bytes, Path)):
            raise HistoryError('scope_history_sources_invalid')
        supplied = list(source_data_dirs)
        if len(supplied) > MAX_SOURCES:
            raise HistoryError('scope_history_source_limit')
        target_ids = {row['chat_id'] for row in target['chats']}
        directories, seen = [], set()
        target_key = _path_key(target['directory'])
        for path in supplied:
            key = _path_key(path)
            if key == target_key or key in seen:
                continue
            seen.add(key)
            source = _descriptor(path)
            _same_account(target, source)
            if target_ids.intersection(row['chat_id'] for row in source['chats']):
                directories.append(str(source['directory']))
        continuity = None
        if continuity_source is not None:
            continuity_key = _path_key(continuity_source)
            continuity = next((path for path in directories if _path_key(path) == continuity_key), None)
            if continuity_key != target_key and continuity is None:
                # A separate continuity source may be disjoint, but may never
                # quietly refer to a different account or unreviewed directory.
                source = _descriptor(continuity_source)
                _same_account(target, source)
                if target_ids.intersection(row['chat_id'] for row in source['chats']):
                    raise HistoryError('scope_history_continuity_source_missing')
        path = target['directory'] / 'private' / PLAN_NAME
        if not directories:
            return None  # Do not discard a pre-existing pending recovery plan.
        if path.exists() or path.is_symlink():
            read_private_json(path)
        plan = {'schema_version': 1, 'nonce': secrets.token_hex(16),
                'target_binding_digest': target['binding'], 'sources': directories,
                'continuity_source': continuity}
        temporary = path.with_name('.history-plan-' + plan['nonce'] + '.json')
        write_private_json_new(temporary, plan)
        ensure_private_dir(path.parent)
        os.replace(temporary, path)
        return {'planned': True, 'source_count': len(directories),
                'continuous_source_selected': continuity is not None}
    except HistoryError:
        raise
    except Exception:
        raise HistoryError('scope_history_plan_invalid') from None
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _epochs(value):
    if (not isinstance(value, dict) or not 1 <= len(value) <= 8
            or any(not isinstance(name, str) or not SHARD.fullmatch(name) for name in value)):
        raise HistoryError('scope_history_epochs_invalid')
    for epoch in value.values():
        if (not isinstance(epoch, dict) or set(epoch) != {'file_identity', 'salt_sha256', 'key_sha256'}
                or not isinstance(epoch['file_identity'], list) or len(epoch['file_identity']) != 2
                or any(type(n) is not int or n < 0 for n in epoch['file_identity'])
                or any(not isinstance(epoch[name], str) or DIGEST.fullmatch(epoch[name]) is None
                       for name in ('salt_sha256', 'key_sha256'))):
            raise HistoryError('scope_history_epochs_invalid')


def _checkpoint(connection, descriptor):
    marker = read_private_json(descriptor['directory'] / 'private/store/collector.identity.json')
    if marker != {'schema_version': 1, 'binding_digest': descriptor['binding']}:
        raise HistoryError('scope_history_source_binding_mismatch')
    rows = connection.execute('SELECT id,binding_digest,initialized,cursors,epochs,baseline_completed_at,'
                              'last_commit_at,commits FROM checkpoint LIMIT 2').fetchall()
    if len(rows) != 1:
        raise HistoryError('scope_history_checkpoint_invalid')
    row = dict(rows[0])
    if (row['id'] != 1 or row['binding_digest'] != descriptor['binding']
            or row['initialized'] not in (0, 1) or type(row['commits']) is not int or row['commits'] < 0):
        raise HistoryError('scope_history_checkpoint_invalid')
    row['cursors'], row['epochs'] = json.loads(row['cursors']), json.loads(row['epochs'])
    if not row['initialized']:
        if (row['cursors'] or row['epochs'] or row['baseline_completed_at'] is not None
                or row['last_commit_at'] is not None or row['commits']
                or connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0]):
            raise HistoryError('scope_history_checkpoint_invalid')
        return row
    _epochs(row['epochs'])
    aliases = set(descriptor['by_alias'])
    if (not isinstance(row['cursors'], dict) or set(row['cursors']) != set(row['epochs'])
            or any(not isinstance(rows, dict) or set(rows) != aliases for rows in row['cursors'].values())):
        raise HistoryError('scope_history_checkpoint_invalid')
    for cursors in row['cursors'].values():
        for cursor in cursors.values():
            if (not isinstance(cursor, dict) or set(cursor) != {'local_id', 'present'}
                    or type(cursor['local_id']) is not int or not 0 <= cursor['local_id'] < 2**63
                    or type(cursor['present']) is not bool
                    or not cursor['present'] and cursor['local_id'] != 0):
                raise HistoryError('scope_history_checkpoint_invalid')
    for field in ('baseline_completed_at', 'last_commit_at'):
        if not isinstance(row[field], str) or datetime.fromisoformat(row[field]).utcoffset() != timezone.utc.utcoffset(None):
            raise HistoryError('scope_history_checkpoint_invalid')
    return row


def _sources(plan, target, epochs):
    selected = {row['chat_id']: row['alias'] for row in target['chats']}
    sources = []
    for path in plan['sources']:
        descriptor = _descriptor(path)
        _same_account(target, descriptor)
        aliases = {row['alias']: selected[row['chat_id']] for row in descriptor['chats']
                   if row['chat_id'] in selected}
        if not aliases:
            continue
        data = CollectorWebData(descriptor['directory'])
        with data._open(allow_uninitialized=True) as (connection, _):
            snapshot = _checkpoint(connection, descriptor)
        if not snapshot['initialized']:
            continue
        if snapshot['epochs'] != epochs:
            raise HistoryError('scope_history_source_epoch_changed')
        sources.append({'descriptor': descriptor, 'data': data, 'snapshot': snapshot, 'aliases': aliases})
    return sources


def _events(sources, target):
    for source in sources:
        descriptor, expected = source['descriptor'], source['snapshot']
        with source['data']._open() as (connection, _):
            if _checkpoint(connection, descriptor) != expected:
                raise HistoryError('scope_history_source_snapshot_changed')
            aliases = source['aliases']
            placeholders = ','.join('?' for _ in aliases)
            cursor = connection.execute('SELECT ' + EVENT_COLUMNS + ' FROM messages WHERE alias IN ('
                                         + placeholders + ') ORDER BY rowid', list(aliases))
            # Each fetch remains bounded; a complete large local history may
            # span many batches without being silently truncated at a limit.
            deadline = time.monotonic() + 1.5
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            while True:
                deadline = time.monotonic() + 1.5
                rows = cursor.fetchmany(32)
                if not rows:
                    break
                for row in rows:
                    event = dict(row)
                    old_alias = event['alias']
                    local_id = event['source_local_id']
                    chat_id = descriptor['by_alias'][old_alias]['chat_id']
                    matches = [relative for relative, epoch in expected['epochs'].items()
                               if _digest([relative, chat_id, local_id, epoch]) == event['source_key']]
                    # An existing target may have imported a later committed
                    # row without advancing its own reader checkpoint. That
                    # row remains valid history when this target becomes the
                    # donor for the next scope; source identity proves its
                    # origin independently of the donor's lower poll cursor.
                    if len(matches) != 1 or type(local_id) is not int or local_id <= 0:
                        raise HistoryError('scope_history_source_message_invalid')
                    event['alias'] = aliases[old_alias]
                    yield event
            cursor.close()
            if _descriptor(descriptor['directory'])['binding'] != descriptor['binding']:
                raise HistoryError('scope_history_source_snapshot_changed')
            _same_account(target, _descriptor(descriptor['directory']))
        # The read-only context's file-identity checks finish before the target
        # transaction can commit any streamed rows from this source.
    current_target = _descriptor(target['directory'])
    if current_target['binding'] != target['binding'] or current_target['account'] != target['account']:
        raise HistoryError('scope_history_target_changed')


def _consume(path, plan):
    before = path.lstat()
    current = read_private_json(path)
    after = path.lstat()
    if (current != plan or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)):
        raise HistoryError('scope_history_plan_changed')
    path.unlink()


def apply_history_plan(store, cursors, epochs, *, initialize=False):
    """Apply one private plan and return the standard destination snapshot.

    New stores inherit continuity cursors only from the explicit prior active
    source. Existing stores only merge saved rows; their checkpoints and rowids
    are preserved exactly. Repeating a committed-but-unconsumed plan is safe.
    """
    directory = getattr(store, 'directory', None)
    if directory is None:
        if initialize:
            store.initialize(cursors, epochs)
        return store.snapshot()
    path = Path(directory).parent / PLAN_NAME
    if not path.exists() and not path.is_symlink():
        if initialize:
            store.initialize(cursors, epochs)
        return store.snapshot()
    try:
        target = _descriptor(Path(directory).parent.parent)
        plan = read_private_json(path)
        if (not isinstance(plan, dict) or set(plan) != {'schema_version', 'nonce', 'target_binding_digest',
                                                     'sources', 'continuity_source'}
                or type(plan['schema_version']) is not int or plan['schema_version'] != 1
                or not isinstance(plan['nonce'], str) or NONCE.fullmatch(plan['nonce']) is None
                or plan['target_binding_digest'] != target['binding']
                or target['binding'] != store.binding_digest
                or not isinstance(plan['sources'], list) or not 1 <= len(plan['sources']) <= MAX_SOURCES
                or any(not isinstance(p, str) for p in plan['sources'])
                or len(set(_path_key(p) for p in plan['sources'])) != len(plan['sources'])
                or any(_path_key(p) == _path_key(target['directory']) for p in plan['sources'])
                or plan['continuity_source'] is not None and plan['continuity_source'] not in plan['sources']):
            raise HistoryError('scope_history_plan_invalid')
        _epochs(epochs)
        sources = _sources(plan, target, epochs)
        inherited = copy.deepcopy(cursors)
        if initialize and plan['continuity_source'] is not None:
            source = next((item for item in sources if str(item['descriptor']['directory']) == plan['continuity_source']), None)
            if source is not None:
                for old_alias, new_alias in source['aliases'].items():
                    for relative in epochs:
                        old = source['snapshot']['cursors'][relative][old_alias]
                        current = inherited[relative][new_alias]
                        if old['local_id'] > current['local_id'] or old['present'] and not current['present']:
                            raise HistoryError('scope_history_continuity_invalid')
                        inherited[relative][new_alias] = copy.deepcopy(old)
        events = _events(sources, target)
        try:
            if initialize:
                result = store.initialize(inherited, epochs, events=events)
            else:
                store.merge_history(cursors, events, epochs)
                result = store.snapshot()
        finally:
            events.close()
        _consume(path, plan)
        return result
    except HistoryError:
        raise
    except StoreError:
        raise
    except Exception:
        raise HistoryError('scope_history_operation_failed') from None
