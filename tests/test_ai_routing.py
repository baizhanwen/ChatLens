"""Service-level routing over temporary data and a completely fake provider."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ai_service as fixtures


class RoutingTests(unittest.TestCase):
    setUp = fixtures.AiAssistantTests.setUp
    tearDown = fixtures.AiAssistantTests.tearDown
    add = fixtures.AiAssistantTests.add
    question = fixtures.AiAssistantTests.question
    wait = fixtures.AiAssistantTests.wait
    ask_done = fixtures.AiAssistantTests.ask_done

    def local(self, question, *, scope=None, time_range='7d', thread_id=None):
        value = {'question': question, 'scope': scope or ['private_test', 'group_test'], 'time_range': time_range}
        if thread_id:
            value['thread_id'] = thread_id
        return self.wait(self.assistant.ask(value))

    def test_sender_variants_count_and_ranking_need_no_config_or_cloud_permission(self):
        self.add('一条业务内容。')
        self.add('另一条业务内容。')
        self.add('群内业务内容。', 'group_test')
        self.assistant.update_config({'base_url': 'https://synthetic-model.invalid/v1', 'model': 'synthetic-model',
                                      'key_action': 'delete', 'api_key': ''})
        for query, metric in [('今天都谁给我发信息了', 'senders'), ('今天有几条消息', 'messages'),
                              ('今天谁最活跃', 'ranking')]:
            with self.subTest(query=query):
                result = self.local(query)
                self.assertEqual(result['stage'], 'done')
                self.assertEqual(result['response_kind'], 'statistics')
                self.assertIsNone(result['model'])
                self.assertEqual(result['statistics']['metric'], metric)
                self.assertEqual(result['statistics']['message_count'], 3)
                self.assertEqual(result['statistics']['sender_count'], 1)
                if metric == 'senders':
                    self.assertIn('无法排除本人', result['answer'])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.assistant.cloud_request_attempts, 0)

    def test_preview_has_no_history_writes_counts_or_provider_calls_and_hides_raw_ids(self):
        self.add('报价明天更新。')
        before = hashlib.sha256(self.assistant.data.database.read_bytes()).hexdigest()
        threads = self.assistant.list_threads()
        plan = self.assistant.preview_plan({'question': '今天谁给我发了消息', 'scope': ['private_test'], 'time_range': '7d'})['plan']
        self.assertEqual(plan['route'], 'local_stats')
        self.assertFalse(plan['requires_model'])
        self.assertEqual(plan['time_label'], '今天')
        self.assertEqual(self.assistant.list_threads(), threads)
        self.assertEqual(hashlib.sha256(self.assistant.data.database.read_bytes()).hexdigest(), before)
        self.assertNotIn('synthetic_raw_sender_id', json.dumps(plan))
        self.assertEqual(self.provider.calls, [])

    def test_today_narrows_seven_days_and_followup_inherits_metric_for_yesterday(self):
        self.add('本日的内容。')
        self.add('昨日的内容。')
        local_now = datetime.now(timezone(timedelta(hours=8)))
        yesterday = local_now.replace(hour=12, minute=0, second=0) - timedelta(days=1)
        self.store._connection.execute('UPDATE messages SET sent_at=? WHERE rowid=2', (int(yesterday.timestamp()),))
        first = self.local('今天收到多少条消息')
        self.assertEqual(first['statistics']['message_count'], 1)
        second = self.local('那昨天呢', thread_id=first['thread_id'])
        self.assertEqual(second['plan']['metric'], 'messages')
        self.assertEqual(second['plan']['time_label'], '昨天')
        self.assertEqual(second['statistics']['message_count'], 1)
        self.assertEqual(self.provider.calls, [])

    def test_outside_time_window_clarifies_without_model_and_does_not_expand_scope(self):
        self.add('今日。')
        result = self.local('上个月一共有多少条消息', time_range='today', scope=['private_test'])
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(result['response_kind'], 'system')
        self.assertEqual(result['plan']['route'], 'clarify')
        self.assertEqual(result['plan']['scope'], ['private_test'])
        self.assertEqual(self.provider.calls, [])

    def test_analytical_paraphrase_uses_bounded_real_context_after_zero_literal_matches(self):
        self.add('这批货周五送达。')
        result = self.ask_done(question='交付进展如何', time_range='7d')
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(result['response_kind'], 'assistant')
        self.assertTrue(result['coverage']['retrieval_fallback'])
        self.assertEqual(result['evidence'][0]['body'], '这批货周五送达。')
        self.assertEqual(len(self.provider.calls), 1)

    def test_mixed_request_supplies_exact_local_aggregates_to_model(self):
        for index in range(35):
            self.add('合成业务进度%d。' % index, 'private_test' if index % 2 else 'group_test')
        result = self.ask_done(question='统计今天各会话消息数量并分析重点',
                               scope=['private_test', 'group_test'], time_range='7d')
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(result['plan']['route'], 'mixed')
        self.assertEqual(result['statistics']['message_count'], 35)
        self.assertEqual(result['statistics']['group_by'], 'conversation')
        self.assertEqual(sorted(row['message_count'] for row in result['statistics']['rows']), [17, 18])
        context = json.loads(self.provider.calls[-1]['messages'][-1]['content'])
        self.assertEqual(context['local_statistics']['message_count'], 35)
        self.assertLessEqual(len(context['evidence']['messages']), 30)
        for forbidden in ('synthetic_raw_sender_id', 'source_key', 'message_id', 'private_test', 'group_test'):
            self.assertNotIn(forbidden, json.dumps(context['local_statistics']))
        self.assertIn('35', result['answer'])
        fixtures.service.validate_citations(result['answer'], result['evidence'])

    def test_local_history_keeps_statistics_and_public_plan_without_internal_context(self):
        self.add('合成内容。')
        result = self.local('今天消息数量是多少')
        thread = self.assistant.get_thread(result['thread_id'])
        answer = thread['messages'][-1]
        self.assertEqual(answer['response_kind'], 'statistics')
        self.assertEqual(answer['plan']['route'], 'local_stats')
        self.assertNotIn('query_plan', answer)
        self.assertNotIn('_plan', self.assistant.get_job(result['id']))
        self.assertEqual(answer['statistics']['message_count'], 1)

    def test_queries_recount_new_commits_instead_of_reusing_previous_totals(self):
        self.add('首条。')
        first = self.local('今天有多少条消息')
        self.add('后到的消息。')
        second = self.local('再统计一下今天有多少条消息', thread_id=first['thread_id'])
        self.assertEqual(first['statistics']['message_count'], 1)
        self.assertEqual(second['statistics']['message_count'], 2)
        self.assertEqual(self.provider.calls, [])

    def test_statistics_scope_cannot_include_other_selected_conversation_implicitly(self):
        self.add('私聊内容。')
        for _ in range(3):
            self.add('群聊内容。', 'group_test')
        result = self.local('今天有多少条消息', scope=['private_test'])
        self.assertEqual(result['statistics']['message_count'], 1)
        self.assertTrue(all(row['conversation_id'] == 'private_test' for row in result['evidence']))

    def test_analysis_preserves_literal_and_sender_filters_in_provider_context(self):
        self.add('合成退款安排周五确认。')
        self.add('SYNTHETIC_EXCLUDED_OTHER_PERSON 退款延期。')
        self.add('SYNTHETIC_EXCLUDED_OTHER_TOPIC 采购正常。')
        self.store._connection.execute('UPDATE messages SET sender_id=? WHERE rowid=2', ('another_sender',))
        result = self.ask_done(question='分析合成客户说的包含“退款”的消息', time_range='7d')
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(len(self.provider.calls), 1)
        sent = json.dumps(self.provider.calls[-1]['messages'], ensure_ascii=False)
        self.assertIn('合成退款安排', sent)
        self.assertNotIn('SYNTHETIC_EXCLUDED_OTHER_PERSON', sent)
        self.assertNotIn('SYNTHETIC_EXCLUDED_OTHER_TOPIC', sent)

    def test_least_active_ranking_orders_ascending(self):
        self.add('甲的内容。')
        for _ in range(3):
            self.add('乙的内容。', 'group_test')
        result = self.local('今天哪个会话消息最少')
        self.assertEqual(result['stage'], 'done')
        self.assertEqual(result['statistics']['order'], 'asc')
        self.assertEqual(result['statistics']['rows'][0]['message_count'], 1)

    def test_uncertain_business_predicates_are_never_reported_as_full_population_counts(self):
        self.add('内容未提及欠款或延期。')
        for query in ['有多少客户欠款', '有几个人要求延期', '今天张三有几条消息', '今天谁没给我发消息']:
            with self.subTest(query=query):
                plan = self.assistant.preview_plan({'question': query, 'scope': ['private_test'], 'time_range': '7d'})['plan']
                self.assertNotEqual(plan['route'], 'local_stats')


if __name__ == '__main__':
    unittest.main()
