import { statusForScore } from "../lib/types";
import { formatDuration } from "../lib/format";
import { IconClock } from "./Icons";

/** The one hero figure per view — the overall scorecard score, as a
 * circular progress gauge (score by arc length, status by color band —
 * never by hue alone: the label + dot beside it carry the same status
 * in words). Center number is proportional, not tabular — a big
 * standalone figure, not a column that needs to align. */
export function ScoreHero({
  score,
  tableName,
  scanSeconds,
  rowsScanned,
}: {
  score: number;
  tableName: string;
  /** Wall-clock time the whole scan took, from the server. */
  scanSeconds?: number;
  rowsScanned?: number;
}) {
  const status = statusForScore(score);
  const clamped = Math.max(0, Math.min(100, score));
  const radius = 52;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference * (1 - clamped / 100);

  return (
    <div
      className="card fade-in"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "1.75rem",
        padding: "1.75rem 2rem",
        flexWrap: "wrap",
        position: "relative",
        overflow: "hidden",
        background: `radial-gradient(420px 220px at 0% 0%, color-mix(in srgb, ${status.color} 16%, transparent), transparent 70%), var(--card-glass)`,
      }}
    >
      <div style={{ position: "relative", width: "124px", height: "124px", flexShrink: 0 }}>
        <svg width="124" height="124" viewBox="0 0 124 124" style={{ transform: "rotate(-90deg)" }}>
          <circle cx="62" cy="62" r={radius} fill="none" stroke="var(--meter-track)" strokeWidth="10" />
          <circle
            cx="62"
            cy="62"
            r={radius}
            fill="none"
            stroke={status.color}
            strokeWidth="10"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            style={{
              transition: "stroke-dashoffset 800ms var(--ease)",
              filter: `drop-shadow(0 0 6px color-mix(in srgb, ${status.color} 55%, transparent))`,
            }}
          />
        </svg>
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <span style={{ fontSize: "1.75rem", fontWeight: 700, color: "var(--text-primary)", lineHeight: 1 }}>
            {clamped.toFixed(0)}
          </span>
          <span style={{ fontSize: "0.6875rem", color: "var(--text-muted)", marginTop: "0.1rem" }}>/ 100</span>
        </div>
      </div>

      <div style={{ minWidth: "220px" }}>
        <div style={{ fontSize: "0.8125rem", color: "var(--text-muted)", marginBottom: "0.375rem" }}>
          Overall data quality &mdash; {tableName}
        </div>
        <div
          className="tabular"
          style={{ fontSize: "1.375rem", fontWeight: 600, color: "var(--text-primary)", marginBottom: "0.625rem" }}
        >
          {clamped.toFixed(1)}
          <span style={{ fontSize: "0.9375rem", fontWeight: 400, color: "var(--text-secondary)" }}> / 100</span>
        </div>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: "0.4rem",
            padding: "0.3rem 0.7rem",
            borderRadius: "var(--radius-full)",
            background: "var(--surface-2)",
            border: `1px solid ${status.color}`,
          }}
        >
          <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
            <circle cx="4" cy="4" r="4" fill={status.color} />
          </svg>
          <span style={{ fontSize: "0.8125rem", fontWeight: 500, color: "var(--text-secondary)" }}>{status.label}</span>
        </span>
        {scanSeconds !== undefined && scanSeconds > 0 && (
          <div className="scan-time tabular">
            <IconClock size={15} />
            <span>
              {rowsScanned !== undefined ? `Scanned ${rowsScanned.toLocaleString()} rows in ` : "Scan completed in "}
              <strong style={{ color: "var(--text-primary)" }}>{formatDuration(scanSeconds)}</strong>
            </span>
          </div>
        )}
      </div>
    </div>
  );
}
