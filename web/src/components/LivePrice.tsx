"use client";

import clsx from "clsx";
import { useEffect, useRef, useState } from "react";

import { useLivePrice } from "@/hooks/usePriceStream";
import { num, pct, tone } from "@/lib/format";

/** Price that ticks live; briefly flashes on change. */
export default function LivePrice({ symbol, price, prevClose, showChange = true }: { symbol: string; price: number | null; prevClose?: number | null; showChange?: boolean }) {
  const q = useLivePrice(symbol, price !== null ? { price, prev_close: prevClose ?? undefined } : null);
  const last = useRef<number | undefined>(undefined);
  const [flash, setFlash] = useState<"up" | "down" | null>(null);
  useEffect(() => {
    if (q?.price === undefined) return;
    if (last.current !== undefined && q.price !== last.current) {
      setFlash(q.price > last.current ? "up" : "down");
      const t = setTimeout(() => setFlash(null), 700);
      last.current = q.price;
      return () => clearTimeout(t);
    }
    last.current = q.price;
  }, [q?.price]);
  if (!q?.price) return <span className="text-muted">—</span>;
  const chg = q.prev_close ? ((q.price - q.prev_close) / q.prev_close) * 100 : null;
  return (
    <span className={clsx("tnum rounded px-1 transition-colors", flash === "up" && "bg-up/15", flash === "down" && "bg-down/15")}>
      ₹{num(q.price, 2)}
      {showChange && <span className={clsx("ml-1.5 text-xs", tone(chg))}>{pct(chg)}</span>}
    </span>
  );
}
