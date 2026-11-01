"""Canonical multi-conversation selection; no real account IDs or databases."""
import hashlib
from pathlib import Path
import sys
import unittest

MODULES = Path(__file__).resolve().parents[1] / 'scripts/windows-proof'
sys.path.insert(0, str(MODULES))
from conversation_scope import (MAX_CONVERSATIONS, ScopeError, alias_for, aliases_from_binding,
                                kind_for, normalize_conversations, valid_alias)


def selected(chat, alias=None, **extra):
    return {'alias': alias or alias_for(chat), 'chat_id': chat, **extra}


class ConversationScopeTests(unittest.TestCase):
    def test_stable_opaque_aliases_and_kind_do_not_include_source_identity(self):
        for chat in ('synthetic_person_a', 'synthetic_group_a@chatroom'):
            alias = alias_for(chat)
            self.assertEqual(alias, 'chat_' + hashlib.sha256(chat.encode()).hexdigest()[:20])
            self.assertTrue(valid_alias(alias))
            self.assertNotIn(chat, alias)
        self.assertEqual(kind_for('synthetic_person_a'), 'private')
        self.assertEqual(kind_for('synthetic_group_a@chatroom'), 'group')

    def test_legacy_canonical_rows_keep_order_and_three_field_shape(self):
        peer, group = 'synthetic_peer', 'synthetic_group@chatroom'
        result = normalize_conversations({'conversations': [selected(group, 'group_test'), selected(peer, 'private_test')]})
        self.assertEqual([row['alias'] for row in result], ['private_test', 'group_test'])
        self.assertTrue(all(set(row) == {'alias', 'chat_id', 'table'} for row in result))
        self.assertEqual(result[0]['table'], 'Msg_' + hashlib.md5(peer.encode()).hexdigest())

    def test_multiple_private_and_multiple_group_selections_are_order_independent(self):
        chats = ['synthetic_peer_a', 'synthetic_peer_b', 'synthetic_group_a@chatroom', 'synthetic_group_b@chatroom']
        result = normalize_conversations({'conversations': [selected(chat) for chat in chats]})
        self.assertEqual(result, normalize_conversations({'conversations': [selected(chat) for chat in reversed(chats)]}))
        self.assertEqual(len(result), 4)
        self.assertEqual(len(normalize_conversations({'conversations': [selected(chat) for chat in chats[:2]]})), 2)
        self.assertEqual(len(normalize_conversations({'conversations': [selected(chat) for chat in chats[2:]]})), 2)

    def test_selection_limit_and_duplicate_source_ids_are_refused(self):
        maximum = [selected('synthetic_peer_%d' % index) for index in range(MAX_CONVERSATIONS)]
        self.assertEqual(len(normalize_conversations({'conversations': maximum})), MAX_CONVERSATIONS)
        for rows in ([], maximum + [selected('synthetic_one_too_many')], [maximum[0], maximum[0]]):
            with self.subTest(count=len(rows)), self.assertRaises(ScopeError):
                normalize_conversations({'conversations': rows})

    def test_bad_alias_kind_table_and_source_path_are_never_normalized_into_valid_scope(self):
        cases = [selected('synthetic_peer', 'chat_' + '0' * 20), selected('synthetic_peer', 'not_a_scope'),
                 selected('synthetic_peer', 'group_test'), selected('synthetic_group@chatroom', 'private_test'),
                 selected('synthetic_peer', kind='group'), selected('synthetic_peer', table='Msg_arbitrary'),
                 {'alias': 'private_test', 'chat_id': '../unselected'}]
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ScopeError):
                normalize_conversations({'conversations': [row]})

    def test_explicit_binding_config_and_scope_are_strict_but_old_generic_binding_is_distinct(self):
        one, two = selected('synthetic_a'), selected('synthetic_b')
        self.assertEqual(set(aliases_from_binding({'config': {'conversations': [one, two]}})), {one['alias'], two['alias']})
        self.assertEqual(aliases_from_binding({'scope': [one['alias']]}), (one['alias'],))
        self.assertIsNone(aliases_from_binding({'synthetic_old_generic': True}))
        self.assertIsNone(aliases_from_binding({'scope': 'explicit_synthetic_two_conversations'}))
        for binding in ({'config': None}, {'config': {}, 'scope': ['private_test']},
                        {'scope': ['not_a_scope']}, {'scope': []}, {'scope': ['private_test', 'private_test']}):
            with self.subTest(binding=binding), self.assertRaises(ScopeError):
                aliases_from_binding(binding)


if __name__ == '__main__':
    unittest.main()
