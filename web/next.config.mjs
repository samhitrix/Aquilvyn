/**
 * In Docker the browser talks to the nginx gateway (same origin), so no rewrites are needed.
 * In `npm run dev` (no gateway) we mirror the gateway's path routing to the local service ports.
 */
const DEV_ROUTES = {
  8001: ["auth", "household", "members", "audit", "identity"],
  8002: ["profiles", "portfolios", "transactions", "holdings", "holding-prefs", "imports", "performance", "groups", "tax"],
  8003: ["market"],
  8004: ["analysis"],
  8005: ["advisor"],
  8006: ["dashboard"],
  8007: ["readiness"],
};

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  poweredByHeader: false,
  async rewrites() {
    if (process.env.API_INTERNAL_URL) {
      return [{ source: "/api/:path*", destination: `${process.env.API_INTERNAL_URL}/api/:path*` }];
    }
    if (process.env.NODE_ENV !== "development") return [];
    return Object.entries(DEV_ROUTES).flatMap(([port, prefixes]) =>
      prefixes.flatMap((p) => [
        { source: `/api/v1/${p}`, destination: `http://localhost:${port}/api/v1/${p}` },
        { source: `/api/v1/${p}/:path*`, destination: `http://localhost:${port}/api/v1/${p}/:path*` },
      ]),
    );
  },
};

export default nextConfig;
