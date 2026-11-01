"""Finite synthetic process/heartbeat checks; no real chat or private data."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[1] / 'scripts/windows-proof/collector_supervisor.py'
sys.path.insert(0, str(SOURCE.parent))
try:
    SPEC = importlib.util.spec_from_file_location('chatlens_collector_supervisor_tested', SOURCE)
    supervisor = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(supervisor)
finally:
    sys.path.remove(str(SOURCE.parent))


class Clock:
    def __init__(self):
        self.value = 0.0

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def heartbeat(sequence=1, stage='collecting', **extra):
    document = {'schema_version': 1, 'kind': 'chatlens_collector_heartbeat',
                'sequence': sequence, 'stage': stage,
                'recorded_at_utc': '2026-01-01T00:00:00+00:00',
                'total_messages': 0, 'new_messages_this_run': 0,
                'resumed_from_checkpoint': False}
    document.update(extra)
    return document


class Child:
    def __init__(self, command, options, clock, output, *, finish=None, grace_exit=True,
                 ignore_terminate=False, terminate_code=-15, private_result=None):
        self.command, self.options = command, options
        self.clock, self.output = clock, output
        self.finish, self.grace_exit = finish, grace_exit
        self.ignore_terminate, self.terminate_code = ignore_terminate, terminate_code
        self.returncode = None
        self.terminated = self.killed = False
        self.stop_observed_at = None
        if private_result is not None:
            (output / 'result-to-send.json').write_bytes(private_result)

    def poll(self):
        if self.returncode is None:
            if self.finish is not None and self.clock.now() >= self.finish:
                self.returncode = 0
            elif (self.output / 'stop.request').exists():
                if self.stop_observed_at is None:
                    self.stop_observed_at = self.clock.now()
                if self.grace_exit and self.clock.now() - self.stop_observed_at >= 0.2:
                    self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.ignore_terminate:
            self.returncode = self.terminate_code

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout):
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


class CollectorSupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-collector-supervisor-')
        self.root = Path(self.temp.name).resolve()
        self.output = self.root / 'synthetic-report'
        self.clock = Clock()
        self.children = []

    def tearDown(self):
        self.temp.cleanup()

    def run_fake(self, *, reader=None, sleep=None, finish=None, startup=1.0, live=1.0,
                 grace=0.8, grace_exit=True, ignore_terminate=False, terminate_code=-15,
                 private_result=None):
        def factory(command, **options):
            child = Child(command, options, self.clock, self.output, finish=finish,
                          grace_exit=grace_exit, ignore_terminate=ignore_terminate,
                          terminate_code=terminate_code, private_result=private_result)
            self.children.append(child)
            return child
        return supervisor.supervise(['fixed-synthetic-child'], self.output,
                                    popen_factory=factory, clock=self.clock.now,
                                    sleep=sleep or self.clock.sleep,
                                    heartbeat_reader=reader or (lambda _: heartbeat(int(self.clock.now() * 10) + 1)),
                                    startup_limit=startup, heartbeat_limit=live, grace_limit=grace)

    def interrupt_once(self, when=0.4):
        raised = False
        def sleep(seconds):
            nonlocal raised
            if not raised and self.clock.now() >= when:
                raised = True
                raise KeyboardInterrupt
            self.clock.sleep(seconds)
        return sleep

    def test_progressing_collection_has_no_total_runtime_limit(self):
        result = self.run_fake(finish=60, startup=0.6, live=0.6)
        self.assertEqual(result['status'], '通过')
        self.assertFalse(result['timed_out'])
        self.assertIsNone(result['total_running_time_limit'])
        self.assertGreaterEqual(result['elapsed_seconds'], 60)
        self.assertFalse(self.children[0].terminated)

    def test_paused_collection_with_new_heartbeats_can_wait_without_total_limit(self):
        result = self.run_fake(reader=lambda _: heartbeat(int(self.clock.now() * 10) + 1, 'paused'),
                               finish=20, startup=0.6, live=0.6)
        self.assertFalse(result['timed_out'])
        self.assertEqual(result['last_stage'], 'paused')
        self.assertGreaterEqual(result['elapsed_seconds'], 20)

    def test_startup_uses_its_own_budget_instead_of_live_heartbeat_budget(self):
        result = self.run_fake(reader=lambda _: None, startup=2, live=0.4)
        self.assertTrue(result['timed_out'])
        self.assertGreaterEqual(result['elapsed_seconds'], 2)
        self.assertLess(result['elapsed_seconds'], 2.5)
        self.assertTrue(self.children[0].terminated)

    def test_starting_heartbeats_cannot_extend_total_startup_budget(self):
        result = self.run_fake(reader=lambda _: heartbeat(int(self.clock.now() * 10) + 1, 'starting'),
                               startup=1, live=0.4)
        self.assertTrue(result['timed_out'])
        self.assertGreaterEqual(result['elapsed_seconds'], 1)
        self.assertLess(result['elapsed_seconds'], 1.5)

    def test_duplicate_sequence_and_changing_time_do_not_extend_live_budget(self):
        result = self.run_fake(reader=lambda _: heartbeat(1, recorded_at_utc=str(self.clock.now())), live=0.6)
        self.assertTrue(result['timed_out'])
        self.assertGreaterEqual(result['elapsed_seconds'], 0.6)
        self.assertLess(result['elapsed_seconds'], 1)

    def test_regressing_sequences_do_not_extend_live_budget(self):
        result = self.run_fake(reader=lambda _: heartbeat(10 if self.clock.now() == 0 else 9), live=0.6)
        self.assertTrue(result['timed_out'])
        self.assertLess(result['elapsed_seconds'], 1)

    def test_ctrl_c_requests_graceful_stop_and_does_not_terminate_cooperative_child(self):
        with mock.patch.object(supervisor.signal, 'signal', return_value='SYNTHETIC_ORIGINAL') as setter:
            result = self.run_fake(sleep=self.interrupt_once())
        self.assertEqual(result['reason'], 'collector_normal_stop')
        self.assertEqual(result['status'], '通过')
        self.assertTrue(result['normal_stop_requested'])
        self.assertFalse(self.children[0].terminated)
        self.assertTrue((self.output / 'stop.request').is_file())
        self.assertIn(mock.call(supervisor.signal.SIGINT, 'SYNTHETIC_ORIGINAL'), setter.call_args_list)

    def test_grace_deadline_terminates_then_kills_only_its_child_handle(self):
        unrelated = mock.Mock()
        with mock.patch.object(supervisor.subprocess, 'run', side_effect=AssertionError('must not run process-name commands')):
            result = self.run_fake(sleep=self.interrupt_once(), grace_exit=False, ignore_terminate=True)
        self.assertEqual(result['reason'], 'collector_stop_deadline')
        self.assertEqual(result['status'], '失败')
        self.assertTrue(self.children[0].terminated)
        self.assertTrue(self.children[0].killed)
        self.assertEqual(result['child_exit_code'], -9)
        self.assertTrue(result['child_cleanup_confirmed'])
        unrelated.terminate.assert_not_called()
        unrelated.kill.assert_not_called()

    def test_timeout_then_zero_exit_is_still_failure(self):
        result = self.run_fake(reader=lambda _: heartbeat(1), live=0.4, terminate_code=0)
        self.assertTrue(result['timed_out'])
        self.assertEqual(result['child_exit_code'], 0)
        self.assertEqual(result['status'], '失败')

    def test_stop_request_failure_still_reaps_child_and_saves_supervisor_report(self):
        with mock.patch.object(supervisor, 'request_stop', side_effect=OSError('SYNTHETIC_PRIVATE_PATH')):
            result = self.run_fake(sleep=self.interrupt_once(), grace_exit=False)
        self.assertEqual(result['status'], '失败')
        self.assertTrue(result['child_cleanup_confirmed'])
        self.assertTrue((self.output / 'supervisor.json').is_file())
        self.assertNotIn('SYNTHETIC_PRIVATE_PATH', json.dumps(result))

    def test_stop_request_failure_is_not_success_if_child_exits_zero_during_cleanup(self):
        with mock.patch.object(supervisor, 'request_stop', side_effect=OSError('SYNTHETIC_PRIVATE_PATH')):
            result = self.run_fake(sleep=self.interrupt_once(), grace_exit=False, terminate_code=0)
        self.assertEqual(result['child_exit_code'], 0)
        self.assertEqual(result['reason'], 'collector_stop_request_failed')
        self.assertEqual(result['status'], '失败')

    def test_process_launch_is_no_shell_and_does_not_share_console_interrupt_group(self):
        self.run_fake(finish=0)
        options = self.children[0].options
        self.assertIs(options['shell'], False)
        self.assertIsNone(options['stdin'])
        self.assertIsNone(options['stdout'])
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        if os.name == 'nt':
            self.assertEqual(options['creationflags'], subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            self.assertTrue(options['start_new_session'])

    def test_existing_output_is_untouched_and_no_child_starts(self):
        self.output.mkdir()
        sentinel = self.output / 'result-to-send.json';sentinel.write_bytes(b'SYNTHETIC_EXISTING_REPORT')
        with self.assertRaises(ValueError):
            self.run_fake()
        self.assertEqual(sentinel.read_bytes(), b'SYNTHETIC_EXISTING_REPORT')
        self.assertEqual(self.children, [])

    def test_existing_child_result_is_preserved_on_timeout(self):
        result = self.run_fake(reader=lambda _: heartbeat(1), live=0.4,
                               private_result=b'SYNTHETIC_CHILD_REPORT_BYTES')
        self.assertTrue(result['timed_out'])
        self.assertEqual((self.output / 'result-to-send.json').read_bytes(), b'SYNTHETIC_CHILD_REPORT_BYTES')
        self.assertTrue((self.output / 'supervisor.json').exists())

    def test_unknown_read_count_stays_null_on_supervisor_only_fallback(self):
        self.run_fake(reader=lambda _: heartbeat(1), live=0.4)
        result = json.loads((self.output / 'result-to-send.json').read_text())
        self.assertEqual(result['status'], '未验证')
        self.assertIsNone(result['chat_messages_read'])
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))

    def test_heartbeat_schema_rejects_private_extras_invalid_counts_and_unknown_stage(self):
        path = self.root / 'heartbeat.json'
        for extra in ({'private_text': 'SYNTHETIC_SECRET'}, {'stage': 'SYNTHETIC_UNKNOWN'},
                      {'schema_version': True}, {'sequence': True}, {'new_messages_this_run': -1},
                      {'resumed_from_checkpoint': 1}):
            with self.subTest(extra=extra):
                path.write_text(json.dumps(heartbeat(**extra)), encoding='utf-8')
                self.assertIsNone(supervisor.read_heartbeat(path))
        path.write_text(json.dumps(heartbeat(2)), encoding='utf-8')
        self.assertEqual(supervisor.read_heartbeat(path)['sequence'], 2)

    def test_real_synthetic_sleep_child_is_stopped_by_heartbeat_deadline(self):
        child_code = 'import time; time.sleep(30)'
        result = supervisor.supervise([sys.executable, '-B', '-c', child_code], self.output,
                                      startup_limit=0.25, heartbeat_limit=0.1, grace_limit=0.1)
        self.assertTrue(result['timed_out'])
        self.assertTrue(result['child_cleanup_confirmed'])
        self.assertLess(result['elapsed_seconds'], 3)
        self.assertEqual(result['status'], '失败')

    def test_real_cooperative_child_observes_stop_file_and_exits_without_termination(self):
        child_code = '''
import json,pathlib,sys,time
root=pathlib.Path(sys.argv[1]);sequence=1
while not (root/'stop.request').exists():
 document={'schema_version':1,'kind':'chatlens_collector_heartbeat','sequence':sequence,'stage':'collecting','recorded_at_utc':'2026-01-01T00:00:00+00:00','total_messages':0,'new_messages_this_run':0,'resumed_from_checkpoint':False}
 temp=root/'synthetic-progress.tmp';temp.write_text(json.dumps(document));temp.replace(root/'status.json')
 sequence+=1;time.sleep(.01)
(root/'result-to-send.json').write_text(json.dumps({'synthetic_child_graceful_stop':True}))
'''
        start, interrupted = time.monotonic(), False
        children = []
        def sleep(seconds):
            nonlocal interrupted
            if not interrupted and time.monotonic() - start >= .25:
                interrupted = True
                raise KeyboardInterrupt
            time.sleep(seconds)
        def factory(command, **options):
            child = subprocess.Popen(command, **options)
            children.append(child)
            child.terminate = mock.Mock(wraps=child.terminate)
            child.kill = mock.Mock(wraps=child.kill)
            return child
        result = supervisor.supervise([sys.executable, '-B', '-c', child_code, str(self.output)], self.output,
                                      popen_factory=factory, sleep=sleep, startup_limit=3,
                                      heartbeat_limit=3, grace_limit=3)
        self.assertEqual(result['reason'], 'collector_normal_stop')
        self.assertEqual(result['status'], '通过')
        children[0].terminate.assert_not_called()
        children[0].kill.assert_not_called()
        self.assertTrue(json.loads((self.output / 'result-to-send.json').read_text())['synthetic_child_graceful_stop'])

    def test_launch_failure_is_redacted_and_reported_without_child_cleanup_actions(self):
        def factory(*_, **__):
            raise OSError('SYNTHETIC_PRIVATE_LAUNCH_DETAIL')
        result = supervisor.supervise(['fixed-synthetic-child'], self.output,
                                      popen_factory=factory, clock=self.clock.now,
                                      sleep=self.clock.sleep, startup_limit=1, heartbeat_limit=1)
        self.assertEqual(result['status'], '失败')
        self.assertFalse(result['child_started'])
        self.assertTrue(result['child_cleanup_confirmed'])
        self.assertNotIn('SYNTHETIC_PRIVATE_LAUNCH_DETAIL', json.dumps(result))
        self.assertTrue((self.output / 'supervisor.json').is_file())


if __name__ == '__main__':
    unittest.main()
