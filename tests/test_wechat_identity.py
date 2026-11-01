"""Synthetic account and selected sender metadata; no real user data."""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts/windows-proof'))
import collector_web_meta as metadata
from conversation_scope import alias_for
from wechat_identity import account_candidates, establish_identity, message_direction, trusted_self_identity
from collector_private import read_private_json

SELF, PEER, OTHER = 'synthetic_self', 'synthetic_peer', 'synthetic_other'
GROUP = 'synthetic_room@chatroom'
BINDING = 'e' * 64
CHATS = [{'alias': 'private_test', 'chat_id': PEER}, {'alias': 'group_test', 'chat_id': GROUP}]


class IdentityProofTests(unittest.TestCase):
    def identity(self, observed=None):
        return establish_identity(BINDING, [SELF], [SELF], CHATS,
                                  observed or {'private_test': [PEER, SELF]})

    def source(self, identity=None):
        return {'binding_digest': BINDING, 'identity': identity or self.identity()}

    def test_only_independent_private_sender_and_account_contact_proof_verifies(self):
        identity = self.identity()
        self.assertEqual(trusted_self_identity(self.source(identity), BINDING), SELF)
        self.assertEqual(message_direction(self.source(), BINDING, SELF), 'outgoing')
        self.assertEqual(message_direction(self.source(), BINDING, PEER), 'incoming')
        self.assertEqual(message_direction(self.source(), BINDING, None), 'unknown')

    def test_candidate_directory_or_contact_alone_are_not_verification(self):
        for observed in ({'private_test': [PEER]}, {'group_test': [SELF]}, {'private_test': [SELF, PEER, None]},
                         {'private_test': [SELF, PEER, OTHER]}):
            with self.subTest(observed=observed):
                self.assertEqual(self.identity(observed)['state'], 'unknown')
        self.assertEqual(establish_identity(BINDING, [SELF], [], CHATS, {'private_test': [SELF]})['state'], 'unknown')

    def test_system_peer_or_self_chat_do_not_supply_private_two_party_proof(self):
        for peer in ('filehelper', SELF, 'gh_synthetic_service', GROUP):
            identity = establish_identity(BINDING, [SELF], [SELF], [{'alias': 'private_test', 'chat_id': peer}],
                                          {'private_test': [SELF]})
            self.assertEqual(identity['state'], 'unknown')

    def test_binding_and_all_proof_fields_are_required_naked_ids_are_untrusted(self):
        original = self.source()
        cases = [{'binding_digest': BINDING, 'self_sender_id': SELF},
                 {**original, 'binding_digest': 'a' * 64},
                 {**original, 'identity': {**original['identity'], 'binding_digest': 'a' * 64}}]
        for flag in ('private_non_peer_unique', 'account_candidate_matched', 'contact_candidate_matched'):
            value = copy.deepcopy(original); value['identity']['proof'][flag] = False; cases.append(value)
        for value in cases:
            self.assertIsNone(trusted_self_identity(value, BINDING))
            self.assertEqual(message_direction(value, BINDING, PEER), 'unknown')

    def test_directory_suffix_is_strict_candidate_only(self):
        self.assertEqual(account_candidates('/tmp/synthetic_self_ab12/db_storage'), (SELF, SELF + '_ab12'))
        self.assertEqual(account_candidates('/tmp/synthetic_self_zzzz/db_storage'), (SELF + '_zzzz',))
        self.assertEqual(account_candidates('/tmp/other'), ())


class IdentityMetadataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-owner-proof-')
        self.directory = Path(self.tmp.name).resolve()
        self.root = self.directory / (SELF + '_ab12') / 'db_storage'
        (self.root / 'contact').mkdir(parents=True); (self.root / 'message').mkdir()
        self.private = self.directory / 'workspace/private'; self.private.mkdir(parents=True, mode=0o700)
        self.contact = self.root / 'contact/contact.db'
        c = sqlite3.connect(self.contact)
        c.execute('CREATE TABLE contact(username TEXT,remark TEXT,nick_name TEXT)')
        c.executemany('INSERT INTO contact VALUES(?,?,?)', [(SELF, '', '合成本人'), (PEER, '我', ''), (GROUP, '', '合成群')])
        c.commit(); c.close()
        self.message = self.root / 'message/message_0.db'
        self.table = 'Msg_' + hashlib.md5(PEER.encode()).hexdigest()
        c = sqlite3.connect(self.message)
        c.execute('CREATE TABLE Name2Id(user_name TEXT)')
        c.executemany('INSERT INTO Name2Id(rowid,user_name) VALUES(?,?)', [(3, PEER), (27, SELF), (88, OTHER)])
        c.execute('CREATE TABLE "' + self.table + '"(real_sender_id INTEGER,local_type INTEGER,message_content TEXT)')
        c.executemany('INSERT INTO "' + self.table + '" VALUES(?,?,?)', [(3, 1, 'SYNTHETIC_BODY_NEVER_READ'), (27, 1, 'SYNTHETIC_OLD_BODY_NEVER_IMPORTED')])
        c.commit(); c.close()
        self.keys = {'contact/contact.db': '01' * 32, 'message/message_0.db': '02' * 32}
        self.trace = []

    def tearDown(self):
        self.tmp.cleanup()

    def open(self, driver, path, key):
        self.assertIn(Path(path), (self.contact, self.message))
        c = sqlite3.connect(Path(path).as_uri() + '?mode=ro', uri=True)
        c.execute('PRAGMA query_only=ON'); c.set_trace_callback(self.trace.append)
        return c

    def refresh(self, chats=None, observed=None, hint=None, binding=BINDING, target=None):
        web = metadata.WebMetadata(target or self.private, binding, '2026-01-01T00:00:00+00:00', identity_hint=hint)
        with patch.object(metadata, '_open', side_effect=self.open):
            web.refresh_catalog(object(), self.root, self.keys, {'conversations': chats or CHATS},
                                observed or {'private_test': [PEER], 'group_test': []})
        return read_private_json((target or self.private) / 'catalog.json')

    def test_selected_source_sender_metadata_verifies_without_reading_or_importing_old_text(self):
        before = [self.contact.read_bytes(), self.message.read_bytes()]
        result = self.refresh()
        self.assertEqual(trusted_self_identity(result, BINDING), SELF)
        self.assertEqual(result['identity']['proof']['observation_source'], 'selected_private_sender_metadata')
        self.assertNotIn('message_content', '\n'.join(self.trace))
        self.assertNotIn('SYNTHETIC_BODY_NEVER_READ', json.dumps(result))
        self.assertEqual(before, [self.contact.read_bytes(), self.message.read_bytes()])
        self.assertEqual((self.private / 'catalog.json').stat().st_mode & 0o777, 0o600)

    def test_conflicting_private_sender_metadata_does_not_verify(self):
        c = sqlite3.connect(self.message); c.execute('INSERT INTO "' + self.table + '" VALUES(88,1,?)', ('SYNTHETIC_UNREAD',)); c.commit(); c.close()
        self.assertEqual(self.refresh()['identity']['state'], 'unknown')

    def test_verified_identity_can_transfer_to_same_account_group_only_scope_without_messages_or_history(self):
        previous = self.refresh()
        group = {'alias': alias_for(GROUP), 'chat_id': GROUP}
        target = self.directory / 'second-profile/private'; target.mkdir(parents=True, mode=0o700)
        hint = {'catalog': previous, 'binding_digest': BINDING, 'account_context': metadata.account_context(self.root)}
        current = self.refresh(chats=[group], observed={group['alias']: []}, hint=hint, binding='f' * 64, target=target)
        self.assertEqual(trusted_self_identity(current, 'f' * 64), SELF)
        self.assertTrue(current['identity']['proof']['inherited_from_scope'])
        self.assertEqual([p.name for p in target.iterdir()], ['catalog.json'])

    def test_cross_account_hint_and_unbound_metadata_do_not_verify_group_only_scope(self):
        previous = self.refresh();group = {'alias': alias_for(GROUP), 'chat_id': GROUP}
        hint = {'catalog': previous, 'binding_digest': BINDING, 'account_context': 'a' * 64}
        target = self.directory / 'other-profile/private'; target.mkdir(parents=True, mode=0o700)
        result = self.refresh(chats=[group], observed={group['alias']: []}, hint=hint, binding='f' * 64, target=target)
        self.assertEqual(result['identity']['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
