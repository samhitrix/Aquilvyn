"use client";

import { useState } from "react";

import InstrumentSearch, { type Picked } from "@/components/InstrumentSearch";
import { ErrorNote, Section, Skeleton } from "@/components/ui";
import { inr, num } from "@/lib/format";
import { useAddTxn, useDeleteTxn, usePortfolios, useProfiles, useTxns } from "@/lib/queries";
import { useUrlState } from "@/lib/useUrlState";
import { SortTh, useSort } from "@/lib/useSort";

const TYPES = ["buy", "sell", "sip", "dividend", "contribution", "interest", "withdrawal", "bonus", "split"];
const today = () => new Date().toISOString().slice(0, 10);

export default function TransactionsPage() {
  const { data: profiles } = useProfiles();
  const { data: portfolios } = usePortfolios();
  const [filterPf, setFilterPf] = useUrlState("portfolio", "");
  const filters = { portfolio_id: filterPf || undefined };
  const txns = useTxns(filters);
  const add = useAddTxn(filters);
  const del = useDeleteTxn(filters);
  const [picked, setPicked] = useState<Picked | null>(null);
  const [f, setF] = useState({ portfolio_id: "", txn_type: "buy", trade_date: today(), quantity: "", price: "", fees: "", amount: "" });
  const pfName = (id: string) => { const p = portfolios?.find((x) => x.id === id); return p ? `${profiles?.find((q) => q.id === p.profile_id)?.display_name ?? ""} · ${p.name}` : ""; };
  const [sorted, sort] = useSort(txns.data ?? [], {
    date: (t) => t.trade_date, symbol: (t) => t.symbol, type: (t) => t.txn_type, pf: (t) => pfName(t.portfolio_id),
    qty: (t) => t.quantity, price: (t) => t.price, amount: (t) => t.amount,
  });
  const cashOnly = ["dividend", "contribution", "interest", "withdrawal"].includes(f.txn_type);

  function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!picked || !f.portfolio_id) return;
    add.mutate({
      portfolio_id: f.portfolio_id, instrument_id: picked.id ?? undefined, symbol: picked.id ? undefined : picked.symbol, asset_type: picked.asset_type,
      txn_type: f.txn_type, trade_date: f.trade_date, quantity: Number(f.quantity || 0), price: Number(f.price || 0), fees: Number(f.fees || 0),
      amount: f.amount ? Number(f.amount) : undefined, client_ref: crypto.randomUUID(), _display: { symbol: picked.symbol },
    });
    setF({ ...f, quantity: "", price: "", fees: "", amount: "" });
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Transactions</h1>
      <Section title="Add a transaction" action={<span className="text-xs text-muted">Appears instantly · retried safely (idempotent)</span>}>
        <form onSubmit={submit} className="grid gap-3 md:grid-cols-4 2xl:grid-cols-8">
          <div className="md:col-span-2"><label className="label">Instrument</label><InstrumentSearch onPick={setPicked} /></div>
          <div><label className="label">Portfolio</label>
            <select className="input" required value={f.portfolio_id} onChange={(e) => setF({ ...f, portfolio_id: e.target.value })}>
              <option value="">Choose…</option>{portfolios?.map((p) => <option key={p.id} value={p.id}>{pfName(p.id)}</option>)}
            </select></div>
          <div><label className="label">Type</label>
            <select className="input" value={f.txn_type} onChange={(e) => setF({ ...f, txn_type: e.target.value })}>{TYPES.map((t) => <option key={t} value={t}>{t}</option>)}</select></div>
          <div><label className="label">Date</label><input className="input" type="date" max={today()} value={f.trade_date} onChange={(e) => setF({ ...f, trade_date: e.target.value })} /></div>
          {cashOnly ? (
            <div><label className="label">Amount (₹)</label><input className="input" inputMode="decimal" required value={f.amount} onChange={(e) => setF({ ...f, amount: e.target.value })} /></div>
          ) : (
            <>
              <div><label className="label">{f.txn_type === "split" ? "Ratio (new per old)" : "Quantity / units"}</label><input className="input" inputMode="decimal" required value={f.quantity} onChange={(e) => setF({ ...f, quantity: e.target.value })} /></div>
              <div><label className="label">Price / NAV (₹)</label><input className="input" inputMode="decimal" value={f.price} onChange={(e) => setF({ ...f, price: e.target.value })} /></div>
            </>
          )}
          <div><label className="label">Charges (₹)</label><input className="input" inputMode="decimal" value={f.fees} onChange={(e) => setF({ ...f, fees: e.target.value })} /></div>
          <div className="flex items-end"><button className="btn-primary w-full" disabled={!picked || !f.portfolio_id}>Add</button></div>
        </form>
        <p className="mt-2 text-xs text-muted">EPF / PPF / FD? Create the account under Family → Retirement accounts, then log contributions here.</p>
        <ErrorNote error={add.error ?? del.error} />
      </Section>
      <div className="flex items-center gap-2">
        <select className="input w-auto" value={filterPf} onChange={(e) => setFilterPf(e.target.value)} aria-label="Filter by portfolio">
          <option value="">All portfolios</option>{portfolios?.map((p) => <option key={p.id} value={p.id}>{pfName(p.id)}</option>)}
        </select>
      </div>
      {txns.isLoading ? <Skeleton className="h-64" /> : (
        <div className="card max-h-[calc(100vh-9rem)] overflow-auto">
          <table className="w-full">
            <thead className="sticky top-0 z-[1] border-b border-line bg-surface"><tr><SortTh s={sort} k="date">Date</SortTh><SortTh s={sort} k="symbol">Instrument</SortTh>
              <SortTh s={sort} k="type">Type</SortTh><SortTh s={sort} k="pf">Portfolio</SortTh><SortTh s={sort} k="qty" className="th text-right">Qty</SortTh>
              <SortTh s={sort} k="price" className="th text-right">Price</SortTh><SortTh s={sort} k="amount" className="th text-right">Amount</SortTh><th className="th" /></tr></thead>
            <tbody>
              {sorted.map((t) => (
                <tr key={t.id} className={`border-t border-line ${t._optimistic ? "opacity-60" : ""}`}>
                  <td className="td">{t.trade_date}</td><td className="td">{t.symbol}{t._optimistic && <span className="ml-2 text-xs text-muted">saving…</span>}</td>
                  <td className="td capitalize">{t.txn_type}</td><td className="td text-ink2">{pfName(t.portfolio_id)}</td>
                  <td className="td tnum text-right">{num(t.quantity, 3)}</td><td className="td tnum text-right">₹{num(t.price)}</td><td className="td tnum text-right">{inr(t.amount)}</td>
                  <td className="td text-right">{!t._optimistic && <button className="text-xs text-down" onClick={() => confirm("Delete this transaction?") && del.mutate(t.id)}>Delete</button>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
