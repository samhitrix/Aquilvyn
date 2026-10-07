"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { ErrorNote, Section } from "@/components/ui";
import { api } from "@/lib/api";
import { inr, num, titleCase } from "@/lib/format";
import { invalidatePortfolio, qk, usePortfolios, useProfiles } from "@/lib/queries";
import { SortTh, useSort } from "@/lib/useSort";

type Group = {
  key: string; type: "pan" | "account" | "file"; pan_masked?: string | null; investor?: string | null; account_label?: string;
  matched_profile: { id: string; name: string } | null; match_reason?: string | null; hint_conflict?: string | null; suggest_reason?: string | null;
  epf?: { name?: string | null; establishment?: string | null; fy?: string | null; pension?: number; closing?: { on: string; employee: number; employer: number; pension: number } | null };
  suggested: { profile_id?: string; create?: string }; summary: Record<string, any>;
};
type Preview = { token: string; kind: string; kind_label: string; filename: string; rows: number; warnings: any[]; errors: any[]; error_count: number; groups: Group[];
  statement?: { broker: string | null; period: string[] | null; checks: { label: string; reported: number; parsed: number; ok: boolean }[]; charges: number };
  depository?: DepositoryReport };
type Choice = { target: string; createName: string; portfolioId: string; totalValue?: string }; // target: profile id | "create" | "skip"

const STATUS: Record<string, [string, string]> = {
  new: ["New", "bg-up/15 text-up"], update: ["Quantity changes", "bg-warn/15 text-warn"], same: ["Unchanged", "bg-page text-ink2"],
  remove: ["Will be removed", "bg-down/15 text-down"], unmatched: ["Can't identify — skipped", "bg-down/15 text-down"],
  kept: ["Kept (not in file)", "bg-page text-muted"],
  in_cas: ["Already in your CAS — kept from it", "bg-brand/10 text-brand"],
};

/** Exactly what the import will change for this person — computed by the server before anything is written. */
function ChangePlan({ token, groupKey, profileId, mode, portfolioId }: { token: string; groupKey: string; profileId: string; mode: string; portfolioId: string }) {
  const q = useQuery({
    queryKey: ["import-plan", token, groupKey, profileId, mode, portfolioId],
    queryFn: () => api<any>("/imports/plan", { body: { token, key: groupKey, profile_id: profileId, mode, portfolio_id: portfolioId || undefined } }),
    staleTime: 60_000, retry: false,
  });
  if (q.isLoading) return <p className="mt-2 text-xs text-muted">Checking what will change…</p>;
  if (q.error) return <p className="mt-2 text-xs text-down">{(q.error as Error).message}</p>;
  const pl = q.data;
  if (pl.kind === "tax") {
    const c = pl.counts;
    return (
      <p className="mt-2 rounded-lg bg-page px-3 py-2 text-xs">
        Into <b>{pl.profile}</b>&apos;s Tax records{pl.period ? <> for <b>{pl.period[0]} → {pl.period[1]}</b></> : null}: <b className="text-up">{c.sales} sale(s)</b>,{" "}
        <b className="text-up">{c.dividends} dividend(s)</b>{c.interest ? <>, {c.interest} interest</> : null} · realised {inr(pl.realised)} · income {inr(pl.income)}.
        {c.replaced_sales + c.replaced_income > 0
          ? <> Replaces <b>{c.replaced_sales + c.replaced_income}</b> row(s) imported earlier for the same account and dates (kept for undo — never doubled).</>
          : " Holdings are not changed."}
      </p>
    );
  }
  if (pl.kind === "txn") {
    return (
      <p className="mt-2 rounded-lg bg-page px-3 py-2 text-xs">
        Into <b>{pl.profile} · {pl.portfolio}</b>: <b className="text-up">{pl.counts.new} new</b> transaction(s)
        {pl.counts.already_imported ? <>, <b>{pl.counts.already_imported}</b> already imported earlier (skipped, not doubled)</> : null}.
      </p>
    );
  }
  const order = ["update", "new", "remove", "unmatched", "in_cas", "same", "kept"];
  const rows = [...pl.rows].sort((a: any, b: any) => order.indexOf(a.status) - order.indexOf(b.status));
  return (
    <div className="mt-2 rounded-lg border border-line">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line px-3 py-2 text-xs">
        <b>What will change for {pl.profile}</b>
        <span className="flex flex-wrap gap-1">{order.filter((k) => pl.counts[k]).map((k) => <span key={k} className={`chip ${STATUS[k][1]}`}>{pl.counts[k]} {STATUS[k][0].toLowerCase()}</span>)}</span>
      </div>
      <div className="max-h-72 overflow-auto">
        <ChangeTable rows={rows} order={order} />
      </div>
      <p className="border-t border-line px-3 py-2 text-xs text-ink2">Invested in {pl.portfolios.join(" + ")}: {inr(pl.invested_now)} now → <b className="text-ink">{inr(pl.invested_after)}</b> after import</p>
    </div>
  );
}

function ChangeTable({ rows: input, order }: { rows: any[]; order: string[] }) {
  const [rows, sort] = useSort<any>(input, {
    name: (r) => r.name, now: (r) => r.now, file: (r) => r.file, after: (r) => r.after, change: (r) => -order.indexOf(r.status),
  }, "change_sort");
  return (
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-surface"><tr><SortTh s={sort} k="name">Holding</SortTh><SortTh s={sort} k="now" className="th text-right">Now</SortTh>
            <SortTh s={sort} k="file" className="th text-right">In file</SortTh><SortTh s={sort} k="after" className="th text-right">After</SortTh>
            <SortTh s={sort} k="change">Change</SortTh></tr></thead>
          <tbody>{rows.map((r: any, i: number) => (
            <tr key={i} className="border-t border-line">
              <td className="td py-1.5">{r.name}<span className="block text-[0.7rem] text-muted">{r.symbol}</span></td>
              <td className="td tnum py-1.5 text-right">{r.now == null ? "—" : num(r.now, 3)}</td>
              <td className="td tnum py-1.5 text-right">{num(r.file, 3)}</td>
              <td className="td tnum py-1.5 text-right font-medium">{r.after == null ? "—" : num(r.after, 3)}</td>
              <td className="td py-1.5"><span className={`chip ${(STATUS[r.status] ?? ["", "bg-page"])[1]}`}>{(STATUS[r.status] ?? [r.status])[0]}</span></td>
            </tr>
          ))}</tbody>
        </table>
  );
}

export default function ImportPage() {
  const qc = useQueryClient();
  const { data: portfolios } = usePortfolios();
  const { data: profiles } = useProfiles();
  const [file, setFile] = useState<File | null>(null);
  const [password, setPassword] = useState("");
  const [locked, setLocked] = useState(false);  // the chosen PDF is password-protected (or the server said so)
  const [mode, setMode] = useState<"sync" | "replace">("sync");
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [forProfile, setForProfile] = useState("");
  const submitting = useRef(false);
  const jobs = useQuery({ queryKey: qk.imports, queryFn: () => api<any[]>("/imports") });
  const preview = useMutation({
    mutationFn: (mapping?: ColumnMapping) => {
      const form = new FormData();
      form.append("file", file!);
      if (password) form.append("password", password);
      if (forProfile) form.append("profile_id", forProfile);
      if (mapping) form.append("mapping", JSON.stringify(mapping));
      return api<Preview>("/imports/preview", { form });
    },
    onError: (e) => { if ((e as any)?.detail?.detail?.needs_password) setLocked(true); },
    onSuccess: (pv) => {
      setChoices(Object.fromEntries(pv.groups.map((g) => [g.key, {
        target: g.suggested.profile_id ?? ("create" in g.suggested ? "create" : ""), createName: g.suggested.create ?? "", portfolioId: "",
      }])));
      commit.reset();
    },
  });
  const commit = useMutation({
    mutationFn: (pv: Preview) => api<any>("/imports/commit", {
      body: {
        token: pv.token, mode: pv.kind === "nps_statement" ? "sync" : mode,
        choices: Object.fromEntries(Object.entries(choices).map(([k, c]) => [k,
          c.target === "skip" ? { skip: true } : {
            ...(c.target === "create" ? { create: c.createName } : { profile_id: c.target, ...(c.portfolioId ? { portfolio_id: c.portfolioId } : {}) }),
            ...(c.totalValue?.trim() ? { total_value: c.totalValue.replace(/[,₹\s]/g, "") } : {}),
          }])),
      },
    }),
    onSuccess: () => { [qk.imports, qk.profiles, qk.portfolios].forEach((k) => qc.invalidateQueries({ queryKey: k })); invalidatePortfolio(qc); },
    onSettled: () => { submitting.current = false; },
  });
  const doCommit = (p: Preview) => {  // a double click must never write the same import twice
    if (submitting.current) return;
    submitting.current = true;
    commit.mutate(p);
  };
  const remove = useMutation({
    mutationFn: (id: string) => api<{ deleted: number }>(`/imports/${id}`, { method: "DELETE" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.imports }); invalidatePortfolio(qc); },
  });
  const restore = useMutation({
    mutationFn: (id: string) => api<{ restored: number; superseded: number }>(`/imports/${id}/restore`, { method: "POST" }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: qk.imports }); invalidatePortfolio(qc); },
  });
  const pfLabel = (id: string) => {
    const p = portfolios?.find((x) => x.id === id);
    return p ? `${profiles?.find((q) => q.id === p.profile_id)?.display_name ?? ""} · ${p.name}` : "—";
  };
  const [jobRows, jobSort] = useSort<any>(jobs.data ?? [], {
    when: (j) => j.created_at, file: (j) => j.filename, into: (j) => pfLabel(j.portfolio_id), type: (j) => j.kind, status: (j) => j.status,
    result: (j) => j.stats?.created ?? j.stats?.deleted ?? 0,
  }, "imports_sort");
  const isPdf = file?.name.toLowerCase().endsWith(".pdf");
  const pv = preview.data;
  const reset = () => { preview.reset(); commit.reset(); setFile(null); setPassword(""); setLocked(false); };
  const ready = pv && pv.groups.every((g) => { const c = choices[g.key]; return c && (c.target === "skip" || (c.target === "create" ? c.createName.trim() : c.target)); })
    && pv.groups.some((g) => choices[g.key]?.target !== "skip");
  return (
    <div className="space-y-4">
      <h1 className="text-xl font-semibold">Import</h1>
      <Section title={commit.data ? "3 · Done" : pv?.depository ? "Holdings check · NSDL / CDSL statement" : pv ? "2 · Review before importing" : "1 · Upload a statement"}>
        {!pv && (
          <>
            <div className="mb-4 grid gap-3 lg:grid-cols-2">
              <div className="rounded-lg border border-up/40 bg-up/5 p-3 text-sm">
                <b className="text-up">Recommended: a holdings statement</b> — what you own today, stocks and funds together.
                <ul className="mt-1 list-disc pl-5 text-ink2">
                  <li><b>Zerodha</b> Console → Portfolio → Holdings → Download (<b>.xlsx</b>: Equity + Mutual Funds)</li>
                  <li><b>Upstox, ICICI Direct, Kotak, Paytm Money, SBI Securities, Groww, Angel One, 5paisa …</b> — their holdings download (Excel / CSV)</li>
                  <li>A layout Aquilvyn doesn&apos;t know yet? You map its columns once; it&apos;s remembered.</li>
                </ul>
              </div>
              <div className="rounded-lg border border-line p-3 text-sm">
                <b>Transaction history</b> — for exact tax lots and XIRR (needs your <i>complete</i> history).
                <ul className="mt-1 list-disc pl-5 text-ink2">
                  <li><b>CAMS / KFintech CAS</b> PDF (<b>Detailed</b>, since inception) — password = PAN in capitals (the password box appears when a PDF needs one). Every PAN in it goes to the right person.</li>
                  <li><b>Any broker&apos;s trade book</b> (Excel / CSV) — Zerodha Tradebook, Upstox Trade report, ICICI Direct “All Transaction”, SBI / Kotak trade book …</li>
                  <li><b>Tax P&amp;L / capital gains</b> report from any broker — goes to the Tax tab</li>
                  <li><b>EPFO passbook</b> PDF (passbook.epfindia.gov.in → Download Passbook, one per year) — contributions, interest and withdrawals; no password needed</li>
                  <li><b>NSDL / CDSL eCAS</b> PDF (monthly, from the depository) — checks your share holdings across <i>every</i> broker against Aquilvyn</li>
                </ul>
              </div>
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              <div><label className="label">Import for</label>
                <select className="input" value={forProfile} onChange={(e) => setForProfile(e.target.value)}>
                  <option value="">Detect from the file (PAN / account)</option>
                  {profiles?.map((p) => <option key={p.id} value={p.id}>{p.display_name}{p.pan_masked ? ` · PAN ${p.pan_masked}` : ""}</option>)}
                </select></div>
              <div className="md:col-span-2"><label className="label">File (.xlsx, .xls, .csv or .pdf · max 15 MB)</label>
                <input className="input" type="file" accept=".xlsx,.xls,.csv,.pdf,.json,text/csv,application/pdf,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel"
                       onChange={(e) => { const f = e.target.files?.[0] ?? null; setFile(f); setLocked(false); setPassword(""); if (f) pdfIsLocked(f).then(setLocked); }} /></div>
              {isPdf && locked && <div><label className="label">PDF password</label><input className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="PAN in capitals" /></div>}
            </div>
            <p className="mt-2 text-xs text-muted">Nothing is saved yet — the next step shows whose statement it is and exactly what will change.</p>
            <RememberedLayouts />
            <button className="btn-primary mt-3" disabled={!file || preview.isPending} onClick={() => preview.mutate(undefined)}>{preview.isPending ? "Reading file…" : "Read file →"}</button>
            {needsMapping(preview.error)
              ? <ColumnMapper key={needsMapping(preview.error)!.fingerprint} cand={needsMapping(preview.error)!} message={(preview.error as Error).message}
                  busy={preview.isPending} fileName={file?.name ?? ""} onSubmit={(m) => preview.mutate(m)} />
              : <ErrorNote error={preview.error} />}
          </>
        )}

        {pv?.depository && <DepositoryCheck report={pv.depository} onDone={reset} />}
        {pv && !pv.depository && !commit.data && (
          <div className="space-y-3">
            <p className="text-sm text-ink2"><b className="text-ink">{pv.kind_label}</b> · {pv.filename} · {pv.rows} rows
              {pv.kind === "nps_statement" && <span className="chip ml-2 bg-up/15 text-up">✔ Detected as NPS automatically</span>}</p>
            {pv.statement && (
              <div className="rounded-lg border border-line p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <b>{pv.statement.broker ?? "Broker"} tax statement</b>
                  {pv.statement.period && <span className="text-ink2">{pv.statement.period[0]} → {pv.statement.period[1]}</span>}
                  {pv.statement.checks.length > 0 && (pv.statement.checks.every((c) => c.ok)
                    ? <span className="chip bg-up/15 text-up">✔ All {pv.statement.checks.length} totals reconcile with the statement</span>
                    : <span className="chip bg-warn/15 text-warn">! {pv.statement.checks.filter((c) => !c.ok).length} total(s) don&apos;t match — see below</span>)}
                </div>
                <details className="mt-1 text-xs"><summary className="cursor-pointer text-brand">Self-checks</summary>
                  <ul className="mt-1 space-y-0.5">{pv.statement.checks.map((c, i) => (
                    <li key={i} className={c.ok ? "text-ink2" : "text-warn"}>{c.ok ? "✔" : "!"} {c.label}: statement {inr(c.reported)} · rows {inr(c.parsed)}</li>
                  ))}</ul>
                </details>
              </div>
            )}
            {pv.warnings.length > 0 && <ul className="space-y-1 rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm text-warn">{pv.warnings.map((w, i) => <li key={i}>{w.error}</li>)}</ul>}
            {pv.error_count > 0 && (
              <details className="rounded-lg border border-down/40 bg-down/10 p-3 text-sm text-down">
                <summary>{pv.error_count} row(s) can&apos;t be read and will be skipped</summary>
                <ul className="mt-2 list-disc pl-5">{pv.errors.map((e, i) => <li key={i}><b>{String(e.row)}:</b> {e.error}</li>)}</ul>
              </details>
            )}
            <div className="grid gap-3 xl:grid-cols-2">
              {pv.groups.map((g) => {
                const c = choices[g.key] ?? { target: "", createName: "", portfolioId: "" };
                const set = (patch: Partial<Choice>) => setChoices({ ...choices, [g.key]: { ...c, ...patch } });
                const ownPfs = portfolios?.filter((p) => p.profile_id === c.target) ?? [];
                return (
                  <div key={g.key} className="rounded-lg border border-line p-3 text-sm">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <b>{g.investor ? titleCase(g.investor) : g.pan_masked ? `Folios with PAN ${g.pan_masked}` : g.account_label ?? "This file"}</b>
                      <span className="flex gap-1">
                        {g.pan_masked && g.investor && <span className="chip bg-page">PAN {g.pan_masked}</span>}
                        {g.account_label && g.investor && <span className="chip bg-page">{g.account_label}</span>}
                        {g.hint_conflict && <span className="chip bg-warn/15 text-warn">This statement belongs to {g.hint_conflict}</span>}
                        {g.matched_profile ? <span className="chip bg-up/15 text-up">✔ {g.match_reason}</span>
                          : g.suggest_reason ? <span className="chip bg-up/15 text-up">Suggested: {g.suggest_reason}</span>
                          : g.type !== "file" ? <span className="chip bg-warn/15 text-warn">No profile has this {g.type === "pan" ? "PAN" : "account"} yet</span> : null}
                      </span>
                    </div>
                    <p className="mt-1 text-ink2">
                      {pv.kind === "tax_pnl" ? `${g.summary.sales} sale(s) ${g.summary.from ? `(${g.summary.from} → ${g.summary.to})` : ""} · realised ${inr(g.summary.realised)} · dividends ${inr(g.summary.dividends)}${g.summary.zero_cost ? ` · ${g.summary.zero_cost} with missing cost` : ""}`
                        : pv.kind === "nps_statement" ? `${g.summary.rows} NPS scheme(s) · invested ${inr(g.summary.invested)} · goes to their Retirement portfolio, priced at the statement NAV`
                        : pv.kind === "holdings" ? `${g.summary.stocks} stocks/ETFs · ${g.summary.funds} funds · invested ${inr(g.summary.invested)}`
                        : `${g.summary.rows} transactions · ${g.summary.instruments} instruments · ${g.summary.from ?? "?"} → ${g.summary.to ?? "?"}${g.summary.estimated ? ` · ${g.summary.estimated} estimated opening balance(s)` : ""}`}
                    </p>
                    {g.epf && (
                      <div className="mt-2 rounded-md border border-line bg-page px-3 py-2 text-xs text-ink2">
                        <p>EPF passbook{g.epf.fy ? ` · FY ${g.epf.fy}` : ""}{g.epf.establishment ? ` · ${g.epf.establishment}` : ""}
                          {g.epf.closing ? ` · closing balance ${inr(g.epf.closing.employee + g.epf.closing.employer)} (employee + employer)` : ""}.
                          {g.epf.pension ? ` Pension (EPS) ${inr(g.epf.pension)} isn't counted — it's paid as a monthly pension, not withdrawable.` : ""}</p>
                        <label className="label mt-2">Total EPF balance, all employers (optional)</label>
                        <input className="input max-w-xs" inputMode="decimal" placeholder="e.g. 18,40,000" value={c.totalValue ?? ""} onChange={(e) => set({ totalValue: e.target.value })} />
                        <p className="mt-1">This passbook covers one employer only (Member ID {g.account_label?.replace(/^EPF Member ID /, "")}). If you have balances from earlier
                          employers that EPFO hasn&apos;t merged into it, enter your total — the difference is added as one adjustment. Leave it empty to use the passbook alone.</p>
                      </div>
                    )}
                    {g.type === "account" && !g.matched_profile && !g.epf && (
                      <p className="mt-1 text-xs text-muted">This file names only the broker account ({g.account_label}), not a PAN. Pick the person once — the account is
                        remembered, and every later file from it goes to them automatically.</p>
                    )}
                    <div className="mt-2 grid gap-2 sm:grid-cols-2">
                      <div><label className="label">Import into</label>
                        <select className="input" value={c.target} onChange={(e) => set({ target: e.target.value, portfolioId: "" })}>
                          <option value="">Choose…</option>
                          {profiles?.map((p) => <option key={p.id} value={p.id}>{p.display_name}{p.pan_masked ? ` · PAN ${p.pan_masked}` : ""}</option>)}
                          <option value="create">➕ Create a new profile…</option>
                          <option value="skip">Skip (don&apos;t import)</option>
                        </select></div>
                      {c.target === "create" ? (
                        <div><label className="label">New profile name</label><input className="input" autoFocus={!c.createName} placeholder="Whose PAN is this?" value={c.createName} onChange={(e) => set({ createName: e.target.value })} /></div>
                      ) : !["holdings", "nps_statement", "tax_pnl", "epf_passbook"].includes(pv.kind) && c.target && c.target !== "skip" ? (
                        <div><label className="label">Portfolio</label>
                          <select className="input" value={c.portfolioId} onChange={(e) => set({ portfolioId: e.target.value })}>
                            <option value="">Automatic ({pv.kind.startsWith("cas") ? "Mutual Funds" : "Stocks & ETFs"})</option>
                            {ownPfs.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                          </select></div>
                      ) : null}
                    </div>
                    {g.pan_masked && c.target && !["create", "skip"].includes(c.target) && !profiles?.find((p) => p.id === c.target)?.pan_masked && (
                      <p className="mt-2 text-xs text-ink2">This profile has no PAN yet — it will be tagged with {g.pan_masked} so future statements match automatically.</p>
                    )}
                    {c.target === "create" && <p className="mt-2 text-xs text-ink2">A new profile is created; everything in this group is added to it.</p>}
                    {c.target && !["create", "skip"].includes(c.target) && (
                      <ChangePlan token={pv.token} groupKey={g.key} profileId={c.target} mode={pv.kind === "nps_statement" ? "sync" : mode} portfolioId={c.portfolioId} />
                    )}
                  </div>
                );
              })}
            </div>
            {pv.kind === "holdings" && (
              <fieldset className="space-y-1 text-sm">
                <legend className="label">How to apply this holdings statement (neither option can double a holding; both can be undone with Delete import)</legend>
                <label className="flex items-start gap-2"><input className="mt-1" type="radio" checked={mode === "sync"} onChange={() => setMode("sync")} />
                  <span><b>Sync with this statement</b> (recommended) — each holding in the file is set to the file&apos;s quantity; holdings from an earlier statement of this account that are no longer listed are removed. CAS history, manual entries and other brokers are kept.</span></label>
                <label className="flex items-start gap-2"><input className="mt-1" type="radio" checked={mode === "replace"} onChange={() => setMode("replace")} />
                  <span><b>Replace everything</b> — this person&apos;s stock / fund holdings become exactly this file, across all their portfolios of that kind, <b className="text-down">including CAS history and manual entries</b>.</span></label>
              </fieldset>
            )}
            <ErrorNote error={commit.error} />
            <div className="flex gap-2">
              <button className="btn-primary" disabled={!ready || commit.isPending} onClick={() => doCommit(pv)}>{commit.isPending ? "Importing…" : "Import"}</button>
              <button className="btn-ghost" onClick={reset}>Cancel</button>
            </div>
          </div>
        )}

        {commit.data && (
          <div className="space-y-2">
            {commit.data.jobs.map((j: any) => (
              <div key={j.id} className="rounded-lg bg-page p-3 text-sm">
                <b>{j.status === "done" ? "✔" : "✖"} {j.profile.name}</b>{j.stats.portfolios?.length ? ` → ${j.stats.portfolios.join(" + ")}` : ""} — {j.stats.created} {j.kind === "holdings" ? "positions" : "new transactions"}, {j.stats.duplicates_skipped} duplicates skipped, {j.stats.errors} errors{j.stats.warnings ? `, ${j.stats.warnings} notes` : ""}.
                {j.errors?.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5">{j.errors.slice(0, 15).map((e: any, i: number) => <li key={i} className={e.level === "warning" ? "text-ink2" : "text-down"}><b>{e.level === "warning" ? "Note" : String(e.row)}:</b> {e.error}</li>)}</ul>}
              </div>
            ))}
            <button className="btn-ghost" onClick={reset}>Import another file</button>
          </div>
        )}
      </Section>
      <Section title="Previous imports">
        <p className="mb-3 text-sm text-ink2">Imported the wrong file, or into the wrong portfolio? <b>Delete import</b> removes every transaction that file added, in one go — then you can import it again. To wipe a whole portfolio, use <b>Clear data</b> on the Family page.</p>
        <ErrorNote error={remove.error ?? restore.error} />
        {restore.data && <p className="mb-2 rounded-lg bg-up/10 px-3 py-2 text-sm text-up">Restored {restore.data.restored} rows{restore.data.superseded ? ` (${restore.data.superseded} newer rows for the same holdings set aside)` : ""}.</p>}
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead><tr><SortTh s={jobSort} k="when">When</SortTh><SortTh s={jobSort} k="file">File</SortTh><SortTh s={jobSort} k="into">Into</SortTh>
              <SortTh s={jobSort} k="type">Type</SortTh><SortTh s={jobSort} k="status">Status</SortTh><SortTh s={jobSort} k="result">Result</SortTh><th className="th" /></tr></thead>
            <tbody>{jobRows.map((j) => (
              <tr key={j.id} className="border-t border-line">
                <td className="td whitespace-nowrap">{new Date(j.created_at).toLocaleString("en-IN")}</td>
                <td className="td">{j.filename}</td>
                <td className="td text-ink2">{pfLabel(j.portfolio_id)}</td>
                <td className="td">{j.kind}</td>
                <td className="td"><span className={`chip ${j.status === "done" ? "bg-up/15 text-up" : j.status === "replaced" ? "bg-warn/15 text-warn" : j.status === "deleted" ? "bg-page text-muted" : "bg-down/15 text-down"}`}>{j.status}</span></td>
                <td className="td text-ink2">{j.status === "deleted" ? `${j.stats.deleted ?? 0} removed` : `${j.stats.created ?? 0} new · ${j.stats.duplicates_skipped ?? 0} dup · ${j.stats.errors ?? 0} err`}</td>
                <td className="td text-right">
                  {["replaced", "deleted"].includes(j.status) && (j.stats.created ?? 0) > 0 && (
                    <button className="btn-ghost text-brand" disabled={restore.isPending}
                            onClick={() => { if (confirm(`Bring back the ${j.stats.created} rows imported from ${j.filename}?\n\nFor the holdings in this file, the restored rows become the current ones (anything imported later for those holdings is set aside, not deleted).`)) restore.mutate(j.id); }}>
                      {restore.isPending && restore.variables === j.id ? "Restoring…" : "Restore"}
                    </button>
                  )}
                  {!["deleted", "replaced"].includes(j.status) && (j.stats.created ?? 0) > 0 && (
                    <button className="btn-ghost text-down" disabled={remove.isPending}
                            onClick={() => { if (confirm(`Delete all ${j.stats.created} transactions imported from ${j.filename}?`)) remove.mutate(j.id); }}>
                      {remove.isPending && remove.variables === j.id ? "Deleting…" : "Delete import"}
                    </button>
                  )}
                </td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

type MappingField = { key: string; label: string; required: boolean };
type MappingCandidate = {
  sheet: string; row: number; headers: string[]; samples: string[][]; fingerprint: string; kind: "trades" | "holdings";
  suggested: Record<string, Record<string, string>>; fields: Record<string, MappingField[]>; one_of: Record<string, string[][]>;
};
type ColumnMapping = { kind: string; fields: Record<string, string>; label: string };

function needsMapping(err: unknown): MappingCandidate | null {
  const d = (err as { detail?: { detail?: { needs_mapping?: MappingCandidate } } } | null)?.detail?.detail;
  return d?.needs_mapping ?? null;
}

/** "Map the columns once": which column is the date, buy/sell, quantity … — remembered for this layout. */
function ColumnMapper({ cand, message, busy, fileName, onSubmit }: {
  cand: MappingCandidate; message: string; busy: boolean; fileName: string; onSubmit: (m: ColumnMapping) => void;
}) {
  const [kind, setKind] = useState<"trades" | "holdings">(cand.kind);
  const [fields, setFields] = useState<Record<string, string>>(cand.suggested[cand.kind] ?? {});
  const [label, setLabel] = useState(fileName.replace(/\.[^.]+$/, ""));
  const switchKind = (k: "trades" | "holdings") => { setKind(k); setFields(cand.suggested[k] ?? {}); };
  const missing = [
    ...cand.fields[kind].filter((f) => f.required && !fields[f.key]).map((f) => f.label),
    ...cand.one_of[kind].filter((g) => !g.some((k) => fields[k])).map((g) => g.map((k) => cand.fields[kind].find((f) => f.key === k)?.label).join(" or ")),
  ];
  return (
    <div className="mt-4 space-y-3 rounded-xl border-2 border-brand/40 p-4">
      <p className="text-sm"><b className="text-brand">New file layout.</b> {message} Aquilvyn remembers your answer, so the next file like this imports straight away.</p>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <span className="font-medium">This file is</span>
        {(["trades", "holdings"] as const).map((k) => (
          <label key={k} className="inline-flex items-center gap-1.5"><input type="radio" checked={kind === k} onChange={() => switchKind(k)} />
            {k === "trades" ? "a trade history (buys & sells)" : "a holdings statement (what I own now)"}</label>
        ))}
      </div>
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full text-xs">
          <thead><tr>{cand.headers.map((h, i) => <th key={i} className="th whitespace-nowrap">{h || <span className="text-muted">(no name)</span>}</th>)}</tr></thead>
          <tbody>{cand.samples.map((r, i) => <tr key={i} className="border-t border-line">{cand.headers.map((_, j) => <td key={j} className="td whitespace-nowrap text-ink2">{r[j] ?? ""}</td>)}</tr>)}</tbody>
        </table>
      </div>
      <p className="text-xs text-muted">Sheet “{cand.sheet}”, header on row {cand.row}. Only the column names are saved — never your numbers.</p>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {cand.fields[kind].map((f) => (
          <div key={f.key}>
            <label className="label">{f.label}{f.required ? " *" : ""}</label>
            <select className="input" value={fields[f.key] ?? ""} onChange={(e) => setFields({ ...fields, [f.key]: e.target.value })}>
              <option value="">— not in this file —</option>
              {cand.headers.filter(Boolean).map((h) => <option key={h} value={h}>{h}</option>)}
            </select>
          </div>
        ))}
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <div><label className="label">Name this layout</label><input className="input w-64" value={label} onChange={(e) => setLabel(e.target.value)} placeholder="e.g. Upstox trades" /></div>
        <button className="btn-primary" disabled={busy || missing.length > 0} onClick={() => onSubmit({ kind, fields, label })}>{busy ? "Reading with this mapping…" : "Use this mapping →"}</button>
        {missing.length > 0 && <span className="text-xs text-warn">Still needed: {missing.join(" · ")}</span>}
      </div>
    </div>
  );
}

/** Layouts whose columns you mapped once — shown so a wrong mapping can be forgotten (and mapped again). */
function RememberedLayouts() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["import-templates"], queryFn: () => api<{ id: string; label: string; kind: string; uses: number }[]>("/imports/templates") });
  const forget = useMutation({
    mutationFn: (id: string) => api(`/imports/templates/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["import-templates"] }),
  });
  if (!q.data?.length) return null;
  return (
    <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink2">
      <span>Remembered file layouts:</span>
      {q.data.map((t) => (
        <span key={t.id} className="chip bg-page">{t.label} · {t.kind === "trades" ? "trades" : "holdings"} · used {t.uses}×
          <button className="ml-1 text-down" title="Forget this mapping (you'll be asked again next time)" onClick={() => forget.mutate(t.id)}>×</button></span>
      ))}
    </p>
  );
}

type DepoLine = { isin: string; name: string; depository_qty?: number; our_qty: number; value?: number; status: "ok" | "differs" | "missing" | "extra";
  profile?: { id: string; name: string } | null };
type DepositoryReport = {
  accounts: { broker: string; type: string; client: string; holder: string | null; pan_masked: string | null; profile: { id: string; name: string } | null;
    value: number; lines: DepoLine[] }[];
  extra: DepoLine[]; counts: Record<string, number>; unmatched_pans: string[]; period: { from: string | null; to: string | null };
};
const DEPO: Record<string, [string, string]> = {
  ok: ["✔ matches", "bg-up/15 text-up"], differs: ["≠ quantity differs", "bg-warn/15 text-warn"],
  missing: ["✖ not in Aquilvyn", "bg-down/15 text-down"], extra: ["? not at the depository", "bg-warn/15 text-warn"],
};

/** NSDL/CDSL eCAS: every demat account (any broker) compared with Aquilvyn — nothing is imported (no costs in it). */
function DepoTable({ lines }: { lines: DepositoryReport["accounts"][number]["lines"] }) {
  const [rows, sort] = useSort(lines, { name: (l) => l.name, depo: (l) => Number(l.depository_qty), ours: (l) => Number(l.our_qty) || null, status: (l) => DEPO[l.status]?.[0] ?? l.status }, "depo_sort");
  return (
    <table className="w-full text-sm">
      <thead><tr><SortTh s={sort} k="name">Share</SortTh><SortTh s={sort} k="depo" className="th text-right">At the depository</SortTh>
        <SortTh s={sort} k="ours" className="th text-right">In Aquilvyn</SortTh><SortTh s={sort} k="status">Result</SortTh></tr></thead>
      <tbody>{rows.map((l) => (
        <tr key={l.isin} className="border-t border-line">
          <td className="td">{l.name}<span className="block text-xs text-muted">{l.isin}</span></td>
          <td className="td tnum text-right">{l.depository_qty}</td><td className="td tnum text-right">{l.our_qty || "—"}</td>
          <td className="td"><span className={`chip ${DEPO[l.status][1]}`}>{DEPO[l.status][0]}</span></td>
        </tr>))}</tbody>
    </table>
  );
}

function DepositoryCheck({ report, onDone }: { report: DepositoryReport; onDone: () => void }) {
  const c = report.counts;
  return (
    <div className="space-y-3">
      <p className="text-sm">
        Your depository statement{report.period.to ? ` (to ${report.period.to})` : ""} lists what is in each demat account, whichever broker it&apos;s with — but not what
        you paid, so nothing is imported from it. It&apos;s compared with Aquilvyn instead:{" "}
        <span className="chip bg-up/15 text-up">{c.ok} match</span> <span className="chip bg-warn/15 text-warn">{c.differs} differ</span>{" "}
        <span className="chip bg-down/15 text-down">{c.missing} missing</span> <span className="chip bg-warn/15 text-warn">{c.extra} not at the depository</span>
      </p>
      {report.unmatched_pans.length > 0 && <p className="rounded-lg bg-warn/10 px-3 py-2 text-sm text-warn">No family member has PAN {report.unmatched_pans.join(", ")} yet — add it to their profile (Family page) to check their accounts.</p>}
      {report.accounts.map((a, i) => (
        <div key={i} className="rounded-lg border border-line">
          <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 text-sm">
            <b>{a.broker}</b><span className="text-ink2">{a.type} · client {a.client}</span>
            <span className="text-ink2">· {a.holder}{a.pan_masked ? ` (PAN ${a.pan_masked})` : ""}</span>
            <span className="ml-auto text-xs text-muted">{a.profile ? `compared with ${a.profile.name}` : "no matching family member"}</span>
          </div>
          <DepoTable lines={a.lines} />
        </div>
      ))}
      {report.extra.length > 0 && (
        <div className="rounded-lg border border-line p-3 text-sm">
          <b>In Aquilvyn but in none of these demat accounts</b> — sold already, or recorded twice?
          <ul className="mt-1 list-disc pl-5 text-ink2">{report.extra.map((e) => <li key={e.isin}>{e.name} — {e.our_qty} ({e.profile?.name ?? ""})</li>)}</ul>
        </div>
      )}
      <p className="text-xs text-muted">To fix a “missing” or “differs” line, import that broker&apos;s holdings statement or trade book. Mutual funds are checked through your CAMS / KFintech CAS.</p>
      <button className="btn-ghost" onClick={onDone}>Check another file</button>
    </div>
  );
}

/** A PDF is password-protected when its trailer has an /Encrypt entry — checked locally, nothing is uploaded. */
async function pdfIsLocked(f: File): Promise<boolean> {
  if (!f.name.toLowerCase().endsWith(".pdf")) return false;
  try {
    const buf = new Uint8Array(await f.slice(Math.max(0, f.size - 4096)).arrayBuffer());
    const tail = new TextDecoder("latin1").decode(buf);
    if (tail.includes("/Encrypt")) return true;
    const head = new TextDecoder("latin1").decode(new Uint8Array(await f.slice(0, 4096).arrayBuffer()));
    return head.includes("/Encrypt");
  } catch {
    return false;
  }
}
