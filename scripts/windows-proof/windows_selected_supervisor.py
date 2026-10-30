"""Bounded parent supervision of the fixed selected-test child, using stdlib only.

Never opens the selected key/configuration or process memory. Child console I/O
is inherited. Only this Popen handle may be terminated; WeChat is never targeted.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time


INITIAL = frozenset(('startup', 'private_setup', 'config_validation', 'driver_smoke',
                     'file_preflight', 'read_message0', 'read_message1', 'read_contact',
                     'cached_key_check', 'process_identity', 'memory_markers',
                     'memory_references', 'saving_keys', 'checking_wal'))
WAITING = frozenset(('waiting_private_name', 'waiting_group_name', 'waiting_start'))
POST_INPUT = frozenset(('contact_lookup', 'saving_config', 'native_precheck', 'message_baseline'))
STAGES = INITIAL | WAITING | POST_INPUT | {'collecting', 'finalizing'}
LIMITS = {'initial': 120.0, 'post_input': 120.0, 'collecting': 330.0, 'finalizing': 30.0}
PROGRESS_KIND = 'chatlens_selected_progress'
PROGRESS_FIELDS = frozenset(('schema_version', 'kind', 'stage', 'recorded_at_utc',
                             'stdin_tty', 'stdout_tty', 'stderr_tty'))


class SupervisorError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _tty(stream):
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _write_new(path, document):
    # Only new public metadata; never overwrite a child's or user's report.
    with path.open('x', encoding='utf-8') as target:
        json.dump(document, target, ensure_ascii=False, indent=2, allow_nan=False)
        target.write('\n')


def _windows_progress_bytes(path, *, api=None, check_handle=None, normalize=None):
    """A short read sharing DELETE so the child can atomically replace progress."""
    import ctypes as C
    if api is None or check_handle is None or normalize is None:
        from windows_keyproof_platform import _api, _checked, _path
        api, check_handle, normalize = _api(), _checked, _path
    value = normalize(path)
    handle = api.CreateFileW(value, 0x80000000, 7, None, 3, 0x00200000, None)
    if handle in (None, C.c_void_p(-1).value):
        raise SupervisorError('supervisor_progress_read_failed')
    try:
        check_handle(api, handle, value, False)
        buffer, count = C.create_string_buffer(4097), C.c_uint32()
        if not api.ReadFile(handle, buffer, 4097, C.byref(count), None):
            raise SupervisorError('supervisor_progress_read_failed')
        return buffer.raw[:count.value]
    finally:
        api.CloseHandle(handle)


def _read_progress(path, local_check):
    """Return only enum/boolean fields, never echo arbitrary child file values."""
    try:
        if not local_check(path, False) or path.is_symlink() or not path.is_file():
            return None
        if os.name == 'nt':
            raw = _windows_progress_bytes(path)
        else:
            with path.open('rb') as source:
                raw = source.read(4097)
        if len(raw) > 4096:
            return None
        value = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(value, dict) or set(value) != PROGRESS_FIELDS
                or type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or value.get('kind') != PROGRESS_KIND
                or type(value.get('stage')) is not str or value['stage'] not in STAGES
                or type(value.get('recorded_at_utc')) is not str
                or len(value['recorded_at_utc']) > 64
                or any(type(value.get(field)) is not bool for field in
                       ('stdin_tty', 'stdout_tty', 'stderr_tty'))):
            return None
        # Timestamps are not trusted for deadlines; parent monotonic time is used.
        return {field: value[field] for field in ('stage', 'stdin_tty', 'stdout_tty', 'stderr_tty')}
    except Exception:
        return None


def _group(stage, last_group):
    if stage in INITIAL:
        return 'initial'
    if stage in POST_INPUT:
        return 'post_input'
    if stage == 'collecting':
        return 'collecting'
    if stage == 'finalizing':
        return 'finalizing'
    if stage in WAITING:
        return None
    return last_group


def _terminate_owned_child(child):
    """Finite terminate/kill sequence, exclusively through our Popen instance."""
    if child.poll() is not None:
        return child.returncode, True
    try:
        child.terminate()
    except OSError:
        pass
    try:
        return child.wait(timeout=3), True
    except (subprocess.TimeoutExpired, OSError):
        try:
            child.kill()
        except OSError:
            pass
    try:
        return child.wait(timeout=3), True
    except (subprocess.TimeoutExpired, OSError):
        return child.poll(), False


def _stop_owned_child(child):
    # A second Ctrl+C must not skip the finite terminate/kill cleanup sequence.
    previous = None
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError, AttributeError):
        pass
    try:
        return _terminate_owned_child(child)
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def _persist(output, metadata, local_check):
    if not output.exists():
        output.mkdir()  # Caller confirmed absence before launching its child.
    if not local_check(output, True) or output.is_symlink() or not output.is_dir():
        raise SupervisorError('supervisor_output_directory_unsafe')
    _write_new(output / 'supervisor.json', metadata)
    if metadata['reason'] != 'child_finished':
        fallback = {
            'schema_version': 1, 'stage': 'selected_supervisor',
            'recorded_at_utc': _now(), 'status': metadata['status'],
            'reason': metadata['reason'], 'business_read_status': '未验证',
            'chat_messages_read': None, 'controlled_read_count_verified': False,
            'last_safe_stage': metadata['last_stage'],
            'supervisor': metadata, 'messages_sent_by_tool': 0, 'cloud_calls': 0,
        }
        primary = output / 'result-to-send.json'
        target = output / 'supervisor-result-to-send.json' if primary.exists() else primary
        try:
            _write_new(target, fallback)
        except FileExistsError:
            if target == primary:
                _write_new(output / 'supervisor-result-to-send.json', fallback)
            else:
                raise


def supervise(command, output, *, popen_factory=subprocess.Popen, clock=time.monotonic,
              sleep=time.sleep, emit=None, local_check=None, group_limits=None,
              poll_interval=0.2, tty_state=None, nonce=None, working_directory=None):
    """Testable finite supervision; CLI alone constructs the fixed child command.

    ``command`` is a trusted internal list; there is no command override CLI.
    Test seams accept synthetic Popen/clock/local-check implementations.
    """
    output = Path(output)
    if local_check is None:
        from windows_keyproof_platform import path_is_local
        local_check = path_is_local
    if output.exists() or output.is_symlink():
        raise SupervisorError('supervisor_output_already_exists')
    if not local_check(output.parent, True):
        raise SupervisorError('supervisor_local_output_parent_required')
    limits = dict(LIMITS if group_limits is None else group_limits)
    if set(limits) != set(LIMITS) or any(type(v) not in (int, float) or v <= 0 for v in limits.values()):
        raise SupervisorError('supervisor_limits_invalid')
    if emit is None:
        emit = lambda line: print(line, flush=True)
    tty = tty_state or {'stdin_tty': _tty(sys.stdin), 'stdout_tty': _tty(sys.stdout), 'stderr_tty': _tty(sys.stderr)}
    tty = {key: bool(tty.get(key, False)) for key in ('stdin_tty', 'stdout_tty', 'stderr_tty')}
    token = nonce or secrets.token_hex(12)
    if not isinstance(token, str) or not 8 <= len(token) <= 64 or any(c not in '0123456789abcdef' for c in token):
        raise SupervisorError('supervisor_nonce_invalid')
    progress = output.parent / ('.' + output.name + '-' + token + '.progress.json')
    _write_new(progress, {'schema_version': 1, 'kind': PROGRESS_KIND, 'stage': 'startup',
                          'recorded_at_utc': _now(), **tty})
    started_at = last_tick = clock()
    last_notice = started_at - 10
    spent = {key: 0.0 for key in LIMITS}
    active_group = last_group = 'initial'
    stage, child_tty = 'startup', None
    child, started, interrupted, timed_out, cleanup_confirmed = None, False, False, False, True
    code, reason = None, 'supervisor_launch_failed'
    timeout_group = None
    try:
        child = popen_factory(list(command) + ['--progress-file', str(progress)],
                              shell=False, stdin=None, stdout=None, stderr=subprocess.DEVNULL,
                              cwd=working_directory)
        started = True
        while True:
            tick = clock()
            if active_group is not None:
                spent[active_group] += max(0.0, tick - last_tick)
            last_tick = tick
            observed = _read_progress(progress, local_check)
            if observed is None:
                stage = 'unknown'
                # Invalid/unknown stages may not convert an input wait into infinity.
                active_group = last_group
            else:
                stage = observed['stage']
                child_tty = {key: observed[key] for key in tty}
                active_group = _group(stage, last_group)
                if active_group is not None:
                    last_group = active_group
            code = child.poll()
            if code is not None:
                reason = 'child_finished' if code == 0 else 'selected_child_failed'
                break
            if active_group is not None and spent[active_group] >= limits[active_group]:
                timed_out, timeout_group, reason = True, active_group, 'selected_child_hard_timeout'
                break
            if active_group is not None and tick - last_notice >= 10:
                emit('监督阶段：%s；本组已用 %d 秒。' % (stage, int(spent[active_group])))
                last_notice = tick
            sleep(poll_interval)
    except KeyboardInterrupt:
        interrupted, reason = True, 'selected_child_interrupted'
    except Exception:
        reason = 'selected_supervisor_operation_failed' if started else 'supervisor_launch_failed'
    finally:
        if started and child is not None:
            try:
                code, cleanup_confirmed = _stop_owned_child(child)
            except Exception:
                cleanup_confirmed = False
    if not cleanup_confirmed:
        reason = 'selected_child_cleanup_unconfirmed'
    metadata = {
        'schema_version': 1, 'kind': 'chatlens_selected_supervisor',
        'recorded_at_utc': _now(), 'last_stage': stage,
        'status': '待外部条件' if timed_out else '未验证' if interrupted else '通过' if reason == 'child_finished' else '失败',
        'reason': reason, 'child_started': started, 'child_exit_code': code,
        'timed_out': timed_out, 'timeout_group': timeout_group,
        'interrupted': interrupted, 'child_cleanup_confirmed': cleanup_confirmed,
        'elapsed_seconds': round(max(0.0, clock() - started_at), 3),
        'active_group_seconds': {key: round(value, 3) for key, value in spent.items()},
        'parent_console': tty, 'child_console': child_tty,
    }
    if started:
        try:
            _persist(output, metadata, local_check)
        except Exception:
            emit('监督结果未能保存。只反馈此固定状态，不发送私有文件。')
            metadata['report_saved'] = False
        else:
            metadata['report_saved'] = True
    else:
        metadata['report_saved'] = False
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path, required=True)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--creation-time', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if sys.platform != 'win32' or os.name != 'nt':
            raise SupervisorError('windows_supervisor_required')
        package = Path(__file__).resolve().parent
        expected = package / 'runtime' / 'python.exe'
        if os.path.normcase(str(Path(sys.executable).resolve())) != os.path.normcase(str(expected.resolve())):
            raise SupervisorError('fixed_portable_python_required')
        command = [sys.executable, '-I', '-S', '-B', '-X', 'utf8',
                   str(package / 'windows_selected_runner.py'),
                   '--key-file', str(args.key_file), '--pid', str(args.pid),
                   '--creation-time', str(args.creation_time), '--output', str(args.output)]
        metadata = supervise(command, args.output, working_directory=package)
        print('监督结束：%s；原因：%s。' % (metadata['status'], metadata['reason']), flush=True)
        print('如存在，请同时回传本次 supervisor.json；private 留在本机。', flush=True)
        if metadata['reason'] != 'child_finished':
            print('如已有 result-to-send.json，再一并回传 supervisor-result-to-send.json（如存在）。', flush=True)
        return 0 if (metadata['reason'] == 'child_finished' and metadata['child_cleanup_confirmed']
                     and metadata['report_saved']) else 2
    except (Exception, KeyboardInterrupt) as error:
        reason = error.code if isinstance(error, SupervisorError) else 'selected_supervisor_start_failed'
        print('监督未启动：%s。没有新的读取通过结论。' % reason, flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
