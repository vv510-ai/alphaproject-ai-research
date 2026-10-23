"""Persist rules and immutable event snapshots; run a finite, explicitly synthetic lesson."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import base64
import binascii
import json
import logging
import threading
import time
import uuid

from .anomaly_rules import DEMO_FRESHNESS_SECONDS, Sample, inspect_sample, meets, threshold
from .live_policy import FRESHNESS_SECONDS, proof_eligibility, session_context
from .quotes import CHINA_TIME, Quote
from .securities import LookupFailure, market_for

POLL_SECONDS = 5  # Cache/evidence checks only; the independent collector fetches every 60s.
DEMO_STEP_SECONDS = 2
logger = logging.getLogger("alphaproject_lab")


class AnomalyService:
    def __init__(self, store, quotes, notifications, clock=None, detail_prefix=''):
        self.store, self.quotes, self.notifications = store, quotes, notifications
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.stop_event = threading.Event()
        self.thread = None
        self.detail_prefix = detail_prefix
        self.extended = None

    def initialize(self):
        with self.store.connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS anomaly_rules (
                    market TEXT NOT NULL, symbol TEXT NOT NULL, watchlist_created_at TEXT NOT NULL,
                    up_threshold TEXT NOT NULL, down_threshold TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL, updated_at TEXT NOT NULL, last_checked_at TEXT, last_result TEXT,
                    PRIMARY KEY (market,symbol),
                    FOREIGN KEY (market,symbol) REFERENCES watchlist(market,symbol) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS anomaly_observations (
                    mode TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
                    last_source_at TEXT NOT NULL, trade_date TEXT NOT NULL, rule_version INTEGER NOT NULL,
                    watchlist_created_at TEXT NOT NULL, PRIMARY KEY (mode,market,symbol),
                    FOREIGN KEY (market,symbol) REFERENCES watchlist(market,symbol) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    id TEXT PRIMARY KEY, mode TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
                    direction TEXT NOT NULL, trade_date TEXT NOT NULL, created_at TEXT NOT NULL,
                    snapshot TEXT NOT NULL, run_id TEXT,
                    UNIQUE (mode,market,symbol,direction,trade_date)
                );
                CREATE TABLE IF NOT EXISTS anomaly_runs (
                    id TEXT PRIMARY KEY, market TEXT NOT NULL, symbol TEXT NOT NULL,
                    watchlist_created_at TEXT NOT NULL, rule_version INTEGER NOT NULL,
                    direction TEXT NOT NULL, notify INTEGER NOT NULL, threshold TEXT NOT NULL,
                    created_at TEXT NOT NULL, next_at TEXT NOT NULL, status TEXT NOT NULL,
                    step INTEGER NOT NULL DEFAULT 0, results TEXT NOT NULL DEFAULT '[]', error_message TEXT
                );
                CREATE TABLE IF NOT EXISTS anomaly_runtime (
                    id INTEGER PRIMARY KEY CHECK (id=1), heartbeat_at TEXT,
                    checks INTEGER NOT NULL DEFAULT 0, last_error TEXT
                );
                INSERT OR IGNORE INTO anomaly_runtime (id) VALUES (1);
            """)
            db.execute("UPDATE anomaly_runs SET status='cancelled', error_message='服务重启，未完成的演练已停止；已经保存的事件保留，不自动重跑。' WHERE status IN ('pending','running')")

    def save_rule(self, market, symbol, generation, up, down, enabled, expected_version):
        if market_for(symbol) != market:
            raise LookupFailure("invalid_symbol", "市场与股票代码不一致。", 400)
        up, down = format(threshold(up), "f"), format(threshold(down), "f")
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT * FROM watchlist WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            if not item or item["created_at"] != generation:
                raise LookupFailure("watchlist_changed", "自选记录已经变化，请刷新后再保存。", 409)
            old = db.execute("SELECT * FROM anomaly_rules WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            version = old["version"] if old and old["watchlist_created_at"] == generation else 0
            if version != expected_version:
                raise LookupFailure("rule_changed", "规则已被另一操作修改，请重新读取后再保存。", 409)
            db.execute("""INSERT INTO anomaly_rules (market,symbol,watchlist_created_at,up_threshold,down_threshold,enabled,version,updated_at)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT (market,symbol) DO UPDATE SET
                watchlist_created_at=excluded.watchlist_created_at,up_threshold=excluded.up_threshold,
                down_threshold=excluded.down_threshold,enabled=excluded.enabled,version=excluded.version,
                updated_at=excluded.updated_at,last_checked_at=NULL,last_result=NULL""",
                       (market, symbol, generation, up, down, int(enabled), version + 1, self.clock().isoformat()))
            return dict(db.execute("SELECT * FROM anomaly_rules WHERE market=? AND symbol=?", (market, symbol)).fetchone())

    @staticmethod
    def public_run(row):
        result = dict(row)
        result["notify"] = bool(result["notify"])
        result["results"] = json.loads(result["results"])
        return result

    def state(self):
        dashboard = self.quotes.dashboard()
        quote_views = {(r["market"], r["symbol"]): r for r in dashboard["items"]}
        with self.store.connect() as db:
            rules = {(r["market"], r["symbol"]): dict(r) for r in db.execute("SELECT * FROM anomaly_rules")}
            items = []
            for row in db.execute("SELECT * FROM watchlist ORDER BY display_order,created_at DESC"):
                rule = rules.get((row["market"], row["symbol"]))
                if rule and rule["watchlist_created_at"] != row["created_at"]:
                    rule = None
                view = quote_views.get((row["market"], row["symbol"]))
                quote = view["quote"] if view and view["created_at"] == row["created_at"] else self.quotes._view(None, self.clock())
                items.append({**dict(row), "rule": rule, "quote": quote})
            events = []
            for row in db.execute("SELECT * FROM anomaly_events ORDER BY created_at DESC,id DESC LIMIT 40"):
                events.append(self._public_event(db, row))
            runs = [self.public_run(row) for row in db.execute("SELECT * FROM anomaly_runs ORDER BY created_at DESC,id DESC LIMIT 10")]
            runtime = dict(db.execute("SELECT * FROM anomaly_runtime WHERE id=1").fetchone())
        heartbeat = runtime["heartbeat_at"]
        runtime["running"] = bool(heartbeat and 0 <= (self.clock() - datetime.fromisoformat(heartbeat)).total_seconds() < 90)
        enabled = [item for item in items if item["rule"] and item["rule"]["enabled"]]
        ready = [item for item in enabled if item["quote"]["eligibility"]["eligible"]]
        context = session_context(self.clock())
        if ready:
            reason = f"{len(ready)} 只已启用规则的股票具备最近成交证据，正在判断阈值；不代表官方停牌状态。"
        elif context["state"] != "trading":
            reason = context["reason"]
        elif not enabled:
            reason = "真实规则尚未开启；自选报价仍按后台采集状态更新。"
        else:
            reason = "真实检测暂停：" + enabled[0]["quote"]["eligibility"]["reason"]
        return {"items": items, "events": events, "runs": runs, "runtime": runtime,
                "live_ready": bool(ready), "live_pause_reason": reason, "check_interval_seconds": POLL_SECONDS,
                "collector": dashboard["collector"],
                "demo_freshness_seconds": DEMO_FRESHNESS_SECONDS}

    def _public_event(self, db, row):
        event = dict(row)
        event['snapshot'] = json.loads(event['snapshot'])
        event['snapshot']['quote'].pop('raw_text', None)
        message = db.execute('SELECT * FROM notification_messages WHERE event_id=?', (row['id'],)).fetchone()
        event['notification'] = self.notifications.public_record(message) if message else None
        event['detail_path'] = self.detail_prefix + '/#event=' + row['id']
        return event

    def event(self, event_id):
        with self.store.connect() as db:
            row = db.execute('SELECT * FROM anomaly_events WHERE id=?', (event_id,)).fetchone()
            if not row:
                raise LookupFailure('event_not_found', '这条事件不存在，请核对链接或回异动列表选择。', 404)
            return self._public_event(db, row)

    @staticmethod
    def _history_time(value):
        if not isinstance(value, str) or len(value) > 64:
            raise ValueError("invalid timestamp")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        return parsed.astimezone(timezone.utc).isoformat()

    def event_history(self, since, cursor=None, limit=100):
        """Read live history without the UI's 40-event cap or notification writes."""
        try:
            normalized_since = self._history_time(since)
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
                raise ValueError("invalid limit")
            after = None
            through = self.clock().astimezone(timezone.utc).isoformat()
            if cursor is not None:
                if not isinstance(cursor, str) or not 1 <= len(cursor) <= 1024:
                    raise ValueError("invalid cursor")
                decoded = base64.b64decode(cursor.encode('ascii'), altchars=b'-_', validate=True)
                value = json.loads(decoded)
                if not isinstance(value, dict) or set(value) != {'since', 'through', 'created_at', 'id'}:
                    raise ValueError("invalid cursor fields")
                if value['since'] != normalized_since:
                    raise ValueError("cursor belongs to another date range")
                through = self._history_time(value['through'])
                self._history_time(value['created_at'])
                if not isinstance(value['id'], str) or str(uuid.UUID(value['id'])) != value['id']:
                    raise ValueError("invalid event id")
                after = value
        except (ValueError, TypeError, OverflowError, UnicodeError, binascii.Error):
            raise LookupFailure('invalid_event_history', '历史查询时间、游标或数量无法核对。', 400)
        conditions = ["mode='live'", 'julianday(created_at)>=julianday(?)', 'julianday(created_at)<=julianday(?)']
        parameters = [normalized_since, through]
        if after:
            conditions.append('''(julianday(created_at)>julianday(?) OR
                (julianday(created_at)=julianday(?) AND
                 (created_at>? OR (created_at=? AND id>?))))''')
            parameters.extend([after['created_at']] * 4 + [after['id']])
        query = ('SELECT * FROM anomaly_events WHERE ' + ' AND '.join(conditions)
                 + ' ORDER BY julianday(created_at),created_at,id LIMIT ?')
        with self.store.connect() as db:
            db.execute('BEGIN')
            rows = db.execute(query, (*parameters, limit + 1)).fetchall()
            events = [self._public_event(db, row) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            value = {'since': normalized_since, 'through': through,
                     'created_at': last['created_at'], 'id': last['id']}
            next_cursor = base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode()).decode('ascii')
        return {'events': events, 'event_history': {'since': since, 'through': through,
                'next_cursor': next_cursor, 'complete': next_cursor is None}, 'as_of': self.clock().isoformat()}

    def queue_demo(self, request_id, market, symbol, generation, expected_version, direction, notify):
        now = self.clock()
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM anomaly_runs WHERE id=?", (request_id,)).fetchone()
            if existing:
                return self.public_run(existing)
            item = db.execute("SELECT * FROM watchlist WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            rule = db.execute("SELECT * FROM anomaly_rules WHERE market=? AND symbol=?", (market, symbol)).fetchone()
            if not item or item["created_at"] != generation or not rule or rule["watchlist_created_at"] != generation or rule["version"] != expected_version:
                raise LookupFailure("demo_rule_changed", "请先保存这只股票的规则，再按最新规则演练。", 409)
            if direction not in ("up", "down"):
                raise LookupFailure("invalid_direction", "请选择上涨或下跌演练。", 422)
            if db.execute("SELECT 1 FROM anomaly_runs WHERE status IN ('pending','running')").fetchone():
                raise LookupFailure("demo_busy", "上一轮演练尚未结束，请先看完三个步骤。", 409)
            value = rule["up_threshold" if direction == "up" else "down_threshold"]
            # Keep all three teaching prices positive, including the below-threshold sample.
            if not Decimal("0.01") < Decimal(value) < Decimal("99"):
                raise LookupFailure("demo_threshold_range", "三步演练需要阈值大于 0.01%、小于 99%；真实规则不受这个演练条件限制。", 422)
            if notify:
                self.notifications._require_config()
                if not db.execute("SELECT enabled FROM notification_settings WHERE id=1").fetchone()[0]:
                    raise LookupFailure("notifications_off", "请先在飞书通知中开启通道，或选择只在网页演练。", 409)
            db.execute("""INSERT INTO anomaly_runs
                (id,market,symbol,watchlist_created_at,rule_version,direction,notify,threshold,created_at,next_at,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,'pending')""",
                       (request_id, market, symbol, generation, expected_version, direction, int(notify), value,
                        now.isoformat(), (now + timedelta(seconds=DEMO_STEP_SECONDS)).isoformat()))
            return self.public_run(db.execute("SELECT * FROM anomaly_runs WHERE id=?", (request_id,)).fetchone())

    def evaluate(self, rule, sample: Sample, mode="live", directions=("up", "down"), run_id=None, notify=True):
        """Recheck current identity/rule and save event + outbox in one short transaction."""
        now = self.clock()
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            item = db.execute("SELECT * FROM watchlist WHERE market=? AND symbol=?", (rule["market"], rule["symbol"])).fetchone()
            current = db.execute("SELECT * FROM anomaly_rules WHERE market=? AND symbol=?", (rule["market"], rule["symbol"])).fetchone()
            if not item or item["created_at"] != rule["watchlist_created_at"] or not current or current["version"] != rule["version"] or current["watchlist_created_at"] != rule["watchlist_created_at"] or (mode == "live" and not current["enabled"]):
                return [{"status": "cancelled", "reason": "自选或规则已变化，本次停止检查。"}]
            observation = db.execute("SELECT * FROM anomaly_observations WHERE mode=? AND market=? AND symbol=?",
                                     (mode, rule["market"], rule["symbol"])).fetchone()
            reason, change = inspect_sample(sample, rule["market"], rule["symbol"], now,
                                            observation["last_source_at"] if observation else None, mode=mode)
            if not reason and mode == "live":
                # The dashboard can be read just before a failure/removal/new refresh.
                # Recheck the committed evidence under the event transaction as well.
                saved = db.execute("SELECT * FROM quote_live_evidence WHERE market=? AND symbol=?", (rule["market"], rule["symbol"])).fetchone()
                cached = db.execute("SELECT * FROM quote_cache WHERE market=? AND symbol=?", (rule["market"], rule["symbol"])).fetchone()
                try:
                    evidence = json.loads(saved["payload"]) if saved else {}
                    eligibility = evidence.get("eligibility", {})
                    proof = eligibility.get("proof")
                    latest = json.loads(cached["payload"]) if cached and cached["payload"] else None
                    if (not saved or not cached or saved["watchlist_created_at"] != rule["watchlist_created_at"]
                            or cached["watchlist_created_at"] != rule["watchlist_created_at"] or cached["last_status"] != "succeeded"
                            or not eligibility.get("eligible") or proof != sample.eligibility.get("proof")):
                        reason = "最新采集资格已变化，等待当前有效证据。"
                    else:
                        checked = proof_eligibility(latest, proof, now, rule["watchlist_created_at"])
                        if not checked["eligible"]:
                            reason = checked["reason"]
                except (TypeError, ValueError, AttributeError):
                    reason = "最新采集证据无法核对，暂停本次检测。"
            if reason:
                if mode == "live":
                    db.execute("UPDATE anomaly_rules SET last_checked_at=?,last_result=? WHERE market=? AND symbol=?",
                               (now.isoformat(), reason, rule["market"], rule["symbol"]))
                return [{"status": "blocked", "reason": reason}]
            quote = sample.quote
            trade_date = datetime.fromisoformat(quote.source_reported_at).astimezone(CHINA_TIME).date().isoformat()
            first = not observation or observation["trade_date"] != trade_date or observation["rule_version"] != rule["version"] or observation["watchlist_created_at"] != rule["watchlist_created_at"]
            db.execute("""INSERT INTO anomaly_observations (mode,market,symbol,last_source_at,trade_date,rule_version,watchlist_created_at)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT (mode,market,symbol) DO UPDATE SET last_source_at=excluded.last_source_at,
                trade_date=excluded.trade_date,rule_version=excluded.rule_version,watchlist_created_at=excluded.watchlist_created_at""",
                       (mode, rule["market"], rule["symbol"], quote.source_reported_at, trade_date, rule["version"], rule["watchlist_created_at"]))
            results = []
            for direction in directions:
                value = rule["up_threshold" if direction == "up" else "down_threshold"]
                if not meets(change, direction, value):
                    results.append({"status": "below", "reason": "未达到这条规则的阈值", "direction": direction})
                    continue
                old = db.execute("SELECT id FROM anomaly_events WHERE mode=? AND market=? AND symbol=? AND direction=? AND trade_date=?",
                                 (mode, rule["market"], rule["symbol"], direction, trade_date)).fetchone()
                if old:
                    results.append({"status": "duplicate", "reason": "同日同方向已有事件，不重复记录或推送", "direction": direction, "event_id": old["id"]})
                    continue
                event_id = str(uuid.uuid4())
                snapshot = {"quote": quote.to_dict(), "change_pct": format(change, "f"), "direction": direction,
                            "threshold": value, "rule_version": rule["version"], "up_threshold": rule["up_threshold"],
                            "down_threshold": rule["down_threshold"], "watchlist_created_at": rule["watchlist_created_at"],
                            "first_observation": first, "detection_label": "首次观察已满足" if first else "本次采样已达到阈值",
                            "mode": mode, "market_state": sample.market_state,
                            "freshness_limit_seconds": FRESHNESS_SECONDS if mode == "live" else DEMO_FRESHNESS_SECONDS,
                            "formula": "(现价 - 昨收) ÷ 昨收 × 100%", "repeat_policy": "同一股票、同一方向、同一日期最多一次"}
                if mode == "live":
                    snapshot["eligibility"] = sample.eligibility
                    snapshot.update({key: sample.eligibility["proof"][key] for key in
                                     ("session_id", "session_end_at", "calendar_version")})
                db.execute("INSERT INTO anomaly_events (id,mode,market,symbol,direction,trade_date,created_at,snapshot,run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                           (event_id, mode, rule["market"], rule["symbol"], direction, trade_date, now.isoformat(), json.dumps(snapshot, ensure_ascii=False, allow_nan=False), run_id))
                if mode == "live" or notify:
                    self.notifications.queue_event(db, event_id, mode, now)
                results.append({"status": "triggered", "reason": snapshot["detection_label"], "direction": direction, "event_id": event_id})
            if mode == "live":
                db.execute("UPDATE anomaly_rules SET last_checked_at=?,last_result=? WHERE market=? AND symbol=?",
                           (now.isoformat(), "；".join(r["reason"] for r in results), rule["market"], rule["symbol"]))
            return results

    def advance_demo(self):
        now = self.clock()
        with self.store.connect() as db:
            run = db.execute("SELECT * FROM anomaly_runs WHERE status IN ('pending','running') AND next_at<=? ORDER BY created_at LIMIT 1", (now.isoformat(),)).fetchone()
            if not run:
                return
            run = dict(run)
            rule = db.execute("SELECT * FROM anomaly_rules WHERE market=? AND symbol=?", (run["market"], run["symbol"])).fetchone()
            item = db.execute("SELECT * FROM watchlist WHERE market=? AND symbol=?", (run["market"], run["symbol"])).fetchone()
        reason = None
        if (now - datetime.fromisoformat(run["created_at"])).total_seconds() > 300:
            reason = "演练等待超过 5 分钟，已停止，不补跑过时步骤。"
        elif not rule or not item or rule["version"] != run["rule_version"] or item["created_at"] != run["watchlist_created_at"]:
            reason = "自选或规则已变化，演练停止；已有事件保留。"
        if reason:
            with self.store.connect() as db, db:
                db.execute("UPDATE anomaly_runs SET status='cancelled',error_message=? WHERE id=?", (reason, run["id"]))
            return
        level, step = Decimal(run["threshold"]), run["step"]
        change = [level - Decimal("0.01"), level, level + Decimal("0.20")][step]
        if run["direction"] == "down":
            change = -change
        quote = Quote(run["market"], run["symbol"], item["name"], format(Decimal(100) + change, "f"), "100",
                      format(change, "f"), format(change, "f"), now.isoformat(), now.isoformat(),
                      "教学合成数据 · 非真实行情", "", "synthetic lesson sample")
        result = self.evaluate(dict(rule), Sample(quote, "trading", True, True), "demo", (run["direction"],), run["id"], bool(run["notify"]))[0]
        results = json.loads(run["results"])
        results.append({"step": step + 1, "price": quote.price, "previous_close": "100", "change_pct": quote.change_pct,
                        "checked_at": now.isoformat(), **result})
        cancelled = result["status"] == "cancelled"
        with self.store.connect() as db, db:
            db.execute("UPDATE anomaly_runs SET status=?,step=?,results=?,next_at=?,error_message=? WHERE id=? AND step=?",
                       ("cancelled" if cancelled else "completed" if step == 2 else "running", step + 1, json.dumps(results, ensure_ascii=False),
                        (now + timedelta(seconds=DEMO_STEP_SECONDS)).isoformat(), result["reason"] if cancelled else None, run["id"], step))

    def check_live(self):
        # This worker only reads committed evidence; network I/O belongs to collector.
        dashboard = {(r["market"], r["symbol"]): r for r in self.quotes.dashboard()["items"]}
        with self.store.connect() as db:
            rules = [dict(r) for r in db.execute("SELECT * FROM anomaly_rules WHERE enabled=1")]
        for rule in rules:
            view = dashboard.get((rule["market"], rule["symbol"]))
            quote_view = view["quote"] if view else {}
            data = quote_view.get("data")
            try:
                quote = Quote(**{**data, "raw_text": ""}) if data else None
                self.evaluate(rule, Sample(quote, quote_view.get("market_state", "unknown"),
                                           quote_view.get("realtime_verified", False), False, quote_view.get("eligibility")))
            except Exception as failure:
                # One corrupt rule must not prevent other stocks or pending sends.
                logger.error("Live rule check failed for %s%s: %s", rule["market"], rule["symbol"], type(failure).__name__)
                with self.store.connect() as db, db:
                    db.execute("UPDATE anomaly_rules SET last_checked_at=?,last_result=? WHERE market=? AND symbol=?",
                               (self.clock().isoformat(), "这条规则本次检查未完成，已有事件保留。", rule["market"], rule["symbol"]))
        if self.extended is not None:
            self.extended.check_live()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="alpha-lab-anomalies", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)

    def _run(self):
        next_check = 0.0
        while not self.stop_event.is_set():
            error = None
            try:
                self.advance_demo()
                if time.monotonic() >= next_check:
                    self.check_live()
                    next_check = time.monotonic() + POLL_SECONDS
                self.notifications.process_pending_events()
            except Exception as failure:
                logger.error("Anomaly worker cycle failed: %s", type(failure).__name__)
                error = "本次后台检查未完成，已有事件保留；请查看规则与通知记录。"
            try:
                with self.store.connect() as db, db:
                    db.execute("UPDATE anomaly_runtime SET heartbeat_at=?,checks=checks+1,last_error=? WHERE id=1", (self.clock().isoformat(), error))
            except Exception as failure:
                logger.error("Anomaly heartbeat write failed: %s", type(failure).__name__)
            self.stop_event.wait(1)
