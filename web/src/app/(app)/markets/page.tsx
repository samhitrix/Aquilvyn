"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useRouter } from "next/navigation";

import InstrumentSearch from "@/components/InstrumentSearch";
import LivePrice from "@/components/LivePrice";
import { Delta, Section, Skeleton } from "@/components/ui";
import { num } from "@/lib/format";
import { prefetch, useOverview } from "@/lib/queries";
import type { Quote } from "@/lib/types";

export default function MarketsPage() {
  const { data, isLoading } = useOverview();
  const router = useRouter();
  const qc = useQueryClient();
  const row = (q: Quote) => (
    <li key={q.symbol} onMouseEnter={() => prefetch.holding(qc, q.symbol)}>
      <Link href={`/markets/${encodeURIComponent(q.symbol)}`} className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 hover:bg-page">
        <span className="truncate">{q.name ?? q.symbol}</span><LivePrice symbol={q.symbol} price={q.price} prevClose={q.prev_close} />
      </Link>
    </li>
  );
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Markets</h1>
        <div className="w-full max-w-md"><InstrumentSearch onPick={(p) => router.push(`/markets/${encodeURIComponent(p.symbol)}`)} placeholder="Analyse any stock / ETF / mutual fund…" /></div>
      </div>
      {isLoading ? <Skeleton className="h-64" /> : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-6">
            {data?.indices.slice(0, 6).map((i, idx) => (
              <Link key={i.symbol} href={`/markets/${encodeURIComponent(i.symbol)}`} className={`card p-4 hover:bg-page ${idx >= 4 ? "hidden 2xl:block" : ""}`}>
                <div className="text-xs text-ink2">{i.name}</div>
                <div className="mt-1 text-xl font-semibold"><LivePrice symbol={i.symbol} price={i.price} prevClose={i.prev_close} showChange={false} /></div>
                <Delta v={i.change_pct} />
              </Link>
            ))}
          </div>
          <div className="grid gap-4 lg:grid-cols-3 2xl:grid-cols-4">
            <Section title="Top gainers"><ul className="text-sm">{data?.gainers.map(row)}</ul></Section>
            <Section title="Top losers"><ul className="text-sm">{data?.losers.map(row)}</ul></Section>
            <Section title="Most active (volume)" className="hidden 2xl:block"><ul className="text-sm">{data?.most_active?.map(row)}</ul></Section>
            <Section title="Sectors today">
              <ul className="space-y-1.5 text-sm">{data?.sectors.map((s) => (
                <li key={s.symbol} className="flex items-center gap-2">
                  <span className="w-40 shrink-0 truncate text-ink2">{s.sector}</span>
                  <span className="relative h-2 flex-1 rounded bg-line/50">
                    <span className="absolute top-0 h-2 rounded" style={{ left: s.change_pct >= 0 ? "50%" : `${50 + Math.max(-50, s.change_pct * 10)}%`, width: `${Math.min(50, Math.abs(s.change_pct) * 10)}%`, background: s.change_pct >= 0 ? "rgb(var(--up))" : "rgb(var(--down))" }} />
                  </span>
                  <Delta v={s.change_pct} />
                </li>
              ))}</ul>
              {data?.vix && <p className="mt-3 text-sm text-ink2">India VIX <b className="tnum">{num(data.vix.price)}</b> <Delta v={data.vix.change_pct} /> — higher = more fear</p>}
            </Section>
          </div>
        </>
      )}
    </div>
  );
}
