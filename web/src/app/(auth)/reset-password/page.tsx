"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";

/** Opened from the link `python scripts/fm.py reset-password <email>` prints: choose a new password (the link works once). */
export default function ResetPasswordPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [pw, setPw] = useState({ a: "", b: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  useEffect(() => setToken(new URLSearchParams(window.location.search).get("token") ?? ""), []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (pw.a !== pw.b) return setErr("The two passwords don't match.");
    setBusy(true);
    setErr(null);
    try {
      await api("/auth/password/reset", { body: { token, new_password: pw.a } });
      setDone(true);
      setTimeout(() => router.replace("/login"), 2500);
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
          <div className="text-xl font-semibold">Aquil<span className="text-brand">vyn</span></div>
          <p className="mt-1 text-sm text-ink2">Choose a new password</p>
        </div>
        {!token ? (
          <p className="text-sm text-ink2">This page opens from a password-reset link. Use “Forgot password?” on the sign-in page to get one.</p>
        ) : done ? (
          <p role="status" className="rounded-lg bg-up/10 px-3 py-2 text-sm text-up">Password changed. Taking you to sign in…</p>
        ) : (
          <>
            <div><label className="label">New password</label>
              <input className="input" type="password" autoComplete="new-password" minLength={10} required value={pw.a} onChange={(e) => setPw({ ...pw, a: e.target.value })} />
              <p className="mt-1 text-xs text-muted">At least 10 characters. You'll be signed out on other devices.</p></div>
            <div><label className="label">Repeat it</label>
              <input className="input" type="password" autoComplete="new-password" minLength={10} required value={pw.b} onChange={(e) => setPw({ ...pw, b: e.target.value })} /></div>
            {err && <p role="alert" className="rounded-lg bg-down/10 px-3 py-2 text-sm text-down">{err}</p>}
            <button className="btn-primary w-full" disabled={busy}>{busy ? "Please wait…" : "Set new password"}</button>
          </>
        )}
        <p className="text-center text-sm text-ink2"><Link className="text-brand" href="/login">Back to sign in</Link></p>
      </form>
    </main>
  );
}
