import type { ReactNode } from "react";
import { Inter } from "next/font/google";
import { NavBar } from "./components/NavBar";
import { BackgroundArt } from "./components/BackgroundArt";
import { Footer } from "./components/Footer";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-sans", display: "swap" });

export const metadata = {
  title: "DBCaaS",
  description: "Schema-agnostic data connectivity with agentic data quality scoring",
};

// Runs before first paint so the saved (or OS-preferred) theme is applied
// with no light/dark flash. Storage can throw (private mode): fall back to
// the OS preference.
const THEME_INIT = `(function(){try{var t=localStorage.getItem("dbcaas-theme");if(t!=="light"&&t!=="dark"){t=window.matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"}document.documentElement.setAttribute("data-theme",t)}catch(e){}})();`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    // suppressHydrationWarning: THEME_INIT sets data-theme before React hydrates.
    <html lang="en" className={inter.variable} suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_INIT }} />
      </head>
      <body>
        <BackgroundArt />
        <div className="app-content" style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
          <NavBar />
          <div style={{ flex: 1 }}>{children}</div>
          <Footer />
        </div>
      </body>
    </html>
  );
}
