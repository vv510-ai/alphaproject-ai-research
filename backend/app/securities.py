"""Resolve stock identity. Quotes are not stored or displayed in this lesson."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import re
import time

import httpx

SOURCE = "腾讯行情 / qt.gtimg.cn"
SOURCE_URL = "https://qt.gtimg.cn/q="


class LookupFailure(Exception):
    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def market_for(symbol: str) -> str:
    if not re.fullmatch(r"[0-9]{6}", symbol):
        raise LookupFailure("invalid_symbol", "请输入 6 位股票代码，例如 600519。", 400)
    if symbol.startswith(("600", "601", "603", "605", "688")):
        return "sh"
    if symbol.startswith(("000", "001", "002", "003", "300", "301")):
        return "sz"
    raise LookupFailure("unsupported_symbol", "目前支持已列明前缀的沪深 A 股，请换一个股票代码。", 400)


@dataclass(frozen=True)
class Security:
    symbol: str
    market: str
    name: str
    source: str
    verified_at: str

    def to_dict(self) -> dict:
        return asdict(self)


def parse_identity(body: str, symbol: str) -> Security:
    market = market_for(symbol)
    if body.strip() in ('v_pv_none_match="1";', 'v_pv_none_match="1"'):
        raise LookupFailure("not_found", "没有查到这个股票代码，请核对后重试。", 404)
    match = re.fullmatch(r'\s*v_([a-z]{2}[0-9]{6})="([^"\r\n]*)";?\s*', body)
    if not match or match[1] != market + symbol:
        raise LookupFailure("invalid_source", "来源返回的股票身份无法核对，请稍后重试。", 503)
    fields = match[2].split("~")
    if len(fields) < 3 or fields[2] != symbol:
        raise LookupFailure("invalid_source", "来源返回的股票代码不一致，本次未采用。", 503)
    name = fields[1].strip()
    if not name or len(name) > 64 or any(ord(char) < 32 for char in name):
        raise LookupFailure("invalid_source", "来源未提供有效股票名称，请稍后重试。", 503)
    return Security(symbol, market, name, SOURCE, datetime.now(timezone.utc).isoformat())


def fetch_source_text(symbol: str) -> str:
    market = market_for(symbol)
    try:
        # Keep this client separate from the earlier experiment's monkeypatch.
        with httpx.Client(
            timeout=httpx.Timeout(8, connect=4), trust_env=False, follow_redirects=False
        ) as client:
            started = time.monotonic()
            with client.stream(
                "GET", SOURCE_URL + market + symbol,
                headers={"User-Agent": "AlphaProjectLab/0.2"},
            ) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 16_384 or time.monotonic() - started > 10:
                        raise LookupFailure("invalid_source", "来源响应异常或读取过久，请稍后重试。", 503)
                    chunks.append(chunk)
        return b"".join(chunks).decode("gb18030")
    except (httpx.HTTPError, UnicodeError) as error:
        raise LookupFailure(
            "source_unavailable", "暂时无法从来源取得数据，请稍后重试；已有记录仍可查看。", 503
        ) from error


class TencentResolver:
    def resolve(self, symbol: str) -> Security:
        return parse_identity(fetch_source_text(symbol), symbol)
