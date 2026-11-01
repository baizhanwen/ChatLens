"""Private, selected-scope display names and poll-driven Web status metadata."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

from collector_private import ensure_private_dir, read_private_json, validate_local_path, write_private_json_new
from windows_selected_engine import ALIASES, ID, _columns, _open
from conversation_scope import normalize_conversations
from wechat_identity import account_candidates, establish_identity, trusted_self_identity, unknown_identity


def account_context(root):
    root = Path(root)
    info = root.stat()
    return hashlib.sha256(json.dumps([str(root), info.st_dev, info.st_ino], separators=(',', ':')).encode()).hexdigest()


def selected_sender_metadata(driver, root, keys, chats):
    """Sender-ID metadata only in explicitly selected private tables.

    No message text or old message event is imported. The account owner can be
    corroborated even if the first newly-collected messages are all incoming.
    """
    result = {}
    deadline = time.monotonic() + 1.5
    shards = sorted(key for key in keys if re.fullmatch(r'message/message_[0-7]\.db', key))
    for relative in shards:
        path = Path(root) / relative
        epoch = source_identity(path)
        connection = _open(driver, path, keys[relative])
        try:
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute('BEGIN')
            if not (_columns(connection, 'Name2Id') or set()) >= {'user_name'}:
                raise ValueError('web_identity_schema_unavailable')
            for chat in chats:
                peer = chat['chat_id']
                if peer.endswith('@chatroom'):
                    continue
                table = 'Msg_' + hashlib.md5(peer.encode()).hexdigest()
                columns = _columns(connection, table)
                if columns is None:
                    continue
                if not columns >= {'real_sender_id', 'local_type'}:
                    raise ValueError('web_identity_schema_unavailable')
                if time.monotonic() > deadline:
                    raise ValueError('web_identity_query_deadline')
                rows = connection.execute('SELECT DISTINCT n.user_name FROM "' + table
                    + '" AS m LEFT JOIN Name2Id AS n ON n.rowid=m.real_sender_id '
                      'WHERE (m.local_type & 4294967295)=1 LIMIT 3').fetchall()
                observed = result.setdefault(chat['alias'], set())
                observed.update(row[0] for row in rows)
        finally:
            connection.rollback()
            connection.close()
        if source_identity(path) != epoch:
            raise ValueError('web_identity_source_changed')
    return {alias: list(values) for alias, values in result.items()}


def source_identity(path):
    validate_local_path(path)
    info = path.stat()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        salt = stream.read(16)
    after = path.stat()
    if ((info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)
            or (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino) or len(salt) != 16):
        raise ValueError('web_catalog_source_changed')
    return info.st_dev, info.st_ino, salt


class WebMetadata:
    def __init__(self, private_dir, binding_digest, started_at, *, clock=time.monotonic, identity_hint=None):
        self.private_dir = ensure_private_dir(Path(private_dir))
        self.binding_digest = binding_digest
        self.started_at = started_at
        self.clock = clock
        self.failed = False
        self.instance_id = secrets.token_hex(16)
        self._last_names_at = None
        self._last_ids = None
        self._contact_epoch = None
        self.identity_hint = identity_hint

    def write(self, name, value):
        if name not in ('catalog.json', 'web-status.json'):
            raise ValueError('web_metadata_filename_invalid')
        ensure_private_dir(self.private_dir)
        destination = self.private_dir / name
        if destination.exists() or destination.is_symlink():
            read_private_json(destination)  # Validate ownership before replacing.
        temporary = self.private_dir / ('.web-meta-' + secrets.token_hex(12) + '.json')
        write_private_json_new(temporary, value)
        try:
            # Windows readers hold short read-only handles without DELETE sharing.
            for attempt in range(6):
                try:
                    os.replace(temporary, destination)
                    break
                except PermissionError:
                    if attempt == 5:
                        raise
                    time.sleep(.02)
        finally:
            if temporary.exists():
                temporary.unlink()

    def ensure_catalog(self, config):
        """Publish the explicit scope even when optional name lookup fails."""
        chats = normalize_conversations(config)
        target = self.private_dir / 'catalog.json'
        if target.exists() or target.is_symlink():
            existing = read_private_json(target)
            old_ids = {row.get('id') for row in existing.get('conversations', []) if isinstance(row, dict)}
            expected = {row['alias'] for row in chats}
            legacy = expected == {'private_test', 'group_test'} and existing.get('binding_digest') is None
            if old_ids != expected or not (existing.get('binding_digest') == self.binding_digest or legacy):
                raise ValueError('web_catalog_binding_mismatch')
            return
        self.write('catalog.json', {'schema_version': 1, 'binding_digest': self.binding_digest,
            'conversations': [{'id': row['alias'], 'kind': 'group' if row['chat_id'].endswith('@chatroom') else 'private',
                               'name': '所选群聊' if row['chat_id'].endswith('@chatroom') else '所选私聊',
                               'sender_names': {}} for row in chats],
            'identity': unknown_identity(self.binding_digest), 'updated_at_utc': datetime.now(timezone.utc).isoformat()})

    def status(self, stage, total, added, resumed, reason=None):
        if stage not in ('starting', 'collecting', 'paused', 'stopping', 'stopped', 'error'):
            raise ValueError('web_metadata_stage_invalid')
        value = {'schema_version': 1, 'stage': stage,
                 'binding_digest': self.binding_digest, 'instance_id': self.instance_id,
                 'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                 'started_at_utc': self.started_at, 'total_messages': total,
                 'new_messages_this_run': added, 'resumed_from_checkpoint': bool(resumed)}
        if reason is not None:
            value['reason'] = reason
        self.write('web-status.json', value)

    def refresh_catalog(self, driver, root, keys, config, observed_senders):
        chats = normalize_conversations(config)
        by_alias = {chat['alias']: chat for chat in chats}
        aliases = tuple(by_alias)
        requested = {chat['chat_id'] for chat in chats}
        candidates = account_candidates(root)
        requested.update(candidates)
        for alias in aliases:
            requested.update(value for value in observed_senders.get(alias, [])
                             if isinstance(value, str) and ID.fullmatch(value))
        signature = (tuple(sorted(requested)), tuple((alias,
            tuple(sorted(value for value in observed_senders.get(alias, []) if isinstance(value, str))),
            any(not isinstance(value, str) for value in observed_senders.get(alias, []))) for alias in aliases))
        tick = self.clock()
        if self._last_ids == signature and self._last_names_at is not None and tick - self._last_names_at < 60:
            return
        names = {}
        contact_matches = set()
        path = Path(root) / 'contact/contact.db'
        epoch = source_identity(path)
        if self._contact_epoch is not None and epoch != self._contact_epoch:
            raise ValueError('web_catalog_source_changed')
        sidecars = {}
        for suffix in ('-wal', '-shm'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                validate_local_path(sidecar)
                sidecars[suffix] = sidecar.stat()
        if '-wal' in sidecars and sidecars['-wal'].st_size > 0 and '-shm' not in sidecars:
            raise ValueError('web_catalog_wal_shm_unavailable')
        connection = _open(driver, path, keys['contact/contact.db'])
        try:
            deadline = time.monotonic() + 1.5
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute('BEGIN')
            columns = _columns(connection, 'contact')
            if columns is None or not {'username', 'remark', 'nick_name'}.issubset(columns):
                raise ValueError('web_catalog_schema_unavailable')
            for username in sorted(requested):
                if time.monotonic() > deadline:
                    raise ValueError('web_catalog_query_deadline')
                rows = connection.execute(
                    'SELECT substr(remark,1,512),substr(nick_name,1,512) FROM contact WHERE username COLLATE BINARY=? LIMIT 2',
                    (username,)).fetchall()
                if username in candidates and len(rows) == 1:
                    contact_matches.add(username)
                values = set()
                for remark, nickname in rows:
                    value = next((x.strip() for x in (remark, nickname) if isinstance(x, str) and x.strip()), None)
                    if value:
                        value = ''.join(c for c in value if c >= ' ' and c != '\x7f')[:256]
                        if value:
                            values.add(value)
                if len(values) == 1:
                    names[username] = values.pop()
        finally:
            connection.rollback()
            connection.close()
        if source_identity(path) != epoch:
            raise ValueError('web_catalog_source_changed')
        conversations = []
        for alias in aliases:
            chat = by_alias[alias]
            visible_senders = set(observed_senders.get(alias, [])) | {chat['chat_id']}
            group = chat['chat_id'].endswith('@chatroom')
            conversations.append({'id': alias, 'kind': 'group' if group else 'private',
                                  'name': names.get(chat['chat_id'], '所选群聊' if group else '所选私聊'),
                                  'sender_names': {key: value for key, value in names.items() if key in visible_senders}})
        identity = establish_identity(self.binding_digest, candidates, contact_matches, chats, observed_senders)
        context = account_context(root)
        if identity.get('state') != 'verified' and identity.get('reason') != 'private_sender_conflict' and len(contact_matches) == 1:
            # Metadata-only fallback stays inside the selected source tables.
            try:
                source_senders = selected_sender_metadata(driver, root, keys, chats)
                identity = establish_identity(self.binding_digest, candidates, contact_matches, chats, source_senders)
                if identity.get('state') == 'verified':
                    identity['proof']['observation_source'] = 'selected_private_sender_metadata'
            except Exception:
                identity = unknown_identity(self.binding_digest, 'identity_source_metadata_unavailable')
        if identity.get('state') != 'verified' and identity.get('reason') != 'private_sender_conflict' and len(contact_matches) == 1:
            hints = [self.identity_hint]
            previous = self.private_dir / 'catalog.json'
            if previous.exists():
                try:
                    hints.append({'catalog': read_private_json(previous), 'binding_digest': self.binding_digest,
                                  'account_context': context})
                except Exception:
                    pass
            for hint in hints:
                if not isinstance(hint, dict) or hint.get('account_context') != context:
                    continue
                catalog = hint.get('catalog')
                old = catalog.get('identity', {}) if isinstance(catalog, dict) else {}
                sender = trusted_self_identity(catalog, hint.get('binding_digest'))
                if sender not in contact_matches or old.get('account_context') != context:
                    continue
                identity = dict(old, binding_digest=self.binding_digest, verified_at=datetime.now(timezone.utc).isoformat())
                identity['proof'] = dict(old['proof'], inherited_from_scope=True,
                                         origin_binding_digest=hint['binding_digest'])
                break
        if identity.get('state') == 'verified':
            identity['account_context'] = context
        self.write('catalog.json', {'schema_version': 1, 'binding_digest': self.binding_digest,
                                   'conversations': conversations,
                                   'identity': identity,
                                   'updated_at_utc': datetime.now(timezone.utc).isoformat()})
        self._last_ids, self._last_names_at = signature, tick
        self._contact_epoch = epoch
