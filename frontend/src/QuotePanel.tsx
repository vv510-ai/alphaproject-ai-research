import type { QuoteState } from './api';

const number = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const time = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
  hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
});
const signed = (value: string) => `${Number(value) > 0 ? '+' : ''}${number.format(Number(value))}`;
const marketLabels = {
  trading: '连续竞价时段', break: '午间休市', auction: '集合竞价', closed: '已闭市', unknown: '交易时段待确认',
};

function ageText(seconds: number | null) {
  if (seconds === null) return '时间未知';
  if (seconds < 0) return '来源时间晚于本机，需核对时钟';
  if (seconds < 60) return `来源时间距今约 ${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `来源时间距今约 ${Math.floor(seconds / 60)} 分钟`;
  if (seconds < 86400) return `来源时间距今约 ${Math.floor(seconds / 3600)} 小时`;
  return `来源时间距今约 ${Math.floor(seconds / 86400)} 天 ${Math.floor(seconds % 86400 / 3600)} 小时`;
}

export default function QuotePanel({ name, state, loading, disabled, actionError, onRefresh, onDismiss }: {
  name: string; state?: QuoteState; loading: boolean; disabled: boolean;
  actionError?: string; onRefresh: () => void; onDismiss: () => void;
}) {
  const data = state?.data;
  const retry = state?.retry_after_seconds ?? 0;
  const processing = loading || state?.refresh_state === 'processing';
  const direction = data ? Number(data.change) > 0 ? 'up' : Number(data.change) < 0 ? 'down' : 'flat' : 'flat';
  const error = actionError || state?.error_message;
  const eligibility = state?.eligibility;
  return <section className="quote-panel" aria-label={`${name}报价`}>
    <div className="quote-topline">
      <span className={`quote-state ${state?.data_state ?? 'unavailable'}`}>{data ? state?.data_label : '尚无可用报价'}</span>
      <button className="button button-outline quote-refresh" onClick={onRefresh}
        disabled={disabled || processing || retry > 0} aria-label={`${data ? '更新' : '获取'}${name}报价`}>
        {processing ? '正在获取…' : retry > 0 ? `${retry} 秒后可更新` : data ? '更新报价' : '获取报价'}
      </button>
    </div>
    <div className={`quote-eligibility ${eligibility?.eligible ? 'is-ready' : 'is-waiting'}`}>
      <div><strong>{eligibility?.eligible ? '可用于异动判断' : '暂不用于异动判断'}</strong><span>{marketLabels[state?.market_state ?? 'unknown']}</span></div>
      <p>{eligibility?.reason || '正在确认这只股票的数据条件。'}</p>
    </div>
    {data ? <>
      <div className="quote-values">
        <div><span className="quote-caption">最新可用价格</span><p className={`quote-price ${direction}`}>{number.format(Number(data.price))}<span>元</span></p></div>
        <div className={`quote-change ${direction}`}><strong>{signed(data.change_pct)}%</strong><span>{signed(data.change)} 元 · 相对昨收</span></div>
      </div>
      <div className="quote-times">
        <div><span>报价时间（来源）</span><time dateTime={data.source_reported_at}>{time.format(new Date(data.source_reported_at))}</time><small>{ageText(state?.source_age_seconds ?? null)}</small></div>
        <div><span>本次接收时间</span><time dateTime={data.received_at}>{time.format(new Date(data.received_at))}</time><small>收到数据的时间，不代表行情刚刚更新</small></div>
      </div>
      <details className="quote-evidence"><summary>查看报价依据</summary><dl>
        <div><dt>来源</dt><dd>{data.source}</dd></div>
        <div><dt>来源股票</dt><dd>{data.name} · {data.symbol}</dd></div>
        <div><dt>同一来源昨收</dt><dd>{number.format(Number(data.previous_close))} 元</dd></div>
        <div><dt>涨跌幅核对</dt><dd>（价格 − 昨收）÷ 昨收 × 100%</dd></div>
        <div><dt>时间含义</dt><dd>接口返回的时间，按北京时间展示；未核实为最后成交时刻。</dd></div>
        <div><dt>判断时段</dt><dd>已核验交易日的连续竞价时段；午休、集合竞价和闭市时暂停。</dd></div>
        <div><dt>成交活动</dt><dd>最近两份新报价的来源时间与成交量都增加，才允许判断；尚未接入官方停牌状态。</dd></div>
        <div><dt>数据时效</dt><dd>通过检查表示可用于规则判断，不保证报价与交易所同步。</dd></div>
      </dl></details>
    </> : <p className="quote-empty">连续竞价时段会自动采集报价，也可尝试“获取报价”。暂未取得数据时保持为空。</p>}
    {error && <div className="quote-error" role="status"><p>{data ? '本次更新未完成，保留上次报价。' : '本次未取得可用报价。'}{error}</p>{actionError && <button className="text-button" onClick={onDismiss}>关闭操作提示</button>}</div>}
  </section>;
}
