"""Private, selected-scope display names and poll-driven Web status metadata."""
from datetime import datetime, timezone
import os
from pathlib import Path
import secrets
import time

from collector_private import ensure_private_dir, read_private_json, validate_local_path, write_private_json_new
from windows_selected_engine import ALIASES, ID, _columns, _open


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
    def __init__(self, private_dir, binding_digest, started_at, *, clock=time.monotonic):
        self.private_dir = ensure_private_dir(Path(private_dir))
        self.binding_digest = binding_digest
        self.started_at = started_at
        self.clock = clock
        self.failed = False
        self.instance_id = secrets.token_hex(16)
        self._last_names_at = None
        self._last_ids = None
        self._contact_epoch = None

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
        chats = config['conversations']
        by_alias = {chat['alias']: chat for chat in chats}
        if set(by_alias) != set(ALIASES):
            raise ValueError('web_catalog_scope_invalid')
        requested = {chat['chat_id'] for chat in chats}
        for alias in ALIASES:
            requested.update(value for value in observed_senders.get(alias, [])
                             if isinstance(value, str) and ID.fullmatch(value))
        signature = tuple(sorted(requested))
        tick = self.clock()
        if self._last_ids == signature and self._last_names_at is not None and tick - self._last_names_at < 60:
            return
        names = {}
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
        for alias in ALIASES:
            chat = by_alias[alias]
            visible_senders = set(observed_senders.get(alias, [])) | {chat['chat_id']}
            conversations.append({'id': alias, 'kind': 'group' if alias == 'group_test' else 'private',
                                  'name': names.get(chat['chat_id'], '所选群聊' if alias == 'group_test' else '所选私聊'),
                                  'sender_names': {key: value for key, value in names.items() if key in visible_senders}})
        self.write('catalog.json', {'schema_version': 1, 'binding_digest': self.binding_digest,
                                   'conversations': conversations,
                                   'updated_at_utc': datetime.now(timezone.utc).isoformat()})
        self._last_ids, self._last_names_at = signature, tick
        self._contact_epoch = epoch
