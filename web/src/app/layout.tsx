import type { Metadata, Viewport } from "next";

import "@fontsource-variable/inter";
import "./globals.css";
import Providers from "./providers";

export const metadata: Metadata = {
  title: "Aquilvyn",
  description: "Family wealth tracking with an evidence-backed AI advisor",
};
export const viewport: Viewport = { width: "device-width", initialScale: 1, themeColor: "#4f46e5" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en-IN" suppressHydrationWarning>
      <head>
        {/* restore theme before paint to avoid a flash */}
        <script dangerouslySetInnerHTML={{ __html: `try{var t=localStorage.getItem('fm-theme');if(t)document.documentElement.dataset.theme=t}catch(e){}` }} />
      </head>
      <body className="min-h-screen font-sans">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
