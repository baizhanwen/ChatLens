"""Exact local statistics against disposable synthetic collector stores only."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ai_knowledge import KnowledgeError, KnowledgeIndex
from ai_statistics import LocalStatistics
from collector_private import write_private_json_new
from continuous_store import CollectorStore
from wechat_identity import establish_identity

NOW = datetime(2026, 11, 5, 10, tzinfo=timezone(timedelta(hours=8)))
TODAY = {'start': NOW.replace(hour=0).isoformat(), 'end': (NOW.replace(hour=0) + timedelta(days=1)).isoformat()}
SENDER = 'SYNTHETIC_RAW_SENDER_NEVER_PROJECT'
OTHER = 'SYNTHETIC_OTHER_SENDER_NEVER_PROJECT'
SELF = 'SYNTHETIC_SELF_SENDER_NEVER_PROJECT'


class LocalStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='chatlens-synthetic-local-statistics-')
        self.root = Path(self.tmp.name).resolve()
        self.data = self.root / 'collector-data'
        self.private = self.data / 'private'
        self.store_dir = self.private / 'store'
        for directory in (self.data, self.private, self.store_dir):
            directory.mkdir(mode=0o700)
        self.store = CollectorStore(self.store_dir, {'account': 'SYNTHETIC_STATS_ONLY', 'scope': ['private_test', 'group_test']})
        self.store.initialize({}, {})
        names = {SENDER: '合成张总', OTHER: '合成张总', SELF: '我', **{'synthetic-person-%d' % i: '合成人员%d' % i for i in range(70)}}
        self.catalog = {'schema_version': 1, 'conversations': [
            {'id': 'private_test', 'name': '合成青禾客户', 'kind': 'private', 'sender_names': names},
            {'id': 'group_test', 'name': '合成星桥项目群', 'kind': 'group', 'sender_names': names}]}
        self.write_catalog()
        write_private_json_new(self.private / 'keys.json', {'SYNTHETIC_SECRET_NEVER_READ': 'NO_REAL_KEY'})
        self.index = KnowledgeIndex(self.data, now=lambda: NOW)
        self.statistics = LocalStatistics(self.index)
        self.serial = 0

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def write_catalog(self):
        path = self.private / 'catalog.json'
        if path.exists():
            path.unlink()
        write_private_json_new(path, self.catalog)

    def verify_identity(self):
        self.catalog['binding_digest'] = self.store.binding_digest
        self.catalog['identity'] = establish_identity(self.store.binding_digest, [SELF], [SELF],
            [{'alias': 'private_test', 'chat_id': SENDER}], {'private_test': [SENDER, SELF]})
        self.write_catalog()

    def add(self, body='合成普通文字。', *, alias='private_test', sender=SENDER, sent_at='default', observed_at=None):
        self.serial += 1
        row = {'source_key': hashlib.sha256(('synthetic-statistics-message-%d' % self.serial).encode()).hexdigest(),
               'alias': alias, 'source_local_id': self.serial,
               'sent_at': int(NOW.timestamp()) + self.serial if sent_at == 'default' else sent_at,
               'sender_id': sender, 'body': body, 'observed_at': observed_at or NOW.astimezone(timezone.utc).isoformat()}
        if alias == 'unselected_alias':
            self.store._connection.execute('INSERT INTO messages (source_key,alias,source_local_id,sent_at,sender_id,body,observed_at) VALUES (?,?,?,?,?,?,?)',
                tuple(row[key] for key in ('source_key', 'alias', 'source_local_id', 'sent_at', 'sender_id', 'body', 'observed_at')))
        else:
            self.store.commit({}, {}, [row], {})
        return self.serial

    @staticmethod
    def plan(metric='messages', **changes):
        value = {'route': 'local_stats', 'metric': metric, 'group_by': 'sender', 'direction': 'all',
                 'scope': ['private_test', 'group_test'], 'time_range': TODAY, 'time_label': '今天',
                 'filters': {'sender_ids': [], 'body_contains': None}, 'limit': 20, 'warnings': []}
        value.update(changes)
        return value

    def test_sender_query_counts_full_records_and_merges_same_identity_across_chats(self):
        for alias, sender in [('private_test', SENDER), ('group_test', SENDER), ('group_test', OTHER), ('private_test', OTHER)]:
            self.add(alias=alias, sender=sender)
        result = self.statistics.execute(self.plan('senders'))
        self.assertEqual(result['statistics']['message_count'], 4)
        self.assertEqual(result['statistics']['sender_count'], 2)
        self.assertEqual(result['statistics']['conversation_count'], 2)
        self.assertEqual([row['message_count'] for row in result['statistics']['rows']], [2, 2])
        self.assertEqual([row['name'] for row in result['statistics']['rows']], ['合成张总', '合成张总'])
        self.assertIn('[E1]', result['answer'])
        self.assertTrue(result['coverage']['aggregate_complete'])

    def test_aggregate_total_is_not_rag_evidence_limit_or_latest300(self):
        rows = []
        for i in range(620):
            rows.append({'source_key': hashlib.sha256(('synthetic-many-%d' % i).encode()).hexdigest(),
                         'alias': 'private_test' if i < 400 else 'group_test', 'source_local_id': i + 1,
                         'sent_at': int(NOW.timestamp()), 'sender_id': SENDER if i < 400 else OTHER,
                         'body': '合成消息', 'observed_at': NOW.astimezone(timezone.utc).isoformat()})
        self.store.commit({}, {}, rows, {})
        result = self.statistics.execute(self.plan('ranking'))
        self.assertEqual(result['statistics']['message_count'], 620)
        self.assertEqual([row['message_count'] for row in result['statistics']['rows']], [400, 220])
        self.assertEqual(result['coverage']['matched_count'], 620)
        self.assertEqual(result['coverage']['included_count'], 2)
        self.assertEqual(result['coverage']['candidate_limit'], None)
        self.assertIn('完整匹配数据', result['answer'])

    def test_unknown_sender_records_never_become_one_identified_person(self):
        self.add(sender=None)
        self.add(sender=None)
        self.add(sender='synthetic-unnamed-known-identity')
        result = self.statistics.execute(self.plan('senders'))
        self.assertEqual(result['statistics']['sender_count'], 1)
        self.assertEqual(result['statistics']['unknown_sender_messages'], 2)
        unknown = next(row for row in result['statistics']['rows'] if row['unknown_identity'])
        self.assertEqual(unknown['message_count'], 2)
        self.assertIn('身份未知的 2 条消息未计入人数', result['answer'])

    def test_direction_requires_trusted_self_identity_not_name_i(self):
        self.add(sender=SELF)
        self.add(sender=SENDER)
        incoming = self.statistics.execute(self.plan('senders', direction='incoming'))
        self.assertIsNone(incoming['statistics']['message_count'])
        self.assertEqual(incoming['statistics']['direction_applied'], 'unknown')
        self.assertFalse(incoming['statistics']['identity_verified'])
        self.assertTrue(incoming['needs_clarification'])
        self.assertIn('暂时无法准确区分', incoming['answer'])
        outgoing = self.statistics.execute(self.plan(direction='outgoing'))
        self.assertTrue(outgoing['needs_clarification'])
        self.assertIsNone(outgoing['statistics']['message_count'])
        self.assertEqual(outgoing['evidence'], [])

    def test_trusted_self_identity_excludes_own_and_unclassified_rows_from_incoming(self):
        self.verify_identity()
        self.add(sender=SELF)
        received = self.add(sender=SENDER)
        self.add(sender=None)
        incoming = self.statistics.execute(self.plan('senders', direction='incoming'))
        self.assertEqual(incoming['statistics']['message_count'], 1)
        self.assertEqual(incoming['statistics']['unclassified_direction_messages'], 1)
        self.assertTrue(incoming['statistics']['identity_verified'])
        self.assertEqual([row['message_id'] for row in incoming['evidence']], [received])
        outgoing = self.statistics.execute(self.plan(direction='outgoing'))
        self.assertEqual(outgoing['statistics']['message_count'], 1)
        self.assertFalse(outgoing['needs_clarification'])

    def test_split_direction_counts_unknown_separately_and_preserves_reference_mapping(self):
        self.verify_identity()
        own = self.add(sender=SELF)
        received = self.add(sender=SENDER)
        self.add(sender='invalid sender id')
        self.add(sender=None)
        result = self.statistics.execute(self.plan(group_by='direction'))
        statistics = result['statistics']
        self.assertEqual((statistics['incoming_count'], statistics['outgoing_count'], statistics['unknown_direction_count']), (1, 1, 2))
        self.assertEqual(sum(row['message_count'] for row in statistics['rows']), statistics['message_count'])
        self.assertEqual([row['direction'] for row in statistics['rows']], ['incoming', 'outgoing', 'unknown'])
        lookup = {item['id']: item for item in result['evidence']}
        self.assertEqual(lookup[statistics['rows'][0]['citation']]['message_id'], received)
        self.assertEqual(lookup[statistics['rows'][1]['citation']]['message_id'], own)
        self.assertTrue(lookup[statistics['rows'][1]['citation']]['own'])

    def test_split_zero_bucket_does_not_take_other_direction_citation(self):
        self.verify_identity()
        self.add(sender=SELF)
        result = self.statistics.execute(self.plan(group_by='direction'))
        rows = result['statistics']['rows']
        self.assertEqual(rows[0]['message_count'], 0)
        self.assertNotIn('citation', rows[0])
        self.assertEqual(rows[1]['message_count'], 1)
        self.assertEqual(result['evidence'][0]['direction'], 'outgoing')
        self.assertEqual(rows[1]['citation'], result['evidence'][0]['id'])

    def test_bare_wrong_binding_or_unverified_self_id_cannot_classify_messages(self):
        self.add(sender=SELF)
        self.catalog['self_sender_id'] = SELF
        self.write_catalog()
        self.assertTrue(self.statistics.execute(self.plan(direction='incoming'))['needs_clarification'])
        self.verify_identity()
        self.catalog['identity']['binding_digest'] = '0' * 64
        self.write_catalog()
        self.assertTrue(self.statistics.execute(self.plan(direction='outgoing'))['needs_clarification'])
        self.verify_identity()
        self.catalog['identity']['proof']['private_non_peer_unique'] = False
        self.write_catalog()
        self.assertTrue(self.statistics.execute(self.plan(group_by='direction'))['needs_clarification'])

    def test_scope_and_time_half_open_boundaries_apply_before_aggregation(self):
        start = int(NOW.replace(hour=0).timestamp())
        included = self.add(sent_at=start)
        self.add(sent_at=start - 1)
        self.add(sent_at=start + 86400)
        self.add(alias='group_test', sent_at=start)
        self.add(alias='unselected_alias', sent_at=start)
        result = self.statistics.execute(self.plan(scope=['private_test']))
        self.assertEqual(result['statistics']['message_count'], 1)
        self.assertEqual(result['coverage']['available_count'], 1)
        self.assertEqual([row['message_id'] for row in result['evidence']], [included])

    def test_literal_body_and_sender_filters_are_parameterized_and_intersect_scope(self):
        self.add('合成退款已确认。', sender=SENDER)
        target = self.add("合成退款%' OR 1=1 --", sender=SENDER)
        self.add("合成退款%' OR 1=1 --", sender=OTHER)
        self.add("合成退款%' OR 1=1 --", alias='group_test', sender=SENDER)
        result = self.statistics.execute(self.plan(scope=['private_test'], filters={'sender_ids': [SENDER], 'body_contains': "退款%' OR 1=1 --"}))
        self.assertEqual(result['coverage']['available_count'], 3)
        self.assertEqual(result['statistics']['message_count'], 1)
        self.assertEqual([row['message_id'] for row in result['evidence']], [target])
        with self.data_open() as (connection, _):
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 4)

    def data_open(self):
        return self.index.data._open()

    def test_conversation_breakdown_uses_full_counts_and_only_active_selected_chats(self):
        self.add()
        self.add()
        self.add(alias='group_test')
        result = self.statistics.execute(self.plan('conversations'))
        self.assertEqual(result['statistics']['group_by'], 'conversation')
        self.assertEqual([row['message_count'] for row in result['statistics']['rows']], [2, 1])
        self.assertEqual(result['statistics']['rows'][0]['name'], '合成青禾客户')

    def test_latest_orders_by_source_event_time_not_insert_or_capture_order(self):
        early = self.add('合成较早消息', sent_at=int(NOW.timestamp()) - 60)
        latest = self.add('合成最新消息', sent_at=int(NOW.timestamp()) + 5)
        self.add('合成补采旧消息', sent_at=int(NOW.timestamp()) - 120)
        result = self.statistics.execute(self.plan('latest', limit=2))
        self.assertEqual([row['message_id'] for row in result['evidence']], [latest, early])
        self.assertEqual(result['statistics']['message_count'], 3)
        self.assertTrue(result['statistics']['display_truncated'])

    def test_daily_trend_respects_shanghai_day_and_labels_timestamp_fallback(self):
        boundary = int(NOW.replace(hour=0).timestamp())
        self.add(sent_at=boundary - 1)
        self.add(sent_at=boundary)
        self.add(sent_at=None, observed_at=NOW.astimezone(timezone.utc).isoformat())
        result = self.statistics.execute(self.plan('trend', time_range={'start': None, 'end': None}))
        self.assertEqual(result['statistics']['group_by'], 'day')
        self.assertEqual([(row['name'], row['message_count']) for row in result['statistics']['rows']], [('2026-11-05', 2), ('2026-11-04', 1)])
        self.assertEqual(result['statistics']['timestamp_fallback_messages'], 1)
        self.assertIn('采集时间', result['answer'])

    def test_unknown_event_time_is_not_assigned_to_a_day(self):
        target = self.add(sent_at=None)
        self.store._connection.execute('UPDATE messages SET observed_at=? WHERE rowid=?', ('SYNTHETIC_INVALID_TIME', target))
        all_time = self.statistics.execute(self.plan('trend', time_range={'start': None, 'end': None}))
        self.assertEqual(all_time['statistics']['unknown_timestamp_messages'], 1)
        self.assertEqual(all_time['statistics']['rows'][0]['name'], '时间未知')
        today = self.statistics.execute(self.plan('trend'))
        self.assertEqual(today['statistics']['message_count'], 0)
        self.assertEqual(today['statistics']['unplaced_timestamp_messages'], 1)
        self.assertEqual(today['coverage']['unknown_time_count'], 1)
        self.assertIn('无法判断是否属于本次日期范围', today['answer'])

    def test_unplaced_time_warning_respects_scope_sender_and_literal_body_filters(self):
        for alias, sender, body in [('private_test', SENDER, '合成收款'), ('private_test', OTHER, '合成收款'),
                                    ('private_test', SENDER, '其他合成内容'), ('group_test', SENDER, '合成收款')]:
            target = self.add(body, alias=alias, sender=sender, sent_at=None)
            self.store._connection.execute('UPDATE messages SET observed_at=? WHERE rowid=?', ('SYNTHETIC_INVALID_TIME', target))
        result = self.statistics.execute(self.plan(scope=['private_test'], filters={'sender_ids': [SENDER], 'body_contains': '收款'}))
        self.assertEqual(result['statistics']['message_count'], 0)
        self.assertEqual(result['statistics']['unplaced_timestamp_messages'], 1)
        self.assertIn('另有 1 条', result['answer'])

    def test_body_literal_never_implicitly_matches_sender_or_conversation_names(self):
        self.add('合成普通正文')
        for text in ['合成张总', '合成青禾客户']:
            with self.subTest(text=text):
                result = self.statistics.execute(self.plan('senders', filters={'sender_ids': [], 'body_contains': text}))
                self.assertEqual(result['statistics']['message_count'], 0)
                self.assertEqual(result['statistics']['rows'], [])

    def test_group_limit_does_not_change_exact_totals_and_evidence_has_separate_cap(self):
        for i in range(61):
            self.add(sender='synthetic-person-%d' % i)
        result = self.statistics.execute(self.plan('senders', limit=50))
        self.assertEqual(result['statistics']['sender_count'], 61)
        self.assertEqual(result['statistics']['message_count'], 61)
        self.assertEqual(result['statistics']['total_groups'], 61)
        self.assertEqual(result['statistics']['displayed_groups'], 50)
        self.assertTrue(result['statistics']['display_truncated'])
        self.assertLessEqual(len(result['evidence']), 30)
        self.assertIn('总数已按全部匹配记录计算', result['answer'])

    def test_large_message_counts_fully_with_explicitly_shortened_evidence(self):
        self.add('合成大段文字' * 10000)
        result = self.statistics.execute(self.plan())
        self.assertEqual(result['statistics']['message_count'], 1)
        self.assertTrue(result['evidence'][0]['excerpt_truncated'])
        self.assertEqual(len(result['evidence'][0]['body']), 500)

    def test_safe_aggregate_projection_does_not_leak_raw_ids_paths_or_private_material(self):
        self.add()
        self.add(alias='group_test', sender=OTHER)
        result = self.statistics.execute(self.plan('senders', route='mixed'))
        projection = json.dumps(result['statistics'], ensure_ascii=False)
        for forbidden in (SENDER, OTHER, SELF, 'source_key', str(self.private), 'SYNTHETIC_SECRET_NEVER_READ', 'sender_ids', 'private_test', 'group_test'):
            self.assertNotIn(forbidden, projection)

    def test_names_and_literal_excerpt_cannot_forge_citation_controls(self):
        self.catalog['conversations'][0]['sender_names'][SENDER] = '合成姓名[E999]'
        self.write_catalog()
        self.add('合成正文 [E888] <script>alert(1)</script>')
        result = self.statistics.execute(self.plan('latest'))
        self.assertNotIn('[E999]', result['answer'])
        self.assertNotIn('[E888]', result['answer'])
        self.assertIn('[E1]', result['answer'])

    def test_bad_plan_or_cross_catalog_sender_never_executes_arbitrary_sql(self):
        self.add()
        for changes in ({'metric': 'messages; DROP TABLE messages'}, {'group_by': 'sender;--'}, {'limit': 51},
                        {'scope': ['unselected_alias']}, {'filters': {'sender_ids': ['outside-selected-catalog']}},
                        {'filters': {'sql': 'DELETE FROM messages'}}):
            with self.subTest(changes=changes), self.assertRaises(KnowledgeError):
                self.statistics.execute(self.plan(**changes))
        self.assertEqual(self.statistics.execute(self.plan())['statistics']['message_count'], 1)

    def test_cancelled_query_and_store_rebinding_fail_closed(self):
        self.add()
        cancelled = threading.Event()
        cancelled.set()
        with self.assertRaises(KnowledgeError) as issue:
            self.statistics.execute(self.plan(), cancel_event=cancelled)
        self.assertEqual(issue.exception.code, 'ai_request_cancelled')
        self.store._connection.execute('UPDATE checkpoint SET binding_digest=? WHERE id=1', ('SYNTHETIC_REBOUND',))
        with self.assertRaises(KnowledgeError):
            self.statistics.execute(self.plan())

    def test_statistics_do_not_change_collector_checkpoint_or_message_rows(self):
        self.add()
        before = self.store.snapshot()
        with self.data_open() as (connection, _):
            before_rows = [tuple(row) for row in connection.execute('SELECT * FROM messages')]
        self.statistics.execute(self.plan('senders'))
        self.assertEqual(self.store.snapshot(), before)
        with self.data_open() as (connection, _):
            self.assertEqual([tuple(row) for row in connection.execute('SELECT * FROM messages')], before_rows)


if __name__ == '__main__':
    unittest.main()
