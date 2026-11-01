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
from continuous_store import CollectorStore, StoreError
from conversation_scope import alias_for, normalize_conversations
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

    def verified_identity(self):
        return {'schema_version': 1, 'state': 'verified', 'self_sender_id': 'synthetic_verified_self',
                'method': 'private_peer_account_match', 'binding_digest': self.store.binding_digest,
                'proof': {'account_candidate_matched': True, 'contact_candidate_matched': True,
                          'private_non_peer_unique': True, 'private_peer_id': 'synthetic_sender'}}

    def status(self, stage='collecting', age=0, **extra):
        value = dict(schema_version=1, stage=stage, recorded_at_utc=(NOW - timedelta(seconds=age)).isoformat(),
                     started_at_utc=(NOW - timedelta(minutes=2)).isoformat(), resumed_from_checkpoint=True)
        value.update(extra)
        self.metadata('web-status.json', value)

    def commit(self, *rows):
        return self.store.commit({}, {}, list(rows), {})

    def dynamic_profile(self):
        """A separately bound selected scope, never a mutation of the old store."""
        self.store.close()
        self.data = self.root / 'dynamic-profile'
        self.private = self.data / 'private'
        self.directory = self.private / 'store'
        for directory in (self.data, self.private, self.directory):
            directory.mkdir(mode=0o700)
        chats = ['synthetic-peer-0', 'synthetic-peer-1', 'synthetic-peer-2',
                 'synthetic-group-0@chatroom', 'synthetic-group-1@chatroom']
        selected = normalize_conversations({'conversations': [
            {'alias': alias_for(chat), 'chat_id': chat} for chat in chats]})
        self.store = CollectorStore(self.directory, {'account': 'SYNTHETIC_DYNAMIC_ACCOUNT',
                                                    'config': {'conversations': selected}})
        cursors = {'message/message_0.db': {row['alias']: {'local_id': 0, 'present': True}
                                         for row in selected}}
        self.store.initialize(cursors, {})
        self.reader = CollectorWebData(self.data, now=lambda: NOW)
        identity = self.verified_identity()
        peer = next(row for row in selected if row['chat_id'] == chats[0])
        identity['proof'].update(private_peer_id=peer['chat_id'], private_alias=peer['alias'])
        catalog = {'schema_version': 1, 'binding_digest': self.store.binding_digest,
                   'identity': identity, 'conversations': []}
        events = []
        for index, row in enumerate(selected):
            kind = 'group' if row['chat_id'].endswith('@chatroom') else 'private'
            incoming = 'synthetic-member' if kind == 'group' else row['chat_id']
            catalog['conversations'].append({'id': row['alias'], 'kind': kind, 'name': '合成会话%d' % index,
                'sender_names': {incoming: '合成联系人%d' % index}})
            for direction, sender in [('incoming', incoming), ('outgoing', 'synthetic_verified_self'), ('unknown', '')]:
                events.append(event(len(events) + 1, row['alias'], body='ONLY_SCOPE_%d_%s' % (index, direction), sender=sender))
        self.store.commit(cursors, cursors, events, {})
        self.metadata('catalog.json', catalog)
        return selected, catalog, cursors

    def test_five_selected_conversations_are_isolated_in_listing_counts_search_and_pages(self):
        selected, _, cursors = self.dynamic_profile()
        outside = alias_for('synthetic-unselected-peer')
        with self.assertRaisesRegex(StoreError, 'collector_event_scope_invalid'):
            self.store.commit(cursors, cursors, [event(100, outside, body='EXCLUDED_DYNAMIC_BODY')], {})
        # Deliberate DB-level corruption tests the independent read boundary;
        # the production CollectorStore above refuses this out-of-scope event.
        self.store._connection.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                                       tuple(event(100, outside, body='EXCLUDED_DYNAMIC_BODY').values()))
        before = hashlib.sha256(self.reader.database.read_bytes()).hexdigest()
        rows = self.reader.conversations()['conversations']
        self.assertEqual([row['id'] for row in rows], sorted(row['alias'] for row in selected))
        self.assertEqual([row['count'] for row in rows], [3] * 5)
        self.assertEqual(sum(row['type'] == 'private' for row in rows), 3)
        self.assertEqual(sum(row['type'] == 'group' for row in rows), 2)
        self.assertEqual(self.reader.status()['total_messages'], 15)
        for index, row in enumerate(rows):
            latest = self.reader.messages(row['id'], limit=2)
            self.assertTrue(latest['has_more'])
            self.assertTrue(all(item['conversation_id'] == row['id'] for item in latest['messages']))
            older = self.reader.messages(row['id'], before=latest['before_cursor'], limit=2)
            all_rows = older['messages'] + latest['messages']
            self.assertEqual([item['direction'] for item in all_rows], ['incoming', 'outgoing', 'unknown'])
            self.assertEqual([item['own'] for item in all_rows], [False, True, False])
            self.assertEqual(len(self.reader.messages(row['id'], q='ONLY_SCOPE_%d_' % index)['messages']), 3)
            self.assertEqual(self.reader.messages(row['id'], q='EXCLUDED_DYNAMIC_BODY')['messages'], [])
        self.assertEqual(self.reader.messages()['messages'][0]['conversation_id'], rows[0]['id'])
        for alias in (outside, 'private_test', 'group_test'):
            for arguments in ({}, {'after': 0}, {'before': 101}, {'q': 'EXCLUDED_DYNAMIC_BODY'}):
                with self.assertRaisesRegex(WebDataError, 'web_conversation_not_selected'):
                    self.reader.messages(alias, **arguments)
        self.assertNotIn('EXCLUDED_DYNAMIC_BODY', json.dumps([rows, self.reader.status()]))
        self.assertEqual(hashlib.sha256(self.reader.database.read_bytes()).hexdigest(), before)

    def test_dynamic_catalog_requires_binding_valid_alias_unique_rows_and_valid_kind(self):
        _, catalog, _ = self.dynamic_profile()
        invalid = []
        missing = dict(catalog)
        del missing['binding_digest']
        invalid.append(missing)
        invalid.append(dict(catalog, binding_digest='other-synthetic-account'))
        invalid.append(dict(catalog, conversations=[]))
        invalid.append(dict(catalog, conversations=[catalog['conversations'][0]] * 2))
        for field, value in [('id', 'unselected_raw_source_identifier'), ('kind', 'unexpected_kind')]:
            invalid.append(dict(catalog, conversations=[dict(catalog['conversations'][0], **{field: value})]))
        invalid.append(dict(catalog, conversations=[{'id': alias_for('synthetic-limit-%d' % i),
            'kind': 'private', 'name': '合成', 'sender_names': {}} for i in range(51)]))
        for source in invalid:
            self.metadata('catalog.json', source)
            with self.assertRaises(WebDataError):
                self.reader.conversations()
            with self.assertRaises(WebDataError):
                self.reader.status()
        one = dict(catalog, conversations=catalog['conversations'][:1])
        self.metadata('catalog.json', one)
        self.assertEqual(len(self.reader.conversations()['conversations']), 1)
        fifty = dict(catalog, conversations=[{'id': alias_for('synthetic-limit-%d' % i),
            'kind': 'private', 'name': '合成', 'sender_names': {}} for i in range(50)])
        self.assertEqual(len(self.reader._catalog(self.store.binding_digest, fifty)), 50)

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
        self.assertTrue(all(row['direction'] == 'unknown' for row in messages))
        self.assertEqual(self.reader.status()['total_messages'], 4)
        public = json.dumps([rows, messages, self.reader.status()], ensure_ascii=False)
        for secret in ('EXCLUDED_BODY', 'unknown_raw_identifier', 'synthetic_sender', 'source_key',
                       str(self.private), 'SYNTHETIC_SECRET_ROOT'):
            self.assertNotIn(secret, public)

    def test_verified_identity_projects_three_directions_without_exposing_identifiers(self):
        self.catalog(binding_digest=self.store.binding_digest, identity=self.verified_identity())
        self.commit(event(1, sender='synthetic_verified_self'), event(2, sender='synthetic_sender'),
                    event(3, sender=''), event(4, sender='synthetic_nickname_me'),
                    event(5, 'group_test', sender='synthetic_verified_self'),
                    event(6, 'group_test', sender='synthetic_unmapped_peer'))
        before = hashlib.sha256(self.reader.database.read_bytes()).hexdigest()
        rows = self.reader.messages('private_test')['messages']
        self.assertEqual([row['direction'] for row in rows], ['outgoing', 'incoming', 'unknown', 'incoming'])
        self.assertEqual([row['own'] for row in rows], [True, False, False, False])
        self.assertEqual([row['sender'] for row in rows], ['我', '合成客户', '未知发送人', '我'])
        self.assertTrue(rows[0]['sender_known'])
        group = self.reader.messages('group_test')['messages']
        self.assertEqual([row['direction'] for row in group], ['outgoing', 'incoming'])
        self.assertEqual(group[1]['sender'], '未知发送人')
        public = json.dumps([rows, group, self.reader.conversations()], ensure_ascii=False)
        for secret in ('synthetic_verified_self', 'synthetic_unmapped_peer', 'private_peer_id',
                       'self_sender_id', 'private_peer_account_match', self.store.binding_digest):
            self.assertNotIn(secret, public)
        self.assertEqual(hashlib.sha256(self.reader.database.read_bytes()).hexdigest(), before)

    def test_legacy_identity_fields_and_incomplete_proof_do_not_establish_direction(self):
        self.commit(event(1, sender='synthetic_verified_self'))
        for extra in [
                {'self_sender_id': 'synthetic_verified_self'},
                {'binding_digest': self.store.binding_digest,
                 'identity': {'state': 'unknown', 'self_sender_id': 'synthetic_verified_self'}},
                {'identity': self.verified_identity()},
                {'binding_digest': self.store.binding_digest,
                 'identity': dict(self.verified_identity(), binding_digest='other-synthetic-binding')},
                {'binding_digest': self.store.binding_digest,
                 'identity': dict(self.verified_identity(), proof={})}]:
            with self.subTest(fields=sorted(extra)):
                self.catalog(**extra)
                row = self.reader.messages('private_test')['messages'][0]
                self.assertFalse(row['own'])
                self.assertEqual(row['direction'], 'unknown')

    def test_identity_metadata_can_label_committed_messages_without_rewriting_store_or_checkpoint(self):
        self.commit(event(1, sender='synthetic_verified_self'))
        before = hashlib.sha256(self.reader.database.read_bytes()).hexdigest()
        self.catalog()
        self.assertEqual(self.reader.messages('private_test')['messages'][0]['direction'], 'unknown')
        self.catalog(binding_digest=self.store.binding_digest, identity=self.verified_identity())
        row = self.reader.messages('private_test')['messages'][0]
        self.assertEqual(row['direction'], 'outgoing')
        self.assertEqual(row['sender'], '我')
        internal = self.reader._catalog(self.store.binding_digest,
                                        self.reader._metadata('catalog.json'))
        self.assertTrue(all(row['identity_verified'] for row in internal.values()))
        self.assertEqual(hashlib.sha256(self.reader.database.read_bytes()).hexdigest(), before)

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
        self.catalog()
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
