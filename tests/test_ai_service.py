"""Synthetic selected store + real knowledge + fake model service only."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import ai_provider
import ai_service as service
from collector_private import read_private_json, write_private_json_new
from continuous_store import CollectorStore

SYNTHETIC_KEY = 'SYNTHETIC_NOT_A_REAL_MODEL_API_KEY'


class FakeProvider:
    # Reuse the real *local-only* configuration implementation. Network entry
    # points are replaced completely, including connection tests.
    public_config = staticmethod(ai_provider.public_config)
    load_config = staticmethod(ai_provider.load_config)
    update_config = staticmethod(ai_provider.update_config)

    def __init__(self):
        self.calls = []
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        self.answer = '已找到本轮聊天依据。[E1]'
        self.failure = None

    def chat(self, messages, config, *, cancel_event):
        self.calls.append({'kind': 'chat', 'messages': messages, 'model': config.model})
        self.entered.set()
        if not self.release.wait(3):
            raise RuntimeError('SYNTHETIC_TEST_GATE_TIMEOUT')
        if self.failure:
            raise self.failure
        # Deliberately ignore cancellation here. The service must independently
        # reject late answers even if a provider cannot abort its own operation.
        return {'text': self.answer, 'model': config.model, 'usage': {'total_tokens': 12}}

    def test_connection(self, config, *, cancel_event):
        self.calls.append({'kind': 'test', 'model': config.model})
        return {'ok': True}


class AiAssistantTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-ai-service-')
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'collector-data'
        self.private = self.data / 'private'
        self.store_dir = self.private / 'store'
        for directory in (self.data, self.private, self.store_dir):
            directory.mkdir(mode=0o700)
        self.store = CollectorStore(self.store_dir, {'account': 'SYNTHETIC_SERVICE_ACCOUNT', 'selected': ['private_test', 'group_test']})
        self.store.initialize({}, {})
        write_private_json_new(self.private / 'catalog.json', {'schema_version': 1, 'conversations': [
            {'id': 'private_test', 'name': '合成私聊甲', 'kind': 'private', 'sender_names': {'synthetic_raw_sender_id': '合成客户'}},
            {'id': 'group_test', 'name': '合成项目群', 'kind': 'group', 'sender_names': {'synthetic_raw_sender_id': '合成成员'}}]})
        self.provider = FakeProvider()
        self.ai_dir = self.private / 'ai'
        self.assistant = service.AiAssistant(self.data, self.ai_dir, provider=self.provider)
        self.assistants = [self.assistant]
        self.assistant.update_config({'base_url': 'https://synthetic-model.invalid/v1', 'model': 'synthetic-model',
                                      'key_action': 'replace', 'api_key': SYNTHETIC_KEY})
        self.serial = 0

    def tearDown(self):
        self.provider.release.set()
        for assistant in self.assistants:
            assistant.close()
        self.store.close()
        self.tmp.cleanup()

    def add(self, body, alias='private_test'):
        self.serial += 1
        observed = datetime.now(timezone.utc)
        self.store.commit({}, {}, [{'source_key': hashlib.sha256(('synthetic-ai-service-%d' % self.serial).encode()).hexdigest(),
            'alias': alias, 'source_local_id': self.serial, 'sent_at': int(observed.timestamp()),
            'sender_id': 'synthetic_raw_sender_id', 'body': body, 'observed_at': observed.isoformat()}], {})

    def question(self, question='今天有哪些重点', scope=None, time_range='all', thread_id=None, **extra):
        target = self.assistant.config()['target_fingerprint']
        value = {'question': question, 'scope': scope or ['private_test'], 'time_range': time_range,
                 'target_fingerprint': target, 'confirmed_target': target}
        if thread_id is not None:
            value['thread_id'] = thread_id
        value.update(extra)
        return value

    def wait(self, launched):
        job_id = launched['job_id']
        self.assistant.jobs[job_id]['worker'].join(timeout=4)
        self.assertFalse(self.assistant.jobs[job_id]['worker'].is_alive(), 'synthetic worker did not finish')
        return self.assistant.get_job(job_id)

    def ask_done(self, **arguments):
        return self.wait(self.assistant.ask(self.question(**arguments)))

    def test_scope_target_and_cloud_consent_are_checked_before_provider_calls_or_thread_creation(self):
        invalid = [self.question(scope=['unselected_alias']), self.question(target_fingerprint='stale'),
                   self.question(confirmed_target=None)]
        expected = ['ai_scope_invalid', 'ai_target_changed', 'ai_cloud_scope_confirmation_required']
        for value, code in zip(invalid, expected):
            with self.assertRaises(service.AiError) as caught:
                self.assistant.ask(value)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.assistant.list_threads()['threads'], [])

    def test_empty_evidence_finishes_as_system_result_without_model_call(self):
        result = self.ask_done()
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(result['response_kind'], 'system')
        self.assertIsNone(result['model'])
        self.assertEqual(self.provider.calls, [])
        thread = self.assistant.get_thread(result['thread_id'])
        self.assertEqual([row['role'] for row in thread['messages']], ['user', 'system'])
        self.assertEqual(result['coverage']['available_count'], 0)

    def test_real_retrieval_valid_reference_and_provider_projection_are_preserved(self):
        self.add('这笔订单需要周五交付。')
        self.add('SYNTHETIC_EXCLUDED_BODY', 'unselected_alias')
        result = self.ask_done()
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(result['response_kind'], 'assistant')
        self.assertEqual(result['cited'], ['E1'])
        self.assertEqual(result['evidence'][0]['body'], '这笔订单需要周五交付。')
        sent = json.dumps(self.provider.calls[0]['messages'], ensure_ascii=False)
        for forbidden in ('source_key', 'message_id', 'conversation_id', 'private_test', 'group_test',
                          'synthetic_raw_sender_id', 'SYNTHETIC_EXCLUDED_BODY', SYNTHETIC_KEY,
                          self.store.binding_digest, str(self.private)):
            self.assertNotIn(forbidden, sent)
        self.assertEqual(self.provider.calls[0]['messages'][0]['role'], 'system')
        self.assertIn('不可信', self.provider.calls[0]['messages'][0]['content'])

    def test_unknown_or_missing_reference_is_error_and_never_saved_as_assistant(self):
        self.add('报价已确认。')
        for answer, code in [('未引用的回答', 'knowledge_answer_missing_citations'),
                             ('编造不存在的消息[E999]', 'knowledge_answer_unknown_citation')]:
            self.provider.answer = answer
            result = self.ask_done()
            self.assertEqual(result['stage'], 'error')
            self.assertEqual(result['error']['code'], code)
            self.assertFalse(any(row['role'] == 'assistant' for row in self.assistant.get_thread(result['thread_id'])['messages']))

    def test_history_persists_locally_across_restart_and_rejects_another_store_binding(self):
        self.add('发票待确认。')
        result = self.ask_done()
        thread_id = result['thread_id']
        saved = self.assistant.get_thread(thread_id)
        self.assistant.close()
        reopened = service.AiAssistant(self.data, self.ai_dir, provider=self.provider)
        self.assistants.append(reopened)
        self.assertEqual(reopened.get_thread(thread_id), saved)
        stored = self.ai_dir / 'threads' / (thread_id + '.json')
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.store._connection.execute('UPDATE checkpoint SET binding_digest=?', ('0' * 64,))
        other = service.AiAssistant(self.data, self.ai_dir, provider=self.provider)
        self.assistants.append(other)
        with self.assertRaises(service.AiError) as caught:
            other.get_thread(thread_id)
        self.assertEqual(caught.exception.code, 'ai_thread_binding_invalid')

    def test_followup_forwards_only_user_questions_from_same_scope_and_time(self):
        self.add('采购报价已确认。')
        self.add('项目交付周五确认。', 'group_test')
        self.provider.answer = 'SYNTHETIC_PRIOR_AI_ANSWER_MUST_NOT_FORWARD。[E1]'
        first = self.ask_done(question='请查看报价')
        thread = first['thread_id']
        self.provider.answer = '本轮回答。[E1]'
        self.ask_done(question='SYNTHETIC_OTHER_SCOPE_USER_QUESTION', scope=['group_test'], thread_id=thread)
        self.ask_done(question='SYNTHETIC_OTHER_TIME_USER_QUESTION', time_range='today', thread_id=thread)
        self.ask_done(question='今天重点', thread_id=thread)
        context = json.loads(self.provider.calls[-1]['messages'][-1]['content'])
        self.assertEqual(context['previous_user_questions_for_reference_only'], ['请查看报价'])
        sent = json.dumps(self.provider.calls[-1]['messages'], ensure_ascii=False)
        self.assertNotIn('SYNTHETIC_PRIOR_AI_ANSWER_MUST_NOT_FORWARD', sent)
        self.assertNotIn('SYNTHETIC_OTHER_SCOPE_USER_QUESTION', sent)
        self.assertNotIn('SYNTHETIC_OTHER_TIME_USER_QUESTION', sent)

    def test_cancelled_late_model_response_is_not_persisted_as_an_answer(self):
        self.add('采购报价待确认。')
        self.provider.release.clear()
        launched = self.assistant.ask(self.question())
        self.assertTrue(self.provider.entered.wait(2))
        self.assistant.cancel(launched['job_id'])
        self.provider.release.set()
        result = self.wait(launched)
        self.assertEqual(result['stage'], 'cancelled')
        self.assertNotIn('answer', result)
        thread = self.assistant.get_thread(launched['thread_id'])
        self.assertFalse(any(row['role'] == 'assistant' for row in thread['messages']))

    def test_target_revision_change_discards_response_from_old_target(self):
        self.add('采购待确认。')
        self.provider.release.clear()
        launched = self.assistant.ask(self.question())
        self.assertTrue(self.provider.entered.wait(2))
        old_target = self.assistant.config()['target_fingerprint']
        self.assistant.update_config({'base_url': 'https://synthetic-other.invalid/v1', 'model': 'other-model',
                                      'key_action': 'replace', 'api_key': 'SYNTHETIC_NEW_KEY_ONLY'})
        self.assertNotEqual(self.assistant.config()['target_fingerprint'], old_target)
        self.provider.release.set()
        result = self.wait(launched)
        self.assertEqual(result['stage'], 'cancelled')
        self.assertNotIn('answer', result)
        self.assertFalse(any(row['role'] == 'assistant' for row in self.assistant.get_thread(launched['thread_id'])['messages']))

    def test_account_change_during_model_call_rejects_old_answer_before_persistence(self):
        self.add('付款时间周五确认。')
        self.provider.release.clear()
        launched = self.assistant.ask(self.question())
        self.assertTrue(self.provider.entered.wait(2))
        self.store._connection.execute('UPDATE checkpoint SET binding_digest=?', ('0' * 64,))
        self.provider.release.set()
        result = self.wait(launched)
        self.assertEqual(result['stage'], 'error')
        self.assertEqual(result['error']['code'], 'ai_account_changed')
        self.assertNotIn('answer', result)
        raw = read_private_json(self.ai_dir / 'threads' / (launched['thread_id'] + '.json'))
        self.assertFalse(any(row['role'] == 'assistant' for row in raw['messages']))

    def test_done_is_not_visible_before_answer_is_durably_persisted(self):
        self.add('采购报价已经确认。')
        entered, release, configured = threading.Event(), threading.Event(), threading.Event()
        original = self.assistant._finish_thread
        def pause(job, message):
            if message['role'] == 'assistant':
                entered.set()
                if not release.wait(3):
                    raise RuntimeError('SYNTHETIC_PERSIST_GATE_TIMEOUT')
            return original(job, message)
        changer = None
        with patch.object(self.assistant, '_finish_thread', side_effect=pause):
            launched = self.assistant.ask(self.question())
            try:
                self.assertTrue(entered.wait(2))
                # Inspect the synthetic job without taking its transaction
                # lock: get_job legitimately waits for atomic finalization.
                self.assertNotEqual(self.assistant.jobs[launched['job_id']]['stage'], 'done')
                def configure():
                    self.assistant.update_config({'base_url': 'https://synthetic-other.invalid/v1', 'model': 'other',
                                                  'key_action': 'replace', 'api_key': 'SYNTHETIC_OTHER_KEY'})
                    configured.set()
                changer = threading.Thread(target=configure)
                changer.start()
                self.assertFalse(configured.wait(.05))
            finally:
                release.set()
                if changer is not None:
                    changer.join(timeout=3)
            result = self.wait(launched)
        self.assertEqual(result['stage'], 'done')
        self.assertTrue(configured.is_set())
        raw = read_private_json(self.ai_dir / 'threads' / (launched['thread_id'] + '.json'))
        self.assertTrue(any(row['role'] == 'assistant' for row in raw['messages']))

    def test_provider_failure_is_fixed_retryable_system_feedback_without_fake_ai(self):
        self.add('订单待确认。')
        self.provider.failure = ai_provider.ProviderError('ai_provider_timeout')
        failed = self.ask_done()
        self.assertEqual(failed['stage'], 'error')
        self.assertEqual(failed['error']['code'], 'ai_provider_timeout')
        self.assertNotIn('answer', failed)
        self.provider.failure = RuntimeError('SYNTHETIC_PRIVATE_RAW_PROVIDER_RESPONSE')
        unknown = self.ask_done()
        self.assertEqual(unknown['error']['code'], 'ai_generation_failed')
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(unknown))
        self.provider.failure = None
        self.assertEqual(self.ask_done()['stage'], 'done')

    def test_public_config_hides_key_and_connection_test_uses_no_chat_evidence(self):
        self.add('SYNTHETIC_CHAT_MUST_NOT_BE_SENT_BY_TEST')
        config = self.assistant.config()
        self.assertTrue(config['has_key'])
        self.assertNotIn(SYNTHETIC_KEY, json.dumps(config))
        self.assertNotIn('api_key', config)
        result = self.wait(self.assistant.test({'target_fingerprint': config['target_fingerprint']}))
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(self.provider.calls, [{'kind': 'test', 'model': 'synthetic-model'}])
        self.assertEqual(self.assistant.list_threads()['threads'], [])

    def test_single_worker_busy_then_new_request_retries_after_completion(self):
        self.add('订单需确认。')
        self.provider.release.clear()
        launched = self.assistant.ask(self.question())
        self.assertTrue(self.provider.entered.wait(2))
        with self.assertRaises(service.AiError) as caught:
            self.assistant.ask(self.question())
        self.assertEqual(caught.exception.code, 'ai_busy')
        self.provider.release.set()
        self.assertEqual(self.wait(launched)['stage'], 'done')
        self.assertEqual(self.ask_done()['stage'], 'done')
        self.assertEqual(len(self.provider.calls), 2)

    def test_newly_committed_messages_are_retrieved_in_later_turn_not_old_snapshot_only(self):
        self.add('第一条交付说明。')
        first = self.ask_done()
        self.add('SYNTHETIC_NEWLY_COMMITTED_MESSAGE')
        second = self.ask_done(thread_id=first['thread_id'])
        self.assertEqual(first['coverage']['available_count'], 1)
        self.assertEqual(second['coverage']['available_count'], 2)
        self.assertIn('SYNTHETIC_NEWLY_COMMITTED_MESSAGE', json.dumps(self.provider.calls[-1]['messages'], ensure_ascii=False))

    def test_history_budget_rejects_question_without_mutating_saved_thread(self):
        thread_id = self.assistant.new_thread()['id']
        target = self.ai_dir / 'threads' / (thread_id + '.json')
        before = target.read_bytes()
        with patch.object(service, 'MAX_NEW_JSON_BYTES', 4300):
            with self.assertRaises(service.AiError) as caught:
                self.assistant.ask(self.question(thread_id=thread_id))
        self.assertEqual(caught.exception.code, 'ai_history_limit')
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.assistant.jobs, {})

    def test_answer_json_reservation_stops_before_provider_call_and_is_retryable_in_new_thread(self):
        self.add('采购需要确认。')
        # A small but valid local-history quota can fit the question/evidence,
        # while it cannot safely reserve an escaped 64KiB model answer.
        with patch.object(service, 'MAX_NEW_JSON_BYTES', 64000):
            failed = self.ask_done()
        self.assertEqual(failed['stage'], 'error')
        self.assertEqual(failed['error']['code'], 'ai_history_limit')
        self.assertNotIn('answer', failed)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.assistant.model_request_attempts, 0)
        self.assertFalse(any(row['role'] == 'assistant' for row in self.assistant.get_thread(failed['thread_id'])['messages']))
        self.assertEqual(self.ask_done()['stage'], 'done')
        self.assertEqual(len(self.provider.calls), 1)

    def test_oversized_utf8_or_control_character_answer_never_persists_as_assistant(self):
        self.add('交付日期待确认。')
        for answer in ('汉' * (65536 // 3 + 1) + '[E1]', '异常\x01内容[E1]', '异常\x7f内容[E1]'):
            with self.subTest(byte_length=len(answer.encode('utf-8'))):
                self.provider.answer = answer
                failed = self.ask_done()
                self.assertEqual(failed['stage'], 'error')
                self.assertEqual(failed['error']['code'], 'ai_answer_too_large')
                self.assertNotIn('answer', failed)
                thread = self.assistant.get_thread(failed['thread_id'])
                self.assertFalse(any(row['role'] == 'assistant' for row in thread['messages']))
        self.provider.answer = '结论\n\t有对应的聊天依据。[E1]'
        self.assertEqual(self.ask_done()['stage'], 'done')

    def test_unencodable_or_overlong_question_is_rejected_before_call_or_history_write(self):
        for question in ('问' * 1800 + '\ud800', '问' * 2001):
            with self.subTest(length=len(question)):
                with self.assertRaises(service.AiError) as caught:
                    self.assistant.ask(self.question(question=question))
                self.assertEqual(caught.exception.code, 'ai_question_invalid')
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.assistant.list_threads()['threads'], [])
        self.assertEqual(self.assistant.jobs, {})


if __name__ == '__main__':
    unittest.main()
