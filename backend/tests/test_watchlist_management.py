from datetime import datetime, timedelta, timezone
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.securities import LookupFailure, Security
from backend.app.store import WatchlistStore

HEADERS = {'X-Alpha-Request':'watchlist-v1','Origin':'http://testserver'}


def security(symbol='600519'):
    return Security(symbol, 'sh' if symbol.startswith('6') else 'sz', '测试'+symbol,
                    '合成身份', '2026-10-23T00:00:00+00:00')


def make_store(tmp_path):
    store = WatchlistStore(tmp_path/'management.sqlite3')
    store.initialize()
    return store


def test_additive_migration_keeps_old_rows_and_old_insert_delete_compatible(tmp_path):
    path = tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE watchlist(market TEXT,symbol TEXT,name TEXT,source TEXT,verified_at TEXT,created_at TEXT,PRIMARY KEY(market,symbol))')
        db.execute('PRAGMA user_version=1')
        old = ('sh','600519','旧名字','旧来源','old-verified','old-generation')
        db.execute('INSERT INTO watchlist VALUES(?,?,?,?,?,?)',old)
    store = WatchlistStore(path)
    store.initialize()
    first = store.get('sh','600519')
    assert tuple(first[key] for key in ('market','symbol','name','source','verified_at','created_at')) == old
    store.initialize()
    assert store.get('sh','600519') == first
    # The previous binary explicitly names these six columns.
    with sqlite3.connect(path) as db:
        db.execute('INSERT INTO watchlist(market,symbol,name,source,verified_at,created_at) VALUES(?,?,?,?,?,?)',
                   ('sz','000001','旧代码新增','旧来源','t','new-generation'))
        assert db.execute('PRAGMA user_version').fetchone()[0] == 1
    assert store.get('sz','000001')['is_holding'] is False
    assert store.remove('sz','000001')
    assert store.get('sh','600519') == first


def test_groups_metadata_and_atomic_stale_edit_rejection(tmp_path):
    store=make_store(tmp_path)
    item,_=store.add(security())
    group=store.create_group('长期观察')
    assert store.create_group(' 长期观察 ') == group
    updated=store.update_metadata('sh','600519',item['created_at'],0,group['id'],True)
    assert updated['is_holding'] is True and updated['metadata_version']==1
    with pytest.raises(LookupFailure, match='另一操作'):
        store.update_metadata('sh','600519',item['created_at'],0,None,False)
    assert store.get('sh','600519') == updated
    store.rename_group(group['id'],'主要持仓')
    assert store.groups()[0]['name']=='主要持仓'
    assert store.delete_group(group['id'])
    after=store.get('sh','600519')
    assert after['group_id'] is None and after['is_holding'] is True and after['metadata_version']==2


def test_concurrent_metadata_update_has_one_winner(tmp_path):
    store=make_store(tmp_path)
    item,_=store.add(security())
    def update(holding):
        try:
            store.update_metadata('sh','600519',item['created_at'],0,None,holding)
            return 'saved'
        except LookupFailure as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(update,[True,False]))
    assert results.count('saved')==1
    assert results.count('watchlist_changed')==1


def order_request(item):
    return {'market':item['market'],'symbol':item['symbol'],'watchlist_created_at':item['created_at'],
            'metadata_version':item['metadata_version']}


def test_full_list_order_persists_and_rejects_missing_new_or_stale_rows(tmp_path):
    store=make_store(tmp_path)
    a,_=store.add(security())
    b,_=store.add(security('000001'))
    assert [x['symbol'] for x in store.list_items()]==['000001','600519']
    result=store.reorder([order_request(a),order_request(b)])
    assert [x['symbol'] for x in result]==['600519','000001']
    with pytest.raises(LookupFailure):
        store.reorder([order_request(a),order_request(b)])
    with pytest.raises(LookupFailure):
        store.reorder([order_request(result[0])])
    with pytest.raises(LookupFailure):
        store.reorder([order_request(result[0]),order_request(result[0])])
    reopened=WatchlistStore(store.path)
    reopened.initialize()
    assert reopened.list_items()==result


def test_delete_generation_guard_undo_expiry_conflict_and_replay(tmp_path):
    now=[datetime(2026,10,23,tzinfo=timezone.utc)]
    store=make_store(tmp_path)
    store.clock=lambda:now[0]
    first,_=store.add(security())
    group=store.create_group('组合')
    item=store.update_metadata('sh','600519',first['created_at'],0,group['id'],True)
    deleted=store.remove_with_undo('sh','600519',item['created_at'])
    now[0]+=timedelta(seconds=1)
    new,_=store.add(security())
    with pytest.raises(LookupFailure):
        store.remove_with_undo('sh','600519',item['created_at'])
    with pytest.raises(LookupFailure):
        store.undo_remove(deleted['undo_token'])
    assert store.get('sh','600519')==new
    store.remove('sh','600519')
    restored=store.undo_remove(deleted['undo_token'])
    assert restored['restored'] is True
    assert restored['item']['created_at'] != item['created_at']
    assert restored['item']['is_holding'] is True and restored['item']['group_id']==group['id']
    assert store.undo_remove(deleted['undo_token'])['restored'] is False
    second=store.remove_with_undo('sh','600519')
    now[0]+=timedelta(minutes=5)
    with pytest.raises(LookupFailure,match='超过 5 分钟'):
        store.undo_remove(second['undo_token'])
    assert store.list_items()==[]


def test_http_management_reopen_and_undo_disables_rule_keeps_events(tmp_path):
    class Resolver:
        def resolve(self,symbol): return security(symbol)
    path=tmp_path/'api.sqlite3'
    app=create_app(path,Resolver(),['testserver'],run_monitor=False)
    with TestClient(app) as client:
        item=client.post('/api/watchlist',json={'symbol':'600519'},headers=HEADERS).json()['item']
        group=client.post('/api/watchlist/groups',json={'name':'观察'},headers=HEADERS).json()['item']
        payload={'watchlist_created_at':item['created_at'],'expected_version':0,'group_id':group['id'],'is_holding':True}
        assert client.patch('/api/watchlist/sh/600519',json=payload,headers=HEADERS).status_code==200
        assert client.patch('/api/watchlist/sh/600519',json=payload,headers=HEADERS).status_code==409
        app.state.anomalies.save_rule('sh','600519',item['created_at'],'3','3',True,0)
        with app.state.store.connect() as db,db:
            db.execute('INSERT INTO anomaly_events(id,mode,market,symbol,direction,trade_date,created_at,snapshot) VALUES(?,?,?,?,?,?,?,?)',
                       ('saved-history','live','sh','600519','up','2026-10-22','2026-10-22T00:00:00+00:00',json.dumps({'original':True})))
        removed=client.delete('/api/watchlist/sh/600519',params={'watchlist_created_at':item['created_at']},headers=HEADERS).json()
        restored=client.post('/api/watchlist/undo/'+removed['undo_token'],headers=HEADERS).json()
        assert restored['restored'] and restored['rule_restored_disabled']
        assert restored['item']['group_id']==group['id']
        with app.state.store.connect() as db:
            rule=db.execute('SELECT * FROM anomaly_rules').fetchone()
            assert rule['enabled']==0 and rule['up_threshold']=='3'
            assert rule['watchlist_created_at']==restored['item']['created_at']
            assert db.execute('SELECT snapshot FROM anomaly_events').fetchone()[0]==json.dumps({'original':True})
        dashboard=client.get('/api/dashboard').json()
        assert dashboard['items'][0]['is_holding'] is True
        assert client.patch('/api/watchlist/groups/'+group['id'],json={'name':'新组名'},headers=HEADERS).status_code==200
    with TestClient(create_app(path,Resolver(),['testserver'],run_monitor=False)) as client:
        assert client.get('/api/watchlist/groups').json()['items'][0]['name']=='新组名'
        assert client.get('/api/watchlist').json()['items'][0]['created_at']==restored['item']['created_at']


@pytest.mark.parametrize('name',['',' '*2,'x'*21,'x\nline'])
def test_invalid_group_name_does_not_save(tmp_path,name):
    store=make_store(tmp_path)
    with pytest.raises(LookupFailure): store.create_group(name)
    assert store.groups()==[]
