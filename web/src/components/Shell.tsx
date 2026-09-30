"use client";

import clsx from "clsx";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";

import { usePriceConnection } from "@/hooks/usePriceStream";
import { auth, logout, refreshSession } from "@/lib/api";
import { toggleTheme } from "@/lib/theme";

const NAV = [
  { href: "/dashboard", label: "Dashboard", icon: "◧" },
  { href: "/advisor", label: "Advisor", icon: "✦" },
  { href: "/holdings", label: "Holdings", icon: "▤" },
  { href: "/analytics", label: "Analytics", icon: "◔" },
  { href: "/tax", label: "Tax", icon: "₹" },
  { href: "/markets", label: "Markets", icon: "↗" },
  { href: "/transactions", label: "Transactions", icon: "⇅" },
  { href: "/import", label: "Import", icon: "⇪" },
  { href: "/family", label: "Family", icon: "👪" },
  { href: "/ask", label: "Ask", icon: "💬" },
  { href: "/settings", label: "Settings", icon: "⚙" },
];
const MOBILE = ["/dashboard", "/advisor", "/holdings", "/markets", "/ask"];

export default function Shell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const path = usePathname();
  const user = useSyncExternalStore(auth.subscribe, () => auth.user, () => null);
  const [ready, setReady] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  useEffect(() => { try { setCollapsed(localStorage.getItem("fm-sidebar") === "collapsed"); } catch {} }, []);
  const toggleSidebar = () => setCollapsed((c) => { try { localStorage.setItem("fm-sidebar", c ? "open" : "collapsed"); } catch {} return !c; });
  usePriceConnection();

  useEffect(() => {
    if (auth.token) { setReady(true); return; }
    refreshSession().then((ok) => (ok ? setReady(true) : router.replace("/login")));
  }, [router]);

  if (!ready || !user) return <div className="grid min-h-screen place-items-center text-sm text-muted">Loading your portfolio…</div>;

  return (
    <div className="min-h-screen md:flex">
      <aside className={clsx("sticky top-0 hidden h-screen shrink-0 flex-col border-r border-line bg-surface p-3 transition-[width] md:flex", collapsed ? "w-16" : "w-56")}>
        <div className="flex items-center justify-between px-1 py-2">
          {!collapsed && <Link href="/dashboard" className="px-1 text-lg font-semibold">Aquil<span className="text-brand">vyn</span></Link>}
          <button onClick={toggleSidebar} className="rounded-lg px-2 py-1 text-ink2 hover:bg-page" aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"} title={collapsed ? "Expand" : "Collapse"}>
            {collapsed ? "»" : "«"}
          </button>
        </div>
        {!collapsed && <div className="px-2 pb-3 text-xs text-muted">{user.household_name}</div>}
        <nav className="flex-1 space-y-0.5">
          {NAV.map((n) => (
            <Link key={n.href} href={n.href} prefetch title={n.label} className={clsx("flex items-center gap-2 rounded-lg px-2 py-2 text-sm", collapsed && "justify-center", path.startsWith(n.href) ? "bg-brand/10 font-medium text-brand" : "text-ink2 hover:bg-page")}>
              <span aria-hidden className="w-5 text-center">{n.icon}</span>{!collapsed && n.label}
            </Link>
          ))}
        </nav>
        <div className="space-y-1 border-t border-line pt-3 text-sm">
          {!collapsed && <div className="truncate px-2 text-ink2" title={user.email}>{user.full_name || user.email}</div>}
          {!collapsed && <div className="px-2 text-xs capitalize text-muted">{user.role}</div>}
          <button onClick={toggleTheme} title="Light / dark" className="w-full rounded-lg px-2 py-1.5 text-left text-ink2 hover:bg-page">◐{!collapsed && " Light / dark"}</button>
          <button onClick={() => logout().then(() => router.replace("/login"))} title="Sign out" className="w-full rounded-lg px-2 py-1.5 text-left text-ink2 hover:bg-page">⎋{!collapsed && " Sign out"}</button>
        </div>
      </aside>
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-surface/95 px-4 py-3 backdrop-blur md:hidden">
        <span className="font-semibold">Aquil<span className="text-brand">vyn</span></span>
        <div className="flex gap-3 text-sm text-ink2">
          <Link href="/transactions">Txns</Link><Link href="/family">Family</Link>
          <button onClick={toggleTheme} aria-label="Toggle light/dark theme">◐</button><Link href="/settings" aria-label="Settings">⚙</Link>
        </div>
      </header>
      <main className="w-full min-w-0 flex-1 px-4 pb-24 pt-4 md:px-6 md:pb-8 2xl:px-10">{children}</main>
      <nav className="fixed inset-x-0 bottom-0 z-10 grid grid-cols-5 border-t border-line bg-surface md:hidden">
        {NAV.filter((n) => MOBILE.includes(n.href)).map((n) => (
          <Link key={n.href} href={n.href} className={clsx("flex flex-col items-center py-2 text-[11px]", path.startsWith(n.href) ? "text-brand" : "text-ink2")}>
            <span aria-hidden className="text-base">{n.icon}</span>{n.label}
          </Link>
        ))}
      </nav>
    </div>
  );
}
