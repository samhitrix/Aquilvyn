import type { Config } from "tailwindcss";

const v = (name: string) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./src/**/*.{ts,tsx}"],
  darkMode: ["class", '[data-theme="dark"]'],
  theme: {
    extend: {
      colors: {
        page: v("page"), surface: v("surface"), raised: v("raised"), line: v("line"),
        ink: v("ink"), ink2: v("ink2"), muted: v("muted"),
        brand: v("brand"), up: v("up"), down: v("down"), warn: v("warn"),
      },
      fontFamily: { sans: ["Inter Variable", "Inter", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"] },
    },
  },
  plugins: [],
} satisfies Config;
