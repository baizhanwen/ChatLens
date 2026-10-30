"""Synthetic SQLCipher contact metadata only; never access a real account."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from urllib.parse import unquote, urlparse


SPEC = importlib.util.spec_from_file_location(
    "selected_contact_resolver_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "configure_wechat_selected_test.py")
resolver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resolver)
try:
    import sqlcipher3.dbapi2 as cipher
except ImportError:
    cipher = None


SYNTHETIC_KEY = "63" * 32
PRIVATE_ID = "wxid_synthetic_test_friend"
GROUP_ID = "synthetic_selected_group@chatroom"
PRIVATE_NAME = "合成精确测试好友"
GROUP_NAME = "合成精确测试群"
PRIVATE_BODY = "SYNTHETIC_UNSELECTED_BODY_MUST_NOT_BE_READ"


class FixtureOnlyDriver:
    def __init__(self, root):
        self.root = root.resolve()
        self.body_reads = []

    def connect(self, database, *args, **kwargs):
        path = Path(unquote(urlparse(str(database)).path))
        if not path.resolve().is_relative_to(self.root):
            raise AssertionError("refused_non_fixture_database")
        connection = cipher.connect(database, *args, **kwargs)

        def authorize(action, table, column, database_name, trigger):
            if action == cipher.SQLITE_READ and column == "message_content":
                self.body_reads.append(table)
                return cipher.SQLITE_DENY
            return cipher.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection


@unittest.skipIf(cipher is None, "requires isolated SQLCipher test environment")
class SyntheticExactContactResolverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wechat-synthetic-contact-resolver-")
        self.root = Path(self.temp.name)
        self.db_root = self.root / "account_fixture" / "db_storage"
        (self.db_root / "contact").mkdir(parents=True)
        self.writer = cipher.connect(str(self.db_root / "contact/contact.db"))
        self.writer.execute('PRAGMA key = "x\'%s\'"' % SYNTHETIC_KEY)
        self.writer.execute("PRAGMA cipher_compatibility = 4")
        self.writer.execute("PRAGMA cipher_page_size = 4096")
        # NOCASE deliberately checks that the resolver forces exact BINARY.
        self.writer.execute("CREATE TABLE contact (username TEXT, nick_name TEXT COLLATE NOCASE, remark TEXT, alias TEXT)")
        self.writer.executemany("INSERT INTO contact VALUES (?,?,?,?)", [
            (PRIVATE_ID, PRIVATE_NAME, "合成私聊备注", "synthetic_private_alias"),
            (GROUP_ID, GROUP_NAME, "", ""),
            ("wxid_synthetic_unselected", PRIVATE_NAME + "额外", "", ""),
        ])
        self.writer.execute("CREATE TABLE Msg_unselected (message_content TEXT)")
        self.writer.execute("INSERT INTO Msg_unselected VALUES (?)", (PRIVATE_BODY,))
        self.writer.commit()
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.request = {"run_id": "synthetic-contact-resolution-run",
                        "private_name": PRIVATE_NAME, "group_name": GROUP_NAME,
                        "private_tokens": ["SYNTH_PRIVATE_%d" % i for i in range(3)],
                        "group_tokens": ["SYNTH_GROUP_%d" % i for i in range(3)]}
        self.request_path = self.root / "private_request.json"
        self.key_path = self.root / "private_keys.json"
        # Unrelated key entries are intentionally invalid: they must be ignored.
        self.keys = {"keys": {"contact/contact.db": {"enc_key": SYNTHETIC_KEY},
                              "message/message_0.db": {"enc_key": "UNRELATED_KEY_MUST_NOT_BE_USED"}}}
        self.output = self.root / "private_output" / "selected.json"
        self.driver = FixtureOnlyDriver(self.root)

    def tearDown(self):
        self.writer.close()
        self.temp.cleanup()

    def run_resolver(self, driver=None):
        self.request_path.write_text(json.dumps(self.request), encoding="utf-8")
        self.key_path.write_text(json.dumps(self.keys), encoding="utf-8")
        args = SimpleNamespace(request=self.request_path, key_file=self.key_path, output=self.output)
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            result = resolver.run(args, driver=driver or self.driver, db_root=self.db_root)
        return result, stream.getvalue()

    def assert_private_stdout(self, output):
        for secret in (SYNTHETIC_KEY, PRIVATE_ID, GROUP_ID, PRIVATE_NAME, GROUP_NAME,
                       PRIVATE_BODY, str(self.root), *self.request["private_tokens"], *self.request["group_tokens"]):
            self.assertNotIn(secret, output)

    def test_exact_encrypted_metadata_creates_private_config_without_any_message_body(self):
        result, stdout = self.run_resolver()
        self.assertEqual(result, 0, stdout)
        config = json.loads(self.output.read_text())
        self.assertEqual(config["run_id"], self.request["run_id"])
        self.assertEqual([c["chat_id"] for c in config["conversations"]], [PRIVATE_ID, GROUP_ID])
        self.assertEqual([c["alias"] for c in config["conversations"]], ["private_test", "group_test"])
        self.assertEqual([c["expected_sender_id"] for c in config["conversations"]], [PRIVATE_ID, PRIVATE_ID])
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.output.parent.stat().st_mode), 0o700)
        self.assertEqual(self.driver.body_reads, [])
        self.assert_private_stdout(stdout)

    def test_duplicate_visible_private_name_is_rejected(self):
        self.writer.execute("INSERT INTO contact VALUES (?,?,?,?)", ("wxid_synthetic_duplicate", PRIVATE_NAME, "", ""))
        self.writer.commit()
        result, stdout = self.run_resolver()
        self.assertEqual(result, 2)
        self.assertIn("contact_exact_name_ambiguous", stdout)
        self.assertFalse(self.output.exists())
        self.assert_private_stdout(stdout)

    def test_type_filter_disambiguates_same_private_and_group_name(self):
        common = "合成同名私聊群"
        self.writer.execute("UPDATE contact SET nick_name=? WHERE username IN (?,?)", (common, PRIVATE_ID, GROUP_ID))
        self.writer.commit()
        self.request.update(private_name=common, group_name=common)
        result, stdout = self.run_resolver()
        self.assertEqual(result, 0, stdout)
        self.assertEqual([c["chat_id"] for c in json.loads(self.output.read_text())["conversations"]], [PRIVATE_ID, GROUP_ID])

    def test_binary_equality_overrides_nocase_column_and_rejects_partial_names(self):
        self.writer.execute("UPDATE contact SET nick_name='SyntheticExactCase' WHERE username=?", (PRIVATE_ID,))
        self.writer.commit()
        self.request["private_name"] = "syntheticexactcase"
        result, stdout = self.run_resolver()
        self.assertEqual(result, 2)
        self.assertIn("contact_exact_name_not_found", stdout)
        self.assertFalse(self.output.exists())
        self.request["private_name"] = "SyntheticExact"
        result, stdout = self.run_resolver()
        self.assertEqual(result, 2)
        self.assertIn("contact_exact_name_not_found", stdout)
        self.assertFalse(self.output.exists())

    def test_exact_username_and_quoted_name_are_parameterized(self):
        self.request["private_name"] = PRIVATE_ID
        quoted = "合成群' OR 1=1 --"
        self.writer.execute("UPDATE contact SET nick_name=? WHERE username=?", (quoted, GROUP_ID))
        self.writer.commit()
        self.request["group_name"] = quoted
        result, stdout = self.run_resolver()
        self.assertEqual(result, 0, stdout)
        self.assertNotIn(quoted, stdout)

    def test_missing_schema_column_is_explicit_external_condition(self):
        self.writer.execute("ALTER TABLE contact RENAME COLUMN alias TO unsupported_alias")
        self.writer.commit()
        result, stdout = self.run_resolver()
        self.assertEqual(result, 2)
        self.assertIn("contact_metadata_schema_unsupported", stdout)
        self.assertIn("待外部条件", stdout)
        self.assertFalse(self.output.exists())

    def test_invalid_contact_key_does_not_fall_back_to_message_key(self):
        self.keys["keys"]["contact/contact.db"]["enc_key"] = "not-a-key"
        self.keys["keys"]["message/message_0.db"]["enc_key"] = SYNTHETIC_KEY
        result, stdout = self.run_resolver()
        self.assertEqual(result, 2)
        self.assertIn("contact_key_unavailable_or_invalid", stdout)
        self.assertFalse(self.output.exists())
        self.assert_private_stdout(stdout)

    def test_existing_output_is_not_overwritten(self):
        self.output.parent.mkdir()
        self.output.write_text("SYNTHETIC_EXISTING_CONFIGURATION")
        result, stdout = self.run_resolver()
        self.assertEqual(result, 2)
        self.assertIn("private_configuration_already_exists", stdout)
        self.assertEqual(self.output.read_text(), "SYNTHETIC_EXISTING_CONFIGURATION")

    def test_missing_tokens_and_native_failure_never_emit_input_content(self):
        del self.request["private_tokens"]
        result, stdout = self.run_resolver()
        self.assertEqual(result, 2)
        self.assertIn("private_request_unavailable_or_invalid", stdout)
        self.request["private_tokens"] = ["SYNTH_PRIVATE_%d" % i for i in range(3)]

        class FailingDriver:
            def connect(self, *args, **kwargs):
                raise RuntimeError("SYNTHETIC_NATIVE_SECRET " + SYNTHETIC_KEY + PRIVATE_NAME)

        result, stdout = self.run_resolver(driver=FailingDriver())
        self.assertEqual(result, 2)
        self.assertNotIn("SYNTHETIC_NATIVE_SECRET", stdout)
        self.assert_private_stdout(stdout)


if __name__ == "__main__":
    unittest.main()
