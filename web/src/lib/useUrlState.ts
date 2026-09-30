"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { Scope } from "./queries";

/** A filter that survives refresh and Back: kept in the page URL (?key=value), and remembered per page so
 *  opening the tab from the sidebar brings back the last choice. Defaults are left out of the URL. */
export function useUrlState<T extends string = string>(key: string, initial: NoInfer<T>): [T, (v: T) => void] {
  const [value, setValue] = useState<T>(initial);
  const store = useRef("");
  useEffect(() => {
    store.current = `fm:filter:${window.location.pathname}:${key}`;
    const fromUrl = new URLSearchParams(window.location.search).get(key);
    let saved: string | null = null;
    try { saved = window.localStorage.getItem(store.current); } catch { /* private mode */ }
    const v = (fromUrl ?? saved) as T | null;
    if (v != null && v !== initial) {
      setValue(v);
      if (fromUrl == null) writeUrl(key, v, initial);  // restored from memory: show it in the URL too
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  const set = useCallback((v: T) => {
    setValue(v);
    writeUrl(key, v, initial);
    try {
      if (v === initial) window.localStorage.removeItem(store.current);
      else window.localStorage.setItem(store.current, v);
    } catch { /* ignore */ }
  }, [key, initial]);
  return [value, set];
}

function writeUrl(key: string, v: string, initial: string) {
  const u = new URL(window.location.href);
  if (v === initial || v === "") u.searchParams.delete(key);
  else u.searchParams.set(key, v);
  window.history.replaceState(window.history.state, "", u.toString());
}

/** The Whole family / person / group picker, persisted the same way (?view=profile:<id>). */
export function useScopeState(key = "view"): [Scope, (s: Scope) => void] {
  const [raw, setRaw] = useUrlState<string>(key, "household");
  const scope = useMemo<Scope>(() => {
    const [t, id] = raw.split(":");
    return t === "profile" || t === "group" || t === "portfolio" ? { type: t, id } : { type: "household" };
  }, [raw]);
  const set = useCallback((s: Scope) => setRaw(s.type === "household" ? "household" : `${s.type}:${s.id}`), [setRaw]);
  return [scope, set];
}
