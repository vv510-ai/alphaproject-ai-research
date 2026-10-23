import { useCallback, useEffect, useRef, useState } from 'react';
import type { FormEvent, ReactNode } from 'react';
import { api, appBase, formatTime, itemKey, knownTime, marketName, messageOf, recordKey } from './api';
import type { AccessInfo, Security, WatchGroup, WatchItem, WatchUndo } from './api';
import type { QuoteState } from './api';
import QuotePanel from './QuotePanel';
import DailyPanel from './DailyPanel';
import NotificationsPanel from './NotificationsPanel';
import AnomaliesPanel from './AnomaliesPanel';
import BriefingsPanel from './BriefingsPanel';
import EvidenceEntry from './EvidenceEntry';
import ResearchPanel from './ResearchPanel';
import LegacyHistoryPanel from './LegacyHistoryPanel';
import WatchGroups from './WatchGroups';

function Icon({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <svg className={`icon ${className}`} width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
}
const Refresh = () => <Icon><path d="M20 7v5h-5M4 17v-5h5" /><path d="M6.1 7A7 7 0 0 1 18 5l2 2M4 17l2 2a7 7 0 0 0 11.9-2" /></Icon>;
const Plus = () => <Icon><path d="M12 5v14M5 12h14" /></Icon>;
const Check = () => <Icon><path d="m5 12 4 4L19 6" /></Icon>;

export default function App() {
  const [evidenceHash, setEvidenceHash] = useState(window.location.hash);
  useEffect(() => {
    const update = () => setEvidenceHash(window.location.hash);
    window.addEventListener('hashchange', update);
    return () => window.removeEventListener('hashchange', update);
  }, []);
  const [items, setItems] = useState<WatchItem[]>([]);
  const [groups, setGroups] = useState<WatchGroup[]>([]);
  const [groupError, setGroupError] = useState('');
  const [groupReadError, setGroupReadError] = useState('');
  const [groupFilter, setGroupFilter] = useState('all');
  const [holdingOnly, setHoldingOnly] = useState(false);
  const [managementBusy, setManagementBusy] = useState('');
  const [metadataPending, setMetadataPending] = useState<{ key: string; group_id: string | null; is_holding: boolean } | null>(null);
  const [undo, setUndo] = useState<WatchUndo | null>(null);
  const [undoExpired, setUndoExpired] = useState(false);
  const [researchKey, setResearchKey] = useState<string | null>(null);
  const [anomalyStock, setAnomalyStock] = useState<WatchItem | null>(null);
  const mutation = useRef(false);
  const groupRequest = useRef<AbortController | null>(null);
  const pageView = evidenceHash === '#view=markets' ? 'markets' : evidenceHash === '#view=news' ? 'news' : evidenceHash === '#view=history' ? 'history' : 'watchlist';
  const [loaded, setLoaded] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [listError, setListError] = useState('');
  const [removeError, setRemoveError] = useState('');
  const [lastRead, setLastRead] = useState('');
  const [symbol, setSymbol] = useState('');
  const [candidate, setCandidate] = useState<Security | null>(null);
  const [looking, setLooking] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState('');
  const [notice, setNotice] = useState('');
  const [confirmKey, setConfirmKey] = useState<string | null>(null);
  const [removingKey, setRemovingKey] = useState<string | null>(null);
  const [showAccess, setShowAccess] = useState(false);
  const [quoteLoadingKey, setQuoteLoadingKey] = useState<string | null>(null);
  const [quoteErrors, setQuoteErrors] = useState<Record<string, string>>({});
  const [access, setAccess] = useState<AccessInfo | null>(null);
  const accessUrls = access?.mode === 'public' && access.public_url ? [access.public_url] : access?.lan_urls ?? [];
  const [dailyKey, setDailyKey] = useState<string | null>(null);
  const dailyTrigger = useRef<HTMLButtonElement | null>(null);
  const [showNotifications, setShowNotifications] = useState(false);
  const notificationTrigger = useRef<HTMLButtonElement>(null);
  const [showAnomalies, setShowAnomalies] = useState(false);
  const anomalyTrigger = useRef<HTMLButtonElement>(null);
  const [showBriefings, setShowBriefings] = useState(false);
  const briefingTrigger = useRef<HTMLButtonElement>(null);
  const listRequest = useRef<AbortController | null>(null);
  const lookupRequest = useRef<AbortController | null>(null);
  const lookupSequence = useRef(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const loadList = useCallback(async (quiet = false, afterMutation = false) => {
    if (mutation.current && !afterMutation) return;
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    if (!quiet) setRefreshing(true);
    try {
      const result = await api<{ items: WatchItem[]; total: number }>('/dashboard', {}, controller.signal);
      if (controller.signal.aborted) return;
      if (!Array.isArray(result.items)) throw new Error('列表数据格式不正确，请稍后重试。');
      setItems(result.items);
      setLoaded(true);
      setListError('');
      setLastRead(new Date().toISOString());
    } catch (error) {
      if (!controller.signal.aborted) setListError(messageOf(error));
    } finally {
      if (listRequest.current === controller) setRefreshing(false);
    }
  }, []);

  const loadGroups = useCallback(async (afterMutation = false) => {
    if (mutation.current && !afterMutation) return;
    groupRequest.current?.abort();
    const controller = new AbortController(); groupRequest.current = controller;
    try {
      const result = await api<{ items: WatchGroup[] }>('/watchlist/groups', {}, controller.signal);
      if (!Array.isArray(result.items)) throw new Error('分组读取未完成，请刷新列表重试。');
      if (!controller.signal.aborted) { setGroups(result.items); setGroupReadError(''); }
    } catch (error) { if (!controller.signal.aborted) setGroupReadError(messageOf(error)); }
  }, []);

  useEffect(() => {
    let stopped = false;
    let timer: number;
    async function poll() {
      if (!document.hidden) await Promise.all([loadList(true), loadGroups()]);
      if (!stopped) timer = window.setTimeout(poll, 5000);
    }
    const onVisible = () => { if (!document.hidden) void loadList(true); };
    void poll();
    document.addEventListener('visibilitychange', onVisible);
    const accessController = new AbortController();
    void api<AccessInfo>('/access', {}, accessController.signal)
      .then(result => setAccess(result)).catch(() => {});
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      listRequest.current?.abort();
      lookupRequest.current?.abort();
      groupRequest.current?.abort();
      accessController.abort();
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [loadList, loadGroups]);

  useEffect(() => {
    if (!undo) return;
    const update = () => setUndoExpired(Date.now() >= new Date(undo.expires_at).getTime());
    update(); const timer = window.setInterval(update, 1000);
    return () => window.clearInterval(timer);
  }, [undo]);

  const alreadyAdded = candidate && items.some(item => itemKey(item) === itemKey(candidate));
  const dailyItem = items.find(item => recordKey(item) === dailyKey);
  const visibleItems = items.filter(item => (!holdingOnly || item.is_holding) && (groupFilter === 'all' || groupFilter === 'ungrouped' ? groupFilter === 'all' || !item.group_id : item.group_id === groupFilter));
  const filtered = groupFilter !== 'all' || holdingOnly;

  function startMutation(key: string) {
    if (mutation.current) return false;
    mutation.current = true; setManagementBusy(key);
    listRequest.current?.abort(); groupRequest.current?.abort();
    return true;
  }
  async function finishMutation() {
    await Promise.all([loadList(true, true), loadGroups(true)]);
    mutation.current = false; setManagementBusy('');
  }

  async function manageGroup(kind: 'create' | 'rename' | 'delete', name: string, group?: WatchGroup) {
    if (kind !== 'delete' && !name) { setGroupError('请填写分组名称。'); return false; }
    if (!startMutation('groups')) return false;
    setGroupError(''); setNotice('');
    try {
      await api(`/watchlist/groups${group ? `/${encodeURIComponent(group.id)}` : ''}`, {
        method: kind === 'create' ? 'POST' : kind === 'rename' ? 'PATCH' : 'DELETE',
        ...(kind === 'delete' ? {} : { body: JSON.stringify({ name }) }),
      });
      if (kind === 'delete' && groupFilter === group?.id) setGroupFilter('ungrouped');
      setNotice(kind === 'delete' ? `已删除“${group?.name}”分组，股票仍保留在未分组中。` : `已保存“${name}”分组。`);
      return true;
    } catch (error) { setGroupError(messageOf(error)); return false; }
    finally { await finishMutation(); }
  }

  async function updateMetadata(item: WatchItem, updates: { group_id?: string | null; is_holding?: boolean }) {
    if (!startMutation(recordKey(item))) return;
    setRemoveError(''); setNotice('');
    setMetadataPending({ key: recordKey(item), group_id: item.group_id ?? null, is_holding: !!item.is_holding, ...updates });
    try {
      const result = await api<{ item: WatchItem }>(`/watchlist/${item.market}/${item.symbol}`, { method: 'PATCH', body: JSON.stringify({
        watchlist_created_at: item.created_at, expected_version: item.metadata_version ?? 0,
        group_id: item.group_id ?? null, is_holding: !!item.is_holding, ...updates,
      }) });
      setItems(previous => previous.map(row => recordKey(row) === recordKey(item) ? { ...row, ...result.item } : row));
      setNotice(`${item.name}的${'group_id' in updates ? '分组' : '持仓标记'}已保存。`);
    } catch (error) { setRemoveError(`${item.name}的修改未确认：${messageOf(error)} 请核对最新列表后再操作。`); }
    finally { await finishMutation(); setMetadataPending(null); }
  }

  async function moveItem(item: WatchItem, direction: -1 | 1) {
    const index = items.findIndex(row => recordKey(row) === recordKey(item));
    const nextIndex = index + direction;
    if (filtered || nextIndex < 0 || nextIndex >= items.length || !startMutation('reorder')) return;
    setRemoveError(''); setNotice('');
    const next = [...items]; [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
    try {
      const result = await api<{ items: WatchItem[]; total: number }>('/watchlist/reorder', { method: 'POST', body: JSON.stringify({ items: next.map(row => ({
        market: row.market, symbol: row.symbol, watchlist_created_at: row.created_at, metadata_version: row.metadata_version ?? 0,
      })) }) });
      setItems(previous => result.items.map(row => ({ ...previous.find(old => recordKey(old) === recordKey(row)), ...row })));
      setNotice(`已${direction < 0 ? '上移' : '下移'}${item.name}，顺序已保存。`);
    } catch (error) { setRemoveError(`顺序未保存：${messageOf(error)} 请核对最新顺序后重试。`); }
    finally { await finishMutation(); }
  }

  async function restoreItem() {
    if (!undo || undoExpired || !startMutation('undo')) return;
    setRemoveError('');
    try {
      const result = await api<{ item: WatchItem; restored: boolean; rule_restored_disabled: boolean }>(`/watchlist/undo/${encodeURIComponent(undo.token)}`, { method: 'POST' });
      setUndo(null); setGroupFilter('all'); setHoldingOnly(false);
      setNotice(`已恢复${result.item.name}及分组、持仓标记。${result.rule_restored_disabled ? '原有规则设置已恢复为关闭，需核对后重新启用。' : '历史记录保留，旧提醒不会补发。'}`);
    } catch (error) { setRemoveError(`撤销未确认：${messageOf(error)} 请核对最新列表。`); }
    finally { await finishMutation(); }
  }

  async function lookup(event: FormEvent) {
    event.preventDefault();
    setFormError('');
    setNotice('');
    setCandidate(null);
    const value = symbol.trim();
    if (!/^[0-9]{6}$/.test(value)) {
      setFormError('请输入 6 位股票代码，例如 600519。');
      inputRef.current?.focus();
      return;
    }
    const existing = items.find(item => item.symbol === value);
    if (existing) {
      setCandidate(existing);
      return;
    }
    lookupRequest.current?.abort();
    const controller = new AbortController();
    lookupRequest.current = controller;
    const sequence = ++lookupSequence.current;
    setLooking(true);
    try {
      const result = await api<Security>(`/securities/${value}`, {}, controller.signal);
      if (sequence === lookupSequence.current && !controller.signal.aborted) setCandidate(result);
    } catch (error) {
      if (!controller.signal.aborted && sequence === lookupSequence.current) setFormError(messageOf(error));
    } finally {
      if (sequence === lookupSequence.current) setLooking(false);
    }
  }

  async function add() {
    if (!candidate || saving || !startMutation('add')) return;
    setSaving(true);
    setFormError('');
    try {
      const result = await api<{ item: WatchItem; created: boolean }>('/watchlist', {
        method: 'POST', body: JSON.stringify({ symbol: candidate.symbol }),
      });
      setItems(previous => previous.some(item => itemKey(item) === itemKey(result.item))
        ? previous : [...previous, result.item].sort((left, right) => (left.display_order ?? 0) - (right.display_order ?? 0)));
      setNotice(result.created ? `${result.item.name}已加入自选。手机打开同一地址即可查看。` : `${result.item.name}已经在自选中，没有重复添加。`);
    } catch (error) {
      setFormError(messageOf(error));
    } finally {
      setSaving(false);
      await finishMutation();
    }
  }

  async function remove(item: WatchItem) {
    const key = itemKey(item);
    if (removingKey || !startMutation('remove')) return;
    setRemovingKey(key);
    setRemoveError('');
    setNotice('');
    try {
      const result = await api<{ removed: boolean; undo_token?: string; undo_expires_at?: string }>(`/watchlist/${item.market}/${item.symbol}?watchlist_created_at=${encodeURIComponent(item.created_at)}`, { method: 'DELETE' });
      setItems(previous => previous.filter(row => itemKey(row) !== key));
      setConfirmKey(null);
      setNotice(`已将${item.name}移出自选。`);
      if (result.undo_token && result.undo_expires_at) setUndo({ token: result.undo_token, expires_at: result.undo_expires_at, name: item.name });
    } catch (error) {
      setRemoveError(`${item.name}移除未确认：${messageOf(error)}`);
    } finally {
      setRemovingKey(null);
      await finishMutation();
    }
  }

  function focusAdd() {
    if (pageView !== 'watchlist') window.location.hash = '';
    window.setTimeout(() => {
      document.getElementById('add-stock')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      inputRef.current?.focus({ preventScroll: true });
    }, 0);
  }

  function openAlerts(item: WatchItem, trigger: HTMLButtonElement) {
    anomalyTrigger.current = trigger; setAnomalyStock(item); setShowAnomalies(true);
    window.setTimeout(() => { document.getElementById('anomaly-title')?.focus(); document.getElementById('anomaly-details')?.scrollIntoView({ block: 'start' }); }, 0);
  }

  async function refreshQuote(item: WatchItem) {
    if (quoteLoadingKey) return;
    // A removed and re-added stock is a different record, even with the same code.
    const key = recordKey(item);
    setQuoteLoadingKey(key);
    setQuoteErrors(previous => ({ ...previous, [key]: '' }));
    try {
      const result = await api<{ refresh_succeeded: boolean; quote: QuoteState }>(`/quotes/${item.market}/${item.symbol}/refresh`, { method: 'POST' });
      setItems(previous => previous.map(row => recordKey(row) === key ? { ...row, quote: result.quote } : row));
      // The server's quote state says whether data was accepted; HTTP 200 alone does not.
    } catch (error) {
      setQuoteErrors(previous => ({ ...previous, [key]: messageOf(error) }));
    } finally {
      setQuoteLoadingKey(null);
      await loadList(true);
    }
  }

  return <>
    <a className="skip-link" href="#main-content">跳到主要内容</a>
    <header className="site-header">
      <a className="brand" href={appBase} aria-label="AlphaProject 首页"><span className="brand-mark">α</span><span>AlphaProject <span className="brand-lab">统一版</span></span></a>
      <button className="button button-quiet" onClick={() => setShowAccess(value => !value)} aria-expanded={showAccess} aria-controls="phone-access">
        <Icon><rect x="7" y="2" width="10" height="20" rx="2" /><path d="M11 18h2" /></Icon><span>在手机打开</span>
      </button>
    </header>

    <main id="main-content" tabIndex={-1}>
      <section className="page-heading" aria-labelledby="page-title">
        <div><div className="eyebrow"><span className="small-line" />{pageView === 'watchlist' ? '个人关注列表' : pageView === 'markets' ? '观察市场全貌' : pageView === 'news' ? '阅读有来源的资料' : '保留过去的记录'}</div><h1 id="page-title">{pageView === 'watchlist' ? '共享自选' : pageView === 'markets' ? '综合行情' : pageView === 'news' ? '资讯与日历' : '旧版历史档案'}<span className="title-dot">。</span></h1><p>{pageView === 'watchlist' ? '从关注的股票出发，看行情、设提醒、查依据。' : '先核对来源与时点，再理解资料能说明什么。'}</p></div>
        <button className="button button-outline heading-add" onClick={focusAdd}><Plus />添加股票</button>
      </section>

      <nav className="main-navigation" aria-label="主要功能"><a href="#" aria-current={pageView === 'watchlist' ? 'page' : undefined}>自选股票<span>{items.length}</span></a><a href="#view=markets" aria-current={pageView === 'markets' ? 'page' : undefined}>综合行情</a><a href="#view=news" aria-current={pageView === 'news' ? 'page' : undefined}>资讯与日历</a><a href="#view=history" aria-current={pageView === 'history' ? 'page' : undefined}>旧版历史档案</a></nav>

      {showAccess && <section className="access-panel" id="phone-access" aria-labelledby="access-title">
        <div><h2 id="access-title">{access?.mode === 'public' ? '在不同设备打开同一份列表' : '让手机读取同一份列表'}</h2><p>{access?.mode === 'public' ? '在手机或电脑浏览器打开下面的地址，无需账号或密码。本机电脑关闭后仍可使用；所有访问者共用列表、通知设置与模型预算。' : access?.mode === 'lan' ? '手机与电脑连接同一 Wi-Fi，保持电脑上的服务运行，在手机浏览器输入下面的地址。' : '正在读取当前入口信息。'}</p></div>
        <div className="access-addresses">{accessUrls.length ? accessUrls.map(url => <label key={url}><span className="sr-only">手机访问地址</span><input name="access-url" readOnly value={url} onFocus={event => event.target.select()} /></label>) : <p>暂未读到可分享的地址，请刷新页面后再试。</p>}</div>
      </section>}

      {pageView === 'markets' && <ResearchPanel mode="markets" />}
      {pageView === 'news' && <ResearchPanel mode="news" />}
      {pageView === 'history' && <LegacyHistoryPanel />}
      {pageView === 'watchlist' && <>
      <div className="notification-entry"><button className="button button-outline" ref={notificationTrigger} aria-controls="notification-details" aria-expanded={showNotifications} onClick={() => setShowNotifications(value => !value)}>飞书通知<span aria-hidden="true">↗</span></button><button className="button button-outline" ref={anomalyTrigger} aria-controls="anomaly-details" aria-expanded={showAnomalies} onClick={() => { setAnomalyStock(null); setShowAnomalies(value => !value); }}>异动检测<span aria-hidden="true">↗</span></button><button className="button button-outline" ref={briefingTrigger} aria-controls="briefing-details" aria-expanded={showBriefings} onClick={() => setShowBriefings(value => !value)}>早盘简报<span aria-hidden="true">↗</span></button><span>管理提醒、通知与每日摘要</span></div>
      {showNotifications && <NotificationsPanel onClose={() => { setShowNotifications(false); notificationTrigger.current?.focus(); }} />}
      {evidenceHash && !['#', '#main-content'].includes(evidenceHash) && <EvidenceEntry hash={evidenceHash} onNotifications={() => {
        setShowNotifications(true);
        window.setTimeout(() => { document.getElementById('notification-title')?.focus(); document.getElementById('notification-details')?.scrollIntoView({ block: 'start' }); }, 0);
      }} />}
      {showAnomalies && <AnomaliesPanel stock={anomalyStock} onClearStock={() => setAnomalyStock(null)} onClose={() => { setShowAnomalies(false); anomalyTrigger.current?.focus(); }} onNotifications={() => {
        setShowNotifications(true);
        window.setTimeout(() => { document.getElementById('notification-title')?.focus(); document.getElementById('notification-details')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 0);
      }} />}
      {showBriefings && <BriefingsPanel onClose={() => { setShowBriefings(false); briefingTrigger.current?.focus(); }} onNotifications={() => {
        setShowNotifications(true);
        window.setTimeout(() => { document.getElementById('notification-title')?.focus(); document.getElementById('notification-details')?.scrollIntoView({ behavior: 'smooth', block: 'start' }); }, 0);
      }} />}

      <div className="workspace">
        <section className="list-panel panel" aria-labelledby="watchlist-title">
          <div className="panel-heading"><div><div className="section-label">WATCHLIST</div><h2 id="watchlist-title">关注的股票 <span className="count">{items.length}</span></h2></div><button className="button button-icon" aria-label="刷新列表" title="刷新列表" onClick={() => void loadList()} disabled={refreshing}><Refresh /></button></div>

          <div className="connection-line"><span className={`status-dot ${listError ? 'offline' : loaded ? 'online' : ''}`} /><span>{listError ? '暂未同步 · 显示上次读取结果' : lastRead ? `最近同步 ${formatTime(lastRead)}` : '正在连接共享列表…'}</span></div>
          <p className="quotes-intro">交易时段由后台持续获取报价，关闭网页也会继续。也可手动更新；行情状态和暂停原因显示在每只股票下方。</p>
          <div className="watchlist-filters"><label>查看分组<select name="watchlist-group-filter" value={groupFilter} onChange={event => setGroupFilter(event.target.value)}><option value="all">全部分组</option><option value="ungrouped">未分组</option>{groups.map(group => <option key={group.id} value={group.id}>{group.name}</option>)}</select></label><label className="holding-filter"><input type="checkbox" checked={holdingOnly} onChange={event => setHoldingOnly(event.target.checked)} />只看持仓标记</label>{filtered && <button className="button button-quiet" onClick={() => { setGroupFilter('all'); setHoldingOnly(false); }}>显示全部</button>}</div>
          {filtered && <p className="filter-note">当前显示 {visibleItems.length} / {items.length} 只；调整顺序前先选择“显示全部”。</p>}
          {listError && <div className="alert alert-error" role="alert"><span>{listError}</span><button className="text-button" onClick={() => void loadList()}>重新读取</button></div>}
          {removeError && <div className="alert alert-error" role="alert"><span>{removeError}</span><button className="text-button" onClick={() => setRemoveError('')}>关闭提示</button></div>}
          <div className="notice" role="status" aria-live="polite">{notice}</div>
          {undo && <div className="undo-notice" role="status"><div><strong>已移出{undo.name}</strong><p>{undoExpired ? '撤销时限已过。需要时可重新添加，历史记录仍保留。' : `可在 ${knownTime(undo.expires_at)} 前撤销。恢复后提醒规则保持关闭。`}</p></div><div><button className="button button-outline" disabled={undoExpired || !!managementBusy} onClick={() => void restoreItem()}>{managementBusy === 'undo' ? '正在恢复…' : '撤销移除'}</button><button className="button button-quiet" disabled={managementBusy === 'undo'} onClick={() => setUndo(null)}>关闭撤销提示</button></div></div>}

          {!loaded && !listError ? <div className="empty-state"><div className="loading-line" /><p>正在读取已保存的自选…</p></div> : items.length === 0 ? <div className="empty-state">
            <div className="empty-symbol" aria-hidden="true"><Icon><path d="M4 18h16M6 14l4-4 4 2 4-6" /><path d="M15 6h3v3" /></Icon></div>
            <h3>{loaded ? '从第一只关注的股票开始' : '暂时无法读取列表'}</h3><p>{loaded ? '输入代码、核对名称，再加入自选。\n添加后，电脑和手机都会看到它。' : '请检查网络连接，再重新读取；仍失败时查看服务状态。'}</p>
            {loaded && <button className="button button-outline" onClick={focusAdd}><Plus />添加第一只股票</button>}
          </div> : !visibleItems.length ? <div className="empty-state"><h3>这个筛选下还没有股票</h3><p>可切换分组，或取消“只看持仓标记”。</p><button className="button button-outline" onClick={() => { setGroupFilter('all'); setHoldingOnly(false); }}>显示全部自选</button></div> : <div className="stock-list" role="list" aria-label="已保存的自选股票">
            {visibleItems.map(item => <article className="stock-row" role="listitem" key={recordKey(item)}>
              <div className={`market-tile ${item.market}`} aria-hidden="true">{item.market === 'sh' ? '沪' : '深'}</div>
              <div className="stock-main"><h3>{item.name}{item.is_holding && <span className="holding-badge">持仓</span>}</h3><div className="stock-meta"><span className="stock-code">{item.symbol}</span><span className="meta-separator">·</span><span>{marketName(item.market)}</span></div></div>
              <div className="saved-time"><span>添加于</span><time dateTime={item.created_at}>{formatTime(item.created_at)}</time></div>
              <div className="row-actions">{confirmKey === itemKey(item) ? <div className="confirm-actions"><span className="confirm-label">移出自选？</span><button className="button button-danger-small" onClick={() => void remove(item)} disabled={!!managementBusy} aria-label={`确认移除${item.name}`}>{removingKey === itemKey(item) ? '移除中…' : '确认移除'}</button><button className="button button-quiet" onClick={() => setConfirmKey(null)} disabled={!!managementBusy}>取消</button></div> : <button className="button button-quiet remove-button" disabled={!!managementBusy} onClick={() => setConfirmKey(itemKey(item))} aria-label={`移除${item.name}`}>移除</button>}</div>
              <div className="stock-management"><label>分组<select aria-label={`${item.name}分组`} value={(metadataPending?.key === recordKey(item) ? metadataPending.group_id : item.group_id) || ''} disabled={!!managementBusy || !!groupReadError} onChange={event => void updateMetadata(item, { group_id: event.target.value || null })}><option value="">未分组</option>{groups.map(group => <option value={group.id} key={group.id}>{group.name}</option>)}</select></label><label className="holding-toggle"><input type="checkbox" checked={metadataPending?.key === recordKey(item) ? metadataPending.is_holding : !!item.is_holding} disabled={!!managementBusy} onChange={event => void updateMetadata(item, { is_holding: event.target.checked })} aria-label={`标记${item.name}为持仓`} />{metadataPending?.key === recordKey(item) ? '正在保存标记…' : '持仓标记'}</label><div className="stock-order"><button className="button button-quiet" aria-label={`上移${item.name}`} disabled={!!managementBusy || filtered || items.findIndex(row => recordKey(row) === recordKey(item)) === 0} onClick={() => void moveItem(item, -1)}>上移 ↑</button><button className="button button-quiet" aria-label={`下移${item.name}`} disabled={!!managementBusy || filtered || items.findIndex(row => recordKey(row) === recordKey(item)) === items.length - 1} onClick={() => void moveItem(item, 1)}>下移 ↓</button></div></div>
              {groupReadError && <p className="stock-management-error">分组暂未同步，请刷新列表后再修改分组。{groupReadError}</p>}
              <QuotePanel name={item.name} state={item.quote} loading={quoteLoadingKey === recordKey(item)}
                disabled={quoteLoadingKey !== null || removingKey !== null} actionError={quoteErrors[recordKey(item)]}
                onRefresh={() => void refreshQuote(item)} onDismiss={() => setQuoteErrors(previous => ({ ...previous, [recordKey(item)]: '' }))} />
              <div className="daily-entry"><button className="button button-outline" aria-label={`查看${item.name}日线与成交量`} aria-controls="daily-details" aria-expanded={dailyKey === recordKey(item)} onClick={event => {
                dailyTrigger.current = event.currentTarget;
                setDailyKey(dailyKey === recordKey(item) ? null : recordKey(item));
              }}>日线与成交量<span aria-hidden="true">↗</span></button><button className="button button-outline" onClick={event => openAlerts(item, event.currentTarget)} aria-label={`设置${item.name}提醒`}>提醒设置<span aria-hidden="true">↗</span></button><a className="button button-outline" href={`${appBase}#stock=${item.market}${item.symbol}`} aria-label={`解释${item.name}的已保存报价`}>解释这份报价<span aria-hidden="true">↗</span></a><button className="button button-outline" aria-expanded={researchKey === recordKey(item)} onClick={() => setResearchKey(researchKey === recordKey(item) ? null : recordKey(item))} aria-label={`查看${item.name}资讯与公告`}>资讯与公告<span aria-hidden="true">↗</span></button></div>
              {dailyItem && dailyKey === recordKey(item) && <DailyPanel key={recordKey(dailyItem)} item={dailyItem} onClose={() => { setDailyKey(null); dailyTrigger.current?.focus(); }} />}
              {researchKey === recordKey(item) && <ResearchPanel key={researchKey} mode="stock" market={item.market} symbol={item.symbol} name={item.name} onClose={() => setResearchKey(null)} />}
            </article>)}
          </div>}
          <div className="list-footer"><Icon><path d="m5 12 4 4L19 6" /></Icon><span>{access?.mode === 'public' ? '列表保存在云端，关闭电脑后仍可从其他设备读取。' : '列表保存在服务数据库，刷新或重启程序后仍会保留。'}</span></div>
        </section>

        <aside className="add-panel panel" id="add-stock" aria-labelledby="add-title">
          <div className="section-label">ADD TO WATCHLIST</div><h2 id="add-title">添加股票</h2><p className="form-intro">先找到它，再确认加入。</p>
          <form onSubmit={event => void lookup(event)} noValidate>
            <label className="field-label" htmlFor="symbol">股票代码</label><div className="input-wrap"><input id="symbol" ref={inputRef} value={symbol} onChange={event => {
              setSymbol(event.target.value.replace(/\s/g, '')); setCandidate(null); setFormError(''); setNotice('');
              lookupRequest.current?.abort(); lookupSequence.current += 1; setLooking(false);
            }} inputMode="numeric" autoComplete="off" maxLength={6} placeholder="例如 600519" disabled={saving} aria-describedby="symbol-hint symbol-error" aria-invalid={!!formError} /><span className="input-hint">6 位</span></div>
            <p className="field-hint" id="symbol-hint">输入沪深 A 股代码，核对名称后添加。</p>
            <button className="button button-primary full-width" type="submit" disabled={looking || saving}>{looking ? <><span className="spinner" />正在核对…</> : <><Icon><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></Icon>查找股票</>}</button>
          </form>
          <div id="symbol-error" className={formError ? 'alert alert-error form-error' : ''} role="alert">{formError}</div>
          {candidate && <section className="candidate" aria-label="待确认的股票"><div className="candidate-label"><span className="status-dot online" />{alreadyAdded ? '已在自选中' : '已核对股票身份'}</div><h3>{candidate.name}</h3><p className="candidate-code">{candidate.symbol}<span>·</span>{marketName(candidate.market)}</p><button className="button button-primary full-width" onClick={() => void add()} disabled={saving || !!alreadyAdded}>{alreadyAdded ? <><Check />已在自选中</> : saving ? <><span className="spinner" />正在保存…</> : <><Plus />加入自选</>}</button><p className="source-note">名称来源：{candidate.source}<br />核对时间：{formatTime(candidate.verified_at, true)}</p></section>}
          <WatchGroups groups={groups} busy={!!managementBusy} error={groupError || groupReadError} onCreate={name => manageGroup('create', name)} onRename={(group, name) => manageGroup('rename', name, group)} onDelete={group => manageGroup('delete', '', group)} />
          <div className="form-note"><span>02</span><p>刚收到，不等于刚产生。<br />查看价格时，也核对来源时间。</p></div>
        </aside>
      </div>
      </>}
      <footer className="page-footer"><span>AlphaProject{access && ` · ${access.mode === 'public' ? '云端' : '本机'} ${access.release?.app_version ?? ''}`}</span><span>{access?.mode === 'public' ? <a href={`${appBase}release.json`} target="_blank" rel="noreferrer">查看部署版本</a> : '一份列表 · 电脑与手机共享'}</span></footer>
    </main>
  </>;
}
