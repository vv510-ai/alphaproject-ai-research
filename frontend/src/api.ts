export const appBase = import.meta.env.BASE_URL;
export type AccessInfo = { mode: 'lan' | 'public'; lan_urls: string[]; public_url?: string; release: { release_id: string; app_version: string } };

export type Security = {
  symbol: string;
  market: 'sh' | 'sz';
  name: string;
  source: string;
  verified_at: string;
};

export type QuoteData = {
  market: 'sh' | 'sz'; symbol: string; name: string;
  price: string; previous_close: string; change: string; change_pct: string;
  source_reported_at: string; received_at: string;
  source: string; source_url: string; price_unit: string; change_pct_unit: string;
};

export type QuoteState = {
  data: QuoteData | null;
  data_state: 'unavailable' | 'historical' | 'today_unverified' | 'clock_issue' | 'fresh' | 'stale';
  data_label: string;
  source_age_seconds: number | null;
  refresh_state: 'idle' | 'processing' | 'succeeded' | 'failed' | 'interrupted';
  last_attempt_at: string | null;
  error_message: string | null;
  retry_after_seconds: number;
  next_refresh_at: string | null;
  market_state: 'trading' | 'break' | 'auction' | 'closed' | 'unknown';
  realtime_verified: boolean;
  eligibility: QuoteEligibility;
};

export type QuoteEligibility = {
  eligible: boolean; code: string; reason: string; checked_at: string | null;
  session_id: string | null; proof: RecentTradeProof | null;
};

export type RecentTradeProof = {
  policy_version: string; market: string; symbol: string; watchlist_created_at: string;
  session_id: string; session_end_at: string; calendar_version: string;
  freshness_seconds: number; checked_at: string;
  previous: RecentTradeSample;
  current: RecentTradeSample;
};

export type RecentTradeSample = {
  source_reported_at: string; received_at: string; cumulative_volume: string;
  acquisition?: { origin: 'collector' | 'manual'; cycle_id: string | null; started_at: string; committed_at: string };
};

export type QuoteCollectorState = {
  running: boolean; heartbeat_at: string | null; last_cycle_at: string | null; next_cycle_at: string | null;
  cycle_id?: string | null;
  state: string; label: string; interval_seconds: number; freshness_seconds: number;
  total: number; attempted: number; succeeded: number; failed: number; last_error: string | null;
};

export type WatchItem = Security & {
  created_at: string; quote?: QuoteState; group_id?: string | null;
  is_holding?: boolean; display_order?: number; metadata_version?: number;
};
export type WatchGroup = { id: string; name: string; created_at: string };
export type WatchUndo = { token: string; expires_at: string; name: string };

export type ResearchKey = 'indices' | 'global' | 'sectors' | 'news' | 'announcements' | 'calendar';
export type ResearchItem = {
  id: string; name?: string | null; code?: string | null; price?: string | number | null;
  change_pct?: string | number | null; as_of?: string | null; title?: string | null;
  summary?: string | null; url?: string | null; source_url?: string | null;
  published_at?: string | null; received_at?: string | null; source?: string | null;
  change_kind?: 'new' | 'updated' | 'unchanged';
  scheduled_at?: string | null; stale?: boolean; ai_eligible?: boolean; eligibility_note?: string | null;
  is_stale?: boolean; carried_forward?: boolean; missing_fields?: string[];
};
export type ResearchSection = {
  key: ResearchKey; title: string; status: 'available' | 'partial' | 'unavailable' | 'stale';
  source: string | null; source_url: string | null; as_of: string | null; received_at: string | null;
  error_message: string | null; items: ResearchItem[];
};
export type ResearchState = {
  scope: string; market: string | null; symbol: string | null; snapshot_id: string | null;
  created_at: string | null; refresh_state: string; last_attempt_at: string | null;
  retry_after_seconds: number; error_message: string | null; sections: ResearchSection[];
  cutoff?: string | null; missing?: unknown; excluded?: unknown;
  selection_notes?: string[];
  refresh_in_progress?: boolean; active_scope?: string | null;
};
export type ResearchEvidence = ResearchItem & { original_id?: string; section: string };
export type LegacyRecord = {
  id: string; kind: string; source_id: string; occurred_at: string | null; symbol: string | null;
  status: string | null; content: unknown;
};
export type LegacyHistory = { items: LegacyRecord[]; next_cursor: string | null; total: number };

export type DailyBar = {
  date: string; open: string; close: string; high: string; low: string;
  volume: string; raw_volume: string;
};

export type DailyData = {
  market: 'sh' | 'sz'; symbol: string; name: string; bars: DailyBar[];
  requested_start: string; requested_end: string; earliest_trade_date: string; latest_trade_date: string;
  received_at: string; source: string; source_url: string; raw_volume_unit: '手' | '股';
  volume_multiplier: number; excluded_outside_range: number;
  price_unit: '元'; volume_unit: '股'; adjustment: 'none'; adjustment_label: '不复权';
};

export type DailyState = {
  market: 'sh' | 'sz'; symbol: string; watchlist_created_at: string; data: DailyData | null;
  refresh_state: 'idle' | 'processing' | 'succeeded' | 'failed' | 'interrupted';
  error_message: string | null; last_attempt_at: string | null; retry_after_seconds: number;
};

export type NotificationRecord = {
  id: string; created_at: string; updated_at: string; group_name: string; bot_name: string;
  status: 'pending' | 'checking' | 'sending' | 'accepted' | 'failed' | 'unknown' | 'cancelled' | 'expired';
  error_message: string | null; confirmed_at: string | null;
  kind: 'test' | 'demo' | 'anomaly' | 'briefing'; event_id: string | null; expires_at: string | null; briefing_id: string | null;
};

export type AnomalyRule = {
  market: 'sh' | 'sz'; symbol: string; watchlist_created_at: string;
  up_threshold: string; down_threshold: string; enabled: number; version: number;
  updated_at: string; last_checked_at: string | null; last_result: string | null;
};
export type ExtendedRuleConfig = {
  limit_enabled: boolean; breakout_enabled: boolean; volume_enabled: boolean; amplitude_enabled: boolean; event_enabled: boolean;
  volume_ratio: string; amplitude_pct: string; lookback_days: number;
};
export type ExtendedRule = {
  market: string; symbol: string; watchlist_created_at: string; version: number; enabled: boolean | number;
  config: ExtendedRuleConfig; last_checked_at: string | null; last_results: unknown; updated_at: string;
};
export type AnomalyStep = {
  step: number; price: string; previous_close: string; change_pct: string; checked_at: string;
  status: 'below' | 'triggered' | 'duplicate' | 'blocked' | 'cancelled'; reason: string; event_id?: string;
};
export type AnomalyRun = {
  id: string; market: 'sh' | 'sz'; symbol: string; direction: 'up' | 'down'; notify: boolean;
  threshold: string; created_at: string; status: 'pending' | 'running' | 'completed' | 'cancelled';
  step: number; results: AnomalyStep[]; error_message: string | null;
};
export type AnomalyEvent = {
  id: string; mode: 'live' | 'demo'; market: 'sh' | 'sz'; symbol: string; direction: 'up' | 'down' | 'limit_up' | 'limit_down' | 'breakout' | 'breakdown' | 'volume' | 'amplitude' | 'event';
  trade_date: string; created_at: string; notification: NotificationRecord | null;
  snapshot: { quote: QuoteData; change_pct: string; direction: 'up' | 'down'; threshold: string;
    rule_version: number; first_observation: boolean; detection_label: string; formula: string; repeat_policy: string;
    eligibility?: QuoteEligibility; rule_type?: string; rule_label?: string; condition_text?: string;
    metric_value?: string | number | null; rule_evidence?: Record<string, unknown> };
};
export type AnomalyState = {
  items: (WatchItem & { rule: AnomalyRule | null; extended_rule?: ExtendedRule | null })[]; events: AnomalyEvent[]; runs: AnomalyRun[];
  runtime: { heartbeat_at: string | null; checks: number; last_error: string | null; running: boolean };
  collector: QuoteCollectorState;
  live_ready: boolean; live_pause_reason: string; check_interval_seconds: number; demo_freshness_seconds: number;
};

export type ExplanationFact = { id: string; label: string; text: string; values: Record<string, string> };
export type ExplanationEvidence = {
  event_id?: string; snapshot_id?: string; evidence_hash: string; market: string; symbol: string; name: string;
  mode: 'demo' | 'live' | 'quote'; facts: ExplanationFact[]; source: string; source_url: string; created_at: string;
  missing_evidence: { id: string; label: string; reason: string }[]; cause_limit: string;
  research_context?: ResearchState | null; research_evidence?: ResearchEvidence[]; research_used_count?: number; research_input_count?: number;
};
export type ExplanationRecord = {
  id: string; event_id: string | null; quote_snapshot_id: string | null; evidence_hash: string; prompt_version: string; requested_model: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'rejected' | 'interrupted';
  created_at: string; updated_at: string; retry_of: string | null; cost_basis: string; price_profile: string;
  input_tokens: number | null; output_tokens: number | null; actual_model: string | null;
  model_fingerprint: string | null; elapsed_ms: number | null; error_code: string | null;
  error_message: string | null; reviewed_at: string | null; estimated_cost: string;
  evidence: ExplanationEvidence;
  output: { statements: { kind: 'change' | 'trigger' | 'context' | 'scope'; text: string; evidence_ids: string[] }[];
    research_statements?: { text: string; evidence_ids: string[]; relation: 'context_only'; usage_note?: string }[] } | null;
  research_used_count?: number; research_input_count?: number; trace?: AnalysisTrace[];
};
export type ExplanationState = {
  configured: boolean; config_error: string | null; model: string | null; prompt_version: string;
  evidence: ExplanationEvidence; records: ExplanationRecord[];
  budget: { day_used: string; month_used: string; daily_limit: string | null; monthly_limit: string | null; note: string };
  next_call_reservation: string; max_output_tokens: number; automatic_check_note: string;
};

// Available on the LAN HTTP entry as well as localhost.
export function newRequestId() {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export type NotificationState = {
  configured: boolean; config_error: string | null; enabled: boolean; target_fingerprint: string;
  group_name: string | null; bot_name: string | null; verified_at: string | null; check_error: string | null;
  retry_after_seconds: number; active: boolean; records: NotificationRecord[];
};

export type BriefingCalendar = {
  date: string; state: 'open' | 'closed' | 'unknown'; reason: string; previous_trade_date: string | null;
  version: string; verified_on: string; covered_years: number[]; sources: { name: string; url: string }[]; note: string;
};
export type BriefingItem = Pick<WatchItem, 'market' | 'symbol' | 'name' | 'created_at'> & {
  status: 'covered' | 'missing'; reason_code: string | null; reason: string | null;
  data: { trade_date: string; close: string; volume: string; price_unit: '元'; volume_unit: '股';
    adjustment_label: string; source: string; source_url: string; received_at: string; origin: string;
    raw_volume_unit: string; volume_multiplier: number; bar: DailyBar } | null;
};
export type BriefingReport = {
  id: string; report_date: string; trigger_kind: 'manual' | 'scheduled';
  status: 'pending' | 'running' | 'succeeded' | 'partial' | 'failed' | 'interrupted' | 'empty';
  created_at: string; updated_at: string; retry_of: string | null; error_message: string | null;
  snapshot: { calendar: BriefingCalendar; watchlist: Pick<WatchItem, 'market' | 'symbol' | 'name' | 'created_at'>[]; frozen_at: string; watchlist_hash: string };
  output: { report_date: string; data_trade_date: string; data_cutoff_label: string;
    watchlist_frozen_at: string; completed_at: string; coverage: { total: number; covered: number; missing: number };
    items: BriefingItem[]; calendar: BriefingCalendar; model_cost: string; note: string;
    research_context?: ResearchState | null; research_evidence?: ResearchEvidence[]; research_cutoff?: string;
    research_coverage?: { available_sections: string[]; missing_sections: string[]; new_count: number; updated_count: number };
    narrative?: string; trace?: AnalysisTrace[] } | null;
  notification: NotificationRecord | null; watchlist_changed: boolean;
  delivery_status?: string;
};
export type AnalysisTrace = { stage: string; status: string; at?: string; duration_ms?: number; input_tokens?: number; output_tokens?: number; estimated_cost?: string; research_input_count?: number; research_used_count?: number; error_code?: string };
export type BriefingState = {
  today: string; calendar: BriefingCalendar;
  settings: { enabled: boolean; send_time: '08:45' | '09:00'; version: number; updated_at: string };
  schedule: { status: string; scheduled_at: string; report_id: string | null; reason: string | null };
  reports: BriefingReport[]; active: boolean; notifications_enabled: boolean;
  runtime: { running: boolean; heartbeat_at: string | null; last_error: string | null; checks: number };
  model_cost: string; start_window_seconds: number; send_window_minutes: number;
};

export class ApiFailure extends Error {
  readonly status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

export async function api<T>(path: string, options: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) controller.abort();
  const timeout = window.setTimeout(abort, 15_000);
  const headers = new Headers(options.headers);
  if (options.method && options.method !== 'GET') {
    headers.set('X-Alpha-Request', 'watchlist-v1');
    if (options.body) headers.set('Content-Type', 'application/json');
  }
  try {
    const response = await fetch(`${appBase}api${path}`, { ...options, headers, signal: controller.signal, cache: 'no-store' });
    const data = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiFailure(data?.error?.message ?? `请求未完成（${response.status}），请刷新后重试。`, response.status);
    }
    if (data === null) throw new Error('返回内容无法读取，请刷新后重试。');
    return data as T;
  } catch (error) {
    if (signal?.aborted) throw error;
    if (controller.signal.aborted) throw new Error('等待超时，未收到确认；请刷新列表核对后重试。');
    if (error instanceof TypeError) throw new Error('连接中断，未收到确认；请检查网络，稍后刷新核对。');
    throw error;
  } finally {
    window.clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
  }
}

export const messageOf = (error: unknown) => error instanceof Error ? error.message : '操作未完成，请稍后重试。';
export const itemKey = (item: Security) => item.market + item.symbol;
export const recordKey = (item: WatchItem) => `${itemKey(item)}:${item.created_at}`;
export const marketName = (market: Security['market']) => market === 'sh' ? '沪市 A 股' : '深市 A 股';
export const formatTime = (value: string, full = false) => new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
  ...(full ? { year: 'numeric' as const } : {}), hour12: false,
}).format(new Date(value));

export const knownTime = (value: string | null | undefined) => {
  if (!value || !Number.isFinite(new Date(value).getTime())) return '待确认';
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return `${value}（仅日期）`;
  if (/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/.test(value) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return `${value.replace('T', ' ')}（时区未记录）`;
  return formatTime(value, true);
};
export function safeHttpUrl(value: string | null | undefined) {
  if (!value) return null;
  try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null; }
  catch { return null; }
}

export type QuoteSnapshot = { id: string; created_at: string; market: string; symbol: string; quote: QuoteData; detail_path: string };
export type StockExplanationState = { current: { quote: QuoteData; quote_hash: string; watchlist_created_at: string } | null; error_message: string | null; snapshots: QuoteSnapshot[] };
