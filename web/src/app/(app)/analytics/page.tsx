"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useMemo, useState } from "react";
import Donut, { OTHER_COLOR, withColors } from "@/components/Donut";
import ScopeSwitcher from "@/components/ScopeSwitcher";
import { toast } from "@/components/Toast";
import { Empty, ErrorNote, Section, Skeleton, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { inr } from "@/lib/format";
import { type Scope, useGroups, usePortfolios, useRecs } from "@/lib/queries";
import type { ConsolidationGroup } from "@/lib/types";
import { useScopeState, useUrlState } from "@/lib/useUrlState";
import { SortTh, useSort } from "@/lib/useSort";

type Part = { key: string; label: string; value: number; pct: number };
type ClassHolding = { name: string; symbol: string; kind: string; value: number; pct: number; people: string[] };
type Exposure = {
  basis: string; asset: string; lookthrough: boolean; total: number; equity_total: number; notes: string[];
  asset_classes: (Part & { holdings?: ClassHolding[] })[]; products: Part[]; caps: Part[];
  sectors: (Part & { holdings: { name: string; value: number }[] })[];
  holdings: { name: string; symbol: string; value: number; pct: number; cap: string; kind: string }[];
  lookthrough_detail?: LookThrough | null;
};
type LookThrough = {
  stocks: { isin: string; name: string; direct: number; via_funds: number; total: number; pct: number; funds: { name: string; value: number; weight: number }[] }[];
  overlap: { a: string; b: string; overlap_pct: number; common_count: number; common: { name: string; a_weight: number; b_weight: number }[] }[];
  covered: { name: string; scheme: string; as_of: string | null; stale: boolean }[];
  missing: string[]; stale: string[]; fund_value: number; via_funds_total: number;
};

// entity → colour stays fixed whatever the filter (colour follows the thing, not its rank)
const CLASS_COLOR: Record<string, string> = {
  equity: "var(--s1)", debt: "var(--s3)", gold: "var(--s4)", hybrid: "var(--s7)", real_estate: "var(--s2)",
  alternative: "var(--s5)", cash: "var(--s6)", other: OTHER_COLOR,
};
const PRODUCT_COLOR: Record<string, string> = {
  Stocks: "var(--s1)", ETFs: "var(--s2)", "Mutual funds": "var(--s3)", "Retirement (EPF/PPF/NPS)": "var(--s4)",
  "Deposits & bonds": "var(--s5)", Gold: "var(--s6)", "REITs / InvITs": "var(--s7)", Other: OTHER_COLOR,
};
const CAP_COLOR: Record<string, string> = {
  large: "rgb(var(--ink))", mid: "rgb(var(--ink2))", small: "rgb(var(--muted))", flexi: "var(--s7)", unknown: "rgb(var(--line))",
};
const CAP_SHORT: Record<string, string> = { large: "Large", mid: "Mid", small: "Small", flexi: "Flexi/multi", unknown: "—" };

export default function AnalyticsPage() {
  const [scope, setScope] = useScopeState();
  const [basis, setBasis] = useUrlState<"current" | "invested">("basis", "current");
  const [asset, setAsset] = useUrlState("asset", "all");
  const [lt, setLt] = useUrlState<"on" | "off">("lookthrough", "on");
  const lookthrough = lt === "on";
  const setLookthrough = (v: boolean) => setLt(v ? "on" : "off");
  const q = useQuery({
    queryKey: ["exposure", scope, basis, asset, lookthrough],
    queryFn: () => api<Exposure>("/holdings/exposure", { query: { scope: scope.type, id: scope.id, basis, asset, lookthrough } }),
    placeholderData: keepPreviousData, staleTime: 30_000,
  });
  const d = q.data;
  const stockSlices = d ? withColors(d.holdings.map((h) => ({ key: h.symbol, label: h.name, value: h.value, pct: h.pct }))) : [];
  const equityShare = (k: string) => d?.products.find((p) => p.key === k);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Analytics</h1>
          <p className="text-sm text-ink2">What you own, by asset class, product, sector and company size.</p>
        </div>
        <ScopeSwitcher value={scope} onChange={setScope} />
      </div>

      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 rounded-xl border border-line bg-surface px-4 py-3 text-sm">
        <fieldset className="flex items-center gap-3">
          <legend className="sr-only">Value basis</legend>
          {(["current", "invested"] as const).map((b) => (
            <label key={b} className="inline-flex items-center gap-1.5"><input type="radio" checked={basis === b} onChange={() => setBasis(b)} />{b === "current" ? "Current value" : "Invested"}</label>
          ))}
        </fieldset>
        <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Asset class">
          {[{ key: "all", label: "All" }, ...(d?.asset_classes ?? [])].map((c) => (
            <button key={c.key} onClick={() => setAsset(c.key)}
                    className={`chip border ${asset === c.key ? "border-brand bg-brand/10 text-brand" : "border-line bg-page text-ink2 hover:text-ink"}`}>{c.label}</button>
          ))}
        </div>
        <label className="inline-flex items-center gap-1.5"><input type="checkbox" checked={lookthrough} onChange={(e) => setLookthrough(e.target.checked)} />Include mutual funds (look-through by category)</label>
      </div>
      <ErrorNote error={q.error} />

      {q.isLoading ? <div className="grid gap-4 xl:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-72" />)}</div>
        : !d || d.total <= 0 ? <Empty title="Nothing to analyse in this view">Import a holdings statement or add transactions first.</Empty> : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile label={basis === "current" ? "Value in view" : "Invested in view"} value={inr(d.total, { compact: true })} sub={asset === "all" ? "all asset classes" : d.asset_classes.find((c) => c.key === asset)?.label} />
            <StatTile label="Stocks" value={`${(equityShare("Stocks")?.pct ?? 0).toFixed(1)}%`} sub={inr(equityShare("Stocks")?.value ?? 0, { compact: true })} />
            <StatTile label="Mutual funds" value={`${(equityShare("Mutual funds")?.pct ?? 0).toFixed(1)}%`} sub={inr(equityShare("Mutual funds")?.value ?? 0, { compact: true })} />
            <StatTile label="ETFs" value={`${(equityShare("ETFs")?.pct ?? 0).toFixed(1)}%`} sub={inr(equityShare("ETFs")?.value ?? 0, { compact: true })} />
          </div>

          <div className="grid gap-4 lg:grid-cols-2">
            <Section title="Asset classes"><Donut title="Portfolio" slices={withColors(d.asset_classes, CLASS_COLOR)} /></Section>
            <Section title="Stocks vs mutual funds vs others"><Donut title="Products" slices={withColors(d.products, PRODUCT_COLOR)} /></Section>
          </div>

          <ClassContents classes={d.asset_classes.filter((c) => asset === "all" || c.key === asset)} basis={basis} />

          {d.equity_total > 0 && (<>
          <div className="grid gap-4 lg:grid-cols-2">
            <Section title="Sectors (equity)"><Donut title="Sectors" slices={withColors(d.sectors)} empty="No equity in this view" /></Section>
            <Section title="Company size & holdings (equity)">
              <Donut title="Stocks" slices={stockSlices} inner={withColors(d.caps, CAP_COLOR)} innerTitle="Size" empty="No equity in this view" />
            </Section>
          </div>

          <div className="grid gap-4 xl:grid-cols-2">
            <Section title="Sectors — what's inside">
              <div className="max-h-[28rem] overflow-auto">
                <SectorTable sectors={d.sectors} />
              </div>
            </Section>
            <Section title="Equity holdings by size">
              <div className="max-h-[28rem] overflow-auto">
                <SizeTable holdings={d.holdings} />
              </div>
            </Section>
          </div>
          </>)}
          {lookthrough && d.lookthrough_detail && <LookThroughSections lt={d.lookthrough_detail} scope={scope} />}
          <ul className="space-y-1 text-xs text-muted">{d.notes.map((n, i) => <li key={i}>ⓘ {n}</li>)}</ul>
        </>
      )}
    </div>
  );
}

/** What's inside each asset class — e.g. Debt: EPF, PPF, a liquid fund, a debt ETF — with value, share and who holds it. */
function ClassContents({ classes, basis }: { classes: Exposure["asset_classes"]; basis: string }) {
  if (!classes.some((c) => c.holdings?.length)) return null;
  return (
    <div className={`grid gap-4 ${classes.length > 1 ? "xl:grid-cols-2" : ""}`}>
      {classes.map((c) => (
        <Section key={c.key} title={`What's in ${c.label} · ${inr(c.value, { compact: true })} (${c.pct.toFixed(1)}% of all)`}>
          <div className="max-h-[24rem] overflow-auto"><ClassTable c={c} basis={basis} /></div>
        </Section>
      ))}
    </div>
  );
}

function SectorTable({ sectors }: { sectors: Exposure["sectors"] }) {
  const [rows, sort] = useSort(sectors, { label: (s) => s.label, value: (s) => s.value, pct: (s) => s.pct, n: (s) => s.holdings.length }, "sector_sort");
  return (
    <table className="w-full text-sm">
      <thead className="sticky top-0 bg-surface"><tr><SortTh s={sort} k="label">Sector</SortTh><SortTh s={sort} k="value" className="th text-right">Value</SortTh>
        <SortTh s={sort} k="pct" className="th text-right">Share of equity</SortTh><SortTh s={sort} k="n">Holdings</SortTh></tr></thead>
      <tbody>{rows.map((s) => (
        <tr key={s.key} className="border-t border-line align-top">
          <td className="td font-medium">{s.label}</td>
          <td className="td tnum text-right">{inr(s.value)}</td>
          <td className="td tnum text-right">{s.pct.toFixed(1)}%</td>
          <td className="td text-xs text-ink2">{s.holdings.map((h) => h.name).join(" · ")}</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

const CAP_RANK: Record<string, number> = { large: 3, mid: 2, small: 1 };

function SizeTable({ holdings }: { holdings: Exposure["holdings"] }) {
  const [rows, sort] = useSort(holdings, { name: (h) => h.name, cap: (h) => CAP_RANK[h.cap] ?? 0, value: (h) => h.value, pct: (h) => h.pct }, "size_sort");
  return (
    <table className="w-full text-sm">
      <thead className="sticky top-0 bg-surface"><tr><SortTh s={sort} k="name">Holding</SortTh><SortTh s={sort} k="cap">Size</SortTh>
        <SortTh s={sort} k="value" className="th text-right">Value</SortTh><SortTh s={sort} k="pct" className="th text-right">Weight</SortTh></tr></thead>
      <tbody>{rows.map((h) => (
        <tr key={h.symbol} className="border-t border-line">
          <td className="td">{h.name}<span className="block text-xs text-muted">{h.kind === "fund" ? "Mutual fund" : h.kind === "etf" ? "ETF" : "Stock"}</span></td>
          <td className="td"><span className="chip bg-page">{CAP_SHORT[h.cap] ?? h.cap}</span></td>
          <td className="td tnum text-right">{inr(h.value)}</td>
          <td className="td tnum text-right">{h.pct.toFixed(1)}%</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

function ClassTable({ c, basis }: { c: Exposure["asset_classes"][number]; basis: string }) {
  const [rows, sort] = useSort(c.holdings ?? [], { name: (h) => h.name, kind: (h) => h.kind, value: (h) => h.value, pct: (h) => h.pct }, "class_sort");
  return (
    <table className="w-full text-sm">
      <thead className="sticky top-0 bg-surface"><tr><SortTh s={sort} k="name">Holding</SortTh><SortTh s={sort} k="kind">Type</SortTh>
        <SortTh s={sort} k="value" className="th text-right">{basis === "invested" ? "Invested" : "Value"}</SortTh>
        <SortTh s={sort} k="pct" className="th text-right">{`Share of ${c.label.toLowerCase()}`}</SortTh></tr></thead>
      <tbody>{rows.map((h) => (
        <tr key={h.symbol} className="border-t border-line">
          <td className="td">{h.name}{h.people.length > 0 && <span className="block text-xs text-muted">{h.people.join(", ")}</span>}</td>
          <td className="td text-ink2">{h.kind}</td>
          <td className="td tnum text-right">{inr(h.value)}</td>
          <td className="td tnum text-right">{h.pct.toFixed(1)}%</td>
        </tr>
      ))}</tbody>
    </table>
  );
}

/** MF look-through (E16): true stock exposure (direct + through funds) and how much your funds overlap. */
function LookThroughSections({ lt, scope }: { lt: LookThrough; scope: Scope }) {
  const qc = useQueryClient();
  const groups = useGroups();
  const portfolios = usePortfolios();
  // the plan follows the selector: one person / group / portfolio — everyone only when the whole family is selected
  const filter = useMemo<PlanFilter>(() => {
    if (scope.type === "profile") return { profiles: new Set([scope.id!]) };
    if (scope.type === "group") return { profiles: new Set(groups.data?.find((g) => g.id === scope.id)?.profile_ids ?? []) };
    if (scope.type === "portfolio") {
      const owner = portfolios.data?.find((p) => p.id === scope.id)?.profile_id;
      return { profiles: new Set(owner ? [owner] : []), funds: new Set(lt.covered.map((c) => c.name)) };
    }
    return { profiles: null };
  }, [scope, groups.data, portfolios.data, lt.covered]);
  const refresh = useMutation({
    mutationFn: () => api<{ amc: string; ok: boolean | null; error?: string | null; skipped?: string }[]>("/market/funds/refresh", { body: { force: true } }),
    onSuccess: (r) => {
      const bad = r.filter((x) => x.ok === false);
      if (bad.length) toast("error", `${bad.length} of ${r.length} fund house(s) couldn't be read`, bad.map((b) => `${b.amc}: ${b.error}`).join("\n"));
      else toast("ok", "Fund holdings updated", `${r.length} fund house(s) checked`);
      qc.invalidateQueries({ queryKey: ["exposure"] });
    },
    onError: (e: Error) => toast("error", "Couldn't fetch fund holdings", e.message),
  });
  const latest = lt.covered.map((c) => c.as_of).filter(Boolean).sort().pop();
  const max = Math.max(1, ...lt.stocks.slice(0, 15).map((s) => s.total));
  const [top, stockSort] = useSort(lt.stocks.slice(0, 15), {
    name: (s) => s.name, split: (s) => (s.total ? s.via_funds / s.total : null), direct: (s) => s.direct, via: (s) => s.via_funds, total: (s) => s.total, pct: (s) => s.pct,
  }, "stock_sort");
  const [pairs, pairSort] = useSort(lt.overlap, { funds: (p) => p.a, overlap: (p) => p.overlap_pct, common: (p) => p.common_count, todo: (p) => p.overlap_pct >= 50 }, "pair_sort");
  return (
    <>
      <Section title="True stock exposure — direct + through your funds"
        action={<button className="btn-ghost py-1 text-xs" disabled={refresh.isPending} onClick={() => refresh.mutate()}>
          {refresh.isPending ? "Fetching…" : "↻ Fetch fund holdings now"}</button>}>
        <p className="mb-2 text-xs text-ink2">
          {lt.covered.length > 0
            ? <>Inside {lt.covered.length} fund(s)/ETF(s) worth {inr(lt.fund_value, { compact: true })}, from the monthly portfolio disclosures{latest ? ` (latest ${latest})` : ""}. </>
            : <>No fund holdings loaded yet — fund houses publish them monthly. </>}
          {lt.missing.length > 0 && <ShortList label="Not loaded yet" names={lt.missing} />}
          {lt.stale.length > 0 && <> <ShortList label="Older than 2 months" names={lt.stale} /></>}
        </p>
        {top.length > 0 && (
          <div className="max-h-[32rem] overflow-auto">
            <div className="mb-2 flex gap-4 text-xs text-ink2">
              <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: "var(--s1)" }} />Held directly</span>
              <span className="inline-flex items-center gap-1.5"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: "var(--s3)" }} />Through funds</span>
            </div>
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface"><tr>
                <SortTh s={stockSort} k="name">Stock</SortTh><SortTh s={stockSort} k="split" className="th w-1/4" title="Sorts by the share held through funds">Direct · through funds</SortTh>
                <SortTh s={stockSort} k="direct" className="th text-right">Direct</SortTh><SortTh s={stockSort} k="via" className="th text-right">Through funds</SortTh>
                <SortTh s={stockSort} k="total" className="th text-right">Total</SortTh><SortTh s={stockSort} k="pct" className="th text-right">Share of equity</SortTh>
              </tr></thead>
              <tbody>{top.map((s) => (
                <tr key={s.isin} className="border-t border-line align-top">
                  <td className="td font-medium">{s.name}
                    {s.funds.length > 0 && <span className="block text-xs font-normal text-muted">via {s.funds.map((f) => f.name).join(", ")}</span>}</td>
                  <td className="td">
                    <div className="flex h-2.5 w-full gap-[2px] overflow-hidden rounded-sm" title={`${inr(s.direct)} direct · ${inr(s.via_funds)} through funds`}>
                      {s.direct > 0 && <span style={{ width: `${(s.direct / max) * 100}%`, background: "var(--s1)" }} className="rounded-sm" />}
                      {s.via_funds > 0 && <span style={{ width: `${(s.via_funds / max) * 100}%`, background: "var(--s3)" }} className="rounded-sm" />}
                    </div>
                  </td>
                  <td className="td tnum text-right">{s.direct ? inr(s.direct) : "—"}</td>
                  <td className="td tnum text-right">{s.via_funds ? inr(s.via_funds) : "—"}</td>
                  <td className="td tnum text-right font-medium">{inr(s.total)}</td>
                  <td className="td tnum text-right">{s.pct.toFixed(1)}%</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        )}
      </Section>
      {lt.overlap.length > 0 && (
        <Section title="Fund overlap — do your funds hold the same stocks?">
          <PlanFilterCtx.Provider value={filter}><ConsolidationPlan /></PlanFilterCtx.Provider>
          <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Every pair{scope.type !== "household" ? " (in this selection)" : ""}</h3>
          <table className="w-full text-sm">
            <thead><tr><SortTh s={pairSort} k="funds">Funds</SortTh><SortTh s={pairSort} k="overlap" className="th text-right">Overlap</SortTh>
              <SortTh s={pairSort} k="common">Biggest shared stocks</SortTh><SortTh s={pairSort} k="todo">What to do</SortTh></tr></thead>
            <tbody>{pairs.map((p) => (
              <tr key={p.a + p.b} className="border-t border-line align-top">
                <td className="td"><span className="block">{p.a}</span><span className="block text-xs text-muted">⇄</span><span className="block">{p.b}</span></td>
                <td className="td text-right">
                  <span className={`chip tnum ${p.overlap_pct >= 50 ? "bg-warn/15 text-warn" : "bg-page"}`}>{p.overlap_pct >= 50 ? "⚠ " : ""}{p.overlap_pct.toFixed(0)}%</span>
                  <span className="block text-xs text-muted">{p.common_count} in common</span>
                </td>
                <td className="td text-xs text-ink2">{p.common.map((c) => c.name).join(" · ")}</td>
                <td className="td text-xs">{p.overlap_pct >= 50 ? <PlanFilterCtx.Provider value={filter}><PairVerdict a={p.a} b={p.b} /></PlanFilterCtx.Provider> : <span className="text-muted">Fine — different enough</span>}</td>
              </tr>
            ))}</tbody>
          </table>
          <p className="mt-2 text-xs text-muted">Overlap = how much of the two funds&apos; stock portfolios is the same (100% = identical). Above 50% you are paying two expense ratios for largely one portfolio.</p>
        </Section>
      )}
    </>
  );
}

function ShortList({ label, names }: { label: string; names: string[] }) {
  const [open, setOpen] = useState(false);
  const shown = open ? names : names.slice(0, 3);
  return (
    <span className="text-muted">
      {label} ({names.length}): {shown.join(", ")}
      {names.length > 3 && (
        <button className="ml-1 underline" onClick={() => setOpen(!open)}>{open ? "show less" : `+${names.length - 3} more`}</button>
      )}.
    </span>
  );
}

type PlanFilter = { profiles: Set<string> | null; funds?: Set<string> };
const PlanFilterCtx = createContext<PlanFilter>({ profiles: null });
type PersonPlan = ConsolidationGroup & { person?: string | null };

/** The advisor's fund plans for the selection: per person (a plan is always one person's funds); for a single
 *  portfolio only the funds in it (plus the fund to keep). */
function usePlans(): PersonPlan[] | undefined {
  const recs = useRecs({ status: "open" });
  const f = useContext(PlanFilterCtx);
  return recs.data
    ?.filter((r) => r.rule_id?.startsWith("fund_consolidation:") && r.short_report?.plan && (!f.profiles || f.profiles.has(r.profile_id ?? "")))
    .map((r) => ({ ...r.short_report.plan!, person: r.name }))
    .map((g) => (f.funds ? { ...g, moves: g.moves.filter((m) => f.funds!.has(m.name)) } : g))
    .filter((g) => !f.funds || g.moves.length > 0 || f.funds.has(g.keep));
}

function useLineups() {
  const recs = useRecs({ status: "open" });
  const f = useContext(PlanFilterCtx);
  return recs.data?.filter((r) => r.rule_id === "fund_lineup" && (!f.profiles || f.profiles.has(r.profile_id ?? ""))) ?? [];
}

const KEEP = <span className="chip bg-up/15 font-semibold text-up">KEEP · INVEST HERE</span>;
const MOVE = <span className="chip bg-down/15 font-semibold text-down">SELL OVER TIME</span>;
const FREEZE = <span className="chip bg-brand/15 font-semibold text-brand">HOLD</span>;
const FREEZE_SIP = <span className="chip bg-brand/15 font-semibold text-brand">HOLD · MOVE SIP</span>;
const REPLACE = <span className="chip bg-down/15 font-semibold text-down">REPLACE · IT LAGS</span>;
/** Where a role's new money goes: the kept fund, or — when even that one lags — its replacement. */
const intoOf = (g: { keep: string; replace?: boolean; into_name?: string | null }) =>
  g.replace ? (g.into_name ?? `a better fund (see ${g.keep} on the Advisor)`) : g.keep;

/** The advisor's plan: per group of overlapping funds, the one to keep and how to move the others into it. */
function ConsolidationPlan() {
  const plans = usePlans();
  const lineups = useLineups();
  const f = useContext(PlanFilterCtx);
  const many = !f.profiles || f.profiles.size > 1;
  if (!plans) return null;
  if (!plans.length)
    return <p className="rounded-md border border-line bg-page px-3 py-2 text-xs text-ink2">Run the advisor (all holdings, or the <b>Mutual funds</b> filter) to get a plan: which fund to keep and how to move out of the others without needless tax.</p>;
  return (
    <div className="space-y-3">
      {!f.funds && lineups.map((lineup) => (
        <div key={lineup.id} className="rounded-lg border border-brand/40 bg-brand/5 p-3 text-sm">
          <p className="font-semibold">{many && <span className="mr-2 text-xs uppercase text-brand">{lineup.name}</span>}{lineup.headline.split(": ").slice(1).join(": ") || lineup.headline}</p>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-ink2">{(lineup.short_report.do ?? "").split(/ (?=Step \d)/).map((x) => <li key={x}>{x}</li>)}</ul>
          <details className="mt-2 text-xs text-ink2"><summary className="cursor-pointer">Which funds are counted, by role</summary>
            <ul className="mt-1 list-disc space-y-1 pl-5">{(lineup.short_report.reasons ?? []).filter((r) => r.includes("keep ") || r.includes("replace ")).map((r) => <li key={r}>{r}</li>)}</ul>
          </details>
        </div>
      ))}
      {plans.map((g) => (
        <div key={g.keep_id} className="rounded-lg border border-line p-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">{many && g.person && <span className="text-xs font-semibold uppercase text-brand">{g.person}</span>}{g.role && <span className="text-xs font-semibold uppercase text-muted">{g.role}</span>}
            {g.replace ? <>{REPLACE}<b>{g.keep}</b>{g.keep_value ? <span className="text-ink2">{inr(g.keep_value, { compact: true })}</span> : null}
              <span className="text-ink2">→ new money into</span>{KEEP}<b>{intoOf(g)}</b></>
              : <>{KEEP}<b>{g.keep}</b>{g.keep_value ? <span className="text-ink2">{inr(g.keep_value, { compact: true })}</span> : null}</>}</div>
          <p className="mt-1 text-xs text-ink2">{g.replace ? <>Even the best {g.role ? g.role.toLowerCase() + " " : ""}fund you hold lags ({g.keep_reason}) — it is sold over time too{g.keep_move?.do ? <>: {g.keep_move.do}</> : "."}</>
            : <>Why this one: {g.keep_reason}.</>}</p>
          <ul className="mt-2 space-y-2">
            {g.moves.map((m) => (
              <li key={m.id} className="border-t border-line pt-2 text-sm">
                <div className="flex flex-wrap items-center gap-2">{m.mode === "freeze" ? (m.sip ? FREEZE_SIP : FREEZE) : MOVE}<b>{m.name}</b>
                  {m.overlap_pct > 0 && <span className="chip bg-warn/15 tnum text-warn">{m.overlap_pct.toFixed(0)}% same stocks as {g.keep}</span>}
                  {m.value ? <span className="text-xs text-ink2">{inr(m.value, { compact: true })}</span> : null}</div>
                <p className="mt-1 text-xs"><b>What to do:</b> {m.do}</p>
                {m.mode !== "freeze" && m.costs.map((c) => <p key={c} className="text-xs text-muted">{c}</p>)}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

/** One pair, read off the plan: which of the two to keep, or that both move into a third fund. */
function PairVerdict({ a, b }: { a: string; b: string }) {
  const plans = usePlans();
  const g = plans?.find((p) => [a, b].includes(p.keep) || p.moves.some((m) => m.name === a || m.name === b));
  if (!g) return <span className="text-muted">{plans ? "Run the advisor for a keep / move verdict." : "…"}</span>;
  const moving = (n: string) => g.moves.some((m) => m.name === n);
  if (g.keep === a || g.keep === b) {
    const other = g.keep === a ? b : a;
    const m = g.moves.find((x) => x.name === other);
    return m ? <span className="flex flex-wrap items-center gap-1">{g.replace ? REPLACE : KEEP}<span>{g.keep}</span>{m.mode === "freeze" ? (m.sip ? FREEZE_SIP : FREEZE) : MOVE}<span>{other}</span>
      {g.replace ? <><span>· new money into</span><b>{intoOf(g)}</b></> : null}</span>
      : <span className="text-muted">{g.replace ? "Replace" : "Keep"} {g.keep}; {other} is handled in another group.</span>;
  }
  if (moving(a) && moving(b)) return <span className="flex flex-wrap items-center gap-1"><span>Both: new money only into</span>{KEEP}<span>{intoOf(g)}</span></span>;
  return <span className="text-muted">See the plan above.</span>;
}
