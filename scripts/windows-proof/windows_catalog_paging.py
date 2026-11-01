"""Page already verified metadata with opaque, query- and domain-bound cursors.

No source database is opened by this module. A cursor is a per-process HMAC over
an offset and fixed-size digests; it contains no account/contact IDs or names.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets

from web_data import WebDataError

PAGE_SIZE = 200
CURSOR = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).digest()[:16]


def _invalid():
    raise WebDataError('windows_scope_cursor_invalid', 409)


def page_conversations(manager, query='', cursor=''):
    """Called under the manager lock, after the expected-generation guard."""
    if (not isinstance(query, str) or len(query) > 256 or '\0' in query
            or len(query.encode('utf-8')) > 1024):
        raise ValueError('windows_scope_query_invalid')
    if not isinstance(cursor, str) or cursor and CURSOR.fullmatch(cursor) is None:
        _invalid()
    status = manager._catalog_status()
    if not manager.catalog_ready:
        return {'conversations': [], 'selected_conversations': [], 'has_more': False,
                'next_cursor': None, 'catalog': status}
    rows = [{key: row[key] for key in ('id', 'name', 'type', 'selected')} for row in manager.contacts]
    selected = [row for row in rows if row['selected']]
    if len(selected) > 50:
        raise WebDataError('windows_scope_selection_invalid', 409)
    # The new first-use entry searches first. Existing callers may retain their
    # old empty-query page until they migrate; both paths seed only explicit
    # already-selected items, never every matching result.
    if not query and getattr(manager, 'search_first', False):
        if cursor:
            _invalid()
        return {'conversations': [], 'selected_conversations': selected, 'has_more': False,
                'next_cursor': None, 'catalog': status, 'search_required': True}
    rows.sort(key=lambda row: (not row['selected'], row['type'], row['name'].casefold(), row['id']))
    # Replacing a catalog, changing selected marks, or changing names invalidates
    # its old cursor even if this happens within the same scope generation.
    revision = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')).digest()[:16]
    binding = _digest(manager.scope_generation) + _digest(query) + revision
    secret = getattr(manager, '_catalog_cursor_secret', None)
    if secret is None:
        secret = manager._catalog_cursor_secret = secrets.token_bytes(32)
    filtered = [row for row in rows if query.casefold() in row['name'].casefold()]
    offset = 0
    if cursor:
        try:
            decoded = base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4))
        except (ValueError, TypeError):
            _invalid()
        if len(decoded) != 68:
            _invalid()
        payload, signature = decoded[:-16], decoded[-16:]
        if (not hmac.compare_digest(signature, hmac.new(secret, payload, hashlib.sha256).digest()[:16])
                or not hmac.compare_digest(payload[4:], binding)):
            _invalid()
        offset = int.from_bytes(payload[:4], 'big')
        if offset > len(filtered):
            _invalid()
    end = min(offset + PAGE_SIZE, len(filtered))
    more = end < len(filtered)
    next_cursor = None
    if more:
        payload = end.to_bytes(4, 'big') + binding
        signature = hmac.new(secret, payload, hashlib.sha256).digest()[:16]
        next_cursor = base64.urlsafe_b64encode(payload + signature).decode('ascii').rstrip('=')
    selected = [] if cursor else selected
    return {'conversations': filtered[offset:end], 'selected_conversations': selected,
            'has_more': more, 'next_cursor': next_cursor, 'catalog': status}
