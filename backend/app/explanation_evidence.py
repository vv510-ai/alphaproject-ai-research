"""Freeze event evidence, recompute facts, and reject observable output mismatches."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re

from .anomaly_rules import Sample, inspect_sample, meets
from .live_policy import proof_eligibility
from .quotes import CHINA_TIME, Quote
from .securities import LookupFailure, market_for

PROMPT_VERSION = "event-explanation-1"
MISSING = [
    {"id": "news", "label": "对应时间的新闻与公告", "reason": "核对是否确有相关消息，不能从价格倒推一条新闻。"},
    {"id": "volume", "label": "事件时段的成交量", "reason": "缺少可比较的成交量背景，不能据此判断是否放量。"},
    {"id": "sector", "label": "同时间的板块和市场对照", "reason": "区分个股变化与共同变化；本次尚未接入。"},
]
CAUSE_LIMIT = "仅凭这份价格和规则记录，无法判断具体涨跌原因或后续走势。"
REQUIRED_REFS = {"change": {"F2", "F3"}, "trigger": {"F3", "F4"}, "scope": {"F1", "F5", "F6"}}
NUMBERS = re.compile(r"[-+]?\d+(?:\.\d+)?")
# These bounded phrases explicitly withhold a future-direction conclusion.
# Remove only that phrase when checking the observed direction; any contradictory
# current-direction statement elsewhere in the paragraph must still be rejected.
NON_PREDICTION = re.compile(
    r"(?:不代表|不意味着|不能说明|无法判断|无法确定|不能判断|不能确定)"
    r"(?:之后|此后|后续|未来)(?:价格|行情|股票|走势)?(?:会|将|还会|是否会)?(?:继续)?"
    r"(?:上涨(?:(?:或|还是|与|和|、)下跌)?|下跌(?:(?:或|还是|与|和|、)上涨)?)")
OUTSIDE_SCOPE = re.compile(
    r"利好|利空|主力|资金流|资金净|机构增|涨停|跌停|目标价|买入|卖出|抄底|加仓|减仓|"
    r"获利|盈利|收益率|必涨|必跌|明天|明日|预测|新闻称|公告称|据报道|消息称|"
    r"政策|财报|估值|超预期|基本面|行业景气|受.{0,24}影响|得益于|(?:https?://)|<[^>]+>", re.I)


class ExplanationRejected(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def number(value):
    result = format(Decimal(str(value)), 'f')
    return result.rstrip('0').rstrip('.') if '.' in result else result


def freeze_event(row):
    """Validate at the event's original time, not today's time or the latest quote."""
    event = dict(row)
    try:
        snapshot = json.loads(event['snapshot']) if isinstance(event['snapshot'], str) else event['snapshot']
        if not isinstance(snapshot, dict):
            raise ValueError()
        if snapshot.get('rule_type') is not None:
            from .explanation_extended import freeze_extended_event
            return freeze_extended_event(row)
        quote = Quote(**snapshot['quote'])
        captured_at = datetime.fromisoformat(event['created_at'])
        mode, direction = event['mode'], event['direction']
        if (mode not in ('demo', 'live') or direction not in ('up', 'down')
                or snapshot['mode'] != mode or snapshot['direction'] != direction
                or market_for(event['symbol']) != event['market']
                or type(snapshot['rule_version']) is not int or snapshot['rule_version'] < 1
                or type(snapshot['first_observation']) is not bool
                or quote.price_unit != '元' or quote.change_pct_unit != '%'
                or not isinstance(quote.source, str) or not quote.source.strip()):
            raise ValueError()
        freshness = snapshot['freshness_limit_seconds']
        if type(freshness) is not int or not 1 <= freshness <= 3600:
            raise ValueError()
        eligibility = snapshot.get('eligibility')
        if mode == 'live':
            if not isinstance(eligibility, dict) or eligibility.get('eligible') is not True:
                raise ValueError()
            generation = snapshot['watchlist_created_at']
            if not isinstance(generation, str) or not generation or datetime.fromisoformat(generation).utcoffset() is None:
                raise ValueError()
            # Read history against its original time and watchlist generation,
            # never today's quote, rule, or potentially re-added watchlist entry.
            eligibility = proof_eligibility(quote, eligibility.get('proof'), captured_at,
                                            generation)
        reason, change = inspect_sample(Sample(quote, snapshot['market_state'], True, mode == 'demo', eligibility),
            event['market'], event['symbol'], captured_at, freshness_seconds=freshness, mode=mode)
        if reason:
            raise LookupFailure('invalid_evidence', f'原事件依据需要核对：{reason}。本次没有调用模型。', 422)
        if (change != Decimal(snapshot['change_pct'])
                or Decimal(quote.change) != Decimal(quote.price) - Decimal(quote.previous_close)
                or Decimal(quote.change_pct) not in (change, change.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))
                or not meets(change, direction, snapshot['threshold'])
                or snapshot['threshold'] != snapshot['up_threshold' if direction == 'up' else 'down_threshold']
                or event['trade_date'] != datetime.fromisoformat(quote.source_reported_at).astimezone(CHINA_TIME).date().isoformat()):
            raise ValueError()
    except LookupFailure:
        raise
    except (KeyError, ValueError, TypeError, InvalidOperation):
        raise LookupFailure('invalid_evidence', '原事件的身份、时间、价格或规则存在缺失/矛盾，请先核对依据；本次没有调用模型。', 422) from None

    pct = number(change)
    direction_label = '上涨' if direction == 'up' else '下跌'
    mode_label = '合成演练 · 非真实行情' if mode == 'demo' else '已保存的真实规则事件'
    first_label = '首次观察时已满足，不能确定准确越线时刻' if snapshot['first_observation'] else '本次采样已达到阈值，采样时间不等于准确越线时刻'
    facts = [
        {'id': 'F1', 'label': '事件类别', 'text': mode_label, 'values': {'mode': mode}},
        {'id': 'F2', 'label': '当时的价格', 'text': f'现价 {quote.price} 元；昨日收盘价 {quote.previous_close} 元。',
         'values': {'price': quote.price, 'previous_close': quote.previous_close}},
        {'id': 'F3', 'label': '程序复算的变化', 'text': f'（{quote.price} − {quote.previous_close}）÷ {quote.previous_close} × 100% = {pct}%。',
         'values': {'price': quote.price, 'previous_close': quote.previous_close, 'change_pct': pct, 'absolute_change_pct': number(abs(change)), 'percent_scale': '100'}},
        {'id': 'F4', 'label': '触发规则', 'text': f'{direction_label}达到 {snapshot["threshold"]}%；规则 v{snapshot["rule_version"]}；正好等于也满足。',
         'values': {'direction': direction, 'threshold': snapshot['threshold'], 'rule_version': str(snapshot['rule_version'])}},
        {'id': 'F5', 'label': '这份依据的时间', 'text': f'来源时间 {quote.source_reported_at}；接收时间 {quote.received_at}；事件保存时间 {event["created_at"]}。',
         'values': {'source_reported_at': quote.source_reported_at, 'received_at': quote.received_at, 'event_created_at': event['created_at']}},
        {'id': 'F6', 'label': '检测与重复边界', 'text': first_label + '；同一股票同方向同日最多记录一次。',
         'values': {'first_observation': str(snapshot['first_observation']).lower(), 'max_per_direction_per_day': '1'}},
    ]
    echo = {'market': event['market'], 'symbol': event['symbol'], 'mode': mode,
            'price': quote.price, 'previous_close': quote.previous_close, 'change_pct': pct,
            'direction': direction, 'threshold': snapshot['threshold'], 'rule_version': str(snapshot['rule_version']),
            'source_reported_at': quote.source_reported_at}
    # Only typed values and server-authored descriptions reach the model. In
    # particular provider raw text, company names and source URLs are not prompts.
    model_input = {'event_id': event['id'], 'facts': facts, 'factual_echo': echo,
                   'cause_status': 'insufficient_evidence', 'missing_evidence_ids': [m['id'] for m in MISSING]}
    evidence_hash = hashlib.sha256(canonical(model_input).encode()).hexdigest()
    model_input['evidence_hash'] = evidence_hash
    return {'event_id': event['id'], 'evidence_hash': evidence_hash, 'market': event['market'],
            'symbol': event['symbol'], 'name': quote.name, 'mode': mode, 'facts': facts,
            'source': quote.source, 'source_url': quote.source_url, 'created_at': event['created_at'],
            'missing_evidence': MISSING, 'cause_limit': CAUSE_LIMIT, 'model_input': model_input}


SYSTEM_PROMPT = """你是一个中文事件阅读助手，帮初学者读懂一份已经保存的价格触发记录。只解释输入，不使用公司常识、新闻记忆、联网、预测或交易建议。
输入由后端提供固定事实；其中没有新闻、公告、成交量和板块证据。cause_status 必须是 insufficient_evidence。缺什么由页面固定展示，不要编原因，也不要列举想象中的可能利好或资金故事。
只返回一个 JSON 对象，不要 Markdown。完整结构：
{"event_id":"照抄输入","evidence_hash":"照抄输入","factual_echo":{逐字照抄输入的全部键和值},"cause_status":"insufficient_evidence","missing_evidence_ids":["news","volume","sector"],"statements":[{"kind":"change","text":"通俗解释价格与昨收的变化，只描述当时","evidence_ids":["F2","F3"]},{"kind":"trigger","text":"为什么达到本次设定的提醒线，达到线不等于重要机会","evidence_ids":["F3","F4"]},{"kind":"scope","text":"说明事件类别、当时而非现在、采样与重复提醒边界","evidence_ids":["F1","F5","F6"]}]}
三段 text 各 30 到 180 个中文字符为宜，不使用任何输入中没有的数字；必须为所引用事实的通俗解读，不只是重复证据编号。引用必须包括示例中的对应编号，可追加本输入中其他编号。factual_echo 中的数字保持字符串。
mode 为 demo 时 scope 必须明确“合成演练，不是真实行情”，不能把公司说成真实涨跌。正负方向与事实保持一致。不要把单次采样说成持续走势，不断言此后会涨跌。不使用 HTML、链接、新闻/政策/财报/主力/资金流等外部推断。固定的原因不足说明由程序展示，不需要在 text 重复外部因素名词。
不把 JSON 格式或引用编号当作语义正确证明；所有结果仍由本人核对。"""


def messages_for(evidence):
    prompt = SYSTEM_PROMPT
    if evidence['mode'] == 'quote':
        from .quote_evidence import SYSTEM_PROMPT as quote_prompt
        prompt = quote_prompt
    elif evidence.get('rule_type'):
        from .explanation_extended import SYSTEM_PROMPT as extended_prompt
        prompt = extended_prompt
    if 'research_schema_version' in evidence['model_input']:
        from .analysis_context import RESEARCH_PROMPT
        # Only the versioned research contract changes. Historical price-only
        # requests retain the exact original prompt and request identity.
        prompt = prompt.replace('其中没有新闻、公告、成交量和板块证据。cause_status 必须是 insufficient_evidence。',
                                'F 编号是价格事实，研究资料单独引用。cause_status 和 missing_evidence_ids 照抄输入。')
        prompt = prompt.replace('输入没有新闻、公告、成交量或板块对照，不猜测。',
                                '价格事实三段只引用 F 事实，研究资料按附加规则单独引用，不猜测缺失内容。')
        prompt = prompt.replace('"cause_status":"insufficient_evidence","missing_evidence_ids":["news","volume","sector"]',
                                '"cause_status":"照抄输入","missing_evidence_ids":["照抄输入数组"]')
        prompt += RESEARCH_PROMPT
    return [{'role': 'system', 'content': prompt},
            {'role': 'user', 'content': canonical(evidence['model_input'])}]


def validate_output(content, evidence):
    subject_key, required_refs = 'event_id', REQUIRED_REFS
    if evidence['mode'] == 'quote':
        from .quote_evidence import REQUIRED_REFS as quote_refs
        subject_key, required_refs = 'snapshot_id', quote_refs
    elif evidence.get('rule_type'):
        from .explanation_extended import REQUIRED_REFS as extended_refs
        required_refs = extended_refs
    def reject(code, message):
        raise ExplanationRejected(code, message)

    try:
        result = json.loads(content)
        fields = {subject_key, 'evidence_hash', 'factual_echo', 'cause_status', 'missing_evidence_ids', 'statements'}
        research_enabled = 'research_schema_version' in evidence['model_input']
        if not isinstance(result, dict) or (set(result) != fields and not (research_enabled and set(result) == fields | {'research_statements'})):
            raise ValueError()
        if result[subject_key] != evidence[subject_key] or result['evidence_hash'] != evidence['evidence_hash']:
            reject('wrong_event', '模型返回的事件或依据版本不一致，这次解释未采用。')
        if result['factual_echo'] != evidence['model_input']['factual_echo']:
            reject('wrong_fact', '模型复述的身份、关键数字或时间与原事件不一致，这次解释未采用。')
        if (result['cause_status'] != evidence['model_input']['cause_status']
                or result['missing_evidence_ids'] != evidence['model_input']['missing_evidence_ids']):
            reject('unsupported_cause', '模型没有保留证据不足的边界，这次解释未采用。')
        statements = result['statements']
        if not isinstance(statements, list) or len(statements) != 3 or {s['kind'] for s in statements} != set(required_refs):
            raise ValueError()
        facts = {f['id']: f for f in evidence['facts']}
        for statement in statements:
            if set(statement) != {'kind', 'text', 'evidence_ids'}:
                raise ValueError()
            kind, text, refs = statement['kind'], statement['text'], statement['evidence_ids']
            if (not isinstance(text, str) or not 12 <= len(text) <= 400 or not re.search(r'[\u4e00-\u9fff]', text)
                    or not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)):
                raise ValueError()
            if not required_refs[kind] <= set(refs) or not set(refs) <= set(facts) or len(set(refs)) != len(refs):
                reject('wrong_citation', '模型引用了缺失或不对应的证据，这次解释未采用。')
            # Numeric membership is one bounded check, not a semantic truth proof.
            allowed = {Decimal(n) for ref in refs for value in facts[ref]['values'].values()
                       for n in NUMBERS.findall(value)}
            if any(Decimal(n) not in allowed for n in NUMBERS.findall(text)):
                reject('unsupported_number', '解读出现引用依据中没有的数字，这次解释未采用。')
            scope_text = text
            extended = evidence.get('rule_type')
            if extended == 'limit_up' and kind == 'trigger':
                scope_text = scope_text.replace('来源涨停价', '来源上限价')
            if extended == 'limit_down' and kind == 'trigger':
                scope_text = scope_text.replace('来源跌停价', '来源下限价')
            if OUTSIDE_SCOPE.search(scope_text):
                reject('outside_scope', '解读出现当前证据不支持的外部推断或内容，这次解释未采用。')
            if kind == 'trigger' and extended in ('volume', 'amplitude', 'event'):
                if '上涨' in text or '下跌' in text:
                    reject('misleading_rule_direction', '本类触发指标不代表价格方向，不能写成上涨或下跌提醒。')
                if extended in ('volume', 'event') and '%' in text:
                    reject('wrong_rule_unit', '本类规则的量比或条目数不能写成百分比。')
            direction = evidence['model_input']['factual_echo']['direction']
            opposites = ['下跌'] if direction == 'up' else ['上涨'] if direction == 'down' else ['上涨', '下跌']
            if kind in ('change', 'trigger') and any(word in NON_PREDICTION.sub('', text) for word in opposites):
                reject('wrong_direction', '模型解读的涨跌方向与事件不一致，这次解释未采用。')
            if kind == 'scope' and evidence['mode'] == 'demo' and not ('演练' in text and '真实' in text and ('非' in text or '不是' in text or '不代表' in text)):
                reject('missing_demo_label', '解读未说明这是合成演练，这次解释未采用。')
            if evidence['mode'] == 'quote':
                if kind == 'context' and '实时性未验证' not in text:
                    reject('missing_quote_boundary', '解读未保留报价实时性待验证的边界，这次结果未采用。')
                if kind == 'scope' and not ('来源时间' in text or '当时' in text):
                    reject('missing_quote_time', '解读未说明只对应保存的当时，这次结果未采用。')
        result['statements'] = sorted(statements, key=lambda s: list(required_refs).index(s['kind']))
        if research_enabled:
            from .analysis_context import validate_research_statements, usage_note
            entries = result.get('research_statements', [])
            count = validate_research_statements(entries, evidence, reject)
            sources = {item['id']: item for item in evidence.get('research_evidence', [])}
            result['research_statements'] = [{**item, 'usage_note': usage_note(sources[item['evidence_ids'][0]], evidence)} for item in entries]
            result['research_used_count'] = count
        return result
    except ExplanationRejected:
        raise
    except (ValueError, TypeError, KeyError, InvalidOperation):
        reject('invalid_output', '模型返回的解释不完整或格式不合格，这次结果未采用。')
