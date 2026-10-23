"""Typed, immutable quote evidence; never invent an anomaly or imply realtime data."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re

from .explanation_evidence import canonical, number
from .quotes import CHINA_TIME, Quote
from .securities import LookupFailure, market_for

PROMPT_VERSION = 'quote-explanation-1'
REQUIRED_REFS = {'change': {'F2', 'F3'}, 'context': {'F1', 'F4'}, 'scope': {'F5', 'F6'}}
SYSTEM_PROMPT = """你是中文报价阅读助手。只解释一份固定报价，不把它说成异动触发记录。不使用公司常识、新闻记忆、联网、预测或交易建议。
只返回 JSON：{"snapshot_id":"照抄输入","evidence_hash":"照抄输入","factual_echo":{逐字照抄输入全部键和值},"cause_status":"insufficient_evidence","missing_evidence_ids":["news","volume","sector"],"statements":[{"kind":"change","text":"通俗说明现价、昨收和程序复算变化，只描述这份报价","evidence_ids":["F2","F3"]},{"kind":"context","text":"说明这是固定报价，未据此判断触发提醒，实时性未验证","evidence_ids":["F1","F4"]},{"kind":"scope","text":"说明这份报价对应来源时间，只代表当时，不能说明持续走势","evidence_ids":["F5","F6"]}]}
每段中文30到180字；只用引用事实里有的数字，可增加其他已有引用。数字保持字符串。通俗正文优先用 change_pct_display 保留两位小数的涨跌幅，并用“约”说明取舍；factual_echo 仍全部照抄。方向 up 为上涨，down 为下跌，flat 为持平。context 必须说明“实时性未验证”；scope 必须明确“当时”或“来源时间”。不能将历史报价说成现在的行情。不使用 HTML、链接、买卖建议、外部涨跌原因或想象中的原因。输入没有新闻、公告、成交量或板块对照，不猜测。"""


def validate_quote(payload, market, symbol, frozen_at):
    try:
        quote = Quote(**{**payload, 'raw_text': ''})
        if any(not isinstance(value, str) or not re.fullmatch(r'-?[0-9]{1,12}(?:\.[0-9]{1,8})?', value)
               for value in (quote.price, quote.previous_close, quote.change, quote.change_pct)):
            raise ValueError()
        at = datetime.fromisoformat(frozen_at)
        source_at, received = map(datetime.fromisoformat, (quote.source_reported_at, quote.received_at))
        price, previous, change, percent = map(Decimal, (quote.price, quote.previous_close, quote.change, quote.change_pct))
        if (quote.market != market or quote.symbol != symbol or market_for(symbol) != market
                or not all(t.tzinfo and t.utcoffset() is not None for t in (at, source_at, received))
                or not all(n.is_finite() for n in (price, previous, change, percent))
                or price <= 0 or previous <= 0 or quote.price_unit != '元' or quote.change_pct_unit != '%'
                or not isinstance(quote.source, str) or not quote.source.strip()
                or not isinstance(quote.name, str) or not quote.name.strip()
                or (received - at).total_seconds() > 120 or (source_at - received).total_seconds() > 120
                or (source_at - at).total_seconds() > 0):
            raise ValueError()
        computed = (price - previous) / previous * 100
        if change != price - previous or percent not in (computed, computed.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)):
            raise ValueError()
        return quote, computed
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        raise LookupFailure('invalid_quote_evidence', '报价的身份、时间、单位或数字不完整/矛盾，请先重新获取并核对；没有调用模型。', 422) from None


def freeze_quote(row):
    quote, change = validate_quote(json.loads(row['payload']), row['market'], row['symbol'], row['created_at'])
    source_day = datetime.fromisoformat(quote.source_reported_at).astimezone(CHINA_TIME).date().isoformat()
    frozen_day = datetime.fromisoformat(row['created_at']).astimezone(CHINA_TIME).date().isoformat()
    direction = 'up' if change > 0 else 'down' if change < 0 else 'flat'
    display_pct = format(change.quantize(Decimal('.01'), rounding=ROUND_HALF_UP), '.2f')
    facts = [
        {'id': 'F1', 'label': '依据类别', 'text': '用户固定的一份报价，未据此判断是否触发异动；不是触发事件。', 'values': {'mode': 'quote'}},
        {'id': 'F2', 'label': '保存的价格', 'text': f'报价 {quote.price} 元；昨收 {quote.previous_close} 元。', 'values': {'price': quote.price, 'previous_close': quote.previous_close}},
        {'id': 'F3', 'label': '程序复算的变化', 'text': f'（{quote.price} − {quote.previous_close}）÷ {quote.previous_close} × 100% = {number(change)}%；保留两位小数为 {display_pct}%。',
         'values': {'price': quote.price, 'previous_close': quote.previous_close, 'change_pct': number(change), 'change_pct_display': display_pct,
                    'absolute_change_pct': number(abs(change)), 'absolute_change_pct_display': number(abs(Decimal(display_pct))), 'percent_scale': '100'}},
        {'id': 'F4', 'label': '数据时效', 'text': ('固定时已是非今日报价' if source_day < frozen_day else '固定时来源日期为当天') + '；实时性未验证，交易状态未知。', 'values': {'source_date': source_day, 'frozen_date': frozen_day}},
        {'id': 'F5', 'label': '来源与保存时间', 'text': f'来源时间 {quote.source_reported_at}；接收时间 {quote.received_at}；固定时间 {row["created_at"]}。',
         'values': {'source_reported_at': quote.source_reported_at, 'received_at': quote.received_at, 'frozen_at': row['created_at']}},
        {'id': 'F6', 'label': '解读边界', 'text': '只描述这一份报价与昨收的差异；不能推出持续走势、准确变动时刻或涨跌原因。', 'values': {'scope': 'saved_quote_only'}},
    ]
    echo = {'market': row['market'], 'symbol': row['symbol'], 'mode': 'quote', 'direction': direction,
            'price': quote.price, 'previous_close': quote.previous_close, 'change_pct': number(change), 'change_pct_display': display_pct,
            'source_reported_at': quote.source_reported_at}
    missing = [
        {'id': 'news', 'label': '对应时间的新闻与公告', 'reason': '不能从报价倒推出一条新闻。'},
        {'id': 'volume', 'label': '对应时段的成交量', 'reason': '本次固定报价没有包含这个字段。'},
        {'id': 'sector', 'label': '同时间的板块和市场对照', 'reason': '不能判断是否为共同变化。'},
    ]
    model_input = {'snapshot_id': row['id'], 'facts': facts, 'factual_echo': echo,
                   'cause_status': 'insufficient_evidence', 'missing_evidence_ids': [m['id'] for m in missing]}
    digest = hashlib.sha256(canonical(model_input).encode()).hexdigest()
    model_input['evidence_hash'] = digest
    return {'snapshot_id': row['id'], 'evidence_hash': digest, 'market': row['market'], 'symbol': row['symbol'],
            'name': quote.name, 'mode': 'quote', 'facts': facts, 'source': quote.source, 'source_url': quote.source_url,
            'created_at': row['created_at'], 'missing_evidence': missing,
            'cause_limit': '仅凭这份报价，无法判断具体涨跌原因或后续走势。', 'model_input': model_input}
