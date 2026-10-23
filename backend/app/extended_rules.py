"""Optional rule arithmetic over the same frozen, qualified quote.

Independent implementation, informed by the original application's rule types.
Tencent optional fields were checked against direct payloads on 2026-10-23.
Source-reported limits are never inferred from a board/name percentage.
"""
from datetime import date,datetime,timedelta
from decimal import Decimal,InvalidOperation
import hashlib
import json
import re

from .quotes import CHINA_TIME,Quote,parse_quote
from .anomaly_rules import Sample,inspect_sample,threshold
from .live_policy import FRESHNESS_SECONDS
from .securities import LookupFailure
from .trading_calendar import TradingCalendar

DEFAULT_CONFIG={'limit_enabled':False,'breakout_enabled':False,'volume_enabled':False,'amplitude_enabled':False,
                'event_enabled':False,'volume_ratio':'3','amplitude_pct':'8','lookback_days':60}
KIND_FLAG={'limit_up':'limit_enabled','limit_down':'limit_enabled','breakout':'breakout_enabled',
           'breakdown':'breakout_enabled','volume':'volume_enabled','amplitude':'amplitude_enabled','event':'event_enabled'}
LABELS={'limit_up':'触及来源涨停价','limit_down':'触及来源跌停价','breakout':'突破历史收盘价',
        'breakdown':'跌破历史收盘价','volume':'量比提醒','amplitude':'振幅提醒','event':'新增相关资讯'}


def validate_config(config):
    if not isinstance(config,dict) or set(config)!=set(DEFAULT_CONFIG):
        raise LookupFailure('invalid_extended_rule','扩展规则字段不完整，请刷新后重试。',422)
    if any(type(config[key]) is not bool for key in DEFAULT_CONFIG if key.endswith('_enabled')):
        raise LookupFailure('invalid_extended_rule','规则开关必须为开启或关闭。',422)
    if type(config['lookback_days']) is not int or not 5<=config['lookback_days']<=120:
        raise LookupFailure('invalid_extended_rule','历史区间请填写 5 到 120 个交易日。',422)
    result=dict(config)
    for key in ('volume_ratio','amplitude_pct'):
        result[key]=format(threshold(config[key]),'f')
    return result


def positive(value,zero=False):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9]{1,13}(?:\.[0-9]{1,8})?',value):
        raise ValueError('missing or invalid optional number')
    number=Decimal(value)
    if number<0 or (not zero and number==0):raise ValueError('non-positive optional number')
    return number


def metrics(quote):
    """Unknown optional fields stay null; they do not invalidate the base price."""
    result={'high':None,'low':None,'volume_ratio':None,'limit_up':None,'limit_down':None,
            'source':quote.source,'source_url':quote.source_url,'source_reported_at':quote.source_reported_at,
            'received_at':quote.received_at,'errors':{},'raw_sha256':None}
    try:
        reparsed=parse_quote(quote.raw_text,quote.symbol,datetime.fromisoformat(quote.received_at))
        if any(getattr(reparsed,key)!=getattr(quote,key) for key in ('market','symbol','price','previous_close','source_reported_at')):
            raise ValueError('raw quote differs from qualified price')
        fields=quote.raw_text.split('"')[1].split('~')
        result['raw_sha256']=hashlib.sha256(quote.raw_text.encode()).hexdigest()
    except (LookupFailure,ValueError,IndexError,TypeError):
        result['errors']={'source':'原始报价无法与已核验价格对应，不使用可选指标。'}
        return result
    for key,index in (('high',33),('low',34),('limit_up',47),('limit_down',48),('volume_ratio',49)):
        try:result[key]=format(positive(fields[index].strip(),zero=key=='volume_ratio'),'f')
        except (IndexError,ValueError,InvalidOperation):result['errors'][key]='来源没有提供可核对的数值。'
    if result['high'] and result['low'] and not positive(result['low'])<=positive(quote.price)<=positive(result['high']):
        result['high']=result['low']=None
        result['errors']['range']='来源最高、最低与现价不一致。'
    if result['limit_up'] and result['limit_down']:
        low,high,previous,current=map(positive,(result['limit_down'],result['limit_up'],quote.previous_close,quote.price))
        if not low<previous<high or not low<=current<=high:
            result['limit_up']=result['limit_down']=None
            result['errors']['limits']='来源涨跌停价格与现价/昨收不一致，不按固定比例推断。'
    return result


def history_evidence(quote,daily,days,now):
    if not isinstance(daily,dict) or daily.get('market')!=quote.market or daily.get('symbol')!=quote.symbol:
        raise ValueError('缺少对应股票的历史日线，请获取日线。')
    if daily.get('adjustment')!='none' or daily.get('price_unit')!='元':
        raise ValueError('历史价格口径与当前规则不一致。')
    boundary=TradingCalendar().context(now.astimezone(CHINA_TIME).date())['previous_trade_date']
    if not boundary:raise ValueError('前一交易日无法核对。')
    received=datetime.fromisoformat(daily['received_at'])
    if received.utcoffset() is None or received>now:raise ValueError('历史数据接收时间无法核对。')
    if received < datetime.fromisoformat(boundary+'T15:00:00+08:00'):
        raise ValueError('历史数据取得早于前一交易日收盘，不能作为完整收盘区间。')
    bars=[bar for bar in daily['bars'] if bar['date']<=boundary]
    if len(bars)<days or bars[-1]['date']!=boundary:
        raise ValueError(f'需要截至前一交易日的 {days} 根日线；当前覆盖不足。')
    selected=bars[-days:]
    dates=[date.fromisoformat(bar['date']) for bar in selected]
    if dates!=sorted(set(dates)) or any(TradingCalendar().day(value)[0]!='open' for value in dates):
        raise ValueError('历史日线日期重复、顺序异常或不在已核验交易日内。')
    # Reject missing sessions rather than turning "60 records" into a false 60-day window.
    expected=[]
    cursor=dates[-1]
    while len(expected)<days:
        state,_=TradingCalendar().day(cursor)
        if state=='unknown':raise ValueError('历史区间跨入尚未核验的交易日历。')
        if state=='open':expected.append(cursor)
        cursor-=timedelta(days=1)
    if dates!=list(reversed(expected)):raise ValueError('历史区间有缺失交易日，暂不触发突破提醒。')
    closes=[positive(bar['close']) for bar in selected]
    return {'bars':[{'date':bar['date'],'close':bar['close']} for bar in selected],
            'high_close':format(max(closes),'f'),'low_close':format(min(closes),'f'),'lookback_days':days,
            'source':daily['source'],'source_url':daily['source_url'],'received_at':daily['received_at'],
            'adjustment':'none','price_unit':'元','note':'比较不复权历史收盘价，除权除息对价格的影响仍需人工核对。'}


def evaluate_rules(quote,config,now,*,daily=None,research=None,enabled_at=None,metric_evidence=None,history=None):
    """No persistence, network calls, or messaging. Returns a result for every enabled type."""
    config=validate_config(config)
    price=positive(quote.price)
    m=metric_evidence if metric_evidence is not None else metrics(quote)
    result=[]
    def row(kind,status,reason,metric=None,level=None,evidence=None):
        result.append({'rule_type':kind,'rule_label':LABELS[kind],'status':status,'reason':reason,
                       'metric_value':format(metric,'f') if metric is not None else None,
                       'threshold':format(level,'f') if level is not None else None,
                       'condition_text':reason,'rule_evidence':evidence or {}})
    if config['limit_enabled']:
        for kind,key in (('limit_up','limit_up'),('limit_down','limit_down')):
            level=m.get(key)
            if not level or not m.get('limit_up') or not m.get('limit_down'):
                row(kind,'blocked','缺少可核对的来源涨跌停价格，不按股票名称或固定百分比猜测。');continue
            level=positive(level)
            valid=positive(m['limit_down'])<positive(quote.previous_close)<positive(m['limit_up']) and positive(m['limit_down'])<=price<=positive(m['limit_up'])
            if not valid:row(kind,'blocked','来源涨跌停价格与当前价格不一致。');continue
            hit=price==level
            row(kind,'triggered' if hit else 'below',f'现价 {price} 元'+('已触及' if hit else '未触及')+f'来源{("涨停" if kind=="limit_up" else "跌停")}价 {level} 元。',price,level,{'metrics':m})
    if config['volume_enabled']:
        level=threshold(config['volume_ratio'])
        try:value=positive(m['volume_ratio'],zero=True)
        except (ValueError,TypeError,KeyError):row('volume','blocked','来源量比缺失，不把未知值当作零。')
        else:row('volume','triggered' if value>level else 'below',f'来源量比 {value} 倍'+('超过' if value>level else '未超过')+f'阈值 {level} 倍。',value,level,{'metrics':m})
    if config['amplitude_enabled']:
        level=threshold(config['amplitude_pct'])
        try:
            high,low=positive(m['high']),positive(m['low'])
            if not low<=price<=high:raise ValueError()
        except (ValueError,TypeError,KeyError):row('amplitude','blocked','最高最低价格缺失或与现价不一致。')
        else:
            value=(high-low)/positive(quote.previous_close)*100
            row('amplitude','triggered' if value>level else 'below',f'（最高 {high} − 最低 {low}）÷ 昨收 {quote.previous_close} × 100% = {value:.4f}%，'+('超过' if value>level else '未超过')+f'阈值 {level}%。',value,level,{'metrics':m})
    if config['breakout_enabled']:
        try:h=history or history_evidence(quote,daily,config['lookback_days'],now)
        except (ValueError,KeyError,TypeError) as error:
            for kind in ('breakout','breakdown'):row(kind,'blocked',str(error))
        else:
            for kind,key in (('breakout','high_close'),('breakdown','low_close')):
                level=positive(h[key]);hit=price>level if kind=='breakout' else price<level
                row(kind,'triggered' if hit else 'below',f'现价 {price} 元'+('已突破' if kind=='breakout' and hit else '已跌破' if hit else '未越过')+f'前 {config["lookback_days"]} 个交易日收盘价边界 {level} 元（不复权）。',price,level,{'history':h})
    if config['event_enabled']:
        candidates=[]
        if isinstance(research,dict):
            for section in research.get('sections',[]):
                if section.get('key') not in ('news','announcements') or section.get('status') not in ('available','partial'):continue
                for item in section.get('items',[]):
                    try:
                        published=datetime.fromisoformat(item['published_at']);received=datetime.fromisoformat(item['received_at'])
                        start=datetime.fromisoformat(enabled_at) if enabled_at else now
                        if any(value.utcoffset() is None for value in (published,received,start)):continue
                        if not start<=published<=received<=now or now-published>timedelta(hours=4):continue
                        url=item.get('url')
                        if not item.get('id') or not isinstance(url,str) or not url.startswith(('https://','http://')) or not item.get('title'):continue
                        candidates.append({key:item.get(key) for key in ('id','title','summary','url','published_at','received_at','source','content_fingerprint','change_kind')})
                    except (ValueError,TypeError,KeyError):continue
        if candidates:
            chosen=max(candidates,key=lambda item:datetime.fromisoformat(item['published_at']))
            row('event','triggered','启用后发布且已取得来源的相关资讯；标题命中或公司公告不等于已证明涨跌原因。',Decimal(1),Decimal(1),{'news':chosen,'enabled_at':enabled_at})
        else:row('event','below' if research and research.get('snapshot_id') else 'blocked','尚无启用之后发布的有效相关资讯；已有旧资讯不会补发。')
    return result


def validate_extended_event(row):
    """Recompute the saved trigger at its own timestamp before an AI explanation."""
    event=dict(row)
    try:
        snapshot=json.loads(event['snapshot']) if isinstance(event['snapshot'],str) else event['snapshot']
        kind=snapshot['rule_type']
        if event['mode']!='live' or kind not in KIND_FLAG or event['direction']!=kind or snapshot['direction']!=kind:
            raise ValueError()
        quote=Quote(**{**snapshot['quote'],'raw_text':''})
        captured=datetime.fromisoformat(event['created_at'])
        eligibility=snapshot['eligibility']
        reason,change=inspect_sample(Sample(quote,snapshot['market_state'],True,False,eligibility),event['market'],event['symbol'],captured,mode='live')
        if reason:raise ValueError(reason)
        if change!=Decimal(snapshot['change_pct']) or snapshot['freshness_limit_seconds']!=FRESHNESS_SECONDS:
            raise ValueError()
        if type(snapshot['rule_version']) is not int or snapshot['rule_version']<1:raise ValueError()
        config=validate_config(snapshot['config'])
        if not config[KIND_FLAG[kind]]:raise ValueError()
        ev=snapshot['rule_evidence']
        history=ev.get('history')
        if history:
            reconstructed={'market':quote.market,'symbol':quote.symbol,'bars':history['bars'],'adjustment':'none','price_unit':'元',
                           'source':history['source'],'source_url':history['source_url'],'received_at':history['received_at']}
            history=history_evidence(quote,reconstructed,config['lookback_days'],captured)
        research={'snapshot_id':'frozen','sections':[{'key':'news','status':'available','items':[ev['news']]}]} if 'news' in ev else None
        results=evaluate_rules(quote,config,captured,research=research,enabled_at=ev.get('enabled_at'),metric_evidence=ev.get('metrics',{}),history=history)
        matching=next(result for result in results if result['rule_type']==kind)
        if matching['status']!='triggered' or any(matching[key]!=snapshot[key] for key in ('metric_value','threshold','condition_text','rule_label')):
            raise ValueError()
        if event['trade_date']!=datetime.fromisoformat(quote.source_reported_at).astimezone(CHINA_TIME).date().isoformat():raise ValueError()
        return event,snapshot,quote,change
    except (KeyError,ValueError,TypeError,InvalidOperation,LookupFailure,StopIteration) as error:
        raise LookupFailure('invalid_extended_evidence','这条扩展事件的原始指标、时间或触发条件无法核对，未调用模型。',422) from error
