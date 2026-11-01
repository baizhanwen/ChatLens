"""Bounded canonical conversation scopes shared by collection and Web readers.

New aliases are opaque hashes of the selected source chat ID. Existing two-chat
aliases remain valid so their private checkpoints need no migration or rewrite.
"""
from __future__ import annotations

import hashlib
import re

MAX_CONVERSATIONS = 50
LEGACY_ALIASES = ('private_test', 'group_test')
CHAT_ID = re.compile(r'[A-Za-z0-9_@.-]{1,256}\Z')
OPAQUE_ALIAS = re.compile(r'chat_[0-9a-f]{20}\Z')


class ScopeError(ValueError):
    def __init__(self, code='conversation_scope_invalid'):
        self.code = code
        super().__init__(code)


def valid_alias(value):
    return isinstance(value, str) and (value in LEGACY_ALIASES or OPAQUE_ALIAS.fullmatch(value) is not None)


def alias_for(chat_id):
    if not isinstance(chat_id, str) or CHAT_ID.fullmatch(chat_id) is None:
        raise ScopeError()
    return 'chat_' + hashlib.sha256(chat_id.encode('utf-8')).hexdigest()[:20]


def kind_for(chat_id):
    if not isinstance(chat_id, str) or CHAT_ID.fullmatch(chat_id) is None:
        raise ScopeError()
    return 'group' if chat_id.endswith('@chatroom') else 'private'


def normalize_conversations(config):
    """Return canonical three-field rows; preserve legacy binding serialization."""
    supplied = config.get('conversations') if isinstance(config, dict) else None
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= MAX_CONVERSATIONS:
        raise ScopeError()
    result, aliases, chats = [], set(), set()
    for row in supplied:
        if not isinstance(row, dict):
            raise ScopeError()
        alias, chat_id = row.get('alias'), row.get('chat_id')
        if (not valid_alias(alias) or not isinstance(chat_id, str) or CHAT_ID.fullmatch(chat_id) is None
                or alias in aliases or chat_id in chats):
            raise ScopeError()
        kind = kind_for(chat_id)
        if (alias in LEGACY_ALIASES and (alias == 'group_test') != (kind == 'group')
                or alias not in LEGACY_ALIASES and alias != alias_for(chat_id)
                or row.get('kind', kind) != kind):
            raise ScopeError()
        table = 'Msg_' + hashlib.md5(chat_id.encode('utf-8')).hexdigest()
        if row.get('table', table) != table:
            raise ScopeError()
        result.append({'alias': alias, 'chat_id': chat_id, 'table': table})
        aliases.add(alias); chats.add(chat_id)
    # The two original aliases keep their historical private/group order.
    priority = {alias: index for index, alias in enumerate(LEGACY_ALIASES)}
    return sorted(result, key=lambda row: (priority.get(row['alias'], len(priority)), row['alias']))


def aliases_from_binding(binding):
    """Explicit scopes are strict; None identifies a pre-scope generic store.

    Production collection always binds config.conversations. The None fallback
    only preserves old generic-store consumers that supplied no usable scope;
    malformed explicit config/scope must never silently enter that fallback.
    """
    if not isinstance(binding, dict):
        raise ScopeError()
    if 'config' in binding:
        return tuple(row['alias'] for row in normalize_conversations(binding['config']))
    if 'conversations' in binding:
        return tuple(row['alias'] for row in normalize_conversations(binding))
    if 'scope' in binding:
        scope = binding['scope']
        if isinstance(scope, str):
            # Historical generic-store verification used a descriptive label,
            # not executable aliases. Production bindings always carry config.
            return None
        if (not isinstance(scope, (list, tuple)) or not 1 <= len(scope) <= MAX_CONVERSATIONS
                or any(not valid_alias(alias) for alias in scope) or len(set(scope)) != len(scope)):
            raise ScopeError()
        return tuple(sorted(scope))
    return None
