/**
 * TanStack Query layer: one key factory, hooks per resource, optimistic mutations.
 * staleTime is tuned per resource so navigation is served from cache instantly (zero-perceived
 * latency) while background refetches keep data fresh; live prices come from the WebSocket.
 */
import { keepPreviousData, useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";

import { toast } from "@/components/Toast";

import { api } from "./api";
import type {
  AdvisorSummary, Bar, Dashboard, HoldingsResponse, MarketOverview, Portfolio, Profile, Readiness, Recommendation, RecommendationDetail, Txn,
} from "./types";

export type Scope = { type: "household" | "profile" | "group" | "portfolio"; id?: string };

export const qk = {
  dashboard: (s: Scope) => ["dashboard", s.type, s.id ?? null] as const,
  holdings: (s: Scope) => ["holdings", s.type, s.id ?? null] as const,
  holding: (instrumentId: string, profileId?: string) => ["holding", instrumentId, profileId ?? null] as const,
  profiles: ["profiles"] as const,
  portfolios: ["portfolios"] as const,
  groups: ["groups"] as const,
  txns: (f: Record<string, string | undefined>) => ["transactions", f] as const,
  recs: (f: Record<string, string | undefined>) => ["recommendations", f] as const,
  rec: (id: string) => ["recommendation", id] as const,
  advisorSummary: ["advisor-summary"] as const,
  overview: ["market-overview"] as const,
  analysis: (symbol: string) => ["analysis", symbol] as const,
  history: (symbol: string, days: number) => ["history", symbol, days] as const,
  news: (symbol: string) => ["news", symbol] as const,
  search: (q: string) => ["search", q] as const,
  aiProviders: ["ai-providers"] as const,
  members: ["members"] as const,
  shadow: ["shadow"] as const,
  scorecard: ["scorecard"] as const,
  imports: ["imports"] as const,
  prefs: ["prefs"] as const,
};

const scopeQuery = (s: Scope) => ({ scope: s.type, id: s.id });

export const useDashboard = (s: Scope) =>
  useQuery({
    queryKey: qk.dashboard(s), queryFn: () => api<Dashboard>("/dashboard", { query: scopeQuery(s) }), staleTime: 15_000, placeholderData: keepPreviousData,
    // an upstream hiccup or prices still loading → poll until the full picture is in
    refetchInterval: (q) => (q.state.data?.errors?.holdings || q.state.data?.holdings?.unpriced_count ? 10_000 : false),
  });
export const useHoldings = (s: Scope) =>
  useQuery({
    queryKey: qk.holdings(s), queryFn: () => api<HoldingsResponse>("/holdings", { query: scopeQuery(s) }), staleTime: 15_000, placeholderData: keepPreviousData,
    refetchInterval: (q) => (q.state.data?.unpriced_count ? 10_000 : false),
  });
export const useProfiles = () => useQuery({ queryKey: qk.profiles, queryFn: () => api<Profile[]>("/profiles"), staleTime: 60_000 });
export const usePortfolios = () => useQuery({ queryKey: qk.portfolios, queryFn: () => api<Portfolio[]>("/portfolios"), staleTime: 60_000 });
export const useGroups = () => useQuery({ queryKey: qk.groups, queryFn: () => api<{ id: string; name: string; profile_ids: string[] }[]>("/groups"), staleTime: 60_000 });
export const useTxns = (f: Record<string, string | undefined>) =>
  useQuery({ queryKey: qk.txns(f), queryFn: () => api<Txn[]>("/transactions", { query: { ...f, limit: 300 } }), staleTime: 30_000 });
export const useRecs = (f: Record<string, string | undefined>) =>
  useQuery({ queryKey: qk.recs(f), queryFn: () => api<Recommendation[]>("/advisor/recommendations", { query: f }), staleTime: 20_000,
    // while AI reviews are still landing, keep the cards current without a page refresh
    refetchInterval: (q) => (q.state.data?.some((r) => r.ai_consensus === "pending" || r.short_report?.ai_stale) ? 20_000 : 60_000) });
export const useRec = (id: string) => useQuery({ queryKey: qk.rec(id), queryFn: () => api<RecommendationDetail>(`/advisor/recommendations/${id}`), staleTime: 20_000 });
export const useAdvisorSummary = () => useQuery({ queryKey: qk.advisorSummary, queryFn: () => api<AdvisorSummary>("/advisor/summary"), staleTime: 20_000 });
export const useOverview = () => useQuery({ queryKey: qk.overview, queryFn: () => api<MarketOverview>("/market/overview"), refetchInterval: 15_000 });
export const useAnalysis = (symbol: string) =>
  useQuery({ queryKey: qk.analysis(symbol), queryFn: () => api<any>(`/analysis/instrument/${encodeURIComponent(symbol)}`), staleTime: 60_000, enabled: !!symbol });
export const useHistory = (symbol: string, days = 730) =>
  useQuery({ queryKey: qk.history(symbol, days), queryFn: () => api<{ bars: Bar[]; instrument: any }>(`/market/history/${encodeURIComponent(symbol)}`, { query: { days } }), staleTime: 300_000, enabled: !!symbol });
export const useNews = (symbol: string) =>
  useQuery({ queryKey: qk.news(symbol), queryFn: () => api<any[]>(`/market/news/${encodeURIComponent(symbol)}`), staleTime: 600_000, enabled: !!symbol });
export const useSearch = (q: string) =>
  useQuery({ queryKey: qk.search(q), queryFn: () => api<any[]>("/market/instruments/search", { query: { q } }), enabled: q.length >= 2, staleTime: 300_000 });

/** Route-level prefetch helpers (called on hover / focus so the next page renders from cache). */
export const prefetch = {
  holding: (qc: QueryClient, symbol: string) => {
    qc.prefetchQuery({ queryKey: qk.analysis(symbol), queryFn: () => api(`/analysis/instrument/${encodeURIComponent(symbol)}`), staleTime: 60_000 });
    qc.prefetchQuery({ queryKey: qk.history(symbol, 730), queryFn: () => api(`/market/history/${encodeURIComponent(symbol)}`, { query: { days: 730 } }), staleTime: 300_000 });
  },
  rec: (qc: QueryClient, id: string) => qc.prefetchQuery({ queryKey: qk.rec(id), queryFn: () => api(`/advisor/recommendations/${id}`), staleTime: 20_000 }),
};

export function invalidatePortfolio(qc: QueryClient) {
  ["transactions", "holdings", "dashboard", "advisor-summary", "recommendations"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
}

export type NewTxn = {
  portfolio_id: string; instrument_id?: string; symbol?: string; asset_type?: string; txn_type: string; trade_date: string;
  quantity: number; price: number; fees?: number; amount?: number; notes?: string; client_ref: string; _display?: { symbol: string };
};

/** Optimistic create: the row appears instantly with a pending marker, rolls back on error. */
export function useAddTxn(filters: Record<string, string | undefined>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ _display, ...body }: NewTxn) => api<Txn>("/transactions", { body }),
    onMutate: async (t) => {
      await qc.cancelQueries({ queryKey: qk.txns(filters) });
      const prev = qc.getQueryData<Txn[]>(qk.txns(filters));
      const temp: Txn = {
        id: `temp-${t.client_ref}`, portfolio_id: t.portfolio_id, instrument_id: t.instrument_id ?? "", symbol: t._display?.symbol ?? t.symbol ?? "",
        asset_type: t.asset_type ?? "", txn_type: t.txn_type, trade_date: t.trade_date, quantity: t.quantity, price: t.price, fees: t.fees ?? 0,
        amount: t.amount ?? t.quantity * t.price, notes: t.notes ?? "", source: "manual", client_ref: t.client_ref, version: 0, _optimistic: true,
      };
      qc.setQueryData<Txn[]>(qk.txns(filters), (old) => [temp, ...(old ?? [])]);
      return { prev };
    },
    onError: (_e, _t, ctx) => ctx?.prev && qc.setQueryData(qk.txns(filters), ctx.prev),
    onSuccess: (saved, t) => qc.setQueryData<Txn[]>(qk.txns(filters), (old) => (old ?? []).map((x) => (x.client_ref === t.client_ref ? saved : x))),
    onSettled: () => invalidatePortfolio(qc),
  });
}

export function useDeleteTxn(filters: Record<string, string | undefined>) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api(`/transactions/${id}`, { method: "DELETE" }),
    onMutate: async (id) => {
      await qc.cancelQueries({ queryKey: qk.txns(filters) });
      const prev = qc.getQueryData<Txn[]>(qk.txns(filters));
      qc.setQueryData<Txn[]>(qk.txns(filters), (old) => (old ?? []).filter((x) => x.id !== id));
      return { prev };
    },
    onError: (_e, _id, ctx) => ctx?.prev && qc.setQueryData(qk.txns(filters), ctx.prev),
    onSettled: () => invalidatePortfolio(qc),
  });
}

/** Optimistic accept/snooze/dismiss on the advisor inbox. */
export function useActOnRec() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, action, note, snooze_days }: { id: string; action: string; note?: string; snooze_days?: number }) =>
      api<Recommendation>(`/advisor/recommendations/${id}/act`, { body: { action, note, snooze_days } }),
    onMutate: async ({ id, action }) => {
      const status = { accept: "accepted", snooze: "snoozed", dismiss: "dismissed", reopen: "open" }[action] ?? action;
      const snapshots = qc.getQueriesData<Recommendation[]>({ queryKey: ["recommendations"] });
      snapshots.forEach(([key, data]) => data && qc.setQueryData<Recommendation[]>(key, data.map((r) => (r.id === id ? { ...r, status } : r))));
      qc.setQueryData<any>(qk.rec(id), (old: any) => (old ? { ...old, status } : old));
      // the dashboard's "Top actions" come from its own request: take the card off there too
      if (status !== "open") {
        qc.getQueriesData<any>({ queryKey: ["dashboard"] }).forEach(([key, d]) => d?.advisor?.top_actions && qc.setQueryData<any>(key, {
          ...d, advisor: { ...d.advisor, top_actions: d.advisor.top_actions.filter((r: Recommendation) => r.id !== id) } }));
      }
      return { snapshots };
    },
    onSuccess: (r, v) => toast("ok", { accept: "Accepted", snooze: "Snoozed for 7 days", dismiss: "Dismissed", reopen: "Reopened" }[v.action] ?? "Saved",
      v.action === "accept" ? `${r.symbol ?? r.name ?? "The call"} is recorded in your Shadow Portfolio — no real order is placed.` : undefined),
    onError: (e: Error, _v, ctx) => {
      ctx?.snapshots.forEach(([key, data]) => qc.setQueryData<Recommendation[]>(key, data));
      toast("error", "Couldn't save that", e.message);
    },
    onSettled: (_d, _e, v) => {
      qc.invalidateQueries({ queryKey: ["recommendations"] });
      qc.invalidateQueries({ queryKey: qk.rec(v.id) });
      qc.invalidateQueries({ queryKey: qk.shadow });
      qc.invalidateQueries({ queryKey: qk.advisorSummary });
      qc.invalidateQueries({ queryKey: ["dashboard"] });
    },
  });
}

export type RunScope = { profile_id?: string; asset_types?: string[]; other_assets?: boolean; instrument_ids?: string[]; label?: string };

/** The readiness engine's verdict per holding (fundamentals · technicals · analysis up to date · AI review). */
export const useReadiness = (profile_id?: string) =>
  useQuery({ queryKey: ["readiness", profile_id ?? ""], queryFn: () => api<Readiness>("/readiness", { query: { profile_id } }),
    staleTime: 20_000, refetchInterval: 30_000 });

const LIVE_KEYS = ["recommendations", "recommendation", "advisor-summary", "dashboard", "readiness", "advisor-runs"];
const refreshAdvisor = (qc: QueryClient) => LIVE_KEYS.forEach((k) => qc.invalidateQueries({ queryKey: [k] }));

/** After a run the AI reviews finish in the background: keep the page updating by itself, and say when they're done. */
async function watchReviews(qc: QueryClient, ids: string[], label: string) {
  const deadline = Date.now() + 4 * 60_000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 8000));
    refreshAdvisor(qc);
    let recs: Recommendation[] = [];
    try {
      recs = await api<Recommendation[]>("/advisor/recommendations", { query: { status: "open,accepted,snoozed,dismissed,superseded", include_hold: "true" } });
    } catch {
      continue;
    }
    const mine = recs.filter((r) => ids.includes(r.id));
    const waiting = mine.filter((r) => r.ai_consensus === "pending" || r.short_report?.ai_stale);
    if (mine.length && !waiting.length) {
      const failed = mine.filter((r) => r.ai_consensus === "ai_failed");
      if (failed.length) toast("warn", `AI review finished · ${label}`, `${mine.length - failed.length} reviewed, ${failed.length} failed — ${failed[0].ai_error ?? "see the card"}.`);
      else toast("ok", `AI review done · ${label}`, `${mine.length} call(s) checked by AI.`);
      return;
    }
  }
  toast("info", `AI review still running · ${label}`, "The health check keeps retrying it; the cards update by themselves.");
}

export function useRunAdvisor() {
  const qc = useQueryClient();
  return useMutation({
    // a person's id, or exactly what the Advisor filters show: person + kinds of holding (label = "mutual funds" …)
    mutationFn: (arg?: string | RunScope) => {
      const s: RunScope = typeof arg === "string" ? { profile_id: arg } : arg ?? {};
      return api<any>("/advisor/run", { body: { profile_id: s.profile_id, asset_types: s.asset_types, other_assets: s.other_assets, instrument_ids: s.instrument_ids } })
        .then((res) => ({ ...res, label: s.label }));
    },
    onMutate: (arg) => {
      const label = typeof arg === "object" && arg?.label ? arg.label : "your holdings";
      toast("info", `Analysing ${label}…`, "Keep working — the page updates by itself when it's done.");
    },
    onError: (e: Error) => toast("error", "Analysis failed", e.message),
    onSuccess: (res: any) => {
      refreshAdvisor(qc);
      if (res?.skipped) return toast("warn", "Analysis not run", res.reason);
      if (res?.holdings === 0 && res?.label && String(res?.scope ?? "").includes("no matching holding")) {
        return toast("info", `${res.label} is no longer held`, "Its advice card has been closed.");
      }
      const n = (res?.new ?? 0) + (res?.updated ?? 0);
      const unpriced: string[] = res?.skipped_unpriced ?? [];
      const label = res?.holdings === 1 && res?.label ? res.label : `${res?.holdings ?? 0} ${res?.label && !/^[A-Z0-9&-]+$/.test(res.label) ? res.label : "holding(s)"}`;
      toast("ok", `Analysis done · ${label}`,
        `${n ? `${n} call(s) updated` : "No change to the calls"}.` +
        (unpriced.length ? ` Skipped (no price): ${unpriced.slice(0, 5).join(", ")}${unpriced.length > 5 ? "…" : ""}.` : "") +
        (res?.to_review?.length ? ` AI is reviewing ${res.to_review.length} call(s) — you'll get a note when it's done.` : ""));
      if (res?.to_review?.length) void watchReviews(qc, res.to_review, res?.holdings === 1 && res?.label ? res.label : "your holdings");
    },
  });
}

export function useAiProviders() {
  return useQuery({ queryKey: qk.aiProviders, queryFn: () => api<any>("/advisor/ai-providers"), staleTime: 60_000 });
}
