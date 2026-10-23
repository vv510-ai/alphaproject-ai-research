"""Public endpoint contract tests using bounded httpx MockTransport responses."""
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event, Lock

import httpx
import pytest

from backend.app.research_provider import PublicResearchProvider, new_york_timezone

NOW = datetime(2026, 10, 23, 6, 0, tzinfo=timezone.utc)


def provider(handler):
    return PublicResearchProvider(clock=lambda: NOW, transport=httpx.MockTransport(handler))


def quote(code, *, at="20261023135900", price="100", pct="1.0"):
    parts = [""] * 35
    for index, value in {1: "测试指数", 2: code[-6:], 3: price, 30: at, 32: pct}.items():
        parts[index] = value
    return f'v_{code}="' + "~".join(parts) + '";'


def test_tencent_batches_three_indices_and_keeps_both_times():
    requests = []
    def handler(request):
        requests.append(request)
        assert "cookie" not in request.headers
        return httpx.Response(200, content="\n".join(quote(c) for c in ("sh000001", "sz399001", "sz399006")).encode("gb18030"))
    result = provider(handler).fetch_section("indices")
    assert len(requests) == 1
    assert len(result["items"]) == 3
    assert result["source_attempts"][0]["status"] == "available"
    row = result["items"][0]
    assert row["as_of"] == "2026-10-23T13:59:00+08:00"
    assert datetime.fromisoformat(row["received_at"]) == NOW
    assert row["raw_time"] == "20261023135900"


def test_fallback_has_dated_values_and_keeps_primary_failure():
    def handler(request):
        if request.url.host == "qt.gtimg.cn":
            return httpx.Response(503)
        assert request.url.host == "push2delay.eastmoney.com"
        return httpx.Response(200, json={"rc": 0, "data": {"total": 3, "diff": [
            {"f12": code, "f14": "指数", "f2": 99, "f3": -1, "f124": int(NOW.timestamp()) - 10}
            for code in ("000001", "399001", "399006")]}})
    result = provider(handler).fetch_section("indices")
    assert result["source"] == "东方财富"
    assert result["source_attempts"][0]["error_message"] == "HTTP 503"
    assert result["source_attempts"][1]["status"] == "available"
    assert len(result["items"]) == 3


def test_global_us_timezone_preserves_new_york_source_clock():
    def handler(request):
        return httpx.Response(200, content="\n".join(quote(c, at="2026-10-22 16:31:52") for c in ("usDJI", "usIXIC", "usINX", "hkHSI")).encode("gb18030"))
    result = provider(handler).fetch_section("global")
    assert len(result["items"]) == 4
    assert result["items"][0]["as_of"] == "2026-10-22T16:31:52-04:00"
    assert result["items"][3]["as_of"] == "2026-10-22T16:31:52+08:00"
    assert new_york_timezone(datetime(2026, 12, 1)).utcoffset(None) == timedelta(hours=-5)
    assert new_york_timezone(datetime(2026, 3, 9)).utcoffset(None) == timedelta(hours=-4)


def test_partial_primary_is_preserved_when_fallback_fails():
    def handler(request):
        if request.url.host == "qt.gtimg.cn":
            return httpx.Response(200, content=quote("sh000001").encode("gb18030"))
        return httpx.Response(500)
    result = provider(handler).fetch_section("indices")
    assert result["status"] == "partial"
    assert result["source"] == "腾讯行情"
    assert len(result["items"]) == 1
    assert result["source_attempts"][-1]["status"] == "unavailable"


def test_future_quote_and_missing_time_cannot_pass_as_available():
    def handler(request):
        if request.url.host == "qt.gtimg.cn":
            return httpx.Response(200, content=(quote("sh000001", at="20261024140000") + quote("sz399001", at="")).encode("gb18030"))
        return httpx.Response(500)
    assert provider(handler).fetch_section("indices")["status"] == "unavailable"


def test_cls_cumulative_pages_and_industry_only_membership():
    pages = []
    def handler(request):
        path = request.url.path
        if path.endswith("plate_list"):
            page = int(request.url.params["page"])
            pages.append(page)
            rows = [{"secu_code": "industry1", "secu_name": "真实行业", "change": .01}]
            if page == 2:
                rows.append({"secu_code": "industry2", "secu_name": "另一行业", "change": -.01})
            return httpx.Response(200, json={"code": 200, "data": {"is_all": int(page == 2), "plate_data": rows}})
        if path.endswith("detail"):
            return httpx.Response(200, json={"code": 200, "data": {"plate": [{"secu_code": "concept"}, {"secu_code": "industry1"}]}})
        if path.endswith("kline"):
            assert request.url.params["secu_code"] == "industry1"
            return httpx.Response(200, json={"code": 200, "data": [{"secu_code": "industry1", "date": 20261022, "close_px": 100, "change": .01}]})
        raise AssertionError(request.url)
    result = provider(handler).fetch_section("sectors", "sh", "600519")
    assert result.get("status") == "available"
    assert pages == [1, 2]
    assert result["items"][0]["industry"] == "真实行业"
    assert result["items"][0]["change_pct"] == 1
    assert result["items"][0]["as_of"] == "2026-10-22"
    assert result["items"][0]["as_of_precision"] == "date"


def test_repeated_industry_pages_cannot_be_invented_into_a_ranking():
    calls = []
    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json={"code": 200, "data": {"is_all": 0, "plate_data": [{"secu_code": "one", "secu_name": "行业", "change": .1}]}})
    source = provider(handler)
    with pytest.raises(ValueError, match="累计分页"):
        source._industry_universe()
    assert len(calls) == 2


def test_news_stable_original_id_and_explicit_related_symbols():
    def handler(request):
        assert request.url.path == "/v1/roll/get_roll_list"
        assert "sign" in request.url.params
        return httpx.Response(200, json={"errno": 0, "data": {"roll_data": [
            {"id": 123, "title": "增长10%", "content": "没有下跌", "ctime": int(NOW.timestamp()) - 60, "stock_list": [{"StockID": "sh600519"}]},
            {"id": 124, "title": "其他股票", "content": "文本碰巧600519", "ctime": int(NOW.timestamp()) - 30, "stock_list": [{"StockID": "sz000001"}]},
        ]}})
    result = provider(handler)._cls_news("600519")
    assert len(result["items"]) == 1
    assert result["items"][0]["upstream_id"] == "123"
    assert result["items"][0]["summary"] == "没有下跌"
    assert result["items"][0]["url"] == "https://www.cls.cn/detail/123"
    assert result["status"] == "partial"  # Latest feed is not a full stock search.


def test_jsonp_never_executes_callback_or_uses_upstream_cookie():
    def handler(request):
        assert "cookie" not in request.headers
        return httpx.Response(200, text='other_callback({"result":{}});malicious()')
    with pytest.raises(ValueError, match="格式"):
        provider(handler)._stock_news("600519")


@pytest.mark.parametrize("extension", ["pdf", "PDF"])
def test_cninfo_fallback_resolves_org_id_and_uses_pdf_source(extension):
    paths = []
    def handler(request):
        paths.append(str(request.url))
        if "eastmoney" in request.url.host:
            return httpx.Response(567, text="blocked")
        if request.url.path.endswith("szse_stock.json"):
            return httpx.Response(200, json={"stockList": [{"code": "600519", "orgId": "verified-org"}]})
        assert request.method == "POST"
        assert b"stock=600519%2Cverified-org" in request.content
        assert "cookie" not in request.headers
        return httpx.Response(200, json={"totalAnnouncement": 2, "hasMore": False, "announcements": [
            {"secCode": "600519", "announcementId": "notice1", "announcementTitle": "利润未降10%", "announcementTime": int(NOW.timestamp() * 1000), "adjunctUrl": f"finalpage/2026-10-23/notice1.{extension}"},
            {"secCode": "000001", "announcementId": "other", "announcementTitle": "另一股票", "announcementTime": int(NOW.timestamp() * 1000), "adjunctUrl": "finalpage/2026-10-23/other.pdf"},
        ]})
    result = provider(handler).fetch_section("announcements", "sh", "600519")
    assert result["source"] == "巨潮资讯"
    assert result["source_attempts"][0]["error_message"] == "HTTP 567"
    assert len(result["items"]) == 1
    assert result["items"][0]["title"] == "利润未降10%"
    assert result["items"][0]["url"] == f"https://static.cninfo.com.cn/finalpage/2026-10-23/notice1.{extension}"
    assert len(paths) == 3


def test_cninfo_zero_results_is_distinct_from_malformed_response():
    no_rows = provider(lambda request: httpx.Response(200, json={"totalAnnouncement": 0, "announcements": None, "hasMore": False}))
    assert no_rows._cninfo_notices(None)["items"] == []
    broken = provider(lambda request: httpx.Response(200, json={"announcements": None}))
    with pytest.raises(ValueError):
        broken._cninfo_notices(None)


def test_macro_future_schedule_does_not_turn_actual_into_known_fact():
    def handler(request):
        return httpx.Response(200, json={"code": 20000, "data": {"items": [
            {"id": 1, "country": "测试", "title": "已公布", "public_date": int(NOW.timestamp()) - 60, "actual": "0", "forecast": "", "previous": "-1", "uri": "https://example.org/1"},
            {"id": 2, "country": "测试", "title": "未来公布", "public_date": int(NOW.timestamp()) + 60, "actual": "3", "forecast": "2", "previous": "1"},
            {"id": 3, "country": "测试", "title": "缺少实际值", "public_date": int(NOW.timestamp()) - 90, "actual": "", "forecast": "-", "previous": "-"},
        ]}})
    rows = provider(handler)._macro()
    assert rows[0]["actual"] == 0
    assert rows[0]["forecast"] is None
    assert rows[0]["published_at"]
    assert rows[1]["published_at"] is None and rows[1]["actual"] is None
    assert rows[2]["published_at"] is None and rows[2]["previous"] is None


def test_calendar_distinguishes_macro_failure_from_earnings_failure():
    def handler(request):
        if "awtmt" in request.url.host:
            return httpx.Response(200, json={"code": 20000, "data": {"items": []}})
        return httpx.Response(503)
    result = provider(handler).fetch_section("calendar")
    assert result["status"] == "partial"
    assert result["items"] == []
    assert result["source_attempts"][0]["status"] == "available"
    assert result["source_attempts"][1]["error_message"] == "HTTP 503"


def test_earnings_effective_dates_and_unknown_publication_are_preserved():
    def handler(request):
        return httpx.Response(200, json={"success": True, "result": {"pages": 1, "data": [
            {"SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "测试", "REPORT_DATE": "2026-09-30", "APPOINT_PUBLISH_DATE": "2026-10-23", "ACTUAL_PUBLISH_DATE": "2026-10-22"},
            {"SECURITY_CODE": "000001", "SECURITY_NAME_ABBR": "测试二", "REPORT_DATE": "2026-09-30", "APPOINT_PUBLISH_DATE": "2026-10-23"},
        ]}})
    rows = provider(handler)._earnings(None)
    assert len(rows) == 1 and rows[0]["symbol"] == "000001"
    assert rows[0]["published_at"] is None
    assert rows[0]["scheduled_at"] == "2026-10-23"


def test_oversized_http_response_fails_with_bounded_payload(monkeypatch):
    import backend.app.research_provider as module
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 100)
    source = provider(lambda request: httpx.Response(200, text="x" * 101))
    with pytest.raises(TimeoutError, match="budget"):
        source._read("https://example.org/feed")


def test_calendar_fetches_bounded_pages_and_rejects_repeated_pages():
    calls = []
    def handler(request):
        page = int(request.url.params["pageNumber"])
        calls.append(page)
        assert request.url.params["pageSize"] == "500"
        return httpx.Response(200, json={"success": True, "result": {"pages": 2, "data": [
            {"SECURITY_CODE": str(page).zfill(6), "SECURITY_NAME_ABBR": "测试", "REPORT_DATE": "2026-09-30", "APPOINT_PUBLISH_DATE": "2026-10-23"}]}})
    assert len(provider(handler)._earnings(None)) == 2
    assert calls == [1, 2]
    repeated = provider(lambda request: httpx.Response(200, json={"success": True, "result": {"pages": 2, "data": [
        {"SECURITY_CODE": "000001", "REPORT_DATE": "2026-09-30", "APPOINT_PUBLISH_DATE": "2026-10-23"}]}}))
    with pytest.raises(ValueError, match="重复"):
        repeated._earnings(None)


def test_all_feed_threads_share_four_http_slots():
    reached_four, release, lock = Event(), Event(), Lock()
    active, peak = 0, 0
    def handler(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                reached_four.set()
        assert release.wait(3)
        with lock:
            active -= 1
        return httpx.Response(200, text="ok")
    source = provider(handler)
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(source._read, "https://example.org/feed") for _ in range(6)]
        try:
            assert reached_four.wait(2)
            assert peak == 4
        finally:
            release.set()
        assert all(future.result() == "ok" for future in futures)
    assert peak <= 4
