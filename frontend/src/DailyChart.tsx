import { useEffect, useRef, useState } from 'react';
import {
  CandlestickSeries, ColorType, CrosshairMode, HistogramSeries, createChart,
} from 'lightweight-charts';
import type { IChartApi, ISeriesApi, MouseEventParams, Time } from 'lightweight-charts';
import type { DailyBar } from './api';

const up = '#b23b32', down = '#287159', flat = '#6b7270';
const barColor = (bar: DailyBar) => Number(bar.close) > Number(bar.open) ? up : Number(bar.close) < Number(bar.open) ? down : flat;
const volumeAxis = (value: number) => value >= 1e8 ? `${(value / 1e8).toFixed(2)}亿`
  : value >= 1e4 ? `${(value / 1e4).toFixed(1)}万` : Math.round(value).toLocaleString('zh-CN');

function dayOf(value: Time | undefined) {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object') return `${value.year}-${String(value.month).padStart(2, '0')}-${String(value.day).padStart(2, '0')}`;
  return null;
}

export default function DailyChart({ bars, selectedDate, mode, rangeRevision, onSelect }: {
  bars: DailyBar[]; selectedDate: string; mode: 'recent' | 'all'; rangeRevision: number; onSelect: (day: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const previousRange = useRef<{ bars: DailyBar[]; mode: 'recent' | 'all'; revision: number } | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!container.current) return;
    let chart: IChartApi | undefined;
    try {
      chart = createChart(container.current, {
        autoSize: true,
        layout: {
          background: { type: ColorType.Solid, color: '#ffffff' }, textColor: '#51665a',
          fontSize: 12, fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif', attributionLogo: true,
          panes: { separatorColor: '#e7ece4', separatorHoverColor: '#d7e2d3', enableResize: false },
        },
        grid: { vertLines: { visible: false }, horzLines: { color: '#eef1eb' } },
        crosshair: { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: '#e3e9df' },
        timeScale: { borderColor: '#e3e9df', timeVisible: false, rightOffset: 1, fixLeftEdge: false },
        localization: { locale: 'zh-CN', dateFormat: 'yyyy-MM-dd' },
        handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      });
      candleRef.current = chart.addSeries(CandlestickSeries, {
        upColor: up, downColor: down, wickUpColor: up, wickDownColor: down, borderVisible: false,
        priceFormat: { type: 'price', precision: 2, minMove: 0.01 }, priceLineVisible: false,
      });
      volumeRef.current = chart.addSeries(HistogramSeries, {
        priceFormat: { type: 'custom', minMove: 1, formatter: volumeAxis }, priceLineVisible: false,
      }, 1);
      chart.panes()[1].setHeight(110);
      const choose = (event: MouseEventParams<Time>) => {
        const day = dayOf(event.time);
        if (day) onSelectRef.current(day);
      };
      chart.subscribeClick(choose);
      chartRef.current = chart;
      setReady(true);
      return () => {
        chart?.unsubscribeClick(choose);
        chart?.remove();
        chartRef.current = null;
        candleRef.current = null;
        volumeRef.current = null;
        previousRange.current = null;
      };
    } catch {
      chart?.remove();
      setError('图表暂时无法显示，可使用日期选择和下方逐日数据表核对。');
    }
  }, []);

  useEffect(() => {
    if (!ready || !candleRef.current || !volumeRef.current) return;
    candleRef.current.setData(bars.map(bar => ({ time: bar.date, open: Number(bar.open), high: Number(bar.high),
      low: Number(bar.low), close: Number(bar.close), color: barColor(bar), wickColor: barColor(bar) })));
    volumeRef.current.setData(bars.map(bar => ({ time: bar.date, value: Number(bar.volume), color: barColor(bar) })));
  }, [bars, ready]);

  useEffect(() => {
    const chart = chartRef.current, candles = candleRef.current;
    const index = bars.findIndex(bar => bar.date === selectedDate);
    if (!chart || !candles || !ready || index < 0) return;
    const previous = previousRange.current;
    const resetRange = !previous || previous.bars !== bars || previous.mode !== mode || previous.revision !== rangeRevision;
    if (resetRange) {
      if (mode === 'all') chart.timeScale().fitContent();
      else {
        const from = index >= bars.length - 30 ? Math.max(0, bars.length - 30) - 1 : Math.max(-1, index - 15);
        chart.timeScale().setVisibleLogicalRange({ from, to: from + Math.min(31, bars.length + 1) });
      }
    } else {
      const visible = chart.timeScale().getVisibleLogicalRange();
      if (visible && (index < visible.from || index > visible.to)) {
        const span = Math.max(10, visible.to - visible.from);
        const from = Math.max(-1, Math.min(index - span / 2, bars.length - span));
        chart.timeScale().setVisibleLogicalRange({ from, to: from + span });
      }
    }
    previousRange.current = { bars, mode, revision: rangeRevision };
    // Range changes are applied on the chart's next frame; do not read the old
    // range in a second effect or position the crosshair before that frame.
    const frame = requestAnimationFrame(() => chart.setCrosshairPosition(Number(bars[index].close), selectedDate, candles));
    return () => cancelAnimationFrame(frame);
  }, [bars, mode, selectedDate, ready, rangeRevision]);

  return <>
    <div className="daily-chart-labels"><span>日 K 线 · 元</span><span>下图成交量 · 股（轴以万 / 亿简写）</span></div>
    {error && <p className="alert alert-error" role="status">{error}</p>}
    <div className="daily-chart" ref={container} role="img" aria-label="日K线与成交量图，可点击日期，下方提供同一份数据的文字明细"
      data-bar-count={bars.length} data-selected-date={selectedDate} />
  </>;
}
