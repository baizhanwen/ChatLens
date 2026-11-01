"""Local, bounded query planning over the currently selected chat knowledge base.

This module never reads a store, calls a model, or emits SQL. Its output is a
small typed plan consumed by the read-only statistics/retrieval executors. A
question cannot enlarge the conversation or time range chosen in the UI.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import re
import unicodedata

SCHEMA_VERSION = 1
LOCAL_TIMEZONE = timezone(timedelta(hours=8), 'Asia/Shanghai')
MAX_QUESTION_CHARS = 2000
MAX_LIMIT = 50
ALIASES = ('private_test', 'group_test')
STATS_METRICS = {'messages', 'senders', 'conversations', 'ranking', 'latest', 'trend'}
CHINESE_DIGITS = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
                  '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
NUMBER = r'[0-9零〇一二两三四五六七八九十百千]+'
ANALYTIC = re.compile(
    r'总结|分析|概括|梳理|提炼|重点|风险|投诉|退款|询价|报价|商机|意向|态度|情绪|承诺|'
    r'约定|跟进|待办|建议|原因|为什么|为何|怎么(?:办|做|回|说)|如何|是否|是不是|'
    r'有(?:无|没有|哪些|什么).{0,8}(?:问题|异常|机会)|需要注意|值得关注|重要|拍板|'
    r'summar(?:y|ize)|analy[sz]e|risk|follow\s*up|recommend|why\b', re.I)
COUNT = re.compile(r'多少|几条|几个人|几人|几个|几位|总数|总量|数量|计数|条数|人数|消息数|信息数|发言数|来信数|统计|计一下|数一下|how\s+many|count\b', re.I)
RANK = re.compile(r'排行|排名|排个序|排一下|排序|最多|最少|最不活跃|最不热闹|最活跃|最热闹|最频繁|最常|top\s*\d*|(?:most|least)\s+(?:active|messages)|fewest|rank', re.I)
MESSAGE = re.compile(r'消息|信息|来信|私信|微信|聊天|发言|说话|留言|联系|找我|打招呼|聊过|聊了|聊得|message(?:s|d)?|chats?|senders?', re.I)
SENDER = re.compile(r'谁|哪些人|什么人|哪几(?:个|位)?人|客户|联系人|朋友|发送(?:人|者)|发信人|人数|几个人|几人|几位|多少人|who\b|sender|people', re.I)
CONVERSATION = re.compile(r'会话|群(?:聊|里|内|组)?|私聊|conversation|group\b', re.I)
LATEST = re.compile(r'最后(?:一条|一次)?|最新|最近(?:一条|一则|一封)|最晚|上次|最后谁|最近谁|刚才谁|latest|last\s+message', re.I)
TREND = re.compile(r'趋势|按天|每天|逐日|每日|分天|按日期|daily|per\s+day|trend', re.I)


class QueryPlanError(ValueError):
    def __init__(self, code, message='问题或查询范围无效。'):
        self.code, self.message, self.status = code, message, 400
        super().__init__(code)


def _normalize(value):
    return unicodedata.normalize('NFKC', value).casefold()


def _integer(value):
    if value.isdigit():
        return int(value)
    if not value or any(c not in CHINESE_DIGITS and c not in '十百千' for c in value):
        return None
    if not any(c in value for c in '十百千'):
        return int(''.join(str(CHINESE_DIGITS[c]) for c in value))
    total, current = 0, 0
    for char in value:
        if char in CHINESE_DIGITS:
            current = CHINESE_DIGITS[char]
        else:
            total += (current or 1) * {'十': 10, '百': 100, '千': 1000}[char]
            current = 0
    return total + current


def _iso(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise QueryPlanError('query_time_invalid')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(LOCAL_TIMEZONE)
    except (ValueError, OverflowError):
        raise QueryPlanError('query_time_invalid') from None


def _window(choice, current):
    if isinstance(choice, dict):
        if set(choice) - {'start', 'end'}:
            raise QueryPlanError('query_time_invalid')
        start, end = _iso(choice.get('start')), _iso(choice.get('end'))
        if start is not None and end is not None and start >= end:
            raise QueryPlanError('query_time_invalid')
        return (start, end), '所选时间范围'
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if choice == 'all':
        return (None, None), '全部已采时间'
    if choice == '7d':
        return (start - timedelta(days=6), current + timedelta(microseconds=1)), '最近 7 天'
    if choice == 'today':
        return (start, current + timedelta(microseconds=1)), '今天'
    raise QueryPlanError('query_time_invalid')


def _day(value):
    return datetime.combine(value, datetime.min.time(), LOCAL_TIMEZONE)


def _date_value(match, current):
    year = int(match.group('year')) if match.group('year') else current.year
    return date(year, int(match.group('month')), int(match.group('day')))


DATE = re.compile(r'(?<![\d])(?:(?P<year>\d{4})[年/.-])?(?P<month>\d{1,2})[月/.-](?P<day>\d{1,2})(?:[日号])?(?!\d)')


def _explicit_time(text, current):
    """Return a single explicit time interval; ambiguous periods need a choice."""
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    until_now = current + timedelta(microseconds=1)
    candidates = []
    dates = list(DATE.finditer(text))
    if dates:
        try:
            values = [_date_value(match, current) for match in dates]
        except ValueError:
            return None, None, '日期无效，请确认月份和日期。'
        if len(values) == 1:
            first = _day(values[0]); candidates.append((first, first + timedelta(days=1), values[0].isoformat()))
        elif len(values) == 2 and re.search(r'到|至|~|—|\bto\b', text[dates[0].end():dates[1].start()]):
            first, last = _day(values[0]), _day(values[1]) + timedelta(days=1)
            if first >= last:
                return None, None, '开始日期晚于结束日期，请重新指定。'
            candidates.append((first, last, values[0].isoformat() + ' 至 ' + values[1].isoformat()))
        else:
            return None, None, '请指定一个连续的时间范围，或分开查询这些日期。'
        text = DATE.sub(' ', text)
    relative = [
        (r'大前天', -3, '大前天'), (r'前天', -2, '前天'),
        (r'昨天|昨日|\byesterday\b', -1, '昨天'),
        (r'今天|今日|当天|\btoday\b', 0, '今天'),
    ]
    for expression, offset, label in relative:
        if re.search(expression, text):
            first = start + timedelta(days=offset)
            candidates.append((first, until_now if offset == 0 else first + timedelta(days=1), label))
            text = re.sub(expression, ' ', text)
    if re.search(r'上(?:个)?周|上星期|\blast\s+week\b', text):
        first = start - timedelta(days=current.weekday() + 7)
        candidates.append((first, first + timedelta(days=7), '上周'))
        text = re.sub(r'上(?:个)?周|上星期|\blast\s+week\b', ' ', text)
    if re.search(r'本周|这周|这星期|本星期|\bthis\s+week\b', text):
        candidates.append((start - timedelta(days=current.weekday()), until_now, '本周'))
        text = re.sub(r'本周|这周|这星期|本星期|\bthis\s+week\b', ' ', text)
    if re.search(r'上(?:个)?月|\blast\s+month\b', text):
        last = start.replace(day=1)
        first = (last - timedelta(days=1)).replace(day=1)
        candidates.append((first, last, '上个月'))
        text = re.sub(r'上(?:个)?月|\blast\s+month\b', ' ', text)
    if re.search(r'本月|这个月|这月|\bthis\s+month\b', text):
        candidates.append((start.replace(day=1), until_now, '本月'))
        text = re.sub(r'本月|这个月|这月|\bthis\s+month\b', ' ', text)
    for match in re.finditer(r'(?:最近|近|过去|这|前|last\s+|past\s+)\s*(' + NUMBER + r')\s*(天|日|周|星期|小时|days?|weeks?|hours?)', text):
        number, unit = _integer(match[1]), match[2]
        if number is None or not 1 <= number <= 3660:
            return None, None, '时间跨度需为 1 至 3660 个单位，请缩小范围。'
        if unit in ('小时', 'hour', 'hours'):
            first = current - timedelta(hours=number)
            label = '过去 %d 小时' % number
        else:
            days = number * (7 if unit in ('周', '星期', 'week', 'weeks') else 1)
            if days > 3660:
                return None, None, '时间跨度过大，请缩小范围。'
            first = start - timedelta(days=days - 1)
            label = '最近 %d 天' % days
        candidates.append((first, until_now, label))
    if re.search(r'所有时间|全部时间|不限时间|从开始|启用以来|采集以来|\ball\s+time\b', text):
        candidates.append((None, None, '全部已采时间'))
    if not candidates:
        if re.search(r'明天|后天|下周|下个月|\btomorrow\b', text):
            return None, None, '本机只能查询已经采集的消息，请选择过去或当前的时间范围。'
        return None, None, None
    if len(candidates) > 1:
        unique = {(a, b) for a, b, _ in candidates}
        if len(unique) > 1:
            return None, None, '问题包含多个时间范围，请分开查询或明确一个连续区间。'
    first, last, label = candidates[0]
    return (first, last), label, None


def _intersection(selected, requested):
    starts = [value for value in (selected[0], requested[0]) if value is not None]
    ends = [value for value in (selected[1], requested[1]) if value is not None]
    first, last = max(starts) if starts else None, min(ends) if ends else None
    return first, last


def _bounds(values):
    return {field: value.isoformat() if value is not None else None for field, value in zip(('start', 'end'), values)}


def _body_filter(text):
    # Only an explicit literal/keyword request can become an exact SQL filter.
    # Arbitrary quoted customer text must not be promoted to instructions.
    quoted = re.search(r'(?:包含|含有|提到|出现|带有|带着|内容(?:是|为)?|原文(?:是|为)?|关键词|containing|contains|mentions?)\s*[“「『\"\']([^”」』\"\']{1,200})[”」』\"\']', text, re.I)
    if quoted:
        return quoted[1], text[:quoted.start(1)] + ' ' * len(quoted[1]) + text[quoted.end(1):]
    keyword = re.search(r'(?:关键词|关键字)\s*[:：]\s*([^,，。;；?？\n]{1,80})', text)
    if keyword:
        return keyword[1].strip(), text[:keyword.start(1)] + ' ' * len(keyword[1]) + text[keyword.end(1):]
    return None, text


def _catalog_entries(catalog):
    if not isinstance(catalog, dict):
        raise QueryPlanError('query_catalog_invalid')
    for alias, value in catalog.items():
        if alias not in ALIASES or not isinstance(value, dict) or not isinstance(value.get('name'), str):
            raise QueryPlanError('query_catalog_invalid')
    return catalog


def _entities(text, selected, catalog):
    """Only producer-known identities can narrow a scope or become a filter."""
    conversation_hits, sender_hits = set(), set()
    all_names = {}
    for alias, value in catalog.items():
        name = _normalize(value['name']).strip()
        if len(name) >= 2:
            all_names.setdefault(name, set()).add(alias)
        for identifier, display in value.get('sender_names', {}).items():
            if not isinstance(identifier, str) or not isinstance(display, str):
                continue
            name = _normalize(display).strip()
            if len(name) >= 2 and name not in {'未知发送人', '群成员', '联系人', '客户', '我自己'}:
                if name in text:
                    sender_hits.add(identifier)
    for name, aliases in all_names.items():
        if name in text:
            conversation_hits.update(aliases)
    # A unique non-generic prefix (青禾客户 -> 青禾) is a useful local hint.
    # Do not match an arbitrary two-character overlap such as "项目" or "测试".
    if not conversation_hits:
        generic = {'所选', '私聊', '群聊', '项目', '测试', '合成', '客户'}
        prefixes = {}
        for name, aliases in all_names.items():
            prefix = name[:2]
            if prefix not in generic:
                prefixes.setdefault(prefix, set()).update(aliases)
        for prefix, aliases in prefixes.items():
            if len(aliases) == 1 and prefix in text:
                conversation_hits.update(aliases)
    if conversation_hits:
        scope = [alias for alias in selected if alias in conversation_hits]
        if not scope:
            return [], [], '问题提到的会话不在当前选中范围内，请先调整会话选择。'
    else:
        scope = list(selected)
    group_only = bool(re.search(r'仅(?:看|统计|查)?群|只(?:看|统计|查)?群|群聊(?:里|中|内)?|群里|群内|各群|每个群|哪个群|哪些群|多少个?群|几个群|群消息|群发言|^(?:那|那么)?群呢[?？]?$', text))
    private_only = bool(re.search(r'私聊|私信|单聊', text))
    if group_only and not private_only:
        scope = [alias for alias in scope if catalog.get(alias, {}).get('kind') == 'group']
    if private_only and not group_only:
        scope = [alias for alias in scope if catalog.get(alias, {}).get('kind') == 'private']
    if not scope:
        return [], [], '当前选中范围内没有相应类型的会话，请调整会话选择。'
    if sender_hits:
        available_ids = {identifier for alias in scope for identifier in catalog.get(alias, {}).get('sender_names', {})}
        sender_hits.intersection_update(available_ids)
        if not sender_hits:
            return scope, [], '提到的发送人不在当前会话范围内，请调整范围。'
        named_ids = {}
        for alias in scope:
            for identifier, name in catalog.get(alias, {}).get('sender_names', {}).items():
                if len(name) >= 2 and _normalize(name) in text:
                    named_ids.setdefault(_normalize(name), set()).add(identifier)
        if any(len(ids) > 1 for ids in named_ids.values()):
            return scope, [], '当前范围内存在同名但身份不同的发送人，请先限定会话或明确要查询哪一位。'
    return scope, sorted(sender_hits), None


def _classification(text):
    """Compose dimension, operation and content intent instead of phrase patches."""
    analytic = bool(ANALYTIC.search(text))
    count, rank, latest, trend = bool(COUNT.search(text)), bool(RANK.search(text)), bool(LATEST.search(text)), bool(TREND.search(text))
    message, sender, conversation = bool(MESSAGE.search(text) or re.search(r'多少条|几条|条数', text)), bool(SENDER.search(text)), bool(CONVERSATION.search(text))
    group = 'conversation' if conversation and (not sender or bool(re.search(r'哪个群|哪些群|各群|群(?:聊)?(?:排行|排名|最)|哪个会话', text))) else 'sender'
    metric, dimension = None, None
    if trend and (message or count or rank):
        metric, dimension = 'trend', 'day'
    elif rank and (message or sender or conversation or re.search(r'活跃|热闹', text)):
        metric, dimension = 'ranking', group
    elif latest and (message or sender or conversation):
        metric, dimension = 'latest', None
    elif count and message and re.search(r'(?:每(?:个|一个)|各(?:个)?)(?:群(?:聊)?|会话)|按(?:会话|群)', text):
        metric, dimension = 'messages', 'conversation'
    elif count and message and re.search(r'(?:每(?:个|位|一位)?|各(?:个|位)?)(?:人|联系人|发送人|发送者|客户)|按(?:联系人|发送人|发送者|人)', text):
        metric, dimension = 'messages', 'sender'
    elif count and re.search(r'多少(?:条|则|封)?(?:新)?(?:消息|信息|来信|发言)|(?:消息|信息|来信|发言)(?:总数|总量|数量|数目|数|有多少)', text):
        metric, dimension = 'messages', None
    elif count and (sender or re.search(r'人数|几人|几位|people', text)) and not re.search(r'多少条|几条|条数|消息总', text):
        metric, dimension = 'senders', 'sender'
    elif count and conversation and not re.search(r'多少条|几条|条数|消息总|消息数|信息数|发言数', text):
        metric, dimension = 'conversations', 'conversation'
    elif count and message:
        metric, dimension = 'messages', None
    elif sender and (message or re.search(r'谁.{0,8}(?:找过|联系过|联系了|找了|来过)|(?:哪些人|有谁).{0,8}(?:联系|找)', text)):
        metric, dimension = 'senders', 'sender'
    elif conversation and re.search(r'哪些|哪个|哪几个|有消息|有来信|有发言|活跃', text):
        metric, dimension = 'conversations', 'conversation'
    if analytic:
        # A semantic predicate cannot be counted directly from sender/body
        # columns ("有多少客户想退款"). Explicit compound tasks are mixed.
        clauses = re.split(r'并且|并|然后|再|顺便|同时|以及|[,，;；]', text)
        explicit_stats = bool(metric and len(clauses) > 1 and any(
            not ANALYTIC.search(clause) and (COUNT.search(clause) or RANK.search(clause) or LATEST.search(clause))
            and (MESSAGE.search(clause) or SENDER.search(clause) or CONVERSATION.search(clause))
            for clause in clauses))
        return ('mixed', metric, dimension) if explicit_stats else ('analysis', None, None)
    return ('local_stats', metric, dimension) if metric else ('analysis', None, None)


def _previous(previous):
    if not isinstance(previous, dict) or previous.get('schema_version') != SCHEMA_VERSION:
        return None
    if previous.get('metric') is not None and not isinstance(previous.get('metric'), str):
        return None
    if (previous.get('route') not in ('local_stats', 'mixed', 'analysis')
            or previous.get('metric') not in STATS_METRICS | {None}
            or previous.get('group_by') not in ('sender', 'conversation', 'day', None)
            or previous.get('direction') not in ('incoming', 'outgoing', 'all')
            or not isinstance(previous.get('scope'), list)
            or any(not isinstance(alias, str) or alias not in ALIASES for alias in previous['scope'])):
        return None
    if not isinstance(previous.get('retrieval_query', ''), str):
        return None
    filters = previous.get('filters', {})
    if (not isinstance(filters, dict) or not isinstance(filters.get('sender_ids', []), list)
            or any(not isinstance(value, str) for value in filters.get('sender_ids', []))
            or filters.get('body_contains') is not None and (not isinstance(filters['body_contains'], str) or len(filters['body_contains']) > 200)):
        return None
    if not isinstance(previous.get('time_range'), dict):
        return None
    try:
        _window(previous['time_range'], datetime.now(timezone.utc))
    except QueryPlanError:
        return None
    return previous


def _ellipsis(text, catalog):
    """A follow-up changes only a date/object, with no new substantive topic."""
    remaining = DATE.sub(' ', text)
    remaining = re.sub(r'(?:最近|近|过去|这|前|last\s+|past\s+)\s*' + NUMBER + r'\s*(?:天|日|周|星期|小时|days?|weeks?|hours?)', ' ', remaining)
    remaining = re.sub(r'大前天|前天|昨天|昨日|今天|今日|当天|本周|这周|上个周|上周|这星期|本星期|上星期|本月|这个月|这月|上个月|上月|yesterday|today|this\s+week|last\s+week|this\s+month|last\s+month', ' ', remaining)
    names = set()
    for value in catalog.values():
        names.add(_normalize(value['name']))
        names.update(_normalize(name) for name in value.get('sender_names', {}).values() if isinstance(name, str))
    for name in sorted(names, key=len, reverse=True):
        if len(name) >= 2:
            remaining = remaining.replace(name, ' ')
    remaining = re.sub(r'那么|那|呢|也|还有|再看|看看|看下|一下|只看|仅看|换成|改成|同样|的|看|请|怎么样|怎样|情况|群聊|私聊|私信|单聊|群', ' ', remaining)
    return not re.sub(r'[\s?？。.!！,，:：;；]', '', remaining)


def _metadata_residue(text, catalog):
    """Require every meaningful local-stat condition to have an interpretation.

    This is a positive grammar for supported metadata, not a growing blacklist
    of business topics. Any remaining predicate (欠款/延期/新客户/unknown future
    wording) stays on the analysis route instead of silently counting all rows.
    """
    remaining = _erase_known_labels(text, catalog)
    remaining = DATE.sub(' ', remaining)
    remaining = re.sub(r'(?:最近|近|过去|这|前|last\s+|past\s+)\s*' + NUMBER + r'\s*(?:天|日|周|星期|小时|days?|weeks?|hours?)', ' ', remaining)
    remaining = re.sub(r'(?:前\s*' + NUMBER + r'\s*(?:名|位|个|条)|top\s*' + NUMBER + r')', ' ', remaining)
    remaining = re.sub(r'大前天|前天|昨天|昨日|今天|今日|当天|本周|这周|上个周|上周|这星期|本星期|上星期|本月|这个月|这月|上个月|上月|所有时间|全部时间|不限时间|从开始|启用以来|采集以来|yesterday|today|this\s+week|last\s+week|this\s+month|last\s+month|all\s+time', ' ', remaining)
    remaining = re.sub(r'新(?:消息|信息|来信)', ' ', remaining)
    for pattern in (COUNT, RANK, LATEST, TREND, SENDER, CONVERSATION, MESSAGE):
        remaining = pattern.sub(' ', remaining)
    # Functional grammar and explicitly interpreted literal operators only.
    words = ('统计一下', '数一数', '帮我看一下', '帮我看', '帮我查', '告诉我', '请问', '请', '看看', '看一下',
             '再', '重新', '麻烦', '能否', '可以', '能不能', '能帮我', '帮忙', '帮我', '你',
             '列出来', '列出', '列一下', '显示', '查看', '查一下', '查下', '查询', '汇总', '计算', '一共', '总共',
             '总计', '总共', '合计', '总', '共', '全部', '所有', '当前', '所选', '已采集', '已采', '已经',
             '各自', '分别', '每个', '每位', '每天', '每人', '各', '按', '最近', '活跃', '热闹', '先', '产生', '接收', '接到',
             '收到了', '收到过', '收到', '发给', '发来', '发过', '发了', '发送', '发出', '发', '来过', '来',
             '联系过', '找过', '联系了', '找了', '过', '有过', '共有', '是否有', '有没有', '有', '哪些', '哪几个',
             '哪个', '哪位', '几', '个', '位', '人', '条', '则', '封', '次数', '频次', '内容', '原文', '关键词',
             '关键字', '包含', '含有', '提到', '出现', '带有', '带着', '从多到少', '从少到多', '降序', '升序',
             '最高', '最低', '多', '少', '数', '量', '到', '至', '是什么时候', '是什么', '是多少', '是', '为',
             '给', '对', '向', '从', '里', '内', '中', '上', '和', '与', '跟', '的', '得', '了', '吗', '呢', '都', '我',
             '那么', '那', '这', '也', '一下', '吧')
    for word in sorted(set(words), key=len, reverse=True):
        remaining = remaining.replace(word, ' ')
    remaining = re.sub(r'\b(?:how|many|who|which|what|when|received|receive|sent|send|from|to|me|my|the|a|an|by|in|on|of|all|are|is|was|were|did|do|does|have|has|had|there|please|list|show|give|with|containing|contains|mentions?|each|per|within|counts?)\b', ' ', remaining)
    return re.sub(r'[\s?？。.!！,，:：;；“”「」『』\"\'()（）~—-]', '', remaining)


def _erase_known_labels(text, catalog):
    """A producer name is a query operand, never an operation/time keyword."""
    remaining = text
    names = set()
    prefixes = {}
    for alias, entry in catalog.items():
        name = _normalize(entry['name'])
        names.add(name)
        if name[:2] not in {'所选', '私聊', '群聊', '项目', '测试', '合成', '客户'}:
            prefixes.setdefault(name[:2], set()).add(alias)
        names.update(_normalize(value) for value in entry.get('sender_names', {}).values() if isinstance(value, str))
    names.update(prefix for prefix, aliases in prefixes.items() if len(aliases) == 1)
    for name in sorted(names, key=len, reverse=True):
        if len(name) >= 2:
            remaining = remaining.replace(name, ' ')
    return remaining


def _unresolved_entity(residue, masked):
    # A human-looking unresolved target must be clarified, never treated as an
    # unconstrained count. Other residual content is delegated to analysis.
    surnames = '赵钱孙李周吴郑王冯陈蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜谢邹喻柏水窦章云苏潘葛范彭鲁韦马苗方任袁柳唐罗薛雷贺倪汤滕殷徐钟陆邵石崔程龚黄萧尹姚顾孟龙段毛高蔡田胡温凌霍虞万支柯卢莫房裘缪干解应宗丁宣贲邓郁单杭洪包诸左江童颜郭林刁刘许曾谭夏熊白叶肖汪付赖廖邱邵侯邵宋董傅余杜戴梁于任易黎池常滕庄辛蒙聂简骆焦孔欧阳司马上官'
    if re.fullmatch(r'(?:(?:小|老|阿)[' + surnames + r'][\u3400-\u9fff]{0,5}|[' + surnames + r'][\u3400-\u9fff]{1,6})', residue):
        return True
    if residue and re.search(r'公司|商贸|门店|工作室|先生|女士|老板|总经理|联系人|发送人', residue):
        return True
    for name in re.findall(r'(?<![A-Za-z])[A-Z][a-z]{1,30}(?![A-Za-z])', masked):
        if name.casefold() in residue:
            return True
    return False


def plan_query(question, *, scope, time_range, catalog, previous=None, current=None):
    """Make an executable plan using only local question and catalog metadata.

    ``previous`` is a previously validated plan, never raw prior chat prose.
    The executor must validate this structure again; clients cannot supply SQL.
    """
    if not isinstance(question, str) or not question.strip() or '\0' in question or len(question) > MAX_QUESTION_CHARS:
        raise QueryPlanError('query_question_invalid')
    try:
        question.encode('utf-8')
    except UnicodeError:
        raise QueryPlanError('query_question_invalid') from None
    if (not isinstance(scope, (list, tuple)) or not scope
            or any(not isinstance(alias, str) or alias not in ALIASES for alias in scope)
            or len(set(scope)) != len(scope)):
        raise QueryPlanError('query_scope_invalid')
    selected = [alias for alias in ALIASES if alias in scope]
    catalog = _catalog_entries(catalog)
    current = current or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise QueryPlanError('query_time_invalid')
    current = current.astimezone(LOCAL_TIMEZONE)
    selected_window, selected_label = _window(time_range, current)
    literal, masked = _body_filter(question.strip())
    text = _normalize(masked)
    semantic_text = _erase_known_labels(text, catalog)
    requested, label, clarification = _explicit_time(semantic_text, current)
    window = _intersection(selected_window, requested) if requested else selected_window
    warnings = []
    if requested is not None and window != requested:
        warnings.append('时间已限制在页面选中的范围内。')
    if window[0] is not None and window[1] is not None and window[0] >= window[1]:
        clarification = '问题中的时间与页面所选范围没有交集，请先调整时间范围再查询。'
        window = selected_window
    effective_scope, sender_ids, scope_clarification = _entities(text, selected, catalog)
    clarification = clarification or scope_clarification
    route, metric, group_by = _classification(semantic_text)
    if metric == 'messages' and re.search(r'分别|各自|各(?:发|有|多少|几)', text):
        group_by = 'sender' if len(sender_ids) > 1 else 'conversation'
    if re.search(r'(?:列出|统计|查询|查一下|查看).{0,20}(?:从未|从没|没有|没|未).{0,20}(?:发|联系|说).{0,12}(?:人|客户|群)', text):
        clarification = '当前只有已采消息，没有完整联系人活动全集，暂不能精确列出从未发消息的对象。'
    direction = 'incoming' if re.search(r'收到|来信|私信|给我|找我|联系我|incoming|received', text) else 'all'
    if re.search(r'(?<![给向对找])我(?:自己)?(?:今天|昨天|最近|本周|这周|一共|总共|总计|共|总){0,4}(?:发|回复|回了|说了)|(?<![给向对找])我(?:发出|发送|已回复)|我给|outgoing|sent\s+by\s+me', text):
        direction = 'outgoing'
        if route in ('local_stats', 'mixed'):
            clarification = '当前已采数据没有可靠的本人发送标记，暂不能精确统计本人发出或回复的条数。'
    if route in ('local_stats', 'mixed') and re.search(r'我(?:和|与|、)|(?:和|与|、)我', text):
        clarification = '当前已采数据没有可靠的本人发送标记，暂不能将本人和其他发送人一起做精确分人统计。'
    if re.search(r'几条|多少条|多少(?:个|位|人)?|统计|总数|数量', text) and re.search(r'未回复|没回复|没有回复|未读|没读|已读|回复率|响应时间', text):
        clarification = '当前数据不包含可靠的已读或回复状态，无法精确统计；可以改问需要跟进的线索并核对原话。'
    if route == 'local_stats' and re.search(r'未回复|没回复|没有回复|未读|没读|已读|回复率|响应时间', text):
        clarification = '当前数据不包含可靠的已读或回复状态，无法精确统计；可以改问需要跟进的线索并核对原话。'
    limit_match = re.search(r'(?:前\s*(' + NUMBER + r')\s*(?:名|位|个|条)|top\s*(' + NUMBER + r'))', text)
    asked_limit = _integer(limit_match[1] or limit_match[2]) if limit_match else None
    limit = min(MAX_LIMIT, max(1, asked_limit or 10)) if limit_match else (1 if metric == 'latest' else 20)
    if asked_limit and asked_limit > MAX_LIMIT:
        warnings.append('单次最多展示 %d 项，完整计数仍以数据库统计为准。' % MAX_LIMIT)
    order = 'asc' if re.search(r'最少|最不活跃|最不热闹|least|fewest|升序|从少到多', text) else 'desc'
    inherited = False
    # Elliptical follow-ups may carry date/entity changes but no new operation.
    # Never inherit an intent into a substantive new question or outside the UI.
    previous = _previous(previous)
    elliptical = _ellipsis(text, catalog)
    temporal_followup = requested is not None and elliptical and not ANALYTIC.search(text) and metric is None
    scope_followup = elliptical and bool(re.search(r'^(?:那|那么|只看|仅看|换成|改成|看看|看下|看)', text)) and (effective_scope != selected or sender_ids)
    if (previous is not None and (temporal_followup or scope_followup) and not ANALYTIC.search(text) and metric is None):
        old_metric = previous.get('metric')
        if old_metric in STATS_METRICS or previous.get('route') == 'analysis':
            route, metric, group_by = previous['route'], old_metric, previous.get('group_by')
            if not sender_ids and not scope_followup:
                prior_scope = previous.get('scope', [])
                effective_scope = [alias for alias in effective_scope if alias in prior_scope]
                if not effective_scope:
                    clarification = '上一轮会话与当前所选范围不一致，请重新明确查询对象。'
            if requested is None:
                old_time = previous.get('time_range')
                if isinstance(old_time, dict):
                    old_window, _ = _window(old_time, current)
                    window = _intersection(selected_window, old_window)
                    if window[0] is not None and window[1] is not None and window[0] >= window[1]:
                        clarification = '上一轮时间与当前所选范围不一致，请重新明确时间范围。'
                        window = selected_window
                    label = previous.get('time_label')
            direction = previous.get('direction') if previous.get('direction') in ('incoming', 'outgoing', 'all') else direction
            order = previous.get('order') if previous.get('order') in ('asc', 'desc') else order
            if literal is None:
                old_filters = previous.get('filters', {})
                if isinstance(old_filters, dict):
                    literal = old_filters.get('body_contains') if isinstance(old_filters.get('body_contains'), str) else None
                    if not sender_ids and not scope_followup:
                        allowed = {identifier for alias in effective_scope for identifier in catalog.get(alias, {}).get('sender_names', {})}
                        old_ids = set(old_filters.get('sender_ids', []))
                        sender_ids = sorted(old_ids & allowed)
                        if old_ids and not sender_ids:
                            clarification = '上一轮发送人已不在当前可识别范围内，请重新明确发送人或会话。'
            inherited = True
    if route in ('local_stats', 'mixed'):
        # Unsupported qualifiers must not disappear behind a plausible total.
        negative = re.sub(r'有没有', '', text)
        if re.search(r'(?:谁|哪些人|哪些客户|哪个群|哪些群).{0,20}(?:没有|没|未|从不|不曾|从未).{0,12}(?:发|联系|说|消息|信息|来信)|(?:没有|无|零)(?:任何)?(?:消息|信息|发言)|不包含|不含|未提到|没有提到|排除', negative):
            clarification = '当前仅有已采消息，没有完整联系人活动全集或排除条件，暂不能精确回答未发消息的对象或反向筛选。'
        if re.search(r'上午|下午|晚上|凌晨|中午|早上|傍晚|深夜|周[一二三四五六日天末]|星期[一二三四五六日天]|(?:点|时|分)(?:到|至)|\d{1,2}:\d{2}', text):
            clarification = '这个问题还限定了星期或一天内的时段，请先明确完整日期；当前支持整日、周、月和日期区间统计。'
        if re.search(r'(?:超过|至少|不少于|低于|小于|大于|不超过|至多|少于|多于)\s*' + NUMBER, text):
            clarification = '当前本机统计尚不支持数量阈值筛选，可以先查询消息条数或发送人活跃排行。'
        if re.search(r'联系次数|沟通次数|(?:联系|沟通|聊)(?:了|过)?几次|聊了多久|沟通时长', text):
            clarification = '当前数据记录的是消息，尚不能可靠划分一次完整沟通或统计实际沟通时长；可以查询消息条数。'
        clauses = [text]
        if route == 'mixed':
            clauses = [clause for clause in re.split(r'并且|并|然后|再|顺便|同时|以及|[,，;；]', text)
                       if not ANALYTIC.search(clause) and (COUNT.search(clause) or RANK.search(clause) or LATEST.search(clause))
                       and (MESSAGE.search(clause) or SENDER.search(clause) or CONVERSATION.search(clause))]
        residues = [_metadata_residue(clause, catalog) for clause in clauses]
        remaining = ''.join(residues)
        if remaining and not clarification:
            if _unresolved_entity(remaining, masked):
                clarification = '无法确认问题中提到的联系人或会话，请使用当前范围内可识别的完整名称，或先选择对应会话。'
            else:
                route, metric, group_by = 'analysis', None, None
    if clarification:
        route = 'clarify'
    return {'schema_version': SCHEMA_VERSION, 'route': route, 'metric': metric, 'group_by': group_by,
            'direction': direction, 'scope': effective_scope, 'time_range': _bounds(window),
            'time_label': label or selected_label,
            'filters': {'sender_ids': sender_ids, 'body_contains': literal},
            'limit': limit, 'order': order, 'requires_model': route in ('analysis', 'mixed'),
            'retrieval_query': previous.get('retrieval_query', question.strip()) if inherited and route in ('analysis', 'mixed') else question.strip(), 'warnings': warnings,
            'clarification': clarification, 'source': 'local', 'inherited': inherited}
