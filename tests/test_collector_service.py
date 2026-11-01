"""Synthetic collector-service lifecycle/configuration checks; no real WeChat."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
import collector_service as service
import collector_private as private


PEER, GROUP = 'synthetic_service_peer', 'synthetic_service_group@chatroom'
PRIVATE_DETAIL = 'SYNTHETIC_PRIVATE_DETAIL_NEVER_PUBLIC'


def table(chat):
    return 'Msg_' + hashlib.md5(chat.encode()).hexdigest()


def report():
    return {'new_messages_this_run': 0, 'duplicate_events_skipped_this_run': 0,
            'total_messages': 0, 'resumed_from_checkpoint': False, 'paused_polls': 0, 'busy_retries': 0}


class FakeHeartbeat:
    def __init__(self):
        self.rows = []

    def emit(self, *args):
        self.rows.append(args)


class FakeReader:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = 0
        self.committed = 0

    def poll(self):
        value = self.steps[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        self.committed += value.get('new_messages', 0)
        return value


class FakeNative:
    def __init__(self, running=True):
        self.running, self.calls = running, 0

    def sample(self):
        self.calls += 1
        return {'running': self.running, 'probe_succeeded': True, 'frontmost': False, 'screen_locked': False}


class CollectorServiceLoopTests(unittest.TestCase):
    def run_loop(self, reader, native=None, *, final_count=None, initial=None):
        data = initial or report()
        heartbeat = FakeHeartbeat()
        elapsed = [0.0]
        def sleep(seconds):
            elapsed[0] += seconds
        target = len(reader.steps) if final_count is None else final_count
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            service.collect_loop(reader, object(), native or FakeNative(), lambda: reader.calls >= target,
                                 heartbeat, data, sleep=sleep, clock=lambda: elapsed[0])
        return data, heartbeat.rows, output.getvalue()

    def test_paused_reader_never_commits_and_recovery_resumes_same_loop(self):
        paused = service.CollectorError('continuous_native_state_not_ready')
        reader = FakeReader([paused, paused, {'new_messages': 2, 'duplicates': 0, 'total_messages': 2,
                                            'body': PRIVATE_DETAIL}])
        data, rows, stdout = self.run_loop(reader)
        self.assertEqual(data['paused_polls'], 2)
        self.assertEqual(data['new_messages_this_run'], 2)
        self.assertEqual(reader.committed, 2)
        self.assertEqual([row[0] for row in rows], ['paused', 'paused', 'collecting'])
        self.assertNotIn(PRIVATE_DETAIL, stdout)
        self.assertIn('2', stdout)

    def test_unknown_native_state_can_pause_then_recover_without_advancing_counts(self):
        reader = FakeReader([service.CollectorError('continuous_native_state_unavailable'),
                             {'new_messages': 1, 'duplicates': 0, 'total_messages': 4}])
        initial = report(); initial['total_messages'] = 3; initial['resumed_from_checkpoint'] = True
        data, rows, _ = self.run_loop(reader, initial=initial)
        self.assertEqual(data['paused_polls'], 1)
        self.assertEqual(rows[0], ('paused', 3, 0, True))
        self.assertEqual(rows[1], ('collecting', 4, 1, True))

    def test_source_busy_is_bounded_and_does_not_advance_checkpoint(self):
        reader = FakeReader([service.CollectorError('continuous_source_busy') for _ in range(6)])
        data, heartbeat = report(), FakeHeartbeat()
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(service.CollectorError, 'continuous_source_busy'):
                service.collect_loop(reader, object(), FakeNative(), lambda: False, heartbeat, data,
                                     sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(data['busy_retries'], 5)
        self.assertEqual(reader.committed, 0)
        self.assertEqual(reader.calls, 6)
        self.assertEqual(data['total_messages'], 0)

    def test_success_resets_busy_retry_budget(self):
        steps = [service.CollectorError('continuous_source_busy') for _ in range(5)]
        steps += [{'new_messages': 1, 'duplicates': 0, 'total_messages': 1}]
        steps += [service.CollectorError('continuous_source_busy') for _ in range(5)]
        reader = FakeReader(steps)
        data, _, _ = self.run_loop(reader)
        self.assertEqual(data['busy_retries'], 10)
        self.assertEqual(data['new_messages_this_run'], 1)

    def test_source_process_exit_stops_before_poll_preserving_counts(self):
        reader = FakeReader([{'new_messages': 1, 'duplicates': 0, 'total_messages': 1}])
        data = report(); data['total_messages'] = 7
        with self.assertRaisesRegex(service.CollectorError, 'collector_source_process_unavailable_restart_required'):
            service.collect_loop(reader, object(), FakeNative(False), lambda: False, FakeHeartbeat(), data,
                                 sleep=lambda _: None, clock=lambda: 0)
        self.assertEqual(reader.calls, 0)
        self.assertEqual(data['total_messages'], 7)

    def test_stop_request_does_not_poll_or_reset_existing_counts(self):
        reader = FakeReader([])
        initial = report(); initial['total_messages'] = 9
        data, rows, stdout = self.run_loop(reader, final_count=0, initial=initial)
        self.assertEqual(data['total_messages'], 9)
        self.assertEqual(reader.calls, 0)
        self.assertEqual(rows, [])
        self.assertEqual(stdout, '')

    def test_non_retryable_error_is_redacted_for_report_reason(self):
        error = RuntimeError(PRIVATE_DETAIL)
        self.assertEqual(service.safe_code(error), 'collector_operation_failed')
        self.assertEqual(service.safe_code(service.CollectorError('bad ' + PRIVATE_DETAIL)), 'collector_operation_failed')
        reader = FakeReader([error])
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(RuntimeError):
                service.collect_loop(reader, object(), FakeNative(), lambda: False, FakeHeartbeat(), report(),
                                     sleep=lambda _: None, clock=lambda: 0)
        self.assertNotIn(PRIVATE_DETAIL, stdout.getvalue())


class CollectorServiceSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-service-')
        self.root = Path(self.tmp.name).resolve()
        self.account = self.root / 'synthetic-account'; self.account.mkdir()
        self.exe = self.root / 'Weixin.exe'; self.exe.write_bytes(b'SYNTHETIC_NOT_EXECUTED')
        self.old = self.root / 'old-r2-private'; self.old.mkdir(mode=0o700)
        self.destination = self.root / 'collector-data'
        self.selected = self.old / 'selected-config.json'
        self.config = {'conversations': [
            {'alias': 'private_test', 'chat_id': PEER, 'table': table(PEER), 'expected_sender_id': PEER},
            {'alias': 'group_test', 'chat_id': GROUP, 'table': table(GROUP), 'expected_sender_id': PEER}]}
        self.original = {'db_root': str(self.account), 'config': self.config,
                         'tokens': {'private_test': ['OLD_SYNTHETIC_PRIVATE_1', 'OLD_SYNTHETIC_PRIVATE_2'],
                                    'group_test': ['OLD_SYNTHETIC_GROUP_1', 'OLD_SYNTHETIC_GROUP_2']},
                         'run_nonce': 'SYNTHETIC_OLD_NONCE'}
        self.keys = {'schema_version': 1, 'db_root': str(self.account), 'exe_path': str(self.exe),
                     'keys': {relative: {'enc_key': ('%02x' % (index + 1)) * 32}
                              for index, relative in enumerate(service.RELATIVES)}}
        private.write_private_json_new(self.selected, self.original)
        private.write_private_json_new(self.old / 'selected-keys.json', self.keys)

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_setup_copies_only_selected_scope_and_generates_stable_new_six_code_plan(self):
        before = {p.name: p.read_bytes() for p in self.old.iterdir()}
        scope, root, exe, values, expected, binding = service.prepare_session(self.destination, self.selected)
        self.assertEqual(root, self.account)
        self.assertEqual(exe, self.exe)
        self.assertEqual(set(values), set(service.RELATIVES))
        self.assertEqual(scope['config'], self.config)
        self.assertNotIn('tokens', scope)
        self.assertNotIn('run_nonce', scope)
        all_tokens = [token for rows in scope['test_tokens'].values() for token in rows]
        self.assertEqual(len(set(all_tokens)), 6)
        self.assertTrue(all(token.startswith('CLC-') for token in all_tokens))
        self.assertEqual(expected, {'private_test': PEER, 'group_test': PEER})
        self.assertEqual(binding['config'], self.config)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.old.iterdir()})
        self.assertEqual({p.name for p in (self.destination / 'private').iterdir()}, {'keys.json', 'scope.json', 'store'})

    def test_resume_uses_private_copies_after_old_r2_files_are_removed(self):
        first = service.prepare_session(self.destination, self.selected)
        for path in self.old.iterdir():
            path.unlink()
        restored = service.prepare_session(self.destination)
        self.assertEqual(first, restored)

    def test_existing_session_ignores_new_source_instead_of_overwriting_scope_or_keys(self):
        first = service.prepare_session(self.destination, self.selected)
        private_paths = [self.destination / 'private' / name for name in ('keys.json', 'scope.json')]
        before = {p.name: p.read_bytes() for p in private_paths}
        bogus = self.root / 'nonexistent-other-selected-config.json'
        second = service.prepare_session(self.destination, bogus)
        self.assertEqual(first, second)
        self.assertEqual(before, {p.name: p.read_bytes() for p in private_paths})

    def test_wrong_account_or_extra_conversation_is_rejected_before_session_creation(self):
        keys = copy.deepcopy(self.keys); keys['db_root'] = str(self.root)
        key_path = self.old / 'selected-keys.json'
        key_path.unlink(); private.write_private_json_new(key_path, keys)
        with self.assertRaisesRegex(service.CollectorError, 'collector_configuration_account_mismatch'):
            service.prepare_session(self.destination, self.selected)
        self.assertFalse(self.destination.exists())
        key_path.unlink(); private.write_private_json_new(key_path, self.keys)
        original = copy.deepcopy(self.original)
        original['config']['conversations'].append({'alias': 'other', 'chat_id': 'synthetic_other', 'table': table('synthetic_other')})
        self.selected.unlink(); private.write_private_json_new(self.selected, original)
        with self.assertRaisesRegex(Exception, 'continuous_configuration_invalid'):
            service.prepare_session(self.destination, self.selected)
        self.assertFalse(self.destination.exists())

    def test_missing_existing_scope_or_store_fails_without_reinitialization(self):
        service.prepare_session(self.destination, self.selected)
        store = self.destination / 'private/store'
        store.rmdir()
        with self.assertRaises(Exception):
            service.prepare_session(self.destination, self.selected)
        self.assertFalse(store.exists())
        (self.destination / 'private/scope.json').unlink()
        with self.assertRaises(Exception):
            service.prepare_session(self.destination, self.selected)
        self.assertFalse((self.destination / 'private/scope.json').exists())

    def test_public_heartbeat_only_contains_fixed_counts_and_never_private_details(self):
        output = self.root / 'public-output'; output.mkdir()
        heartbeat = service.Heartbeat(output)
        heartbeat.emit('collecting', 4, 2, True)
        value = json.loads((output / 'status.json').read_text())
        self.assertEqual(set(value), {'schema_version', 'kind', 'sequence', 'stage', 'recorded_at_utc',
                                     'total_messages', 'new_messages_this_run', 'resumed_from_checkpoint'})
        serialized = json.dumps(value)
        for forbidden in (PEER, GROUP, str(self.root), PRIVATE_DETAIL, *[v['enc_key'] for v in self.keys['keys'].values()]):
            self.assertNotIn(forbidden, serialized)
        with self.assertRaisesRegex(service.CollectorError, 'collector_status_invalid'):
            heartbeat.emit('collecting', PRIVATE_DETAIL)

    def test_public_status_refuses_symlink_target(self):
        output = self.root / 'public-output'; output.mkdir()
        other = self.root / 'protected-untouched.json'; other.write_text(PRIVATE_DETAIL)
        target = output / 'status.json'
        try:
            target.symlink_to(other)
        except OSError:
            self.skipTest('synthetic symlink unavailable')
        with self.assertRaisesRegex(service.CollectorError, 'collector_public_output_unsafe'):
            service.Heartbeat(output).emit('starting')
        self.assertEqual(other.read_text(), PRIVATE_DETAIL)

    def test_private_preflight_failure_stops_before_reader_poll(self):
        reader = FakeReader([{'new_messages': 1, 'duplicates': 0, 'total_messages': 1}])
        def preflight():
            raise private.PrivateError('collector_private_permissions_invalid')
        with self.assertRaisesRegex(private.PrivateError, 'collector_private_permissions_invalid'):
            service.collect_loop(reader, object(), FakeNative(), lambda: False, FakeHeartbeat(), report(),
                                 sleep=lambda _: None, clock=lambda: 0, preflight=preflight)
        self.assertEqual(reader.calls, 0)

    def test_main_shows_ready_only_after_reader_has_established_baseline_and_stops_cleanly(self):
        import windows_keyproof_core as proof
        import windows_keyproof_platform as platform
        import windows_selected_native as native_module
        events = []
        output = self.root / 'public-output'; output.mkdir()
        (output / 'stop.request').write_bytes(b'SYNTHETIC_STOP')
        fake_package = types.ModuleType('sqlcipher3'); fake_package.__path__ = []
        fake_package.dbapi2 = types.ModuleType('sqlcipher3.dbapi2')

        class DummyStore:
            def __init__(self, *args):
                events.append('store')
            def stats(self):
                return {'total_messages': 0, 'commits': 0}
            def controlled_summary(self, *args):
                return {'status': '未验证', 'observed_events': 0}
            def snapshot(self):
                return {'initialized': True, 'last_commit_at': '2026-01-01T00:00:00+00:00'}
            def close(self):
                events.append('store_closed')

        class DummyReader:
            resumed = False
            baseline_completed_at = '2026-01-01T00:00:00+00:00'
            def __init__(self, *args):
                events.append('baseline_committed')
            def close(self):
                events.append('reader_closed')

        class DummyNative(FakeNative):
            def __init__(self, *args):
                super().__init__()
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        show_plan = service.show_plan
        def show(*args):
            events.append('ready')
            return show_plan(*args)

        argv = ['collector_service.py', '--data-dir', str(self.destination), '--selected-config', str(self.selected),
                '--pid', '123', '--creation-time', '456', '--output', str(output)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(service.sys, 'argv', argv))
            stack.enter_context(patch.object(service.sys, 'platform', 'win32'))
            stack.enter_context(patch.object(service.signal, 'signal'))
            stack.enter_context(patch.dict(sys.modules, {'sqlcipher3': fake_package, 'sqlcipher3.dbapi2': fake_package.dbapi2}))
            stack.enter_context(patch.object(proof, 'page_authenticates', return_value=True))
            stack.enter_context(patch.object(platform, 'read_page', return_value=b'SYNTHETIC_PAGE'))
            stack.enter_context(patch.object(native_module, 'detect_shm_preconditions', return_value={}))
            stack.enter_context(patch.object(native_module, 'NativeState', DummyNative))
            stack.enter_context(patch.object(service, 'CollectorStore', DummyStore))
            stack.enter_context(patch.object(service, 'ContinuousReader', DummyReader))
            stack.enter_context(patch.object(service, 'show_plan', side_effect=show))
            stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            self.assertEqual(service.main(), 0)
        self.assertLess(events.index('baseline_committed'), events.index('ready'))
        saved = json.loads((output / 'result-to-send.json').read_text())
        self.assertTrue(saved['normal_stop_requested'])
        self.assertTrue(saved['checkpoint_saved'])
        self.assertEqual(saved['reason'], 'collector_stopped_with_committed_checkpoint')
        self.assertEqual(saved['new_messages_this_run'], 0)
        self.assertEqual(json.loads((output / 'status.json').read_text())['stage'], 'stopped')
        for forbidden in (PEER, GROUP, str(self.root), PRIVATE_DETAIL):
            self.assertNotIn(forbidden, json.dumps(saved))
        scope = private.read_private_json(self.destination / 'private/scope.json')
        for token in [token for rows in scope['test_tokens'].values() for token in rows]:
            self.assertIn(token, stdout.getvalue())
            self.assertNotIn(token, json.dumps(saved))

    def test_resume_plan_does_not_repeat_already_issued_codes_or_show_contact_ids(self):
        scope, *_ = service.prepare_session(self.destination, self.selected)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            service.show_plan(scope, {}, True)
        text = output.getvalue()
        self.assertIn('READY', text)
        for token in scope['test_tokens']['private_test'][:2] + scope['test_tokens']['group_test'][:2]:
            self.assertNotIn(token, text)
        for token in (scope['test_tokens']['private_test'][2], scope['test_tokens']['group_test'][2]):
            self.assertNotIn(token, text)
        self.assertNotIn(PEER, text)
        self.assertNotIn(GROUP, text)
        self.assertNotIn(str(self.root), text)


if __name__ == '__main__':
    unittest.main()
