"""Supervise our local collector by heartbeat, without a total running limit."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from collector_service import Heartbeat, STAGES, now, write_public
from windows_selected_supervisor import _stop_owned_child, _windows_progress_bytes

FIELDS = {'schema_version', 'kind', 'sequence', 'stage', 'recorded_at_utc',
          'total_messages', 'new_messages_this_run', 'resumed_from_checkpoint'}


def read_heartbeat(path):
    try:
        from collector_private import validate_local_path
        validate_local_path(path)
        if path.stat().st_nlink != 1:
            return None
        if os.name == 'nt':
            raw = _windows_progress_bytes(path)
        else:
            with path.open('rb') as stream:
                raw = stream.read(4097)
        if len(raw) > 4096:
            return None
        value = json.loads(raw)
        if (set(value) != FIELDS or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['kind'] != 'chatlens_collector_heartbeat' or value['stage'] not in STAGES
                or type(value['sequence']) is not int or value['sequence'] < 1
                or type(value['resumed_from_checkpoint']) is not bool
                or any(type(value[k]) is not int or value[k] < 0 for k in ('total_messages', 'new_messages_this_run'))
                or not isinstance(value['recorded_at_utc'], str) or len(value['recorded_at_utc']) > 64):
            return None
        return value
    except Exception:
        return None


def request_stop(output):
    path = Path(output) / 'stop.request'
    with path.open('xb') as stream:
        stream.write(b'normal stop requested\n')
        stream.flush()
        os.fsync(stream.fileno())


def supervise(command, output, *, popen_factory=subprocess.Popen, clock=time.monotonic, sleep=time.sleep,
              heartbeat_reader=read_heartbeat, startup_limit=120, heartbeat_limit=90, grace_limit=10):
    """Internal command is fixed by CLI; seams accept synthetic children only."""
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('collector_report_directory_exists')
    from collector_private import validate_local_path
    validate_local_path(output.parent, require_dir=True)
    output.mkdir()
    Heartbeat(output).emit('starting')
    started = last_progress = clock()
    last_sequence = 0
    initialized, stop_sent, timed_out = False, False, False
    child, cleanup, exit_code = None, True, None
    reason, stage = 'collector_launch_failed', 'starting'
    try:
        options = {'shell': False, 'stdin': None, 'stdout': None, 'stderr': subprocess.DEVNULL,
                   'cwd': str(Path(__file__).resolve().parent)}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        child = popen_factory(list(command), **options)
        while True:
            # An owning Web launcher has no shared Ctrl+C process group. Its
            # exclusive output directory is the only control channel it owns.
            owner_stop = output / 'supervisor-stop.request'
            if owner_stop.exists() or owner_stop.is_symlink():
                validate_local_path(owner_stop)
                if not owner_stop.is_file() or owner_stop.stat().st_nlink != 1:
                    raise ValueError('collector_owner_stop_invalid')
                raise KeyboardInterrupt
            observed = heartbeat_reader(output / 'status.json')
            tick = clock()
            if observed is not None and observed['sequence'] > last_sequence:
                last_sequence, last_progress = observed['sequence'], tick
                stage = observed['stage']
                initialized = initialized or stage in ('collecting', 'paused')
            exit_code = child.poll()
            if exit_code is not None:
                reason = 'collector_child_finished' if exit_code == 0 else 'collector_child_failed'
                break
            if (not initialized and tick - started >= startup_limit) or (initialized and tick - last_progress >= heartbeat_limit):
                timed_out, reason = True, 'collector_heartbeat_timeout'
                break
            sleep(0.2)
    except KeyboardInterrupt:
        # The child has its own console process group and notices this local file.
        try:
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        except (ValueError, OSError):
            previous = None
        try:
            request_stop(output)
            stop_sent = True
            deadline = clock() + grace_limit
            while child is not None and child.poll() is None and clock() < deadline:
                sleep(0.2)
            reason = 'collector_normal_stop' if child is not None and child.poll() == 0 else 'collector_stop_deadline'
        except Exception:
            reason = 'collector_stop_request_failed'
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
    except Exception:
        reason = 'collector_supervisor_operation_failed'
    finally:
        if child is not None:
            exit_code, cleanup = _stop_owned_child(child)
    if not cleanup:
        reason = 'collector_child_cleanup_unconfirmed'
    result = {'schema_version': 1, 'kind': 'chatlens_collector_supervisor', 'recorded_at_utc': now(),
              'status': '通过' if (exit_code == 0 and cleanup and not timed_out
                                   and reason in ('collector_child_finished', 'collector_normal_stop')) else '失败',
              'reason': reason,
              'child_started': child is not None, 'child_exit_code': exit_code, 'child_cleanup_confirmed': cleanup,
              'normal_stop_requested': stop_sent, 'timed_out': timed_out, 'last_stage': stage,
              'elapsed_seconds': round(clock() - started, 3), 'total_running_time_limit': None,
              'heartbeat_timeout_seconds': heartbeat_limit}
    write_public(output / 'supervisor.json', result)
    if not (output / 'result-to-send.json').exists():
        write_public(output / 'result-to-send.json',
                     {'schema_version': 1, 'stage': 'continuous_supervisor', 'status': '未验证',
                      'reason': reason, 'checkpoint_state': 'Inspect same local store on restart; never reset baseline.',
                      'chat_messages_read': None, 'messages_sent_by_tool': 0, 'cloud_calls': 0})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--selected-config', type=Path)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--creation-time', type=int, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--web-metadata', action='store_true')
    args = parser.parse_args()
    package = Path(__file__).resolve().parent
    expected = package / 'runtime' / 'python.exe'
    if sys.platform != 'win32' or Path(sys.executable).resolve() != expected.resolve():
        print('reason=fixed_windows_collector_runtime_required', flush=True)
        return 2
    command = [str(expected), '-I', '-S', '-B', '-X', 'utf8', str(package / 'collector_service.py'),
               '--data-dir', str(args.data_dir), '--pid', str(args.pid), '--creation-time', str(args.creation_time),
               '--output', str(args.output)]
    if args.selected_config is not None:
        command += ['--selected-config', str(args.selected_config)]
    if args.web_metadata:
        command += ['--web-metadata']
    try:
        result = supervise(command, args.output)
    except Exception:
        print('reason=collector_supervisor_start_or_report_failed', flush=True)
        return 2
    print('采集步骤已结束；只回传本次 result-to-send.json 和 supervisor.json。', flush=True)
    return 0 if result['status'] == '通过' else 2


if __name__ == '__main__':
    raise SystemExit(main())
