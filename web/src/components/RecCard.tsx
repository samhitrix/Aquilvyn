"use client";

import { useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import Link from "next/link";
import { useState } from "react";

import { inr } from "@/lib/format";
import { prefetch, useActOnRec, useRunAdvisor } from "@/lib/queries";
import type { Recommendation } from "@/lib/types";

import { ReadinessTicks, useReadinessRow } from "./Readiness";
import { BucketTag, ConsensusBadge, VERDICT_TONE } from "./ui";

const DO_TONE: Record<string, string> = {
  EXIT: "border-down", TRIM: "border-down", ADD: "border-up", ACCUMULATE: "border-up", HOLD: "border-ink2", REVIEW: "border-warn", SWITCH: "border-warn", REBALANCE: "border-warn",
};
const PLAIN: Record<string, string> = { EXIT: "Sell", TRIM: "Sell some", ADD: "Buy more", ACCUMULATE: "Buy more (gradually)", HOLD: "Hold", SWITCH: "Switch", REVIEW: "Hold · data missing", REBALANCE: "Rebalance" };

/** "TATAPOWER — Sell" · "Parag Parikh Flexi Cap — Decision: Hold" · portfolio findings keep their headline. */
function title(rec: Recommendation): { name: string; decision: string | null } {
  const verdict = rec.short_report.verdict ?? PLAIN[rec.action] ?? rec.action;
  if (!rec.instrument_id) return { name: rec.headline, decision: null };
  if (rec.asset_type === "stock" || rec.asset_type === "etf") {
    return { name: (rec.symbol ?? rec.name ?? "").replace(/\.(NS|BO)$/, ""), decision: verdict };
  }
  return { name: rec.name ?? rec.symbol ?? "", decision: `Decision: ${verdict}` };
}

/** A clean tile: the holding and the decision, one status line. Click to see what to do, why, the AI's view,
 *  the data checks and the actions. */
export default function RecCard({ rec, compact = false }: { rec: Recommendation; compact?: boolean }) {
  const qc = useQueryClient();
  const act = useActOnRec();
  const again = useRunAdvisor();
  const ready = useReadinessRow(rec.id);
  const [open, setOpen] = useState(false);
  const s = rec.short_report;
  const t = title(rec);
  const level = s.conviction ?? (rec.confidence >= 0.75 ? "high" : rec.confidence >= 0.6 ? "medium" : "low");
  return (
    <article className={clsx("card p-4 transition", open && "ring-1 ring-brand/40")} onMouseEnter={() => prefetch.rec(qc, rec.id)}>
      <button className="block w-full text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
        <div className="flex items-start justify-between gap-3">
          <h3 className="text-lg font-bold leading-snug">
            {t.name}{t.decision && <> <span className="text-muted">—</span>{" "}
              <span className={clsx("inline-block rounded-md px-2 py-0.5 align-middle text-base font-bold", VERDICT_TONE[rec.action] ?? "bg-ink text-surface")}>{t.decision}</span></>}
          </h3>
          <span aria-hidden className={clsx("mt-1 text-muted transition", open && "rotate-180")}>▾</span>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs">
          <BucketTag bucket={rec.bucket} />
          {rec.horizon && rec.horizon !== "n/a" && <span className="chip border border-line">{rec.horizon === "short_term" ? "Short-term" : "Long-term"}</span>}
          {rec.action !== "REVIEW" && (
            <span className="inline-flex items-center gap-1.5 text-ink2" title="Confidence = rule strength × data quality × signal agreement, adjusted by the AI review">
              <span className="h-1.5 w-10 overflow-hidden rounded-full bg-line">
                <span className={clsx("block h-full rounded-full", level === "high" ? "bg-up" : level === "medium" ? "bg-warn" : "bg-muted")} style={{ width: `${Math.round(rec.confidence * 100)}%` }} />
              </span>
              <b className="tnum text-ink">{Math.round(rec.confidence * 100)}%</b> {level}
            </span>
          )}
          <ConsensusBadge value={rec.ai_consensus} action={rec.action} stale={s.ai_stale} />
          {ready && ready.status !== "ready" && (
            <span className={clsx("chip", ready.status === "attention" ? "bg-down/15 text-down" : "bg-warn/15 text-warn")} title="Open for the data checks">
              {ready.status === "attention" ? "✖ data needs attention" : "… data being fixed"}</span>
          )}
          {rec.profile_name && <span className="chip bg-page text-ink2">👤 {rec.profile_name}</span>}
          {rec.status !== "open" && <span className="chip bg-line capitalize text-ink2">{rec.status}</span>}
        </div>
      </button>

      {open && (
        <div className="mt-3 border-t border-line pt-3">
          {s.do && (
            <p className={`rounded-lg border-l-4 bg-page px-3 py-2 text-[0.95rem] font-semibold ${DO_TONE[rec.action] ?? "border-ink2"}`}>
              <span className="mr-1 text-xs font-bold uppercase tracking-wide text-ink2">What to do</span> {s.do}
            </p>
          )}
          {s.ai_opinion && (
            <div className="mt-2 rounded-lg border border-brand/40 bg-brand/5 px-3 py-2 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-bold uppercase tracking-wide text-brand">{s.ai_opinion.provider} opinion</span>
                <span className="chip bg-brand font-bold text-white">{s.ai_opinion.label}</span>
                {s.ai_opinion.confidence != null && <span className="text-xs text-ink2">AI confidence {Math.round(s.ai_opinion.confidence * 100)}%</span>}
                <span className="text-xs text-muted">not verified by live data · advisory only — check with your CA</span>
              </div>
              {s.ai_opinion.why && <p className="mt-1 whitespace-pre-line">{s.ai_opinion.why}</p>}
              {s.ai_opinion.risks && <p className="mt-1 whitespace-pre-line text-ink2"><b>Risks:</b> {s.ai_opinion.risks}</p>}
            </div>
          )}
          {s.ai_view?.plain && !s.ai_opinion && (
            <p className="mt-2 rounded-lg bg-page px-3 py-2 text-sm"><span className="mr-1 text-xs font-bold uppercase tracking-wide text-brand">{s.ai_view.provider} review</span> {s.ai_view.plain}</p>
          )}
          {s.ai_suggests && (
            <p className="mt-1 text-xs text-ink2">{s.ai_suggests.provider} would {s.ai_suggests.action === "EXIT" ? "sell" : s.ai_suggests.action === "TRIM" ? "sell part" : s.ai_suggests.action.toLowerCase()} instead — not applied: {s.ai_suggests.why_not}.</p>
          )}
          {s.leaning && (
            <p className="mt-1 text-xs text-ink2">Not sure enough to act: the rules lean <b>{s.leaning === "EXIT" ? "sell" : s.leaning === "TRIM" ? "sell some" : "buy more"}</b>, but confidence is only {Math.round(rec.confidence * 100)}%.</p>
          )}
          {!compact && s.reasons?.length > 0 && (
            <>
              <p className="mt-3 text-xs font-bold uppercase tracking-wide text-ink2">Why</p>
              <ul className="mt-1 list-disc space-y-1 pl-5 text-sm text-ink2">{s.reasons.slice(0, 3).map((r, i) => <li key={i}>{r}</li>)}</ul>
              {s.tax_impact && <p className="mt-2 rounded-lg bg-page px-3 py-2 text-sm"><span className="font-medium">Tax · </span>{s.tax_impact}</p>}
            </>
          )}
          <ReadinessTicks row={ready} detailed />
          {!ready && rec.ai_consensus === "ai_failed" && rec.ai_error && <p className="mt-2 text-xs text-down"><b>Why not verified:</b> {rec.ai_error}</p>}
          <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
            {rec.rupee_impact ? <span className="text-ink2">Impact <span className="tnum font-medium text-ink">{inr(rec.rupee_impact, { compact: true })}</span></span> : null}
            <span className="text-xs text-muted">Data {s.data_quality?.grade ?? "?"} · rule {rec.rule_id}</span>
          </div>
        </div>
      )}

      <div className="mt-3 flex flex-wrap gap-2 text-sm">
        {rec.status === "open" ? (
          <>
            <button className="btn-primary py-1" disabled={act.isPending} title="Records your decision and a virtual trade in the Shadow Portfolio. No real order is placed."
              onClick={() => act.mutate({ id: rec.id, action: "accept" })}>Accept</button>
            <button className="btn-ghost py-1" disabled={act.isPending} onClick={() => act.mutate({ id: rec.id, action: "dismiss" })}>Dismiss</button>
            {open && <button className="btn-ghost py-1" disabled={act.isPending} onClick={() => act.mutate({ id: rec.id, action: "snooze", snooze_days: 7 })}>Snooze 7d</button>}
          </>
        ) : (
          <button className="btn-ghost py-1" disabled={act.isPending} onClick={() => act.mutate({ id: rec.id, action: "reopen" })}>Reopen</button>
        )}
        {open && rec.instrument_id && (
          <button className="btn-ghost py-1" disabled={again.isPending} title="Analyse only this holding again, with the latest data"
            onClick={() => again.mutate({ profile_id: rec.profile_id ?? undefined, instrument_ids: [rec.instrument_id!], label: t.name })}>
            {again.isPending ? "Analysing…" : "↻ Analyse again"}</button>
        )}
        <Link href={`/advisor/${rec.id}`} className="btn-ghost ml-auto py-1">Full report →</Link>
      </div>
    </article>
  );
}
