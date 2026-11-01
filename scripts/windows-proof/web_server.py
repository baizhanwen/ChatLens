"""Authenticated loopback-only Web reader for an existing collector store."""
from __future__ import annotations

import argparse
import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
from urllib.parse import parse_qs, urlsplit
import webbrowser

from collector_private import validate_local_path
from web_data import CollectorWebData, WebDataError


COOKIE = 'chatlens_session'
STATIC = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/index.html': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
          '/assets/chatlens-mark.svg': ('assets/chatlens-mark.svg', 'image/svg+xml')}
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"


class LocalWebServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # A disconnected/malformed local request must never print a traceback
        # containing a request target, cookie or private exception detail.
        pass


def make_server(data, static_dir, *, port=0, token=None):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError('web_port_invalid')
    static_dir = validate_local_path(Path(static_dir), require_dir=True)
    token = token or secrets.token_urlsafe(32)
    if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token) is None:
        raise ValueError('web_session_token_invalid')

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        server_version = 'ChatLensLocal'
        sys_version = ''

        def log_message(self, *_):
            pass

        def log_error(self, *_):
            pass

        def send_error(self, code, message=None, explain=None):
            self._json(code if isinstance(code, int) else 400, {'error': {'code': 'web_request_rejected'}})

        def _response(self, code, body, content_type, *, cookie=False, head=False):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', CSP)
            self.send_header('Connection', 'close')
            if cookie:
                self.send_header('Set-Cookie', COOKIE + '=' + self.server.token + '; Path=/; HttpOnly; SameSite=Strict')
            self.end_headers()
            self.close_connection = True
            if not head:
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def _json(self, code, obj, *, cookie=False):
            raw = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
            self._response(code, raw, 'application/json; charset=utf-8', cookie=cookie)

        def _request_allowed(self):
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or hosts[0] != self.server.expected_host:
                return False
            origins = self.headers.get_all('Origin', [])
            if len(origins) > 1 or origins and origins[0] != self.server.base_url:
                return False
            fetch_site = self.headers.get('Sec-Fetch-Site')
            if fetch_site not in (None, 'none', 'same-origin'):
                return False
            return True

        def _cookie_authenticated(self):
            values = self.headers.get_all('Cookie', [])
            if len(values) != 1:
                return False
            try:
                cookies = SimpleCookie()
                cookies.load(values[0])
                value = cookies.get(COOKIE)
                return value is not None and hmac.compare_digest(value.value, self.server.token)
            except Exception:
                return False

        def _bootstrap_authenticated(self):
            values = self.headers.get_all('Authorization', [])
            try:
                return len(values) == 1 and hmac.compare_digest(values[0], 'Bearer ' + self.server.token)
            except (TypeError, UnicodeError):
                return False

        def do_GET(self):
            if not self._request_allowed():
                self._json(403, {'error': {'code': 'web_origin_or_host_rejected'}})
                return
            if len(self.path) > 4096:
                self._json(414, {'error': {'code': 'web_request_too_large'}})
                return
            try:
                parsed = urlsplit(self.path)
            except ValueError:
                self._json(400, {'error': {'code': 'web_request_target_invalid'}})
                return
            if parsed.scheme or parsed.netloc or not parsed.path.startswith('/'):
                self._json(400, {'error': {'code': 'web_request_target_invalid'}})
                return
            if parsed.path in STATIC:
                name, content_type = STATIC[parsed.path]
                try:
                    path = validate_local_path(self.server.static_dir / name)
                    if path.stat().st_size > 1024 * 1024:
                        raise ValueError
                    with path.open('rb') as stream:
                        raw = stream.read(1024 * 1024 + 1)
                    if len(raw) > 1024 * 1024:
                        raise ValueError
                except Exception:
                    self._json(404, {'error': {'code': 'web_static_file_unavailable'}})
                    return
                self._response(200, raw, content_type)
                return
            if parsed.path == '/api/session':
                if not self._cookie_authenticated() and not self._bootstrap_authenticated():
                    self._json(401, {'error': {'code': 'web_session_required'}})
                    return
                self._json(200, {'mode': 'live', 'capabilities': {'read_messages': True, 'control_collection': False},
                                 'poll_interval_ms': 1000}, cookie=True)
                return
            if not parsed.path.startswith('/api/'):
                self._json(404, {'error': {'code': 'web_route_not_found'}})
                return
            if not self._cookie_authenticated():
                self._json(401, {'error': {'code': 'web_session_required'}})
                return
            try:
                query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=False, max_num_fields=8)
                if any(len(values) != 1 for values in query.values()):
                    raise WebDataError('web_query_invalid', 400)
                if parsed.path in ('/api/conversations', '/api/status'):
                    if query:
                        raise WebDataError('web_query_invalid', 400)
                    result = self.server.data.conversations() if parsed.path.endswith('conversations') else self.server.data.status()
                elif parsed.path == '/api/messages':
                    if set(query) - {'conversation', 'before', 'after', 'limit', 'q'}:
                        raise WebDataError('web_query_invalid', 400)
                    def integer(name, default=None):
                        value = query.get(name, [None])[0]
                        if value is None:
                            return default
                        if re.fullmatch(r'[0-9]{1,19}', value) is None:
                            raise WebDataError('web_query_invalid', 400)
                        return int(value)
                    result = self.server.data.messages(query.get('conversation', [''])[0], before=integer('before'),
                                                       after=integer('after'), limit=integer('limit', 100), q=query.get('q', [''])[0])
                else:
                    raise WebDataError('web_route_not_found', 404)
                self._json(200, result)
            except WebDataError as error:
                self._json(error.status, {'error': {'code': error.code}})
            except (ValueError, OverflowError):
                self._json(400, {'error': {'code': 'web_query_invalid'}})
            except Exception:
                self._json(503, {'error': {'code': 'web_data_unavailable'}})

        def _unsupported(self):
            self._json(405, {'error': {'code': 'web_method_not_allowed'}})

        do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_HEAD = _unsupported

    server = LocalWebServer(('127.0.0.1', port), Handler)
    server.data, server.static_dir, server.token = data, static_dir, token
    server.expected_host = '127.0.0.1:%d' % server.server_address[1]
    server.base_url = 'http://' + server.expected_host
    server.launch_url = server.base_url + '/#token=' + token
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--static-dir', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-open', action='store_true')
    args = parser.parse_args()
    server = None
    try:
        server = make_server(CollectorWebData(args.data_dir), args.static_dir, port=args.port)
        print('ChatLens 本机页面已启动，仅监听本机；按 Ctrl+C 关闭。', flush=True)
        if not args.no_open:
            webbrowser.open(server.launch_url)
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    except Exception:
        print('reason=web_local_server_start_failed', flush=True)
        return 2
    finally:
        if server is not None:
            server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
