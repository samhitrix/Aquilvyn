"use client";

import Link from "next/link";

import AllocationBar from "@/components/AllocationBar";
import { PerformanceChart } from "@/components/charts";
import LivePrice from "@/components/LivePrice";
import RecCard from "@/components/RecCard";
import ScopeSwitcher from "@/components/ScopeSwitcher";
import { PriceBanner } from "@/components/StatusBanners";
import { Delta, Empty, ErrorNote, Section, Skeleton, StatTile } from "@/components/ui";
import { inr, pct, titleCase, tone } from "@/lib/format";
import { useDashboard, useRunAdvisor } from "@/lib/queries";
import { useScopeState } from "@/lib/useUrlState";
import type { Holding } from "@/lib/types";
import { SortTh, useSort } from "@/lib/useSort";

const XL_COLS: Record<number, string> = { 4: "xl:grid-cols-4", 5: "xl:grid-cols-5", 6: "xl:grid-cols-6" };

export default function DashboardPage() {
  const [scope, setScope] = useScopeState();
  const { data, isLoading, error } = useDashboard(scope);
  const run = useRunAdvisor();
  const s = data?.holdings?.summary;
  // prefer this FY's figures from tax statements; otherwise what the ledger's own sell / dividend rows add up to
  const realised = s ? (s.fy ? s.fy_realised ?? 0 : s.realised_pnl) : 0;
  const income = s ? (s.fy ? s.fy_income ?? 0 : s.dividends) : 0;
  const stats = data?.advisor?.last_run?.stats;
  const regime = data?.advisor?.last_run?.regime;
  const members = scope.type === "group" ? data?.groups?.find((g) => g.id === scope.id)?.profile_ids ?? [] : [];
  const memberScores = members.map((id) => stats?.health?.[id]?.score).filter((v): v is number => v != null);
  const health = scope.type === "profile" && scope.id ? stats?.health?.[scope.id]?.score
    : scope.type === "group" ? (memberScores.length ? memberScores.reduce((a, b) => a + b, 0) / memberScores.length : null)
    : stats?.family_health;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">{scope.type === "household" ? "Family dashboard" : "Dashboard"}</h1>
          <p className="text-sm text-ink2">Live values · {data?.holdings?.as_of ?? "…"}</p>
        </div>
        <div className="flex gap-2">
          <ScopeSwitcher value={scope} onChange={setScope} />
          <button className="btn-primary" onClick={() => run.mutate(scope.type === "profile" ? scope.id : undefined)} disabled={run.isPending}>
            {run.isPending ? "Analysing…" : "✦ Run advisor"}
          </button>
        </div>
      </div>
      <ErrorNote error={error ?? run.error} />
      {data?.errors?.holdings && (
        <p role="alert" className="rounded-lg bg-down/10 px-3 py-2 text-sm text-down">Couldn&apos;t load holdings right now ({data.errors.holdings}). Your transactions are safe — retrying automatically.</p>
      )}
      {(() => {
        const me = data?.profiles?.find((p) => p.relationship === "self");
        return me && !me.pan_masked && !me.date_of_birth ? (
          <div className="rounded-lg border border-brand/40 bg-brand/10 px-3 py-2 text-sm">
            <b>Finish setting up your profile</b> — add your PAN and date of birth so statements match you automatically and advice fits your age.{" "}
            <Link href="/welcome" className="font-semibold text-brand underline">Complete now →</Link>
          </div>) : null;
      })()}
      {data?.holdings && <PriceBanner unpriced={data.holdings.unpriced_count} stale={data.holdings.stale_count} npsNavAsOf={data.holdings.nps_nav_as_of} />}

      {isLoading ? (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 2xl:grid-cols-6">{[0, 1, 2, 3, 4, 5].map((i) => <Skeleton key={i} className="h-24" />)}</div>
      ) : data?.errors?.holdings && !s ? null : !s || s.holdings_count === 0 ? (
        <Empty title="No holdings yet">
          Add a transaction, import a broker tradebook or a CAMS/KFintech CAS, or add your EPF/PPF account.
          <div className="mt-3 flex justify-center gap-2"><Link href="/transactions" className="btn-primary">Add transaction</Link><Link href="/import" className="btn-ghost">Import</Link></div>
        </Empty>
      ) : (
        <>
          {/* realised gains / dividends only when there is any — holdings statements carry neither */}
          <div className={`grid gap-3 sm:grid-cols-2 lg:grid-cols-3 ${XL_COLS[4 + (realised ? 1 : 0) + (income ? 1 : 0)]}`}>
            <StatTile label="Net worth" value={inr(s.market_value, { compact: true })} sub={`Invested ${inr(s.invested, { compact: true })}`} />
            <StatTile label="Today" value={<span className={tone(s.day_change)}>{inr(s.day_change)}</span>} delta={s.day_change_pct} />
            <StatTile label="Total gain" value={<span className={tone(s.unrealised_pnl)}>{inr(s.unrealised_pnl, { compact: true })}</span>} delta={s.unrealised_pct}
                      sub={s.xirr_pct != null ? `XIRR ${pct(s.xirr_pct, 1, false)} p.a.` : "absolute return"}
                      hint="XIRR is the annualised return accounting for when each rupee went in — shown once money has been invested for a year (AMFI convention)." />
            <StatTile label="Portfolio health" value={health != null ? `${Math.round(health)}/100` : "—"}
                      sub={regime?.regime ? `Market: ${titleCase(regime.regime)}` : "Run the advisor"} hint={regime?.playbook} />
            {realised ? <Link href="/tax"><StatTile label={s.fy ? `Realised gains · FY ${s.fy}` : "Realised gains"} value={<span className={tone(realised)}>{inr(realised, { compact: true })}</span>} sub={s.fy ? "from your tax statements →" : "from sales so far"} /></Link> : null}
            {income ? <Link href="/tax"><StatTile label={s.fy ? `Dividends & interest · FY ${s.fy}` : "Dividends & interest"} value={inr(income, { compact: true })} sub={s.fy ? "taxed at your slab →" : `${s.holdings_count} holdings`} /></Link> : null}
          </div>

          <div className="grid gap-4 lg:grid-cols-3 2xl:grid-cols-4">
            <Section title="Top actions this week" className="lg:col-span-2" action={<Link href="/advisor" className="text-sm text-brand">Advisor inbox →</Link>}>
              {data?.advisor?.top_actions?.length ? (
                <div className="space-y-3">{data.advisor.top_actions.slice(0, 4).map((r) => <RecCard key={r.id} rec={r} compact />)}</div>
              ) : (
                <p className="text-sm text-ink2">{data?.advisor?.last_run ? "Nothing needs your attention — doing nothing is a valid decision." : "Run the advisor to analyse every holding."}</p>
              )}
              <p className={`mt-3 text-xs ${data?.advisor?.ai.mode === "rules_only" ? "text-warn" : "text-up"}`}>{data?.advisor?.ai.mode === "rules_only" ? "○ Rules-only — no AI model connected (Settings → AI providers)" : `✔ AI review: ${data?.advisor?.ai.reviewers.join(", ")}`}</p>
            </Section>
            <Section title="Asset allocation">
              <AllocationBar data={s.allocation} />
            </Section>
            <Section title="Markets" className="hidden 2xl:block" action={<Link href="/markets" className="text-sm text-brand">More →</Link>}>
              <MarketList indices={data?.market?.indices ?? []} />
            </Section>
          </div>

          {data?.holdings?.profiles && data.holdings.profiles.length > 1 && (
            <Section title="Family members">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
                {data.holdings.profiles.map((p) => (
                  <button key={p.profile_id} onClick={() => setScope({ type: "profile", id: p.profile_id })} className="rounded-lg border border-line p-3 text-left hover:bg-page">
                    <div className="flex items-center justify-between"><span className="font-medium">{p.name}</span><span className="text-xs capitalize text-muted">{p.relationship}</span></div>
                    <div className="tnum mt-1 text-lg font-semibold">{inr(p.market_value, { compact: true })}</div>
                    <div className="text-sm"><Delta v={p.unrealised_pct} /> <span className="text-ink2">· XIRR {pct(p.xirr_pct, 1, false)}</span></div>
                    {stats?.health?.[p.profile_id]?.score != null && <div className="mt-1 text-xs text-ink2">Health {Math.round(stats.health[p.profile_id].score)}/100</div>}
                  </button>
                ))}
              </div>
            </Section>
          )}

          <div className="grid gap-4 lg:grid-cols-3 2xl:grid-cols-2">
            <Section title="Performance" className="lg:col-span-2 2xl:col-span-1"><PerformanceChart points={data?.performance ?? []} /></Section>
            <Section title="Markets" className="2xl:hidden" action={<Link href="/markets" className="text-sm text-brand">More →</Link>}>
              <MarketList indices={data?.market?.indices ?? []} />
            </Section>
            <HoldingsBlock className="hidden 2xl:block" rows={data?.holdings?.holdings ?? []} />
          </div>

          <HoldingsBlock className="2xl:hidden" rows={data?.holdings?.holdings ?? []} />
          <p className="text-xs text-muted">Analysis for personal research — not SEBI-registered investment advice. No orders are placed in this phase.</p>
        </>
      )}
    </div>
  );
}

function MarketList({ indices }: { indices: { symbol: string; name?: string; price: number; prev_close: number }[] }) {
  return (
    <ul className="space-y-2 text-sm">
      {indices.slice(0, 6).map((i) => (
        <li key={i.symbol} className="flex justify-between gap-2"><span className="truncate text-ink2">{i.name}</span><LivePrice symbol={i.symbol} price={i.price} prevClose={i.prev_close} /></li>
      ))}
    </ul>
  );
}

function HoldingsBlock({ rows, className }: { rows: Holding[]; className?: string }) {
  const total = rows.reduce((a, h) => a + h.market_value, 0) || 1;
  const [sorted, sort] = useSort(rows.slice(0, 8), {
    name: (h) => h.name, owner: (h) => h.profile_name, price: (h) => h.price, value: (h) => h.market_value, gain: (h) => h.unrealised_pct,
  }, "top");
  return (
    <Section title="Biggest holdings" className={className} action={<Link href="/holdings" className="text-sm text-brand">All holdings →</Link>}>
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead><tr><SortTh s={sort} k="name">Holding</SortTh><SortTh s={sort} k="owner">Owner</SortTh><SortTh s={sort} k="price" className="th text-right">Price</SortTh>
            <SortTh s={sort} k="value" className="th text-right">Value</SortTh><SortTh s={sort} k="value" className="th hidden text-right xl:table-cell">Weight</SortTh>
            <SortTh s={sort} k="gain" className="th text-right">Gain</SortTh></tr></thead>
          <tbody>
            {sorted.map((h) => (
              <tr key={`${h.profile_id}-${h.instrument_id}`} className="border-t border-line">
                <td className="td"><Link className="hover:text-brand" href={`/holdings/${h.instrument_id}?profile=${h.profile_id}&symbol=${encodeURIComponent(h.symbol)}`}>{h.name}</Link></td>
                <td className="td text-ink2">{h.profile_name}</td>
                <td className="td text-right">{h.price !== null ? <LivePrice symbol={h.symbol} price={h.price} prevClose={h.prev_close} /> : <span className="text-muted">accrual</span>}</td>
                <td className="td tnum text-right">{inr(h.market_value)}</td>
                <td className="td tnum hidden text-right text-ink2 xl:table-cell">{((h.market_value / total) * 100).toFixed(1)}%</td>
                <td className="td text-right"><Delta v={h.unrealised_pct} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}
