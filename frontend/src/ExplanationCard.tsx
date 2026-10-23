import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { api, ApiFailure, formatTime, messageOf, newRequestId } from './api';
import type { ExplanationRecord, ExplanationState } from './api';
import { AnalysisTraceView, ResearchEvidenceList } from './ResearchPanel';

type GenerateRequest = { request_id: string; evidence_hash: string; retry_of: string | null };
const stateLabels: Record<ExplanationRecord['status'], string> = {
  pending: '等待生成', running: '正在解读这份依据', succeeded: 'AI 解读已保存',
  failed: '生成未完成', rejected: '检查未通过 · 结果未采用', interrupted: '调用已中断',
};
const sectionLabels = { change: '发生了什么变化', trigger: '为什么触发提醒', context: '这份报价代表什么', scope: '这次结果能说明到哪里' };
const reviewLabels = ['我核对了价格、计算和涨跌方向', '我点开了依据，能对上这份记录', '解读没有编造消息或具体涨跌原因'];

export default function ExplanationCard({ eventId, contextKind = 'event', defaultOpen = false }: { eventId: string; contextKind?: 'event' | 'quote'; defaultOpen?: boolean }) {
  const path = contextKind === 'quote' ? `/quote-snapshots/${eventId}/explanations` : `/events/${eventId}/explanations`;
  const subjectLabel = contextKind === 'quote' ? '固定报价' : '事件';
  const [open, setOpen] = useState(defaultOpen);
  const [view, setView] = useState<ExplanationState | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [checks, setChecks] = useState([false, false, false]);
  const trigger = useRef<HTMLButtonElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const alive = useRef(true);
  const readRequest = useRef<AbortController | null>(null);
  const actionRequest = useRef<AbortController | null>(null);
  const pending = useRef<GenerateRequest | null>(null);
  const instance = useId();
  const storageKey = `alpha-ai-request-${contextKind === 'quote' ? 'quote-' : ''}${eventId}`;
  const bodyId = `explanation-${contextKind}-${eventId}-${instance}`;

  const remember = useCallback((value: GenerateRequest | null) => {
    pending.current = value;
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* Memory still protects this mounted page. */ }
  }, [storageKey]);
  const load = useCallback(async () => {
    readRequest.current?.abort();
    const controller = new AbortController(); readRequest.current = controller;
    try {
      const next = await api<ExplanationState>(path, {}, controller.signal);
      if (!controller.signal.aborted && alive.current) {
        setView(next); setLoadError('');
        const found = pending.current && next.records.find(record => record.id === pending.current?.request_id);
        if (found) { setSelectedId(found.id); remember(null); }
      }
    } catch (failure) {
      if (!controller.signal.aborted && alive.current) setLoadError(messageOf(failure));
    }
  }, [path, remember]);

  useEffect(() => {
    alive.current = true;
    try {
      const value = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (value && typeof value.request_id === 'string' && typeof value.evidence_hash === 'string') pending.current = value;
    } catch { /* A malformed local hint is ignored, never sent to the model. */ }
    return () => { alive.current = false; readRequest.current?.abort(); actionRequest.current?.abort(); };
  }, [storageKey]);
  useEffect(() => {
    if (!open) return;
    let stopped = false, timer: number;
    async function poll() { if (!document.hidden) await load(); if (!stopped) timer = window.setTimeout(poll, 3000); }
    void poll();
    heading.current?.focus({ preventScroll: true });
    return () => { stopped = true; window.clearTimeout(timer); readRequest.current?.abort(); };
  }, [open, load]);

  const selected = view?.records.find(record => record.id === selectedId) || view?.records[0];
  const evidence = selected?.evidence || view?.evidence;
  const active = view?.records.some(record => record.status === 'pending' || record.status === 'running');
  const currentRecords = view?.records.filter(record => record.evidence_hash === view.evidence.evidence_hash
    && record.prompt_version === view.prompt_version && record.requested_model === (view.model || 'deepseek-flash'));
  const successful = currentRecords?.find(record => record.status === 'succeeded');
  const latest = currentRecords?.[0];
  useEffect(() => { setChecks([false, false, false]); }, [selected?.id]);

  async function generate() {
    if (!view || busy || active) return;
    setBusy(true); setActionError(''); setNotice('');
    const controller = new AbortController(); actionRequest.current = controller;
    const body = pending.current || { request_id: newRequestId(), evidence_hash: view.evidence.evidence_hash,
      retry_of: latest && ['failed', 'rejected', 'interrupted'].includes(latest.status) ? latest.id : null };
    remember(body);
    try {
      const result = await api<{ record: ExplanationRecord; reused: boolean }>(path,
        { method: 'POST', body: JSON.stringify(body) }, controller.signal);
      remember(null);
      if (alive.current) {
        setSelectedId(result.record.id);
        setNotice(result.reused ? '已读取同一份依据的现有记录，没有新增模型请求。' : '请求已交给后台。结果保存后会在这里显示，关掉页面也不会自动重发。');
      }
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status >= 400 && failure.status < 500) remember(null);
      if (!controller.signal.aborted && alive.current) setActionError(messageOf(failure));
    } finally {
      if (alive.current) { setBusy(false); await load(); }
    }
  }

  async function saveReview() {
    if (!selected || reviewBusy || !checks.every(Boolean)) return;
    setReviewBusy(true); setActionError('');
    const controller = new AbortController(); actionRequest.current = controller;
    try {
      await api(`/explanations/${selected.id}/review`, { method: 'POST', body: JSON.stringify({
        facts_checked: checks[0], citations_checked: checks[1], no_invented_causes: checks[2],
      }) }, controller.signal);
      if (alive.current) setNotice('你的核对已保存。这个记录表示本人检查过，不代表模型永远正确。');
    } catch (failure) {
      if (!controller.signal.aborted && alive.current) setActionError(messageOf(failure));
    } finally {
      if (alive.current) { setReviewBusy(false); await load(); }
    }
  }

  function showFact(id: string) {
    const node = document.getElementById(`${bodyId}-${id}`) as HTMLDetailsElement | null;
    if (node) { node.open = true; node.querySelector('summary')?.focus(); node.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
  }

  return <div className="explanation-card">
    <button className="button button-outline" ref={trigger} aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>{open ? '收起 AI 解释' : contextKind === 'quote' ? '查看依据并解释这份报价' : '查看依据并解释这次异动'}</button>
    {open && <section id={bodyId} aria-labelledby={`${bodyId}-title`}>
      <div className="explanation-intro"><span className="section-label">READ THE EVIDENCE · LESSON 13</span><h4 id={`${bodyId}-title`} ref={heading} tabIndex={-1}>先看事实，再读 AI 解读</h4><p>解释使用这份{subjectLabel}保存的记录。首页报价和规则后来变化，也不会替换这里的依据。</p></div>
      {loadError && <div className="alert alert-error" role="alert">读取失败，保留上次内容。{loadError}<button className="text-button" onClick={() => void load()}>重新读取</button></div>}
      {!view ? !loadError && <p className="notification-help">正在读取{subjectLabel}依据与已有解释…</p> : <>
        <div className="explanation-budget"><strong>{view.configured ? `模型：${view.model}` : '模型尚未配置'}</strong><span>今日估算 {view.budget.day_used} / {view.budget.daily_limit ?? '未设'} 元</span><span>本月 {view.budget.month_used} / {view.budget.monthly_limit ?? '未设'} 元</span><p>{view.budget.note}</p>{view.config_error && <p className="notification-failure">{view.config_error}</p>}</div>
        {evidence && <>
          <div className="explanation-section-label">01 · 可以复算的事实</div>
          <p className="notification-help">{evidence.name} · {evidence.symbol} · {evidence.mode === 'demo' ? '合成演练，非真实行情' : evidence.mode === 'quote' ? '固定报价 · 实时性未验证' : '历史事件依据'} · {formatTime(evidence.created_at, true)}</p>
          <div className="explanation-facts">{evidence.facts.map(fact => <details key={fact.id} id={`${bodyId}-${fact.id}`} open={['F2', 'F3', 'F4'].includes(fact.id)}>
            <summary><span>{fact.id}</span>{fact.label}</summary><p>{fact.text}</p>
          </details>)}</div>
          <p className="notification-help">这些事实由程序从保存的记录读取和复算。点击标题可展开；来源：{evidence.source}。</p>
          <ResearchEvidenceList context={evidence.research_context} evidence={evidence.research_evidence} idPrefix={bodyId} usedCount={selected?.research_used_count ?? evidence.research_used_count} />
          {evidence.research_evidence && <p className="notification-help">模型输入资料 {selected?.research_input_count ?? evidence.research_input_count ?? evidence.research_evidence.length} 条；已保存结果实际引用 {selected?.research_used_count ?? 0} 条。未被引用的资料仍可在上方核对。</p>}
        </>}
        <div className="explanation-section-label">02 · AI 帮你读懂</div>
        <div className="notification-actions">
          {successful ? <button className="button button-primary" onClick={() => { setSelectedId(successful.id); setNotice('正在显示已保存的解释，没有调用模型。'); }}>查看已保存的解释</button>
            : <button className="button button-primary" disabled={!view.configured || busy || !!active || !!loadError} onClick={() => void generate()}>{busy ? '正在提交…' : active ? '后台正在生成…' : pending.current ? '核对上次生成请求' : latest ? '重试一次（会再次调用模型）' : '用这些依据生成解释'}</button>}
          <button className="text-button" onClick={() => void load()}>读取最新记录</button>
        </div>
        {!successful && <p className="notification-help">本次最多输出 {view.max_output_tokens} tokens，先预留约 {view.next_call_reservation} 元。只在手动点击时调用；刷新读取已保存结果。</p>}
        {actionError && <div className="alert alert-error" role="alert">{actionError}<button className="text-button" onClick={() => setActionError('')}>关闭提示</button></div>}
        {notice && <p className="anomaly-notice" role="status">{notice}</p>}
        {view.records.length > 1 && <label className="explanation-history">查看生成记录<select id={`${bodyId}-history`} name="explanation-history" value={selected?.id || ''} onChange={e => setSelectedId(e.target.value)}>{view.records.map(record => <option key={record.id} value={record.id}>{formatTime(record.created_at, true)} · {stateLabels[record.status]}</option>)}</select></label>}
        {selected && <div className="explanation-result" aria-live="polite">
          <span className={`notification-badge ${selected.status === 'succeeded' ? 'on' : ''}`}>{stateLabels[selected.status]}</span>
          {selected.error_message && <p className="notification-failure">{selected.error_message}</p>}
          {selected.output && <>
            <p className="notification-help">{view.automatic_check_note}</p>
            {selected.output.statements.map(statement => <article className="explanation-statement" key={statement.kind}><h5>{sectionLabels[statement.kind]}</h5><p>{statement.text}</p><div className="explanation-citations"><span>核对依据：</span>{statement.evidence_ids.map(id => <button key={id} className="text-button" onClick={() => showFact(id)}>{selected.evidence.facts.find(f => f.id === id)?.label || id}</button>)}</div></article>)}
            {!!selected.output.research_statements?.length && <div className="explanation-research-statements"><h5>可核对的背景资料</h5>{selected.output.research_statements.map((statement, index) => <article className="explanation-statement" key={index}><p>{statement.text}</p>{statement.usage_note && <p className="research-usage-note">怎样使用这条资料：{statement.usage_note}</p>}<div className="explanation-citations"><span>核对固定引用：</span>{statement.evidence_ids.map(id => <button key={id} className="text-button" onClick={() => showFact(id)}>{selected.evidence.research_evidence?.find(item => item.id === id)?.title || id}</button>)}</div></article>)}</div>}
          </>}
          <details className="explanation-metadata"><summary>生成记录与费用依据</summary><dl><div><dt>{subjectLabel}编号</dt><dd>{selected.event_id || selected.quote_snapshot_id}</dd></div><div><dt>模型 / 提示词版本</dt><dd>{selected.actual_model || selected.requested_model} / {selected.prompt_version}</dd></div><div><dt>生成时间</dt><dd>{formatTime(selected.created_at, true)}</dd></div><div><dt>输入 / 输出 tokens</dt><dd>{selected.input_tokens ?? '未取得'} / {selected.output_tokens ?? '未取得'}</dd></div><div><dt>本次估算</dt><dd>{selected.estimated_cost} 元 · {selected.cost_basis === 'usage_estimate' ? '按返回用量估算' : selected.cost_basis === 'reserved_unknown' ? '用量未知，保留预留' : '未调用或明确拒绝'}</dd></div></dl></details>
          <AnalysisTraceView trace={selected.trace} />
        </div>}
        {evidence && <div className="explanation-missing"><strong>原因证据仍然不足</strong><p>{evidence.cause_limit}</p><ul>{evidence.missing_evidence.map(item => <li key={item.id}><b>{item.label}</b><span>{item.reason}</span></li>)}</ul><p>这是当前输入的固定边界，不会因为 AI 写得流畅就消失。</p></div>}
        {selected?.status === 'succeeded' && <div className="explanation-review"><div className="explanation-section-label">03 · 轮到你核对</div>{selected.reviewed_at ? <p className="anomaly-notice">你已在 {formatTime(selected.reviewed_at, true)} 保存核对。</p> : <><p className="notification-help">逐条检查上面的解读；有一项不确定，就先保留未核对，不必勾选。</p>{reviewLabels.map((label, index) => <label className="anomaly-checkbox" key={label}><input id={`${bodyId}-review-${index}`} name={`explanation-review-${index}`} type="checkbox" checked={checks[index]} onChange={e => setChecks(old => old.map((value, at) => at === index ? e.target.checked : value))} disabled={reviewBusy} /><span>{label}</span></label>)}<button className="button button-outline" onClick={() => void saveReview()} disabled={reviewBusy || !checks.every(Boolean)}>{reviewBusy ? '正在保存核对…' : '保存我的核对'}</button></>}</div>}
      </>}
    </section>}
  </div>;
}
