export type User = { id: string; household_id: string; household_name: string; email: string; full_name: string; role: string; permissions: string[]; has_password?: boolean; has_recovery_code?: boolean };
export type TokenOut = { access_token: string; expires_in: number; user: User; recovery_code?: string | null };

export type Quote = { symbol: string; price: number; prev_close: number; change: number; change_pct: number; ts: string; source: string; day_high?: number | null; day_low?: number | null; name?: string };

export type Profile = {
  id: string; display_name: string; relationship: string; pan_masked: string | null; date_of_birth: string | null; age: number | null;
  email?: string | null; mobile?: string | null;
  tax_slab_pct: number | null; risk_profile: string; retirement_age: number; target_allocation: Record<string, number>;
  drawdown_thresholds: Record<string, number>; color: string | null; linked_user_id: string | null;
  pan_tagged?: boolean; accounts?: { id: string; kind: string; label: string }[];
};
export type Portfolio = { id: string; profile_id: string; name: string; kind: string; broker: string | null };

export type Lot = { buy_date: string; qty: number; cost: number };
export type Holding = {
  instrument_id: string; symbol: string; asset_type: string; name: string; sector: string | null; quantity: number; avg_cost: number;
  invested: number; price: number | null; prev_close: number | null; market_value: number; day_change: number; unrealised_pnl: number;
  unrealised_pct: number | null; realised_pnl: number; dividends: number; xirr_pct: number | null; holding_days: number | null;
  profile_id: string; profile_name: string | null; intent: string | null; intent_source: string | null; lots: Lot[]; sip?: { active: boolean | null; last_buy: string | null; monthly: boolean; source?: "dates" | "unknown" | "you" } | null; priced: boolean; price_status?: "live" | "stale" | "statement" | "nav_statement" | null; price_as_of?: string | null;
  meta: Record<string, unknown>;
};
export type Slice = Record<string, { value: number; pct: number }>;
export type Summary = {
  invested: number; market_value: number; unrealised_pnl: number; unrealised_pct: number | null; realised_pnl: number; dividends: number;
  day_change: number; day_change_pct: number | null; xirr_pct: number | null; allocation: Slice; by_asset_type: Slice; by_sector: Slice;
  holdings_count: number; unpriced: string[];
  fy?: string; fy_realised?: number; fy_income?: number;  // this FY, from imported tax statements
};
export type ProfileSummary = Omit<Summary, "by_sector"> & { profile_id: string; name: string; relationship: string; color: string | null };
export type HoldingsResponse = { as_of: string; summary: Summary | null; profiles: ProfileSummary[]; holdings: Holding[]; unpriced_count?: number; stale_count?: number; nps_nav_as_of?: string | null };

export type Txn = {
  id: string; portfolio_id: string; instrument_id: string; symbol: string; asset_type: string; txn_type: string; trade_date: string;
  quantity: number; price: number; fees: number; amount: number; notes: string; source: string; client_ref: string | null; version: number;
  _optimistic?: boolean;
};

export type ConsolidationMove = { id: string; name: string; value: number | null; overlap_pct: number; common: string[]; common_count?: number;
  why_keep?: string | null; do: string; costs: string[]; move_value: number; mode?: "exit" | "freeze"; role?: string; sip?: boolean };
export type ConsolidationGroup = { keep_id: string; keep: string; keep_value: number | null; keep_score: number | null; keep_reason: string;
  moves: ConsolidationMove[]; role?: string; into_name?: string | null; lineup?: boolean;
  replace?: boolean; keep_move?: ConsolidationMove | null };

export type ShortReport = {
  action: string; horizon: string; intent: string | null; confidence: number; headline: string; reasons: string[]; tax_impact: string | null;
  what_would_change: string; suggestion: { type: string; value?: number; quantity?: number | null; note?: string; tranches?: number; annual_cost_saving?: number };
  rupee_impact: number; urgent: boolean; data_quality: { grade?: string; score?: number }; composite_score: number | null;
  changed_from?: string; change_reason?: string;
  ai_stale?: { reviewed_at: string; provider: string } | null;
  switch_into?: { id: string; name: string } | null; consolidate_from?: string[]; plan_tax?: { fy: string; ltcg: number };
  switch_candidates?: { code: string; name: string; score: number; consistency_pct: number | null; return_3y_pct: number | null; rolling_years?: number; held?: boolean }[];
  switch_category?: string;
  sip_into?: { name: string; code?: string | null; active_sip?: boolean } | null;
  plan?: ConsolidationGroup;
  do?: string; verdict?: string; conviction?: "high" | "medium" | "low"; leaning?: string | null; rule_action?: string; ai_adjusted?: boolean; rules_confidence?: number; ai_overruled?: boolean;
  ai_view?: { provider: string; model: string; stance: string; suggested_action: string | null; plain: string; confidence: number | null } | null;
  ai_suggests?: { provider: string; action: string; plain: string; applied: boolean; why_not: string } | null;
  ai_opinion?: { provider: string; model: string; action: string; label: string; horizon: string | null; confidence: number | null;
    plain: string; why: string; risks: string } | null;
};
export type Recommendation = {
  id: string; scope: string; profile_id: string | null; instrument_id: string | null; symbol: string | null; name: string | null;
  asset_type: string | null; action: string; horizon: string; intent: string | null; confidence: number; priority: number; bucket: string;
  actionable: boolean; rupee_impact: number | null; headline: string; short_report: ShortReport; narrative: string | null;
  ai_consensus: string; ai_error?: string | null; status: string; change_reason: string | null; profile_name?: string | null; rule_id: string; rulebook_version: string; created_at: string; updated_at: string;
};
export type AIReview = { provider: string; model: string; stance: string; confidence: number | null; issues: { type: string; detail: string }[]; rationale: string; counter_case: string; reviewed_at: string; current: boolean; suggested_action?: string | null; plain_verdict?: string | null; horizon?: string | null; opinion?: boolean;
};
export type RecommendationDetail = Recommendation & { full_report: Record<string, any>; ai_reviews: AIReview[] };

export type AdvisorSummary = {
  last_run: null | { id: string; at: string; rulebook_version: string; stats: Record<string, any>; regime: Record<string, any> };
  last_attempt?: null | { at: string; status: string; reason: string | null };
  top_actions: Recommendation[];
  ai: { reviewers: string[]; order?: string[]; primary: string | null; mode: string };
  ai_status?: { reviewed: number; failed: number; waiting: number; no_ai: number;
    last_error: null | { provider: string; detail: string | null; at: string } };
  schedule?: { daily: string; next_daily_run: string; also: string;
    latest: null | { status: string; trigger: string; started_at: string; finished_at: string | null; error: string | null } };
};
export type AdvisorRunRow = { id: string; trigger: string; scope: string; status: string; started_at: string; finished_at: string | null;
  holdings: number | null; new: number | null; updated: number | null; error: string | null };
export type MarketOverview = { indices: Quote[]; vix: Quote | null; sectors: (Quote & { sector: string })[]; gainers: Quote[]; losers: Quote[]; most_active?: (Quote & { volume?: number })[] };
export type Dashboard = {
  holdings: HoldingsResponse | null; performance: { date: string; invested: number; market_value: number }[] | null;
  profiles: Profile[] | null; groups: { id: string; name: string; profile_ids: string[] }[] | null;
  advisor: AdvisorSummary | null; market: MarketOverview | null; errors: Record<string, string>;
};
export type Bar = { date: string; open: number | null; high: number | null; low: number | null; close: number; volume: number };

export type SourceHealth = { status: "ok" | "failing" | "unknown"; last_ok_ago_s: number | null; last_error_ago_s: number | null; last_error?: string; last_count?: number; note?: string | null };
export type MarketStatus = { mode: string; simulated_equities: boolean; sources: Record<string, SourceHealth> };

export type ReadinessCheck = { state: "ok" | "failed" | "waiting" | "n/a"; detail: string };
export type ReadinessFix = { attempts: number; last_try: string; next_try: string; last_action: string; last_error: string | null };
export type ReadinessRow = {
  rec_id: string | null; profile_id: string | null; profile_name: string | null; instrument_id: string; symbol: string; name: string | null;
  asset_type: string; status: "ready" | "fixing" | "attention"; checks: Record<string, ReadinessCheck>; fixes: Record<string, ReadinessFix>; checked_at: string;
};
export type Readiness = {
  summary: { holdings: number; ready: number; fixing: number; attention: number; failing: Record<string, number> };
  labels: Record<string, string>; holdings: ReadinessRow[]; next_pass: string; every_minutes: number;
  last_pass: null | { started_at: string; finished_at: string | null; status: string; trigger: string; error: string | null; stats: Record<string, any> };
};
