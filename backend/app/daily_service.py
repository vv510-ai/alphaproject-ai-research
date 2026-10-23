"""Persist one validated history batch per watchlist record, without splicing sources."""

from datetime import datetime, timedelta, timezone
import json
import math
import uuid

from .daily import DailyFailure, TencentDailySource
from .quotes import CHINA_TIME
from .securities import LookupFailure, market_for

REFRESH_INTERVAL_SECONDS = 30


class DailyService:
    def __init__(self, store, source=None, clock=None):
        self.store = store
        self.source = source or TencentDailySource()
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def initialize(self):
        with self.store.connect() as connection, connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS daily_cache (
                    market TEXT NOT NULL, symbol TEXT NOT NULL,
                    watchlist_created_at TEXT NOT NULL, payload TEXT,
                    last_attempt_at TEXT NOT NULL,
                    last_status TEXT NOT NULL CHECK (last_status IN ('processing','succeeded','failed')),
                    error_code TEXT, error_message TEXT, attempt_token TEXT NOT NULL,
                    PRIMARY KEY (market,symbol),
                    FOREIGN KEY (market,symbol) REFERENCES watchlist(market,symbol) ON DELETE CASCADE
                )
            """)

    def _item(self, connection, market, symbol):
        if market_for(symbol) != market:
            raise LookupFailure("invalid_symbol", "市场与股票代码不一致，请刷新列表。", 400)
        row = connection.execute("SELECT * FROM watchlist WHERE market=? AND symbol=?", (market, symbol)).fetchone()
        if not row:
            raise LookupFailure("not_in_watchlist", "这只股票已不在自选中，请刷新列表。", 404)
        return row

    def _view(self, item, row):
        if row and row["watchlist_created_at"] != item["created_at"]:
            row = None
        data = json.loads(row["payload"]) if row and row["payload"] else None
        if data:
            data.pop("raw_text", None)
        retry, error = 0, row["error_message"] if row else None
        status = row["last_status"] if row else "idle"
        if row:
            next_time = datetime.fromisoformat(row["last_attempt_at"]) + timedelta(seconds=REFRESH_INTERVAL_SECONDS)
            retry = max(0, math.ceil((next_time - self.clock()).total_seconds()))
            if status == "processing" and retry == 0:
                status, error = "interrupted", "上次日线获取未取得确认，可以重新获取。"
        return {"market": item["market"], "symbol": item["symbol"], "watchlist_created_at": item["created_at"],
                "data": data, "refresh_state": status, "error_message": error,
                "last_attempt_at": row["last_attempt_at"] if row else None, "retry_after_seconds": retry}

    def get(self, market, symbol):
        with self.store.connect() as connection:
            connection.execute("BEGIN")
            item = self._item(connection, market, symbol)
            row = connection.execute("SELECT * FROM daily_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            return self._view(item, row)

    def _reserve(self, market, symbol, expected_created_at):
        now, token = self.clock(), uuid.uuid4().hex
        with self.store.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            item = self._item(connection, market, symbol)
            if item["created_at"] != expected_created_at:
                raise LookupFailure("daily_record_changed", "这条自选已发生变化，请重新打开日线详情。", 409)
            row = connection.execute("SELECT * FROM daily_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            same_item = row and row["watchlist_created_at"] == item["created_at"]
            if same_item and self._view(item, row)["retry_after_seconds"]:
                raise LookupFailure("daily_cooldown", "刚请求过这只股票的日线，请稍后再更新；已有图表仍可查看。", 429)
            connection.execute("""
                INSERT INTO daily_cache (market,symbol,watchlist_created_at,payload,last_attempt_at,last_status,error_code,error_message,attempt_token)
                VALUES (?,?,?,?,?,'processing',NULL,NULL,?)
                ON CONFLICT (market,symbol) DO UPDATE SET watchlist_created_at=excluded.watchlist_created_at,
                    payload=excluded.payload,last_attempt_at=excluded.last_attempt_at,last_status='processing',
                    error_code=NULL,error_message=NULL,attempt_token=excluded.attempt_token
            """, (market, symbol, item["created_at"], row["payload"] if same_item else None, now.isoformat(), token))
        return token, item["created_at"], now

    def _finish(self, market, symbol, token, generation, history, failure):
        with self.store.connect() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            item = connection.execute("SELECT * FROM watchlist WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            row = connection.execute("SELECT * FROM daily_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            if not item or item["created_at"] != generation or not row or row["attempt_token"] != token:
                raise LookupFailure("daily_refresh_superseded", "自选或日线请求已经变化，本次结果没有写入。", 409)
            payload = row["payload"]
            if history and not failure:
                if history.market != market or history.symbol != symbol or history.adjustment != "none":
                    failure = DailyFailure("wrong_daily_identity", "来源身份或复权口径与本次请求不符，本次未采用。")
                elif payload and history.latest_trade_date < json.loads(payload)["latest_trade_date"]:
                    failure = DailyFailure("regressed_daily_date", "本次最新日期比已保存日线更早，保留原来的图表。")
                else:
                    payload = json.dumps(history.to_dict(), ensure_ascii=False, allow_nan=False)
            connection.execute("""
                UPDATE daily_cache SET payload=?,last_status=?,error_code=?,error_message=?
                WHERE market=? AND symbol=? AND attempt_token=?
            """, (payload, "failed" if failure else "succeeded", failure.code if failure else None,
                  failure.message if failure else None, market, symbol, token))
            saved = connection.execute("SELECT * FROM daily_cache WHERE market=? AND symbol=?", (market, symbol)).fetchone()
        return {"refresh_succeeded": failure is None, "history": self._view(item, saved)}

    def refresh(self, market, symbol, expected_created_at):
        token, generation, now = self._reserve(market, symbol, expected_created_at)
        end = now.astimezone(CHINA_TIME).date() - timedelta(days=1)
        start = end - timedelta(days=180)
        history, failure = None, None
        try:
            history = self.source.fetch(symbol, start, end)
        except LookupFailure as error:
            failure = error
        return self._finish(market, symbol, token, generation, history, failure)
