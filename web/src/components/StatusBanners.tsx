"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";

import { api } from "@/lib/api";
import type { AdvisorSummary, MarketStatus } from "@/lib/types";

const SOURCE_LABEL: Record<string, string> = {
  yahoo: "Stock prices (Yahoo Finance)", fundamentals: "Company fundamentals (Yahoo Finance)", nse_size_lists: "NSE index lists (company size)", amfi_nav: "Mutual-fund NAVs (AMFI via mfapi.in)", amfi_master: "AMFI scheme list", quotes: "Price service",
};

export const ago = (s: number | null | undefined) =>
  s == null ? "never" : s < 90 ? `${s}s ago` : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 172800 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;

export function useMarketStatus(enabled = true) {
  return useQuery({ queryKey: ["market-status"], queryFn: () => api<MarketStatus>("/market/status"), enabled, refetchInterval: 30_000, staleTime: 10_000 });
}

/** Red/amber banner explaining *why* prices are missing or old — never a silent "at cost". */
export function PriceBanner({ unpriced = 0, stale = 0, npsNavAsOf }: { unpriced?: number; stale?: number; npsNavAsOf?: string | null }) {
  const st = useMarketStatus(true);
  const failing = Object.entries(st.data?.sources ?? {}).filter(([, v]) => v.status === "failing");
  if (st.data?.simulated_equities) {
    return (
      <div role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-sm text-down">
        <b>Stock prices are SIMULATED</b> (MARKET_DATA_PROVIDER=simulated in .env) — values, gains and advice for stocks are not real.
        Set <code>MARKET_DATA_PROVIDER=yahoo</code> in <code>.env</code> and run <code>python scripts/fm.py up-lite</code>. Mutual-fund NAVs are always real.
      </div>
    );
  }
  // NPS has no price feed — its NAV comes from the imported statement, which is expected. Only remind when it's old.
  const npsNote = npsNavAsOf ? (
    <div className="rounded-lg border border-line bg-surface px-3 py-2 text-sm text-ink2">
      NPS values use the NAV from your statement of <b className="text-ink">{new Date(npsNavAsOf).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" })}</b>.
      {" "}<Link href="/import" className="underline">Import a newer NPS statement</Link> to update them.
    </div>
  ) : null;
  if (!unpriced && !stale) return npsNote;  // only when *your* holdings are affected — see Settings → Data sources for the rest
  const tone = unpriced ? "border-down/40 bg-down/10 text-down" : "border-warn/40 bg-warn/10 text-warn";
  return (
    <div role="alert" className={`rounded-lg border px-3 py-2 text-sm ${tone}`}>
      <b>{unpriced ? `No live price for ${unpriced} holding(s) — shown at cost.` : `${stale} holding(s) use the last known / statement price.`}</b>{" "}
      {failing.length ? failing.map(([k, v]) => <span key={k} className="block text-xs">{SOURCE_LABEL[k] ?? k}: {v.last_error} ({ago(v.last_error_ago_s)}; last success {ago(v.last_ok_ago_s)})</span>)
        : <span className="text-xs">Retrying automatically.</span>}
      <Link href="/settings#data-sources" className="text-xs underline">Data sources →</Link>
    </div>
  );
}

/** Makes it unmistakable whether an AI actually reviewed the advice. */
export function AIBanner({ summary }: { summary?: AdvisorSummary | null }) {
  if (!summary) return null;
  const ai = summary.ai;
  const attempt = summary.last_attempt;
  return (
    <div className="space-y-2">
      {attempt?.reason && (
        <div role="alert" className="rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-sm text-down">
          <b>Last analysis skipped</b> ({new Date(attempt.at).toLocaleString("en-IN")}): {attempt.reason}
          {summary.last_run ? <span className="block text-xs">Showing the previous complete analysis from {new Date(summary.last_run.at).toLocaleString("en-IN")}.</span> : null}
        </div>
      )}
      {ai.mode === "rules_only" ? (
        <div className="rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
          <b>No AI model connected — rules-only.</b> Every verdict below is calculated by FolioSense&apos;s transparent rulebook from your holdings and market data
          (each card shows the rule and data behind it). No AI has checked it. <Link href="/settings#ai" className="underline">Add Claude / OpenAI / Gemini / Ollama →</Link>
        </div>
      ) : (
        <div className="rounded-lg border border-up/40 bg-up/10 px-3 py-2 text-sm text-up">
          <b>AI review on:</b> {ai.reviewers.join(", ")}{ai.primary ? ` · narration by ${ai.primary}` : ""}
        </div>
      )}
    </div>
  );
}
