"""Fixed-recipient manual and event notifications with persistent delivery boundaries."""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
import sqlite3
import uuid

from .feishu import FeishuClient, FeishuFailure
from .feishu_config import ConfigFailure, load_config
from .securities import LookupFailure
from .briefing_content import message_body as briefing_message_body
from .quotes import CHINA_TIME
from .live_policy import proof_eligibility

ACTIVE = ("pending", "checking", "sending")


class NotificationService:
    def __init__(self, store, config_path, client=None, clock=None, detail_base_url=None):
        self.store, self.clock = store, clock or (lambda: datetime.now(timezone.utc))
        self.config_path, self._provided_client = config_path, client
        self.config, self.client = None, client
        self.config_error = None
        self.detail_base_url = detail_base_url.rstrip('/') if detail_base_url else None

    def initialize(self):
        # Importing the app (including offline tests) must not open the real vault.
        self.config_error = None
        try:
            self.config = load_config(self.config_path)
        except ConfigFailure as error:
            self.config, self.config_error = None, str(error)
        self.client = self._provided_client or (FeishuClient(self.config) if self.config else None)
        with self.store.connect() as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS notification_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL DEFAULT 0,
                fingerprint TEXT NOT NULL, group_name TEXT, bot_name TEXT,
                verified_at TEXT, check_error TEXT, revision INTEGER NOT NULL DEFAULT 0
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS notification_messages (
                id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                target_fingerprint TEXT NOT NULL, group_name TEXT NOT NULL, bot_name TEXT NOT NULL,
                status TEXT NOT NULL, provider_message_id TEXT, error_message TEXT,
                receipt_hash TEXT NOT NULL, confirmed_at TEXT, confirmation_attempts INTEGER NOT NULL DEFAULT 0
            )""")
            if "revision" not in {row[1] for row in db.execute("PRAGMA table_info(notification_settings)")}:
                db.execute("ALTER TABLE notification_settings ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
            columns = {row[1] for row in db.execute("PRAGMA table_info(notification_messages)")}
            for name, declaration in (("kind", "TEXT NOT NULL DEFAULT 'test'"), ("event_id", "TEXT"), ("expires_at", "TEXT"), ("briefing_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE notification_messages ADD COLUMN {name} {declaration}")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS notification_event_once ON notification_messages(event_id) WHERE event_id IS NOT NULL")
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS notification_briefing_once ON notification_messages(briefing_id) WHERE briefing_id IS NOT NULL")
            fingerprint = self.config.fingerprint if self.config else ""
            db.execute("INSERT OR IGNORE INTO notification_settings (id, fingerprint) VALUES (1, ?)", (fingerprint,))
            settings = db.execute("SELECT * FROM notification_settings WHERE id = 1").fetchone()
            if settings["fingerprint"] != fingerprint:
                db.execute("UPDATE notification_settings SET enabled = 0, fingerprint = ?, verified_at = NULL, check_error = NULL WHERE id = 1", (fingerprint,))
            if self.config:
                db.execute("UPDATE notification_settings SET group_name = ?, bot_name = ? WHERE id = 1", (self.config.group_name, self.config.bot_name))
            now = self.clock().isoformat()
            db.execute("UPDATE notification_messages SET status = 'expired', updated_at = ?, error_message = '提醒已超过有效期，不再发送；事件依据仍保留。' WHERE status IN ('pending','checking') AND expires_at <= ?", (now, now))
            db.execute("UPDATE notification_messages SET status = 'cancelled', updated_at = ?, error_message = '程序重启，未提交的消息已取消；重新开启不会补发。' WHERE status IN ('pending', 'checking')", (now,))
            db.execute("UPDATE notification_messages SET status = 'unknown', updated_at = ?, error_message = '发送途中程序重启，结果待核对，不会自动重发。' WHERE status = 'sending'", (now,))

    def _require_config(self):
        if not self.config or not self.client:
            raise LookupFailure("notification_not_configured", self.config_error or "尚未导入飞书配置，请先在这台电脑运行配置导入脚本。", 409)

    @staticmethod
    def public_record(row):
        # An allowlist keeps receipt hashes and provider identifiers out of the page.
        keys = ("id", "created_at", "updated_at", "group_name", "bot_name", "status", "error_message", "confirmed_at", "kind", "event_id", "expires_at", "briefing_id")
        return {key: row[key] for key in keys}

    def state(self):
        with self.store.connect() as db, db:
            self._expire(db)
            # A lost background task must not leave the page saying "sending" forever.
            cutoff = (self.clock() - timedelta(seconds=120)).isoformat()
            db.execute("UPDATE notification_messages SET status = 'cancelled', error_message = '处理超时，尚未提交的消息已取消。' WHERE ((kind='test' AND status='pending') OR status='checking') AND updated_at < ?", (cutoff,))
            db.execute("UPDATE notification_messages SET status = 'unknown', error_message = '发送超过等待时间，结果待核对，不会自动重发。' WHERE status = 'sending' AND updated_at < ?", (cutoff,))
            settings = dict(db.execute("SELECT * FROM notification_settings WHERE id = 1").fetchone())
            records = db.execute("SELECT * FROM notification_messages ORDER BY created_at DESC, id DESC LIMIT 20").fetchall()
            active = db.execute("SELECT count(*) FROM notification_messages WHERE status IN ('pending', 'checking', 'sending')").fetchone()[0]
            last = db.execute("SELECT max(created_at) FROM notification_messages WHERE kind='test'").fetchone()[0]
        # The manual test's cooldown is independent of automatically queued events.
        cooldown = max(0, int(30 - (self.clock() - datetime.fromisoformat(last)).total_seconds()) + 1) if last else 0
        return {"configured": self.config is not None, "config_error": self.config_error,
                "enabled": bool(settings["enabled"]), "target_fingerprint": settings["fingerprint"],
                "group_name": settings["group_name"], "bot_name": settings["bot_name"],
                "verified_at": settings["verified_at"], "check_error": settings["check_error"],
                "retry_after_seconds": cooldown, "active": bool(active),
                "records": [self.public_record(row) for row in records]}

    def verify(self):
        self._require_config()
        try:
            target = self.client.verify_target()
        except FeishuFailure as error:
            with self.store.connect() as db, db:
                db.execute("UPDATE notification_settings SET check_error = ? WHERE id = 1", (error.message,))
            raise LookupFailure("target_unverified", error.message, 502) from None
        with self.store.connect() as db, db:
            db.execute("UPDATE notification_settings SET group_name = ?, bot_name = ?, verified_at = ?, check_error = NULL WHERE id = 1",
                       (target.group_name, target.bot_name, self.clock().isoformat()))
        return self.state()

    def set_enabled(self, enabled: bool):
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE notification_settings SET revision = revision + 1 WHERE id = 1")
            revision = db.execute("SELECT revision FROM notification_settings WHERE id = 1").fetchone()[0]
            if not enabled:
                db.execute("UPDATE notification_settings SET enabled = 0 WHERE id = 1")
                db.execute("UPDATE notification_messages SET status = 'cancelled', updated_at = ?, error_message = '通知已关闭，此消息尚未提交，已取消；事件仍保留。' WHERE status IN ('pending', 'checking')", (self.clock().isoformat(),))
        if enabled:
            self.verify()
            with self.store.connect() as db, db:
                applied = db.execute("UPDATE notification_settings SET enabled = 1 WHERE id = 1 AND revision = ?", (revision,)).rowcount
            if not applied:
                raise LookupFailure("setting_superseded", "通知设置已被另一操作更新，请查看当前状态。", 409)
        return self.state()

    def queue_test(self, request_id: str, fingerprint: str):
        """Return the record and the one-time body; plaintext code is never persisted."""
        self._require_config()
        now = self.clock()
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM notification_messages WHERE id = ?", (request_id,)).fetchone()
            if old:
                return self.public_record(old), None
            settings = db.execute("SELECT * FROM notification_settings WHERE id = 1").fetchone()
            if not settings["enabled"]:
                raise LookupFailure("notifications_off", "通知已关闭，请先开启。", 409)
            if fingerprint != self.config.fingerprint or settings["fingerprint"] != fingerprint:
                raise LookupFailure("target_changed", "接收配置已经变化，请刷新页面核对后再发送。", 409)
            if db.execute("SELECT 1 FROM notification_messages WHERE status IN ('pending', 'checking', 'sending') LIMIT 1").fetchone():
                raise LookupFailure("notification_busy", "上一条测试仍在处理，请先查看结果。", 409)
            last = db.execute("SELECT created_at FROM notification_messages WHERE kind='test' ORDER BY created_at DESC LIMIT 1").fetchone()
            if last and (now - datetime.fromisoformat(last[0])).total_seconds() < 30:
                raise LookupFailure("notification_cooldown", "两次测试至少间隔 30 秒，请先查看上一条结果。", 429)
            code = f"{secrets.randbelow(1000000):06d}"
            digest = hashlib.sha256((request_id + ":" + code).encode()).hexdigest()
            db.execute("""INSERT INTO notification_messages
                (id, created_at, updated_at, target_fingerprint, group_name, bot_name, status, receipt_hash)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
                       (request_id, now.isoformat(), now.isoformat(), fingerprint, settings["group_name"], settings["bot_name"], digest))
            row = db.execute("SELECT * FROM notification_messages WHERE id = ?", (request_id,)).fetchone()
        local_time = now.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        text = ("【AlphaProject Lab｜通知连通测试】\n"
                "这是一条学习工程测试消息，不是股票异动提醒。\n"
                f"记录标识：{request_id}\n创建时间：{local_time}（北京时间）\n"
                f"收件核验码：{code}\n"
                "看到此消息后，请回到学习版网页的“飞书通知”，填写这条记录的核验码，确认已收到。")
        return self.public_record(row), text

    def _expire(self, db):
        now = self.clock().isoformat()
        db.execute("UPDATE notification_messages SET status='expired',updated_at=?,error_message='提醒已超过有效期，不再发送；事件依据仍保留。' WHERE status IN ('pending','checking') AND expires_at<=?", (now, now))

    def queue_event(self, db, event_id, mode, now):
        """Caller owns the event transaction: no event can commit without its outbox state."""
        existing = db.execute("SELECT * FROM notification_messages WHERE event_id=?", (event_id,)).fetchone()
        if existing:
            return self.public_record(existing)
        settings = db.execute("SELECT * FROM notification_settings WHERE id=1").fetchone()
        enabled = bool(self.config and self.client and settings["enabled"] and settings["fingerprint"] == self.config.fingerprint)
        expires_at = now + timedelta(minutes=5)
        if mode == 'live':
            event = db.execute('SELECT snapshot FROM anomaly_events WHERE id=?', (event_id,)).fetchone()
            snapshot = json.loads(event['snapshot'])
            proof = snapshot.get('eligibility', {}).get('proof')
            eligibility = proof_eligibility(snapshot['quote'], proof, now, snapshot['watchlist_created_at'])
            enabled = enabled and eligibility['eligible']
            # The event's source timestamp, not queue creation, bounds delivery.
            if eligibility['eligible']:
                deadlines = [datetime.fromisoformat(proof[part][key]) + timedelta(seconds=proof['freshness_seconds'])
                             for part in ('previous', 'current') for key in ('source_reported_at', 'received_at')]
                expires_at = min(*deadlines, datetime.fromisoformat(proof['session_end_at']))
            else:
                expires_at = now
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "alphaproject-lab-event:" + event_id))
        db.execute("""INSERT INTO notification_messages
            (id,created_at,updated_at,target_fingerprint,group_name,bot_name,status,receipt_hash,kind,event_id,expires_at,error_message)
            VALUES (?,?,?,?,?,?,?,'',?,?,?,?)""",
                   (message_id, now.isoformat(), now.isoformat(), settings["fingerprint"], settings["group_name"] or "尚未配置",
                    settings["bot_name"] or "尚未配置", "pending" if enabled else "cancelled", "demo" if mode == "demo" else "anomaly",
                    event_id, expires_at.astimezone(timezone.utc).isoformat(), None if enabled else "通知未开启、配置不可用或行情资格已失效；事件已保存，不自动补发。"))
        return self.public_record(db.execute("SELECT * FROM notification_messages WHERE id=?", (message_id,)).fetchone())

    def process_pending_events(self):
        with self.store.connect() as db, db:
            self._expire(db)
            row = db.execute("SELECT id FROM notification_messages WHERE (event_id IS NOT NULL OR briefing_id IS NOT NULL) AND status='pending' ORDER BY created_at,id LIMIT 1").fetchone()
        if row:
            self.process(row["id"])

    def _event_still_wanted(self, db, row):
        if row["briefing_id"]:
            report = db.execute('SELECT * FROM briefing_reports WHERE id=?', (row['briefing_id'],)).fetchone()
            settings = db.execute('SELECT * FROM briefing_settings WHERE id=1').fetchone()
            run = db.execute('SELECT * FROM briefing_schedule_runs WHERE report_id=?', (row['briefing_id'],)).fetchone()
            now = self.clock()
            wanted = bool(report and report['status'] in ('succeeded', 'partial')
                          and report['report_date'] == now.astimezone(CHINA_TIME).date().isoformat()
                          and settings and settings['enabled']
                          and run and run['status'] == 'completed' and run['settings_version'] == settings['version']
                          and now >= datetime.fromisoformat(run['scheduled_at']))
            if wanted:
                for item in json.loads(report['output_json'])['items']:
                    if item['status'] != 'covered':
                        continue
                    current = db.execute('SELECT created_at FROM watchlist WHERE market=? AND symbol=?', (item['market'], item['symbol'])).fetchone()
                    if not current or current['created_at'] != item['created_at']:
                        wanted = False
                        break
            if not wanted:
                db.execute("UPDATE notification_messages SET status='cancelled',updated_at=?,error_message='简报设置、自选或定时记录已变化，尚未提交的简报取消；网页记录保留。' WHERE id=? AND status IN ('pending','checking')", (self.clock().isoformat(), row['id']))
            return wanted
        if not row["event_id"]:
            return True
        event = db.execute("SELECT * FROM anomaly_events WHERE id=?", (row["event_id"],)).fetchone()
        wanted = False
        if event:
            snapshot = json.loads(event["snapshot"])
            item = db.execute("SELECT created_at FROM watchlist WHERE market=? AND symbol=?", (event["market"], event["symbol"])).fetchone()
            wanted = bool(item and item["created_at"] == snapshot["watchlist_created_at"])
            if wanted and event["mode"] == "live":
                extended_kind = snapshot.get('rule_type')
                if extended_kind:
                    from .extended_rules import KIND_FLAG
                    rule = db.execute('SELECT * FROM extended_anomaly_rules WHERE market=? AND symbol=?', (event['market'],event['symbol'])).fetchone()
                    wanted = bool(rule and extended_kind in KIND_FLAG and json.loads(rule['config_json']).get(KIND_FLAG[extended_kind]))
                else:
                    rule = db.execute("SELECT enabled,watchlist_created_at,version FROM anomaly_rules WHERE market=? AND symbol=?", (event["market"], event["symbol"])).fetchone()
                wanted = bool(wanted and rule and rule["enabled"] and rule["watchlist_created_at"] == item["created_at"]
                              and rule['version'] == snapshot['rule_version'])
                if wanted:
                    wanted = self._live_event_is_current(db, event, snapshot)
        if not wanted:
            db.execute("UPDATE notification_messages SET status='cancelled',updated_at=?,error_message='自选、规则或行情资格已变化；待发提醒取消，事件依据保留。' WHERE id=? AND status IN ('pending','checking')", (self.clock().isoformat(), row["id"]))
        return wanted

    def _live_event_is_current(self, db, event, snapshot):
        """Recheck the frozen event and the latest collection before each handoff."""
        try:
            now = self.clock()
            proof = snapshot.get('eligibility', {}).get('proof')
            if not proof_eligibility(snapshot['quote'], proof, now, snapshot['watchlist_created_at'])['eligible']:
                return False
            latest = db.execute('SELECT * FROM quote_cache WHERE market=? AND symbol=?',
                                (event['market'], event['symbol'])).fetchone()
            evidence = db.execute('SELECT * FROM quote_live_evidence WHERE market=? AND symbol=?',
                                  (event['market'], event['symbol'])).fetchone()
            if (not latest or not evidence or latest['last_status'] != 'succeeded'
                    or latest['watchlist_created_at'] != snapshot['watchlist_created_at']
                    or evidence['watchlist_created_at'] != snapshot['watchlist_created_at']):
                return False
            saved = json.loads(evidence['payload'])['eligibility']
            return bool(saved['eligible'] and proof_eligibility(
                json.loads(latest['payload']), saved.get('proof'), now,
                snapshot['watchlist_created_at'])['eligible'])
        except (ValueError, TypeError, KeyError):
            return False

    def process(self, request_id: str, text: str | None = None):
        detail_url = None
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._expire(db)
            row = db.execute("SELECT * FROM notification_messages WHERE id=?", (request_id,)).fetchone()
            if not row or row["status"] != "pending":
                return
            if not self._event_still_wanted(db, row):
                return
            if text is None and row['briefing_id']:
                report = db.execute('SELECT output_json FROM briefing_reports WHERE id=?', (row['briefing_id'],)).fetchone()
                if not report or not report['output_json']:
                    db.execute("UPDATE notification_messages SET status='failed',error_message='简报依据缺失，未提交消息。' WHERE id=?", (request_id,))
                    return
                code = f'{secrets.randbelow(1000000):06d}'
                db.execute('UPDATE notification_messages SET receipt_hash=? WHERE id=?',
                           (hashlib.sha256((request_id + ':' + code).encode()).hexdigest(), request_id))
                text = briefing_message_body(row['briefing_id'], json.loads(report['output_json']))
                text += f'\n发送记录：{request_id}\n收件核验码：{code}\n收到后可回学习版“飞书通知”填写核验码。'
            if text is None:
                event = db.execute("SELECT * FROM anomaly_events WHERE id=?", (row["event_id"],)).fetchone()
                if not event:
                    db.execute("UPDATE notification_messages SET status='failed',error_message='事件依据缺失，未提交消息。' WHERE id=?", (request_id,))
                    return
                snapshot = json.loads(event["snapshot"])
                if self.detail_base_url:
                    detail_url = self.detail_base_url + '/#event=' + str(uuid.UUID(event['id']))
                quote = snapshot["quote"]
                code = f"{secrets.randbelow(1000000):06d}"
                db.execute("UPDATE notification_messages SET receipt_hash=? WHERE id=?", (hashlib.sha256((request_id + ":" + code).encode()).hexdigest(), request_id))
                label = "合成演练 · 非真实行情" if event["mode"] == "demo" else "价格规则提醒"
                comparison = "上涨" if event["direction"] == "up" else "下跌"
                rule_text = f"{comparison}达到 {snapshot['threshold']}%"
                repeat_text = '同日同方向最多一次。'
                if snapshot.get('rule_type'):
                    label = snapshot['rule_label']
                    rule_text = snapshot['condition_text']
                    repeat_text = '同日同类规则最多一次。'
                text = (f"【AlphaProject Lab｜{label}】\n"
                        f"股票：{quote['name']} {event['market']}{event['symbol']}\n"
                        f"现价：{quote['price']} 元；昨收：{quote['previous_close']} 元\n"
                        f"相对昨收：{snapshot['change_pct']}%；规则：{rule_text}\n"
                        f"判断：{snapshot['detection_label']}；{repeat_text}\n"
                        f"来源：{quote['source']}\n来源时间：{quote['source_reported_at']}\n"
                        f"事件标识：{event['id']}\n发送记录：{request_id}\n收件核验码：{code}\n"
                        "这是规则触发结果，不说明涨跌原因。请回网页核对该事件，并在飞书通知中填写核验码。")
            claimed = db.execute("UPDATE notification_messages SET status = 'checking', updated_at = ? WHERE id = ? AND status = 'pending'", (self.clock().isoformat(), request_id)).rowcount
        if not claimed:
            return
        try:
            self.verify()
        except LookupFailure as error:
            self._finish(request_id, "failed", error=error.message, expected="checking")
            return
        except Exception:
            self._finish(request_id, "failed", error="接收群核对未完成，本次未提交消息。", expected="checking")
            return
        # A switch-off while the network check was running cancels the unsent record.
        with self.store.connect() as db, db:
            self._expire(db)
            row = db.execute("SELECT * FROM notification_messages WHERE id=?", (request_id,)).fetchone()
            if not self._event_still_wanted(db, row):
                return
            submitted = db.execute("""UPDATE notification_messages SET status = 'sending', updated_at = ?
                WHERE id = ? AND status = 'checking' AND target_fingerprint = ?
                AND EXISTS (SELECT 1 FROM notification_settings WHERE id = 1 AND enabled = 1 AND fingerprint = ?)""",
                                   (self.clock().isoformat(), request_id, self.config.fingerprint, self.config.fingerprint)).rowcount
        if not submitted:
            return
        try:
            message_id = (self.client.send_test(request_id, text, detail_url=detail_url)
                          if detail_url else self.client.send_test(request_id, text))
        except FeishuFailure as error:
            self._finish(request_id, "unknown" if error.unknown else "failed", error=error.message)
        except Exception:
            # An unexpected exception after handoff cannot safely be called a rejection.
            self._finish(request_id, "unknown", error="发送结果待核对，系统不会自动重发；请到群里查看。")
        else:
            self._finish(request_id, "accepted", message_id=message_id)

    def queue_briefing(self, db, report_id, now, expires_at):
        """Called in the transaction that finishes the one daily scheduled attempt."""
        existing = db.execute('SELECT * FROM notification_messages WHERE briefing_id=?', (report_id,)).fetchone()
        if existing:
            return self.public_record(existing)
        settings = db.execute('SELECT * FROM notification_settings WHERE id=1').fetchone()
        enabled = bool(self.config and self.client and settings['enabled'] and settings['fingerprint'] == self.config.fingerprint)
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'alpha-lab-briefing-message:' + report_id))
        db.execute("""INSERT INTO notification_messages
            (id,created_at,updated_at,target_fingerprint,group_name,bot_name,status,receipt_hash,kind,briefing_id,expires_at,error_message)
            VALUES (?,?,?,?,?,?,?,'','briefing',?,?,?)""",
                   (message_id, now.isoformat(), now.isoformat(), settings['fingerprint'], settings['group_name'] or '尚未配置',
                    settings['bot_name'] or '尚未配置', 'pending' if enabled else 'cancelled', report_id, expires_at.astimezone(timezone.utc).isoformat(),
                    None if enabled else '通知未开启或配置不可用；简报已保存，不自动补发。'))
        return self.public_record(db.execute('SELECT * FROM notification_messages WHERE id=?', (message_id,)).fetchone())

    def _finish(self, request_id, status, error=None, message_id=None, expected="sending"):
        try:
            with self.store.connect() as db, db:
                db.execute("UPDATE notification_messages SET status = ?, updated_at = ?, error_message = ?, provider_message_id = ? WHERE id = ? AND status = ?",
                           (status, self.clock().isoformat(), error, message_id, request_id, expected))
        except sqlite3.Error:
            # Keep the durable sending state. Restart recovery marks it unknown, never resends.
            return

    def confirm(self, request_id, code):
        with self.store.connect() as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM notification_messages WHERE id = ?", (request_id,)).fetchone()
            if not row:
                raise LookupFailure("notification_not_found", "未找到这条测试记录，请刷新后重试。", 404)
            if row["confirmed_at"]:
                return self.public_record(row)
            if row["status"] not in ("accepted", "unknown"):
                raise LookupFailure("notification_not_ready", "这条测试尚未进入可核对状态，请先等待处理结果。", 409)
            if row["confirmation_attempts"] >= 10:
                raise LookupFailure("receipt_attempt_limit", "这条记录已核对 10 次，请直接核对飞书消息后另发一条测试。", 429)
            correct = hmac.compare_digest(row["receipt_hash"], hashlib.sha256((request_id + ":" + code).encode()).hexdigest())
            db.execute("UPDATE notification_messages SET confirmation_attempts = confirmation_attempts + 1 WHERE id = ?", (request_id,))
            if correct:
                db.execute("UPDATE notification_messages SET confirmed_at = ? WHERE id = ?", (self.clock().isoformat(), request_id))
            result = db.execute("SELECT * FROM notification_messages WHERE id = ?", (request_id,)).fetchone()
        if not correct:
            raise LookupFailure("receipt_mismatch", "核验码不匹配，请核对飞书里相同记录标识的消息。", 400)
        return self.public_record(result)
