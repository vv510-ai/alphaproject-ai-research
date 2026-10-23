"""Read-only history traversal over persisted fixtures, without external services."""
from datetime import datetime, timedelta, timezone
import json
import uuid

from fastapi.testclient import TestClient
import pytest

from backend.app.main import create_app


@pytest.fixture
def history_app(tmp_path):
    now = [datetime(2026, 10, 22, 3, 10, tzinfo=timezone.utc)]
    app = create_app(tmp_path / 'history.sqlite3', clock=lambda: now[0], run_monitor=False,
                     allowed_hosts=['testserver'])
    with TestClient(app) as client:
        yield app, client, now


def insert_events(app, count, *, mode='live', start=0, at=None):
    at = at or datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
    records = []
    with app.state.store.connect() as db, db:
        for index in range(start, start + count):
            event_id = str(uuid.UUID(int=index + 1))
            created = (at + timedelta(seconds=(index - start) // 2)).isoformat()
            db.execute('''INSERT INTO anomaly_events
                (id,mode,market,symbol,direction,trade_date,created_at,snapshot,run_id)
                VALUES (?,?, 'sh',?, 'up', '2026-10-22',?,?,NULL)''',
                (event_id, mode, str(600000 + index), created,
                 json.dumps({'quote': {'raw_text': 'controlled fixture', 'price': '103'}})))
            records.append((event_id, created))
    return records


def test_history_pages_do_not_lose_live_events_behind_recent_demos(history_app):
    app, client, _ = history_app
    expected = insert_events(app, 65)
    insert_events(app, 45, mode='demo', start=100, at=datetime(2026, 10, 22, 3, tzinfo=timezone.utc))
    assert all(event['mode'] == 'demo' for event in client.get('/api/anomalies').json()['events'])
    params = {'since': '2026-10-22T02:00:00Z', 'limit': 7}
    seen, through = [], None
    for _ in range(12):
        response = client.get('/api/anomalies/events', params=params)
        assert response.status_code == 200
        body = response.json()
        assert all(event['mode'] == 'live' and 'raw_text' not in event['snapshot']['quote'] for event in body['events'])
        seen.extend(event['id'] for event in body['events'])
        metadata = body['event_history']
        through = through or metadata['through']
        assert metadata['through'] == through
        if metadata['complete']:
            assert metadata['next_cursor'] is None
            break
        params['cursor'] = metadata['next_cursor']
    else:
        pytest.fail('bounded traversal did not finish')
    assert seen == [event_id for event_id, _ in expected]
    assert len(set(seen)) == 65


def test_since_is_inclusive_and_normalizes_timezones(history_app):
    app, client, _ = history_app
    expected = insert_events(app, 6)
    one = client.get('/api/anomalies/events', params={'since': '2026-10-22T10:00:01+08:00'}).json()
    two = client.get('/api/anomalies/events', params={'since': '2026-10-22T02:00:01Z'}).json()
    assert [event['id'] for event in one['events']] == [event_id for event_id, _ in expected[2:]]
    assert one['events'] == two['events']


def test_cursor_keeps_original_upper_bound_and_does_not_write(history_app):
    app, client, now = history_app
    expected = insert_events(app, 4)
    params = {'since': '2026-10-22T02:00:00Z', 'limit': 2}
    first = client.get('/api/anomalies/events', params=params).json()
    insert_events(app, 1, start=500, at=now[0] + timedelta(seconds=1))
    now[0] += timedelta(seconds=5)
    with app.state.store.connect() as db:
        before = [tuple(row) for row in db.execute('SELECT * FROM anomaly_events ORDER BY id')]
    second = client.get('/api/anomalies/events', params={**params, 'cursor': first['event_history']['next_cursor']}).json()
    assert second['event_history']['through'] == first['event_history']['through']
    assert second['event_history']['complete']
    assert [event['id'] for event in first['events'] + second['events']] == [item[0] for item in expected]
    with app.state.store.connect() as db:
        assert before == [tuple(row) for row in db.execute('SELECT * FROM anomaly_events ORDER BY id')]
    assert len(client.get('/api/anomalies/events', params={'since': params['since']}).json()['events']) == 5


@pytest.mark.parametrize('params', [
    {'since': '2026-10-22'}, {'since': 'not a date'}, {'since': '2026-10-22T02:00:00Z', 'limit': 0},
    {'since': '2026-10-22T02:00:00Z', 'limit': 101},
    {'since': '2026-10-22T02:00:00Z', 'cursor': '%%%invalid'},
    {'since': '2026-10-22T02:00:00Z', 'cursor': 'W10='},
])
def test_invalid_queries_fail_without_side_effects(history_app, params):
    app, client, _ = history_app
    assert client.get('/api/anomalies/events', params=params).status_code == 400
    with app.state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM anomaly_events').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM notification_messages').fetchone()[0] == 0


def test_cursor_cannot_silently_change_the_requested_start(history_app):
    app, client, _ = history_app
    insert_events(app, 3)
    first = client.get('/api/anomalies/events', params={'since': '2026-10-22T02:00:00Z', 'limit': 1}).json()
    response = client.get('/api/anomalies/events', params={
        'since': '2026-10-22T02:00:01Z', 'cursor': first['event_history']['next_cursor']})
    assert response.status_code == 400
