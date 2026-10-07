export type ThemeChoice = "system" | "light" | "dark";

export function getTheme(): ThemeChoice {
  try { return (localStorage.getItem("fm-theme") as ThemeChoice) || "system"; } catch { return "system"; }
}

/** "system" follows the OS (prefers-color-scheme); light/dark pin it. Charts read CSS tokens, so
 *  they pick the new palette on their next render. */
export function setTheme(t: ThemeChoice) {
  const root = document.documentElement;
  if (t === "system") delete root.dataset.theme;
  else root.dataset.theme = t;
  try { t === "system" ? localStorage.removeItem("fm-theme") : localStorage.setItem("fm-theme", t); } catch {}
  window.dispatchEvent(new Event("fm-theme"));
}

export function toggleTheme() {
  const cur = document.documentElement.dataset.theme ?? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  setTheme(cur === "dark" ? "light" : "dark");
}
