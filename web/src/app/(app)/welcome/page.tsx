"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { toast } from "@/components/Toast";
import { ErrorNote, Section } from "@/components/ui";
import { api, auth } from "@/lib/api";
import { qk, useProfiles } from "@/lib/queries";

const PAN_RE = /^[A-Z]{5}[0-9]{4}[A-Z]$/;
const SLABS: [string, string][] = [["0", "No tax / below ₹4 L"], ["5", "5%"], ["10", "10%"], ["15", "15%"], ["20", "20%"], ["25", "25%"], ["30", "30%"]];

/** First sign-in with Google: Google only shares your name and email — this asks for the rest once. */
export default function WelcomePage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { data: profiles } = useProfiles();
  const self = useMemo(() => profiles?.find((p) => p.relationship === "self"), [profiles]);
  const [f, setF] = useState({ name: "", household: "", pan: "", noPan: false, dob: "", slab: "", risk: "moderate" });
  const [err, setErr] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const u = auth.user;
    if (u && !f.name) setF((x) => ({ ...x, name: u.full_name ?? "", household: u.full_name ? `${u.full_name.split(" ")[0]}'s Family` : "" }));
  }, [f.name]);

  const pan = f.pan.trim().toUpperCase();
  const panOk = f.noPan || PAN_RE.test(pan);
  const ready = f.name.trim().length > 1 && panOk && !!f.dob && !!self;

  async function save() {
    if (!self) return;
    setBusy(true);
    setErr(null);
    try {
      await api("/auth/me", { method: "PATCH", body: { full_name: f.name.trim() } });
      if (f.household.trim()) await api("/household", { method: "PATCH", body: { name: f.household.trim() } }).catch(() => undefined);
      await api(`/profiles/${self.id}`, { method: "PATCH", body: {
        display_name: f.name.trim(), date_of_birth: f.dob, risk_profile: f.risk,
        ...(f.noPan ? {} : { pan }), ...(f.slab !== "" ? { tax_slab_pct: Number(f.slab) } : {}),
      } });
      qc.invalidateQueries({ queryKey: qk.profiles });
      toast("ok", "Welcome to FolioSense", "Next: import a CAS or your broker's holdings statement.");
      router.replace("/import");
    } catch (e) {
      setErr(e);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div>
        <h1 className="text-2xl font-bold">Welcome — a few details first</h1>
        <p className="text-sm text-ink2">Google only shared your name and email. These make statements match you automatically (PAN) and keep tax and advice right for you.</p>
      </div>
      <Section title="Your details" accent="brand">
        <div className="grid gap-3 sm:grid-cols-2">
          <div><label className="label">Your name</label><input className="input" value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></div>
          <div><label className="label">Family / household name</label><input className="input" value={f.household} onChange={(e) => setF({ ...f, household: e.target.value })} /></div>
          <div>
            <label className="label">PAN</label>
            <input className="input uppercase" value={f.pan} disabled={f.noPan} maxLength={10} placeholder="ABCDE1234F"
                   onChange={(e) => setF({ ...f, pan: e.target.value })} aria-invalid={!panOk} />
            <label className="mt-1 flex items-center gap-1 text-xs text-ink2"><input type="checkbox" checked={f.noPan} onChange={(e) => setF({ ...f, noPan: e.target.checked })} />I don&apos;t have a PAN yet</label>
            {!panOk && f.pan && <p className="mt-1 text-xs text-down">A PAN looks like ABCDE1234F (5 letters, 4 digits, 1 letter).</p>}
            <p className="mt-1 text-xs text-muted">Used to route CAS / tax statements to you. Stored encrypted; only the last 4 are ever shown.</p>
          </div>
          <div><label className="label">Date of birth</label><input className="input" type="date" value={f.dob} onChange={(e) => setF({ ...f, dob: e.target.value })} />
            <p className="mt-1 text-xs text-muted">Sets your target equity / debt mix and retirement timeline.</p></div>
          <div><label className="label">Income-tax slab (optional)</label>
            <select className="input" value={f.slab} onChange={(e) => setF({ ...f, slab: e.target.value })}>
              <option value="">Not sure — assume 30%</option>{SLABS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select><p className="mt-1 text-xs text-muted">For dividends, debt funds and the Tax tab.</p></div>
          <div><label className="label">Risk profile</label>
            <select className="input" value={f.risk} onChange={(e) => setF({ ...f, risk: e.target.value })}>
              <option value="conservative">Conservative — protect capital</option><option value="moderate">Moderate — balanced</option>
              <option value="aggressive">Aggressive — maximise growth</option>
            </select></div>
        </div>
        <ErrorNote error={err} />
        <div className="mt-4 flex items-center gap-2">
          <button className="btn-primary" disabled={!ready || busy} onClick={save}>{busy ? "Saving…" : "Save and continue"}</button>
          {!self && <span className="text-xs text-muted">Setting up your profile…</span>}
          <button className="btn-ghost ml-auto text-sm" onClick={() => router.replace("/dashboard")}>Later</button>
        </div>
      </Section>
    </div>
  );
}
