"""New entry paths with isolated storage, controlled HTTP and synthetic model replies."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import json
import sqlite3
import uuid

from fastapi.testclient import TestClient
import pytest

from backend.app.ai_client import Completion
from backend.app.anomaly_rules import Sample
from backend.app.explanation_evidence import ExplanationRejected, messages_for, validate_output
from backend.app.explanation_schema import FIELDS, initialize_schema
from backend.app.feishu import FeishuClient
from backend.app.main import create_app
from backend.app.quotes import Quote
from backend.app.securities import LookupFailure, Security
from backend.tests.test_explanations import Clock, CONFIG, HEADERS, FakeModel, valid_content
from backend.tests.test_notifications import CONFIG as FEISHU_CONFIG, transport_fixture


def quote_content(data):
    echo = data['factual_echo']
    label = {'up': '上涨', 'down': '下跌', 'flat': '持平'}[echo['direction']]
    return {'snapshot_id': data['snapshot_id'], 'evidence_hash': data['evidence_hash'], 'factual_echo': echo.copy(),
            'cause_status': 'insufficient_evidence', 'missing_evidence_ids': ['news', 'volume', 'sector'], 'statements': [
                {'kind': 'change', 'text': f'这份报价为 {echo["price"]} 元，昨收 {echo["previous_close"]} 元，相对昨收{label}，对应变化 {echo["change_pct"]}%。', 'evidence_ids': ['F2', 'F3']},
                {'kind': 'context', 'text': '这是用户固定的一份报价，未据此判断是否触发提醒，实时性未验证，不能代替此刻行情。', 'evidence_ids': ['F1', 'F4']},
                {'kind': 'scope', 'text': '解释对应来源时间的当时记录，只描述保存的价格差异，不能判断变化会持续多久。', 'evidence_ids': ['F5', 'F6']},
            ]}


class Model(FakeModel):
    def complete(self, messages):
        self.calls.append(messages)
        if self.failure: raise self.failure
        data = json.loads(messages[-1]['content'])
        output = quote_content(data) if 'snapshot_id' in data else valid_content(data)
        if self.mutate: self.mutate(output)
        return Completion(json.dumps(output, ensure_ascii=False), 'deepseek-flash', 'fixture', 1200, 500, 'stop', 30)


@pytest.fixture
def rig(tmp_path):
    clock, model = Clock(), Model()
    class Source:
        calls = 0
        def fetch(self, symbol):
            self.calls += 1
            return Quote('sh', symbol, '合成股票', '103', '100', '3', '3', clock().isoformat(), clock().isoformat(), '受控测试源', '', 'raw-not-in-model')
    source = Source()
    app = create_app(tmp_path/'fixture.sqlite3', allowed_hosts=['testserver'], clock=clock,
                     quote_source=source, ai_client=model, ai_config=CONFIG, run_monitor=False)
    with TestClient(app) as client:
        item, _ = app.state.store.add(Security('600519', 'sh', '合成股票', '合成身份', clock().isoformat()))
        app.state.quotes.refresh('sh', '600519')
        yield app, client, clock, model, source, item


def snapshot(rig):
    app, client, *_ = rig
    current = client.get('/api/stocks/sh/600519/explanations').json()['current']
    result = client.post('/api/stocks/sh/600519/explanation-snapshots', headers=HEADERS, json={
        'quote_hash': current['quote_hash'], 'watchlist_created_at': current['watchlist_created_at']})
    assert result.status_code == 200, result.text
    return result.json()['snapshot']['id']


def generate(rig, sid, request_id=None, retry_of=None):
    client = rig[1]; url = f'/api/quote-snapshots/{sid}/explanations'
    state = client.get(url).json()
    return client.post(url, headers=HEADERS, json={'request_id': request_id or str(uuid.uuid4()),
        'evidence_hash': state['evidence']['evidence_hash'], 'retry_of': retry_of})


def test_whole_quote_path_cache_update_delete_restart_and_same_budget(rig):
    app, client, clock, model, source, item = rig
    sid = snapshot(rig)
    assert snapshot(rig) == sid
    assert not model.calls and source.calls == 1
    response = generate(rig, sid); assert response.status_code == 202
    record = client.get(f'/api/quote-snapshots/{sid}/explanations').json()['records'][0]
    assert record['status'] == 'succeeded' and record['event_id'] is None and record['quote_snapshot_id'] == sid
    assert record['reviewed_at'] is None and 'raw-not-in-model' not in json.dumps(model.calls)
    assert generate(rig, sid).json()['reused'] and len(model.calls) == 1
    frozen = record['evidence']
    clock.advance(86400); app.state.quotes.refresh('sh', '600519')
    assert snapshot(rig) != sid
    app.state.store.remove('sh', '600519')
    assert client.get(f'/api/quote-snapshots/{sid}/explanations').json()['evidence'] == frozen
    app.state.explanations.initialize()
    state = client.get(f'/api/quote-snapshots/{sid}/explanations').json()
    assert state['records'][0]['output'] == record['output'] and state['budget']['month_used'] == record['estimated_cost']
    assert client.get('/api/stocks/sh/600519/explanations').json()['current'] is None
    with app.state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM anomaly_events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM notification_messages').fetchone()[0] == 0
        assert not db.execute('PRAGMA foreign_key_check').fetchall()


@pytest.mark.parametrize('changes', [
    {'price': '0'}, {'previous_close': '0'}, {'change': '4'}, {'change_pct': '9'}, {'price': 'NaN'},
    {'market': 'sz'}, {'symbol': '000001'}, {'price_unit': '美元'}, {'change_pct_unit': '元'},
    {'source_reported_at': '2027-01-01T00:00:00+08:00'}, {'received_at': '2027-01-01T00:00:00+08:00'},
    {'source_reported_at': '2026-10-18T01:30:00'}, {'price': '1e9999999'},
])
def test_bad_quote_blocks_snapshot_without_calls(rig, changes):
    app, client, _, model, *_ = rig
    with app.state.store.connect() as db, db:
        quote = json.loads(db.execute('SELECT payload FROM quote_cache').fetchone()[0]); quote.update(changes)
        db.execute('UPDATE quote_cache SET payload=?', (json.dumps(quote),))
    state = client.get('/api/stocks/sh/600519/explanations').json()
    assert state['current'] is None and state['error_message'] and not model.calls
    assert client.post('/api/stocks/sh/600519/explanation-snapshots', headers=HEADERS,
        json={'watchlist_created_at': rig[-1]['created_at'], 'quote_hash': 'a'*64}).status_code == 422


def test_stale_preview_race_and_deleted_readded_identity(rig):
    app, client, clock, *_ = rig
    preview = client.get('/api/stocks/sh/600519/explanations').json()['current']
    body = {key: preview[key] for key in ('quote_hash', 'watchlist_created_at')}
    clock.advance(60); app.state.quotes.refresh('sh', '600519')
    assert client.post('/api/stocks/sh/600519/explanation-snapshots', headers=HEADERS, json=body).status_code == 409
    app.state.store.remove('sh', '600519')
    app.state.store.add(Security('600519', 'sh', '重新添加的股票', '测试', clock().isoformat()))
    assert client.post('/api/stocks/sh/600519/explanation-snapshots', headers=HEADERS, json=body).status_code == 409


def test_concurrent_freeze_same_snapshot_and_shared_ai_busy_budget_aliases(rig):
    app, client, _, model, _, item = rig
    current = app.state.quote_snapshots.state('sh', '600519')['current']
    with ThreadPoolExecutor(max_workers=2) as pool:
        pairs = list(pool.map(lambda _: app.state.quote_snapshots.save('sh','600519',item['created_at'],current['quote_hash']), range(2)))
    assert pairs[0][0]['id'] == pairs[1][0]['id'] and sum(pair[1] for pair in pairs) == 1
    sid = pairs[0][0]['id']; service = app.state.explanations
    rule = app.state.anomalies.save_rule('sh','600519',item['created_at'],'3','3',False,0)
    quote = Quote(**{**current['quote'],'raw_text':''})
    eid = app.state.anomalies.evaluate(rule,Sample(quote,'trading',True,True),mode='demo',notify=False)[0]['event_id']
    request_id = str(uuid.uuid4()); digest = service.state(sid,kind='quote')['evidence']['evidence_hash']
    record, _ = service.queue(sid,request_id,digest,kind='quote')
    with pytest.raises(LookupFailure) as caught: service.queue(eid,str(uuid.uuid4()),service.state(eid)['evidence']['evidence_hash'])
    assert caught.value.code == 'ai_busy'
    service.process(record['id'])
    alias = str(uuid.uuid4()); assert not service.queue(sid,alias,digest,kind='quote')[1]
    with pytest.raises(LookupFailure) as caught: service.queue(eid,alias,service.state(eid)['evidence']['evidence_hash'])
    assert caught.value.code == 'request_reused'
    with pytest.raises(LookupFailure): service.queue(eid,request_id,service.state(eid)['evidence']['evidence_hash'])
    assert service.state(eid)['budget']['month_used'] == service.state(sid,kind='quote')['budget']['month_used']
    service.config = replace(CONFIG, daily_budget='0.000001')
    with pytest.raises(LookupFailure) as caught: service.queue(eid,str(uuid.uuid4()),service.state(eid)['evidence']['evidence_hash'])
    assert caught.value.code == 'ai_budget_exhausted' and len(model.calls) == 1


def test_old_quote_supported_bad_model_boundary_rejected_and_explicit_retry(rig):
    app, client, clock, model, *_ = rig
    clock.advance(86400); sid = snapshot(rig)
    state = client.get(f'/api/quote-snapshots/{sid}/explanations').json()
    assert '非今日' in state['evidence']['facts'][3]['text']
    model.mutate = lambda body: body['statements'][1].update(text='这是现在的实时行情，代表此刻可以采用的价格。')
    assert generate(rig, sid).status_code == 202
    row = client.get(f'/api/quote-snapshots/{sid}/explanations').json()['records'][0]
    assert row['status'] == 'rejected' and row['output'] is None
    assert generate(rig,sid).status_code == 409
    model.mutate = None
    clock.advance(1)
    assert generate(rig,sid,retry_of=row['id']).status_code == 202
    assert client.get(f'/api/quote-snapshots/{sid}/explanations').json()['records'][0]['status'] == 'succeeded'


def test_down_and_flat_directions_valid_and_opposite_rejected(rig):
    app, _, _, _, _, _ = rig
    for price,change in [('97','-3'),('100','0')]:
        with app.state.store.connect() as db, db:
            data=json.loads(db.execute('SELECT payload FROM quote_cache').fetchone()[0]);data.update(price=price,change=change,change_pct=change)
            db.execute('UPDATE quote_cache SET payload=?',(json.dumps(data),))
        sid=snapshot(rig)
        with app.state.store.connect() as db: evidence=app.state.quote_snapshots.evidence(db,sid)
        content=quote_content(evidence['model_input']);validate_output(json.dumps(content),evidence)
        content['statements'][0]['text']='这份报价相对昨日收盘价格上涨，描述的是保存时的报价情况。'
        with pytest.raises(ExplanationRejected):validate_output(json.dumps(content),evidence)


def test_explanation_can_use_explicit_program_rounded_percent_only(rig):
    app = rig[0]
    with app.state.store.connect() as db, db:
        data = json.loads(db.execute('SELECT payload FROM quote_cache').fetchone()[0])
        data.update(price='11.65', previous_close='11.50', change='0.15', change_pct='1.30')
        db.execute('UPDATE quote_cache SET payload=?', (json.dumps(data),))
    sid = snapshot(rig)
    with app.state.store.connect() as db: evidence = app.state.quote_snapshots.evidence(db,sid)
    content = quote_content(evidence['model_input'])
    content['statements'][0]['text'] = '这份保存报价相对昨收上涨约 1.30%，这里只描述保存当时的价格差异。'
    validate_output(json.dumps(content),evidence)
    content['statements'][0]['text'] = content['statements'][0]['text'].replace('1.30','1.31')
    with pytest.raises(ExplanationRejected): validate_output(json.dumps(content),evidence)


def test_event_link_reads_exact_old_event_after_list_limit_and_delete(rig):
    app,client,clock,_,_,item=rig
    rule=app.state.anomalies.save_rule('sh','600519',item['created_at'],'3','3',False,0)
    quote=Quote('sh','600519','事件测试','103','100','3','3',clock().isoformat(),clock().isoformat(),'测试','','')
    eid=app.state.anomalies.evaluate(rule,Sample(quote,'trading',True,True),mode='demo',notify=False)[0]['event_id']
    original=client.get(f'/api/events/{eid}').json()
    with app.state.store.connect() as db,db:
        row=dict(db.execute('SELECT * FROM anomaly_events WHERE id=?',(eid,)).fetchone())
        for i in range(41):
            new={**row,'id':str(uuid.uuid4()),'trade_date':f'fixture-{i}','created_at':f'2026-12-31T23:{i:02d}:00+00:00'}
            db.execute('INSERT INTO anomaly_events ('+','.join(new)+') VALUES ('+','.join('?' for _ in new)+')',tuple(new.values()))
    assert not any(event['id']==eid for event in client.get('/api/anomalies').json()['events'])
    app.state.store.remove('sh','600519')
    assert client.get(f'/api/events/{eid}').json()==original
    assert client.get('/api/events/not-a-uuid').status_code==422
    assert client.get(f'/api/events/{uuid.uuid4()}').status_code==404
    assert 'raw_text' not in original['snapshot']['quote']


@pytest.mark.parametrize('base,note',[('http://192.168.1.2:18765','电脑服务需保持运行'),('https://8.155.130.86/lab','无需电脑开机或同一 Wi-Fi')])
def test_actual_feishu_message_builder_has_link_same_id_and_receipt_text(base,note):
    transport,calls=transport_fixture();client=FeishuClient(FEISHU_CONFIG,transport)
    request_id=str(uuid.uuid4());event_id=str(uuid.uuid4());url=f'{base}/#event={event_id}'
    client.send_test(request_id,'合成演练\n收件核验码：123456',detail_url=url)
    sent=json.loads(calls[-1][1]);content=json.loads(sent['content'])
    assert sent['msg_type']=='post' and sent['uuid']==request_id and sent['receive_id']==FEISHU_CONFIG.chat_id
    assert content['zh_cn']['content'][1]==[{'tag':'a','text':'查看这次事件','href':url}]
    assert '123456' in content['zh_cn']['content'][0][0]['text']
    assert note in content['zh_cn']['content'][2][0]['text']


def test_event_outbox_real_client_payload_and_api_target_share_identity(tmp_path, monkeypatch):
    from backend.app import main, notification_service
    monkeypatch.setattr(main, 'lan_addresses', lambda: ['192.168.4.5'])
    monkeypatch.setenv('ALPHA_LAB_PORT', '19876')
    monkeypatch.setattr(notification_service, 'load_config', lambda path: FEISHU_CONFIG)
    transport, calls = transport_fixture()
    clock = Clock()
    app = create_app(tmp_path/'message.sqlite3', allowed_hosts=['testserver'], clock=clock,
                     notification_client=FeishuClient(FEISHU_CONFIG, transport), run_monitor=False)
    with TestClient(app) as client:
        item, _ = app.state.store.add(Security('600519','sh','合成股票','测试身份',clock().isoformat()))
        assert client.post('/api/notifications/settings', headers=HEADERS, json={'enabled': True}).status_code == 200
        rule = app.state.anomalies.save_rule('sh','600519',item['created_at'],'3','3',False,0)
        quote = Quote('sh','600519','合成股票','103','100','3','3',clock().isoformat(),clock().isoformat(),'测试','','')
        eid = app.state.anomalies.evaluate(rule,Sample(quote,'trading',True,True),mode='demo',notify=True)[0]['event_id']
        app.state.notifications.process_pending_events()
        app.state.notifications.process_pending_events()
        sent = [json.loads(body) for path,body in calls if path.endswith('/messages')]
        assert len(sent) == 1
        content = json.loads(sent[0]['content'])['zh_cn']['content']
        assert content[1][0]['href'] == f'http://192.168.4.5:19876/#event={eid}'
        assert eid in content[0][0]['text'] and '合成演练' in content[0][0]['text']
        event = client.get(f'/api/events/{eid}').json()
        assert event['id'] == eid and event['notification']['id'] == sent[0]['uuid']
        assert event['notification']['status'] == 'accepted' and event['notification']['confirmed_at'] is None
