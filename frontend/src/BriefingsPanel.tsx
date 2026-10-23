import { useCallback, useEffect, useRef, useState } from 'react';
import { api, ApiFailure, formatTime, knownTime, messageOf, newRequestId } from './api';
import type { BriefingReport, BriefingState, NotificationRecord } from './api';
import { AnalysisTraceView, ResearchEvidenceList, ResearchSectionView } from './ResearchPanel';

const reportLabels: Record<BriefingReport['status'], string> = {
  pending: '已排队', running: '正在整理数据', succeeded: '完整摘要已保存', partial: '摘要已保存 · 有缺失项',
  failed: '本次未形成摘要', interrupted: '生成已中断', empty: '自选清单为空',
};
const scheduleLabels: Record<string, string> = {
  disabled: '自动生成已关闭', waiting_time: '等待今天的约定时间', due: '已到约定时间，等待后台领取',
  waiting: '后台已领取，正在等待摘要', completed: '定时生成已完成', missed: '今天已错过自动生成时间',
  closed: '今天休市', calendar_unknown: '日历尚未核验', cancelled: '今天的任务已取消',
  expired: '已超过今天的发送时段', failed: '今天的定时任务未完成', empty: '自选为空，未发送', interrupted: '今天的定时任务已中断',
};
const deliveryLabels: Record<NotificationRecord['status'], string> = {
  pending: '等待发送', checking: '正在核对接收群', sending: '发送中', accepted: '飞书已接收',
  failed: '发送失败', unknown: '发送结果待核对', cancelled: '未发送 · 已取消', expired: '未发送 · 已过期',
};
type GenerateRequest = { request_id: string; report_date: string; retry_of: string | null };
const storageKey = 'alpha-briefing-pending-request';
const READY = ['succeeded', 'partial'];

export default function BriefingsPanel({ onClose, onNotifications }: { onClose: () => void; onNotifications: () => void }) {
  const [view, setView] = useState<BriefingState | null>(null);
  const [selectedId, setSelectedId] = useState('');
  const [enabled, setEnabled] = useState(false);
  const [sendTime, setSendTime] = useState<'08:45' | '09:00'>('08:45');
  const [version, setVersion] = useState(-1);
  const [dirty, setDirty] = useState(false);
  const [loadError, setLoadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [uncertain, setUncertain] = useState(false);
  const alive = useRef(true);
  const heading = useRef<HTMLHeadingElement>(null);
  const readRequest = useRef<AbortController | null>(null);
  const actionRequest = useRef<AbortController | null>(null);
  const pending = useRef<GenerateRequest | null>(null);
  const remember = useCallback((value: GenerateRequest | null) => {
    pending.current = value;
    if (alive.current) setUncertain(!!value);
    try { if (value) sessionStorage.setItem(storageKey, JSON.stringify(value)); else sessionStorage.removeItem(storageKey); } catch { /* The mounted page retains the request. */ }
  }, []);
  const load = useCallback(async () => {
    readRequest.current?.abort();
    const controller = new AbortController(); readRequest.current = controller;
    try {
      const next = await api<BriefingState>('/briefings', {}, controller.signal);
      if (alive.current && !controller.signal.aborted) {
        setView(next); setLoadError('');
        const found = pending.current && next.reports.find(row => row.id === pending.current?.request_id);
        if (found) { setSelectedId(found.id); remember(null); }
      }
    } catch (failure) { if (alive.current && !controller.signal.aborted) setLoadError(messageOf(failure)); }
  }, [remember]);
  useEffect(() => {
    alive.current = true;
    try {
      const saved = JSON.parse(sessionStorage.getItem(storageKey) || 'null');
      if (saved && typeof saved.request_id === 'string' && typeof saved.report_date === 'string') remember(saved);
    } catch { /* Ignore malformed browser hints. */ }
    let stopped = false, timer: number;
    async function poll() { if (!document.hidden) await load(); if (!stopped) timer = window.setTimeout(poll, 3000); }
    void poll(); heading.current?.focus({ preventScroll: true });
    return () => { alive.current = false; stopped = true; window.clearTimeout(timer); readRequest.current?.abort(); actionRequest.current?.abort(); };
  }, [load, remember]);
  useEffect(() => {
    if (view && !dirty && view.settings.version >= version) {
      setEnabled(view.settings.enabled); setSendTime(view.settings.send_time); setVersion(view.settings.version);
    }
  }, [view, dirty, version]);

  const todayRecords = view?.reports.filter(row => row.report_date === view.today) || [];
  const reusable = todayRecords.find(row => READY.includes(row.status));
  const latest = todayRecords[0];
  const selected = view?.reports.find(row => row.id === selectedId) || latest || view?.reports[0];
  const changedElsewhere = !!view && version !== view.settings.version;

  function restoreSettings() {
    if (!view) return;
    setEnabled(view.settings.enabled); setSendTime(view.settings.send_time); setVersion(view.settings.version); setDirty(false); setActionError('');
  }
  async function saveSettings() {
    if (!view || busy) return;
    setBusy('save'); setActionError(''); setNotice('');
    // Prevent an older in-flight read from replacing a just-saved settings version.
    readRequest.current?.abort();
    const controller = new AbortController(); actionRequest.current = controller;
    try {
      const next = await api<BriefingState>('/briefings/settings', { method: 'PUT', body: JSON.stringify({ enabled, send_time: sendTime, expected_version: version }) }, controller.signal);
      if (alive.current) {
        setView(next); setEnabled(next.settings.enabled); setSendTime(next.settings.send_time); setVersion(next.settings.version); setDirty(false);
        setNotice(next.settings.enabled ? `已保存：交易日北京时间 ${next.settings.send_time} 自动生成，按飞书开关发送。` : '已关闭自动生成，尚未提交的简报消息已取消。');
      }
    } catch (failure) { if (alive.current && !controller.signal.aborted) setActionError(messageOf(failure)); }
    finally { if (alive.current) { setBusy(''); await load(); } }
  }
  async function generate() {
    if (!view || busy || view.active) return;
    if (reusable && !pending.current) { setSelectedId(reusable.id); setNotice('已打开今天保存的摘要，没有重新取数，也没有发送消息。'); return; }
    setBusy('generate'); setActionError(''); setNotice('');
    const controller = new AbortController(); actionRequest.current = controller;
    const body = pending.current || { request_id: newRequestId(), report_date: view.today,
      retry_of: latest && ['failed', 'interrupted'].includes(latest.status) ? latest.id : null };
    remember(body);
    try {
      const result = await api<{ record: BriefingReport; reused: boolean }>('/briefings', { method: 'POST', body: JSON.stringify(body) }, controller.signal);
      remember(null);
      if (alive.current) { setSelectedId(result.record.id); setNotice(result.reused ? '已读取当天已有记录，没有重复生成。' : '已交给后台整理，关闭页面后仍会继续。手动生成只保存到网页。'); }
    } catch (failure) {
      if (failure instanceof ApiFailure && failure.status >= 400 && failure.status < 500) remember(null);
      if (alive.current && !controller.signal.aborted) setActionError(messageOf(failure));
    } finally { if (alive.current) { setBusy(''); await load(); } }
  }

  return <section className="panel notification-panel briefing-panel" id="briefing-details" aria-labelledby="briefing-title">
    <div className="notification-heading"><div><span className="section-label">BEFORE THE OPEN · LESSON 14</span><h2 id="briefing-title" ref={heading} tabIndex={-1}>早盘简报</h2></div><button className="button button-quiet" onClick={onClose}>收起</button></div>
    <p className="briefing-intro">开盘前，先把关注清单与上一交易日的数据放在一起。只整理能核对的事实，缺什么就写明什么。</p>
    {loadError && <div className="alert alert-error" role="alert">读取未完成，保留上次内容。{loadError}<button className="text-button" onClick={() => void load()}>重新读取</button></div>}
    {!view ? <p className="briefing-intro">正在读取简报与设置…</p> : <>
      <div className="briefing-calendar"><span>今天 · {view.today}</span><strong>{view.calendar.state === 'open' ? `数据看 ${view.calendar.previous_trade_date} 收盘` : view.calendar.reason}</strong><p>生成时间与行情日期是两回事。这里的收盘价和成交量属于上一交易日，不是今天盘中的报价。</p></div>
      <div className="briefing-settings">
        <h3>安排每天的整理</h3>
        <label className="anomaly-checkbox" htmlFor="briefing-enabled"><input id="briefing-enabled" name="briefing-enabled" type="checkbox" checked={enabled} disabled={!!busy} onChange={event => { setEnabled(event.target.checked); setDirty(true); }} /><span>交易日自动生成<span>服务需保持运行；云端版无需电脑开机。关闭浏览器不影响后台，错过时点不自动补发。</span></span></label>
        <div className="briefing-setting-row"><label htmlFor="briefing-send-time">北京时间<select id="briefing-send-time" name="briefing-send-time" value={sendTime} disabled={!!busy} onChange={event => { setSendTime(event.target.value as '08:45' | '09:00'); setDirty(true); }}><option value="08:45">08:45 · 开盘前留出核对时间</option><option value="09:00">09:00 · 稍晚整理</option></select></label><button className="button button-primary" onClick={() => void saveSettings()} disabled={!!busy || !dirty || changedElsewhere}>{busy === 'save' ? '保存中…' : '保存定时设置'}</button></div>
        {changedElsewhere && dirty && <p className="alert alert-error">设置已被另一操作更新。<button className="text-button" onClick={restoreSettings}>读取已保存设置</button></p>}
        <p className="briefing-muted">已保存：{view.settings.enabled ? `交易日 ${view.settings.send_time} 自动生成` : '自动生成关闭'} · 飞书通知{view.notifications_enabled ? '开启' : '关闭'} · 本模块 AI 费用 0 元。<button className="text-button" onClick={onNotifications}>查看飞书开关</button></p>
      </div>
      <div className="briefing-schedule" role="status"><strong>{scheduleLabels[view.schedule.status] || view.schedule.status}</strong>{view.schedule.reason && <p>{view.schedule.reason}</p>}<p>后台{view.runtime.running ? '正在运行' : '状态待核对'}{view.runtime.heartbeat_at ? ` · 最近检查 ${formatTime(view.runtime.heartbeat_at)}` : ''}。{view.runtime.last_error}</p><p>定时生成完成与飞书接收是两个状态；发送结果要看对应消息记录。</p></div>
      <div className="briefing-actions"><button className="button button-outline" onClick={() => void generate()} disabled={!!busy || view.active || (!uncertain && !reusable && view.calendar.state !== 'open')}>{busy === 'generate' ? '正在提交…' : view.active ? '后台正在生成…' : uncertain ? '核对刚才的生成请求' : reusable ? '查看今日已保存摘要' : latest && ['failed', 'interrupted'].includes(latest.status) ? '明确重试今日摘要' : '手动生成今日摘要'}</button><button className="text-button" onClick={() => void load()}>刷新记录</button><p>手动生成只保存网页。若早于定时时点生成，后台到点会复用当天结果；错过时点后补生成不会补发。</p></div>
      {notice && <p className="anomaly-notice" role="status">{notice}</p>}
      {actionError && <div className="alert alert-error" role="alert">{actionError}</div>}
      {view.reports.length > 1 && <label className="explanation-history" htmlFor="briefing-history">查看已保存记录<select id="briefing-history" name="briefing-history" value={selected?.id || ''} onChange={event => setSelectedId(event.target.value)}>{view.reports.map(row => <option key={row.id} value={row.id}>{row.report_date} · {reportLabels[row.status]} · {formatTime(row.created_at)}</option>)}</select></label>}
      {selected ? <article className="briefing-report" aria-labelledby="briefing-report-title">
        <div className="anomaly-rule-title"><h3 id="briefing-report-title">{selected.report_date} 早盘简报</h3><span className="anomaly-mode">{reportLabels[selected.status]}</span></div>
        <p className="briefing-muted">{selected.trigger_kind === 'manual' ? '手动发起' : '定时发起'} · 清单固定于 {formatTime(selected.snapshot.frozen_at, true)}</p>
        {selected.watchlist_changed && <p className="briefing-warning">当前自选与生成时不同。下面保留当时的清单；下一交易日再按新清单整理。</p>}
        {selected.error_message && <p className="briefing-warning">{selected.error_message}</p>}
        {selected.output && <>
          <div className="briefing-coverage"><strong>覆盖 {selected.output.coverage.covered} / {selected.output.coverage.total} 只</strong><span>缺失 {selected.output.coverage.missing} 只</span><span>数据截至 {selected.output.data_cutoff_label}</span></div>
          {selected.status === 'empty' && <p className="briefing-intro">先在首页添加自选，再生成摘要。空清单不会发送消息。</p>}
          <div className="briefing-items">{selected.output.items.map(item => <section className="briefing-item" key={item.market + item.symbol + item.created_at}>
            <div className="anomaly-rule-title"><h4>{item.name} <span>{item.market}{item.symbol}</span></h4><span>{item.data ? '已覆盖' : '未覆盖'}</span></div>
            {item.data ? <><dl className="briefing-values"><div><dt>收盘价 · 元</dt><dd>{item.data.close}</dd></div><div><dt>成交量 · 股</dt><dd>{Number(item.data.volume).toLocaleString('zh-CN')}</dd></div></dl><p className="briefing-muted">{item.data.trade_date} · {item.data.adjustment_label} · {item.data.source}</p><details className="briefing-source"><summary>核对日期、来源与成交量单位</summary><dl><div><dt>来源接口</dt><dd>{item.data.source_url}</dd></div><div><dt>数据接收时间</dt><dd>{formatTime(item.data.received_at, true)}</dd></div><div><dt>成交量换算</dt><dd>{item.data.bar.raw_volume} {item.data.raw_volume_unit} × {item.data.volume_multiplier} = {item.data.volume} 股</dd></div><div><dt>本次采用</dt><dd>{item.data.origin === 'saved_daily' ? '已保存且通过日期核对的日线' : '本次新取得并核对的日线'}</dd></div></dl></details></> : <p className="briefing-warning">{item.reason}</p>}
          </section>)}</div>
          {selected.output.research_coverage && <div className="briefing-research"><h4>市场、资讯与日程</h4><p className="briefing-muted">资料截止：{knownTime(selected.output.research_cutoff)} · 有资料 {selected.output.research_coverage.available_sections.length} 类 · 缺项 {selected.output.research_coverage.missing_sections.length} 类 · 新增 {selected.output.research_coverage.new_count} 条 · 内容更新 {selected.output.research_coverage.updated_count} 条。</p>{selected.output.narrative && <p className="briefing-narrative">{selected.output.narrative}</p>}
            <ResearchEvidenceList context={selected.output.research_context} evidence={selected.output.research_evidence} idPrefix={`briefing-${selected.id}`} />
            {!!selected.output.research_context?.sections.length && <details className="briefing-source"><summary>展开全部市场与资讯资料</summary><div className="research-sections">{selected.output.research_context.sections.map(section => <ResearchSectionView key={section.key} section={section} />)}</div></details>}
          </div>}
          <p className="briefing-muted">{selected.output.note}</p>
          <p className="briefing-muted">摘要保存于 {formatTime(selected.output.completed_at, true)}</p>
          <AnalysisTraceView trace={selected.output.trace} />
        </>}
        <div className="anomaly-delivery"><strong>{selected.notification ? deliveryLabels[selected.notification.status] : selected.delivery_status === 'local_only' ? '仅保存网页' : selected.delivery_status === 'not_queued' ? '尚未进入发送队列' : '没有发送记录'}</strong><button className="text-button" onClick={onNotifications}>查看通知记录</button></div>
        {selected.notification?.error_message && <p className="briefing-warning">{selected.notification.error_message}</p>}
        {selected.notification?.status === 'accepted' && <p className="briefing-muted">{selected.notification.confirmed_at ? '你已确认收到。' : '飞书接口已接收；本人实收可到通知记录确认。'}</p>}
        <details className="briefing-source"><summary>查看固定清单与日历依据</summary><p>简报编号：{selected.id}</p><p>生成时清单：{selected.snapshot.watchlist.map(item => `${item.name} ${item.market}${item.symbol}`).join('；') || '空'}</p><p>日历版本：{selected.snapshot.calendar.version}</p>{selected.snapshot.calendar.sources.map(source => <p key={source.url}><a href={source.url} target="_blank" rel="noreferrer">{source.name}</a></p>)}</details>
      </article> : <div className="empty-state"><h3>还没有早盘摘要</h3><p>可以先手动生成，核对日期、价格、成交量和缺失项。</p></div>}
      <p className="briefing-muted briefing-calendar-note">日历核验于 {view.calendar.verified_on}，目前覆盖 {view.calendar.covered_years.join('、')} 年。{view.calendar.note}</p>
    </>}
  </section>;
}
