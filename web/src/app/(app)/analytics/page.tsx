"use client";

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import Donut, { OTHER_COLOR, withColors } from "@/components/Donut";
import ScopeSwitcher from "@/components/ScopeSwitcher";
import { Empty, ErrorNote, Section, Skeleton, StatTile } from "@/components/ui";
import { api } from "@/lib/api";
import { inr } from "@/lib/format";
import { useScopeState, useUrlState } from "@/lib/useUrlState";

type Part = { key: string; label: string; value: number; pct: number };
type ClassHolding = { name: string; symbol: string; kind: string; value: number; pct: number; people: string[] };
type Exposure = {
  basis: string; asset: string; lookthrough: boolean; total: number; equity_total: number; notes: string[];
  asset_classes: (Part & { holdings?: ClassHolding[] })[]; products: Part[]; caps: Part[];
  sectors: (Part & { holdings: { name: string; value: number }[] })[];
  holdings: { name: string; symbol: string; value: number; pct: number; cap: string; kind: string }[];
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
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-surface"><tr><th className="th">Sector</th><th className="th text-right">Value</th><th className="th text-right">Share of equity</th><th className="th">Holdings</th></tr></thead>
                  <tbody>{d.sectors.map((s) => (
                    <tr key={s.key} className="border-t border-line align-top">
                      <td className="td font-medium">{s.label}</td>
                      <td className="td tnum text-right">{inr(s.value)}</td>
                      <td className="td tnum text-right">{s.pct.toFixed(1)}%</td>
                      <td className="td text-xs text-ink2">{s.holdings.map((h) => h.name).join(" · ")}</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Section>
            <Section title="Equity holdings by size">
              <div className="max-h-[28rem] overflow-auto">
                <table className="w-full text-sm">
                  <thead className="sticky top-0 bg-surface"><tr><th className="th">Holding</th><th className="th">Size</th><th className="th text-right">Value</th><th className="th text-right">Weight</th></tr></thead>
                  <tbody>{d.holdings.map((h) => (
                    <tr key={h.symbol} className="border-t border-line">
                      <td className="td">{h.name}<span className="block text-xs text-muted">{h.kind === "fund" ? "Mutual fund" : h.kind === "etf" ? "ETF" : "Stock"}</span></td>
                      <td className="td"><span className="chip bg-page">{CAP_SHORT[h.cap] ?? h.cap}</span></td>
                      <td className="td tnum text-right">{inr(h.value)}</td>
                      <td className="td tnum text-right">{h.pct.toFixed(1)}%</td>
                    </tr>
                  ))}</tbody>
                </table>
              </div>
            </Section>
          </div>
          </>)}
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
          <div className="max-h-[24rem] overflow-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-surface"><tr><th className="th">Holding</th><th className="th">Type</th><th className="th text-right">{basis === "invested" ? "Invested" : "Value"}</th><th className="th text-right">Share of {c.label.toLowerCase()}</th></tr></thead>
              <tbody>{(c.holdings ?? []).map((h) => (
                <tr key={h.symbol} className="border-t border-line">
                  <td className="td">{h.name}{h.people.length > 0 && <span className="block text-xs text-muted">{h.people.join(", ")}</span>}</td>
                  <td className="td text-ink2">{h.kind}</td>
                  <td className="td tnum text-right">{inr(h.value)}</td>
                  <td className="td tnum text-right">{h.pct.toFixed(1)}%</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </Section>
      ))}
    </div>
  );
}
