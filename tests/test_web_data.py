"""Synthetic only: read-only Web projection of a live local collector store."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from collector_private import write_private_json_new
from continuous_store import CollectorStore
from web_data import CollectorWebData, WebDataError

NOW = datetime(2026, 10, 31, 10, 0, tzinfo=timezone.utc)
BINDING = {'synthetic_only': True, 'selected_aliases': ['private_test', 'group_test']}


def event(number, alias='private_test', body=None, sender='synthetic_sender'):
    return {'source_key': hashlib.sha256(('synthetic-web-%s-%s' % (alias, number)).encode()).hexdigest(),
            'alias': alias, 'source_local_id': number, 'sent_at': 1700000000 + number,
            'sender_id': sender, 'body': body if body is not None else '合成消息 %d' % number,
            'observed_at': NOW.isoformat()}


class WebDataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-web-data-')
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'collector-data'
        self.private = self.data / 'private'
        self.directory = self.private / 'store'
        for directory in (self.data, self.private, self.directory):
            directory.mkdir(mode=0o700)
        self.store = CollectorStore(self.directory, BINDING)
        self.store.initialize({}, {})
        self.reader = CollectorWebData(self.data, now=lambda: NOW)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def metadata(self, name, value):
        target = self.private / name
        if target.exists():
            target.unlink()
        write_private_json_new(target, value)

    def catalog(self, **extra):
        self.metadata('catalog.json', dict(schema_version=1, conversations=[
            {'id': 'private_test', 'name': '合成私聊', 'kind': 'private',
             'sender_names': {'synthetic_sender': '合成客户', 'synthetic_nickname_me': '我'}},
            {'id': 'group_test', 'name': '合成群聊', 'kind': 'group', 'sender_names': {}}
        ], **extra))

    def status(self, stage='collecting', age=0, **extra):
        value = dict(schema_version=1, stage=stage, recorded_at_utc=(NOW - timedelta(seconds=age)).isoformat(),
                     started_at_utc=(NOW - timedelta(minutes=2)).isoformat(), resumed_from_checkpoint=True)
        value.update(extra)
        self.metadata('web-status.json', value)

    def commit(self, *rows):
        return self.store.commit({}, {}, list(rows), {})

    def test_empty_store_uses_selected_generic_names_without_fabricating_messages(self):
        rows = self.reader.conversations()['conversations']
        self.assertEqual([row['name'] for row in rows], ['所选私聊', '所选群聊'])
        self.assertEqual([row['count'] for row in rows], [0, 0])
        self.assertTrue(all(row['latest_id'] is None for row in rows))
        self.assertEqual(self.reader.messages('private_test'),
                         dict(messages=[], has_more=False, before_cursor=None, after_cursor=None))
        self.assertEqual(self.reader.status()['state'], 'unavailable')

    def test_selected_only_catalog_projection_and_unknown_sender(self):
        self.catalog(private_source='SYNTHETIC_SECRET_ROOT')
        self.commit(event(1), event(2, 'group_test'), event(3, 'excluded_alias', body='EXCLUDED_BODY'),
                    event(4, sender='unknown_raw_identifier'), event(5, sender='synthetic_nickname_me'))
        rows = self.reader.conversations()['conversations']
        self.assertEqual([row['name'] for row in rows], ['合成私聊', '合成群聊'])
        self.assertEqual([row['count'] for row in rows], [3, 1])
        self.assertEqual([row['latest_id'] for row in rows], [5, 2])
        messages = self.reader.messages('private_test')['messages']
        self.assertEqual([row['sender'] for row in messages], ['合成客户', '未知发送人', '我'])
        self.assertEqual([row['sender_known'] for row in messages], [True, False, True])
        self.assertTrue(all(row['own'] is False for row in messages))
        self.assertEqual(self.reader.status()['total_messages'], 4)
        public = json.dumps([rows, messages, self.reader.status()], ensure_ascii=False)
        for secret in ('EXCLUDED_BODY', 'unknown_raw_identifier', 'synthetic_sender', 'source_key',
                       str(self.private), 'SYNTHETIC_SECRET_ROOT'):
            self.assertNotIn(secret, public)

    def test_latest_before_after_pages_use_committed_rowids_ascending(self):
        self.commit(*(event(n) for n in range(1, 8)))
        latest = self.reader.messages('private_test', limit=3)
        self.assertEqual([row['id'] for row in latest['messages']], [5, 6, 7])
        self.assertTrue(latest['has_more'])
        older = self.reader.messages('private_test', before=latest['before_cursor'], limit=3)
        self.assertEqual([row['id'] for row in older['messages']], [2, 3, 4])
        self.assertTrue(older['has_more'])
        first = self.reader.messages('private_test', before=older['before_cursor'], limit=3)
        self.assertEqual([row['id'] for row in first['messages']], [1])
        self.assertFalse(first['has_more'])
        increment = self.reader.messages('private_test', after=0, limit=3)
        self.assertEqual([row['id'] for row in increment['messages']], [1, 2, 3])
        self.commit(event(8))
        self.assertEqual([row['id'] for row in self.reader.messages('private_test', after=7)['messages']], [8])

    def test_query_is_literal_parameterized_substring_and_never_expands_scope(self):
        needle = "%' OR 1=1 --"
        self.commit(event(1, body='普通合成文字'), event(2, body='前缀' + needle + '后缀'),
                    event(3, 'group_test', body=needle), event(4, body='100%_literal'))
        matches = self.reader.messages('private_test', q=needle)['messages']
        self.assertEqual([row['id'] for row in matches], [2])
        self.assertEqual([row['id'] for row in self.reader.messages('private_test', q='%_')['messages']], [4])
        self.assertEqual(self.reader.messages('private_test', q='不存在')['messages'], [])

    def test_invalid_alias_cursors_limit_and_query_have_fixed_400_codes(self):
        cases = [('excluded', {}), ('private_test', {'limit': 0}), ('private_test', {'limit': 101}),
                 ('private_test', {'limit': True}), ('private_test', {'before': 3, 'after': 0}),
                 ('private_test', {'after': -1}), ('private_test', {'before': 2**63}),
                 ('private_test', {'after': True}), ('private_test', {'q': '\0'}),
                 ('private_test', {'q': '长' * 342}), ('private_test', {'q': '\ud800'})]
        for alias, parameters in cases:
            with self.subTest(alias=alias, parameters=repr(parameters)):
                with self.assertRaises(WebDataError) as caught:
                    self.reader.messages(alias, **parameters)
                self.assertEqual(caught.exception.status, 400)
                self.assertRegex(caught.exception.code, '^web_[a-z_]+$')

    def test_response_byte_cap_keeps_pages_lossless_and_oversized_row_fails_closed(self):
        body = 'x' * (900 * 1024)
        self.commit(*(event(n, body=body) for n in range(1, 7)))
        page = self.reader.messages('private_test', after=0)
        self.assertEqual([row['id'] for row in page['messages']], [1, 2, 3, 4])
        self.assertTrue(page['has_more'])
        self.assertEqual([row['id'] for row in self.reader.messages('private_test', after=page['after_cursor'])['messages']], [5, 6])
        latest = self.reader.messages('private_test')
        self.assertEqual([row['id'] for row in latest['messages']], [3, 4, 5, 6])
        self.assertEqual([row['id'] for row in self.reader.messages('private_test', before=latest['before_cursor'])['messages']], [1, 2])
        # Bypass the producer's own bound to simulate a damaged/untrusted DB.
        self.store._connection.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                                       tuple(event(7, body='x' * (1024 * 1024 + 1)).values()))
        with self.assertRaisesRegex(WebDataError, 'web_message_body_invalid'):
            self.reader.messages('private_test', after=6)

    def test_uncommitted_rows_hidden_reader_cannot_write_and_no_writer_lock_acquisition(self):
        self.commit(event(1))
        source = event(2)
        self.store._connection.execute('BEGIN IMMEDIATE')
        self.store._connection.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)', tuple(source.values()))
        try:
            self.assertEqual([row['id'] for row in self.reader.messages('private_test')['messages']], [1])
        finally:
            self.store._connection.rollback()
        with self.reader._open() as (connection, _):
            self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM messages")
        self.commit(event(2))
        self.assertEqual(self.reader.conversations()['conversations'][0]['count'], 2)

    def test_active_heartbeat_stale_future_and_stopping_status(self):
        self.commit(event(1))
        for stage in ('starting', 'collecting', 'paused', 'stopping'):
            with self.subTest(stage=stage):
                self.status(stage, age=15)
                self.assertEqual(self.reader.status()['state'], stage)
                self.status(stage, age=16)
                self.assertEqual(self.reader.status()['state'], 'stale')
                self.status(stage, age=-6)
                self.assertEqual(self.reader.status()['state'], 'stale')
        self.status('stopped', age=1000, reason='collector_stopped_with_committed_checkpoint')
        result = self.reader.status()
        self.assertEqual(result['state'], 'stopped')
        self.assertFalse(result['stale'])
        self.assertTrue(result['resumed_from_checkpoint'])
        self.assertEqual(result['total_messages'], 1)

    def test_status_does_not_echo_paths_untrusted_extra_fields_or_instance(self):
        self.status(reason='SYNTHETIC/path/private-secret', instance_id='SYNTHETIC_INSTANCE',
                    private_source='SYNTHETIC_KEY', total_messages=999)
        result = self.reader.status()
        self.assertEqual(result['reason'], 'collector_status_reason_unavailable')
        self.assertEqual(result['total_messages'], 0)
        self.assertNotIn('SYNTHETIC', json.dumps(result))

    def test_metadata_binding_mismatches_and_invalid_schema_fail_closed(self):
        self.catalog(binding_digest='wrong-synthetic-binding')
        with self.assertRaisesRegex(WebDataError, 'web_catalog_binding_mismatch'):
            self.reader.conversations()
        self.metadata('catalog.json', {'schema_version': 1, 'conversations': []})
        with self.assertRaisesRegex(WebDataError, 'web_catalog_invalid'):
            self.reader.messages('private_test')
        self.status(binding_digest='wrong-synthetic-binding')
        with self.assertRaisesRegex(WebDataError, 'web_status_binding_mismatch'):
            self.reader.status()
        self.status(stage='arbitrary')
        with self.assertRaisesRegex(WebDataError, 'web_collector_status_invalid'):
            self.reader.status()

    def test_missing_or_corrupt_database_never_creates_or_replaces_store(self):
        self.store.close()
        self.reader.database.unlink()
        before = {path.name for path in self.directory.iterdir()}
        with self.assertRaises(WebDataError):
            self.reader.conversations()
        self.assertEqual(before, {path.name for path in self.directory.iterdir()})
        self.reader.database.write_bytes(b'SYNTHETIC CORRUPT DATABASE')
        self.reader.database.chmod(0o600)
        with self.assertRaises(WebDataError) as caught:
            self.reader.conversations()
        self.assertEqual(str(caught.exception), 'web_store_read_failed')
        self.assertEqual(self.reader.database.read_bytes(), b'SYNTHETIC CORRUPT DATABASE')

    def test_uninitialized_or_schema_changed_store_fails_without_repair(self):
        self.store._connection.execute('UPDATE checkpoint SET initialized=0 WHERE id=1')
        self.status('starting')
        self.assertEqual(self.reader.status()['state'], 'starting')
        with self.assertRaisesRegex(WebDataError, 'web_store_not_initialized'):
            self.reader.conversations()
        self.store._connection.execute('UPDATE checkpoint SET initialized=1 WHERE id=1')
        self.store._connection.execute('ALTER TABLE messages ADD COLUMN unexpected TEXT')
        with self.assertRaisesRegex(WebDataError, 'web_store_schema_invalid'):
            self.reader.conversations()

    @unittest.skipIf(os.name == 'nt', 'POSIX permission and link boundary')
    def test_loose_permissions_and_symlink_hardlink_or_sidecars_rejected(self):
        self.reader.database.chmod(0o644)
        with self.assertRaisesRegex(WebDataError, 'web_store_permissions_invalid'):
            self.reader.conversations()
        self.reader.database.chmod(0o600)
        for suffix in ('-wal', '-shm'):
            sidecar = Path(str(self.reader.database) + suffix)
            sidecar.write_bytes(b'')
            sidecar.chmod(0o600)
            with self.assertRaisesRegex(WebDataError, 'web_store_contents_invalid'):
                self.reader.conversations()
            sidecar.unlink()
        journal = Path(str(self.reader.database) + '-journal')
        journal.symlink_to(self.reader.database)
        with self.assertRaises(WebDataError):
            self.reader.conversations()
        journal.unlink()
        link = self.root / 'hardlink'
        os.link(self.reader.database, link)
        with self.assertRaises(WebDataError):
            self.reader.conversations()
        link.unlink()
        self.private.chmod(0o755)
        with self.assertRaises(WebDataError):
            self.reader.conversations()

    def test_readonly_load_and_live_commits_do_not_acquire_collector_writer_lock(self):
        # The producer retains its single-writer lock throughout. Each API call
        # opens a separate readonly SQLite connection and immediately releases it.
        failures, counts, done = [], [], threading.Event()
        def reader():
            while not done.is_set():
                try:
                    counts.append(self.reader.conversations()['conversations'][0]['count'])
                    self.reader.messages('private_test', after=0, limit=5)
                except WebDataError as error:
                    if error.code != 'web_store_busy':
                        failures.append(error.code)
        thread = threading.Thread(target=reader)
        thread.start()
        try:
            # The runtime writer uses a bounded SQLite busy timeout because
            # DELETE-journal readers may briefly hold a normal shared lock.
            self.assertEqual(self.store._connection.execute('PRAGMA busy_timeout').fetchone()[0], 3000)
            for number in range(1, 26):
                self.commit(event(number))
        finally:
            done.set()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.reader.conversations()['conversations'][0]['count'], 25)
        self.assertTrue(counts)


if __name__ == '__main__':
    unittest.main()
