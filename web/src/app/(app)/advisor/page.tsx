"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { ReadinessPanel } from "@/components/Readiness";
import RecCard from "@/components/RecCard";
import { AIBanner } from "@/components/StatusBanners";
import { AutoHide, Empty, ErrorNote, Section, Skeleton, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { inr, titleCase } from "@/lib/format";
import { qk, useAdvisorSummary, useProfiles, useRecs, useRunAdvisor } from "@/lib/queries";
import type { AdvisorRunRow, AdvisorSummary } from "@/lib/types";
import { useUrlState } from "@/lib/useUrlState";
import { SortTh, useSort } from "@/lib/useSort";

const TABS = ["inbox", "all verdicts", "decided", "shadow portfolio", "scorecard"] as const;
type RunFilter = { asset_types?: string[]; other_assets?: boolean };
const ASSETS: Record<string, { label: string; noun: string; match: (t: string | null) => boolean; run: RunFilter }> = {
  all: { label: "All assets", noun: "holdings", match: () => true, run: {} },
  mf: { label: "Mutual funds", noun: "mutual funds", match: (t) => t === "mutual_fund", run: { asset_types: ["mutual_fund"] } },
  stocks: { label: "Stocks & ETFs", noun: "stocks & ETFs", match: (t) => t === "stock" || t === "etf", run: { asset_types: ["stock", "etf"] } },
  other: { label: "NPS / EPF / PPF / others", noun: "NPS / EPF / PPF / other holdings", match: (t) => !!t && !["mutual_fund", "stock", "etf"].includes(t), run: { other_assets: true } },
  // portfolio-level findings (rebalance, concentration) need the whole portfolio, so this runs everything
  portfolio: { label: "Portfolio-level (rebalance, concentration)", noun: "holdings (whole portfolio)", match: (t) => t === null, run: {} },
};

const TRIGGER: Record<string, string> = { manual: "you clicked Run", nightly: "daily run" };
const trig = (t: string) => TRIGGER[t] ?? (t.startsWith("event:") ? `after ${t.slice(6).replace(/[._]/g, " ")}` : t);
const RUN_TONE: Record<string, string> = { done: "bg-up/15 text-up", skipped: "bg-warn/15 text-warn", failed: "bg-down/15 text-down", running: "bg-brand/15 text-brand" };

/** When the advisor runs by itself, and how the recent runs went (with the reason when one failed). */
function RunSchedule({ schedule }: { schedule?: AdvisorSummary["schedule"] }) {
  const [open, setOpen] = useState(false);
  const runs = useQuery({ queryKey: ["advisor-runs"], queryFn: () => api<AdvisorRunRow[]>("/advisor/runs?limit=15"), enabled: open, refetchInterval: open ? 15_000 : false });
  const [runRows, sort] = useSort(runs.data ?? [], {
    started: (r) => r.started_at, why: (r) => trig(r.trigger), status: (r) => r.status, holdings: (r) => r.holdings, details: (r) => r.error ?? "",
  }, "runs_sort");
  if (!schedule) return null;
  const l = schedule.latest;
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <b>Runs automatically</b>
        <span className="text-ink2">{schedule.daily} · next: <b className="text-ink">{new Date(schedule.next_daily_run).toLocaleString("en-IN", { weekday: "short", day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}</b> · also {schedule.also}.</span>
        {l && <span className={`chip ${RUN_TONE[l.status] ?? "bg-line"}`}>last: {l.status} ({trig(l.trigger)}){l.finished_at ? ` · ${new Date(l.finished_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}` : ""}</span>}
        <button className="ml-auto text-xs text-brand" onClick={() => setOpen(!open)}>{open ? "Hide run history" : "Run history"}</button>
      </div>
      {l?.status !== "done" && l?.error && <p className="mt-1 text-xs text-down">Why: {l.error}</p>}
      {open && (
        <table className="mt-2 w-full text-xs">
          <thead><tr><SortTh s={sort} k="started">Started</SortTh><SortTh s={sort} k="why">Why it ran</SortTh><SortTh s={sort} k="status">Result</SortTh>
            <SortTh s={sort} k="holdings" className="th text-right">Holdings</SortTh><SortTh s={sort} k="details">Details</SortTh></tr></thead>
          <tbody>{runRows.map((r) => (
            <tr key={r.id} className="border-t border-line">
              <td className="td whitespace-nowrap">{new Date(r.started_at).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}</td>
              <td className="td">{trig(r.trigger)}{r.scope.startsWith("profile:") ? " · one person" : ""}{r.scope.includes("|") ? ` · ${r.scope.split("|")[1].replace(/_/g, " ").replace(/,/g, ", ")} only` : ""}</td>
              <td className="td"><span className={`chip ${RUN_TONE[r.status] ?? "bg-line"}`}>{r.status}</span></td>
              <td className="td tnum text-right">{r.holdings ?? "—"}</td>
              <td className="td text-ink2">{r.error ?? (r.status === "done" ? `${r.new ?? 0} new, ${r.updated ?? 0} updated` : "")}</td>
            </tr>))}</tbody>
        </table>
      )}
    </div>
  );
}

export default function AdvisorPage() {
  const [tab, setTab] = useUrlState<(typeof TABS)[number]>("tab", "inbox");
  const [profile, setProfile] = useUrlState<string>("person", "");
  const [assetRaw, setAsset] = useUrlState<string>("asset", "all");
  const asset = assetRaw in ASSETS ? assetRaw : "all";  // an old or mistyped link must not break the page
  const { data: profiles } = useProfiles();
  const summary = useAdvisorSummary();
  const run = useRunAdvisor();
  const filters: Record<string, string | undefined> =
    tab === "inbox" ? { status: "open", profile_id: profile || undefined }
    : tab === "all verdicts" ? { status: "open,accepted,snoozed,dismissed", include_hold: "true", profile_id: profile || undefined }
    : { status: "accepted,snoozed,dismissed", profile_id: profile || undefined };
  const recs = useRecs(filters);
  const last = summary.data?.last_run;
  const personName = profiles?.find((p) => p.id === profile)?.display_name;
  const [schedShown, setSchedShown] = useState(0);
  const shown = (recs.data ?? []).filter((r) => ASSETS[asset].match(r.asset_type));
  const count = (k: string) => (recs.data ?? []).filter((r) => ASSETS[k].match(r.asset_type)).length;
  const groups = { urgent: shown.filter((r) => r.bucket === "urgent"), focus: shown.filter((r) => r.bucket === "focus"), fyi: shown.filter((r) => r.bucket === "fyi") };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3 lg:flex-nowrap">
        <div className="min-w-0 flex-1">
          <h1 className="text-xl font-semibold">Advisor</h1>
          <p className="text-sm text-ink2">
            {last ? <>Last analysis {new Date(last.at).toLocaleString("en-IN")} · market <b className="cursor-help underline decoration-dotted" title={last.regime?.playbook ? `Market playbook: ${last.regime.playbook}` : ""}>{titleCase(last.regime?.regime)}</b> · <button className="text-brand" onClick={() => setSchedShown((n) => n + 1)}>schedule & run history</button></> : "Not run yet"}
          </p>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2 lg:justify-end">
          <select className="input w-auto" value={asset} onChange={(e) => setAsset(e.target.value)} aria-label="Asset type">
            {Object.entries(ASSETS).map(([k, v]) => <option key={k} value={k}>{v.label}{recs.data ? ` (${count(k)})` : ""}</option>)}
          </select>
          <select className="input w-auto" value={profile} onChange={(e) => setProfile(e.target.value)} aria-label="Profile">
            <option value="">Whole family</option>{profiles?.map((p) => <option key={p.id} value={p.id}>{p.display_name}</option>)}
          </select>
          <button className="btn-primary" title={`Analyses exactly what the filters show: ${ASSETS[asset].noun}${personName ? ` of ${personName}` : " of the whole family"}`}
            onClick={() => run.mutate({ profile_id: profile || undefined, ...ASSETS[asset].run, label: `${ASSETS[asset].noun}${personName ? ` for ${personName}` : ""}` })}
            disabled={run.isPending}>{run.isPending ? `Analysing ${ASSETS[asset].noun}…` : `✦ Analyse ${asset === "all" || asset === "portfolio" ? "all holdings" : ASSETS[asset].noun}${personName ? ` · ${personName}` : " · whole family"}`}</button>
        </div>
      </div>
      <ErrorNote error={run.error ?? recs.error} />
      <AutoHide showKey={`${summary.data?.ai.mode}:${summary.data?.last_attempt?.reason ?? ""}:${schedShown}`}>
        <div className="space-y-2">
          <AIBanner summary={summary.data} />
          <RunSchedule schedule={summary.data?.schedule} />
        </div>
      </AutoHide>
      <ReadinessPanel profileId={profile || undefined} />
      {run.data?.skipped && <p role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-sm text-down"><b>Analysis not run:</b> {run.data.reason}</p>}
      {run.data && !run.data.skipped && <p className="rounded-lg bg-brand/10 px-3 py-2 text-sm">Analysed {run.data.holdings} {run.data.label ?? "holdings"} → {Object.entries(run.data.actions ?? {}).map(([k, v]) => `${v} ${k}`).join(" · ")}. {run.data.new} new, {run.data.updated} updated, {run.data.resolved} resolved.{run.data.skipped_unpriced?.length ? ` Skipped (no live price): ${run.data.skipped_unpriced.join(", ")}.` : ""}</p>}

      <div className="flex gap-1 overflow-x-auto border-b border-line">
        {TABS.map((t) => <button key={t} onClick={() => setTab(t)} className={`whitespace-nowrap px-3 py-2 text-sm capitalize ${tab === t ? "border-b-2 border-brand font-medium text-brand" : "text-ink2"}`}>{t}</button>)}
      </div>

      {tab === "shadow portfolio" ? <Shadow /> : tab === "scorecard" ? <Scorecard /> : recs.isLoading ? <Skeleton className="h-64" /> : !shown.length ? (
        <Empty title={tab === "inbox" ? "Inbox zero" : "Nothing here"}>{tab === "inbox" ? "No action needed right now. Doing nothing is a valid decision." : "Run the analysis to get a verdict for every holding."}</Empty>
      ) : tab === "inbox" ? (
        <div className="space-y-5">
          {(["urgent", "focus", "fyi"] as const).map((b) => groups[b].length > 0 && (
            <div key={b} className="space-y-3">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-ink2">{b === "urgent" ? "Urgent" : b === "focus" ? "This week's focus (max 7, ranked by ₹ impact)" : "FYI"}</h2>
              <div className="grid gap-3 2xl:grid-cols-2">{groups[b].map((r) => <RecCard key={r.id} rec={r} />)}</div>
            </div>
          ))}
        </div>
      ) : (
        <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-3">{shown.map((r) => <RecCard key={r.id} rec={r} compact />)}</div>
      )}
    </div>
  );
}

function Shadow() {
  const { data, isLoading } = useQuery({ queryKey: qk.shadow, queryFn: () => api<any>("/advisor/shadow") });
  const [trades, sort] = useSort<any>(data?.trades ?? [], {
    symbol: (t) => t.symbol, qty: (t) => t.qty_delta, then: (t) => t.price_then, now: (t) => t.price_now, diff: (t) => t.difference,
  }, "shadow_sort");
  if (isLoading) return <Skeleton className="h-40" />;
  return (
    <Section title="Shadow portfolio — 'what if I had followed the calls I accepted?'">
      <p className="mb-3 text-sm text-ink2">{data?.explanation} Accepting never places a real order in this phase.</p>
      <StatTile label="Net difference vs your actual portfolio" value={inr(data?.net_difference)} />
      <table className="mt-3 w-full text-sm">
        <thead><tr><SortTh s={sort} k="symbol">Symbol</SortTh><SortTh s={sort} k="qty" className="th text-right">Virtual qty</SortTh>
          <SortTh s={sort} k="then" className="th text-right">Then</SortTh><SortTh s={sort} k="now" className="th text-right">Now</SortTh>
          <SortTh s={sort} k="diff" className="th text-right">Difference</SortTh></tr></thead>
        <tbody>{trades.map((t: any, i: number) => (
          <tr key={i} className="border-t border-line"><td className="td">{t.symbol} <span className="text-xs text-muted">{t.executed_on}</span></td><td className="td tnum text-right">{t.qty_delta.toFixed(2)}</td><td className="td tnum text-right">₹{t.price_then}</td><td className="td tnum text-right">₹{t.price_now}</td><td className="td tnum text-right">{inr(t.difference)}</td></tr>
        ))}</tbody>
      </table>
    </Section>
  );
}

function Scorecard() {
  const { data, isLoading } = useQuery({ queryKey: qk.scorecard, queryFn: () => api<any>("/advisor/scorecard") });
  const [byRule, sort] = useSort<[string, any]>(Object.entries(data?.by_rule ?? {}), {
    rule: ([k]) => k, right: ([, v]) => v.right, wrong: ([, v]) => v.wrong, neutral: ([, v]) => v.neutral, hit: ([, v]) => v.hit_rate_pct,
  }, "score_sort");
  if (isLoading) return <Skeleton className="h-40" />;
  return (
    <Section title="Scorecard — how the advisor's calls actually played out">
      <p className="mb-3 text-sm text-ink2">{data?.method}</p>
      {!data?.evaluated_calls ? <p className="text-sm text-muted">No calls are old enough yet (first evaluation after 30 days).</p> : (
        <table className="w-full text-sm">
          <thead><tr><SortTh s={sort} k="rule">Rule</SortTh><SortTh s={sort} k="right" className="th text-right">Right</SortTh>
            <SortTh s={sort} k="wrong" className="th text-right">Wrong</SortTh><SortTh s={sort} k="neutral" className="th text-right">Neutral</SortTh>
            <SortTh s={sort} k="hit" className="th text-right">Hit rate</SortTh></tr></thead>
          <tbody>{byRule.map(([k, v]: any) => (
            <tr key={k} className="border-t border-line"><td className="td">{k}</td><td className="td tnum text-right">{v.right}</td><td className="td tnum text-right">{v.wrong}</td><td className="td tnum text-right">{v.neutral}</td><td className="td tnum text-right">{v.hit_rate_pct ?? "—"}%</td></tr>
          ))}</tbody>
        </table>
      )}
    </Section>
  );
}
