"""Service-level routing over temporary data and a completely fake provider."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ai_service as fixtures
from ai_knowledge import KnowledgeError
from ai_query import QueryPlanError
from collector_private import write_private_json_new
from conversation_scope import alias_for, normalize_conversations
from continuous_store import CollectorStore
from wechat_identity import establish_identity


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
                    self.assertIn('共收到', result['answer'])
                    self.assertTrue(result['statistics']['identity_verified'])
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

    def test_received_sent_and_split_queries_use_verified_identity_without_model(self):
        self.add('收到的业务文字。')
        self.add('发出的业务文字。')
        self.add('未知方向的文字。')
        self.store._connection.execute('UPDATE messages SET sender_id=? WHERE rowid=2', ('synthetic_self_sender',))
        self.store._connection.execute('UPDATE messages SET sender_id=NULL WHERE rowid=3')
        incoming = self.local('今天谁给我发消息了')
        self.assertEqual(incoming['statistics']['message_count'], 1)
        self.assertEqual(incoming['statistics']['sender_count'], 1)
        outgoing = self.local('我今天发了几条消息')
        self.assertEqual(outgoing['statistics']['message_count'], 1)
        split = self.local('今天收到和发出的消息分别有多少条')
        statistics = split['statistics']
        self.assertEqual((statistics['incoming_count'], statistics['outgoing_count'], statistics['unknown_direction_count']), (1, 1, 1))
        self.assertEqual(sum(row['message_count'] for row in statistics['rows']), 3)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.assistant.status()['knowledge']['direction_identity'], {'state': 'verified', 'verified': True})
        target = self.assistant.evidence('private_test', 2)
        self.assertTrue(next(row for row in target['messages'] if row['anchor'])['own'])

    def test_analysis_only_forwards_the_requested_direction(self):
        self.add('SYNTHETIC_INCOMING_BODY 采购确认。')
        self.add('SYNTHETIC_OUTGOING_BODY 采购确认。')
        self.store._connection.execute('UPDATE messages SET sender_id=? WHERE rowid=2', ('synthetic_self_sender',))
        result = self.ask_done(question='总结我发出的消息', time_range='7d')
        self.assertEqual(result['stage'], 'done')
        sent = json.dumps(self.provider.calls[-1]['messages'], ensure_ascii=False)
        self.assertIn('SYNTHETIC_OUTGOING_BODY', sent)
        self.assertNotIn('SYNTHETIC_INCOMING_BODY', sent)
        self.assertNotIn('synthetic_self_sender', sent)
        self.assertEqual(result['evidence'][0]['direction'], 'outgoing')


class DynamicScopeRoutingTests(unittest.TestCase):
    wait = fixtures.AiAssistantTests.wait

    def test_plan_summary_for_fifty_long_named_chats_does_not_expand_the_composer(self):
        catalog = {alias_for('synthetic-long-%d' % index): {'name': '合成超长会话名' * 30, 'kind': 'private', 'sender_names': {}}
                   for index in range(50)}
        with patch.object(self.assistant.knowledge, 'catalog', return_value=catalog):
            public = self.assistant._public_plan({'scope': list(catalog), 'metric': 'messages', 'time_label': '今天',
                                                  'time_range': {}, 'route': 'local_stats', 'requires_model': False})
        self.assertEqual(len(public['scope']), 50)
        self.assertIn('50', public['scope_label'])
        self.assertLess(len(public['summary']), 180)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-multi-ai-')
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'profile-a'
        self.private = self.data / 'private'
        self.store_dir = self.private / 'store'
        for directory in (self.data, self.private, self.store_dir):
            directory.mkdir(mode=0o700)
        chat_ids = ['synthetic-peer-0', 'synthetic-peer-1', 'synthetic-peer-2',
                    'synthetic-group-3@chatroom', 'synthetic-group-4@chatroom']
        self.chats = normalize_conversations({'conversations': [
            {'alias': alias_for(chat), 'chat_id': chat} for chat in chat_ids]})
        self.aliases = [row['alias'] for row in self.chats]
        self.by_chat = {row['chat_id']: row['alias'] for row in self.chats}
        self.store = CollectorStore(self.store_dir, {'account': 'SYNTHETIC_ACCOUNT_A', 'config': {'conversations': self.chats}})
        self.cursors = {'message/message_0.db': {alias: {'local_id': 0, 'present': True} for alias in self.aliases}}
        self.store.initialize(self.cursors, {})
        names = dict(zip(chat_ids, ['青禾客户', '星桥客户', '远帆客户', '交付讨论群', '订单确认群']))
        own = 'synthetic-profile-self'
        senders = {own: '合成本人', 'synthetic-group-member': '合成群成员',
                   **{chat: '合成联系人%d' % i for i, chat in enumerate(chat_ids[:3])}}
        proof_alias = self.by_chat[chat_ids[0]]
        catalog = {'schema_version': 1, 'binding_digest': self.store.binding_digest,
            'identity': establish_identity(self.store.binding_digest, [own], [own], self.chats,
                {proof_alias: [chat_ids[0], own]}),
            'conversations': [{'id': row['alias'], 'kind': 'group' if row['chat_id'].endswith('@chatroom') else 'private',
                               'name': names[row['chat_id']], 'sender_names': senders} for row in self.chats]}
        write_private_json_new(self.private / 'catalog.json', catalog)
        tick = datetime.now(timezone.utc)
        events = []
        for index, row in enumerate(self.chats):
            for own_message in (False, True):
                number = len(events) + 1
                sender = own if own_message else 'synthetic-group-member' if row['chat_id'].endswith('@chatroom') else row['chat_id']
                body = ('SYNTHETIC_SCOPE_%s_%s' % (row['chat_id'], 'outgoing' if own_message else 'incoming')) + ' 本轮原文。'
                events.append({'source_key': hashlib.sha256(('synthetic-multi-%d' % number).encode()).hexdigest(),
                    'alias': row['alias'], 'source_local_id': number, 'sent_at': int(tick.timestamp()),
                    'sender_id': sender, 'body': body, 'observed_at': tick.isoformat()})
        self.store.commit(self.cursors, self.cursors, events, {})
        self.provider = fixtures.FakeProvider()
        self.assistant = fixtures.service.AiAssistant(self.data, self.private / 'assistant', provider=self.provider)
        self.extra = []

    def tearDown(self):
        self.assistant.close()
        for assistant, store in self.extra:
            assistant.close(); store.close()
        self.store.close(); self.tmp.cleanup()

    def query(self, question, scope=None, **extra):
        return self.wait(self.assistant.ask({'question': question, 'scope': scope or self.aliases,
                                             'time_range': '7d', **extra}))

    def test_three_private_two_group_stats_and_received_direction_cover_all_selected_only(self):
        total = self.query('今天一共有多少条消息')
        self.assertEqual(total['statistics']['message_count'], 10)
        self.assertEqual(total['statistics']['conversation_count'], 5)
        incoming = self.query('今天谁给我发消息了')
        self.assertEqual(incoming['statistics']['message_count'], 5)
        self.assertEqual(incoming['statistics']['sender_count'], 4)
        self.assertTrue(all(row['direction'] == 'incoming' for row in incoming['evidence']))
        grouped = self.query('统计各会话消息数量')
        self.assertEqual(len(grouped['statistics']['rows']), 5)
        self.assertEqual(sum(row['message_count'] for row in grouped['statistics']['rows']), 10)
        named = self.query('青禾客户今天有多少条消息')
        self.assertEqual(named['plan']['scope'], [self.by_chat['synthetic-peer-0']])
        self.assertEqual(named['statistics']['message_count'], 2)
        self.assertEqual(self.provider.calls, [])

    def test_single_conversation_analysis_and_citation_never_include_other_selected_bodies(self):
        alias = self.by_chat['synthetic-peer-0']
        self.assistant.update_config({'base_url': 'https://synthetic-model.invalid/v1', 'model': 'synthetic-model',
                                     'key_action': 'replace', 'api_key': fixtures.SYNTHETIC_KEY})
        target = self.assistant.config()['target_fingerprint']
        result = self.query('分析沟通重点', scope=[alias], target_fingerprint=target, confirmed_target=target)
        self.assertEqual(result['stage'], 'done')
        self.assertTrue(all(row['conversation_id'] == alias for row in result['evidence']))
        sent = json.dumps(self.provider.calls[-1]['messages'], ensure_ascii=False)
        self.assertIn('SYNTHETIC_SCOPE_synthetic-peer-0', sent)
        for other in ('synthetic-peer-1', 'synthetic-peer-2', 'synthetic-group-3', 'synthetic-group-4'):
            self.assertNotIn('SYNTHETIC_SCOPE_' + other, sent)
        evidence = result['evidence'][0]
        context = self.assistant.evidence(alias, evidence['message_id'])
        self.assertTrue(all(row['conversation_id'] == alias for row in context['messages']))

    def test_valid_but_unbound_alias_cannot_be_queried_or_create_history(self):
        outside = alias_for('synthetic-outside-profile')
        before = self.assistant.list_threads()
        with self.assertRaises(QueryPlanError):
            self.assistant.preview_plan({'question': '消息多少条', 'scope': [outside], 'time_range': 'all'})
        with self.assertRaises(QueryPlanError):
            self.query('今天消息多少条', scope=[outside])
        with self.assertRaises(KnowledgeError):
            self.assistant.knowledge.lookup_message_context(outside, 1)
        self.assertEqual(self.assistant.list_threads(), before)
        self.assertEqual(self.provider.calls, [])

    def test_another_account_or_scope_binding_cannot_load_copied_ai_history(self):
        result = self.query('今天有多少条消息')
        directory = self.root / 'profile-b'
        private = directory / 'private'; store_dir = private / 'store'
        for path in (directory, private, store_dir):
            path.mkdir(mode=0o700)
        store = CollectorStore(store_dir, {'account': 'SYNTHETIC_ACCOUNT_B', 'config': {'conversations': self.chats}})
        store.initialize(self.cursors, {})
        source = json.loads((self.private / 'catalog.json').read_text())
        source['binding_digest'] = store.binding_digest
        source.pop('identity')
        write_private_json_new(private / 'catalog.json', source)
        other = fixtures.service.AiAssistant(directory, private / 'assistant', provider=fixtures.FakeProvider())
        self.extra.append((other, store))
        thread_id = result['thread_id']
        thread = json.loads((self.assistant.threads_dir / (thread_id + '.json')).read_text())
        write_private_json_new(other.threads_dir / (thread_id + '.json'), thread)
        with self.assertRaises(fixtures.service.AiError) as caught:
            other.get_thread(thread_id)
        self.assertEqual(caught.exception.code, 'ai_thread_binding_invalid')


if __name__ == '__main__':
    unittest.main()
