"""Synthetic same-account history inheritance; never open real chat or keys."""
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
sys.path.insert(0, str(MODULES))
from collector_private import write_private_json_new
from continuous_store import CollectorStore, StoreError
from conversation_scope import alias_for, normalize_conversations
import scope_history as history

GROUP = 'synthetic_history_group@chatroom'
PRIVATE = 'synthetic_history_peer'
ADDED = 'synthetic_history_added'
OTHER = 'synthetic_history_unselected'
SHARDS = ('message/message_0.db', 'message/message_1.db')
NOW = datetime(2026, 11, 2, 10, tzinfo=timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def epochs():
    return {name: {'file_identity': [7, index + 100],
                   'salt_sha256': hashlib.sha256(('synthetic-salt-' + name).encode()).hexdigest(),
                   'key_sha256': hashlib.sha256(('synthetic-key-fingerprint-' + name).encode()).hexdigest()}
            for index, name in enumerate(SHARDS)}


class ScopeHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-history-')
        self.root = Path(self.temporary.name).resolve()
        self.account = self.root / 'synthetic-account-a'
        self.account.mkdir()
        self.other_account = self.root / 'synthetic-account-b'
        self.other_account.mkdir()
        self.executable = self.root / 'WeChat.exe'
        self.executable.write_bytes(b'SYNTHETIC_EXECUTABLE_NOT_RUN')
        self.epoch = epochs()
        self.profiles = {}
        self.stores = []

    def tearDown(self):
        for store in self.stores:
            store.close()
        self.temporary.cleanup()

    def profile(self, name, chats, *, legacy=False, initialize=True, cursor=10,
                platform='macos', account=None, create_store=True, epoch=None):
        account = self.account if account is None else account
        path = self.root / name
        for directory in (path, path / 'private', path / 'private/store'):
            directory.mkdir(mode=0o700)
        rows = [{'alias': ('group_test' if chat.endswith('@chatroom') else 'private_test')
                          if legacy else alias_for(chat), 'chat_id': chat} for chat in chats]
        config = {'conversations': normalize_conversations({'conversations': rows})}
        identifier = hashlib.sha256(name.encode()).hexdigest()[:32]
        root_identity = [account.stat().st_dev, account.stat().st_ino]
        scope = {'schema_version': 1, 'session_id': identifier, 'db_root': str(account), 'config': config}
        if platform == 'macos':
            scope.update(platform='macos', root_identity=root_identity,
                         shards=list(SHARDS), client_version='4.1.11')
            binding = {'schema_version': 1, 'platform': 'macos', 'session_id': identifier,
                       'root': str(account), 'root_identity': root_identity,
                       'config': config, 'shards': list(SHARDS)}
        else:
            scope.update(exe_path=str(self.executable), test_tokens={})
            binding = {'schema_version': 1, 'session_id': identifier, 'root': str(account),
                       'root_identity': root_identity, 'config': config,
                       'executable': str(self.executable), 'test_tokens': {}}
        write_private_json_new(path / 'private/scope.json', scope)
        cursors = {relative: {row['alias']: {'local_id': cursor, 'present': True}
                             for row in config['conversations']} for relative in SHARDS}
        profile = {'path': path, 'binding': binding, 'config': config, 'scope': scope,
                   'cursors': cursors, 'epochs': copy.deepcopy(self.epoch if epoch is None else epoch)}
        self.profiles[name] = profile
        if create_store:
            profile['store'] = self.open_store(profile)
            if initialize:
                profile['store'].initialize(cursors, profile['epochs'])
        return profile

    def open_store(self, profile, **options):
        store = CollectorStore(profile['path'] / 'private/store', profile['binding'], **options)
        self.stores.append(store)
        return store

    def event(self, profile, chat, number, *, shard=0, body=None):
        alias = next(row['alias'] for row in profile['config']['conversations'] if row['chat_id'] == chat)
        relative = SHARDS[shard]
        return {'source_key': digest([relative, chat, number, profile['epochs'][relative]]),
                'alias': alias, 'source_local_id': number, 'sent_at': 1700000000 + number,
                'sender_id': 'synthetic_history_sender',
                'body': body or 'SYNTHETIC_HISTORY_BODY_%s_%d_%d' % (chat, shard, number),
                'observed_at': NOW}

    def commit(self, profile, *events):
        store = profile['store']
        return store.commit(store.snapshot()['cursors'], store.snapshot()['cursors'],
                            list(events), profile['epochs'])

    def saved(self, profile):
        with sqlite3.connect(profile['path'] / 'private/store/collector.sqlite3') as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute('SELECT rowid,' + history.EVENT_COLUMNS + ' FROM messages ORDER BY rowid')]

    def plan(self, target, sources, continuity=None):
        return history.write_history_plan(target['path'], [source['path'] for source in sources],
                                          continuity_source=continuity['path'] if continuity else None)

    def apply(self, target, *, initialize=False):
        return history.apply_history_plan(target['store'], target['cursors'], target['epochs'], initialize=initialize)

    def plan_path(self, profile):
        return profile['path'] / 'private/history-plan.json'

    def rewrite(self, path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def assert_fixed_error(self, error):
        self.assertRegex(str(error), r'^(scope_history|collector)_[a-z_]+$')
        self.assertNotIn(str(self.root), str(error))
        self.assertNotIn('SYNTHETIC_HISTORY_BODY', str(error))

    def test_metadata_only_plan_can_precede_target_store_and_never_reads_keys_or_bodies(self):
        source = self.profile('source', [PRIVATE, GROUP], legacy=True)
        target = self.profile('target', [GROUP, ADDED], create_store=False)
        with mock.patch.object(history.CollectorWebData, '_open', side_effect=AssertionError('must not open source SQLite while planning')):
            result = self.plan(target, [source], source)
        self.assertEqual(result, {'planned': True, 'source_count': 1, 'continuous_source_selected': True})
        plan = json.loads(self.plan_path(target).read_text())
        self.assertEqual(set(plan), {'schema_version', 'nonce', 'target_binding_digest', 'sources', 'continuity_source'})
        self.assertEqual(plan['target_binding_digest'], digest(target['binding']))
        self.assertNotIn('SYNTHETIC_HISTORY_BODY', self.plan_path(target).read_text())
        self.assertFalse((target['path'] / 'private/store/collector.sqlite3').exists())

    def test_new_scope_streams_legacy_overlap_and_keeps_new_chat_baseline_per_shard(self):
        source = self.profile('source', [PRIVATE, GROUP], legacy=True, cursor=20)
        copied = [self.event(source, GROUP, 4), self.event(source, GROUP, 6, shard=1)]
        self.commit(source, *copied, self.event(source, PRIVATE, 7))
        before = source['store'].snapshot()
        source_hash = hashlib.sha256((source['path'] / 'private/store/collector.sqlite3').read_bytes()).hexdigest()
        target = self.profile('target', [GROUP, ADDED], initialize=False, cursor=100)
        self.plan(target, [source], source)
        result = self.apply(target, initialize=True)
        for relative in SHARDS:
            self.assertEqual(result['cursors'][relative][alias_for(GROUP)]['local_id'], 20)
            self.assertEqual(result['cursors'][relative][alias_for(ADDED)]['local_id'], 100)
        rows = self.saved(target)
        self.assertEqual(len(rows), 2)
        for row, original in zip(rows, copied):
            for field in original:
                self.assertEqual(row[field], alias_for(GROUP) if field == 'alias' else original[field])
        self.assertFalse(self.plan_path(target).exists())
        self.assertEqual(source['store'].snapshot(), before)
        self.assertEqual(hashlib.sha256((source['path'] / 'private/store/collector.sqlite3').read_bytes()).hexdigest(), source_hash)

    def test_archived_selected_history_can_merge_without_borrowing_its_new_chat_cursor(self):
        previous = self.profile('previous', [GROUP], cursor=20)
        archived = self.profile('archived', [ADDED, OTHER], cursor=40)
        self.commit(previous, self.event(previous, GROUP, 5))
        self.commit(archived, self.event(archived, ADDED, 8), self.event(archived, OTHER, 9))
        target = self.profile('target', [GROUP, ADDED], initialize=False, cursor=100)
        self.plan(target, [previous, archived], previous)
        state = self.apply(target, initialize=True)
        self.assertEqual(len(self.saved(target)), 2)
        for relative in SHARDS:
            self.assertEqual(state['cursors'][relative][alias_for(GROUP)]['local_id'], 20)
            self.assertEqual(state['cursors'][relative][alias_for(ADDED)]['local_id'], 100)

    def test_existing_target_keeps_every_checkpoint_field_rowid_and_reference_payload(self):
        source = self.profile('source', [GROUP], cursor=20)
        self.commit(source, self.event(source, GROUP, 5))
        target = self.profile('target', [GROUP, ADDED], cursor=100)
        existing = self.event(target, GROUP, 50)
        self.commit(target, existing)
        before, original = target['store'].snapshot(), self.saved(target)[0]
        assistant = target['path'] / 'assistant'
        assistant.mkdir(mode=0o700)
        saved_reference = assistant / 'synthetic-reference.json'
        write_private_json_new(saved_reference, {'message_id': original['rowid'], 'conversation_id': alias_for(GROUP)})
        reference_bytes = saved_reference.read_bytes()
        self.plan(target, [source], source)
        after = self.apply(target)
        self.assertEqual(after, before)
        self.assertEqual(self.saved(target)[0], original)
        self.assertEqual(len(self.saved(target)), 2)
        self.assertEqual(saved_reference.read_bytes(), reference_bytes)

    def test_history_without_explicit_continuity_never_rewinds_fresh_baseline(self):
        source = self.profile('source', [GROUP], cursor=20)
        self.commit(source, self.event(source, GROUP, 5))
        target = self.profile('target', [GROUP], initialize=False, cursor=100)
        self.plan(target, [source])
        result = self.apply(target, initialize=True)
        self.assertEqual(result['cursors'], target['cursors'])
        self.assertEqual(len(self.saved(target)), 1)

    def test_restored_target_can_donate_newer_imported_rows_before_its_next_poll(self):
        old = self.profile('old', [GROUP], cursor=5)
        self.commit(old, self.event(old, GROUP, 4))
        recent = self.profile('recent', [GROUP, PRIVATE], cursor=20)
        self.commit(recent, self.event(recent, GROUP, 10))
        old_snapshot = old['store'].snapshot()
        self.plan(old, [recent], recent)
        self.apply(old)
        self.assertEqual(old['store'].snapshot(), old_snapshot)
        self.assertEqual(len(self.saved(old)), 2)
        target = self.profile('next', [GROUP, ADDED], initialize=False, cursor=100)
        self.plan(target, [old], old)
        state = self.apply(target, initialize=True)
        self.assertEqual(len(self.saved(target)), 2)
        for relative in SHARDS:
            self.assertEqual(state['cursors'][relative][alias_for(GROUP)]['local_id'], 5)

    def test_repeat_sources_deduplicate_and_repeated_plan_is_idempotent(self):
        first = self.profile('first', [GROUP])
        second = self.profile('second', [GROUP])
        self.commit(first, self.event(first, GROUP, 5))
        duplicate = self.event(second, GROUP, 5)
        duplicate['observed_at'] = datetime(2026, 11, 2, 11, tzinfo=timezone.utc).isoformat()
        self.commit(second, duplicate)
        target = self.profile('target', [GROUP], cursor=100)
        self.plan(target, [first, second])
        before = target['store'].snapshot()
        self.apply(target)
        row = self.saved(target)[0]
        self.plan(target, [first, second])
        self.apply(target)
        self.assertEqual(self.saved(target), [row])
        self.assertEqual(row['observed_at'], NOW)
        self.assertEqual(target['store'].snapshot(), before)

    def test_conflicting_original_content_rolls_back_all_rows_and_keeps_plan(self):
        source = self.profile('source', [GROUP])
        self.commit(source, self.event(source, GROUP, 4), self.event(source, GROUP, 5))
        target = self.profile('target', [GROUP])
        self.commit(target, self.event(target, GROUP, 5, body='SYNTHETIC_CONFLICTING_CONTENT'))
        before, original = target['store'].snapshot(), self.saved(target)
        self.plan(target, [source])
        with self.assertRaises(StoreError) as error:
            self.apply(target)
        self.assert_fixed_error(error.exception)
        self.assertEqual(self.saved(target), original)
        self.assertEqual(target['store'].snapshot(), before)
        self.assertTrue(self.plan_path(target).exists())

    def test_foreign_account_or_platform_is_refused_before_plan_or_import(self):
        target = self.profile('target', [GROUP], initialize=False)
        for name, options in [('foreign', {'account': self.other_account}), ('windows', {'platform': 'windows'})]:
            source = self.profile(name, [GROUP], **options)
            with self.subTest(name=name), self.assertRaisesRegex(history.HistoryError, 'account_mismatch'):
                self.plan(target, [source])
        self.assertFalse(self.plan_path(target).exists())

    def test_windows_account_also_binds_exact_executable_path(self):
        source = self.profile('source', [GROUP], platform='windows')
        target = self.profile('target', [GROUP], platform='windows', initialize=False)
        self.commit(source, self.event(source, GROUP, 4))
        self.plan(target, [source], source)
        self.apply(target, initialize=True)
        self.assertEqual(len(self.saved(target)), 1)
        other_executable = self.root / 'another' / 'WeChat.exe'
        other_executable.parent.mkdir()
        other_executable.write_bytes(b'SYNTHETIC_OTHER_EXECUTABLE_NOT_RUN')
        scope = dict(source['scope'], exe_path=str(other_executable))
        self.rewrite(source['path'] / 'private/scope.json', scope)
        with self.assertRaisesRegex(history.HistoryError, 'account_mismatch'):
            self.plan(target, [source])

    def test_recheck_plan_sources_prevents_cross_account_path_substitution(self):
        source = self.profile('source', [GROUP])
        target = self.profile('target', [GROUP], initialize=False)
        foreign = self.profile('foreign', [GROUP], account=self.other_account)
        self.plan(target, [source])
        plan = json.loads(self.plan_path(target).read_text())
        plan['sources'] = [str(foreign['path'])]
        self.rewrite(self.plan_path(target), plan)
        with self.assertRaisesRegex(history.HistoryError, 'account_mismatch'):
            self.apply(target, initialize=True)
        self.assertFalse(target['store'].snapshot()['initialized'])

    def test_source_epoch_marker_schema_and_checkpoint_fail_closed(self):
        cases = ('epoch', 'marker', 'checkpoint', 'schema')
        for case in cases:
            with self.subTest(case=case):
                source = self.profile('source-' + case, [GROUP])
                self.commit(source, self.event(source, GROUP, 4))
                target = self.profile('target-' + case, [GROUP], initialize=False)
                self.plan(target, [source])
                if case == 'marker':
                    marker = source['path'] / 'private/store/collector.identity.json'
                    self.rewrite(marker, {'schema_version': 1, 'binding_digest': 'f' * 64})
                else:
                    con = source['store']._connection
                    if case == 'epoch':
                        changed = copy.deepcopy(self.epoch)
                        changed[SHARDS[0]]['salt_sha256'] = 'f' * 64
                        con.execute('UPDATE checkpoint SET epochs=?', (canonical(changed),))
                    elif case == 'checkpoint':
                        con.execute('UPDATE checkpoint SET cursors=?', ('{}',))
                    else:
                        con.execute('CREATE TABLE synthetic_unexpected_table(value)')
                with self.assertRaises(Exception) as error:
                    self.apply(target, initialize=True)
                self.assert_fixed_error(error.exception)
                self.assertFalse(target['store'].snapshot()['initialized'])
                self.assertEqual(self.saved(target), [])
                self.assertTrue(self.plan_path(target).exists())

    def test_source_message_identity_is_verified_and_partial_seed_rolls_back(self):
        source = self.profile('source', [GROUP])
        valid = self.event(source, GROUP, 4)
        invalid = self.event(source, GROUP, 5)
        invalid['source_key'] = 'd' * 64
        self.commit(source, valid, invalid)
        target = self.profile('target', [GROUP], initialize=False)
        self.plan(target, [source])
        with self.assertRaises(Exception) as error:
            self.apply(target, initialize=True)
        self.assert_fixed_error(error.exception)
        self.assertEqual(self.saved(target), [])
        self.assertFalse(target['store'].snapshot()['initialized'])

    def test_continuity_cursor_cannot_exceed_fresh_source_maximum(self):
        source = self.profile('source', [GROUP], cursor=200)
        target = self.profile('target', [GROUP], initialize=False, cursor=100)
        self.plan(target, [source], source)
        with self.assertRaisesRegex(history.HistoryError, 'continuity_invalid'):
            self.apply(target, initialize=True)
        self.assertFalse(target['store'].snapshot()['initialized'])

    def test_zero_overlap_and_uninitialized_source_do_not_invent_continuity(self):
        unrelated = self.profile('unrelated', [OTHER])
        target = self.profile('target', [GROUP], initialize=False, cursor=100)
        self.assertIsNone(self.plan(target, [unrelated], unrelated))
        self.assertFalse(self.plan_path(target).exists())
        empty = self.profile('empty', [GROUP], initialize=False)
        self.plan(target, [empty], empty)
        result = self.apply(target, initialize=True)
        self.assertEqual(result['cursors'], target['cursors'])
        self.assertEqual(self.saved(target), [])

    def test_source_limit_symlinks_and_wrong_target_binding_are_refused(self):
        source = self.profile('source', [GROUP])
        target = self.profile('target', [GROUP], initialize=False)
        with self.assertRaisesRegex(history.HistoryError, 'source_limit'):
            self.plan(target, [source] * 101)
        if os.name != 'nt':
            alias = self.root / 'synthetic-alias'
            alias.symlink_to(source['path'], target_is_directory=True)
            with self.assertRaises(history.HistoryError):
                history.write_history_plan(target['path'], [alias])
        self.plan(target, [source])
        plan = json.loads(self.plan_path(target).read_text())
        plan['target_binding_digest'] = 'e' * 64
        self.rewrite(self.plan_path(target), plan)
        with self.assertRaisesRegex(history.HistoryError, 'plan_invalid'):
            self.apply(target, initialize=True)
        self.assertFalse(target['store'].snapshot()['initialized'])

    def test_large_history_is_streamed_past_normal_poll_batch_limit(self):
        source = self.profile('source', [GROUP], cursor=20000)
        for start, stop in ((1, 10001), (10001, 10006)):
            self.commit(source, *(self.event(source, GROUP, n) for n in range(start, stop)))
        target = self.profile('target', [GROUP], initialize=False, cursor=30000)
        self.plan(target, [source], source)
        self.apply(target, initialize=True)
        self.assertEqual(target['store'].stats()['total_messages'], 10005)
        self.assertEqual(target['store'].snapshot()['cursors'][SHARDS[0]][alias_for(GROUP)]['local_id'], 20000)

    def test_committed_plan_cleanup_fault_retries_without_rebaseline_or_duplicate(self):
        source = self.profile('source', [GROUP])
        self.commit(source, self.event(source, GROUP, 4))
        target = self.profile('target', [GROUP], initialize=False, cursor=100)
        self.plan(target, [source], source)
        def fault(stage):
            if stage == 'after_baseline_commit':
                raise RuntimeError('SYNTHETIC_PRIVATE_EXCEPTION_NEVER_EXPORTED')
        target['store']._fault_hook = fault
        with self.assertRaises(history.HistoryError) as error:
            self.apply(target, initialize=True)
        self.assert_fixed_error(error.exception)
        before, rows = target['store'].snapshot(), self.saved(target)
        self.assertTrue(before['initialized'])
        self.assertTrue(self.plan_path(target).exists())
        target['store']._fault_hook = None
        history.apply_history_plan(target['store'], before['cursors'], target['epochs'])
        self.assertEqual(target['store'].snapshot(), before)
        self.assertEqual(self.saved(target), rows)
        self.assertFalse(self.plan_path(target).exists())

    def test_existing_merge_fault_before_or_after_commit_retries_without_checkpoint_change(self):
        source = self.profile('source', [GROUP])
        self.commit(source, self.event(source, GROUP, 4))
        for phase in ('before_history_commit', 'after_history_commit'):
            with self.subTest(phase=phase):
                target = self.profile('target-' + phase, [GROUP], cursor=100)
                self.commit(target, self.event(target, GROUP, 50))
                before, original = target['store'].snapshot(), self.saved(target)[0]
                self.plan(target, [source])
                def fault(stage):
                    if stage == phase:
                        raise RuntimeError('SYNTHETIC_PRIVATE_MERGE_FAILURE')
                target['store']._fault_hook = fault
                with self.assertRaises(Exception) as error:
                    self.apply(target)
                self.assert_fixed_error(error.exception)
                self.assertEqual(target['store'].snapshot(), before)
                self.assertEqual(self.saved(target)[0], original)
                self.assertEqual(len(self.saved(target)), 1 if phase == 'before_history_commit' else 2)
                self.assertTrue(self.plan_path(target).exists())
                target['store']._fault_hook = None
                self.apply(target)
                self.assertEqual(target['store'].snapshot(), before)
                self.assertEqual(self.saved(target)[0], original)
                self.assertEqual(len(self.saved(target)), 2)

    def test_success_does_not_remove_a_newer_replacement_plan(self):
        source = self.profile('source', [GROUP])
        self.commit(source, self.event(source, GROUP, 4))
        target = self.profile('target', [GROUP], initialize=False, cursor=100)
        self.plan(target, [source], source)
        replacement = json.loads(self.plan_path(target).read_text())
        replacement['nonce'] = 'f' * 32
        def replace(stage):
            if stage == 'after_baseline_commit':
                self.rewrite(self.plan_path(target), replacement)
        target['store']._fault_hook = replace
        with self.assertRaisesRegex(history.HistoryError, 'plan_changed'):
            self.apply(target, initialize=True)
        self.assertEqual(json.loads(self.plan_path(target).read_text()), replacement)
        self.assertEqual(target['store'].stats()['total_messages'], 1)

    def test_real_child_crash_before_or_after_seed_transaction_is_restart_safe(self):
        child = '''
import os,sys,json
sys.path.insert(0,sys.argv[1])
from continuous_store import CollectorStore
from scope_history import apply_history_plan
binding,cursors,epochs=json.loads(sys.argv[3])
def fault(stage):
 if stage==sys.argv[4]:os._exit(71)
store=CollectorStore(sys.argv[2],binding,fault_hook=fault)
apply_history_plan(store,cursors,epochs,initialize=True)
'''
        source = self.profile('source', [GROUP])
        self.commit(source, self.event(source, GROUP, 4))
        for stage in ('before_baseline_commit', 'after_baseline_commit'):
            with self.subTest(stage=stage):
                target = self.profile('target-' + stage, [GROUP], initialize=False, cursor=100)
                target['store'].close()
                self.plan(target, [source], source)
                process = subprocess.run([sys.executable, '-B', '-c', child, str(MODULES),
                    str(target['path'] / 'private/store'), json.dumps([target['binding'], target['cursors'], target['epochs']]), stage],
                    capture_output=True, timeout=15)
                self.assertEqual(process.returncode, 71)
                self.assertEqual(process.stdout, b'')
                target['store'] = self.open_store(target)
                initialized = target['store'].snapshot()['initialized']
                self.assertEqual(initialized, stage == 'after_baseline_commit')
                history.apply_history_plan(target['store'], target['store'].snapshot()['cursors'] if initialized else target['cursors'], target['epochs'], initialize=not initialized)
                self.assertEqual(target['store'].stats()['total_messages'], 1)
                self.assertFalse(self.plan_path(target).exists())

    def test_no_plan_preserves_generic_store_and_directoryless_consumers(self):
        class Shim:
            def __init__(self): self.state = {'initialized': False}
            def initialize(self, cursors, epochs): self.state = {'initialized': True, 'cursors': cursors, 'epochs': epochs}
            def snapshot(self): return self.state
        shim = Shim()
        self.assertEqual(history.apply_history_plan(shim, {}, {}, initialize=True),
                         {'initialized': True, 'cursors': {}, 'epochs': {}})
        directory = self.root / 'synthetic-generic-store'
        directory.mkdir(mode=0o700)
        with CollectorStore(directory, {'synthetic_generic': True}) as store:
            self.assertTrue(history.apply_history_plan(store, {}, {}, initialize=True)['initialized'])


if __name__ == '__main__':
    unittest.main()
