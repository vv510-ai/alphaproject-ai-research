"""Bounded Tencent daily data adapter; source rows stay traceable to their units."""

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import json
import re
import time

import httpx

from .quotes import CHINA_TIME
from .securities import LookupFailure, market_for

SOURCE = "腾讯证券 / proxy.finance.qq.com"
SOURCE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
AMOUNT_ROUNDING_YUAN = Decimal("50")  # Source amount is reported to 0.01 ten-thousand yuan.


class DailyFailure(LookupFailure):
    def __init__(self, code, message):
        super().__init__(code, message, 502)


@dataclass(frozen=True)
class DailyHistory:
    market: str
    symbol: str
    name: str
    bars: list[dict]
    requested_start: str
    requested_end: str
    earliest_trade_date: str
    latest_trade_date: str
    received_at: str
    source: str
    source_url: str
    raw_volume_unit: str
    volume_multiplier: int
    excluded_outside_range: int
    raw_text: str
    price_unit: str = "元"
    volume_unit: str = "股"
    adjustment: str = "none"
    adjustment_label: str = "不复权"

    def to_dict(self):
        return asdict(self)


def _number(value, label):
    # JSON numeric values are not accepted: preserve source decimal text exactly.
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,13}(?:\.[0-9]{1,8})?", value):
        raise DailyFailure("invalid_daily_number", f"来源{label}缺失或异常，本次未采用。")
    return Decimal(value)


def parse_daily(body: str, symbol: str, start: date, end: date, received_at: datetime) -> DailyHistory:
    market = market_for(symbol)
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must include a timezone")
    today = received_at.astimezone(CHINA_TIME).date()
    if start > end or end >= today:
        raise ValueError("daily request must end before the current Shanghai date")
    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as error:
        raise DailyFailure("invalid_daily_response", "来源没有返回可读取的日线，本次未采用。") from error
    key = market + symbol
    if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
        raise DailyFailure("invalid_daily_response", "来源未确认日线查询成功，本次未采用。")
    data = payload.get("data")
    node = data.get(key) if isinstance(data, dict) else None
    identity = node.get("qt", {}).get(key) if isinstance(node, dict) and isinstance(node.get("qt"), dict) else None
    if not isinstance(identity, list) or len(identity) < 3 or identity[2] != symbol:
        raise DailyFailure("wrong_daily_identity", "来源日线的股票身份无法核对，本次未采用。")
    name = identity[1]
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise DailyFailure("wrong_daily_identity", "来源股票名称异常，本次未采用。")
    rows = node.get("day")
    if not isinstance(rows, list) or not rows or len(rows) > 250:
        # Never silently substitute qfqday/hfqday for an unadjusted request.
        raise DailyFailure("missing_unadjusted_daily", "来源没有可用的不复权日线，本次未采用。")
    factor = 1 if symbol.startswith("688") else 100
    raw_unit = "股" if factor == 1 else "手"
    bars, seen, excluded = [], set(), 0
    for raw in rows:
        if not isinstance(raw, list) or len(raw) < 9:
            raise DailyFailure("missing_daily_fields", "来源日线字段不足，无法核对价格与成交量。")
        day = raw[0]
        if not isinstance(day, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", day):
            raise DailyFailure("invalid_daily_date", "来源交易日期格式异常，本次未采用。")
        try:
            trade_date = date.fromisoformat(day)
        except ValueError as error:
            raise DailyFailure("invalid_daily_date", "来源交易日期无效，本次未采用。") from error
        if day in seen or trade_date > today:
            raise DailyFailure("invalid_daily_date", "来源日期重复或晚于今天，本次未采用。")
        seen.add(day)
        # This endpoint may ignore the date bounds; never include today's partial bar.
        if not start <= trade_date <= end:
            excluded += 1
            continue
        opening, closing, high, low = [_number(raw[i], "价格") for i in (1, 2, 3, 4)]
        if min(opening, closing, high, low) <= 0 or not (low <= min(opening, closing) <= max(opening, closing) <= high):
            raise DailyFailure("invalid_daily_prices", "来源开高低收价格对不上，本次未采用。")
        volume = _number(raw[5], "成交量") * factor
        amount = _number(raw[8], "成交额") * 10000
        if volume != volume.to_integral_value() or volume > Decimal("9000000000000000"):
            raise DailyFailure("invalid_daily_volume", "来源成交量不能换算成有效的整数股，本次未采用。")
        if volume == 0:
            unit_ok = amount == 0
        else:
            unit_ok = amount > 0 and low * volume - AMOUNT_ROUNDING_YUAN <= amount <= high * volume + AMOUNT_ROUNDING_YUAN
        if not unit_ok:
            raise DailyFailure("inconsistent_daily_volume", "来源成交量单位或成交额与日内价格不一致，本次未采用。")
        bars.append({"date": day, "open": str(opening), "close": str(closing), "high": str(high),
                     "low": str(low), "volume": str(int(volume)), "raw_volume": raw[5]})
    if not bars:
        raise DailyFailure("empty_daily_range", "查询区间内没有可用日线，不补造交易记录。")
    bars.sort(key=lambda row: row["date"])
    return DailyHistory(
        market=market, symbol=symbol, name=name.strip(), bars=bars,
        requested_start=start.isoformat(), requested_end=end.isoformat(),
        earliest_trade_date=bars[0]["date"], latest_trade_date=bars[-1]["date"],
        received_at=received_at.astimezone(timezone.utc).isoformat(), source=SOURCE,
        source_url=SOURCE_URL, raw_volume_unit=raw_unit, volume_multiplier=factor,
        excluded_outside_range=excluded, raw_text=body,
    )


class TencentDailySource:
    def fetch(self, symbol: str, start: date, end: date) -> DailyHistory:
        key = market_for(symbol) + symbol
        params = {"param": f"{key},day,{start.isoformat()},{end.isoformat()},200,"}
        started = time.monotonic()
        try:
            with httpx.Client(timeout=httpx.Timeout(8, connect=4), trust_env=False, follow_redirects=False) as client:
                with client.stream("GET", SOURCE_URL, params=params) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > 512 * 1024 or time.monotonic() - started > 10:
                            raise DailyFailure("daily_response_limit", "来源日线过大或等待过久，本次未采用。")
                        chunks.append(chunk)
            body = b"".join(chunks).decode("utf-8")
        except (httpx.HTTPError, UnicodeError) as error:
            raise DailyFailure("daily_source_unavailable", "暂时无法从来源取得日线，请稍后重试。") from error
        return parse_daily(body, symbol, start, end, datetime.now(timezone.utc))
