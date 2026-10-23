import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { api, knownTime, messageOf } from './api';
import type { LegacyHistory, LegacyRecord } from './api';

const kindLabels: Record<string, string> = { watchlist_item: '旧自选记录', watchlist_group: '旧分组记录', briefing: '旧简报', notification: '旧通知', undelivered: '旧未发送队列', budget: '旧预算记录', rule_settings: '旧规则设置' };
type MigrationOverview = { status?: string; imported_at?: string; source_fingerprint?: string; kinds?: Record<string, number>; watchlist?: { imported?: number; existing?: number }; warnings?: unknown[] };

function readable(value: unknown): string {
  if (value === null || value === undefined) return '未记录';
  if (typeof value === 'string') return value;
  return JSON.stringify(value, null, 2);
}

function RecordCard({ item }: { item: LegacyRecord }) {
  const object = item.content && typeof item.content === 'object' && !Array.isArray(item.content) ? item.content as Record<string, unknown> : null;
  const text = object && ['title', 'summary', 'content', 'message', 'text'].map(key => object[key]).find(value => typeof value === 'string' && value.length);
  const deliveryLabel = ({ success: '成功', delivered: '已投递', accepted: '接口已受理' } as Record<string, string>)[item.status || ''];
  return <article className="legacy-record">
    <div className="research-item-heading"><h3>{item.symbol || '综合记录'} · {kindLabels[item.kind] || item.kind}</h3><span className="research-status">{deliveryLabel ? `旧版记录${deliveryLabel}（未确认本人实收）` : `原状态：${item.status || '未记录'}`}</span></div>
    {item.kind === 'undelivered' && <p className="research-warning">这条旧队列仅供归档回查，不会补发。</p>}
    <p className="research-item-meta">记录时间：{knownTime(item.occurred_at)} · 原记录编号：{item.source_id}</p>
    {typeof text === 'string' && <p className="legacy-preview">{text}</p>}
    <details className="legacy-content"><summary>查看完整归档内容</summary><pre>{readable(item.content)}</pre><p className="research-item-meta">档案编号：{item.id}</p></details>
  </article>;
}

export default function LegacyHistoryPanel() {
  const [view, setView] = useState<LegacyHistory | null>(null);
  const [migration, setMigration] = useState<MigrationOverview | null>(null);
  const [migrationError, setMigrationError] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [kind, setKind] = useState('');
  const [symbol, setSymbol] = useState('');
  const [filter, setFilter] = useState({ kind: '', symbol: '' });
  const request = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const heading = useRef<HTMLHeadingElement>(null);
  const load = useCallback(async (cursor?: string) => {
    request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setBusy(true); setError('');
    const params = new URLSearchParams({ limit: '30' });
    if (filter.kind) params.set('kind', filter.kind);
    if (filter.symbol) params.set('symbol', filter.symbol);
    if (cursor) params.set('cursor', cursor);
    try {
      const result = await api<LegacyHistory>(`/legacy/history?${params}`, {}, controller.signal);
      if (!Array.isArray(result.items)) throw new Error('档案内容格式不正确，请重新读取。');
      if (!controller.signal.aborted && alive.current) setView(previous => cursor && previous ? {
        ...result, items: [...previous.items, ...result.items.filter(item => !previous.items.some(old => old.id === item.id))],
      } : result);
    } catch (failure) { if (!controller.signal.aborted && alive.current) setError(messageOf(failure)); }
    finally { if (!controller.signal.aborted && alive.current) setBusy(false); }
  }, [filter]);
  useEffect(() => {
    alive.current = true; void load(); heading.current?.focus({ preventScroll: true });
    const controller = new AbortController();
    void api<MigrationOverview>('/legacy/migration', {}, controller.signal).then(result => { if (!controller.signal.aborted) setMigration(result); }).catch(failure => { if (!controller.signal.aborted) setMigrationError(messageOf(failure)); });
    return () => { alive.current = false; request.current?.abort(); controller.abort(); };
  }, [load]);
  function search(event: FormEvent) {
    event.preventDefault();
    const next = { kind: kind.trim(), symbol: symbol.trim() };
    if (next.kind === filter.kind && next.symbol === filter.symbol) void load();
    else { setView(null); setFilter(next); }
  }
  return <section className="panel legacy-panel" aria-labelledby="legacy-title">
    <div className="research-heading"><div><div className="section-label">READ-ONLY ARCHIVE</div><h2 id="legacy-title" ref={heading} tabIndex={-1}>旧版历史档案</h2></div><button className="button button-outline" disabled={busy} onClick={() => void load()}>{busy ? '正在读取…' : '刷新档案'}</button></div>
    <p className="research-intro">旧版生成记录、异动和通知保留在这里。档案只供回查；原发送状态按原样展示，未发送的记录不会补发。</p>
    <details className="migration-summary"><summary>查看迁移范围与待确认事项</summary>{migrationError && <p className="research-warning">{migrationError}</p>}{migration !== null ? <><p className="research-cache-note">导入状态：{migration.status || '尚无导入记录'} · 导入时间：{knownTime(migration.imported_at)}。此状态仅表示资料导入，不代表旧业务已完成或本人收到消息。</p>{migration.watchlist && <p className="notification-help">自选：导入 {migration.watchlist.imported ?? '待确认'} 只，已存在 {migration.watchlist.existing ?? '待确认'} 只。</p>}{migration.kinds && <ul className="migration-counts">{Object.entries(migration.kinds).map(([key, count]) => <li key={key}><span>{kindLabels[key] || key}</span><strong>{count} 条</strong></li>)}</ul>}{!!migration.warnings?.length && <ul className="research-selection-notes">{migration.warnings.map((warning, index) => <li key={index}>{readable(warning)}</li>)}</ul>}<details className="legacy-content"><summary>查看导入校验标识</summary><pre>{readable(migration)}</pre></details></> : <p className="notification-help">{migrationError ? '暂未读到迁移概要，可刷新页面重试。' : '正在读取迁移概要…'}</p>}</details>
    <form className="legacy-filters" onSubmit={search}><label>股票代码<input name="legacy-symbol" value={symbol} onChange={event => setSymbol(event.target.value)} inputMode="numeric" autoComplete="off" placeholder="留空查看全部" maxLength={20} /></label><label>记录类型<select name="legacy-kind" aria-label="记录类型" value={kind} onChange={event => setKind(event.target.value)}><option value="">全部类型</option>{Object.entries(kindLabels).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label><button className="button button-primary" disabled={busy} type="submit">筛选档案</button>{(filter.kind || filter.symbol) && <button className="button button-quiet" type="button" onClick={() => { setKind(''); setSymbol(''); setView(null); setFilter({ kind: '', symbol: '' }); }}>清除筛选</button>}</form>
    {error && <div className="alert alert-error" role="alert">档案读取未完成。{error}<button className="text-button" disabled={busy} onClick={() => void load()}>重新读取档案</button></div>}
    {view && <p className="research-cache-note" role="status">找到 {view.total} 条，已展示 {view.items.length} 条。</p>}
    {!view ? !error && <div className="empty-state"><p>正在读取已迁移的旧记录…</p></div> : !view.items.length ? <div className="empty-state"><h3>{filter.kind || filter.symbol ? '没有匹配的档案' : '还没有已迁移的旧记录'}</h3><p>{filter.kind || filter.symbol ? '清除筛选后可以查看全部档案。' : '迁移状态见上方“查看迁移范围与待确认事项”。'}</p></div> : <div className="legacy-records">{view.items.map(item => <RecordCard key={item.id} item={item} />)}</div>}
    {view?.next_cursor && <button className="button button-outline legacy-more" disabled={busy} onClick={() => void load(view.next_cursor || undefined)}>{busy ? '正在读取…' : '读取更多档案'}</button>}
  </section>;
}
