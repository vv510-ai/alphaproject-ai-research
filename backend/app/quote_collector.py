"""A bounded quote worker, independent of browsers, notifications and briefings."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
import json
import logging
import threading
import uuid

from .live_policy import COLLECT_INTERVAL_SECONDS, FRESHNESS_SECONDS, session_context
from .securities import LookupFailure

MAX_CONCURRENCY = 4
MAX_PER_CYCLE = 20
logger = logging.getLogger("alphaproject_lab")


class QuoteCollector:
    def __init__(self, store, quotes, clock=None):
        self.store, self.quotes = store, quotes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.thread = None
        self.stop_event = threading.Event()
        self._cycle_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_due = None
        self._last_session = None
        self._status = {"running": False, "heartbeat_at": None, "last_cycle_at": None, "cycle_id": None,
                        "next_cycle_at": None, "state": "stopped", "label": "后台采集尚未启动",
                        "interval_seconds": COLLECT_INTERVAL_SECONDS, "freshness_seconds": FRESHNESS_SECONDS,
                        "total": 0, "attempted": 0, "succeeded": 0, "failed": 0, "last_error": None}

    def is_alive(self):
        return bool(self.thread and self.thread.is_alive())

    def state(self):
        with self._state_lock:
            value = dict(self._status)
        heartbeat = value["heartbeat_at"]
        try:
            current = bool(heartbeat and 0 <= (self.clock() - datetime.fromisoformat(heartbeat)).total_seconds() < 10)
        except (TypeError, ValueError):
            current = False
        value["running"] = self.is_alive() and current
        return value

    def _pulse(self, **changes):
        with self._state_lock:
            self._status.update(changes)
            self._status.update(heartbeat_at=self.clock().isoformat(),
                                running=self.is_alive() and self._status["state"] != "stopped")
            payload = json.dumps(self._status, ensure_ascii=False, allow_nan=False)
        with self.store.connect() as db, db:
            db.execute("UPDATE quote_collector_runtime SET payload=? WHERE id=1", (payload,))

    def _fetch(self, item, cycle_id):
        # A cycle can straddle lunch/close or shutdown while jobs wait for a slot.
        if self.stop_event.is_set() or session_context(self.clock())["state"] != "trading":
            return False, "已到暂停时段或服务正在停止，本次未取数。"
        try:
            result = self.quotes.refresh(item["market"], item["symbol"], expected_generation=item["created_at"],
                                         collector_cycle_id=cycle_id)
            return result["refresh_succeeded"], result["quote"].get("error_message")
        except LookupFailure as error:
            return False, error.message
        except Exception as error:
            logger.warning("Quote collection failed for %s%s: %s", item["market"], item["symbol"], type(error).__name__)
            return False, "这只股票本次采集未完成；旧报价保留，等待下轮重试。"

    def tick(self):
        """One scheduler tick; an in-progress cycle is never overlapped."""
        if not self._cycle_lock.acquire(blocking=False):
            return self.state()
        try:
            now = self.clock()
            context = session_context(now)
            with self.store.connect() as db:
                total = db.execute("SELECT count(*) FROM watchlist").fetchone()[0]
            if context["state"] != "trading":
                self._next_due = None
                self._last_session = None
                self._pulse(state=context["state"], label=context["reason"], total=total, next_cycle_at=None)
                return self.state()
            if not total:
                self._next_due = None
                self._pulse(state="empty", label="尚无自选，等待添加股票。", total=0, next_cycle_at=None,
                            attempted=0, succeeded=0, failed=0, last_error=None)
                return self.state()
            if self._last_session != context["session_id"]:
                self._next_due = None
                self._last_session = context["session_id"]
            if self._next_due and now < self._next_due:
                self._pulse(total=total)
                return self.state()
            self._next_due = now + timedelta(seconds=COLLECT_INTERVAL_SECONDS)
            # Sort by oldest attempt, including failed attempts, so >20 names rotate fairly.
            with self.store.connect() as db:
                items = [dict(row) for row in db.execute("""
                    SELECT w.market,w.symbol,w.created_at FROM watchlist w
                    LEFT JOIN quote_cache c ON c.market=w.market AND c.symbol=w.symbol
                        AND c.watchlist_created_at=w.created_at
                    WHERE julianday(c.last_attempt_at) IS NULL OR julianday(c.last_attempt_at)<=julianday(?)
                    ORDER BY coalesce(julianday(c.last_attempt_at),0),w.created_at,w.market,w.symbol LIMIT ?
                """, ((now - timedelta(seconds=30)).isoformat(), MAX_PER_CYCLE))]
            cycle_id = uuid.uuid4().hex
            self._pulse(state="collecting", label="正在获取自选报价；只有证据合格的数据参与提醒。", total=total,
                        cycle_id=cycle_id,
                        last_cycle_at=now.isoformat(), next_cycle_at=self._next_due.isoformat(),
                        attempted=len(items), succeeded=0, failed=0, last_error=None)
            succeeded, failed, last_error = 0, 0, None
            # No source request holds a SQLite transaction. The scheduler remains able
            # to heartbeat each second even if the bounded source calls take 50 seconds.
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENCY, thread_name_prefix="alpha-lab-quote") as executor:
                pending = {executor.submit(self._fetch, item, cycle_id) for item in items}
                while pending:
                    done, pending = wait(pending, timeout=1, return_when=FIRST_COMPLETED)
                    for future in done:
                        accepted, error = future.result()
                        succeeded += int(accepted)
                        failed += int(not accepted)
                        if error:
                            last_error = error
                    self._pulse(succeeded=succeeded, failed=failed, last_error=last_error)
            context = session_context(self.clock())
            if context["state"] != "trading":
                self._next_due = None
                self._pulse(state=context["state"], label=context["reason"], next_cycle_at=None)
            else:
                self._pulse(state="waiting", label="本轮采集结束，等待下一轮；60 秒为初始工程间隔。",
                            succeeded=succeeded, failed=failed, last_error=last_error)
            return self.state()
        finally:
            self._cycle_lock.release()

    def start(self):
        if self.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="alpha-lab-quotes", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception as error:
                    logger.error("Quote collector cycle failed: %s", type(error).__name__)
                    try:
                        self._pulse(state="error", label="本轮采集未完成，等待下轮检查。",
                                    last_error="后台采集未完成，已有报价与事件保留。")
                    except Exception as storage_error:
                        logger.error("Quote collector status failed: %s", type(storage_error).__name__)
                self.stop_event.wait(1)
        finally:
            try:
                self._pulse(state="stopped", label="后台采集已停止。", next_cycle_at=None)
            except Exception as error:
                logger.error("Quote collector shutdown status failed: %s", type(error).__name__)
