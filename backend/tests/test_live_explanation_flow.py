"""Real live-event/AI services and SQLite; clock, feed and model are isolated.

No natural trading result is claimed. Raw prices and volume are controlled, all
external HTTP is blocked, and no real credential or recipient is used.
"""

import json
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app import main as main_module
from backend.app import notification_service as notification_module
from backend.app.explanation_evidence import freeze_event
from backend.app.securities import LookupFailure
from backend.tests.test_explanations import CONFIG as AI_CONFIG, FakeModel, HEADERS
from backend.tests.test_live_acceptance import Clock, RawFeed, FakeFeishuWire, CONFIG, security


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.delenv('CREDENTIALS_DIRECTORY', raising=False)
    monkeypatch.delenv('ALPHA_LAB_PUBLIC_URL', raising=False)
    monkeypatch.setattr(main_module, 'lan_addresses', lambda: [])
    monkeypatch.setattr(notification_module, 'load_config', lambda _: CONFIG)

    def blocked(*args, **kwargs):
        raise AssertionError('Real HTTP is forbidden in live explanation regression')

    async def blocked_async(*args, **kwargs):
        raise AssertionError('Real async HTTP is forbidden in live explanation regression')

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', blocked_async)
    clock = Clock()
    feed, wire, model = RawFeed(clock), FakeFeishuWire(), FakeModel()

    class Resolver:
        def resolve(self, symbol):
            return security(clock, symbol)

    def application():
        return main_module.create_app(tmp_path / 'live-explanation.sqlite3', Resolver(), ['testserver'],
                                      quote_source=feed, clock=clock, notification_client=wire.client,
                                      run_monitor=False, ai_client=model, ai_config=AI_CONFIG)

    yield SimpleNamespace(application=application, clock=clock, feed=feed, wire=wire, model=model)
    assert wire.requests == [] and wire.messages == []


def create_live_event(rig, app, client, direction='up'):
    added = client.post('/api/watchlist', headers=HEADERS, json={'symbol': '600519'})
    assert added.status_code == 201
    item = added.json()['item']
    rule = client.post('/api/anomalies/rules/sh/600519', headers=HEADERS, json={
        'watchlist_created_at': item['created_at'], 'expected_version': 0,
        'up_threshold': '3', 'down_threshold': '3', 'enabled': True,
    })
    assert rule.status_code == 200
    for index, price in enumerate(('102.00', '103.00') if direction == 'up' else ('98.00', '97.00')):
        if index:
            rig.clock.advance()
        rig.feed.publish(price=price, volume='1100' if index else '1000')
        refreshed = client.post('/api/quotes/sh/600519/refresh', headers=HEADERS)
        assert refreshed.status_code == 200
        assert refreshed.json()['quote']['eligibility']['eligible'] is bool(index)
        app.state.anomalies.check_live()
        events = client.get('/api/anomalies').json()['events']
        assert len(events) == index
    event = events[0]
    assert event['mode'] == 'live' and event['direction'] == direction
    assert event['snapshot']['eligibility']['eligible'] is True
    assert event['snapshot']['quote']['price'] == price
    assert rig.feed.calls == ['600519', '600519']
    return event


def stored_event(app, event_id):
    with app.state.store.connect() as db:
        return dict(db.execute('SELECT * FROM anomaly_events WHERE id=?', (event_id,)).fetchone())


@pytest.mark.parametrize('direction', ['up', 'down'])
def test_valid_live_event_can_freeze_read_and_generate_through_public_api(isolated, direction):
    rig, app = isolated, isolated.application()
    with TestClient(app) as client:
        event = create_live_event(rig, app, client, direction)
        route = f'/api/events/{event["id"]}/explanations'
        response = client.get(route)
        assert response.status_code == 200, response.json()
        evidence = response.json()['evidence']
        frozen = freeze_event(stored_event(app, event['id']))
        assert frozen['event_id'] == event['id'] and frozen['mode'] == 'live'
        assert frozen['evidence_hash'] == evidence['evidence_hash']
        assert frozen['model_input']['factual_echo']['price'] == event['snapshot']['quote']['price']
        assert rig.model.calls == []
        requested = client.post(route, headers=HEADERS, json={
            'request_id': str(uuid.uuid4()), 'evidence_hash': evidence['evidence_hash'],
        })
        assert requested.status_code == 202, requested.json()
        state = client.get(route).json()
        record = state['records'][0]
        assert record['status'] == 'succeeded' and record['event_id'] == event['id']
        assert record['evidence'] == evidence and record['reviewed_at'] is None
        assert record['output']['factual_echo']['mode'] == 'live'
        assert record['output']['factual_echo']['direction'] == direction
        assert record['output']['factual_echo']['price'] == event['snapshot']['quote']['price']
        assert record['output']['cause_status'] == 'insufficient_evidence'
        repeated = client.post(route, headers=HEADERS, json={
            'request_id': str(uuid.uuid4()), 'evidence_hash': evidence['evidence_hash'],
        })
        assert repeated.status_code == 202 and repeated.json()['reused'] is True
        assert len(rig.model.calls) == 1


def corrupt_snapshot(snapshot, fault):
    if fault == 'missing':
        snapshot.pop('eligibility')
    elif fault == 'null':
        snapshot['eligibility'] = None
    elif fault == 'denied':
        snapshot['eligibility']['eligible'] = False
    elif fault == 'forged_flag':
        snapshot['eligibility'] = {'eligible': True, 'proof': {}}
    elif fault == 'null_snapshot_generation':
        snapshot['watchlist_created_at'] = None
    elif fault in ('empty_snapshot_generation', 'naive_snapshot_generation', 'all_null_generations'):
        generation = {'empty_snapshot_generation': '',
                      'naive_snapshot_generation': '2026-10-22T10:00:00',
                      'all_null_generations': None}[fault]
        snapshot['watchlist_created_at'] = generation
        proof = snapshot['eligibility']['proof']
        for record in (proof, proof['previous'], proof['current']):
            record['watchlist_created_at'] = generation
    else:
        proof = snapshot['eligibility']['proof']
        if fault == 'wrong_identity':
            proof['current']['symbol'] = '000001'
        elif fault == 'wrong_quote_hash':
            proof['current']['quote_hash'] = '0' * 64
        elif fault == 'no_volume_growth':
            proof['current']['cumulative_volume'] = proof['previous']['cumulative_volume']
        elif fault == 'source_not_advanced':
            proof['current']['source_reported_at'] = proof['previous']['source_reported_at']
        elif fault == 'wrong_session':
            proof['session_id'] = '2026-10-22/pm'
        elif fault == 'old_generation_pair':
            # Internally consistent proof still belongs to another watchlist
            # generation; match the frozen event, never today's current list.
            for record in (proof, proof['previous'], proof['current']):
                record['watchlist_created_at'] = '2026-01-01T00:00:00+00:00'
        else:
            raise AssertionError(fault)


@pytest.mark.parametrize('fault', ['missing', 'null', 'denied', 'forged_flag', 'wrong_identity',
                                  'wrong_quote_hash', 'no_volume_growth', 'source_not_advanced',
                                  'wrong_session', 'old_generation_pair', 'null_snapshot_generation',
                                  'empty_snapshot_generation', 'naive_snapshot_generation', 'all_null_generations'])
def test_bad_frozen_live_qualification_never_reaches_model_or_budget(isolated, fault):
    rig, app = isolated, isolated.application()
    with TestClient(app) as client:
        event = create_live_event(rig, app, client)
        snapshot = json.loads(stored_event(app, event['id'])['snapshot'])
        corrupt_snapshot(snapshot, fault)
        with app.state.store.connect() as db, db:
            db.execute('UPDATE anomaly_events SET snapshot=? WHERE id=?', (json.dumps(snapshot), event['id']))
        with pytest.raises(LookupFailure) as rejected:
            freeze_event(stored_event(app, event['id']))
        assert rejected.value.code == 'invalid_evidence'
        route = f'/api/events/{event["id"]}/explanations'
        read = client.get(route)
        assert read.status_code == 422 and read.json()['error']['code'] == 'invalid_evidence'
        requested = client.post(route, headers=HEADERS, json={
            'request_id': str(uuid.uuid4()), 'evidence_hash': '0' * 64,
        })
        assert requested.status_code == 422 and requested.json()['error']['code'] == 'invalid_evidence'
        assert rig.model.calls == []
        with app.state.store.connect() as db:
            assert db.execute('SELECT count(*) FROM ai_explanations').fetchone()[0] == 0
            assert db.execute('SELECT count(*) FROM ai_request_aliases').fetchone()[0] == 0


def test_live_history_uses_original_time_and_generation_after_changes_and_restart(isolated):
    rig, app = isolated, isolated.application()
    with TestClient(app) as client:
        event = create_live_event(rig, app, client)
        route = f'/api/events/{event["id"]}/explanations'
        first = client.get(route)
        assert first.status_code == 200, first.json()
        evidence = first.json()['evidence']
        original = stored_event(app, event['id'])
        # Replace the present quote and rule, then move beyond the source F=120s.
        rig.clock.advance()
        rig.feed.publish(price='110.00', volume='1200')
        assert client.post('/api/quotes/sh/600519/refresh', headers=HEADERS).status_code == 200
        changed = client.post('/api/anomalies/rules/sh/600519', headers=HEADERS, json={
            'watchlist_created_at': event['snapshot']['watchlist_created_at'], 'expected_version': 1,
            'up_threshold': '8', 'down_threshold': '8', 'enabled': False,
        })
        assert changed.status_code == 200
        assert client.delete('/api/watchlist/sh/600519', headers=HEADERS).json()['removed'] is True
        readded = client.post('/api/watchlist', headers=HEADERS, json={'symbol': '600519'})
        assert readded.status_code == 201
        assert readded.json()['item']['created_at'] != event['snapshot']['watchlist_created_at']
        rig.clock.advance(86400)
        assert client.get(route).json()['evidence'] == evidence
        requested = client.post(route, headers=HEADERS, json={
            'request_id': str(uuid.uuid4()), 'evidence_hash': evidence['evidence_hash'],
        })
        assert requested.status_code == 202, requested.json()
        record = client.get(route).json()['records'][0]
        assert record['status'] == 'succeeded' and record['evidence'] == evidence
        assert record['output']['factual_echo']['price'] == '103.00'
        assert record['output']['factual_echo']['rule_version'] == '1'
        assert stored_event(app, event['id']) == original
    with TestClient(rig.application()) as restarted:
        state = restarted.get(route).json()
        assert state['evidence'] == evidence and state['records'][0] == record
        assert len(rig.model.calls) == 1
