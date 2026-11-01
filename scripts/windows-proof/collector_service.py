"""Local selected-text collection; durable cursors, no model or network calls."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import sys
import tempfile
import time

from collector_private import (ensure_private_dir, read_private_json,
                               validate_local_path, write_private_json_new)
from continuous_reader import ContinuousReader, _config
from continuous_store import CollectorStore
from windows_selected_engine import ID, SHARDS

STAGES = {'starting', 'collecting', 'paused', 'stopping', 'stopped', 'error'}
RELATIVES = SHARDS + ('contact/contact.db',)


class CollectorError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_code(error):
    value = getattr(error, 'code', None)
    return value if isinstance(value, str) and re.fullmatch(r'[a-z0-9_]{1,100}', value) else 'collector_operation_failed'


def write_public(path, document):
    """Atomic, counts-only local status; caller supplies only fixed schema data."""
    path = Path(path)
    validate_local_path(path.parent, require_dir=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise CollectorError('collector_public_output_unsafe')
    descriptor, temporary = tempfile.mkstemp(prefix='.collector-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(document, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Heartbeat:
    def __init__(self, output):
        self.path = Path(output) / 'status.json'
        self.sequence = 0
        self.metadata = None

    def emit(self, stage, total=0, added=0, resumed=False):
        if stage not in STAGES or type(total) is not int or type(added) is not int:
            raise CollectorError('collector_status_invalid')
        self.sequence += 1
        write_public(self.path, {'schema_version': 1, 'kind': 'chatlens_collector_heartbeat',
                                'sequence': self.sequence, 'stage': stage, 'recorded_at_utc': now(),
                                'total_messages': total, 'new_messages_this_run': added,
                                'resumed_from_checkpoint': bool(resumed)})
        if self.metadata is not None:
            try:
                self.metadata.status(stage, total, added, resumed)
            except Exception:
                self.metadata.failed = True  # Stale Web status must not stop durable capture.


def _same_path(first, second):
    return os.path.normcase(os.path.abspath(str(first))) == os.path.normcase(os.path.abspath(str(second)))


def validate_scope(scope, keys):
    if (not isinstance(scope, dict) or scope.get('schema_version') != 1
            or not isinstance(keys, dict) or keys.get('schema_version') != 1
            or set(keys.get('keys', {})) != set(RELATIVES)):
        raise CollectorError('collector_configuration_invalid')
    root = validate_local_path(Path(scope['db_root']), require_dir=True)
    exe = validate_local_path(Path(scope['exe_path']))
    if not _same_path(root, keys.get('db_root', '')) or not _same_path(exe, keys.get('exe_path', '')):
        raise CollectorError('collector_configuration_account_mismatch')
    if exe.name.lower() not in ('weixin.exe', 'wechat.exe'):
        raise CollectorError('collector_executable_unsupported')
    chats = _config(scope.get('config'))
    managed = scope.get('scope_kind') == 'selected_conversations'
    expected = {}
    for row in scope['config']['conversations']:
        value = row.get('expected_sender_id')
        if managed and value is None:
            continue
        if not isinstance(value, str) or ID.fullmatch(value) is None:
            raise CollectorError('collector_sender_configuration_invalid')
        expected[row['alias']] = value
    tokens = scope.get('test_tokens')
    if managed:
        if tokens != {} or expected:
            raise CollectorError('collector_managed_test_plan_invalid')
    elif (not isinstance(tokens, dict) or set(tokens) != set(expected)
            or any(not isinstance(rows, list) or len(rows) != 3 for rows in tokens.values())
            or any(not isinstance(token, str) or re.fullmatch(r'[A-Z0-9_-]{8,96}', token) is None
                   for rows in tokens.values() for token in rows)
            or len({token for rows in tokens.values() for token in rows}) != 6):
        raise CollectorError('collector_test_plan_invalid')
    values = {}
    for relative in RELATIVES:
        value = keys['keys'][relative].get('enc_key') if isinstance(keys['keys'][relative], dict) else None
        if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{64}', value) is None:
            raise CollectorError('collector_key_configuration_invalid')
        values[relative] = value.lower()
    if not isinstance(scope.get('session_id'), str) or re.fullmatch(r'[0-9a-f]{32}', scope['session_id']) is None:
        raise CollectorError('collector_session_identity_invalid')
    return root, exe, values, expected


def prepare_session(data_dir, selected_config=None):
    """Copy only the previously authorized two-conversation scope on first use."""
    data_dir = Path(data_dir)
    existed = data_dir.exists() or data_dir.is_symlink()
    if existed:
        ensure_private_dir(data_dir)
        private = ensure_private_dir(data_dir / 'private')
        scope = read_private_json(private / 'scope.json')
        keys = read_private_json(private / 'keys.json')
        ensure_private_dir(private / 'store')
    else:
        if selected_config is None:
            raise CollectorError('collector_initial_configuration_required')
        selected_config = validate_local_path(Path(selected_config))
        if selected_config.name != 'selected-config.json':
            raise CollectorError('collector_selected_configuration_filename_invalid')
        original = read_private_json(selected_config)
        keys = read_private_json(selected_config.with_name('selected-keys.json'))
        if not isinstance(original, dict) or not _same_path(original.get('db_root', ''), keys.get('db_root', '')):
            raise CollectorError('collector_configuration_account_mismatch')
        nonce = secrets.token_hex(4).upper()
        scope = {'schema_version': 1, 'session_id': secrets.token_hex(16),
                 'db_root': original.get('db_root'), 'exe_path': keys.get('exe_path'),
                 'config': original.get('config'),
                 'test_tokens': {alias: ['CLC-%s-%s-%d' % (letter, nonce, n) for n in (1, 2, 3)]
                                 for alias, letter in (('private_test', 'P'), ('group_test', 'G'))}}
        validate_scope(scope, keys)  # Validate before creating any persistent session.
        ensure_private_dir(data_dir, create=True)
        private = ensure_private_dir(data_dir / 'private', create=True)
        ensure_private_dir(private / 'store', create=True)
        write_private_json_new(private / 'keys.json', keys)
        write_private_json_new(private / 'scope.json', scope)
    root, exe, values, expected = validate_scope(scope, keys)
    identity = root.stat()
    binding = {'schema_version': 1, 'session_id': scope['session_id'], 'root': str(root),
               'root_identity': [identity.st_dev, identity.st_ino], 'executable': str(exe),
               'config': scope['config'], 'test_tokens': scope['test_tokens']}
    return scope, root, exe, values, expected, binding


def show_plan(scope, summary, resumed):
    print('READY：持续采集已启动；本地保存所选私聊和群的新文字。按一次 Ctrl+C 正常停止。', flush=True)
    print('恢复时使用同一个包和 collector-data；不会重新建立旧消息基线。', flush=True)
    if scope.get('scope_kind') == 'selected_conversations':
        print('当前关注 %d 个会话；各会话独立保存进度，不自动扩大范围。' % len(scope['config']['conversations']), flush=True)
    elif resumed:
        print('已从持久检查点恢复；不要重发已发送的编号，停机消息会自动补采。', flush=True)
    else:
        print('首次验收：现在再让同一位测试朋友发送下面前四条，每条各一次。', flush=True)
        for index in (0, 1):
            for alias, title in (('private_test', '私聊'), ('group_test', '群聊')):
                print(title + '：' + scope['test_tokens'][alias][index], flush=True)
        print('以下两条仅在确认本程序已停止后发送；发送后重开同一个 Start-Collector.cmd：', flush=True)
        for alias, title in (('private_test', '私聊'), ('group_test', '群聊')):
            print(title + '：' + scope['test_tokens'][alias][2], flush=True)
    print('测试码只显示在本机；private 与 collector-data 不要回传。', flush=True)


def collect_loop(reader, store, native, stop_requested, heartbeat, report, *, sleep=time.sleep, clock=time.monotonic, preflight=None, on_commit=None):
    failures = 0
    last_notice, last_stage = clock() - 10, None
    while not stop_requested():
        if preflight is not None:
            preflight()
        state = native.sample()
        if state.get('running') is not True:
            raise CollectorError('collector_source_process_unavailable_restart_required')
        stage = 'collecting'
        pause_reason = None
        try:
            result = reader.poll()
            failures = 0
            report['new_messages_this_run'] += result['new_messages']
            report['duplicate_events_skipped_this_run'] += result['duplicates']
            report['total_messages'] = result['total_messages']
            if result['new_messages'] and on_commit is not None:
                on_commit()
        except Exception as error:
            code = safe_code(error)
            if code in ('continuous_native_state_not_ready', 'continuous_native_state_unavailable'):
                stage = 'paused'
                pause_reason = 'native'
                report['paused_polls'] += 1
            elif code == 'continuous_source_busy' and failures < 5:
                failures += 1
                report['busy_retries'] += 1
                stage = 'paused'
                pause_reason = 'busy'
            else:
                raise
        heartbeat.emit(stage, report['total_messages'], report['new_messages_this_run'], report['resumed_from_checkpoint'])
        if stage != last_stage or clock() - last_notice >= 10:
            notice = ('数据库忙，保留检查点后重试。' if pause_reason == 'busy' else
                      '已暂停，等待微信回到后台且屏幕解锁。' if stage == 'paused' else '持续采集中。')
            print(notice +
                  ' 本次新增 %d 条；本地累计 %d 条。' % (report['new_messages_this_run'], report['total_messages']), flush=True)
            last_notice, last_stage = clock(), stage
        sleep(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--selected-config', type=Path)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--creation-time', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--web-metadata', action='store_true')
    parser.add_argument('--verify-active-account', action='store_true')
    args = parser.parse_args()
    report = {'schema_version': 1, 'stage': 'continuous_collection', 'started_at_utc': now(),
              'status': '未验证', 'reason': 'not_started', 'scope': 'two_previously_selected_conversations_new_text_only',
              'target_client_version': '4.1.15.56', 'python_version': sys.version.split()[0],
              'resumed_from_checkpoint': False, 'restored_messages': 0, 'new_messages_this_run': 0,
              'duplicate_events_skipped_this_run': 0, 'total_messages': 0, 'paused_polls': 0, 'busy_retries': 0,
              'checkpoint_saved': False, 'normal_stop_requested': False, 'messages_sent_by_tool': 0, 'cloud_calls': 0,
              'source_database_records_modified': False, 'source_process_modified': False,
              'source_shm_may_receive_normal_sqlite_lock_metadata': True, 'strict_end_to_end_latency_measured': False,
              'private_acl_verified': False, 'controlled_summary_before': None, 'controlled_summary_after': None}
    store = reader = None
    heartbeat = None
    metadata = None
    try:
        if sys.platform != 'win32':
            raise CollectorError('windows_collector_launcher_required')
        validate_local_path(args.output, require_dir=True)
        heartbeat = Heartbeat(args.output)
        heartbeat.emit('starting')
        scope, root, exe, keys, expected, binding = prepare_session(args.data_dir, args.selected_config)
        account_probe = None
        account_checked = [float('-inf')]
        def verify_account():
            if account_probe is not None and time.monotonic() - account_checked[0] >= 3:
                result = account_probe()
                if result.get('state') != 'matched' or result.get('matched') is not True:
                    raise CollectorError('collector_active_account_not_verified')
                account_checked[0] = time.monotonic()
        if args.verify_active_account:
            from windows_account_probe import WindowsAccountProbe
            account_probe = WindowsAccountProbe(args.pid, args.creation_time, str(exe), str(root))
            verify_account()
        if scope.get('scope_kind') == 'selected_conversations':
            report['scope'] = 'explicitly_selected_conversations_new_text_only'
            report['selected_conversation_count'] = len(scope['config']['conversations'])
        report['private_acl_verified'] = True
        import windows_keyproof_core as proof
        import windows_keyproof_platform as platform
        from windows_selected_native import NativeState, detect_shm_preconditions
        for relative in RELATIVES:
            if not proof.page_authenticates(bytes.fromhex(keys[relative]), platform.read_page(root / relative)):
                raise CollectorError('collector_database_key_authentication_failed')
        detect_shm_preconditions(root, list(RELATIVES))
        import sqlcipher3.dbapi2 as driver
        store = CollectorStore(args.data_dir / 'private' / 'store', binding)
        report['restored_messages'] = report['total_messages'] = store.stats()['total_messages']
        report['controlled_summary_before'] = (store.controlled_summary(scope['test_tokens'], expected)
                                               if scope.get('scope_kind') != 'selected_conversations' else None)
        if args.web_metadata:
            from collector_web_meta import WebMetadata
            hint_file = args.data_dir / 'private' / 'identity-hint.json'
            hint = read_private_json(hint_file) if hint_file.exists() or hint_file.is_symlink() else None
            metadata = WebMetadata(args.data_dir / 'private', store.binding_digest, report['started_at_utc'],
                                   **({'identity_hint': hint} if hint is not None else {}))
            heartbeat.metadata = metadata
            heartbeat.emit('starting', report['total_messages'])
            metadata.ensure_catalog(scope['config'])
            try:
                metadata.refresh_catalog(driver, root, keys, scope['config'], store.sender_ids_by_alias(include_unknown=True))
            except Exception:
                metadata.failed = True
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # Parent sends our local stop request.
        with NativeState(args.pid, str(exe), args.creation_time) as native:
            reader = ContinuousReader(driver, root, keys, scope['config'], store, native.sample)
            report['resumed_from_checkpoint'] = reader.resumed
            report['baseline_completed_at'] = reader.baseline_completed_at
            heartbeat.emit('collecting', report['total_messages'], resumed=reader.resumed)
            show_plan(scope, report['controlled_summary_before'], reader.resumed)
            def preflight():
                verify_account()
                ensure_private_dir(args.data_dir)
                ensure_private_dir(args.data_dir / 'private')
                ensure_private_dir(args.data_dir / 'private' / 'store')
            def on_commit():
                if metadata is not None:
                    try:
                        metadata.ensure_catalog(scope['config'])
                        metadata.refresh_catalog(driver, root, keys, scope['config'], store.sender_ids_by_alias(include_unknown=True))
                    except Exception:
                        metadata.failed = True
            collect_loop(reader, store, native, lambda: (args.output / 'stop.request').exists(), heartbeat, report,
                         preflight=preflight, on_commit=on_commit)
            report['normal_stop_requested'] = True
            report['status'], report['reason'] = '通过', 'collector_stopped_with_committed_checkpoint'
    except BaseException as error:
        report['status'], report['reason'] = '失败', safe_code(error)
    finally:
        if reader is not None:
            reader.close()
        if store is not None:
            try:
                snapshot = store.snapshot()
                report['checkpoint_saved'] = snapshot['initialized']
                report['last_commit_at_utc'] = snapshot['last_commit_at']
                report['total_messages'] = store.stats()['total_messages']
                report['controlled_summary_after'] = (store.controlled_summary(scope['test_tokens'], expected)
                                                      if scope.get('scope_kind') != 'selected_conversations' else None)
            except Exception:
                report['status'], report['reason'] = '失败', 'collector_final_checkpoint_query_failed'
            if metadata is not None:
                try:
                    metadata.status('stopped' if report['status'] == '通过' else 'error', report['total_messages'],
                                    report['new_messages_this_run'], report['resumed_from_checkpoint'], report['reason'])
                except Exception:
                    metadata.failed = True
                heartbeat.metadata = None  # Publish final state before releasing the writer lock.
            store.close()
        report['finished_at_utc'] = now()
        if metadata is not None:
            report['web_metadata_status'] = '未验证' if metadata.failed else '通过'
        try:
            write_public(args.output / 'result-to-send.json', report)
            if heartbeat is not None:
                heartbeat.emit('stopped' if report['status'] == '通过' else 'error', report['total_messages'],
                               report['new_messages_this_run'], report['resumed_from_checkpoint'])
        except Exception:
            print('结果保存失败；请反馈这一固定状态，私人目录留在本机。', flush=True)
            return 2
    print('已停止；reason=' + report['reason'], flush=True)
    print('仅回传本次 result-to-send.json 与 supervisor.json；collector-data/private 留在本机。', flush=True)
    return 0 if report['status'] == '通过' else 2


if __name__ == '__main__':
    raise SystemExit(main())
