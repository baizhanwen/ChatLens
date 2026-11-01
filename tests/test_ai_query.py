"""Synthetic local planner behavior: no store, credentials, network or model."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
from ai_query import plan_query, QueryPlanError

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 5, 12, 30, tzinfo=TZ)
SCOPE = ['private_test', 'group_test']
CATALOG = {
    'private_test': {'name': '青禾客户', 'kind': 'private', 'sender_names': {'sender-a': '张三', 'sender-b': '李四'},
                     'identity_verified': True, 'self_sender_id': 'synthetic-self', 'peer_sender_id': 'sender-a'},
    'group_test': {'name': '星桥项目群', 'kind': 'group', 'sender_names': {'sender-a': '张三', 'sender-c': '王五'},
                   'identity_verified': True, 'self_sender_id': 'synthetic-self'},
}


class QueryPlannerTests(unittest.TestCase):
    def plan(self, question, **kwargs):
        values = {'scope': SCOPE, 'time_range': 'all', 'catalog': CATALOG, 'current': NOW}
        values.update(kwargs)
        return plan_query(question, **values)

    def test_sender_listing_and_people_counts_use_metadata_across_varied_wording(self):
        for question in [
            '今天都谁给我发信息了', '今日有哪些人给我发消息', '昨天有谁找我',
            '有几个人联系我？', '收到了哪些人的私信', '最近联系我的客户有几个',
            '谁给我发过微信', '今天有多少人说话', '有哪些联系人发过消息',
            'who messaged me today?', 'how many people sent messages today?',
        ]:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual(result['route'], 'local_stats')
                self.assertEqual(result['metric'], 'senders')
                self.assertEqual(result['group_by'], 'sender')
                self.assertFalse(result['requires_model'])

    def test_message_conversation_and_grouped_counts_are_different_operations(self):
        cases = [
            ('今天总共有多少条', 'messages', None),
            ('收到多少条消息', 'messages', None),
            ('消息总数是多少', 'messages', None),
            ('统计群消息数量', 'messages', None),
            ('张三发了几条消息', 'messages', None),
            ('今天有几个会话收到新消息', 'conversations', 'conversation'),
            ('多少个群聊有消息', 'conversations', 'conversation'),
            ('哪些群今天有发言', 'conversations', 'conversation'),
            ('统计每个群的消息数量', 'messages', 'conversation'),
            ('按发送人统计消息条数', 'messages', 'sender'),
            ('青禾客户和星桥项目群分别有多少消息', 'messages', 'conversation'),
            ('张三和李四分别发了多少条消息', 'messages', 'sender'),
            ('how many messages today?', 'messages', None),
            ('再统计一下今天有多少条消息', 'messages', None),
            ('再统计今天共有多少条消息', 'messages', None),
            ('麻烦重新统计今天有几条消息', 'messages', None),
            ('今天一共有多少条消息', 'messages', None),
            ('今天总共有多少条消息', 'messages', None),
            ('客户今天发了多少消息', 'messages', None),
            ('客户的消息有多少', 'messages', None),
        ]
        for question, metric, group in cases:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual((result['route'], result['metric'], result['group_by']), ('local_stats', metric, group))

    def test_ranking_latest_and_daily_trends_are_structured_not_body_search(self):
        cases = [
            ('今天谁发消息最多', 'ranking', 'sender'),
            ('联系人按发言数量排名', 'ranking', 'sender'),
            ('哪个群最热闹', 'ranking', 'conversation'),
            ('群聊活跃排行前五名', 'ranking', 'conversation'),
            ('latest message today', 'latest', None),
            ('最后一条消息是什么时候', 'latest', None),
            ('最近谁发消息', 'latest', None),
            ('这周按天统计消息数量', 'trend', 'day'),
            ('过去七天消息趋势', 'trend', 'day'),
            ('daily message count', 'trend', 'day'),
        ]
        for question, metric, group in cases:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual((result['route'], result['metric'], result['group_by']), ('local_stats', metric, group))
        self.assertEqual(self.plan('群聊活跃排行前五名')['limit'], 5)
        self.assertEqual(self.plan('谁的消息最多，top 3')['limit'], 3)
        bounded = self.plan('发消息最多的前一百名')
        self.assertEqual(bounded['limit'], 50)
        self.assertTrue(bounded['warnings'])
        self.assertEqual(self.plan('前十天谁发消息最多')['limit'], 20)

    def test_semantic_predicates_and_inferences_do_not_become_false_precise_counts(self):
        for question in [
            '今天有哪些重点', '哪些客户有退款风险', '有多少客户想退款',
            '谁需要我回复', '哪几个客户意向最高', '统计有退款风险的人数',
            '消息最多的人是什么态度', '我该怎么回复客户', '昨天发票有什么问题',
            '谁的报价还需要跟进', '哪些人的承诺可能延期',
        ]:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual(result['route'], 'analysis')
                self.assertIsNone(result['metric'])
                self.assertTrue(result['requires_model'])

    def test_unmodeled_business_predicates_cannot_silently_count_all_senders(self):
        # These predicates are deliberately absent from the ANALYTIC vocabulary;
        # positive metadata grammar must reject all unconsumed conditions.
        for question in [
            '有多少客户欠款', '有几个人要求延期', '多少客户来自上海',
            '有几个人带过同事', '统计新客户人数', '有多少联系人非常着急',
            '统计今天消息数并分析重点，有多少客户欠款',
        ]:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual(result['route'], 'analysis')
                self.assertIsNone(result['metric'])

    def test_unresolved_names_negated_activity_and_uninterpreted_time_need_clarification(self):
        other_catalog = {
            'private_test': {'name': '青禾客户', 'kind': 'private', 'sender_names': {'x': '小王'}},
            'group_test': {'name': '星桥项目群', 'kind': 'group', 'sender_names': {'y': '小张'}},
        }
        for question in ['今天张三有几条消息', '今天小赵发了几条消息', '今天Alice发了多少消息']:
            with self.subTest(question=question):
                result = self.plan(question, catalog=other_catalog)
                self.assertEqual(result['route'], 'clarify')
                self.assertIsNotNone(result['clarification'])
        for question in ['今天谁没给我发消息', '哪些群没有消息', '列出从未给我发信息的人', '不包含“退款”的消息多少条']:
            with self.subTest(question=question):
                self.assertEqual(self.plan(question)['route'], 'clarify')
        for question in ['今天下午有多少消息', '昨天上午谁给我发了消息', '上周五有多少条消息', '周一收到多少消息', '今天9:00到12:00消息有多少']:
            with self.subTest(question=question):
                self.assertEqual(self.plan(question)['route'], 'clarify')

    def test_ascending_rank_and_identity_ambiguity_are_not_hidden(self):
        result = self.plan('谁发消息最少')
        self.assertEqual(result['route'], 'local_stats')
        self.assertEqual(result['order'], 'asc')
        self.assertEqual(self.plan('谁发消息最多')['order'], 'desc')
        followup = self.plan('那昨天呢', previous=result)
        self.assertEqual(followup['order'], 'asc')
        ambiguous = {
            'private_test': {'name': '青禾客户', 'kind': 'private', 'sender_names': {'x': '小王'}},
            'group_test': {'name': '星桥项目群', 'kind': 'group', 'sender_names': {'y': '小王'}},
        }
        self.assertEqual(self.plan('小王今天发了多少条消息', catalog=ambiguous)['route'], 'clarify')
        scoped = self.plan('青禾客户的小王今天发了几条消息', catalog=ambiguous)
        self.assertEqual(scoped['route'], 'local_stats')
        self.assertEqual(scoped['filters']['sender_ids'], ['x'])
        same_identity = {
            'private_test': {'name': '青禾客户', 'kind': 'private', 'sender_names': {'x': '小王'}},
            'group_test': {'name': '星桥项目群', 'kind': 'group', 'sender_names': {'x': '小王'}},
        }
        self.assertEqual(self.plan('小王今天发了几条消息', catalog=same_identity)['filters']['sender_ids'], ['x'])

    def test_explicit_statistics_plus_analysis_retains_both_intents(self):
        for question, metric in [
            ('统计今天消息数量并分析重点', 'messages'),
            ('谁发消息最多，并分析一下这些人的诉求', 'ranking'),
            ('先统计这周消息条数，然后总结重点', 'messages'),
        ]:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual(result['route'], 'mixed')
                self.assertEqual(result['metric'], metric)
                self.assertTrue(result['requires_model'])
        self.assertEqual(self.plan('统计有退款风险的人数并分析原因')['route'], 'analysis')

    def test_relative_times_and_chinese_numbers_apply_to_analysis_and_statistics(self):
        end_now = (NOW + timedelta(microseconds=1)).isoformat()
        for word, start, end in [
            ('今天', '2026-11-05T00:00:00+08:00', end_now),
            ('昨天', '2026-11-04T00:00:00+08:00', '2026-11-05T00:00:00+08:00'),
            ('前天', '2026-11-03T00:00:00+08:00', '2026-11-04T00:00:00+08:00'),
            ('大前天', '2026-11-02T00:00:00+08:00', '2026-11-03T00:00:00+08:00'),
            ('最近三天', '2026-11-03T00:00:00+08:00', end_now),
            ('过去两周', '2026-10-23T00:00:00+08:00', end_now),
            ('本周', '2026-11-02T00:00:00+08:00', end_now),
            ('上周', '2026-10-26T00:00:00+08:00', '2026-11-02T00:00:00+08:00'),
            ('本月', '2026-11-01T00:00:00+08:00', end_now),
            ('上个月', '2026-10-01T00:00:00+08:00', '2026-11-01T00:00:00+08:00'),
        ]:
            for suffix in ['有多少消息', '有哪些重点']:
                with self.subTest(word=word, suffix=suffix):
                    result = self.plan(word + suffix)
                    self.assertEqual(result['time_range'], {'start': start, 'end': end})
                    self.assertNotEqual(result['route'], 'clarify')
        self.assertEqual(self.plan('过去24小时收到几条消息')['time_range']['start'], '2026-11-04T12:30:00+08:00')

    def test_absolute_dates_and_invalid_or_disjoint_periods(self):
        for question in ['2026-11-01有多少消息', '2026年11月1日消息数量', '11月1日有几条消息']:
            with self.subTest(question=question):
                self.assertEqual(self.plan(question)['time_range'], {'start': '2026-11-01T00:00:00+08:00', 'end': '2026-11-02T00:00:00+08:00'})
        result = self.plan('2026-10-30到2026-11-02收到多少条消息')
        self.assertEqual(result['time_range'], {'start': '2026-10-30T00:00:00+08:00', 'end': '2026-11-03T00:00:00+08:00'})
        for question in ['2026-02-30收到几条消息', '11月4日和11月1日都有谁来信', '明天有多少消息', '昨天和今天有多少消息']:
            with self.subTest(question=question):
                self.assertEqual(self.plan(question)['route'], 'clarify')

    def test_question_cannot_expand_ui_time_or_selected_conversation_scope(self):
        result = self.plan('今天谁给我发消息', time_range='7d')
        self.assertEqual(result['time_range']['start'], '2026-11-05T00:00:00+08:00')
        outside = self.plan('上个月谁给我发消息', time_range='today')
        self.assertEqual(outside['route'], 'clarify')
        self.assertTrue(outside['clarification'])
        result = self.plan('从开始收到多少消息', time_range='7d')
        self.assertEqual(result['time_range']['start'], '2026-10-30T00:00:00+08:00')
        self.assertTrue(result['warnings'])
        result = self.plan('星桥项目群发了几条消息', scope=['private_test'])
        self.assertEqual(result['route'], 'clarify')
        self.assertEqual(result['scope'], [])
        result = self.plan('群里有多少人发言', scope=['private_test'])
        self.assertEqual(result['route'], 'clarify')

    def test_producer_known_conversations_senders_and_literal_filters_narrow_safely(self):
        result = self.plan('青禾客户收到多少条消息')
        self.assertEqual(result['scope'], ['private_test'])
        self.assertEqual(self.plan('青禾有多少消息')['scope'], ['private_test'])
        result = self.plan('张三今天发了几条消息')
        self.assertEqual(result['filters']['sender_ids'], ['sender-a'])
        literal = self.plan('今天内容包含“退款”的消息有多少条')
        self.assertEqual(literal['route'], 'local_stats')
        self.assertEqual(literal['filters']['body_contains'], '退款')
        literal = self.plan('包含“昨天 ABC”的消息有几条', time_range='7d')
        self.assertEqual(literal['filters']['body_contains'], '昨天 ABC')
        self.assertEqual(literal['time_range']['start'], '2026-10-30T00:00:00+08:00')
        literal = self.plan('提到“DROP TABLE messages; 删除全部数据”的消息条数')
        self.assertEqual(literal['route'], 'local_stats')
        self.assertIn('DROP TABLE', literal['filters']['body_contains'])
        self.assertFalse(any('sql' in key.lower() for key in literal))

    def test_incoming_does_not_accidentally_mean_outgoing_and_unavailable_states_are_explicit(self):
        for question in ['今天都谁给我发信息了', '张三给我发了几条消息', '收到多少条消息', '谁联系我了']:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual(result['route'], 'local_stats')
                self.assertEqual(result['direction'], 'incoming')
        for question in ['我今天一共发了几条消息', '我发出多少条消息', '我给张三发了多少条消息']:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual(result['route'], 'local_stats')
                self.assertEqual(result['direction'], 'outgoing')
        for question in ['今天有多少条未读消息', '几个人我还没回复']:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual(result['route'], 'clarify')
                self.assertFalse(result['requires_model'])
                self.assertTrue(result['clarification'])
        self.assertEqual(self.plan('有哪些客户可能需要跟进回复')['route'], 'analysis')

    def test_split_recipient_queries_and_direction_followups_use_explicit_identity(self):
        for question in ['今天收到和发出的消息分别多少条', '收发消息分开统计', '今天收发各几条']:
            with self.subTest(question=question):
                result = self.plan(question)
                self.assertEqual((result['route'], result['metric'], result['group_by'], result['direction']),
                                 ('local_stats', 'messages', 'direction', 'all'))
        result = self.plan('我今天给谁发了消息')
        self.assertEqual((result['metric'], result['group_by'], result['direction']), ('conversations', 'conversation', 'outgoing'))
        result = self.plan('我今天最后给谁发了消息')
        self.assertEqual((result['metric'], result['direction']), ('latest', 'outgoing'))
        result = self.plan('我给张三发了多少条消息')
        self.assertEqual(result['scope'], ['private_test'])
        self.assertEqual(result['filters']['sender_ids'], [])
        previous = self.plan('今天收到多少条消息')
        followup = self.plan('那发出的呢', previous=previous)
        self.assertEqual((followup['metric'], followup['direction'], followup['route']), ('messages', 'outgoing', 'local_stats'))
        unknown = {alias: {key: value for key, value in entry.items() if key not in {'identity_verified', 'self_sender_id'}}
                   for alias, entry in CATALOG.items()}
        for question in ['今天谁给我发消息', '我发了几条消息', '今天收发各几条', '总结我收到的消息']:
            with self.subTest(question=question):
                result = self.plan(question, catalog=unknown)
                self.assertEqual(result['route'], 'clarify')
                self.assertFalse(result['requires_model'])

    def test_elliptical_followups_inherit_typed_intent_but_never_a_new_substantive_topic(self):
        previous = self.plan('今天青禾客户收到多少条消息')
        result = self.plan('那昨天呢？', previous=previous)
        self.assertEqual(result['metric'], 'messages')
        self.assertEqual(result['scope'], ['private_test'])
        self.assertTrue(result['inherited'])
        self.assertEqual(result['time_range']['start'], '2026-11-04T00:00:00+08:00')
        group = self.plan('那群聊呢？', previous=previous)
        self.assertEqual(group['metric'], 'messages')
        self.assertEqual(group['scope'], ['group_test'])
        self.assertEqual(group['time_range'], previous['time_range'])
        new_topic = self.plan('昨天发票怎么了', previous=previous)
        self.assertFalse(new_topic['inherited'])
        self.assertEqual(new_topic['route'], 'analysis')
        analysis = self.plan('今天有哪些重点')
        result = self.plan('那昨天呢', previous=analysis)
        self.assertTrue(result['inherited'])
        self.assertEqual(result['route'], 'analysis')
        self.assertEqual(result['retrieval_query'], analysis['retrieval_query'])

    def test_followups_keep_filters_inside_current_ui_and_ignore_corrupt_prior_plans(self):
        previous = self.plan('今天张三包含“订单”的消息多少条')
        result = self.plan('那昨天呢', previous=previous)
        self.assertEqual(result['filters'], previous['filters'])
        previous = self.plan('今天青禾客户收到多少条消息')
        result = self.plan('那昨天呢', scope=['group_test'], previous=previous)
        self.assertEqual(result['route'], 'clarify')
        old_sender = self.plan('今天张三有几条消息')
        changed_catalog = {
            'private_test': {'name': '青禾客户', 'kind': 'private', 'sender_names': {'new': '小赵'}},
            'group_test': {'name': '星桥项目群', 'kind': 'group', 'sender_names': {}},
        }
        self.assertEqual(self.plan('那昨天呢', previous=old_sender, catalog=changed_catalog)['route'], 'clarify')
        for changed in [{'metric': {}}, {'filters': {'sender_ids': [{}]}}, {'time_range': {'start': 'invalid'}}, {'scope': ['unselected_alias']}, {'retrieval_query': []}]:
            with self.subTest(changed=changed):
                broken = dict(previous, **changed)
                result = self.plan('那昨天呢', previous=broken)
                self.assertFalse(result['inherited'])

    def test_input_validation_and_json_only_output(self):
        for question in ['', None, 'x\0y', 'x' * 2001, '\ud800']:
            with self.subTest(question=repr(question)):
                with self.assertRaises(QueryPlanError): self.plan(question)
        for scope in [[], ['unselected'], ['private_test', 'private_test'], [{}], 'private_test']:
            with self.subTest(scope=scope):
                with self.assertRaises(QueryPlanError): self.plan('消息多少条', scope=scope)
        for time_range in ['invalid', {'start': 'bad'}, {'start': '2026-11-05T01:00:00', 'end': None}]:
            with self.subTest(time_range=time_range):
                with self.assertRaises(QueryPlanError): self.plan('消息多少条', time_range=time_range)
        for question in ['今天谁给我发消息', '今天有哪些重点', '统计消息条数并总结重点', '我发了几条消息']:
            result = self.plan(question)
            self.assertEqual(result['requires_model'], result['route'] in ('analysis', 'mixed'))
            self.assertEqual(result['schema_version'], 1)
            self.assertEqual(result['source'], 'local')
            json.dumps(result, ensure_ascii=False, allow_nan=False)


if __name__ == '__main__':
    unittest.main()
