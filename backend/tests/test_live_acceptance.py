"""Independent live-monitor acceptance using controlled raw feeds and fake Feishu.

No natural intraday observation is asserted by this suite. Prices, cumulative
volume, dates, identities and provider responses are deliberately synthetic.
The production parser, cache, eligibility, event transaction and delivery code
remain in the path; only external I/O and the clock are substituted.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from threading import Event, Lock
import time
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import httpx
import pytest

from backend.app import notification_service as notification_module
from backend.app.anomaly_service import AnomalyService
from backend.app.feishu import FeishuClient
from backend.app.feishu_config import FeishuConfig
from backend.app.main import create_app
from backend.app.notification_service import NotificationService
from backend.app.quote_service import QuoteService
from backend.app.quotes import CHINA_TIME, parse_quote
from backend.app.securities import LookupFailure, Security, market_for
from backend.app.store import WatchlistStore


SYMBOL = "600519"
CONFIG = FeishuConfig(
    "cli_liveacceptance", "FAKE_LIVE_ACCEPTANCE_SECRET", "oc_liveacceptance",
    "ou_liveacceptance", "隔离验收假群", "隔离验收假机器人", "2026-10-22T02:00:00+00:00",
)
DETAIL_BASE = "https://example.invalid/lab"


class Clock:
    def __init__(self, value="2026-10-22T10:00:00+08:00"):
        self.set(value)

    def set(self, value):
        self.now = datetime.fromisoformat(value).astimezone(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds=60):
        self.now += timedelta(seconds=seconds)


def raw_quote(symbol, at, *, price="103.00", volume="1000"):
    """A controlled Tencent-shaped response, including the original field 6."""
    previous = Decimal("100.00")
    change = Decimal(price) - previous
    percent = (change / previous * 100).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    fields = [""] * 35
    for index, value in {
        0: "1", 1: "合成验收股票", 2: symbol, 3: price, 4: str(previous),
        6: volume, 30: at.astimezone(CHINA_TIME).strftime("%Y%m%d%H%M%S"),
        31: str(change), 32: str(percent),
    }.items():
        fields[index] = value
    return f'v_{market_for(symbol)}{symbol}="' + "~".join(fields) + '";'


class RawFeed:
    def __init__(self, clock):
        self.clock = clock
        self.raw = {}
        self.failure = None
        self.received_at = None
        self.during_fetch = None
        self.calls = []
        self.active = self.peak_active = 0
        self.lock = Lock()

    def publish(self, symbol=SYMBOL, *, price="103.00", volume="1000", at=None):
        self.raw[symbol] = raw_quote(symbol, at or self.clock(), price=price, volume=volume)

    def fetch(self, symbol):
        with self.lock:
            self.calls.append(symbol)
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            body = self.raw[symbol]
        try:
            if self.during_fetch:
                self.during_fetch(symbol)
            if self.failure:
                raise self.failure
            return parse_quote(body, symbol, self.received_at or self.clock())
        finally:
            with self.lock:
                self.active -= 1


class FakeFeishuWire:
    """Exercise the real provider client, including target checks and POST body."""
    def __init__(self):
        self.requests = []
        self.messages = []
        self.during_target_check = None
        self.client = FeishuClient(CONFIG, transport=httpx.MockTransport(self.handle))

    def handle(self, request):
        path = request.url.path
        self.requests.append((request.method, path))
        if path.endswith("/tenant_access_token/internal"):
            body = {"code": 0, "tenant_access_token": "FAKE_LOCAL_TOKEN", "expire": 7200}
        elif path.endswith("/members"):
            body = {"code": 0, "data": {"has_more": False, "member_total": 1,
                    "items": [{"member_id": CONFIG.owner_id}]}}
        elif path.endswith("/bot/v3/info"):
            body = {"code": 0, "bot": {"activate_status": 2, "app_name": CONFIG.bot_name}}
        elif path.endswith("/messages"):
            self.messages.append(json.loads(request.content))
            body = {"code": 0, "data": {"message_id": "om_liveacceptance"}}
        elif path.endswith("/chats/" + CONFIG.chat_id):
            if self.during_target_check:
                action, self.during_target_check = self.during_target_check, None
                action()
            body = {"code": 0, "data": {"chat_type": "private", "chat_mode": "group",
                    "user_count": "1", "bot_count": "1", "owner_id": CONFIG.owner_id,
                    "name": CONFIG.group_name}}
        else:
            raise AssertionError("Unexpected fake-provider path: " + path)
        return httpx.Response(200, json=body)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("ALPHA_LAB_PUBLIC_URL", raising=False)
    monkeypatch.setattr(notification_module, "load_config", lambda _: CONFIG)

    def blocked(*args, **kwargs):
        raise AssertionError("Real HTTP is forbidden in live acceptance")

    async def blocked_async(*args, **kwargs):
        raise AssertionError("Real async HTTP is forbidden in live acceptance")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked_async)


def security(clock, symbol=SYMBOL):
    return Security(symbol, market_for(symbol), "合成验收股票", "独立验收夹具", clock().isoformat())


def reopen(store, clock, feed, wire):
    quotes = QuoteService(store, feed, clock)
    quotes.initialize()
    notifications = NotificationService(store, store.path.parent / "absent-test-vault", wire.client, clock, DETAIL_BASE)
    notifications.initialize()
    anomalies = AnomalyService(store, quotes, notifications, clock, "/lab")
    anomalies.initialize()
    return SimpleNamespace(store=store, clock=clock, feed=feed, wire=wire,
                           quotes=quotes, notifications=notifications, anomalies=anomalies)


@pytest.fixture
def rig(tmp_path):
    store, clock = WatchlistStore(tmp_path / "live-acceptance.sqlite3"), Clock()
    store.initialize()
    store.add(security(clock))
    result = reopen(store, clock, RawFeed(clock), FakeFeishuWire())
    save_rule(result)
    result.notifications.set_enabled(True)
    return result


def save_rule(rig, *, symbol=SYMBOL, up="3", down="3", enabled=True):
    market = market_for(symbol)
    item = rig.store.get(market, symbol)
    with rig.store.connect() as db:
        prior = db.execute("SELECT version FROM anomaly_rules WHERE market=? AND symbol=?", (market, symbol)).fetchone()
    return rig.anomalies.save_rule(market, symbol, item["created_at"], up, down, enabled, prior[0] if prior else 0)


def refresh(rig, *, symbol=SYMBOL, price="103.00", volume="1000", at=None):
    rig.feed.publish(symbol, price=price, volume=volume, at=at)
    return rig.quotes.refresh(market_for(symbol), symbol)["quote"]


def pair(rig, *, first_price="102.00", second_price="103.00", symbol=SYMBOL):
    first = refresh(rig, symbol=symbol, price=first_price, volume="1000")
    rig.clock.advance()
    second = refresh(rig, symbol=symbol, price=second_price, volume="1100")
    return first, second


def quote_view(rig, symbol=SYMBOL):
    return next(item["quote"] for item in rig.quotes.dashboard()["items"] if item["symbol"] == symbol)


def events(rig):
    return rig.anomalies.state()["events"]


def queued_live_event(rig):
    pair(rig)
    assert quote_view(rig)["eligibility"]["eligible"] is True
    rig.anomalies.check_live()
    saved = events(rig)
    assert len(saved) == 1 and saved[0]["mode"] == "live"
    assert saved[0]["notification"]["status"] == "pending"
    return saved[0]


def test_raw_feed_to_live_event_and_provider_acceptance_once(rig):
    first = refresh(rig, price="103.00", volume="1000")
    assert first["eligibility"]["eligible"] is False  # Price alone cannot qualify.
    rig.anomalies.check_live()
    assert not events(rig) and not rig.notifications.state()["records"]

    rig.clock.advance()
    second = refresh(rig, price="103.00", volume="1100")
    assert second["eligibility"]["eligible"] is True
    assert second["market_state"] == "trading" and second["realtime_verified"] is True
    rig.anomalies.check_live()
    saved = events(rig)
    assert len(saved) == 1 and saved[0]["mode"] == "live"
    event = saved[0]
    assert event["snapshot"]["quote"]["price"] == "103.00"
    assert event["snapshot"]["quote"]["source_reported_at"] == second["data"]["source_reported_at"]
    assert Decimal(event["snapshot"]["change_pct"]) == 3
    assert event["snapshot"]["rule_version"] == 1

    rig.notifications.process_pending_events()
    rig.anomalies.check_live()
    rig.notifications.process_pending_events()
    assert len(events(rig)) == len(rig.notifications.state()["records"]) == len(rig.wire.messages) == 1
    record = rig.notifications.state()["records"][0]
    request = rig.wire.messages[0]
    assert record["status"] == "accepted" and record["confirmed_at"] is None
    assert request["uuid"] == record["id"] and request["receive_id"] == CONFIG.chat_id
    assert request["msg_type"] == "post"
    content = json.loads(request["content"])["zh_cn"]["content"]
    assert event["id"] in content[0][0]["text"]
    assert content[1][0]["href"] == DETAIL_BASE + "/#event=" + event["id"]
    assert "合成演练" not in content[0][0]["text"]  # This is the controlled live-code path.
    for _ in range(3):
        rig.quotes.dashboard()
    assert rig.feed.calls == [SYMBOL, SYMBOL]  # Reads do not cause upstream requests.


@pytest.mark.parametrize("fault", [
    "same_source_time", "source_regression", "future_source", "future_receipt",
    "no_volume", "invalid_volume", "no_activity", "volume_regression",
    "historical", "stale_source", "fetch_failure",
])
def test_invalid_second_observation_never_promotes_displayed_price_to_live(rig, fault):
    refresh(rig, price="103.00", volume="1000")
    first_at = rig.clock()
    rig.clock.advance()
    at, volume = rig.clock(), "1100"
    if fault == "same_source_time":
        at = first_at
    elif fault == "source_regression":
        at = first_at - timedelta(seconds=1)
    elif fault == "future_source":
        at += timedelta(seconds=1)
    elif fault == "future_receipt":
        rig.feed.received_at = rig.clock() + timedelta(seconds=1)
    elif fault == "no_volume":
        volume = ""
    elif fault == "invalid_volume":
        volume = "NaN"
    elif fault == "no_activity":
        volume = "1000"
    elif fault == "volume_regression":
        volume = "900"
    elif fault == "historical":
        at -= timedelta(days=1)
    elif fault == "stale_source":
        at -= timedelta(seconds=121)
    elif fault == "fetch_failure":
        rig.feed.failure = LookupFailure("offline_fixture", "受控断网", 503)
    refresh(rig, volume=volume, at=at)
    view = quote_view(rig)
    assert view["eligibility"]["eligible"] is False
    assert view["eligibility"]["reason"] and view["realtime_verified"] is False
    rig.anomalies.check_live()
    assert not events(rig) and not rig.notifications.state()["records"]
    assert not rig.wire.messages


def test_eligibility_expires_without_another_fetch_or_any_page_mutation(rig):
    pair(rig)
    assert quote_view(rig)["eligibility"]["eligible"] is True
    # The current quote is only 61 seconds old; the earlier proof sample is 121.
    rig.clock.advance(61)
    assert quote_view(rig)["eligibility"]["eligible"] is False
    rig.anomalies.check_live()
    assert not events(rig) and not rig.notifications.state()["records"]
    assert len(rig.feed.calls) == 2


@pytest.mark.parametrize("second_at", [
    "2026-10-22T09:25:00+08:00", "2026-10-22T09:29:00+08:00",
    "2026-10-22T11:30:00+08:00", "2026-10-22T12:15:00+08:00",
    "2026-10-22T14:57:00+08:00", "2026-10-22T15:00:00+08:00",
    "2026-10-01T10:01:00+08:00", "2026-10-24T10:01:00+08:00",
    "2027-01-04T10:01:00+08:00",
])
def test_excluded_sessions_and_unverified_year_cannot_emit_live_events(rig, second_at):
    rig.clock.set(second_at)
    rig.clock.advance(-60)
    pair(rig, first_price="103.00")
    assert quote_view(rig)["eligibility"]["eligible"] is False
    rig.anomalies.check_live()
    assert not events(rig) and not rig.notifications.state()["records"]


def test_afternoon_must_observe_two_samples_in_its_own_session(rig):
    pair(rig)
    assert quote_view(rig)["eligibility"]["eligible"] is True
    rig.clock.set("2026-10-22T13:00:00+08:00")
    first_afternoon = refresh(rig, volume="2000")
    assert first_afternoon["eligibility"]["eligible"] is False
    rig.anomalies.check_live()
    assert not events(rig)
    rig.clock.advance()
    assert refresh(rig, volume="2100")["eligibility"]["eligible"] is True
    rig.anomalies.check_live()
    assert len(events(rig)) == 1


@pytest.mark.parametrize("during_verification", [False, True])
@pytest.mark.parametrize("change", [
    "rule_version", "disabled", "delete_readd", "expired", "lunch", "fetch_failure", "activity_stopped",
])
def test_pending_event_is_rechecked_before_provider_handoff(rig, change, during_verification):
    if change == "lunch":
        rig.clock.set("2026-10-22T11:28:00+08:00")
    event = queued_live_event(rig)
    snapshot = event["snapshot"]

    def mutate():
        if change == "rule_version":
            save_rule(rig, up="4", enabled=True)
        elif change == "disabled":
            save_rule(rig, enabled=False)
        elif change == "delete_readd":
            rig.store.remove("sh", SYMBOL)
            rig.store.add(security(rig.clock))
            save_rule(rig)
        elif change == "expired":
            rig.clock.advance(61)
        elif change == "lunch":
            rig.clock.set("2026-10-22T11:30:00+08:00")
        elif change in ("fetch_failure", "activity_stopped"):
            rig.clock.advance(31)
            if change == "fetch_failure":
                rig.feed.failure = LookupFailure("offline_fixture", "发送核验时来源失败", 503)
            refresh(rig, volume="1100")

    if during_verification:
        rig.wire.during_target_check = mutate
    else:
        mutate()
    rig.notifications.process_pending_events()
    rig.notifications.process_pending_events()
    assert not rig.wire.messages
    assert rig.notifications.state()["records"][0]["status"] in ("cancelled", "expired")
    assert events(rig)[0]["snapshot"] == snapshot  # Cancellation keeps the original evidence.


def test_collection_gap_must_warm_up_again_before_activity_can_qualify(rig):
    refresh(rig, volume="1000")
    rig.clock.advance(121)
    assert refresh(rig, volume="1100")["eligibility"]["eligible"] is False
    rig.anomalies.check_live()
    assert not events(rig)
    rig.clock.advance()
    assert refresh(rig, volume="1200")["eligibility"]["eligible"] is True
    rig.anomalies.check_live()
    assert len(events(rig)) == 1


def test_cache_replaced_after_dashboard_read_cannot_commit_the_earlier_proof(rig, monkeypatch):
    pair(rig)
    original_dashboard = rig.quotes.dashboard
    changed = False

    def interleaved_read():
        nonlocal changed
        captured = original_dashboard()
        if not changed:
            changed = True
            rig.clock.advance(31)
            refresh(rig, price="104.00", volume="1200")
        return captured

    # Inject only the interleaving: both reads and the competing refresh still
    # execute the real implementations against the same SQLite database.
    with monkeypatch.context() as context:
        context.setattr(rig.quotes, "dashboard", interleaved_read)
        rig.anomalies.check_live()
    assert not events(rig) and not rig.notifications.state()["records"]
    rig.anomalies.check_live()
    assert len(events(rig)) == 1
    assert events(rig)[0]["snapshot"]["quote"]["price"] == "104.00"


def test_delete_readd_needs_new_activity_and_keeps_the_daily_deduplication_key(rig):
    original = queued_live_event(rig)
    original_generation = rig.store.get("sh", SYMBOL)["created_at"]
    rig.store.remove("sh", SYMBOL)
    item, _ = rig.store.add(security(rig.clock))
    assert item["created_at"] != original_generation
    save_rule(rig)
    rig.clock.advance()
    assert refresh(rig, volume="3000")["eligibility"]["eligible"] is False
    rig.anomalies.check_live()
    rig.clock.advance()
    assert refresh(rig, volume="3100")["eligibility"]["eligible"] is True
    rig.anomalies.check_live()
    rig.notifications.process_pending_events()
    assert [event["id"] for event in events(rig)] == [original["id"]]
    assert len(rig.notifications.state()["records"]) == 1 and not rig.wire.messages


@pytest.mark.parametrize("already_accepted", [False, True])
def test_restart_preserves_events_without_replaying_pending_or_accepted_messages(rig, already_accepted):
    original = queued_live_event(rig)
    if already_accepted:
        rig.notifications.process_pending_events()
    sent_before = len(rig.wire.messages)
    restored = reopen(rig.store, rig.clock, rig.feed, rig.wire)
    restored.anomalies.check_live()
    restored.notifications.process_pending_events()
    assert [event["id"] for event in events(restored)] == [original["id"]]
    assert len(rig.wire.messages) == sent_before
    assert restored.notifications.state()["records"][0]["status"] == ("accepted" if already_accepted else "cancelled")


def collector_for(rig):
    from backend.app.quote_collector import QuoteCollector
    collector = QuoteCollector(rig.store, rig.quotes, rig.clock)
    rig.quotes.collector = collector
    return collector


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.02)
    return False


def test_collector_limits_parallel_work_and_rotates_beyond_twenty_stocks(rig):
    symbols = [SYMBOL, *(f"600{index:03d}" for index in range(1, 25))]
    for symbol in symbols:
        rig.store.add(security(rig.clock, symbol))
        rig.feed.publish(symbol)
    collector = collector_for(rig)
    entered, release = Event(), Event()

    def block(_):
        if rig.feed.active >= 4:
            entered.set()
        assert release.wait(5), "Fixture gate did not release"

    rig.feed.during_fetch = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        cycle = pool.submit(collector.tick)
        try:
            assert entered.wait(5)
            assert len(rig.feed.calls) == 4  # Remaining stocks cannot enter I/O yet.
            collector.tick()  # A concurrent scheduler invocation must not add a second round.
            assert len(rig.feed.calls) == 4
        finally:
            release.set()
        cycle.result(timeout=5)
    assert len(rig.feed.calls) == len(set(rig.feed.calls)) == 20
    assert rig.feed.peak_active == 4
    rig.feed.during_fetch = None
    rig.clock.advance()
    for symbol in symbols:
        rig.feed.publish(symbol, volume="1200")
    collector.tick()
    assert len(rig.feed.calls) <= 40
    assert set(rig.feed.calls) == set(symbols)  # Five initially deferred stocks get a turn.


@pytest.mark.parametrize("background_first", [False, True])
def test_manual_and_background_share_inflight_ownership_even_after_click_cooldown(rig, background_first):
    rig.feed.publish()
    collector = collector_for(rig)
    entered, release = Event(), Event()

    def block(_):
        entered.set()
        assert release.wait(5), "Fixture gate did not release"

    rig.feed.during_fetch = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(collector.tick if background_first else lambda: rig.quotes.refresh("sh", SYMBOL))
        try:
            assert entered.wait(5)
            rig.clock.advance(31)  # Expiring the click cooldown cannot steal a still-active request.
            another_service = QuoteService(rig.store, rig.feed, rig.clock)
            with pytest.raises(LookupFailure) as busy:
                another_service.refresh("sh", SYMBOL)
            assert busy.value.status == 429
            collector.tick()
            assert rig.feed.calls == [SYMBOL]
        finally:
            release.set()
        owner.result(timeout=5)
    assert rig.feed.peak_active == 1 and rig.feed.calls == [SYMBOL]


def test_jobs_waiting_for_a_collection_slot_do_not_fetch_after_lunch_begins(rig):
    rig.clock.set("2026-10-22T11:29:00+08:00")
    symbols = [SYMBOL, *(f"600{index:03d}" for index in range(1, 8))]
    for symbol in symbols:
        rig.store.add(security(rig.clock, symbol))
        rig.feed.publish(symbol)
    collector = collector_for(rig)
    entered, release = Event(), Event()

    def block(_):
        if rig.feed.active >= 4:
            entered.set()
        assert release.wait(5), "Fixture gate did not release"

    rig.feed.during_fetch = block
    with ThreadPoolExecutor(max_workers=1) as pool:
        cycle = pool.submit(collector.tick)
        try:
            assert entered.wait(5)
            rig.clock.set("2026-10-22T11:30:00+08:00")
        finally:
            release.set()
        cycle.result(timeout=5)
    assert len(rig.feed.calls) == 4
    assert collector.state()["state"] == "break"
    assert all(not item["quote"]["eligibility"]["eligible"] for item in rig.quotes.dashboard()["items"])


def app_for(rig, *, run_monitor):
    from backend.tests.test_briefings import EmptyResearchProvider
    return create_app(rig.store.path, allowed_hosts=["testserver"], quote_source=rig.feed,
                      clock=rig.clock, notification_client=rig.wire.client, run_monitor=run_monitor, research_source=EmptyResearchProvider())


def test_second_application_is_refused_before_it_can_cancel_the_first_outbox(rig):
    first, second = app_for(rig, run_monitor=False), app_for(rig, run_monitor=False)
    with TestClient(first):
        pending = queued_live_event(rig)
        with pytest.raises(RuntimeError, match="已有服务运行"):
            with TestClient(second):
                pytest.fail("A second app must never reach its running state")
        assert rig.notifications.state()["records"][0]["status"] == "pending"
        first.state.notifications.process_pending_events()
        assert len(rig.wire.messages) == 1
        assert first.state.anomalies.event(pending["id"])["notification"]["status"] == "accepted"


def test_registered_workers_collect_detect_and_deliver_without_page_reads(rig):
    # Persist only a first sample. Startup must obtain the second sample itself;
    # no request, scheduler tick or anomaly-check helper runs after startup.
    assert refresh(rig, volume="1000")["eligibility"]["eligible"] is False
    rig.clock.advance()
    rig.feed.publish(volume="1100")
    app = app_for(rig, run_monitor=True)

    def accepted():
        with rig.store.connect() as db:
            return db.execute("SELECT count(*) FROM notification_messages WHERE kind='anomaly' AND status='accepted'").fetchone()[0] == 1

    with TestClient(app):
        assert wait_for(accepted, seconds=10), {
            "collector": app.state.collector.state(),
            "events": app.state.anomalies.state()["events"],
            "notifications": app.state.notifications.state()["records"],
        }
        assert app.state.collector.is_alive()
        assert rig.feed.calls == [SYMBOL, SYMBOL]
        assert len(rig.wire.messages) == 1
        assert len(app.state.anomalies.state()["events"]) == 1
        assert app.state.anomalies.state()["events"][0]["mode"] == "live"
    assert not app.state.collector.is_alive()
    assert not app.state.anomalies.thread.is_alive()
    assert not app.state.briefings.is_alive()


def test_blocked_quote_collection_does_not_block_demo_or_manual_briefing_workers(rig):
    from backend.tests.test_briefings import DailySource

    rig.feed.publish()
    entered, release = Event(), Event()

    def block(_):
        entered.set()
        assert release.wait(12), "Fixture gate did not release"

    rig.feed.during_fetch = block
    from backend.tests.test_briefings import EmptyResearchProvider
    app = create_app(rig.store.path, allowed_hosts=["testserver"], clock=rig.clock,
                     quote_source=rig.feed, daily_source=DailySource(rig.clock),
                     notification_client=rig.wire.client, run_monitor=True, research_source=EmptyResearchProvider())
    request_id = str(uuid.uuid4())
    headers = {"X-Alpha-Request": "watchlist-v1", "Origin": "http://testserver"}

    def step_at_least(expected):
        with rig.store.connect() as db:
            row = db.execute("SELECT step FROM anomaly_runs WHERE id=?", (request_id,)).fetchone()
            return bool(row and row[0] >= expected)

    def briefing_finished():
        with rig.store.connect() as db:
            return db.execute("SELECT count(*) FROM briefing_reports WHERE status='succeeded'").fetchone()[0] == 1

    with TestClient(app) as client:
        try:
            assert entered.wait(3)
            response = client.post("/api/anomalies/demo", headers=headers, json={
                "request_id": request_id, "market": "sh", "symbol": SYMBOL,
                "watchlist_created_at": rig.store.get("sh", SYMBOL)["created_at"],
                "expected_version": 1, "direction": "up", "notify": False,
            })
            assert response.status_code == 202
            response = client.post("/api/briefings", headers=headers, json={
                "request_id": str(uuid.uuid4()), "report_date": "2026-10-22", "retry_of": None,
            })
            assert response.status_code == 202
            for step in (1, 2, 3):
                rig.clock.advance(3)
                assert wait_for(lambda: step_at_least(step), seconds=2)
            assert wait_for(briefing_finished, seconds=2)
            assert rig.feed.active == 1  # Both workflows finished while the feed was still blocked.
            assert not rig.wire.messages  # Web-only demo and manual briefing never send.
        finally:
            release.set()


def test_verified_first_trading_day_does_not_need_previous_year_calendar(rig):
    # Jan 5 is itself known in the checked 2026 calendar. Live activity uses the
    # source-provided previous close and does not need the briefing's prior-date lookup.
    rig.clock.set("2026-01-05T10:00:00+08:00")
    pair(rig)
    assert quote_view(rig)["eligibility"]["eligible"] is True
    rig.anomalies.check_live()
    assert len(events(rig)) == 1
