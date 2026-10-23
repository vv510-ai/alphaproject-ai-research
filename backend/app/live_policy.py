"""Conservative recent-trade evidence; this is not an official halt-status feed."""

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

from .quotes import CHINA_TIME, Quote, parse_quote
from .securities import LookupFailure
from .trading_calendar import TradingCalendar, VERSION as CALENDAR_VERSION

POLICY_VERSION = "recent-trade-pair-v1"
FRESHNESS_SECONDS = 120  # Initial conservative engineering bound, not a source SLA.
COLLECT_INTERVAL_SECONDS = 60


def session_context(now, calendar=None):
    """Only the two continuous-auction windows can qualify a live sample."""
    result = {"state": "unknown", "reason": "系统时间或交易日历尚无法核对。",
              "session_id": None, "calendar_version": None, "session_end_at": None}
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return result
    local = now.astimezone(CHINA_TIME)
    day_state, day_reason = (calendar or TradingCalendar()).day(local.date())
    result["calendar_version"] = CALENDAR_VERSION
    # Live samples only require today's calendar. The briefing's previous-day lookup
    # can legitimately be unknown on the first covered trading day of a year.
    if day_state != "open":
        result.update(state="closed" if day_state == "closed" else "unknown", reason=day_reason)
        return result
    current = local.time().replace(tzinfo=None)
    if time(9, 30) <= current < time(11, 30):
        part, end = "am", time(11, 30)
    elif time(13) <= current < time(14, 57):
        part, end = "pm", time(14, 57)
    else:
        if time(11, 30) <= current < time(13):
            result.update(state="break", reason="午间休市，暂停采集与真实检测。")
        elif time(9, 15) <= current < time(9, 30) or time(14, 57) <= current < time(15):
            result.update(state="auction", reason="集合竞价或开盘前阶段，暂停真实检测。")
        else:
            result.update(state="closed", reason="当前不在连续竞价时段，暂停采集与真实检测。")
        return result
    result.update(state="trading", reason="连续竞价时段；仍须核对每只股票最近成交证据。",
                  session_id=f"{local.date().isoformat()}/{part}",
                  session_end_at=datetime.combine(local.date(), end, CHINA_TIME).isoformat())
    return result


def blocked(code, reason, checked_at=None, session_id=None):
    return {"eligible": False, "code": code, "reason": reason,
            "checked_at": checked_at, "session_id": session_id, "proof": None}


def quote_fingerprint(quote):
    data = quote.to_dict() if isinstance(quote, Quote) else dict(quote)
    data.pop("raw_text", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone required")
    return parsed


def _valid_record(record, context, now):
    source_at, received_at = _timestamp(record["source_reported_at"]), _timestamp(record["received_at"])
    if source_at > received_at or received_at > now:
        return "future_time", "报价时间位于未来，暂停真实检测。"
    if any(not 0 <= (now - instant).total_seconds() <= FRESHNESS_SECONDS
           for instant in (source_at, received_at)):
        return "stale", "成交证据已超过 120 秒，等待新的连续采样。"
    if any(session_context(instant)["session_id"] != context["session_id"]
           for instant in (source_at, received_at)) or record["session_id"] != context["session_id"]:
        return "session_changed", "两次采样必须来自当前同一连续竞价时段。"
    if record["calendar_version"] != context["calendar_version"]:
        return "calendar_changed", "交易日历依据已变化，等待新的连续采样。"
    for name in ("price", "previous_close", "cumulative_volume"):
        value = record[name]
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,8})?", value):
            return "invalid_evidence", "成交证据中的价格或累计量无法核对。"
        if not Decimal(value).is_finite() or Decimal(value) < 0 or (name != "cumulative_volume" and Decimal(value) == 0):
            return "invalid_evidence", "成交证据中的价格或累计量无法核对。"
    if any(not re.fullmatch(r"[a-f0-9]{64}", record[key]) for key in ("quote_hash", "raw_sha256")):
        return "invalid_evidence", "成交证据的报价摘要无法核对。"
    return None


def proof_eligibility(quote, proof, now, watchlist_created_at=None):
    """Recheck an immutable pair at use/send time. No network or database writes."""
    context = session_context(now)
    checked_at = proof.get("checked_at") if isinstance(proof, dict) else None
    def reject(code, reason):
        return blocked(code, reason, checked_at, context["session_id"])
    if context["state"] != "trading":
        return reject(context["state"], context["reason"])
    if not isinstance(proof, dict) or not quote:
        return reject("unverified", "等待同一连续竞价时段内两次有效采样及累计成交量增长。")
    try:
        data = quote.to_dict() if isinstance(quote, Quote) else dict(quote)
        if proof["policy_version"] != POLICY_VERSION or proof["freshness_seconds"] != FRESHNESS_SECONDS:
            return reject("policy_changed", "成交证据策略已变化，等待重新采样。")
        if (proof["session_id"] != context["session_id"] or
                proof["session_end_at"] != context["session_end_at"] or
                proof["calendar_version"] != context["calendar_version"]):
            return reject("session_changed", "成交证据不属于当前连续竞价时段。")
        if not 0 <= (now - _timestamp(proof["checked_at"])).total_seconds() <= FRESHNESS_SECONDS:
            return reject("stale", "成交证据已过时，等待重新采样。")
        if watchlist_created_at is not None and proof["watchlist_created_at"] != watchlist_created_at:
            return reject("watchlist_changed", "自选已删除或重新添加，等待新的采样。")
        previous, current = proof["previous"], proof["current"]
        for record in (previous, current):
            if any(record[key] != data[key] or record[key] != proof[key] for key in ("market", "symbol")):
                return reject("wrong_identity", "成交证据与当前股票身份不一致。")
            if record["watchlist_created_at"] != proof["watchlist_created_at"]:
                return reject("watchlist_changed", "成交证据来自不同的自选记录。")
            reason = _valid_record(record, context, now)
            if reason:
                return reject(*reason)
        if any(previous[key] != current[key] for key in ("source", "source_url", "previous_close")):
            return reject("basis_changed", "来源或昨收基准变化，等待同口径的连续采样。")
        if _timestamp(current["source_reported_at"]) <= _timestamp(previous["source_reported_at"]):
            return reject("source_not_advanced", "来源时间没有前进，暂停真实检测。")
        if _timestamp(current["received_at"]) <= _timestamp(previous["received_at"]):
            return reject("receipt_not_advanced", "接收时间没有前进，暂停真实检测。")
        if Decimal(current["cumulative_volume"]) <= Decimal(previous["cumulative_volume"]):
            return reject("no_recent_trade", "相邻采样累计成交量未增长，尚未观察到最近成交。")
        if quote_fingerprint(data) != current["quote_hash"]:
            return reject("quote_changed", "成交证据与这份报价不一致。")
        if data.get("raw_text") and hashlib.sha256(data["raw_text"].encode("utf-8")).hexdigest() != current["raw_sha256"]:
            return reject("quote_changed", "成交证据与来源原文不一致。")
    except (KeyError, TypeError, ValueError, InvalidOperation, OverflowError):
        return reject("invalid_evidence", "成交证据缺失或无法核对，等待重新采样。")
    return {"eligible": True, "code": "recent_trade_observed",
            "reason": "相邻有效采样的时间和累计量增长，观察到最近成交；非官方停牌状态。",
            "checked_at": checked_at, "session_id": context["session_id"], "proof": proof}


def build_live_evidence(quote, prior, generation, now, *, acquisition=None):
    """Called once for a newly fetched quote, never while displaying cached data."""
    context = session_context(now)
    def result(candidate, eligibility):
        return {"version": POLICY_VERSION, "candidate": candidate, "eligibility": eligibility}
    def reject(code, reason, candidate=None):
        return result(candidate, blocked(code, reason, now.isoformat(), context["session_id"]))
    if context["state"] != "trading":
        return reject(context["state"], context["reason"])
    try:
        # Verify the original source response still agrees with the accepted Quote.
        parsed = parse_quote(quote.raw_text, quote.symbol, _timestamp(quote.received_at))
        if quote_fingerprint(parsed) != quote_fingerprint(quote):
            return reject("invalid_evidence", "报价与来源原文不一致，暂停真实检测。")
        volume = quote.raw_text.split('"')[1].split("~")[6].strip()
        if not re.fullmatch(r"[0-9]{1,20}(?:\.[0-9]{1,8})?", volume):
            return reject("missing_volume", "来源未提供可信累计成交量，暂停真实检测。")
        candidate = {key: getattr(quote, key) for key in (
            "market", "symbol", "source", "source_url", "price", "previous_close", "source_reported_at", "received_at")}
        candidate.update(watchlist_created_at=generation, session_id=context["session_id"],
                         calendar_version=context["calendar_version"], cumulative_volume=volume,
                         quote_hash=quote_fingerprint(quote),
                         raw_sha256=hashlib.sha256(quote.raw_text.encode("utf-8")).hexdigest())
        if acquisition is not None:
            candidate["acquisition"] = dict(acquisition)
        reason = _valid_record(candidate, context, now)
        if reason:
            return reject(*reason)
        previous = prior.get("candidate") if isinstance(prior, dict) and prior.get("version") == POLICY_VERSION else None
        if not previous:
            return reject("warming_up", "已保存一次有效采样，等待下一次来源时间与累计成交量增长。", candidate)
        proof = {"policy_version": POLICY_VERSION, "market": quote.market, "symbol": quote.symbol,
                 "watchlist_created_at": generation, "session_id": context["session_id"],
                 "session_end_at": context["session_end_at"], "calendar_version": context["calendar_version"],
                 "freshness_seconds": FRESHNESS_SECONDS, "checked_at": now.isoformat(),
                 "previous": previous, "current": candidate}
        return result(candidate, proof_eligibility(quote, proof, now, generation))
    except (LookupFailure, KeyError, IndexError, TypeError, ValueError, InvalidOperation, OverflowError):
        return reject("invalid_evidence", "来源原文、时间或成交量无法核对，暂停真实检测。")
