"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";

import LivePrice from "@/components/LivePrice";
import ScopeSwitcher from "@/components/ScopeSwitcher";
import { PriceBanner } from "@/components/StatusBanners";
import { toast } from "@/components/Toast";
import { Delta, Empty, ErrorNote, Skeleton, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { assetLabel, inr, num, pct, titleCase, tone } from "@/lib/format";
import { prefetch, useHoldings, useRecs, useRunAdvisor } from "@/lib/queries";
import { useScopeState, useUrlState } from "@/lib/useUrlState";
import { SortTh, useSort } from "@/lib/useSort";
import type { Holding } from "@/lib/types";

export default function HoldingsPage() {
  const qc = useQueryClient();
  const [scope, setScope] = useScopeState();
  const [type, setType] = useUrlState("type", "all");
  const [q, setQ] = useUrlState("q", "");
  const { data, isLoading, error } = useHoldings(scope);
  const { data: recs } = useRecs({ status: "open,accepted,snoozed", include_hold: "true" });
  const verdict = useMemo(() => new Map((recs ?? []).filter((r) => r.instrument_id).map((r) => [`${r.profile_id}:${r.instrument_id}`, r])), [recs]);
  // search: every word must start a word in the name, symbol, owner, type or advisor verdict ("hdfc mid", "saroj etf", "switch")
  const words = q.toLowerCase().replace(/[^a-z0-9&.]+/g, " ").trim().split(" ").filter(Boolean);
  const hayOf = (h: Holding) => {
    const r = verdict.get(`${h.profile_id}:${h.instrument_id}`);
    return " " + [h.name, h.symbol, h.profile_name, assetLabel[h.asset_type] ?? h.asset_type, h.intent, r?.short_report?.verdict, r?.action,
      h.sip?.active ? "sip" : ""]
      .join(" ").toLowerCase().replace(/[^a-z0-9&.]+/g, " ") + " ";
  };
  const inView = (data?.holdings ?? []).filter((h) => h.quantity > 0 && (type === "all" || h.asset_type === type));
  // the whole phrase ("person b", "hdfc mid") wins when it matches; otherwise every word must start a word somewhere
  const phrase = " " + words.join(" ");
  const byPhrase = words.length > 1 ? inView.filter((h) => hayOf(h).includes(phrase)) : [];
  const rows = !words.length ? inView : byPhrase.length ? byPhrase : inView.filter((h) => { const hay = hayOf(h); return words.every((w) => hay.includes(" " + w)); });
  const total = rows.reduce((a, h) => a + h.market_value, 0);
  const invested = rows.reduce((a, h) => a + h.invested, 0);
  const gain = total - invested;
  const day = rows.reduce((a, h) => a + (h.day_change || 0), 0);
  const realised = rows.reduce((a, h) => a + (h.realised_pnl || 0), 0);
  const s = type === "all" && !words.length ? data?.summary : null;  // portfolio-level XIRR only for the unfiltered view
  const run = useRunAdvisor();
  const types = [...new Set((data?.holdings ?? []).map((h) => h.asset_type))];
  const [sorted, sort] = useSort(rows, {
    name: (h) => h.name, owner: (h) => h.profile_name, intent: (h) => h.intent, qty: (h) => h.quantity, avg: (h) => (h.price !== null ? h.avg_cost : null),
    invested: (h) => h.invested, price: (h) => h.price, value: (h) => h.market_value, today: (h) => h.day_change || null,
    gain: (h) => h.unrealised_pnl, returns: (h) => h.xirr_pct ?? h.unrealised_pct, weight: (h) => h.market_value,
    advisor: (h) => { const r = verdict.get(`${h.profile_id}:${h.instrument_id}`); return r ? (r.short_report?.verdict ?? r.action) : null; },
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Holdings</h1>
        <div className="flex flex-wrap gap-2">
          <div className="relative">
            <span aria-hidden className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted">⌕</span>
            <input type="search" className="input w-56 pl-7" placeholder="Search holdings…" value={q} onChange={(e) => setQ(e.target.value)}
                   onKeyDown={(e) => e.key === "Escape" && setQ("")} aria-label="Search holdings" />
          </div>
          <select className="input w-auto" value={type} onChange={(e) => setType(e.target.value)} aria-label="Asset type">
            <option value="all">All assets</option>{types.map((t) => <option key={t} value={t}>{assetLabel[t] ?? t}</option>)}
          </select>
          <ScopeSwitcher value={scope} onChange={setScope} />
        </div>
      </div>
      <ErrorNote error={error ?? run.error} />
      {data && <PriceBanner unpriced={data.unpriced_count} stale={data.stale_count} npsNavAsOf={data.nps_nav_as_of} />}
      {rows.length > 0 && (
        <div className={`grid gap-3 sm:grid-cols-2 lg:grid-cols-3 ${realised ? "xl:grid-cols-6" : "xl:grid-cols-5"}`}>
          <StatTile label="Invested" value={inr(invested, { compact: true })} sub={`${rows.length} holdings`} />
          <StatTile label="Current value" value={inr(total, { compact: true })} />
          <StatTile label="Total gain" value={<span className={tone(gain)}>{inr(gain, { compact: true })}</span>} delta={invested ? (gain / invested) * 100 : null} />
          <StatTile label="Today" value={<span className={tone(day)}>{inr(day)}</span>} delta={total - day ? (day / (total - day)) * 100 : null} />
          <StatTile label={s?.xirr_pct != null ? "XIRR (annualised)" : "Returns"} value={s?.xirr_pct != null ? pct(s.xirr_pct, 1) : pct(invested ? (gain / invested) * 100 : null, 1)}
                    sub={s?.xirr_pct != null ? "since first investment" : "absolute"} hint="XIRR is shown once money has been invested for at least a year (AMFI convention); before that, absolute return." />
          {realised ? <StatTile label="Realised gains" value={<span className={tone(realised)}>{inr(realised, { compact: true })}</span>} sub="from sales so far" /> : null}
        </div>
      )}
      {isLoading ? <Skeleton className="h-64" /> : rows.length === 0 ? <Empty title={words.length ? `No holdings match “${q.trim()}”` : "No holdings in this view"} /> : (
        <div className="card max-h-[calc(100vh-9rem)] overflow-auto">
          <table className="w-full [&_td.tnum]:whitespace-nowrap [&_td.text-right]:whitespace-nowrap">
            <thead className="sticky top-0 z-[1] border-b border-line bg-surface"><tr>
              <SortTh s={sort} k="name" className="th sticky left-0 bg-surface">Holding</SortTh><SortTh s={sort} k="owner">Owner</SortTh>
              <SortTh s={sort} k="intent" className="th hidden min-[1800px]:table-cell">Intent</SortTh>
              <SortTh s={sort} k="qty" className="th text-right">Qty</SortTh><SortTh s={sort} k="avg" className="th hidden text-right md:table-cell">Avg cost</SortTh>
              <SortTh s={sort} k="invested" className="th text-right">Invested</SortTh>
              <SortTh s={sort} k="price" className="th text-right">Price</SortTh><SortTh s={sort} k="value" className="th text-right">Current value</SortTh>
              <SortTh s={sort} k="today" className="th hidden text-right xl:table-cell">Today</SortTh>
              <SortTh s={sort} k="gain" className="th text-right">Gain</SortTh>
              <SortTh s={sort} k="returns" className="th hidden text-right md:table-cell" title="XIRR once held ≥ 1 year; absolute return before that">Returns</SortTh>
              <SortTh s={sort} k="weight" className="th hidden text-right min-[1800px]:table-cell">Weight</SortTh><SortTh s={sort} k="advisor">Advisor</SortTh>
            </tr></thead>
            <tbody>
              {sorted.map((h) => {
                const r = verdict.get(`${h.profile_id}:${h.instrument_id}`);
                return (
                  <tr key={`${h.profile_id}-${h.instrument_id}`} className="border-t border-line hover:bg-page" onMouseEnter={() => h.price !== null && prefetch.holding(qc, h.symbol)}>
                    <td className="td sticky left-0 min-w-[13rem] max-w-[22rem] bg-surface">
                      <Link className="font-medium hover:text-brand" href={`/holdings/${h.instrument_id}?profile=${h.profile_id}&symbol=${encodeURIComponent(h.symbol)}`}>{h.name}</Link>
                      <div className="text-xs text-muted">{assetLabel[h.asset_type]} · {h.symbol}
                        {h.sip && <SipChip h={h} />}</div>
                    </td>
                    <td className="td whitespace-nowrap text-ink2">{h.profile_name}</td>
                    <td className="td hidden text-xs min-[1800px]:table-cell">{h.intent ? <span className="chip bg-page">{titleCase(h.intent)}{h.intent_source === "auto" ? " (auto)" : ""}</span> : "—"}</td>
                    <td className="td tnum text-right">{num(h.quantity, 3)}</td>
                    <td className="td tnum hidden text-right md:table-cell">{h.price !== null ? `₹${num(h.avg_cost)}` : "—"}</td>
                    <td className="td tnum text-right">{inr(h.invested)}</td>
                    <td className="td text-right">{h.price !== null ? (
                      <div>
                        <LivePrice symbol={h.symbol} price={h.price} prevClose={h.prev_close} />
                        {h.price_status === "nav_statement" ? (
                          <div className="text-[0.7rem] text-muted" title="NPS NAV from your imported statement">NAV {h.price_as_of ? new Date(h.price_as_of).toLocaleDateString("en-IN", { day: "numeric", month: "short" }) : ""}</div>
                        ) : h.price_status && h.price_status !== "live" && (
                          <div className="text-[0.7rem] text-warn" title={`Price as of ${h.price_as_of ?? "unknown"}`}>{h.price_status === "statement" ? "statement price" : "last known"}</div>
                        )}
                      </div>
                    ) : <span className={`chip ${h.priced ? "bg-page text-muted" : "bg-down/15 text-down"}`}>{h.priced ? "accrual" : "no price"}</span>}</td>
                    <td className="td tnum text-right font-medium">{inr(h.market_value)}</td>
                    <td className={`td tnum hidden text-right xl:table-cell ${tone(h.day_change)}`}>{h.day_change ? inr(h.day_change) : "—"}</td>
                    <td className="td text-right"><div className={`tnum ${tone(h.unrealised_pnl)}`}>{inr(h.unrealised_pnl)}</div><Delta v={h.unrealised_pct} /></td>
                    <td className="td tnum hidden text-right md:table-cell"><Returns h={h} /></td>
                    <td className="td tnum hidden text-right text-ink2 min-[1800px]:table-cell">{total ? ((h.market_value / total) * 100).toFixed(1) + "%" : "—"}</td>
                    <td className="td">{r ? <Link href={`/advisor/${r.id}`} className="chip bg-page hover:text-brand" title={`${Math.round(r.confidence * 100)}% confidence`}>{r.short_report?.verdict ?? r.action} · {Math.round(r.confidence * 100)}%</Link>
                      : <button className="chip bg-brand/10 text-brand hover:bg-brand/20 disabled:opacity-60" disabled={run.isPending}
                                onClick={() => run.mutate({ profile_id: h.profile_id, instrument_ids: [h.instrument_id], label: h.symbol })}
                                title={`Analyse only ${h.symbol}`}>{run.isPending && run.variables && typeof run.variables !== "string" && run.variables.label === h.symbol ? "Analysing…" : "Run advisor"}</button>}</td>
                  </tr>
                );
              })}
            </tbody>
            <tfoot className="sticky bottom-0 border-t-2 border-line bg-surface font-semibold"><tr>
              <td className="td sticky left-0 bg-surface">Total</td><td className="td" /><td className="td hidden min-[1800px]:table-cell" /><td className="td" /><td className="td hidden md:table-cell" />
              <td className="td tnum text-right">{inr(invested)}</td><td className="td" /><td className="td tnum text-right">{inr(total)}</td>
              <td className={`td tnum hidden text-right xl:table-cell ${tone(day)}`}>{inr(day)}</td>
              <td className="td text-right"><div className={`tnum ${tone(gain)}`}>{inr(gain)}</div><Delta v={invested ? (gain / invested) * 100 : null} /></td>
              <td className="td tnum hidden text-right md:table-cell">{s?.xirr_pct != null ? pct(s.xirr_pct, 1) : "—"}</td><td className="td hidden min-[1800px]:table-cell" /><td className="td" />
            </tr></tfoot>
          </table>
        </div>
      )}
    </div>
  );
}

function Returns({ h }: { h: Holding }) {
  if (h.xirr_pct != null) return <span title="XIRR — annualised, accounts for when each rupee went in" className={tone(h.xirr_pct)}>{pct(h.xirr_pct, 1)} <span className="text-xs text-muted">p.a.</span></span>;
  if (h.unrealised_pct == null) return <>—</>;
  return <span title="Held under a year: absolute return (not annualised)" className={tone(h.unrealised_pct)}>{pct(h.unrealised_pct, 1)} <span className="text-xs text-muted">abs</span></span>;
}

const day = (d: string) => new Date(d).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });

/** Is a SIP running in this fund? Green when known; "SIP ?" when the purchase dates are unknown (holdings file).
 *  Click to answer yes / no yourself — your answer wins, and the advisor's fund plan uses it too. */
function SipChip({ h }: { h: Holding }) {
  const qc = useQueryClient();
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);  // the menu floats above the table (sticky cells)
  const open = pos !== null;
  const setOpen = (v: boolean) => !v && setPos(null);
  useEffect(() => {
    if (!open) return;
    const opened = Date.now();
    const close = () => setPos(null);
    const onScroll = () => { if (Date.now() - opened > 400) setPos(null); };  // ignore the scroll that brought the row into view
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    document.addEventListener("click", close);
    return () => { window.removeEventListener("scroll", onScroll, true); window.removeEventListener("resize", close); document.removeEventListener("click", close); };
  }, [open]);
  const save = useMutation({
    mutationFn: (sip: boolean | null) => api("/holding-prefs", { method: "PUT", body: { profile_id: h.profile_id, instrument_id: h.instrument_id, sip } }),
    onSuccess: (_, sip) => {
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["holdings"] });
      toast("ok", sip === null ? "Back to automatic" : sip ? "Marked: a SIP runs here" : "Marked: no SIP here",
            "The advisor uses this on its next run (fund plan: where new money goes).");
    },
    onError: (e: Error) => toast("error", "Couldn't save", e.message),
  });
  const s = h.sip!;
  if (s.active === false && s.source !== "you") return null;  // the statement's dates say no SIP
  const label = s.active === null ? "SIP ?" : "SIP";
  const title = s.source === "you" ? (s.active ? "You marked: a SIP runs in this fund" : "You marked: no SIP in this fund")
    : s.active === null ? "Purchase dates unknown (imported from a broker's holdings file) — so a SIP can't be seen. Import your CAMS / KFintech CAS, or click to tell us."
    : `Active SIP — last purchase ${s.last_buy ? day(s.last_buy) : "recently"}${s.monthly ? ", buying every month" : ""} (from your statement's purchase dates)`;
  return (
    <span className="ml-1.5 inline-block">
      <button type="button" title={title} onClick={(e) => {
        e.preventDefault(); e.stopPropagation();
        if (open) return setPos(null);
        const r = e.currentTarget.getBoundingClientRect();
        setPos({ top: r.bottom + 4, left: Math.min(r.left, window.innerWidth - 240) });
      }}
              className={`chip font-semibold ${s.active ? "bg-up/15 text-up" : s.active === false ? "bg-page text-muted line-through" : "bg-page text-muted"}`}>
        {label}{s.source === "you" ? " ✓" : ""}
      </button>
      {open && createPortal(
        <span style={{ position: "fixed", top: pos.top, left: pos.left }} onClick={(e) => e.stopPropagation()}
              className="z-50 flex w-56 flex-col rounded-lg border border-line bg-surface p-1 text-xs text-ink shadow-lg">
          <span className="px-2 py-1 text-muted">Is a SIP running in this fund?</span>
          {([[true, "Yes, a SIP runs here"], [false, "No SIP"], [null, "Work it out from my statements"]] as const).map(([v, t]) => (
            <button key={String(v)} type="button" disabled={save.isPending} onClick={() => save.mutate(v)}
                    className="rounded px-2 py-1 text-left hover:bg-page">{t}</button>
          ))}
        </span>,
        document.body,
      )}
    </span>
  );
}
