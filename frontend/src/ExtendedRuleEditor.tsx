import { useEffect, useRef, useState } from 'react';
import { api, knownTime, messageOf } from './api';
import type { AnomalyState, ExtendedRule, ExtendedRuleConfig } from './api';

const initial: ExtendedRuleConfig = { limit_enabled: false, breakout_enabled: false, volume_enabled: false, amplitude_enabled: false, event_enabled: false, volume_ratio: '3', amplitude_pct: '8', lookback_days: 60 };
const options = [
  { key: 'limit_enabled', title: '涨停或跌停条件', help: '核对来源给出的涨停、跌停价格。字段缺失或与当前价格不一致时暂停，不按股票名称猜测。' },
  { key: 'breakout_enabled', title: '突破或跌破历史区间', help: '对照前几个交易日的不复权收盘价边界。日线不足时暂停，除权除息影响仍需人工核对。' },
  { key: 'volume_enabled', title: '量比异常', help: '按行情来源直接提供的量比判断。来源未提供时暂停，不把未知值当作 0。' },
  { key: 'amplitude_enabled', title: '振幅异常', help: '核对本次行情的最高、最低和昨收；缺少字段时暂停判断。' },
  { key: 'event_enabled', title: '新资讯或公告', help: '发现符合条件的新增资料时保留来源。资讯提醒不代表已证实价格变化原因。' },
] as const;

function ResultNotes({ value }: { value: unknown }) {
  if (!value) return <p className="notification-help">还没有扩展条件的判断记录。</p>;
  const values = Array.isArray(value) ? value : typeof value === 'object' ? Object.entries(value).map(([key, item]) => ({ key, ...(item && typeof item === 'object' ? item : { reason: item }) })) : [{ reason: value }];
  if (!values.length) return <p className="notification-help">还没有扩展条件的判断记录。</p>;
  return <ul className="extended-results">{values.map((entry, index) => {
    const row: Record<string, unknown> = entry && typeof entry === 'object' ? entry as Record<string, unknown> : { reason: entry };
    const title = row.rule_label || row.title || row.rule_type || row.key || '最近判断';
    const reason = row.reason || row.message || row.status || '状态待确认';
    return <li key={index}><strong>{String(title)}</strong><span>{typeof reason === 'string' ? reason : JSON.stringify(reason)}</span></li>;
  })}</ul>;
}

export default function ExtendedRuleEditor({ item, onRefresh }: { item: AnomalyState['items'][number]; onRefresh: () => Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState<ExtendedRule | null>(item.extended_rule || null);
  const [config, setConfig] = useState<ExtendedRuleConfig>(item.extended_rule?.config || initial);
  const [enabled, setEnabled] = useState(!!item.extended_rule?.enabled);
  const [version, setVersion] = useState(item.extended_rule?.version ?? 0);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [reload, setReload] = useState(0);
  const alive = useRef(true);
  const request = useRef<AbortController | null>(null);
  const path = `/anomalies/extended/${item.market}/${item.symbol}`;
  const saved = item.extended_rule && item.extended_rule.version >= (current?.version ?? 0) ? item.extended_rule : current;
  const changed = !!saved && saved.version > version;
  useEffect(() => { alive.current = true; return () => { alive.current = false; request.current?.abort(); }; }, []);
  useEffect(() => {
    if (item.extended_rule && !dirty && item.extended_rule.version >= version) {
      setCurrent(item.extended_rule); setConfig(item.extended_rule.config); setEnabled(!!item.extended_rule.enabled); setVersion(item.extended_rule.version);
    }
  }, [item.extended_rule, dirty, version]);
  useEffect(() => {
    if (!open || current || item.extended_rule) return;
    const controller = new AbortController(); request.current = controller;
    setBusy(true);
    void api<ExtendedRule>(path, {}, controller.signal).then(result => {
      if (!controller.signal.aborted && alive.current) { setBusy(false); setCurrent(result); setConfig(result.config); setVersion(result.version); setEnabled(!!result.enabled); setError(''); }
    }).catch(failure => { if (!controller.signal.aborted && alive.current) setError(messageOf(failure)); })
      .finally(() => { if (!controller.signal.aborted && alive.current) setBusy(false); });
    return () => controller.abort();
  }, [open, path, current, item.extended_rule, reload]);
  function restore() {
    if (!saved) return;
    setConfig(saved.config); setEnabled(!!saved.enabled); setVersion(saved.version); setDirty(false); setError(''); setNotice('');
  }
  function change(updates: Partial<ExtendedRuleConfig>) { setConfig(previous => ({ ...previous, ...updates })); setDirty(true); setNotice(''); }
  async function save() {
    if (busy || changed || !current && !item.extended_rule) return;
    if (!Number.isInteger(config.lookback_days) || config.lookback_days < 5 || config.lookback_days > 120) { setError('历史对照天数请填写 5 至 120 之间的整数。'); return; }
    if (!/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(config.volume_ratio) || Number(config.volume_ratio) <= 0 || !/^(?:\d+(?:\.\d+)?|\.\d+)$/.test(config.amplitude_pct) || Number(config.amplitude_pct) <= 0) { setError('量比与振幅阈值请填写大于 0 的数字。'); return; }
    setBusy(true); setError(''); setNotice(''); request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    try {
      const result = await api<ExtendedRule>(path, { method: 'PUT', body: JSON.stringify({ watchlist_created_at: item.created_at, expected_version: version, enabled, config }) }, controller.signal);
      if (alive.current && !controller.signal.aborted) {
        setCurrent(result); setVersion(result.version); setConfig(result.config); setEnabled(!!result.enabled); setDirty(false);
        setNotice(result.enabled ? '扩展提醒设置已保存。只有所选条件和资料资格都满足时才会触发。' : '扩展提醒设置已保存，总开关保持关闭。');
      }
    } catch (failure) { if (alive.current && !controller.signal.aborted) setError(messageOf(failure)); }
    finally { if (alive.current) { setBusy(false); await onRefresh(); } }
  }
  const prefix = `extended-${item.market}${item.symbol}`;
  return <details className="extended-rule" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>更多提醒条件<span>{saved ? `规则 v${saved.version} · ${saved.enabled ? '总开关开启' : '总开关关闭'}` : '展开后读取设置'}</span></summary>
    <p className="notification-help">以下条件独立于上方涨跌幅规则。先选择条件，再开启总开关并保存；数据不够时会显示原因。</p>
    {open && <>
      <label className="anomaly-checkbox"><input type="checkbox" checked={enabled} disabled={busy || !saved} onChange={event => { setEnabled(event.target.checked); setDirty(true); setNotice(''); }} /><span>启用扩展提醒<span>勾选本身不会保存。下方每类条件也需要单独选中。</span></span></label>
      <div className="extended-options">{options.map(option => <label className="anomaly-checkbox" key={option.key}><input type="checkbox" checked={!!config[option.key]} disabled={busy || !saved} onChange={event => change({ [option.key]: event.target.checked })} /><span>{option.title}<span>{option.help}</span></span></label>)}</div>
      <div className="extended-fields"><label htmlFor={`${prefix}-lookback`}>历史对照天数<input id={`${prefix}-lookback`} inputMode="numeric" type="number" min={5} max={120} step={1} value={config.lookback_days || ''} disabled={busy || !saved} onChange={event => change({ lookback_days: Number(event.target.value) })} /></label><label htmlFor={`${prefix}-volume`}>量比阈值（倍）<input id={`${prefix}-volume`} inputMode="decimal" value={config.volume_ratio} disabled={busy || !saved} onChange={event => change({ volume_ratio: event.target.value })} /></label><label htmlFor={`${prefix}-amplitude`}>振幅阈值（%）<input id={`${prefix}-amplitude`} inputMode="decimal" value={config.amplitude_pct} disabled={busy || !saved} onChange={event => change({ amplitude_pct: event.target.value })} /></label></div>
      {changed && dirty && <p className="research-warning">其他操作已修改这份设置。你的草稿仍保留，请先<button className="text-button" onClick={restore}>读取已保存的扩展设置</button>。</p>}
      <div className="notification-actions"><button className="button button-outline" disabled={busy || !saved || changed} onClick={() => void save()}>{busy ? '正在处理…' : '保存扩展提醒'}</button>{dirty && <span className="notification-help">有尚未保存的修改</span>}</div>
      {error && <div className="research-warning" role="alert">{error}{!saved && <button className="text-button" disabled={busy} onClick={() => setReload(value => value + 1)}>重新读取扩展设置</button>}</div>}<div className="anomaly-notice" role="status">{notice}</div>
      <div className="extended-saved"><strong>{!saved ? '扩展设置尚未读取' : saved.enabled ? '已保存：扩展提醒总开关开启' : '已保存：扩展提醒总开关关闭'}</strong><p className="notification-help">最近检查：{knownTime(saved?.last_checked_at)}。判断采用的具体口径与资料，会保存在触发事件中。</p><ResultNotes value={saved?.last_results} /></div>
    </>}
  </details>;
}
