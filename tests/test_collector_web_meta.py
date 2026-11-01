"""Only synthetic selected contacts; no user's WeChat process or database."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
import collector_web_meta as metadata
from collector_private import read_private_json
from collector_service import Heartbeat
from continuous_store import CollectorStore

PEER = 'synthetic_meta_peer'
GROUP = 'synthetic_meta_group@chatroom'
SENDER = 'synthetic_meta_sender'
EXCLUDED = 'synthetic_meta_excluded'
CONFIG = {'conversations': [
    {'alias': 'private_test', 'chat_id': PEER},
    {'alias': 'group_test', 'chat_id': GROUP}]}


class CollectorWebMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-web-metadata-')
        self.root = Path(self.tmp.name).resolve()
        self.private = self.root / 'private'; self.private.mkdir(mode=0o700)
        (self.root / 'account/contact').mkdir(parents=True)
        self.path = self.root / 'account/contact/contact.db'
        connection = sqlite3.connect(self.path)
        connection.execute('CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT)')
        connection.executemany('INSERT INTO contact VALUES(?,?,?)', [
            (PEER, '合成客户备注', '合成昵称'), (GROUP, '', '合成测试群'),
            (SENDER, None, '合成群成员'), (EXCLUDED, 'SYNTHETIC_EXCLUDED_NAME', '')])
        connection.commit(); connection.close()
        self.queries = []
        self.tick = 100
        self.meta = metadata.WebMetadata(self.private, 'f' * 64, '2026-01-01T00:00:00+00:00', clock=lambda: self.tick)
        self.keys = {'contact/contact.db': '01' * 32}

    def tearDown(self):
        self.tmp.cleanup()

    def open_plain_synthetic(self, driver, path, key):
        self.assertEqual(Path(path), self.path)
        self.assertEqual(key, self.keys['contact/contact.db'])
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
        connection.execute('PRAGMA query_only=ON')
        connection.set_trace_callback(self.queries.append)
        return connection

    def refresh(self, observed=None):
        with patch.object(metadata, '_open', side_effect=self.open_plain_synthetic):
            self.meta.refresh_catalog(object(), self.root / 'account', self.keys, CONFIG,
                                      observed or {'private_test': [PEER], 'group_test': [SENDER]})

    def test_catalog_exact_selected_names_and_observed_sender_only_without_source_mutation(self):
        before = self.path.read_bytes()
        self.refresh()
        catalog = read_private_json(self.private / 'catalog.json')
        self.assertEqual([row['name'] for row in catalog['conversations']], ['合成客户备注', '合成测试群'])
        self.assertEqual(catalog['conversations'][1]['sender_names'][SENDER], '合成群成员')
        self.assertNotIn(SENDER, catalog['conversations'][0]['sender_names'])
        serialized = json.dumps(catalog, ensure_ascii=False)
        self.assertNotIn('SYNTHETIC_EXCLUDED_NAME', serialized)
        self.assertNotIn(EXCLUDED, '\n'.join(self.queries))
        lookups = [sql for sql in self.queries if sql.startswith('SELECT substr')]
        self.assertEqual(len(lookups), 3)
        self.assertTrue(all('WHERE username COLLATE BINARY=' in sql for sql in lookups))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.private.glob('.web-meta-*')), [])
        if os.name != 'nt':
            self.assertEqual((self.private / 'catalog.json').stat().st_mode & 0o777, 0o600)

    def test_metadata_refresh_is_cached_but_new_observed_sender_refreshes_immediately(self):
        self.refresh({'private_test': [PEER], 'group_test': []})
        count = len(self.queries)
        self.refresh({'private_test': [PEER], 'group_test': []})
        self.assertEqual(len(self.queries), count)
        self.refresh()
        self.assertGreater(len(self.queries), count)

    def test_duplicate_ambiguous_contact_and_unknown_sender_use_explicit_fallback(self):
        connection = sqlite3.connect(self.path)
        connection.execute('INSERT INTO contact VALUES(?,?,?)', (PEER, '不同合成备注', ''))
        connection.commit(); connection.close()
        self.refresh({'private_test': ['synthetic_unknown'], 'group_test': []})
        row = read_private_json(self.private / 'catalog.json')['conversations'][0]
        self.assertEqual(row['name'], '所选私聊')
        self.assertEqual(row['sender_names'], {})

    def test_replaced_source_during_query_is_rejected_without_publishing_catalog(self):
        original = metadata.source_identity
        calls = 0
        def identity(path):
            nonlocal calls
            calls += 1
            current = original(path)
            return current if calls == 1 else (current[0], current[1] + 1, current[2])
        with patch.object(metadata, 'source_identity', side_effect=identity):
            with self.assertRaisesRegex(ValueError, 'source_changed'):
                self.refresh()
        self.assertFalse((self.private / 'catalog.json').exists())

    def test_query_deadline_retains_previous_catalog(self):
        self.refresh()
        target = self.private / 'catalog.json'
        before = target.read_bytes()
        self.tick += 61
        with patch.object(metadata.time, 'monotonic', side_effect=[0, 2]):
            with self.assertRaisesRegex(ValueError, 'query_deadline'):
                self.refresh()
        self.assertEqual(target.read_bytes(), before)

    def test_metadata_replace_retries_short_windows_reader_sharing_conflict(self):
        replace = metadata.os.replace
        calls = 0
        def locked_once(source, target):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PermissionError('SYNTHETIC_SHARE_CONFLICT')
            return replace(source, target)
        with patch.object(metadata.os, 'replace', side_effect=locked_once), patch.object(metadata.time, 'sleep'):
            self.meta.status('collecting', 7, 1, True)
        self.assertEqual(calls, 2)
        self.assertEqual(read_private_json(self.private / 'web-status.json')['total_messages'], 7)
        self.assertEqual(list(self.private.glob('.web-meta-*')), [])

    def test_failed_atomic_replace_keeps_previous_status_and_cleans_only_own_temp(self):
        self.meta.status('collecting', 2, 0, True)
        before = (self.private / 'web-status.json').read_bytes()
        with patch.object(metadata.os, 'replace', side_effect=PermissionError), patch.object(metadata.time, 'sleep'):
            with self.assertRaises(PermissionError):
                self.meta.status('stopped', 3, 1, True)
        self.assertEqual((self.private / 'web-status.json').read_bytes(), before)
        self.assertEqual(list(self.private.glob('.web-meta-*')), [])

    def test_unprotected_or_redirected_destination_is_never_overwritten(self):
        target = self.private / 'catalog.json'
        other = self.root / 'other.json'; other.write_bytes(b'SYNTHETIC_UNTOUCHED')
        target.symlink_to(other)
        with self.assertRaises(Exception):
            self.refresh()
        self.assertEqual(other.read_bytes(), b'SYNTHETIC_UNTOUCHED')

    def test_heartbeat_metadata_failure_does_not_discard_committed_collection_progress(self):
        output = self.root / 'public'; output.mkdir()
        heartbeat = Heartbeat(output)
        heartbeat.metadata = self.meta
        with patch.object(self.meta, 'status', side_effect=OSError('SYNTHETIC_PRIVATE_ERROR')):
            heartbeat.emit('collecting', 10, 2, True)
        self.assertTrue(self.meta.failed)
        public = json.loads((output / 'status.json').read_text())
        self.assertEqual(public['total_messages'], 10)
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR', json.dumps(public))

    def test_sender_lookup_uses_only_stored_selected_aliases_and_keeps_body_private(self):
        directory = self.private / 'store'; directory.mkdir(mode=0o700)
        with CollectorStore(directory, {'synthetic_only': True}) as store:
            store.initialize({}, {})
            rows = []
            for number, (alias, sender) in enumerate([('private_test', PEER), ('group_test', SENDER), ('excluded', EXCLUDED)], 1):
                rows.append({'source_key': hashlib.sha256(str(number).encode()).hexdigest(), 'alias': alias,
                             'source_local_id': number, 'sent_at': number, 'sender_id': sender,
                             'body': 'SYNTHETIC_PRIVATE_BODY', 'observed_at': '2026-01-01T00:00:00+00:00'})
            store.commit({}, {}, rows, {})
            result = store.sender_ids_by_alias()
            self.assertEqual(result, {'private_test': [PEER], 'group_test': [SENDER]})
            self.assertNotIn('SYNTHETIC_PRIVATE_BODY', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
