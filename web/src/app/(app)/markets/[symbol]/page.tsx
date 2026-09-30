"use client";

import { useParams } from "next/navigation";

import { ChartBlock, DataQualityNote, FundamentalPanel, FundPanel, TechnicalPanel } from "@/components/AnalysisPanels";
import LivePrice from "@/components/LivePrice";
import { ErrorNote, Section, Skeleton } from "@/components/ui";
import { assetLabel, titleCase } from "@/lib/format";
import { useAnalysis, useHistory, useNews } from "@/lib/queries";

export default function SymbolPage() {
  const symbol = decodeURIComponent(useParams<{ symbol: string }>().symbol);
  const a = useAnalysis(symbol);
  const h = useHistory(symbol, 730);
  const news = useNews(symbol);
  const inst = a.data?.instrument ?? h.data?.instrument;
  const t = a.data?.technical;
  const f = a.data?.fundamental;
  const verdict = t?.available && f?.available
    ? `${titleCase(f.verdict)} business (${f.score}/100), ${titleCase(t.verdict).toLowerCase()} technicals (${t.score}/100), ${t.trend}.`
    : t?.available ? `${titleCase(t.verdict)} technicals (${t.score}/100), ${t.trend}.` : null;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{inst?.name ?? symbol}</h1>
          <p className="text-sm text-ink2">{symbol} · {assetLabel[inst?.asset_type] ?? ""} {inst?.sector ? `· ${inst.sector}` : ""}</p>
        </div>
        <div className="text-2xl font-semibold"><LivePrice symbol={symbol} price={a.data?.quote?.price ?? null} prevClose={a.data?.quote?.prev_close} /></div>
      </div>
      <ErrorNote error={a.error ?? h.error} />
      {verdict && <p className="rounded-lg border border-line bg-surface px-3 py-2 text-sm"><b>Snapshot ·</b> {verdict} <span className="text-muted">Add it to a portfolio for a personalised action with sizing and tax timing.</span></p>}
      <div className="grid gap-4 xl:grid-cols-5">
        <div className="xl:col-span-3">{h.isLoading || a.isLoading ? <Skeleton className="h-96" /> : h.data && <ChartBlock bars={h.data.bars} analysis={a.data} />}</div>
        <div className="xl:col-span-2">{t && <TechnicalPanel t={t} />}</div>
      </div>
      <DataQualityNote dq={a.data?.data_quality} />
      <div className="grid gap-4 lg:grid-cols-2 2xl:grid-cols-3">
        {f && <FundamentalPanel f={f} />}
        {a.data?.fund && <FundPanel m={a.data.fund} />}
        {a.data?.risk?.available && (
          <Section title="Risk">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
              {[["Volatility", `${a.data.risk.volatility_pct}%`], ["CAGR", `${a.data.risk.cagr_pct}%`], ["Sharpe", a.data.risk.sharpe], ["Max drawdown", `${a.data.risk.max_drawdown_pct}%`],
                ["1-day VaR 95%", `${a.data.risk.var_95_1d_pct}%`], ["Beta vs NIFTY", a.data.market_beta?.beta ?? "—"]].map(([k, v]) => <div key={k} className="flex justify-between border-b border-line/60 py-1"><dt className="text-ink2">{k}</dt><dd className="tnum">{v}</dd></div>)}
            </dl>
          </Section>
        )}
      </div>
      {news.data && news.data.length > 0 && (
        <Section title="What's happening">
          <ul className="space-y-2 text-sm">{news.data.map((n: any, i: number) => (
            <li key={i}>{n.link ? <a className="hover:text-brand" href={n.link} target="_blank" rel="noreferrer">{n.title}</a> : n.title} <span className="text-xs text-muted">— {n.publisher}{n.published_at ? ` · ${String(n.published_at).slice(0, 10)}` : ""}</span></li>
          ))}</ul>
        </Section>
      )}
    </div>
  );
}
