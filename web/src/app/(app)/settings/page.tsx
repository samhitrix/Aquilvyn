"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState, useSyncExternalStore } from "react";

import ProfileEditor, { LinkedAccounts } from "@/components/ProfileEditor";
import { ago, useMarketStatus } from "@/components/StatusBanners";
import RecoveryCodeBox from "@/components/RecoveryCodeBox";
import { ErrorNote, Section } from "@/components/ui";
import { toast } from "@/components/Toast";
import { api, auth } from "@/lib/api";
import type { User } from "@/lib/types";
import { qk, useHoldings, useProfiles } from "@/lib/queries";
import { getTheme, setTheme, type ThemeChoice } from "@/lib/theme";
import { SortTh, useSort } from "@/lib/useSort";

/** Your own name and the family's name (shown top-left in the sidebar). Only the owner can rename the family. */
function Account() {
  const user = auth.user;
  const [name, setName] = useState(user?.full_name ?? "");
  const [family, setFamily] = useState(user?.household_name ?? "");
  const owner = user?.role === "owner";
  const save = useMutation({
    mutationFn: async () => {
      let u = user!;
      if (name.trim() && name.trim() !== user?.full_name) u = await api<User>("/auth/me", { method: "PATCH", body: { full_name: name.trim() } });
      if (owner && family.trim() && family.trim() !== user?.household_name) {
        const h = await api<{ name: string }>("/household", { method: "PATCH", body: { name: family.trim() } });
        u = { ...u, household_name: h.name };
      }
      return u;
    },
    onSuccess: (u) => { auth.setUser(u); toast("ok", "Saved", `${u.full_name} · ${u.household_name}`); },
    onError: (e: Error) => toast("error", "Couldn't save", e.message),
  });
  if (!user) return null;
  const changed = name.trim() !== user.full_name || (owner && family.trim() !== user.household_name);
  return (
    <Section title="You & your family">
      <div className="grid gap-3 md:grid-cols-3">
        <div><label className="label">Your name</label><input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={200} /></div>
        <div>
          <label className="label">Family name</label>
          <input className="input" value={family} onChange={(e) => setFamily(e.target.value)} maxLength={200} disabled={!owner} placeholder="The Sharma Family" />
          {!owner && <p className="mt-1 text-xs text-muted">Only the family&apos;s owner can rename it.</p>}
        </div>
        <div className="flex items-end">
          <button className="btn-primary" disabled={!changed || !name.trim() || save.isPending} onClick={() => save.mutate()}>{save.isPending ? "Saving…" : "Save"}</button>
        </div>
      </div>
      <p className="mt-2 text-xs text-muted">Signed in as {user.email} · {titleCaseRole(user.role)}</p>
    </Section>
  );
}

const titleCaseRole = (r: string) => r.charAt(0).toUpperCase() + r.slice(1);

/** Your recovery code: with your email it resets a forgotten password ("Forgot password?") — no email server needed. */
function RecoveryCode() {
  const user = auth.user;
  const [pw, setPw] = useState("");
  const [code, setCode] = useState<string | null>(null);
  const make = useMutation({
    mutationFn: () => api<{ recovery_code: string }>("/auth/recovery-code", { body: { current_password: pw } }),
    onSuccess: (r) => { setCode(r.recovery_code); setPw(""); if (user) auth.setUser({ ...user, has_recovery_code: true }); },
    onError: (e: Error) => toast("error", "Couldn't create the code", e.message),
  });
  if (!user) return null;
  return (
    <div className="mt-4 border-t border-line pt-3">
      <p className="text-sm font-semibold">Recovery code</p>
      <p className="mt-1 text-sm text-ink2">{user.has_recovery_code
        ? "You have a recovery code. Make a new one if you lost it — the old one stops working."
        : "You don't have a recovery code yet. Make one now: it's how you get back in if you forget your password."}</p>
      {code ? (
        <div className="mt-2 max-w-md"><RecoveryCodeBox code={code} title="Your new recovery code" /></div>
      ) : (
        <form className="mt-2 flex flex-wrap items-end gap-2" onSubmit={(e) => { e.preventDefault(); make.mutate(); }}>
          <div><label className="label">Current password</label>
            <input className="input w-56" type="password" autoComplete="current-password" required value={pw} onChange={(e) => setPw(e.target.value)} /></div>
          <button className="btn-primary" disabled={make.isPending || !pw}>{make.isPending ? "Creating…" : user.has_recovery_code ? "Make a new code" : "Create recovery code"}</button>
        </form>
      )}
    </div>
  );
}

/** Change your password (accounts created with email + password; Gmail sign-in has no password here). */
/** A member forgot their password and has no recovery code (e.g. made before codes existed): the owner / an admin
 *  makes a one-time link (30 minutes) and gives it to them. */
function MemberResetLink({ member }: { member: { id: string; role: string; has_password?: boolean } }) {
  const me = useSyncExternalStore(auth.subscribe, () => auth.user, () => null);
  const [link, setLink] = useState<string | null>(null);
  const make = useMutation({
    mutationFn: () => api<{ link: string }>(`/members/${member.id}/reset-link`, { method: "POST" }),
    onSuccess: (r) => setLink(r.link),
    onError: (e: Error) => toast("error", "Couldn't make the link", e.message),
  });
  if (!me || member.id === me.id || !["owner", "admin"].includes(me.role) || member.has_password === false || (member.role === "owner" && me.role !== "owner")) return null;
  if (link)
    return (
      <span className="inline-flex items-center gap-2">
        <input readOnly className="input w-64 text-xs" value={link} onFocus={(e) => e.currentTarget.select()} />
        <button className="btn-ghost text-xs" onClick={() => { navigator.clipboard?.writeText(link); toast("ok", "Link copied", "Valid for 30 minutes, once."); }}>Copy</button>
      </span>
    );
  return <button className="text-xs text-brand hover:underline" disabled={make.isPending} onClick={() => make.mutate()}
                 title="For a member who forgot their password and has no recovery code">{make.isPending ? "Making…" : "Password reset link"}</button>;
}

function Password() {
  const user = useSyncExternalStore(auth.subscribe, () => auth.user, () => null);
  const [f, setF] = useState({ current: "", a: "", b: "" });
  const [code, setCode] = useState<string | null>(null);
  const first = user?.has_password === false;  // no password on this account yet (e.g. forgotten, signing in with Gmail)
  const save = useMutation({
    mutationFn: () => {
      if (f.a !== f.b) throw new Error("The two new passwords don't match.");
      return api<{ recovery_code?: string }>("/auth/password", { body: { new_password: f.a, current_password: f.current } });
    },
    onSuccess: (r) => {
      setF({ current: "", a: "", b: "" });
      if (r?.recovery_code) setCode(r.recovery_code);
      if (user) auth.setUser({ ...user, has_password: true, has_recovery_code: true });
      toast("ok", first ? "Password set" : "Password changed",
            first ? `You can now sign in with ${user?.email} and this password.` : "Other devices were signed out.");
    },
    onError: (e: Error) => toast("error", first ? "Couldn't set the password" : "Couldn't change the password", e.message),
  });
  if (!user) return null;
  return (
    <div id="password" className="scroll-mt-4">
    <Section title="Password & recovery">
      {first && <p className="mb-3 text-sm text-ink2">This account has no password right now, so you can&apos;t sign in with email and password. Set one below.</p>}
      <form className="grid gap-3 md:grid-cols-4" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        {!first && <div><label className="label">Current password</label>
          <input className="input" type="password" autoComplete="current-password" required value={f.current} onChange={(e) => setF({ ...f, current: e.target.value })} /></div>}
        <div><label className="label">New password</label>
          <input className="input" type="password" autoComplete="new-password" minLength={10} required value={f.a} onChange={(e) => setF({ ...f, a: e.target.value })} /></div>
        <div><label className="label">Repeat new password</label>
          <input className="input" type="password" autoComplete="new-password" minLength={10} required value={f.b} onChange={(e) => setF({ ...f, b: e.target.value })} /></div>
        <div className="flex items-end"><button className="btn-primary" disabled={save.isPending}>{save.isPending ? "Saving…" : first ? "Set password" : "Change password"}</button></div>
      </form>
      <p className="mt-2 text-xs text-muted">At least 10 characters.</p>
      {code ? <div className="mt-4"><RecoveryCodeBox code={code} title="Save your recovery code — it's how you get back in if you forget your password">
                <button type="button" className="btn-ghost w-full" onClick={() => setCode(null)}>I&apos;ve saved it</button></RecoveryCodeBox></div>
            : !first && <RecoveryCode />}
    </Section>
    </div>
  );
}

function Appearance() {
  const [t, setT] = useState<ThemeChoice>("system");
  useEffect(() => setT(getTheme()), []);
  return (
    <Section title="Appearance">
      <div className="flex gap-2" role="radiogroup" aria-label="Theme">
        {(["system", "light", "dark"] as const).map((c) => (
          <button key={c} role="radio" aria-checked={t === c} onClick={() => { setTheme(c); setT(c); }}
                  className={t === c ? "btn-primary capitalize" : "btn-ghost capitalize"}>
            {c === "system" ? "🖥 System" : c === "light" ? "☀ Light" : "☾ Dark"}
          </button>
        ))}
      </div>
      <p className="mt-2 text-xs text-muted">System follows your device setting. Both themes use colour steps chosen for contrast on their own background.</p>
    </Section>
  );
}

const DEFAULT_MODEL: Record<string, string> = {
  claude: "claude-opus-5", openai: "", gemini: "", groq: "llama-3.3-70b-versatile", cloudflare: "@cf/meta/llama-3.3-70b-instruct-fp8-fast", ollama: "llama3.1",
};
const PROVIDER_LABEL: Record<string, string> = {
  claude: "Claude (Anthropic)", openai: "OpenAI", gemini: "Google Gemini", groq: "Groq", cloudflare: "Cloudflare Workers AI", ollama: "Ollama (local)",
};

type AiTest = { ok: boolean; status?: number | null; latency_ms?: number; model?: string; reply?: string; error?: string; hint?: string | null };

function announceTest(t: AiTest, what: string) {
  if (!t) return;
  if (t.ok) toast("ok", `${what}: ${t.model} is working`, `HTTP ${t.status ?? 200} in ${((t.latency_ms ?? 0) / 1000).toFixed(1)}s${t.reply ? ` · replied “${t.reply}”` : ""}`);
  else toast("error", `${what}: the AI did not respond correctly`, `${t.status ? `HTTP ${t.status} · ` : ""}${t.error ?? "unknown error"}${t.hint ? ` — ${t.hint}` : ""}`);
}

const move = (xs: string[], i: number, d: number) => { const out = [...xs]; [out[i], out[i + d]] = [out[i + d], out[i]]; return out; };

type AiTestAll = { sample: string | null; note: string | null; results: { provider: string; model: string; ping: AiTest; paused: boolean; paused_reason?: string | null;
  review?: { ok: boolean; symbol: string; packet_chars: number; latency_ms: number; stance: string | null; suggested_action: string | null; plain: string | null; error: string | null } }[] };

function AiTestTable({ data }: { data: AiTestAll }) {
  const [results, sort] = useSort(data.results, {
    model: (r) => PROVIDER_LABEL[r.provider] ?? r.provider, ping: (r) => r.ping.ok, review: (r) => (r.review ? r.review.ok : null),
  }, "ai_sort");
  return (
    <div className="mb-3 overflow-x-auto rounded-lg border border-line">
      <table className="w-full text-sm">
        <thead><tr><SortTh s={sort} k="model">Model</SortTh><SortTh s={sort} k="ping">1 · Connection</SortTh>
          <SortTh s={sort} k="review">{`2 · Real review${data.sample ? ` (${data.sample})` : ""}`}</SortTh></tr></thead>
        <tbody>{results.map((r) => (
          <tr key={r.provider} className="border-t border-line align-top">
            <td className="td"><b>{PROVIDER_LABEL[r.provider] ?? r.provider}</b><span className="block text-xs text-ink2">{r.model}</span>
              {r.paused && <span className="chip mt-1 bg-warn/15 text-warn" title={r.paused_reason ?? ""}>paused</span>}</td>
            <td className="td"><TestResult t={r.ping} /></td>
            <td className="td">{!r.review ? <span className="text-xs text-muted">{r.ping.ok ? (data.sample ? "—" : "no call to review yet — run the analysis first") : "skipped (connection failed)"}</span>
              : r.review.ok ? <span className="text-xs text-up">✔ {r.review.stance}{r.review.suggested_action ? ` · would ${r.review.suggested_action}` : ""} · {(r.review.latency_ms / 1000).toFixed(1)}s · {r.review.packet_chars.toLocaleString()} chars sent
                  {r.review.plain && <span className="block text-ink2">“{r.review.plain}”</span>}</span>
              : <span className="block max-w-md break-words text-xs text-down">✖ {r.review.error}</span>}</td>
          </tr>))}
          {!data.results.length && <tr><td className="td text-muted" colSpan={3}>{data.note}</td></tr>}
        </tbody>
      </table>
    </div>
  );
}

function TestResult({ t }: { t?: AiTest | "running" }) {
  if (!t) return <span className="text-xs text-muted">not tested</span>;
  if (t === "running") return <span className="text-xs text-ink2" role="status">testing…</span>;
  return t.ok
    ? <span className="text-xs text-up">✔ HTTP {t.status ?? 200} · {((t.latency_ms ?? 0) / 1000).toFixed(1)}s</span>
    : <span className="block max-w-md break-words text-xs text-down">✖ {t.status ? `HTTP ${t.status} · ` : ""}{t.error}{t.hint ? ` — ${t.hint}` : ""}</span>;
}

export default function SettingsPage() {
  const qc = useQueryClient();
  const ai = useQuery({ queryKey: qk.aiProviders, queryFn: () => api<any>("/advisor/ai-providers") });
  const members = useQuery({ queryKey: qk.members, queryFn: () => api<any[]>("/members") });
  const rulebook = useQuery({ queryKey: ["rulebook"], queryFn: () => api<any>("/advisor/rulebook") });
  const [f, setF] = useState({ provider: "claude", model: DEFAULT_MODEL.claude, api_key: "", base_url: "", is_primary: true });
  const [m, setM] = useState({ email: "", full_name: "", role: "member", temporary_password: "" });

  const [tests, setTests] = useState<Record<string, AiTest | "running">>({});
  const saveAi = useMutation({
    mutationFn: () => {
      setTests((t) => ({ ...t, [f.provider]: "running" }));
      return api<any>("/advisor/ai-providers", { method: "PUT", body: { ...f, api_key: f.api_key || undefined, base_url: f.base_url || undefined } });
    },
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: qk.aiProviders }); setF({ ...f, api_key: "" });
      setTests((t) => ({ ...t, [res.provider]: res.test }));
      announceTest(res.test, "Saved");
    },
    onError: () => setTests((t) => { const { [f.provider]: _, ...rest } = t; return rest; }),
  });
  const reorder = useMutation({
    mutationFn: (providers: string[]) => api("/advisor/ai-providers/order", { method: "PUT", body: { providers } }),
    onSuccess: () => qc.invalidateQueries({ queryKey: qk.aiProviders }),
    onError: (e: Error) => toast("error", "Couldn't change the order", e.message),
  });
  const retry = useMutation({
    mutationFn: (p: string) => api(`/advisor/ai-providers/${p}/pause`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.aiProviders }); toast("ok", "Provider un-paused", "It will be tried again on the next AI check."); },
  });
  const testAi = useMutation({
    mutationFn: (p: string) => { setTests((t) => ({ ...t, [p]: "running" })); return api<AiTest>(`/advisor/ai-providers/${p}/test`, { method: "POST" }); },
    onSuccess: (res, p) => { setTests((t) => ({ ...t, [p]: res })); announceTest(res, "Test"); },
    onError: (e: Error, p) => { setTests((t) => ({ ...t, [p]: { ok: false, error: e.message } })); toast("error", "Connection test failed", e.message); },
  });
  const testAll = useMutation({
    mutationFn: () => api<AiTestAll>("/advisor/ai-providers/test-all", { method: "POST" }),
    onSuccess: (res) => {
      setTests((t) => ({ ...t, ...Object.fromEntries(res.results.map((r) => [r.provider, r.ping])) }));
      const good = res.results.filter((r) => r.ping.ok && (r.review?.ok ?? true)).length;
      toast(good === res.results.length ? "ok" : "warn", `${good} of ${res.results.length} AI model(s) fully working`,
        res.results.filter((r) => !(r.ping.ok && (r.review?.ok ?? true))).map((r) => `${r.provider}: ${r.review?.error ?? r.ping.error ?? "failed"}`).join(" · ") || undefined);
      qc.invalidateQueries({ queryKey: qk.aiProviders });
    },
    onError: (e: Error) => toast("error", "Test all failed", e.message),
  });
  const toggleAi = useMutation({
    mutationFn: (v: { provider: string; enabled: boolean }) => api(`/advisor/ai-providers/${v.provider}/enabled`, { method: "PUT", body: { enabled: v.enabled } }),
    onSuccess: (_r, v) => { qc.invalidateQueries({ queryKey: qk.aiProviders }); toast("ok", `${PROVIDER_LABEL[v.provider] ?? v.provider} switched ${v.enabled ? "on" : "off"}`, v.enabled ? "It's back in the order below." : "The advisor won't use it until you switch it on again."); },
    onError: (e: Error) => toast("error", "Couldn't change it", e.message),
  });
  const delAi = useMutation({ mutationFn: (p: string) => api(`/advisor/ai-providers/${p}`, { method: "DELETE" }), onSuccess: () => qc.invalidateQueries({ queryKey: qk.aiProviders }) });
  const addMember = useMutation({ mutationFn: () => api("/members", { body: m }), onSuccess: () => { qc.invalidateQueries({ queryKey: qk.members }); setM({ email: "", full_name: "", role: "member", temporary_password: "" }); } });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Settings</h1>
      <Account />
      <Password />
      <Appearance />
      <Profiles />
      <DataSources />
      <div id="ai" />
      <Section title="AI providers — tried in this order" accent="brand"
        action={<button className="btn-ghost py-1" disabled={testAll.isPending || !ai.data?.configured?.length} onClick={() => testAll.mutate()}>
          {testAll.isPending ? "Testing every model…" : "▶ Test all models"}</button>}>
        <p className="mb-3 text-sm text-ink2">
          The rules engine makes every call; the AI checks it (Agree / Caution / Disagree moves the confidence), writes the plain-words summary and answers
          &quot;Ask&quot;. The <b>first</b> provider below is used; if it fails, the next one is tried. A provider that runs out of quota or is rate-limited is
          <b> paused</b> automatically (10 minutes for a per-minute limit, 24 hours for a daily quota). <b>Test all models</b> sends each one a ping and
          one real review of your top call, so a model that answers a ping but fails on real reviews shows up here. Keys are encrypted (AES-256-GCM) and never shown again.
        </p>
        <ol className="mb-3 space-y-2">
          {(ai.data?.configured ?? []).map((p: any, i: number, all: any[]) => (
            <li key={p.provider} className="flex flex-wrap items-center gap-2 rounded-lg border border-line px-3 py-2 text-sm">
              {p.order ? <span className="chip bg-brand text-white" title="Tried in this position">{p.order}</span> : <span className="chip bg-line text-ink2">off</span>}
              <b>{PROVIDER_LABEL[p.provider] ?? p.provider}</b><span className="text-ink2">{p.model}</span>
              <span className="text-xs text-muted">{p.api_key_hint ?? (p.provider === "ollama" ? "local" : "no key")}{p.provider === "cloudflare" && p.base_url ? ` · account ${String(p.base_url).slice(0, 6)}…` : ""}</span>
              {!p.is_enabled && <span className="chip bg-line text-ink2">disabled</span>}
              {p.paused_until && (
                <span className="chip bg-warn/15 text-warn" title={p.paused_reason ?? ""}>paused until {new Date(p.paused_until).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })} (quota / rate limit)</span>
              )}
              <TestResult t={tests[p.provider]} />
              <span className="ml-auto flex items-center gap-2">
                <button className="text-xs text-ink2 disabled:opacity-30" disabled={i === 0 || reorder.isPending} aria-label="Move up"
                        onClick={() => reorder.mutate(move(all.map((x) => x.provider), i, -1))}>▲</button>
                <button className="text-xs text-ink2 disabled:opacity-30" disabled={i === all.length - 1 || reorder.isPending} aria-label="Move down"
                        onClick={() => reorder.mutate(move(all.map((x) => x.provider), i, 1))}>▼</button>
                {p.paused_until && <button className="text-xs text-brand" onClick={() => retry.mutate(p.provider)}>Retry now</button>}
                <button className="text-xs text-brand disabled:opacity-60" disabled={tests[p.provider] === "running"} onClick={() => testAi.mutate(p.provider)}>Test</button>
                <button className="text-xs text-ink2" title={p.is_enabled ? "Stop using this model (its settings are kept)" : "Use this model again"}
                  onClick={() => toggleAi.mutate({ provider: p.provider, enabled: !p.is_enabled })}>{p.is_enabled ? "Switch off" : "Switch on"}</button>
                {p.from_env ? <span className="text-xs text-muted" title="The key comes from .env — switch it off here, or remove it from .env">from .env</span>
                  : <button className="text-xs text-down" onClick={() => delAi.mutate(p.provider)}>Remove</button>}
              </span>
            </li>
          ))}
          {ai.data?.order_used?.length ? <li className="text-xs text-ink2">Order used right now: <b>{ai.data.order_used.map((x: string) => x.split(":")[0]).join(" → ")}</b></li> : null}
          {!ai.data?.configured?.length && <li className="text-sm text-muted">No provider yet — rules-only mode. Add one below (Groq and Cloudflare have free tiers).</li>}
        </ol>
        {testAll.data && <AiTestTable data={testAll.data} />}
        <div className="grid gap-3 md:grid-cols-5">
          <div><label className="label">Provider</label>
            <select className="input" value={f.provider} onChange={(e) => setF({ ...f, provider: e.target.value, model: DEFAULT_MODEL[e.target.value], base_url: "" })}>
              {Object.entries(PROVIDER_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select></div>
          <div><label className="label">Model</label><input className="input" value={f.model} onChange={(e) => setF({ ...f, model: e.target.value })} placeholder="model id" /></div>
          {f.provider === "ollama" ? <div className="md:col-span-2"><label className="label">Ollama URL</label><input className="input" value={f.base_url} onChange={(e) => setF({ ...f, base_url: e.target.value })} placeholder="http://localhost:11434" /></div>
            : f.provider === "cloudflare" ? (
              <div className="grid grid-cols-2 gap-2 md:col-span-2">
                <div><label className="label">Account ID</label><input className="input" value={f.base_url} onChange={(e) => setF({ ...f, base_url: e.target.value })} placeholder="dash.cloudflare.com → Account ID" /></div>
                <div><label className="label">API token</label><input className="input" type="password" autoComplete="off" value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value })} /></div>
              </div>)
            : <div className="md:col-span-2"><label className="label">API key</label><input className="input" type="password" autoComplete="off" value={f.api_key} onChange={(e) => setF({ ...f, api_key: e.target.value })} /></div>}
          <div className="flex items-end gap-2"><label className="flex items-center gap-1 text-sm" title="Put it at the top of the order"><input type="checkbox" checked={f.is_primary} onChange={(e) => setF({ ...f, is_primary: e.target.checked })} />Use first</label>
            <button className="btn-primary flex-1" disabled={!f.model || saveAi.isPending} onClick={() => saveAi.mutate()}>{saveAi.isPending ? "Saving & testing…" : "Save & test"}</button></div>
        </div>
        <p className="mt-2 text-xs text-muted">New providers join the end of the order unless &quot;Use first&quot; is ticked. Groq: console.groq.com → API keys. Cloudflare: Workers AI → &quot;Use REST API&quot; token + your Account ID.</p>
        <ErrorNote error={saveAi.error} />
      </Section>

      <Section title="Household members (logins)">
        <table className="mb-3 w-full text-sm"><tbody>{members.data?.map((u) => (
          <tr key={u.id} className="border-t border-line"><td className="td">{u.full_name}</td><td className="td text-ink2">{u.email}</td><td className="td capitalize">{u.role}</td><td className="td text-ink2">{u.is_active ? "active" : "disabled"}</td>
            <td className="td text-right"><MemberResetLink member={u} /></td></tr>
        ))}</tbody></table>
        <div className="grid gap-3 md:grid-cols-5">
          <input className="input" placeholder="Name" value={m.full_name} onChange={(e) => setM({ ...m, full_name: e.target.value })} />
          <input className="input" placeholder="Email" type="email" value={m.email} onChange={(e) => setM({ ...m, email: e.target.value })} />
          <select className="input" value={m.role} onChange={(e) => setM({ ...m, role: e.target.value })}>{["admin", "member", "advisor", "viewer"].map((r) => <option key={r}>{r}</option>)}</select>
          <input className="input" type="password" placeholder="Temporary password (10+)" value={m.temporary_password} onChange={(e) => setM({ ...m, temporary_password: e.target.value })} />
          <button className="btn-ghost" disabled={addMember.isPending} onClick={() => addMember.mutate()}>Add member</button>
        </div>
        <p className="mt-2 text-xs text-muted">Roles: admin (everything except household ownership) · member (manages profiles granted to them) · advisor (read-only across family, e.g. your CA) · viewer (read-only).</p>
        <ErrorNote error={addMember.error} />
      </Section>

      <Section title={`Advisor rulebook v${rulebook.data?.version ?? ""}`}>
        <p className="mb-2 text-sm text-ink2">Transparent by design — these are the exact rules, in order. Edit <code>services/advisor/advisor_svc/rulebook.yaml</code> to tune them (hot-reloaded).</p>
        <ol className="space-y-1 text-sm">{rulebook.data?.rules?.map((r: any) => <li key={r.id}><code>{r.id}</code> → <b>{r.action}</b> <span className="text-ink2">— {r.reason}</span></li>)}</ol>
      </Section>
    </div>
  );
}

function Profiles() {
  const { data: profiles } = useProfiles();
  const [editing, setEditing] = useState<string | null>(null);
  return (
    <Section title="Profiles (you and family members)">
      <p className="mb-3 text-sm text-ink2">Date of birth, risk profile and tax slab drive the target allocation and tax advice — keep them current.</p>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
        {profiles?.map((p) => (
          <div key={p.id} className="rounded-lg border border-line p-3 text-sm">
            <div className="flex items-center justify-between"><b>{p.display_name}</b>
              <button className="text-brand hover:underline" onClick={() => setEditing(editing === p.id ? null : p.id)}>{editing === p.id ? "Close" : "Edit"}</button></div>
            <div className="text-ink2 capitalize">{p.relationship} · PAN {p.pan_masked ?? "missing"} · {p.age ? `${p.age} yrs` : "age not set"} · {p.risk_profile}</div>
            <LinkedAccounts profile={p} />
            {editing === p.id && <ProfileEditor profile={p} onDone={() => setEditing(null)} />}
          </div>
        ))}
      </div>
    </Section>
  );
}

const SOURCES: [string, string][] = [
  ["yahoo", "Stock & ETF prices — Yahoo Finance (delayed)"], ["amfi_nav", "Mutual-fund NAVs — AMFI via mfapi.in (daily, after market close)"],
  ["amfi_master", "AMFI scheme list — maps fund ISINs to scheme codes"],
  ["nse_size_lists", "NSE Nifty 100 / Midcap 150 lists — large / mid / small cap"],
  ["fundamentals", "Company fundamentals — Yahoo → Screener.in → NSE → Finnhub / Alpha Vantage (if keys set) → yfinance"],
];

type SourceTest = { symbol: string; mf: string | null; mode: string; live: boolean; note: string | null;
  results: { group: string; source: string; status: string; detail: string; ms?: number }[] };
const SRC_TONE: Record<string, string> = { ok: "bg-up/15 text-up", empty: "bg-warn/15 text-warn", error: "bg-down/15 text-down" };

/** One real request to every source — prices, history, index, MF NAV and each fundamentals source. */
function SourceTester() {
  const holdings = useHoldings({ type: "household" });
  const hs = holdings.data?.holdings ?? [];
  const firstStock = hs.find((h) => h.asset_type === "stock")?.symbol ?? "RELIANCE.NS";
  const firstMf = hs.find((h) => h.asset_type === "mutual_fund" && /^\d+$/.test(h.symbol))?.symbol;
  const [sym, setSym] = useState("");
  const run = useMutation({
    mutationFn: () => api<SourceTest>("/market/sources/test", { query: { symbol: (sym || firstStock).trim().toUpperCase(), mf: firstMf } }),
    onSuccess: (r) => {
      const bad = r.results.filter((x) => x.status === "error" || x.status === "empty");
      const fundOk = r.results.some((x) => x.group === "fundamentals" && x.status === "ok");
      toast(bad.length ? "warn" : "ok", `${r.results.length - bad.length} of ${r.results.length} sources answered`,
        !fundOk && r.live ? "No fundamentals source returned data — see the table for each source's error." : undefined);
    },
    onError: (e: Error) => toast("error", "Source test failed", e.message),
  });
  const [srcRows, srcSort] = useSort(run.data?.results ?? [], {
    group: (r) => r.group, source: (r) => r.source, status: (r) => r.status, detail: (r) => r.detail, ms: (r) => r.ms,
  }, "src_sort");
  return (
    <div className="mt-4 rounded-lg border border-line p-3">
      <div className="flex flex-wrap items-end gap-2">
        <div><label className="label">Test with stock</label>
          <input className="input w-40" value={sym} placeholder={firstStock} onChange={(e) => setSym(e.target.value)} /></div>
        <button className="btn-primary" disabled={run.isPending} onClick={() => run.mutate()}>{run.isPending ? "Testing every source…" : "▶ Test all sources"}</button>
        <span className="text-xs text-muted">One real request to each source{firstMf ? " (and your first mutual fund's NAV)" : ""}. Takes up to ~30 s.</span>
      </div>
      {run.data?.note && <p className="mt-2 text-sm text-warn">{run.data.note}</p>}
      {run.data && (
        <table className="mt-3 w-full text-sm">
          <thead><tr><SortTh s={srcSort} k="group">What</SortTh><SortTh s={srcSort} k="source">Source</SortTh><SortTh s={srcSort} k="status">Result</SortTh>
            <SortTh s={srcSort} k="detail">Details</SortTh><SortTh s={srcSort} k="ms" className="th text-right">Time</SortTh></tr></thead>
          <tbody>{srcRows.map((r, i) => (
            <tr key={i} className="border-t border-line align-top">
              <td className="td capitalize text-ink2">{r.group}</td><td className="td font-medium">{r.source}</td>
              <td className="td"><span className={`chip ${SRC_TONE[r.status] ?? "bg-line text-ink2"}`}>{r.status === "ok" ? "✔ working" : r.status === "empty" ? "answered, no data" : r.status === "error" ? "✖ failed" : r.status}</span></td>
              <td className="td max-w-xl break-words text-xs">{r.detail}</td>
              <td className="td tnum text-right text-xs text-ink2">{r.ms != null ? `${(r.ms / 1000).toFixed(1)}s` : ""}</td>
            </tr>))}</tbody>
        </table>
      )}
    </div>
  );
}

function DataSources() {
  const st = useMarketStatus(true);
  const all: [string, string][] = [...SOURCES, ...Object.keys(st.data?.sources ?? {}).filter((k) => k.startsWith("fund_portfolio:")).sort()
    .map((k): [string, string] => [k, `Fund holdings — ${k.slice("fund_portfolio:".length)} (monthly portfolio, for look-through)`])];
  const [srcList, sort] = useSort(all, {
    source: ([, label]) => label, status: ([k]) => st.data?.sources?.[k]?.status ?? "unknown", ok: ([k]) => st.data?.sources?.[k]?.last_ok_ago_s,
    error: ([k]) => st.data?.sources?.[k]?.last_error ?? "",
  }, "sources_sort");
  return (
    <section id="data-sources">
      <Section title="Data sources">
        {st.data?.simulated_equities && (
          <p className="mb-3 rounded-lg border border-down/40 bg-down/10 px-3 py-2 text-sm text-down">
            Stock prices are <b>simulated</b>. Set <code>MARKET_DATA_PROVIDER=yahoo</code> in <code>.env</code> and run <code>python scripts/fm.py up-lite</code>.
          </p>
        )}
        <table className="w-full text-sm">
          <thead><tr><SortTh s={sort} k="source">Source</SortTh><SortTh s={sort} k="status">Status</SortTh><SortTh s={sort} k="ok" title="Sorts by how long ago">Last success</SortTh>
            <SortTh s={sort} k="error">Last error</SortTh></tr></thead>
          <tbody>{srcList.map(([k, label]) => {
            const v = st.data?.sources?.[k];
            const tone = !v || v.status === "unknown" ? "bg-line text-ink2" : v.status === "ok" ? "bg-up/15 text-up" : "bg-down/15 text-down";
            return (
              <tr key={k} className="border-t border-line">
                <td className="td">{label}</td>
                <td className="td"><span className={`chip ${tone}`}>{v?.status === "ok" ? "working" : v?.status === "failing" ? "failing" : "not used yet"}</span></td>
                <td className="td text-ink2">{ago(v?.last_ok_ago_s)}{v?.last_count ? ` · ${v.last_count} ${k.startsWith("fund_portfolio:") ? "fund(s)" : "prices"}` : ""}</td>
                <td className="td text-xs">
                  {v?.status === "failing" && v.last_error ? <span className="text-down">{v.last_error} ({ago(v.last_error_ago_s)})</span> : v?.last_error ? <span className="text-muted">earlier: {v.last_error.slice(0, 120)}</span> : "—"}
                  {v?.note && <span className="block text-warn">{v.note}</span>}
                </td>
              </tr>
            );
          })}</tbody>
        </table>
        <SourceTester />
      </Section>
    </section>
  );
}
