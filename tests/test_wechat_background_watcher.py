"""Explicit synthetic SQLCipher/WAL tests; no actual WeChat files or keys."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "wechat_background_watcher", Path(__file__).resolve().parents[1] / "scripts" / "watch_wechat_test_messages.py")
watcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watcher)

try:
    import sqlcipher3.dbapi2 as cipher
except ImportError:
    cipher = None


@unittest.skipIf(cipher is None, "requires the isolated SQLCipher test environment")
class SyntheticEncryptedWalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "message").mkdir()
        self.path = self.root / "message" / "message_0.db"
        self.key = "13" * 32  # Clearly synthetic, unrelated to any account.
        self.writer = cipher.connect(str(self.path))
        self.writer.execute('PRAGMA key = "x\'%s\'"' % self.key)
        self.writer.execute("PRAGMA cipher_compatibility = 4")
        self.writer.execute("PRAGMA cipher_page_size = 4096")
        self.writer.execute('CREATE TABLE "%s" (local_id INTEGER PRIMARY KEY, create_time INTEGER, server_id INTEGER, '
                            'local_type INTEGER, message_content BLOB, WCDB_CT_message_content INTEGER)' % watcher.TABLE)
        self.writer.execute('CREATE TABLE Msg_other_conversation (local_id INTEGER PRIMARY KEY, message_content TEXT)')
        self.writer.execute('INSERT INTO "%s" VALUES (1,100,1001,1,?,0)' % watcher.TABLE, ("SYNTHETIC_OLD_TOKEN",))
        self.writer.commit()
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.reader = watcher.open_readonly(self.path, self.key, cipher)

    def tearDown(self):
        self.reader.close()
        self.writer.close()
        self.temp.cleanup()

    def test_new_encrypted_wal_rows_are_read_without_old_bodies_or_other_chats(self):
        cursor = watcher.baseline(self.reader)
        self.assertEqual(cursor, 1)
        self.writer.execute('INSERT INTO "%s" VALUES (2,101,1002,1,?,0)' % watcher.TABLE, ("SYNTHETIC_NEW_TOKEN",))
        self.writer.execute('INSERT INTO "%s" VALUES (3,102,1003,3,?,0)' % watcher.TABLE, ("SYNTHETIC_MEDIA_TOKEN",))
        self.writer.execute('INSERT INTO Msg_other_conversation VALUES (1,?)', ("SYNTHETIC_SECRET_OTHER_CHAT",))
        self.writer.commit()
        self.assertGreater(Path(str(self.path) + "-wal").stat().st_size, 32)
        rows, updated = watcher.read_new_text(self.reader, cursor)
        self.assertEqual([row[4] for row in rows], ["SYNTHETIC_NEW_TOKEN"])
        self.assertEqual(updated, 3)
        rows_again, _ = watcher.read_new_text(self.reader, updated)
        self.assertEqual(rows_again, [])

    def test_query_only_and_readonly_connection_reject_writes(self):
        self.assertEqual(self.reader.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(cipher.DatabaseError):
            self.reader.execute('INSERT INTO "%s" VALUES (8,108,1008,1,?,0)' % watcher.TABLE, ("FORBIDDEN_SYNTHETIC_WRITE",))

    def test_bounded_pagination_does_not_skip_new_wal_rows(self):
        self.writer.executemany('INSERT INTO "%s" VALUES (?,?,?,1,?,0)' % watcher.TABLE,
                               [(i, 100 + i, 1000 + i, "SYNTHETIC_%04d" % i) for i in range(2, 607)])
        self.writer.commit()
        first, cursor = watcher.read_new_text(self.reader, 1)
        second, end = watcher.read_new_text(self.reader, cursor)
        self.assertEqual((len(first), len(second), end), (500, 105, 606))
        self.assertEqual(len({row[0] for row in first + second}), 605)

    def test_complete_run_excludes_old_other_and_unmatched_text_from_public_result(self):
        token, private_unmatched = "SYNTHETIC_EXPECTED_TOKEN", "SYNTHETIC_PRIVATE_UNMATCHED_TEXT"
        self.writer.execute('UPDATE "%s" SET message_content=? WHERE local_id=1' % watcher.TABLE, (token,))
        self.writer.commit()
        key_file = self.root / "synthetic_keys.json"
        key_file.write_text(json.dumps({"keys": {"message/message_0.db": {"enc_key": self.key}}}))
        output = self.root / "public_result.json"
        args = SimpleNamespace(db_root=self.root, key_file=key_file, tokens=[token], seconds=2,
                               output=output, native_probe=self.root / "unused-probe", stop_when_complete=True)
        background = {"probe_succeeded": True, "running": True, "frontmost": False,
                      "hidden": True, "screen_locked": False, "all_ax_windows_minimized": None}
        events = []

        def emit_and_insert(event, **fields):
            events.append(dict(event=event, **fields))
            if event == "ready":
                self.writer.execute('INSERT INTO "%s" VALUES (2,101,1002,1,?,0)' % watcher.TABLE, (token,))
                self.writer.execute('INSERT INTO "%s" VALUES (3,102,1003,1,?,0)' % watcher.TABLE, (private_unmatched,))
                self.writer.execute('INSERT INTO Msg_other_conversation VALUES (1,?)', (token,))
                self.writer.commit()

        with patch.object(watcher, "native_state", return_value=background), patch.object(watcher, "emit", side_effect=emit_and_insert):
            self.assertEqual(watcher.run(args, driver=cipher), 0)
        report = json.loads(output.read_text())
        self.assertEqual(report["status"], "通过")
        self.assertEqual(report["tokens"][0]["matched_count"], 1)
        self.assertTrue(report["tokens"][0]["body_exact_match"])
        self.assertTrue(report["background_verified"])
        serialized = json.dumps(dict(report=report, events=events))
        for private_value in (token, private_unmatched, self.key, str(self.root)):
            self.assertNotIn(private_value, serialized)
        with self.assertRaises(FileExistsError):
            watcher.save_report(output, {"replacement": True})
        self.assertEqual(json.loads(output.read_text()), report)


class SyntheticPrivacyAndCompressionTests(unittest.TestCase):
    def test_zstandard_body_and_background_require_two_matching_state_samples(self):
        try:
            import zstandard
        except ImportError:
            self.skipTest("requires isolated zstandard dependency")
        content = "SYNTHETIC_TEXT_TOKEN"
        compressed = zstandard.ZstdCompressor().compress(content.encode())
        self.assertEqual(watcher.decode_body(compressed, 4), content)
        background = {"probe_succeeded": True, "running": True, "frontmost": False, "screen_locked": False}
        self.assertTrue(watcher.confirmed_background(background, background))
        self.assertFalse(watcher.confirmed_background(background, dict(background, frontmost=True)))
        self.assertFalse(watcher.confirmed_background(background, dict(background, screen_locked=None)))


if __name__ == "__main__":
    unittest.main()
