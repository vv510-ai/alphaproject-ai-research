"""Build a factual previous-session summary without asking a language model."""

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .daily import DailyFailure
from .quotes import CHINA_TIME
from .analysis_context import select_evidence


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def list_digest(items):
    return hashlib.sha256(encoded(items).encode()).hexdigest()


def freeze_items(rows):
    return [{key: row[key] for key in ('market', 'symbol', 'name', 'created_at')} for row in rows]


def missing(item, code, reason):
    return {**item, 'status': 'missing', 'reason_code': code, 'reason': reason, 'data': None}


def from_history(item, history, target_date, now, origin):
    """Validate cached/fetched selected data again at the summary boundary."""
    try:
        if history['market'] != item['market'] or history['symbol'] != item['symbol']:
            raise ValueError('identity')
        if history['adjustment'] != 'none' or history['price_unit'] != '元' or history['volume_unit'] != '股':
            raise ValueError('units')
        received = datetime.fromisoformat(history['received_at'])
        if received.tzinfo is None or received.utcoffset() is None:
            raise ValueError('timezone')
        if received > now + timedelta(seconds=120) or received < datetime.combine(date.fromisoformat(target_date), time(15), CHINA_TIME):
            raise ValueError('receive time')
        matches = [bar for bar in history['bars'] if bar['date'] == target_date]
        if len(matches) != 1:
            raise DailyFailure('briefing_date_missing', f'缺少 {target_date} 的有效日线；不沿用更早数据，停牌或来源缺失的原因尚未核实。')
        bar = matches[0]
        if any(not isinstance(bar[key], str) or not re.fullmatch(r'[0-9]{1,13}(?:\.[0-9]{1,8})?', bar[key])
               for key in ('open', 'close', 'high', 'low', 'volume', 'raw_volume')):
            raise ValueError('numbers')
        opening, closing, high, low = [Decimal(bar[key]) for key in ('open', 'close', 'high', 'low')]
        if min(opening, closing, high, low) <= 0 or not low <= min(opening, closing) <= max(opening, closing) <= high:
            raise ValueError('OHLC')
        volume, raw = Decimal(bar['volume']), Decimal(bar['raw_volume'])
        factor = 1 if item['symbol'].startswith('688') else 100
        if volume != volume.to_integral_value() or raw * factor != volume or history['volume_multiplier'] != factor:
            raise ValueError('volume')
        if history['raw_volume_unit'] != ('股' if factor == 1 else '手'):
            raise ValueError('raw volume unit')
        if not isinstance(history['source'], str) or not history['source'] or not isinstance(history['source_url'], str):
            raise ValueError('source')
        data = {'trade_date': target_date, 'close': str(closing), 'volume': str(int(volume)),
                'price_unit': '元', 'volume_unit': '股', 'adjustment': 'none', 'adjustment_label': '不复权',
                'bar': {key: bar[key] for key in ('date', 'open', 'close', 'high', 'low', 'volume', 'raw_volume')},
                'raw_volume_unit': history['raw_volume_unit'], 'volume_multiplier': factor,
                'source': history['source'], 'source_url': history['source_url'], 'received_at': history['received_at'],
                'origin': origin}
        return {**item, 'status': 'covered', 'reason_code': None, 'reason': None, 'data': data}
    except DailyFailure:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        raise DailyFailure('briefing_invalid_data', '这份日线的身份、时间、价格或成交量单位未能核对，本次不采用。') from None


def make_summary(snapshot, items, completed_at):
    covered = sum(item['status'] == 'covered' for item in items)
    total = len(items)
    status = 'empty' if not total else 'failed' if not covered else 'succeeded' if covered == total else 'partial'
    result = {'status': status, 'report_date': snapshot['calendar']['date'],
            'data_trade_date': snapshot['calendar']['previous_trade_date'],
            'data_cutoff_label': f"{snapshot['calendar']['previous_trade_date']} 收盘日线",
            'watchlist_frozen_at': snapshot['frozen_at'], 'completed_at': completed_at,
            'coverage': {'total': total, 'covered': covered, 'missing': total - covered},
            'items': items, 'calendar': snapshot['calendar'], 'mode': 'data_summary',
            'ai_status': 'not_used_by_choice', 'model_cost': '0',
            'note': '按已核对数据整理，未调用 AI；没有新闻、原因推断或买卖建议。数据日期不等于本次生成时间。'}
    context = snapshot.get('research_context')
    if context is not None:
        sections = context['sections']
        new_items = [item for section in sections for item in section['items'] if item.get('change_kind') == 'new']
        updated_items = [item for section in sections for item in section['items'] if item.get('change_kind') == 'updated']
        available = [section['key'] for section in sections if section['status'] in ('available', 'partial') and section['items']]
        missing_sections = [section['key'] for section in sections if section['key'] not in available]
        result.update(research_context=context, research_evidence=select_evidence(context, limit=24),
                      research_cutoff=context['cutoff'], research_used_count=0,
                      research_coverage={'available_sections': available, 'missing_sections': missing_sections,
                                         'new_count': len(new_items), 'updated_count': len(updated_items)},
                      narrative=research_narrative(sections, new_items, updated_items),
                      trace=snapshot.get('trace', []),
                      note='按固定日线与已保存的市场、资讯和日程资料整理，未调用 AI。新闻只作背景，不推断确定涨跌原因或提供买卖建议；缺项和过期资料单独保留。')
        result['data_cutoff_label'] += '；研究资料截至 ' + context['cutoff']
    return result


def research_narrative(sections, new_items, updated_items):
    paragraphs = []
    for section in sections:
        title, values = section['title'], section['items']
        if not values:
            paragraphs.append(f'{title}：本次未取得可核对资料。')
            continue
        samples = []
        for item in values[:2]:
            label = item['summary'] if item.get('summary') and len(item['summary']) <= 240 else item['title']
            suffix = '（已过期，仅留档）' if item.get('stale') else '（仅作日程展示，发布时间未知）' if item.get('eligibility_note') and section['key'] == 'calendar' else ''
            samples.append(label + suffix)
        paragraphs.append(title + '：' + '；'.join(samples))
    paragraphs.append(f'本次资料中新增 {len(new_items)} 条、内容更新 {len(updated_items)} 条；“新增”指相对本系统前次快照，不表示全部刚发布，也不保证资讯已穷尽。')
    return '\n\n'.join(paragraphs)


def message_body(report_id, output):
    count = output['coverage']
    lines = [f"【AlphaProject Lab｜{output['report_date']} 早盘简报】",
             f"数据截至：{output['data_trade_date']} 收盘；非今日实时行情。",
             f"生成时自选 {count['total']} 只；覆盖 {count['covered']} 只；缺失 {count['missing']} 只。"]
    # Keep the platform text bounded, and disclose omitted rows rather than hiding them.
    for item in output['items'][:12]:
        identity = f"{item['name']} {item['market']}{item['symbol']}"
        data = item['data']
        lines.append(f"{identity}：收盘 {data['close']} 元；成交 {int(data['volume']):,} 股。" if data else f"{identity}：未覆盖，{item['reason']}")
    if count['total'] > 12:
        lines.append(f"消息列出前 12 只，完整 {count['total']} 只清单与来源可在学习版早盘简报查看。")
    if output.get('research_context'):
        lines.append('研究资料截止：' + output['research_cutoff'])
        for section in output['research_context']['sections']:
            entries = section['items']
            if entries:
                item = entries[0]
                lines.append(f"{section['title']}：{item['title']}" + ('（过期，仅留档）' if item.get('stale') else ''))
            else:
                lines.append(f"{section['title']}：本次缺项。")
        research_count = output['research_coverage']
        lines.append(f"相对前次资料：新增 {research_count['new_count']} 条；更新 {research_count['updated_count']} 条。完整来源和发布时间见网页。")
    lines.extend(['价格不复权；按保存资料整理，未调用 AI。相关报道不证明涨跌原因，没有买卖建议。',
                  f'简报标识：{report_id}', '请到学习版“早盘简报”核对完整清单、日期、来源及缺失项。'])
    return '\n'.join(lines)
