"use client";
/** TradingView lightweight-charts wrappers. One y-axis per chart (indicator panes are separate
 *  charts, never a dual axis). Colours come from the CSS tokens so light/dark both work. */
import { createChart, CrosshairMode, type IChartApi, type Time } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

/** bump on theme change so charts re-read the CSS tokens */
function useThemeVersion() {
  const [v, setV] = useState(0);
  useEffect(() => {
    const bump = () => setV((x) => x + 1);
    const mq = matchMedia("(prefers-color-scheme: dark)");
    window.addEventListener("fm-theme", bump);
    mq.addEventListener("change", bump);
    return () => { window.removeEventListener("fm-theme", bump); mq.removeEventListener("change", bump); };
  }, []);
  return v;
}

import type { Bar } from "@/lib/types";

const css = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
/** overlay colours may be given as CSS token names ("--s2") so they follow the theme */
const col = (c: string) => (c.startsWith("--") ? css(c) : c);
const rgb = (name: string) => `rgb(${css(name).split(" ").join(",")})`;

function baseChart(el: HTMLElement, height: number): IChartApi {
  return createChart(el, {
    height,
    autoSize: true,
    layout: { background: { color: "transparent" }, textColor: rgb("--muted"), fontFamily: "system-ui, sans-serif" },
    grid: { vertLines: { visible: false }, horzLines: { color: css("--grid") } },
    rightPriceScale: { borderColor: css("--axis") },
    timeScale: { borderColor: css("--axis") },
    crosshair: { mode: CrosshairMode.Normal },
    localization: { locale: "en-IN" },  // never depend on the browser locale string (some envs report e.g. "en-US@posix")
  });
}

type Overlay = { name: string; data: (number | null)[]; color: string; dashed?: boolean };

export function PriceChart({ bars, overlays = [], height = 340 }: { bars: Bar[]; overlays?: Overlay[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  useEffect(() => {
    if (!ref.current || !bars.length) return;
    const chart = baseChart(ref.current, height);
    const hasOhlc = bars.some((b) => b.open !== null && b.high !== null && b.open !== b.close);
    if (hasOhlc) {
      const s = chart.addCandlestickSeries({ upColor: rgb("--up"), downColor: rgb("--down"), wickUpColor: rgb("--up"), wickDownColor: rgb("--down"), borderVisible: false });
      s.setData(bars.map((b) => ({ time: b.date as Time, open: b.open ?? b.close, high: b.high ?? b.close, low: b.low ?? b.close, close: b.close })));
    } else {
      const s = chart.addLineSeries({ color: css("--s1"), lineWidth: 2 });
      s.setData(bars.map((b) => ({ time: b.date as Time, value: b.close })));
    }
    const tail = bars.slice(-Math.max(...overlays.map((o) => o.data.length), 0));
    for (const o of overlays) {
      const s = chart.addLineSeries({ color: col(o.color), lineWidth: 2, lineStyle: o.dashed ? 2 : 0, priceLineVisible: false, lastValueVisible: false, title: o.name });
      s.setData(o.data.map((v, i) => (v === null ? null : { time: tail[i]?.date as Time, value: v })).filter((x): x is { time: Time; value: number } => !!x && !!x.time));
    }
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [bars, overlays, height, theme]);
  return <div ref={ref} className="w-full" style={{ height }} />;
}

export function IndicatorChart({ dates, series, bands, height = 120 }: { dates: string[]; series: Overlay[]; bands?: number[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  useEffect(() => {
    if (!ref.current || !dates.length) return;
    const chart = baseChart(ref.current, height);
    series.forEach((o, idx) => {
      const s = chart.addLineSeries({ color: col(o.color), lineWidth: 2, priceLineVisible: false, lastValueVisible: idx === 0, title: o.name });
      s.setData(o.data.map((v, i) => (v === null ? null : { time: dates[i] as Time, value: v })).filter((x): x is { time: Time; value: number } => !!x));
      if (idx === 0) bands?.forEach((b) => s.createPriceLine({ price: b, color: css("--axis"), lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: "" }));
    });
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [dates, series, bands, height, theme]);
  return <div ref={ref} className="w-full" style={{ height }} />;
}

export function PerformanceChart({ points, height = 220 }: { points: { date: string; invested: number; market_value: number }[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const theme = useThemeVersion();
  useEffect(() => {
    if (!ref.current || points.length < 2) return;
    const chart = baseChart(ref.current, height);
    const v = chart.addAreaSeries({ lineColor: css("--s1"), topColor: css("--s1") + "33", bottomColor: css("--s1") + "00", lineWidth: 2, title: "Value" });
    v.setData(points.map((p) => ({ time: p.date as Time, value: p.market_value })));
    const i = chart.addLineSeries({ color: rgb("--muted"), lineWidth: 2, lineStyle: 2, title: "Invested", priceLineVisible: false });
    i.setData(points.map((p) => ({ time: p.date as Time, value: p.invested })));
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [points, height, theme]);
  if (points.length < 2)
    return <p className="text-sm text-muted">The performance history builds up from daily snapshots (taken after market close). Check back tomorrow.</p>;
  return <div ref={ref} className="w-full" style={{ height }} />;
}
