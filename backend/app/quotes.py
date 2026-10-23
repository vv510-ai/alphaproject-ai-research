"""Parse a source quote without inventing numbers, timestamps or market state."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re

from .securities import SOURCE, SOURCE_URL, LookupFailure, fetch_source_text, parse_identity

CHINA_TIME = timezone(timedelta(hours=8))


class QuoteFailure(LookupFailure):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, 502)


@dataclass(frozen=True)
class Quote:
    market: str
    symbol: str
    name: str
    price: str
    previous_close: str
    change: str
    change_pct: str
    source_reported_at: str
    received_at: str
    source: str
    source_url: str
    raw_text: str
    price_unit: str = "元"
    change_pct_unit: str = "%"

    def to_dict(self):
        return asdict(self)


def parse_quote(body: str, symbol: str, received_at: datetime) -> Quote:
    if received_at.tzinfo is None or received_at.utcoffset() is None:
        raise ValueError("received_at must include a timezone")
    identity = parse_identity(body, symbol)
    fields = body.split('"')[1].split("~")
    if len(fields) < 33:
        raise QuoteFailure("missing_fields", "来源报价字段不完整，本次未采用。")
    values = {}
    for name, index in {"price": 3, "previous_close": 4, "change": 31, "change_pct": 32}.items():
        value = fields[index].strip()
        # Limit the grammar and length before Decimal, including NaN and huge exponents.
        if not re.fullmatch(r"-?[0-9]{1,12}(?:\.[0-9]{1,8})?", value):
            raise QuoteFailure("invalid_number", "来源价格或涨跌幅缺失/异常，本次未采用。")
        try:
            values[name] = Decimal(value)
        except InvalidOperation as error:
            raise QuoteFailure("invalid_number", "来源数字无法核对，本次未采用。") from error
    if values["price"] <= 0 or values["previous_close"] <= 0:
        raise QuoteFailure("invalid_price", "来源价格或昨收不可用，不把零值当作有效报价。")
    change = values["price"] - values["previous_close"]
    percent = change / values["previous_close"] * 100
    # Require exact agreement with the supplied prices, then round percentage to
    # two decimal places. A broad tolerance can accept an opposite-signed change.
    expected_percent = percent.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if change != values["change"] or expected_percent != values["change_pct"]:
        raise QuoteFailure("inconsistent_values", "来源价格、昨收和涨跌幅对不上，本次未采用。")
    raw_time = fields[30].strip()
    if not raw_time or raw_time == "-":
        raise QuoteFailure("missing_time", "来源时间缺失，无法判断报价新旧，本次未采用。")
    if not re.fullmatch(r"[0-9]{14}", raw_time):
        raise QuoteFailure("invalid_time", "来源时间格式异常，本次未采用。")
    try:
        source_time = datetime.strptime(raw_time, "%Y%m%d%H%M%S").replace(tzinfo=CHINA_TIME)
    except ValueError as error:
        raise QuoteFailure("invalid_time", "来源日期无效，本次未采用。") from error
    if source_time > received_at:
        raise QuoteFailure("future_time", "来源时间晚于本机接收时间，需核对时钟，本次未采用。")
    return Quote(
        market=identity.market, symbol=symbol, name=identity.name,
        **{key: format(value, "f") for key, value in values.items()},
        source_reported_at=source_time.isoformat(), received_at=received_at.isoformat(),
        source=SOURCE, source_url=SOURCE_URL + identity.market + symbol, raw_text=body,
    )


class TencentQuoteSource:
    def fetch(self, symbol: str) -> Quote:
        body = fetch_source_text(symbol)
        return parse_quote(body, symbol, datetime.now(timezone.utc))
