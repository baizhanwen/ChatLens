"""Own a loopback Web server and, optionally, one supervised selected collector."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import webbrowser

from collector_private import ensure_private_dir, validate_local_path
from collector_service import now, safe_code, write_public
from web_data import CollectorWebData
from web_server import make_server


def enable_assistant(server, data):
    # Source checkout keeps AI modules one directory above these portable
    # helpers; the Windows archive places them beside this file instead.
    candidates = Path(__file__).resolve().parent.parent
    if (candidates / 'ai_service.py').is_file():
        sys.path.insert(0, str(candidates))
    from ai_service import AiAssistant
    from ai_web_server import attach_ai_routes
    server.assistant = None
    server.assistant_closing = False
    def get_service():
        with server.assistant_lock:
            if server.assistant_closing:
                from ai_service import AiError
                raise AiError('ai_service_stopped', 'AI 服务正在关闭。', 503)
            if server.assistant is None:
                server.assistant = AiAssistant(data.data_dir, data.private / 'assistant')
            return server.assistant
    server.assistant_lock = threading.RLock()
    attach_ai_routes(server, get_service)


def owned_command(package, data_dir, output, pid, creation_time):
    return [str(package / 'runtime/python.exe'), '-I', '-S', '-B', '-X', 'utf8',
            str(package / 'collector_supervisor.py'), '--data-dir', str(data_dir),
            '--output', str(output), '--pid', str(pid), '--creation-time', str(creation_time),
            '--web-metadata']


def stop_collection(child, child_output, *, grace=20, clock=time.monotonic, sleep=time.sleep):
    """Signal only this run's child through its exclusively new output directory."""
    if child is None:
        return None, True
    if child.poll() is not None:
        return child.returncode, True
    previous = None
    try:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass
    try:
        deadline = clock() + grace
        stop_written = False
        while child.poll() is None and clock() < deadline:
            if not stop_written and child_output.is_dir():
                validate_local_path(child_output, require_dir=True)
                try:
                    with (child_output / 'supervisor-stop.request').open('xb') as target:
                        target.write(b'normal stop requested by owning Web launcher\n')
                        target.flush()
                        os.fsync(target.fileno())
                    stop_written = True
                except FileExistsError:
                    stop_written = True
            sleep(.1)
        # The supervisor has its own owned-child timeout/cleanup. Do not kill a
        # live supervisor blindly and leave its collector running.
        if child.poll() is None:
            return None, False
        return child.returncode, True
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def run(data_dir, output, *, package, pid=None, creation_time=None, read_only=False,
        no_open=False, popen_factory=subprocess.Popen, open_browser=webbrowser.open,
        wait=None, server_factory=make_server, manage_scopes=False, manager_factory=None, wait_for_wechat=False,
        workspace_dir=None):
    """The CLI fixes package/runtime paths; test seams use synthetic data only."""
    if manage_scopes or workspace_dir is not None:
        return run_managed(data_dir, output, package=package, pid=pid, creation_time=creation_time,
                           read_only=read_only, no_open=no_open, popen_factory=popen_factory,
                           open_browser=open_browser, wait=wait, manager_factory=manager_factory,
                           wait_for_wechat=wait_for_wechat, workspace_dir=workspace_dir)
    if wait_for_wechat:
        raise ValueError('windows_wait_requires_scope_manager')
    data_dir, output, package = Path(data_dir), Path(output), Path(package)
    if output.exists() or output.is_symlink():
        raise ValueError('web_output_already_exists')
    validate_local_path(output.parent, require_dir=True)
    ensure_private_dir(data_dir)
    ensure_private_dir(data_dir / 'private')
    ensure_private_dir(data_dir / 'private' / 'store')
    if read_only and (pid is not None or creation_time is not None):
        raise ValueError('web_read_only_process_conflict')
    if not read_only and (type(pid) is not int or pid <= 0 or type(creation_time) is not int or creation_time <= 0):
        raise ValueError('web_collector_identity_required')
    data = CollectorWebData(data_dir)
    data.status()  # Fail safely for invalid/missing state; never create a baseline.
    output.mkdir()
    report = {'schema_version': 1, 'stage': 'local_web_runtime', 'started_at_utc': now(),
              'status': '未验证', 'reason': 'not_started', 'read_only': bool(read_only),
              'server_started': False, 'browser_open_requested': False,
              'collector_child_started': False, 'collector_child_exit_code': None,
              'collector_cleanup_confirmed': True, 'server_closed': False,
              'web_live_end_to_end_status': '未验证', 'messages_sent_by_tool': 0, 'cloud_calls': 0,
              'source_database_records_modified': False, 'private_data_copied': False}
    server = child = worker = None
    child_output = output / 'collector-run'
    shutdown = threading.Event()
    try:
        server = server_factory(data, package / 'web', port=0)
        if hasattr(server, 'RequestHandlerClass'):
            enable_assistant(server, data)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        worker.start()
        report['server_started'] = True
        if not read_only:
            options = {'shell': False, 'stdin': None, 'stdout': None, 'stderr': subprocess.DEVNULL,
                       'cwd': str(package)}
            if os.name == 'nt':
                options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options['start_new_session'] = True
            child = popen_factory(owned_command(package, data_dir, child_output, pid, creation_time), **options)
            report['collector_child_started'] = True
        print('ChatLens 本机页面：' + server.base_url, flush=True)
        print('正在尝试打开已授权的本机页面。页面登录凭据只在浏览器和内存中，不写报告。', flush=True)
        if not no_open:
            report['browser_open_requested'] = True
            opened = bool(open_browser(server.launch_url))
            if not opened:
                # The fragment is not sent to HTTP logs. Show only to the local
                # owner if their default-browser launch failed; never in reports.
                print('默认浏览器未打开，请在本机浏览器粘贴下行地址，不要转发：', flush=True)
                print(server.launch_url, flush=True)
        print('只读浏览已保存消息。' if read_only else '正在启动原范围采集；可在页面查看实际心跳状态。', flush=True)
        print('按一次 Ctrl+C 关闭本机页面服务，并停止本轮启动的采集。', flush=True)
        notified = False
        while not shutdown.is_set():
            if not worker.is_alive():
                raise ValueError('web_server_unexpected_stop')
            if child is not None and child.poll() is not None and not notified:
                print('本轮采集进程已结束，页面仍可浏览已有消息；实际状态以页面为准。', flush=True)
                notified = True
            if wait is None:
                shutdown.wait(.25)
            else:
                wait(shutdown)
        report['status'], report['reason'] = '通过', 'local_web_stopped'
    except KeyboardInterrupt:
        report['status'], report['reason'] = '通过', 'local_web_normal_stop'
    except Exception as error:
        report['status'], report['reason'] = '失败', safe_code(error)
    finally:
        if child is not None:
            try:
                code, clean = stop_collection(child, child_output)
                report['collector_child_exit_code'], report['collector_cleanup_confirmed'] = code, clean
                if not clean:
                    report['status'], report['reason'] = '失败', 'web_collector_stop_not_confirmed'
                elif code != 0:
                    report['status'], report['reason'] = '失败', 'web_collector_child_failed'
            except Exception:
                report['status'], report['reason'] = '失败', 'web_collector_stop_failed'
                report['collector_cleanup_confirmed'] = False
        if server is not None:
            assistant_lock = getattr(server, 'assistant_lock', None)
            if assistant_lock is not None:
                with assistant_lock:
                    server.assistant_closing = True
                    assistant = server.assistant
            else:
                assistant = None
            if assistant is not None:
                assistant.close()
                report['cloud_calls'] = assistant.cloud_request_attempts
                report['model_request_attempts'] = assistant.model_request_attempts
                report['cloud_count_kind'] = 'request_attempts'
            if worker is not None and worker.is_alive():
                server.shutdown()
            server.server_close()
            if worker is not None and worker.ident is not None:
                worker.join(timeout=2)
            report['server_closed'] = worker is None or not worker.is_alive()
            if not report['server_closed']:
                report['status'], report['reason'] = '失败', 'web_server_stop_not_confirmed'
        report['finished_at_utc'] = now()
        write_public(output / 'result-to-send.json', report)
    return report


def run_managed(data_dir, output, *, package, pid=None, creation_time=None, read_only=False,
                no_open=False, popen_factory=subprocess.Popen, open_browser=webbrowser.open,
                wait=None, manager_factory=None, wait_for_wechat=False, workspace_dir=None):
    """Production multi-scope owner; old single-scope callers remain compatible."""
    from windows_web_manager import WindowsWebManager
    from windows_web_server import make_windows_server
    data_dir, output, package = Path(workspace_dir if workspace_dir is not None else data_dir), Path(output), Path(package)
    if output.exists() or output.is_symlink():
        raise ValueError('web_output_already_exists')
    validate_local_path(output.parent, require_dir=True)
    if workspace_dir is None:
        ensure_private_dir(data_dir)
        ensure_private_dir(data_dir / 'private')
        ensure_private_dir(data_dir / 'private' / 'store')
    else:
        if read_only:
            raise ValueError('windows_workspace_explicit_read_only_conflict')
        wait_for_wechat = True
    if read_only and (pid is not None or creation_time is not None):
        raise ValueError('web_read_only_process_conflict')
    if read_only and wait_for_wechat:
        raise ValueError('windows_wait_and_explicit_read_only_conflict')
    identity_supplied = pid is not None or creation_time is not None
    if not read_only and (not wait_for_wechat or identity_supplied) and (
            type(pid) is not int or pid <= 0 or type(creation_time) is not int or creation_time <= 0):
        raise ValueError('web_collector_identity_required')
    if workspace_dir is None:
        CollectorWebData(data_dir).status()  # Legacy entry never re-baselines data.
    output.mkdir()
    report = {'schema_version': 1, 'stage': 'windows_multi_scope_web_runtime', 'started_at_utc': now(),
              'status': '未验证', 'reason': 'not_started', 'read_only': bool(read_only),
              'server_started': False, 'browser_open_requested': False,
              'collector_child_started': False, 'collector_child_exit_code': None,
              'collector_cleanup_confirmed': True, 'server_closed': False,
              'web_live_end_to_end_status': '未验证', 'messages_sent_by_tool': 0, 'cloud_calls': 0,
              'source_database_records_modified': False, 'private_data_copied_into_package': False,
              'scope_limit': 50, 'account_isolation': 'source_root_and_verified_active_account',
              'old_data_and_configuration_preserved': True, 'wait_for_wechat': bool(wait_for_wechat)}
    server = worker = manager = None
    shutdown = threading.Event()
    def start_collector(target, child_output):
        options = {'shell': False, 'stdin': None, 'stdout': None, 'stderr': subprocess.DEVNULL,
                   'cwd': str(package)}
        if os.name == 'nt':
            options['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            options['start_new_session'] = True
        command = owned_command(package, target, child_output, manager.pid, manager.creation_time) + ['--verify-active-account']
        return popen_factory(command, **options)
    try:
        if workspace_dir is not None and manager_factory is None:
            from windows_onboarding import WindowsOnboardingManager
            manager_factory = WindowsOnboardingManager
        manager = (manager_factory or WindowsWebManager)(data_dir, package, output, pid=pid,
            creation_time=creation_time, read_only=read_only, start_collector=start_collector,
            stop_collector=stop_collection, wait_for_wechat=wait_for_wechat)
        manager.start()
        server = make_windows_server(manager, package / 'web', port=0)
        parent = Path(__file__).resolve().parent.parent
        if (parent / 'ai_web_server.py').is_file() and str(parent) not in sys.path:
            sys.path.insert(0, str(parent))
        from ai_web_server import attach_ai_routes
        attach_ai_routes(server, manager.get_ai_service)
        worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .1}, daemon=True)
        worker.start()
        report['server_started'] = True
        print('ChatLens 本机页面：' + server.base_url, flush=True)
        print('原数据目录与配置保留；关注范围和 AI 历史独立保存。按一次 Ctrl+C 正常停止。', flush=True)
        if not no_open:
            report['browser_open_requested'] = True
            if not open_browser(server.launch_url):
                print('默认浏览器未打开，请仅在本机浏览器使用下行地址，不要转发：', flush=True)
                print(server.launch_url, flush=True)
        while not shutdown.is_set():
            if not worker.is_alive():
                raise ValueError('web_server_unexpected_stop')
            manager.tick()
            if wait is None:
                shutdown.wait(.25)
            else:
                wait(shutdown)
        report['status'], report['reason'] = '通过', 'local_web_stopped'
    except KeyboardInterrupt:
        report['status'], report['reason'] = '通过', 'local_web_normal_stop'
    except Exception as error:
        report['status'], report['reason'] = '失败', safe_code(error)
    finally:
        if manager is not None:
            reader_readiness_verified = False
            if manager.wait_for_wechat:
                try:
                    with manager.lock:
                        owned = manager._owned_collector_status()
                    report['collector_stage_before_close'] = owned['collector_stage']
                    reader_readiness_verified = bool(owned['collector_running'] and owned['collector_stage'] in ('collecting', 'paused'))
                except Exception:
                    report['collector_stage_before_close'] = 'unavailable'
                report['reader_readiness_verified'] = reader_readiness_verified
            try:
                manager.close()
            except Exception:
                report['status'], report['reason'] = '失败', 'web_collector_stop_not_confirmed'
            report['collector_child_started'] = manager.children_started > 0
            report['collector_child_exit_code'] = manager.last_child_exit
            report['collector_cleanup_confirmed'] = manager.cleanup_confirmed
            report['cloud_calls'] = manager.cloud_request_attempts
            report['model_request_attempts'] = manager.model_request_attempts
            report['manager_final_stage'] = manager.stage
            report['manager_final_reason'] = manager.reason
            report['account_verified'] = bool(manager.account_matched and not manager.read_only and not manager.account_blocked)
            report['account_probe_reason'] = manager.account_probe_reason
            report['process_state'] = manager.process_state
            report['process_reason'] = manager.process_reason
            report['connection_state'] = manager.connection_state
            report['collection_requested'] = manager.collection_requested
            report['selected_conversation_count'] = len(manager.active_config.get('conversations', []))
            try:
                report['saved_scope_count'] = len(manager.setup().get('profiles', []))
            except Exception:
                report['saved_scope_count'] = None
            report['collection_setup_status'] = (
                '未验证' if manager.read_only or manager.account_blocked or manager.wait_for_wechat and (
                    not manager.children_started or manager.connection_state in (
                        'waiting_for_wechat', 'checking_account', 'account_unverified', 'wrong_account')) else
                '失败' if manager.reason or not manager.cleanup_confirmed or manager.last_child_exit not in (None, 0) else
                '通过' if manager.children_started and manager.stage == 'running'
                         and (not manager.wait_for_wechat or reader_readiness_verified) else '未验证')
            if not manager.cleanup_confirmed:
                report['status'], report['reason'] = '失败', 'web_collector_stop_not_confirmed'
            elif manager.last_child_exit is not None and manager.last_child_exit != 0:
                report['status'], report['reason'] = '失败', 'web_collector_child_failed'
            elif manager.account_blocked:
                report['status'], report['reason'] = '未验证', 'windows_active_account_not_verified'
        if server is not None:
            if worker is not None and worker.is_alive():
                server.shutdown()
            server.server_close()
            if worker is not None and worker.ident is not None:
                worker.join(timeout=2)
            report['server_closed'] = worker is None or not worker.is_alive()
            if not report['server_closed']:
                report['status'], report['reason'] = '失败', 'web_server_stop_not_confirmed'
        report['finished_at_utc'] = now()
        write_public(output / 'result-to-send.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument('--data-dir', type=Path)
    location.add_argument('--workspace-dir', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--pid', type=int)
    parser.add_argument('--creation-time', type=int)
    parser.add_argument('--read-only', action='store_true')
    parser.add_argument('--no-open', action='store_true')
    parser.add_argument('--manage-scopes', action='store_true')
    parser.add_argument('--wait-for-wechat', action='store_true')
    args = parser.parse_args()
    package = Path(__file__).resolve().parent
    if sys.platform != 'win32' or Path(sys.executable).resolve() != (package / 'runtime/python.exe').resolve():
        print('reason=fixed_windows_web_runtime_required', flush=True)
        return 2
    try:
        result = run(args.data_dir, args.output, package=package, pid=args.pid, creation_time=args.creation_time,
                     read_only=args.read_only, no_open=args.no_open, manage_scopes=args.manage_scopes,
                     wait_for_wechat=args.wait_for_wechat, workspace_dir=args.workspace_dir)
    except Exception:
        print('reason=web_runtime_start_or_report_failed', flush=True)
        return 2
    print('本轮已结束；只回传本次 result-to-send.json，private 和登录地址留在本机。', flush=True)
    return 0 if result['status'] == '通过' else 2


if __name__ == '__main__':
    raise SystemExit(main())
