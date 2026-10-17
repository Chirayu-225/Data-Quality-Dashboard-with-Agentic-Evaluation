import { METRIC_LABELS, MetricScoreOut, statusForScore } from "../lib/types";

/** Meter form, per the dataviz skill: fill carries severity (status
 * palette by score band), unfilled track is a flat neutral step —
 * state reads across the whole bar, not just the filled portion.
 *
 * `note` is specifically for putting the accuracy denominator right on
 * the meter it belongs to — "based on X of Y rows" — rather than
 * leaving it to a separate panel the viewer has to go cross-reference.
 * Optional, since only ACCURACY has a denominator worth calling out
 * this way; every other metric's denominator is just the row count. */
interface MetricMeterProps {
  metric: MetricScoreOut;
  note?: string;
  /** A single action shown right under the note — used for exactly one
   * case today: ACCURACY reading N/A because AI review wasn't
   * requested for this scan. Lets a viewer act on the explanation at
   * the moment its value is clearest, rather than having to go back
   * to the form and remember to flip the toggle themselves. */
  action?: { label: string; onClick: () => void };
}

export function MetricMeter({ metric, note, action }: MetricMeterProps) {
  const label = METRIC_LABELS[metric.metric] ?? metric.metric;

  // Not evaluated (no applicable check ran) — an empty/neutral track,
  // never a score, and never rendered as if it were a clean 100. See
  // service/app/scoring/scorer.py's module docstring for the bug this
  // distinction fixes.
  if (!metric.evaluated || metric.score === null) {
    return (
      <div style={{ padding: "0.5rem 0" }}>
        <div style={{ display: "grid", gridTemplateColumns: "140px 1fr 64px", alignItems: "center", gap: "0.75rem" }}>
          <span style={{ fontSize: "0.9375rem", color: "var(--text-primary)" }}>{label}</span>
          <div
            style={{
              height: "10px",
              borderRadius: "5px",
              background: "var(--meter-track)",
              overflow: "hidden",
            }}
            role="meter"
            aria-label={`${label} score — not evaluated`}
          />
          <span className="tabular" style={{ fontSize: "0.9375rem", color: "var(--text-secondary)", textAlign: "right" }}>
            N/A
          </span>
        </div>
        {note && (
          <div style={{ gridColumn: "2 / 3", fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.2rem", marginLeft: "152px" }}>
            {note}
            {action && (
              <button
                type="button"
                onClick={action.onClick}
                style={{
                  all: "unset",
                  cursor: "pointer",
                  color: "var(--accent)",
                  fontSize: "0.75rem",
                  fontWeight: 600,
                  marginLeft: "0.5rem",
                  textDecoration: "underline",
                }}
              >
                {action.label}
              </button>
            )}
          </div>
        )}
      </div>
    );
  }

  const score = metric.score;
  const status = statusForScore(score);
  return (
    <div style={{ padding: "0.5rem 0" }}>
      <div style={{ display: "grid", gridTemplateColumns: "140px 1fr 64px", alignItems: "center", gap: "0.75rem" }}>
        <span style={{ fontSize: "0.9375rem", color: "var(--text-primary)" }}>{label}</span>
        <div
          style={{
            height: "10px",
            borderRadius: "5px",
            background: "var(--meter-track)",
            overflow: "hidden",
          }}
          role="meter"
          aria-valuenow={score}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label={`${label} score`}
        >
          <div
            style={{
              width: `${Math.max(0, Math.min(100, score))}%`,
              height: "100%",
              background: status.color,
              borderRadius: "5px",
              transition: "width 0.3s ease",
            }}
          />
        </div>
        <span className="tabular" style={{ fontSize: "0.9375rem", color: "var(--text-secondary)", textAlign: "right" }}>
          {score.toFixed(1)}
        </span>
      </div>
      {note && (
        <div style={{ fontSize: "0.75rem", color: "var(--text-muted)", marginTop: "0.2rem", marginLeft: "152px" }}>
          {note}
        </div>
      )}
    </div>
  );
}
