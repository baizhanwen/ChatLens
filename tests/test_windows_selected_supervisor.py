"""Synthetic process/clock tests: no WeChat, user data, keys or Windows APIs."""
import importlib.util
import ctypes
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/windows-proof/windows_selected_supervisor.py'
SPEC = importlib.util.spec_from_file_location('windows_selected_supervisor_tested', SOURCE)
supervisor = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supervisor)


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def local_check(path, require_dir=False):
    path = Path(path)
    return path.exists() and not path.is_symlink() and (path.is_dir() if require_dir else path.is_file())


def progress(path, stage, extra=None):
    document = {'schema_version': 1, 'kind': supervisor.PROGRESS_KIND,
                'stage': stage, 'recorded_at_utc': '2026-01-01T00:00:00+00:00',
                'stdin_tty': True, 'stdout_tty': True, 'stderr_tty': False}
    if extra:
        document.update(extra)
    Path(path).write_text(json.dumps(document), encoding='utf-8')


class FakeChild:
    def __init__(self, command, options, clock, phases=None, finish=None, ignore_terminate=False):
        self.command, self.options, self.clock = command, options, clock
        self.progress = Path(command[-1])
        self.phases = phases or [(0, 'startup')]
        self.finish, self.ignore_terminate = finish, ignore_terminate
        self.returncode = None
        self.terminated = self.killed = False
        self.poll()

    def poll(self):
        if self.returncode is None:
            stage = self.phases[0][1]
            for when, candidate in self.phases:
                if self.clock.now() >= when:
                    stage = candidate
            progress(self.progress, stage)
            if self.finish is not None and self.clock.now() >= self.finish:
                self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.ignore_terminate:
            self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = Clock()
        self.logs, self.children = [], []

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, phases=None, finish=None, limits=None, ignore_terminate=False, sleep=None):
        def factory(command, **options):
            child = FakeChild(command, options, self.clock, phases, finish, ignore_terminate)
            self.children.append(child)
            return child
        return supervisor.supervise(
            ['fixed-synthetic-child'], self.root/'synthetic-run', popen_factory=factory,
            clock=self.clock.now, sleep=sleep or self.clock.sleep, emit=self.logs.append,
            local_check=local_check, poll_interval=0.1,
            group_limits=limits or dict.fromkeys(supervisor.LIMITS, 1.0),
            tty_state={'stdin_tty': True, 'stdout_tty': True, 'stderr_tty': False},
            nonce='123456abcdef')

    def test_initial_deadline_does_not_reset_for_new_stage(self):
        result = self.run_fake([(0,'startup'),(.4,'read_message0'),(.8,'memory_markers')])
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['timeout_group'], 'initial')
        self.assertLess(result['elapsed_seconds'], 1.5)
        self.assertTrue(self.children[0].terminated)

    def test_waiting_for_user_has_no_initial_timeout_or_periodic_noise(self):
        result = self.run_fake([(0,'waiting_private_name')], finish=30)
        self.assertEqual(result['reason'], 'child_finished')
        self.assertFalse(result['timed_out'])
        self.assertEqual(self.logs, [])
        self.assertEqual(result['active_group_seconds']['initial'], 0)

    def test_post_input_budget_accumulates_across_input_wait(self):
        result = self.run_fake([(0,'contact_lookup'),(.4,'waiting_group_name'),
                                (2,'saving_config')])
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['timeout_group'], 'post_input')
        self.assertLess(result['elapsed_seconds'], 3.5)

    def test_unknown_stage_after_wait_cannot_wait_forever(self):
        result = self.run_fake([(0,'waiting_private_name'),(.4,'SECRET_ARBITRARY_STAGE')])
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['last_stage'], 'unknown')
        self.assertNotIn('SECRET_ARBITRARY_STAGE', json.dumps(result)+''.join(self.logs))

    def test_collecting_and_finalizing_have_distinct_budgets(self):
        result = self.run_fake([(0,'collecting'),(.4,'finalizing')],
                               limits={'initial':.2,'post_input':.2,'collecting':2,'finalizing':.3})
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['timeout_group'], 'finalizing')

    def test_stubborn_child_is_killed_after_finite_terminate_wait(self):
        result = self.run_fake(ignore_terminate=True)
        self.assertTrue(self.children[0].terminated)
        self.assertTrue(self.children[0].killed)
        self.assertTrue(result['child_cleanup_confirmed'])
        self.assertEqual(result['child_exit_code'], -9)

    def test_ctrl_c_cleans_only_the_owned_child_and_saves_fallback(self):
        def interrupt(_):
            raise KeyboardInterrupt
        result = self.run_fake(sleep=interrupt)
        self.assertTrue(result['interrupted'])
        self.assertTrue(self.children[0].terminated)
        document=json.loads((self.root/'synthetic-run/result-to-send.json').read_text())
        self.assertIsNone(document['chat_messages_read'])
        self.assertEqual(document['business_read_status'], '未验证')

    def test_process_inherits_console_without_shell_or_stderr_leak(self):
        self.run_fake(finish=0)
        options=self.children[0].options
        self.assertIs(options['shell'], False)
        self.assertIsNone(options['stdin'])
        self.assertIsNone(options['stdout'])
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertEqual(self.children[0].command[-2], '--progress-file')

    def test_existing_output_is_never_modified_or_launched(self):
        output=self.root/'synthetic-run';output.mkdir()
        sentinel=output/'result-to-send.json';sentinel.write_bytes(b'EXISTING_REPORT')
        with self.assertRaises(supervisor.SupervisorError):
            self.run_fake()
        self.assertEqual(sentinel.read_bytes(),b'EXISTING_REPORT')
        self.assertEqual(self.children, [])
        self.assertEqual(list(self.root.glob('.*progress.json')), [])

    def test_report_write_failure_is_explicit_and_child_is_reaped(self):
        with mock.patch.object(supervisor, '_persist', side_effect=OSError('SYNTHETIC_PRIVATE_PATH')):
            result=self.run_fake(finish=0)
        self.assertIs(result['report_saved'],False)
        self.assertTrue(result['child_cleanup_confirmed'])
        self.assertNotIn('SYNTHETIC_PRIVATE_PATH',''.join(self.logs))

    def test_nonce_rejected_before_any_progress_or_child_creation(self):
        for nonce in ('abc','g'*24,'a'*65):
            with self.assertRaises(supervisor.SupervisorError):
                supervisor.supervise(['synthetic'],self.root/'new-run',nonce=nonce,
                    local_check=local_check,popen_factory=lambda *a,**k:self.fail('child must not start'))
        self.assertEqual(list(self.root.iterdir()),[])

    def test_cleanup_temporarily_ignores_sigint_and_restores_handler(self):
        with mock.patch.object(supervisor.signal,'signal',return_value='ORIGINAL_HANDLER') as handler:
            result=self.run_fake(ignore_terminate=True)
        self.assertTrue(result['child_cleanup_confirmed'])
        self.assertEqual(handler.call_args_list,
            [mock.call(supervisor.signal.SIGINT,supervisor.signal.SIG_IGN),
             mock.call(supervisor.signal.SIGINT,'ORIGINAL_HANDLER')])

    def test_existing_child_result_is_preserved_and_supplement_added(self):
        def factory(command, **options):
            output=self.root/'synthetic-run';output.mkdir()
            (output/'result-to-send.json').write_bytes(b'CHILD_REPORT_BYTES')
            child=FakeChild(command,options,self.clock)
            self.children.append(child)
            return child
        result=supervisor.supervise(['synthetic'],self.root/'synthetic-run',
            popen_factory=factory,clock=self.clock.now,sleep=self.clock.sleep,
            local_check=local_check,group_limits=dict.fromkeys(supervisor.LIMITS,.3),
            emit=self.logs.append,poll_interval=.1)
        self.assertTrue(result['timed_out'])
        output=self.root/'synthetic-run'
        self.assertEqual((output/'result-to-send.json').read_bytes(),b'CHILD_REPORT_BYTES')
        self.assertTrue((output/'supervisor-result-to-send.json').exists())

    def test_progress_rejects_unknown_fields_and_non_boolean_tty(self):
        path=self.root/'progress.json'
        progress(path,'startup',{'secret':'MUST_NOT_LEAK'})
        self.assertIsNone(supervisor._read_progress(path,local_check))
        progress(path,'startup',{'stdin_tty':1})
        self.assertIsNone(supervisor._read_progress(path,local_check))

    def test_windows_progress_read_shares_delete_and_always_closes(self):
        payload=b'{"synthetic":"metadata"}'
        calls=[]
        class API:
            def CreateFileW(self,*args):
                calls.append(('open',args));return 123
            def ReadFile(self,handle,buffer,size,count,overlapped):
                ctypes.memmove(buffer,payload,len(payload))
                ctypes.cast(count,ctypes.POINTER(ctypes.c_uint32))[0]=len(payload)
                return True
            def CloseHandle(self,handle):
                calls.append(('close',handle));return True
        raw=supervisor._windows_progress_bytes('synthetic',api=API(),
            check_handle=lambda *args:None,normalize=str)
        self.assertEqual(raw,payload)
        self.assertEqual(calls[0][1][2],7)  # FILE_SHARE_READ|WRITE|DELETE
        self.assertEqual(calls[-1],('close',123))

    def test_real_synthetic_sleep_process_is_stopped_by_hard_deadline(self):
        owned=[]
        def factory(command, **options):
            child=subprocess.Popen(command,**options)
            owned.append(child)
            return child
        started=time.monotonic()
        result=supervisor.supervise([sys.executable,'-I','-S','-B','-c',
                                    'import time; time.sleep(60)'],self.root/'real-synthetic-run',
            popen_factory=factory,local_check=local_check,emit=self.logs.append,
            group_limits=dict.fromkeys(supervisor.LIMITS,.2),poll_interval=.02)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['child_cleanup_confirmed'])
        self.assertIsNotNone(owned[0].poll())
        self.assertLess(time.monotonic()-started,5)


if __name__ == '__main__':
    unittest.main()
