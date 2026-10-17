import { IconActivity } from "./Icons";

// Illustrative values only, labelled "Sample" on the card: this is a
// picture of what a scorecard looks like, not output from any scan.
const METRICS: [string, number][] = [
  ["Completeness", 96],
  ["Uniqueness", 92],
  ["Validity", 97],
  ["Consistency", 98],
];

export function HeroPreview() {
  const r = 34;
  const c = 2 * Math.PI * r;
  return (
    <div className="card hero-preview art-drift" aria-hidden="true">
      <span className="sample-tag">Sample</span>
      <div style={{ display: "flex", alignItems: "center", gap: "0.95rem" }}>
        <svg width="84" height="84" viewBox="0 0 84 84" style={{ transform: "rotate(-90deg)", flexShrink: 0 }}>
          <circle cx="42" cy="42" r={r} fill="none" stroke="var(--meter-track)" strokeWidth="8" />
          <circle
            cx="42" cy="42" r={r} fill="none" stroke="var(--status-good)" strokeWidth="8" strokeLinecap="round"
            strokeDasharray={c} strokeDashoffset={c * 0.06}
            style={{ filter: "drop-shadow(0 0 5px color-mix(in srgb, var(--status-good) 55%, transparent))" }}
          />
        </svg>
        <div>
          <div style={{ fontSize: "0.6875rem", color: "var(--text-muted)", letterSpacing: "0.05em", textTransform: "uppercase" }}>
            Overall quality
          </div>
          <div style={{ fontSize: "1.9rem", fontWeight: 750, lineHeight: 1.1, letterSpacing: "-0.03em", color: "var(--text-primary)" }}>
            94<span style={{ fontSize: "0.95rem", fontWeight: 500, color: "var(--text-muted)" }}> / 100</span>
          </div>
        </div>
      </div>
      <div style={{ marginTop: "0.9rem" }}>
        {METRICS.map(([name, v]) => (
          <div key={name} className="mini-metric">
            <span>{name}</span>
            <span className="mini-track"><i style={{ width: `${v}%` }} /></span>
            <b>{v}</b>
          </div>
        ))}
      </div>
      <span className="float-chip">
        <IconActivity size={14} /> 12 rows flagged, with reasons
      </span>
    </div>
  );
}
