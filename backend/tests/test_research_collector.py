"""Collector persistence, rule-generation checks and previous-trading-day gates."""
from datetime import datetime, timedelta, timezone
import json
from threading import Event

import pytest

from backend.app.research_collector import ResearchCollector
from backend.app.securities import Security
from backend.app.store import WatchlistStore

NOW = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = NOW
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class Research:
    def __init__(self):
        self.calls = []
        self.callback = None
        self.values = {}
    def state(self, market=None, symbol=None):
        return self.values.get((market, symbol), {})
    def refresh(self, market=None, symbol=None):
        self.calls.append((market, symbol))
        if self.callback:
            self.callback(market, symbol)
        result = {"snapshot_id": str(len(self.calls)), "refresh_state": "succeeded", "refresh_in_progress": False}
        self.values[(market, symbol)] = result
        return result


class Daily:
    def __init__(self, generation):
        self.calls, self.generation, self.view = [], generation, {}
        self.callback = None
        self.failure = False
    def get(self, market, symbol):
        return self.view
    def refresh(self, market, symbol, generation):
        self.calls.append((market, symbol, generation))
        if self.callback:
            self.callback()
        if self.failure:
            raise RuntimeError("test source failed")
        self.view = {"watchlist_created_at": generation, "data": {
            "market": market, "symbol": symbol, "adjustment": "none", "latest_trade_date": "2026-10-22", "bars": [{"date": "2026-10-22"}]}}
        return {"refresh_succeeded": True, "history": self.view}


@pytest.fixture
def setup(tmp_path):
    store = WatchlistStore(tmp_path / "collector.sqlite3")
    store.initialize()
    item, _ = store.add(Security("600519", "sh", "测试股票", "合成夹具", NOW.isoformat()))
    clock, research = Clock(), Research()
    daily = Daily(item["created_at"])
    collector = ResearchCollector(store, research, daily, clock)
    return collector, research, daily, clock, item


def add_rule(collector, item, *, event=False, breakout=False, enabled=True):
    with collector.store.connect() as db, db:
        db.execute("""CREATE TABLE IF NOT EXISTS extended_anomaly_rules (
            market TEXT,symbol TEXT,watchlist_created_at TEXT,version INTEGER,enabled INTEGER,config_json TEXT,
            PRIMARY KEY(market,symbol))""")
        db.execute("INSERT INTO extended_anomaly_rules VALUES(?,?,?,?,?,?)",
                   (item["market"], item["symbol"], item["created_at"], 1, int(enabled), json.dumps({"event_enabled": event, "breakout_enabled": breakout})))


def advance_overview(collector, clock):
    collector.tick()
    clock.advance(3)


def test_no_extended_table_is_safe_and_get_does_not_initialize_or_fetch(setup):
    collector, research, _, clock, _ = setup
    assert collector.state()["jobs"] == []
    with collector.store.connect() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='research_collector_jobs'").fetchone()
    collector.tick()
    assert research.calls == [(None, None)]
    for _ in range(3):
        collector.state()
        collector.tick()
    assert len(research.calls) == 1
    clock.advance(901)
    collector.tick()
    assert len(research.calls) == 2


def test_only_enabled_event_rule_gets_stock_research_and_restart_keeps_cooldown(setup):
    collector, research, _, clock, item = setup
    add_rule(collector, item, event=True)
    advance_overview(collector, clock)
    collector.tick()
    assert research.calls == [(None, None), ("sh", "600519")]
    restarted = ResearchCollector(collector.store, research, clock=clock)
    restarted.tick()
    assert len(research.calls) == 2
    clock.advance(301)
    restarted.tick()
    assert research.calls[-1] == ("sh", "600519")
    assert len(research.calls) == 3


@pytest.mark.parametrize("event,enabled", [(False, True), (True, False), ("false", True)])
def test_disabled_or_nonboolean_flag_does_not_fetch_stock(setup, event, enabled):
    collector, research, daily, clock, item = setup
    add_rule(collector, item, event=event, enabled=enabled)
    advance_overview(collector, clock)
    collector.tick()
    assert research.calls == [(None, None)]
    assert daily.calls == []


def test_breakout_fetches_previous_day_once_per_day_persistently(setup):
    collector, research, daily, clock, item = setup
    add_rule(collector, item, breakout=True)
    advance_overview(collector, clock)
    collector.tick()
    assert len(daily.calls) == 1
    assert research.calls == [(None, None)]
    saved = next(row for row in collector.state()["jobs"] if row["kind"] == "daily")
    assert saved["state"] == "succeeded" and saved["success_date"] == "2026-10-23"
    restarted = ResearchCollector(collector.store, research, daily, clock)
    clock.advance(301)
    restarted.tick()
    assert len(daily.calls) == 1


def test_valid_daily_cache_needs_no_upstream_call(setup):
    collector, _, daily, clock, item = setup
    add_rule(collector, item, breakout=True)
    daily.refresh("sh", "600519", item["created_at"])
    daily.calls.clear()
    advance_overview(collector, clock)
    collector.tick()
    assert daily.calls == []
    assert any(row["state"] == "cached" for row in collector.state()["jobs"])


def test_daily_failed_source_has_five_minute_persistent_cooldown(setup):
    collector, _, daily, clock, item = setup
    add_rule(collector, item, breakout=True)
    daily.failure = True
    advance_overview(collector, clock)
    collector.tick()
    assert len(daily.calls) == 1
    collector.tick()
    assert len(daily.calls) == 1
    assert next(row for row in collector.state()["jobs"] if row["kind"] == "daily")["success_date"] is None
    clock.advance(301)
    collector.tick()
    assert len(daily.calls) == 2


def test_successful_http_with_old_daily_date_does_not_get_success_day(setup):
    collector, _, daily, clock, item = setup
    add_rule(collector, item, breakout=True)
    original = daily.refresh
    def old(*args):
        result = original(*args)
        daily.view["data"]["latest_trade_date"] = "2026-10-21"
        return result
    daily.refresh = old
    advance_overview(collector, clock)
    collector.tick()
    job = next(row for row in collector.state()["jobs"] if row["kind"] == "daily")
    assert job["state"] == "failed" and job["success_date"] is None


def test_rule_changed_during_fetch_cancels_completion(setup):
    collector, research, _, clock, item = setup
    add_rule(collector, item, event=True)
    advance_overview(collector, clock)
    def turn_off(market, symbol):
        with collector.store.connect() as db, db:
            db.execute("UPDATE extended_anomaly_rules SET enabled=0,version=version+1")
    research.callback = turn_off
    collector.tick()
    job = next(row for row in collector.state()["jobs"] if row["kind"] == "research")
    assert job["state"] == "cancelled"
    assert job["last_success_at"] is None


def test_removed_readded_watchlist_generation_is_not_collected_with_old_rule(setup):
    collector, research, daily, clock, item = setup
    add_rule(collector, item, event=True, breakout=True)
    with collector.store.connect() as db, db:
        db.execute("UPDATE watchlist SET created_at='new-generation' WHERE symbol='600519'")
    advance_overview(collector, clock)
    collector.tick()
    assert research.calls == [(None, None)]
    assert daily.calls == []


def test_midnight_during_daily_fetch_does_not_mark_new_day_success(setup):
    collector, _, daily, clock, item = setup
    add_rule(collector, item, breakout=True)
    advance_overview(collector, clock)
    daily.callback = lambda: clock.advance(20 * 3600)
    collector.tick()
    job = next(row for row in collector.state()["jobs"] if row["kind"] == "daily")
    assert job["state"] == "failed"
    assert job["success_date"] is None


def test_unknown_year_never_guesses_previous_trading_day(setup):
    collector, _, daily, clock, item = setup
    add_rule(collector, item, breakout=True)
    clock.now = datetime(2027, 1, 8, 6, tzinfo=timezone.utc)
    advance_overview(collector, clock)
    collector.tick()
    assert daily.calls == []


def test_network_is_outside_write_transaction_and_stop_is_observed(setup):
    collector, research, _, _, _ = setup
    entered = Event()
    def callback(market, symbol):
        with collector.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS collector_network_probe(id INTEGER)")
        entered.set()
    research.callback = callback
    assert collector.start()
    assert entered.wait(3)
    assert collector.stop(timeout=3)
    assert not collector.is_alive()
    calls = len(research.calls)
    collector.tick()
    assert len(research.calls) == calls


def test_multiple_instances_cannot_claim_same_job(setup):
    collector, research, daily, clock, _ = setup
    collector.initialize()
    other = ResearchCollector(collector.store, research, daily, clock)
    other.initialize()
    job = collector._job("overview")
    assert collector._claim(job, clock())
    assert not other._claim(job, clock())
