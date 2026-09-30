"use client";
/**
 * One shared WebSocket for the whole app. Components call `useLivePrice(symbol, fallback)`;
 * the hook subscribes the symbol and re-renders only that component when its tick arrives
 * (useSyncExternalStore — no global re-render per tick). Reconnects with backoff and
 * re-subscribes; pings keep the symbols in the server's "hot set".
 */
import { useEffect, useSyncExternalStore } from "react";

import { auth, refreshSession } from "@/lib/api";
import type { Quote } from "@/lib/types";

const prices = new Map<string, Quote>();
const subs = new Map<string, number>(); // symbol -> refcount
const listeners = new Map<string, Set<() => void>>();
let ws: WebSocket | null = null;
let retry = 0;
let pingTimer: ReturnType<typeof setInterval> | null = null;

function wsUrl(): string {
  const explicit = process.env.NEXT_PUBLIC_WS_URL;
  const base = explicit ?? `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}`;
  return `${base}/api/v1/ws/prices?token=${encodeURIComponent(auth.token ?? "")}`;
}

function connect() {
  if (typeof window === "undefined" || ws || !auth.token) return;
  ws = new WebSocket(wsUrl());
  ws.onopen = () => {
    retry = 0;
    if (subs.size) ws?.send(JSON.stringify({ subscribe: [...subs.keys()] }));
    pingTimer = setInterval(() => ws?.readyState === 1 && ws.send('{"type":"ping"}'), 60_000);
  };
  ws.onmessage = async (ev) => {
    const text = typeof ev.data === "string" ? ev.data : await (ev.data as Blob).text();
    const msg = JSON.parse(text);
    if (msg.type !== "ticks") return;
    for (const q of msg.data as Quote[]) {
      prices.set(q.symbol, q);
      listeners.get(q.symbol)?.forEach((l) => l());
    }
  };
  ws.onclose = async (ev) => {
    ws = null;
    if (pingTimer) clearInterval(pingTimer);
    if (ev.code === 4401) await refreshSession();
    retry = Math.min(retry + 1, 6);
    setTimeout(connect, 500 * 2 ** retry);
  };
}

function subscribe(symbol: string, cb: () => void) {
  if (!listeners.has(symbol)) listeners.set(symbol, new Set());
  listeners.get(symbol)!.add(cb);
  const n = (subs.get(symbol) ?? 0) + 1;
  subs.set(symbol, n);
  connect();
  if (n === 1 && ws?.readyState === 1) ws.send(JSON.stringify({ subscribe: [symbol] }));
  return () => {
    listeners.get(symbol)?.delete(cb);
    const left = (subs.get(symbol) ?? 1) - 1;
    if (left <= 0) {
      subs.delete(symbol);
      if (ws?.readyState === 1) ws.send(JSON.stringify({ unsubscribe: [symbol] }));
    } else subs.set(symbol, left);
  };
}

export function useLivePrice(symbol: string | null | undefined, fallback?: Partial<Quote> | null): Partial<Quote> | null {
  const q = useSyncExternalStore(
    (cb) => (symbol ? subscribe(symbol, cb) : () => {}),
    () => (symbol ? prices.get(symbol) ?? null : null),
    () => null,
  );
  return q ?? fallback ?? null;
}

export function usePriceConnection() {
  useEffect(() => {
    connect();
    return auth.subscribe(() => { if (auth.token) connect(); }) as () => void;
  }, []);
}
