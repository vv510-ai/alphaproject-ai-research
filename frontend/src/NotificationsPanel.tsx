import { useCallback, useEffect, useRef, useState } from 'react';
import { api, appBase, formatTime, messageOf, newRequestId } from './api';
import type { NotificationRecord, NotificationState } from './api';

const statusNames: Record<NotificationRecord['status'], string> = {
  pending: '等待核对接收群', checking: '正在核对接收群', sending: '发送中 · 等待回执',
  accepted: '飞书已接收', failed: '本次未发送成功', unknown: '发送结果待核对', cancelled: '已取消 · 未提交',
  expired: '已过期 · 未提交',
};
const pendingKey = 'alpha-lab-feishu-pending-test';

export default function NotificationsPanel({ onClose }: { onClose: () => void }) {
  const [view, setView] = useState<NotificationState | null>(null);
  const [readError, setReadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [busy, setBusy] = useState('');
  const [codes, setCodes] = useState<Record<string, string>>({});
  const [pendingId, setPendingId] = useState(() => sessionStorage.getItem(pendingKey));
  const heading = useRef<HTMLHeadingElement>(null);
  const readRequest = useRef<AbortController | null>(null);
  const actionRequest = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const busyRef = useRef(false);

  const load = useCallback(async () => {
    if (busyRef.current) return;
    readRequest.current?.abort();
    const controller = new AbortController();
    readRequest.current = controller;
    try {
      const result = await api<NotificationState>('/notifications', {}, controller.signal);
      if (controller.signal.aborted || !alive.current) return;
      setView(result);
      setReadError('');
      const saved = sessionStorage.getItem(pendingKey);
      if (saved && result.records.some(row => row.id === saved)) {
        sessionStorage.removeItem(pendingKey);
        setPendingId(null);
      }
    } catch (error) {
      if (!controller.signal.aborted && alive.current) setReadError(messageOf(error));
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    let stopped = false;
    let timer: number;
    async function poll() {
      if (!document.hidden) await load();
      if (!stopped) timer = window.setTimeout(poll, 3000);
    }
    heading.current?.focus({ preventScroll: true });
    heading.current?.scrollIntoView({ block: 'start', behavior: 'smooth' });
    void poll();
    return () => {
      alive.current = false;
      stopped = true;
      window.clearTimeout(timer);
      readRequest.current?.abort();
      actionRequest.current?.abort();
    };
  }, [load]);

  async function act(name: string, path: string, body?: object) {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(name);
    setActionError('');
    readRequest.current?.abort();
    const controller = new AbortController();
    actionRequest.current = controller;
    try {
      await api(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) }, controller.signal);
      if (name === 'send') {
        sessionStorage.removeItem(pendingKey);
        if (alive.current) setPendingId(null);
      }
    } catch (error) {
      if (!controller.signal.aborted && alive.current) setActionError(messageOf(error));
    } finally {
      busyRef.current = false;
      if (alive.current) { setBusy(''); await load(); }
    }
  }

  function send() {
    if (!view || busyRef.current) return;
    const requestId = pendingId || newRequestId();
    sessionStorage.setItem(pendingKey, requestId);
    setPendingId(requestId);
    void act('send', '/notifications/test', { request_id: requestId, target_fingerprint: view.target_fingerprint });
  }

  return <section id="notification-details" className="panel notification-panel" aria-labelledby="notification-title">
    <div className="notification-heading"><div><div className="section-label">FEISHU NOTIFICATIONS</div><h2 id="notification-title" ref={heading} tabIndex={-1}>飞书通知</h2><p>查看接收群，核对测试和异动提醒的送达结果。</p></div><button className="button button-quiet" onClick={onClose}>收起通知</button></div>
    {readError && <div className="alert alert-error" role="alert">读取状态失败，下面保留上次结果。{readError}<button className="text-button" onClick={() => void load()}>重新读取</button></div>}
    {actionError && <div className="alert alert-error" role="alert">{actionError}<button className="text-button" onClick={() => setActionError('')}>关闭提示</button></div>}
    {!view ? <p className="notification-help">正在读取通知设置…</p> : <>
      {!view.configured ? <p className="notification-help">{view.config_error || '尚未导入飞书配置，请在电脑上完成配置导入后重启服务。'}</p> : <>
        <div className="notification-target"><div><span className="notification-caption">接收群</span><h3>{view.group_name}</h3><p>机器人：{view.bot_name} · 复用原项目配置</p></div><span className={`notification-badge ${view.enabled ? 'is-on' : ''}`}>{view.enabled ? '通知已开启' : '通知已关闭'}</span></div>
        <p className="notification-help">{view.verified_at ? `最近核对 ${formatTime(view.verified_at, true)}：私有群、1 位成员、1 个机器人。` : '开启前会核对接收群，之后每次发送前也会再核对。'}成员变化会停止发送。</p>
        {view.check_error && <p className="notification-failure" role="status">最近一次核对未通过：{view.check_error}</p>}
        <div className="notification-actions"><button className={`button ${view.enabled ? 'button-outline' : 'button-primary'}`} onClick={() => void act('toggle', '/notifications/settings', { enabled: !view.enabled })} disabled={!!busy}>{busy === 'toggle' ? '正在保存…' : view.enabled ? '关闭通知' : '开启通知'}</button><button className="button button-quiet" onClick={() => void act('verify', '/notifications/verify')} disabled={!!busy}>{busy === 'verify' ? '核对中…' : '重新核对接收群'}</button></div>
        <p className="notification-help">关闭后，尚未提交的消息会取消；已经发送中的消息可能仍会送达。异动事件仍会保存，重新开启不会补发已取消的消息。</p>
        <div className="notification-test"><div><h3>发一条连通测试</h3><p>消息包含“学习工程测试”标记、记录标识、北京时间和收件核验码。核验码只在飞书里查看。</p></div><button className="button button-primary" onClick={send} disabled={!!busy || !view.enabled || !!readError || view.active || view.retry_after_seconds > 0}>{busy === 'send' || view.active ? '正在处理测试…' : view.retry_after_seconds > 0 ? `${view.retry_after_seconds} 秒后可再测试` : pendingId ? '核对上次测试请求' : '发送一条测试'}</button></div>
      </>}
      <div className="notification-records"><div className="notification-records-heading"><h3>最近的发送记录</h3><span>最多展示 20 条 · 全部保存在服务数据库</span></div>{view.records.length === 0 ? <p className="notification-help">还没有测试记录。开启后，主动点击“发送一条测试”。</p> : <ol>{view.records.map(record => <li key={record.id}>
        <div className="notification-record-top"><strong>{statusNames[record.status]}</strong><span className={`notification-receipt ${record.confirmed_at ? 'confirmed' : ''}`}>{record.confirmed_at ? '你已确认收到' : '收件尚未确认'}</span></div>
        <p className="notification-record-meta">{formatTime(record.created_at, true)} · {record.group_name}<br />记录：<span>{record.id}</span></p>
        {record.event_id && <p className="notification-record-meta">{record.kind === 'demo' ? '合成演练 · 非真实行情' : '异动事件'}<br />事件：<a href={`${appBase}#event=${record.event_id}`}>打开对应事件 · {record.event_id}</a></p>}
        {record.briefing_id && <p className="notification-record-meta">早盘数据简报<br />简报：<span>{record.briefing_id}</span></p>}
        {record.error_message && <p className="notification-failure">{record.error_message}</p>}
        {record.confirmed_at ? <p className="notification-help">收件确认于 {formatTime(record.confirmed_at, true)}。</p> : (record.status === 'accepted' || record.status === 'unknown') && <form className="notification-receipt-form" onSubmit={event => { event.preventDefault(); void act(`confirm:${record.id}`, `/notifications/${record.id}/confirm`, { code: codes[record.id] || '' }); }}>
          <label htmlFor={`receipt-${record.id}`}>把飞书里这条消息的 6 位核验码填在这里</label><div><input id={`receipt-${record.id}`} inputMode="numeric" pattern="[0-9]{6}" maxLength={6} autoComplete="off" required placeholder="6 位核验码" value={codes[record.id] || ''} onChange={event => setCodes(previous => ({ ...previous, [record.id]: event.target.value.replace(/\D/g, '') }))} /><button className="button button-outline" type="submit" disabled={!!busy || (codes[record.id] || '').length !== 6}>{busy === `confirm:${record.id}` ? '核对中…' : '确认我已收到'}</button></div>
        </form>}
      </li>)}</ol>}</div>
    </>}
    <p className="notification-footnote">飞书已接收 ≠ 你已经看到。发送结果未知时先查看群消息，系统不会自动重发。</p>
  </section>;
}
