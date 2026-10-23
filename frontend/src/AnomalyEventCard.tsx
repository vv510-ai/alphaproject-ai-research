import type { AnomalyEvent, NotificationRecord } from './api';
import { appBase } from './api';
import ExplanationCard from './ExplanationCard';

const deliveryLabels: Record<NotificationRecord['status'], string> = {
  pending: '等待发送', checking: '正在核对接收群', sending: '发送中 · 等待回执', accepted: '飞书已接收',
  failed: '发送失败', unknown: '发送结果待核对', cancelled: '未发送 · 已取消', expired: '未发送 · 已过期',
};
export default function AnomalyEventCard({ event, onNotifications, defaultOpen = false }: { event: AnomalyEvent; onNotifications: () => void; defaultOpen?: boolean }) {
  const proof = event.snapshot.eligibility?.proof;
  const thresholdEvent = event.direction === 'up' || event.direction === 'down';
  const titles: Record<string, string> = { limit_up: '达到涨停条件', limit_down: '达到跌停条件', breakout: '突破历史区间', breakdown: '跌破历史区间', volume: '成交量异常', amplitude: '振幅异常', event: '资讯事件提醒' };
  const title = thresholdEvent ? `${event.direction === 'up' ? '上涨' : '下跌'}达到 ${event.snapshot.threshold}%` : event.snapshot.rule_label || titles[event.direction] || '规则提醒';
  return <article className="anomaly-event">
        <span className={`anomaly-mode ${event.mode}`}>{event.mode === 'demo' ? '合成演练 · 非真实行情' : '真实规则事件'}</span>
        <h3>{event.snapshot.quote.name} · {title}</h3>
        {thresholdEvent ? <p className="anomaly-calculation">（{event.snapshot.quote.price} − {event.snapshot.quote.previous_close}）÷ {event.snapshot.quote.previous_close} × 100% = {Number(event.snapshot.change_pct).toFixed(2)}%</p> : <p className="anomaly-calculation">{event.snapshot.condition_text || '当时的条件与原始依据见下方。'}</p>}
        <p className="notification-help">{event.snapshot.detection_label} · {event.snapshot.repeat_policy}。这是采样发现的时间。</p>
        <div className="anomaly-delivery"><strong>{event.notification ? deliveryLabels[event.notification.status] : event.mode === 'demo' ? '本次只在网页演练' : '尚无通知记录'}</strong>{event.notification?.confirmed_at ? <span>你已确认收到</span> : event.notification && ['accepted', 'unknown'].includes(event.notification.status) && <button className="text-button" onClick={onNotifications}>去核对飞书收件</button>}</div>
        {event.notification?.error_message && <p className="notification-failure">{event.notification.error_message}</p>}
        <details className="quote-evidence"><summary>查看当时依据与事件标识</summary><dl>
          <div><dt>事件标识</dt><dd>{event.id}</dd></div><div><dt>股票身份</dt><dd>{event.market}{event.symbol}</dd></div>
          <div><dt>数据来源</dt><dd>{event.snapshot.quote.source}</dd></div><div><dt>来源时间</dt><dd>{event.snapshot.quote.source_reported_at}</dd></div>
          <div><dt>接收时间</dt><dd>{event.snapshot.quote.received_at}</dd></div><div><dt>保存时间</dt><dd>{event.created_at}</dd></div>
          <div><dt>规则版本</dt><dd>v{event.snapshot.rule_version} · {title}</dd></div>
          {event.snapshot.change_pct !== undefined && <div><dt>未舍入涨跌幅</dt><dd>{event.snapshot.change_pct}%</dd></div>}
          {event.snapshot.metric_value !== undefined && <div><dt>触发时数值</dt><dd>{event.snapshot.metric_value ?? '未记录'}</dd></div>}
          {event.snapshot.rule_evidence && Object.entries(event.snapshot.rule_evidence).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{typeof value === 'string' ? value : JSON.stringify(value)}</dd></div>)}
          {event.mode === 'live' && proof && <>
            <div><dt>前次来源时间</dt><dd>{proof.previous.source_reported_at}</dd></div>
            <div><dt>两次累计成交量原值</dt><dd>{proof.previous.cumulative_volume} → {proof.current.cumulative_volume}（同一来源口径，仅比较增长）</dd></div>
            <div><dt>触发时的行情资格</dt><dd>{event.snapshot.eligibility?.reason}</dd></div>
          </>}
        </dl><p className="notification-help">这份依据在触发时保存；之后改规则、更新报价或移出自选，都不会改掉它。</p></details>
        <a className="text-button event-detail-link" href={`${appBase}#event=${event.id}`}>打开这条事件的独立页面</a>
        <ExplanationCard eventId={event.id} defaultOpen={defaultOpen} />
      </article>;
}
