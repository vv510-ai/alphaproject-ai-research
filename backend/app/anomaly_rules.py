"""A price rule is arithmetic plus evidence checks, never a price prediction."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

from .quotes import CHINA_TIME, Quote
from .live_policy import FRESHNESS_SECONDS, proof_eligibility
from .securities import LookupFailure

DEMO_FRESHNESS_SECONDS = 120


def threshold(value: str) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}(?:\.[0-9]{1,4})?", value):
        raise LookupFailure("invalid_threshold", "阈值请填写正数，最多 4 位整数和 4 位小数。", 422)
    number = Decimal(value)
    if number <= 0:
        raise LookupFailure("invalid_threshold", "上涨和下跌阈值都必须大于 0。", 422)
    return number


@dataclass(frozen=True)
class Sample:
    quote: Quote | None
    market_state: str = "unknown"
    realtime_verified: bool = False
    synthetic: bool = False
    eligibility: dict | None = None


def inspect_sample(sample: Sample, market: str, symbol: str, now: datetime,
                   last_source_at: str | None = None, freshness_seconds: int = DEMO_FRESHNESS_SECONDS,
                   mode: str = "live") -> tuple[str | None, Decimal | None]:
    """Return a visible reason or an unrounded change. No database/network side effects."""
    if mode not in ("demo", "live"):
        return "检测类别无法核对", None
    if mode == "live":
        freshness_seconds = FRESHNESS_SECONDS
    quote = sample.quote
    if quote is None:
        return "尚无可用报价", None
    if quote.market != market or quote.symbol != symbol:
        return "报价与股票身份不一致", None
    if sample.synthetic != (mode == "demo"):
        return "演练数据与真实数据类别不一致", None
    try:
        numbers = (quote.price, quote.previous_close)
        if not all(isinstance(n, str) and re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,8})?", n) for n in numbers):
            return "现价或昨收缺失/无效", None
        price, previous = map(Decimal, numbers)
        if price <= 0 or previous <= 0:
            return "现价和昨收必须大于零", None
        source_at, received_at = map(datetime.fromisoformat, (quote.source_reported_at, quote.received_at))
        if any(t.tzinfo is None or t.utcoffset() is None for t in (source_at, received_at, now)):
            return "报价时间缺少时区", None
        if source_at > received_at or received_at > now:
            return "报价时间位于未来，需核对时钟", None
        if last_source_at and source_at < datetime.fromisoformat(last_source_at):
            return "来源时间倒退，保留上次有效依据", None
        if source_at.astimezone(CHINA_TIME).date() != now.astimezone(CHINA_TIME).date():
            return "报价不是本次检查日期的数据", None
        if (now - source_at).total_seconds() > freshness_seconds:
            return "报价已超过本次检测的时效条件", None
    except (ValueError, TypeError, InvalidOperation):
        return "报价数字或时间无法核对", None
    if sample.market_state != "trading":
        return {"closed": "当前为非交易时段", "break": "当前为午间休市", "auction": "集合竞价阶段不参与真实检测",
                "halted": "证券当前停牌"}.get(sample.market_state, "交易日历或证券交易状态尚未核验"), None
    if not sample.realtime_verified:
        reason = (sample.eligibility.get("reason") if isinstance(sample.eligibility, dict)
                  and not sample.eligibility.get("eligible") else None)
        return reason or "盘中更新与行情时效尚未核验", None
    if mode == "live":
        eligibility = sample.eligibility if isinstance(sample.eligibility, dict) else {}
        verified = proof_eligibility(quote, eligibility.get("proof"), now)
        if not eligibility.get("eligible"):
            return "当前采样未取得参与真实检测的资格", None
        if not verified["eligible"]:
            return verified["reason"], None
    return None, (price - previous) / previous * 100


def meets(change_pct: Decimal, direction: str, value: str) -> bool:
    level = threshold(value)
    return change_pct >= level if direction == "up" else change_pct <= -level
