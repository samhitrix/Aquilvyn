"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useParams } from "next/navigation";

import { FundamentalPanel, FundPanel, TechnicalPanel } from "@/components/AnalysisPanels";
import RecCard from "@/components/RecCard";
import { toast } from "@/components/Toast";
import { ConsensusBadge, ErrorNote, Section, Skeleton } from "@/components/ui";
import { api } from "@/lib/api";
import { inr, num, pct, titleCase } from "@/lib/format";
import { useAiProviders, useRec, useRunAdvisor } from "@/lib/queries";
import { ago, useMarketStatus } from "@/components/StatusBanners";
import { SortTh, useSort } from "@/lib/useSort";

const STANCE_UI: Record<string, { label: string; chip: string; text: string; meaning: (p: string) => string }> = {
  agree: { label: "✔ Agrees", chip: "bg-up text-white", text: "text-up", meaning: (p) => `${titleCase(p)} checked the evidence and agrees with this call.` },
  caution: { label: "! Caution", chip: "bg-warn text-white", text: "text-warn", meaning: (p) => `${titleCase(p)} doesn't reject the call but found reasons to be careful — read the problems below before acting.` },
  disagree: { label: "✖ Disagrees", chip: "bg-down text-white", text: "text-down", meaning: (p) => `${titleCase(p)} disagrees with this call — don't act on it without reading why.` },
  error: { label: "Error", chip: "bg-ink2 text-white", text: "text-ink2", meaning: (p) => `${titleCase(p)} couldn't review this call — it is not verified by this AI.` },
};
const ISSUE_UI: Record<string, [string, string]> = {
  logic: ["Logic problem", "bg-down/15 text-down"], missing_context: ["Missing context", "bg-warn/15 text-warn"], data: ["Data problem", "bg-warn/15 text-warn"],
  risk: ["Risk", "bg-down/15 text-down"], error: ["Error", "bg-page text-ink2"], tax: ["Tax", "bg-brand/15 text-brand"], valuation: ["Valuation", "bg-brand/15 text-brand"],
};

type SourceResult = { source: string; status: string; detail: string; ms?: number; fields?: string[] };

/** Fetch this stock's fundamentals again (the result is saved), show what each source said, then re-analyse
 *  only this holding so the report uses them. */
function SourceCheck({ symbol, profileId, instrumentId, onDone }: { symbol: string; profileId: string | null; instrumentId: string | null; onDone: () => void }) {
  const qc = useQueryClient();
  const run = useRunAdvisor();
  const check = useMutation({
    mutationFn: async () => {
      const sources = await api<{ sources: SourceResult[]; note?: string }>(`/market/fundamentals/${encodeURIComponent(symbol)}/sources`);
      const saved = await api<{ fundamentals: any }>(`/market/fundamentals/${encodeURIComponent(symbol)}`, { query: { refresh: "true" } });
      return { ...sources, saved: !!saved.fundamentals };
    },
    onSuccess: (res) => {
      if (res.saved && instrumentId) {
        run.mutate({ profile_id: profileId ?? undefined, instrument_ids: [instrumentId], label: symbol }, {
          onSuccess: () => { onDone(); qc.invalidateQueries({ queryKey: ["readiness"] }); },
        });
      } else if (!res.saved) toast("warn", `No source returned fundamentals for ${symbol}`, "The readiness engine keeps retrying automatically; see each source's answer below.");
    },
  });
  const tone: Record<string, string> = { ok: "text-up", empty: "text-warn", error: "text-down", "not set up": "text-muted" };
  return (
    <div className="mt-2">
      <button className="btn-ghost py-1 text-sm" disabled={check.isPending || run.isPending || !symbol} onClick={() => check.mutate()}>
        {check.isPending ? "Fetching from every source…" : run.isPending ? "Re-analysing with the new data…" : "Fetch fundamentals now & re-analyse"}</button>
      {check.data && (
        <ul className="mt-2 space-y-1 text-sm">
          {check.data.note && <li className="text-ink2">{check.data.note}</li>}
          <li className={check.data.saved ? "text-up" : "text-down"}>{check.data.saved ? "✔ Fundamentals saved — the report is being rebuilt with them." : "✖ Nothing saved: no source had data."}</li>
          {check.data.sources.map((x) => (
            <li key={x.source} className="flex flex-wrap gap-2"><b className="w-28">{x.source}</b>
              <span className={`font-semibold ${tone[x.status] ?? ""}`}>{x.status}</span>
              <span className="text-ink2">{x.detail}{x.ms != null ? ` · ${x.ms} ms` : ""}</span></li>
          ))}
        </ul>
      )}
      <ErrorNote error={check.error} />
    </div>
  );
}

function LotsTable({ lots, estimated }: { lots: any[]; estimated: boolean }) {
  const [rows, sort] = useSort<any>(lots, { lot: (l) => l.buy_date, qty: (l) => l.qty, cost: (l) => l.cost, term: (l) => l.term, gain: (l) => l.gain }, "lots_sort");
  return (
    <table className="mt-2 w-full text-sm"><thead><tr><SortTh s={sort} k="lot">Lot</SortTh><SortTh s={sort} k="qty" className="th text-right">Qty</SortTh>
      <SortTh s={sort} k="cost" className="th text-right">Cost</SortTh><SortTh s={sort} k="term">Term</SortTh><SortTh s={sort} k="gain" className="th text-right">Gain</SortTh></tr></thead>
      <tbody>{rows.map((l: any, i: number) => <tr key={i} className="border-t border-line"><td className="td">{estimated ? "date unknown" : l.buy_date}</td><td className="td tnum text-right">{num(l.qty, 3)}</td><td className="td tnum text-right">₹{num(l.cost)}</td><td className="td">{l.term}{l.long_term_on && l.term === "short" ? ` (LT on ${l.long_term_on})` : ""}</td><td className="td tnum text-right">{inr(l.gain)}</td></tr>)}</tbody></table>
  );
}

export default function ReportPage() {
  const { id } = useParams<{ id: string }>();
  const { data: r, isLoading, error, refetch } = useRec(id);
  const providers = useAiProviders();
  const market = useMarketStatus(true);
  const fund = market.data?.sources?.fundamentals;
  const active: string[] = (providers.data?.active ?? []).map((a: any) => a.provider);
  const review = useMutation({
    mutationFn: () => api<any>(`/advisor/recommendations/${id}/review`, { method: "POST" }),
    onSuccess: (res) => {
      refetch();
      if (res.mode === "rules_only") return toast("warn", "Not verified — no AI model connected", "Add Claude / OpenAI / Gemini / Ollama in Settings → AI providers.");
      if (res.mode === "budget_exhausted") return toast("warn", "Not verified — monthly AI budget used up", "Raise the budget in Settings → AI providers, or wait until next month.");
      const results: any[] = res.results ?? [];
      const failed = results.filter((x) => x.stance === "error");
      const lines = results.map((x) => `${x.provider}: ${x.stance === "error" ? `failed — ${x.error}` : x.stance}${x.latency_ms ? ` (${(x.latency_ms / 1000).toFixed(1)}s)` : ""}`).join(" · ");
      if (!results.length) toast("info", "Nothing to re-check", "No AI returned a result.");
      else if (failed.length === results.length) toast("error", "Not verified — AI check failed", lines);
      else if (res.consensus === "verified") toast("ok", "Verified by AI", lines);
      else toast("warn", res.consensus === "needs_review" ? "Not verified — AIs disagree" : "AI verified with caution", lines);
    },
    onError: (e: Error) => toast("error", "AI re-check failed", e.message),
  });
  if (isLoading) return <Skeleton className="h-96" />;
  if (!r) return <ErrorNote error={error} />;
  const fr = r.full_report ?? {};
  const ev = fr.evidence ?? {};
  const trace = fr.decision_trace ?? {};
  const dd = ev.drawdown;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between gap-2">
        <Link href="/advisor" className="text-sm text-brand">← Advisor inbox</Link>
        <span className="text-xs text-muted">Report {r.id.slice(0, 8)} · input hash {fr.input_hash?.slice(0, 12)} · rulebook v{r.rulebook_version}</span>
      </div>
      <RecCard rec={r} />
      {String(r.rule_id).startsWith("data_") && (
        <div role="alert" className="rounded-xl border-2 border-warn bg-warn/10 p-4">
          <p className="text-base font-bold text-warn">No buy or sell call yet — the data to judge it is missing</p>
          <p className="mt-1 text-sm">{fr.decision_trace?.matched_rule === "data_no_fundamentals"
            ? "This stock's company fundamentals (valuation, profits, debt) couldn't be loaded, so Aquilvyn won't tell you to buy or sell it on price moves alone. Keep holding; the call appears automatically once the data loads."
            : "There isn't enough price history to read the trend, so Aquilvyn won't make a buy or sell call. Keep holding; the call appears automatically once history loads."}</p>
          {fund?.status === "failing" && <p className="mt-1 text-sm text-ink2">Source status: {fund.last_error} ({ago(fund.last_error_ago_s)}).</p>}
        </div>
      )}
      {fr.data_quality && (
        <Section title="Data sources & quality" accent={["C", "D"].includes(fr.data_quality.grade) ? "warn" : "up"}>
          <p className="text-sm">Grade <b>{fr.data_quality.grade}</b> ({fr.data_quality.score}/100) · checked {fr.data_quality.checked_at?.slice(0, 16)}</p>
          <ul className="mt-1 text-sm text-ink2">{Object.entries(fr.data_quality.sources ?? {}).map(([k, v]: any) => <li key={k}>{titleCase(k)}: {v.source} {v.as_of ? `· as of ${String(v.as_of).slice(0, 16)}` : ""} {v.bars ? `· ${v.bars} bars` : ""}</li>)}</ul>
          {fr.data_quality.issues?.length > 0 && <ul className="mt-1 space-y-0.5 text-sm">{fr.data_quality.issues.map((i: any, k: number) => <li key={k} className={i.severity === "high" ? "font-medium text-down" : "text-warn"}>⚠ {i.message}</li>)}</ul>}
          {r.asset_type === "stock" && <SourceCheck symbol={r.symbol ?? ""} profileId={r.profile_id} instrumentId={r.instrument_id} onDone={refetch} />}
          {fund?.status === "failing" && <p className="mt-2 text-sm text-ink2">Why fundamentals are missing: <b className="text-ink">{fund.last_error}</b> ({ago(fund.last_error_ago_s)}). <Link href="/settings#data-sources" className="text-brand underline">Data sources →</Link></p>}
        </Section>
      )}

      {r.narrative && <Section title="In plain words" accent="brand"><p className="text-sm leading-relaxed">{r.narrative}</p></Section>}

      <Section title="AI review panel" accent="brand" action={<div className="flex items-center gap-2"><ConsensusBadge value={r.ai_consensus} action={r.action} /><button className="btn-ghost py-1" onClick={() => review.mutate()} disabled={review.isPending}>{review.isPending ? `Checking with ${active.length ? active.join(", ") : "AI"}…` : "Re-check with AIs"}</button></div>}>
        {review.isPending && (
          <p className="mb-3 rounded-lg border border-line bg-page px-3 py-2 text-sm text-ink2" role="status">
            Sending the evidence to {active.length ? active.join(", ") : "your AI models"} and waiting for each verdict — this usually takes 10–60 seconds…
          </p>
        )}
        {r.ai_reviews.length === 0 ? (
          <p className="text-sm text-ink2">{r.ai_consensus === "rules_only" ? "Not verified by AI — no AI provider is configured, so this call was made by the rules engine only. Add Claude / OpenAI / Gemini / Ollama in Settings → AI providers." : "Not verified yet — the AI check hasn't run. Click “Re-check with AIs”."}</p>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {r.ai_reviews.map((v) => {
              const st = STANCE_UI[v.stance] ?? STANCE_UI.error;
              return (
                <div key={v.provider} className="rounded-xl border border-line p-4">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div><b className="text-base capitalize">{v.provider}</b>
                      <div className="text-xs text-muted">{v.model} · {new Date(v.reviewed_at).toLocaleString("en-IN")} {v.current ? "" : "· (older inputs)"}</div></div>
                    <span className={`chip px-3 text-sm font-bold ${v.opinion && v.stance !== "error" ? "bg-brand text-white" : st.chip}`}>
                      {v.opinion && v.stance !== "error" ? "Opinion" : st.label}</span>
                  </div>
                  <p className={`mt-2 text-sm font-semibold ${v.opinion && v.stance !== "error" ? "text-brand" : st.text}`}>
                    {v.opinion && v.stance !== "error"
                      ? `${titleCase(v.provider)}'s own analyst opinion — live data was missing, so nothing is verified; it draws on general knowledge that may be outdated. Advisory only.`
                      : st.meaning(v.provider)}</p>
                  {(v.plain_verdict || v.suggested_action) && (
                    <p className="mt-2 rounded-lg bg-page px-3 py-2 text-sm">
                      <span className="mr-1 text-xs font-bold uppercase tracking-wide text-ink2">Its call</span>
                      {v.suggested_action && <b className="mr-1">{({ EXIT: "Sell", TRIM: "Sell part", HOLD: "Hold", ADD: "Buy more", ACCUMULATE: "Buy more", SWITCH: "Switch", REVIEW: "Wait for data" } as Record<string, string>)[v.suggested_action] ?? v.suggested_action}.</b>}
                      {v.plain_verdict}
                    </p>
                  )}
                  {v.rationale && (<>
                    <h4 className="mt-3 text-xs font-bold uppercase tracking-wide text-ink2">Summary</h4>
                    <p className="mt-1 text-sm leading-relaxed">{v.rationale}</p>
                  </>)}
                  {v.counter_case && (<>
                    <h4 className="mt-3 text-xs font-bold uppercase tracking-wide text-ink2">The other side</h4>
                    <p className="mt-1 text-sm leading-relaxed text-ink2">{v.counter_case}</p>
                  </>)}
                  {v.issues.length > 0 && (<>
                    <h4 className="mt-3 text-xs font-bold uppercase tracking-wide text-ink2">{v.stance === "error" ? "What went wrong" : "Problems it found"}</h4>
                    <ul className="mt-1 space-y-2 text-sm">{v.issues.map((i, k) => (
                      <li key={k} className="flex gap-2">
                        <span className={`chip h-fit shrink-0 font-semibold ${ISSUE_UI[i.type]?.[1] ?? "bg-page text-ink2"}`}>{ISSUE_UI[i.type]?.[0] ?? titleCase(i.type)}</span>
                        <span className="leading-relaxed">{i.detail}</span>
                      </li>))}</ul>
                  </>)}
                </div>
              );
            })}
          </div>
        )}
        <ErrorNote error={review.error} />
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        {ev.position && (
          <Section title="Position" accent="ink">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
              {[["Quantity", num(ev.position.quantity, 3)], ["Avg cost", ev.position.avg_cost ? `₹${num(ev.position.avg_cost)}` : "—"], ["Price", ev.position.price ? `₹${num(ev.position.price)}` : "accrual"],
                ["Invested", inr(ev.position.invested)], ["Value", inr(ev.position.market_value)], ["Gain", `${inr(ev.position.unrealised_pnl)} (${pct(ev.position.unrealised_pct)})`],
                ["XIRR", ev.position.dates_estimated ? "n/a (dates unknown)" : pct(ev.position.xirr_pct, 1, false)],
                ["Held", ev.position.dates_estimated ? "unknown (holdings statement)" : `${ev.position.holding_days ?? 0} days`]].map(([k, v]) => (
                <div key={k} className="flex justify-between border-b border-line/60 py-1"><dt className="text-ink2">{k}</dt><dd className="tnum">{v}</dd></div>))}
            </dl>
            {ev.sizing && <p className="mt-2 text-sm text-ink2">Weight in profile {ev.sizing.weight_pct}% (cap {ev.sizing.cap_pct ?? "—"}%){ev.sizing.atr_stop ? ` · ATR stop ₹${num(ev.sizing.atr_stop)}` : ""}</p>}
          </Section>
        )}
        {dd && (
          <Section title="Drawdown Sentinel" accent="down">
            <p className="text-sm">From cost <b>{pct(dd.from_cost_pct)}</b> · from 52-week high <b>{pct(dd.from_high_pct)}</b> · your alert level <b>{dd.threshold_pct}%</b> → {dd.triggered ? <span className="text-down">triggered</span> : <span className="text-up">within limits</span>}</p>
            {dd.attribution?.available && (
              <>
                <p className="mt-2 text-sm text-ink2">Since {dd.attribution.since}: total {pct(dd.attribution.total_pct)} explained as —</p>
                <div className="mt-2 space-y-1.5">
                  {[["Market (β " + dd.attribution.market_beta + " × NIFTY)", dd.attribution.market_pct, "--s1"], ["Sector", dd.attribution.sector_pct, "--s2"], ["Company-specific", dd.attribution.stock_specific_pct, "--s3"]].map(([k, v, c]) => (
                    <div key={k as string} className="flex items-center gap-2 text-sm">
                      <span className="w-48 shrink-0 text-ink2">{k}</span>
                      <span className="h-2 rounded" style={{ width: `${Math.min(100, Math.abs(v as number) * 2)}%`, background: `var(${c})` }} />
                      <span className="tnum">{pct(v as number)}</span>
                    </div>
                  ))}
                </div>
                <p className="mt-2 text-sm">Dominant driver: <b>{titleCase(dd.attribution.dominant_driver)}</b></p>
              </>
            )}
          </Section>
        )}
      </div>

      {ev.tax?.applicable && (
        <Section title="Tax picture (Indian rules, FIFO)" accent="warn">
          <p className="text-sm">Short-term gain {inr(ev.tax.short_term_gain)} @ {ev.tax.rates.short_term_pct}% · Long-term gain {inr(ev.tax.long_term_gain)} @ {ev.tax.rates.long_term_pct}% (₹{num(ev.tax.rates.ltcg_exemption, 0)}/FY exempt) · Est. tax if sold now <b>{inr(ev.tax.est_tax_if_sold_now)}</b></p>
          {ev.position?.dates_estimated
            ? <p className="mt-1 text-sm text-warn">Purchase dates are unknown (holdings statement), so the short/long-term split below is an estimate — import a tradebook or CAS for exact lots.</p>
            : ev.tax.days_to_ltcg != null && <p className="mt-1 text-sm text-warn">Waiting {ev.tax.days_to_ltcg} days turns the next lot long-term and saves ~{inr(ev.tax.saving_if_wait)}.</p>}
          <LotsTable lots={ev.tax.lots} estimated={!!ev.position?.dates_estimated} />
        </Section>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        {ev.technical?.available && <TechnicalPanel t={ev.technical} />}
        {ev.fundamental?.available && <FundamentalPanel f={ev.fundamental} />}
        {r.asset_type === "stock" && !ev.fundamental?.available && (
          <Section title="Fundamental analysis" accent="warn">
            <p className="text-sm font-medium text-warn">Not available — {ev.fundamental?.reason ?? "no company data could be loaded for this stock"}.</p>
            <p className="mt-1 text-sm text-ink2">Without valuation, profits and debt data, Aquilvyn makes no buy or sell call on this stock.
              {fund?.status === "failing" ? ` Source error: ${fund.last_error}.` : ""}</p>
          </Section>
        )}
        {ev.fund?.available && <FundPanel m={ev.fund} />}
      </div>

      <Section title="Decision trace — exactly how this call was made" accent="ink">
        <p className="text-sm text-ink2">Rulebook v{trace.rulebook_version}. Rules are tried top-to-bottom; the first rule whose conditions all hold decides.</p>
        {trace.rules_evaluated && (
          <ol className="mt-2 space-y-1 text-sm">
            {trace.rules_evaluated.map((t: any, i: number) => (
              <li key={t.rule} className={t.matched ? "font-medium text-brand" : "text-ink2"}>
                {i + 1}. {t.matched ? "✔" : "✖"} <code>{t.rule}</code> → {t.action}
                {!t.matched && t.failed_condition && <span className="text-xs text-muted"> — needs {t.failed_condition.fact} {t.failed_condition.op} {JSON.stringify(t.failed_condition.expected)}, actual {JSON.stringify(t.failed_condition.actual)}</span>}
              </li>
            ))}
          </ol>
        )}
        {trace.dimension_scores && (
          <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
            <div><b>Dimension scores</b>{Object.entries(trace.dimension_scores).map(([k, v]: any) => <div key={k} className="flex justify-between border-b border-line/60 py-0.5"><span className="text-ink2">{titleCase(k)}</span><span className="tnum">{v ?? "n/a"}</span></div>)}</div>
            <div><b>Weights ({trace.composite?.profile})</b>{Object.entries(trace.composite?.weights_used ?? {}).map(([k, v]: any) => <div key={k} className="flex justify-between border-b border-line/60 py-0.5"><span className="text-ink2">{titleCase(k)}</span><span className="tnum">{Math.round(v * 100)}%</span></div>)}
              <div className="mt-1">Composite <b>{trace.composite?.score ?? "—"}</b></div></div>
          </div>
        )}
        {trace.confidence && <p className="mt-2 text-sm text-ink2">Confidence = base {trace.confidence.base} × data quality {trace.confidence.data_quality_multiplier} × signal alignment {trace.confidence.signal_alignment} ({trace.confidence.signals_agreeing}/{trace.confidence.signals_total} signals agree).</p>}
        <details className="mt-2 text-sm"><summary className="cursor-pointer text-brand">All facts fed to the rulebook</summary><pre className="mt-2 max-h-80 overflow-auto rounded-lg bg-page p-3 text-xs">{JSON.stringify(trace.facts, null, 2)}</pre></details>
      </Section>

      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="Risks & counter-arguments" accent="down">
          {fr.risks_and_counter_arguments?.length ? <ul className="list-disc space-y-1 pl-5 text-sm">{fr.risks_and_counter_arguments.map((x: string, i: number) => <li key={i}>{x}</li>)}</ul> : <p className="text-sm text-muted">No strong counter-signals.</p>}
        </Section>
        {fr.scenario && (
          <Section title="Scenario (market shock)" accent="warn">
            <ul className="space-y-1 text-sm">{fr.scenario.filter((s: any) => s.estimated_change !== undefined).map((s: any) => <li key={s.scenario} className="flex justify-between"><span>{s.scenario}</span><span className="tnum">{inr(s.estimated_change)} → {inr(s.estimated_value)}</span></li>)}</ul>
          </Section>
        )}
      </div>

      <p className="text-xs text-muted">{fr.disclaimer}</p>
    </div>
  );
}
