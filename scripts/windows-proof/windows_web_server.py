"""Windows multi-scope routes using the shared authenticated loopback contract."""
from __future__ import annotations

import json
from pathlib import Path
import re
import socket
from urllib.parse import parse_qs, urlsplit

from collector_private import validate_local_path
from web_server import make_server
from web_data import WebDataError


WINDOWS_STATIC = {'/connect.html': ('connect.html', 'text/html; charset=utf-8'),
              '/connect.js': ('connect.js', 'text/javascript; charset=utf-8'),
              '/connect.css': ('connect.css', 'text/css; charset=utf-8')}
MUTATIONS = {'/api/setup/initialize', '/api/setup/select', '/api/setup/catalog', '/api/setup/profile'}


def _pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError


def make_windows_server(manager, static_dir, *, port=0, token=None):
    """Return the base server with additional fixed, authenticated Windows routes."""
    server = make_server(manager, static_dir, port=port, token=token)
    parent_handler = server.RequestHandlerClass

    class WindowsHandler(parent_handler):
        def _target(self):
            if not self._request_allowed():
                self._json(403, {'error': {'code': 'web_origin_or_host_rejected'}})
                return None
            if len(self.path) > 4096:
                self._json(414, {'error': {'code': 'web_request_too_large'}})
                return None
            try:
                parsed = urlsplit(self.path)
                if parsed.scheme or parsed.netloc or not parsed.path.startswith('/'):
                    raise ValueError
                return parsed
            except ValueError:
                self._json(400, {'error': {'code': 'web_request_target_invalid'}})
                return None

        def _session(self):
            if not self._cookie_authenticated():
                self._json(401, {'error': {'code': 'web_session_required'}})
                return False
            return True

        def _operation(self, action):
            try:
                result = action()
                self._json(200, result if isinstance(result, dict) else {'accepted': True})
            except WebDataError as error:
                self._json(error.status, {'error': {'code': 'scope_changed' if error.code == 'scope_changed'
                                                   else 'windows_setup_operation_failed'}})
            except Exception:
                # Native errors, account paths, IDs, provider output and keys
                # are manager-private. Even an exception's .code is untrusted.
                self._json(503, {'error': {'code': 'windows_setup_operation_failed'}})

        def _scope_guard(self):
            context = getattr(self.server.data, 'public_scope_context', None)
            if not callable(context):
                return True
            values = self.headers.get_all('X-ChatLens-Scope', [])
            if len(values) != 1 or values[0] != context()['scope_generation']:
                self._json(409, {'error': {'code': 'scope_changed'}})
                return False
            return True

        def _generation(self):
            return self.headers.get('X-ChatLens-Scope') if callable(getattr(self.server.data, 'public_scope_context', None)) else None

        def do_GET(self):
            parsed = self._target()
            if parsed is None:
                return
            if parsed.path == '/api/session' and callable(getattr(self.server.data, 'public_scope_context', None)):
                if parsed.query:
                    self._json(400, {'error': {'code': 'windows_setup_query_invalid'}})
                    return
                if not self._cookie_authenticated() and not self._bootstrap_authenticated():
                    self._json(401, {'error': {'code': 'web_session_required'}})
                    return
                self._json(200, {'mode': 'live', 'capabilities': {'read_messages': True, 'control_collection': False},
                                 'poll_interval_ms': 1000, 'platform': 'windows', **self.server.data.public_scope_context()}, cookie=True)
                return
            if parsed.path.startswith('/api/') and parsed.path not in ('/api/setup', '/api/session'):
                if not self._session() or not self._scope_guard():
                    return
            if parsed.path in WINDOWS_STATIC:
                name, content_type = WINDOWS_STATIC[parsed.path]
                try:
                    path = validate_local_path(self.server.static_dir / name)
                    if path.stat().st_size > 1024 * 1024:
                        raise ValueError
                    with path.open('rb') as source:
                        raw = source.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ValueError
                except Exception:
                    self._json(404, {'error': {'code': 'web_static_file_unavailable'}})
                    return
                self._response(200, raw, content_type)
                return
            if parsed.path not in ('/api/setup', '/api/setup/conversations'):
                return super().do_GET()
            if not self._session():
                return
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=2)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError
                if parsed.path == '/api/setup':
                    if query:
                        raise ValueError
                    return self._operation(self.server.data.setup)
                if set(query) - {'q'}:
                    raise ValueError
                q = query.get('q', [''])[0]
                if len(q) > 256 or '\0' in q or len(q.encode('utf-8')) > 1024:
                    raise ValueError
            except (ValueError, UnicodeError):
                self._json(400, {'error': {'code': 'windows_setup_query_invalid'}})
                return
            self._operation(lambda: self.server.data.list_conversations(q, expected_generation=self._generation())
                            if self._generation() else self.server.data.list_conversations(q))

        def do_POST(self):
            parsed = self._target()
            if parsed is None:
                return
            if parsed.path.startswith('/api/'):
                if not self._session() or not self._scope_guard():
                    return
            if parsed.path not in MUTATIONS:
                return self._unsupported()
            if not self._session():
                return
            # Browsers supply Origin on same-origin JSON mutations. Require
            # it explicitly; the top-document exception only applies to GET.
            if self.headers.get_all('Origin', []) != [self.server.base_url]:
                self._json(403, {'error': {'code': 'web_origin_or_host_rejected'}})
                return
            try:
                if parsed.query or self.headers.get_all('Transfer-Encoding', []):
                    raise ValueError
                types = self.headers.get_all('Content-Type', [])
                lengths = self.headers.get_all('Content-Length', [])
                if (len(types) != 1 or re.fullmatch(r'application/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?',
                                                  types[0], flags=re.IGNORECASE) is None
                        or len(lengths) != 1 or re.fullmatch(r'[0-9]{1,5}', lengths[0]) is None):
                    raise ValueError
                length = int(lengths[0])
                if not 2 <= length <= 32768:
                    raise ValueError
                self.connection.settimeout(3)
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ValueError
                value = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_bad_constant)
                if not isinstance(value, dict):
                    raise ValueError
                supplied_generation = value.pop('scope_generation', None)
                if supplied_generation is not None and supplied_generation != self._generation():
                    self._json(409, {'error': {'code': 'scope_changed'}})
                    return
                if parsed.path in ('/api/setup/initialize', '/api/setup/catalog'):
                    if value:
                        raise ValueError
                elif parsed.path == '/api/setup/profile':
                    if set(value) != {'profile_id'} or not isinstance(value['profile_id'], str) or not re.fullmatch(r'legacy|[0-9a-f]{64}', value['profile_id']):
                        raise ValueError
                elif set(value) == {'conversation_ids'}:
                    identifiers = value['conversation_ids']
                    if (not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 50
                            or any(not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                                   or '\0' in identifier for identifier in identifiers)
                            or len(set(identifiers)) != len(identifiers)):
                        raise ValueError
                else:
                    if set(value) != {'private_id', 'group_id'}:
                        raise ValueError
                    if any(not isinstance(identifier, str) or not 1 <= len(identifier) <= 256
                           or '\0' in identifier or len(identifier.encode('utf-8')) > 1024 for identifier in value.values()):
                        raise ValueError
            except (ValueError, UnicodeError, RecursionError, OSError, socket.timeout):
                self._json(400, {'error': {'code': 'windows_setup_body_invalid'}})
                return
            if parsed.path == '/api/setup/initialize':
                self._operation(lambda: self.server.data.request_initialize(expected_generation=self._generation())
                                if self._generation() else self.server.data.request_initialize())
            elif parsed.path == '/api/setup/catalog':
                self._operation(lambda: self.server.data.request_catalog(expected_generation=self._generation()))
            elif parsed.path == '/api/setup/profile':
                self._operation(lambda: self.server.data.restore_profile(value['profile_id'], expected_generation=self._generation()))
            elif 'conversation_ids' in value:
                self._operation(lambda: self.server.data.select_many(value['conversation_ids'], expected_generation=self._generation()))
            else:
                self._operation(lambda: self.server.data.select(value['private_id'], value['group_id'], expected_generation=self._generation())
                                if self._generation() else self.server.data.select(value['private_id'], value['group_id']))

    server.RequestHandlerClass = WindowsHandler
    server.launch_url = server.base_url + '/#token=' + server.token
    return server
