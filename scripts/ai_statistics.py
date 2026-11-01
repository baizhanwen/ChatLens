"""Exact, bounded, local statistics over the selected collector store.

The executor accepts a small declarative plan, never SQL or model instructions.
All aggregates run over the full matching snapshot. Evidence cards are bounded
representative examples and are explicitly independent of the aggregate totals.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import sqlite3
import time

from ai_knowledge import EVENT_TIME, VALID_SENT, KnowledgeError, MAX_CONTEXT_CHARS, MAX_EVIDENCE
from web_data import WebDataError
from wechat_identity import trusted_self_identity, valid_sender

LOCAL_TIMEZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')
METRICS = {'messages', 'senders', 'conversations', 'ranking', 'latest', 'trend'}
GROUPS = {'sender', 'conversation', 'day', 'direction'}
EVIDENCE_COLUMNS = ('rowid,alias,sender_id,sent_at,observed_at,typeof(body) AS body_type,'
                    'length(body) AS body_chars,substr(body,1,500) AS body')


def _display(value, maximum=80):
    """Names/content are data; prevent them from forging local citation labels."""
    value = ' '.join(str(value or '').split())
    value = re.sub(r'\[(E[0-9]+)\]', r'［\1］', value)
    return value if len(value) <= maximum else value[:maximum - 1] + '…'


class LocalStatistics:
    def __init__(self, index):
        self.index = index
        self.data = index.data

    def _plan(self, plan):
        if not isinstance(plan, dict):
            raise KnowledgeError('statistics_plan_invalid', 400)
        metric, group = plan.get('metric'), plan.get('group_by', 'sender')
        direction, limit = plan.get('direction', 'all'), plan.get('limit', 20)
        order = plan.get('order', 'desc')
        if (plan.get('route') not in {'local_stats', 'mixed'} or metric not in METRICS
                or (group not in GROUPS and not (group is None and metric in {'messages', 'latest'}))
                or direction not in {'all', 'incoming', 'outgoing'} or order not in {'asc', 'desc'}
                or group == 'direction' and metric != 'messages'
                or type(limit) is not int or not 1 <= limit <= 50):
            raise KnowledgeError('statistics_plan_invalid', 400)
        scope = self.index._scope(plan.get('scope'))
        normalized, seconds = self.index._range(plan.get('time_range'))
        filters = plan.get('filters', {})
        if not isinstance(filters, dict) or set(filters) - {'sender_ids', 'body_contains'}:
            raise KnowledgeError('statistics_plan_invalid', 400)
        senders, body = filters.get('sender_ids', []), filters.get('body_contains')
        if (not isinstance(senders, list) or len(senders) > 256
                or any(not isinstance(value, str) or not value or len(value) > 256 or '\0' in value for value in senders)
                or len(senders) != len(set(senders))
                or body is not None and (not isinstance(body, str) or not body or len(body) > 500 or '\0' in body)):
            raise KnowledgeError('statistics_plan_invalid', 400)
        return metric, group, direction, limit, scope, normalized, seconds, senders, body, order

    @staticmethod
    def _totals(connection, where, parameters):
        return dict(connection.execute(
            'SELECT COUNT(*) AS message_count,'
            "COUNT(DISTINCT CASE WHEN chatlens_sender_valid(sender_id)=1 THEN sender_id END) AS sender_count,"
            'COUNT(DISTINCT alias) AS conversation_count,'
            "COALESCE(SUM(CASE WHEN chatlens_sender_valid(sender_id)=0 THEN 1 ELSE 0 END),0) AS unknown_sender_messages,"
            'COALESCE(SUM(CASE WHEN ' + EVENT_TIME + ' IS NULL THEN 1 ELSE 0 END),0) AS unknown_timestamp_messages,'
            'COALESCE(SUM(CASE WHEN NOT (' + VALID_SENT + ') AND julianday(observed_at) IS NOT NULL THEN 1 ELSE 0 END),0) AS timestamp_fallback_messages,'
            "COALESCE(SUM(CASE WHEN typeof(body)<>'text' THEN 1 ELSE 0 END),0) AS invalid_body_count "
            'FROM messages WHERE ' + where, parameters).fetchone())

    def execute(self, plan, *, cancel_event=None):
        metric, group, direction, limit, scope, normalized, seconds, senders, body, sort_order = self._plan(plan)
        where, parameters = self.index._where(scope, seconds)
        untimed_where, untimed_parameters = self.index._where(scope, {'start': None, 'end': None})
        source = self.data._metadata('catalog.json')
        started = time.monotonic()
        cancelled = lambda: cancel_event is not None and cancel_event.is_set()
        if cancelled():
            raise KnowledgeError('ai_request_cancelled', 409)
        try:
            with self.data._open() as (connection, state):
                self.index._check_binding(state)
                connection.create_function('chatlens_sender_valid', 1, valid_sender, deterministic=True)
                connection.set_progress_handler(lambda: int(cancelled() or time.monotonic() - started > 1.5), 10000)
                catalog = self.index._catalog(state, source, connection=connection)
                known_ids = {key for alias in scope for key in catalog[alias]['sender_names']}
                if any(sender not in known_ids for sender in senders):
                    raise KnowledgeError('statistics_sender_scope_invalid', 400)
                watermark = {alias: connection.execute('SELECT COALESCE(MAX(rowid),0) FROM messages WHERE alias=?', (alias,)).fetchone()[0] for alias in scope}
                base = self._totals(connection, where, parameters)
                if body is not None:
                    where += ' AND instr(body,?)>0'
                    parameters.append(body)
                    untimed_where += ' AND instr(body,?)>0'
                    untimed_parameters.append(body)
                if senders:
                    sender_filter = ' AND sender_id IN (%s)' % ','.join('?' for _ in senders)
                    where += sender_filter
                    parameters.extend(senders)
                    untimed_where += sender_filter
                    untimed_parameters.extend(senders)
                self_id = trusted_self_identity(source, state['binding_digest'])
                self_ids = [self_id] if self_id is not None else []
                unclassified_direction = 0
                applied_direction = direction
                warnings = []
                if (direction != 'all' or group == 'direction') and not self_ids:
                    applied_direction = 'unknown'
                    warnings.append('self_identity_unavailable')
                elif direction == 'incoming':
                    unclassified_direction = connection.execute('SELECT COUNT(*) FROM messages WHERE ' + where + ' AND chatlens_sender_valid(sender_id)=0', parameters).fetchone()[0]
                    incoming_filter = ' AND chatlens_sender_valid(sender_id)=1 AND sender_id NOT IN (%s)' % ','.join('?' for _ in self_ids)
                    where += incoming_filter
                    parameters.extend(self_ids)
                    untimed_where += incoming_filter
                    untimed_parameters.extend(self_ids)
                elif direction == 'outgoing':
                    outgoing_filter = ' AND sender_id IN (%s)' % ','.join('?' for _ in self_ids)
                    where += outgoing_filter
                    parameters.extend(self_ids)
                    untimed_where += outgoing_filter
                    untimed_parameters.extend(self_ids)
                unplaced_timestamps = connection.execute('SELECT COUNT(*) FROM messages WHERE ' + untimed_where
                    + ' AND ' + EVENT_TIME + ' IS NULL', untimed_parameters).fetchone()[0]
                totals = self._totals(connection, where, parameters)
                if totals.pop('invalid_body_count') or base['invalid_body_count']:
                    raise KnowledgeError('statistics_message_invalid')
                identity_missing = (direction != 'all' or group == 'direction') and not self_ids
                direction_counts = {'incoming_count': None, 'outgoing_count': None,
                                    'unknown_direction_count': totals['message_count']}
                if self_id is not None:
                    counts = connection.execute("SELECT COALESCE(SUM(CASE WHEN chatlens_sender_valid(sender_id)=1 AND sender_id<>? THEN 1 ELSE 0 END),0),"
                        "COALESCE(SUM(CASE WHEN sender_id=? THEN 1 ELSE 0 END),0),"
                        "COALESCE(SUM(CASE WHEN chatlens_sender_valid(sender_id)=0 THEN 1 ELSE 0 END),0) FROM messages WHERE " + where,
                        [self_id, self_id] + parameters).fetchone()
                    direction_counts = dict(zip(('incoming_count', 'outgoing_count', 'unknown_direction_count'), counts))
                output_rows, representatives, representative_positions, total_groups = [], [], [], 0
                if not identity_missing and group == 'direction':
                    definitions = [('incoming', '收到', ' AND chatlens_sender_valid(sender_id)=1 AND sender_id<>?', [self_id]),
                                   ('outgoing', '发出', ' AND sender_id=?', [self_id]),
                                   ('unknown', '方向未知', ' AND chatlens_sender_valid(sender_id)=0', [])]
                    for bucket, label, clause, extra in definitions:
                        count = direction_counts[bucket + '_count' if bucket != 'unknown' else 'unknown_direction_count']
                        if bucket == 'unknown' and not count:
                            continue
                        index = len(output_rows)
                        output_rows.append({'direction': bucket, 'name': label, 'message_count': count})
                        if count:
                            raw = connection.execute('SELECT ' + EVIDENCE_COLUMNS + ' FROM messages WHERE ' + where + clause
                                + ' ORDER BY rowid DESC LIMIT 1', parameters + extra).fetchone()
                            representatives.append(raw)
                            representative_positions.append(index)
                    total_groups = len(output_rows)
                elif not identity_missing and metric == 'latest':
                    rows = connection.execute('SELECT ' + EVIDENCE_COLUMNS + ' FROM messages WHERE ' + where
                                              + ' ORDER BY ' + EVENT_TIME + ' DESC,rowid DESC LIMIT ?', parameters + [limit]).fetchall()
                    total_groups = totals['message_count']
                    for row in rows:
                        sender = '我' if self_id is not None and row['sender_id'] == self_id else catalog[row['alias']]['sender_names'].get(row['sender_id']) or '未知发送人'
                        output_rows.append({'sender': _display(sender), 'conversation': _display(catalog[row['alias']]['name']),
                                            'time': self._time(row), 'excerpt': _display(row['body'], 120)})
                        representatives.append(row)
                        representative_positions.append(len(output_rows) - 1)
                elif not identity_missing and (metric != 'messages' or group is not None):
                    group = 'sender' if metric == 'senders' else 'conversation' if metric == 'conversations' else 'day' if metric == 'trend' else group
                    expression = {'sender': 'CASE WHEN chatlens_sender_valid(sender_id)=1 THEN sender_id END', 'conversation': 'alias',
                                  'day': "date(" + EVENT_TIME + ",'unixepoch','+8 hours')"}[group]
                    total_groups = connection.execute('SELECT COUNT(*) FROM (SELECT 1 FROM messages WHERE ' + where + ' GROUP BY ' + expression + ')', parameters).fetchone()[0]
                    order = 'group_key DESC' if metric == 'trend' else 'message_count ' + sort_order.upper() + ',representative DESC'
                    rows = connection.execute('SELECT ' + expression + ' AS group_key,COUNT(*) AS message_count,MAX(rowid) AS representative '
                                              'FROM messages WHERE ' + where + ' GROUP BY ' + expression + ' ORDER BY ' + order + ' LIMIT ?', parameters + [limit]).fetchall()
                    for position, row in enumerate(rows, 1):
                        raw = connection.execute('SELECT ' + EVIDENCE_COLUMNS + ' FROM messages WHERE rowid=? AND ' + where,
                                                 [row['representative']] + parameters).fetchone()
                        if raw is None:
                            raise KnowledgeError('statistics_snapshot_changed', 409)
                        key = row['group_key']
                        if group == 'sender':
                            label = next((catalog[alias]['sender_names'][key] for alias in scope if key in catalog[alias]['sender_names']), None)
                            if self_id is not None and key == self_id:
                                label = '我'
                            label = label or ('身份未知的消息' if key is None else '未知姓名的发送人 %d' % position)
                        elif group == 'conversation':
                            label = catalog[key]['name']
                        else:
                            label = key or '时间未知'
                        output_rows.append({'name': _display(label), 'message_count': row['message_count'],
                                            'unknown_identity': group == 'sender' and key is None})
                        representatives.append(raw)
                        representative_positions.append(len(output_rows) - 1)
                elif not identity_missing and totals['message_count']:
                    raw = connection.execute('SELECT ' + EVIDENCE_COLUMNS + ' FROM messages WHERE ' + where
                                             + ' ORDER BY rowid DESC LIMIT 1', parameters).fetchone()
                    representatives.append(raw)
                    representative_positions.append(None)
                self.index._check_binding(state)
                if cancelled():
                    raise KnowledgeError('ai_request_cancelled', 409)
        except KnowledgeError:
            raise
        except (WebDataError, sqlite3.Error):
            if cancelled():
                raise KnowledgeError('ai_request_cancelled', 409) from None
            raise KnowledgeError('statistics_store_unavailable') from None

        evidence, seen = [], set()
        for position, row in enumerate(representatives):
            if len(evidence) >= MAX_EVIDENCE:
                break
            if row['rowid'] in seen:
                label = next(item['id'] for item in evidence if item['message_id'] == row['rowid'])
            else:
                item = self.index._local(row, catalog)
                if item is None:
                    continue
                item['id'] = 'E%d' % (len(evidence) + 1)
                item['excerpt_truncated'] = row['body_chars'] > 500
                if len(json.dumps(evidence + [item], ensure_ascii=False)) > MAX_CONTEXT_CHARS:
                    break
                evidence.append(item)
                seen.add(row['rowid'])
                label = item['id']
            output_position = representative_positions[position]
            if output_position is not None:
                output_rows[output_position]['citation'] = label
        if totals['unknown_sender_messages']:
            warnings.append('unknown_sender_identity')
        if totals['timestamp_fallback_messages']:
            warnings.append('observed_time_used')
        if unplaced_timestamps:
            warnings.append('unknown_event_time')
        if unclassified_direction:
            warnings.append('unclassified_direction_messages_excluded')
        if total_groups > len(output_rows):
            warnings.append('display_limit')
        statistics = {'metric': metric, 'group_by': group, 'order': sort_order, **totals, **direction_counts, 'total_groups': total_groups,
                      'displayed_groups': len(output_rows), 'display_truncated': total_groups > len(output_rows),
                      'rows': output_rows, 'aggregate_complete': not identity_missing,
                      'direction_requested': direction, 'direction_applied': applied_direction,
                      'identity_verified': bool(self_ids), 'unclassified_direction_messages': unclassified_direction,
                      'unplaced_timestamp_messages': unplaced_timestamps,
                      'time_range': normalized, 'time_label': _display(plan.get('time_label') or '所选时间范围', 160),
                      'scope_names': [_display(catalog[alias]['name']) for alias in scope],
                      'warnings': list(dict.fromkeys(warnings)), 'evidence_is_representative': True,
                      'data_boundary': '仅当前选定会话中启用采集后已保存的文字，不代表完整微信历史。'}
        if identity_missing:
            statistics['message_count'] = None
            statistics['sender_count'] = None
            statistics['conversation_count'] = None
        coverage = {'mode': 'statistics', 'available_count': base['message_count'],
                    'matched_count': 0 if identity_missing else totals['message_count'],
                    'included_count': len(evidence), 'included_match_count': len(evidence), 'context_count': 0,
                    'scanned_count': totals['message_count'], 'candidate_limit': None, 'watermark': watermark,
                    'has_more': False, 'truncated': False, 'truncation_reasons': [],
                    'time_range': normalized, 'aggregate_complete': not identity_missing,
                    'evidence_is_representative': True, 'timestamp_fallback_count': totals['timestamp_fallback_messages'],
                    'unknown_time_count': unplaced_timestamps,
                    'evidence_timestamp_fallback_count': sum(row['time_source'] == 'observed_at' for row in evidence)}
        answer = self._answer(statistics, evidence, needs_identity=identity_missing)
        return {'answer': answer, 'evidence': evidence, 'coverage': coverage, 'statistics': statistics,
                'needs_clarification': identity_missing}

    @staticmethod
    def _time(row):
        value = row['sent_at']
        if type(value) is int and 0 <= value <= 253402300799:
            try:
                return datetime.fromtimestamp(value, LOCAL_TIMEZONE).isoformat()
            except (ValueError, OverflowError, OSError):
                pass
        return row['observed_at']

    @staticmethod
    def _answer(statistics, evidence, *, needs_identity):
        if needs_identity:
            return ('当前账号的本人发送身份尚未可靠确认，暂时无法准确区分收到与发出的消息。'
                    '请等待本机身份核对完成；不能依据显示名猜测本人身份。')
        count, metric = statistics['message_count'], statistics['metric']
        prefix = statistics['time_label'] + '，在' + '、'.join(statistics['scope_names']) + '中'
        source = ' '.join('[' + item['id'] + ']' for item in evidence[:2])
        action = '共收到' if statistics['direction_applied'] == 'incoming' else '共发出' if statistics['direction_applied'] == 'outgoing' else '共匹配'
        lines = [prefix + action + ' %d 条已采文字。%s' % (count, (' ' + source) if source else '')]
        if statistics['group_by'] == 'direction':
            lines.append('收发方向按已核对的本人账号 ID 区分，未知身份单独保留。')
        if metric in {'senders', 'ranking'} and statistics['group_by'] == 'sender':
            lines.append('可识别的发送者共 %d 位；身份未知的 %d 条消息未计入人数。' % (statistics['sender_count'], statistics['unknown_sender_messages']))
        elif metric == 'conversations':
            lines.append(('发往的会话对象（私聊联系人或群会话）共 %d 个。' if statistics['direction_applied'] == 'outgoing'
                          else '有文字记录的会话共 %d 个。') % statistics['conversation_count'])
        elif metric == 'messages':
            lines.append(('涉及 %d 个会话。' % statistics['conversation_count']) if statistics['direction_applied'] == 'outgoing'
                         else '涉及 %d 个会话、%d 位可识别发送者。' % (statistics['conversation_count'], statistics['sender_count']))
        for row in statistics['rows']:
            citation = (' [' + row['citation'] + ']') if row.get('citation') else ''
            if metric == 'latest':
                lines.append('· %s · %s · %s：%s%s' % (row['sender'], row['conversation'], _display(row['time'], 32), row['excerpt'], citation))
            else:
                lines.append('· %s：%d 条%s' % (row['name'], row['message_count'], citation))
        if statistics['display_truncated']:
            lines.append('共 %d 项，当前展示前 %d 项；以上总数已按全部匹配记录计算。' % (statistics['total_groups'], statistics['displayed_groups']))
        if statistics['timestamp_fallback_messages']:
            lines.append('%d 条缺少原始发送时间，已采用采集时间归入日期，不将其视为原始发送时刻。' % statistics['timestamp_fallback_messages'])
        if statistics['unknown_timestamp_messages']:
            lines.append('%d 条没有可用时间，未归入具体日期。' % statistics['unknown_timestamp_messages'])
        elif statistics['unplaced_timestamp_messages']:
            lines.append('另有 %d 条符合会话与筛选条件的文字没有可用时间，无法判断是否属于本次日期范围，未计入日期统计。' % statistics['unplaced_timestamp_messages'])
        if statistics['unclassified_direction_messages']:
            lines.append('%d 条发送身份未知，未计入已确认来信。' % statistics['unclassified_direction_messages'])
        if not statistics['identity_verified']:
            lines.append('本人身份尚未可靠核对，本轮只统计全部已采消息，不推断收发方向。')
        if evidence:
            lines.append('引用为代表性原消息；统计总数来自完整匹配数据，不是证据卡数量。')
        lines.append(statistics['data_boundary'])
        return '\n'.join(lines)
