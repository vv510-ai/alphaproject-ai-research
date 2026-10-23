"""Synthetic quote fixtures test data rejection, storage and API contracts."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from threading import Event

from fastapi.testclient import TestClient
import pytest

from backend.app.main import create_app
from backend.app.quote_service import QuoteService
from backend.app.quotes import parse_quote
from backend.app.securities import LookupFailure, Security
from backend.app.store import WatchlistStore

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
SECURITY = Security("600519", "sh", "合成测试股票", "测试夹具", NOW.isoformat())
HEADERS = {"X-Alpha-Request": "watchlist-v1", "Origin": "http://testserver"}


def body(**changes):
    fields = [""] * 35
    for i, value in {0: "1", 1: SECURITY.name, 2: "600519", 3: "103.00", 4: "100.00", 30: "20261017135900", 31: "3.00", 32: "3.00"}.items():
        fields[i] = value
    for index, value in changes.items():
        fields[int(index)] = value
    return 'v_sh600519="' + "~".join(fields) + '";'


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds=31):
        self.now += timedelta(seconds=seconds)


class SourceFixture:
    def __init__(self, clock):
        self.clock, self.calls = clock, 0
        self.raw = body()
        self.failure = None
        self.during_fetch = None

    def fetch(self, symbol):
        self.calls += 1
        if self.during_fetch:
            self.during_fetch()
        if self.failure:
            raise self.failure
        return parse_quote(self.raw, symbol, self.clock())


@pytest.fixture
def service(tmp_path):
    store = WatchlistStore(tmp_path / "quotes.sqlite3")
    store.initialize()
    store.add(SECURITY)
    clock = Clock()
    source = SourceFixture(clock)
    quotes = QuoteService(store, source, clock)
    quotes.initialize()
    return quotes, source, clock


def test_parse_keeps_values_units_and_two_distinct_times():
    result = parse_quote(body(), "600519", NOW)
    assert (result.price, result.previous_close, result.change, result.change_pct) == ("103.00", "100.00", "3.00", "3.00")
    assert result.price_unit == "元"
    assert result.change_pct_unit == "%"
    assert result.source_reported_at == "2026-10-17T13:59:00+08:00"
    assert result.received_at == "2026-10-17T06:00:00+00:00"
    assert result.raw_text == body()


@pytest.mark.parametrize("changes,code", [
    ({"3": "NaN"}, "invalid_number"), ({"3": ""}, "invalid_number"),
    ({"3": "0"}, "invalid_price"), ({"4": "0"}, "invalid_price"),
    ({"32": "8.00"}, "inconsistent_values"), ({"31": "2.50"}, "inconsistent_values"),
    ({"30": ""}, "missing_time"), ({"30": "-"}, "missing_time"),
    ({"30": "20260230120000"}, "invalid_time"), ({"30": "20261017140001"}, "future_time"),
])
def test_rejects_unusable_source_data(changes, code):
    with pytest.raises(LookupFailure) as error:
        parse_quote(body(**changes), "600519", NOW)
    assert error.value.code == code


def test_wrong_identity_never_becomes_quote():
    for raw in [body().replace("v_sh600519", "v_sz600519"), body(**{"2": "600000"})]:
        with pytest.raises(LookupFailure):
            parse_quote(raw, "600519", NOW)


@pytest.mark.parametrize("price,close,change,percent", [
    ("10.01", "10.00", "0.01", "0.10"),
    ("10.00", "10.01", "-0.01", "-0.10"),
    ("10.00", "10.00", "0.00", "0.00"),
    ("1329.00", "1312.97", "16.03", "1.22"),
])
def test_small_changes_and_rounded_percent_remain_consistent(price, close, change, percent):
    quote = parse_quote(body(**{"3": price, "4": close, "31": change, "32": percent}), "600519", NOW)
    assert (quote.change, quote.change_pct) == (change, percent)


@pytest.mark.parametrize("change,percent", [
    ("-0.01", "0.10"),  # Opposite amount sign used to pass a 0.02 tolerance.
    ("0.01", "-0.10"),
    ("0.02", "0.10"),
    ("0.01", "0.11"),
])
def test_inconsistent_amount_or_percentage_preserves_saved_quote(service, change, percent):
    quotes, source, clock = service
    original = quotes.refresh("sh", "600519")["quote"]["data"]
    clock.advance()
    source.raw = body(**{"3": "10.01", "4": "10.00", "31": change, "32": percent})
    result = quotes.refresh("sh", "600519")
    assert result["refresh_succeeded"] is False
    assert result["quote"]["data"] == original
    assert result["quote"]["refresh_state"] == "failed"
    assert "对不上" in result["quote"]["error_message"]


def test_data_date_is_classified_in_shanghai_not_utc(service):
    quotes, source, clock = service
    clock.now = datetime(2026, 10, 16, 16, 30, tzinfo=timezone.utc)  # Shanghai Oct 17 00:30
    source.raw = body(**{"30": "20261016230000"})
    view = quotes.refresh("sh", "600519")["quote"]
    assert view["data_state"] == "historical"
    assert view["source_age_seconds"] == 5400
    assert not view["realtime_verified"]
    assert view["market_state"] == "closed"


def test_persisted_quote_and_original_body_survive_new_service(service):
    quotes, source, clock = service
    original = quotes.refresh("sh", "600519")["quote"]["data"]
    with quotes.store.connect() as connection:
        saved = json.loads(connection.execute("SELECT payload FROM quote_cache").fetchone()[0])
    assert saved["raw_text"] == body()
    assert "raw_text" not in original
    reopened = QuoteService(WatchlistStore(quotes.store.path), source, clock)
    reopened.initialize()
    assert reopened.dashboard()["items"][0]["quote"]["data"] == original
    for _ in range(3):
        reopened.dashboard()
    assert source.calls == 1  # Page reads do not fetch upstream.


def test_failed_refresh_and_regressed_time_keep_entire_previous_quote(service):
    quotes, source, clock = service
    original = quotes.refresh("sh", "600519")["quote"]["data"]
    clock.advance()
    source.failure = LookupFailure("offline", "合成来源连接失败", 503)
    failed = quotes.refresh("sh", "600519")
    assert failed["refresh_succeeded"] is False
    assert failed["quote"]["refresh_state"] == "failed"
    assert failed["quote"]["data"] == original
    assert failed["quote"]["error_message"] == "合成来源连接失败"
    clock.advance()
    source.failure = None
    source.raw = body(**{"30": "20261017135859"})
    regressed = quotes.refresh("sh", "600519")
    assert regressed["refresh_succeeded"] is False
    assert regressed["quote"]["data"] == original
    assert "更早" in regressed["quote"]["error_message"]
    clock.advance()
    source.raw = body(**{"30": "20261017140100"})
    assert quotes.refresh("sh", "600519")["refresh_succeeded"] is True


def test_missing_time_keeps_empty_quote_instead_of_zero(service):
    quotes, source, _ = service
    source.raw = body(**{"30": ""})
    result = quotes.refresh("sh", "600519")
    assert result["refresh_succeeded"] is False
    assert result["quote"]["data"] is None
    assert result["quote"]["refresh_state"] == "failed"
    assert "来源时间缺失" in result["quote"]["error_message"]


def test_second_request_and_restart_keep_the_cooldown(service):
    quotes, source, clock = service
    quotes.refresh("sh", "600519")
    clock.advance(10)
    reopened = QuoteService(quotes.store, source, clock)
    with pytest.raises(LookupFailure) as error:
        reopened.refresh("sh", "600519")
    assert error.value.status == 429
    assert source.calls == 1
    assert reopened.dashboard()["items"][0]["quote"]["retry_after_seconds"] == 20


def test_concurrent_device_refresh_only_fetches_once(service):
    quotes, source, _ = service
    entered, finish = Event(), Event()
    def block():
        entered.set()
        assert finish.wait(5)
    source.during_fetch = block
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(quotes.refresh, "sh", "600519")
        assert entered.wait(5)
        try:
            with pytest.raises(LookupFailure) as error:
                quotes.refresh("sh", "600519")
            assert error.value.status == 429
        finally:
            finish.set()
        assert first.result()["refresh_succeeded"] is True
    assert source.calls == 1


@pytest.mark.parametrize("readd", [False, True])
def test_delete_during_fetch_does_not_resurrect_or_poison_readded_item(service, readd):
    quotes, source, _ = service
    def remove():
        assert quotes.store.remove("sh", "600519")  # Works while upstream request is in progress.
        if readd:
            quotes.store.add(SECURITY)
    source.during_fetch = remove
    with pytest.raises(LookupFailure) as error:
        quotes.refresh("sh", "600519")
    assert error.value.status == 409
    with quotes.store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM quote_cache").fetchone()[0] == 0
    result = quotes.dashboard()["items"]
    assert len(result) == int(readd)
    if readd:
        assert result[0]["quote"]["data"] is None


def test_unconfirmed_fetch_becomes_retryable_after_guard_interval(service):
    quotes, source, clock = service
    source.failure = RuntimeError("synthetic interrupted request")
    with pytest.raises(RuntimeError):
        quotes.refresh("sh", "600519")
    assert quotes.dashboard()["items"][0]["quote"]["refresh_state"] == "processing"
    clock.advance()
    view = quotes.dashboard()["items"][0]["quote"]
    assert view["refresh_state"] == "interrupted"
    assert view["retry_after_seconds"] == 0


def test_api_contract_and_same_origin_guard(tmp_path):
    clock = Clock()
    source = SourceFixture(clock)
    app = create_app(tmp_path / "api.sqlite3", allowed_hosts=["testserver"], quote_source=source, clock=clock, run_monitor=False)
    with TestClient(app) as client:
        app.state.store.add(SECURITY)
        assert client.get("/api/dashboard").json()["items"][0]["quote"]["data"] is None
        assert source.calls == 0
        assert client.post("/api/quotes/sh/600519/refresh").status_code == 403
        assert client.post("/api/quotes/sz/600519/refresh", headers=HEADERS).status_code == 400
        assert client.post("/api/quotes/sz/000001/refresh", headers=HEADERS).status_code == 404
        result = client.post("/api/quotes/sh/600519/refresh", headers=HEADERS).json()
        assert result["refresh_succeeded"] is True
        assert result["quote"]["data"]["price"] == "103.00"
        assert client.get("/api/dashboard").json()["items"][0]["quote"]["data"] == result["quote"]["data"]
        assert "quote" not in client.get("/api/watchlist").json()["items"][0]  # Old endpoint unchanged.
        clock.advance()
        source.raw = body(**{"3": "NaN"})
        rejected = client.post("/api/quotes/sh/600519/refresh", headers=HEADERS).json()
        assert rejected["refresh_succeeded"] is False  # HTTP success is not accepted market data.
        assert rejected["quote"]["data"] == result["quote"]["data"]
