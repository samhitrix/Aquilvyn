"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useParams, useSearchParams } from "next/navigation";
import { useEffect, useState } from "react";

import { ChartBlock, DataQualityNote, FundamentalPanel, FundPanel, TechnicalPanel } from "@/components/AnalysisPanels";
import LivePrice from "@/components/LivePrice";
import RecCard from "@/components/RecCard";
import { Delta, ErrorNote, Section, Skeleton, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { assetLabel, inr, num, pct, titleCase } from "@/lib/format";
import { useHoldings } from "@/lib/queries";
import type { Recommendation, Txn } from "@/lib/types";
import { SortTh, useSort } from "@/lib/useSort";

export default function HoldingPage() {
  const { instrumentId } = useParams<{ instrumentId: string }>();
  const sp = useSearchParams();
  const profileId = sp.get("profile") ?? undefined;
  const symbol = sp.get("symbol") ?? undefined;
  const { data, isLoading, error } = useQuery({
    queryKey: ["holding", instrumentId, profileId ?? null, symbol ?? null],
    queryFn: () => api<any>(`/dashboard/holding/${instrumentId}`, { query: { profile_id: profileId, symbol } }),
    staleTime: 30_000,
  });
  const { data: hold } = useHoldings(profileId ? { type: "profile", id: profileId } : { type: "household" });
  const row = hold?.holdings.find((h) => h.instrument_id === instrumentId && (!profileId || h.profile_id === profileId));
  const recs: Recommendation[] = data?.recommendations ?? [];
  const current = recs.find((r) => r.status !== "superseded");
  const a = data?.analysis;
  const gainOf = (l: { qty: number; cost: number }) => (row && row.price !== null ? (row.price - l.cost) * l.qty : null);
  const [lots, sort] = useSort(row?.lots ?? [], { bought: (l) => l.buy_date, qty: (l) => l.qty, cost: (l) => l.cost, gain: gainOf }, "lots_sort");

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{row?.name ?? a?.instrument?.name ?? symbol}</h1>
          <p className="text-sm text-ink2">{symbol} · {assetLabel[row?.asset_type ?? a?.instrument?.asset_type] ?? ""} · {row?.profile_name}</p>
        </div>
        {symbol && row?.price !== null && <div className="text-2xl font-semibold"><LivePrice symbol={symbol} price={row?.price ?? a?.quote?.price ?? null} prevClose={row?.prev_close ?? a?.quote?.prev_close} /></div>}
      </div>
      <ErrorNote error={error} />
      {row && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Value" value={inr(row.market_value)} sub={`${num(row.quantity, 3)} units @ ₹${num(row.avg_cost)}`} />
          <StatTile label="Unrealised gain" value={inr(row.unrealised_pnl)} delta={row.unrealised_pct} />
          <StatTile label="XIRR" value={pct(row.xirr_pct, 1, false)} sub={`${row.holding_days ?? 0} days held`} />
          <StatTile label="Realised + dividends" value={inr(row.realised_pnl + row.dividends)} />
        </div>
      )}
      {current ? <RecCard rec={current} /> : <Section title="Advisor"><p className="text-sm text-ink2">No verdict yet — run the advisor from the dashboard.</p></Section>}
      {isLoading ? <Skeleton className="h-80" /> : (
        <>
          <div className="grid gap-4 xl:grid-cols-5">
            <div className="xl:col-span-3">{data?.history?.bars?.length > 0 && a && <ChartBlock bars={data.history.bars} analysis={a} />}</div>
            <div className="xl:col-span-2">{a?.technical && <TechnicalPanel t={a.technical} />}</div>
          </div>
          <DataQualityNote dq={a?.data_quality} />
          <div className="grid gap-4 lg:grid-cols-2">
            {a?.fundamental && <FundamentalPanel f={a.fundamental} />}
            {a?.fund && <FundPanel m={a.fund} />}
          </div>
        </>
      )}
      {profileId && <IntentForm profileId={profileId} instrumentId={instrumentId} initial={data?.prefs} />}
      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Tax lots (FIFO)">
          <table className="w-full text-sm">
            <thead><tr><SortTh s={sort} k="bought">Bought</SortTh><SortTh s={sort} k="qty" className="th text-right">Qty</SortTh>
              <SortTh s={sort} k="cost" className="th text-right">Cost</SortTh><SortTh s={sort} k="gain" className="th text-right">Gain</SortTh></tr></thead>
            <tbody>{lots.map((l, i) => {
              const g = gainOf(l);
              return <tr key={i} className="border-t border-line"><td className="td">{l.buy_date}</td><td className="td tnum text-right">{num(l.qty, 3)}</td><td className="td tnum text-right">₹{num(l.cost)}</td><td className="td tnum text-right">{inr(g)}</td></tr>;
            })}</tbody>
          </table>
        </Section>
        <Section title="Transactions">
          <ul className="divide-y divide-line text-sm">
            {(data?.transactions as Txn[] | undefined)?.map((t) => (
              <li key={t.id} className="flex justify-between py-2"><span>{t.trade_date} · <span className="capitalize">{t.txn_type}</span></span><span className="tnum text-ink2">{num(t.quantity, 3)} @ ₹{num(t.price)} · {inr(t.amount)}</span></li>
            ))}
          </ul>
        </Section>
      </div>
      {recs.length > 1 && (
        <Section title="History of calls on this holding">
          <ul className="space-y-1 text-sm">{recs.map((r) => <li key={r.id} className="flex gap-2"><span className="text-muted">{r.created_at.slice(0, 10)}</span><b>{r.action}</b><span className="text-ink2">{r.status}</span><span className="text-ink2">{r.change_reason}</span></li>)}</ul>
        </Section>
      )}
      {data?.news?.length > 0 && (
        <Section title="What's happening">
          <ul className="space-y-2 text-sm">{data.news.map((n: any, i: number) => (
            <li key={i}>{n.link ? <a className="hover:text-brand" href={n.link} target="_blank" rel="noreferrer">{n.title}</a> : n.title} <span className="text-xs text-muted">— {n.publisher}</span></li>
          ))}</ul>
        </Section>
      )}
    </div>
  );
}

function IntentForm({ profileId, instrumentId, initial }: { profileId: string; instrumentId: string; initial: any }) {
  const qc = useQueryClient();
  const [f, setF] = useState({ intent: "", thesis: "", drawdown_threshold_pct: "", stop_price: "", target_price: "" });
  useEffect(() => {
    if (initial) setF({ intent: initial.intent ?? "", thesis: initial.thesis ?? "", drawdown_threshold_pct: initial.drawdown_threshold_pct ?? "", stop_price: initial.stop_price ?? "", target_price: initial.target_price ?? "" });
  }, [initial]);
  const save = useMutation({
    mutationFn: () => api("/holding-prefs", { method: "PUT", body: {
      profile_id: profileId, instrument_id: instrumentId, intent: f.intent || null, thesis: f.thesis,
      drawdown_threshold_pct: f.drawdown_threshold_pct ? Number(f.drawdown_threshold_pct) : null,
      stop_price: f.stop_price ? Number(f.stop_price) : null, target_price: f.target_price ? Number(f.target_price) : null } }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ["holding", instrumentId] }); qc.invalidateQueries({ queryKey: ["holdings"] }); },
  });
  return (
    <Section title="Your plan for this holding" action={<span className="text-xs text-muted">The advisor re-analyses when you save</span>}>
      <div className="grid gap-3 md:grid-cols-5">
        <div><label className="label">Intent</label>
          <select className="input" value={f.intent} onChange={(e) => setF({ ...f, intent: e.target.value })}>
            <option value="">Auto-detect</option>{["core", "satellite", "trade", "retirement", "goal"].map((i) => <option key={i} value={i}>{titleCase(i)}</option>)}
          </select></div>
        <div><label className="label">Drawdown alert %</label><input className="input" inputMode="decimal" value={f.drawdown_threshold_pct} onChange={(e) => setF({ ...f, drawdown_threshold_pct: e.target.value })} placeholder="default by intent" /></div>
        <div><label className="label">Stop price (trades)</label><input className="input" inputMode="decimal" value={f.stop_price} onChange={(e) => setF({ ...f, stop_price: e.target.value })} /></div>
        <div><label className="label">Target price</label><input className="input" inputMode="decimal" value={f.target_price} onChange={(e) => setF({ ...f, target_price: e.target.value })} /></div>
        <div className="flex items-end"><button className="btn-primary w-full" onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending ? "Saving…" : save.isSuccess ? "Saved ✔" : "Save plan"}</button></div>
      </div>
      <div className="mt-3"><label className="label">Why do you own this? (thesis)</label><textarea className="input min-h-[70px]" value={f.thesis} onChange={(e) => setF({ ...f, thesis: e.target.value })} placeholder="e.g. Market leader, ROE > 20%, debt-free, 15% revenue growth" /></div>
      <ErrorNote error={save.error} />
      <p className="mt-2 text-xs text-muted">CORE = long-term compounder (fundamentals-weighted) · SATELLITE = medium-term · TRADE = short-term with stop/target (technicals-weighted).</p>
    </Section>
  );
}
