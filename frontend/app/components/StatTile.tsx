/** A single headline number — the "not a chart" answer for a single
 * measure, per the dataviz skill's form-choice step. */
export function StatTile({
  label,
  value,
  sublabel,
  accent,
}: {
  label: string;
  value: string;
  sublabel?: string;
  accent?: string;
}) {
  return (
    <div
      className="card card-interactive tile-accent"
      style={{
        padding: "1rem 1.25rem 1rem 1.4rem",
        minWidth: "140px",
        flex: "1 1 140px",
      }}
    >
      <div style={{ fontSize: "0.75rem", fontWeight: 500, letterSpacing: "0.03em", textTransform: "uppercase", color: "var(--text-muted)", marginBottom: "0.4rem" }}>{label}</div>
      <div
        className="tabular"
        style={{ fontSize: "1.75rem", fontWeight: 600, color: accent ?? "var(--text-primary)", lineHeight: 1.1 }}
      >
        {value}
      </div>
      {sublabel && <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.25rem" }}>{sublabel}</div>}
    </div>
  );
}
