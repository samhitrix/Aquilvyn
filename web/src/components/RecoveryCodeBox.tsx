"use client";

import { useState } from "react";

/** A recovery code, shown once, with copy + "I've saved it". */
export default function RecoveryCodeBox({ code, title, children }: { code: string; title: string; children?: React.ReactNode }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="space-y-2 rounded-lg border-2 border-brand/40 bg-brand/5 px-3 py-3 text-sm">
      <p className="font-semibold">{title}</p>
      <p className="text-xs text-ink2">Save it somewhere safe (a password manager, or on paper). If you ever forget your password, this code plus your
        email lets you set a new one — no email needed. It&apos;s shown only now.</p>
      <div className="flex items-center gap-2">
        <code className="flex-1 rounded-md bg-surface px-3 py-2 text-center text-base font-semibold tracking-widest text-ink">{code}</code>
        <button type="button" className="btn-ghost" onClick={() => { navigator.clipboard?.writeText(code); setCopied(true); }}>{copied ? "Copied ✓" : "Copy"}</button>
      </div>
      {children}
    </div>
  );
}
