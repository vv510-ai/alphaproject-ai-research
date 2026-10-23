"""Explain a revalidated optional rule without turning every rule into a price fall."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import re

from .analysis_context import empty_context, enrich_evidence, normalize_context
from .explanation_evidence import CAUSE_LIMIT, MISSING, canonical, number
from .extended_rules import validate_extended_event
from .live_policy import proof_eligibility
from .securities import LookupFailure, market_for

PROMPT_VERSION = 'extended-event-explanation-1'
REQUIRED_REFS = {'change': {'F2', 'F3'}, 'trigger': {'F4'}, 'scope': {'F1', 'F5', 'F6'}}
UNITS = {'limit_up': '元', 'limit_down': '元', 'breakout': '元', 'breakdown': '元',
         'volume': '倍', 'amplitude': '%', 'event': '条'}


def freeze_extended_event(row):
    event, snapshot, quote, change = validate_extended_event(row)
    try:
        captured = datetime.fromisoformat(event['created_at'])
        generation = snapshot['watchlist_created_at']
        if (not isinstance(generation, str) or not generation or datetime.fromisoformat(generation).utcoffset() is None
                or market_for(event['symbol']) != event['market'] or snapshot.get('mode') != 'live'
                or type(snapshot['first_observation']) is not bool
                or quote.price_unit != '元' or quote.change_pct_unit != '%'
                or not isinstance(quote.source, str) or not quote.source.strip()):
            raise ValueError()
        eligibility = proof_eligibility(quote, snapshot['eligibility']['proof'], captured, generation)
        if not eligibility['eligible']:
            raise ValueError()
        if (Decimal(quote.change) != Decimal(quote.price) - Decimal(quote.previous_close)
                or Decimal(quote.change_pct) not in (change, change.quantize(Decimal('.01'), rounding=ROUND_HALF_UP))):
            raise ValueError()
        metric = snapshot['rule_evidence'].get('metrics')
        if metric:
            if (metric.get('raw_sha256') != eligibility['proof']['current']['raw_sha256']
                    or any(metric.get(key) != getattr(quote, key) for key in
                           ('source', 'source_url', 'source_reported_at', 'received_at'))):
                raise ValueError()
    except (KeyError, ValueError, TypeError, InvalidOperation, OverflowError):
        raise LookupFailure('invalid_extended_evidence', '扩展事件的价格、指标来源或原自选代次不一致，未调用模型。', 422) from None

    kind = snapshot['rule_type']
    direction = 'up' if change > 0 else 'down' if change < 0 else 'flat'
    pct, pct_display = number(change), format(change.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')
    unit = UNITS[kind]
    rule_values = {'rule_type': kind, 'metric_value': snapshot['metric_value'],
                   'metric_display': format(Decimal(snapshot['metric_value']), '.4f'),
                   'threshold': snapshot['threshold'], 'metric_unit': unit, 'rule_version': str(snapshot['rule_version'])}
    metric_data = snapshot['rule_evidence'].get('metrics', {})
    for key in ('high', 'low', 'volume_ratio', 'limit_up', 'limit_down'):
        value = metric_data.get(key)
        if isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?', value):
            rule_values[key] = value
    history = snapshot['rule_evidence'].get('history')
    if history:
        rule_values.update(lookback_days=str(history['lookback_days']), high_close=history['high_close'],
                           low_close=history['low_close'], history_through=history['bars'][-1]['date'])
    if kind == 'amplitude':
        rule_values.update(previous_close=quote.previous_close, percent_scale='100')
    if kind == 'event':
        news = snapshot['rule_evidence']['news']
        rule_values.update(published_at=news['published_at'], received_at=news['received_at'])
    trigger_text = snapshot['condition_text'] + f' 指标单位：{unit}；规则 v{snapshot["rule_version"]}。'
    first = '首次观察已满足本类条件，不能确定条件首次出现时刻' if snapshot['first_observation'] else '本次采样已满足本类条件，采样时间不等于条件首次出现时刻'
    facts = [
        {'id': 'F1', 'label': '事件类别', 'text': '已保存的真实规则事件：' + snapshot['rule_label'],
         'values': {'mode': 'live', 'rule_type': kind}},
        {'id': 'F2', 'label': '当时的价格', 'text': f'现价 {quote.price} 元；昨日收盘价 {quote.previous_close} 元。',
         'values': {'price': quote.price, 'previous_close': quote.previous_close}},
        {'id': 'F3', 'label': '程序复算的价格变化',
         'text': f'（{quote.price} − {quote.previous_close}）÷ {quote.previous_close} × 100% = {pct}%；显示约 {pct_display}%。该价格变化与触发指标分开。',
         'values': {'price': quote.price, 'previous_close': quote.previous_close, 'change_pct': pct,
                    'change_pct_display': pct_display, 'absolute_change_pct': number(abs(change)), 'percent_scale': '100'}},
        {'id': 'F4', 'label': '本次触发条件', 'text': trigger_text, 'values': rule_values},
        {'id': 'F5', 'label': '这份依据的时间',
         'text': f'来源时间 {quote.source_reported_at}；接收时间 {quote.received_at}；事件保存时间 {event["created_at"]}。',
         'values': {'source_reported_at': quote.source_reported_at, 'received_at': quote.received_at, 'event_created_at': event['created_at']}},
        {'id': 'F6', 'label': '检测与重复边界', 'text': first + '；同一股票同一规则类型同日最多记录一次。',
         'values': {'first_observation': str(snapshot['first_observation']).lower(), 'max_per_type_per_day': '1'}},
    ]
    echo = {'market': event['market'], 'symbol': event['symbol'], 'mode': 'live', 'rule_type': kind,
            'price': quote.price, 'previous_close': quote.previous_close, 'change_pct': pct,
            'change_pct_display': pct_display, 'direction': direction, 'metric_value': snapshot['metric_value'],
            'metric_unit': unit, 'threshold': snapshot['threshold'], 'rule_version': str(snapshot['rule_version']),
            'source_reported_at': quote.source_reported_at}
    missing = [dict(value) for value in MISSING]
    if kind == 'volume':
        missing[1]['reason'] = '已有来源量比，但其详细比较基准与同期成交量明细仍需核对，不能推断资金方向。'
    model_input = {'event_id': event['id'], 'facts': facts, 'factual_echo': echo,
                   'rule_evidence_hash': hashlib.sha256(canonical(snapshot['rule_evidence']).encode()).hexdigest(),
                   'cause_status': 'insufficient_evidence', 'missing_evidence_ids': [value['id'] for value in missing]}
    digest = hashlib.sha256(canonical(model_input).encode()).hexdigest()
    model_input['evidence_hash'] = digest
    evidence = {'event_id': event['id'], 'evidence_hash': digest, 'market': event['market'], 'symbol': event['symbol'],
                'name': quote.name, 'mode': 'live', 'rule_type': kind, 'rule_label': snapshot['rule_label'], 'facts': facts,
                'source': quote.source, 'source_url': quote.source_url, 'created_at': event['created_at'],
                'missing_evidence': missing, 'cause_limit': CAUSE_LIMIT, 'model_input': model_input,
                'trigger_evidence': snapshot['rule_evidence']}
    if kind == 'event':
        # The exact article which triggered the saved event is mandatory evidence,
        # even when a later research cache no longer includes it.
        context = empty_context(event['created_at'])
        context.update(snapshot_id='event:' + event['id'], created_at=event['created_at'], selection_notes=[])
        section = next(item for item in context['sections'] if item['key'] == 'news')
        section.update(status='available', source=news.get('source'), source_url=news.get('url'),
                       received_at=news.get('received_at'), as_of=news.get('published_at'), error_message=None, items=[news])
        context = normalize_context(context, event['created_at'])
        selected = next(item for item in context['sections'] if item['key'] == 'news')['items']
        if not selected or not selected[0]['ai_eligible']:
            raise LookupFailure('invalid_extended_evidence', '触发资讯缺少可安全引用的完整身份、来源或时间，未调用模型。', 422)
        evidence['trigger_research_evidence'] = selected
        evidence = enrich_evidence(evidence, context)
    return evidence


SYSTEM_PROMPT = """你是中文规则记录阅读助手。解释本次保存的扩展规则事件，不联网，不用公司常识、记忆或想象补原因，不预测或提供买卖建议。
只返回JSON：{"event_id":"照抄输入","evidence_hash":"照抄输入","factual_echo":{逐字照抄全部键和值},"cause_status":"照抄输入","missing_evidence_ids":["照抄输入数组"],"statements":[{"kind":"change","text":"按F2和F3说明当时价格与昨收差异","evidence_ids":["F2","F3"]},{"kind":"trigger","text":"按F4解释真正触发条件和指标单位，不把指标当作涨跌幅","evidence_ids":["F4"]},{"kind":"scope","text":"说明这是真实规则记录、当时采样及同股同类型同日去重边界","evidence_ids":["F1","F5","F6"]}]}。
每段30至180中文字符，只用引用事实中的数字，可增加已有F引用。factual_echo数字保持字符串。change优先用change_pct_display并加“约”，方向根据direction的up/down/flat，而不是rule_type。
trigger须按rule_type处理：volume为来源量比，单位倍；amplitude为（最高-最低）除昨收的振幅，单位%；这两类都要求超过阈值，不含正好等于。event为新资料触发，指标1是条目数，不能写1%的涨跌。volume/amplitude/event的trigger段不写“上涨”或“下跌”，价格方向只在change段说明。
limit_up/limit_down只说明触及来源上限价/下限价，不能说持续封住，也不猜法定比例；breakout/breakdown说明超出前N个交易日不复权收盘价边界，除权除息等影响仍需人工核对。不能将采样当作确切首次触线时刻。
不使用HTML、链接、外部原因、主力/资金流/利好/利空等推断；只解释固定依据。对应资料如存在将按附加规则单独引用，它不证明因果。"""
