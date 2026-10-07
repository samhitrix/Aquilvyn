"use client";

import { useMemo } from "react";

import { useUrlState } from "./useUrlState";

type Val = string | number | boolean | null | undefined;
export type SortKeys<T> = Record<string, (row: T) => Val>;
export type Sort = { key: string; dir: "asc" | "desc"; toggle: (key: string) => void; numeric: (key: string) => boolean };

/** Click-to-sort for a table: every column with a key in ``keys`` sorts on click (numbers start high → low,
 *  text A → Z; a second click flips it, a third goes back to the table's own order). Empty values always go last.
 *  The choice is kept like a filter — in the URL (``?<param>=value:desc``) and remembered per page. */
export function useSort<T>(rows: T[], keys: SortKeys<T>, param = "sort"): [T[], Sort] {
  const [raw, setRaw] = useUrlState<string>(param, "");
  const [key, d] = raw.split(":");
  const dir: "asc" | "desc" = d === "asc" ? "asc" : "desc";
  const get = key ? keys[key] : undefined;
  const numeric = (k: string) => {
    const f = keys[k];
    const v = f && rows.map(f).find((x) => x !== null && x !== undefined && x !== "");
    return typeof v === "number" || typeof v === "boolean";
  };
  const sorted = useMemo(() => {
    if (!get) return rows;
    const blank = (v: Val) => v === null || v === undefined || v === "" || (typeof v === "number" && Number.isNaN(v));
    return rows.map((r, i) => ({ r, i, v: get(r) })).sort((a, b) => {
      if (blank(a.v) || blank(b.v)) return blank(a.v) === blank(b.v) ? a.i - b.i : blank(a.v) ? 1 : -1;
      const c = typeof a.v === "string" || typeof b.v === "string"
        ? String(a.v).localeCompare(String(b.v), "en-IN", { numeric: true, sensitivity: "base" })
        : Number(a.v) - Number(b.v);
      return (dir === "asc" ? c : -c) || a.i - b.i;
    }).map((x) => x.r);
  }, [rows, get, dir]);
  const toggle = (k: string) => {
    const first = numeric(k) ? "desc" : "asc";
    if (k !== key) return setRaw(`${k}:${first}`);
    if (dir === first) return setRaw(`${k}:${first === "desc" ? "asc" : "desc"}`);
    setRaw("");  // third click: back to the table's own order
  };
  return [sorted, { key: get ? key : "", dir, toggle, numeric }];
}

/** A sortable column heading: same look as ``<th className="th">``, with a ▲/▼ for the active column. */
export function SortTh({ s, k, className = "th", title, children }: { s: Sort; k: string; className?: string; title?: string; children: React.ReactNode }) {
  const on = s.key === k;
  const right = className.includes("text-right");
  return (
    <th className={className} title={title} aria-sort={on ? (s.dir === "asc" ? "ascending" : "descending") : "none"}>
      <button type="button" onClick={() => s.toggle(k)}
              className={`inline-flex items-center gap-1 whitespace-nowrap hover:text-brand ${right ? "flex-row-reverse" : ""} ${on ? "text-brand" : ""}`}
              title={`Sort by ${typeof children === "string" ? children : "this column"}`}>
        {children}
        <span aria-hidden className={`text-[0.65rem] ${on ? "" : "opacity-30"}`}>{on ? (s.dir === "asc" ? "▲" : "▼") : "↕"}</span>
      </button>
    </th>
  );
}
