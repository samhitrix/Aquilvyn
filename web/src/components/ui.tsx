"use client";

import clsx from "clsx";
import { useEffect, useState } from "react";

import { pct, tone } from "@/lib/format";

export function StatTile({ label, value, delta, sub, hint }: { label: string; value: React.ReactNode; delta?: number | null; sub?: React.ReactNode; hint?: string }) {
  return (
    <div className="card p-4" title={hint}>
      <div className="text-xs font-medium text-ink2">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
      {(delta !== undefined || sub) && (
        <div className="mt-1 flex items-center gap-2 text-sm">
          {delta !== undefined && <Delta v={delta} />}
          {sub && <span className="text-ink2">{sub}</span>}
        </div>
      )}
    </div>
  );
}

export function Delta({ v, d = 2 }: { v: number | null | undefined; d?: number }) {
  if (v === null || v === undefined) return <span className="text-muted">—</span>;
  return (
    <span className={clsx("tnum font-medium", tone(v))}>
      <span aria-hidden>{v > 0 ? "▲" : v < 0 ? "▼" : "•"}</span> {pct(v, d)}
    </span>
  );
}

const ACTION_STYLE: Record<string, string> = {
  ADD: "bg-up/15 text-up", ACCUMULATE: "bg-up/15 text-up", HOLD: "bg-line text-ink2", TRIM: "bg-warn/15 text-warn",
  EXIT: "bg-down/15 text-down", SWITCH: "bg-brand/15 text-brand", REVIEW: "bg-warn/15 text-warn", REBALANCE: "bg-brand/15 text-brand",
};
const ACTION_ICON: Record<string, string> = { ADD: "＋", ACCUMULATE: "⤓", HOLD: "‖", TRIM: "✂", EXIT: "⏏", SWITCH: "⇄", REVIEW: "?", REBALANCE: "⚖" };

export function ActionBadge({ action, horizon }: { action: string; horizon?: string }) {
  return (
    <span className={clsx("chip", ACTION_STYLE[action] ?? "bg-line text-ink2")}>
      <span aria-hidden>{ACTION_ICON[action]}</span>{action}
      {horizon && horizon !== "n/a" && <span className="font-normal opacity-80">· {horizon === "short_term" ? "short-term" : "long-term"}</span>}
    </span>
  );
}

/** One colour per decision, used wherever a decision is shown (tiles, report, dashboard). */
export const VERDICT_TONE: Record<string, string> = {
  EXIT: "bg-red-600 text-white",            // Sell
  TRIM: "bg-orange-500 text-white",         // Sell some
  HOLD: "bg-sky-600 text-white",            // Hold
  ADD: "bg-green-600 text-white",           // Buy more
  ACCUMULATE: "bg-emerald-600 text-white",  // Buy more (gradually)
  SWITCH: "bg-violet-600 text-white",       // Switch
  REVIEW: "bg-amber-400 text-black",        // Hold · data missing
  REBALANCE: "bg-indigo-600 text-white",    // Rebalance
};
const CONVICTION: Record<string, string> = { high: "High confidence", medium: "Medium confidence", low: "Low confidence — not sure" };

/** The one-word call (Sell / Hold / Buy more …) with how sure we are, as a number and in words. */
export function VerdictBadge({ action, verdict, conviction, confidence, horizon }: {
  action: string; verdict?: string; conviction?: string; confidence: number; horizon?: string;
}) {
  const level = conviction ?? (confidence >= 0.75 ? "high" : confidence >= 0.6 ? "medium" : "low");
  const label = verdict ?? ({ EXIT: "Sell", TRIM: "Sell some", ADD: "Buy more", ACCUMULATE: "Buy more (gradually)", HOLD: "Hold", SWITCH: "Switch", REVIEW: "Hold · data missing", REBALANCE: "Rebalance" }[action] ?? action);
  return (
    <span className="inline-flex items-center gap-2">
      <span className={clsx("chip px-2.5 text-sm font-bold", VERDICT_TONE[action] ?? "bg-ink text-surface")}>{label}</span>
      {horizon && horizon !== "n/a" && <span className="chip border border-line font-medium text-ink">{horizon === "short_term" ? "Short-term" : "Long-term"}</span>}
      {action === "REVIEW" ? <span className="text-xs text-ink2">rules: no call — data missing</span> : (
      <span className="inline-flex items-center gap-1.5 text-xs text-ink2" title="Confidence = rule strength × data quality × signal agreement, adjusted by the AI review">
        <span className="h-1.5 w-12 overflow-hidden rounded-full bg-line"><span className={clsx("block h-full rounded-full", level === "high" ? "bg-up" : level === "medium" ? "bg-warn" : "bg-muted")} style={{ width: `${Math.round(confidence * 100)}%` }} /></span>
        <b className="tnum text-ink">{Math.round(confidence * 100)}%</b> · {CONVICTION[level]}
      </span>)}
    </span>
  );
}

const CONSENSUS: Record<string, [string, string, string, string]> = {
  verified: ["✔", "Verified by AI", "bg-up/15 text-up", "Every AI that reviewed the evidence agreed with this call."],
  caution: ["!", "AI verified · with caution", "bg-warn/15 text-warn", "The AIs didn't disagree, but at least one flagged a caution — see the full report."],
  needs_review: ["✖", "Not verified · AIs disagree", "bg-down/15 text-down", "At least one AI disagreed with this call — read both sides in the full report."],
  ai_failed: ["✖", "Not verified · AI check failed", "bg-down/15 text-down", "An AI was asked but every call failed — open the full report to see the error."],
  rules_only: ["○", "Not verified by AI · rules only", "bg-warn/15 text-warn", "No AI model is connected, so only the rules engine made this call. Add one in Settings → AI providers."],
  pending: ["…", "Not verified yet · AI check pending", "bg-line text-ink2", "The AI check hasn't finished yet."],
};
export function ConsensusBadge({ value, action, stale }: { value: string; action?: string; stale?: { reviewed_at: string; provider: string } | null }) {
  if (stale && ["verified", "caution", "needs_review"].includes(value) && action !== "REVIEW") {
    // yesterday's review still stands until today's lands
    const [icon, label, cls] = CONSENSUS[value];
    const day = new Date(stale.reviewed_at).toLocaleDateString("en-IN", { day: "numeric", month: "short" });
    return <span className={clsx("chip font-semibold", cls)} title={`Reviewed by ${stale.provider} on ${day} with slightly older data. That review stays valid; a re-check with today's data is queued.`}>
      <span aria-hidden>{icon}</span>{label} · {day}</span>;
  }
  if (action === "REVIEW" && ["verified", "caution", "needs_review"].includes(value)) {  // nothing to verify: the data to judge is missing
    return <span className="chip bg-line font-semibold text-ink2" title="Live data is missing, so the AI gave its own opinion from general knowledge — nothing was verified.">○ AI opinion · not verified by data</span>;
  }
  const [icon, label, cls, hint] = CONSENSUS[value] ?? CONSENSUS.pending;
  return <span className={clsx("chip font-semibold", cls)} title={hint}><span aria-hidden>{icon}</span>{label}</span>;
}

export function BucketTag({ bucket }: { bucket: string }) {
  const s = bucket === "urgent" ? "bg-down text-white" : bucket === "focus" ? "bg-brand text-white" : "bg-line text-ink2";
  return <span className={clsx("chip uppercase tracking-wide", s)}>{bucket}</span>;
}

export function ConfidenceBar({ value }: { value: number }) {
  return (
    <span className="inline-flex items-center gap-2 text-xs text-ink2" title="Confidence = rule base × data quality × signal alignment">
      <span className="h-1.5 w-16 overflow-hidden rounded-full bg-line"><span className="block h-full rounded-full bg-brand" style={{ width: `${Math.round(value * 100)}%` }} /></span>
      {Math.round(value * 100)}%
    </span>
  );
}

export type Accent = "brand" | "up" | "down" | "warn" | "ink";
const ACCENT: Record<Accent, [string, string]> = {  // [bar, heading text]
  brand: ["bg-brand", "text-brand"], up: ["bg-up", "text-up"], down: ["bg-down", "text-down"], warn: ["bg-warn", "text-warn"], ink: ["bg-ink2", "text-ink"],
};

export function Section({ title, action, children, className, accent }: {
  title: string; action?: React.ReactNode; children: React.ReactNode; className?: string; accent?: Accent;
}) {
  const [bar, text] = ACCENT[accent ?? "ink"];
  return (
    <section className={clsx("card p-4", className)}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className={clsx("flex items-center gap-2 font-bold", accent ? clsx("text-base", text) : "text-sm")}>
          {accent && <span aria-hidden className={clsx("h-4 w-1 rounded-full", bar)} />}{title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={clsx("animate-pulse rounded-lg bg-line/60", className)} />;
}

export function Empty({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-line p-8 text-center">
      <div className="font-medium">{title}</div>
      {children && <div className="mt-2 text-sm text-ink2">{children}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  if (!error) return null;
  return <p role="alert" className="rounded-lg bg-down/10 px-3 py-2 text-sm text-down">{(error as Error).message}</p>;
}

/** Shows a notice, then tucks it away after `ms` (default 30 s) so the page stays clean. It comes back when
 *  `showKey` changes (the situation changed), on the next visit, or while the pointer is over it. */
export function AutoHide({ children, ms = 30_000, showKey = "" }: { children: React.ReactNode; ms?: number; showKey?: string }) {
  const [shown, setShown] = useState(true);
  const [hover, setHover] = useState(false);
  useEffect(() => {
    setShown(true);
    const t = setTimeout(() => setShown(false), ms);
    return () => clearTimeout(t);
  }, [ms, showKey]);
  if (!shown && !hover) return null;
  return <div onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>{children}</div>;
}
