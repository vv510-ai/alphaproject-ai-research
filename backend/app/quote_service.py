"""Shared latest-quote cache. A manual refresh is distinct from reading the page."""

from datetime import datetime, timedelta, timezone
import json
import math
import threading
import uuid

from .live_policy import (COLLECT_INTERVAL_SECONDS, FRESHNESS_SECONDS, POLICY_VERSION,
                          blocked, build_live_evidence, proof_eligibility, session_context)
from .live_schema import initialize_live_schema
from .quotes import CHINA_TIME, Quote, QuoteFailure, TencentQuoteSource
from .securities import LookupFailure, market_for
from .store import WatchlistStore

REFRESH_INTERVAL_SECONDS = 30  # Click guard, not a verified source quota or freshness limit.
_INFLIGHT_LOCK = threading.Lock()
_INFLIGHT = set()


class QuoteService:
    def __init__(self, store: WatchlistStore, source=None, clock=None):
        self.store = store
        self.source = source or TencentQuoteSource()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.collector = None

    def initialize(self):
        with self.store.connect() as connection, connection:
            # Additive schema: the v1 watchlist and its user_version remain compatible.
            connection.execute("""
                CREATE TABLE IF NOT EXISTS quote_cache (
                    market TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    watchlist_created_at TEXT NOT NULL,
                    payload TEXT,
                    last_attempt_at TEXT NOT NULL,
                    last_status TEXT NOT NULL CHECK (last_status IN ('processing', 'succeeded', 'failed')),
                    error_code TEXT,
                    error_message TEXT,
                    attempt_token TEXT NOT NULL,
                    PRIMARY KEY (market, symbol),
                    FOREIGN KEY (market, symbol) REFERENCES watchlist(market, symbol) ON DELETE CASCADE
                )
            """)
        initialize_live_schema(self.store)

    @staticmethod
    def _evidence(row):
        try:
            value = json.loads(row["payload"]) if row else None
            return value if isinstance(value, dict) else None
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _write_evidence(connection, market, symbol, generation, evidence):
        connection.execute("""INSERT INTO quote_live_evidence (market,symbol,watchlist_created_at,payload)
            VALUES (?,?,?,?) ON CONFLICT (market,symbol) DO UPDATE SET
            watchlist_created_at=excluded.watchlist_created_at,payload=excluded.payload""",
                           (market, symbol, generation, json.dumps(evidence, ensure_ascii=False, allow_nan=False)))

    def _view(self, row, now, evidence=None):
        data = self._evidence(row)
        context = session_context(now)
        saved = evidence.get("eligibility", {}) if isinstance(evidence, dict) else {}
        if not isinstance(saved, dict):
            saved = {}
        eligibility = proof_eligibility(data, saved.get("proof"), now,
                                        row["watchlist_created_at"] if row else None)
        if context["state"] == "trading" and not saved.get("proof"):
            eligibility = blocked(saved.get("code", "unverified"),
                                  saved.get("reason", eligibility["reason"]), saved.get("checked_at"), context["session_id"])
        if row and row["last_status"] != "succeeded" and context["state"] == "trading":
            eligibility = blocked("source_error" if row["last_status"] == "failed" else "fetch_pending",
                                  "本次获取失败，旧报价只供查看；等待新的连续采样。" if row["last_status"] == "failed"
                                  else "正在获取新报价，等待本次来源确认。",
                                  row["last_attempt_at"], context["session_id"])
        if data:
            data.pop("raw_text", None)  # Original text stays in SQLite and its digest stays in proof.
        age = None
        if not data:
            state, note = "unavailable", "尚无可用报价"
        else:
            try:
                # A bad persisted row affects this stock, never the entire dashboard.
                Quote(**{**data, "raw_text": ""})
                source_time = datetime.fromisoformat(data["source_reported_at"])
                age = math.floor((now - source_time).total_seconds())
                if age < 0:
                    state, note = "clock_issue", "时间待核对"
                elif source_time.astimezone(CHINA_TIME).date() < now.astimezone(CHINA_TIME).date():
                    state, note = "historical", "非今日报价"
                else:
                    state, note = "today_unverified", "今日报价 · 实时性待验证"
                    if eligibility["eligible"]:
                        note = "今日报价 · 最近成交已观察"
            except (KeyError, TypeError, ValueError, OverflowError):
                data, age, state, note = None, None, "unavailable", "已保存报价无法核对，等待重新获取。"
                eligibility = blocked("invalid_evidence", note, saved.get("checked_at"), context["session_id"])
        retry = 0
        next_at = None
        status = row["last_status"] if row else "idle"
        error = row["error_message"] if row else None
        if row:
            try:
                next_time = datetime.fromisoformat(row["last_attempt_at"]) + timedelta(seconds=REFRESH_INTERVAL_SECONDS)
                next_at = next_time.isoformat()
                retry = max(0, math.ceil((next_time - now).total_seconds()))
            except (ValueError, TypeError, OverflowError):
                error = "上次获取时间无法核对，可以重新获取。"
                eligibility = blocked("invalid_attempt_time", error, None, context["session_id"])
            if status == "processing" and retry == 0:
                status = "interrupted"
                error = "上次获取未取得确认，可以重新获取。"
        return {
            "data": data, "data_state": state, "data_label": note, "source_age_seconds": age,
            "refresh_state": status, "last_attempt_at": row["last_attempt_at"] if row else None,
            "error_message": error, "retry_after_seconds": retry, "next_refresh_at": next_at,
            "market_state": context["state"], "realtime_verified": eligibility["eligible"],
            "eligibility": eligibility,
        }

    def collector_state(self):
        if self.collector is not None:
            return self.collector.state()
        return {"running": False, "heartbeat_at": None, "last_cycle_at": None, "next_cycle_at": None, "cycle_id": None,
                "state": "stopped", "label": "后台采集尚未启动", "interval_seconds": COLLECT_INTERVAL_SECONDS,
                "freshness_seconds": FRESHNESS_SECONDS, "total": len(self.store.list_items()),
                "attempted": 0, "succeeded": 0, "failed": 0, "last_error": None}

    def dashboard(self):
        now = self.clock()
        with self.store.connect() as connection:
            items = connection.execute("SELECT * FROM watchlist ORDER BY display_order, created_at DESC, market, symbol").fetchall()
            cached = {(row["market"], row["symbol"]): row for row in connection.execute("SELECT * FROM quote_cache")}
            evidence = {(row["market"], row["symbol"]): row for row in connection.execute("SELECT * FROM quote_live_evidence")}
        result = []
        for item in items:
            row = cached.get((item["market"], item["symbol"]))
            # Also safe if a prior version removed/re-added a stock without foreign keys enabled.
            if row and row["watchlist_created_at"] != item["created_at"]:
                row = None
            proof_row = evidence.get((item["market"], item["symbol"]))
            if proof_row and proof_row["watchlist_created_at"] != item["created_at"]:
                proof_row = None
            result.append({**self.store.public_item(item), "quote": self._view(row, now, self._evidence(proof_row))})
        return {"items": result, "total": len(result), "as_of": now.isoformat(), "collector": self.collector_state()}

    def _reserve(self, market, symbol, expected_generation=None):
        if market_for(symbol) != market:
            raise LookupFailure("invalid_symbol", "市场与股票代码不一致，请刷新列表后重试。", 400)
        now, token = self.clock(), uuid.uuid4().hex
        with self.store.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute("SELECT * FROM watchlist WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            if not item:
                raise LookupFailure("not_in_watchlist", "这只股票已不在自选中，请刷新列表。", 404)
            if expected_generation is not None and item["created_at"] != expected_generation:
                raise LookupFailure("watchlist_changed", "自选已重新添加，本轮旧采集任务已停止。", 409)
            row = connection.execute("SELECT * FROM quote_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            same_item = row and row["watchlist_created_at"] == item["created_at"]
            if same_item:
                retry = self._view(row, now)["retry_after_seconds"]
                if retry:
                    raise LookupFailure("quote_cooldown", f"刚刚已请求过这只股票，请 {retry} 秒后再获取；现有报价仍可查看。", 429)
            connection.execute("""
                INSERT INTO quote_cache (market,symbol,watchlist_created_at,payload,last_attempt_at,last_status,error_code,error_message,attempt_token)
                VALUES (?,?,?,?,?,'processing',NULL,NULL,?)
                ON CONFLICT (market,symbol) DO UPDATE SET
                    watchlist_created_at=excluded.watchlist_created_at, payload=excluded.payload,
                    last_attempt_at=excluded.last_attempt_at, last_status='processing',
                    error_code=NULL,error_message=NULL,attempt_token=excluded.attempt_token
            """, (market, symbol, item["created_at"], row["payload"] if same_item else None, now.isoformat(), token))
            old_evidence = connection.execute("SELECT * FROM quote_live_evidence WHERE market=? AND symbol=?",
                                              (market, symbol)).fetchone()
            evidence = self._evidence(old_evidence) if old_evidence and old_evidence["watchlist_created_at"] == item["created_at"] else None
            evidence = evidence or {"version": POLICY_VERSION, "candidate": None}
            evidence["eligibility"] = blocked("fetch_pending", "正在获取新报价，等待来源确认。", now.isoformat(), session_context(now)["session_id"])
            self._write_evidence(connection, market, symbol, item["created_at"], evidence)
        return token, item["created_at"]

    def _finish(self, market, symbol, token, generation, quote: Quote | None, failure: LookupFailure | None,
                collector_cycle_id=None):
        with self.store.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute("SELECT created_at FROM watchlist WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            row = connection.execute("SELECT * FROM quote_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            if not item or item["created_at"] != generation or not row or row["attempt_token"] != token:
                raise LookupFailure("refresh_superseded", "自选或获取任务已经变化，本次结果没有写入，请刷新查看。", 409)
            payload = row["payload"]
            now = self.clock()
            if quote and not failure:
                if quote.market != market or quote.symbol != symbol:
                    failure = QuoteFailure("wrong_identity", "来源股票身份不一致，本次未采用。")
                elif self._older_than_cache(quote, row):
                    failure = QuoteFailure("regressed_time", "来源时间比已保存报价更早，本次保留原来的值。")
                else:
                    payload = json.dumps(quote.to_dict(), ensure_ascii=False, allow_nan=False)
            prior = self._evidence(connection.execute("SELECT * FROM quote_live_evidence WHERE market=? AND symbol=?",
                                                      (market, symbol)).fetchone())
            if quote and not failure:
                # This transaction marker travels with the exact sample. A later
                # read proves it was committed; reads never relabel old evidence.
                acquisition = {"origin": "collector" if collector_cycle_id else "manual",
                               "cycle_id": collector_cycle_id, "started_at": row["last_attempt_at"],
                               "committed_at": now.isoformat()}
                evidence = build_live_evidence(quote, prior, generation, now, acquisition=acquisition)
            else:
                evidence = {"version": POLICY_VERSION, "candidate": None,
                            "eligibility": blocked("source_error", "本次获取失败，旧报价只供查看；等待新的连续采样。",
                                                   now.isoformat(), session_context(now)["session_id"])}
            self._write_evidence(connection, market, symbol, generation, evidence)
            status = "failed" if failure else "succeeded"
            connection.execute("""
                UPDATE quote_cache SET payload=?,last_status=?,error_code=?,error_message=?
                WHERE market=? AND symbol=? AND attempt_token=?
            """, (payload, status, failure.code if failure else None, failure.message if failure else None, market, symbol, token))
            final_row = connection.execute("SELECT * FROM quote_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
        return {"refresh_succeeded": failure is None, "quote": self._view(final_row, self.clock(), evidence)}

    def _older_than_cache(self, quote, row):
        cached = self._evidence(row)
        try:
            prior = datetime.fromisoformat(cached["source_reported_at"]) if cached else None
            return bool(prior and datetime.fromisoformat(quote.source_reported_at) < prior)
        except (KeyError, TypeError, ValueError):
            # An unusable old cache is replaceable by a newly parsed source quote.
            return False

    def refresh(self, market: str, symbol: str, expected_generation=None, *, collector_cycle_id=None):
        # Only the registered collector supplies this server-side identifier.
        # The public manual-refresh route does not accept provenance parameters.
        if collector_cycle_id is not None:
            try:
                if uuid.UUID(collector_cycle_id).hex != collector_cycle_id:
                    raise ValueError("noncanonical cycle id")
            except (ValueError, TypeError, AttributeError):
                raise LookupFailure("invalid_collection_cycle", "采集轮次无法核对，本次未取数。", 400)
        key = (str(self.store.path), market, symbol)
        with _INFLIGHT_LOCK:
            if key in _INFLIGHT:
                raise LookupFailure("quote_busy", "这只股票正在获取中，请稍后查看结果。", 429)
            _INFLIGHT.add(key)
        try:
            token, generation = self._reserve(market, symbol, expected_generation)
            quote, failure = None, None
            try:
                quote = self.source.fetch(symbol)  # No database transaction is held during network I/O.
            except LookupFailure as error:
                failure = error
            except Exception:
                # Interrupted/manual calls still keep their old processing/cooldown contract,
                # but no subsequent request may bridge an unconfirmed sample into proof.
                with self.store.connect() as db, db:
                    row = db.execute("SELECT attempt_token FROM quote_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
                    if row and row["attempt_token"] == token:
                        self._write_evidence(db, market, symbol, generation, {"version": POLICY_VERSION, "candidate": None,
                            "eligibility": blocked("source_error", "本次来源获取未完成，等待新的连续采样。", self.clock().isoformat())})
                raise
            return self._finish(market, symbol, token, generation, quote, failure, collector_cycle_id)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(key)
