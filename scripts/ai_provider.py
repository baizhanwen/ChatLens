"""User-configured text-only Chat Completions; private credentials, no retries.

Calling code owns account scope and explicit target consent. Importing,
loading, saving or inspecting a configuration never performs a network call.
"""
from dataclasses import dataclass, field
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
from queue import SimpleQueue
import re
import secrets
import socket
import ssl
import sys
import threading
import time
from urllib.parse import unquote, urlsplit, urlunsplit

PRIVATE_HELPERS = Path(__file__).resolve().parent / 'windows-proof'
if str(PRIVATE_HELPERS) not in sys.path:
    sys.path.insert(0, str(PRIVATE_HELPERS))
from collector_private import ensure_private_dir, read_private_json, write_private_json_new


CONFIG_NAME = 'provider.json'
REQUEST_TIMEOUT = 45.0
MAX_BODY_BYTES = 1024 * 1024
MAX_CONTEXT_BYTES = 512 * 1024
MAX_MESSAGE_BYTES = 128 * 1024
MAX_MESSAGES = 64
MINIMAX_MODELS = ('MiniMax-M3.1-Flash', 'MiniMax-M3')
MINIMAX_PRESETS = (
    {'id': 'minimax-cn', 'label': 'MiniMax 国内', 'base_url': 'https://api.minimax.cn/v1',
     'model': 'MiniMax-M3.1-Flash', 'models': MINIMAX_MODELS,
     'source_url': 'https://platform.minimax.cn/docs/api-reference/text-chat-openai'},
    {'id': 'minimax-global', 'label': 'MiniMax 国际', 'base_url': 'https://api.minimax.io/v1',
     'model': 'MiniMax-M3.1-Flash', 'models': MINIMAX_MODELS,
     'source_url': 'https://platform.minimax.io/docs/api-reference/text-chat-openai'},
)
_CONFIG_LOCK = threading.RLock()
_DNS_SLOTS = threading.BoundedSemaphore(4)


class ProviderError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProviderConfig:
    base_url: str = ''
    model: str = ''
    api_key: str = field(default='', repr=False)
    credential_revision: str = field(default='', repr=False)

    def get(self, name, default=None):
        return getattr(self, name, default)

    def __getitem__(self, name):
        if name not in ('base_url', 'model', 'api_key', 'credential_revision'):
            raise KeyError(name)
        return getattr(self, name)


def _url(value):
    try:
        if (not isinstance(value, str) or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value)
                or any(c in value for c in ('\\', '?', '#'))):
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        host = parsed.hostname.encode('idna').decode('ascii').lower()
        if '%' in host:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
            local = address.is_loopback
            rendered_host = '[' + host + ']' if address.version == 6 else host
        except ValueError:
            if (len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part)
                                       for part in host.split('.'))):
                raise ValueError
            local, rendered_host = False, host
        if parsed.scheme == 'http' and not local:
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        if any(part in ('.', '..') for part in unquote(parsed.path).split('/')):
            raise ValueError
        default_port = 443 if parsed.scheme == 'https' else 80
        authority = rendered_host + (':' + str(port) if port is not None and port != default_port else '')
        normalized = urlunsplit((parsed.scheme, authority, parsed.path.rstrip('/'), '', ''))
        normalized.encode('ascii')
        return normalized, local
    except Exception:
        raise ProviderError('ai_provider_url_invalid') from None


def _model(value):
    try:
        if not isinstance(value, str) or not 1 <= len(value.encode('utf-8')) <= 256 or any(ord(c) < 33 or ord(c) == 127 for c in value):
            raise ValueError
        return value
    except Exception:
        raise ProviderError('ai_provider_model_invalid') from None


def _key(value):
    if not isinstance(value, str) or len(value) > 8192 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ProviderError('ai_provider_key_invalid')
    return value


def _configuration(value, *, stored=False):
    try:
        base, model = value.get('base_url', ''), value.get('model', '')
        key, revision = value.get('api_key', ''), value.get('credential_revision', '')
        if not isinstance(base, str) or not isinstance(model, str):
            raise ValueError
        base = _url(base)[0] if base else ''
        model = _model(model) if model else ''
        _key(key)
        if not isinstance(revision, str) or (revision and re.fullmatch(r'[0-9a-f]{32}', revision) is None):
            raise ValueError
        if stored and (not isinstance(value, dict) or set(value) != {'schema_version', 'base_url', 'model', 'api_key', 'credential_revision'}
                       or type(value['schema_version']) is not int or value['schema_version'] != 1 or not revision):
            raise ValueError
        return ProviderConfig(base, model, key, revision)
    except ProviderError:
        raise
    except Exception:
        raise ProviderError('ai_provider_configuration_invalid') from None


def load_config(private_dir):
    """Internal config; credential field is excluded from repr and public views."""
    try:
        directory = ensure_private_dir(Path(private_dir))
        path = directory / CONFIG_NAME
        if not path.exists() and not path.is_symlink():
            return ProviderConfig()
        return _configuration(read_private_json(path, maxbytes=32768), stored=True)
    except Exception:
        raise ProviderError('ai_provider_configuration_unavailable') from None


def _public(config):
    local = _url(config.base_url)[1] if config.base_url else False
    configured = bool(config.base_url and config.model and (local or config.api_key))
    fingerprint = None
    if config.base_url and config.model:
        target = [config.base_url + '/chat/completions', config.model, config.credential_revision]
        fingerprint = hashlib.sha256(json.dumps(target, separators=(',', ':')).encode()).hexdigest()
    return {'base_url': config.base_url, 'model': config.model, 'has_key': bool(config.api_key),
            'configured': configured, 'target_fingerprint': fingerprint, 'is_local': local}


def public_config(private_dir):
    return _public(load_config(private_dir))


def update_config(private_dir, *, base_url, model, key_action='keep', api_key=None):
    """Explicit keep/replace/delete; a new API base never reuses its old key."""
    with _CONFIG_LOCK:
        old = load_config(private_dir)
        if not isinstance(base_url, str) or not isinstance(model, str):
            raise ProviderError('ai_provider_configuration_invalid')
        base = _url(base_url)[0] if base_url else ''
        model = _model(model) if model else ''
        changed = (base, model) != (old.base_url, old.model)
        endpoint_changed = base != old.base_url
        if key_action not in ('keep', 'replace', 'delete'):
            raise ProviderError('ai_provider_key_action_invalid')
        if key_action == 'replace':
            key = _key(api_key)
            if not key:
                raise ProviderError('ai_provider_key_required')
        else:
            if api_key not in (None, ''):
                raise ProviderError('ai_provider_key_action_invalid')
            key = old.api_key if key_action == 'keep' and not endpoint_changed else ''
        revision = old.credential_revision
        if not revision or changed or key_action != 'keep':
            revision = secrets.token_hex(16)
        config = ProviderConfig(base, model, key, revision)
        value = {'schema_version': 1, 'base_url': base, 'model': model, 'api_key': key, 'credential_revision': revision}
        directory, temporary = Path(private_dir), None
        try:
            ensure_private_dir(directory)
            temporary = directory / ('.provider-' + secrets.token_hex(12) + '.json')
            write_private_json_new(temporary, value)
            os.replace(temporary, directory / CONFIG_NAME)
        except Exception:
            raise ProviderError('ai_provider_configuration_write_failed') from None
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return _public(config)


def _messages(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_MESSAGES:
        raise ProviderError('ai_provider_messages_invalid')
    result, total = [], 0
    try:
        for row in value:
            if not isinstance(row, dict) or set(row) != {'role', 'content'} or row['role'] not in ('system', 'user', 'assistant') or not isinstance(row['content'], str):
                raise ValueError
            size = len(row['content'].encode('utf-8'))
            total += size
            if size > MAX_MESSAGE_BYTES or total > MAX_CONTEXT_BYTES or '\0' in row['content']:
                raise ValueError
            result.append({'role': row['role'], 'content': row['content']})
        return result
    except Exception:
        raise ProviderError('ai_provider_messages_invalid') from None


def _payload(config, messages, limit):
    payload = {'model': config.model, 'messages': _messages(messages), 'stream': False}
    if urlsplit(config.base_url).hostname in ('api.minimax.cn', 'api.minimax.io') and config.model in MINIMAX_MODELS:
        payload.update(max_completion_tokens=limit, reasoning_split=True)
        if config.model == 'MiniMax-M3.1-Flash':
            payload['reasoning_effort'] = 'low'
        else:
            payload['thinking'] = {'type': 'disabled'}
    else:
        payload['max_tokens'] = limit
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_BODY_BYTES:
        raise ProviderError('ai_provider_request_too_large')
    return raw


def _check(deadline, cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise ProviderError('ai_request_cancelled')
    if time.monotonic() >= deadline:
        raise ProviderError('ai_provider_timeout')


def _addresses(host, port, deadline, cancel_event):
    try:
        address = ipaddress.ip_address(host)
        return [(socket.AF_INET6 if address.version == 6 else socket.AF_INET, socket.SOCK_STREAM,
                 socket.IPPROTO_TCP, '', (host, port, 0, 0) if address.version == 6 else (host, port))]
    except ValueError:
        pass
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ProviderError('ai_provider_resolver_busy')
    done, result = threading.Event(), SimpleQueue()
    def resolve():
        try:
            result.put(socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
        except Exception:
            result.put(None)
        finally:
            _DNS_SLOTS.release(); done.set()
    try:
        threading.Thread(target=resolve, name='chatlens-provider-dns', daemon=True).start()
    except Exception:
        _DNS_SLOTS.release()
        raise ProviderError('ai_provider_network_failed') from None
    while not done.wait(.05):
        _check(deadline, cancel_event)
    _check(deadline, cancel_event)
    values = result.get()
    if not values:
        raise ProviderError('ai_provider_network_failed')
    return values[:8]


def _final_response(raw, model, api_key=''):
    def pairs(rows):
        result = {}
        for name, value in rows:
            if name in result:
                raise ValueError
            result[name] = value
        return result
    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(document, dict) or 'error' in document:
            raise ValueError
        if document.get('base_resp', {}).get('status_code', 0) != 0:
            raise ValueError
        choice = document['choices'][0]
        if choice.get('finish_reason') == 'length':
            raise ProviderError('ai_provider_output_limit')
        text = choice['message']['content']
        if not isinstance(text, str) or not text.strip() or len(text.encode('utf-8')) > MAX_BODY_BYTES:
            raise ValueError
        if api_key:
            text = text.replace(api_key, '[凭据已隐藏]')
        usage = document.get('usage', {})
        usage = {name: usage[name] for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                 if isinstance(usage, dict) and type(usage.get(name)) is int and 0 <= usage[name] <= 2**53}
        return {'text': text, 'model': model, 'usage': usage}
    except ProviderError:
        raise
    except Exception:
        raise ProviderError('ai_provider_response_invalid') from None


def _complete(messages, config, cancel_event, limit):
    config = _configuration(config)
    if not _public(config)['configured']:
        raise ProviderError('ai_provider_not_configured')
    raw = _payload(config, messages, limit)
    deadline = time.monotonic() + REQUEST_TIMEOUT
    _check(deadline, cancel_event)
    parsed = urlsplit(config.base_url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    addresses = _addresses(parsed.hostname, port, deadline, cancel_event)
    done, state_lock, owned = threading.Event(), threading.Lock(), {'socket': None}
    connection = None
    def abort():
        with state_lock:
            current = owned['socket']
        if current is not None:
            try:
                current.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                current.close()
            except OSError:
                pass
    def watch():
        while not done.wait(.05):
            if time.monotonic() >= deadline or cancel_event is not None and cancel_event.is_set():
                abort(); return
    watcher = threading.Thread(target=watch, name='chatlens-provider-cancel', daemon=True)
    try:
        watcher.start()
        connected = None
        for family, kind, protocol, _, address in addresses:
            _check(deadline, cancel_event)
            candidate = socket.socket(family, kind, protocol)
            with state_lock:
                owned['socket'] = candidate
            try:
                candidate.settimeout(max(.001, deadline - time.monotonic()))
                candidate.connect(address)
                connected = candidate
                break
            except OSError:
                candidate.close()
        _check(deadline, cancel_event)
        if connected is None:
            raise ProviderError('ai_provider_network_failed')
        if parsed.scheme == 'https':
            context = ssl.create_default_context()
            connected = context.wrap_socket(connected, server_hostname=parsed.hostname, do_handshake_on_connect=False)
            with state_lock:
                owned['socket'] = connected
            _check(deadline, cancel_event)
            connected.do_handshake()
        _check(deadline, cancel_event)
        connection = http.client.HTTPConnection(parsed.hostname, port, timeout=max(.001, deadline - time.monotonic()))
        connection.set_debuglevel(0)
        connection.sock = connected
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        if config.api_key:
            headers['Authorization'] = 'Bearer ' + config.api_key
        connection.request('POST', parsed.path.rstrip('/') + '/chat/completions', body=raw, headers=headers)
        _check(deadline, cancel_event)
        response = connection.getresponse()
        _check(deadline, cancel_event)
        if 300 <= response.status < 400:
            raise ProviderError('ai_provider_redirect_refused')
        if response.status in (401, 403):
            raise ProviderError('ai_provider_authentication_failed')
        if response.status == 429:
            raise ProviderError('ai_provider_rate_limited')
        if response.status != 200:
            raise ProviderError('ai_provider_upstream_failed')
        if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
            raise ProviderError('ai_provider_response_encoding_invalid')
        length = response.getheader('Content-Length')
        if length is not None and (not re.fullmatch(r'[0-9]{1,12}', length) or int(length) > MAX_BODY_BYTES):
            raise ProviderError('ai_provider_response_too_large')
        body = response.read(MAX_BODY_BYTES + 1)
        _check(deadline, cancel_event)
        if len(body) > MAX_BODY_BYTES:
            raise ProviderError('ai_provider_response_too_large')
        return _final_response(body, config.model, config.api_key)
    except ProviderError:
        raise
    except ssl.SSLCertVerificationError:
        _check(deadline, cancel_event)
        raise ProviderError('ai_provider_tls_verification_failed') from None
    except Exception:
        _check(deadline, cancel_event)
        raise ProviderError('ai_provider_network_failed') from None
    finally:
        done.set()
        abort()
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
        if watcher.ident is not None:
            watcher.join(timeout=.2)


def chat(messages, config, cancel_event=None):
    return _complete(messages, config, cancel_event, 2048)


def test_connection(config, cancel_event=None):
    """Only call after the user's explicit test action; contains no chat data."""
    return _complete([{'role': 'user', 'content': 'This is a connection test. Reply only OK.'}],
                     config, cancel_event, 512)
