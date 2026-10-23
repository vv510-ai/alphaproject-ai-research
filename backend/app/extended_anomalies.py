"""The original rule families, integrated into the existing event/outbox ledger."""
from datetime import datetime,timezone
from decimal import Decimal
import json
import uuid

from .extended_rules import DEFAULT_CONFIG,KIND_FLAG,evaluate_rules,validate_config
from .live_policy import FRESHNESS_SECONDS,proof_eligibility
from .quotes import CHINA_TIME,Quote
from .securities import LookupFailure,market_for


class ExtendedAnomalies:
    def __init__(self,store,quotes,notifications,research=None,clock=None):
        self.store,self.quotes,self.notifications,self.research=store,quotes,notifications,research
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def initialize(self):
        with self.store.connect() as db,db:
            db.execute('''CREATE TABLE IF NOT EXISTS extended_anomaly_rules(
                market TEXT NOT NULL,symbol TEXT NOT NULL,watchlist_created_at TEXT NOT NULL,
                version INTEGER NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,config_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,last_checked_at TEXT,last_results_json TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY(market,symbol),FOREIGN KEY(market,symbol) REFERENCES watchlist(market,symbol) ON DELETE CASCADE
            )''')

    @staticmethod
    def public(row,item):
        if not row or row['watchlist_created_at']!=item['created_at']:
            return {'market':item['market'],'symbol':item['symbol'],'watchlist_created_at':item['created_at'],
                    'version':0,'enabled':False,'config':dict(DEFAULT_CONFIG),'updated_at':None,'last_checked_at':None,'last_results':[]}
        return {**{key:row[key] for key in ('market','symbol','watchlist_created_at','version','updated_at','last_checked_at')},
                'enabled':bool(row['enabled']),'config':json.loads(row['config_json']),'last_results':json.loads(row['last_results_json'])}

    def rule(self,market,symbol):
        if market_for(symbol)!=market:raise LookupFailure('invalid_symbol','市场与代码不一致。',422)
        with self.store.connect() as db:
            item=db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?',(market,symbol)).fetchone()
            if not item:raise LookupFailure('not_in_watchlist','这只股票已经不在自选中。',404)
            row=db.execute('SELECT * FROM extended_anomaly_rules WHERE market=? AND symbol=?',(market,symbol)).fetchone()
            return self.public(row,item)

    def attach_rules(self,items):
        with self.store.connect() as db:
            rows={(row['market'],row['symbol']):row for row in db.execute('SELECT * FROM extended_anomaly_rules')}
        return [{**item,'extended_rule':self.public(rows.get((item['market'],item['symbol'])),item)} for item in items]

    def save(self,market,symbol,generation,expected_version,enabled,config):
        if market_for(symbol)!=market:raise LookupFailure('invalid_symbol','市场与代码不一致。',422)
        config=validate_config(config)
        with self.store.connect() as db,db:
            db.execute('BEGIN IMMEDIATE')
            item=db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?',(market,symbol)).fetchone()
            if not item or item['created_at']!=generation:
                raise LookupFailure('watchlist_changed','自选记录已经变化，请刷新后再保存。',409)
            previous=db.execute('SELECT * FROM extended_anomaly_rules WHERE market=? AND symbol=?',(market,symbol)).fetchone()
            version=previous['version'] if previous and previous['watchlist_created_at']==generation else 0
            if version!=expected_version:raise LookupFailure('rule_changed','扩展规则已由另一操作更新，请刷新后再保存。',409)
            now=self.clock().isoformat()
            db.execute('''INSERT INTO extended_anomaly_rules(market,symbol,watchlist_created_at,version,enabled,config_json,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(market,symbol) DO UPDATE SET watchlist_created_at=excluded.watchlist_created_at,
                version=excluded.version,enabled=excluded.enabled,config_json=excluded.config_json,updated_at=excluded.updated_at,
                last_checked_at=NULL,last_results_json='[]' ''',(market,symbol,generation,version+1,int(enabled),json.dumps(config),now))
            return self.public(db.execute('SELECT * FROM extended_anomaly_rules WHERE market=? AND symbol=?',(market,symbol)).fetchone(),item)

    def check_live(self):
        with self.store.connect() as db:
            rules=[dict(row) for row in db.execute('SELECT * FROM extended_anomaly_rules WHERE enabled=1')]
        for rule in rules:
            try:self.evaluate(rule)
            except Exception:
                with self.store.connect() as db,db:
                    result=[{'status':'blocked','reason':'本次扩展规则检查未完成，原有事件仍保留。'}]
                    db.execute('UPDATE extended_anomaly_rules SET last_checked_at=?,last_results_json=? WHERE market=? AND symbol=? AND version=?',
                               (self.clock().isoformat(),json.dumps(result,ensure_ascii=False),rule['market'],rule['symbol'],rule['version']))

    def evaluate(self,rule,notify=True):
        now=self.clock()
        research=self.research.state(rule['market'],rule['symbol']) if self.research else None
        with self.store.connect() as db,db:
            db.execute('BEGIN IMMEDIATE')
            identity=(rule['market'],rule['symbol'])
            item=db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?',identity).fetchone()
            current=db.execute('SELECT * FROM extended_anomaly_rules WHERE market=? AND symbol=?',identity).fetchone()
            if not item or not current or not current['enabled'] or current['version']!=rule['version'] or item['created_at']!=rule['watchlist_created_at']:
                return [{'status':'cancelled','reason':'自选或扩展规则已变化。'}]
            config=validate_config(json.loads(current['config_json']))
            cached=db.execute('SELECT * FROM quote_cache WHERE market=? AND symbol=?',identity).fetchone()
            saved=db.execute('SELECT * FROM quote_live_evidence WHERE market=? AND symbol=?',identity).fetchone()
            reason=None
            try:
                if not cached or not saved or cached['last_status']!='succeeded' or not cached['payload']:
                    raise ValueError('等待当前成功采集的报价。')
                if cached['watchlist_created_at']!=item['created_at'] or saved['watchlist_created_at']!=item['created_at']:
                    raise ValueError('报价属于已经移除的自选记录。')
                quote=Quote(**json.loads(cached['payload']))
                evidence=json.loads(saved['payload'])
                if not evidence.get('eligibility',{}).get('eligible'):raise ValueError('等待连续有效的成交证据。')
                eligibility=proof_eligibility(quote,evidence['eligibility'].get('proof'),now,item['created_at'])
                if not eligibility['eligible']:raise ValueError(eligibility['reason'])
            except (KeyError,TypeError,ValueError) as error:reason=str(error)
            if reason:
                results=[{'rule_type':kind,'status':'blocked','reason':reason} for kind,flag in KIND_FLAG.items() if config[flag]]
            else:
                daily_row=db.execute('SELECT * FROM daily_cache WHERE market=? AND symbol=?',identity).fetchone()
                daily=json.loads(daily_row['payload']) if daily_row and daily_row['watchlist_created_at']==item['created_at'] and daily_row['payload'] else None
                results=evaluate_rules(quote,config,now,daily=daily,research=research,enabled_at=current['updated_at'])
                trade_date=datetime.fromisoformat(quote.source_reported_at).astimezone(CHINA_TIME).date().isoformat()
                change=(Decimal(quote.price)-Decimal(quote.previous_close))/Decimal(quote.previous_close)*100
                prior_results={entry.get('rule_type'):entry for entry in json.loads(current['last_results_json'])}
                for result in results:
                    if result['status']!='triggered':continue
                    kind=result['rule_type']
                    existing=db.execute('SELECT id FROM anomaly_events WHERE mode=\'live\' AND market=? AND symbol=? AND direction=? AND trade_date=?',(*identity,kind,trade_date)).fetchone()
                    if existing:
                        result.update(status='duplicate',event_id=existing['id'],reason='同一股票同类规则当日已有事件，不重复投递。')
                        continue
                    quote_data=quote.to_dict();quote_data.pop('raw_text',None)
                    prior=prior_results.get(kind,{})
                    first=prior.get('status')!='below'
                    snapshot={**{key:result[key] for key in ('rule_type','rule_label','condition_text','metric_value','threshold','rule_evidence')},
                              'mode':'live','direction':kind,'quote':quote_data,'change_pct':format(change,'f'),
                              'config':config,'rule_version':current['version'],'watchlist_created_at':item['created_at'],
                              'first_observation':first,'market_state':'trading','eligibility':eligibility,
                              'freshness_limit_seconds':FRESHNESS_SECONDS,'repeat_policy':'同一股票同类规则同日最多记录一次。',
                              'detection_label':('首次有效检查已满足条件，不能确定准确开始时刻。' if first else '上次有效检查未满足，本次已满足；准确开始时刻位于两次采样之间。'),
                              'formula':result['condition_text']}
                    event_id=str(uuid.uuid4())
                    db.execute('INSERT INTO anomaly_events(id,mode,market,symbol,direction,trade_date,created_at,snapshot) VALUES(?,\'live\',?,?,?,?,?,?)',
                               (event_id,*identity,kind,trade_date,now.isoformat(),json.dumps(snapshot,ensure_ascii=False,allow_nan=False)))
                    if notify:self.notifications.queue_event(db,event_id,'live',now)
                    result['event_id']=event_id
            public=[{key:value for key,value in result.items() if key!='rule_evidence'} for result in results]
            db.execute('UPDATE extended_anomaly_rules SET last_checked_at=?,last_results_json=? WHERE market=? AND symbol=? AND version=?',
                       (now.isoformat(),json.dumps(public,ensure_ascii=False),*identity,current['version']))
            return results
