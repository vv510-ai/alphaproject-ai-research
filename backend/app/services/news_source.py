"""F3 T004 — news_source.

Wraps AkShare 财联社电报 + 个股公告 with a small TTL cache (default 60s) and
failure tolerance so F3 explanation can degrade to "信息不全" instead of
propagating upstream errors (FR-012).

AkShare endpoint names are isolated behind module-level shims so tests can
monkey-patch them without touching the network.
"""
from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from collections.abc import Callable
from typing import Any

from app.models.explain import NewsItem

logger = logging.getLogger(__name__)


# ── AkShare shims (named per plan.md; verified at runtime, not import) ─────
def _akshare_telegraph() -> Any:  # pragma: no cover - network shim
    import akshare as ak
    import akshare.stock_feature.stock_info as stock_info
    from akshare.request import make_request_with_retry_json

    # The upstream wrapper hardcodes 10 retries with exponential backoff
    # (> 8 minutes). Let our TTL refresh own retries instead of blocking an API.
    def fetch_once(*args, **kwargs):
        kwargs["max_retries"] = 1
        return make_request_with_retry_json(*args, **kwargs)

    stock_info.make_request_with_retry_json = fetch_once

    return ak.stock_info_global_cls(symbol="全部")


def _akshare_announcements(code: str) -> Any:  # pragma: no cover - network shim
    import akshare as ak

    today = date.today()
    return ak.stock_individual_notice_report(
        security=code, symbol="全部",
        begin_date=(today - timedelta(days=7)).strftime("%Y%m%d"),
        end_date=today.strftime("%Y%m%d"),
    )


def _df_to_items(df: Any, *, title_keys=("title", "标题", "公告标题", "content", "内容"),
                 time_keys=("pub_time", "公告日期", "发布日期", "发布时间", "date", "日期")) -> list[NewsItem]:
    """Coerce a DataFrame-like object to NewsItem list, lenient on column names."""
    if df is None:
        return []
    try:
        records = df.to_dict("records") if hasattr(df, "to_dict") else list(df)
    except Exception:
        return []
    out: list[NewsItem] = []
    for row in records:
        title = next((row[k] for k in title_keys if k in row and row[k]), None)
        if not title:
            continue
        ts = next((row[k] for k in time_keys if k in row and row[k]), None)
        if row.get("发布日期") and row.get("发布时间"):
            ts = f"{row['发布日期']} {row['发布时间']}"
        out.append(NewsItem(title=str(title), published_at=str(ts) if ts else None))
    return out


class NewsSource:
    """In-memory TTL cache around AkShare news endpoints."""

    def __init__(
        self,
        *,
        ttl_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, list[NewsItem]]] = {}

    def _get(self, key: str) -> list[NewsItem] | None:
        hit = self._cache.get(key)
        if not hit:
            return None
        stamped, items = hit
        if self._clock() - stamped > self.ttl_s:
            return None
        return items

    def _put(self, key: str, items: list[NewsItem]) -> None:
        self._cache[key] = (self._clock(), items)

    def fetch_telegraph(self) -> list[NewsItem]:
        cached = self._get("telegraph")
        if cached is not None:
            return cached
        try:
            items = _df_to_items(_akshare_telegraph())
        except Exception as exc:
            logger.warning("news_source telegraph failed: %s", exc)
            items = []
        self._put("telegraph", items)
        return items

    def fetch_announcements(self, code: str) -> list[NewsItem]:
        key = f"ann:{code}"
        cached = self._get(key)
        if cached is not None:
            return cached
        try:
            items = _df_to_items(_akshare_announcements(code))
        except Exception as exc:
            logger.warning("news_source announcements(%s) failed: %s", code, exc)
            items = []
        self._put(key, items)
        return items
