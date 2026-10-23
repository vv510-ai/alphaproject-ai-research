from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from backend.app.main import create_app
from backend.app.securities import LookupFailure, Security, market_for, parse_identity
from backend.app.store import WatchlistStore

HEADERS = {"X-Alpha-Request": "watchlist-v1", "Origin": "http://testserver"}
SAMPLE = Security("600519", "sh", "贵州茅台", "合成身份夹具，不是真实取数", "2026-10-17T00:00:00+00:00")


class ResolverFixture:
    def __init__(self):
        self.calls = 0
        self.failure = False

    def resolve(self, symbol):
        self.calls += 1
        market_for(symbol)
        if self.failure:
            raise LookupFailure("source_unavailable", "测试来源暂不可用", 503)
        if symbol != SAMPLE.symbol:
            raise LookupFailure("not_found", "测试代码未找到", 404)
        return SAMPLE


def test_save_read_reopen_and_remove(tmp_path):
    path = tmp_path / "watchlist.sqlite3"
    source = ResolverFixture()
    with TestClient(create_app(path, source, ["testserver"], run_monitor=False)) as desktop:
        assert desktop.get("/api/watchlist").json() == {"items": [], "total": 0}
        result = desktop.post("/api/watchlist", json={"symbol": SAMPLE.symbol}, headers=HEADERS)
        assert result.status_code == 201
        original = result.json()["item"]
        assert original["name"] == SAMPLE.name
        assert desktop.get("/api/watchlist").json()["items"] == [original]
    # Reconstruct the whole application against the same real file.
    with TestClient(create_app(path, source, ["testserver"], run_monitor=False)) as other_client:
        assert other_client.get("/api/watchlist").json()["items"] == [original]
        source.failure = True
        duplicate = other_client.post("/api/watchlist", json={"symbol": SAMPLE.symbol}, headers=HEADERS)
        assert duplicate.status_code == 200
        assert duplicate.json() == {"item": original, "created": False}
        assert source.calls == 1  # Existing records work while the provider is unavailable.
        removed = other_client.delete("/api/watchlist/sh/600519", headers=HEADERS).json()
        assert removed['removed'] is True and removed['undo_token'] and removed['undo_expires_at']
        assert other_client.delete("/api/watchlist/sh/600519", headers=HEADERS).json() == {"removed": False}
        assert other_client.get("/api/watchlist").json()["items"] == []


def test_real_db_concurrent_duplicates(tmp_path):
    store = WatchlistStore(tmp_path / "concurrent.sqlite3")
    store.initialize()
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: store.add(SAMPLE), range(12)))
    assert sum(created for _, created in results) == 1
    assert len(store.list_items()) == 1
    assert all(item == results[0][0] for item, _ in results)


@pytest.mark.parametrize("payload", [{"symbol": "abc"}, {"symbol": 600519}, {"symbol": "600519", "name": "伪造名称"}])
def test_invalid_payload_is_not_saved(tmp_path, payload):
    source = ResolverFixture()
    with TestClient(create_app(tmp_path / "input.sqlite3", source, ["testserver"], run_monitor=False)) as client:
        assert client.post("/api/watchlist", json=payload, headers=HEADERS).status_code == 422
        assert source.calls == 0
        assert client.get("/api/watchlist").json()["total"] == 0


def test_unknown_and_unavailable_provider_do_not_save(tmp_path):
    source = ResolverFixture()
    with TestClient(create_app(tmp_path / "failed.sqlite3", source, ["testserver"], run_monitor=False)) as client:
        assert client.post("/api/watchlist", json={"symbol": "000999"}, headers=HEADERS).status_code == 404
        assert client.post("/api/watchlist", json={"symbol": "123456"}, headers=HEADERS).status_code == 400
        source.failure = True
        result = client.post("/api/watchlist", json={"symbol": SAMPLE.symbol}, headers=HEADERS)
        assert result.status_code == 503
        assert "created" not in result.json()
        assert client.get("/api/watchlist").json()["items"] == []


def test_write_lock_failure_is_visible_and_recoverable(tmp_path):
    path = tmp_path / "busy.sqlite3"
    with TestClient(create_app(path, ResolverFixture(), ["testserver"], run_monitor=False)) as client:
        connection = sqlite3.connect(path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = client.post("/api/watchlist", json={"symbol": SAMPLE.symbol}, headers=HEADERS)
            assert result.status_code == 503
            assert result.json()["error"]["code"] == "storage_unavailable"
        finally:
            connection.rollback()
            connection.close()
        assert client.get("/api/watchlist").json()["items"] == []
        assert client.post("/api/watchlist", json={"symbol": SAMPLE.symbol}, headers=HEADERS).status_code == 201


def test_request_boundaries(tmp_path):
    with TestClient(create_app(tmp_path / "origin.sqlite3", ResolverFixture(), ["testserver"], run_monitor=False)) as client:
        body = {"symbol": SAMPLE.symbol}
        assert client.post("/api/watchlist", json=body).status_code == 403
        assert client.post("/api/watchlist", json=body, headers={**HEADERS, "Origin": "https://example.invalid"}).status_code == 403
        assert client.get("/api/watchlist", headers={"Host": "untrusted.invalid"}).status_code == 400
        assert client.get("/api/watchlist").json()["items"] == []
        assert client.post("/api/watchlist", json=body, headers=HEADERS).status_code == 201
        assert client.delete("/api/watchlist/sz/600519", headers=HEADERS).status_code == 400
        assert client.get("/api/watchlist").json()["total"] == 1


@pytest.mark.parametrize("body", [
    'v_sz600519="1~错误市场~600519";',
    'v_sh600519="1~错误代码~600000";',
    'v_sh600519="1~~600519";',
    '<html>temporarily unavailable</html>',
])
def test_identity_rejects_wrong_or_missing_information(body):
    with pytest.raises(LookupFailure):
        parse_identity(body, "600519")


def test_identity_parse_without_requiring_live_price():
    # Identity lookup can work with a non-trading/stale quote; it makes no price claim.
    item = parse_identity('v_sh600519="1~贵州茅台~600519~0~0";', "600519")
    assert (item.market, item.symbol, item.name) == ("sh", "600519", "贵州茅台")
    with pytest.raises(LookupFailure) as error:
        parse_identity('v_pv_none_match="1";', "600519")
    assert error.value.status == 404


def test_default_database_path_is_independent_of_working_directory(tmp_path, monkeypatch):
    from backend.app.main import ROOT
    monkeypatch.delenv("ALPHA_LAB_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    application = create_app(resolver=ResolverFixture(), allowed_hosts=["testserver"], run_monitor=False)
    assert application.state.store.path == ROOT / ".runtime" / "watchlist.sqlite3"
    assert not (tmp_path / ".runtime").exists()
