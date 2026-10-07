"use client";

import { useGroups, useProfiles, type Scope } from "@/lib/queries";

export default function ScopeSwitcher({ value, onChange }: { value: Scope; onChange: (s: Scope) => void }) {
  const { data: profiles } = useProfiles();
  const { data: groups } = useGroups();
  const v = value.type === "household" ? "household" : `${value.type}:${value.id}`;
  return (
    <select aria-label="View" className="input w-auto" value={v}
            onChange={(e) => { const [t, id] = e.target.value.split(":"); onChange(t === "household" ? { type: "household" } : { type: t as Scope["type"], id }); }}>
      <option value="household">👪 Whole family</option>
      {profiles?.map((p) => <option key={p.id} value={`profile:${p.id}`}>👤 {p.display_name}</option>)}
      {groups?.map((g) => <option key={g.id} value={`group:${g.id}`}>🗂 {g.name}</option>)}
    </select>
  );
}
