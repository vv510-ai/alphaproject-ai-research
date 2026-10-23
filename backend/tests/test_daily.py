"""Synthetic daily histories exercise units, boundaries and persistent results."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
import pytest

from backend.app.daily import parse_daily
from backend.app.daily_service import DailyService
from backend.app.main import create_app
from backend.app.securities import LookupFailure, Security
from backend.app.store import WatchlistStore

NOW = datetime(2026, 10, 17, 16, 5, tzinfo=timezone.utc)  # Oct 18 in Shanghai.
START, END = date(2026, 4, 20), date(2026, 10, 17)
SECURITY = Security("600519", "sh", "合成测试股票", "测试夹具", NOW.isoformat())
HEADERS = {"X-Alpha-Request": "watchlist-v1", "Origin": "http://testserver"}


def body(rows=None, code="sh600519", series="day"):
    if rows is None:
        rows = [["2026-10-15", "100.00", "101.00", "102.00", "99.00", "100.00", {}, "0.01", "100.00"],
                ["2026-10-16", "101.00", "102.00", "103.00", "100.00", "200.00", {}, "0.02", "204.00"]]
    return json.dumps({"code": 0, "data": {code: {series: rows, "qt": {code: ["1", SECURITY.name, code[2:]]}}}})


class Clock:
    now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds=31):
        self.now += timedelta(seconds=seconds)


class SourceFixture:
    def __init__(self, clock):
        self.clock, self.calls, self.raw = clock, 0, body()
        self.failure = self.during_fetch = None

    def fetch(self, symbol, start, end):
        self.calls += 1
        self.requested_dates = start, end
        if self.during_fetch:
            self.during_fetch()
        if self.failure:
            raise self.failure
        return parse_daily(self.raw, symbol, start, end, self.clock())


@pytest.fixture
def service(tmp_path):
    store = WatchlistStore(tmp_path / "daily.sqlite3")
    store.initialize()
    item, _ = store.add(SECURITY)
    clock = Clock()
    source = SourceFixture(clock)
    daily = DailyService(store, source, clock)
    daily.initialize()
    return daily, source, clock, item


def refresh(service):
    daily, _, _, item = service
    return daily.refresh("sh", "600519", item["created_at"])


def test_keeps_identity_dates_units_raw_and_ohlc():
    result = parse_daily(body(), "600519", START, END, NOW)
    assert (result.price_unit, result.volume_unit, result.raw_volume_unit, result.volume_multiplier) == ("元", "股", "手", 100)
    assert result.adjustment == "none"
    assert result.latest_trade_date == "2026-10-16"
    assert result.raw_text == body()
    assert result.bars[-1] == {"date": "2026-10-16", "open": "101.00", "close": "102.00", "high": "103.00", "low": "100.00", "volume": "20000", "raw_volume": "200.00"}


@pytest.mark.parametrize("code,factor", [("sh600519", 100), ("sz000001", 100), ("sz300750", 100), ("sh688981", 1)])
def test_captured_source_samples_pass_unit_crosscheck(code, factor):
    # Captured earlier in this lesson; this test is offline and does not claim fresh data.
    path = Path(__file__).parent / "fixtures" / f"{code}-newfqkline-probe.json"
    parsed = parse_daily(path.read_text(encoding="utf-8"), code[2:], START, END, NOW)
    assert parsed.volume_multiplier == factor
    assert parsed.bars[-1]["volume"] == {"sh600519": "3850200", "sz000001": "111185100", "sz300750": "29742400", "sh688981": "37855220"}[code]


@pytest.mark.parametrize("index,value", [(1, "NaN"), (1, "0"), (3, "99"), (4, "103"), (5, "-1"), (5, "0.001"), (5, "2.00"), (8, "0"), (8, "20400")])
def test_bad_price_or_volume_never_replaces_cached_history(service, index, value):
    daily, source, clock, _ = service
    original = refresh(service)["history"]["data"]
    clock.advance()
    payload = json.loads(body())
    payload["data"]["sh600519"]["day"][-1][index] = value
    source.raw = json.dumps(payload)
    failed = refresh(service)
    assert not failed["refresh_succeeded"]
    assert failed["history"]["data"] == original
    assert failed["history"]["error_message"]


@pytest.mark.parametrize("kind", ["wrong_code", "wrong_market", "wrong_identity", "qfq", "duplicate", "invalid_date", "future", "empty", "truncated"])
def test_rejects_wrong_kind_identity_or_dates(kind):
    payload = json.loads(body())
    node = payload["data"]["sh600519"]
    if kind == "wrong_code":
        payload["data"]["sh600000"] = payload["data"].pop("sh600519")
    elif kind == "wrong_market":
        payload["data"]["sz600519"] = payload["data"].pop("sh600519")
    elif kind == "wrong_identity":
        node["qt"]["sh600519"][2] = "600000"
    elif kind == "qfq":
        node["qfqday"] = node.pop("day")
    elif kind == "duplicate":
        node["day"].append(node["day"][0])
    elif kind == "invalid_date":
        node["day"][0][0] = "2026-02-30"
    elif kind == "future":
        node["day"][-1][0] = "2026-10-19"
    elif kind == "empty":
        node["day"] = []
    elif kind == "truncated":
        node["day"][0] = node["day"][0][:6]
    with pytest.raises(LookupFailure):
        parse_daily(json.dumps(payload), "600519", START, END, NOW)


def test_filters_outside_range_and_today_without_filling_missing_dates():
    rows = json.loads(body())["data"]["sh600519"]["day"]
    rows += [["2026-04-19", *rows[0][1:]], ["2026-10-18", *rows[0][1:]]]
    parsed = parse_daily(body(list(reversed(rows))), "600519", START, END, NOW)
    assert [r["date"] for r in parsed.bars] == ["2026-10-15", "2026-10-16"]
    assert parsed.excluded_outside_range == 2


def test_explicit_zero_volume_is_kept_and_not_invented():
    row = ["2026-10-16", "100", "100", "100", "100", "0", {}, "0", "0"]
    parsed = parse_daily(body([row]), "600519", START, END, NOW)
    assert parsed.bars[0]["volume"] == "0"


def test_data_remains_empty_when_no_valid_response(service):
    daily, source, _, _ = service
    source.raw = body(series="qfqday")
    result = refresh(service)
    assert result["history"]["data"] is None
    assert result["history"]["refresh_state"] == "failed"


def test_reopen_reads_raw_batch_without_network_and_keeps_cooldown(service):
    daily, source, clock, item = service
    original = refresh(service)["history"]["data"]
    assert source.requested_dates == (START, END)
    with daily.store.connect() as connection:
        payload = json.loads(connection.execute("SELECT payload FROM daily_cache").fetchone()[0])
    assert payload["raw_text"] == body() and "raw_text" not in original
    reopened = DailyService(WatchlistStore(daily.store.path), source, clock)
    reopened.initialize()
    for _ in range(3):
        assert reopened.get("sh", "600519")["data"] == original
    assert source.calls == 1
    clock.advance(10)
    with pytest.raises(LookupFailure) as error:
        reopened.refresh("sh", "600519", item["created_at"])
    assert error.value.status == 429 and source.calls == 1


def test_failed_or_older_batch_keeps_previous_then_recovers(service):
    daily, source, clock, _ = service
    original = refresh(service)["history"]["data"]
    clock.advance()
    source.failure = LookupFailure("offline", "合成来源不可用", 503)
    assert refresh(service)["history"]["data"] == original
    source.failure = None
    clock.advance()
    source.raw = body(json.loads(body())["data"]["sh600519"]["day"][:1])
    old = refresh(service)
    assert not old["refresh_succeeded"] and old["history"]["data"] == original
    assert "更早" in old["history"]["error_message"]
    clock.advance()
    source.raw = body()
    assert refresh(service)["refresh_succeeded"]


def test_simultaneous_refresh_calls_source_once(service):
    daily, source, _, item = service
    entered, finish = Event(), Event()
    def block():
        entered.set()
        assert finish.wait(5)
    source.during_fetch = block
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(daily.refresh, "sh", "600519", item["created_at"])
        assert entered.wait(5)
        try:
            with pytest.raises(LookupFailure) as error:
                refresh(service)
            assert error.value.status == 429
        finally:
            finish.set()
        assert pending.result()["refresh_succeeded"]
    assert source.calls == 1


@pytest.mark.parametrize("readd", [False, True])
def test_late_fetch_cannot_write_into_removed_or_readded_record(service, readd):
    daily, source, _, _ = service
    def remove():
        daily.store.remove("sh", "600519")
        if readd:
            daily.store.add(SECURITY)
    source.during_fetch = remove
    with pytest.raises(LookupFailure) as error:
        refresh(service)
    assert error.value.status == 409
    with daily.store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM daily_cache").fetchone()[0] == 0


def test_stale_client_generation_does_not_even_fetch(service):
    daily, source, _, item = service
    daily.store.remove("sh", "600519")
    daily.store.add(SECURITY)
    with pytest.raises(LookupFailure) as error:
        daily.refresh("sh", "600519", item["created_at"])
    assert error.value.status == 409 and source.calls == 0


def test_interrupted_request_can_be_retried_after_interval(service):
    daily, source, clock, _ = service
    source.failure = RuntimeError("synthetic interruption")
    with pytest.raises(RuntimeError):
        refresh(service)
    assert daily.get("sh", "600519")["refresh_state"] == "processing"
    clock.advance()
    assert daily.get("sh", "600519")["refresh_state"] == "interrupted"
    source.failure = None
    assert refresh(service)["refresh_succeeded"]


def test_api_returns_record_identity_and_separates_refresh_failure(tmp_path):
    clock = Clock()
    source = SourceFixture(clock)
    app = create_app(tmp_path / "api.sqlite3", allowed_hosts=["testserver"], clock=clock, daily_source=source, run_monitor=False)
    with TestClient(app) as client:
        item, _ = app.state.store.add(SECURITY)
        endpoint = "/api/daily/sh/600519"
        payload = {"watchlist_created_at": item["created_at"]}
        assert client.get(endpoint).json()["data"] is None and source.calls == 0
        assert client.get("/api/daily/sz/600519").status_code == 400
        assert client.get("/api/daily/sz/000001").status_code == 404
        assert client.post(endpoint + "/refresh", json=payload).status_code == 403
        assert client.post(endpoint + "/refresh", headers=HEADERS, json={}).status_code == 422
        result = client.post(endpoint + "/refresh", headers=HEADERS, json=payload).json()
        assert result["refresh_succeeded"] and result["history"]["watchlist_created_at"] == item["created_at"]
        assert client.get(endpoint).json()["data"]["bars"][-1]["volume"] == "20000"
        clock.advance()
        source.raw = "not json"
        failed = client.post(endpoint + "/refresh", headers=HEADERS, json=payload)
        assert failed.status_code == 200 and not failed.json()["refresh_succeeded"]
        assert failed.json()["history"]["data"] == result["history"]["data"]
        assert client.get("/api/watchlist").json()["total"] == 1
