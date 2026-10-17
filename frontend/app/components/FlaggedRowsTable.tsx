"use client";

import { useEffect, useMemo, useState } from "react";
import { FlaggedRowOut } from "../lib/types";

const PAGE_SIZE = 8;

/** A viewer's own agree/disagree call on one flagged row — kept ENTIRELY
 * client-side, for this component instance's lifetime only. There is no
 * backend endpoint for this and none is planned yet: the service is
 * deliberately stateless (see main.py's module docstring), so nothing
 * here persists across a page reload or reaches the server. This is a
 * quick, honest "does this flag look right to you?" pass while looking
 * at results together, not a submitted review — the summary line makes
 * that non-persistence explicit rather than implying it was saved. */
type ReviewLabel = "agree" | "disagree";

function ReviewControls({
  value,
  onSet,
}: {
  value: ReviewLabel | undefined;
  onSet: (label: ReviewLabel) => void;
}) {
  return (
    <div style={{ display: "flex", gap: "0.35rem", marginTop: "0.4rem" }}>
      <button
        type="button"
        onClick={() => onSet("agree")}
        aria-pressed={value === "agree"}
        title="This flag looks right to me"
        style={reviewButtonStyle(value === "agree", "var(--status-good)")}
      >
        <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true">
          <path
            d="M3 8.5 6.5 12 13 4.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Agree
      </button>
      <button
        type="button"
        onClick={() => onSet("disagree")}
        aria-pressed={value === "disagree"}
        title="This looks like a false positive to me"
        style={reviewButtonStyle(value === "disagree", "var(--status-critical)")}
      >
        <svg width="12" height="12" viewBox="0 0 16 16" aria-hidden="true">
          <path
            d="M3.5 3.5 12.5 12.5 M12.5 3.5 3.5 12.5"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
          />
        </svg>
        Disagree
      </button>
    </div>
  );
}

function reviewButtonStyle(active: boolean, activeColor: string): React.CSSProperties {
  return {
    display: "inline-flex",
    alignItems: "center",
    gap: "0.25rem",
    padding: "0.15rem 0.5rem",
    borderRadius: "999px",
    border: `1px solid ${active ? activeColor : "var(--gridline)"}`,
    background: active ? activeColor : "transparent",
    color: active ? "#fff" : "var(--text-muted)",
    fontSize: "0.75rem",
    cursor: "pointer",
  };
}

function VerificationBadge({ label }: { label: FlaggedRowOut["verification_label"] }) {
  if (!label) return null;
  const spec =
    label === "confirmed"
      ? { color: "var(--status-critical)", text: "Confirmed issue" }
      : { color: "var(--status-warning)", text: "Needs review" };
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.3rem", fontSize: "0.8125rem" }}>
      <svg width="8" height="8" viewBox="0 0 8 8" aria-hidden="true">
        <circle cx="4" cy="4" r="4" fill={spec.color} />
      </svg>
      <span style={{ color: "var(--text-secondary)" }}>{spec.text}</span>
    </span>
  );
}

/** Compact preview of a row's fields — the first 2-3 non-blank values,
 * not the whole record. Full data is one click away (RowData below),
 * rather than every row dumping its entire CSV line into the table. */
function rowPreview(rowData: Record<string, unknown>): string {
  const entries = Object.entries(rowData).filter(([, v]) => v !== "" && v !== null && v !== undefined);
  const preview = entries.slice(0, 3).map(([k, v]) => `${k}: ${v}`).join(" · ");
  const remaining = Object.keys(rowData).length - Math.min(3, entries.length);
  return remaining > 0 ? `${preview} · +${remaining} more` : preview;
}

function RowData({ rowData, expanded, onToggle }: { rowData: Record<string, unknown>; expanded: boolean; onToggle: () => void }) {
  return (
    <div>
      {!expanded && (
        <button
          onClick={onToggle}
          style={{
            all: "unset",
            cursor: "pointer",
            color: "var(--text-secondary)",
            fontSize: "0.8125rem",
            textAlign: "left",
          }}
        >
          <code style={{ wordBreak: "break-word" }}>{rowPreview(rowData)}</code>
        </button>
      )}
      {expanded && (
        <div>
          <code style={{ fontSize: "0.8125rem", wordBreak: "break-word", color: "var(--text-primary)" }}>
            {Object.entries(rowData)
              .map(([k, v]) => `${k}: ${v === "" || v === null ? "(blank)" : v}`)
              .join(", ")}
          </code>
          <button
            onClick={onToggle}
            style={{
              display: "block",
              all: "unset",
              cursor: "pointer",
              color: "var(--accent)",
              fontSize: "0.75rem",
              marginTop: "0.3rem",
            }}
          >
            Collapse
          </button>
        </div>
      )}
      {!expanded && (
        <button
          onClick={onToggle}
          style={{ all: "unset", cursor: "pointer", color: "var(--accent)", fontSize: "0.75rem", display: "block", marginTop: "0.2rem" }}
        >
          Show all fields
        </button>
      )}
    </div>
  );
}

type FilterKey = "problems" | "all" | "deterministic" | "semantic" | "confirmed" | "security";

const FILTERS: { key: FilterKey; label: string }[] = [
  { key: "problems", label: "Needs attention" },
  { key: "all", label: "All" },
  { key: "deterministic", label: "Deterministic" },
  { key: "semantic", label: "Semantic" },
  { key: "confirmed", label: "Confirmed" },
  { key: "security", label: "Security" },
];

// A row the prompt-injection backstop flagged has its deterministic
// reason prefixed "SECURITY (...)" (see service/app/main.py) — that
// prefix is the only signal available here, since FlaggedRowOut
// doesn't carry severity directly (see CoveragePanel/SecurityFinding
// for the structured version of the same fact).
function isSecurityRow(row: FlaggedRowOut): boolean {
  return row.deterministic_reasons.some((r) => r.startsWith("SECURITY"));
}

function matchesFilter(row: FlaggedRowOut, filter: FilterKey): boolean {
  switch (filter) {
    case "problems":
      return row.counts_as_problem !== false;
    case "deterministic":
      return row.deterministic_reasons.length > 0;
    case "semantic":
      return row.semantic_reason !== null;
    case "confirmed":
      return row.verification_label === "confirmed";
    case "security":
      return isSecurityRow(row);
    default:
      return true;
  }
}

export function FlaggedRowsTable({ rows }: { rows: FlaggedRowOut[] }) {
  // Open on "Needs attention" only when some rows are blank-only noise;
  // otherwise the two views are identical and "All" is the plain default.
  const [filter, setFilter] = useState<FilterKey>(() =>
    rows.some((r) => r.counts_as_problem === false) ? "problems" : "all"
  );
  const [page, setPage] = useState(0);
  const [expandedRows, setExpandedRows] = useState<Set<number>>(new Set());
  const [review, setReview] = useState<Map<number, ReviewLabel>>(new Map());

  // `rows` is a fresh array reference every time a new scan result comes
  // in from the parent (a brand-new fetch response), but the SAME
  // reference across local re-renders that just change filter/page/
  // expanded state. So resetting on [rows] clears prior review marks
  // exactly when a new scan replaces them, and never in between.
  useEffect(() => {
    setReview(new Map());
  }, [rows]);

  function setRowReview(rowIndex: number, label: ReviewLabel) {
    setReview((prev) => {
      const next = new Map(prev);
      if (next.get(rowIndex) === label) {
        next.delete(rowIndex); // clicking the same choice again clears it
      } else {
        next.set(rowIndex, label);
      }
      return next;
    });
  }

  const reviewCounts = useMemo(() => {
    let agree = 0;
    let disagree = 0;
    for (const label of review.values()) {
      if (label === "agree") agree += 1;
      else disagree += 1;
    }
    return { agree, disagree };
  }, [review]);

  const counts = useMemo(
    () => ({
      problems: rows.filter((r) => matchesFilter(r, "problems")).length,
      all: rows.length,
      deterministic: rows.filter((r) => matchesFilter(r, "deterministic")).length,
      semantic: rows.filter((r) => matchesFilter(r, "semantic")).length,
      confirmed: rows.filter((r) => matchesFilter(r, "confirmed")).length,
      security: rows.filter((r) => matchesFilter(r, "security")).length,
    }),
    [rows]
  );

  const filtered = useMemo(() => rows.filter((r) => matchesFilter(r, filter)), [rows, filter]);
  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const clampedPage = Math.min(page, totalPages - 1);
  const pageRows = filtered.slice(clampedPage * PAGE_SIZE, clampedPage * PAGE_SIZE + PAGE_SIZE);

  function changeFilter(next: FilterKey) {
    setFilter(next);
    setPage(0);
  }

  function toggleExpanded(rowIndex: number) {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(rowIndex)) next.delete(rowIndex);
      else next.add(rowIndex);
      return next;
    });
  }

  if (rows.length === 0) {
    return (
      <p style={{ color: "var(--text-secondary)", fontSize: "0.9375rem" }}>
        No rows flagged — every row passed every check that ran.
      </p>
    );
  }

  return (
    <div>
      {(reviewCounts.agree > 0 || reviewCounts.disagree > 0) && (
        <p style={{ fontSize: "0.8125rem", color: "var(--text-muted)", margin: "0 0 0.75rem" }}>
          Your review (this screen only, not saved): {reviewCounts.agree} agreed &middot;{" "}
          {reviewCounts.disagree} marked false positive
        </p>
      )}

      <div style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap", marginBottom: "0.875rem" }}>
        {FILTERS.map((f) => {
          const active = filter === f.key;
          return (
            <button
              key={f.key}
              onClick={() => changeFilter(f.key)}
              style={{
                padding: "0.3rem 0.75rem",
                borderRadius: "999px",
                border: `1px solid ${active ? "var(--accent)" : "var(--gridline)"}`,
                background: active ? "var(--accent)" : "transparent",
                color: active ? "#fff" : "var(--text-secondary)",
                fontSize: "0.8125rem",
                cursor: "pointer",
              }}
            >
              {f.label} ({counts[f.key]})
            </button>
          );
        })}
      </div>

      <div style={{ overflowX: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: "0.875rem" }}>
          <thead>
            <tr style={{ borderBottom: "1px solid var(--gridline)", textAlign: "left" }}>
              <th style={{ padding: "0.5rem 0.75rem", color: "var(--text-muted)", fontWeight: 500 }}>Row</th>
              <th style={{ padding: "0.5rem 0.75rem", color: "var(--text-muted)", fontWeight: 500 }}>Data</th>
              <th style={{ padding: "0.5rem 0.75rem", color: "var(--text-muted)", fontWeight: 500 }}>Why it was flagged</th>
              <th style={{ padding: "0.5rem 0.75rem", color: "var(--text-muted)", fontWeight: 500 }}>Your review</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) => (
              <tr key={row.row_index} style={{ borderBottom: "1px solid var(--gridline)" }}>
                <td className="tabular" style={{ padding: "0.6rem 0.75rem", verticalAlign: "top", color: "var(--text-secondary)" }}>
                  {row.row_index}
                </td>
                <td style={{ padding: "0.6rem 0.75rem", verticalAlign: "top", color: "var(--text-primary)", maxWidth: "320px" }}>
                  <RowData
                    rowData={row.row_data}
                    expanded={expandedRows.has(row.row_index)}
                    onToggle={() => toggleExpanded(row.row_index)}
                  />
                </td>
                <td style={{ padding: "0.6rem 0.75rem", verticalAlign: "top" }}>
                  {row.deterministic_reasons.map((reason, i) => (
                    <div key={i} style={{ color: "var(--text-secondary)", marginBottom: "0.25rem" }}>
                      {reason}
                    </div>
                  ))}
                  {row.semantic_reason && (
                    <div style={{ marginTop: row.deterministic_reasons.length ? "0.4rem" : 0 }}>
                      <div style={{ color: "var(--text-primary)" }}>
                        {row.semantic_reason}
                        {row.semantic_confidence !== null && (
                          <span className="tabular" style={{ color: "var(--text-muted)" }}>
                            {" "}
                            (self-reported {row.semantic_confidence.toFixed(2)})
                          </span>
                        )}
                      </div>
                      {row.derived_confidence !== null && (
                        <div
                          className="tabular"
                          style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", marginTop: "0.15rem" }}
                          title="Derived from how this flag was produced and verified, not from the model's own self-reported number — see the derivation note in the API response."
                        >
                          Derived confidence: {row.derived_confidence.toFixed(2)}
                        </div>
                      )}
                      <div style={{ marginTop: "0.2rem" }}>
                        <VerificationBadge label={row.verification_label} />
                      </div>
                    </div>
                  )}
                </td>
                <td style={{ padding: "0.6rem 0.75rem", verticalAlign: "top" }}>
                  <ReviewControls
                    value={review.get(row.row_index)}
                    onSet={(label) => setRowReview(row.row_index, label)}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginTop: "0.875rem" }}>
        <span style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
          Showing {filtered.length === 0 ? 0 : clampedPage * PAGE_SIZE + 1}
          {"–"}
          {Math.min(filtered.length, clampedPage * PAGE_SIZE + PAGE_SIZE)} of {filtered.length}
        </span>
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <button
            onClick={() => setPage((p) => Math.max(0, p - 1))}
            disabled={clampedPage === 0}
            style={pagerButtonStyle(clampedPage === 0)}
          >
            Previous
          </button>
          <span style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", alignSelf: "center" }}>
            Page {clampedPage + 1} of {totalPages}
          </span>
          <button
            onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
            disabled={clampedPage >= totalPages - 1}
            style={pagerButtonStyle(clampedPage >= totalPages - 1)}
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}

function pagerButtonStyle(disabled: boolean): React.CSSProperties {
  return {
    padding: "0.3rem 0.75rem",
    borderRadius: "6px",
    border: "1px solid var(--gridline)",
    background: "var(--surface-1)",
    color: disabled ? "var(--text-muted)" : "var(--text-primary)",
    fontSize: "0.8125rem",
    cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? 0.5 : 1,
  };
}
