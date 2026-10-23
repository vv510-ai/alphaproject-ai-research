"""Six modules through public APIs and registered workers; only external I/O is fake."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import time
import uuid
import re

from fastapi.testclient import TestClient

from backend.app import notification_service
from backend.app.ai_client import AITransportFailure
from backend.app.main import create_app
from backend.app.quotes import Quote
from backend.app.securities import Security
from backend.tests.test_briefings import Clock, DailySource, Sender, CONFIG as FEISHU_CONFIG, HEADERS
from backend.tests.test_explanations import FakeModel, CONFIG as MODEL_CONFIG


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.04)
    raise AssertionError('The expected integration state did not appear before the deadline')


def test_six_module_flow_keeps_notification_independent_of_slow_failing_ai(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_service, 'load_config', lambda _: FEISHU_CONFIG)
    clock = Clock()  # 2026-10-19 08:44:50 China time.
    daily = DailySource(clock)
    daily.close = '100.00'
    send_entered, send_release = threading.Event(), threading.Event()
    model_entered, model_release = threading.Event(), threading.Event()

    class Resolver:
        def resolve(self, symbol):
            return Security(symbol, 'sz', '联调合成股票', 'isolated resolver', clock().isoformat())

    class Quotes:
        def fetch(self, symbol):
            return Quote('sz', symbol, '联调合成股票', '102', '100', '2', '2',
                         clock().isoformat(), clock().isoformat(), 'isolated quote source', '', 'fixture')

    class GatedSender(Sender):
        gated = False
        def verify_target(self):
            if self.gated:
                send_entered.set()
                assert send_release.wait(12)
            return super().verify_target()

    class GatedModel(FakeModel):
        first = True
        def complete(self, messages):
            if self.first:
                self.first = False
                self.calls.append(messages)
                model_entered.set()
                assert model_release.wait(12)
                raise AITransportFailure('fixture_timeout', '隔离模型等待后超时', uncertain=True)
            return super().complete(messages)

    sender, model = GatedSender(), GatedModel()
    database = tmp_path / 'six-modules.sqlite3'

    def application():
        from backend.tests.test_briefings import EmptyResearchProvider
        return create_app(database, Resolver(), ['testserver'], quote_source=Quotes(), clock=clock,
                          daily_source=daily, notification_client=sender, run_monitor=True,
                          ai_client=model, ai_config=MODEL_CONFIG, research_source=EmptyResearchProvider())

    with TestClient(application()) as client:
        def get(path):
            response = client.get('/api' + path)
            assert response.status_code == 200
            return response.json()
        def post(path, body=None):
            return client.post('/api' + path, headers=HEADERS, json=body)
        try:
            # Real API -> resolver boundary -> actual SQLite -> independent reread.
            added = post('/watchlist', {'symbol': '000001'})
            assert added.status_code == 201
            item = added.json()['item']
            assert get('/watchlist')['items'] == [item]
            assert post('/watchlist', {'symbol': '000001'}).json()['created'] is False
            assert post('/quotes/sz/000001/refresh').json()['refresh_succeeded']
            assert get('/dashboard')['items'][0]['quote']['data']['price'] == '102'
            history = post('/daily/sz/000001/refresh', {'watchlist_created_at': item['created_at']}).json()
            assert history['refresh_succeeded'] and history['history']['data']['latest_trade_date'] == '2026-10-16'
            assert post('/notifications/settings', {'enabled': True}).status_code == 200
            assert client.put('/api/briefings/settings', headers=HEADERS, json={
                'enabled': True, 'send_time': '08:45', 'expected_version': 0}).status_code == 200
            rule = post('/anomalies/rules/sz/000001', {'watchlist_created_at': item['created_at'],
                'expected_version': 0, 'up_threshold': '3', 'down_threshold': '3', 'enabled': False})
            assert rule.status_code == 200
            sender.gated = True
            demo_id = str(uuid.uuid4())
            assert post('/anomalies/demo', {'request_id': demo_id, 'market': 'sz', 'symbol': '000001',
                'watchlist_created_at': item['created_at'], 'expected_version': 1, 'direction': 'down', 'notify': True}).status_code == 202
            clock.advance(3)
            until(lambda: get('/anomalies')['runs'][0]['step'] >= 1)
            clock.advance(3)
            assert send_entered.wait(4)
            event = get('/anomalies')['events'][0]
            event_id = event['id']
            assert event['snapshot']['quote']['price'] == '97'
            assert event['notification']['event_id'] == event_id
            assert event['notification']['status'] == 'checking'
            route = f'/events/{event_id}/explanations'
            evidence_hash = get(route)['evidence']['evidence_hash']
            request_id = str(uuid.uuid4())
            body = {'request_id': request_id, 'evidence_hash': evidence_hash, 'retry_of': None}
            with ThreadPoolExecutor(max_workers=1) as pool:
                generation = pool.submit(post, route, body)
                try:
                    assert model_entered.wait(3)
                    clock.advance(20)  # Cross the scheduled minute while AI is waiting.
                    send_release.set()
                    until(lambda: get('/anomalies')['events'][0]['notification']['status'] == 'accepted')
                    until(lambda: len(get('/briefings')['reports']) == 1 and
                          get('/briefings')['reports'][0]['notification'] and
                          get('/briefings')['reports'][0]['notification']['status'] == 'accepted')
                    assert get(route)['records'][0]['status'] == 'running'
                    assert len(sender.sent) == 2  # One anomaly, one scheduled briefing.
                    assert post(route, body).json()['reused'] is True
                    assert len(model.calls) == 1
                finally:
                    model_release.set()
                assert generation.result(timeout=3).status_code == 202
            failed = get(route)['records'][0]
            assert failed['status'] == 'failed' and failed['error_code'] == 'fixture_timeout'
            assert failed['cost_basis'] == 'reserved_unknown'
            # User explicitly retries; same-event evidence and base notification stay intact.
            retry = post(route, {'request_id': str(uuid.uuid4()), 'evidence_hash': evidence_hash, 'retry_of': failed['id']})
            assert retry.status_code == 202
            success = get(route)['records'][0]
            assert success['status'] == 'succeeded' and success['event_id'] == event_id
            assert success['output']['factual_echo']['price'] == '97'
            assert post(f"/explanations/{success['id']}/review", {
                'facts_checked': True, 'citations_checked': True, 'no_invented_causes': True}).status_code == 200
            assert get(route)['records'][0]['reviewed_at']
            assert post(route, {'request_id': str(uuid.uuid4()), 'evidence_hash': evidence_hash, 'retry_of': None}).json()['reused']
            assert len(model.calls) == 2 and len(sender.sent) == 2
            briefing = get('/briefings')['reports'][0]
            assert briefing['snapshot']['watchlist'][0]['created_at'] == item['created_at']
            assert briefing['output']['items'][0]['symbol'] == '000001'
            assert briefing['output']['data_trade_date'] == '2026-10-16'
            assert briefing['output']['items'][0]['data']['volume'] == '10000'
            assert briefing['notification']['briefing_id'] == briefing['id']
            assert event_id in sender.sent[0][1] and briefing['id'] in sender.sent[1][1]
            receipt = re.search(r'收件核验码：(\d{6})', sender.sent[0][1]).group(1)
            notification_id = event['notification']['id']
            assert post(f'/notifications/{notification_id}/confirm', {'code': receipt}).status_code == 200
            confirmed = next(row for row in get('/notifications')['records'] if row['id'] == notification_id)
            assert confirmed['confirmed_at']
            before_cost = get(route)['budget']['month_used']
            # Removing an active list entry must retain existing evidence and explanations.
            assert client.delete('/api/watchlist/sz/000001', headers=HEADERS).json()['removed']
            assert get('/watchlist')['total'] == 0
            assert get('/anomalies')['events'][0]['id'] == event_id
            assert get('/briefings')['reports'][0]['output'] == briefing['output']
            assert get('/briefings')['reports'][0]['watchlist_changed']
            assert get(route)['records'][0]['output'] == success['output']
        finally:
            send_release.set()
            model_release.set()
    with TestClient(application()) as restarted:
        assert restarted.get('/api/watchlist').json()['total'] == 0
        explanation = restarted.get('/api' + route).json()
        assert explanation['records'][0]['output'] == success['output']
        assert explanation['records'][0]['reviewed_at'] and explanation['budget']['month_used'] == before_cost
        assert restarted.get('/api/briefings').json()['reports'][0]['id'] == briefing['id']
        assert restarted.get('/api/anomalies').json()['events'][0]['id'] == event_id
        persisted = next(row for row in restarted.get('/api/notifications').json()['records'] if row['id'] == notification_id)
        assert persisted['confirmed_at'] == confirmed['confirmed_at']
        assert len(model.calls) == len(sender.sent) == 2
