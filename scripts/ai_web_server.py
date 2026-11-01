"""Authenticated AI routes layered over the existing local-only Web server."""
import json
import re
from urllib.parse import parse_qs, urlsplit

from ai_service import AiError
from ai_provider import ProviderError
from ai_knowledge import KnowledgeError
from ai_query import QueryPlanError


def pairs(rows):
    result = {}
    for key, value in rows:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def attach_ai_routes(server, get_service):
    parent = server.RequestHandlerClass

    class Handler(parent):
        def _ai_target(self):
            if not self._request_allowed():
                self._json(403, {'error': {'code': 'web_origin_or_host_rejected'}})
                return None
            try:
                if len(self.path) > 4096:
                    raise ValueError
                target = urlsplit(self.path)
                if target.scheme or target.netloc or not target.path.startswith('/'):
                    raise ValueError
                return target
            except ValueError:
                self._json(400, {'error': {'code': 'ai_request_invalid'}})
                return None

        def _ai_session(self):
            if not self._cookie_authenticated():
                self._json(401, {'error': {'code': 'web_session_required'}})
                return False
            return True

        def _ai_operation(self, function):
            try:
                self._json(200, function(get_service()))
            except AiError as error:
                self._json(error.status, {'error': {'code': error.code, 'message': error.message}})
            except ProviderError as error:
                messages = {'ai_provider_url_invalid': '请填写 HTTPS 模型 API 地址，或本机回环地址。',
                            'ai_provider_model_invalid': '请填写有效的模型名称。',
                            'ai_provider_key_required': '请在本机填写 API Key。',
                            'ai_provider_key_invalid': 'API Key 格式无效。',
                            'ai_provider_key_action_invalid': '请选择有效的密钥保存方式。'}
                code = error.code if error.code in messages else 'ai_provider_configuration_failed'
                self._json(400, {'error': {'code': code, 'message': messages.get(code, '模型配置暂不可用，请检查后重试。')}})
            except KnowledgeError as error:
                self._json(error.status, {'error': {'code': 'ai_knowledge_unavailable',
                                                  'message': '当前知识库或原消息不可用，请重新检查范围。'}})
            except QueryPlanError as error:
                self._json(400, {'error': {'code': 'ai_query_plan_invalid', 'message': error.message}})
            except Exception:
                self._json(503, {'error': {'code': 'ai_operation_failed', 'message': '本机 AI 服务暂不可用，请稍后重试。'}})

        def do_GET(self):
            target = self._ai_target()
            if target is None:
                return
            if not target.path.startswith('/api/ai/'):
                return super().do_GET()
            if not self._ai_session():
                return
            try:
                query = parse_qs(target.query, keep_blank_values=True, max_num_fields=4)
                if any(len(values) != 1 for values in query.values()):
                    raise ValueError
                if target.path == '/api/ai/evidence':
                    if set(query) != {'conversation_id', 'message_id'}:
                        raise ValueError
                    identifier = query['message_id'][0]
                    if re.fullmatch(r'[0-9]{1,19}', identifier) is None:
                        raise ValueError
                    return self._ai_operation(lambda ai: ai.evidence(query['conversation_id'][0], int(identifier)))
                if query:
                    raise ValueError
            except ValueError:
                self._json(400, {'error': {'code': 'ai_query_invalid'}})
                return
            actions = {'/api/ai/status': lambda ai: ai.status(), '/api/ai/config': lambda ai: ai.config(),
                       '/api/ai/threads': lambda ai: ai.list_threads()}
            if target.path in actions:
                return self._ai_operation(actions[target.path])
            match = re.fullmatch(r'/api/ai/(jobs|threads)/([a-f0-9]{32})', target.path)
            if match:
                return self._ai_operation(lambda ai: ai.get_job(match[2]) if match[1] == 'jobs' else ai.get_thread(match[2]))
            self._json(404, {'error': {'code': 'ai_route_not_found'}})

        def do_POST(self):
            target = self._ai_target()
            if target is None:
                return
            if not target.path.startswith('/api/ai/'):
                return super().do_POST()
            if not self._ai_session():
                return
            if self.headers.get_all('Origin', []) != [self.server.base_url]:
                self._json(403, {'error': {'code': 'web_origin_or_host_rejected'}})
                return
            try:
                if target.query or self.headers.get_all('Transfer-Encoding', []):
                    raise ValueError
                lengths, kinds = self.headers.get_all('Content-Length', []), self.headers.get_all('Content-Type', [])
                if (len(lengths) != 1 or re.fullmatch(r'[0-9]{1,5}', lengths[0]) is None
                        or len(kinds) != 1 or re.fullmatch(r'application/json(?:;\s*charset=utf-8)?', kinds[0], re.I) is None):
                    raise ValueError
                count = int(lengths[0])
                if not 2 <= count <= 32768:
                    raise ValueError
                self.connection.settimeout(3)
                raw = self.rfile.read(count)
                if len(raw) != count:
                    raise ValueError
                def invalid(value):
                    raise ValueError
                value = json.loads(raw.decode('utf-8'), object_pairs_hook=pairs, parse_constant=invalid)
                if not isinstance(value, dict):
                    raise ValueError
            except (ValueError, OSError, UnicodeError, RecursionError):
                self._json(400, {'error': {'code': 'ai_body_invalid'}})
                return
            if target.path in ('/api/ai/threads',) and value:
                self._json(400, {'error': {'code': 'ai_body_invalid'}})
                return
            actions = {'/api/ai/config': lambda ai: ai.update_config(value),
                       '/api/ai/test': lambda ai: ai.test(value),
                       '/api/ai/plan': lambda ai: ai.preview_plan(value),
                       '/api/ai/questions': lambda ai: ai.ask(value),
                       '/api/ai/threads': lambda ai: ai.new_thread()}
            if target.path in actions:
                return self._ai_operation(actions[target.path])
            match = re.fullmatch(r'/api/ai/jobs/([a-f0-9]{32})/cancel', target.path)
            if match and not value:
                return self._ai_operation(lambda ai: ai.cancel(match[1]))
            self._json(404, {'error': {'code': 'ai_route_not_found'}})

    server.RequestHandlerClass = Handler
    return server
