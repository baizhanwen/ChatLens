"""Synthetic transaction/crash tests. Never open real WeChat or private state."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / 'scripts/windows-proof/continuous_store.py'
SPEC = importlib.util.spec_from_file_location('chatlens_continuous_store', MODULE)
store_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(store_module)
CollectorStore, StoreError = store_module.CollectorStore, store_module.StoreError
BINDING = {'selected_scope': ['synthetic_private', 'synthetic_group'], 'account_fingerprint': 'f' * 64}
EPOCHS = {'message_0': {'salt_sha256': 'a' * 64, 'file_identity': [1, 2], 'key_sha256': 'b' * 64}}
CURSORS = {'message_0': {'private': {'local_id': 4, 'present': True}, 'group': {'local_id': 2, 'present': True}}}
NEXT = {'message_0': {'private': {'local_id': 5, 'present': True}, 'group': {'local_id': 2, 'present': True}}}


def event(number=5, body='SYNTHETIC_PRIVATE_MESSAGE', key=None):
    return {'source_key': key or hashlib.sha256(('synthetic-source-%d' % number).encode()).hexdigest(),
            'alias': 'private', 'source_local_id': number, 'sent_at': 1700000000,
            'sender_id': 'synthetic_sender', 'body': body,
            'observed_at': datetime.now(timezone.utc).isoformat()}


class ContinuousStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-store-')
        self.directory = Path(self.tmp.name) / 'private-state'
        self.directory.mkdir(mode=0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_once_restore_exact_baseline_cursor_and_body(self):
        with CollectorStore(self.directory, BINDING) as storage:
            self.assertFalse(storage.snapshot()['initialized'])
            baseline = storage.initialize(CURSORS, EPOCHS)
            with self.assertRaisesRegex(StoreError, 'collector_baseline_already_initialized'):
                storage.initialize(NEXT, EPOCHS)
            result = storage.commit(CURSORS, NEXT, [event()], EPOCHS)
            self.assertEqual(result, {'inserted': 1, 'duplicates': 0, 'total_messages': 1})
            self.assertEqual(storage.stats(), {'total_messages': 1, 'commits': 1})
        with CollectorStore(self.directory, BINDING) as storage:
            self.assertTrue(storage.resumed)
            snapshot = storage.snapshot()
            self.assertEqual(snapshot['cursors'], NEXT)
            self.assertEqual(snapshot['epochs'], EPOCHS)
            self.assertEqual(snapshot['baseline_completed_at'], baseline['baseline_completed_at'])
        with sqlite3.connect(self.directory / store_module.DB_NAME) as connection:
            self.assertEqual(connection.execute('SELECT body FROM messages').fetchone()[0], 'SYNTHETIC_PRIVATE_MESSAGE')

    def test_same_body_separate_physical_messages_survive(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
            result = storage.commit(CURSORS, NEXT, [event(5), event(6)], EPOCHS)
            self.assertEqual(result['inserted'], 2)
            self.assertEqual(storage.stats()['total_messages'], 2)

    def test_repeat_source_dedupes_but_changed_body_is_refused_atomically(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
            first = event()
            storage.commit(CURSORS, NEXT, [first], EPOCHS)
            self.assertEqual(storage.commit(NEXT, NEXT, [event()], EPOCHS)['duplicates'], 1)
            changed = event(body='SYNTHETIC_CHANGED')
            with self.assertRaisesRegex(StoreError, 'collector_source_message_conflict'):
                storage.commit(NEXT, NEXT, [event(6), changed], EPOCHS)
            self.assertEqual(storage.stats()['total_messages'], 1)
            self.assertEqual(storage.stats()['commits'], 2)

    def test_stale_cursors_and_changed_epochs_do_not_advance(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
            with self.assertRaisesRegex(StoreError, 'collector_checkpoint_conflict'):
                storage.commit({}, NEXT, [event()], EPOCHS)
            with self.assertRaisesRegex(StoreError, 'collector_source_epoch_changed'):
                storage.commit(CURSORS, NEXT, [event()], {'changed': True})
            self.assertEqual(storage.snapshot()['cursors'], CURSORS)
            self.assertEqual(storage.stats()['total_messages'], 0)

    def test_precommit_fault_rolls_back_messages_and_cursors(self):
        for fault_stage in ('after_message_inserts', 'before_commit'):
            with self.subTest(stage=fault_stage):
                directory = self.directory / fault_stage
                directory.mkdir(mode=0o700)
                def fail(stage):
                    if stage == fault_stage:
                        raise RuntimeError('SYNTHETIC_PRIVATE_DETAIL_MUST_NOT_ESCAPE')
                with CollectorStore(directory, BINDING, fault_hook=fail) as storage:
                    storage.initialize(CURSORS, EPOCHS)
                    with self.assertRaises(StoreError) as caught:
                        storage.commit(CURSORS, NEXT, [event()], EPOCHS)
                    self.assertEqual(str(caught.exception), 'collector_batch_commit_failed')
                    self.assertEqual(storage.snapshot()['cursors'], CURSORS)
                    self.assertEqual(storage.stats()['total_messages'], 0)
                with CollectorStore(directory, BINDING) as storage:
                    storage.commit(CURSORS, NEXT, [event()], EPOCHS)
                    self.assertEqual(storage.stats()['total_messages'], 1)

    def test_baseline_fault_does_not_publish_partial_initialization(self):
        def fail(stage):
            if stage == 'before_baseline_commit':
                raise RuntimeError('SYNTHETIC_FAILURE')
        with CollectorStore(self.directory, BINDING, fault_hook=fail) as storage:
            with self.assertRaisesRegex(StoreError, 'collector_baseline_commit_failed'):
                storage.initialize(CURSORS, EPOCHS)
            self.assertFalse(storage.snapshot()['initialized'])
        with CollectorStore(self.directory, BINDING) as storage:
            self.assertFalse(storage.resumed)
            storage.initialize(CURSORS, EPOCHS)

    def test_after_commit_fault_keeps_durable_cursor_and_exactly_one_row(self):
        def fail(stage):
            if stage == 'after_commit':
                raise RuntimeError('SYNTHETIC_POST_COMMIT')
        with CollectorStore(self.directory, BINDING, fault_hook=fail) as storage:
            storage.initialize(CURSORS, EPOCHS)
            with self.assertRaisesRegex(RuntimeError, 'SYNTHETIC_POST_COMMIT'):
                storage.commit(CURSORS, NEXT, [event()], EPOCHS)
        with CollectorStore(self.directory, BINDING) as storage:
            self.assertEqual(storage.snapshot()['cursors'], NEXT)
            self.assertEqual(storage.stats()['total_messages'], 1)
            self.assertEqual(storage.commit(NEXT, NEXT, [event()], EPOCHS)['duplicates'], 1)

    def test_real_child_abrupt_exit_before_or_after_commit(self):
        child = '''
import importlib.util,json,os,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('synthetic_store',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
binding,cursors,next_cursors,epochs,event=json.loads(sys.argv[3])
def fault(stage):
 if stage==sys.argv[4]:os._exit(73)
s=m.CollectorStore(Path(sys.argv[2]),binding,fault_hook=fault)
s.commit(cursors,next_cursors,[event],epochs)
'''
        for stage in ('before_commit', 'after_commit'):
            with self.subTest(stage=stage):
                directory = self.directory / stage
                directory.mkdir(mode=0o700)
                with CollectorStore(directory, BINDING) as storage:
                    storage.initialize(CURSORS, EPOCHS)
                process = subprocess.run([sys.executable, '-B', '-c', child, str(MODULE), str(directory),
                                          json.dumps([BINDING, CURSORS, NEXT, EPOCHS, event()]), stage],
                                         capture_output=True, timeout=15)
                self.assertEqual(process.returncode, 73, process.stderr.decode())
                self.assertEqual(process.stdout, b'')
                with CollectorStore(directory, BINDING) as storage:
                    if stage == 'before_commit':
                        self.assertEqual(storage.snapshot()['cursors'], CURSORS)
                        self.assertEqual(storage.stats()['total_messages'], 0)
                        storage.commit(CURSORS, NEXT, [event()], EPOCHS)
                    else:
                        self.assertEqual(storage.snapshot()['cursors'], NEXT)
                    self.assertEqual(storage.stats()['total_messages'], 1)

    def test_real_separate_process_lock_contention(self):
        child = '''
import importlib.util,json,sys
spec=importlib.util.spec_from_file_location('synthetic_store',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
try:
 s=m.CollectorStore(sys.argv[2],json.loads(sys.argv[3]));s.close()
except m.StoreError as error:
 print(error.code);raise SystemExit(19)
'''
        with CollectorStore(self.directory, BINDING):
            process = subprocess.run([sys.executable, '-B', '-c', child, str(MODULE), str(self.directory), json.dumps(BINDING)], capture_output=True, timeout=15)
            self.assertEqual(process.returncode, 19)
            self.assertEqual(process.stdout.decode().strip(), 'collector_already_running_or_lock_unavailable')
        with CollectorStore(self.directory, BINDING):
            pass

    def test_missing_database_or_identity_refuses_new_baseline(self):
        for name in (store_module.DB_NAME, store_module.IDENTITY_NAME):
            with self.subTest(name=name):
                directory = self.directory / name
                directory.mkdir(mode=0o700)
                with CollectorStore(directory, BINDING) as storage:
                    storage.initialize(CURSORS, EPOCHS)
                (directory / name).unlink()
                with self.assertRaisesRegex(StoreError, 'collector_state_pair_incomplete'):
                    CollectorStore(directory, BINDING)

    def test_deleted_database_and_marker_still_refuses_reset_when_lock_survives(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
        for name in (store_module.DB_NAME, store_module.IDENTITY_NAME):
            (self.directory / name).unlink()
        with self.assertRaisesRegex(StoreError, 'collector_state_pair_incomplete'):
            CollectorStore(self.directory, BINDING)

    def test_wrong_scope_and_corrupt_database_are_refused(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
        with self.assertRaisesRegex(StoreError, 'collector_state_binding_mismatch'):
            CollectorStore(self.directory, {'different': True})
        database = self.directory / store_module.DB_NAME
        database.write_bytes(b'SYNTHETIC_CORRUPT_SQLITE')
        with self.assertRaisesRegex(StoreError, 'collector_state_database_invalid'):
            CollectorStore(self.directory, BINDING)

    def test_unknown_file_symlink_and_nonprivate_permissions_are_refused(self):
        extra = self.directory / 'unknown.txt'
        extra.write_text('synthetic')
        with self.assertRaisesRegex(StoreError, 'collector_state_unexpected_file'):
            CollectorStore(self.directory, BINDING)
        extra.unlink()
        if hasattr(os, 'symlink'):
            outside = self.directory.parent / 'synthetic-outside'
            outside.write_text('synthetic')
            try:
                (self.directory / store_module.DB_NAME).symlink_to(outside)
            except OSError:
                pass
            else:
                with self.assertRaisesRegex(StoreError, 'collector_state_file_unsafe'):
                    CollectorStore(self.directory, BINDING)
                (self.directory / store_module.DB_NAME).unlink()
        if os.name != 'nt':
            self.directory.chmod(0o755)
            with self.assertRaisesRegex(StoreError, 'collector_state_permissions_unsafe'):
                CollectorStore(self.directory, BINDING)
            self.directory.chmod(0o700)

    def test_replaced_database_during_run_stops_before_commit(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
            database = self.directory / store_module.DB_NAME
            replacement = self.directory.parent / 'replacement.sqlite3'
            replacement.write_bytes(database.read_bytes())
            replacement.chmod(0o600)
            try:
                os.replace(replacement, database)
            except PermissionError:
                if os.name == 'nt':
                    return  # Windows denies replacing this open database itself.
                raise
            with self.assertRaisesRegex(StoreError, 'collector_state_file_identity_changed'):
                storage.commit(CURSORS, NEXT, [event()], EPOCHS)

    def test_invalid_event_does_not_write_or_advance(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
            invalid = event();invalid['source_local_id'] = True
            with self.assertRaises(StoreError):
                storage.commit(CURSORS, NEXT, [invalid], EPOCHS)
            self.assertEqual(storage.snapshot()['cursors'], CURSORS)
            self.assertEqual(storage.stats()['total_messages'], 0)

    def test_controlled_summary_six_codes_missing_then_complete_without_private_values(self):
        tokens = {'private': ['SYNTHETIC_PRIVATE_%d' % i for i in (1, 2, 3)],
                  'group': ['SYNTHETIC_GROUP_%d' % i for i in (1, 2, 3)]}
        senders = {'private': 'synthetic_sender', 'group': 'synthetic_sender'}
        rows = []
        for index in (1, 2):
            for alias in ('private', 'group'):
                row = event(len(rows) + 5, tokens[alias][index - 1]);row['alias'] = alias
                rows.append(row)
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
            storage.commit(CURSORS, NEXT, rows, EPOCHS)
            before = storage.snapshot(), storage.stats()
            first = storage.controlled_summary(tokens, senders)
            self.assertEqual(first['status'], '未验证')
            self.assertFalse(first['all_expected_tokens_observed'])
            self.assertEqual(first['observed_events'], 4)
            self.assertEqual((storage.snapshot(), storage.stats()), before)
            self.assertEqual(storage.controlled_summary({}, {}), {
                'status': '未验证', 'reason': 'controlled_selected_text_incomplete',
                'all_expected_tokens_observed': False, 'observed_events': 0,
                'unique_source_messages': 0, 'conversations': []})
            later = []
            for number, alias in enumerate(('private', 'group'), 10):
                row = event(number, tokens[alias][2]);row['alias'] = alias;later.append(row)
            storage.commit(NEXT, NEXT, later, EPOCHS)
            summary = storage.controlled_summary(tokens, senders)
            self.assertEqual(summary['status'], '通过')
            self.assertTrue(summary['all_expected_tokens_observed'])
            self.assertEqual(summary['observed_events'], 6)
            self.assertEqual(summary['unique_source_messages'], 6)
            for conversation in summary['conversations']:
                for outcome in conversation['tokens']:
                    self.assertEqual(outcome['matched_count'], 1)
                    self.assertTrue(outcome['verified'])
                    self.assertEqual(outcome['source_sent_at'], 1700000000)
                    self.assertIsNotNone(outcome['observed_at'])
            serialized = json.dumps(summary)
            self.assertNotIn('synthetic_sender', serialized)
            self.assertNotIn(rows[0]['body'], serialized)
            self.assertNotIn(rows[0]['source_key'], serialized)

    def test_controlled_summary_rejects_extra_text_wrong_sender_and_duplicate_tokens(self):
        token = 'SYNTHETIC_CONTROLLED_1'
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
            storage.commit(CURSORS, NEXT, [event(5, token + '_EXTRA')], EPOCHS)
            summary = storage.controlled_summary({'private': [token]}, {'private': 'synthetic_sender'})
            outcome = summary['conversations'][0]['tokens'][0]
            self.assertEqual(outcome['matched_count'], 1)
            self.assertFalse(outcome['verified'])
            self.assertEqual(summary['status'], '失败')
            storage.commit(NEXT, NEXT, [event(6, token)], EPOCHS)
            outcome = storage.controlled_summary({'private': [token]}, {'private': 'synthetic_sender'})['conversations'][0]['tokens'][0]
            self.assertEqual(outcome['matched_count'], 2)
            self.assertFalse(outcome['verified'])
            self.assertIsNone(outcome['observed_at'])
            another = 'SYNTHETIC_CONTROLLED_2'
            storage.commit(NEXT, NEXT, [event(7, another)], EPOCHS)
            for sender in (None, 'synthetic_wrong_sender'):
                with self.subTest(sender=sender):
                    outcome = storage.controlled_summary({'private': [another]}, {'private': sender})['conversations'][0]['tokens'][0]
                    self.assertEqual(outcome['matched_count'], 1)
                    self.assertFalse(outcome['verified'])
            with self.assertRaisesRegex(StoreError, 'collector_controlled_test_configuration_invalid'):
                storage.controlled_summary({'private': [token, token]}, {'private': 'synthetic_sender'})

    def test_unexpected_trigger_in_existing_database_is_refused(self):
        with CollectorStore(self.directory, BINDING) as storage:
            storage.initialize(CURSORS, EPOCHS)
        with sqlite3.connect(self.directory / store_module.DB_NAME) as connection:
            connection.execute('CREATE TRIGGER synthetic_trigger AFTER INSERT ON messages BEGIN DELETE FROM messages; END')
        with self.assertRaisesRegex(StoreError, 'collector_state_schema_invalid'):
            CollectorStore(self.directory, BINDING)


if __name__ == '__main__':
    unittest.main()
