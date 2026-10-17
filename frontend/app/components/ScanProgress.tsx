import { JobProgress, JobState, StageInfo } from "../lib/runScanJob";
import { formatClock } from "../lib/format";

interface Props {
  status: JobState;
  progress: JobProgress;
  elapsedSeconds: number;
  stages?: StageInfo[];
  onCancel?: () => void;
}

// Real progress from the server's own coverage counter (rows a semantic
// batch has reliably reasoned over so far / total rows). A scan with no AI
// review never reports row progress - it just finishes quickly - so with
// total_rows === 0 this shows an indeterminate shimmer rather than a bar.
// The clock is the server's elapsed time (see useScanJob), so it is right
// after a page refresh too.
const STAGE_LABEL: Record<StageInfo["id"], string> = {
  checks: "Deterministic checks",
  instruction: "Your instruction",
  semantic: "AI review",
  verify: "Independent verification",
  score: "Scoring",
};

function stageDetail(s: StageInfo): string {
  const c = s.counts;
  if (s.status === "skipped") return s.reason ?? "skipped";
  if (s.status === "pending") return "waiting";
  switch (s.id) {
    case "checks":
      return s.status === "done"
        ? `${c.checks_run ?? 0} checks run, ${c.rows_flagged ?? 0} rows flagged`
        : `${c.checks_done ?? 0} of ${c.checks_total ?? 0} checks`;
    case "instruction":
      return s.reason ?? "compiling";
    case "semantic":
      return `${(c.rows_considered ?? 0).toLocaleString()} of ${(c.rows_total ?? 0).toLocaleString()} rows reviewed` +
        (s.status === "done" ? `, ${c.flags_raised ?? 0} flags raised` : "");
    case "verify":
      return s.status === "done"
        ? `${c.confirmed ?? 0} confirmed, ${c.needs_review ?? 0} need review, ${c.rejected ?? 0} rejected`
        : `checking ${c.flags_to_verify ?? 0} flags`;
    case "score":
      return s.status === "done" ? `${c.metrics_evaluated ?? 0} of ${c.metrics_total ?? 0} areas evaluated` : "computing";
  }
}

function StageRail({ stages }: { stages: StageInfo[] }) {
  return (
    <ol style={{ listStyle: "none", margin: "1rem 0 0", padding: 0, display: "grid", gap: "0.35rem" }}>
      {stages.map((s) => (
        <li
          key={s.id}
          style={{
            display: "flex",
            gap: "0.6rem",
            fontSize: "0.8125rem",
            color: s.status === "pending" || s.status === "skipped" ? "var(--text-muted)" : "var(--text-primary)",
          }}
        >
          <span style={{ width: "1.1rem", textAlign: "center" }} aria-hidden="true">
            {s.status === "done" ? "✓" : s.status === "running" ? "●" : s.status === "skipped" ? "–" : "○"}
          </span>
          <span style={{ minWidth: "11rem", fontWeight: s.status === "running" ? 600 : 400 }}>{STAGE_LABEL[s.id]}</span>
          <span style={{ color: "var(--text-secondary)" }}>{stageDetail(s)}</span>
        </li>
      ))}
    </ol>
  );
}

export function ScanProgress({ status, progress, elapsedSeconds, stages, onCancel }: Props) {
  const hasRows = progress.total_rows > 0;
  const unreadable = progress.rows_unreadable ?? 0;
  const pct = hasRows ? Math.min(100, Math.round((progress.rows_considered / progress.total_rows) * 100)) : 0;
  const stage =
    status === "queued"
      ? "Queued, waiting for a free worker"
      : hasRows
        ? `AI review: ${progress.rows_considered.toLocaleString()} of ${progress.total_rows.toLocaleString()} rows reviewed (${pct}%)`
        : "Running checks";

  return (
    <div role="status" aria-live="polite" className="card progress-card fade-in">
      <div className="progress-head">
        <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", minWidth: 0 }}>
          <span className="pulse-dot" aria-hidden="true" />
          <div style={{ minWidth: 0 }}>
            <div style={{ fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)" }}>{stage}</div>
            {unreadable > 0 && (
              <div style={{ fontSize: "0.8125rem", color: "var(--status-warning)", marginTop: "0.2rem" }}>
                {unreadable.toLocaleString()} rows came back unreadable and are not counted as reviewed
              </div>
            )}
          </div>
        </div>
        <div>
          <div className="timer-label">Elapsed</div>
          <div className="timer" aria-label={`Elapsed time ${formatClock(elapsedSeconds)}`}>
            {formatClock(elapsedSeconds)}
          </div>
        </div>
      </div>

      <div
        className={`bar-track${hasRows ? "" : " shimmer-bar"}`}
        style={{ marginTop: "1rem" }}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={hasRows ? pct : undefined}
      >
        {hasRows ? <div className="bar-fill" style={{ width: `${pct}%` }} /> : <span />}
      </div>

      {stages && stages.length > 0 && <StageRail stages={stages} />}

      {onCancel && (
        <div style={{ marginTop: "0.9rem", display: "flex", justifyContent: "flex-end" }}>
          <button type="button" className="btn-ghost" onClick={onCancel}>
            Cancel scan
          </button>
        </div>
      )}
    </div>
  );
}
