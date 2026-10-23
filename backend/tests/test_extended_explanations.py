"""Qualified source pairs -> optional rule event -> immutable AI explanation."""
from datetime import timedelta
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from backend.app.ai_client import Completion
from backend.app.extended_rules import DEFAULT_CONFIG
from backend.tests.test_explanations import HEADERS
from backend.tests.test_live_explanation_flow import isolated
from backend.tests.test_unified_explanations import Provider


class ExtendedModel:
    def __init__(self):
        self.calls, self.mutate = [], None

    def complete(self, messages):
        self.calls.append(messages)
        data = json.loads(messages[-1]['content'])
        echo = data['factual_echo']
        label = {'up': '上涨', 'down': '下跌', 'flat': '持平'}[echo['direction']]
        values = next(fact['values'] for fact in data['facts'] if fact['id'] == 'F4')
        triggers = {
            'volume': f'来源量比为 {echo["metric_value"]} 倍，超过设定的 {echo["threshold"]} 倍，因此满足本类提醒；量比并不代表价格变化幅度。',
            'amplitude': f'按照保存的最高和最低价格复算，振幅为 {values["metric_display"]}%，超过设定 {echo["threshold"]}% 的阈值。这是日内价格范围的指标。',
            'limit_up': f'现价 {echo["price"]} 元已触及来源涨停价 {echo["threshold"]} 元；这只是本次采样条件，不能确定保持多久。',
            'limit_down': f'现价 {echo["price"]} 元已触及来源跌停价 {echo["threshold"]} 元；这只是本次采样条件，不能确定保持多久。',
            'event': '本次取得启用后发布且保留来源和时间的新资讯，因此满足资料提醒条件；并不据此推断价格变化原因。',
        }
        output = {'event_id': data['event_id'], 'evidence_hash': data['evidence_hash'],
                  'factual_echo': echo, 'cause_status': data['cause_status'],
                  'missing_evidence_ids': data['missing_evidence_ids'], 'statements': [
            {'kind': 'change', 'text': f'这份报价为 {echo["price"]} 元，昨收 {echo["previous_close"]} 元，相对昨收{label}，程序复算的变化为 {echo["change_pct_display"]}%。只描述保存当时。', 'evidence_ids': ['F2', 'F3']},
            {'kind': 'trigger', 'text': triggers[echo['rule_type']], 'evidence_ids': ['F4']},
            {'kind': 'scope', 'text': '这是已经保存的真实规则事件，只对应当时采样；不能确定条件首次出现的准确时刻，同一股票同类规则当天最多记录一次。', 'evidence_ids': ['F1', 'F5', 'F6']}]}
        if 'research_schema_version' in data:
            output['research_statements'] = [{'text': source['summary'] or source['title'], 'evidence_ids': [source['id']],
                                             'relation': 'context_only'} for source in data['research_evidence'][:3]]
        if self.mutate:
            self.mutate(output)
        return Completion(json.dumps(output, ensure_ascii=False), 'deepseek-flash', 'extended-isolated', 1800, 700, 'stop', 30)


def create_extended(rig, app, client, kind):
    item = client.post('/api/watchlist', headers=HEADERS, json={'symbol': '600519'}).json()['item']
    config = dict(DEFAULT_CONFIG)
    flag = {'volume': 'volume_enabled', 'amplitude': 'amplitude_enabled',
            'limit_up': 'limit_enabled', 'limit_down': 'limit_enabled', 'event': 'event_enabled'}[kind]
    config[flag] = True
    response = client.put('/api/anomalies/extended/sh/600519', headers=HEADERS, json={
        'watchlist_created_at': item['created_at'], 'expected_version': 0, 'enabled': True, 'config': config})
    assert response.status_code == 200, response.text
    rule = response.json()
    ending = {'volume': '103.00', 'amplitude': '100.00', 'limit_up': '110.00', 'limit_down': '90.00', 'event': '103.00'}[kind]
    for index, price in enumerate(('100.00', ending)):
        if index:
            rig.clock.advance()
        rig.feed.publish(price=price, volume='1100' if index else '1000')
        fields = rig.feed.raw['600519'].split('"')[1].split('~')
        fields += [''] * (50 - len(fields))
        for offset, value in {33: '110.00', 34: '90.00', 47: '110.00', 48: '90.00', 49: '4.00'}.items():
            fields[offset] = value
        rig.feed.raw['600519'] = 'v_sh600519="' + '~'.join(fields) + '";'
        assert client.post('/api/quotes/sh/600519/refresh', headers=HEADERS).status_code == 200
    if kind == 'event':
        provider = Provider(rig.clock)
        provider.mutate = lambda key, section: section['items'][0].update(published_at=(rig.clock() - timedelta(seconds=30)).isoformat()) if key in ('news', 'announcements', 'calendar') else None
        app.state.research.provider = provider
        app.state.research.refresh('sh', '600519')
    outcomes = app.state.extended.evaluate(rule, notify=False)
    hit = next(item for item in outcomes if item['rule_type'] == kind)
    assert hit['status'] == 'triggered', outcomes
    return hit['event_id']


def generate(client, event_id):
    url = f'/api/events/{event_id}/explanations'
    view = client.get(url)
    assert view.status_code == 200, view.text
    response = client.post(url, headers=HEADERS, json={'request_id': str(uuid.uuid4()),
        'evidence_hash': view.json()['evidence']['evidence_hash']})
    assert response.status_code == 202, response.text
    return client.get(url).json()['records'][0]


@pytest.mark.parametrize('kind,direction,unit', [('volume', 'up', '倍'), ('amplitude', 'flat', '%'),
                                               ('limit_up', 'up', '元'), ('limit_down', 'down', '元')])
def test_actual_extended_rule_api_price_pair_to_model_keeps_direction_and_units(isolated, kind, direction, unit):
    app, model = isolated.application(), ExtendedModel()
    with TestClient(app) as client:
        app.state.explanations.client = model
        event_id = create_extended(isolated, app, client, kind)
        record = generate(client, event_id)
        assert record['status'] == 'succeeded', record
        echo = record['output']['factual_echo']
        assert echo['direction'] == direction and echo['rule_type'] == kind and echo['metric_unit'] == unit
        assert record['evidence']['rule_type'] == kind and record['prompt_version'].startswith('extended-event-explanation-1')
        assert json.loads(model.calls[0][-1]['content'])['factual_echo'] == echo
        assert record['reviewed_at'] is None
        with app.state.store.connect() as db:
            assert db.execute('SELECT count(*) FROM notification_messages').fetchone()[0] == 0
        assert isolated.wire.requests == []


def test_news_trigger_uses_its_exact_saved_article_even_without_current_research_cache(isolated):
    app, model = isolated.application(), ExtendedModel()
    with TestClient(app) as client:
        app.state.explanations.client = model
        event_id = create_extended(isolated, app, client, 'event')
        # The triggering article belongs to the event, even after its source cache disappears.
        with app.state.store.connect() as db, db:
            db.execute('DELETE FROM research_cache')
            db.execute('DELETE FROM research_snapshots')
        record = generate(client, event_id)
        assert record['status'] == 'succeeded', record
        assert record['output']['factual_echo']['metric_unit'] == '条'
        assert record['research_input_count'] >= 1 and record['research_used_count'] >= 1
        original = record['evidence']['trigger_evidence']['news']
        assert any(item['original_id'] == original['id'] and item['url'] == original['url'] for item in record['evidence']['research_evidence'])
        assert any(item['text'] == original['summary'] for item in record['output']['research_statements'])
        assert '%' not in record['output']['statements'][1]['text']


@pytest.mark.parametrize('mutation,code', [
    (lambda output: output['factual_echo'].update(direction='down'), 'wrong_fact'),
    (lambda output: output['statements'][1].update(text='来源量比超过设定条件，因此这是一条下跌提醒，说明价格降低。'), 'misleading_rule_direction'),
    (lambda output: output['statements'][1].update(text='来源量比为 4.00%，超过设定的 3%，本次已满足提醒条件。'), 'wrong_rule_unit'),
])
def test_volume_cannot_be_rewritten_as_down_move_or_percent(isolated, mutation, code):
    app, model = isolated.application(), ExtendedModel()
    model.mutate = mutation
    with TestClient(app) as client:
        app.state.explanations.client = model
        event_id = create_extended(isolated, app, client, 'volume')
        record = generate(client, event_id)
        assert record['status'] == 'rejected' and record['error_code'] == code


@pytest.mark.parametrize('fault', ['generation', 'unit', 'metric_source', 'metric_time', 'threshold', 'rule_type'])
def test_corrupt_extended_evidence_is_rejected_before_model_and_budget(isolated, fault):
    app, model = isolated.application(), ExtendedModel()
    with TestClient(app) as client:
        app.state.explanations.client = model
        event_id = create_extended(isolated, app, client, 'volume')
        with app.state.store.connect() as db, db:
            saved = json.loads(db.execute('SELECT snapshot FROM anomaly_events WHERE id=?', (event_id,)).fetchone()[0])
            if fault == 'generation': saved['watchlist_created_at'] = (isolated.clock() + timedelta(seconds=5)).isoformat()
            if fault == 'unit': saved['quote']['price_unit'] = '美元'
            if fault == 'metric_source': saved['rule_evidence']['metrics']['raw_sha256'] = 'f' * 64
            if fault == 'metric_time': saved['rule_evidence']['metrics']['received_at'] = (isolated.clock() + timedelta(seconds=1)).isoformat()
            if fault == 'threshold': saved['threshold'] = '2'
            if fault == 'rule_type': saved['rule_type'] = 'amplitude'
            db.execute('UPDATE anomaly_events SET snapshot=? WHERE id=?', (json.dumps(saved), event_id))
        result = client.get(f'/api/events/{event_id}/explanations')
        assert result.status_code == 422 and result.json()['error']['code'] == 'invalid_extended_evidence'
        assert model.calls == []
        with app.state.store.connect() as db:
            assert db.execute('SELECT count(*) FROM ai_explanations').fetchone()[0] == 0
