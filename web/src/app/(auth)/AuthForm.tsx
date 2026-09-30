"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { login, register } from "@/lib/api";

function GmailIcon() {
  return (
    <svg aria-hidden width="20" height="20" viewBox="0 0 48 48">
      <path fill="#4caf50" d="M45 16.2l-5 2.75-5 4.75V40h7c1.657 0 3-1.343 3-3V16.2z" />
      <path fill="#1e88e5" d="M3 16.2l3.614 1.71L13 23.7V40H6c-1.657 0-3-1.343-3-3V16.2z" />
      <polygon fill="#e53935" points="35,11.2 24,19.45 13,11.2 12,17 13,23.7 24,31.95 35,23.7 36,17" />
      <path fill="#c62828" d="M3 12.298V16.2l10 7.5V11.2L9.876 8.859A4.3 4.3 0 0 0 7.298 8C4.924 8 3 9.924 3 12.298z" />
      <path fill="#fbc02d" d="M45 12.298V16.2l-10 7.5V11.2l3.124-2.341A4.3 4.3 0 0 1 40.702 8C43.076 8 45 9.924 45 12.298z" />
    </svg>
  );
}

export default function AuthForm({ mode }: { mode: "login" | "register" }) {
  const router = useRouter();
  const [f, setF] = useState({ email: "", password: "", full_name: "", household_name: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {  // e.g. /login?error=… after a failed "Login with Gmail"
    const e = new URLSearchParams(window.location.search).get("error");
    if (e) setErr(e);
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      if (mode === "login") await login(f.email, f.password);
      else await register({ email: f.email, password: f.password, full_name: f.full_name, household_name: f.household_name || undefined });
      router.replace("/dashboard");
    } catch (e: any) {
      setErr(e.message ?? "Something went wrong");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="grid min-h-screen place-items-center p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4 p-6 shadow-sm">
        <div>
          <div className="text-xl font-semibold">Folio<span className="text-brand">Matrix</span></div>
          <p className="mt-1 text-sm text-ink2">{mode === "login" ? "Sign in to your family's wealth dashboard" : "Create your family's workspace"}</p>
        </div>
        {mode === "register" && (
          <>
            <div><label className="label">Your name</label><input className="input" required value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></div>
            <div><label className="label">Family / household name (optional)</label><input className="input" placeholder="The Sharma Family" value={f.household_name} onChange={(e) => setF({ ...f, household_name: e.target.value })} /></div>
          </>
        )}
        <div><label className="label">Email</label><input className="input" type="email" autoComplete="email" required value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></div>
        <div>
          <label className="label">Password</label>
          <input className="input" type="password" autoComplete={mode === "login" ? "current-password" : "new-password"} minLength={mode === "register" ? 10 : 1} required value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
          {mode === "register" && <p className="mt-1 text-xs text-muted">At least 10 characters.</p>}
        </div>
        {err && <p role="alert" className="rounded-lg bg-down/10 px-3 py-2 text-sm text-down">{err}</p>}
        <button className="btn-primary w-full" disabled={busy}>{busy ? "Please wait…" : mode === "login" ? "Sign in" : "Create account"}</button>
        <div className="flex items-center gap-3 text-xs text-muted"><span className="h-px flex-1 bg-line" />or<span className="h-px flex-1 bg-line" /></div>
        <a className="btn-ghost flex w-full items-center justify-center gap-2 font-medium" href="/api/v1/auth/oidc/login">
          <GmailIcon />{mode === "login" ? "Login with Gmail" : "Sign up with Gmail"}
        </a>
        <p className="text-center text-sm text-ink2">
          {mode === "login" ? <>New here? <Link className="text-brand" href="/register">Create an account</Link></> : <>Have an account? <Link className="text-brand" href="/login">Sign in</Link></>}
        </p>
      </form>
    </main>
  );
}
