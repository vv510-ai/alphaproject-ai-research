"""Bounded public feeds, with explicit source attempts and no account credentials.

Adapted API field knowledge (not a whole module) from the existing project's
app/integrations/research_data.py and akshare_adapter.py: Tencent index fields,
CLS cumulative industry pages plus dated bars, and WSCN/Eastmoney calendars.
News/notice field contracts were checked against the installed AKShare wrappers
and public responses. This implementation uses existing httpx, no AkShare import,
global requests monkey-patch, cookie replay or retry loop. Sector bars use three
workers under a provider-wide limit of four concurrent HTTP requests.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import threading
import time
from urllib.parse import urlencode

import httpx

from .research_types import CHINA, SECTIONS, instant, number, stamp, text_value, url_value

SECTION_TIMEOUT_SECONDS = 18
MAX_RESPONSE_BYTES = 3_000_000
INDEX_CODES = ("sh000001", "sz399001", "sz399006")
GLOBAL_CODES = ("usDJI", "usIXIC", "usINX", "hkHSI")


def epoch(value, *, milliseconds=False):
    value = number(value)
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000 if milliseconds else value, timezone.utc).isoformat()
    except (ValueError, OSError, OverflowError):
        return None


def local_timestamp(value):
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)):
        return str(value)  # Keep date precision; never invent midnight publication.
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=CHINA).isoformat() if result.tzinfo is None else result.isoformat()
    except ValueError:
        return None


def new_york_timezone(value):
    # Windows may not have an IANA tzdata package. These public current US
    # quotes use post-2007 DST rules (second Sunday in March, first in November).
    if not 2007 <= value.year <= 2100:
        raise ValueError("Unverified US quote timezone")
    march = datetime(value.year, 3, 8, 2)
    november = datetime(value.year, 11, 1, 2)
    start = march + timedelta(days=(6 - march.weekday()) % 7)
    end = november + timedelta(days=(6 - november.weekday()) % 7)
    return timezone(timedelta(hours=-4 if start <= value < end else -5))


class PublicResearchProvider:
    def __init__(self, *, clock=None, transport=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.transport = transport  # A deterministic httpx transport for contract tests.
        self._local = threading.local()
        self._org_cache = None
        self._network_slots = threading.BoundedSemaphore(4)

    @contextmanager
    def _client_scope(self):
        current = getattr(self._local, "client", None)
        if current is not None:
            yield current
            return
        with httpx.Client(trust_env=False, follow_redirects=False, transport=self.transport) as client:
            self._local.client = client
            try:
                yield client
            finally:
                del self._local.client

    def _read(self, url, params=None, *, data=None, encoding="utf-8"):
        deadline = getattr(self._local, "deadline", time.monotonic() + SECTION_TIMEOUT_SECONDS)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Section time budget exhausted")
        referer = ("https://www.cninfo.com.cn/" if "cninfo.com.cn" in url else
                   "https://www.cls.cn/" if "cls.cn" in url else
                   "https://wallstreetcn.com/" if "awtmt.com" in url else
                   "https://quote.eastmoney.com/" if "eastmoney.com" in url else "https://gu.qq.com/")
        if not self._network_slots.acquire(timeout=remaining):
            raise TimeoutError("Research HTTP concurrency budget exhausted")
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Section time budget exhausted")
            with self._client_scope() as client:
                # POST is used only for CNInfo's public read-only announcement query.
                with client.stream("POST" if data is not None else "GET", url, params=params, data=data,
                                   timeout=httpx.Timeout(min(remaining, 5), connect=min(remaining, 4)),
                                   headers={"User-Agent": "Mozilla/5.0 AlphaProjectLabResearch/1.0", "Referer": referer}) as response:
                    response.raise_for_status()
                    chunks, size = [], 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                            raise TimeoutError("Research response exceeded byte/time budget")
                        chunks.append(chunk)
        finally:
            self._network_slots.release()
        return b"".join(chunks).decode(encoding)

    def _json(self, url, params=None, *, data=None):
        return json.loads(self._read(url, params, data=data))

    @staticmethod
    def _failure(error):
        if isinstance(error, httpx.HTTPStatusError):
            return f"HTTP {error.response.status_code}"
        if isinstance(error, ValueError):
            return text_value(str(error), 180)
        return type(error).__name__  # Do not leak request headers, URLs or system paths.

    def _sources(self, candidates):
        attempts, partial = [], None
        for name, url, fetch in candidates:
            try:
                result = fetch()
                result.update(source=name, source_url=url)
                attempts.append({"source": name, "source_url": url, "status": result.get("status", "available"),
                                 "received_at": stamp(self.clock()), "error_message": result.get("error_message")})
                if result.get("status", "available") == "available":
                    result["source_attempts"] = attempts
                    return result
                if partial is None:
                    partial = result
            except Exception as error:
                attempts.append({"source": name, "source_url": url, "status": "unavailable",
                                 "received_at": stamp(self.clock()), "error_message": self._failure(error)})
        result = partial or {"items": [], "status": "unavailable", "source": " / ".join(c[0] for c in candidates),
                             "source_url": candidates[0][1], "error_message": "本次来源均未取得有效资料。"}
        result["source_attempts"] = attempts
        return result

    def fetch_section(self, key, market=None, symbol=None):
        self._local.deadline = time.monotonic() + SECTION_TIMEOUT_SECONDS
        with self._client_scope():
            return self._fetch_section(key, market, symbol)

    def _fetch_section(self, key, market, symbol):
        if key == "indices":
            return self._sources([
                ("腾讯行情", "https://qt.gtimg.cn/", lambda: self._tencent(INDEX_CODES)),
                ("东方财富", "https://quote.eastmoney.com/center/hszs.html", lambda: self._eastmoney_quotes("i:1.000001,i:0.399001,i:0.399006")),
            ])
        if key == "global":
            return self._sources([
                ("腾讯行情", "https://qt.gtimg.cn/", lambda: self._tencent(GLOBAL_CODES)),
                ("东方财富", "https://quote.eastmoney.com/center/gridlist.html#global_q" , lambda: self._eastmoney_quotes("i:100.DJIA,i:100.SPX,i:100.NDX,i:100.HSI")),
            ])
        if key == "sectors":
            return self._sources([
                ("财联社（日线）", "https://www.cls.cn/plate", lambda: self._cls_sectors(market, symbol)),
                ("东方财富", "https://quote.eastmoney.com/center/boardlist.html#industry_board", lambda: self._em_sectors(market, symbol)),
            ])
        if key == "news":
            candidates = [("财联社", "https://www.cls.cn/telegraph", lambda: self._cls_news(symbol)),
                          ("华尔街见闻", "https://wallstreetcn.com/live/global", lambda: self._wscn_news(symbol))]
            if symbol:
                candidates.insert(0, ("东方财富资讯", f"https://so.eastmoney.com/news/s?keyword={symbol}", lambda: self._stock_news(symbol)))
            return self._sources(candidates)
        if key == "announcements":
            return self._sources([
                ("东方财富公告", "https://data.eastmoney.com/notices/", lambda: self._em_notices(symbol)),
                ("巨潮资讯", "https://www.cninfo.com.cn/", lambda: self._cninfo_notices(symbol)),
            ])
        if key == "calendar":
            return self._calendar(symbol)
        raise ValueError("Unknown research section")

    def _tencent(self, codes):
        url = "https://qt.gtimg.cn/q=" + ",".join(codes)
        body = self._read(url, encoding="gb18030")
        found = {}
        for code, payload in re.findall(r'v_(\w+)="([^"\r\n]*)"', body):
            if code not in codes:
                continue
            fields = payload.split("~")
            if len(fields) < 33 or number(fields[3]) is None or number(fields[32]) is None:
                continue
            try:
                raw = fields[30]
                moment = datetime.strptime(raw, "%Y%m%d%H%M%S") if re.fullmatch(r"\d{14}", raw) else datetime.strptime(raw.replace("/", "-"), "%Y-%m-%d %H:%M:%S")
                moment = moment.replace(tzinfo=new_york_timezone(moment) if code.startswith("us") else CHINA)
                if moment > self.clock() + timedelta(seconds=5):
                    continue
            except ValueError:
                continue
            found[code] = {"code": code, "name": fields[1], "price": number(fields[3]), "change_pct": number(fields[32]),
                           "as_of": moment.isoformat(), "raw_time": raw, "received_at": stamp(self.clock()),
                           "source": "腾讯行情", "source_url": "https://qt.gtimg.cn/q=" + code}
        if not found:
            raise ValueError("腾讯没有返回可核对时间的指数。")
        return {"items": [found[code] for code in codes if code in found], "status": "available" if len(found) == len(codes) else "partial",
                "error_message": None if len(found) == len(codes) else "部分指数缺失或时间异常。"}

    def _eastmoney_rows(self, fs):
        last_error = None
        for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
            try:
                rows, expected = [], None
                for page in range(1, 4):
                    data = self._json(f"https://{host}/api/qt/clist/get", {"fs": fs, "fields": "f12,f14,f2,f3,f124", "fltt": 2,
                                     "invt": 2, "np": 1, "pn": page, "pz": 100, "fid": "f12", "po": 0})
                    payload = data.get("data") if data.get("rc") == 0 else None
                    if not isinstance(payload, dict) or not payload.get("diff"):
                        raise ValueError("东方财富报价列表缺失。")
                    chunk = payload["diff"]
                    rows.extend(list(chunk.values()) if isinstance(chunk, dict) else chunk)
                    expected = int(payload.get("total", len(rows)))
                    if len(rows) >= expected:
                        break
                if len(rows) < expected or len({r.get("f12") for r in rows}) != len(rows):
                    raise ValueError("东方财富分页不完整或重复。")
                return rows
            except Exception as error:
                last_error = error
        raise ValueError("东方财富报价备用节点失败：" + self._failure(last_error)) from last_error

    def _eastmoney_quotes(self, fs):
        rows = []
        for row in self._eastmoney_rows(fs):
            when = epoch(row.get("f124"))
            if not when or not row.get("f12") or not row.get("f14"):
                continue
            rows.append({"code": str(row["f12"]), "name": row["f14"], "price": number(row.get("f2")),
                         "change_pct": number(row.get("f3")), "as_of": when, "raw_time": str(row.get("f124")),
                         "source": "东方财富", "source_url": "https://quote.eastmoney.com/", "received_at": stamp(self.clock())})
        if not rows:
            raise ValueError("东方财富缺少带时间的报价。")
        expected = len(fs.split(",")) if fs.startswith("i:") else len(rows)
        return {"items": rows, "status": "available" if len(rows) == expected else "partial",
                "error_message": None if len(rows) == expected else "备用源部分指数缺失。"}

    def _cls(self, path, params):
        data = self._json("https://x-quote.cls.cn" + path, params)
        if data.get("code") != 200 or data.get("data") is None:
            raise ValueError("财联社返回资料不可用。")
        return data["data"]

    def _industry_universe(self):
        previous = set()
        for page in range(1, 5):
            data = self._cls("/web_quote/plate/plate_list", {"type": "industry", "way": "change", "page": page, "rever": 1})
            rows = data.get("plate_data")
            if not isinstance(rows, list) or not rows:
                raise ValueError("财联社行业列表缺失。")
            codes = [r.get("secu_code") for r in rows]
            if any(not code for code in codes) or len(set(codes)) != len(rows) or (previous and not previous < set(codes)):
                raise ValueError("财联社累计分页重复或不完整。")
            if any(not r.get("secu_name") or number(r.get("change")) is None for r in rows):
                raise ValueError("财联社行业列表字段缺失。")
            if data.get("is_all") == 1:
                return rows
            previous = set(codes)
        raise ValueError("财联社行业列表超过分页上限。")

    def _cls_bar(self, row):
        bars = self._cls("/quote/stock/kline", {"secu_code": row["secu_code"], "type": "fd1", "limit": 2})
        if not isinstance(bars, list) or not bars:
            raise ValueError("财联社行业日线缺失。")
        bar = max(bars, key=lambda b: str(b.get("date", "")))
        day = datetime.strptime(str(bar.get("date")), "%Y%m%d").date()
        if bar.get("secu_code") != row["secu_code"] or day > self.clock().astimezone(CHINA).date():
            raise ValueError("行业日线身份或日期不一致。")
        return {"code": row["secu_code"], "name": row["secu_name"], "industry": row["secu_name"],
                "price": number(bar.get("close_px")), "change_pct": round(number(bar["change"]) * 100, 4) if number(bar.get("change")) is not None else None,
                "as_of": day.isoformat(), "as_of_precision": "date", "raw_time": str(bar["date"]),
                "source": "财联社（日线）", "source_url": "https://www.cls.cn/plate?code=" + row["secu_code"],
                "received_at": stamp(self.clock())}

    def _cls_sectors(self, market, symbol):
        universe = self._industry_universe()
        if symbol:
            detail = self._cls("/v2/quote/a/web/stock/detail", {"secu_code": market + symbol})
            membership = {p.get("secu_code") for p in detail.get("plate", [])}
            selected = [row for row in universe if row["secu_code"] in membership]
            if not selected:
                raise ValueError("未找到可核对的行业归属；概念与地域板块不作行业。")
        else:
            ranked = sorted(universe, key=lambda row: number(row["change"]), reverse=True)
            selected = list({row["secu_code"]: row for row in ranked[:5] + ranked[-5:]}.values())
        rows, failures = [], 0
        deadline = getattr(self._local, "deadline", time.monotonic() + SECTION_TIMEOUT_SECONDS)
        def fetch_batch(batch):
            self._local.deadline = deadline
            found, failed = [], 0
            with self._client_scope():
                for row in batch:
                    try:
                        found.append(self._cls_bar(row))
                    except Exception:
                        failed += 1
            return found, failed
        # Three sector-bar workers share the provider-wide four-HTTP semaphore.
        # The source timestamp must come from each bar, never the undated ranking.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="research-sector") as pool:
            futures = [pool.submit(fetch_batch, selected[index::3]) for index in range(min(3, len(selected)))]
            for future in futures:
                found, failed = future.result()
                failures += failed
                for item in found:
                    if symbol:
                        item.update(symbol=symbol, market=market)
                    rows.append(item)
        if not rows:
            raise ValueError("所选行业均没有可核对的日线。")
        mixed = len({row["as_of"] for row in rows}) != 1
        return {"items": sorted(rows, key=lambda r: r["change_pct"] if r["change_pct"] is not None else float("-inf"), reverse=True),
                "status": "partial" if failures or mixed else "available", "coverage": "个股行业成员" if symbol else "行业列表涨跌各前五；展示带日期日线",
                "error_message": "部分行业日线缺失或日期不同，勿视作完整同日排名。" if failures or mixed else None}

    def _em_sectors(self, market, symbol):
        rows = self._eastmoney_quotes("m:90 t:2 f:!50")["items"]
        if symbol:
            result = self._json("https://datacenter.eastmoney.com/securities/api/data/v1/get", {
                "reportName": "RPT_F10_ORG_BASICINFO", "columns": "SECURITY_CODE,BOARD_NAME_2LEVEL",
                "filter": f'(SECUCODE="{symbol}.{market.upper()}")'})
            company = (result.get("result") or {}).get("data") or []
            name = company[0].get("BOARD_NAME_2LEVEL") if company else None
            rows = [row for row in rows if name and row["name"] == name]
            if not rows:
                raise ValueError("备用源没有匹配的行业与报价。")
            for row in rows:
                row.update(symbol=symbol, market=market, industry=name)
        else:
            ranked = sorted(rows, key=lambda r: r["change_pct"] if r["change_pct"] is not None else float("-inf"), reverse=True)
            rows = list({r["code"]: r for r in ranked[:5] + ranked[-5:]}.values())
        return {"items": rows}

    def _cls_news(self, symbol):
        params = {"app": "CailianpressWeb", "category": "", "last_time": int(self.clock().timestamp()),
                  "os": "web", "refresh_type": "1", "rn": "30", "sv": "8.4.6"}
        params["sign"] = hashlib.md5(hashlib.sha1(urlencode(params).encode()).hexdigest().encode()).hexdigest()
        response = self._json("https://www.cls.cn/v1/roll/get_roll_list", params)
        records = (response.get("data") or {}).get("roll_data")
        if response.get("errno") != 0 or not isinstance(records, list):
            raise ValueError("财联社电报返回不可用。")
        items = []
        for row in records:
            related = [str(r.get("StockID", ""))[-6:] for r in row.get("stock_list", [])]
            if symbol and symbol not in related:
                continue
            items.append({"upstream_id": str(row.get("id") or ""), "title": row.get("title") or text_value(row.get("content"), 160),
                          "summary": row.get("content"), "url": "https://www.cls.cn/detail/" + str(row["id"]),
                          "published_at": epoch(row.get("ctime")), "raw_time": str(row.get("ctime")),
                          "received_at": stamp(self.clock()), "related_symbols": related, "source": "财联社", "source_url": "https://www.cls.cn/telegraph"})
        return {"items": items, "status": "partial" if symbol else "available",
                "coverage": "最新30条电报中的明确股票关联" if symbol else "最新30条财经电报",
                "error_message": "备用资讯只覆盖最新电报中的明确股票关联，不代表全部个股新闻。" if symbol else None}

    def _wscn_news(self, symbol):
        data = self._json("https://api-one-wscn.awtmt.com/apiv1/content/lives", {"channel": "global-channel", "limit": 30, "client": "pc"})
        records = (data.get("data") or {}).get("items")
        if data.get("code") != 20000 or not isinstance(records, list):
            raise ValueError("华尔街见闻快讯缺失。")
        items = []
        for row in records:
            symbols = row.get("symbols") or []
            related = [str(r.get("symbol", r.get("code", "")))[:6] if isinstance(r, dict) else str(r)[:6] for r in symbols]
            if symbol and symbol not in related:
                continue
            items.append({"upstream_id": str(row.get("id") or ""), "title": row.get("title") or text_value(row.get("content_text"), 160),
                          "summary": row.get("content_text"), "url": row.get("uri"), "published_at": epoch(row.get("display_time")),
                          "received_at": stamp(self.clock()), "related_symbols": related, "source": "华尔街见闻", "source_url": "https://wallstreetcn.com/live/global"})
        return {"items": items, "status": "partial" if symbol else "available", "coverage": "最新30条快讯中的明确股票关联" if symbol else "最新30条全球快讯"}

    def _stock_news(self, symbol):
        payload = {"uid": "", "keyword": symbol, "type": ["cmsArticleWebOld"], "client": "web", "clientType": "web", "clientVersion": "curr",
                   "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "default", "pageIndex": 1, "pageSize": 20, "preTag": "", "postTag": ""}}}
        body = self._read("https://search-api-web.eastmoney.com/search/jsonp", {"cb": "research_callback", "param": json.dumps(payload, ensure_ascii=False)})
        match = re.fullmatch(r"\s*research_callback\((.*)\);?\s*", body, re.S)
        if not match:
            raise ValueError("个股资讯返回格式不可用。")
        data = json.loads(match[1])
        records = (data.get("result") or {}).get("cmsArticleWebOld")
        if not isinstance(records, list):
            raise ValueError("个股资讯条目缺失。")
        return {"items": [{"upstream_id": row.get("code"), "title": row.get("title"), "summary": row.get("content"),
                           "url": f"https://finance.eastmoney.com/a/{row.get('code')}.html", "published_at": local_timestamp(row.get("date")),
                           "received_at": stamp(self.clock()), "symbol": symbol, "related_symbols": [symbol], "scope_kind": "source_keyword_search",
                           "source": "东方财富资讯", "source_url": f"https://so.eastmoney.com/news/s?keyword={symbol}"} for row in records],
                "coverage": "来源按股票代码检索的20条新闻，按相关性返回，不保证覆盖最新发布"}

    def _em_notices(self, symbol):
        today = self.clock().astimezone(CHINA).date()
        params = {"sr": "-1", "page_size": 50, "page_index": 1, "ann_type": "A", "client_source": "web", "f_node": "0", "s_node": "0",
                  "begin_time": (today - timedelta(days=14 if symbol else 1)).isoformat(), "end_time": today.isoformat()}
        if symbol:
            params["stock_list"] = symbol
        data = self._json("https://np-anotice-stock.eastmoney.com/api/security/ann", params)
        records = (data.get("data") or {}).get("list")
        if not isinstance(records, list):
            raise ValueError("公告来源没有返回条目列表。")
        items = []
        for row in records:
            codes = [str(code.get("stock_code")) for code in row.get("codes", [])]
            if symbol and symbol not in codes:
                continue
            code = symbol or next((code for code in codes if re.fullmatch(r"\d{6}", code)), None)
            if not code or not row.get("art_code"):
                continue
            when = row.get("display_time") or row.get("notice_date")
            items.append({"upstream_id": row["art_code"], "title": row.get("title"), "summary": None,
                          "url": f"https://data.eastmoney.com/notices/detail/{code}/{row['art_code']}.html",
                          "published_at": local_timestamp(when), "raw_time": when, "received_at": stamp(self.clock()),
                          "source": "东方财富公告", "source_url": "https://data.eastmoney.com/notices/", "symbol": code, "related_symbols": codes})
        total = (data.get("data") or {}).get("total_hits", len(items))
        return {"items": items, "total_available": total, "truncated": int(total) > len(records), "coverage": "最近14日最多50条个股公告" if symbol else "最近两日最新50条全市场公告"}

    def _cninfo_notices(self, symbol):
        today = self.clock().astimezone(CHINA).date()
        stock = ""
        if symbol:
            if self._org_cache is None:
                listing = self._json("https://www.cninfo.com.cn/new/data/szse_stock.json").get("stockList")
                if not isinstance(listing, list):
                    raise ValueError("巨潮股票身份列表缺失。")
                self._org_cache = {str(row.get("code")): row.get("orgId") for row in listing}
            org = self._org_cache.get(symbol)
            if not org:
                raise ValueError("巨潮未确认该股票的公告主体。")
            stock = f"{symbol},{org}"
        data = self._json("https://www.cninfo.com.cn/new/hisAnnouncement/query", data={
            "stock": stock, "tabName": "fulltext", "pageSize": 50, "pageNum": 1, "column": "szse",
            "seDate": (today - timedelta(days=14 if symbol else 1)).isoformat() + "~" + today.isoformat()})
        total = data.get("totalAnnouncement")
        records = data.get("announcements")
        if records is None and total == 0:
            records = []
        if not isinstance(records, list) or total is None:
            raise ValueError("巨潮未返回有效公告查询结果。")
        items = []
        for row in records:
            if symbol and row.get("secCode") != symbol:
                continue
            path = str(row.get("adjunctUrl") or "")
            url = "https://static.cninfo.com.cn/" + path if re.fullmatch(r"finalpage/[0-9-]+/[A-Za-z0-9_.-]+\.[pP][dD][fF]", path) else None
            items.append({"upstream_id": row.get("announcementId"), "title": row.get("announcementTitle"), "summary": None,
                          "url": url, "published_at": epoch(row.get("announcementTime"), milliseconds=True),
                          "raw_time": str(row.get("announcementTime")), "received_at": stamp(self.clock()),
                          "source": "巨潮资讯", "source_url": "https://www.cninfo.com.cn/", "symbol": row.get("secCode"), "related_symbols": [row.get("secCode")]})
        return {"items": items, "total_available": total, "truncated": bool(data.get("hasMore")),
                "coverage": "最近14日最多50条个股公告" if symbol else "最近两日最新50条全市场公告"}

    def _calendar(self, symbol):
        attempts, items = [], []
        for name, url, call in [("华尔街见闻", "https://wallstreetcn.com/calendar", self._macro),
                                ("东方财富预约披露", "https://data.eastmoney.com/bbsj/", lambda: self._earnings(symbol))]:
            try:
                result = call()
                items.extend(result)
                attempts.append({"source": name, "source_url": url, "status": "available", "received_at": stamp(self.clock()), "error_message": None})
            except Exception as error:
                attempts.append({"source": name, "source_url": url, "status": "unavailable", "received_at": stamp(self.clock()), "error_message": self._failure(error)})
        success = sum(a["status"] == "available" for a in attempts)
        return {"source": "华尔街见闻 / 东方财富预约披露", "source_url": SECTIONS["calendar"][2], "items": items,
                "status": "available" if success == 2 else "partial" if success else "unavailable", "source_attempts": attempts,
                "error_message": None if success == 2 else "部分日历来源未取到；经济事件与财报预约不能相互替代。",
                "coverage": "今日经济事件与财报预约；计划时间不等于发布时间"}

    def _macro(self):
        start = self.clock().astimezone(CHINA).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        data = self._json("https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas", {"start": int(start.timestamp()), "end": int(end.timestamp())})
        records = (data.get("data") or {}).get("items")
        if data.get("code") != 20000 or not isinstance(records, list):
            raise ValueError("经济日历条目缺失。")
        items = []
        for row in records:
            when = epoch(row.get("public_date"))
            if not when or not start <= instant(when) < end or not row.get("title"):
                continue
            actual = number(row.get("actual"))
            # public_date is the scheduled release time. Without a released value,
            # treating it as publication would manufacture a factual timestamp.
            published = when if actual is not None and instant(when) <= self.clock() else None
            items.append({"upstream_id": str(row.get("id") or row.get("calendar_key") or ""), "title": f"{row.get('country', '')} {row['title']}",
                          "summary": None, "url": row.get("uri") or "https://wallstreetcn.com/calendar", "scheduled_at": when,
                          "published_at": published, "raw_time": str(row.get("public_date")), "event_type": "economic_release",
                          "actual": actual if published else None, "forecast": number(row.get("forecast")), "previous": number(row.get("previous")),
                          "unit": row.get("unit"), "quantity": row.get("quantity"), "observation_date": row.get("observation_date"),
                          "importance": number(row.get("importance")), "country": row.get("country"), "received_at": stamp(self.clock()),
                          "source": "华尔街见闻", "source_url": "https://wallstreetcn.com/calendar"})
        return items

    def _earnings(self, symbol):
        today = self.clock().astimezone(CHINA).date().isoformat()
        expression = f"(APPOINT_PUBLISH_DATE='{today}')" + (f'(SECURITY_CODE="{symbol}")' if symbol else "")
        records, seen = [], set()
        for page in range(1, 6):
            data = self._json("https://datacenter-web.eastmoney.com/api/data/v1/get", {"reportName": "RPT_PUBLIC_BS_APPOIN", "columns": "ALL",
                              "pageSize": 500, "pageNumber": page, "filter": expression, "sortColumns": "REPORT_DATE,SECURITY_CODE", "sortTypes": "-1,1"})
            result = data.get("result")
            if not data.get("success") or not isinstance(result, dict) or not isinstance(result.get("data"), list):
                raise ValueError("财报预约日历不可用，不能当作没有预约。")
            pages = int(result.get("pages", 1))
            if pages > 5:
                raise ValueError("今日预约超过2500条抓取上限，未声称完整覆盖。")
            for row in result["data"]:
                identity = (row.get("SECURITY_CODE"), row.get("REPORT_DATE"))
                if identity in seen:
                    raise ValueError("财报预约分页出现重复记录，未采用。")
                seen.add(identity)
                records.append(row)
            if page >= pages:
                break
        items = []
        for row in records:
            # Actual disclosure supersedes appointments; the effective latest
            # changed appointment supersedes the first date.
            effective = next((row.get(k) for k in ("ACTUAL_PUBLISH_DATE", "APPOINT_PUBLISH_DATE", "THIRD_CHANGE_DATE", "SECOND_CHANGE_DATE", "FIRST_CHANGE_DATE", "FIRST_APPOINT_DATE") if row.get(k)), None)
            if not effective or str(effective)[:10] != today or (symbol and row.get("SECURITY_CODE") != symbol):
                continue
            code = str(row.get("SECURITY_CODE", ""))
            period = str(row.get("REPORT_DATE", ""))[:10]
            items.append({"upstream_id": f"{code}:{period}", "title": f"{row.get('SECURITY_NAME_ABBR', code)} 财报披露预约",
                          "summary": f"报告期 {period}；预约日期 {today}，是否已披露须核对公告。", "url": f"https://data.eastmoney.com/bbsj/{code}.html",
                          "published_at": None, "scheduled_at": today, "event_type": "earnings_appointment", "symbol": code, "related_symbols": [code],
                          "report_period": period, "received_at": stamp(self.clock()), "source": "东方财富预约披露", "source_url": "https://data.eastmoney.com/bbsj/"})
        return items
