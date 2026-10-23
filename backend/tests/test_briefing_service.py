"""F4 T008-T012 — briefing_service orchestration."""
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.db import get_briefing_by_date, init_db, add_llm_cost, get_llm_cost_today
from app.models.briefing import BlockStatus, BriefingVersion, DataBlock
from app.models.explain import NewsItem
from app.models.push import Priority, PushRequest
from app.services.briefing_service import BriefingService
from app.services.llm_service import LLMResponse, TemplateSignal


def test_local_preview_persists_without_sending(db_path):
    push = _StubPush()
    service = _build_service(db_path=db_path, push=push)
    report = service.generate_local()
    assert report["push_status"] == "local_only"
    assert report["content_json"]
    assert push.sent == []


def test_missing_subfeed_never_claims_complete_followup(db_path):
    market = _ready_market_src()
    market.fetch.return_value.data['availability'] = {'sectors': 'missing'}
    service = _build_service(db_path=db_path, market=market,
        llm=_StubLLM(LLMResponse(text='仅整理可用事实', source='llm_primary', cost_cny=0)))
    service.generate_and_push(is_followup=True)
    record = get_briefing_by_date(db_path, service.clock().date().isoformat())
    assert record['version'] == 'warmup'


def test_regeneration_updates_archive_time(db_path):
    import sqlite3
    service = _build_service(db_path=db_path)
    first = service.generate_local()
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE briefing_record SET created_at='2000-01-01 00:00:00'")
    second = service.generate_local()
    assert second['on_date'] == first['on_date']
    assert second['created_at'] != '2000-01-01 00:00:00'


# ── fixtures & helpers ────────────────────────────────────────────────
@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "test.db"
    init_db(p)
    return p


class _StubPush:
    def __init__(self):
        self.sent: list[PushRequest] = []

    def send(self, req: PushRequest):
        self.sent.append(req)
        outcome = MagicMock()
        outcome.status = "delivered"
        return outcome


class _StubLLM:
    def __init__(self, response):
        self._resp = response
        self.calls = 0

    def complete(self, messages):
        self.calls += 1
        return self._resp


def _ready_market_src():
    s = MagicMock()
    s.fetch.return_value = DataBlock(
        name="market_overview",
        data={"global": [{"name": "纳指", "change_pct": -0.5}], "yesterday": {"sh": 3200}, "sectors": []},
    )
    return s


def _ready_cal_src():
    s = MagicMock()
    s.fetch.return_value = DataBlock(
        name="calendar",
        data={"earnings": [], "econ": [{"name": "CPI"}]},
    )
    return s


def _news():
    n = MagicMock()
    n.fetch_telegraph.return_value = [NewsItem(title="昨夜新规出台")]
    return n


def _watchlist_snapshot():
    return [{"code": "600519", "name": "贵州茅台", "is_holding": True}]


def _build_service(
    *,
    db_path: Path,
    push=None,
    llm=None,
    market=None,
    calendar=None,
    news=None,
    watchlist_snapshot=None,
    clock=None,
):
    return BriefingService(
        db_path=db_path,
        push=push or _StubPush(),
        llm=llm or _StubLLM(LLMResponse(text="AI 简报正文", source="llm_primary", cost_cny=0.0)),
        market_source=market or _ready_market_src(),
        calendar_source=calendar or _ready_cal_src(),
        news_source=news or _news(),
        watchlist_snapshot=watchlist_snapshot or _watchlist_snapshot,
        clock=clock or (lambda: datetime(2026, 5, 28, 9, 15, 0)),
    )


# ── T008: fetch 5 sources ────────────────────────────────────────────
def test_fetch_all_blocks_when_sources_ok(db_path: Path):
    svc = _build_service(db_path=db_path)
    blocks = svc.fetch_blocks()
    assert blocks["market_overview"].status == BlockStatus.ready
    assert blocks["calendar"].status == BlockStatus.ready
    assert blocks["news"].status == BlockStatus.ready
    assert blocks["watchlist"].status == BlockStatus.ready
    # data carried through
    assert blocks["news"].data[0]["title"] == "昨夜新规出台"
    assert blocks["watchlist"].data[0]["code"] == "600519"


def test_fetch_marks_news_missing_on_failure(db_path: Path):
    bad_news = MagicMock()
    bad_news.fetch_telegraph.side_effect = RuntimeError("CLS down")
    svc = _build_service(db_path=db_path, news=bad_news)
    blocks = svc.fetch_blocks()
    assert blocks["news"].status == BlockStatus.missing


def test_fetch_marks_watchlist_missing_on_failure(db_path: Path):
    def boom():
        raise RuntimeError("db down")

    svc = _build_service(db_path=db_path, watchlist_snapshot=boom)
    blocks = svc.fetch_blocks()
    assert blocks["watchlist"].status == BlockStatus.missing


# ── T009: LLM generation + ≤1200 char cap + degrade ──────────────────
def test_generate_uses_llm_text(db_path: Path):
    llm = _StubLLM(LLMResponse(text="AI 简报正文", source="llm_primary", cost_cny=0.0))
    svc = _build_service(db_path=db_path, llm=llm)
    content = svc.build_content(svc.fetch_blocks())
    assert content.body_text == "AI 简报正文"
    assert content.version == BriefingVersion.full
    assert llm.calls == 1


def test_briefing_records_cost_in_shared_daily_budget(db_path: Path):
    llm = _StubLLM(LLMResponse(text="正文", source="llm_primary", cost_cny=0.012))
    svc = _build_service(db_path=db_path, llm=llm)
    svc.build_content(svc.fetch_blocks())
    assert get_llm_cost_today(db_path) == pytest.approx(0.012)


@pytest.mark.parametrize("budget", [0.0, 5.0])
def test_briefing_skips_cloud_call_when_budget_exhausted(db_path, monkeypatch, budget):
    monkeypatch.setattr(settings, "LLM_DAILY_BUDGET", budget)
    add_llm_cost(db_path, 5.0)
    llm = _StubLLM(LLMResponse(text="不应被调用", source="llm_primary", cost_cny=1))
    svc = _build_service(db_path=db_path, llm=llm)
    content = svc.build_content(svc.fetch_blocks())
    assert llm.calls == 0
    assert content.version == BriefingVersion.raw
    assert get_llm_cost_today(db_path) == pytest.approx(5.0)


def test_generate_truncates_to_1200_chars(db_path: Path):
    llm = _StubLLM(LLMResponse(text="超" * 2000, source="llm_primary", cost_cny=0.0))
    svc = _build_service(db_path=db_path, llm=llm)
    content = svc.build_content(svc.fetch_blocks())
    assert len(content.body_text) <= settings.BRIEFING_BODY_LIMIT


def test_generate_falls_back_to_raw_on_llm_template_signal(db_path: Path):
    llm = _StubLLM(TemplateSignal())
    svc = _build_service(db_path=db_path, llm=llm)
    content = svc.build_content(svc.fetch_blocks())
    assert content.version == BriefingVersion.raw
    # 裸数据版仍含部分结构化数据
    assert len(content.body_text) > 0


# ── T010: push + archive (sensitive_filter tail + no dedup key) ──────
def test_push_carries_disclaimer_tail_and_no_dedup_key(db_path: Path):
    push = _StubPush()
    svc = _build_service(db_path=db_path, push=push)
    svc.generate_and_push()
    assert len(push.sent) == 1
    req = push.sent[0]
    assert req.priority == Priority.system
    # 简报不去重: code / signal 为空
    assert req.code is None
    assert req.signal is None
    # 风险尾标
    assert "不构成投资建议" in req.content.get("text", "") or "不构成投资建议" in json.dumps(req.content, ensure_ascii=False)


def test_push_archives_record(db_path: Path):
    svc = _build_service(db_path=db_path)
    svc.generate_and_push()
    row = get_briefing_by_date(db_path, "2026-05-28")
    assert row is not None
    assert row["push_status"] == "delivered"


# ── T011: degrade — empty watchlist / all sources fail ───────────────
def test_empty_watchlist_skips_block_in_card(db_path: Path):
    push = _StubPush()
    svc = _build_service(db_path=db_path, push=push, watchlist_snapshot=lambda: [])
    svc.generate_and_push()
    text = json.dumps(push.sent[0].content, ensure_ascii=False)
    assert "我的自选股" not in text


def test_all_data_sources_fail_yields_placeholder_briefing(db_path: Path):
    def boom():
        raise RuntimeError("all down")

    bad_market = MagicMock()
    bad_market.fetch.return_value = DataBlock(name="market_overview", data=None, status=BlockStatus.missing)
    bad_cal = MagicMock()
    bad_cal.fetch.return_value = DataBlock(name="calendar", data=None, status=BlockStatus.missing)
    bad_news = MagicMock()
    bad_news.fetch_telegraph.side_effect = RuntimeError("cls down")

    push = _StubPush()
    svc = _build_service(
        db_path=db_path,
        push=push,
        market=bad_market,
        calendar=bad_cal,
        news=bad_news,
        watchlist_snapshot=boom,
    )
    svc.generate_and_push()
    row = get_briefing_by_date(db_path, "2026-05-28")
    assert row["version"] == "placeholder"
    text = json.dumps(push.sent[0].content, ensure_ascii=False)
    assert "今日数据获取异常" in text


# ── T012: warmup at 9:15 + full version at 9:18 ──────────────────────
def test_warmup_then_full_overwrites_same_day(db_path: Path):
    # Round 1: 9:15 with news missing → warmup
    bad_news = MagicMock()
    bad_news.fetch_telegraph.side_effect = RuntimeError("not ready")
    push = _StubPush()
    svc1 = _build_service(db_path=db_path, push=push, news=bad_news)
    svc1.generate_and_push(is_followup=False)
    row1 = get_briefing_by_date(db_path, "2026-05-28")
    assert row1["version"] == "warmup"

    # Round 2: 9:18 with all ok → full overwrites
    svc2 = _build_service(db_path=db_path, push=push)
    svc2.generate_and_push(is_followup=True)
    row2 = get_briefing_by_date(db_path, "2026-05-28")
    assert row2["version"] == "full"
    assert len(push.sent) == 2
