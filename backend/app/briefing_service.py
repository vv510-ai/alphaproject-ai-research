"""Durable manual and scheduled morning summaries, consumed by one local worker."""

from datetime import date, datetime, time, timedelta, timezone
import json
import logging
import threading
import time as monotonic_time
import uuid

from .briefing_content import encoded, freeze_items, from_history, list_digest, make_summary, missing
from .quotes import CHINA_TIME
from .securities import LookupFailure
from .trading_calendar import TradingCalendar
from .analysis_context import freeze_research

ACTIVE = ('pending', 'running')
READY = ('succeeded', 'partial')
GENERATION_SECONDS = 90
START_WINDOW_SECONDS = 60
SEND_WINDOW_MINUTES = 15
logger = logging.getLogger('alphaproject_lab')


class BriefingService:
    def __init__(self, store, daily, notifications, clock=None, calendar=None, research=None):
        self.store, self.daily, self.notifications = store, daily, notifications
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.calendar = calendar or TradingCalendar()
        self.stop_event = threading.Event()
        self.thread = None
        self.generation_thread = None
        self.research = research

    def initialize(self):
        with self.store.connect() as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS briefing_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
                    send_time TEXT NOT NULL DEFAULT '08:45', version INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS briefing_reports (
                    id TEXT PRIMARY KEY, report_date TEXT NOT NULL, trigger_kind TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL, output_json TEXT, retry_of TEXT,
                    error_code TEXT, error_message TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS briefing_ready_day ON briefing_reports(report_date)
                    WHERE status IN ('succeeded','partial');
                CREATE UNIQUE INDEX IF NOT EXISTS briefing_active_day ON briefing_reports(report_date)
                    WHERE status IN ('pending','running');
                CREATE TABLE IF NOT EXISTS briefing_requests (
                    request_id TEXT PRIMARY KEY, report_date TEXT NOT NULL, retry_of TEXT, report_id TEXT NOT NULL,
                    FOREIGN KEY(report_id) REFERENCES briefing_reports(id)
                );
                CREATE TABLE IF NOT EXISTS briefing_schedule_runs (
                    report_date TEXT PRIMARY KEY, scheduled_at TEXT NOT NULL, claimed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, settings_version INTEGER NOT NULL,
                    status TEXT NOT NULL, report_id TEXT, reason TEXT
                );
                CREATE TABLE IF NOT EXISTS briefing_runtime (
                    id INTEGER PRIMARY KEY CHECK(id=1), heartbeat_at TEXT, last_error TEXT,
                    checks INTEGER NOT NULL DEFAULT 0
                );
                INSERT OR IGNORE INTO briefing_runtime(id) VALUES (1);
            """)
            now = self.clock().isoformat()
            db.execute('INSERT OR IGNORE INTO briefing_settings(id,updated_at) VALUES (1,?)', (now,))
            db.execute("UPDATE briefing_reports SET status='interrupted',updated_at=?,error_code='restart_interrupted',error_message='服务重启，未完成的简报已中断；可手动重试，不自动补发。' WHERE status IN ('pending','running')", (now,))
            db.execute("UPDATE briefing_schedule_runs SET status='interrupted',updated_at=?,reason='服务重启，已领取但未完成的定时任务不重跑、不补推。' WHERE status='waiting'", (now,))

    @staticmethod
    def _due(day, send_time):
        hour, minute = map(int, send_time.split(':'))
        return datetime.combine(day, time(hour, minute), CHINA_TIME)

    @staticmethod
    def _items(db):
        return freeze_items(db.execute('SELECT * FROM watchlist ORDER BY created_at DESC,market,symbol').fetchall())

    def public_report(self, db, row):
        if row is None:
            return None
        result = dict(row)
        result['snapshot'] = json.loads(result.pop('snapshot_json'))
        result['output'] = json.loads(result.pop('output_json')) if result['output_json'] else None
        # Pop a null serialized field too; the wire contract is always output, never output_json.
        result.pop('output_json', None)
        message = db.execute('SELECT * FROM notification_messages WHERE briefing_id=?', (row['id'],)).fetchone()
        result['notification'] = self.notifications.public_record(message) if message else None
        result['delivery_status'] = (result['notification']['status'] if result['notification'] else
                                     'local_only' if row['trigger_kind'] == 'manual' else 'not_queued')
        result['watchlist_changed'] = result['snapshot']['watchlist_hash'] != list_digest(self._items(db))
        return result

    def state(self):
        now = self.clock()
        today = now.astimezone(CHINA_TIME).date()
        calendar = self.calendar.context(today)
        with self.store.connect() as db:
            settings = dict(db.execute('SELECT * FROM briefing_settings WHERE id=1').fetchone())
            settings['enabled'] = bool(settings['enabled'])
            run = db.execute('SELECT * FROM briefing_schedule_runs WHERE report_date=?', (today.isoformat(),)).fetchone()
            reports = [self.public_report(db, row) for row in db.execute('SELECT * FROM briefing_reports ORDER BY created_at DESC,rowid DESC LIMIT 20').fetchall()]
            runtime = dict(db.execute('SELECT * FROM briefing_runtime WHERE id=1').fetchone())
            notification_settings = db.execute('SELECT enabled FROM notification_settings WHERE id=1').fetchone()
            active = db.execute("SELECT count(*) FROM briefing_reports WHERE status IN ('pending','running')").fetchone()[0]
        due = self._due(today, settings['send_time'])
        if run:
            schedule = dict(run)
        else:
            status = ('disabled' if not settings['enabled'] else 'calendar_unknown' if calendar['state'] == 'unknown' else
                      'closed' if calendar['state'] == 'closed' else 'waiting_time' if now < due else
                      'due' if now < due + timedelta(seconds=START_WINDOW_SECONDS) else 'missed')
            schedule = {'report_date': today.isoformat(), 'scheduled_at': due.isoformat(), 'status': status,
                        'report_id': None, 'claimed_at': None, 'reason': calendar['reason'] if status in ('closed', 'calendar_unknown') else None}
        heartbeat = runtime['heartbeat_at']
        runtime['running'] = bool(heartbeat and 0 <= (now - datetime.fromisoformat(heartbeat)).total_seconds() < GENERATION_SECONDS + 60)
        return {'today': today.isoformat(), 'calendar': calendar, 'settings': settings, 'schedule': schedule,
                'reports': reports, 'active': bool(active), 'runtime': runtime,
                'notifications_enabled': bool(notification_settings['enabled']),
                'mode': 'data_summary', 'ai_status': 'not_used_by_choice', 'model_cost': '0',
                'start_window_seconds': START_WINDOW_SECONDS, 'send_window_minutes': SEND_WINDOW_MINUTES}

    def set_settings(self, enabled, send_time, expected_version):
        if type(enabled) is not bool or send_time not in ('08:45', '09:00'):
            raise LookupFailure('invalid_briefing_setting', '请使用页面中的时间与开关。', 422)
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT * FROM briefing_settings WHERE id=1').fetchone()
            if current['version'] != expected_version:
                raise LookupFailure('briefing_setting_changed', '另一操作已更新设置，请重新读取后再保存。', 409)
            if bool(current['enabled']) == enabled and current['send_time'] == send_time:
                return self.state()
            db.execute('UPDATE briefing_settings SET enabled=?,send_time=?,version=version+1,updated_at=? WHERE id=1',
                       (int(enabled), send_time, self.clock().isoformat()))
            # Any revision invalidates unsent work captured under the old settings.
            db.execute("UPDATE briefing_schedule_runs SET status='cancelled',updated_at=?,reason='定时设置已更新，旧设置下未完成的任务不补推。' WHERE status='waiting'", (self.clock().isoformat(),))
            db.execute("UPDATE notification_messages SET status='cancelled',updated_at=?,error_message='早盘简报设置已更新，尚未提交的消息已取消。' WHERE briefing_id IS NOT NULL AND status IN ('pending','checking')", (self.clock().isoformat(),))
        return self.state()

    def _queue(self, db, request_id, report_date, retry_of, kind, calendar):
        alias = db.execute('SELECT * FROM briefing_requests WHERE request_id=?', (request_id,)).fetchone()
        if alias:
            if alias['report_date'] != report_date or alias['retry_of'] != retry_of:
                raise LookupFailure('briefing_request_changed', '同一请求号不能用于不同日期或重试，请刷新后重新操作。', 409)
            return db.execute('SELECT * FROM briefing_reports WHERE id=?', (alias['report_id'],)).fetchone(), False
        items = self._items(db)
        records = db.execute('SELECT * FROM briefing_reports WHERE report_date=? ORDER BY created_at DESC,rowid DESC', (report_date,)).fetchall()
        reusable = next((row for row in records if row['status'] in (*READY, *ACTIVE)), None)
        if not reusable and records and records[0]['status'] == 'empty':
            if json.loads(records[0]['snapshot_json'])['watchlist_hash'] == list_digest(items):
                reusable = records[0]
        if reusable:
            db.execute('INSERT INTO briefing_requests VALUES (?,?,?,?)', (request_id, report_date, retry_of, reusable['id']))
            return reusable, False
        if records and records[0]['status'] in ('failed', 'interrupted') and retry_of != records[0]['id']:
            raise LookupFailure('briefing_retry_required', '上次生成未完成，请查看原因后明确重试。', 409)
        if retry_of and (not records or records[0]['id'] != retry_of):
            raise LookupFailure('briefing_retry_changed', '重试目标已变化，请刷新后查看最新记录。', 409)
        if db.execute("SELECT 1 FROM briefing_reports WHERE status IN ('pending','running') LIMIT 1").fetchone():
            raise LookupFailure('briefing_busy', '另一份简报仍在生成，请先查看结果。', 409)
        now = self.clock().isoformat()
        snapshot = {'calendar': calendar, 'watchlist': items, 'watchlist_hash': list_digest(items), 'frozen_at': now}
        output = make_summary(snapshot, [], now) if not items else None
        db.execute('INSERT INTO briefing_reports(id,report_date,trigger_kind,status,created_at,updated_at,snapshot_json,output_json,retry_of) VALUES (?,?,?,?,?,?,?,?,?)',
                   (request_id, report_date, kind, 'pending' if items else 'empty', now, now, encoded(snapshot), encoded(output) if output else None, retry_of))
        db.execute('INSERT INTO briefing_requests VALUES (?,?,?,?)', (request_id, report_date, retry_of, request_id))
        return db.execute('SELECT * FROM briefing_reports WHERE id=?', (request_id,)).fetchone(), True

    def queue_manual(self, request_id, report_date, retry_of=None):
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            alias = db.execute('SELECT * FROM briefing_requests WHERE request_id=?', (request_id,)).fetchone()
            if alias:
                row, created = self._queue(db, request_id, report_date, retry_of, 'manual', {})
                return self.public_report(db, row), created
            today = self.clock().astimezone(CHINA_TIME).date()
            if report_date != today.isoformat():
                raise LookupFailure('briefing_day_changed', '北京时间日期已变化，请重新读取今天的简报。', 409)
            calendar = self.calendar.context(today)
            if calendar['state'] != 'open':
                raise LookupFailure('briefing_calendar_unavailable', calendar['reason'], 409)
            row, created = self._queue(db, request_id, report_date, retry_of, 'manual', calendar)
            return self.public_report(db, row), created

    def claim_schedule(self):
        now = self.clock()
        day = now.astimezone(CHINA_TIME).date()
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            settings = db.execute('SELECT * FROM briefing_settings WHERE id=1').fetchone()
            if not settings['enabled'] or db.execute('SELECT 1 FROM briefing_schedule_runs WHERE report_date=?', (day.isoformat(),)).fetchone():
                return
            due = self._due(day, settings['send_time'])
            if now < due:
                return
            calendar = self.calendar.context(day)
            status, reason, report_id = 'waiting', None, None
            if calendar['state'] != 'open':
                status, reason = ('closed' if calendar['state'] == 'closed' else 'calendar_unknown'), calendar['reason']
            elif now >= due + timedelta(seconds=START_WINDOW_SECONDS):
                status, reason = 'missed', '未在约定分钟领取任务，本次未生成，不自动补发；可手动补生成到网页。'
            else:
                previous = db.execute('SELECT id,status FROM briefing_reports WHERE report_date=? ORDER BY created_at DESC,rowid DESC LIMIT 1', (day.isoformat(),)).fetchone()
                retry = previous['id'] if previous and previous['status'] in ('failed', 'interrupted') else None
                try:
                    queued, _ = self._queue(db, str(uuid.uuid5(uuid.NAMESPACE_URL, 'alpha-lab-briefing:' + day.isoformat())), day.isoformat(), retry, 'scheduled', calendar)
                    report_id = queued['id']
                except LookupFailure as error:
                    status, reason = 'failed', error.message
            db.execute('INSERT INTO briefing_schedule_runs(report_date,scheduled_at,claimed_at,updated_at,settings_version,status,report_id,reason) VALUES (?,?,?,?,?,?,?,?)',
                       (day.isoformat(), due.isoformat(), now.isoformat(), now.isoformat(), settings['version'], status, report_id, reason))

    def _still_listed(self, item):
        current = self.store.get(item['market'], item['symbol'])
        return bool(current and current['created_at'] == item['created_at'])

    def _collect_item(self, item, target_date):
        if not self._still_listed(item):
            return missing(item, 'watchlist_changed', '这条自选已移除或替换，停止取数，保留原清单记录。')
        try:
            history = self.daily.get(item['market'], item['symbol'])['data']
            if history:
                try:
                    return from_history(item, history, target_date, self.clock(), 'saved_daily')
                except LookupFailure:
                    pass  # Invalid/missing target date is not silently reused.
            target = date.fromisoformat(target_date)
            history = self.daily.source.fetch(item['symbol'], target - timedelta(days=45), target).to_dict()
            if not self._still_listed(item):
                return missing(item, 'watchlist_changed', '取数期间这条自选已移除或替换，本次不采用。')
            return from_history(item, history, target_date, self.clock(), 'fetched_daily')
        except LookupFailure as error:
            return missing(item, error.code, error.message)
        except Exception:
            return missing(item, 'briefing_source_error', '本次日线获取或核对未完成，不补造数值。')

    def _research_snapshot(self, snapshot):
        started = monotonic_time.monotonic()
        refresh_error = None
        try:
            # One bounded market refresh, entirely outside database transactions.
            # No stock-by-stock request fanout, no LLM call, no extra schedule.
            self.research.refresh()
        except Exception:
            refresh_error = '本次研究资料刷新未完成；仍按截止时点核对已有缓存，缺项保留。'
        cutoff = self.clock()
        context = freeze_research(self.research, cutoff)
        if refresh_error:
            context['selection_notes'].append(refresh_error)
        context['stock_snapshot_ids'] = []
        sections = {section['key']: section for section in context['sections']}
        seen = {item['id'] for section in context['sections'] for item in section['items']}
        for stock in snapshot['watchlist']:
            local = freeze_research(self.research, cutoff, stock['market'], stock['symbol'])
            context['stock_snapshot_ids'].append({'market': stock['market'], 'symbol': stock['symbol'],
                                                 'snapshot_id': local.get('snapshot_id')})
            for section in local['sections']:
                if section['key'] not in ('news', 'announcements'):
                    continue
                target = sections[section['key']]
                for item in section['items']:
                    if item['id'] in seen or len(target['items']) >= 30:
                        continue
                    seen.add(item['id'])
                    target['items'].append(item)
                    if target['status'] == 'unavailable':
                        target['status'] = 'partial'
                        target['error_message'] = '全市场本节缺项，仅补入自选已有缓存；覆盖不保证完整。'
        context['selection_notes'].append('全市场最新有限条目，加上自选已有缓存；不保证覆盖每只股票的全部资讯或公告。')
        snapshot['research_context'] = context
        snapshot['trace'] = [{'stage': 'research_frozen', 'status': 'partial' if refresh_error else 'completed',
                              'at': cutoff.isoformat(), 'duration_ms': int((monotonic_time.monotonic() - started) * 1000),
                              'item_count': sum(len(section['items']) for section in context['sections'])}]

    def process_next(self):
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM briefing_reports WHERE status='pending' ORDER BY created_at,rowid LIMIT 1").fetchone()
            if not row:
                return
            db.execute("UPDATE briefing_reports SET status='running',updated_at=? WHERE id=? AND status='pending'", (self.clock().isoformat(), row['id']))
            report = dict(row)
        snapshot = json.loads(report['snapshot_json'])
        items = []
        deadline = monotonic_time.monotonic() + GENERATION_SECONDS
        try:
            if self.research is not None:
                self._research_snapshot(snapshot)
                with self.store.connect() as db, db:
                    db.execute("UPDATE briefing_reports SET snapshot_json=? WHERE id=? AND status='running'", (encoded(snapshot), report['id']))
            daily_started = monotonic_time.monotonic()
            for item in snapshot['watchlist']:
                if self.stop_event.is_set():
                    raise LookupFailure('briefing_stopped', '服务正在停止，本次简报中断；不自动补发。', 409)
                if self.clock().astimezone(CHINA_TIME).date().isoformat() != report['report_date']:
                    items.append(missing(item, 'briefing_day_changed', '生成跨过北京时间日期，本项不再取数。'))
                elif monotonic_time.monotonic() >= deadline:
                    items.append(missing(item, 'briefing_deadline', '本次整理已达到等待上限，尚未取得本项数据。'))
                else:
                    items.append(self._collect_item(item, snapshot['calendar']['previous_trade_date']))
            # A removal between collection and commit must not become a new reminder.
            items = [value if self._still_listed(value) else missing(value, 'watchlist_changed', '生成期间这条自选已移除或替换，本次未覆盖。') for value in items]
            if self.research is not None:
                snapshot['trace'].extend([
                    {'stage': 'daily_validated', 'status': 'completed', 'at': self.clock().isoformat(),
                     'duration_ms': int((monotonic_time.monotonic() - daily_started) * 1000),
                     'covered_count': sum(value['status'] == 'covered' for value in items)},
                    {'stage': 'model_call', 'status': 'not_used_by_choice', 'at': self.clock().isoformat(),
                     'input_tokens': 0, 'output_tokens': 0, 'estimated_cost': '0'}])
            output = make_summary(snapshot, items, self.clock().isoformat())
            with self.store.connect() as db, db:
                db.execute("UPDATE briefing_reports SET status=?,updated_at=?,snapshot_json=?,output_json=?,error_code=?,error_message=? WHERE id=? AND status='running'",
                           (output['status'], self.clock().isoformat(), encoded(snapshot), encoded(output), 'briefing_no_data' if output['status'] == 'failed' else None,
                            '所有自选都没有可核对的目标日数据，本次未形成简报，不发送。' if output['status'] == 'failed' else None, report['id']))
        except Exception as error:
            interrupted = isinstance(error, LookupFailure) and error.code == 'briefing_stopped'
            with self.store.connect() as db, db:
                db.execute("UPDATE briefing_reports SET status=?,updated_at=?,error_code=?,error_message=? WHERE id=? AND status='running'",
                           ('interrupted' if interrupted else 'failed', self.clock().isoformat(), 'briefing_stopped' if interrupted else 'briefing_generation_error',
                            '本次简报未完成，保留任务记录；可手动重试，不自动补发。', report['id']))
            logger.error('Briefing generation stopped: %s', type(error).__name__)

    def settle_schedule(self):
        now = self.clock()
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            settings = db.execute('SELECT * FROM briefing_settings WHERE id=1').fetchone()
            for run in db.execute("SELECT * FROM briefing_schedule_runs WHERE status='waiting'").fetchall():
                report = db.execute('SELECT * FROM briefing_reports WHERE id=?', (run['report_id'],)).fetchone()
                expiry = datetime.fromisoformat(run['scheduled_at']) + timedelta(minutes=SEND_WINDOW_MINUTES)
                if not settings['enabled'] or settings['version'] != run['settings_version']:
                    status, reason = 'cancelled', '定时设置已关闭或更新，原任务不再推送。'
                elif now >= expiry or run['report_date'] != now.astimezone(CHINA_TIME).date().isoformat():
                    status, reason = 'expired', '已经超过简报发送时段，结果仍可在网页查看，不补推。'
                elif not report:
                    status, reason = 'failed', '生成记录缺失，本次未发送。'
                elif report['status'] in ACTIVE:
                    continue
                elif report['status'] in READY:
                    self.notifications.queue_briefing(db, report['id'], now, expiry)
                    status, reason = 'completed', '简报已生成；发送状态以对应飞书记录为准。'
                else:
                    status = 'empty' if report['status'] == 'empty' else 'failed'
                    reason = '空自选，不发送空消息。' if status == 'empty' else '本次简报未完成，不发送；可手动补生成。'
                db.execute('UPDATE briefing_schedule_runs SET status=?,updated_at=?,reason=? WHERE report_date=? AND status=\'waiting\'',
                           (status, now.isoformat(), reason, run['report_date']))

    def tick(self, background=False):
        self.claim_schedule()
        if background:
            # Receiving a due job must not wait behind a slow manual fetch.
            # Only one generator consumes the durable pending queue.
            if not self.generation_thread or not self.generation_thread.is_alive():
                with self.store.connect() as db:
                    pending = db.execute("SELECT 1 FROM briefing_reports WHERE status='pending' LIMIT 1").fetchone()
                if pending and not self.stop_event.is_set():
                    self.generation_thread = threading.Thread(target=self.process_next, name='alpha-lab-briefing-data', daemon=True)
                    self.generation_thread.start()
        else:
            self.process_next()
        self.settle_schedule()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name='alpha-lab-briefings', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1)
        if self.generation_thread:
            self.generation_thread.join(timeout=1)

    def is_alive(self):
        return bool((self.thread and self.thread.is_alive()) or
                    (self.generation_thread and self.generation_thread.is_alive()))

    def _run(self):
        while not self.stop_event.is_set():
            failure = None
            try:
                self.tick(background=True)
            except Exception as error:
                logger.error('Briefing worker cycle failed: %s', type(error).__name__)
                failure = '简报后台本次检查未完成，已有记录保留。'
            try:
                with self.store.connect() as db, db:
                    db.execute('UPDATE briefing_runtime SET heartbeat_at=?,checks=checks+1,last_error=? WHERE id=1', (self.clock().isoformat(), failure))
            except Exception as error:
                logger.error('Briefing heartbeat write failed: %s', type(error).__name__)
            self.stop_event.wait(1)
