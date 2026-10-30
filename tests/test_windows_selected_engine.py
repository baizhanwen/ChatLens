"""Synthetic SQLCipher/WAL selected-text engine tests; no real accounts/keys."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock


SPEC = importlib.util.spec_from_file_location('windows_selected_engine',
    Path(__file__).resolve().parents[1] / 'scripts/windows-proof/windows_selected_engine.py')
engine = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(engine)
try:
    import sqlcipher3.dbapi2 as cipher
    import zstandard
except ImportError:
    cipher = zstandard = None

PEER, GROUP, OTHER = 'wxid_synthetic_peer', 'synthetic_group@chatroom', 'wxid_synthetic_other'
OLD = 'SYNTHETIC_OLD_BODY_MUST_NOT_MATERIALIZE'
UNSELECTED = 'SYNTHETIC_UNSELECTED_PRIVATE_TEXT'
UNMATCHED = 'SYNTHETIC_UNMATCHED_SELECTED_TEXT'
TOKENS = {'private_test': ['SYNTHETIC_PRIVATE_1', 'SYNTHETIC_PRIVATE_2'],
          'group_test': ['SYNTHETIC_GROUP_1', 'SYNTHETIC_GROUP_2']}
BACKGROUND = {'probe_succeeded': True, 'running': True, 'frontmost': False, 'screen_locked': False}


def table(chat):
    return 'Msg_' + hashlib.md5(chat.encode()).hexdigest()


class GuardedDriver:
    def __init__(self, root):
        self.root, self.paths = root.resolve(), []
        self.forbidden_reads, self.old_reads, self.rollbacks = [], [], 0
        self.fail_table = None

    def connect(self, database, *args, **kwargs):
        from urllib.parse import unquote, urlparse
        path = Path(unquote(urlparse(str(database)).path)).resolve()
        if not path.is_relative_to(self.root):
            raise AssertionError('only synthetic paths are permitted')
        self.paths.append(path)
        connection = cipher.connect(database, *args, **kwargs)

        def authorize(action, source, column, *_):
            if action == cipher.SQLITE_READ and (source == table(OTHER) and column == 'message_content' or
                                                 source == 'contact' and column == 'private_body'):
                self.forbidden_reads.append((source, column))
                return cipher.SQLITE_DENY
            return cipher.SQLITE_OK

        def text_factory(raw):
            if raw == OLD.encode():
                self.old_reads.append(True)
                raise AssertionError('old body materialized')
            return raw.decode()

        connection.set_authorizer(authorize)
        connection.text_factory = text_factory
        parent = self

        class Proxy:
            def __getattr__(self, name):
                return getattr(connection, name)

            def execute(self, sql, *parameters):
                if parent.fail_table and sql.startswith('SELECT local_id,') and parent.fail_table in sql:
                    parent.fail_table = None
                    raise cipher.OperationalError('database is locked SYNTHETIC_PRIVATE_DETAIL')
                return connection.execute(sql, *parameters)

            def rollback(self):
                parent.rollbacks += 1
                return connection.rollback()

        return Proxy()


@unittest.skipIf(cipher is None, 'requires isolated SQLCipher and zstandard dependencies')
class WindowsSelectedEngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-win-engine-')
        self.root = Path(self.tmp.name)
        self.keys = {'message/message_0.db': '13' * 32, 'message/message_1.db': '37' * 32,
                     'contact/contact.db': '55' * 32}
        self.writers, self.pollers = {}, []
        for relative, key in self.keys.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            con = cipher.connect(str(path))
            con.execute('PRAGMA key = "x\'%s\'"' % key)
            con.execute('PRAGMA cipher_compatibility=4')
            con.execute('PRAGMA cipher_page_size=4096')
            if relative.startswith('message/'):
                con.execute('CREATE TABLE Name2Id(user_name TEXT,is_session INTEGER)')
                con.executemany('INSERT INTO Name2Id(rowid,user_name,is_session) VALUES (?,?,0)',
                                [(1, PEER), (2, OTHER)])
                for chat in (PEER, GROUP, OTHER):
                    con.execute('CREATE TABLE "%s" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                                'create_time INTEGER,real_sender_id INTEGER,message_content BLOB,'
                                'WCDB_CT_message_content INTEGER)' % table(chat))
            else:
                con.execute('CREATE TABLE contact(username TEXT,nick_name TEXT,remark TEXT,alias TEXT,private_body TEXT)')
                con.executemany('INSERT INTO contact VALUES (?,?,?,?,?)',
                    [(PEER, 'SyntheticPeer', 'SyntheticPeerRemark', 'SyntheticPeerAlias', UNSELECTED),
                     (GROUP, 'SyntheticGroup', '', '', UNSELECTED)])
            con.commit()
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA wal_autocheckpoint=0')
            self.writers[relative] = con
        self.writer = self.writers['message/message_0.db']
        self.insert(PEER, 1, OLD)
        self.insert(GROUP, 1, TOKENS['group_test'][0])
        self.insert(OTHER, 1, UNSELECTED)
        self.writer.commit()
        self.driver = GuardedDriver(self.root)
        self.config = {'conversations': [
            {'alias': 'private_test', 'chat_id': PEER, 'table': table(PEER), 'expected_sender_id': PEER},
            {'alias': 'group_test', 'chat_id': GROUP, 'table': table(GROUP), 'expected_sender_id': PEER}]}

    def tearDown(self):
        for poller in self.pollers:
            poller.close()
        for writer in self.writers.values():
            writer.close()
        self.tmp.cleanup()

    def insert(self, chat, local_id, body, kind=1, sender=1, compression=0, shard=0):
        self.writers['message/message_%d.db' % shard].execute('INSERT INTO "%s" VALUES (?,?,?,?,?,?)' % table(chat),
            (local_id, kind, 1900000000 + local_id, sender, body, compression))

    def poller(self):
        instance = engine.Poller(self.driver, self.root, self.keys, self.config, TOKENS)
        self.pollers.append(instance)
        return instance

    def assert_redacted(self, *values):
        text = json.dumps(values, ensure_ascii=False)
        for value in (PEER, GROUP, OTHER, OLD, UNSELECTED, UNMATCHED, str(self.root),
                      'SyntheticPeer', 'SyntheticGroup', *self.keys.values(), *sum(TOKENS.values(), [])):
            self.assertNotIn(value, text)

    def test_resolve_uses_only_four_contact_fields_with_exact_binary_and_group_filter(self):
        # The same display name on a private contact must not confuse group lookup.
        contact = self.writers['contact/contact.db']
        contact.execute('INSERT INTO contact VALUES (?,?,?,?,?)', (OTHER, 'SyntheticGroup', '', '', UNSELECTED))
        contact.commit()
        config = engine.resolve(self.driver, self.root, self.keys, 'SyntheticPeerAlias', 'SyntheticGroup')
        self.assertEqual([row['chat_id'] for row in config['conversations']], [PEER, GROUP])
        self.assertEqual({row['expected_sender_id'] for row in config['conversations']}, {PEER})
        self.assertEqual(self.driver.paths, [(self.root / 'contact/contact.db').resolve()])
        self.assertEqual(self.driver.forbidden_reads, [])
        with self.assertRaises(engine.EngineError) as caught:
            engine.resolve(self.driver, self.root, self.keys, 'syntheticpeer', 'SyntheticGroup')
        self.assertEqual(caught.exception.code, 'private_test_contact_not_unique')

    def test_ambiguous_contact_metadata_is_rejected_without_names_in_error(self):
        contact = self.writers['contact/contact.db']
        contact.execute('INSERT INTO contact VALUES (?,?,?,?,?)', (OTHER, 'SyntheticPeer', '', '', UNSELECTED))
        contact.commit()
        with self.assertRaises(engine.EngineError) as caught:
            engine.resolve(self.driver, self.root, self.keys, 'SyntheticPeer', 'SyntheticGroup')
        self.assertEqual(caught.exception.code, 'private_test_contact_not_unique')
        self.assertNotIn('SyntheticPeer', str(caught.exception))

    def test_duplicate_same_username_is_one_contact_identity(self):
        contact = self.writers['contact/contact.db']
        contact.execute('INSERT INTO contact VALUES (?,?,?,?,?)', (PEER, 'SyntheticPeer', '', '', UNSELECTED))
        contact.commit()
        config = engine.resolve(self.driver, self.root, self.keys, 'SyntheticPeer', 'SyntheticGroup')
        self.assertEqual(config['conversations'][0]['chat_id'], PEER)

    def test_new_wal_text_only_selected_conversations_are_exactly_verified_and_redacted(self):
        reader = self.poller()
        self.assertEqual(reader.poll(lambda: BACKGROUND), [])
        for index, token in enumerate(TOKENS['private_test'], 2):
            self.insert(PEER, index, token)
        self.insert(GROUP, 2, PEER + ':\n' + TOKENS['group_test'][0])
        self.insert(GROUP, 3, PEER + ':\n' + TOKENS['group_test'][1])
        self.insert(PEER, 4, UNMATCHED)
        self.insert(PEER, 5, TOKENS['private_test'][0], kind=3)
        self.insert(OTHER, 2, TOKENS['private_test'][0])
        self.writer.commit()
        self.assertGreater(Path(str(self.root / 'message/message_0.db') + '-wal').stat().st_size, 32)
        state = Mock(return_value=BACKGROUND)
        observed = reader.poll(state)
        self.assertEqual(state.call_count, 2)
        self.assertEqual(len(observed), 4)
        self.assertTrue(all(row[field] is True for row in observed for field in
                            ('body_exact_match', 'expected_sender_match', 'background_confirmed')))
        self.assertEqual(reader.summary()['status'], '通过')
        self.assertEqual(reader.summary()['unique_source_messages'], 4)
        self.assertEqual(reader.poll(lambda: BACKGROUND), [])
        self.assertEqual(self.driver.forbidden_reads, [])
        self.assertEqual(self.driver.old_reads, [])
        self.assert_redacted(observed, reader.summary())

    def test_tokens_are_never_matched_against_the_other_selected_conversation(self):
        reader = self.poller()
        self.insert(PEER, 2, TOKENS['group_test'][0])
        self.insert(GROUP, 2, PEER + ':\n' + TOKENS['private_test'][0])
        self.writer.commit()
        self.assertEqual(reader.poll(lambda: BACKGROUND), [])

    def test_selected_table_created_in_a_previously_missing_shard_is_discovered(self):
        other = self.writers['message/message_1.db']
        other.execute('DROP TABLE "%s"' % table(GROUP))
        other.commit()
        reader = self.poller()
        self.assertNotIn(('message/message_1.db', 'group_test'), reader._cursors)
        # A malformed newly-created selected table must not commit a new cursor.
        other.execute('CREATE TABLE "%s" (local_id INTEGER PRIMARY KEY)' % table(GROUP))
        other.commit()
        with self.assertRaises(engine.EngineError) as caught:
            reader.poll(lambda: BACKGROUND)
        self.assertEqual(caught.exception.code, 'selected_message_schema_unsupported')
        self.assertNotIn(('message/message_1.db', 'group_test'), reader._cursors)
        other.execute('DROP TABLE "%s"' % table(GROUP))
        other.execute('CREATE TABLE "%s" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                      'create_time INTEGER,real_sender_id INTEGER,message_content BLOB,'
                      'WCDB_CT_message_content INTEGER)' % table(GROUP))
        self.insert(GROUP, 1, PEER + ':\n' + TOKENS['group_test'][0], shard=1)
        other.commit()
        observed = reader.poll(lambda: BACKGROUND)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]['alias'], 'group_test')
        self.assertTrue(observed[0]['body_exact_match'])
        self.assertEqual(reader.poll(lambda: BACKGROUND), [])

    def test_paging_keeps_all_605_identical_real_messages_and_does_not_jump_to_media_upper(self):
        reader = self.poller()
        for local_id in range(2, 607):
            self.insert(PEER, local_id, TOKENS['private_test'][0])
        self.insert(PEER, 900, TOKENS['private_test'][0], kind=3)
        self.writer.commit()
        first, second = reader.poll(lambda: BACKGROUND), reader.poll(lambda: BACKGROUND)
        self.assertEqual((len(first), len(second)), (500, 105))
        self.assertEqual(len({row['event_id_digest'] for row in first + second}), 605)
        self.assertEqual(reader._cursors[('message/message_0.db', 'private_test')], 606)
        self.assertEqual(reader.poll(lambda: BACKGROUND), [])

    def test_busy_after_a_prior_selected_read_rolls_back_all_ram_progress(self):
        reader = self.poller()
        before = dict(reader._cursors)
        self.insert(PEER, 2, TOKENS['private_test'][0])
        self.insert(GROUP, 2, PEER + ':\n' + TOKENS['group_test'][0])
        self.writer.commit()
        self.driver.fail_table = table(GROUP)
        with self.assertRaises(engine.EngineError) as caught:
            reader.poll(lambda: BACKGROUND)
        self.assertEqual(caught.exception.code, 'selected_poll_or_native_state_failed')
        self.assertNotIn('SYNTHETIC_PRIVATE_DETAIL', str(caught.exception))
        self.assertEqual(reader._cursors, before)
        self.assertEqual(reader.observations, [])
        self.assertGreaterEqual(self.driver.rollbacks, 2)
        self.assertEqual(len(reader.poll(lambda: BACKGROUND)), 2)
        self.assertEqual(reader.poll(lambda: BACKGROUND), [])

    def test_compression_4_and_5_are_bounded_and_dictionary_2_and_3_fail_closed(self):
        reader = self.poller()
        compressed = zstandard.ZstdCompressor().compress(TOKENS['private_test'][0].encode())
        self.insert(PEER, 2, compressed, compression=2)
        self.writer.commit()
        before = dict(reader._cursors)
        for flag in (2, 3):
            self.writer.execute('UPDATE "%s" SET WCDB_CT_message_content=? WHERE local_id=2' % table(PEER), (flag,))
            self.writer.commit()
            with self.assertRaises(engine.EngineError) as caught:
                reader.poll(lambda: BACKGROUND)
            self.assertEqual(caught.exception.code, 'dictionary_compression_unsupported')
            self.assertEqual(reader._cursors, before)
        self.writer.execute('UPDATE "%s" SET WCDB_CT_message_content=4 WHERE local_id=2' % table(PEER))
        self.insert(PEER, 3, zstandard.ZstdCompressor().compress(TOKENS['private_test'][1].encode()), compression=5)
        self.writer.commit()
        self.assertEqual([row['body_exact_match'] for row in reader.poll(lambda: BACKGROUND)], [True, True])
        too_large = zstandard.ZstdCompressor().compress(b'A' * (engine.MAX_BODY + 1))
        with self.assertRaises(engine.EngineError):
            engine._decode(too_large, 4)
        for flag in (4, 5):
            with self.assertRaises(engine.EngineError) as caught:
                engine._decode(TOKENS['private_test'][0], flag)
            self.assertEqual(caught.exception.code, 'compressed_message_storage_type_unsupported')

    def test_private_prefix_and_disagreeing_group_prefix_are_not_stripped(self):
        reader = self.poller()
        self.insert(PEER, 2, PEER + ':\n' + TOKENS['private_test'][0])
        self.insert(PEER, 3, PEER + '\n' + TOKENS['private_test'][1])
        self.insert(GROUP, 2, OTHER + ':\n' + TOKENS['group_test'][0], sender=1)
        self.insert(GROUP, 3, PEER + '\n' + TOKENS['group_test'][1], sender=1)
        self.insert(GROUP, 4, TOKENS['group_test'][1], sender=None)
        self.writer.commit()
        observed = reader.poll(lambda: BACKGROUND)
        self.assertEqual([row['body_exact_match'] for row in observed], [False, False, False, False, True])
        self.assertIsNone(observed[-1]['expected_sender_match'])
        self.assertEqual(reader.summary()['status'], '失败')
        self.assert_redacted(observed, reader.summary())

    def test_foreground_or_unknown_probe_cannot_pass_background_verification(self):
        reader = self.poller()
        self.insert(PEER, 2, TOKENS['private_test'][0])
        self.writer.commit()
        states = iter([BACKGROUND, dict(BACKGROUND, frontmost=True)])
        self.assertFalse(reader.poll(lambda: next(states))[0]['background_confirmed'])
        self.assertEqual(reader.summary()['status'], '失败')

    def test_sqlite_without_sqlcipher_is_rejected_and_sources_are_readonly(self):
        with self.assertRaises(engine.EngineError) as caught:
            engine.Poller(sqlite3, self.root, self.keys, self.config, TOKENS)
        self.assertEqual(caught.exception.code, 'sqlcipher_runtime_required')
        with self.poller() as reader:
            for connection in reader._connections.values():
                self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0], 1)
                with self.assertRaises(cipher.DatabaseError):
                    connection.execute('DELETE FROM "%s"' % table(PEER))
        with self.assertRaises(engine.EngineError):
            reader.poll(lambda: BACKGROUND)


if __name__ == '__main__':
    unittest.main()
