"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { ThemeToggle } from "./ThemeToggle";

const LINKS = [
  { href: "/", label: "Live scan" },
  { href: "/connect", label: "Connect a source" },
  { href: "/schema", label: "Schema scan" },
  { href: "/eval", label: "Eval dashboard" },
];

export function NavBar() {
  const pathname = usePathname();
  return (
    <nav
      style={{
        position: "sticky",
        top: 0,
        zIndex: 20,
        display: "flex",
        alignItems: "center",
        gap: "0.25rem",
        padding: "0.7rem 1.5rem",
        borderBottom: "1px solid var(--gridline)",
        background: "color-mix(in srgb, var(--surface-1) 78%, transparent)",
        backdropFilter: "blur(14px) saturate(1.4)",
        WebkitBackdropFilter: "blur(14px) saturate(1.4)",
      }}
    >
      <Link href="/" className="brand" aria-label="DBCaaS home">
        <span className="brand-mark" aria-hidden="true">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
            <ellipse cx="12" cy="6" rx="7" ry="3" />
            <path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6" />
            <path d="M9 17.5l2 2 4-4.5" />
          </svg>
        </span>
        <span className="brand-name">DBCaaS</span>
      </Link>
      {LINKS.map((l) => {
        const active = pathname === l.href;
        return (
          <Link key={l.href} href={l.href} className={`nav-link${active ? " active" : ""}`}>
            {l.label}
          </Link>
        );
      })}
      <span className="nav-spacer" />
      <span className="nav-badge" style={{ marginRight: "0.75rem" }} title="AI review only runs when you switch it on for a scan">
        <i /> AI review is opt-in
      </span>
      <ThemeToggle />
    </nav>
  );
}
