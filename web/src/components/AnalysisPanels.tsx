"use client";
/** Technical + fundamental + fund panels shared by the holding deep-dive and the stock page. */
import { IndicatorChart, PriceChart } from "@/components/charts";
import { Section } from "@/components/ui";
import { num, pct, titleCase } from "@/lib/format";
import type { Bar } from "@/lib/types";

const cssVar = (n: string) => n; // resolved inside the chart so colours follow the theme

export function ChartBlock({ bars, analysis }: { bars: Bar[]; analysis: any }) {
  const series = analysis?.technical?.series;
  const recent = bars.slice(-400);
  const dates = recent.slice(-(series?.rsi?.length ?? 0)).map((b) => b.date);
  const overlays = series
    ? [
        { name: "SMA 50", data: series.sma50, color: cssVar("--s2") },
        { name: "SMA 200", data: series.sma200, color: cssVar("--s7") },
        { name: "BB upper", data: series.bb_upper, color: cssVar("--axis"), dashed: true },
        { name: "BB lower", data: series.bb_lower, color: cssVar("--axis"), dashed: true },
      ]
    : [];
  return (
    <Section accent="brand" title="Price chart" action={<span className="text-xs text-muted">SMA 50 · SMA 200 · Bollinger (20,2)</span>}>
      <PriceChart bars={recent} overlays={overlays} />
      {series && (
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          <div><div className="text-xs text-muted">RSI (14) — 70 overbought / 30 oversold</div><IndicatorChart dates={dates} series={[{ name: "RSI", data: series.rsi, color: cssVar("--s1") }]} bands={[30, 70]} /></div>
          <div><div className="text-xs text-muted">MACD (12,26,9)</div><IndicatorChart dates={dates} series={[{ name: "MACD", data: series.macd, color: cssVar("--s1") }, { name: "Signal", data: series.macd_signal, color: cssVar("--s2") }]} bands={[0]} /></div>
        </div>
      )}
    </Section>
  );
}

export function TechnicalPanel({ t }: { t: any }) {
  if (!t?.available) return <Section accent="brand" title="Technical analysis"><p className="text-sm text-muted">{t?.reason ?? "Not available"}</p></Section>;
  const v = t.values;
  return (
    <Section accent="brand" title="Technical analysis" action={<span className="text-sm">Score <b>{t.score}</b>/100 · {titleCase(t.verdict)} · {t.trend}</span>}>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
        {[["RSI 14", num(v.rsi14, 1)], ["50-DMA", num(v.sma50)], ["200-DMA", num(v.sma200)], ["ATR %", pct(v.atr_pct, 2, false)], ["From 52w high", pct(v.pct_from_52w_high)],
          ["1M", pct(v.return_1m_pct)], ["3M", pct(v.return_3m_pct)], ["1Y", pct(v.return_1y_pct)], ["RS vs bench (3M)", v.relative_strength_3m_pct != null ? `${v.relative_strength_3m_pct > 0 ? "+" : ""}${v.relative_strength_3m_pct} pp` : "—"]]
          .map(([k, val]) => <div key={k} className="flex justify-between border-b border-line/60 py-1"><dt className="text-ink2">{k}</dt><dd className="tnum">{val}</dd></div>)}
      </dl>
      <ul className="mt-3 space-y-1 text-sm">
        {t.signals.map((s: any) => (
          <li key={s.code} className="flex gap-2"><span aria-hidden className={s.bias > 0 ? "text-up" : "text-down"}>{s.bias > 0 ? "▲" : "▼"}</span><span>{s.label}</span><span className="text-ink2">— {s.evidence}</span></li>
        ))}
      </ul>
      {t.levels && <p className="mt-2 text-sm text-ink2">Support {t.levels.support.map((x: number) => `₹${num(x)}`).join(", ") || "—"} · Resistance {t.levels.resistance.map((x: number) => `₹${num(x)}`).join(", ") || "—"}</p>}
    </Section>
  );
}

export function FundamentalPanel({ f }: { f: any }) {
  if (!f?.available) return null;
  return (
    <Section accent="brand" title="Fundamental analysis" action={<span className="text-sm">Score <b>{f.score}</b>/100 · {titleCase(f.verdict)}</span>}>
      <div className="mb-3 flex flex-wrap gap-2 text-xs">
        {Object.entries(f.pillars).map(([k, p]: any) => p.score != null && <span key={k} className="chip bg-page">{titleCase(k)} {Math.round(p.score)}</span>)}
      </div>
      <table className="w-full text-sm">
        <thead><tr><th className="th">Metric</th><th className="th text-right">Value</th><th className="th">Judged against</th><th className="th text-right">Status</th></tr></thead>
        <tbody>
          {f.metrics.map((m: any) => (
            <tr key={m.key} className="border-t border-line">
              <td className="td">{m.label}</td><td className="td tnum text-right">{m.value}{m.unit === "%" ? "%" : m.unit === "x" ? "x" : ` ${m.unit}`}</td>
              <td className="td text-xs text-muted">{m.threshold}</td>
              <td className={`td text-right text-xs font-medium ${m.status === "good" ? "text-up" : m.status === "weak" ? "text-down" : "text-ink2"}`}>{m.status === "good" ? "✔ good" : m.status === "weak" ? "✖ weak" : "• ok"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {f.red_flags?.length > 0 && <ul className="mt-3 space-y-1 text-sm text-down">{f.red_flags.map((r: any) => <li key={r.code}>⚠ {r.message}</li>)}</ul>}
      <p className="mt-2 text-xs text-muted">Source {f.source} · as of {f.as_of?.slice(0, 10)} · coverage {Math.round((f.coverage ?? 0) * 100)}%</p>
    </Section>
  );
}

export function FundPanel({ m }: { m: any }) {
  if (!m?.available) return null;
  return (
    <Section accent="brand" title="Fund analysis" action={<span className="text-sm">Score <b>{m.score ?? "—"}</b>/100 · {titleCase(m.verdict)}</span>}>
      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm sm:grid-cols-3">
        {[["Category", titleCase(m.category)], ["Plan", titleCase(m.plan)], ["Consistency", m.consistency_pct != null ? `${m.consistency_pct}% of periods beat benchmark` : "—"],
          ["Alpha", pct(m.alpha_pct)], ["Beta", num(m.beta)], ["Downside capture", m.downside_capture_pct != null ? `${m.downside_capture_pct}%` : "—"],
          ["CAGR", pct(m.risk?.cagr_pct)], ["Sharpe", num(m.risk?.sharpe)], ["Max drawdown", pct(m.risk?.max_drawdown_pct)],
          ["Expense ratio", m.cost?.expense_ratio_pct != null ? `${m.cost.expense_ratio_pct}% (typical ${m.cost.category_typical_pct}%)` : "—"]]
          .map(([k, val]) => <div key={k} className="flex justify-between gap-2 border-b border-line/60 py-1"><dt className="text-ink2">{k}</dt><dd className="tnum text-right">{val}</dd></div>)}
      </dl>
      {Object.keys(m.rolling ?? {}).length > 0 && (
        <table className="mt-3 w-full text-sm">
          <thead><tr><th className="th">Rolling</th><th className="th text-right">Fund avg</th><th className="th text-right">Benchmark avg</th><th className="th text-right">Worst</th><th className="th text-right">Beat bench</th></tr></thead>
          <tbody>{Object.entries(m.rolling).map(([k, r]: any) => (
            <tr key={k} className="border-t border-line"><td className="td">{k}</td><td className="td tnum text-right">{pct(r.fund_avg_pct)}</td><td className="td tnum text-right">{pct(r.bench_avg_pct)}</td><td className="td tnum text-right">{pct(r.fund_min_pct)}</td><td className="td tnum text-right">{r.beat_benchmark_pct}%</td></tr>
          ))}</tbody>
        </table>
      )}
    </Section>
  );
}

export function DataQualityNote({ dq }: { dq: any }) {
  if (!dq) return null;
  return (
    <div className="text-xs text-ink2">
      Data quality <b>{dq.grade}</b> ({dq.score}/100){dq.issues?.length ? " — " + dq.issues.map((i: any) => i.message).join("; ") : ""}
    </div>
  );
}
