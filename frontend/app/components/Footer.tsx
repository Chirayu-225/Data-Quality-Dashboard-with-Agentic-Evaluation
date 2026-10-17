import Link from "next/link";

export function Footer() {
  return (
    <footer className="site-footer">
      <div className="footer-inner">
        <span>DBCaaS &middot; schema-agnostic data quality scoring</span>
        <nav className="footer-links" aria-label="Footer">
          <Link href="/">Live scan</Link>
          <Link href="/schema">Schema scan</Link>
          <Link href="/eval">Eval dashboard</Link>
        </nav>
      </div>
    </footer>
  );
}
