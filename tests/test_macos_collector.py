"""Synthetic Mac bootstrap/native/pipeline tests. Never open a real account."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import macos_collector as mac
import test_continuous_reader as source_fixture
from test_continuous_reader import BACKGROUND, GROUP, OLD, OTHER, PEER, UNSELECTED, cipher
from collector_private import read_private_json, write_private_json_new
from continuous_reader import EngineError
from continuous_store import CollectorStore
from web_data import CollectorWebData


class MacNativeProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-macos-native-')
        self.path = Path(self.tmp.name).resolve() / 'synthetic-probe-never-executed'
        self.path.write_bytes(b'SYNTHETIC_NATIVE_HELPER_NOT_EXECUTED')
        self.path.chmod(0o700)
        self.calls = []
        self.payload = {'running': True, 'frontmost': False, 'screen_locked': False,
                        'screen_state_available': True, 'matching_applications': 1,
                        'process_id': 12345, 'process_started_at': 1800000000.25, 'version': '4.1.0'}
        self.result = None

    def tearDown(self):
        self.tmp.cleanup()

    def fake_run(self, arguments, **options):
        self.calls.append((arguments, options))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result or SimpleNamespace(returncode=0, stdout=json.dumps(self.payload).encode())

    def probe(self):
        return mac.MacNativeProbe(self.path, expected_version='4.1.0', run=self.fake_run)

    def test_metadata_only_subprocess_and_first_valid_process_binding(self):
        probe = self.probe()
        result = probe()
        self.assertTrue(result['probe_succeeded'])
        self.assertEqual(probe.process_identity, (12345, 1800000000.25, '4.1.0'))
        arguments, options = self.calls[0]
        self.assertEqual(arguments, [str(self.path), 'wechat'])
        self.assertNotIn('--capture', arguments)
        self.assertFalse(options['shell'])
        self.assertEqual(options['timeout'], 5)
        self.assertEqual(options['stderr'], subprocess.DEVNULL)
        self.assertNotIn('process_id', result)
        self.assertNotIn('fragments', result)
        self.payload['process_id'] += 1
        self.assertFalse(probe()['probe_succeeded'])

    def test_unknown_screen_multiple_apps_bad_identity_or_version_are_fail_closed(self):
        for field, value in [('screen_state_available', False), ('screen_state_available', None),
                             ('matching_applications', 2), ('matching_applications', True),
                             ('process_id', 0), ('process_id', True), ('process_started_at', '1'),
                             ('process_started_at', float('nan')), ('process_started_at', 0),
                             ('version', '4.1.1'), ('frontmost', None), ('screen_locked', 0)]:
            with self.subTest(field=field, value=value):
                old = self.payload[field]
                self.payload[field] = value
                self.assertFalse(self.probe()()['probe_succeeded'])
                self.payload[field] = old
        probe = self.probe()
        self.assertTrue(probe()['probe_succeeded'])
        self.payload['process_started_at'] += 1
        self.assertFalse(probe()['probe_succeeded'])

    def test_timeout_malformed_output_and_replaced_probe_cannot_authorize_reads(self):
        probe = self.probe()
        for result in (subprocess.TimeoutExpired('synthetic', 5), SimpleNamespace(returncode=1, stdout=b''),
                       SimpleNamespace(returncode=0, stdout=b'not json'),
                       SimpleNamespace(returncode=0, stdout=b'x' * (128 * 1024 + 1)),
                       SimpleNamespace(returncode=0, stdout=b'[]')):
            self.result = result
            self.assertFalse(probe()['probe_succeeded'])
        self.result = None
        held = self.path.with_name('held-synthetic-helper')
        self.path.rename(held)
        self.path.write_bytes(b'SYNTHETIC_REPLACEMENT_NOT_EXECUTED')
        self.path.chmod(0o700)
        before = len(self.calls)
        self.assertFalse(probe()['probe_succeeded'])
        self.assertEqual(len(self.calls), before)


@unittest.skipIf(cipher is None, 'requires isolated synthetic SQLCipher fixture')
class MacCollectorTests(unittest.TestCase):
    def setUp(self):
        self.source = source_fixture.ContinuousReaderTests(methodName='runTest')
        self.source.setUp()
        self.addCleanup(self.source.tearDown)
        # A Mac account may have only message_0.db. No Windows-only second
        # shard is synthesized or discovered by the actual implementation.
        second = 'message/message_1.db'
        self.source.writers.pop(second).close()
        del self.source.keys[second]
        for suffix in ('', '-wal', '-shm'):
            path = Path(str(self.source.root / second) + suffix)
            if path.exists():
                path.unlink()
        self.legacy = self.source.root.parent / 'legacy-private'
        self.legacy.mkdir(mode=0o700)
        self.selected = self.legacy / 'selected-config.json'
        self.key_file = self.legacy / 'selected-keys.json'
        self.old_state = self.legacy / 'selected-state.json'
        self.data = self.source.root.parent / 'collector-data'
        config = {'run_id': 'SYNTHETIC_OLD_TEST_RUN', 'conversations': [
            {'alias': 'private_test', 'chat_id': PEER, 'tokens': ['SYNTH_OLD_PRIVATE_TOKEN'], 'expected_sender_id': PEER},
            {'alias': 'group_test', 'chat_id': GROUP, 'tokens': ['SYNTH_OLD_GROUP_TOKEN'], 'expected_sender_id': PEER}]}
        write_private_json_new(self.selected, config)
        write_private_json_new(self.old_state, {'integrity': 'SYNTHETIC_OLD_HMAC_NEVER_IMPORT', 'cursors': {'old': 1}})
        contact_key = '75' * 32
        contact = self.source.root / 'contact/contact.db'
        contact.parent.mkdir()
        connection = cipher.connect(str(contact))
        try:
            connection.execute('PRAGMA key = "x\'%s\'"' % contact_key)
            connection.execute('PRAGMA cipher_compatibility=4')
            connection.execute('CREATE TABLE contact(username TEXT PRIMARY KEY,remark TEXT,nick_name TEXT)')
            connection.executemany('INSERT INTO contact VALUES (?,?,?)', [
                (PEER, '合成Mac客户', ''), (GROUP, '合成Mac群', ''), (OTHER, '合成其他群成员', '')])
            connection.commit()
        finally:
            connection.close()
        self.keys = {'keys': {relative: {'enc_key': key} for relative, key in self.source.keys.items()}}
        self.keys['keys']['contact/contact.db'] = {'enc_key': contact_key}
        write_private_json_new(self.key_file, self.keys)
        self.state = dict(BACKGROUND)
        self.collectors = []
        self.addCleanup(lambda: [collector.close() for collector in self.collectors])

    def session(self):
        return mac.prepare_session(self.data, db_root=self.source.root, selected_config=self.selected,
                                   key_file=self.key_file, client_version='4.1.0')

    def collector(self, session=None):
        collector = mac.MacCollector(session or self.session(), lambda: dict(self.state), self.source.driver)
        self.collectors.append(collector)
        return collector

    def replace_private(self, target, value):
        target.unlink()
        write_private_json_new(target, value)

    def test_import_copies_only_explicit_scope_and_key_map_without_touching_old_hmac(self):
        before = {path.name: path.read_bytes() for path in self.legacy.iterdir()}
        session = self.session()
        self.assertEqual(session.shards, ('message/message_0.db',))
        self.assertEqual(set(session.keys), {'message/message_0.db', 'contact/contact.db'})
        self.assertEqual([row['alias'] for row in session.config['conversations']], ['private_test', 'group_test'])
        self.assertTrue(all(row['table'].startswith('Msg_') for row in session.config['conversations']))
        scope = read_private_json(self.data / 'private/scope.json')
        self.assertEqual(scope['platform'], 'macos')
        self.assertEqual(scope['client_version'], '4.1.0')
        self.assertEqual(scope['root_identity'], [self.source.root.stat().st_dev, self.source.root.stat().st_ino])
        serialized = json.dumps(scope)
        for forbidden in ('SYNTHETIC_OLD_TEST_RUN', 'tokens', 'expected_sender_id', 'SYNTHETIC_OLD_HMAC'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.legacy.iterdir()})
        self.assertNotIn(str(self.source.root), repr(session))
        self.assertFalse((self.data / 'private/store/collector.sqlite3').exists())

    def test_resume_uses_private_copies_and_missing_file_never_reinitializes(self):
        first = self.session()
        self.selected.unlink()
        self.key_file.unlink()
        restored = mac.prepare_session(self.data)
        self.assertEqual(restored.binding, first.binding)
        self.assertEqual(restored.keys, first.keys)
        keys = self.data / 'private/keys.json'
        keys.unlink()
        before = self.old_state.read_bytes()
        with self.assertRaises(mac.MacCollectorError):
            mac.prepare_session(self.data)
        self.assertFalse(keys.exists())
        self.assertEqual(self.old_state.read_bytes(), before)

    def test_bad_scope_key_or_out_of_range_shard_fails_before_creating_session(self):
        original = read_private_json(self.selected)
        extra = copy.deepcopy(original)
        extra['conversations'].append({'alias': 'other', 'chat_id': OTHER})
        self.replace_private(self.selected, extra)
        with self.assertRaisesRegex(mac.MacCollectorError, 'macos_selected_scope_invalid'):
            self.session()
        self.assertFalse(self.data.exists())
        self.replace_private(self.selected, original)
        bad = copy.deepcopy(self.keys)
        bad['keys']['message/message_0.db']['enc_key'] = '00' * 32
        self.replace_private(self.key_file, bad)
        with self.assertRaisesRegex(mac.MacCollectorError, 'macos_selected_database_key_authentication_failed'):
            self.session()
        self.assertFalse(self.data.exists())
        bad = copy.deepcopy(self.keys)
        bad['keys']['message/message_8.db'] = {'enc_key': '00' * 32}
        self.replace_private(self.key_file, bad)
        with self.assertRaisesRegex(mac.MacCollectorError, 'macos_selected_keys_invalid'):
            self.session()
        self.assertFalse(self.data.exists())
        self.replace_private(self.key_file, self.keys)
        extra_source = self.source.root / 'message/message_2.db'
        extra_source.write_bytes(b'SYNTHETIC_UNCONFIGURED_SHARD_MUST_NOT_BE_READ')
        with self.assertRaisesRegex(mac.MacCollectorError, 'macos_source_shards_mismatch'):
            self.session()
        self.assertFalse(self.data.exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX private input boundary')
    def test_loose_legacy_permissions_are_rejected_without_repair_or_output(self):
        self.key_file.chmod(0o644)
        with self.assertRaises(mac.MacCollectorError):
            self.session()
        self.assertEqual(self.key_file.stat().st_mode & 0o777, 0o644)
        self.assertFalse(self.data.exists())

    def test_new_baseline_excludes_pre_enable_history_then_collects_all_selected_group_senders(self):
        self.source.insert(PEER, 2, 'SYNTHETIC_BEFORE_ENABLE_MUST_NOT_APPEAR')
        self.source.writer.commit()
        old_state = self.old_state.read_bytes()
        collector = self.collector()
        provider = CollectorWebData(self.data)
        self.assertFalse(collector.resumed)
        self.assertEqual(collector.poll()['new_messages'], 0)
        self.assertEqual(provider.messages('private_test')['messages'], [])
        self.source.insert(PEER, 3, 'SYNTHETIC_MAC_NEW_PRIVATE')
        self.source.insert(GROUP, 2, OTHER + ':\nSYNTHETIC_MAC_OTHER_GROUP_SENDER', sender=2)
        self.source.insert(OTHER, 2, UNSELECTED)
        self.source.writer.commit()
        result = collector.poll()
        self.assertEqual(result['new_messages'], 2)
        self.assertEqual(result['state'], 'collecting')
        self.assertEqual(provider.messages('private_test')['messages'][0]['body'], 'SYNTHETIC_MAC_NEW_PRIVATE')
        group = provider.messages('group_test')['messages'][0]
        self.assertEqual(group['body'], 'SYNTHETIC_MAC_OTHER_GROUP_SENDER')
        self.assertEqual(group['sender'], '合成其他群成员')
        self.assertEqual(self.old_state.read_bytes(), old_state)
        self.assertEqual(self.source.driver.old_reads, [])
        self.assertEqual(self.source.driver.forbidden_reads, [])
        self.assertEqual(provider.status()['total_messages'], 2)

    def test_stopped_collector_resumes_same_baseline_catches_up_without_replaying(self):
        collector = self.collector()
        baseline = collector.baseline_completed_at
        self.source.insert(PEER, 2, 'SYNTHETIC_MAC_IDENTICAL')
        self.source.writer.commit()
        self.assertEqual(collector.poll()['new_messages'], 1)
        collector.close()
        provider = CollectorWebData(self.data)
        self.assertEqual(provider.status()['state'], 'stopped')
        self.source.insert(PEER, 3, 'SYNTHETIC_MAC_IDENTICAL')
        self.source.insert(GROUP, 2, PEER + ':\nSYNTHETIC_MAC_CATCHUP_GROUP')
        self.source.writer.commit()
        self.assertEqual(provider.status()['total_messages'], 1)
        restored = self.collector(mac.prepare_session(self.data))
        self.assertTrue(restored.resumed)
        self.assertEqual(restored.baseline_completed_at, baseline)
        self.assertEqual(restored.poll()['new_messages'], 2)
        self.assertEqual(restored.poll()['new_messages'], 0)
        self.assertEqual(provider.status()['total_messages'], 3)
        messages = provider.messages('private_test')['messages']
        self.assertEqual([row['body'] for row in messages], ['SYNTHETIC_MAC_IDENTICAL'] * 2)
        self.assertNotEqual(messages[0]['id'], messages[1]['id'])

    def test_foreground_and_unknown_native_state_pause_without_advancing_then_catch_up(self):
        collector = self.collector()
        before = collector.store.snapshot()
        self.source.insert(PEER, 2, 'SYNTHETIC_MAC_PAUSED')
        self.source.writer.commit()
        for state in (dict(BACKGROUND, frontmost=True), dict(BACKGROUND, screen_locked=True),
                      dict(BACKGROUND, probe_succeeded=False)):
            self.state = state
            self.assertEqual(collector.poll()['state'], 'paused')
            self.assertEqual(collector.store.snapshot(), before)
            self.assertEqual(CollectorWebData(self.data).status()['state'], 'paused')
        self.state = dict(BACKGROUND)
        self.assertEqual(collector.poll()['new_messages'], 1)

    def test_bounded_busy_retries_or_fatal_source_change_publish_error_before_releasing_lock(self):
        collector = self.collector()
        before = collector.store.snapshot()
        with patch.object(collector.reader, 'poll', side_effect=EngineError('continuous_source_busy')):
            for _ in range(5):
                self.assertEqual(collector.poll()['state'], 'paused')
                self.assertEqual(collector.store.snapshot(), before)
            with self.assertRaises(EngineError):
                collector.poll()
        self.assertTrue(collector.closed)
        self.assertEqual(CollectorWebData(self.data).status()['state'], 'error')
        # The final status was published while this process still owned the
        # lock; another store can now obtain it without changing its baseline.
        with CollectorStore(self.data / 'private/store', collector.session.binding) as restored:
            self.assertEqual(restored.snapshot(), before)

    def test_initial_unknown_native_state_does_not_commit_a_new_baseline(self):
        session = self.session()
        with self.assertRaises(EngineError):
            mac.MacCollector(session, lambda: dict(BACKGROUND, probe_succeeded=False), self.source.driver)
        with CollectorStore(self.data / 'private/store', session.binding) as restored:
            self.assertFalse(restored.snapshot()['initialized'])
            self.assertEqual(restored.stats()['total_messages'], 0)


if __name__ == '__main__':
    unittest.main()
