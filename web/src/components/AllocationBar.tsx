"use client";
/** Part-to-whole as ONE 100% stacked bar + a legend carrying the values (no donut): easier to
 *  compare, direct-labelled, identity never colour-alone. Colours follow the entity (fixed
 *  order per asset class), never its rank. */
import { useState } from "react";

import { inr, titleCase } from "@/lib/format";
import type { Slice } from "@/lib/types";

const ORDER = ["equity", "debt", "gold", "hybrid", "real_estate", "alternative", "cash", "other"];

export default function AllocationBar({ data, targets }: { data: Slice; targets?: Record<string, number> }) {
  const [hover, setHover] = useState<string | null>(null);
  const keys = Object.keys(data).sort((a, b) => (ORDER.indexOf(a) + 99 * +(ORDER.indexOf(a) < 0)) - (ORDER.indexOf(b) + 99 * +(ORDER.indexOf(b) < 0)));
  const color = (k: string) => `var(--s${(ORDER.indexOf(k) >= 0 ? ORDER.indexOf(k) : 7) + 1})`;
  if (!keys.length) return <p className="text-sm text-muted">No holdings yet.</p>;
  return (
    <div>
      <div className="flex h-4 w-full gap-[2px] overflow-hidden rounded" role="img" aria-label="Asset allocation">
        {keys.map((k) => (
          <div key={k} style={{ width: `${data[k].pct}%`, background: color(k), opacity: hover && hover !== k ? 0.35 : 1 }}
               className="h-full first:rounded-l last:rounded-r transition-opacity" title={`${titleCase(k)}: ${data[k].pct.toFixed(1)}% (${inr(data[k].value, { compact: true })})`}
               onMouseEnter={() => setHover(k)} onMouseLeave={() => setHover(null)} />
        ))}
      </div>
      <table className="mt-3 w-full text-sm">
        <tbody>
          {keys.map((k) => (
            <tr key={k} onMouseEnter={() => setHover(k)} onMouseLeave={() => setHover(null)} className={hover === k ? "bg-page" : ""}>
              <td className="py-1"><span className="mr-2 inline-block h-2.5 w-2.5 rounded-sm align-middle" style={{ background: color(k) }} />{titleCase(k)}</td>
              <td className="tnum py-1 text-right font-medium">{data[k].pct.toFixed(1)}%</td>
              <td className="tnum py-1 text-right text-ink2">{inr(data[k].value, { compact: true })}</td>
              {targets && <td className="tnum py-1 text-right text-muted">{targets[k] !== undefined ? `target ${targets[k]}%` : ""}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
