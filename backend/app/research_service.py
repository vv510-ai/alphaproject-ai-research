"""Read-only research views and bounded, explicitly requested persistent refreshes.

This service owns only additive research_* tables. Network calls occur outside
SQLite transactions. Frozen snapshots are never subsequently rewritten.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import json
import math
import threading
import uuid

from .research_types import (MAX_ITEMS, QUOTE_SECTIONS, SECTIONS, empty_section, encoded,
                             freeze, freshness, instant, normalize_item, stamp, text_value, url_value)
from .securities import market_for

REFRESH_COOLDOWN_SECONDS = 60
GLOBAL_COOLDOWN_SECONDS = 2
REFRESH_TIMEOUT_SECONDS = 24
LEASE_SECONDS = 40
CACHE_REUSE_SECONDS = 60
MAX_WORKERS = 3
_GATES_LOCK = threading.Lock()
_GATES = {}


class _Gate:
    def __init__(self):
        self.lock = threading.Lock()
        self.futures = []


class ResearchService:
    def __init__(self, store, provider=None, clock=None):
        self.store = store
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if provider is None:
            from .research_provider import PublicResearchProvider
            provider = PublicResearchProvider(clock=self.clock)
        self.provider = provider
        with _GATES_LOCK:
            self._gate = _GATES.setdefault(str(store.path), _Gate())

    def initialize(self):
        with self.store.connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_cache (
                    scope TEXT PRIMARY KEY, market TEXT, symbol TEXT,
                    snapshot_id TEXT, payload TEXT, last_attempt_at TEXT,
                    refresh_state TEXT NOT NULL DEFAULT 'idle', error_message TEXT,
                    attempt_token TEXT
                );
                CREATE TABLE IF NOT EXISTS research_snapshots (
                    id TEXT PRIMARY KEY, scope TEXT NOT NULL, created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS research_snapshots_scope_time
                    ON research_snapshots(scope,created_at);
                CREATE TABLE IF NOT EXISTS research_item_fingerprints (
                    scope TEXT NOT NULL, section TEXT NOT NULL, item_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
                    PRIMARY KEY(scope,section,item_id)
                );
                CREATE TABLE IF NOT EXISTS research_refresh_lock (
                    id INTEGER PRIMARY KEY CHECK(id=1), token TEXT, scope TEXT,
                    started_at TEXT, expires_at TEXT, last_attempt_at TEXT
                );
                INSERT OR IGNORE INTO research_refresh_lock(id) VALUES(1);
            """)

    @staticmethod
    def _scope(market, symbol):
        if market is None and symbol is None:
            return "overview"
        if not isinstance(symbol, str) or market_for(symbol) != market:
            raise ValueError("The market and stock symbol must match")
        return f"{market}:{symbol}"

    def _empty(self, market, symbol, message=None):
        return {"scope": self._scope(market, symbol), "market": market, "symbol": symbol,
                "snapshot_id": None, "created_at": None, "refresh_state": "idle",
                "last_attempt_at": None, "retry_after_seconds": 0, "error_message": message,
                "sections": [empty_section(key, message or "尚无已保存资料，请刷新后查看。") for key in SECTIONS]}

    @staticmethod
    def _decode(payload):
        try:
            result = json.loads(payload) if payload else None
            if not isinstance(result, dict) or not isinstance(result.get("sections"), list):
                return None
            keys = [section.get("key") for section in result["sections"] if isinstance(section, dict)]
            if keys != list(SECTIONS) or any(not isinstance(s.get("items"), list) for s in result["sections"]):
                return None
            return result
        except (ValueError, TypeError):
            return None

    def state(self, market=None, symbol=None):
        scope, now = self._scope(market, symbol), instant(self.clock(), strict=True)
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM research_cache WHERE scope=?", (scope,)).fetchone()
            lease = db.execute("SELECT * FROM research_refresh_lock WHERE id=1").fetchone()
        result = self._decode(row["payload"]) if row else None
        result = result or self._empty(market, symbol)
        if row:
            result.update(refresh_state=row["refresh_state"], last_attempt_at=row["last_attempt_at"],
                          error_message=row["error_message"])
            previous = instant(row["last_attempt_at"])
            result["retry_after_seconds"] = max(0, math.ceil((previous + timedelta(seconds=REFRESH_COOLDOWN_SECONDS)
                                                            - now).total_seconds())) if previous else 0
            if row["refresh_state"] == "processing":
                valid = lease and lease["token"] == row["attempt_token"] and instant(lease["expires_at"])
                if not valid or instant(lease["expires_at"]) <= now:
                    result.update(refresh_state="interrupted", error_message="上次刷新未取得完成确认；旧资料仍可查看。")
        if lease and lease["token"] and instant(lease["expires_at"]) and instant(lease["expires_at"]) > now:
            result["refresh_in_progress"] = True
            result["active_scope"] = lease["scope"]
        else:
            result["refresh_in_progress"] = False
        for section in result["sections"]:
            freshness(section, now)
        return result

    def _claim(self, scope, market, symbol, now, token):
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            lease = db.execute("SELECT * FROM research_refresh_lock WHERE id=1").fetchone()
            if lease and lease["token"] and instant(lease["expires_at"]) and instant(lease["expires_at"]) > now:
                return False
            if lease and instant(lease["last_attempt_at"]) and (now - instant(lease["last_attempt_at"])).total_seconds() < GLOBAL_COOLDOWN_SECONDS:
                return False
            row = db.execute("SELECT last_attempt_at FROM research_cache WHERE scope=?", (scope,)).fetchone()
            if row and instant(row["last_attempt_at"]) and (now - instant(row["last_attempt_at"])).total_seconds() < REFRESH_COOLDOWN_SECONDS:
                return False
            db.execute("UPDATE research_refresh_lock SET token=?,scope=?,started_at=?,expires_at=?,last_attempt_at=? WHERE id=1",
                       (token, scope, stamp(now), stamp(now + timedelta(seconds=LEASE_SECONDS)), stamp(now)))
            db.execute("""INSERT INTO research_cache(scope,market,symbol,last_attempt_at,refresh_state,attempt_token)
                VALUES(?,?,?,?,'processing',?) ON CONFLICT(scope) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,refresh_state='processing',error_message=NULL,attempt_token=excluded.attempt_token""",
                       (scope, market, symbol, stamp(now), token))
        return True

    def _reusable(self, key, market, symbol, now):
        if market is None or key not in {"indices", "global", "calendar"}:
            return None
        overall = self.state()
        section = next(s for s in overall["sections"] if s["key"] == key)
        received = instant(section.get("received_at"))
        if section["status"] not in {"available", "partial"} or not received or not 0 <= (now - received).total_seconds() < CACHE_REUSE_SECONDS:
            return None
        section["cache_reused"] = True
        for item in section["items"]:
            item["change_kind"] = "unchanged"
        return section

    def _normalize(self, key, raw, received_at):
        if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
            raise ValueError("来源响应没有可核对的条目列表。")
        section = empty_section(key)
        section.update(source=text_value(raw.get("source"), 120) or section["source"],
                       source_url=url_value(raw.get("source_url")) or section["source_url"],
                       status=raw.get("status") if raw.get("status") in {"available", "partial", "unavailable", "stale"} else "available",
                       error_message=text_value(raw.get("error_message"), 600), received_at=received_at)
        seen, invalid = {}, 0
        limit = 500 if key == "calendar" else MAX_ITEMS
        for value in raw["items"][:limit]:
            item = normalize_item(key, value, section["source"], section["source_url"], received_at)
            if item is None:
                invalid += 1
                continue
            if item["id"] in seen and seen[item["id"]]["content_fingerprint"] != item["content_fingerprint"]:
                # Conflicting rows for one source ID are not silently collapsed.
                invalid += 1
                continue
            seen[item["id"]] = item
        section["items"] = list(seen.values())
        field = "as_of" if key in QUOTE_SECTIONS else "published_at"
        dates = [item[field] for item in section["items"] if item.get(field)]
        section["as_of"] = max(dates, key=lambda d: instant(d)) if dates else None
        section["filtered_count"] = invalid
        if invalid or len(raw["items"]) > limit:
            section["status"] = "partial" if section["items"] else "unavailable"
            section["error_message"] = section["error_message"] or "部分条目缺失、冲突或超过显示上限，未采用。"
        for name in ("coverage", "source_attempts", "total_available", "truncated"):
            if name in raw:
                section[name] = raw[name]
        return section

    def refresh(self, market=None, symbol=None):
        scope, now = self._scope(market, symbol), instant(self.clock(), strict=True)
        if not self._gate.lock.acquire(blocking=False):
            return self._busy_state(market, symbol)
        try:
            # Timed-out workers may still be finishing their bounded HTTP calls.
            # Never open another executor until all of them have actually stopped.
            if any(not future.done() for future in self._gate.futures):
                return self._busy_state(market, symbol)
            token = uuid.uuid4().hex
            if not self._claim(scope, market, symbol, now, token):
                return self.state(market, symbol)
            old = {s["key"]: s for s in self.state(market, symbol)["sections"]}
            results, failures = {}, {}
            pool = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="research")
            futures = {}
            try:
                for key in SECTIONS:
                    reused = self._reusable(key, market, symbol, now)
                    if reused is not None:
                        results[key] = reused
                    else:
                        futures[pool.submit(self.provider.fetch_section, key, market, symbol)] = key
                self._gate.futures = list(futures)
                completed, pending = wait(futures, timeout=REFRESH_TIMEOUT_SECONDS)
                for future in completed:
                    key = futures[future]
                    try:
                        results[key] = self._normalize(key, future.result(), stamp(self.clock()))
                        if results[key]["status"] == "unavailable":
                            failures[key] = results[key]["error_message"] or "来源本次没有可用资料。"
                    except Exception as error:
                        failures[key] = "资料获取失败：" + (text_value(str(error), 180) if isinstance(error, ValueError) else type(error).__name__)
                for future in pending:
                    future.cancel()
                    failures[futures[future]] = "刷新超过总等待上限，本次未取得确认。"
            finally:
                pool.shutdown(wait=False, cancel_futures=True)
            for key, message in failures.items():
                section = old[key] if old[key]["items"] else empty_section(key)
                section.update(status="stale" if section["items"] else "unavailable", error_message=message)
                if results.get(key, {}).get("source_attempts"):
                    section["source_attempts"] = results[key]["source_attempts"]
                for item in section["items"]:
                    item.update(change_kind="unchanged", carried_forward=True)
                results[key] = section
            finished = instant(self.clock(), strict=True)
            result = self._empty(market, symbol)
            result.update(snapshot_id="research_" + uuid.uuid4().hex, created_at=stamp(finished),
                          last_attempt_at=stamp(now), refresh_state="failed" if len(failures) == len(SECTIONS) else "partial" if failures else "succeeded",
                          error_message="部分来源刷新失败；保留旧资料并标明陈旧。" if failures else None,
                          sections=[freshness(results[key], finished) for key in SECTIONS])
            with self.store.connect() as db, db:
                # The attempt token protects against an expired lease completing late.
                current = db.execute("SELECT token FROM research_refresh_lock WHERE id=1").fetchone()
                if current and current["token"] == token:
                    for section in result["sections"]:
                        if section["key"] in failures or section.get("cache_reused"):
                            continue
                        for item in section["items"]:
                            previous = db.execute("SELECT * FROM research_item_fingerprints WHERE scope=? AND section=? AND item_id=?",
                                                  (scope, section["key"], item["id"])).fetchone()
                            item["change_kind"] = "new" if previous is None else "unchanged" if previous["fingerprint"] == item["content_fingerprint"] else "updated"
                            item["first_seen_at"] = previous["first_seen_at"] if previous else stamp(finished)
                            db.execute("""INSERT INTO research_item_fingerprints VALUES(?,?,?,?,?,?)
                                ON CONFLICT(scope,section,item_id) DO UPDATE SET
                                fingerprint=excluded.fingerprint,last_seen_at=excluded.last_seen_at""",
                                       (scope, section["key"], item["id"], item["content_fingerprint"], item["first_seen_at"], stamp(finished)))
                    payload = encoded(result)
                    db.execute("INSERT INTO research_snapshots(id,scope,created_at,payload) VALUES(?,?,?,?)",
                               (result["snapshot_id"], scope, result["created_at"], payload))
                    db.execute("UPDATE research_cache SET snapshot_id=?,payload=?,refresh_state=?,error_message=? WHERE scope=? AND attempt_token=?",
                               (result["snapshot_id"], payload, result["refresh_state"], result["error_message"], scope, token))
                    db.execute("UPDATE research_refresh_lock SET token=NULL,scope=NULL,expires_at=NULL WHERE id=1 AND token=?", (token,))
            return self.state(market, symbol)
        finally:
            self._gate.lock.release()

    def _busy_state(self, market, symbol):
        result = self.state(market, symbol)
        result.update(refresh_in_progress=True, retry_after_seconds=max(1, result["retry_after_seconds"]))
        return result

    def freeze_context(self, market=None, symbol=None, cutoff=None):
        now = instant(self.clock(), strict=True)
        boundary = instant(cutoff, strict=True) if cutoff is not None else now
        # A caller cannot opt into future knowledge by passing a future cutoff.
        boundary = min(boundary, now)
        scope = self._scope(market, symbol)
        with self.store.connect() as db:
            row = db.execute("""SELECT payload FROM research_snapshots WHERE scope=? AND created_at<=?
                ORDER BY created_at DESC,rowid DESC LIMIT 1""", (scope, stamp(boundary))).fetchone()
        result = self._decode(row["payload"]) if row else None
        if result is None:
            result = self._empty(market, symbol, "资料截止时点前没有已保存快照；不以后来抓到的内容回填历史。")
        notes = []
        for section in result["sections"]:
            selected, filtered = [], 0
            for item in section["items"]:
                field = "as_of" if section["key"] in QUOTE_SECTIONS else "published_at"
                published, received = instant(item.get(field)), instant(item.get("received_at"))
                if (section["key"] == "calendar" and received and received <= boundary
                        and instant(item.get("scheduled_at")) and (not published or published > boundary)):
                    # A future schedule can have been known before the cutoff.
                    # Keep it for display, explicitly outside factual AI evidence.
                    item.update(ai_eligible=False, missing_time=True, actual=None)
                    selected.append(item)
                    continue
                if not published or not received or published > boundary or received > boundary:
                    filtered += 1
                    continue
                item["ai_eligible"] = True
                selected.append(item)
            if filtered:
                section["status"] = "partial" if selected else "unavailable"
                section["error_message"] = "部分资料缺少可靠时间，或在截止时点之后发布/收到，未进入本次证据。"
                notes.append({"section": section["key"], "excluded_count": filtered, "reason": "time_boundary_or_missing_time"})
            section["items"] = selected
            field = "as_of" if section["key"] in QUOTE_SECTIONS else "published_at"
            dates = [item[field] for item in selected if item.get(field)]
            section["as_of"] = max(dates, key=lambda d: instant(d)) if dates else None
            section["filtered_count"] = section.get("filtered_count", 0) + filtered
            freshness(section, boundary)
        result.update(cutoff=stamp(boundary), frozen_at=stamp(now), selection_notes=notes, refresh_in_progress=False)
        return freeze(result)
