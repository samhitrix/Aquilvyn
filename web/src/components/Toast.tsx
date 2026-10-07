"use client";

import { useEffect, useState } from "react";

export type ToastTone = "ok" | "warn" | "error" | "info";
type Toast = { id: number; tone: ToastTone; title: string; body?: string };

let seq = 0;
const listeners = new Set<(t: Toast) => void>();

/** Show a toast from anywhere (mutations, event handlers). Disappears after 10 s (or ×). */
export function toast(tone: ToastTone, title: string, body?: string) {
  const t = { id: ++seq, tone, title, body };
  listeners.forEach((l) => l(t));
}

const TONE: Record<ToastTone, string> = {
  ok: "border-up/40 text-up", warn: "border-warn/40 text-warn", error: "border-down/40 text-down", info: "border-line text-ink",
};
const ICON: Record<ToastTone, string> = { ok: "✔", warn: "!", error: "✖", info: "i" };

export function Toaster() {
  const [items, setItems] = useState<Toast[]>([]);
  useEffect(() => {
    const add = (t: Toast) => {
      setItems((xs) => [...xs.slice(-3), t]);
      setTimeout(() => setItems((xs) => xs.filter((x) => x.id !== t.id)), 10_000);  // every toast, errors included
    };
    listeners.add(add);
    return () => { listeners.delete(add); };
  }, []);
  if (!items.length) return null;
  return (
    <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex w-[min(24rem,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
      {items.map((t) => (
        <div key={t.id} role={t.tone === "error" ? "alert" : "status"}
             className={`pointer-events-auto rounded-lg border bg-surface px-3 py-2 text-sm shadow-lg ${TONE[t.tone]}`}>
          <div className="flex items-start gap-2">
            <span aria-hidden className="font-semibold">{ICON[t.tone]}</span>
            <div className="min-w-0 flex-1">
              <b>{t.title}</b>
              {t.body && <p className="mt-0.5 break-words text-xs text-ink2">{t.body}</p>}
            </div>
            <button className="text-muted hover:text-ink" aria-label="Dismiss" onClick={() => setItems((xs) => xs.filter((x) => x.id !== t.id))}>×</button>
          </div>
        </div>
      ))}
    </div>
  );
}
