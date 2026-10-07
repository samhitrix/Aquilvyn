"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { Toaster } from "@/components/Toast";
import { ApiError } from "@/lib/api";

function retryAfter(e: ApiError): number | undefined {
  const d = e.detail as { retry_after_seconds?: number } | undefined;
  return typeof d === "object" && d && typeof d.retry_after_seconds === "number" ? d.retry_after_seconds : undefined;
}

export default function Providers({ children }: { children: React.ReactNode }) {
  const [qc] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: true,
            // data is shown from memory for 30 s — moving between pages doesn't re-ask the servers every time
            staleTime: 30_000,
            retry: (n, e) => !(e instanceof ApiError && [401, 403, 404, 422].includes(e.status)) && n < 2,
            // "too many requests": wait as long as the server says (never hammer it — that only extends the wait)
            retryDelay: (n, e) => (e instanceof ApiError && e.status === 429 ? Math.min(30, retryAfter(e) ?? 10) * 1000 : Math.min(1000 * 2 ** n, 8000)),
            gcTime: 10 * 60_000,
          },
        },
      }),
  );
  return <QueryClientProvider client={qc}>{children}<Toaster /></QueryClientProvider>;
}
