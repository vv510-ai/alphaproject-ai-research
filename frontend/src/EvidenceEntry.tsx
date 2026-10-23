import { useCallback, useEffect, useRef, useState } from 'react';
import { api, formatTime, messageOf } from './api';
import type { AnomalyEvent, QuoteSnapshot, StockExplanationState } from './api';
import AnomalyEventCard from './AnomalyEventCard';
import ExplanationCard from './ExplanationCard';

const uuidPattern = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';

function StockQuoteEntry({ market, symbol }: { market: string; symbol: string }) {
  const [view, setView] = useState<StockExplanationState | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const request = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setLoading(true); setError('');
    try {
      const data = await api<StockExplanationState>(`/stocks/${market}/${symbol}/explanations`, {}, controller.signal);
      if (!controller.signal.aborted) { setView(data); setError(''); }
    } catch (failure) { if (!controller.signal.aborted) setError(messageOf(failure)); }
    finally { if (!controller.signal.aborted) setLoading(false); }
  }, [market, symbol]);
  useEffect(() => { void load(); return () => request.current?.abort(); }, [load]);
  async function freeze() {
    if (!view?.current || busy || loading) return;
    setBusy(true); setError('');
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    try {
      const result = await api<{ snapshot: QuoteSnapshot }>(`/stocks/${market}/${symbol}/explanation-snapshots`, {
        method: 'POST', body: JSON.stringify({ watchlist_created_at: view.current.watchlist_created_at, quote_hash: view.current.quote_hash }),
      }, controller.signal);
      if (!controller.signal.aborted) window.location.hash = `quote=${result.snapshot.id}`;
    } catch (failure) { if (!controller.signal.aborted) setError(messageOf(failure)); }
    finally { if (!controller.signal.aborted) setBusy(false); }
  }
  const quote = view?.current?.quote;
  return <>
    <p className="notification-help">先核对已保存的报价，再固定为解释依据。固定不会获取新行情，也不会调用模型；生成解释在下一步手动选择。</p>
    <details className="quote-evidence"><summary>怎样把最新资料用于下一份解释</summary><p className="notification-help">先回到这只股票的“资讯与公告”更新资料，再更新报价，最后固定报价。重复固定同一份已保存报价会打开原记录，不会补入后来抓取的资讯。资料截止为依据保存时刻，行情来源时间另行展示。</p></details>
    {error && <div className="alert alert-error" role="alert">{error}</div>}
    {!view ? loading && <p role="status">正在读取报价…</p> : <>
      {view.error_message && <p className="notification-failure">{view.error_message}</p>}
      {quote && <div className="quote-preview"><h3>{quote.name} · {market}{symbol}</h3><p className="anomaly-calculation">报价 {quote.price} 元 · 昨收 {quote.previous_close} 元 · 变化 {quote.change_pct}%</p><p>来源时间：{formatTime(quote.source_reported_at, true)}<br />接收时间：{formatTime(quote.received_at, true)}<br />来源：{quote.source}</p><p className="notification-help">这是一份已保存的报价，实时性未验证。解释只对应上面的时间。</p><button className="button button-primary" disabled={busy || loading || !!error} onClick={() => void freeze()}>{busy ? '正在固定…' : '固定这份报价并查看依据'}</button></div>}
      <h3>已固定的报价</h3>
      {view.snapshots.length ? <ul className="quote-snapshot-list">{view.snapshots.map(snapshot => <li key={snapshot.id}><a href={snapshot.detail_path}>{snapshot.quote.price} 元 · 来源 {formatTime(snapshot.quote.source_reported_at, true)}</a><span>固定于 {formatTime(snapshot.created_at, true)}</span></li>)}</ul> : <p className="notification-help">还没有固定的报价。后来的行情更新不会改写这里的历史。</p>}
    </>}
    <button className="text-button" disabled={busy || loading} onClick={() => void load()}>{loading ? '正在读取…' : '重新读取已保存的报价'}</button>
  </>;
}

function EventEntry({ id, onNotifications }: { id: string; onNotifications: () => void }) {
  const [event, setEvent] = useState<AnomalyEvent | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let stopped = false, timer: number;
    let controller: AbortController | null = null;
    async function poll() {
      if (!document.hidden) {
        controller = new AbortController();
        try {
          const data = await api<AnomalyEvent>(`/events/${id}`, {}, controller.signal);
          if (!stopped) { setEvent(data); setError(''); }
        } catch (failure) { if (!stopped) setError(messageOf(failure)); }
      }
      if (!stopped) timer = window.setTimeout(poll, 3000);
    }
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); controller?.abort(); };
  }, [id, revision]);
  return <><button className="text-button" onClick={() => setRevision(value => value + 1)}>读取最新事件与收件状态</button>{error && <div className="alert alert-error" role="alert">状态读取失败，保留上次结果。{error}</div>}{event ? <AnomalyEventCard event={event} onNotifications={onNotifications} defaultOpen /> : !error && <p>正在读取原事件…</p>}</>;
}

export default function EvidenceEntry({ hash, onNotifications }: { hash: string; onNotifications: () => void }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); heading.current?.scrollIntoView({ block: 'start' }); }, [hash]);
  const match = new RegExp(`^#(event|quote)=(${uuidPattern})$`, 'i').exec(hash);
  const stock = /^#stock=(sh|sz)([0-9]{6})$/.exec(hash);
  const kind = match?.[1].toLowerCase(), id = match?.[2].toLowerCase();
  const title = stock ? '解释股票报价' : kind === 'event' ? '这次事件的原始依据' : kind === 'quote' ? '这份报价的原始依据' : '链接需要核对';
  return <section className="panel evidence-entry" aria-labelledby="evidence-entry-title">
    <div className="notification-heading"><div><div className="section-label">SAVED EVIDENCE</div><h2 ref={heading} tabIndex={-1} id="evidence-entry-title">{title}</h2></div><a className="button button-quiet" href="#">回到自选列表</a></div>
    {match && <label className="evidence-link">保留此链接，下次仍可打开同一份依据<input name="evidence-link" readOnly value={window.location.href} onFocus={e => e.target.select()} /></label>}
    {kind === 'event' && id ? <EventEntry key={id} id={id} onNotifications={onNotifications} /> : kind === 'quote' && id ? <ExplanationCard key={id} eventId={id} contextKind="quote" defaultOpen /> : stock ? <StockQuoteEntry key={stock[0]} market={stock[1]} symbol={stock[2]} /> : <p className="notification-failure" role="alert">链接标识不完整或不正确。请回到自选、异动或通知记录中重新选择。</p>}
  </section>;
}
