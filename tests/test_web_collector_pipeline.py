"""Synthetic encrypted source -> real collector -> private Web projection.

These tests reuse the guarded SQLCipher source fixture. They do not open an
installed WeChat client, an existing chat database or any user private folder.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

import test_continuous_reader as source_fixture
from test_continuous_reader import GROUP, OLD, OTHER, PEER, UNSELECTED, cipher

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from collector_web_meta import WebMetadata
from web_data import CollectorWebData


@unittest.skipIf(cipher is None, 'requires isolated synthetic SQLCipher fixture')
class WebCollectorPipelineTests(unittest.TestCase):
    def setUp(self):
        # Composition avoids inheriting and accidentally rerunning the entire
        # underlying fixture's test suite as additional integration evidence.
        self.fixture = source_fixture.ContinuousReaderTests(methodName='runTest')
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.data = self.fixture.root.parent / 'collector-data'
        self.data.mkdir(mode=0o700)
        self.private = self.data / 'private'
        self.private.mkdir(mode=0o700)
        state = self.private / 'store'
        self.fixture.state.rename(state)
        self.fixture.state = state
        self.provider = CollectorWebData(self.data)
        self.started = datetime.now(timezone.utc).isoformat()
        contact_key = '71' * 32
        contact = self.fixture.root / 'contact' / 'contact.db'
        contact.parent.mkdir()
        writer = cipher.connect(str(contact))
        try:
            writer.execute('PRAGMA key = "x\'%s\'"' % contact_key)
            writer.execute('PRAGMA cipher_compatibility=4')
            writer.execute('PRAGMA cipher_page_size=4096')
            writer.execute('CREATE TABLE contact(username TEXT PRIMARY KEY,remark TEXT,nick_name TEXT)')
            writer.executemany('INSERT INTO contact VALUES (?,?,?)', [
                (PEER, '合成客户甲', '合成昵称甲'), (GROUP, '合成项目群', '合成群昵称'),
                (OTHER, 'SYNTHETIC_UNSELECTED_CONTACT_FORBIDDEN', '不应展示的联系人')])
            writer.commit()
        finally:
            writer.close()
        self.metadata_keys = dict(self.fixture.keys, **{'contact/contact.db': contact_key})

    def metadata(self, store, *, stage='collecting', added=0, resumed=False):
        metadata = WebMetadata(self.private, store.binding_digest, self.started)
        metadata.refresh_catalog(self.fixture.driver, self.fixture.root, self.metadata_keys,
                                 self.fixture.config, store.sender_ids_by_alias())
        metadata.status(stage, store.stats()['total_messages'], added, resumed)
        return metadata

    def source_contents(self):
        return {str(path.relative_to(self.fixture.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.fixture.root.rglob('*') if path.is_file()}

    def web_snapshot(self):
        return {'conversations': self.provider.conversations(), 'status': self.provider.status(),
                'private': self.provider.messages('private_test'), 'group': self.provider.messages('group_test')}

    def assert_readonly_web(self, storage=None):
        source_before = self.source_contents()
        db_before = hashlib.sha256(self.provider.database.read_bytes()).hexdigest()
        checkpoint_before = storage.snapshot() if storage is not None else None
        commits_before = storage.stats() if storage is not None else None
        result = self.web_snapshot()
        self.assertEqual(self.source_contents(), source_before)
        self.assertEqual(hashlib.sha256(self.provider.database.read_bytes()).hexdigest(), db_before)
        if storage is not None:
            self.assertEqual(storage.snapshot(), checkpoint_before)
            self.assertEqual(storage.stats(), commits_before)
        return result

    def test_live_collection_stop_readonly_restart_catchup_has_no_history_or_duplicates(self):
        reader, store = self.fixture.reader()
        baseline = reader.baseline_completed_at
        self.assertFalse(reader.resumed)
        metadata = self.metadata(store)
        initial = self.assert_readonly_web(store)
        self.assertEqual([row['name'] for row in initial['conversations']['conversations']], ['合成客户甲', '合成项目群'])
        self.assertEqual(initial['private']['messages'], [])
        self.assertEqual(initial['group']['messages'], [])
        self.assertEqual(initial['status']['total_messages'], 0)
        self.fixture.insert(PEER, 2, 'SYNTHETIC_PIPELINE_LIVE_PRIVATE')
        self.fixture.insert(GROUP, 2, PEER + ':\nSYNTHETIC_PIPELINE_LIVE_GROUP')
        self.fixture.insert(OTHER, 2, UNSELECTED)
        self.fixture.writer.commit()
        self.assertEqual(reader.poll()['new_messages'], 2)
        self.metadata(store, added=2)
        live = self.assert_readonly_web(store)
        self.assertEqual([row['count'] for row in live['conversations']['conversations']], [1, 1])
        self.assertEqual(live['private']['messages'][0]['body'], 'SYNTHETIC_PIPELINE_LIVE_PRIVATE')
        self.assertEqual(live['group']['messages'][0]['body'], 'SYNTHETIC_PIPELINE_LIVE_GROUP')
        self.assertEqual(live['group']['messages'][0]['sender'], '合成客户甲')
        previous_ids = {alias: live[alias]['after_cursor'] for alias in ('private', 'group')}
        metadata.status('stopped', 2, 2, False, 'collector_stopped_with_committed_checkpoint')
        reader.close()
        store.close()

        # The synthetic WeChat writer keeps receiving while the collector is
        # stopped. Web alone must not poll source or advance the checkpoint.
        self.fixture.insert(PEER, 3, 'SYNTHETIC_PIPELINE_STOPPED_PRIVATE')
        self.fixture.insert(GROUP, 3, PEER + ':\nSYNTHETIC_PIPELINE_STOPPED_GROUP')
        self.fixture.insert(OTHER, 3, UNSELECTED)
        self.fixture.writer.commit()
        stopped = self.assert_readonly_web()
        self.assertEqual(stopped['status']['state'], 'stopped')
        self.assertEqual(stopped['status']['total_messages'], 2)
        self.assertEqual(stopped['private']['after_cursor'], previous_ids['private'])
        self.assertEqual(stopped['group']['after_cursor'], previous_ids['group'])

        restored, store = self.fixture.reader()
        self.assertTrue(restored.resumed)
        self.assertEqual(restored.baseline_completed_at, baseline)
        self.assertEqual(restored.poll()['new_messages'], 2)
        self.assertEqual(restored.poll()['new_messages'], 0)
        self.metadata(store, added=2, resumed=True)
        resumed = self.assert_readonly_web(store)
        self.assertEqual([row['count'] for row in resumed['conversations']['conversations']], [2, 2])
        self.assertEqual(resumed['status']['total_messages'], 4)
        self.assertTrue(resumed['status']['resumed_from_checkpoint'])
        for page, alias in (('private', 'private_test'), ('group', 'group_test')):
            increment = self.provider.messages(alias, after=previous_ids[page])['messages']
            self.assertEqual(len(increment), 1)
            self.assertIn('STOPPED', increment[0]['body'])
            ids = [row['id'] for row in resumed[page]['messages']]
            self.assertEqual(len(ids), len(set(ids)))
        public = json.dumps(resumed, ensure_ascii=False)
        for forbidden in (OLD, UNSELECTED, OTHER, 'SYNTHETIC_UNSELECTED_CONTACT_FORBIDDEN', PEER, GROUP):
            self.assertNotIn(forbidden, public)
        self.assertEqual(self.fixture.driver.old_reads, [])
        self.assertEqual(self.fixture.driver.forbidden_reads, [])

    def test_source_commit_then_poll_is_required_before_new_message_appears_on_web(self):
        reader, store = self.fixture.reader()
        self.metadata(store)
        baseline = reader.baseline_completed_at
        initial_checkpoint = store.snapshot()
        self.fixture.insert(PEER, 2, 'SYNTHETIC_PIPELINE_PENDING_SOURCE_COMMIT')
        # An uncommitted WAL insert is invisible even to the real collector.
        self.assertEqual(reader.poll()['new_messages'], 0)
        self.assertEqual(self.assert_readonly_web(store)['private']['messages'], [])
        self.fixture.writer.commit()
        # Committing source alone cannot populate the already committed local
        # store. No Web endpoint opens a source database or drives collection.
        self.assertEqual(self.assert_readonly_web(store)['private']['messages'], [])
        self.assertEqual(store.snapshot(), initial_checkpoint)
        self.assertEqual(reader.poll()['new_messages'], 1)
        self.metadata(store, added=1)
        snapshot = self.assert_readonly_web(store)
        self.assertEqual(len(snapshot['private']['messages']), 1)
        self.assertEqual(snapshot['private']['messages'][0]['body'], 'SYNTHETIC_PIPELINE_PENDING_SOURCE_COMMIT')
        self.assertEqual(reader.baseline_completed_at, baseline)
        self.assertEqual(reader.poll()['new_messages'], 0)
        self.assertEqual(store.stats()['total_messages'], 1)
        self.assertEqual(self.fixture.driver.old_reads, [])
        self.assertEqual(self.fixture.driver.forbidden_reads, [])


if __name__ == '__main__':
    unittest.main()
