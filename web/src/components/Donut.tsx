"use client";

import { useState } from "react";

import { inr } from "@/lib/format";

export type Slice = { key: string; label: string; value: number; pct: number; color: string };

const CATEGORICAL = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)", "var(--s7)", "var(--s8)"];
export const OTHER_COLOR = "rgb(var(--muted))";

/** Fixed categorical order (never cycled): the first 7 keep their hue, the rest fold into "Other". */
export function withColors(items: { key: string; label: string; value: number; pct: number }[], fixed?: Record<string, string>, max = 8): Slice[] {
  if (fixed) return items.map((s, i) => ({ ...s, color: fixed[s.key] ?? CATEGORICAL[i % 8] }));
  if (items.length <= max) return items.map((s, i) => ({ ...s, color: CATEGORICAL[i] }));
  const head = items.slice(0, max - 1).map((s, i) => ({ ...s, color: CATEGORICAL[i] }));
  const tail = items.slice(max - 1);
  return [...head, { key: "__other", label: `Other (${tail.length})`, value: tail.reduce((a, s) => a + s.value, 0),
    pct: tail.reduce((a, s) => a + s.pct, 0), color: OTHER_COLOR }];
}

function arc(cx: number, cy: number, r0: number, r1: number, a0: number, a1: number): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const p = (r: number, a: number) => `${cx + r * Math.sin(a)} ${cy - r * Math.cos(a)}`;
  if (a1 - a0 >= Math.PI * 2 - 1e-6) {  // full ring: two halves
    return `M ${p(r1, 0)} A ${r1} ${r1} 0 1 1 ${p(r1, Math.PI)} A ${r1} ${r1} 0 1 1 ${p(r1, 0)} M ${p(r0, 0)} A ${r0} ${r0} 0 1 0 ${p(r0, Math.PI)} A ${r0} ${r0} 0 1 0 ${p(r0, 0)} Z`;
  }
  return `M ${p(r1, a0)} A ${r1} ${r1} 0 ${large} 1 ${p(r1, a1)} L ${p(r0, a1)} A ${r0} ${r0} 0 ${large} 0 ${p(r0, a0)} Z`;
}

function Ring({ slices, r0, r1, hover, setHover, ring }: {
  slices: Slice[]; r0: number; r1: number; hover: string | null; setHover: (k: string | null) => void; ring: string;
}) {
  const total = slices.reduce((a, s) => a + s.value, 0) || 1;
  let a = 0;
  return (
    <g>
      {slices.map((s) => {
        const a0 = a;
        a += (s.value / total) * Math.PI * 2;
        const id = `${ring}:${s.key}`;
        const dim = hover && hover !== id;
        return (
          <path key={id} d={arc(100, 100, r0, r1, a0, a)} fill={s.color} stroke="rgb(var(--surface))" strokeWidth={2} strokeLinejoin="round"
                opacity={dim ? 0.35 : 1} onMouseEnter={() => setHover(id)} onMouseLeave={() => setHover(null)} style={{ transition: "opacity 120ms" }}>
            <title>{`${s.label}: ${inr(s.value)} (${s.pct.toFixed(2)}%)`}</title>
          </path>
        );
      })}
    </g>
  );
}

/** Donut (optionally with an inner ring) + scrollable legend with percentages. Hover a slice or a
 *  legend row to see its ₹ value. Legend text uses ink colours; the dot carries identity. */
export default function Donut({ title, slices, inner, innerTitle, empty = "No data" }: {
  title: string; slices: Slice[]; inner?: Slice[]; innerTitle?: string; empty?: string;
}) {
  const [hover, setHover] = useState<string | null>(null);
  const all = [...(inner ?? []).map((s) => ({ ...s, ring: "in" })), ...slices.map((s) => ({ ...s, ring: "out" }))];
  const hovered = all.find((s) => `${s.ring}:${s.key}` === hover);
  if (!slices.length) return <div className="flex h-48 items-center justify-center text-sm text-muted">{empty}</div>;
  return (
    <div className="flex flex-col items-center gap-4 sm:flex-row sm:items-center">
      <div className="relative aspect-square w-full max-w-[200px] shrink-0">
        <svg viewBox="0 0 200 200" className="h-full w-full" role="img" aria-label={`${title} breakdown`}>
          {inner?.length ? <Ring slices={inner} r0={52} r1={70} hover={hover} setHover={setHover} ring="in" /> : null}
          <Ring slices={slices} r0={inner?.length ? 74 : 62} r1={98} hover={hover} setHover={setHover} ring="out" />
        </svg>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center px-10 text-center">
          {hovered ? (
            <>
              <span className="line-clamp-2 text-xs text-ink2">{hovered.label}</span>
              <span className="tnum text-sm font-semibold">{inr(hovered.value, { compact: true })}</span>
              <span className="tnum text-xs text-ink2">{hovered.pct.toFixed(1)}%</span>
            </>
          ) : <span className="text-sm text-ink2">{title}</span>}
        </div>
      </div>
      <ul className="max-h-56 w-full min-w-[12rem] space-y-1 overflow-y-auto pr-1 text-sm">
        {inner?.length ? (
          <>
            {innerTitle && <li className="text-[0.7rem] font-medium uppercase tracking-wide text-muted">{innerTitle}</li>}
            {inner.map((s) => <LegendRow key={`in:${s.key}`} s={s} id={`in:${s.key}`} hover={hover} setHover={setHover} />)}
            <li className="my-1 border-t border-line" />
          </>
        ) : null}
        {slices.map((s) => <LegendRow key={`out:${s.key}`} s={s} id={`out:${s.key}`} hover={hover} setHover={setHover} />)}
      </ul>
    </div>
  );
}

function LegendRow({ s, id, hover, setHover }: { s: Slice; id: string; hover: string | null; setHover: (k: string | null) => void }) {
  return (
    <li className={`flex cursor-default items-center gap-2 rounded px-1 ${hover === id ? "bg-page" : ""}`} onMouseEnter={() => setHover(id)} onMouseLeave={() => setHover(null)}>
      <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: s.color }} aria-hidden />
      <span className="min-w-0 flex-1 truncate text-ink2" title={s.label}>{s.label}</span>
      <span className="tnum text-ink">{s.pct.toFixed(2)}%</span>
    </li>
  );
}
