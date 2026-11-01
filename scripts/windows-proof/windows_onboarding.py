"""Account-isolated first-use coordinator for the local Windows Web entry.

An existing legacy manager remains responsible for collection and per-scope
storage. This coordinator discovers the actual active account, resolves its own
workspace reference, and performs only explicitly requested initialization.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import subprocess
import sys
import threading
import time

from web_data import WebDataError
from windows_onboarding_native import OnboardingError, validate_candidate

PHASES = {'detecting_account', 'validating_sources', 'memory_markers', 'memory_references',
          'validating_keys', 'preparing_workspace', 'choosing_existing_data'}
REASONS = {'windows_process_not_running', 'windows_onboarding_account_not_observed',
           'windows_process_version_unsupported', 'windows_process_architecture_unsupported',
           'windows_onboarding_account_ambiguous', 'windows_onboarding_process_unavailable',
           'windows_onboarding_mapping_unavailable', 'windows_onboarding_discovery_timeout',
           'windows_onboarding_process_changed', 'windows_onboarding_source_unavailable',
           'windows_onboarding_account_changed', 'windows_onboarding_existing_configuration_invalid',
           'windows_onboarding_key_validation_failed', 'windows_onboarding_operation_failed',
           'windows_onboarding_cancelled', 'windows_onboarding_timeout',
           'windows_onboarding_import_cancelled', 'windows_onboarding_import_failed',
           'windows_workspace_legacy_ambiguous', 'windows_workspace_registered_data_unavailable',
           'windows_workspace_account_limit', 'windows_workspace_account_mismatch',
           'windows_workspace_already_in_use', 'windows_workspace_discovery_limit',
           'windows_workspace_import_invalid', 'windows_workspace_registry_invalid',
           'windows_workspace_registry_or_machine_mismatch', 'windows_workspace_saved_data_limit',
           'windows_workspace_commit_cancelled', 'windows_onboarding_read_budget_exceeded',
           'windows_onboarding_keys_unavailable', 'windows_onboarding_database_layout_unsupported'}


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _owned_process(arguments, payload, *, cancel, progress, timeout, popen=subprocess.Popen):
    """Consume bounded private worker output; cancellation targets our child."""
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
    if len(encoded) > 32768:
        raise OnboardingError('windows_onboarding_operation_failed')
    options = {'stdin': subprocess.PIPE, 'stdout': subprocess.PIPE, 'stderr': subprocess.DEVNULL,
               'shell': False}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    child = popen(arguments, **options)
    rows, reader_failed, writer_failed = Queue(maxsize=32), threading.Event(), threading.Event()
    def reader():
        try:
            for _ in range(128):
                line = child.stdout.readline(32769)
                if not line:
                    return
                if len(line) > 32768:
                    raise ValueError
                rows.put_nowait(line)
            raise ValueError
        except Exception:
            reader_failed.set()
    worker = threading.Thread(target=reader, name='chatlens-onboarding-pipe', daemon=True)
    worker.start()
    def writer():
        try:
            child.stdin.write(encoded)
            child.stdin.close()
        except Exception:
            writer_failed.set()
    input_worker = threading.Thread(target=writer, name='chatlens-onboarding-input', daemon=True)
    input_worker.start()
    deadline, result = time.monotonic() + timeout, None
    try:
        while True:
            if cancel.is_set():
                raise OnboardingError('windows_onboarding_cancelled')
            if time.monotonic() >= deadline:
                raise OnboardingError('windows_onboarding_timeout')
            if reader_failed.is_set() or writer_failed.is_set():
                raise OnboardingError('windows_onboarding_operation_failed')
            try:
                raw = rows.get(timeout=.05)
            except Empty:
                if child.poll() is not None and not worker.is_alive():
                    break
                continue
            try:
                item = json.loads(raw, object_pairs_hook=_pairs)
                if not isinstance(item, dict):
                    raise ValueError
                if set(item) == {'kind', 'phase'} and item['kind'] == 'progress' and item['phase'] in PHASES:
                    progress(item['phase'])
                elif set(item) == {'kind', 'value'} and item['kind'] == 'result' and result is None:
                    result = item['value']
                elif set(item) == {'kind', 'reason'} and item['kind'] == 'error':
                    raise OnboardingError(item['reason'] if item['reason'] in REASONS else 'windows_onboarding_operation_failed')
                else:
                    raise ValueError
            except OnboardingError:
                raise
            except Exception:
                raise OnboardingError('windows_onboarding_operation_failed') from None
        if child.wait(timeout=1) != 0 or not isinstance(result, dict):
            raise OnboardingError('windows_onboarding_operation_failed')
        return result
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=1)
        if child.stdout is not None:
            child.stdout.close()
        worker.join(timeout=.2)
        input_worker.join(timeout=.2)


def _native_command(kind, payload, cancel, progress, *, timeout):
    return _owned_process([sys.executable, '-I', '-S', '-B', '-X', 'utf8',
                           str(Path(__file__).with_name('windows_onboarding_native.py')),
                           '--owned-command', kind], payload, cancel=cancel, progress=progress, timeout=timeout)


def discover_account(cancel, progress):
    progress('detecting_account')
    result = _native_command('discover', {}, cancel, progress, timeout=6)
    if result.get('state') == 'ready':
        validate_candidate(result)
    elif (set(result) != {'state', 'reason', 'process_running'} or result['state'] not in ('waiting_for_wechat', 'ambiguous')
          or result['reason'] not in REASONS or type(result['process_running']) is not bool):
        raise OnboardingError('windows_onboarding_operation_failed')
    return result


def initialize_account(candidate, data_dir, cancel, progress):
    result = _native_command('initialize', {'candidate': candidate, 'data_dir': str(data_dir)},
                             cancel, progress, timeout=100)
    if result != {'state': 'ready', 'reason': 'windows_onboarding_reader_initialized'}:
        raise OnboardingError('windows_onboarding_operation_failed')
    return result


def choose_existing_data(cancel, progress):
    """A folder dialog only after the owner explicitly clicks Import."""
    from windows_keyproof_platform import path_is_local
    executable = Path(os.environ.get('SystemRoot', 'C:\\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    if not path_is_local(executable):
        raise OnboardingError('windows_onboarding_import_failed')
    # Static command text; no paths, names or other user input is interpolated.
    script = """[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
Add-Type -AssemblyName System.Windows.Forms
$d = New-Object Windows.Forms.FolderBrowserDialog
$d.Description = '选择已有的整个 ChatLens collector-data 文件夹'
$d.ShowNewFolderButton = $false
try {
  if ($d.ShowDialog() -eq [Windows.Forms.DialogResult]::OK) {
    @{kind='result';value=@{path=$d.SelectedPath}} | ConvertTo-Json -Compress -Depth 3
  } else { @{kind='error';reason='windows_onboarding_import_cancelled'} | ConvertTo-Json -Compress }
} finally { $d.Dispose() }
"""
    progress('choosing_existing_data')
    result = _owned_process([str(executable), '-NoLogo', '-NoProfile', '-STA', '-Command', script], {},
                            cancel=cancel, progress=progress, timeout=120)
    if set(result) != {'path'} or not isinstance(result['path'], str) or len(result['path']) > 32768:
        raise OnboardingError('windows_onboarding_import_failed')
    return Path(result['path'])


class Task:
    def __init__(self, kind, generation, candidate, action, *, clock=time.monotonic):
        self.kind, self.generation, self.candidate = kind, generation, candidate
        self.clock, self.started = clock, clock()
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.phase = 'detecting_account' if kind == 'discover' else 'validating_sources'
        self.result, self.reason = None, None
        def run():
            try:
                result = action(self.cancel, self.progress)
                if self.cancel.is_set():
                    raise OnboardingError('windows_onboarding_cancelled')
                self.result = result
            except Exception as error:
                code = getattr(error, 'code', None) or str(error)
                self.reason = code if code in REASONS else 'windows_onboarding_operation_failed'
        self.thread = threading.Thread(target=run, name='chatlens-onboarding-' + kind, daemon=True)

    def progress(self, phase):
        if phase not in PHASES:
            raise OnboardingError('windows_onboarding_operation_failed')
        if self.cancel.is_set():
            raise OnboardingError('windows_onboarding_cancelled')
        with self.lock:
            self.phase = phase

    def snapshot(self):
        with self.lock:
            return {'phase': self.phase, 'elapsed_seconds': min(86400, max(0, int(self.clock() - self.started))),
                    'completed': None, 'total': None}


class WindowsOnboardingManager:
    """Workspace coordinator; only an actual detected account may be activated."""
    def __init__(self, workspace_dir, package, output, *, workspace=None, discoverer=discover_account,
                 initializer=initialize_account, importer=choose_existing_data, manager_factory=None,
                 start_collector=None, stop_collector=None, clock=time.monotonic, **options):
        from windows_workspace import Workspace
        self.workspace = workspace or Workspace(workspace_dir, package)
        self.package, self.output = Path(package), Path(output)
        self.discoverer, self.initializer, self.importer = discoverer, initializer, importer
        self.manager_factory = manager_factory
        self.start_collector, self.stop_collector = start_collector, stop_collector
        for name in ('pid', 'creation_time', 'read_only', 'wait_for_wechat'):
            options.pop(name, None)
        self.clock, self.options = clock, options
        self.lock = threading.RLock()
        self._generation, self._candidate, self._delegate = secrets.token_hex(16), None, None
        self._task, self._state, self._reason = None, 'detecting', None
        self._committing = False
        self._committing_task = None
        self._committing_thread = None
        self._commit_done = threading.Event()
        self._commit_done.set()
        self._needs_initialization, self._mode = False, 'new'
        self._process_running = None
        self._last_discovery = float('-inf')
        self._children_started = self._cloud_requests = self._model_requests = 0
        self.closed, self.wait_for_wechat, self.read_only = False, True, False
        self.cleanup_confirmed, self.last_child_exit = True, None
        self.workspace_lease = False

    def __getattr__(self, name):
        delegate = self.__dict__.get('_delegate')
        if delegate is None and self.__dict__.get('closed'):
            delegate = self.__dict__.get('_final_delegate')
        if delegate is not None and hasattr(delegate, name):
            return getattr(delegate, name)
        defaults = {'pid': None, 'creation_time': None, 'child': None, 'child_output': None,
                    'active_config': {'conversations': []}, 'account_matched': False,
                    'account_blocked': True, 'account_probe_reason': self.__dict__.get('_reason'),
                    'process_state': 'unavailable', 'process_reason': self.__dict__.get('_reason'),
                    'connection_state': 'checking_account', 'collection_requested': False,
                    'stage': 'needs_initialization', 'reason': self.__dict__.get('_reason'),
                    'data_dir': None, 'private': self.workspace.private}
        if name in defaults:
            return defaults[name]
        raise AttributeError(name)

    @property
    def scope_generation(self):
        return self._delegate.scope_generation if self._delegate is not None else self._generation

    @property
    def children_started(self):
        return self._children_started + (self._delegate.children_started if self._delegate is not None else 0)

    @property
    def cloud_request_attempts(self):
        return self._cloud_requests + (self._delegate.cloud_request_attempts if self._delegate is not None else 0)

    @property
    def model_request_attempts(self):
        return self._model_requests + (self._delegate.model_request_attempts if self._delegate is not None else 0)

    def _check_generation(self, expected_generation):
        if expected_generation is not None and expected_generation != self.scope_generation:
            raise WebDataError('scope_changed', 409)

    def public_scope_context(self):
        with self.lock:
            if self._delegate is not None:
                return self._delegate.public_scope_context()
            return {'scope_generation': self._generation, 'scope_id': 'onboarding', 'scope_switching': False}

    def start(self):
        self.workspace.acquire()
        self.workspace_lease = True
        self._start_discovery()

    def _new_task(self, kind, action):
        if self.closed or self._task is not None:
            return False
        self._task = Task(kind, self.scope_generation, self._candidate, action, clock=self.clock)
        self._task.thread.start()
        return True

    def _start_discovery(self):
        if self._task is not None or self.closed:
            return
        # A page authorized for a detected, not-yet-initialized account must
        # not initialize another account after recheck. Unbound waiting polls
        # keep their domain so they do not trigger repeated page recovery.
        if self._candidate is not None:
            self._generation = secrets.token_hex(16)
        self._state, self._reason = 'detecting', None
        self._candidate, self._needs_initialization = None, False
        self._process_running = None
        self._last_discovery = self.clock()
        def action(cancel, progress):
            candidate = self.discoverer(cancel, progress)
            if candidate.get('state') != 'ready':
                return {'candidate': candidate, 'data_dir': None}
            data = self.workspace.find_account(candidate['db_root'], candidate['exe_path'])
            return {'candidate': candidate, 'data_dir': data}
        self._new_task('discover', action)

    def _onboarding(self):
        task = self._task
        alive = task is not None and task.thread.is_alive()
        state = 'ready' if self._delegate is not None else self._state
        return {'state': state, 'mode': self._mode, 'reason': self._reason,
                'can_initialize': bool(not self.closed and not self._committing and self._delegate is None and self._needs_initialization and not alive),
                'can_cancel': bool(alive and task.kind != 'discover' and not task.cancel.is_set()),
                'can_import': bool(not self.closed and not self._committing and self._delegate is None and not alive),
                'description': self._description(),
                'progress': task.snapshot() if task is not None else {
                    'phase': None, 'elapsed_seconds': 0, 'completed': None, 'total': None}}

    def _description(self):
        if self._task is not None and self._task.cancel.is_set() and self._task.thread.is_alive():
            return '正在结束本次本机任务，结束前不会启动另一轮；原数据和配置保留。'
        if self._state == 'waiting_for_wechat' and self._process_running is True:
            return '已找到微信程序，当前账号资料尚未核验。请完成微信登录后重试；已登录时也可能暂缺可靠读取依据。'
        descriptions = {
            'detecting': '正在识别这台电脑当前登录的微信和本机资料，不读取聊天正文。',
            'waiting_for_wechat': '请打开微信并完成登录，页面会自动重新检测。',
            'ambiguous': '当前无法唯一确认微信账号，请只保留需要连接的微信账号后重新检测。',
            'needs_initialization': '首次连接此账号，需要在本机初始化读取能力；完成后由你选择关注会话。',
            'initializing': '正在本机初始化读取能力。只保留所需库的验证密钥，不保存进程内存或聊天正文。',
            'ready': '已自动恢复当前账号的本机资料，消息、配置和关注范围分别保留。',
            'error': '本机连接尚未完成，原资料保留。可以按当前原因重试或导入已有数据。'}
        special = {'windows_workspace_legacy_ambiguous': '找到多份属于当前账号的旧资料，无法自动决定。请点击导入已有数据，明确选择要继续使用的一份。',
                   'windows_workspace_registered_data_unavailable': '之前使用的本机数据目录暂不可用。请恢复该目录，或明确导入原资料；不会静默新建空库。',
                   'windows_onboarding_cancelled': '本次任务已取消，已有资料保留。可以重新检测或重试初始化。',
                   'windows_onboarding_timeout': '本次初始化已到用时上限，已有资料保留。可以重新检测后重试。',
                   'windows_onboarding_read_budget_exceeded': '本次初始化已到只读扫描预算上限，当前没有完成读取能力验证。',
                   'windows_onboarding_keys_unavailable': '尚未找到所有所需库的有效读取密钥。请保持当前微信登录，重新检测后重试。',
                   'windows_onboarding_database_layout_unsupported': '这台电脑的微信分库布局或所需库文件尚不在本版支持范围内，当前不会初始化或采集。',
                   'windows_process_version_unsupported': '微信版本尚不在本版支持范围内。当前仅适配4.1.15.56原生x64，不会尝试其他版本的密钥读取。',
                   'windows_process_architecture_unsupported': '检测到的微信架构与此x64测试版不符，当前不会初始化或采集。'}
        return special.get(self._reason, descriptions.get(self._state, descriptions['error']))

    def setup(self):
        with self.lock:
            onboarding = self._onboarding()
            if self._delegate is not None:
                return {**self._delegate.setup(), 'onboarding': onboarding}
            working = self._committing or self._task is not None and self._task.thread.is_alive()
            detected = self._candidate is not None
            connection_state = ('waiting_for_wechat' if self._state == 'waiting_for_wechat' and self._process_running is False else
                                'checking_account' if self._state == 'detecting' else 'account_unverified')
            return {'stage': 'needs_initialization', 'platform': 'windows', 'read_only': False,
                'reason': self._reason, 'onboarding': onboarding,
                'connection': {'state': connection_state, 'reason': self._reason,
                    'description': onboarding['description'], 'actions': ['recheck'] if not working else [],
                    'can_recheck': not working and not self.closed, 'can_start': False, 'can_manage': False,
                    'can_modify_scope': False, 'can_read_saved': False, 'collector_running': False,
                    'collector_stage': 'stopped', 'checks': {'process_running': self._process_running,
                        'account_verified': detected, 'reader_ready': False, 'scope_selected': False, 'collecting': False}},
                'account': {'detected': detected, 'display_name': '当前微信账号' if detected else None},
                'checks': {'wechat_running': self._process_running, 'access_ready': detected, 'reader_ready': False},
                'initialization': {'state': 'working' if working else 'idle', 'action': 'none',
                                   'description': onboarding['description']},
                'progress': onboarding['progress'], **self.public_scope_context(),
                'scope_limits': {'total': 50, 'private': 50, 'group': 50},
                'catalog': {'state': 'idle'}, 'active_scope': None, 'profiles': [], 'workspace_path': '/'}

    def _attach(self, candidate, data_dir, *, expected_generation=None):
        from windows_web_manager import WindowsWebManager
        factory = self.manager_factory or WindowsWebManager
        manager = factory(data_dir, self.package, self.output, pid=candidate['pid'],
            creation_time=candidate['creation_time'], read_only=False, wait_for_wechat=True,
            start_collector=self.start_collector, stop_collector=self.stop_collector,
            **self.options)
        manager.search_first = True
        try:
            manager.start()
            if not manager.account_matched or manager.account_blocked:
                raise OnboardingError('windows_onboarding_account_changed')
            with self.lock:
                if (self.closed or expected_generation is not None and expected_generation != self._generation
                        or self._committing_task is not None and self._committing_task.cancel.is_set()):
                    raise OnboardingError('windows_onboarding_cancelled')
                # Domain validation and publication are one critical section.
                # A cancellation must not fit between validation and attach.
                self._generation = secrets.token_hex(16)
                manager.scope_generation = self._generation
                self._delegate, self._candidate, self._state = manager, candidate, 'ready'
                self._reason, self._needs_initialization = None, False
        except BaseException:
            manager.close()
            raise

    def _detach(self):
        with self.lock:
            manager, self._delegate = self._delegate, None
            self._generation = secrets.token_hex(16)
            self._candidate, self._needs_initialization = None, False
            self._state = 'detecting'
        if manager is not None:
            if self.closed:
                self._final_delegate = manager
            manager.close()
            self._children_started += manager.children_started
            self._cloud_requests += manager.cloud_request_attempts
            self._model_requests += manager.model_request_attempts
            self.cleanup_confirmed, self.last_child_exit = manager.cleanup_confirmed, manager.last_child_exit
            if not manager.cleanup_confirmed:
                raise OnboardingError('windows_onboarding_operation_failed')

    def tick(self):
        if self.closed:
            return
        if self._delegate is not None:
            delegate = self._delegate
            delegate.tick()
            if delegate.account_blocked or delegate.process_state != 'ready':
                self._detach()
                self._start_discovery()
            return
        task = self._task
        if task is not None and not task.thread.is_alive():
            with self.lock:
                self._task = None
                valid = task.generation == self._generation and not task.cancel.is_set()
                if not valid:
                    self._state, self._reason = ('needs_initialization' if self._needs_initialization else 'detecting'), 'windows_onboarding_cancelled'
                    return
                if task.reason:
                    self._state, self._reason = 'error', task.reason
                    if task.reason in ('windows_onboarding_account_changed', 'windows_onboarding_process_changed'):
                        self._candidate, self._needs_initialization = None, False
                        self._generation = secrets.token_hex(16)
                    return
                self._committing = True
                self._committing_task = task
                self._committing_thread = threading.get_ident()
                self._commit_done.clear()
            try:
                if task.kind == 'discover':
                    result = task.result
                    candidate = result['candidate']
                    if candidate['state'] != 'ready':
                        self._candidate, self._needs_initialization = None, False
                        self._process_running = candidate.get('process_running', candidate['reason'] != 'windows_process_not_running')
                        self._state, self._reason = ('error' if candidate['reason'] in (
                            'windows_process_version_unsupported', 'windows_process_architecture_unsupported') else candidate['state']), candidate['reason']
                    else:
                        self._candidate = candidate
                        self._process_running = True
                        if result['data_dir'] is None:
                            self._needs_initialization, self._state, self._reason = True, 'needs_initialization', None
                        else:
                            self._mode = 'reused'
                            self._attach(candidate, result['data_dir'], expected_generation=task.generation)
                elif task.kind == 'initialize':
                    candidate, data_dir = task.result['candidate'], task.result['data_dir']
                    self.workspace.register_account(candidate['db_root'], candidate['exe_path'], data_dir,
                        guard=lambda: not self.closed and task.generation == self._generation and not task.cancel.is_set())
                    self._attach(candidate, data_dir, expected_generation=task.generation)
                elif task.kind == 'import':
                    self.workspace.import_existing(task.result,
                        guard=lambda: not self.closed and task.generation == self._generation and not task.cancel.is_set())
                    self._generation = secrets.token_hex(16)
                    self._committing = False
                    self._start_discovery()
            except Exception as error:
                code = getattr(error, 'code', None) or str(error)
                self._state, self._reason = 'error', code if code in REASONS else 'windows_onboarding_operation_failed'
            finally:
                with self.lock:
                    self._committing = False
                    self._committing_task = None
                    self._committing_thread = None
                    self._commit_done.set()
            return
        if (task is None and self._state in ('detecting', 'waiting_for_wechat', 'ambiguous')
                and self.clock() - self._last_discovery >= 3):
            self._start_discovery()

    def request_recheck(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self._delegate is not None:
                return self._delegate.request_recheck(expected_generation=expected_generation)
            if self._task is None and not self._committing:
                self._start_discovery()
            return self.setup()

    def request_initialize(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if not self._onboarding()['can_initialize'] or self._candidate is None:
                raise WebDataError('windows_initialization_unavailable', 409)
            candidate = dict(self._candidate)
            def action(cancel, progress):
                # Re-discover before any process-memory read; another login
                # between the visible page and the click must not be scanned.
                fresh = self.discoverer(cancel, progress)
                if fresh != candidate:
                    raise OnboardingError('windows_onboarding_account_changed')
                data = self.workspace.create_account_directory(candidate['db_root'], candidate['exe_path'])
                self.initializer(candidate, data, cancel, progress)
                if self.discoverer(cancel, progress) != candidate:
                    raise OnboardingError('windows_onboarding_account_changed')
                return {'candidate': candidate, 'data_dir': data}
            self._state, self._reason, self._mode = 'initializing', None, 'new'
            self._new_task('initialize', action)
            return self.setup()

    def request_cancel(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self._task is not None:
                if self._task.kind == 'discover':
                    raise WebDataError('windows_cancel_unavailable', 409)
                self._task.cancel.set()
                self._generation = secrets.token_hex(16)
            elif self._committing:
                if self._committing_task is not None:
                    self._committing_task.cancel.set()
                self._generation = secrets.token_hex(16)
            return self.setup()

    def request_import(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if not self._onboarding()['can_import']:
                raise WebDataError('windows_import_unavailable', 409)
            def action(cancel, progress):
                path = self.importer(cancel, progress)
                if cancel.is_set():
                    raise OnboardingError('windows_onboarding_cancelled')
                return self.workspace.validate_import(path)
            self._state, self._reason = 'initializing', None
            self._new_task('import', action)
            return self.setup()

    def _call(self, name, *args, expected_generation=None, **kwargs):
        with self.lock:
            self._check_generation(expected_generation)
            delegate = self._delegate
            if delegate is None or self.closed:
                raise WebDataError('web_data_unavailable', 503)
        result = getattr(delegate, name)(*args, expected_generation=expected_generation, **kwargs)
        if isinstance(result, dict) and 'stage' in result:
            result = {**result, 'onboarding': self._onboarding()}
        return result

    def request_catalog(self, expected_generation=None):
        return self._call('request_catalog', expected_generation=expected_generation)

    def request_start(self, expected_generation=None):
        return self._call('request_start', expected_generation=expected_generation)

    def list_conversations(self, q='', *, cursor='', expected_generation=None):
        return self._call('list_conversations', q, cursor=cursor, expected_generation=expected_generation)

    def select_many(self, identifiers, *, expected_generation=None):
        return self._call('select_many', identifiers, expected_generation=expected_generation)

    def select(self, private_id, group_id, *, expected_generation=None):
        return self.select_many([private_id, group_id], expected_generation=expected_generation)

    def restore_profile(self, identifier, *, expected_generation=None):
        return self._call('restore_profile', identifier, expected_generation=expected_generation)

    def conversations(self, *, expected_generation=None):
        return self._call('conversations', expected_generation=expected_generation)

    def messages(self, *args, expected_generation=None, **kwargs):
        return self._call('messages', *args, expected_generation=expected_generation, **kwargs)

    def status(self, *, expected_generation=None):
        return self._call('status', expected_generation=expected_generation)

    def get_ai_service(self, *, expected_generation=None):
        return self._call('get_ai_service', expected_generation=expected_generation)

    def _owned_collector_status(self):
        if self._delegate is not None:
            return self._delegate._owned_collector_status()
        return {'collector_running': False, 'collector_stage': 'stopped', 'heartbeat_at': None,
                'reason': 'collector_not_started'}

    def connection_diagnostics(self, expected_generation=None):
        with self.lock:
            self._check_generation(expected_generation)
            if self._delegate is not None:
                return self._delegate.connection_diagnostics(expected_generation=expected_generation)
            setup = self.setup()
            return {'schema_version': 1, 'kind': 'chatlens_windows_connection_diagnostics', 'platform': 'windows',
                'connection_state': setup['connection']['state'], 'account_reason': self._reason,
                'checks': setup['connection']['checks'], 'account_diagnostics': {},
                'selected_conversation_count': 0, 'collector_running': False, 'collector_stage': 'stopped',
                'catalog': {'schema_version': 1, 'state': 'idle', 'phase': 'idle', 'reason': None,
                    'elapsed_seconds': 0, 'worker_active': False, 'databases_checked': 0, 'tables_seen': 0,
                    'contacts_seen': 0, 'conversations_found': 0}}

    def close(self):
        with self.lock:
            if self.closed and not self.workspace_lease:
                return
            self.closed = True
            self.workspace.begin_close()
            self._generation = secrets.token_hex(16)
            task = self._task
            committing_task = self._committing_task
            committing_thread = self._committing_thread
            if task is not None:
                task.cancel.set()
            if committing_task is not None:
                committing_task.cancel.set()
        deadline = time.monotonic() + 3
        try:
            self._detach()
            if task is not None:
                task.thread.join(timeout=max(0, deadline - time.monotonic()))
                if task.thread.is_alive():
                    self.cleanup_confirmed = False
                    raise OnboardingError('windows_onboarding_operation_failed')
            if not self._commit_done.is_set():
                if committing_thread == threading.get_ident() or not self._commit_done.wait(max(0, deadline - time.monotonic())):
                    self.cleanup_confirmed = False
                    raise OnboardingError('windows_onboarding_operation_failed')
        finally:
            if (self.workspace_lease and (task is None or not task.thread.is_alive())
                    and self._commit_done.is_set()):
                self.workspace.close()
                self.workspace_lease = False
