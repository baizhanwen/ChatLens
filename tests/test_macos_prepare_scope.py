"""Synthetic page-one selection, without launching or signing an app."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import prepare_wechat_read_test as preparation


class MacPreparationScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-prepare-scope-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / 'synthetic-account'
        for relative in ('message/message_0.db', 'contact/contact.db', 'message/message_fts.db'):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'x' * 4096)

    def test_combined_explicit_scope_reads_only_selected_accounts_numeric_messages_and_contact(self):
        with patch.object(preparation, 'only_database_root', side_effect=AssertionError('never automatically choose any account')):
            rows = preparation.selected_database_pages('message_and_contact', self.root)
        self.assertEqual([row[0] for row in rows], ['message/message_0.db', 'contact/contact.db'])
        self.assertTrue(all(len(row[1]) == 4096 for row in rows))

    def test_redirected_database_is_rejected(self):
        target = self.root / 'message/message_0.db'
        target.unlink()
        target.symlink_to(self.root / 'contact/contact.db')
        with self.assertRaisesRegex(preparation.SafeTestError, 'path_invalid'):
            preparation.selected_database_pages('message_and_contact', self.root)

    def test_combined_scope_cannot_fall_back_to_global_account_selection(self):
        with patch.object(preparation, 'only_database_root', return_value=self.root):
            with self.assertRaisesRegex(preparation.SafeTestError, 'scope_not_allowlisted'):
                preparation.selected_database_pages('message_and_contact')


if __name__ == '__main__':
    unittest.main()
