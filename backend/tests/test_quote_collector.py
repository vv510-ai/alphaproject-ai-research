"""Synthetic sources and temporary databases only; no real quote/Feishu/model calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from threading import Event, Lock
import time

import pytest

from backend.app.anomaly_rules import Sample, inspect_sample
from backend.app.anomaly_service import AnomalyService
from backend.app.live_policy import proof_eligibility, session_context
from backend.app.notification_service import NotificationService
from backend.app.quote_collector import QuoteCollector
from backend.app.quote_service import QuoteService
from backend.app.quotes import CHINA_TIME, Quote, parse_quote
from backend.app.securities import LookupFailure, Security
from backend.app.store import WatchlistStore


class Clock:
    def __init__(self, hour=10, minute=0):
        self.now = datetime(2026, 10, 22, hour, minute, tzinfo=CHINA_TIME)

    def __call__(self):
        return self.now

    def advance(self, seconds=60):
        self.now += timedelta(seconds=seconds)


def raw_quote(clock, symbol="600519", volume="100", source_at=None, price="103"):
    fields = [""] * 35
    for index, value in {0: "1", 1: "合成股票", 2: symbol, 3: price, 4: "100", 6: volume,
                         30: (source_at or clock()).astimezone(CHINA_TIME).strftime("%Y%m%d%H%M%S"),
                         31: "3", 32: "3.00"}.items():
        fields[index] = value
    return f'v_sh{symbol}="' + "~".join(fields) + '";'


class Source:
    def __init__(self, clock):
        self.clock = clock
        self.volume = "100"
        self.source_at = None
        self.failure = None
        self.bad_symbol = None
        self.calls = []
        self.gate = None
        self.entered = Event()
        self.four_entered = Event()
        self.lock = Lock()
        self.active = self.max_active = 0

    def fetch(self, symbol):
        with self.lock:
            self.calls.append(symbol)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 4:
                self.four_entered.set()
            self.entered.set()
        try:
            if self.gate:
                assert self.gate.wait(8)
            if self.failure:
                raise self.failure
            if symbol == self.bad_symbol:
                raise RuntimeError("controlled source failure")
            return parse_quote(raw_quote(self.clock, symbol, self.volume, self.source_at), symbol, self.clock())
        finally:
            with self.lock:
                self.active -= 1


def make_rig(tmp_path, count=1, clock=None):
    clock = clock or Clock()
    store = WatchlistStore(tmp_path / "collector.sqlite3")
    store.initialize()
    symbols = ["600519"] if count == 1 else [f"{600000 + index}" for index in range(count)]
    for symbol in symbols:
        store.add(Security(symbol, "sh", "合成股票", "测试夹具", clock().isoformat()))
    source = Source(clock)
    quotes = QuoteService(store, source, clock)
    quotes.initialize()
    collector = QuoteCollector(store, quotes, clock)
    quotes.collector = collector
    return store, clock, source, quotes, collector


def view(quotes, symbol="600519"):
    return next(item["quote"] for item in quotes.dashboard()["items"] if item["symbol"] == symbol)


def eligible_pair(rig):
    store, clock, source, quotes, collector = rig
    collector.tick()
    clock.advance()
    source.volume = "101"
    collector.tick()
    return view(quotes)


@pytest.mark.parametrize("hour,minute,state,session", [
    (9, 14, "closed", None), (9, 15, "auction", None), (9, 29, "auction", None),
    (9, 30, "trading", "am"), (11, 29, "trading", "am"), (11, 30, "break", None),
    (12, 59, "break", None), (13, 0, "trading", "pm"), (14, 56, "trading", "pm"),
    (14, 57, "auction", None), (15, 0, "closed", None),
])
def test_session_windows_are_explicit(hour, minute, state, session):
    context = session_context(Clock(hour, minute)())
    assert context["state"] == state
    assert context["session_id"] == (f"2026-10-22/{session}" if session else None)


def test_unknown_calendar_weekends_and_timezone_fail_closed():
    assert session_context(datetime(2026, 1, 5, 10, tzinfo=CHINA_TIME))["state"] == "trading"
    assert session_context(datetime(2027, 1, 4, 10, tzinfo=CHINA_TIME))["state"] == "unknown"
    assert session_context(datetime(2026, 10, 24, 10, tzinfo=CHINA_TIME))["state"] == "closed"
    assert session_context(datetime(2026, 10, 22, 10))["state"] == "unknown"


def test_background_pair_proves_activity_without_changing_quote_payload(tmp_path):
    rig = make_rig(tmp_path)
    store, clock, source, quotes, collector = rig
    collector.tick()
    assert view(quotes)["eligibility"]["code"] == "warming_up"
    clock.advance()
    source.volume = "101"
    collector.tick()
    current = view(quotes)
    assert current["realtime_verified"] is True
    proof = current["eligibility"]["proof"]
    assert proof["previous"]["cumulative_volume"] == "100"
    assert proof["current"]["cumulative_volume"] == "101"
    assert "非官方停牌" in current["eligibility"]["reason"]
    with store.connect() as db:
        cache = json.loads(db.execute("SELECT payload FROM quote_cache").fetchone()[0])
        saved = db.execute("SELECT payload FROM quote_live_evidence").fetchone()[0]
    assert Quote(**cache).raw_text
    for _ in range(5):
        assert view(quotes)["eligibility"]["proof"] == proof
    with store.connect() as db:
        assert db.execute("SELECT payload FROM quote_live_evidence").fetchone()[0] == saved
    assert len(source.calls) == 2
    clock.advance(61)
    assert view(quotes)["eligibility"]["code"] == "stale"  # Oldest of the two samples expired.
    assert view(quotes)["data"]["source_reported_at"] == current["data"]["source_reported_at"]


@pytest.mark.parametrize("volume,advance_source,reason", [
    ("100", True, "no_recent_trade"), ("99", True, "no_recent_trade"),
    ("", True, "missing_volume"), ("NaN", True, "missing_volume"),
    ("101", False, "source_not_advanced"),
])
def test_missing_activity_never_becomes_eligible(tmp_path, volume, advance_source, reason):
    store, clock, source, quotes, collector = make_rig(tmp_path)
    collector.tick()
    first = clock()
    clock.advance()
    source.volume = volume
    source.source_at = None if advance_source else first
    collector.tick()
    assert view(quotes)["eligibility"]["code"] == reason
    assert not view(quotes)["realtime_verified"]
    assert view(quotes)["market_state"] == "trading"  # Unknown activity is not "halted".


@pytest.mark.parametrize("failure_kind", ["source_error", "future", "regression", "exception"])
def test_failure_clears_proof_preserves_quote_and_requires_new_pair(tmp_path, failure_kind):
    rig = make_rig(tmp_path)
    store, clock, source, quotes, collector = rig
    previous = eligible_pair(rig)["data"]
    clock.advance()
    if failure_kind == "source_error":
        source.failure = LookupFailure("offline", "隔离来源失败", 503)
    elif failure_kind == "future":
        source.source_at = clock() + timedelta(seconds=1)
    elif failure_kind == "regression":
        source.source_at = clock() - timedelta(seconds=61)
    else:
        source.failure = RuntimeError("interrupted source")
    collector.tick()
    assert view(quotes)["data"] == previous
    assert not view(quotes)["eligibility"]["eligible"]
    source.failure = source.source_at = None
    clock.advance()
    source.volume = "102"
    collector.tick()
    assert view(quotes)["eligibility"]["code"] == "warming_up"
    clock.advance()
    source.volume = "103"
    collector.tick()
    assert view(quotes)["eligibility"]["eligible"]


def test_lunch_and_new_session_require_new_evidence_without_offhours_fetch(tmp_path):
    rig = make_rig(tmp_path, clock=Clock(11, 28))
    store, clock, source, quotes, collector = rig
    assert eligible_pair(rig)["eligibility"]["eligible"]
    clock.now = clock().replace(hour=11, minute=30)
    collector.tick()
    assert len(source.calls) == 2
    assert view(quotes)["market_state"] == "break"
    assert not view(quotes)["eligibility"]["eligible"]
    clock.now = clock().replace(hour=13, minute=0)
    source.volume = "102"
    collector.tick()
    assert not view(quotes)["eligibility"]["eligible"]
    clock.advance()
    source.volume = "103"
    collector.tick()
    assert view(quotes)["eligibility"]["proof"]["session_id"] == "2026-10-22/pm"


def test_empty_and_closed_start_never_requests_quotes(tmp_path):
    store, clock, source, quotes, collector = make_rig(tmp_path, clock=Clock(8, 0))
    assert collector.tick()["state"] == "closed"
    assert not source.calls
    store.remove("sh", "600519")
    clock.now = clock().replace(hour=10)
    assert collector.tick()["state"] == "empty"
    assert not source.calls


def test_cycles_are_bounded_and_oldest_first_rotates_more_than_twenty(tmp_path):
    store, clock, source, quotes, collector = make_rig(tmp_path, count=25)
    source.gate = Event()
    with ThreadPoolExecutor(max_workers=1) as caller:
        cycle = caller.submit(collector.tick)
        try:
            assert source.four_entered.wait(3)
            assert len(source.calls) == 4
            collector.tick()  # Overlap is ignored, never a second set of workers.
            assert len(source.calls) == 4
        finally:
            source.gate.set()
        assert cycle.result(timeout=5)["attempted"] == 20
    assert len(source.calls) == 20
    assert source.max_active == 4
    clock.advance()
    source.gate = None
    source.volume = "101"
    collector.tick()
    assert len(source.calls) == 40
    assert len(set(source.calls)) == 25


def test_manual_refresh_and_background_cannot_overlap_even_after_cooldown(tmp_path):
    store, clock, source, quotes, collector = make_rig(tmp_path)
    source.gate = Event()
    with ThreadPoolExecutor(max_workers=1) as caller:
        manual = caller.submit(quotes.refresh, "sh", "600519")
        try:
            assert source.entered.wait(3)
            clock.advance(31)
            collector.tick()
            assert len(source.calls) == 1
            with pytest.raises(LookupFailure) as error:
                QuoteService(store, source, clock).refresh("sh", "600519")
            assert error.value.status == 429
        finally:
            source.gate.set()
        assert manual.result(timeout=3)["refresh_succeeded"]


def test_heartbeat_moves_while_source_waits_and_stop_retains_worker_ownership(tmp_path):
    store, clock, source, quotes, collector = make_rig(tmp_path)
    source.gate = Event()
    collector.start()
    try:
        assert source.entered.wait(3)
        original = collector.state()["heartbeat_at"]
        clock.advance(1)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and collector.state()["heartbeat_at"] == original:
            time.sleep(0.02)
        assert collector.state()["heartbeat_at"] != original
        assert collector.state()["running"]
        collector.stop()
        assert collector.is_alive()  # In-flight network owner cannot be replaced yet.
    finally:
        source.gate.set()
        collector.stop()
        collector.thread.join(timeout=3)
    assert not collector.is_alive()
    assert not collector.state()["running"]


def test_one_source_error_does_not_stop_other_stocks(tmp_path):
    store, clock, source, quotes, collector = make_rig(tmp_path, count=2)
    source.bad_symbol = "600000"
    state = collector.tick()
    assert state["succeeded"] == 1 and state["failed"] == 1
    assert view(quotes, "600001")["data"] is not None
    assert not view(quotes, "600000")["eligibility"]["eligible"]


def test_delete_and_readd_while_fetching_discards_old_generation(tmp_path):
    store, clock, source, quotes, collector = make_rig(tmp_path)
    source.gate = Event()
    with ThreadPoolExecutor(max_workers=1) as caller:
        cycle = caller.submit(collector.tick)
        try:
            assert source.entered.wait(3)
            store.remove("sh", "600519")
            store.add(Security("600519", "sh", "重新添加", "隔离夹具", clock().isoformat()))
        finally:
            source.gate.set()
        cycle.result(timeout=3)
    assert view(quotes)["data"] is None
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM quote_live_evidence").fetchone()[0] == 0
    clock.advance()
    collector.tick()
    assert view(quotes)["eligibility"]["code"] == "warming_up"


def test_raw_and_quote_hash_mismatch_and_bare_bool_cannot_qualify(tmp_path):
    rig = make_rig(tmp_path)
    store, clock, source, quotes, collector = rig
    current = eligible_pair(rig)
    quote = Quote(**current["data"], raw_text="")
    assert inspect_sample(Sample(quote, "trading", True), "sh", "600519", clock())[0]
    altered = dict(current["data"], price="104")
    assert proof_eligibility(altered, current["eligibility"]["proof"], clock())["code"] == "quote_changed"
    altered = dict(current["data"], raw_text="different source response")
    assert proof_eligibility(altered, current["eligibility"]["proof"], clock())["code"] == "quote_changed"


def test_real_pipeline_event_has_committed_proof_and_never_replays(tmp_path):
    rig = make_rig(tmp_path)
    store, clock, source, quotes, collector = rig
    notifications = NotificationService(store, tmp_path / "absent-test-config", clock=clock)
    notifications.initialize()
    anomalies = AnomalyService(store, quotes, notifications, clock)
    anomalies.initialize()
    item = store.get("sh", "600519")
    saved_rule = anomalies.save_rule("sh", "600519", item["created_at"], "3", "3", True, 0)
    collector.tick()
    anomalies.check_live()
    assert not anomalies.state()["events"]
    clock.advance()
    source.volume = "101"
    collector.tick()
    qualified = view(quotes)
    stale_sample = Sample(Quote(**qualified["data"], raw_text=""), "trading", True, False, qualified["eligibility"])
    anomalies.check_live()
    state = anomalies.state()
    assert state["live_ready"] and len(state["events"]) == 1
    event = state["events"][0]
    assert event["snapshot"]["eligibility"]["proof"] == qualified["eligibility"]["proof"]
    assert event["snapshot"]["freshness_limit_seconds"] == 120
    assert event["notification"]["status"] == "cancelled"  # No external config; no send.
    for _ in range(3):
        anomalies.check_live()
    assert len(anomalies.state()["events"]) == 1
    clock.advance(31)
    source.failure = LookupFailure("offline", "受控断网", 503)
    quotes.refresh("sh", "600519")
    # A previously read valid sample cannot get past the changed committed evidence.
    assert anomalies.evaluate(saved_rule, stale_sample)[0]["status"] == "blocked"


def test_initialization_and_offline_read_preserve_existing_runtime_and_evidence(tmp_path):
    rig = make_rig(tmp_path)
    store, clock, source, quotes, collector = rig
    eligible_pair(rig)
    with store.connect() as db:
        before = [tuple(row) for row in db.execute("SELECT * FROM quote_live_evidence")]
        runtime = db.execute("SELECT payload FROM quote_collector_runtime").fetchone()[0]
    reopened = QuoteService(store, source, clock)
    reopened.initialize()
    reopened.collector = QuoteCollector(store, reopened, clock)
    reopened.dashboard()
    with store.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT * FROM quote_live_evidence")] == before
        assert db.execute("SELECT payload FROM quote_collector_runtime").fetchone()[0] == runtime


def test_one_corrupt_cache_time_keeps_dashboard_readable_and_is_repairable(tmp_path):
    store, clock, source, quotes, collector = make_rig(tmp_path, count=2)
    collector.tick()
    with store.connect() as db, db:
        db.execute("UPDATE quote_cache SET last_attempt_at='invalid-time' WHERE symbol='600000'")
    assert view(quotes, "600000")["eligibility"]["code"] == "invalid_attempt_time"
    assert view(quotes, "600001")["data"] is not None
    clock.advance()
    collector.tick()
    assert view(quotes, "600000")["refresh_state"] == "succeeded"
    with store.connect() as db, db:
        db.execute("UPDATE quote_cache SET payload='{bad-json' WHERE symbol='600000'")
    assert view(quotes, "600000")["data"] is None
    assert view(quotes, "600001")["data"] is not None
