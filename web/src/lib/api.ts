/**
 * API client.
 *  - Access token lives only in memory (never localStorage) → not stealable by XSS persistence.
 *  - Refresh token is an HttpOnly, SameSite=Strict cookie scoped to /api/v1/auth; the browser
 *    sends it automatically on /auth/refresh.
 *  - A 401 triggers exactly ONE refresh (single-flight, shared by all concurrent requests), then
 *    the original request is retried once.
 */
import type { TokenOut, User } from "./types";

let accessToken: string | null = null;
let currentUser: User | null = null;
let refreshing: Promise<boolean> | null = null;
const listeners = new Set<() => void>();

export class ApiError extends Error {
  constructor(public status: number, public detail: unknown) {
    super(ApiError.describe(status, detail));
  }

  static describe(status: number, detail: unknown): string {
    const d = (detail as any)?.detail ?? detail;
    if (Array.isArray(d)) return d.map((e: any) => e?.msg ?? String(e)).join("; ");  // FastAPI 422 list
    if (typeof d === "string" && d.trim() && !d.trimStart().startsWith("<")) return d.length > 300 ? d.slice(0, 300) + "…" : d;
    if (d && typeof d === "object" && typeof (d as any).message === "string") return (d as any).message;
    if (status === 413) return "File too large.";
    if (status === 429) return "Too many requests — please wait a few seconds and retry.";
    if (status === 502 || status === 503) return `A backend service is unavailable (HTTP ${status}). Check it's running: python scripts/fm.py ps`;
    if (status === 504) return "The server took too long to respond (HTTP 504). Retry; if it persists check: python scripts/fm.py logs";
    return `Request failed (HTTP ${status}).`;
  }
}

export const auth = {
  get token() { return accessToken; },
  get user() { return currentUser; },
  subscribe(fn: () => void) { listeners.add(fn); return () => listeners.delete(fn); },
  /** e.g. after renaming yourself — keeps the access token, refreshes what the UI shows */
  setUser(u: User) {
    currentUser = u;
    listeners.forEach((l) => l());
  },
  set(t: TokenOut | null) {
    accessToken = t?.access_token ?? null;
    currentUser = t?.user ?? null;
    listeners.forEach((l) => l());
  },
};

export async function refreshSession(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const r = await fetch("/api/v1/auth/refresh", { method: "POST", credentials: "include", headers: { "X-Client-Platform": "web" } });
        if (!r.ok) { auth.set(null); return false; }
        auth.set(await r.json());
        return true;
      } catch { return false; }
      finally { setTimeout(() => { refreshing = null; }, 0); }
    })();
  }
  return refreshing;
}

type Opts = { method?: string; body?: unknown; query?: Record<string, string | number | boolean | undefined | null>; form?: FormData; signal?: AbortSignal };

export async function api<T = any>(path: string, opts: Opts = {}, retry = true): Promise<T> {
  const qs = opts.query ? "?" + new URLSearchParams(Object.entries(opts.query).filter(([, v]) => v !== undefined && v !== null && v !== "").map(([k, v]) => [k, String(v)])).toString() : "";
  const headers: Record<string, string> = { "X-Client-Platform": "web" };
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(`/api/v1${path}${qs}`, {
    method: opts.method ?? (opts.body !== undefined || opts.form ? "POST" : "GET"),
    headers, credentials: "include", signal: opts.signal,
    body: opts.form ?? (opts.body !== undefined ? JSON.stringify(opts.body) : undefined),
  });
  if (res.status === 401 && retry && !path.startsWith("/auth/")) {
    if (await refreshSession()) return api<T>(path, opts, false);
  }
  if (!res.ok) {
    // read the body once — calling .json() then .text() on the same Response throws "body stream already read"
    const raw = await res.text().catch(() => "");
    let detail: unknown = raw;
    try { detail = raw ? JSON.parse(raw) : raw; } catch { /* not JSON (e.g. an HTML error page from the gateway) */ }
    throw new ApiError(res.status, detail);
  }
  return (res.status === 204 ? undefined : await res.json()) as T;
}

/** → a recovery code (shown once) when the account didn't have one yet (made before recovery codes existed) */
export async function login(email: string, password: string): Promise<string | null> {
  const t = await api<TokenOut>("/auth/login", { body: { email, password } });
  auth.set(t);
  return t.recovery_code ?? null;
}
/** → the recovery code (shown once): the way back in if the password is ever forgotten */
export async function register(body: { email: string; password: string; full_name: string; household_name?: string }): Promise<string | null> {
  const t = await api<TokenOut>("/auth/register", { body });
  auth.set(t);
  return t.recovery_code ?? null;
}
export async function logout() {
  try { await api("/auth/logout", { method: "POST" }); } finally { auth.set(null); }
}
