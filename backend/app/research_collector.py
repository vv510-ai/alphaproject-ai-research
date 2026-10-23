"""One server-side reader for research and rule-required daily history.

It never sends messages or runs models. Durable claims survive restarts; network
work is outside transactions, and watchlist generations/rules are rechecked.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading

from .research_types import CHINA, encoded, instant, stamp
from .trading_calendar import TradingCalendar

OVERVIEW_INTERVAL_SECONDS = 15 * 60
STOCK_INTERVAL_SECONDS = 5 * 60
POLL_SECONDS = 2


class ResearchCollector:
    def __init__(self, store, research, daily=None, clock=None):
        self.store, self.research, self.daily = store, research, daily
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.calendar = TradingCalendar()
        self.thread = None
        self.stop_event = threading.Event()
        self._tick_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._initialized = False
        self._status = {"state": "stopped", "running": False, "heartbeat_at": None,
                        "last_attempt_at": None, "last_success_at": None, "active_job": None,
                        "last_error": None, "overview_interval_seconds": OVERVIEW_INTERVAL_SECONDS,
                        "stock_interval_seconds": STOCK_INTERVAL_SECONDS}

    def initialize(self):
        if self._initialized:
            return
        with self.store.connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS research_collector_jobs (
                    job_key TEXT PRIMARY KEY, kind TEXT NOT NULL, market TEXT, symbol TEXT,
                    watchlist_created_at TEXT, rule_version INTEGER, last_attempt_at TEXT,
                    last_success_at TEXT, success_date TEXT, next_due_at TEXT,
                    state TEXT NOT NULL, error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS research_collector_runtime (
                    id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL
                );
                INSERT OR IGNORE INTO research_collector_runtime VALUES(1,'{}');
            """)
        self._initialized = True

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def state(self):
        # No schema writes, task reset or network traffic on a GET path.
        with self._state_lock:
            result = dict(self._status)
        if self._initialized:
            with self.store.connect() as db:
                result["jobs"] = [dict(row) for row in db.execute("""SELECT kind,market,symbol,last_attempt_at,
                    last_success_at,success_date,next_due_at,state,error_message FROM research_collector_jobs
                    ORDER BY last_attempt_at DESC LIMIT 100""")]
        else:
            result["jobs"] = []
        result["running"] = self.is_alive() and not self.stop_event.is_set()
        return result

    def _pulse(self, **changes):
        with self._state_lock:
            self._status.update(changes, heartbeat_at=stamp(self.clock()))
            payload = encoded(self._status)
        with self.store.connect() as db, db:
            db.execute("UPDATE research_collector_runtime SET payload=? WHERE id=1", (payload,))

    def _rules(self):
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='extended_anomaly_rules'").fetchone():
                return []
            rows = db.execute("""SELECT r.market,r.symbol,r.watchlist_created_at,r.version,r.config_json
                FROM extended_anomaly_rules r JOIN watchlist w ON w.market=r.market AND w.symbol=r.symbol
                AND w.created_at=r.watchlist_created_at WHERE r.enabled=1 ORDER BY r.market,r.symbol""").fetchall()
        rules = []
        for row in rows:
            try:
                config = json.loads(row["config_json"])
            except (ValueError, TypeError):
                continue
            if isinstance(config, dict):
                rules.append({**dict(row), "config": config})
        return rules

    def _current(self, job):
        if job["kind"] == "overview":
            return not self.stop_event.is_set()
        flag = "event_enabled" if job["kind"] == "research" else "breakout_enabled"
        return not self.stop_event.is_set() and any(
            rule["market"] == job["market"] and rule["symbol"] == job["symbol"]
            and rule["watchlist_created_at"] == job["watchlist_created_at"]
            and rule["version"] == job["rule_version"] and rule["config"].get(flag) is True
            for rule in self._rules())

    @staticmethod
    def _job(kind, rule=None):
        if rule is None:
            return {"job_key": "overview", "kind": "overview", "market": None, "symbol": None,
                    "watchlist_created_at": None, "rule_version": None}
        identity = f"{kind}:{rule['market']}:{rule['symbol']}:{rule['watchlist_created_at']}"
        return {"job_key": hashlib.sha256(identity.encode()).hexdigest(), "kind": kind,
                "market": rule["market"], "symbol": rule["symbol"],
                "watchlist_created_at": rule["watchlist_created_at"], "rule_version": rule["version"]}

    def _claim(self, job, now):
        interval = OVERVIEW_INTERVAL_SECONDS if job["kind"] == "overview" else STOCK_INTERVAL_SECONDS
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM research_collector_jobs WHERE job_key=?", (job["job_key"],)).fetchone()
            if row:
                if job["kind"] == "daily" and row["success_date"] == now.astimezone(CHINA).date().isoformat():
                    return False
                version_changed = row["rule_version"] != job["rule_version"]
                due = instant(row["next_due_at"])
                if due and due > now and (not version_changed or job["kind"] == "daily"):
                    return False
            db.execute("""INSERT INTO research_collector_jobs(job_key,kind,market,symbol,watchlist_created_at,
                rule_version,last_attempt_at,next_due_at,state) VALUES(?,?,?,?,?,?,?,?,'processing')
                ON CONFLICT(job_key) DO UPDATE SET rule_version=excluded.rule_version,
                last_attempt_at=excluded.last_attempt_at,next_due_at=excluded.next_due_at,
                state='processing',error_message=NULL""",
                       (job["job_key"], job["kind"], job["market"], job["symbol"], job["watchlist_created_at"],
                        job["rule_version"], stamp(now), stamp(now + timedelta(seconds=interval))))
        return True

    def _finish(self, job, state, message=None, *, next_due=None, success_date=None):
        now = stamp(self.clock())
        successful = state in {"succeeded", "partial", "cached"}
        with self.store.connect() as db, db:
            db.execute("""UPDATE research_collector_jobs SET state=?,error_message=?,
                last_success_at=CASE WHEN ? THEN ? ELSE last_success_at END,
                success_date=COALESCE(?,success_date),next_due_at=COALESCE(?,next_due_at)
                WHERE job_key=? AND rule_version IS ?""",
                       (state, message, successful, now, success_date, stamp(next_due) if next_due else None,
                        job["job_key"], job["rule_version"]))
        self._pulse(state="waiting", active_job=None, last_error=message,
                    **({"last_success_at": now} if successful else {}))

    @staticmethod
    def _usable_daily(view, job, previous):
        data = view.get("data") if isinstance(view, dict) else None
        return bool(isinstance(data, dict) and view.get("watchlist_created_at") == job["watchlist_created_at"]
                    and data.get("latest_trade_date") == previous and data.get("adjustment") == "none"
                    and data.get("market") == job["market"] and data.get("symbol") == job["symbol"]
                    and isinstance(data.get("bars"), list) and data["bars"])

    def _collect(self, job, context):
        if not self._current(job):
            self._finish(job, "cancelled", "规则停用、版本或自选代次已改变，本次未继续。")
            return
        if job["kind"] == "daily":
            previous = context["previous_trade_date"]
            view = self.daily.get(job["market"], job["symbol"])
            state = "cached"
            if not self._usable_daily(view, job, previous):
                if not self._current(job):
                    self._finish(job, "cancelled", "请求前规则或自选已改变。")
                    return
                self.daily.refresh(job["market"], job["symbol"], job["watchlist_created_at"])
                view = self.daily.get(job["market"], job["symbol"])
                state = "succeeded"
            if not self._current(job):
                self._finish(job, "cancelled", "请求后规则或自选已改变，本次不计为规则资料成功。")
            elif self.calendar.context(self.clock().astimezone(CHINA).date()).get("previous_trade_date") != previous:
                self._finish(job, "failed", "采集跨过交易日边界，上一交易日已变化；等待下轮重新核对。")
            elif self._usable_daily(view, job, previous):
                day = self.clock().astimezone(CHINA).date()
                tomorrow = datetime.combine(day + timedelta(days=1), datetime.min.time(), CHINA)
                self._finish(job, state, success_date=day.isoformat(), next_due=tomorrow)
            else:
                self._finish(job, "failed", "未取得上一交易日的有效不复权历史，五分钟后重试；不启用突破判断。")
            return
        before = self.research.state(job["market"], job["symbol"])
        result = self.research.refresh(job["market"], job["symbol"])
        if not self._current(job):
            self._finish(job, "cancelled", "资料返回时规则或自选已改变，不计为规则采集成功。")
        elif result.get("refresh_in_progress") or (result.get("snapshot_id") == before.get("snapshot_id") and result.get("retry_after_seconds", 0)):
            delay = min(max(result.get("retry_after_seconds", 2), 2), 60)
            self._finish(job, "deferred", "已有研究刷新或冷却，稍后再检查。", next_due=self.clock() + timedelta(seconds=delay))
        elif result.get("refresh_state") in {"succeeded", "partial"}:
            self._finish(job, result["refresh_state"], result.get("error_message"))
        else:
            self._finish(job, "failed", result.get("error_message") or "本次研究资料未取得刷新确认。")

    def tick(self):
        """Run at most one due job, allowing stop and rule changes between requests."""
        self.initialize()
        if self.stop_event.is_set() or not self._tick_lock.acquire(blocking=False):
            return self.state()
        try:
            now = instant(self.clock(), strict=True)
            context = self.calendar.context(now.astimezone(CHINA).date())
            jobs = [self._job("overview")]
            for rule in self._rules():
                if rule["config"].get("event_enabled") is True:
                    jobs.append(self._job("research", rule))
                if (self.daily is not None and rule["config"].get("breakout_enabled") is True
                        and context["state"] == "open" and context.get("previous_trade_date")):
                    jobs.append(self._job("daily", rule))
            # Oldest attempts first prevent an always-due overview or early stock
            # from starving later stocks. Claims also guard a duplicate collector.
            with self.store.connect() as db:
                attempts = {r["job_key"]: r["last_attempt_at"] for r in db.execute("SELECT job_key,last_attempt_at FROM research_collector_jobs")}
            jobs.sort(key=lambda job: attempts.get(job["job_key"]) or "")
            for job in jobs:
                if not self._current(job) or not self._claim(job, now):
                    continue
                self._pulse(state="collecting", active_job={k: job[k] for k in ("kind", "market", "symbol")},
                            last_attempt_at=stamp(now), last_error=None)
                try:
                    self._collect(job, context)
                except Exception as error:
                    self._finish(job, "failed", "资料采集未完成：" + type(error).__name__)
                break
            else:
                self._pulse(state="waiting", active_job=None)
            return self.state()
        finally:
            self._tick_lock.release()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception as error:
                with self._state_lock:
                    self._status.update(state="failed", last_error=type(error).__name__)
            self.stop_event.wait(POLL_SECONDS)
        self._pulse(state="stopped", active_job=None)

    def start(self):
        self.initialize()
        if self.is_alive():
            return False
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="alpha-research-collector", daemon=True)
        self.thread.start()
        return True

    def stop(self, timeout=2):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=timeout)
        return not self.is_alive()
