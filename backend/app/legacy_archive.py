"""Additive, idempotent import of the retired application's private SQLite copy.

Original IDs/content are retained here; no legacy row becomes a runnable outbox
or an acknowledged message. Active Lab generations and histories always win.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import base64
import binascii
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from .securities import LookupFailure, Security, market_for
from .extended_rules import validate_config

SOURCE_KEY = 'alphaproject-original'
TABLES = ('watchlist_group','watchlist_item','briefing_record','push_log','undelivered','llm_budget')
KINDS = {'watchlist_group','watchlist_item','briefing','notification','undelivered','budget','rule_settings'}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)


def stable_id(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ':'.join(parts)))


def read_legacy_copy(path):
    path=Path(path).resolve()
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Legacy copy failed integrity checks')
        names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not set(TABLES)<=names:
            raise ValueError('Legacy copy is missing expected tables')
        data={name:[dict(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')] for name in TABLES}
    return data


def legacy_budget_usage(db, day, month):
    names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('legacy_budget_usage','legacy_records','legacy_migrations')")}
    imported='legacy_migrations' in names and db.execute("SELECT 1 FROM legacy_migrations WHERE source_key=? AND status='imported'",(SOURCE_KEY,)).fetchone()
    if imported:
        if names!={'legacy_budget_usage','legacy_records','legacy_migrations'}:
            raise ValueError('Imported budget storage is missing')
        expected={}
        for row in db.execute("SELECT content_json FROM legacy_records WHERE source_key=? AND kind='budget'",(SOURCE_KEY,)):
            item=json.loads(row[0]);cost=Decimal(str(item['cost_cny']))
            if not cost.is_finite() or cost<0:raise ValueError('Invalid archived budget')
            expected[item['on_date']]=int((cost*1000000).to_integral_value(rounding=ROUND_CEILING))
        summary=json.loads(db.execute('SELECT summary_json FROM legacy_migrations WHERE source_key=?',(SOURCE_KEY,)).fetchone()[0])
        if len(expected)!=summary['kinds'].get('budget',0):raise ValueError('Archived budget rows are missing')
        actual=dict(db.execute('SELECT budget_day,charged_micro FROM legacy_budget_usage WHERE source_key=?',(SOURCE_KEY,)))
        if actual!=expected:raise ValueError('Imported budget differs from its original archive')
    elif 'legacy_budget_usage' in names and db.execute('SELECT 1 FROM legacy_budget_usage').fetchone():
        raise ValueError('Budget usage has no completed migration')
    if 'legacy_budget_usage' not in names:
        return {'day_used_micro':0,'month_used_micro':0}
    return {
        'day_used_micro':db.execute('SELECT COALESCE(SUM(charged_micro),0) FROM legacy_budget_usage WHERE budget_day=?',(day,)).fetchone()[0],
        'month_used_micro':db.execute('SELECT COALESCE(SUM(charged_micro),0) FROM legacy_budget_usage WHERE substr(budget_day,1,7)=?',(month,)).fetchone()[0],
    }


class LegacyArchive:
    def __init__(self,store,clock=None):
        self.store=store
        self.clock=clock or (lambda:datetime.now(timezone.utc))

    def initialize(self):
        with self.store.connect() as db,db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS legacy_migrations(
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, source_fingerprint TEXT NOT NULL,
                    imported_at TEXT NOT NULL, status TEXT NOT NULL, summary_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS legacy_records(
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL,
                    occurred_at TEXT, symbol TEXT, status TEXT, content_json TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, imported_at TEXT NOT NULL,
                    UNIQUE(source_key,kind,source_id)
                );
                CREATE INDEX IF NOT EXISTS idx_legacy_history ON legacy_records(kind,occurred_at,id);
                CREATE TABLE IF NOT EXISTS legacy_watchlist_links(
                    source_key TEXT NOT NULL, source_id TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
                    watchlist_created_at TEXT, action TEXT NOT NULL, details_json TEXT NOT NULL, imported_at TEXT NOT NULL,
                    PRIMARY KEY(source_key,source_id)
                );
                CREATE TABLE IF NOT EXISTS legacy_budget_usage(
                    source_key TEXT NOT NULL, budget_day TEXT NOT NULL, charged_micro INTEGER NOT NULL CHECK(charged_micro>=0),
                    source_value_json TEXT NOT NULL, imported_at TEXT NOT NULL,
                    PRIMARY KEY(source_key,budget_day)
                );
            ''')

    def import_copy(self,path,identities,rule_settings=None):
        if Path(path).resolve()==self.store.path:
            raise ValueError('Legacy source cannot be the target database')
        data=read_legacy_copy(path)
        return self.import_snapshot(data,identities,rule_settings)

    def import_snapshot(self,data,identities,rule_settings=None):
        """Caller supplies identities resolved before taking the database lock."""
        if set(data)!=set(TABLES):
            raise ValueError('Unexpected legacy source tables')
        source={'tables':data,'rule_settings':rule_settings}
        fingerprint=hashlib.sha256(canonical(source).encode()).hexdigest()
        imported_at=self.clock().isoformat()
        mapped_rules=validate_config(rule_settings['effective']) if isinstance(rule_settings,dict) and 'effective' in rule_settings else None
        budgets=[]
        for item in data['llm_budget']:
            day=item['on_date']
            if date.fromisoformat(day).isoformat()!=day:
                raise ValueError('Invalid legacy budget day')
            try:
                cost=Decimal(str(item['cost_cny']))
                if not cost.is_finite() or cost<0 or cost>Decimal('1000000'):
                    raise ValueError('Invalid legacy budget amount')
                amount=int((cost*1000000).to_integral_value(rounding=ROUND_CEILING))
            except (InvalidOperation,TypeError):
                raise ValueError('Invalid legacy budget amount') from None
            budgets.append((day,amount,canonical(item)))
        with self.store.connect() as db,db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM legacy_migrations WHERE source_key=?',(SOURCE_KEY,)).fetchone()
            if old:
                if old['source_fingerprint']!=fingerprint:
                    raise ValueError('Legacy source changed after import; explicit reconciliation is required')
                return {**json.loads(old['summary_json']),'replayed':True}
            summary={'status':'imported','source_key':SOURCE_KEY,'source_fingerprint':fingerprint,
                     'imported_at':imported_at,'kinds':{},'watchlist':{'imported':0,'existing':0,'archived_deleted':0},
                     'warnings':[],'notification_policy':'旧记录仅归档；未投递不补发，旧成功不等于本人确认。',
                     'budget_policy':'原版每日估算按微元向上取整计入统一预算，不是模型平台账单。'}
            summary['extended_rules_imported']=0
            def archive(kind,source_id,item,occurred_at=None,symbol=None,status=None):
                encoded=canonical(item)
                db.execute('INSERT INTO legacy_records(id,source_key,kind,source_id,occurred_at,symbol,status,content_json,content_sha256,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (stable_id(SOURCE_KEY,kind,str(source_id)),SOURCE_KEY,kind,str(source_id),occurred_at,symbol,status,encoded,hashlib.sha256(encoded.encode()).hexdigest(),imported_at))
                summary['kinds'][kind]=summary['kinds'].get(kind,0)+1
            groups={}
            for row in data['watchlist_group']:
                archive('watchlist_group',row['id'],row,row.get('created_at'))
                name=row.get('name')
                if not isinstance(name,str) or not 1<=len(name.strip())<=20 or '\ufffd' in name or any(ord(c)<32 for c in name):
                    name=f"原版分组 {row['id']}（名称待确认）"[:20]
                    summary['warnings'].append(f"旧分组 {row['id']} 名称无法完整读取；原文保留在档案，新名称标为待确认。")
                name=name.strip()
                existing=db.execute('SELECT id FROM watchlist_groups WHERE name=?',(name,)).fetchone()
                gid=existing['id'] if existing else stable_id(SOURCE_KEY,'group',str(row['id']))
                if not existing:
                    db.execute('INSERT INTO watchlist_groups(id,name,created_at) VALUES(?,?,?)',(gid,name,imported_at))
                else:
                    summary['warnings'].append(f'旧分组 {row["id"]} 使用现有同名分组；原 ID 保留在档案。')
                groups[row['id']]=gid
            maximum=db.execute('SELECT COALESCE(MAX(display_order),-1) FROM watchlist').fetchone()[0]
            for offset,row in enumerate(sorted(data['watchlist_item'],key=lambda item:(item['display_order'],item['code']))):
                symbol=str(row['code'])
                market=market_for(symbol)
                archive('watchlist_item',symbol,row,row.get('joined_at'),symbol,'deleted' if row.get('deleted_at') else 'active')
                current=db.execute('SELECT * FROM watchlist WHERE market=? AND symbol=?',(market,symbol)).fetchone()
                generation=None
                if row.get('deleted_at'):
                    action='archived_deleted'
                elif current:
                    action='existing'
                    generation=current['created_at']
                    summary['warnings'].append(f'{market}{symbol} 两版都有，保留当前自选的代次、名称、管理字段和规则；旧字段可在档案核对。')
                else:
                    identity=identities.get(symbol)
                    if not isinstance(identity,Security) or identity.symbol!=symbol or identity.market!=market or not identity.verified_at:
                        raise ValueError(f'Missing verified identity for {symbol}; no migration committed')
                    if not identity.name or '\ufffd' in identity.name:
                        raise ValueError('Verified identity name is invalid')
                    generation=imported_at
                    db.execute('INSERT INTO watchlist(market,symbol,name,source,verified_at,created_at,group_id,is_holding,display_order,metadata_version) VALUES(?,?,?,?,?,?,?,?,?,0)',
                               (market,symbol,identity.name,identity.source,identity.verified_at,generation,groups.get(row['group_id']),int(bool(row['is_holding'])),maximum+offset+1))
                    action='imported'
                    if mapped_rules is not None:
                        db.execute('INSERT INTO extended_anomaly_rules(market,symbol,watchlist_created_at,version,enabled,config_json,updated_at) VALUES(?,?,?,1,?,?,?)',
                                   (market,symbol,generation,int(any(value for key,value in mapped_rules.items() if key.endswith('_enabled'))),canonical(mapped_rules),imported_at))
                        summary['extended_rules_imported']+=1
                    if row.get('name')!=identity.name:
                        summary['warnings'].append(f'{market}{symbol} 名称已通过 {identity.source} 重新核验；旧名称原文保留。')
                summary['watchlist'][action]+=1
                db.execute('INSERT INTO legacy_watchlist_links(source_key,source_id,market,symbol,watchlist_created_at,action,details_json,imported_at) VALUES(?,?,?,?,?,?,?,?)',
                           (SOURCE_KEY,symbol,market,symbol,generation,action,canonical(row),imported_at))
            for row in data['briefing_record']:
                archive('briefing',row['on_date'],row,row.get('created_at') or row['on_date'],status=row.get('push_status'))
            for row in data['push_log']:
                archive('notification',row['id'],row,row.get('ts'),row.get('code'),row.get('status'))
            for row in data['undelivered']:
                request=json.loads(row['request_json'])
                archive('undelivered',row['id'],row,row.get('queued_at'),request.get('code'),'archived_not_sent')
            for day,amount,encoded in budgets:
                db.execute('INSERT INTO legacy_budget_usage(source_key,budget_day,charged_micro,source_value_json,imported_at) VALUES(?,?,?,?,?)',
                           (SOURCE_KEY,day,amount,encoded,imported_at))
                archive('budget',day,json.loads(encoded),day,status='legacy_estimate')
            if rule_settings is not None:
                archive('rule_settings','current',rule_settings,status='mapped_for_imported_stocks' if mapped_rules is not None else 'archived_requires_mapping')
            summary['legacy_unacknowledged_count']=len(data['push_log'])
            summary['legacy_pending_archived_count']=len(data['undelivered'])
            db.execute('INSERT INTO legacy_migrations(id,source_key,source_fingerprint,imported_at,status,summary_json) VALUES(?,?,?,?,?,?)',
                       (stable_id(SOURCE_KEY,fingerprint),SOURCE_KEY,fingerprint,imported_at,'imported',canonical(summary)))
            return {**summary,'replayed':False}

    def summary(self):
        with self.store.connect() as db:
            row=db.execute('SELECT summary_json FROM legacy_migrations WHERE source_key=?',(SOURCE_KEY,)).fetchone()
        return json.loads(row[0]) if row else {'status':'not_imported','kinds':{},'watchlist':{'imported':0,'existing':0},'warnings':[],'source_fingerprint':None,'imported_at':None}

    @staticmethod
    def public_content(kind,raw):
        if kind=='notification':
            result={key:raw.get(key) for key in ('id','ts','status','retries','code','signal')}
            result['receipt_note']='旧版记录，未确认本人实收；不会因迁移再次发送。'
            if raw.get('error'): result['error_note']='原错误详情保存在私有归档。'
            return result
        if kind=='undelivered':
            return {'id':raw['id'],'queued_at':raw.get('queued_at'),'fail_count':raw.get('fail_count'),
                    'status':'只读归档，不自动补发','request':LegacyArchive._redact(json.loads(raw['request_json']))}
        if kind=='briefing':
            return {**{key:raw.get(key) for key in ('on_date','version','push_status','created_at')},
                    'report':LegacyArchive._redact(json.loads(raw['content_json'])),'receipt_note':'原版投递状态不等于本人确认。'}
        return LegacyArchive._redact(raw)

    @staticmethod
    def _redact(value):
        if isinstance(value,dict):
            blocked=('secret','token','password','authorization','credential','api_key','receive_id','target')
            return {key:LegacyArchive._redact(item) for key,item in value.items() if not any(word in key.lower() for word in blocked)}
        if isinstance(value,list): return [LegacyArchive._redact(item) for item in value]
        return value

    def history(self,kind=None,symbol=None,limit=40,cursor=None):
        if kind=='':kind=None
        if symbol=='':symbol=None
        if kind is not None and kind not in KINDS or type(limit) is not int or not 1<=limit<=100:
            raise LookupFailure('invalid_history','档案类型或条数不正确。',422)
        if symbol is not None: market_for(symbol)
        conditions,params=[],[]
        if kind:conditions.append('kind=?');params.append(kind)
        if symbol:conditions.append('symbol=?');params.append(symbol)
        where=' WHERE '+' AND '.join(conditions) if conditions else ''
        with self.store.connect() as db:
            db.execute('BEGIN')
            total=db.execute('SELECT COUNT(*) FROM legacy_records'+where,params).fetchone()[0]
            if cursor:
                try:
                    if not isinstance(cursor,str) or len(cursor)>1024:raise ValueError()
                    decoded=json.loads(base64.b64decode(cursor.encode('ascii'),altchars=b'-_',validate=True))
                    if set(decoded)!={'kind','symbol','at','id'} or decoded['kind']!=kind or decoded['symbol']!=symbol:
                        raise ValueError()
                    if not isinstance(decoded['at'],str) or len(decoded['at'])>80 or str(uuid.UUID(decoded['id']))!=decoded['id']:
                        raise ValueError()
                    conditions.append("(COALESCE(occurred_at,'')<? OR (COALESCE(occurred_at,'')=? AND id<?))")
                    params.extend([decoded['at'],decoded['at'],decoded['id']])
                except (ValueError,TypeError,KeyError,UnicodeError,binascii.Error):
                    raise LookupFailure('invalid_cursor','档案分页已失效，请从头读取。',422) from None
            where=' WHERE '+' AND '.join(conditions) if conditions else ''
            rows=db.execute("SELECT * FROM legacy_records"+where+" ORDER BY COALESCE(occurred_at,'') DESC,id DESC LIMIT ?",(*params,limit+1)).fetchall()
        next_cursor=None
        if len(rows)>limit:
            last=rows[limit-1]
            next_cursor=base64.urlsafe_b64encode(canonical({'kind':kind,'symbol':symbol,'at':last['occurred_at'] or '','id':last['id']}).encode()).decode('ascii')
        items=[]
        for row in rows[:limit]:
            public={key:row[key] for key in ('id','kind','source_id','occurred_at','symbol','status')}
            public['content']=self.public_content(row['kind'],json.loads(row['content_json']))
            items.append(public)
        return {'items':items,'next_cursor':next_cursor,'total':total}
