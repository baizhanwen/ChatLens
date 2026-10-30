"""Synthetic encrypted DB/WAL checks; never open an actual WeChat account.

Run with .build/wechat-read-venv/bin/python -m unittest discover -s tests
-p test_wechat_selected_reader.py -v. Fixtures use invented identifiers, keys,
messages and a fixed fake native-state result; no native app or network starts.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "wechat_selected_reader.py"
SPEC = importlib.util.spec_from_file_location("wechat_selected_reader_under_test", MODULE_PATH)
selected = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = selected
SPEC.loader.exec_module(selected)

try:
    import sqlcipher3.dbapi2 as cipher
except ImportError:
    cipher = None


PRIVATE_CHAT = "wxid_synthetic_selected_private"
GROUP_CHAT = "synthetic_selected_group@chatroom"
UNSELECTED_CHAT = "wxid_synthetic_unselected"
SENDER = "wxid_synthetic_sender"
OTHER_SENDER = "wxid_synthetic_other_sender"
PRIVATE_TOKEN = "SYNTHETIC_PRIVATE_EXPECTED"
GROUP_TOKEN = "SYNTHETIC_GROUP_EXPECTED"
UNMATCHED = "SYNTHETIC_UNMATCHED_PRIVATE_BODY_DO_NOT_PUBLISH"
UNSELECTED_BODY = "SYNTHETIC_UNSELECTED_BODY_DO_NOT_READ"
OLD_BODY = "SYNTHETIC_OLD_SELECTED_BODY_DO_NOT_READ"
SYNTHETIC_KEY = "27" * 32
BACKGROUND = {
    "probe_succeeded": True,
    "running": True,
    "frontmost": False,
    "hidden": True,
    "screen_locked": False,
    "all_ax_windows_minimized": None,
}


def table_for(chat_id):
    return "Msg_" + hashlib.md5(chat_id.encode()).hexdigest()


class GuardedDriver:
    """Real SQLCipher plus a boundary against unselected-body access."""

    def __init__(self, root):
        self.root = root.resolve()
        self.denied_body_reads = []
        self.body_reads = []
        self.old_body_reads = []

    def __getattr__(self, name):
        return getattr(cipher, name)

    def connect(self, database, *args, **kwargs):
        from urllib.parse import unquote, urlparse

        value = str(database)
        path = Path(unquote(urlparse(value).path)) if value.startswith("file:") else Path(value)
        # Prevent even an accidental fixture error from reaching user files.
        if not path.resolve().is_relative_to(self.root):
            raise AssertionError("synthetic driver refused a non-fixture path")
        connection = cipher.connect(database, *args, **kwargs)

        def text_factory(raw):
            if raw == OLD_BODY.encode():
                self.old_body_reads.append(True)
                raise AssertionError("old selected body was materialized")
            return raw.decode("utf-8")

        connection.text_factory = text_factory

        def authorize(action, table, column, database_name, trigger):
            if action == cipher.SQLITE_READ and column == "message_content":
                self.body_reads.append(table)
                if table == table_for(UNSELECTED_CHAT):
                    self.denied_body_reads.append(table)
                    return cipher.SQLITE_DENY
            return cipher.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection


@unittest.skipIf(cipher is None, "requires the isolated SQLCipher test environment")
class SelectedConversationEncryptedWalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="chatlens-synthetic-selected-")
        self.root = Path(self.temp.name)
        self.db_root = self.root / "synthetic_account" / "db_storage"
        (self.db_root / "message").mkdir(parents=True)
        self.key_file = self.root / "synthetic_keys.json"
        self.state_file = self.root / "private_state" / "reader_state.json"
        self.native_probe = self.root / "never-executed-native-probe"
        self.config = {
            "run_id": "synthetic-selected-reader-run",
            "conversations": [
                {"alias": "private", "chat_id": PRIVATE_CHAT, "tokens": [PRIVATE_TOKEN],
                 "expected_sender_id": SENDER},
                {"alias": "group", "chat_id": GROUP_CHAT, "tokens": [GROUP_TOKEN],
                 "expected_sender_id": SENDER},
            ],
        }
        self.writers = []
        self.readers = []
        self.keys = {}
        self.writer = self.make_shard(0)
        for chat_id, body in ((PRIVATE_CHAT, OLD_BODY), (GROUP_CHAT, GROUP_TOKEN),
                              (UNSELECTED_CHAT, UNSELECTED_BODY)):
            self.insert(self.writer, chat_id, 1, body)
        self.writer.commit()
        self.native_patch = patch.object(selected, "native_state", return_value=BACKGROUND)
        self.native_patch.start()
        self.addCleanup(self.native_patch.stop)
        self.driver = GuardedDriver(self.root)

    def tearDown(self):
        for reader in reversed(self.readers):
            try:
                reader.close()
            except Exception:
                pass
        for writer in reversed(self.writers):
            writer.close()
        self.temp.cleanup()

    def make_shard(self, number, root=None, key=SYNTHETIC_KEY):
        root = root or self.db_root
        (root / "message").mkdir(parents=True, exist_ok=True)
        path = root / "message" / ("message_%d.db" % number)
        writer = cipher.connect(str(path))
        writer.execute('PRAGMA key = "x\'%s\'"' % key)
        writer.execute("PRAGMA cipher_compatibility = 4")
        writer.execute("PRAGMA cipher_page_size = 4096")
        writer.execute("CREATE TABLE Name2Id (user_name TEXT, is_session INTEGER)")
        writer.executemany("INSERT INTO Name2Id (rowid,user_name,is_session) VALUES (?,?,?)",
                           [(1, SENDER, 0), (2, OTHER_SENDER, 0),
                            (3, PRIVATE_CHAT, 1), (4, GROUP_CHAT, 1),
                            (5, UNSELECTED_CHAT, 1)])
        for chat_id in (PRIVATE_CHAT, GROUP_CHAT, UNSELECTED_CHAT):
            writer.execute('CREATE TABLE "%s" ('
                           "local_id INTEGER PRIMARY KEY, create_time INTEGER, server_id INTEGER, "
                           "local_type INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER, "
                           "real_sender_id INTEGER)" % table_for(chat_id))
        writer.commit()
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        self.writers.append(writer)
        if root == self.db_root:
            self.keys["message/message_%d.db" % number] = {"enc_key": key}
            self.key_file.write_text(json.dumps({"keys": self.keys}), encoding="utf-8")
        return writer

    def insert(self, writer, chat_id, local_id, body, local_type=1, sender=1, compression=0):
        writer.execute('INSERT INTO "%s" VALUES (?,?,?,?,?,?,?)' % table_for(chat_id),
                       (local_id, 1900000000 + local_id, 9000000000 + local_id,
                        local_type, body, compression, sender))

    def reader(self, config=None, db_root=None, driver=None):
        reader = selected.SelectedReader(db_root or self.db_root, self.key_file,
                                         config or self.config, self.state_file,
                                         self.native_probe, driver=driver or self.driver)
        self.readers.append(reader)
        return reader

    def observations_for(self, reader, alias):
        return [row for row in reader.observations if row["alias"] == alias]

    def assert_redacted(self, *values):
        encoded = json.dumps(values, ensure_ascii=False, default=str)
        for value in (SYNTHETIC_KEY, UNMATCHED, UNSELECTED_BODY, OLD_BODY, str(self.root),
                      PRIVATE_CHAT, GROUP_CHAT, SENDER, OTHER_SENDER):
            self.assertNotIn(value, encoded)

    def test_only_new_selected_text_is_routed_and_unselected_bodies_are_never_queried(self):
        reader = self.reader()
        self.assertEqual(reader.step(), [])
        self.assertEqual(reader.observations, [])
        self.insert(self.writer, PRIVATE_CHAT, 2, PRIVATE_TOKEN)
        self.insert(self.writer, PRIVATE_CHAT, 3, UNMATCHED)
        self.insert(self.writer, PRIVATE_CHAT, 4, PRIVATE_TOKEN, local_type=3)
        self.insert(self.writer, GROUP_CHAT, 2, SENDER + ":\n" + GROUP_TOKEN)
        self.insert(self.writer, UNSELECTED_CHAT, 2, PRIVATE_TOKEN)
        self.writer.commit()
        self.assertGreater(Path(str(self.db_root / "message/message_0.db") + "-wal").stat().st_size, 32)
        events = reader.step()
        self.assertCountEqual([event["alias"] for event in events], ["private", "group"])
        self.assertTrue(all(event["body_exact_match"] for event in events))
        self.assertTrue(all(event["background_confirmed"] for event in events))
        self.assertEqual(self.driver.denied_body_reads, [])
        self.assertEqual(self.driver.old_body_reads, [])
        self.assertNotIn(table_for(UNSELECTED_CHAT), self.driver.body_reads)
        self.assertEqual(reader.step(), [])
        self.assertEqual(len(reader.observations), 2)
        self.assert_redacted(events, reader.summary(), json.loads(self.state_file.read_text()))

    def test_tokens_cannot_cross_selected_conversation_routes(self):
        reader = self.reader()
        self.insert(self.writer, PRIVATE_CHAT, 2, GROUP_TOKEN)
        self.insert(self.writer, GROUP_CHAT, 2, SENDER + ":\n" + PRIVATE_TOKEN)
        self.writer.commit()
        self.assertEqual(reader.step(), [])
        self.assertEqual(reader.observations, [])

    def test_restart_preserves_observations_and_catches_up_without_replaying(self):
        first = self.reader()
        self.insert(self.writer, PRIVATE_CHAT, 2, PRIVATE_TOKEN)
        self.writer.commit()
        self.assertEqual(len(first.step()), 1)
        old_digest = first.observations[0]["event_id_digest"]
        first.close()
        self.insert(self.writer, PRIVATE_CHAT, 3, PRIVATE_TOKEN)
        self.insert(self.writer, GROUP_CHAT, 2, SENDER + ":\n" + GROUP_TOKEN)
        self.writer.commit()
        restored = self.reader()
        self.assertEqual(len(restored.observations), 1)
        self.assertEqual(restored.observations[0]["event_id_digest"], old_digest)
        new_events = restored.step()
        self.assertEqual(len(new_events), 2)
        self.assertEqual(len(restored.observations), 3)
        self.assertEqual(len({row["event_id_digest"] for row in restored.observations}), 3)
        self.assertEqual(restored.step(), [])

    def test_atomic_state_failure_does_not_advance_disk_or_memory(self):
        reader = self.reader()
        saved = self.state_file.read_bytes()
        observations = copy.deepcopy(reader.observations)
        self.insert(self.writer, PRIVATE_CHAT, 2, PRIVATE_TOKEN)
        self.writer.commit()
        with patch.object(selected.os, "replace", side_effect=OSError("synthetic state commit failure")):
            with self.assertRaises(Exception):
                reader.step()
        self.assertEqual(self.state_file.read_bytes(), saved)
        self.assertEqual(reader.observations, observations)
        self.assertEqual(len(reader.step()), 1)
        self.assertEqual(reader.step(), [])
        self.assertEqual(len(reader.observations), 1)

    def test_more_than_one_page_of_identical_text_does_not_skip_or_deduplicate_real_rows(self):
        reader = self.reader()
        for local_id in range(2, 607):
            self.insert(self.writer, PRIVATE_CHAT, local_id, PRIVATE_TOKEN)
        self.writer.commit()
        events = []
        for _ in range(5):
            batch = reader.step()
            events.extend(batch)
            if not batch:
                break
        self.assertEqual(len(events), 605)
        self.assertEqual(len({event["event_id_digest"] for event in events}), 605)
        self.assertEqual(len(self.observations_for(reader, "private")), 605)
        self.assertEqual(reader.step(), [])

    def test_group_prefix_is_removed_and_sender_id_is_resolved_from_name2id(self):
        reader = self.reader()
        self.insert(self.writer, GROUP_CHAT, 2, SENDER + ":\n" + GROUP_TOKEN, sender=1)
        self.insert(self.writer, GROUP_CHAT, 3, OTHER_SENDER + ":\n" + GROUP_TOKEN, sender=2)
        self.writer.commit()
        events = reader.step()
        self.assertEqual(len(events), 2)
        self.assertTrue(all(event["body_exact_match"] for event in events))
        self.assertEqual([event["expected_sender_match"] for event in events], [True, False])
        self.assertNotEqual(events[0]["sender_digest"], events[1]["sender_digest"])
        self.assert_redacted(events)

    def test_restart_rejects_changed_config_before_consuming_messages(self):
        first = self.reader()
        first.close()
        previous_state = self.state_file.read_bytes()
        changed = copy.deepcopy(self.config)
        changed["conversations"][0]["tokens"] = ["SYNTHETIC_CHANGED_EXPECTATION"]
        with self.assertRaises(Exception):
            self.reader(config=changed)
        self.assertEqual(self.state_file.read_bytes(), previous_state)

    def test_restart_rejects_different_account_root(self):
        first = self.reader()
        first.close()
        other_root = self.root / "different_synthetic_account" / "db_storage"
        self.make_shard(0, root=other_root)
        previous_state = self.state_file.read_bytes()
        with self.assertRaises(Exception):
            self.reader(db_root=other_root)
        self.assertEqual(self.state_file.read_bytes(), previous_state)

    def test_restart_rejects_replaced_database_with_a_new_salt(self):
        first = self.reader()
        first.close()
        previous_state = self.state_file.read_bytes()
        self.writer.close()
        path = self.db_root / "message/message_0.db"
        for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
            if candidate.exists():
                candidate.unlink()
        self.make_shard(0)
        with self.assertRaises(Exception):
            self.reader()
        self.assertEqual(self.state_file.read_bytes(), previous_state)

    def test_corrupt_persisted_state_is_rejected_instead_of_rebaselining(self):
        first = self.reader()
        first.close()
        self.state_file.write_text("{synthetic-invalid-json", encoding="utf-8")
        with self.assertRaises(Exception):
            self.reader()
        self.assertEqual(self.state_file.read_text(), "{synthetic-invalid-json")

    def test_state_and_parent_use_private_permissions_and_contain_no_plaintext_body_or_key(self):
        reader = self.reader()
        self.insert(self.writer, PRIVATE_CHAT, 2, UNMATCHED)
        self.insert(self.writer, PRIVATE_CHAT, 3, PRIVATE_TOKEN)
        self.writer.commit()
        reader.step()
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.state_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.state_file.parent.stat().st_mode), 0o700)
        self.assert_redacted(json.loads(self.state_file.read_text()), reader.summary(), reader.observations)

    def test_new_shard_with_selected_messages_is_not_silently_skipped(self):
        reader = self.reader()
        new_writer = self.make_shard(1)
        self.insert(new_writer, PRIVATE_CHAT, 1, PRIVATE_TOKEN)
        new_writer.commit()
        events = reader.step()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["alias"], "private")
        self.assertEqual(reader.step(), [])

    def test_new_shard_without_a_key_fails_closed_and_does_not_advance_state(self):
        reader = self.reader()
        before = self.state_file.read_bytes()
        self.make_shard(1)
        self.keys.pop("message/message_1.db")
        self.key_file.write_text(json.dumps({"keys": self.keys}), encoding="utf-8")
        with self.assertRaises(Exception):
            reader.step()
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(reader.observations, [])

    def test_frontmost_or_unknown_lock_state_is_not_reported_as_background_verified(self):
        reader = self.reader()
        for local_id, native_state in enumerate((dict(BACKGROUND, frontmost=True),
                                                  dict(BACKGROUND, screen_locked=None)), start=2):
            with self.subTest(native_state=native_state):
                self.insert(self.writer, PRIVATE_CHAT, local_id, PRIVATE_TOKEN)
                self.writer.commit()
                with patch.object(selected, "native_state", return_value=native_state):
                    events = reader.step()
                self.assertEqual(len(events), 1)
                self.assertFalse(events[0]["background_confirmed"])


if __name__ == "__main__":
    unittest.main()
