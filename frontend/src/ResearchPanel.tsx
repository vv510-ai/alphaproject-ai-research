import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { api, knownTime, messageOf, safeHttpUrl } from './api';
import type { AnalysisTrace, ResearchEvidence, ResearchItem, ResearchKey, ResearchSection, ResearchState } from './api';

const labels = { available: '已取得资料', partial: '资料不完整', unavailable: '暂无可用资料', stale: '资料已过期' };
const changeLabels = { new: '新增', updated: '有更新', unchanged: '未变化' };
const numbers = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 4 });
const quoteKeys = new Set(['indices', 'global', 'sectors']);
function ItemWarnings({ item }: { item: ResearchItem }) {
  const names: Record<string, string> = { source: '来源名称', source_url: '来源链接', url: '原文链接', price: '数值', change_pct: '涨跌幅', as_of: '行情时点', published_at: '发布时间', verified_time: '经核对的时间' };
  return <>{item.eligibility_note && <p className="research-warning">{item.eligibility_note}</p>}{!!item.missing_fields?.length && <p className="research-missing">尚缺：{item.missing_fields.map(field => names[field] || field).join('、')}。</p>}</>;
}

export function AnalysisTraceView({ trace }: { trace?: AnalysisTrace[] }) {
  if (!trace?.length) return null;
  const stageLabels: Record<string, string> = { evidence_frozen: '固定解释依据', research_frozen: '固定市场与资讯', budget_reserved: '预留费用', model_call: '模型调用', output_validation: '核对模型输出', usage_settled: '记录用量与费用', daily_validated: '核对收盘日线' };
  const statusLabels: Record<string, string> = { completed: '已完成', failed: '未完成', rejected: '未采用', interrupted: '已中断', partial: '部分完成', not_used_by_choice: '本次未使用' };
  return <details className="analysis-trace"><summary>查看整理与生成过程</summary><ol>{trace.map((entry, index) => <li key={index}><strong>{stageLabels[entry.stage] || entry.stage}</strong><span>{statusLabels[entry.status] || entry.status}</span><p>{knownTime(entry.at)}{entry.duration_ms !== undefined && ` · 耗时 ${entry.duration_ms} 毫秒`}{entry.research_input_count !== undefined && ` · 输入资料 ${entry.research_input_count} 条`}{entry.research_used_count !== undefined && ` · 引用 ${entry.research_used_count} 条`}{entry.estimated_cost !== undefined && ` · 估算 ${entry.estimated_cost} 元`}</p>{entry.error_code && <p>记录代码：{entry.error_code}</p>}</li>)}</ol></details>;
}

function amount(value: string | number | null | undefined, percent = false) {
  if (value === null || value === undefined || value === '') return '待确认';
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return '待确认';
  return `${percent && numeric > 0 ? '+' : ''}${numbers.format(numeric)}${percent ? '%' : ''}`;
}

export function SourceLink({ url, name }: { url?: string | null; name?: string | null }) {
  const safe = safeHttpUrl(url);
  return safe ? <a href={safe} target="_blank" rel="noreferrer">{name || '查看来源'}<span className="sr-only">（在新标签页打开）</span></a> : <span>{name || '来源待确认'}</span>;
}

export function ResearchArticle({ item }: { item: ResearchItem }) {
  return <>
    <div className="research-item-heading"><h4><SourceLink url={item.url || item.source_url} name={item.title || item.name || '标题待确认'} /></h4>{(item.stale || item.is_stale) && <span className="research-status stale">已过期 · 仅留档</span>}{item.carried_forward && <span className="research-status">沿用上次资料</span>}{item.change_kind && item.change_kind !== 'unchanged' && <span className="research-change">{changeLabels[item.change_kind]}</span>}</div>
    {item.summary && <p className="research-summary">{item.summary}</p>}
    <div className="research-item-meta"><span>来源：{item.source || '待确认'}</span><span>{item.published_at ? '发布时间' : item.as_of ? '资料时点' : '发布时间'}：{knownTime(item.published_at || item.as_of)}</span>{item.scheduled_at && <span>日程时间：{knownTime(item.scheduled_at)}</span>}<span>抓取时间：{knownTime(item.received_at)}</span></div>
    <ItemWarnings item={item} />
  </>;
}

export function ResearchSectionView({ section }: { section: ResearchSection }) {
  const id = useId();
  const quotes = quoteKeys.has(section.key);
  return <section className="research-section" aria-labelledby={id}>
    <div className="research-section-heading"><h3 id={id}>{section.title}</h3><span className={`research-status ${section.status}`}>{labels[section.status] || '状态待确认'}</span></div>
    <p className="research-section-source"><SourceLink url={section.source_url} name={section.source} /><span>资料时点：{knownTime(section.as_of)}</span><span>抓取时间：{knownTime(section.received_at)}</span></p>
    {section.error_message && <p className="research-warning">{section.error_message}{section.items.length > 0 && ' 以下保留已取得的资料。'}</p>}
    {!section.items.length ? <p className="research-empty">这一项暂没有可展示的资料。可尝试更新；缺失值不会填为 0。</p> : quotes ? <ul className="research-quotes">{section.items.map(item => {
      const change = item.change_pct == null ? null : Number(item.change_pct);
      return <li key={item.id}><div className="research-item-heading"><h4><SourceLink url={item.source_url || item.url} name={item.name || item.title || item.code} /></h4>{item.code && <span className="stock-code">{item.code}</span>}</div>
        {(item.stale || item.is_stale) && <span className="research-status stale">行情已过期</span>}{item.carried_forward && <span className="research-status">沿用上次资料</span>}
        <div className="research-quote-values"><strong>{amount(item.price)}</strong><span className={change === null || !Number.isFinite(change) ? '' : change > 0 ? 'up' : change < 0 ? 'down' : ''}>{amount(item.change_pct, true)}</span></div>
        <div className="research-item-meta"><span>来源：{item.source || section.source || '待确认'}</span><span>行情时点：{knownTime(item.as_of)}</span><span>抓取时间：{knownTime(item.received_at)}</span></div>
        <ItemWarnings item={item} />
      </li>;
    })}</ul> : <ul className="research-articles">{section.items.map(item => <li key={item.id}><ResearchArticle item={item} /></li>)}</ul>}
  </section>;
}

export function ResearchEvidenceList({ context, evidence, idPrefix, usedCount }: {
  context?: ResearchState | null; evidence?: ResearchEvidence[]; idPrefix: string; usedCount?: number;
}) {
  if (!context && !evidence?.length) return null;
  return <section className="research-evidence" aria-label="本次固定的资讯依据">
    <h4>本次固定的资讯依据</h4>
    <p className="notification-help">资料截止：{knownTime(context?.cutoff || context?.created_at)}。{usedCount !== undefined && `本次引用 ${usedCount} 条资料。`}资讯只能补充背景，不能单凭时间接近确认涨跌原因。</p>
    <p className="notification-help">资料截止为依据保存时刻，不等于行情来源时间；较晚的资料不能证明当时涨跌的原因。</p>
    {context?.error_message && <p className="research-warning">{context.error_message}</p>}
    {!!context?.selection_notes?.length && <ul className="research-selection-notes">{context.selection_notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}
    {!!context?.sections?.length && <ul className="research-coverage">{context.sections.map(section => <li key={section.key}><strong>{section.title}</strong><span>{labels[section.status] || section.status} · {section.items.length} 条</span>{section.error_message && <p>{section.error_message}</p>}</li>)}</ul>}
    {evidence?.length ? <div className="research-evidence-list">{evidence.map(item => <details key={item.id} id={`${idPrefix}-${item.id}`}><summary><span>{item.id}</span>{item.title || item.name || '固定资料'}</summary><div><ResearchArticle item={item} /><p className="research-item-meta">原始记录：{item.original_id || item.id}</p></div></details>)}</div> : <p className="notification-help">这份记录没有可引用的资讯。已保存的价格事实仍可单独核对。</p>}
  </section>;
}

export default function ResearchPanel({ mode = 'markets', market, symbol, name, onClose }: {
  mode?: 'markets' | 'news' | 'stock'; market?: string; symbol?: string; name?: string; onClose?: () => void;
}) {
  const [view, setView] = useState<ResearchState | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [remaining, setRemaining] = useState(0);
  const [notice, setNotice] = useState('');
  const request = useRef<AbortController | null>(null);
  const mutation = useRef(false);
  const alive = useRef(true);
  const deadline = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const id = useId();
  const path = market && symbol ? `/research/${market}/${symbol}` : '/research';
  const title = mode === 'markets' ? '综合行情' : mode === 'news' ? '资讯与日历' : `${name || symbol} · 资讯与公告`;
  const accept = useCallback((result: ResearchState) => {
    if (!Array.isArray(result.sections)) throw new Error('资料格式无法读取，请重新读取。');
    setView(result); setError('');
    deadline.current = Date.now() + Math.max(0, result.retry_after_seconds || 0) * 1000;
    setRemaining(Math.max(0, result.retry_after_seconds || 0));
  }, []);
  const load = useCallback(async () => {
    if (mutation.current) return;
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setLoading(true);
    try {
      const result = await api<ResearchState>(path, {}, controller.signal);
      if (!controller.signal.aborted && alive.current) accept(result);
    } catch (failure) { if (!controller.signal.aborted && alive.current) setError(messageOf(failure)); }
    finally { if (!controller.signal.aborted && alive.current) setLoading(false); }
  }, [accept, path]);
  useEffect(() => {
    alive.current = true;
    void load();
    heading.current?.focus({ preventScroll: true });
    return () => { alive.current = false; request.current?.abort(); };
  }, [load]);
  useEffect(() => {
    const timer = window.setInterval(() => setRemaining(Math.max(0, Math.ceil((deadline.current - Date.now()) / 1000))), 1000);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (!view?.refresh_in_progress && view?.refresh_state !== 'processing' && view?.refresh_state !== 'running') return;
    const timer = window.setInterval(() => { if (!document.hidden) void load(); }, 2500);
    return () => window.clearInterval(timer);
  }, [load, view?.refresh_state, view?.refresh_in_progress]);
  async function refresh() {
    if (refreshing || mutation.current || remaining > 0) return;
    mutation.current = true; setRefreshing(true); setNotice(''); setError('');
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    try {
      const result = await api<ResearchState>(`${path}/refresh`, { method: 'POST' }, controller.signal);
      if (!controller.signal.aborted && alive.current) {
        accept(result);
        setNotice(result.refresh_in_progress && result.active_scope && result.active_scope !== result.scope ? '另一份资料正在更新。完成后可更新这里，已有资料继续保留。' : result.refresh_state === 'succeeded' ? '已更新资料。各项来源与时间见下方。' : result.refresh_state === 'processing' ? '后台正在更新，已有资料继续保留。' : '本次更新状态见下方；未取得的项目保持缺失。');
      }
    } catch (failure) { if (!controller.signal.aborted && alive.current) setError(messageOf(failure)); }
    finally { mutation.current = false; if (alive.current) { setRefreshing(false); setLoading(false); } }
  }
  const selected: ResearchKey[] = mode === 'markets' ? ['indices', 'global', 'sectors'] : mode === 'news' ? ['news', 'announcements', 'calendar'] : ['news', 'announcements', 'calendar', 'sectors'];
  const sections = view?.sections.filter(section => selected.includes(section.key)) || [];
  const processing = refreshing || !!view?.refresh_in_progress || ['processing', 'running'].includes(view?.refresh_state || '');
  const anotherScope = view?.refresh_in_progress && view.active_scope && view.active_scope !== view.scope;
  return <section className={`panel research-panel ${mode === 'stock' ? 'stock-research' : ''}`} aria-labelledby={id}>
    <div className="research-heading"><div><div className="section-label">{mode === 'markets' ? 'MARKET OVERVIEW' : 'SOURCE MATERIALS'}</div><h2 ref={heading} tabIndex={-1} id={id}>{title}</h2></div><div className="research-actions"><button className="button button-outline" disabled={processing || remaining > 0} onClick={() => void refresh()}>{anotherScope ? '等待其他资料更新…' : processing ? '正在更新资料…' : remaining > 0 ? `${remaining} 秒后可更新` : '更新资料'}</button>{onClose && <button className="button button-quiet" onClick={onClose}>收起资料</button>}</div></div>
    <p className="research-intro">{mode === 'markets' ? '把指数、全球市场和板块放在一起查看。不同市场的交易时间不同，请分别核对资料时点。' : '保留资料原始来源和发布时间。新增或更新表示本次抓取的变化，不表示信息已经证实。'}</p>
    <p className="research-cache-note">页面读取已保存资料；“更新资料”才会请求来源。{view?.created_at && `最近保存 ${knownTime(view.created_at)}。`}</p>
    <div className="research-notice" role="status">{notice}</div>
    {error && <div className="alert alert-error" role="alert">读取或更新未完成，已有资料仍保留。{error}<button className="text-button" disabled={loading || refreshing} onClick={() => void load()}>重新读取已保存资料</button></div>}
    {view?.error_message && <p className="research-warning">{view.error_message}</p>}
    {!view ? <div className="empty-state"><h3>{error ? '暂时无法读取资料' : '正在读取已保存资料'}</h3><p>{error ? '重新读取可核对服务状态；不影响自选和已有记录。' : '首次使用时可能尚无缓存，读取完成后可选择更新。'}</p></div> : !sections.length ? <div className="empty-state"><h3>还没有这一类资料</h3><p>选择“更新资料”尝试获取；未取得的来源会保留明确状态。</p></div> : <div className="research-sections">{sections.map(section => <ResearchSectionView section={section} key={section.key} />)}</div>}
  </section>;
}
