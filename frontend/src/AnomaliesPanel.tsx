import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiFailure, formatTime, messageOf, newRequestId, recordKey } from './api';
import type { AnomalyRule, AnomalyState, WatchItem } from './api';
import AnomalyEventCard from './AnomalyEventCard';
import ExtendedRuleEditor from './ExtendedRuleEditor';

const stepLabels = { below: '未触发', triggered: '触发并保存', duplicate: '不重复提醒', blocked: '数据不合格', cancelled: '检查已停止' };
const activityClock = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});
const activityTime = (value: string) => activityClock.format(new Date(value));

export function RuleEditor({ item, active, checksRunning, onRefresh }: {
  item: AnomalyState['items'][number]; active: boolean; checksRunning: boolean; onRefresh: () => Promise<void>;
}) {
  const [up, setUp] = useState(item.rule?.up_threshold || '3');
  const [down, setDown] = useState(item.rule?.down_threshold || '3');
  const [enabled, setEnabled] = useState(!!item.rule?.enabled);
  const [version, setVersion] = useState(item.rule?.version || 0);
  const [savedRule, setSavedRule] = useState(item.rule);
  const [dirty, setDirty] = useState(false);
  const [notify, setNotify] = useState(true);
  const [busy, setBusy] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const request = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const pendingDemo = useRef<{ request_id: string; market: string; symbol: string; watchlist_created_at: string; expected_version: number; direction: 'up' | 'down'; notify: boolean } | null>(null);
  const changedElsewhere = (item.rule?.version || 0) > version;
  const currentRule = item.rule && item.rule.version >= version ? item.rule : savedRule;
  const eligibility = item.quote?.eligibility;
  const checking = !!currentRule?.enabled && !!eligibility?.eligible && checksRunning;
  const id = item.market + item.symbol;

  function restore() {
    setUp(item.rule?.up_threshold || '3'); setDown(item.rule?.down_threshold || '3');
    setEnabled(!!item.rule?.enabled); setVersion(item.rule?.version || 0); setSavedRule(item.rule); setDirty(false); setError(''); setNotice('');
  }
  useEffect(() => {
    // A successful save may return before the next read; an older cached prop must
    // not roll the just-saved values/version back when the read fails or is slow.
    if (!dirty && (item.rule?.version || 0) >= version) {
      setUp(item.rule?.up_threshold || '3'); setDown(item.rule?.down_threshold || '3');
      setEnabled(!!item.rule?.enabled); setVersion(item.rule?.version || 0);
    }
  }, [item.rule?.version, item.rule?.up_threshold, item.rule?.down_threshold, item.rule?.enabled, dirty, version]);
  useEffect(() => { alive.current = true; return () => { alive.current = false; request.current?.abort(); }; }, []);

  async function act(kind: 'save' | 'up' | 'down') {
    if (busy) return;
    setBusy(kind); setError(''); setNotice('');
    const controller = new AbortController(); request.current = controller;
    try {
      if (kind === 'save') {
        const result = await api<{ rule: AnomalyRule }>(`/anomalies/rules/${item.market}/${item.symbol}`, { method: 'POST', body: JSON.stringify({
          watchlist_created_at: item.created_at, up_threshold: up.trim(), down_threshold: down.trim(), enabled, expected_version: version,
        }) }, controller.signal);
        if (alive.current) {
          setUp(result.rule.up_threshold); setDown(result.rule.down_threshold); setEnabled(!!result.rule.enabled);
          setVersion(result.rule.version); setSavedRule(result.rule); setDirty(false);
          setNotice(result.rule.enabled ? '规则已保存并开启。数据条件满足时，后台会自动检查。' : '规则已保存，真实检测保持关闭。');
        }
      } else {
        const body = pendingDemo.current || { request_id: newRequestId(), market: item.market, symbol: item.symbol,
          watchlist_created_at: item.created_at, expected_version: version, direction: kind, notify };
        pendingDemo.current = body;
        await api('/anomalies/demo', { method: 'POST', body: JSON.stringify(body) }, controller.signal);
        pendingDemo.current = null;
        if (alive.current) setNotice('演练已交给后台，下面会依次显示三个检查结果。');
      }
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status >= 400 && failure.status < 500) pendingDemo.current = null;
      if (!controller.signal.aborted && alive.current) setError(messageOf(failure));
    } finally {
      if (alive.current) { setBusy(''); await onRefresh(); }
    }
  }

  return <article className="anomaly-rule" aria-label={`${item.name}异动规则`}>
    <div className="anomaly-rule-title"><h4>{item.name} <span>{item.symbol}</span></h4><span className="notification-badge">{currentRule ? `规则 v${currentRule.version}` : '尚未保存规则'}</span></div>
    <div className="anomaly-fields">
      <label htmlFor={`up-${id}`}>上涨达到（%）<input id={`up-${id}`} inputMode="decimal" value={up} maxLength={9} onChange={e => { setUp(e.target.value); setDirty(true); setNotice(''); }} disabled={!!busy} /></label>
      <label htmlFor={`down-${id}`}>下跌达到（%）<input id={`down-${id}`} inputMode="decimal" value={down} maxLength={9} onChange={e => { setDown(e.target.value); setDirty(true); setNotice(''); }} disabled={!!busy} /></label>
    </div>
    <p className="notification-help">都填写正数。下跌填 3，表示涨跌幅达到 −3%。同日同方向最多记录一次。</p>
    <label className="anomaly-checkbox"><input type="checkbox" checked={enabled} onChange={e => { setEnabled(e.target.checked); setDirty(true); setNotice(''); }} disabled={!!busy} /><span>启用真实行情规则<span>勾选后需保存。仅在交易时段、报价新鲜且成交活动通过检查时判断；演练无需开启。</span></span></label>
    {changedElsewhere && dirty && <p className="notification-failure">规则已从其他操作更新。你的草稿还在，请先<button className="text-button" onClick={restore}>重新读取规则</button>。</p>}
    <div className="notification-actions"><button className="button button-outline" onClick={() => void act('save')} disabled={!!busy || (changedElsewhere && dirty)}>{busy === 'save' ? '正在保存…' : '保存规则'}</button>{dirty && <span className="notification-help">有尚未保存的修改</span>}</div>
    <div className={`anomaly-rule-status ${checking ? 'is-ready' : 'is-waiting'}`}>
      <strong>{!currentRule ? '保存后可开启真实检测' : !currentRule.enabled ? '真实规则已关闭' : !checksRunning ? '规则已开启，检测服务待恢复' : checking ? '规则已开启，后台自动检查' : '规则已开启，等待数据条件'}</strong>
      <dl>
        <div><dt>当前数据</dt><dd>{eligibility?.reason || '正在确认这只股票的数据条件。'}</dd></div>
        <div><dt>最近检查</dt><dd>{currentRule?.last_checked_at ? <time dateTime={currentRule.last_checked_at}>{activityTime(currentRule.last_checked_at)}</time> : '尚未检查'}</dd></div>
        <div><dt>最近判断</dt><dd>{currentRule?.last_result || (currentRule?.enabled ? '等待后台首次检查。' : '开启并保存后开始检查。')}</dd></div>
      </dl>
    </div>
    <details className="anomaly-demo-controls">
      <summary>用合成价格演练</summary><p>假设昨收 100 元，后台依次检查“差一点达到 → 刚好达到 → 继续超过”。演练不会修改真实报价。</p>
      <label className="anomaly-checkbox"><input type="checkbox" checked={notify} onChange={e => setNotify(e.target.checked)} disabled={!!busy} /><span>触发后向已配置的飞书群发送演练提醒<span>消息会标注“合成演练 · 非真实行情”。取消勾选则只在网页演练。</span></span></label>
      <div className="notification-actions"><button className="button button-primary" onClick={() => void act('up')} disabled={!!busy || active || dirty || !currentRule || changedElsewhere}>{busy === 'up' ? '提交中…' : pendingDemo.current ? '核对上次演练请求' : '开始上涨演练'}</button><button className="button button-outline" onClick={() => void act('down')} disabled={!!busy || active || dirty || !currentRule || changedElsewhere || !!pendingDemo.current}>{busy === 'down' ? '提交中…' : '开始下跌演练'}</button></div>
      {!currentRule && <p>先点击“保存规则”，再开始演练。</p>}
    </details>
    {error && <div className="alert alert-error" role="alert">{error}<button className="text-button" onClick={() => setError('')}>关闭提示</button></div>}
    {notice && <p className="anomaly-notice" role="status">{notice}</p>}
    <ExtendedRuleEditor item={item} onRefresh={onRefresh} />
  </article>;
}

export function CollectorStatus({ view }: { view: AnomalyState }) {
  const collector = view.collector;
  return <section className={`anomaly-live-state ${view.live_ready && view.runtime.running && collector?.running ? 'is-ready' : ''}`} aria-label="采集与检测状态">
    <div className="anomaly-runtime-grid">
      <div><span className="anomaly-runtime-label">行情采集</span><strong>{!collector ? '正在确认采集状态' : !collector.running ? '采集服务尚未运行' : collector.label}</strong>
        <p>{collector?.last_cycle_at ? `最近一轮 ${activityTime(collector.last_cycle_at)}` : '尚无采集记录'}{collector?.next_cycle_at ? ` · 下轮预计 ${activityTime(collector.next_cycle_at)}` : ''}</p>
        {!!collector?.attempted && <p>最近一轮获取 {collector.attempted} 只，成功 {collector.succeeded} 只{collector.failed > 0 ? `，${collector.failed} 只未更新，保留上次报价` : ''}。</p>}
      </div>
      <div><span className="anomaly-runtime-label">真实规则检查</span><strong>{!view.runtime.running ? '检测服务尚未运行' : view.live_ready ? '正在检查已开启的规则' : '等待判断条件'}</strong>
        <p>{view.live_pause_reason || (view.live_ready ? '每只股票的数据条件与最近判断，见下方规则。' : '等待交易时段和可用的新报价。')}</p>
        <p>{view.runtime.heartbeat_at ? `最近运行 ${activityTime(view.runtime.heartbeat_at)}` : '尚无运行记录'}</p>
      </div>
    </div>
    {collector?.last_error && <p className="notification-failure">采集未全部完成，未更新的股票保留上次报价。{collector.last_error}</p>}
    {view.runtime.last_error && <p className="notification-failure">规则检查遇到问题。{view.runtime.last_error}</p>}
    <p>关闭网页后，后台仍会继续采集和检查；服务需保持运行。每只股票是否可判断，以下方状态为准。</p>
    <details className="quote-evidence anomaly-runtime-help"><summary>查看采集与判断说明</summary>
      <p>只在已核验交易日的 09:30–11:30、13:00–14:57（北京时间）采集并判断。午休、集合竞价和闭市时暂停。</p>
      <p>行情约每 {collector?.interval_seconds ?? 60} 秒采集一轮，规则每 {view.check_interval_seconds} 秒检查已保存报价。股票较多时会轮流采集。</p>
      <p>同一只股票需要两份新报价，来源时间与成交量都增加，才允许判断。成交活动不足不代表已确认停牌。</p>
      <p>当前把超过 {collector?.freshness_seconds ?? 120} 秒的来源报价视为过旧。这是初始检查标准，不保证报价与交易所同步。</p>
    </details>
  </section>;
}

export function AnomalyHistory({ events, runs, onNotifications }: Pick<AnomalyState, 'events' | 'runs'> & { onNotifications: () => void }) {
  const liveEvents = events.filter(event => event.mode === 'live');
  const demoEvents = events.filter(event => event.mode === 'demo');
  return <>
    <section aria-labelledby="live-events-title">
      <div className="anomaly-section-title"><span>02</span><h3 id="live-events-title">真实异动记录</h3></div>
      {!liveEvents.length ? <p className="notification-help">尚未记录真实异动。规则开启且数据通过检查后，达到阈值才会记录。</p> : <div className="anomaly-events">{liveEvents.map(event => <AnomalyEventCard key={event.id} event={event} onNotifications={onNotifications} />)}</div>}
    </section>
    <section aria-labelledby="demo-history-title">
      <div className="anomaly-section-title"><span>03</span><h3 id="demo-history-title">合成演练记录</h3></div>
      <p className="notification-help">这里使用假设价格，不代表发生了真实异动。</p>
      {!runs.length ? <p className="notification-help">在任一规则中展开“用合成价格演练”，可试一次上涨或下跌。</p> : runs.map(run => <article className="anomaly-run" key={run.id}>
        <div className="anomaly-rule-title"><strong>{run.symbol} · {run.direction === 'up' ? '上涨' : '下跌'} {run.threshold}% 演练</strong><span className="notification-badge">{run.status === 'completed' ? '三步已完成' : run.status === 'cancelled' ? '已停止' : '后台演练中'}</span></div>
        <p className="notification-help">合成价格 · 非真实行情 · {formatTime(run.created_at, true)} · {run.notify ? '首次触发时提交飞书提醒' : '本次只在网页演练'}</p>
        <ol className="anomaly-steps">{[0, 1, 2].map(index => { const step = run.results[index]; return <li key={index} className={step?.status || 'waiting'}><span className="anomaly-step-index">0{index + 1}</span>{step ? <><strong>{step.price} <small>元</small></strong><span>相对昨收 {step.change_pct}%</span><b>{stepLabels[step.status]}</b><p>{step.reason}</p></> : <p>等待后台检查</p>}</li>; })}</ol>
        {run.error_message && <p className="notification-failure">{run.error_message}</p>}
      </article>)}
      {!!demoEvents.length && <><h4 className="anomaly-history-subtitle">演练触发依据</h4><div className="anomaly-events">{demoEvents.map(event => <AnomalyEventCard key={event.id} event={event} onNotifications={onNotifications} />)}</div></>}
    </section>
  </>;
}

export default function AnomaliesPanel({ onClose, onNotifications, stock, onClearStock }: {
  onClose: () => void; onNotifications: () => void; stock?: WatchItem | null; onClearStock?: () => void;
}) {
  const [view, setView] = useState<AnomalyState | null>(null);
  const [error, setError] = useState('');
  const heading = useRef<HTMLHeadingElement>(null);
  const focused = useRef(false);
  const request = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    try {
      const result = await api<AnomalyState>('/anomalies', {}, controller.signal);
      if (!controller.signal.aborted && alive.current) { setView(result); setError(''); }
    } catch (failure) {
      if (!controller.signal.aborted && alive.current) setError(messageOf(failure));
    }
  }, []);
  useEffect(() => {
    alive.current = true;
    let stopped = false, timer: number;
    async function poll() { if (!document.hidden) await load(); if (!stopped) timer = window.setTimeout(poll, 2500); }
    if (!focused.current) { heading.current?.focus({ preventScroll: true }); focused.current = true; }
    void poll();
    return () => { alive.current = false; stopped = true; window.clearTimeout(timer); request.current?.abort(); };
  }, [load]);
  const active = !!view?.runs.some(run => run.status === 'pending' || run.status === 'running');
  const visibleItems = stock ? view?.items.filter(item => recordKey(item) === recordKey(stock)) || [] : view?.items || [];
  const events = stock ? view?.events.filter(event => event.market === stock.market && event.symbol === stock.symbol) || [] : view?.events || [];
  const runs = stock ? view?.runs.filter(run => run.market === stock.market && run.symbol === stock.symbol) || [] : view?.runs || [];
  return <section id="anomaly-details" className="panel anomaly-panel" aria-labelledby="anomaly-title">
    <div className="notification-heading"><div><div className="section-label">PRICE ALERTS</div><h2 id="anomaly-title" tabIndex={-1} ref={heading}>异动检测</h2><p>替你检查价格变化，达到自己设定的条件就留下依据。</p></div><button className="button button-quiet" onClick={onClose}>收起异动检测</button></div>
    {stock && <div className="stock-alert-filter"><strong>当前查看：{stock.name} · {stock.symbol}</strong><button className="button button-outline" onClick={onClearStock}>查看全部股票规则</button></div>}
    <div className="anomaly-explainer"><strong>涨跌幅 =（现价 − 昨收）÷ 昨收 × 100%</strong><p>昨收 100 元，上涨阈值 3%：102.99 元未触发，103 元触发，103.20 元不重复。规则判断只说明“达到条件”，不说明涨跌原因。</p></div>
    {error && <div className="alert alert-error" role="status">{view ? '读取失败，下面保留上次结果。' : '暂时未能读取规则与事件。'}{error}<button className="text-button" onClick={() => void load()}>重新读取</button></div>}
    {!view ? <p className="notification-help">正在读取规则与事件…</p> : <>
      <CollectorStatus view={view} />
      <div className="anomaly-section-title"><span>01</span><h3>设置真实规则</h3></div>
      {!visibleItems.length ? <p className="notification-help">{stock ? '这只股票的自选记录已变化。请回到列表，重新选择提醒设置。' : '先在自选列表添加一只股票，再为它设置规则。'}</p> : visibleItems.map(item => <RuleEditor key={recordKey(item)} item={item} active={active || !!error} checksRunning={view.runtime.running} onRefresh={load} />)}
      <AnomalyHistory events={events} runs={runs} onNotifications={onNotifications} />
    </>}
  </section>;
}
