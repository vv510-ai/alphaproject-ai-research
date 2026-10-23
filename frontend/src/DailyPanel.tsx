import { useCallback, useEffect, useRef, useState } from 'react';
import { api, appBase, messageOf } from './api';
import type { DailyState, WatchItem } from './api';
import DailyChart from './DailyChart';

const price = new Intl.NumberFormat('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const shares = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 });
const time = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
  day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });

export default function DailyPanel({ item, onClose }: { item: WatchItem; onClose: () => void }) {
  const [view, setView] = useState<DailyState | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [readError, setReadError] = useState('');
  const [actionError, setActionError] = useState('');
  const [fetching, setFetching] = useState(false);
  const [selectedDate, setSelectedDate] = useState('');
  const [mode, setMode] = useState<'recent' | 'all'>('recent');
  const [rangeRevision, setRangeRevision] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const request = useRef<AbortController | null>(null);
  const refreshRequest = useRef<AbortController | null>(null);
  const alive = useRef(true);
  const endpoint = `/daily/${item.market}/${item.symbol}`;
  const generation = item.created_at;

  const accept = useCallback((result: DailyState) => {
    if (result.watchlist_created_at !== generation) throw new Error('这条自选已重新添加，请关闭详情后重新打开。');
    setView(previous => ({ ...result, data: previous?.data && result.data?.received_at === previous.data.received_at
      ? previous.data : result.data }));
    if (result.data) setSelectedDate(previous => result.data!.bars.some(bar => bar.date === previous) ? previous : result.data!.latest_trade_date);
  }, [generation]);

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      const result = await api<DailyState>(endpoint, {}, controller.signal);
      if (controller.signal.aborted || !alive.current) return;
      accept(result);
      setLoaded(true);
      setReadError('');
    } catch (error) {
      if (!controller.signal.aborted && alive.current) setReadError(messageOf(error));
    }
  }, [endpoint, accept]);

  useEffect(() => {
    alive.current = true;
    let stopped = false;
    let timer: number;
    const poll = async () => {
      if (!document.hidden) await load();
      if (!stopped) timer = window.setTimeout(poll, 5000);
    };
    void poll();
    const visible = () => { if (!document.hidden) void load(); };
    document.addEventListener('visibilitychange', visible);
    heading.current?.focus({ preventScroll: true });
    heading.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return () => {
      stopped = true;
      alive.current = false;
      window.clearTimeout(timer);
      request.current?.abort();
      refreshRequest.current?.abort();
      document.removeEventListener('visibilitychange', visible);
    };
  }, [load]);

  async function refresh() {
    if (fetching) return;
    setFetching(true);
    setActionError('');
    const controller = new AbortController();
    refreshRequest.current = controller;
    try {
      const result = await api<{ refresh_succeeded: boolean; history: DailyState }>(`${endpoint}/refresh`, {
        method: 'POST', body: JSON.stringify({ watchlist_created_at: generation }),
      }, controller.signal);
      if (controller.signal.aborted || !alive.current) return;
      accept(result.history);
      setLoaded(true);
    } catch (error) {
      if (!controller.signal.aborted && alive.current) setActionError(messageOf(error));
    } finally {
      if (!controller.signal.aborted && alive.current) {
        setFetching(false);
        void load();
      }
    }
  }

  const data = view?.data;
  const selected = data?.bars.find(bar => bar.date === selectedDate);
  const selectedIndex = data?.bars.findIndex(bar => bar.date === selectedDate) ?? -1;
  const processing = fetching || view?.refresh_state === 'processing';
  const retry = view?.retry_after_seconds ?? 0;
  const error = actionError || view?.error_message;

  function showRange(nextMode: 'recent' | 'all') {
    if (!data) return;
    setMode(nextMode);
    setSelectedDate(data.latest_trade_date);
    setRangeRevision(previous => previous + 1);
  }

  return <section className="daily-panel panel" aria-labelledby="daily-title" id="daily-details">
    <div className="daily-heading"><div><div className="section-label">DAILY HISTORY</div><h2 id="daily-title" ref={heading} tabIndex={-1}>{item.name}<span className="daily-symbol">{item.symbol}</span></h2><p>日线与成交量 · 不复权</p></div>
      <div className="daily-actions"><button className="button button-primary" onClick={() => void refresh()} disabled={!loaded || processing || retry > 0}>
        {processing ? '正在获取日线…' : retry > 0 ? `${retry} 秒后可更新` : data ? '更新日线' : '获取日线'}</button><button className="button button-outline" onClick={onClose}>关闭详情</button></div>
    </div>

    <p className="daily-intro">一根日 K 线记录一天的开盘、最高、最低、收盘；下面的柱子记录成交股数。只展示昨日及以前的数据。</p>
    {readError && <div className="alert alert-error" role="alert"><span>日线同步未完成。{readError}</span><button className="text-button" onClick={() => void load()}>重新读取</button></div>}
    {error && <div className="alert alert-error daily-refresh-error" role="status"><span>{data ? '本次更新未完成，保留上次日线。' : '本次未取得可用日线。'}{error}</span>{actionError && <button className="text-button" onClick={() => setActionError('')}>关闭操作提示</button>}</div>}

    {!loaded && !readError ? <div className="empty-state"><div className="loading-line" /><p>正在读取已保存日线…</p></div> : !data ? <div className="daily-empty"><h3>还没有保存这只股票的日线</h3><p>点“获取日线”，先核对真实数据，再生成图表。无数据时保持为空。</p></div> : <>
      <div className="daily-source-line"><span>来源最新返回日期 <strong>{data.latest_trade_date}</strong></span><span>实际 {data.bars.length} 条 · {data.earliest_trade_date} 至 {data.latest_trade_date}</span><span>接收于 <time dateTime={data.received_at}>{time.format(new Date(data.received_at))}</time></span></div>
      <div className="daily-toolbar"><p>点击图中一天，或用下面的日期选择查看明细。范围按钮同时回到最新一天。</p><div className="daily-range" aria-label="图表显示范围"><button className={`button ${mode === 'recent' ? 'button-primary' : 'button-outline'}`} aria-pressed={mode === 'recent'} onClick={() => showRange('recent')}>近 30 条</button><button className={`button ${mode === 'all' ? 'button-primary' : 'button-outline'}`} aria-pressed={mode === 'all'} onClick={() => showRange('all')}>全部</button></div></div>
      <DailyChart bars={data.bars} selectedDate={selectedDate} mode={mode} rangeRevision={rangeRevision} onSelect={day => { if (data.bars.some(bar => bar.date === day)) setSelectedDate(day); }} />
      <p className="daily-legend"><span className="legend-up">红：收盘高于开盘</span><span className="legend-down">绿：收盘低于开盘</span><span>灰：相等。首页涨跌幅则与昨收比较。</span></p>
      <div className="daily-day-selector"><label htmlFor="daily-date">查看哪一天</label><select id="daily-date" value={selectedDate} onChange={event => setSelectedDate(event.target.value)}>{[...data.bars].reverse().map(bar => <option value={bar.date} key={bar.date}>{bar.date}</option>)}</select>
        <button className="button button-outline" disabled={selectedIndex <= 0} onClick={() => setSelectedDate(data.bars[selectedIndex - 1].date)}>前一条</button><button className="button button-outline" disabled={selectedIndex < 0 || selectedIndex >= data.bars.length - 1} onClick={() => setSelectedDate(data.bars[selectedIndex + 1].date)}>后一条</button>
      </div>
      {selected && <div className="daily-selected" aria-live="polite" aria-atomic="true" data-date={selected.date}><h3>{selected.date} 的日线明细</h3><dl>
        <div><dt>开盘价</dt><dd>{price.format(Number(selected.open))}<small>元</small></dd></div><div><dt>最高价</dt><dd>{price.format(Number(selected.high))}<small>元</small></dd></div><div><dt>最低价</dt><dd>{price.format(Number(selected.low))}<small>元</small></dd></div><div><dt>收盘价</dt><dd>{price.format(Number(selected.close))}<small>元</small></dd></div><div className="daily-volume-value"><dt>成交量</dt><dd>{shares.format(Number(selected.volume))}<small>股</small></dd></div>
      </dl><p className="daily-unit-basis">来源原量 {selected.raw_volume} {data.raw_volume_unit} × {data.volume_multiplier} = {shares.format(Number(selected.volume))} 股</p></div>}
      <details className="daily-explanation"><summary>怎样理解这张图和数据口径</summary><div><p><strong>日线看过程，成交量看交易活跃程度。</strong>它们提供变化的背景；仅靠这些数字，不能证明某条消息导致上涨或预测以后会涨。</p><p><strong>不复权：</strong>保留来源历史价格。分红、送转等可能造成价格跳变，不把整段价格涨跌直接当作投资收益。</p><p><strong>来源：</strong>{data.source}；来源身份为 {data.name}（{data.market}{data.symbol}）。价格单位元，成交量统一为股；原量及换算写在当日明细中。</p><p><strong>时间与缺口：</strong>本次查询 {data.requested_start} 至 {data.requested_end}。只画来源返回且通过校验的记录，不补造缺失交易日；最新日期不代表当前报价。交易日历、停牌与数据完整性尚未核验。</p></div></details>
      <details className="daily-table-details"><summary>查看逐日数据表（{data.bars.length} 条）</summary><div className="daily-table-scroll" tabIndex={0} role="region" aria-label="日线数据表，可横向滚动"><table><caption>{item.name} · 不复权 · 价格元 / 成交量股</caption><thead><tr><th scope="col">日期</th><th scope="col">开盘</th><th scope="col">最高</th><th scope="col">最低</th><th scope="col">收盘</th><th scope="col">成交量（股）</th></tr></thead><tbody>{[...data.bars].reverse().map(bar => <tr key={bar.date} className={bar.date === selectedDate ? 'selected' : ''}><th scope="row"><button className="text-button" onClick={() => setSelectedDate(bar.date)} aria-label={`选择 ${bar.date}`}>{bar.date}</button></th><td>{price.format(Number(bar.open))}</td><td>{price.format(Number(bar.high))}</td><td>{price.format(Number(bar.low))}</td><td>{price.format(Number(bar.close))}</td><td>{shares.format(Number(bar.volume))}</td></tr>)}</tbody></table></div></details>
      <p className="chart-attribution">TradingView Lightweight Charts™ · Copyright (с) 2025 TradingView, Inc. <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">TradingView</a> · <a href={`${appBase}licenses/third-party-notices.txt`} target="_blank" rel="noreferrer">图表许可</a></p>
    </>}
  </section>;
}
