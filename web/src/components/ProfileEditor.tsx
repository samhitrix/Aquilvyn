"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ErrorNote } from "@/components/ui";
import { api, auth } from "@/lib/api";
import type { Profile, User } from "@/lib/types";

const REL = ["self", "spouse", "parent", "child", "sibling", "huf", "other"];
const RISK = ["conservative", "moderate", "aggressive"];

/** Edit every field of a profile. PAN is write-only (stored encrypted, shown masked). */
export default function ProfileEditor({ profile, onDone }: { profile: Profile; onDone: () => void }) {
  const qc = useQueryClient();
  const t = profile.target_allocation ?? {};
  const [f, setF] = useState({
    display_name: profile.display_name, relationship: profile.relationship, pan: "", date_of_birth: profile.date_of_birth ?? "",
    email: profile.email ?? "", mobile: profile.mobile ?? "",
    risk_profile: profile.risk_profile, tax_slab_pct: profile.tax_slab_pct?.toString() ?? "", retirement_age: String(profile.retirement_age ?? 60),
    equity: t.equity?.toString() ?? "", debt: t.debt?.toString() ?? "", gold: t.gold?.toString() ?? "",
  });
  const alloc = [f.equity, f.debt, f.gold].some((x) => x !== "");
  const allocSum = [f.equity, f.debt, f.gold].reduce((a, x) => a + (Number(x) || 0), 0);
  const save = useMutation({
    mutationFn: () => api(`/profiles/${profile.id}`, {
      method: "PATCH",
      body: {
        display_name: f.display_name.trim(), relationship: f.relationship, pan: f.pan || undefined, date_of_birth: f.date_of_birth || null,
        email: f.email.trim(), mobile: f.mobile.trim(),
        risk_profile: f.risk_profile, tax_slab_pct: f.tax_slab_pct === "" ? null : Number(f.tax_slab_pct), retirement_age: Number(f.retirement_age),
        ...(alloc ? { target_allocation: { equity: Number(f.equity) || 0, debt: Number(f.debt) || 0, gold: Number(f.gold) || 0 } } : {}),
      },
    }),
    onSuccess: async () => {
      if (profile.linked_user_id && profile.linked_user_id === auth.user?.id) {
        try { auth.setUser(await api<User>("/auth/me")); } catch { /* sidebar refreshes on next login */ }
      }
      ["profiles", "dashboard", "holdings", "advisor-summary", "portfolios", "members"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));
      // the advisor rewrites existing cards with the new name in the background
      setTimeout(() => ["recommendations", "advisor-summary", "recommendation", "dashboard"].forEach((k) => qc.invalidateQueries({ queryKey: [k] })), 1500);
      onDone();
    },
  });
  const set = (k: keyof typeof f) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setF({ ...f, [k]: e.target.value });
  return (
    <form className="mt-2 space-y-2 border-t border-line pt-2" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <div className="grid grid-cols-2 gap-2">
        <div className="col-span-2"><label className="label">Name</label><input className="input" required value={f.display_name} onChange={set("display_name")} /></div>
        <div><label className="label">Relationship</label><select className="input" value={f.relationship} onChange={set("relationship")}>{REL.map((r) => <option key={r}>{r}</option>)}</select></div>
        <div><label className="label">PAN {profile.pan_masked ? `(now ${profile.pan_masked})` : "(not set)"}</label>
          <input className="input uppercase" maxLength={10} pattern="[A-Z]{5}[0-9]{4}[A-Z]" placeholder={profile.pan_masked ? "leave blank to keep" : "ABCDE1234F"} value={f.pan} onChange={(e) => setF({ ...f, pan: e.target.value.toUpperCase() })} /></div>
        <div><label className="label">Date of birth</label><input className="input" type="date" value={f.date_of_birth} onChange={set("date_of_birth")} /></div>
        <div><label className="label">Email <span className="font-normal text-muted">(optional)</span></label>
          <input className="input" type="email" autoComplete="off" placeholder="name@example.com" value={f.email} onChange={set("email")} /></div>
        <div><label className="label">Mobile <span className="font-normal text-muted">(optional)</span></label>
          <input className="input" type="tel" inputMode="tel" placeholder="98765 43210" value={f.mobile} onChange={set("mobile")} /></div>
        <div><label className="label">Retirement age</label><input className="input" inputMode="numeric" value={f.retirement_age} onChange={set("retirement_age")} /></div>
        <div><label className="label">Risk profile</label><select className="input" value={f.risk_profile} onChange={set("risk_profile")}>{RISK.map((r) => <option key={r}>{r}</option>)}</select></div>
        <div><label className="label">Tax slab %</label><input className="input" inputMode="decimal" value={f.tax_slab_pct} onChange={set("tax_slab_pct")} /></div>
      </div>
      <div>
        <label className="label">Target allocation % (optional — otherwise from risk profile and age)</label>
        <div className="grid grid-cols-3 gap-2">
          {(["equity", "debt", "gold"] as const).map((k) => <input key={k} className="input" inputMode="decimal" placeholder={k} aria-label={`${k} %`} value={f[k]} onChange={set(k)} />)}
        </div>
        {alloc && allocSum !== 100 && <p className="mt-1 text-xs text-warn">Adds up to {allocSum}% — should be 100%.</p>}
      </div>
      <ErrorNote error={save.error} />
      <div className="flex gap-2">
        <button className="btn-primary" disabled={save.isPending || (alloc && allocSum !== 100)}>{save.isPending ? "Saving…" : "Save"}</button>
        <button type="button" className="btn-ghost" onClick={onDone}>Cancel</button>
      </div>
    </form>
  );
}

/** Broker accounts linked to a profile (from statements) — the next statement from one of them
 *  is routed to this profile automatically. */
export function LinkedAccounts({ profile }: { profile: Profile }) {
  const qc = useQueryClient();
  const unlink = useMutation({
    mutationFn: (id: string) => api(`/profiles/${profile.id}/accounts/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["profiles"] }),
  });
  if (!profile.accounts?.length) return null;
  return (
    <div className="mt-1 flex flex-wrap gap-1 text-xs">
      {profile.accounts.map((a) => (
        <span key={a.id} className="chip bg-page" title="Statements from this account are imported into this profile automatically">
          🔗 {a.label}
          <button aria-label={`Unlink ${a.label}`} className="text-muted hover:text-down" disabled={unlink.isPending}
                  onClick={() => { if (confirm(`Unlink ${a.label} from ${profile.display_name}?`)) unlink.mutate(a.id); }}>✕</button>
        </span>
      ))}
    </div>
  );
}
