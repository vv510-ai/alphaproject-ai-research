"""Synthetic daily data and fake Feishu; actual API, persistence and registered workers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import threading
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from backend.app import notification_service as notification_module
from backend.app.daily import DailyFailure, parse_daily
from backend.app.feishu import FeishuFailure, Target
from backend.app.feishu_config import FeishuConfig
from backend.app.main import create_app
from backend.app.securities import Security, market_for
from backend.app.trading_calendar import TradingCalendar

NOW = datetime(2026, 10, 19, 0, 44, 50, tzinfo=timezone.utc)
CONFIG = FeishuConfig('cli_briefing_fixture', 'FAKE_BRIEFING_SECRET', 'oc_fixture', 'ou_fixture', '隔离单人群', '隔离机器人', NOW.isoformat())
HEADERS = {'X-Alpha-Request': 'watchlist-v1', 'Origin': 'http://testserver'}


class Clock:
    def __init__(self): self.now = NOW
    def __call__(self): return self.now
    def advance(self, seconds): self.now += timedelta(seconds=seconds)


class EmptyResearchProvider:
    """No network is possible when existing scheduler tests gain research wiring."""
    def fetch_section(self, key, market, symbol):
        return {'status': 'unavailable', 'source': '隔离研究夹具',
                'source_url': 'https://research-fixture.invalid/', 'items': [],
                'error_message': '本用例仅核验原简报流程；研究资料受控缺项。'}


class DailySource:
    def __init__(self, clock):
        self.clock, self.calls = clock, []
        self.fail_for, self.older_for = set(), set()
        self.during_fetch = self.mutate = None
        self.close = '102.00'

    def fetch(self, symbol, start, end):
        self.calls.append((symbol, start.isoformat(), end.isoformat()))
        if self.during_fetch: self.during_fetch()
        if symbol in self.fail_for: raise DailyFailure('fixture_missing', '合成来源本项不可用。')
        target = end
        while TradingCalendar().day(target)[0] == 'closed':
            target -= timedelta(days=1)
        if symbol in self.older_for:
            target -= timedelta(days=1)
        key = market_for(symbol) + symbol
        row = [target.isoformat(), '101.00', self.close, '110.00', '99.00', '100.00', {}, '0.01', self.close]
        body = json.dumps({'code': 0, 'data': {key: {'day': [row], 'qt': {key: ['1', '隔离股票', symbol]}}}})
        history = parse_daily(body, symbol, start, end, self.clock())
        if self.mutate: return self.mutate(history)
        return history


class Sender:
    def __init__(self): self.sent, self.during_check, self.failure = [], None, None
    def verify_target(self):
        if self.during_check: self.during_check()
        return Target(CONFIG.group_name, CONFIG.bot_name, CONFIG.owner_id)
    def send_test(self, request_id, text, detail_url=None):
        self.sent.append((request_id, text))
        if self.failure: raise self.failure
        return 'om_briefing_fixture'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_module, 'load_config', lambda _: CONFIG)
    clock, sender = Clock(), Sender()
    source = DailySource(clock)
    app = create_app(tmp_path / 'briefing.sqlite3', allowed_hosts=['testserver'], clock=clock,
                     daily_source=source, notification_client=sender, run_monitor=False,
                     research_source=EmptyResearchProvider())
    with TestClient(app) as client:
        yield app, client, clock, source, sender


def add(rig, symbol='600519'):
    app, _, clock, *_ = rig
    return app.state.store.add(Security(symbol, market_for(symbol), '隔离股票 ' + symbol, '测试夹具', clock().isoformat()))[0]


def queue(rig, request_id=None, retry_of=None):
    return rig[1].post('/api/briefings', headers=HEADERS, json={
        'request_id': request_id or str(uuid.uuid4()), 'report_date': rig[2]().astimezone(timezone(timedelta(hours=8))).date().isoformat(),
        'retry_of': retry_of})


def state(rig): return rig[1].get('/api/briefings').json()


def enable(rig, notify=True):
    if notify: rig[0].state.notifications.set_enabled(True)
    version = state(rig)['settings']['version']
    response = rig[1].put('/api/briefings/settings', headers=HEADERS,
        json={'enabled': True, 'send_time': '08:45', 'expected_version': version})
    assert response.status_code == 200


@pytest.mark.parametrize('day,status,previous', [
    ('2026-10-19', 'open', '2026-10-16'), ('2026-10-08', 'open', '2026-09-30'),
    ('2026-02-24', 'open', '2026-02-13'), ('2026-10-10', 'closed', None),
    ('2026-09-20', 'closed', None), ('2026-02-16', 'closed', None),
    ('2027-01-04', 'unknown', None), ('2026-01-05', 'unknown', None),
])
def test_calendar_uses_exchange_holidays_and_fails_closed(day, status, previous):
    context = TradingCalendar().context(date.fromisoformat(day))
    assert context['state'] == status and context['previous_trade_date'] == previous


def test_actual_manual_api_summary_persists_and_never_sends(rig):
    add(rig)
    response = queue(rig)
    assert response.status_code == 202 and response.json()['record']['status'] == 'pending'
    rig[0].state.briefings.tick()
    report = state(rig)['reports'][0]
    assert report['status'] == 'succeeded' and report['notification'] is None
    output = report['output']
    assert output['coverage'] == {'total': 1, 'covered': 1, 'missing': 0}
    assert output['data_trade_date'] == '2026-10-16' and output['model_cost'] == '0'
    assert output['items'][0]['data']['close'] == '102.00' and output['items'][0]['data']['volume'] == '10000'
    assert output['items'][0]['data']['origin'] == 'fetched_daily'
    assert not rig[4].sent and not state(rig)['settings']['enabled']
    with rig[0].state.store.connect() as db:
        assert db.execute('SELECT count(*) FROM ai_explanations').fetchone()[0] == 0
        assert json.loads(db.execute('SELECT output_json FROM briefing_reports').fetchone()[0]) == output
    assert 'FAKE_BRIEFING_SECRET' not in json.dumps(state(rig)) and 'raw_text' not in json.dumps(output)


def test_valid_cached_target_day_avoids_refetch_and_old_snapshot_stays_fixed(rig):
    item = add(rig)
    rig[0].state.daily.refresh('sh', item['symbol'], item['created_at'])
    queue(rig); rig[0].state.briefings.tick()
    first = state(rig)['reports'][0]
    assert first['output']['items'][0]['data']['origin'] == 'saved_daily' and len(rig[3].calls) == 1
    add(rig, '000001')
    rig[3].close = '105.00'
    again = queue(rig).json()['record']
    assert again['id'] == first['id'] and again['output'] == first['output'] and again['watchlist_changed']
    assert len(rig[3].calls) == 1


def test_partial_data_discloses_exact_coverage_and_failure(rig):
    add(rig); add(rig, '000001'); rig[3].fail_for.add('000001')
    queue(rig); rig[0].state.briefings.tick()
    report = state(rig)['reports'][0]
    assert report['status'] == 'partial' and report['output']['coverage'] == {'total': 2, 'covered': 1, 'missing': 1}
    missing = next(item for item in report['output']['items'] if item['symbol'] == '000001')
    assert missing['data'] is None and missing['reason_code'] == 'fixture_missing'


def test_older_daily_is_not_substituted_and_explicit_retry_retains_failure(rig):
    add(rig); rig[3].older_for.add('600519')
    queue(rig); rig[0].state.briefings.tick()
    failed = state(rig)['reports'][0]
    assert failed['status'] == 'failed' and failed['output']['coverage']['covered'] == 0
    assert failed['output']['items'][0]['reason_code'] == 'briefing_date_missing'
    assert queue(rig).status_code == 409
    rig[3].older_for.clear()
    response = queue(rig, retry_of=failed['id'])
    assert response.status_code == 202
    rig[0].state.briefings.tick()
    reports = state(rig)['reports']
    assert reports[0]['status'] == 'succeeded' and reports[0]['retry_of'] == failed['id']
    assert reports[1]['status'] == 'failed' and not rig[4].sent


def test_empty_is_saved_without_push_and_new_watchlist_can_generate(rig):
    enable(rig); rig[2].advance(10); rig[0].state.briefings.tick()
    view = state(rig)
    assert view['reports'][0]['status'] == view['schedule']['status'] == 'empty'
    assert not rig[4].sent
    add(rig); queue(rig); rig[0].state.briefings.tick()
    assert state(rig)['reports'][0]['status'] == 'succeeded'
    rig[0].state.notifications.process_pending_events()
    assert not rig[4].sent  # The completed empty scheduled attempt does not backfill.


def test_removal_during_collection_stops_coverage(rig):
    add(rig)
    rig[3].during_fetch = lambda: rig[0].state.store.remove('sh', '600519')
    queue(rig); rig[0].state.briefings.tick()
    report = state(rig)['reports'][0]
    assert report['status'] == 'failed' and report['output']['items'][0]['reason_code'] == 'watchlist_changed'


@pytest.mark.parametrize('change', ['identity', 'volume', 'time'])
def test_dependency_result_must_pass_summary_boundary(rig, change):
    from dataclasses import replace
    add(rig)
    rig[3].mutate = lambda h: replace(h, **({'symbol': '000001'} if change == 'identity' else
        {'volume_multiplier': 1} if change == 'volume' else {'received_at': '2026-10-15T01:00:00+00:00'}))
    queue(rig); rig[0].state.briefings.tick()
    assert state(rig)['reports'][0]['output']['items'][0]['reason_code'] == 'briefing_invalid_data'


def test_schedule_reuses_manual_report_and_sends_once_with_receipt(rig):
    add(rig); enable(rig)
    manual = queue(rig).json()['record']['id']; rig[0].state.briefings.tick()
    assert not rig[4].sent and not state(rig)['reports'][0]['notification']
    rig[2].advance(10); rig[0].state.briefings.tick()
    rig[0].state.notifications.process_pending_events()
    view = state(rig)
    assert view['schedule']['status'] == 'completed' and view['schedule']['report_id'] == manual
    assert view['reports'][0]['notification']['status'] == 'accepted'
    text = rig[4].sent[0][1]
    assert manual in text and '2026-10-16' in text and '10,000 股' in text and '收件核验码' in text
    for _ in range(3): rig[0].state.briefings.tick(); rig[0].state.notifications.process_pending_events()
    assert len(rig[4].sent) == 1 and len(rig[3].calls) == 1


@pytest.mark.parametrize('when,expected', [
    (datetime(2026, 10, 19, 0, 46, tzinfo=timezone.utc), 'missed'),
    (datetime(2026, 10, 18, 0, 45, tzinfo=timezone.utc), 'closed'),
    (datetime(2027, 1, 4, 0, 45, tzinfo=timezone.utc), 'calendar_unknown'),
])
def test_non_eligible_schedule_never_generates(rig, when, expected):
    add(rig); enable(rig); rig[2].now = when
    rig[0].state.briefings.tick()
    assert state(rig)['schedule']['status'] == expected and not state(rig)['reports']
    assert not rig[3].calls and not rig[4].sent
    if expected != 'missed': assert queue(rig).status_code == 409
    else:
        queue(rig); rig[0].state.briefings.tick(); rig[0].state.notifications.process_pending_events()
        assert state(rig)['reports'][0]['status'] == 'succeeded' and not rig[4].sent


@pytest.mark.parametrize('when', ['during_fetch', 'queued', 'during_verify'])
def test_disabling_schedule_cancels_unsent_notification(rig, when):
    add(rig); enable(rig)
    service = rig[0].state.briefings
    def disable(): service.set_settings(False, '08:45', state(rig)['settings']['version'])
    if when == 'during_fetch': rig[3].during_fetch = disable
    rig[2].advance(10); service.tick()
    if when == 'queued': disable()
    if when == 'during_verify': rig[4].during_check = disable
    rig[0].state.notifications.process_pending_events()
    assert not rig[4].sent
    report = state(rig)['reports'][0]
    assert report['status'] == 'succeeded'
    if report['notification']: assert report['notification']['status'] == 'cancelled'


def test_notification_switch_off_does_not_cancel_report_or_backfill(rig):
    add(rig); enable(rig, notify=False); rig[2].advance(10); rig[0].state.briefings.tick()
    assert state(rig)['reports'][0]['status'] == 'succeeded'
    assert state(rig)['reports'][0]['notification']['status'] == 'cancelled'
    rig[0].state.notifications.set_enabled(True); rig[0].state.notifications.process_pending_events()
    assert not rig[4].sent


@pytest.mark.parametrize('case', ['expired', 'removed', 'unknown'])
def test_send_boundaries_preserve_record_without_repeat(rig, case):
    add(rig); enable(rig); rig[2].advance(10); rig[0].state.briefings.tick()
    if case == 'expired': rig[2].advance(900)
    elif case == 'removed': rig[0].state.store.remove('sh', '600519')
    else: rig[4].failure = FeishuFailure('结果未知', unknown=True)
    rig[0].state.notifications.process_pending_events()
    expected = {'expired': 'expired', 'removed': 'cancelled', 'unknown': 'unknown'}[case]
    assert state(rig)['reports'][0]['notification']['status'] == expected
    rig[0].state.notifications.process_pending_events()
    assert len(rig[4].sent) == (1 if case == 'unknown' else 0)


def test_settings_compare_version_and_noop_does_not_cancel_queue(rig):
    add(rig); enable(rig); rig[2].advance(10); rig[0].state.briefings.tick()
    before = state(rig)
    noop = rig[1].put('/api/briefings/settings', headers=HEADERS, json={'enabled': True, 'send_time': '08:45', 'expected_version': 1})
    assert noop.status_code == 200 and noop.json()['settings']['version'] == 1
    assert noop.json()['reports'][0]['notification']['status'] == 'pending'
    conflict = rig[1].put('/api/briefings/settings', headers=HEADERS, json={'enabled': False, 'send_time': '08:45', 'expected_version': 0})
    assert conflict.status_code == 409 and state(rig)['settings'] == before['settings']


def test_request_replay_survives_midnight_and_rejects_changed_body(rig):
    add(rig); original = str(uuid.uuid4()); queue(rig, original); rig[0].state.briefings.tick()
    alias = str(uuid.uuid4()); assert queue(rig, alias).json()['record']['id'] == original
    rig[2].advance(86400)
    same = rig[1].post('/api/briefings', headers=HEADERS, json={'request_id': alias, 'report_date': '2026-10-19', 'retry_of': None})
    assert same.status_code == 202 and same.json()['record']['id'] == original
    assert queue(rig, alias).status_code == 409 and len(rig[3].calls) == 1


def test_parallel_manual_requests_share_one_job(rig):
    add(rig)
    service = rig[0].state.briefings
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.queue_manual(str(uuid.uuid4()), '2026-10-19'), range(2)))
    assert results[0][0]['id'] == results[1][0]['id'] and sum(created for _, created in results) == 1
    service.tick()
    assert len(rig[3].calls) == 1


def test_restart_cancels_unfinished_scheduled_work_and_never_reclaims(rig):
    add(rig); enable(rig); rig[2].advance(10)
    service = rig[0].state.briefings
    service.claim_schedule()
    assert state(rig)['reports'][0]['status'] == 'pending'
    service.initialize(); service.tick(); rig[0].state.notifications.process_pending_events()
    assert state(rig)['reports'][0]['status'] == 'interrupted' and state(rig)['schedule']['status'] == 'interrupted'
    assert not rig[3].calls and not rig[4].sent


def test_actual_registered_workers_cross_clock_boundary_and_deliver(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_module, 'load_config', lambda _: CONFIG)
    clock, sender = Clock(), Sender()
    source = DailySource(clock)
    app = create_app(tmp_path / 'registered.sqlite3', clock=clock, daily_source=source, notification_client=sender, run_monitor=True,
                     research_source=EmptyResearchProvider())
    with TestClient(app):
        app.state.store.add(Security('600519', 'sh', '隔离股票', '夹具', clock().isoformat()))
        app.state.notifications.set_enabled(True)
        app.state.briefings.set_settings(True, '08:45', 0)
        clock.advance(11)
        deadline = time.monotonic() + 8
        while not sender.sent and time.monotonic() < deadline: time.sleep(0.05)
        assert len(sender.sent) == 1
        view = app.state.briefings.state()
        assert view['schedule']['status'] == 'completed' and view['reports'][0]['trigger_kind'] == 'scheduled'
        assert view['reports'][0]['notification']['status'] == 'accepted'
        assert view['runtime']['running']
    assert not app.state.briefings.thread.is_alive() and not app.state.anomalies.thread.is_alive()
    assert not app.state.briefings.is_alive()


@pytest.mark.parametrize('when', ['before_check', 'during_verify'])
@pytest.mark.parametrize('rollback_seconds', [86400, 2700])
def test_clock_rollback_cancels_wrong_day_or_early_briefing(rig, when, rollback_seconds):
    add(rig); enable(rig); rig[2].advance(10); rig[0].state.briefings.tick()
    rollback = lambda: rig[2].advance(-rollback_seconds)
    if when == 'before_check': rollback()
    else: rig[4].during_check = rollback
    rig[0].state.notifications.process_pending_events()
    assert not rig[4].sent
    assert state(rig)['reports'][0]['notification']['status'] == 'cancelled'


def test_registered_scheduler_claims_while_manual_data_fetch_is_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_module, 'load_config', lambda _: CONFIG)
    clock, sender = Clock(), Sender()
    source = DailySource(clock)
    entered, release = threading.Event(), threading.Event()
    def hold_fetch():
        entered.set()
        assert release.wait(8), 'test failed to release the isolated source'
    source.during_fetch = hold_fetch
    app = create_app(tmp_path / 'slow-manual.sqlite3', allowed_hosts=['testserver'], clock=clock,
                     daily_source=source, notification_client=sender, run_monitor=True,
                     research_source=EmptyResearchProvider())
    with TestClient(app) as client:
        try:
            app.state.store.add(Security('600519', 'sh', '隔离股票', '夹具', clock().isoformat()))
            app.state.notifications.set_enabled(True)
            app.state.briefings.set_settings(True, '08:45', 0)
            response = client.post('/api/briefings', headers=HEADERS, json={
                'request_id': str(uuid.uuid4()), 'report_date': '2026-10-19', 'retry_of': None})
            report_id = response.json()['record']['id']
            assert entered.wait(3)
            clock.advance(10)
            deadline = time.monotonic() + 3
            while app.state.briefings.state()['schedule']['status'] != 'waiting' and time.monotonic() < deadline:
                time.sleep(0.05)
            waiting = app.state.briefings.state()['schedule']
            assert waiting['status'] == 'waiting' and waiting['report_id'] == report_id
            clock.advance(65)  # Data finishes at 08:46:05, after the claim window.
            release.set()
            deadline = time.monotonic() + 8
            while (not sender.sent or app.state.briefings.state()['reports'][0]['notification']['status'] != 'accepted') and time.monotonic() < deadline:
                time.sleep(0.05)
            view = app.state.briefings.state()
            assert view['schedule']['status'] == 'completed'
            assert view['reports'][0]['id'] == report_id
            assert view['reports'][0]['notification']['status'] == 'accepted'
            assert len(view['reports']) == len(source.calls) == len(sender.sent) == 1
        finally:
            release.set()
    assert not app.state.briefings.is_alive() and not app.state.anomalies.thread.is_alive()


@pytest.mark.parametrize('method,path,body', [
    ('post', '/api/briefings', {'request_id': 'invalid-fixture', 'report_date': '2026-10-19'}),
    ('put', '/api/briefings/settings', {'enabled': 'not-a-boolean', 'send_time': '08:45', 'expected_version': 0}),
])
def test_invalid_briefing_request_has_relevant_message(rig, method, path, body):
    response = getattr(rig[1], method)(path, headers=HEADERS, json=body)
    assert response.status_code == 422
    assert '简报请求内容不正确' in response.json()['error']['message']
    assert not state(rig)['reports']
