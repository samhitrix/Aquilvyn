"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import clsx from "clsx";
import { useState } from "react";

import { toast } from "@/components/Toast";
import { api } from "@/lib/api";
import { useReadiness } from "@/lib/queries";
import type { Readiness, ReadinessRow } from "@/lib/types";
import { SortTh, useSort } from "@/lib/useSort";

const ORDER = ["fundamentals", "technicals", "analysis", "ai_review"] as const;
const SHORT: Record<string, string> = { fundamentals: "Fundamentals", technicals: "Technicals", analysis: "Analysis current", ai_review: "AI review" };
const TONE: Record<string, string> = { ok: "bg-up/15 text-up", failed: "bg-down/15 text-down", waiting: "bg-warn/15 text-warn", "n/a": "bg-line text-muted" };
const ICON: Record<string, string> = { ok: "✔", failed: "✖", waiting: "…", "n/a": "–" };
const STATUS: Record<string, [string, string]> = {
  ready: ["Ready", "bg-up/15 text-up"], fixing: ["Being fixed", "bg-warn/15 text-warn"], attention: ["Needs attention", "bg-down/15 text-down"],
};
const time = (s?: string | null) => (s ? new Date(s).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" }) : "");

/** One holding's four checks as chips; the failing ones say what's wrong and what the engine is doing about it. */
export function ReadinessTicks({ row, detailed = false }: { row?: ReadinessRow; detailed?: boolean }) {
  if (!row) return null;
  const bad = ORDER.filter((c) => ["failed", "waiting"].includes(row.checks[c]?.state));
  return (
    <div className="mt-2 text-xs">
      <div className="flex flex-wrap items-center gap-1.5">
        {ORDER.map((c) => {
          const ch = row.checks[c];
          if (!ch || ch.state === "n/a") return null;
          return <span key={c} className={clsx("chip", TONE[ch.state])} title={ch.detail}><span aria-hidden>{ICON[ch.state]}</span>{SHORT[c]}</span>;
        })}
        {row.status !== "ready" && <span className={clsx("chip font-semibold", STATUS[row.status][1])}>{STATUS[row.status][0]}</span>}
      </div>
      {(detailed || row.status !== "ready") && bad.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-ink2">
          {bad.map((c) => {
            const f = row.fixes[c];
            return (
              <li key={c}><b className="text-ink">{SHORT[c]}:</b> {row.checks[c].detail}
                {f ? <span className="text-muted"> · fix tried {f.attempts}×{f.last_error ? ` (${f.last_error})` : ""} · next try {time(f.next_try)}</span> : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Looks up a holding's readiness row (shared, cached query — every card on the page uses the same request). */
export function useReadinessRow(recId?: string | null) {
  const q = useReadiness();
  return recId ? q.data?.holdings.find((r) => r.rec_id === recId) : undefined;
}

/** Advisor page: what the readiness engine found for every holding, and a "Check now" button. */
export function ReadinessPanel({ profileId }: { profileId?: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const q = useReadiness();
  const check = useMutation({
    mutationFn: () => api<{ queued: boolean }>("/readiness/check", { method: "POST" }),
    onSuccess: () => {
      toast("info", "Checking every holding now", "Missing data is fetched again, stale calls re-analysed and AI reviews re-queued. This panel updates as it finishes.");
      setTimeout(() => qc.invalidateQueries({ queryKey: ["readiness"] }), 20_000);
    },
    onError: (e: Error) => toast("error", "Couldn't start the check", e.message),
  });
  const data: Readiness | undefined = q.data;
  const rows = (data?.holdings ?? []).filter((r) => !profileId || r.profile_id === profileId);
  const [sortedRows, sort] = useSort(rows, {
    symbol: (r) => r.symbol, status: (r) => r.status, ...Object.fromEntries(ORDER.map((c) => [c, (r: (typeof rows)[number]) => r.checks[c]?.state ?? null])),
  }, "health_sort");
  if (!data) return null;
  const n = { ready: 0, fixing: 0, attention: 0 } as Record<string, number>;
  const failing: Record<string, number> = {};
  rows.forEach((r) => {
    n[r.status] = (n[r.status] ?? 0) + 1;
    ORDER.forEach((c) => { if (["failed", "waiting"].includes(r.checks[c]?.state)) failing[c] = (failing[c] ?? 0) + 1; });
  });
  const lp = data.last_pass;
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-2 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <b>Health check</b>
        <span className="chip bg-up/15 text-up">✔ {n.ready} of {rows.length} ready</span>
        {n.fixing > 0 && <span className="chip bg-warn/15 text-warn">… {n.fixing} being fixed</span>}
        {n.attention > 0 && <span className="chip bg-down/15 text-down">✖ {n.attention} need attention</span>}
        {ORDER.filter((c) => failing[c]).map((c) => <span key={c} className="text-xs text-ink2">{SHORT[c]}: {failing[c]}</span>)}
        <span className="ml-auto flex items-center gap-2 text-xs text-muted">
          {lp ? `checked ${time(lp.finished_at ?? lp.started_at)}${lp.status === "failed" ? " (failed)" : ""}` : "not checked yet"} · every {data.every_minutes} min
          <button className="btn-ghost py-1" onClick={() => check.mutate()} disabled={check.isPending}>{check.isPending ? "Starting…" : "↻ Check now"}</button>
          <button className="text-brand" onClick={() => setOpen(!open)}>{open ? "Hide" : "Details"}</button>
        </span>
      </div>
      {lp?.status === "failed" && lp.error && <p className="mt-1 text-xs text-down">Last check failed: {lp.error}</p>}
      {open && (
        <table className="mt-2 w-full text-xs">
          <thead><tr><SortTh s={sort} k="symbol">Holding</SortTh>{ORDER.map((c) => <SortTh key={c} s={sort} k={c}>{SHORT[c]}</SortTh>)}
            <SortTh s={sort} k="status">Status</SortTh></tr></thead>
          <tbody>{sortedRows.map((r) => (
            <tr key={`${r.profile_id}:${r.instrument_id}`} className="border-t border-line align-top">
              <td className="td"><b>{r.symbol}</b>{r.profile_name ? <span className="block text-muted">{r.profile_name}</span> : null}</td>
              {ORDER.map((c) => {
                const ch = r.checks[c]; const f = r.fixes[c];
                return (
                  <td key={c} className="td">
                    <span className={clsx("chip", TONE[ch?.state ?? "n/a"])}>{ICON[ch?.state ?? "n/a"]}</span>
                    <span className="block max-w-[16rem] text-ink2">{ch?.detail}</span>
                    {f && <span className="block text-muted">tried {f.attempts}× · next {time(f.next_try)}</span>}
                  </td>
                );
              })}
              <td className="td"><span className={clsx("chip", STATUS[r.status][1])}>{STATUS[r.status][0]}</span></td>
            </tr>))}
            {!rows.length && <tr><td className="td text-muted" colSpan={6}>No holdings checked yet — run the analysis once; the engine takes it from there.</td></tr>}
          </tbody>
        </table>
      )}
    </div>
  );
}
