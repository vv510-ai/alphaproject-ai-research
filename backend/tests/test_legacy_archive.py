from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import json
import sqlite3

import pytest

from backend.app.legacy_archive import LegacyArchive,TABLES,legacy_budget_usage,read_legacy_copy
from backend.app.securities import LookupFailure,Security
from backend.app.store import WatchlistStore

NOW=datetime(2026,10,23,7,tzinfo=timezone.utc)
IDENTITY=Security('002119','sz','康强电子','合成核验来源',NOW.isoformat())
RULES={'limit_enabled':True,'volume_enabled':True,'volume_ratio':3}


def source_data():
    return {
        'watchlist_group':[{'id':1,'name':'\ufffd损坏名称','created_at':'2026-09-01T09:00:00'}],
        'watchlist_item':[
            {'code':'002119','name':'\ufffd坏名字','group_id':1,'is_holding':1,'display_order':4,'joined_at':'2026-10-01T01:00:00','deleted_at':None},
            {'code':'600519','name':'原版茅台','group_id':1,'is_holding':0,'display_order':2,'joined_at':'2026-09-01T01:00:00','deleted_at':None},
            {'code':'000001','name':'旧已删除','group_id':None,'is_holding':0,'display_order':1,'joined_at':'2026-09-01','deleted_at':'2026-09-02'},
        ],
        'briefing_record':[{'on_date':'2026-10-22','content_json':json.dumps({'body_text':'旧简报','news':{'data':[{'title':'新闻','api_key':'private-briefing-secret'}]}}),
                            'version':'full','push_status':'delivered','created_at':'2026-10-22T09:18:00'}],
        'push_log':[{'id':1,'ts':'2026-10-22T09:18:01','status':'delivered','retries':0,'target':'private-target',
                     'code':'002119','signal':'volume','uuid':'old-uuid','error':'private-error-secret'}],
        'undelivered':[{'id':1,'uuid':'unknown-uuid','request_json':json.dumps({'code':'002119','content':{'text':'未发正文'},'receive_id':'private-target'}),
                        'fail_count':2,'queued_at':'2026-10-22T10:00:00','is_holding':1}],
        'llm_budget':[{'on_date':'2026-10-22','cost_cny':0.10000000000000002},{'on_date':'2026-10-23','cost_cny':0.25}],
    }


def destination(tmp_path):
    store=WatchlistStore(tmp_path/'target.sqlite3')
    store.initialize()
    item,_=store.add(Security('600519','sh','贵州茅台','当前已核验',NOW.isoformat()))
    item=store.update_metadata('sh','600519',item['created_at'],0,None,True)
    archive=LegacyArchive(store,lambda:NOW)
    archive.initialize()
    return store,archive,item


def test_import_preserves_existing_generation_private_originals_and_no_outbox(tmp_path):
    store,archive,existing=destination(tmp_path)
    result=archive.import_snapshot(source_data(),{'002119':IDENTITY},RULES)
    assert result['watchlist']=={'imported':1,'existing':1,'archived_deleted':1}
    assert store.get('sh','600519')==existing
    assert store.get('sz','000001') is None
    imported=store.get('sz','002119')
    assert imported['name']=='康强电子' and imported['verified_at']==NOW.isoformat()
    assert imported['created_at']==NOW.isoformat() and imported['is_holding'] is True
    assert store.groups()[0]['name']=='原版分组 1（名称待确认）'
    assert result['legacy_pending_archived_count']==1
    with store.connect() as db:
        names={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not {'notification_messages','anomaly_events'} & names
        raw=json.loads(db.execute("SELECT content_json FROM legacy_records WHERE kind='notification'").fetchone()[0])
        assert raw['target']=='private-target' and raw['status']=='delivered'
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
    public=archive.history('notification')['items'][0]
    assert public['status']=='delivered'
    assert '未确认本人实收' in public['content']['receipt_note']
    assert 'private-' not in json.dumps(public)
    assert 'private-' not in json.dumps(archive.history('briefing'))
    assert 'private-' not in json.dumps(archive.history('undelivered'))


def test_repeated_import_does_not_duplicate_change_user_edit_or_recharge(tmp_path):
    store,archive,_=destination(tmp_path)
    first=archive.import_snapshot(source_data(),{'002119':IDENTITY},RULES)
    item=store.get('sz','002119')
    changed=store.update_metadata('sz','002119',item['created_at'],0,None,False)
    replay=archive.import_snapshot(source_data(),{},RULES)
    assert replay['replayed'] and replay['source_fingerprint']==first['source_fingerprint']
    assert store.get('sz','002119')==changed
    with store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM legacy_migrations').fetchone()[0]==1
        assert legacy_budget_usage(db,'2026-10-23','2026-10')=={'day_used_micro':250000,'month_used_micro':350001}
    assert archive.history()['total']==10


def test_changed_source_rejected_without_overwriting_original_archive(tmp_path):
    store,archive,_=destination(tmp_path)
    archive.import_snapshot(source_data(),{'002119':IDENTITY},RULES)
    original=archive.summary()
    changed=source_data()
    changed['push_log'][0]['status']='failed'
    with pytest.raises(ValueError,match='source changed'):
        archive.import_snapshot(changed,{'002119':IDENTITY},RULES)
    assert archive.summary()==original
    assert archive.history('notification')['items'][0]['status']=='delivered'


def test_missing_verified_identity_rolls_back_whole_migration(tmp_path):
    store,archive,item=destination(tmp_path)
    with pytest.raises(ValueError,match='Missing verified identity'):
        archive.import_snapshot(source_data(),{},RULES)
    assert store.list_items()==[item]
    assert store.groups()==[] and archive.history()['total']==0
    assert archive.summary()['status']=='not_imported'


@pytest.mark.parametrize('amount',[-1,float('inf'),float('nan'),'unknown'])
def test_invalid_budget_blocks_import_instead_of_silently_resetting(tmp_path,amount):
    store,archive,_=destination(tmp_path)
    data=source_data()
    data['llm_budget'][0]['cost_cny']=amount
    with pytest.raises(ValueError):
        archive.import_snapshot(data,{'002119':IDENTITY},RULES)
    assert archive.summary()['status']=='not_imported'


def test_keyset_paging_and_scope_bound_cursor(tmp_path):
    store,archive,_=destination(tmp_path)
    archive.import_snapshot(source_data(),{'002119':IDENTITY},RULES)
    first=archive.history(limit=2)
    assert first['next_cursor']
    seen=[item['id'] for item in first['items']]
    cursor=first['next_cursor']
    while cursor:
        page=archive.history(limit=2,cursor=cursor)
        seen.extend(item['id'] for item in page['items'])
        cursor=page['next_cursor']
    assert len(seen)==len(set(seen))==first['total']
    with pytest.raises(LookupFailure): archive.history(kind='briefing',cursor=first['next_cursor'])
    with pytest.raises(LookupFailure): archive.history(cursor='not-a-cursor')
    with pytest.raises(LookupFailure): archive.history(kind='unknown')
    assert archive.history(symbol='002119')['total']==3


def test_two_importers_commit_exactly_one_snapshot(tmp_path):
    store,archive,_=destination(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:archive.import_snapshot(source_data(),{'002119':IDENTITY},RULES),range(2)))
    assert sum(not result['replayed'] for result in results)==1
    assert len(store.list_items())==2


def test_source_read_is_consistent_read_only_and_retains_original_values(tmp_path):
    path=tmp_path/'legacy.sqlite3'
    data=source_data()
    with sqlite3.connect(path) as db:
        for name in TABLES:
            rows=data[name]
            keys=list(rows[0])
            columns=','.join('"'+key+'"' for key in keys)
            db.execute(f'CREATE TABLE "{name}"({columns})')
            db.executemany(f'INSERT INTO "{name}" VALUES({",".join("?" for _ in keys)})',[[row[key] for key in keys] for row in rows])
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    assert read_legacy_copy(path)==data
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
    store,archive,_=destination(tmp_path)
    assert archive.import_copy(path,{'002119':IDENTITY},RULES)['status']=='imported'
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
