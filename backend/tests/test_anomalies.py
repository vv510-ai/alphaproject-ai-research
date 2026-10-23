"""Synthetic market states and fake Feishu only; never open the real runtime vault."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import re
import sqlite3
import time
import uuid

from fastapi.testclient import TestClient
import pytest

from backend.app import notification_service as notification_module
from backend.app.anomaly_rules import Sample, inspect_sample, meets
from backend.app.anomaly_service import AnomalyService
from backend.app.feishu import FeishuFailure, Target
from backend.app.feishu_config import FeishuConfig
from backend.app.main import create_app
from backend.app.notification_service import NotificationService
from backend.app.quote_service import QuoteService
from backend.app.quotes import Quote, CHINA_TIME, parse_quote
from backend.app.live_policy import build_live_evidence
from backend.app.runtime_lock import RuntimeLock
from backend.app.securities import LookupFailure, Security
from backend.app.store import WatchlistStore

NOW = datetime(2026, 10, 19, 2, 0, tzinfo=timezone.utc)
CONFIG = FeishuConfig("cli_anomaly_fixture", "FAKE_ANOMALY_SECRET", "oc_fixture", "ou_fixture", "合成单人群", "合成机器人", NOW.isoformat())
SECURITY = Security("600519", "sh", "合成测试股票", "隔离夹具", NOW.isoformat())
HEADERS = {"X-Alpha-Request": "watchlist-v1", "Origin": "http://testserver"}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds=2):
        self.now += timedelta(seconds=seconds)


class FakeClient:
    def __init__(self):
        self.sent = []
        self.during_check = self.failure = None

    def verify_target(self):
        if self.during_check:
            self.during_check()
        return Target(CONFIG.group_name, CONFIG.bot_name, CONFIG.owner_id)

    def send_test(self, request_id, text, detail_url=None):
        self.sent.append((request_id, text))
        if self.failure:
            raise self.failure
        return "om_fixture"


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_module, "load_config", lambda path: CONFIG)
    store, clock, client = WatchlistStore(tmp_path / "anomalies.sqlite3"), Clock(), FakeClient()
    store.initialize()
    item, _ = store.add(SECURITY)
    quotes = QuoteService(store, clock=clock)
    quotes.initialize()
    clock.store, clock.generation = store, item['created_at']
    notifications = NotificationService(store, tmp_path / "synthetic-vault", client, clock)
    notifications.initialize()
    service = AnomalyService(store, quotes, notifications, clock)
    service.initialize()
    return service, notifications, client, clock, item


def rule(rig, enabled=True, up="3", down="3"):
    service, _, _, _, item = rig
    current = service.state()["items"][0]["rule"]
    return service.save_rule("sh", "600519", item["created_at"], up, down, enabled, current["version"] if current else 0)


def sample(clock, price="103", previous="100", mode="live", **changes):
    if mode == 'demo':
        quote = Quote("sh", "600519", SECURITY.name, price, previous, "3", "3", clock().isoformat(), clock().isoformat(), "合成来源", "", "fixture")
        return Sample(replace(quote, **changes), 'trading', True, True)
    # Controlled rule-layer fixture. The independent live acceptance suite reaches
    # these tables by real QuoteService.refresh calls instead of seeding them.
    def parsed(at, volume):
        difference = Decimal(price) - Decimal(previous)
        percent = (difference / Decimal(previous) * 100).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        fields = [''] * 35
        for index, value in {0:'1', 1:SECURITY.name, 2:'600519', 3:price, 4:previous,
                             6:str(volume), 30:at.astimezone(CHINA_TIME).strftime('%Y%m%d%H%M%S'),
                             31:format(difference, 'f'), 32:format(percent, 'f')}.items():
            fields[index] = value
        return parse_quote('v_sh600519="' + '~'.join(fields) + '";', '600519', at)
    first_at = clock() - timedelta(seconds=30)
    first = build_live_evidence(parsed(first_at, 100), None, clock.generation, first_at)
    quote = parsed(clock(), 101)
    evidence = build_live_evidence(quote, first, clock.generation, clock())
    with clock.store.connect() as db, db:
        db.execute("""INSERT INTO quote_cache (market,symbol,watchlist_created_at,payload,last_attempt_at,last_status,attempt_token)
            VALUES ('sh','600519',?,?,?,'succeeded','rule-fixture') ON CONFLICT(market,symbol) DO UPDATE SET
            watchlist_created_at=excluded.watchlist_created_at,payload=excluded.payload,last_attempt_at=excluded.last_attempt_at,
            last_status='succeeded',error_code=NULL,error_message=NULL""",
                   (clock.generation, json.dumps(quote.to_dict()), clock().isoformat()))
        db.execute("""INSERT INTO quote_live_evidence VALUES ('sh','600519',?,?) ON CONFLICT(market,symbol)
            DO UPDATE SET watchlist_created_at=excluded.watchlist_created_at,payload=excluded.payload""",
                   (clock.generation, json.dumps(evidence)))
    return Sample(replace(quote, **changes), 'trading', True, False, evidence['eligibility'])


def event(rig, mode="live", notify=True):
    service, _, _, clock, _ = rig
    saved = rule(rig)
    result = service.evaluate(saved, sample(clock, mode=mode), mode=mode, notify=notify)
    assert result[0]["status"] == "triggered"
    return result[0]["event_id"]


def test_deleted_watchlist_cancels_pending_event_notification(rig):
    service, notifications, client, _, _ = rig
    notifications.set_enabled(True)
    event_id = event(rig)
    service.store.remove("sh", "600519")
    notifications.process_pending_events()
    assert not client.sent
    assert notifications.state()["records"][0]["status"] == "cancelled"
    assert service.state()["events"][0]["id"] == event_id


def test_live_pending_event_expires_with_source_evidence_not_demo_window(rig):
    _, notifications, client, clock, _ = rig
    notifications.set_enabled(True)
    event(rig)
    clock.advance(121)
    assert notifications.state()["records"][0]["status"] == "expired"
    notifications.process_pending_events()
    assert not client.sent


@pytest.mark.parametrize("prices,direction,expected", [
    (("102.99", "103", "103.20", "99", "103"), "up", ["below", "triggered", "duplicate", "below", "duplicate"]),
    (("97.01", "97", "96.80", "101", "97"), "down", ["below", "triggered", "duplicate", "below", "duplicate"]),
])
def test_boundary_recross_and_replay_are_deduplicated(rig, prices, direction, expected):
    service, _, _, clock, _ = rig
    saved = rule(rig)
    observed = []
    for price in prices:
        observed.append(service.evaluate(saved, sample(clock, price), directions=(direction,))[0]["status"])
        clock.advance()
    assert observed == expected
    stored = service.state()["events"][0]
    assert stored["snapshot"]["quote"]["price"] == prices[1]
    assert stored["snapshot"]["first_observation"] is False
    service.initialize()
    assert service.evaluate(saved, sample(clock, prices[1]), directions=(direction,))[0]["status"] == "duplicate"
    assert len(service.state()["events"]) == 1


def test_next_day_first_observation_and_rule_edits_do_not_rewrite_events(rig):
    service, _, _, clock, _ = rig
    saved = rule(rig)
    service.evaluate(saved, sample(clock))
    original = service.state()["events"][0]["snapshot"]
    updated = rule(rig, up="2")
    assert service.evaluate(updated, sample(clock))[0]["status"] == "duplicate"
    assert service.state()["events"][0]["snapshot"] == original
    clock.advance(86400)
    assert service.evaluate(updated, sample(clock))[0]["status"] == "triggered"
    latest = service.state()["events"][0]
    assert latest["snapshot"]["first_observation"] is True
    assert latest["snapshot"]["rule_version"] == updated["version"]
    assert len(service.state()["events"]) == 2


@pytest.mark.parametrize("value", ["", "0", "-3", "NaN", "Infinity", "1e2", "2.12345", "10000", "3%", 3])
def test_bad_threshold_does_not_overwrite_saved_rule(rig, value):
    service, _, _, _, item = rig
    before = rule(rig)
    with pytest.raises(LookupFailure):
        service.save_rule("sh", "600519", item["created_at"], value, "3", True, before["version"])
    assert service.state()["items"][0]["rule"] == before


@pytest.mark.parametrize("changes,reason", [
    ({"quote": None}, "尚无"),
    ({"previous_close": "0"}, "大于零"), ({"previous_close": ""}, "无效"),
    ({"price": "NaN"}, "无效"), ({"symbol": "600000"}, "身份"),
    ({"market_state": "unknown"}, "尚未核验"), ({"market_state": "halted"}, "停牌"),
    ({"market_state": "closed"}, "非交易"), ({"realtime_verified": False}, "尚未核验"),
    ({"source_reported_at": ""}, "无法核对"),
    ({"source_reported_at": "2026-10-19T02:00:00"}, "时区"),
    ({"source_reported_at": "2026-10-19T02:00:01+00:00"}, "未来"),
    ({"received_at": "2026-10-19T02:00:01+00:00"}, "未来"),
    ({"source_reported_at": "2026-10-18T02:00:00+00:00"}, "日期"),
    ({"source_reported_at": "2026-10-19T01:57:59+00:00"}, "时效"),
    ({"synthetic": True}, "类别"),
])
def test_quality_gate_prevents_event_and_notification(rig, changes, reason):
    service, notifications, _, clock, _ = rig
    current = sample(clock)
    top = {k: v for k, v in changes.items() if k in ("quote", "market_state", "realtime_verified", "synthetic")}
    data = {k: v for k, v in changes.items() if k not in top}
    if data:
        current = replace(current, quote=replace(current.quote, **data))
    current = replace(current, **top)
    result = service.evaluate(rule(rig), current)
    assert result[0]["status"] == "blocked" and reason in result[0]["reason"]
    assert not service.state()["events"] and not notifications.state()["records"]


def test_unrounded_math_freshness_boundary_and_time_regression(rig):
    service, _, _, clock, _ = rig
    saved = rule(rig)
    assert service.evaluate(saved, sample(clock, "102.99999999"))[0]["status"] == "below"
    prior = (clock() - timedelta(seconds=1)).isoformat()
    assert "倒退" in service.evaluate(saved, sample(clock, source_reported_at=prior))[0]["reason"]
    fresh = sample(clock)
    clock.advance(90)  # The earlier source sample is exactly 120 seconds old.
    assert inspect_sample(fresh, "sh", "600519", clock())[0] is None
    clock.advance(1)
    assert "过" in inspect_sample(fresh, "sh", "600519", clock())[0]
    assert meets(Decimal("-3"), "down", "3")


def test_same_event_and_outbox_commit_once_under_concurrency(rig):
    service, notifications, client, clock, _ = rig
    notifications.set_enabled(True)
    saved = rule(rig)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.evaluate(saved, sample(clock))[0]["status"], range(8)))
    assert results.count("triggered") == 1 and results.count("duplicate") == 7
    assert len(service.state()["events"]) == len(notifications.state()["records"]) == 1
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: notifications.process_pending_events(), range(3)))
    assert len(client.sent) == 1


def test_outbox_failure_rolls_back_event_and_observation(rig):
    service, _, _, clock, _ = rig
    saved = rule(rig)
    with service.store.connect() as db:
        db.execute("CREATE TRIGGER fixture_no_outbox BEFORE INSERT ON notification_messages BEGIN SELECT RAISE(ABORT,'fixture'); END")
    with pytest.raises(sqlite3.Error):
        service.evaluate(saved, sample(clock))
    assert not service.state()["events"]
    with service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM anomaly_observations").fetchone()[0] == 0


def test_disabled_removed_or_replaced_rules_cannot_create_new_events(rig):
    service, _, _, clock, item = rig
    old = rule(rig)
    rule(rig, enabled=False)
    assert service.evaluate(old, sample(clock))[0]["status"] == "cancelled"
    disabled = service.state()["items"][0]["rule"]
    assert service.evaluate(disabled, sample(clock))[0]["status"] == "cancelled"
    service.store.remove("sh", "600519")
    service.store.add(SECURITY)
    assert service.evaluate(old, sample(clock))[0]["status"] == "cancelled"
    with pytest.raises(LookupFailure):
        service.save_rule("sh", "600519", item["created_at"], "3", "3", True, old["version"])
    assert not service.state()["events"]


def test_notification_off_retains_event_and_does_not_resend_after_reenable(rig):
    service, notifications, client, _, _ = rig
    event(rig)
    assert notifications.state()["records"][0]["status"] == "cancelled"
    notifications.set_enabled(True)
    notifications.process_pending_events()
    assert len(service.state()["events"]) == 1 and not client.sent


@pytest.mark.parametrize("during_check", [False, True])
def test_expiry_before_handoff_prevents_delivery(rig, during_check):
    _, notifications, client, clock, _ = rig
    notifications.set_enabled(True)
    event(rig)
    if during_check:
        client.during_check = lambda: clock.advance(301)
    else:
        clock.advance(301)
    notifications.process_pending_events()
    assert notifications.state()["records"][0]["status"] == "expired"
    assert not client.sent


def test_deletion_during_target_check_also_prevents_handoff(rig):
    service, notifications, client, _, _ = rig
    notifications.set_enabled(True)
    event(rig)
    client.during_check = lambda: service.store.remove("sh", "600519")
    notifications.process_pending_events()
    assert not client.sent
    assert notifications.state()["records"][0]["status"] == "cancelled"


def test_rule_disabled_before_handoff_cancels_live_notification(rig):
    _, notifications, client, _, _ = rig
    notifications.set_enabled(True)
    event(rig)
    client.during_check = lambda: rule(rig, enabled=False)
    notifications.process_pending_events()
    assert not client.sent
    assert notifications.state()["records"][0]["status"] == "cancelled"


def test_unknown_event_send_is_never_replayed_and_receipt_is_private(rig):
    service, notifications, client, _, _ = rig
    notifications.set_enabled(True)
    event_id = event(rig, mode="demo")
    client.failure = FeishuFailure("synthetic uncertainty", unknown=True)
    notifications.process_pending_events()
    record = notifications.state()["records"][0]
    assert record["status"] == "unknown" and not record["confirmed_at"]
    assert len(client.sent) == 1 and event_id in client.sent[0][1] and "合成演练" in client.sent[0][1]
    code = re.search(r"收件核验码：(\d{6})", client.sent[0][1])[1]
    public = json.dumps({"events": service.state(), "notifications": notifications.state()}, ensure_ascii=False)
    assert code not in public and "receipt_hash" not in public and CONFIG.app_secret not in public
    notifications.initialize()
    notifications.process_pending_events()
    assert len(client.sent) == 1
    assert notifications.confirm(record["id"], code)["confirmed_at"]


def test_demo_is_durable_three_steps_and_does_not_change_real_quote_cache(rig):
    service, notifications, client, clock, item = rig
    notifications.set_enabled(True)
    saved = rule(rig, enabled=False)
    request_id = str(uuid.uuid4())
    run = service.queue_demo(request_id, "sh", "600519", item["created_at"], saved["version"], "up", True)
    for _ in range(3):
        clock.advance()
        service.advance_demo()
    finished = service.state()["runs"][0]
    assert finished["id"] == run["id"] and finished["status"] == "completed"
    assert [x["status"] for x in finished["results"]] == ["below", "triggered", "duplicate"]
    notifications.process_pending_events()
    assert len(client.sent) == 1
    with service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM quote_cache").fetchone()[0] == 0
        assert db.execute("SELECT enabled FROM anomaly_rules").fetchone()[0] == 0
    assert service.queue_demo(request_id, "sh", "600519", item["created_at"], saved["version"], "up", True)["status"] == "completed"
    assert len(service.state()["events"]) == 1
    # A real eligible sample uses its own daily key; synthetic evidence never claims DATA-01.
    live_rule = rule(rig, enabled=True)
    assert service.evaluate(live_rule, sample(clock))[0]["status"] == "triggered"
    assert service.state()["live_ready"] is True


def test_demo_changes_and_restart_cancel_remaining_steps(rig):
    service, _, _, clock, item = rig
    saved = rule(rig, enabled=False)
    service.queue_demo(str(uuid.uuid4()), "sh", "600519", item["created_at"], saved["version"], "up", False)
    rule(rig, up="4")
    clock.advance()
    service.advance_demo()
    assert service.state()["runs"][0]["status"] == "cancelled"
    current = service.state()["items"][0]["rule"]
    service.queue_demo(str(uuid.uuid4()), "sh", "600519", item["created_at"], current["version"], "down", False)
    service.initialize()
    assert all(r["status"] == "cancelled" for r in service.state()["runs"])


def test_os_lock_blocks_second_owner_until_closed(tmp_path):
    first, second = RuntimeLock(tmp_path / "worker.lock"), RuntimeLock(tmp_path / "worker.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="第二份实例"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


def test_rule_change_at_last_demo_step_is_cancelled_not_completed(rig, monkeypatch):
    service, _, _, clock, item = rig
    saved = rule(rig, enabled=False)
    service.queue_demo(str(uuid.uuid4()), "sh", "600519", item["created_at"], saved["version"], "up", False)
    for _ in range(2):
        clock.advance()
        service.advance_demo()
    evaluate = service.evaluate
    def changed_during_step(*args, **kwargs):
        rule(rig, enabled=False, up="4")
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(service, "evaluate", changed_during_step)
    clock.advance()
    service.advance_demo()
    final = service.state()["runs"][0]
    assert final["status"] == "cancelled" and final["results"][-1]["status"] == "cancelled"
    assert len(service.state()["events"]) == 1


def test_api_does_not_accept_client_fabricated_market_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(notification_module, "load_config", lambda path: None)
    app = create_app(tmp_path / "api.sqlite3", allowed_hosts=["testserver"], run_monitor=False)
    with TestClient(app) as client:
        assert client.post('/api/anomalies/demo', json={}, headers={}).status_code == 403
        response = client.post('/api/anomalies/demo', json={"market_state": "trading", "price": "103", "mode": "live"}, headers=HEADERS)
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "invalid_request"
        assert not client.get('/api/anomalies').json()["live_ready"]
        assert not app.state.anomalies.state()["events"]


def test_background_demo_completes_without_browser_polling(rig):
    service, _, _, clock, item = rig
    saved = rule(rig, enabled=False)
    service.queue_demo(str(uuid.uuid4()), "sh", "600519", item["created_at"], saved["version"], "up", False)
    # A running worker owns scheduling. The test advances its clock, never calls
    # advance_demo or the read API to cause work, and observes storage only at the end.
    service.start()
    try:
        for _ in range(3):
            clock.advance(3)
            time.sleep(1.2)
    finally:
        service.stop()
    with service.store.connect() as db:
        assert db.execute("SELECT status FROM anomaly_runs").fetchone()[0] == "completed"
        assert db.execute("SELECT count(*) FROM anomaly_events").fetchone()[0] == 1
        assert db.execute("SELECT checks FROM anomaly_runtime").fetchone()[0] >= 3
