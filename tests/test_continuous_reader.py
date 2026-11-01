"""SQLCipher/WAL and durable restart tests using synthetic accounts only."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
if str(MODULES) not in sys.path:
    sys.path.insert(0, str(MODULES))
import continuous_reader as reader_module
from continuous_store import CollectorStore, DB_NAME

try:
    import sqlcipher3.dbapi2 as cipher
    import zstandard
except ImportError:
    cipher = zstandard = None

PEER, GROUP, OTHER = 'synthetic_reader_peer', 'synthetic_reader_group@chatroom', 'synthetic_reader_other'
OLD = 'SYNTHETIC_OLD_BODY_FORBIDDEN'
UNSELECTED = 'SYNTHETIC_UNSELECTED_BODY_FORBIDDEN'
BACKGROUND = {'probe_succeeded': True, 'running': True, 'frontmost': False, 'screen_locked': False}


def table(chat):
    return 'Msg_' + hashlib.md5(chat.encode()).hexdigest()


class RestrictedDriver:
    def __init__(self, root):
        self.root = root.resolve()
        self.forbidden_reads, self.old_reads = [], []
        self.fail_table = None

    def connect(self, database, *args, **kwargs):
        from urllib.parse import unquote, urlparse
        path = Path(unquote(urlparse(str(database)).path)).resolve()
        if not path.is_relative_to(self.root):
            raise AssertionError('synthetic roots only')
        connection = cipher.connect(database, *args, **kwargs)

        def authorize(action, source, column, *_):
            if action == cipher.SQLITE_READ and source == table(OTHER) and column == 'message_content':
                self.forbidden_reads.append(True)
                return cipher.SQLITE_DENY
            return cipher.SQLITE_OK

        def text_factory(value):
            if value == OLD.encode():
                self.old_reads.append(True)
                raise AssertionError('historical body must not be materialized')
            return value.decode()

        connection.set_authorizer(authorize)
        connection.text_factory = text_factory
        driver = self

        class Proxy:
            def __getattr__(self, name):
                return getattr(connection, name)

            def execute(self, sql, *args):
                if sql.startswith('SELECT local_id,') and driver.fail_table and driver.fail_table in sql:
                    driver.fail_table = None
                    error = cipher.OperationalError('SYNTHETIC_PRIVATE_ERROR locked')
                    error.sqlite_errorcode = 5
                    raise error
                return connection.execute(sql, *args)

        return Proxy()


@unittest.skipIf(cipher is None, 'requires isolated SQLCipher and zstandard')
class ContinuousReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-continuous-reader-')
        self.root = Path(self.tmp.name).resolve() / 'synthetic-source'
        self.root.mkdir()
        self.state = Path(self.tmp.name).resolve() / 'private-state'
        self.state.mkdir(mode=0o700)
        self.keys = {'message/message_0.db': '25' * 32, 'message/message_1.db': '59' * 32}
        self.writers, self.readers, self.stores = {}, [], []
        for relative, key in self.keys.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            con = cipher.connect(str(path))
            con.execute('PRAGMA key = "x\'%s\'"' % key)
            con.execute('PRAGMA cipher_compatibility=4')
            con.execute('PRAGMA cipher_page_size=4096')
            con.execute('CREATE TABLE Name2Id(user_name TEXT,is_session INTEGER)')
            con.executemany('INSERT INTO Name2Id(rowid,user_name,is_session) VALUES(?,?,0)', [(1, PEER), (2, OTHER)])
            for chat in (PEER, GROUP, OTHER):
                self.create_table(con, chat)
            con.commit()
            con.execute('PRAGMA journal_mode=WAL')
            con.execute('PRAGMA wal_autocheckpoint=0')
            self.writers[relative] = con
        self.writer = self.writers['message/message_0.db']
        self.insert(PEER, 1, OLD)
        self.insert(GROUP, 1, OLD)
        self.insert(OTHER, 1, UNSELECTED)
        self.writer.commit()
        self.driver = RestrictedDriver(self.root)
        self.config = {'conversations': [
            {'alias': 'private_test', 'chat_id': PEER, 'table': table(PEER), 'expected_sender_id': PEER},
            {'alias': 'group_test', 'chat_id': GROUP, 'table': table(GROUP), 'expected_sender_id': PEER}]}
        self.binding = {'account': 'synthetic-account-only', 'config': self.config}

    def tearDown(self):
        for instance in self.readers:
            instance.close()
        for storage in self.stores:
            storage.close()
        for writer in self.writers.values():
            writer.close()
        self.tmp.cleanup()

    def create_table(self, con, chat):
        con.execute('CREATE TABLE "%s" (local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                    'create_time INTEGER,real_sender_id INTEGER,message_content BLOB,WCDB_CT_message_content INTEGER)' % table(chat))

    def insert(self, chat, local_id, body, kind=1, sender=1, compression=0, shard=0):
        self.writers['message/message_%d.db' % shard].execute('INSERT INTO "%s" VALUES(?,?,?,?,?,?)' % table(chat),
            (local_id, kind, 1900000000 + local_id, sender, body, compression))

    def storage(self, **kwargs):
        storage = CollectorStore(self.state, self.binding, **kwargs)
        self.stores.append(storage)
        return storage

    def reader(self, storage=None, native=None):
        storage = storage or self.storage()
        instance = reader_module.ContinuousReader(self.driver, self.root, self.keys, self.config, storage,
                                                  native or (lambda: BACKGROUND))
        self.readers.append(instance)
        return instance, storage

    def saved(self):
        with sqlite3.connect(self.state / DB_NAME) as connection:
            return connection.execute('SELECT alias,source_local_id,sender_id,body FROM messages ORDER BY alias,source_local_id').fetchall()

    def test_explicit_single_mac_shard_baseline_poll_and_restart(self):
        relative = 'message/message_1.db'
        self.writers.pop(relative).close()
        (self.root / relative).unlink()
        storage = self.storage()
        reader = reader_module.ContinuousReader(self.driver, self.root, self.keys, self.config, storage,
                                                 lambda: BACKGROUND, shards=('message/message_0.db',))
        self.readers.append(reader)
        self.assertEqual(set(storage.snapshot()['cursors']), {'message/message_0.db'})
        self.insert(PEER, 2, 'SYNTHETIC_MAC_SINGLE_SHARD_NEW')
        self.writer.commit()
        self.assertEqual(reader.poll()['new_messages'], 1)
        reader.close(); storage.close()
        storage = self.storage()
        restored = reader_module.ContinuousReader(self.driver, self.root, self.keys, self.config, storage,
                                                   lambda: BACKGROUND, shards=('message/message_0.db',))
        self.readers.append(restored)
        self.assertTrue(restored.resumed)
        self.assertEqual(restored.poll()['new_messages'], 0)
        self.assertEqual(storage.stats()['total_messages'], 1)
        self.assertEqual(self.driver.old_reads, [])
        self.assertEqual(self.driver.forbidden_reads, [])

    def test_invalid_explicit_shards_and_uninitialized_subset_fail_without_baseline(self):
        storage = self.storage()
        for shards in ((), ('message/message_0.db',) * 2, ('../message_0.db',),
                       ('message/message_8.db',), ('message/message_0.db', [])):
            with self.subTest(shards=shards), self.assertRaises(reader_module.EngineError):
                reader_module.ContinuousReader(self.driver, self.root, self.keys, self.config, storage,
                                                lambda: BACKGROUND, shards=shards)
        with self.assertRaisesRegex(reader_module.EngineError, 'shard_set_changed'):
            reader_module.ContinuousReader(self.driver, self.root, self.keys, self.config, storage,
                                            lambda: BACKGROUND, shards=('message/message_0.db',))
        self.assertFalse(storage.snapshot()['initialized'])

    def test_live_stop_resume_catches_paused_messages_without_old_bodies_or_replays(self):
        reader, storage = self.reader()
        baseline = reader.baseline_completed_at
        self.assertFalse(reader.resumed)
        self.assertEqual(reader.poll()['new_messages'], 0)
        self.insert(PEER, 2, 'SYNTHETIC_LIVE_PRIVATE')
        self.insert(GROUP, 2, PEER + ':\nSYNTHETIC_LIVE_GROUP')
        self.insert(OTHER, 2, UNSELECTED)
        self.writer.commit()
        self.assertEqual(reader.poll()['new_messages'], 2)
        reader.close(); storage.close()
        self.insert(PEER, 3, 'SYNTHETIC_PAUSED_PRIVATE')
        self.insert(GROUP, 3, PEER + ':\nSYNTHETIC_PAUSED_GROUP')
        self.writer.commit()
        restored, storage = self.reader()
        self.assertTrue(restored.resumed)
        self.assertEqual(restored.baseline_completed_at, baseline)
        self.assertEqual(restored.poll()['new_messages'], 2)
        self.assertEqual(restored.poll()['new_messages'], 0)
        self.assertEqual(storage.stats()['total_messages'], 4)
        self.assertEqual([row[-1] for row in self.saved()], ['SYNTHETIC_LIVE_GROUP', 'SYNTHETIC_PAUSED_GROUP',
                                                               'SYNTHETIC_LIVE_PRIVATE', 'SYNTHETIC_PAUSED_PRIVATE'])
        self.assertEqual(self.driver.old_reads, [])
        self.assertEqual(self.driver.forbidden_reads, [])

    def test_identical_messages_paginate_and_trailing_media_cursor_advances_without_body_read(self):
        reader, storage = self.reader()
        for number in range(2, 607):
            self.insert(PEER, number, 'SYNTHETIC_IDENTICAL')
        self.insert(PEER, 900, OLD, kind=3)
        self.writer.commit()
        counts = [reader.poll()['new_messages'] for _ in range(6)]
        self.assertEqual(counts, [128, 128, 128, 128, 93, 0])
        self.assertEqual(storage.stats()['total_messages'], 605)
        self.assertEqual(storage.snapshot()['cursors']['message/message_0.db']['private_test']['local_id'], 900)
        self.insert(PEER, 901, OLD, kind=3)
        self.writer.commit()
        step = reader.poll()
        self.assertEqual(step['new_messages'], 0)
        self.assertTrue(step['cursors_advanced'])
        self.assertEqual(storage.snapshot()['cursors']['message/message_0.db']['private_test']['local_id'], 901)
        self.assertEqual(self.driver.old_reads, [])

    def test_uncommitted_wal_hidden_then_committed_visible_and_source_is_readonly(self):
        reader, storage = self.reader()
        self.insert(PEER, 2, 'SYNTHETIC_WAL_TEXT')
        self.assertEqual(reader.poll()['new_messages'], 0)
        self.writer.commit()
        self.assertEqual(reader.poll()['new_messages'], 1)
        for connection in reader._connections.values():
            self.assertEqual(connection.execute('PRAGMA query_only').fetchone()[0], 1)
            with self.assertRaises(cipher.DatabaseError):
                connection.execute('DELETE FROM "%s"' % table(PEER))

    def test_foreground_after_read_pauses_without_advancing_and_later_catches_up(self):
        reader, storage = self.reader()
        before = storage.snapshot()
        self.insert(PEER, 2, 'SYNTHETIC_PAUSE_BOUNDARY')
        self.writer.commit()
        states = iter([BACKGROUND, dict(BACKGROUND, frontmost=True)])
        reader._native = lambda: next(states)
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_native_state_not_ready'):
            reader.poll()
        self.assertEqual(storage.snapshot(), before)
        self.assertEqual(storage.stats()['total_messages'], 0)
        reader._native = lambda: BACKGROUND
        self.assertEqual(reader.poll()['new_messages'], 1)

    def test_unhealthy_initial_sample_does_not_establish_baseline(self):
        storage = self.storage()
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_native_state_not_ready'):
            self.reader(storage, lambda: dict(BACKGROUND, screen_locked=True))
        self.assertFalse(storage.snapshot()['initialized'])
        self.assertEqual(storage.stats()['total_messages'], 0)

    def test_busy_after_first_selected_query_keeps_all_state_and_redacts_exception(self):
        reader, storage = self.reader()
        before = storage.snapshot()
        self.insert(PEER, 2, 'SYNTHETIC_FIRST_QUERY')
        self.insert(GROUP, 2, PEER + ':\nSYNTHETIC_SECOND_QUERY')
        self.writer.commit()
        self.driver.fail_table = table(GROUP)
        with self.assertRaises(reader_module.EngineError) as caught:
            reader.poll()
        self.assertEqual(caught.exception.code, 'continuous_source_busy')
        self.assertNotIn('SYNTHETIC_PRIVATE_ERROR', str(caught.exception))
        self.assertEqual(storage.snapshot(), before)
        self.assertEqual(reader.poll()['new_messages'], 2)

    def test_unsupported_compression_prevents_partial_commit_then_bounded_decode_succeeds(self):
        reader, storage = self.reader()
        before = storage.snapshot()
        self.insert(PEER, 2, 'SYNTHETIC_BEFORE_BAD')
        encoded = zstandard.ZstdCompressor().compress((PEER + ':\nSYNTHETIC_COMPRESSED').encode())
        self.insert(GROUP, 2, encoded, compression=2)
        self.writer.commit()
        with self.assertRaisesRegex(reader_module.EngineError, 'dictionary_compression_unsupported'):
            reader.poll()
        self.assertEqual(storage.snapshot(), before)
        self.assertEqual(storage.stats()['total_messages'], 0)
        self.writer.execute('UPDATE "%s" SET WCDB_CT_message_content=4 WHERE local_id=2' % table(GROUP))
        self.writer.commit()
        self.assertEqual(reader.poll()['new_messages'], 2)
        self.assertEqual(self.saved()[0][-1], 'SYNTHETIC_COMPRESSED')

    def test_missing_table_state_survives_restart_new_table_catches_up_and_later_drop_stops(self):
        other = self.writers['message/message_1.db']
        other.execute('DROP TABLE "%s"' % table(GROUP)); other.commit()
        reader, storage = self.reader()
        self.assertFalse(storage.snapshot()['cursors']['message/message_1.db']['group_test']['present'])
        reader.close(); storage.close()
        self.create_table(other, GROUP)
        self.insert(GROUP, 1, PEER + ':\nSYNTHETIC_NEW_SHARD_TABLE', shard=1)
        other.commit()
        restored, storage = self.reader()
        self.assertTrue(restored.resumed)
        self.assertEqual(restored.poll()['new_messages'], 1)
        self.assertTrue(storage.snapshot()['cursors']['message/message_1.db']['group_test']['present'])
        before = storage.snapshot()
        other.execute('DROP TABLE "%s"' % table(GROUP)); other.commit()
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_selected_table_disappeared'):
            restored.poll()
        self.assertEqual(storage.snapshot(), before)

    def test_source_hardlink_is_refused_and_ancestor_redirect_is_detected(self):
        reader, storage = self.reader()
        before = storage.snapshot()
        source = self.root / 'message/message_1.db'
        link = self.root / 'synthetic-source-hardlink'
        try:
            os.link(source, link)
        except OSError:
            self.skipTest('synthetic hardlink creation unavailable')
        try:
            with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_path_unsafe'):
                reader.poll()
            self.assertEqual(storage.snapshot(), before)
        finally:
            link.unlink()
        self.assertEqual(reader.poll()['new_messages'], 0)
        # Redirecting a parent must be refused even if final DB inodes are unchanged.
        held = self.root.parent.with_name(self.root.parent.name + '-held')
        original_parent = self.root.parent
        original_parent.rename(held)
        try:
            original_parent.symlink_to(held, target_is_directory=True)
        except (OSError, NotImplementedError):
            held.rename(original_parent)
            self.skipTest('synthetic directory symlink creation unavailable')
        try:
            with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_path_unsafe'):
                reader.poll()
        finally:
            original_parent.unlink()
            held.rename(original_parent)
        self.assertEqual(storage.snapshot(), before)

    def test_nonempty_wal_losing_shm_stops_without_recreating_it_or_advancing(self):
        reader, storage = self.reader()
        self.insert(PEER, 2, 'SYNTHETIC_PENDING_WAL'); self.writer.commit()
        before = storage.snapshot()
        shm = Path(str(self.root / 'message/message_0.db') + '-shm')
        held = self.root / 'synthetic-held-shm'
        try:
            shm.rename(held)
        except OSError:
            self.skipTest('synthetic live sidecar rename unavailable')
        try:
            with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_wal_shm_unavailable'):
                reader.poll()
            self.assertFalse(shm.exists())
            self.assertEqual(storage.snapshot(), before)
        finally:
            held.rename(shm)
        self.assertEqual(reader.poll()['new_messages'], 1)

    def test_source_id_regression_stops_without_resetting_baseline(self):
        reader, storage = self.reader()
        self.insert(PEER, 2, 'SYNTHETIC_BEFORE_SOURCE_RESET'); self.writer.commit()
        self.assertEqual(reader.poll()['new_messages'], 1)
        before = storage.snapshot()
        self.writer.execute('DELETE FROM "%s" WHERE local_id=2' % table(PEER)); self.writer.commit()
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_cursor_regressed'):
            reader.poll()
        self.assertEqual(storage.snapshot(), before)

    def test_shard_set_change_or_file_replacement_never_rebuilds_baseline(self):
        reader, storage = self.reader()
        before = storage.snapshot()
        extra = self.root / 'message/message_2.db'
        extra.write_bytes(b'SYNTHETIC_UNSUPPORTED_SHARD')
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_shard_set_changed'):
            reader.poll()
        extra.unlink()
        source = self.root / 'message/message_1.db'
        replacement = self.root / 'replacement.db'
        shutil.copyfile(source, replacement)
        os.replace(replacement, source)
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_epoch_changed'):
            reader.poll()
        self.assertEqual(storage.snapshot(), before)

    def test_commit_failure_before_transaction_commit_retries_without_loss_or_duplicates(self):
        failed = []
        def fault(stage):
            if stage == 'before_commit' and not failed:
                failed.append(True)
                raise OSError('SYNTHETIC_PRIVATE_DEST_ERROR')
        reader, storage = self.reader(self.storage(fault_hook=fault))
        before = storage.snapshot()
        self.insert(PEER, 2, 'SYNTHETIC_ATOMIC_RECOVERY'); self.writer.commit()
        with self.assertRaisesRegex(Exception, 'collector_batch_commit_failed'):
            reader.poll()
        self.assertEqual(storage.snapshot(), before)
        self.assertEqual(storage.stats()['total_messages'], 0)
        reader.close(); storage.close()
        restored, storage = self.reader()
        self.assertEqual(restored.poll()['new_messages'], 1)
        self.assertEqual(restored.poll()['new_messages'], 0)

    def test_committed_batch_then_process_error_restarts_without_replaying_it(self):
        failed = []
        def fault(stage):
            if stage == 'after_commit' and not failed:
                failed.append(True)
                raise OSError('SYNTHETIC_AFTER_DURABLE_COMMIT')
        reader, storage = self.reader(self.storage(fault_hook=fault))
        self.insert(PEER, 2, 'SYNTHETIC_ALREADY_DURABLE'); self.writer.commit()
        with self.assertRaises(OSError):
            reader.poll()
        self.assertEqual(storage.stats()['total_messages'], 1)
        reader.close(); storage.close()
        restored, storage = self.reader()
        self.assertEqual(restored.poll()['new_messages'], 0)
        self.insert(PEER, 3, 'SYNTHETIC_AFTER_RESTART'); self.writer.commit()
        self.assertEqual(restored.poll()['new_messages'], 1)
        self.assertEqual(storage.stats()['total_messages'], 2)

    def test_changed_key_or_symlink_source_is_refused_before_checkpoint_can_advance(self):
        reader, storage = self.reader()
        before = storage.snapshot()
        reader.close(); storage.close()
        original = self.keys['message/message_1.db']
        self.keys['message/message_1.db'] = '99' * 32
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_epoch_changed'):
            self.reader()
        self.stores[-1].close()
        self.keys['message/message_1.db'] = original
        source = self.root / 'message/message_1.db'
        moved = self.root / 'held-synthetic-message1.db'
        source.rename(moved)
        try:
            source.symlink_to(moved)
        except (OSError, NotImplementedError):
            moved.rename(source)
            self.skipTest('synthetic symlink creation unavailable')
        storage = self.storage()
        with self.assertRaisesRegex(reader_module.EngineError, 'continuous_source_path_unsafe'):
            self.reader(storage)
        self.assertEqual(storage.snapshot(), before)
        source.unlink(); moved.rename(source)

    def test_memory_cap_stops_before_unconsumed_row_and_next_poll_finishes(self):
        reader, storage = self.reader()
        for number in range(2, 7):
            self.insert(PEER, number, 'SYNTHETIC_LARGE_BODY_%d' % number)
        self.writer.commit()
        with patch.object(reader_module, 'MAX_PAIR_BODY_BYTES', 50):
            self.assertEqual(reader.poll()['new_messages'], 2)
            self.assertEqual(storage.snapshot()['cursors']['message/message_0.db']['private_test']['local_id'], 3)
            self.assertEqual(reader.poll()['new_messages'], 2)
            self.assertEqual(reader.poll()['new_messages'], 1)
        self.assertEqual(storage.stats()['total_messages'], 5)

    def test_unknown_sender_stays_unknown_and_only_exact_group_sender_prefix_is_removed(self):
        reader, storage = self.reader()
        self.insert(PEER, 2, PEER + ':\nSYNTHETIC_PRIVATE_PREFIX')
        self.insert(GROUP, 2, OTHER + ':\nSYNTHETIC_DIFFERENT_PREFIX', sender=1)
        self.insert(GROUP, 3, 'SYNTHETIC_UNKNOWN_SENDER', sender=None)
        self.writer.commit()
        self.assertEqual(reader.poll()['new_messages'], 3)
        saved = self.saved()
        self.assertEqual(saved[0][-1], OTHER + ':\nSYNTHETIC_DIFFERENT_PREFIX')
        self.assertEqual(saved[1][-2], None)
        self.assertEqual(saved[2][-1], PEER + ':\nSYNTHETIC_PRIVATE_PREFIX')


if __name__ == '__main__':
    unittest.main()
