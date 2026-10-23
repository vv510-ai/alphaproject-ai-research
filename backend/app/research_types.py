"""Small, JSON-compatible contracts for persisted public research evidence.

The stable-key/content-fingerprint pattern was informed by the reviewed
a-share-research-monitor (87940e4c) design; no similarity code is copied.
Digits, negation and punctuation are never discarded to infer equivalence.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import html
import json
import math
import re
from urllib.parse import urlsplit, urlunsplit

UTC = timezone.utc
CHINA = timezone(timedelta(hours=8))
SECTIONS = {
    "indices": ("A 股指数", "腾讯行情 / 东方财富", "https://gu.qq.com/"),
    "global": ("全球市场", "腾讯行情 / 东方财富", "https://gu.qq.com/"),
    "sectors": ("行业板块", "财联社 / 东方财富", "https://www.cls.cn/plate"),
    "news": ("财经资讯", "财联社 / 东方财富 / 华尔街见闻", "https://www.cls.cn/telegraph"),
    "announcements": ("公司公告", "东方财富", "https://data.eastmoney.com/notices/"),
    "calendar": ("经济与披露日历", "华尔街见闻 / 东方财富", "https://wallstreetcn.com/calendar"),
}
QUOTE_SECTIONS = {"indices", "global", "sectors"}
MAX_ITEMS = 100
# Display freshness is deliberately separate from the click cooldown and cache reuse.
STALE_AFTER = {"indices": 36 * 3600, "global": 96 * 3600, "sectors": 5 * 86400,
               "news": 3 * 86400, "announcements": 14 * 86400, "calendar": 6 * 3600}


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def stamp(value):
    return instant(value, strict=True).astimezone(UTC).isoformat(timespec="microseconds")


def instant(value, *, strict=False):
    """Date-only observations become end-of-day solely for conservative comparisons."""
    try:
        if isinstance(value, datetime):
            result = value
        elif isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            if strict:
                raise ValueError("A timezone-aware timestamp is required")
            return datetime.combine(datetime.fromisoformat(value).date(), time.max, CHINA)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError("Invalid timestamp")
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("A timezone-aware timestamp is required")
        return result
    except (ValueError, TypeError, OverflowError):
        if strict:
            raise ValueError("A timezone-aware timestamp is required") from None
        return None


def text_value(value, limit=2000):
    if value is None:
        return None
    # Plain text, not executable markup; the actual wording remains untrusted evidence.
    value = html.unescape(str(value))
    # Strip actual markup, not comparisons such as "利润<100，收入>200".
    value = re.sub(r"</?[A-Za-z][A-Za-z0-9:_-]*(?:\s[^<>]*?)?\s*/?>", "", value)
    value = "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127))
    return " ".join(value.split())[:limit] or None


def url_value(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return None
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    except ValueError:
        return None


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def empty_section(key, message="尚无已保存资料，请刷新后查看。"):
    title, source, source_url = SECTIONS[key]
    return {"key": key, "title": title, "status": "unavailable", "source": source,
            "source_url": source_url, "as_of": None, "received_at": None,
            "error_message": message, "items": [], "missing_count": 0, "filtered_count": 0}


def identity(key, row):
    source = row.get("source") or "unknown"
    if key in QUOTE_SECTIONS:
        anchor = [source, row.get("code"), row.get("name") if not row.get("code") else None]
    else:
        anchor = [source, row.get("upstream_id") or row.get("url") or
                  [row.get("title"), row.get("published_at"), row.get("scheduled_at")]]
    return "r_" + hashlib.sha256(encoded([key, anchor]).encode()).hexdigest()[:28]


def fingerprint(row):
    ignored = {"id", "received_at", "change_kind", "content_fingerprint", "is_stale",
               "missing_fields", "carried_forward", "first_seen_at", "last_seen_at"}
    return hashlib.sha256(encoded({k: v for k, v in row.items() if k not in ignored}).encode()).hexdigest()


def normalize_item(key, raw, source, source_url, received_at):
    if not isinstance(raw, dict):
        return None
    item = {"source": text_value(raw.get("source") or source, 120),
            "source_url": url_value(raw.get("source_url") or source_url),
            "received_at": stamp(raw["received_at"]) if instant(raw.get("received_at")) else received_at}
    if key in QUOTE_SECTIONS:
        item.update(name=text_value(raw.get("name"), 120), code=text_value(raw.get("code"), 48),
                    price=number(raw.get("price")), change_pct=number(raw.get("change_pct")),
                    as_of=raw.get("as_of") if instant(raw.get("as_of")) else None)
        if not item["name"] or not item["code"]:
            return None
        if item["price"] is not None and item["price"] <= 0:
            item["price"] = None
    else:
        item.update(title=text_value(raw.get("title"), 500), summary=text_value(raw.get("summary"), 4000),
                    url=url_value(raw.get("url")), published_at=raw.get("published_at")
                    if instant(raw.get("published_at")) else None,
                    upstream_id=text_value(raw.get("upstream_id"), 160))
        if not item["title"]:
            return None
        # Display truncation must not turn a numeric/negation edit near the end
        # of a long source article into an "unchanged" fingerprint.
        item["full_content_digest"] = hashlib.sha256(encoded({name: text_value(raw.get(name), 3_000_000)
            for name in ("title", "summary")}).encode()).hexdigest()
    for name in ("symbol", "market", "country", "raw_time", "as_of_precision", "published_at_precision",
                 "industry", "event_type", "report_period", "scope_kind", "unit", "quantity", "observation_date"):
        if raw.get(name) is not None:
            item[name] = text_value(raw[name], 200)
    for name in ("actual", "forecast", "previous", "importance"):
        if name in raw:
            item[name] = number(raw[name])
    if instant(raw.get("scheduled_at")):
        item["scheduled_at"] = raw["scheduled_at"]
    if isinstance(raw.get("related_symbols"), list):
        item["related_symbols"] = [str(s) for s in raw["related_symbols"][:20] if re.fullmatch(r"\d{6}", str(s))]
    item["id"] = identity(key, item)
    item["content_fingerprint"] = fingerprint(item)
    item["change_kind"] = "new"
    return item


def freshness(section, now):
    """A newly received stale quote remains stale, including after a successful fetch."""
    key = section["key"]
    stale_count, missing_count = 0, 0
    for item in section["items"]:
        field = "as_of" if key in QUOTE_SECTIONS else "published_at"
        observed = instant(item.get(field))
        received = instant(item.get("received_at"))
        required = ("source", "source_url", "as_of", "price", "change_pct") if key in QUOTE_SECTIONS else (
            "source", "url", "published_at")
        item["missing_fields"] = [name for name in required if item.get(name) is None]
        age = (now - observed).total_seconds() if observed else None
        # Calendar schedules are not publication times. Their collection age governs display freshness.
        if key == "calendar":
            age = (now - received).total_seconds() if received else None
        item["is_stale"] = age is not None and age > STALE_AFTER[key]
        if observed and observed > now and not (isinstance(item.get(field), str) and len(item[field]) == 10):
            item["missing_fields"].append("verified_time")
        stale_count += int(item["is_stale"])
        missing_count += bool(item["missing_fields"])
    section["stale_count"] = stale_count
    section["missing_count"] = missing_count
    if section["status"] == "unavailable":
        return section
    if section["items"] and stale_count == len(section["items"]):
        section["status"] = "stale"
    elif section["status"] != "stale" and (stale_count or missing_count):
        section["status"] = "partial"
    # A successfully empty list also expires instead of remaining fresh forever.
    received = instant(section.get("received_at"))
    if received and (now - received).total_seconds() > STALE_AFTER[key]:
        section["status"] = "stale"
    return section


def _immutable(*args, **kwargs):
    raise TypeError("Research evidence is frozen; make a JSON copy to edit it")


class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _immutable
    def __deepcopy__(self, memo):
        return self


class FrozenList(list):
    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = reverse = sort = __iadd__ = __imul__ = _immutable
    def __deepcopy__(self, memo):
        return self


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze(item) for item in value)
    return value
