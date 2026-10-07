"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { ErrorNote, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { titleCase } from "@/lib/format";
import ProfileEditor, { LinkedAccounts } from "@/components/ProfileEditor";
import { invalidatePortfolio, qk, useGroups, usePortfolios, useProfiles } from "@/lib/queries";

export default function FamilyPage() {
  const qc = useQueryClient();
  const { data: profiles } = useProfiles();
  const { data: portfolios } = usePortfolios();
  const { data: groups } = useGroups();
  const [p, setP] = useState({ display_name: "", relationship: "spouse", pan: "", date_of_birth: "", risk_profile: "moderate", tax_slab_pct: "" });
  const [acct, setAcct] = useState({ name: "", asset_type: "epf", interest_rate: "8.25", portfolio_id: "" });
  const [g, setG] = useState({ name: "", ids: [] as string[] });
  const [editing, setEditing] = useState<string | null>(null);
  const inv = () => ["profiles", "portfolios", "groups", "holdings", "dashboard"].forEach((k) => qc.invalidateQueries({ queryKey: [k] }));

  const addProfile = useMutation({
    mutationFn: () => api("/profiles", { body: { display_name: p.display_name, relationship: p.relationship, pan: p.pan || undefined, date_of_birth: p.date_of_birth || undefined,
      risk_profile: p.risk_profile, tax_slab_pct: p.tax_slab_pct ? Number(p.tax_slab_pct) : undefined } }),
    onSuccess: () => { inv(); setP({ ...p, display_name: "", pan: "", date_of_birth: "" }); },
  });
  const addAccount = useMutation({
    mutationFn: async () => {
      const inst = await api<any>("/market/instruments/private", { body: { name: acct.name, asset_type: acct.asset_type, meta: { interest_rate: Number(acct.interest_rate) } } });
      return inst;
    },
    onSuccess: () => { inv(); setAcct({ ...acct, name: "" }); },
  });
  const clear = useMutation({
    mutationFn: (id: string) => api<{ deleted: number }>(`/portfolios/${id}/clear`, { method: "POST" }),
    onSuccess: () => { invalidatePortfolio(qc); qc.invalidateQueries({ queryKey: qk.imports }); },
  });
  const addGroup = useMutation({ mutationFn: () => api("/groups", { body: { name: g.name, profile_ids: g.ids } }), onSuccess: () => { inv(); setG({ name: "", ids: [] }); } });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Family</h1>
      <Section title="Profiles">
        <ErrorNote error={clear.error} />
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 2xl:grid-cols-5">
          {profiles?.map((x) => (
            <div key={x.id} className="rounded-lg border border-line p-3 text-sm">
              <div className="flex items-center justify-between gap-2"><b>{x.display_name}</b>
                <span className="flex items-center gap-2"><span className="capitalize text-muted">{x.relationship}</span>
                  <button className="text-brand hover:underline" onClick={() => setEditing(editing === x.id ? null : x.id)}>{editing === x.id ? "Close" : "Edit"}</button></span></div>
              <div className="mt-1 text-ink2">PAN {x.pan_masked ? <span title="Statements with this PAN are matched to this profile">{x.pan_masked} ✔</span> : <span className="text-warn" title="Add a PAN so CAS statements are matched automatically">missing</span>} · {x.age ? `${x.age} yrs` : <span className="text-warn">age —</span>} · {titleCase(x.risk_profile)} · slab {x.tax_slab_pct ?? "—"}%</div>
              <LinkedAccounts profile={x} />
              {editing === x.id && <ProfileEditor profile={x} onDone={() => setEditing(null)} />}
              <ul className="mt-2 space-y-1 border-t border-line pt-2 text-xs">
                {portfolios?.filter((pf) => pf.profile_id === x.id).map((pf) => (
                  <li key={pf.id} className="flex items-center justify-between gap-2">
                    <span className="truncate text-ink2">{pf.name}</span>
                    <button className="shrink-0 text-down hover:underline disabled:opacity-50" disabled={clear.isPending}
                            title="Remove every transaction in this portfolio (the portfolio itself stays)"
                            onClick={() => { if (confirm(`Clear ALL transactions in ${x.display_name} · ${pf.name}? Imports into it are marked deleted and can be re-imported.`)) clear.mutate(pf.id); }}>
                      {clear.isPending && clear.variables === pf.id ? "Clearing…" : "Clear data"}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </Section>
      <Section title="Add a family member (profile)">
        <div className="grid gap-3 md:grid-cols-6">
          <div className="md:col-span-2"><label className="label">Name</label><input className="input" value={p.display_name} onChange={(e) => setP({ ...p, display_name: e.target.value })} /></div>
          <div><label className="label">Relationship</label><select className="input" value={p.relationship} onChange={(e) => setP({ ...p, relationship: e.target.value })}>{["spouse", "parent", "child", "sibling", "huf", "other"].map((r) => <option key={r}>{r}</option>)}</select></div>
          <div><label className="label" title="Stored encrypted. CAS statements with this PAN are matched to this profile.">PAN (matches statements)</label><input className="input uppercase" maxLength={10} value={p.pan} onChange={(e) => setP({ ...p, pan: e.target.value.toUpperCase() })} /></div>
          <div><label className="label">Date of birth</label><input className="input" type="date" value={p.date_of_birth} onChange={(e) => setP({ ...p, date_of_birth: e.target.value })} /></div>
          <div><label className="label">Risk profile</label><select className="input" value={p.risk_profile} onChange={(e) => setP({ ...p, risk_profile: e.target.value })}>{["conservative", "moderate", "aggressive"].map((r) => <option key={r}>{r}</option>)}</select></div>
          <div><label className="label">Tax slab %</label><input className="input" inputMode="decimal" value={p.tax_slab_pct} onChange={(e) => setP({ ...p, tax_slab_pct: e.target.value })} /></div>
        </div>
        <button className="btn-primary mt-3" disabled={!p.display_name || addProfile.isPending} onClick={() => addProfile.mutate()}>Add profile</button>
        <ErrorNote error={addProfile.error} />
      </Section>
      <Section title="Retirement & deposit accounts (EPF · VPF · PPF · FD)">
        <p className="mb-3 text-sm text-ink2">Create the account once, then log contributions/interest in Transactions (choose the account in the instrument search). Value grows at the rate you set.</p>
        <div className="grid gap-3 md:grid-cols-4">
          <div className="md:col-span-2"><label className="label">Account name</label><input className="input" placeholder="EPF — UAN ****1234" value={acct.name} onChange={(e) => setAcct({ ...acct, name: e.target.value })} /></div>
          <div><label className="label">Type</label><select className="input" value={acct.asset_type} onChange={(e) => setAcct({ ...acct, asset_type: e.target.value })}>{["epf", "vpf", "ppf", "fixed_deposit", "bond", "nps"].map((t) => <option key={t} value={t}>{t.toUpperCase()}</option>)}</select></div>
          <div><label className="label">Interest rate % p.a.</label><input className="input" inputMode="decimal" value={acct.interest_rate} onChange={(e) => setAcct({ ...acct, interest_rate: e.target.value })} /></div>
        </div>
        <button className="btn-primary mt-3" disabled={!acct.name || addAccount.isPending} onClick={() => addAccount.mutate()}>{addAccount.isSuccess ? "Created ✔ — now add contributions" : "Create account"}</button>
        <ErrorNote error={addAccount.error} />
      </Section>
      <Section title="Groups (custom consolidated views)">
        <ul className="mb-3 text-sm">{groups?.map((x) => <li key={x.id}>🗂 <b>{x.name}</b> — {x.profile_ids.map((id) => profiles?.find((q) => q.id === id)?.display_name).join(", ")}</li>)}</ul>
        <div className="flex flex-wrap items-end gap-3">
          <div><label className="label">Group name</label><input className="input" value={g.name} onChange={(e) => setG({ ...g, name: e.target.value })} placeholder="Parents" /></div>
          {profiles?.map((x) => <label key={x.id} className="flex items-center gap-1 text-sm"><input type="checkbox" checked={g.ids.includes(x.id)} onChange={(e) => setG({ ...g, ids: e.target.checked ? [...g.ids, x.id] : g.ids.filter((i) => i !== x.id) })} />{x.display_name}</label>)}
          <button className="btn-ghost" disabled={!g.name || !g.ids.length} onClick={() => addGroup.mutate()}>Create group</button>
        </div>
      </Section>
    </div>
  );
}
