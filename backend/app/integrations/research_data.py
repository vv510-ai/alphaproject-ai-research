"""Bounded public research feeds. No credentials, fabricated values or stale fallback.

Field contracts follow public CLS website, Eastmoney and WallstreetCN feeds. Each
successful result carries its source and timestamp; failures propagate to the
existing missing-data handling. Cache keys include the Shanghai calendar day.
"""
from __future__ import annotations

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import RLock
from zoneinfo import ZoneInfo

import requests

SHANGHAI = ZoneInfo("Asia/Shanghai")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


class ResearchData:
    def __init__(self, *, get_json=None, get_text=None, now=None, clock=time.monotonic):
        self._get_json = get_json or self._request
        self._get_text = get_text or self._request_text
        self._now = now or (lambda: datetime.now(SHANGHAI))
        self._clock = clock
        self._cache = {}
        self._failures = {}
        self._lock = RLock()

    @staticmethod
    def _request_text(url):
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, timeout=(4, 8))
            response.raise_for_status()
            response.encoding = "gb18030"
            return response.text

    @staticmethod
    def _request(url, params):
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, params=params, timeout=(4, 8),
                headers={"Referer": "https://www.cls.cn/" if "cls.cn" in url else
                         "https://quote.eastmoney.com/" if "eastmoney.com" in url else "https://wallstreetcn.com/"})
            response.raise_for_status()
            return response.json()

    def _cached(self, key, fetch, ttl=300):
        key = (self._now().date().isoformat(), key)
        # One shared cache prevents repeated scans hitting upstream per stock.
        with self._lock:
            hit = self._cache.get(key)
            if hit and self._clock() - hit[0] < ttl:
                return hit[1]
            failure = self._failures.get(key)
            if failure and self._clock() - failure < 60:
                raise RuntimeError("Upstream temporarily unavailable; retry after cooldown")
        try:
            value = fetch()  # Never cache a failure as a successful empty result.
        except Exception:
            with self._lock:
                self._failures[key] = self._clock()
            raise
        with self._lock:
            self._cache = {k: v for k, v in self._cache.items() if k[0] == key[0]}
            self._cache[key] = (self._clock(), value)
            self._failures.pop(key, None)
        return value

    def _quote(self, path, params):
        error = None
        for host in ("push2delay.eastmoney.com", "push2.eastmoney.com"):
            try:
                data = self._get_json(f"https://{host}/api/qt/{path}/get", params)
                if data.get("rc") != 0 or not data.get("data"):
                    raise ValueError("Empty Eastmoney quote response")
                return data["data"]
            except (requests.RequestException, ValueError, KeyError) as exc:
                error = exc
        raise RuntimeError("Eastmoney quote unavailable") from error

    def _rows(self, fs, *, limit=200):
        rows = []
        for page in range(1, 7):
            data = self._quote("clist", {"fs": fs, "fields": "f12,f14,f2,f3,f124",
                "fltt": 2, "invt": 2, "np": 1, "pn": page, "pz": limit, "fid": "f12", "po": 0})
            chunk = data.get("diff")
            if isinstance(chunk, dict):
                chunk = list(chunk.values())
            if not isinstance(chunk, list) or not chunk:
                raise ValueError("Missing quote rows")
            rows.extend(chunk)
            if len(rows) >= data.get("total", len(rows)):
                break
        else:
            raise ValueError("Truncated quote universe")
        if len({row['f12'] for row in rows}) != len(rows):
            raise ValueError("Repeated quote page")
        result = []
        for row in rows:
            pct = number(row.get("f3"))
            stamp = number(row.get("f124"))
            if pct is None or not row.get("f14") or stamp is None:
                continue
            updated = datetime.fromtimestamp(stamp, SHANGHAI)
            if self._now() - updated > timedelta(days=7):
                continue
            result.append({"code": row["f12"], "name": row["f14"],
                "price": number(row.get("f2")), "change_pct": pct,
                "as_of": updated.isoformat(), "source": "东方财富"})
        if not result:
            raise ValueError("No recent valid quote rows")
        return result

    def global_indices(self):
        def fetch():
            try:
                text = self._get_text("https://qt.gtimg.cn/q=usDJI,usIXIC,usINX,hkHSI")
                rows = []
                for symbol, payload in re.findall(r'v_(\w+)="([^"]*)"', text):
                    fields = payload.split("~")
                    if len(fields) < 33:
                        continue
                    price, pct = number(fields[3]), number(fields[32])
                    zone = ZoneInfo("America/New_York") if symbol.startswith("us") else SHANGHAI
                    updated = datetime.strptime(fields[30].replace("/", "-"), "%Y-%m-%d %H:%M:%S").replace(tzinfo=zone)
                    if price is None or pct is None or self._now() - updated > timedelta(days=7):
                        continue
                    rows.append({"code": symbol, "name": fields[1], "price": price,
                        "change_pct": pct, "as_of": updated.astimezone(SHANGHAI).isoformat(), "source": "腾讯行情"})
                if len(rows) != 4:
                    raise ValueError("Incomplete global quote response")
                return rows
            except (requests.RequestException, ValueError):
                return self._rows("i:100.DJIA,i:100.SPX,i:100.NDX,i:100.HSI")
        return self._cached("global", fetch)

    def _cls(self, path, params):
        response = self._get_json("https://x-quote.cls.cn" + path, params)
        if response.get("code") != 200 or response.get("data") is None:
            raise ValueError("Invalid CLS response")
        return response["data"]

    def sectors(self):
        """CLS industry universe, including its first/second-level industries.

        These undated list values are only used for selection. Published returns
        and dates always come together from each sector's dated daily bar.
        """
        def fetch():
            previous_count = 0
            for page in range(1, 7):
                data = self._cls("/web_quote/plate/plate_list", {
                    "type": "industry", "way": "change", "page": page, "rever": 1})
                chunk = data.get("plate_data")
                if not isinstance(chunk, list) or not chunk:
                    raise ValueError("Missing CLS industry rows")
                # CLS returns a cumulative snapshot (30, then all 54), not offsets.
                if len(chunk) <= previous_count:
                    raise ValueError("Repeated CLS industry page")
                previous_count = len(chunk)
                rows, seen = [], set()
                for row in chunk:
                    code, name, pct = row.get("secu_code"), row.get("secu_name"), number(row.get("change"))
                    if not code or not name or pct is None:
                        raise ValueError("Invalid CLS industry row")
                    if code in seen:
                        raise ValueError("Repeated CLS industry page")
                    seen.add(code)
                    rows.append({"code": code, "name": name, "change_pct": pct * 100})
                if data.get("is_all") == 1:
                    return rows
            raise ValueError("Truncated CLS industry universe")
        return self._cached("sectors", fetch)

    def _sector_bar(self, row):
        def fetch():
            bars = self._cls("/quote/stock/kline", {
                "secu_code": row["code"], "type": "fd1", "limit": 2})
            if not isinstance(bars, list) or not bars:
                raise ValueError("Missing CLS sector bars")
            bar = max(bars, key=lambda b: int(b["date"]))
            day = datetime.strptime(str(bar["date"]), "%Y%m%d").date()
            age = (self._now().date() - day).days
            price, pct = number(bar.get("close_px")), number(bar.get("change"))
            if not 0 <= age <= 7 or pct is None or price is None or price <= 0:
                raise ValueError("Invalid or stale CLS sector bar")
            if bar.get("secu_code") != row["code"]:
                raise ValueError("Mismatched CLS sector bar")
            return {"code": row["code"], "name": row["name"], "price": price,
                    "change_pct": round(pct * 100, 4), "as_of": day.isoformat(),
                    "source": "财联社（日线）"}
        return self._cached("sector-bar:" + row["code"], fetch)

    def sector_ranking(self):
        def fetch():
            try:
                rows = sorted(self.sectors(), key=lambda row: row["change_pct"], reverse=True)
                selected = list({row["code"]: row for row in rows[:5] + rows[-5:]}.values())
                with ThreadPoolExecutor(max_workers=4) as pool:
                    result = list(pool.map(self._sector_bar, selected))
                if len({row["as_of"] for row in result}) != 1:
                    raise ValueError("Mixed CLS sector trading dates")
                return sorted(result, key=lambda row: row["change_pct"], reverse=True)
            except (requests.RequestException, ValueError, KeyError, RuntimeError):
                rows = sorted(self._rows("m:90 t:2 f:!50"), key=lambda row: row["change_pct"], reverse=True)
                return list({row["code"]: row for row in rows[:5] + rows[-5:]}.values())
        return self._cached("sector-ranking", fetch)

    def sector_info(self, code):
        if len(code) != 6 or not code.isdigit():
            raise ValueError("Invalid stock code")
        def fetch():
            prefix = "sh" if code.startswith("6") else "bj" if code.startswith(("4", "8", "9")) else "sz"
            detail = self._cls("/v2/quote/a/web/stock/detail", {"secu_code": prefix + code})
            industries = {row["code"]: row for row in self.sectors()}
            # Preserve upstream order, but exclude all concept/area memberships.
            match = next((industries[p["secu_code"]] for p in detail.get("plate", [])
                          if p.get("secu_code") in industries), None)
            if match is None:
                raise ValueError("No verified CLS industry membership")
            try:
                quote = self._sector_bar(match)
            except (requests.RequestException, ValueError, KeyError, RuntimeError):
                quote = {}
            return {"industry": match["name"], "sector": match["name"],
                    "sector_change_pct": quote.get("change_pct"),
                    "sector_as_of": quote.get("as_of"), "sector_source": "财联社（日线）"}
        try:
            return self._cached("sector-info:" + code, fetch)
        except (requests.RequestException, ValueError, KeyError, RuntimeError):
            return self._eastmoney_sector_info(code)

    def _eastmoney_sector_info(self, code):
        if len(code) != 6 or not code.isdigit():
            raise ValueError("Invalid stock code")
        market = 1 if code.startswith("6") else 0
        def industry():
            # Company data uses a separate source from live quotations.
            suffix = "SH" if market == 1 else "BJ" if code.startswith(("4", "8", "9")) else "SZ"
            response = self._get_json("https://datacenter.eastmoney.com/securities/api/data/v1/get",
                {"reportName": "RPT_F10_ORG_BASICINFO", "columns": "SECURITY_CODE,BOARD_NAME_2LEVEL",
                 "filter": f'(SECUCODE="{code}.{suffix}")'})
            rows = (response.get("result") or {}).get("data") or []
            name = rows[0].get("BOARD_NAME_2LEVEL") if rows else None
            if not name or name == "-":
                raise ValueError("Industry missing")
            return name
        name = self._cached("industry:" + code, industry, ttl=86400)
        try:
            rows = self._cached("eastmoney-sectors", lambda: self._rows("m:90 t:2 f:!50"))
            match = next((r for r in rows if r["name"] == name), None)
        except Exception:
            match = None  # Preserve verified industry even when quotes fail.
        return {"industry": name, "sector": name,
            "sector_change_pct": match["change_pct"] if match else None,
            "sector_as_of": match["as_of"] if match else None,
            "sector_source": "东方财富"}

    def economic_calendar(self):
        def fetch():
            start = self._now().replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
            data = self._get_json("https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas",
                {"start": int(start.timestamp()), "end": int(end.timestamp())})
            if data.get("code") != 20000 or not isinstance(data.get("data", {}).get("items"), list):
                raise ValueError("Invalid economic calendar")
            result = []
            for row in data["data"]["items"]:
                stamp = number(row.get("public_date"))
                if stamp is None or not row.get("title"):
                    continue
                when = datetime.fromtimestamp(stamp, SHANGHAI)
                if not start <= when < end:
                    continue
                result.append({"name": f"{row.get('country', '')} {row['title']}",
                    "time": when.isoformat(), "actual": number(row.get("actual")),
                    "forecast": number(row.get("forecast")), "previous": number(row.get("previous")),
                    "source": "华尔街见闻", "importance": row.get("importance", 0)})
            return sorted(result, key=lambda r: r["time"])
        return self._cached("econ", fetch)

    def earnings_calendar(self):
        def fetch():
            today = self._now().date().isoformat()
            # Server narrows to today's candidate appointments; local filtering
            # excludes dates superseded by subsequent changes.
            candidate_filter = f"(APPOINT_PUBLISH_DATE='{today}')"
            params = {"reportName": "RPT_PUBLIC_BS_APPOIN", "columns": "ALL",
                "pageSize": 500, "pageNumber": 1,
                "filter": candidate_filter,
                "sortColumns": "REPORT_DATE,SECURITY_CODE", "sortTypes": "-1,1"}
            result = []
            pages = 1
            page = 1
            while page <= pages:
                params["pageNumber"] = page
                data = self._get_json("https://datacenter-web.eastmoney.com/api/data/v1/get", params)
                payload = data.get("result")
                if not data.get("success") or not isinstance(payload, dict):
                    raise ValueError("Invalid earnings calendar")
                pages = int(payload.get("pages", 1))
                if pages > 5:
                    raise ValueError("Earnings calendar exceeded bounded page count")
                for row in payload.get("data") or []:
                    effective = next((row.get(k) for k in ("ACTUAL_PUBLISH_DATE", "APPOINT_PUBLISH_DATE", "THIRD_CHANGE_DATE",
                        "SECOND_CHANGE_DATE", "FIRST_CHANGE_DATE", "FIRST_APPOINT_DATE") if row.get(k)), None)
                    if effective and str(effective)[:10] == today:
                        result.append({"code": row["SECURITY_CODE"], "name": row.get("SECURITY_NAME_ABBR", ""),
                            "event": f"{row.get('SECURITY_NAME_ABBR', '')} 财报披露（报告期 {str(row.get('REPORT_DATE', ''))[:10]}）",
                            "date": today, "source": "东方财富预约披露日历"})
                page += 1
            return result
        return self._cached("earnings", fetch, ttl=3600)
