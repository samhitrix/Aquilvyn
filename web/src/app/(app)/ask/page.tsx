"use client";

import { useMutation } from "@tanstack/react-query";
import { useState } from "react";

import { ErrorNote } from "@/components/ui";
import { api } from "@/lib/api";

type Msg = { role: "user" | "assistant"; text: string; meta?: string };
const EXAMPLES = ["Why is my portfolio down today?", "Which holdings should I review first and why?", "Am I too concentrated anywhere?", "How much tax would I pay if I sold my losers?"];

export default function AskPage() {
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [q, setQ] = useState("");
  const ask = useMutation({
    mutationFn: (question: string) => api<any>("/advisor/ask", { body: { question } }),
    onSuccess: (r) => setMsgs((m) => [...m, { role: "assistant", text: r.answer ?? r.message, meta: r.provider ? `${r.provider} · ${r.model}` : "rules-only" }]),
  });
  function send(text: string) {
    if (!text.trim()) return;
    setMsgs((m) => [...m, { role: "user", text }]);
    setQ("");
    ask.mutate(text);
  }
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-4">
      <div><h1 className="text-xl font-semibold">Ask my portfolio</h1><p className="text-sm text-ink2">Answers use only your real holdings, recommendations and market data — no guesses.</p></div>
      <div className="space-y-3">
        {msgs.length === 0 && <div className="flex flex-wrap gap-2">{EXAMPLES.map((e) => <button key={e} className="btn-ghost" onClick={() => send(e)}>{e}</button>)}</div>}
        {msgs.map((m, i) => (
          <div key={i} className={m.role === "user" ? "ml-auto max-w-[85%] rounded-2xl bg-brand px-4 py-2 text-sm text-white" : "card max-w-[95%] whitespace-pre-wrap px-4 py-3 text-sm"}>
            {m.text}{m.meta && <div className="mt-2 text-xs text-muted">{m.meta}</div>}
          </div>
        ))}
        {ask.isPending && <div className="card px-4 py-3 text-sm text-muted">Thinking with your data…</div>}
        <ErrorNote error={ask.error} />
      </div>
      <form onSubmit={(e) => { e.preventDefault(); send(q); }} className="sticky bottom-20 flex gap-2 md:bottom-4">
        <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder="Ask anything about your family's portfolio…" />
        <button className="btn-primary" disabled={ask.isPending}>Ask</button>
      </form>
    </div>
  );
}
