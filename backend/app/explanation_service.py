"""Manual, durable event explanations with reservations and no automatic paid retry."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
import threading
import time

from .ai_client import (AITransportFailure, DeepSeekClient, INPUT_MICRO_PER_TOKEN,
    MAX_OUTPUT_TOKENS, OUTPUT_MICRO_PER_TOKEN, PRICE_PROFILE, input_upper_bound, reservation)
from .ai_config import ConfigFailure, load_config
from .explanation_evidence import (ExplanationRejected, PROMPT_VERSION, canonical,
    freeze_event, messages_for, validate_output)
from .quotes import CHINA_TIME
from .explanation_schema import initialize_schema
from .quote_snapshots import QuoteSnapshots
from .quote_evidence import PROMPT_VERSION as QUOTE_PROMPT_VERSION
from .securities import LookupFailure
from .analysis_context import enrich_evidence, freeze_research


def yuan(micro):
    return format(Decimal(micro) / 1000000, '.6f')


class ExplanationService:
    def __init__(self, store, config_path, client=None, clock=None, config=None,
                 research=None, legacy_budget_usage=None):
        self.store, self.config_path, self.client = store, config_path, client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.config, self.config_error = config, None
        self.active = 0
        self.active_lock = threading.Lock()
        self.research, self.legacy_budget_usage = research, legacy_budget_usage

    def initialize(self):
        if self.config is None:
            try:
                self.config = load_config(self.config_path)
            except ConfigFailure as error:
                self.config_error = str(error)
        if self.config and self.client is None:
            self.client = DeepSeekClient(self.config)
        initialize_schema(self.store)
        with self.store.connect() as db, db:
            now = self.clock().isoformat()
            db.execute("""UPDATE ai_explanations SET status='interrupted',updated_at=?,charged_micro=0,
                cost_basis='not_sent',error_code='restart_before_call',error_message='服务重启前尚未开始调用。需要时请手动重试。'
                WHERE status='pending'""", (now,))
            db.execute("""UPDATE ai_explanations SET status='interrupted',updated_at=?,
                error_code='restart_during_call',error_message='调用途中服务停止，是否产生用量未知；预留估算保留，不自动重试。'
                WHERE status='running'""", (now,))

    def _recover_stale(self, db):
        now = self.clock()
        db.execute("""UPDATE ai_explanations SET status='interrupted',updated_at=?,
            charged_micro=CASE WHEN status='pending' THEN 0 ELSE charged_micro END,
            cost_basis=CASE WHEN status='pending' THEN 'not_sent' ELSE cost_basis END,
            error_code='worker_missing',error_message='未在预期时间取得后台结果；不会自动重试，已有事件仍保留。'
            WHERE status IN ('pending','running') AND updated_at < ?""",
            (now.isoformat(), (now - timedelta(seconds=120)).isoformat()))

    def _budget(self, db):
        today = self.clock().astimezone(CHINA_TIME).strftime('%Y-%m-%d')
        month = today[:7]
        day_used = db.execute('SELECT COALESCE(SUM(charged_micro),0) FROM ai_explanations WHERE budget_day=?', (today,)).fetchone()[0]
        month_used = db.execute('SELECT COALESCE(SUM(charged_micro),0) FROM ai_explanations WHERE budget_month=?', (month,)).fetchone()[0]
        legacy = {'day_used_micro': 0, 'month_used_micro': 0}
        if self.legacy_budget_usage is not None:
            try:
                legacy = self.legacy_budget_usage(db, today, month)
                if any(type(legacy[key]) is not int or legacy[key] < 0 for key in ('day_used_micro', 'month_used_micro')):
                    raise ValueError()
            except Exception:
                raise LookupFailure('budget_history_unavailable', '旧版预算记录暂时无法核对，停止新模型调用；已有解释仍保留。', 409) from None
        day_used += legacy['day_used_micro']
        month_used += legacy['month_used_micro']
        return {'day': today, 'month': month, 'day_used_micro': day_used, 'month_used_micro': month_used,
                'day_used': yuan(day_used), 'month_used': yuan(month_used),
                'daily_limit': self.config.daily_budget if self.config else None,
                'monthly_limit': self.config.monthly_budget if self.config else None,
                'price_profile': PRICE_PROFILE,
                'legacy_day_used': yuan(legacy['day_used_micro']), 'legacy_month_used': yuan(legacy['month_used_micro']),
                'note': ('包含本工程预留与旧版导入的保守用量估算；未知历史不能当作零用量，不是平台账单。'
                         if self.legacy_budget_usage is not None else
                         '仅本学习工程的保守估算，包含未结清预留；不是平台账单，不含原项目用量。')}

    @staticmethod
    def public_evidence(evidence):
        return {key: value for key, value in evidence.items() if key != 'model_input'}

    def public_record(self, row):
        keys = ('id', 'event_id', 'quote_snapshot_id', 'evidence_hash', 'prompt_version', 'requested_model', 'status',
                'created_at', 'updated_at', 'retry_of', 'cost_basis', 'price_profile', 'input_tokens',
                'output_tokens', 'actual_model', 'model_fingerprint', 'elapsed_ms', 'error_code', 'error_message', 'reviewed_at')
        result = {key: row[key] for key in keys}
        result['estimated_cost'] = yuan(row['charged_micro'])
        result['evidence'] = self.public_evidence(json.loads(row['evidence_json']))
        result['output'] = json.loads(row['output_json']) if row['status'] == 'succeeded' and row['output_json'] else None
        result['trace'] = json.loads(row['trace_json']) if 'trace_json' in row.keys() and row['trace_json'] else []
        result['research_input_count'] = len(result['evidence'].get('research_evidence', []))
        result['research_used_count'] = result['output'].get('research_used_count', 0) if result['output'] else 0
        return result

    def _event(self, db, event_id):
        row = db.execute('SELECT * FROM anomaly_events WHERE id=?', (event_id,)).fetchone()
        if not row:
            raise LookupFailure('event_not_found', '这条事件不存在，请回到异动列表重新选择。', 404)
        return freeze_event(row)

    def _base_evidence(self, db, subject_id, kind):
        return self._event(db, subject_id) if kind == 'event' else QuoteSnapshots.evidence(db, subject_id)

    def _evidence(self, db, subject_id, kind):
        base = self._base_evidence(db, subject_id, kind)
        column, prefix, _ = self._subject(kind)
        saved = db.execute('SELECT * FROM ai_subject_evidence WHERE subject_key=?', (prefix + subject_id,)).fetchone()
        if saved:
            if saved['base_hash'] != base['evidence_hash']:
                raise LookupFailure('evidence_changed', '已固定的原始依据发生变化，停止调用并保留原解释。', 409)
            return json.loads(saved['evidence_json'])
        # Old subjects keep their original contract and history even after research is enabled.
        previous = db.execute(f'SELECT evidence_json FROM ai_explanations WHERE {column}=? ORDER BY created_at,id LIMIT 1', (subject_id,)).fetchone()
        if previous:
            saved_evidence = json.loads(previous['evidence_json'])
            if saved_evidence['evidence_hash'] != base['evidence_hash']:
                raise LookupFailure('evidence_changed', '历史解释的原始依据发生变化，停止调用并保留原记录。', 409)
            return saved_evidence
        return base

    def _prepare_evidence(self, subject_id, kind):
        column, prefix, _ = self._subject(kind)
        with self.store.connect() as db:
            evidence = self._evidence(db, subject_id, kind)
            already_bound = db.execute('SELECT 1 FROM ai_subject_evidence WHERE subject_key=?', (prefix + subject_id,)).fetchone()
            old = db.execute(f'SELECT 1 FROM ai_explanations WHERE {column}=? LIMIT 1', (subject_id,)).fetchone()
        if self.research is None or already_bound or old:
            return evidence
        # ResearchService only reads cached snapshots here; never refresh from a GET,
        # inside a SQLite transaction, or when validating a model response.
        context = freeze_research(self.research, evidence['created_at'], evidence['market'], evidence['symbol'])
        enriched = enrich_evidence(evidence, context)
        while enriched.get('research_evidence'):
            try:
                input_upper_bound(messages_for(enriched))
                break
            except AITransportFailure as error:
                if error.code != 'input_too_large':
                    raise
                if evidence.get('trigger_research_evidence') and len(enriched['research_evidence']) <= 1:
                    raise LookupFailure('ai_evidence_too_large', '完整触发依据超出本次模型输入上限，未调用模型；原事件仍保留。', 422) from None
                enriched = enrich_evidence(evidence, context, limit=len(enriched['research_evidence']) - 1)
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute(f'SELECT 1 FROM ai_explanations WHERE {column}=? LIMIT 1', (subject_id,)).fetchone():
                return self._evidence(db, subject_id, kind)
            db.execute('INSERT OR IGNORE INTO ai_subject_evidence VALUES (?,?,?,?)',
                       (prefix + subject_id, evidence['evidence_hash'], canonical(enriched), self.clock().isoformat()))
            return self._evidence(db, subject_id, kind)

    @staticmethod
    def _prompt_version(evidence, prompt):
        if evidence.get('rule_type'):
            from .explanation_extended import PROMPT_VERSION as extended_prompt
            prompt = extended_prompt
        return prompt + '-research-1' if 'research_schema_version' in evidence['model_input'] else prompt

    @staticmethod
    def _subject(kind):
        if kind not in ('event', 'quote'):
            raise ValueError('Unsupported evidence kind')
        return ('event_id', '', PROMPT_VERSION) if kind == 'event' else ('quote_snapshot_id', 'quote:', QUOTE_PROMPT_VERSION)

    def state(self, event_id, kind='event'):
        column, prefix, prompt = self._subject(kind)
        self._prepare_evidence(event_id, kind)
        with self.store.connect() as db, db:
            self._recover_stale(db)
            evidence = self._evidence(db, event_id, kind)
            records = [self.public_record(row) for row in db.execute(
                f'SELECT * FROM ai_explanations WHERE {column}=? ORDER BY created_at DESC,id DESC LIMIT 20', (event_id,))]
            budget = self._budget(db)
        return {'configured': self.config is not None, 'config_error': self.config_error,
                'model': self.config.model if self.config else None, 'prompt_version': self._prompt_version(evidence, prompt),
                'evidence': self.public_evidence(evidence), 'records': records, 'budget': budget,
                'next_call_reservation': yuan(reservation(messages_for(evidence))),
                'max_output_tokens': MAX_OUTPUT_TOKENS,
                'automatic_check_note': '程序核对身份、关键数值、引用与部分越界内容；文字含义仍需本人核对。'}

    def queue(self, event_id, request_id, expected_hash, retry_of=None, kind='event'):
        column, prefix, prompt = self._subject(kind)
        subject_key = prefix + event_id
        now = self.clock()
        preparation_started = time.monotonic()
        self._prepare_evidence(event_id, kind)
        evidence_ms = int((time.monotonic() - preparation_started) * 1000)
        with self.store.connect() as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._recover_stale(db)
            old = db.execute('SELECT * FROM ai_explanations WHERE id=?', (request_id,)).fetchone()
            alias = db.execute('SELECT * FROM ai_request_aliases WHERE request_id=?', (request_id,)).fetchone()
            if alias:
                if alias['event_id'] != subject_key or alias['evidence_hash'] != expected_hash or alias['retry_of'] != retry_of:
                    raise LookupFailure('request_reused', '这个请求号已对应另一操作，请重新读取后操作。', 409)
                old = db.execute('SELECT * FROM ai_explanations WHERE id=?', (alias['record_id'],)).fetchone()
                return self.public_record(old), False
            if old:
                if old[column] != event_id or old['evidence_hash'] != expected_hash or old['retry_of'] != retry_of:
                    raise LookupFailure('request_reused', '这个请求号已对应另一操作，请重新读取后操作。', 409)
                return self.public_record(old), False
            evidence = self._evidence(db, event_id, kind)
            prompt = self._prompt_version(evidence, prompt)
            if evidence['evidence_hash'] != expected_hash:
                raise LookupFailure('evidence_changed', '页面依据与保存记录不一致，请重新读取事件。', 409)
            model = self.config.model if self.config else 'deepseek-flash'
            cache_key = hashlib.sha256(f'{subject_key}|{expected_hash}|{prompt}|{model}'.encode()).hexdigest()
            cached = db.execute("SELECT * FROM ai_explanations WHERE cache_key=? AND status IN ('succeeded','pending','running') ORDER BY created_at DESC LIMIT 1", (cache_key,)).fetchone()
            if cached:
                db.execute('INSERT INTO ai_request_aliases VALUES (?,?,?,?,?)',
                           (request_id, subject_key, expected_hash, retry_of, cached['id']))
                return self.public_record(cached), False
            previous = db.execute('SELECT * FROM ai_explanations WHERE cache_key=? ORDER BY created_at DESC,id DESC LIMIT 1', (cache_key,)).fetchone()
            if (previous and retry_of != previous['id']) or (retry_of and not previous):
                raise LookupFailure('explicit_retry_needed', '这份依据已有失败记录；核对原因后，在该记录点击“重试一次”。', 409)
            if not self.config or not self.client:
                raise LookupFailure('ai_not_configured', self.config_error or '学习版尚未配置模型，请先完成本课模型配置。', 409)
            with self.active_lock:
                still_active = self.active > 0
            if still_active:
                raise LookupFailure('ai_busy', '上一个模型调用仍在结束，请稍后读取结果再操作；没有发起新调用。', 409)
            if db.execute("SELECT 1 FROM ai_explanations WHERE status IN ('pending','running') LIMIT 1").fetchone():
                raise LookupFailure('ai_busy', '另一个解释正在生成，请等它结束后再操作。', 409)
            messages = messages_for(evidence)
            reserved = reservation(messages)
            budget_started = time.monotonic()
            budget = self._budget(db)
            if (budget['day_used_micro'] + reserved > Decimal(self.config.daily_budget) * 1000000
                    or budget['month_used_micro'] + reserved > Decimal(self.config.monthly_budget) * 1000000):
                raise LookupFailure('ai_budget_exhausted', '剩余日/月估算预算不足以预留本次请求，已停止新调用；已有解释仍可查看。', 429)
            trace = [{'stage': 'evidence_frozen', 'status': 'completed', 'at': now.isoformat(),
                      'duration_ms': evidence_ms,
                      'research_input_count': len(evidence.get('research_evidence', [])),
                      'cutoff': evidence.get('research_context', {}).get('cutoff', evidence['created_at'])},
                     {'stage': 'budget_reserved', 'status': 'completed', 'at': now.isoformat(),
                      'duration_ms': int((time.monotonic() - budget_started) * 1000),
                      'reserved_cost': yuan(reserved)}]
            db.execute("""INSERT INTO ai_explanations
                (id,event_id,cache_key,evidence_hash,prompt_version,requested_model,evidence_json,messages_json,status,
                 created_at,updated_at,retry_of,budget_day,budget_month,reserved_micro,charged_micro,cost_basis,price_profile,quote_snapshot_id,trace_json)
                VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,'reserved_unknown',?,?,?)""",
                (request_id,event_id if kind == 'event' else None,cache_key,expected_hash,prompt,model,canonical(evidence),canonical(messages),
                 now.isoformat(),now.isoformat(),retry_of,budget['day'],budget['month'],reserved,reserved,PRICE_PROFILE,event_id if kind == 'quote' else None,canonical(trace)))
            return self.public_record(db.execute('SELECT * FROM ai_explanations WHERE id=?', (request_id,)).fetchone()), True

    def process(self, request_id):
        with self.store.connect() as db, db:
            claimed = db.execute("UPDATE ai_explanations SET status='running',updated_at=? WHERE id=? AND status='pending'", (self.clock().isoformat(), request_id)).rowcount
            if not claimed:
                return
            row = dict(db.execute('SELECT * FROM ai_explanations WHERE id=?', (request_id,)).fetchone())
        with self.active_lock:
            self.active += 1
        fields = {'status': 'failed', 'charged_micro': row['reserved_micro'], 'cost_basis': 'reserved_unknown',
                  'input_tokens': None, 'output_tokens': None, 'actual_model': None, 'model_fingerprint': None,
                  'elapsed_ms': None, 'raw_output': None, 'output_json': None, 'error_code': None, 'error_message': None}
        trace = json.loads(row['trace_json']) if row.get('trace_json') else []
        started = time.monotonic()
        stage = 'model_call'
        try:
            messages = json.loads(row['messages_json'])
            result = self.client.complete(messages)
            trace.append({'stage': stage, 'status': 'completed', 'at': self.clock().isoformat(),
                          'duration_ms': int((time.monotonic() - started) * 1000), 'provider_elapsed_ms': result.elapsed_ms})
            stage, started = 'output_validation', time.monotonic()
            fields.update(actual_model=result.model, model_fingerprint=result.fingerprint,
                          elapsed_ms=result.elapsed_ms, raw_output=result.content)
            if (type(result.prompt_tokens) is int and 0 < result.prompt_tokens <= input_upper_bound(messages)
                    and type(result.completion_tokens) is int and 0 < result.completion_tokens <= MAX_OUTPUT_TOKENS):
                fields.update(input_tokens=result.prompt_tokens, output_tokens=result.completion_tokens,
                    charged_micro=result.prompt_tokens * INPUT_MICRO_PER_TOKEN + result.completion_tokens * OUTPUT_MICRO_PER_TOKEN,
                    cost_basis='usage_estimate')
            if result.finish_reason != 'stop':
                raise ExplanationRejected('incomplete_output', '模型回复被截断或未正常结束，这次解释未采用。')
            output = validate_output(result.content, json.loads(row['evidence_json']))
            fields.update(status='succeeded', output_json=canonical(output))
            trace.append({'stage': stage, 'status': 'completed', 'at': self.clock().isoformat(),
                          'duration_ms': int((time.monotonic() - started) * 1000),
                          'research_used_count': output.get('research_used_count', 0)})
        except ExplanationRejected as error:
            fields.update(status='rejected', error_code=error.code, error_message=error.message)
        except AITransportFailure as error:
            fields.update(error_code=error.code, error_message=error.message)
            if not error.uncertain:
                fields.update(charged_micro=0, cost_basis='not_sent_or_rejected')
        except Exception:
            # No exception text: provider responses/configuration must never reach logs/UI.
            fields.update(error_code='processing_failed', error_message='本次解释未完成，已保留请求记录；请核对后再手动重试。')
        finally:
            try:
                if fields['status'] != 'succeeded':
                    trace.append({'stage': stage, 'status': fields['status'], 'at': self.clock().isoformat(),
                                  'duration_ms': int((time.monotonic() - started) * 1000), 'error_code': fields['error_code']})
                trace.append({'stage': 'usage_settled', 'status': 'completed', 'at': self.clock().isoformat(),
                              'input_tokens': fields['input_tokens'], 'output_tokens': fields['output_tokens'],
                              'cost_basis': fields['cost_basis'], 'estimated_cost': yuan(fields['charged_micro'])})
                fields['trace_json'] = canonical(trace)
                fields['updated_at'] = self.clock().isoformat()
                with self.store.connect() as db, db:
                    db.execute('UPDATE ai_explanations SET ' + ','.join(f'{key}=?' for key in fields)
                               + " WHERE id=? AND status='running'", (*fields.values(), request_id))
            finally:
                with self.active_lock:
                    self.active -= 1

    def confirm_review(self, request_id, facts_checked, citations_checked, no_invented_causes):
        if not all(value is True for value in (facts_checked, citations_checked, no_invented_causes)):
            raise LookupFailure('review_incomplete', '请实际核对三项内容后再保存，不确定时先保留未核对状态。', 422)
        with self.store.connect() as db, db:
            row = db.execute('SELECT * FROM ai_explanations WHERE id=?', (request_id,)).fetchone()
            if not row:
                raise LookupFailure('explanation_not_found', '这条解释不存在。', 404)
            if row['status'] != 'succeeded':
                raise LookupFailure('explanation_not_ready', '只有已生成并展示的解释才能保存人工核对。', 409)
            db.execute('UPDATE ai_explanations SET reviewed_at=COALESCE(reviewed_at,?) WHERE id=?', (self.clock().isoformat(), request_id))
            return self.public_record(db.execute('SELECT * FROM ai_explanations WHERE id=?', (request_id,)).fetchone())
