import { CoverageBadge } from "./CoverageBadge";
import { LocalReviewSummary, ScanResponse } from "../lib/types";

/** "How much should I trust this number?" in one place — coverage (how
 * much of the table the AI layer actually looked at) and security
 * (rows the prompt-injection backstop flagged) are kept visually
 * together here because they answer the same question, even though
 * security findings never feed the score and coverage does (via
 * ACCURACY's denominator — see service/app/scoring/scorer.py).
 *
 * Both halves were backend-correct and UI-invisible before this
 * component: `semantic_rows_considered` existed only in the raw scan
 * JSON (CoverageBadge was a generic on/off chip, used only for
 * "Semantic reasoning: on/off"), and `security_findings` was rendered
 * nowhere at all — only the "SECURITY (...)"-prefixed reason string on
 * a flagged row hinted at it. */
export function CoveragePanel({ result }: { result: ScanResponse }) {
  const totalRows = result.total_rows;
  const considered = result.semantic_rows_considered ?? 0;
  const quarantined = result.quarantined_row_count ?? 0;
  // Not a separate backend field — total_rows, quarantined_row_count,
  // and semantic_rows_considered are already enough to derive this:
  // rows that were neither quarantined nor reliably reasoned over, for
  // some OTHER reason (an opt-in LLM-call budget, a provider outage
  // mid-scan, an unparseable batch response). `warnings` has the prose
  // detail on which, if any, applied this scan.
  const otherSkipped = Math.max(0, totalRows - quarantined - considered);

  const securityFindings = result.security_findings ?? [];
  const highSeverity = securityFindings.filter((f) => f.severity === "high");
  const lowSeverity = securityFindings.filter((f) => f.severity === "low");

  function pct(n: number): number {
    return totalRows > 0 ? (n / totalRows) * 100 : 0;
  }

  return (
    <div className="card" style={{ padding: "1.25rem", display: "flex", flexDirection: "column", gap: "1.5rem" }}>
      <div>
        <h3 style={{ fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 0.6rem" }}>
          AI coverage
        </h3>

        {!result.llm_used ? (
          <>
            {result.ai_review_consent === true ? (
              <CoverageBadge ok={false} label="AI review requested but unavailable (see warnings)" />
            ) : (
              <CoverageBadge ok={true} label="AI review: off (not requested)" />
            )}
            <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", margin: "0.6rem 0 0" }}>
              Data sent externally:{" "}
              <strong style={{ color: "var(--text-primary)" }}>
                {result.data_egress ? (result.data_egress.external_ai_used ? "yes" : "none") : "none"}
              </strong>
            </p>
            {result.local_review && <LocalReviewLine review={result.local_review} />}
          </>
        ) : (
          <>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: "0.875rem", marginBottom: "0.4rem" }}>
              <span style={{ color: "var(--text-secondary)" }}>
                {considered} of {totalRows} row{totalRows === 1 ? "" : "s"} reviewed by AI
              </span>
              <span className="tabular" style={{ color: "var(--text-muted)" }}>
                {pct(considered).toFixed(0)}%
              </span>
            </div>

            {otherSkipped > 0 && (
              <p style={{ fontSize: "0.8125rem", color: "var(--status-warning)", margin: "0 0 0.5rem" }}>
                Scan complete, with {otherSkipped.toLocaleString()} row{otherSkipped === 1 ? "" : "s"} not reviewed
                by AI. The score combines the deterministic checks with the {considered.toLocaleString()} row
                {considered === 1 ? "" : "s"} the AI did review.
              </p>
            )}

            <p style={{ fontSize: "0.8125rem", color: "var(--text-secondary)", margin: "0 0 0.6rem" }}>
              Data sent externally:{" "}
              <strong style={{ color: "var(--text-primary)" }}>
                {result.data_egress?.from_replay
                  ? "none (answers replayed from the local cache)"
                  : result.data_egress?.mode === "values"
                  ? `${(result.data_egress.distinct_values_sent ?? 0).toLocaleString()} distinct values from ${(result.data_egress.columns_shared ?? []).join(", ") || "no columns"}`
                  : `masked rows to ${(result.data_egress?.providers_used ?? []).length > 0 ? "Groq / Gemini" : "AI providers"}${result.data_egress ? ` (${result.data_egress.rows_sent_to_provider.toLocaleString()} rows)` : ""}`}
              </strong>
              {result.data_egress?.partly_from_replay && (
                <span>
                  {" "}
                  ({result.data_egress.calls_replayed} of {(result.data_egress.calls_live ?? 0) + (result.data_egress.calls_replayed ?? 0)} AI calls replayed from cache)
                </span>
              )}
            </p>
            {result.local_review && <LocalReviewLine review={result.local_review} />}

            <div
              role="img"
              aria-label={`Coverage: ${considered} reviewed, ${quarantined} quarantined, ${otherSkipped} skipped, out of ${totalRows} total rows`}
              style={{
                height: "10px",
                borderRadius: "5px",
                background: "var(--meter-track)",
                overflow: "hidden",
                display: "flex",
              }}
            >
              {considered > 0 && <div style={{ width: `${pct(considered)}%`, background: "var(--accent)" }} />}
              {quarantined > 0 && <div style={{ width: `${pct(quarantined)}%`, background: "var(--status-critical)" }} />}
              {otherSkipped > 0 && <div style={{ width: `${pct(otherSkipped)}%`, background: "var(--status-warning)" }} />}
            </div>

            <div style={{ display: "flex", gap: "1rem", marginTop: "0.5rem", fontSize: "0.75rem", flexWrap: "wrap" }}>
              <LegendDot color="var(--accent)" label={`Reviewed (${considered})`} />
              {quarantined > 0 && (
                <LegendDot color="var(--status-critical)" label={`Quarantined, excluded from review (${quarantined})`} />
              )}
              {otherSkipped > 0 && (
                <LegendDot
                  color="var(--status-warning)"
                  label={`Skipped — budget cap, outage, or unparseable response (${otherSkipped})`}
                />
              )}
            </div>
          </>
        )}
      </div>

      <div>
        <h3 style={{ fontSize: "0.9375rem", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 0.6rem" }}>
          Security
        </h3>

        {securityFindings.length === 0 ? (
          // A quiet line, not a hidden section — this is what shows the
          // check actually ran and found nothing, as opposed to never
          // having run at all.
          <p style={{ fontSize: "0.875rem", color: "var(--text-muted)", margin: 0 }}>
            No manipulation patterns detected.
          </p>
        ) : (
          <>
            <div style={{ display: "flex", gap: "0.6rem", marginBottom: "0.75rem", flexWrap: "wrap" }}>
              {highSeverity.length > 0 && (
                <SeverityChip
                  color="var(--status-critical)"
                  text={`${highSeverity.length} quarantined — excluded from AI review`}
                />
              )}
              {lowSeverity.length > 0 && (
                // Deliberately NOT alarming red — low-tier matches are
                // ordinary business phrasing ("pre-approved", "compliance
                // team") reported for visibility, not contained. Red here
                // would make routine CRM/finance language look like a
                // confirmed attack.
                <SeverityChip color="var(--text-muted)" text={`${lowSeverity.length} reported, informational`} />
              )}
            </div>

            <ul style={{ margin: 0, padding: 0, listStyle: "none", display: "flex", flexDirection: "column", gap: "0.5rem" }}>
              {securityFindings.map((f) => (
                <li
                  key={f.row_index}
                  style={{
                    fontSize: "0.8125rem",
                    color: f.severity === "high" ? "var(--text-primary)" : "var(--text-secondary)",
                    display: "flex",
                    gap: "0.5rem",
                    alignItems: "baseline",
                  }}
                >
                  <span className="tabular" style={{ color: "var(--text-muted)", flexShrink: 0 }}>
                    row {f.row_index}
                  </span>
                  <span>
                    {f.detail}
                    {f.matched_columns.length > 0 && (
                      <span style={{ color: "var(--text-muted)" }}> ({f.matched_columns.join(", ")})</span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: "0.35rem", color: "var(--text-muted)" }}>
      <span style={{ width: "8px", height: "8px", borderRadius: "999px", background: color, flexShrink: 0 }} aria-hidden="true" />
      {label}
    </span>
  );
}

function SeverityChip({ color, text }: { color: string; text: string }) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.35rem",
        padding: "0.25rem 0.6rem",
        borderRadius: "var(--radius-full)",
        border: `1px solid ${color}`,
        color,
        fontSize: "0.75rem",
        fontWeight: 600,
      }}
    >
      {text}
    </span>
  );
}

function LocalReviewLine({ review }: { review: LocalReviewSummary }) {
  const skipped = Object.entries(review.columns_skipped);
  return (
    <details style={{ margin: "0.6rem 0 0", fontSize: "0.8125rem", color: "var(--text-secondary)" }}>
      <summary style={{ cursor: "pointer" }}>
        Local review: {review.columns_reviewed.length} column{review.columns_reviewed.length === 1 ? "" : "s"} reviewed,{" "}
        {skipped.length} skipped, {review.distinct_values_scored.toLocaleString()} distinct values checked
      </summary>
      <p style={{ margin: "0.5rem 0 0.25rem" }}>Reviewed: {review.columns_reviewed.join(", ") || "none"}</p>
      {skipped.length > 0 && (
        <ul style={{ margin: 0, paddingLeft: "1.1rem" }}>
          {skipped.map(([col, why]) => (
            <li key={col}>
              {col}: {why}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
