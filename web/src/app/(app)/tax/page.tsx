"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useMemo, useState } from "react";

import ScopeSwitcher from "@/components/ScopeSwitcher";
import { toast } from "@/components/Toast";
import { Empty, ErrorNote, Section, Skeleton, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { inr, num, tone } from "@/lib/format";
import type { Scope } from "@/lib/queries";
import { useScopeState, useUrlState } from "@/lib/useUrlState";

type Line = { key: string; label: string; gross: number; taxable: number; rate: string; tax: number };
type Action = { key: string; kind: "loss" | "gain"; instrument_id: string | null; symbol: string; name: string; profile_id: string | null; type: string;
  type_label: string; price: number; quantity: number; value: number; booked: number; st_part?: number; lt_part?: number; tax_saved: number;
  dates_estimated: boolean };
type Done = { id: string; key: string; kind: string; symbol: string; name: string | null; quantity: number; booked: number; tax_saved: number;
  done_at: string; reflected: boolean };
type Person = {
  profile_id: string; name: string; has_statement: boolean; slab_pct: number; slab_assumed: boolean; realised: number; dividends: number; interest: number;
  lines: Line[]; tax_before_cess: number; cess: number; estimated_tax: number; ltcg_exemption: { limit: number; used: number; left: number };
  carry_forward: { short_term_loss: number; long_term_loss: number; speculative_loss: number; fno_loss: number };
  advance_tax: { due_date: string; cumulative_pct: number; amount_by_then: number; note: string } | null;
  harvest: { ltcg_headroom: number; room_left: number; by: string; actions: Action[]; total_saving: number; future_saving: number } | null;
  harvest_done: Done[];
};
type Lot = { id: string; profile_name: string; symbol: string; asset: string; term: string; buy_date: string | null; sell_date: string | null;
  quantity: number; buy_value: number; sell_value: number; profit: number; taxable_profit: number; cost_override: number | null;
  effective_gain: number; days_held: number | null; flags: string[]; broker: string | null };
type Summary = { fy: string; fys: string[]; current_fy: string; people: Person[]; total: { realised: number; dividends: number; interest: number; estimated_tax: number };
  statements: { id: string; profile_name: string; filename: string; broker: string | null; period: string[] | null; checks_ok: number; checks_total: number; imported_at: string }[];
  zero_cost: Lot[]; disclaimer: string };
type Income = { id: string; profile_name: string; kind: string; symbol: string; ex_date: string | null; quantity: number; per_unit: number | null; amount: number };

const TERM: Record<string, string> = { intraday: "Intraday", short: "Short-term", long: "Long-term", business: "F&O" };
const ASSET: Record<string, string> = { equity: "Shares", equity_mf: "Equity MF", debt: "Debt ETF / bond", debt_mf: "Debt MF", other: "Other", fno: "F&O" };
const dt = (s: string | null) => (s ? new Date(s).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "—");

function params(scope: Scope, fy?: string) {
  const p = new URLSearchParams({ scope: scope.type === "portfolio" ? "household" : scope.type });
  if (scope.id && scope.type !== "household") p.set("id", scope.id);
  if (fy) p.set("fy", fy);
  return p.toString();
}

export default function TaxPage() {
  const [scope, setScope] = useScopeState();
  const [fyRaw, setFyRaw] = useUrlState("fy", "");
  const fy = fyRaw || undefined;
  const setFy = (v: string) => setFyRaw(v);
  const [tab, setTab] = useUrlState<"sales" | "income">("detail", "sales");
  const [filter, setFilter] = useUrlState("filter", "all");
  const q = useQuery({ queryKey: ["tax", scope, fy], queryFn: () => api<Summary>(`/tax/summary?${params(scope, fy)}`), placeholderData: keepPreviousData });
  const activeFy = q.data?.fy;
  const lots = useQuery({ queryKey: ["tax-lots", scope, activeFy], enabled: !!activeFy, queryFn: () => api<Lot[]>(`/tax/realised?${params(scope, activeFy)}`) });
  const income = useQuery({ queryKey: ["tax-income", scope, activeFy], enabled: !!activeFy, queryFn: () => api<Income[]>(`/tax/income?${params(scope, activeFy)}`) });
  const d = q.data;
  const shown = useMemo(() => (lots.data ?? []).filter((l) => filter === "all" || l.term === filter || l.asset === filter || (filter === "flagged" && l.flags.some((f) => !f.startsWith("bonus") && f !== "cost_checked" && !(f === "zero_cost" && l.cost_override != null) && !f.startsWith("cost_estimated")))), [lots.data, filter]);
  const hasData = !!d && (d.statements.length > 0 || d.people.some((p) => p.has_statement));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Tax</h1>
          <p className="text-sm text-ink2">Capital gains, dividends and the estimated tax per person — from your brokers&apos; tax / capital-gains statements.</p>
        </div>
        <div className="flex gap-2">
          <select className="input w-auto" aria-label="Financial year" value={activeFy ?? ""} onChange={(e) => setFy(e.target.value)}>
            {(d?.fys ?? []).map((f) => <option key={f} value={f}>FY {f}{f === d?.current_fy ? " (current)" : ""}</option>)}
          </select>
          <ScopeSwitcher value={scope} onChange={setScope} />
        </div>
      </div>
      <ErrorNote error={q.error} />
      {q.isLoading ? <Skeleton className="h-64" /> : !hasData ? (
        <Empty title={`No tax statement for FY ${activeFy ?? ""} yet`}>
          <p className="max-w-xl text-sm text-ink2">
            Download your broker&apos;s tax report and import it — Zerodha: Console → Reports → <b>Tax P&amp;L</b>; Groww / Upstox / ICICI Direct / others:
            the <b>capital gains</b> report (Excel or CSV). Any file with sell dates, buy &amp; sell values works; Aquilvyn checks its own totals.
          </p>
          <Link href="/import" className="btn-primary mt-3">Import a tax statement</Link>
        </Empty>
      ) : d && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile label={`Realised gains · FY ${d.fy}`} value={<span className={tone(d.total.realised)}>{inr(d.total.realised, { compact: true })}</span>} sub="from sales (taxable figure)" />
            <StatTile label="Dividends & interest" value={inr(d.total.dividends + d.total.interest, { compact: true })} sub="taxed at your slab rate" />
            <StatTile label="Estimated tax" value={inr(d.total.estimated_tax, { compact: true })} sub="on this investment income, incl. 4% cess"
                      hint="Only what Aquilvyn sees — not salary, deductions or TDS already paid. Surcharge not included." />
            <StatTile label="Tax-free LTCG left" value={inr(d.people.reduce((a, p) => a + p.ltcg_exemption.left, 0), { compact: true })}
                      sub={`₹1.25 L per person per FY · ${d.people.length} ${d.people.length === 1 ? "person" : "people"}`} />
          </div>

          {d.zero_cost.length > 0 && <ZeroCost lots={d.zero_cost} />}

          {d.people.map((p) => <PersonCard key={p.profile_id} p={p} current={d.fy === d.current_fy} fy={d.fy} />)}

          <Section title={`Detail · FY ${d.fy}`} action={
            <div className="flex gap-1 text-sm">
              <button className={tab === "sales" ? "btn-primary py-1" : "btn-ghost py-1"} onClick={() => setTab("sales")}>Sales ({lots.data?.length ?? 0})</button>
              <button className={tab === "income" ? "btn-primary py-1" : "btn-ghost py-1"} onClick={() => setTab("income")}>Dividends &amp; interest ({income.data?.length ?? 0})</button>
            </div>}>
            {tab === "sales" ? (
              <>
                <div className="mb-2 flex flex-wrap gap-1 text-xs">
                  {["all", "short", "long", "intraday", "equity", "equity_mf", "flagged"].map((f) => (
                    <button key={f} onClick={() => setFilter(f)} className={`chip ${filter === f ? "bg-brand/15 text-brand" : "bg-page text-ink2"}`}>
                      {f === "all" ? "All" : f === "flagged" ? "Needs attention" : TERM[f] ?? ASSET[f]}
                    </button>
                  ))}
                </div>
                <div className="max-h-[28rem] overflow-auto">
                  <table className="w-full text-sm">
                    <thead className="sticky top-0 bg-surface"><tr>
                      <th className="th">Sold</th><th className="th">Holding</th><th className="th">Type</th><th className="th hidden lg:table-cell">Person</th>
                      <th className="th text-right">Qty</th><th className="th text-right">Buy</th><th className="th text-right">Sell</th><th className="th text-right">Gain</th>
                    </tr></thead>
                    <tbody>{shown.map((l) => (
                      <tr key={l.id} className="border-t border-line">
                        <td className="td whitespace-nowrap">{dt(l.sell_date)}</td>
                        <td className="td">{l.symbol}{l.flags.some((f) => f.startsWith("bonus")) ? <span className="chip ml-1 bg-up/15 text-up">bonus</span>
                          : l.flags.includes("zero_cost") && <span className="chip ml-1 bg-warn/15 text-warn">{l.flags.some((f) => f.startsWith("cost_estimated")) ? "cost estimated" : l.cost_override != null ? "cost entered" : "cost missing"}</span>}
                          <div className="text-xs text-muted">bought {dt(l.buy_date)}{l.days_held != null ? ` · ${l.days_held} days` : ""}</div></td>
                        <td className="td whitespace-nowrap text-xs">{TERM[l.term] ?? l.term} · {ASSET[l.asset] ?? l.asset}</td>
                        <td className="td hidden text-ink2 lg:table-cell">{l.profile_name}</td>
                        <td className="td tnum text-right">{num(l.quantity, 3)}</td>
                        <td className="td tnum text-right">{inr(l.cost_override ?? l.buy_value)}</td>
                        <td className="td tnum text-right">{inr(l.sell_value)}</td>
                        <td className={`td tnum text-right font-medium ${tone(l.effective_gain)}`}>{inr(l.effective_gain)}</td>
                      </tr>
                    ))}</tbody>
                  </table>
                  {!shown.length && <p className="py-6 text-center text-sm text-muted">No sales in this view.</p>}
                </div>
              </>
            ) : (
              <div className="max-h-[28rem] overflow-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-surface"><tr><th className="th">Ex-date</th><th className="th">Holding</th><th className="th">Type</th>
                    <th className="th hidden lg:table-cell">Person</th><th className="th text-right">Qty</th><th className="th text-right">Per share</th><th className="th text-right">Amount</th></tr></thead>
                  <tbody>{(income.data ?? []).map((i) => (
                    <tr key={i.id} className="border-t border-line"><td className="td whitespace-nowrap">{dt(i.ex_date)}</td><td className="td">{i.symbol}</td>
                      <td className="td capitalize">{i.kind}</td><td className="td hidden text-ink2 lg:table-cell">{i.profile_name}</td>
                      <td className="td tnum text-right">{num(i.quantity, 3)}</td><td className="td tnum text-right">{i.per_unit != null ? `₹${num(i.per_unit)}` : "—"}</td>
                      <td className="td tnum text-right font-medium">{inr(i.amount)}</td></tr>
                  ))}</tbody>
                </table>
              </div>
            )}
          </Section>

          <Section title="Statements used" action={<Link href="/import" className="text-sm text-brand">Import another →</Link>}>
            <ul className="space-y-1 text-sm">
              {d.statements.map((s) => (
                <li key={s.id} className="flex flex-wrap items-center gap-2">
                  <b>{s.broker ?? "Broker"}</b><span className="text-ink2">· {s.profile_name} · {s.period ? `${dt(s.period[0])} – ${dt(s.period[1])}` : "period unknown"} · {s.filename}</span>
                  {s.checks_total > 0 && (s.checks_ok === s.checks_total
                    ? <span className="chip bg-up/15 text-up">✔ {s.checks_ok}/{s.checks_total} totals reconcile</span>
                    : <span className="chip bg-warn/15 text-warn">! {s.checks_total - s.checks_ok} total(s) don&apos;t reconcile</span>)}
                </li>
              ))}
            </ul>
          </Section>
          <p className="text-xs text-muted">{d.disclaimer}</p>
        </>
      )}
    </div>
  );
}

function PersonCard({ p, current, fy }: { p: Person; current: boolean; fy: string }) {
  const cf = p.carry_forward;
  const cfTotal = cf.short_term_loss + cf.long_term_loss + cf.speculative_loss + cf.fno_loss;
  return (
    <Section title={p.name} action={<span className="text-sm text-ink2">Estimated tax <b className="tnum text-ink">{inr(p.estimated_tax)}</b></span>}>
      {!p.has_statement && <p className="mb-2 text-sm text-ink2">No tax statement imported for {p.name} this year — only tax-saving ideas from current holdings are shown.</p>}
      {p.has_statement && (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr><th className="th">Income</th><th className="th text-right">Gain</th><th className="th text-right">Taxable</th><th className="th text-right">Rate</th><th className="th text-right">Tax</th></tr></thead>
            <tbody>
              {p.lines.map((l) => (
                <tr key={l.key} className="border-t border-line"><td className="td">{l.label}</td><td className={`td tnum text-right ${tone(l.gross)}`}>{inr(l.gross)}</td>
                  <td className="td tnum text-right">{inr(l.taxable)}</td><td className="td text-right text-xs text-ink2">{l.rate}</td><td className="td tnum text-right">{inr(l.tax)}</td></tr>
              ))}
              <tr className="border-t border-line text-ink2"><td className="td" colSpan={4}>Health &amp; education cess (4%)</td><td className="td tnum text-right">{inr(p.cess)}</td></tr>
              <tr className="border-t border-line font-medium"><td className="td" colSpan={4}>Estimated tax on investment income</td><td className="td tnum text-right">{inr(p.estimated_tax)}</td></tr>
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-3 grid gap-3 text-sm md:grid-cols-3">
        <div className="rounded-lg bg-page p-3">
          <div className="font-medium">Long-term exemption (₹1.25 L)</div>
          <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-line"><div className="h-full bg-brand" style={{ width: `${Math.min(100, (p.ltcg_exemption.used / p.ltcg_exemption.limit) * 100)}%` }} /></div>
          <div className="mt-1 text-ink2">Used {inr(p.ltcg_exemption.used)} · <b className="text-ink">{inr(p.ltcg_exemption.left)} left</b></div>
        </div>
        <div className="rounded-lg bg-page p-3">
          <div className="font-medium">Advance tax</div>
          {p.advance_tax && p.estimated_tax > 0 ? (
            <div className="mt-1 text-ink2">By <b className="text-ink">{dt(p.advance_tax.due_date)}</b>: {p.advance_tax.cumulative_pct}% of the year&apos;s tax — about{" "}
              <b className="text-ink">{inr(p.advance_tax.amount_by_then)}</b> on this income. <span className="text-xs">{p.advance_tax.note}</span></div>
          ) : <div className="mt-1 text-ink2">Nothing due on this income.</div>}
        </div>
        <div className="rounded-lg bg-page p-3">
          <div className="font-medium">Slab rate &amp; losses</div>
          <div className="mt-1 text-ink2">Slab {p.slab_pct}%{p.slab_assumed && <> (assumed — <Link href="/family" className="text-brand underline">set it on the profile</Link>)</>}.
            {cfTotal > 0 ? <> Losses to carry forward: <b className="text-ink">{inr(cfTotal)}</b> (8 years; intraday 4) — file your return on time to keep them.</> : " No losses to carry forward."}</div>
        </div>
      </div>
      {current && p.harvest && (p.harvest.actions.length > 0 || p.harvest_done.length > 0) && <Harvest p={p} h={p.harvest} fy={fy} />}
    </Section>
  );
}

const unitWord = (a: { type: string }) => (a.type === "stock" ? "shares" : "units");
const qty = (a: { type: string; quantity: number }) => (a.type === "mutual_fund" ? num(a.quantity, 3) : num(a.quantity, 0));

function Harvest({ p, h, fy }: { p: Person; h: NonNullable<Person["harvest"]>; fy: string }) {
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["tax"] });
  const done = useMutation({
    mutationFn: (a: Action) => api("/tax/harvest/done", { body: { profile_id: p.profile_id, fy, key: a.key, kind: a.kind, instrument_id: a.instrument_id,
      symbol: a.symbol, name: a.name, quantity: a.quantity, booked: a.booked, st_part: a.st_part ?? 0, lt_part: a.lt_part ?? 0, tax_saved: a.tax_saved } }),
    onSuccess: (_r, a) => { refresh(); toast("ok", `Marked done: ${a.name}`, "The remaining ideas and savings are updated. Re-import your holdings / tax statement when the trade settles."); },
    onError: (e: Error) => toast("error", "Couldn't mark it done", e.message),
  });
  const undo = useMutation({
    mutationFn: (id: string) => api(`/tax/harvest/done/${id}`, { method: "DELETE" }),
    onSuccess: () => { refresh(); toast("info", "Undone", "Moved back to the to-do list."); },
  });
  const save = h.actions.filter((a) => a.kind === "loss" && a.tax_saved > 0);
  const gains = h.actions.filter((a) => a.kind === "gain");
  const later = h.actions.filter((a) => a.kind === "loss" && a.tax_saved <= 0);
  const link = (a: Action) => a.instrument_id
    ? <Link className="font-semibold text-brand hover:underline" href={`/holdings/${a.instrument_id}?profile=${p.profile_id}&symbol=${encodeURIComponent(a.symbol)}`}>{a.name}{a.dates_estimated ? " *" : ""}</Link>
    : <b>{a.name}</b>;
  const row = (a: Action, what: React.ReactNode, result: React.ReactNode) => (
    <li key={a.key} className="grid items-center gap-2 border-t border-line py-2 sm:grid-cols-[minmax(10rem,1.2fr)_2fr_auto_auto]">
      <div>{link(a)}<div className="text-xs text-muted">{a.type_label} · ₹{num(a.price)}</div></div>
      <div className="text-sm">{what}</div>
      <div className="text-right text-sm">{result}</div>
      <button className="btn-ghost py-1 text-sm" disabled={done.isPending} onClick={() => done.mutate(a)}>✔ Mark done</button>
    </li>
  );
  return (
    <div className="mt-4 rounded-xl border-2 border-brand/40 p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-base font-bold text-brand">Tax harvesting · do these before {dt(h.by)}</h3>
        {h.total_saving > 0 && <span className="chip bg-up text-sm font-bold text-white">Save ≈ {inr(h.total_saving)} tax this year</span>}
      </div>

      {save.length > 0 && (
        <div className="mt-3">
          <p className="text-sm font-bold text-up">1 · Book these losses — each one cuts this year&apos;s tax</p>
          <p className="mt-1 rounded-lg bg-page px-3 py-2 text-xs text-ink2">
            <b className="text-ink">How does this save tax{h.ltcg_headroom <= 0 ? " when my ₹1.25 L tax-free limit is already used" : ""}?</b>{" "}
            {h.ltcg_headroom <= 0
              ? "Because it's used up, every extra rupee of profit you booked this year is taxed — 12.5% on long-term, 20% on short-term (plus 4% cess). "
              : "Profits you booked this year are taxed (12.5% long-term above ₹1.25 L, 20% short-term, plus 4% cess). "}
            A loss you book is subtracted from those taxed profits before tax is worked out: book a ₹10,000 loss and ₹10,000 less profit is taxed.
            The tax-free limit isn&apos;t touched — the loss removes profit that would otherwise be taxed. Buying the share back the next day keeps your investment the same.
          </p>
          <ul className="mt-1">{save.map((a) => row(a,
            <>Sell <b>{qty(a)} {unitWord(a)}</b> (≈ {inr(a.value)}) → books a <b className="text-down">{inr(a.booked)}</b> loss. Buy back the next day if you still want to own it.</>,
            <span className="font-bold text-up">saves {inr(a.tax_saved)}</span>))}</ul>
        </div>
      )}
      {gains.length > 0 && (
        <div className="mt-3">
          <p className="text-sm font-bold text-brand">{save.length ? "2" : "1"} · Book long-term gains tax-free — {inr(h.ltcg_headroom)} of your ₹1.25 L exemption is unused</p>
          <ul className="mt-1">{gains.map((a) => row(a,
            <>Sell <b>{qty(a)} {unitWord(a)}</b> (≈ {inr(a.value)}) and buy back the next day → books <b className="text-up">{inr(a.booked)}</b> gain at 0% tax and raises your purchase price.</>,
            <span className="font-medium text-ink">≈ {inr(a.tax_saved)} less tax later</span>))}</ul>
        </div>
      )}
      {!save.length && !gains.length && (
        <p className="mt-2 text-sm text-ink2">Nothing to do right now: {h.ltcg_headroom <= 0 ? "your ₹1.25 L long-term exemption is used and " : ""}no losses would cut this year&apos;s tax.</p>
      )}
      {later.length > 0 && (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-ink2">{later.length} more loss(es) that don&apos;t cut tax this year (no gains left to offset) — useful only if you book gains later</summary>
          <ul className="mt-1">{later.map((a) => row(a, <>Sell {qty(a)} {unitWord(a)} → books {inr(a.booked)} loss (carries forward 8 years).</>, <span className="text-ink2">saves ₹0 now</span>))}</ul>
        </details>
      )}
      {p.harvest_done.length > 0 && (
        <div className="mt-3 border-t border-line pt-2">
          <p className="text-sm font-bold text-ink">Done ✔</p>
          <ul className="mt-1 space-y-1 text-sm">{p.harvest_done.map((d) => (
            <li key={d.id} className="flex flex-wrap items-center gap-2">
              <span className="chip bg-up/15 text-up">✔</span><b>{d.name ?? d.symbol}</b>
              <span className="text-ink2">sold {num(d.quantity, 3)} · booked {inr(d.booked)}{d.tax_saved ? ` · saves ${inr(d.tax_saved)}` : ""} · {dt(d.done_at)}</span>
              {d.reflected ? <span className="chip bg-page text-ink2">confirmed by your tax statement</span>
                : <span className="chip bg-page text-ink2">counted until your next tax statement shows it</span>}
              <button className="text-xs text-down" onClick={() => undo.mutate(d.id)}>Undo</button>
            </li>
          ))}</ul>
        </div>
      )}
      <p className="mt-3 text-xs text-muted">Sales use your oldest units first (FIFO), so the quantities above include them. Check exit loads on mutual funds and STT / brokerage.
        {h.actions.some((a) => a.dates_estimated) ? " * purchase dates estimated from a holdings statement — whether a lot is long-term may differ; import a tradebook or CAS for exact lots." : ""}</p>
    </div>
  );
}

function ZeroCost({ lots }: { lots: Lot[] }) {
  const qc = useQueryClient();
  const [cost, setCost] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: ({ id, v, bonus }: { id: string; v?: number | null; bonus?: boolean }) =>
      api(`/tax/realised/${id}`, { method: "PATCH", body: bonus ? { bonus: true } : { cost: v ?? null } }),
    onSuccess: () => { ["tax", "tax-lots"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })); toast("ok", "Saved", "The gain and estimated tax are updated."); },
    onError: (e: Error) => toast("error", "Couldn't save", e.message),
  });
  const bonusOf = (l: Lot) => l.flags.find((f) => f.startsWith("bonus"));
  const estOf = (l: Lot) => l.flags.find((f) => f.startsWith("cost_estimated"))?.split(":")[1];
  const open = lots.filter((l) => !bonusOf(l) && l.cost_override == null);
  const settled = lots.filter((l) => !open.includes(l));
  return (
    <div role={open.length ? "alert" : undefined} className={`rounded-lg border p-3 text-sm ${open.length ? "border-warn/40 bg-warn/10" : "border-line bg-surface"}`}>
      <b className={open.length ? "text-warn" : "text-ink"}>
        {lots.length} sale(s) show a buy value of ₹0 on the statement{open.length ? ` — ${open.length} need your input` : " — all resolved automatically"}
      </b>
      <ul className="mt-2 space-y-1.5">
        {settled.map((l) => (
          <li key={l.id} className="flex flex-wrap items-center gap-2">
            <span className="w-32 truncate font-medium">{l.symbol}</span>
            {bonusOf(l) ? <span className="chip bg-up/15 text-up">✔ Bonus shares — ₹0 cost is correct by law</span>
              : <span className="chip bg-brand/15 text-brand">≈ cost {inr(l.cost_override)} estimated from {estOf(l) ?? "your data"}</span>}
            <span className="text-xs text-ink2">{bonusOf(l)?.replace(/^bonus:/, "") ?? `sold ${dt(l.sell_date)} · ${num(l.quantity, 3)} units`}</span>
            <details className="text-xs"><summary className="cursor-pointer text-brand">change</summary>
              <span className="mt-1 flex items-center gap-2">
                <input className="input w-28 py-1" inputMode="decimal" placeholder="total cost ₹" value={cost[l.id] ?? ""} onChange={(e) => setCost({ ...cost, [l.id]: e.target.value })} />
                <button className="btn-ghost py-1" disabled={!cost[l.id]} onClick={() => save.mutate({ id: l.id, v: Number(cost[l.id]) })}>Save cost</button>
                {!bonusOf(l) && <button className="btn-ghost py-1" onClick={() => save.mutate({ id: l.id, bonus: true })}>It&apos;s a bonus</button>}
              </span>
            </details>
          </li>
        ))}
        {open.map((l) => (
          <li key={l.id} className="flex flex-wrap items-center gap-2">
            <span className="w-32 truncate font-medium">{l.symbol}</span>
            <span className="text-ink2">sold {dt(l.sell_date)} for {inr(l.sell_value)} ({num(l.quantity, 3)} units, got {dt(l.buy_date)}) · {l.profile_name}</span>
            <button className="btn-ghost py-1" onClick={() => save.mutate({ id: l.id, bonus: true })} title="Bonus shares have a cost of ₹0 by law">It&apos;s a bonus (₹0 is right)</button>
            <span className="text-xs text-muted">or</span>
            <input className="input w-32 py-1" inputMode="decimal" placeholder="total cost ₹" value={cost[l.id] ?? ""}
                   onChange={(e) => setCost({ ...cost, [l.id]: e.target.value })} aria-label={`Total purchase cost of ${l.symbol}`} />
            <button className="btn-ghost py-1" disabled={save.isPending || !cost[l.id]} onClick={() => save.mutate({ id: l.id, v: Number(cost[l.id]) })}>Save cost</button>
          </li>
        ))}
      </ul>
      {open.length > 0 && <p className="mt-2 text-xs text-ink2">No bonus or split was found for these around the purchase date, and there&apos;s no earlier lot to take the cost from — usually shares transferred in (gift, demat move).</p>}
    </div>
  );
}
