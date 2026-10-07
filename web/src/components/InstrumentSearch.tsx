"use client";

import { useEffect, useState } from "react";

import { assetLabel } from "@/lib/format";
import { useSearch } from "@/lib/queries";

export type Picked = { id: string | null; symbol: string; name: string; asset_type: string };

export default function InstrumentSearch({ onPick, placeholder = "Search stock, ETF, mutual fund…" }: { onPick: (p: Picked) => void; placeholder?: string }) {
  const [q, setQ] = useState("");
  const [debounced, setDebounced] = useState("");
  const [open, setOpen] = useState(false);
  useEffect(() => { const t = setTimeout(() => setDebounced(q.trim()), 200); return () => clearTimeout(t); }, [q]);
  const { data, isFetching } = useSearch(debounced);
  return (
    <div className="relative">
      <input className="input" value={q} placeholder={placeholder} onChange={(e) => { setQ(e.target.value); setOpen(true); }} onFocus={() => setOpen(true)}
             role="combobox" aria-expanded={open} aria-controls="instrument-results" />
      {open && debounced.length >= 2 && (
        <ul id="instrument-results" role="listbox" className="absolute z-20 mt-1 max-h-72 w-full overflow-auto rounded-lg border border-line bg-raised shadow-lg">
          {isFetching && !data && <li className="px-3 py-2 text-sm text-muted">Searching…</li>}
          {data?.length === 0 && <li className="px-3 py-2 text-sm text-muted">No matches</li>}
          {data?.map((r) => (
            <li key={`${r.symbol}-${r.asset_type}`} role="option" aria-selected={false}>
              <button type="button" className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm hover:bg-page"
                      onClick={() => { onPick({ id: r.id, symbol: r.symbol, name: r.name, asset_type: r.asset_type }); setQ(r.name); setOpen(false); }}>
                <span className="truncate">{r.name}</span>
                <span className="shrink-0 text-xs text-muted">{r.symbol} · {assetLabel[r.asset_type] ?? r.asset_type}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
