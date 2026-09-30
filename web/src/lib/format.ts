const inrFmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const inr2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export function inr(v: number | null | undefined, opts: { compact?: boolean; decimals?: boolean } = {}): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (opts.compact) {
    if (a >= 1e7) return `${sign}₹${(a / 1e7).toFixed(2)} Cr`;
    if (a >= 1e5) return `${sign}₹${(a / 1e5).toFixed(2)} L`;
  }
  return `${sign}₹${opts.decimals ? inr2.format(a) : inrFmt.format(a)}`;
}
export const num = (v: number | null | undefined, d = 2) => (v === null || v === undefined ? "—" : v.toLocaleString("en-IN", { maximumFractionDigits: d }));
export const pct = (v: number | null | undefined, d = 2, sign = true) => (v === null || v === undefined ? "—" : `${sign && v > 0 ? "+" : ""}${v.toFixed(d)}%`);
export const tone = (v: number | null | undefined) => (v === null || v === undefined || v === 0 ? "text-ink2" : v > 0 ? "text-up" : "text-down");
export const titleCase = (s: string | null | undefined) => (s ?? "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
export const assetLabel: Record<string, string> = {
  stock: "Stock", etf: "ETF", mutual_fund: "Mutual Fund", nps: "NPS", epf: "EPF", vpf: "VPF", ppf: "PPF", bond: "Bond",
  fixed_deposit: "FD", gold: "Gold", reit: "REIT/InvIT", crypto: "Crypto", cash: "Cash", index: "Index",
};
