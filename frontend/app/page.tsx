"use client";

import { useState } from "react";
import { ScanForm } from "./components/ScanForm";
import { ScoreHero } from "./components/ScoreHero";
import { MetricMeter } from "./components/MetricMeter";
import { FlaggedRowsTable } from "./components/FlaggedRowsTable";
import { StatTile } from "./components/StatTile";
import { CoveragePanel } from "./components/CoveragePanel";
import { VersionFootnote } from "./components/VersionFootnote";
import { ScanProgress } from "./components/ScanProgress";
import { HowItWorks } from "./components/HowItWorks";
import { PageHero } from "./components/PageHero";
import { AboutPanel } from "./components/AboutPanel";
import { formatDuration } from "./lib/format";
import { useScanJob } from "./lib/useScanJob";
import { ScanResponse } from "./lib/types";
import { AI_REVIEW_CONSENT_STORAGE_KEY } from "./lib/useAiReviewConsent";

export default function Home() {
  const { result, loading, error, status: jobStatus, progress: jobProgress, stages, jobId, elapsedSeconds, start, cancel } =
    useScanJob<ScanResponse>("/api/scan-jobs");
  // Kept so "Run again with AI review" (see the accuracy meter's
  // action below) can resubmit the SAME files/instruction the user
  // already picked, without the form needing to lift its own file
  // state up just for this one follow-up action.
  const [lastFormData, setLastFormData] = useState<FormData | null>(null);

  async function handleSubmit(formData: FormData) {
    setLastFormData(formData);
    // Bring the live timer and progress card into view right away.
    window.scrollTo({ top: 0, behavior: "smooth" });
    await start(formData);
  }

  // Saves the finished job (result + stage timeline) as a JSON file: the
  // recording a labelled replay is later built from, and the file the
  // accuracy scorer reads. Fetched from the server, so it is the real run.
  async function downloadRunRecord() {
    if (!jobId) return;
    const res = await fetch(`/api/scan-jobs/${encodeURIComponent(jobId)}?events=true`);
    if (!res.ok) return;
    const blob = new Blob([JSON.stringify(await res.json(), null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `run_record_${jobId.slice(0, 8)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  function runAgainWithAiReview() {
    if (!lastFormData) return;
    const formData = new FormData();
    for (const [key, value] of lastFormData.entries()) {
      if (key === "ai_review_consent") continue;
      formData.append(key, value as string | Blob);
    }
    formData.append("ai_review_consent", "true");
    // So the toggle itself reads "on" if the user opens the form again
    // this session — see useAiReviewConsent.ts's own note on why this
    // is a convenience only; the explicit flag above is what actually
    // does the work.
    try {
      sessionStorage.setItem(AI_REVIEW_CONSENT_STORAGE_KEY, "true");
    } catch {
      // best-effort only
    }
    handleSubmit(formData);
  }

  return (
    <main style={{ maxWidth: "980px", margin: "0 auto", padding: "3rem 1.5rem 5rem" }}>
      <PageHero
        eyebrow="Live scan"
        title="Scan any table."
        accent="Trust every row."
        subtitle="Upload a CSV and get a scored quality report: what's blank, duplicated, malformed or out of line, with the reason shown for every flagged row."
        note="No login or saved history yet. Results are held in server memory for up to an hour, then deleted."
      />

      {loading && <ScanProgress status={jobStatus} progress={jobProgress} elapsedSeconds={elapsedSeconds} stages={stages} onCancel={cancel} />}

      <div className="workspace">
        <ScanForm onSubmit={handleSubmit} loading={loading} />
        <AboutPanel kind="single" />
      </div>

      {!loading && !result && !error && <HowItWorks />}

      {error && (
        <div
          role="alert"
          className="fade-in"
          style={{
            marginTop: "1.25rem",
            padding: "0.75rem 1rem",
            borderRadius: "var(--radius-sm)",
            background: "var(--surface-1)",
            border: "1px solid var(--status-critical)",
            color: "var(--text-primary)",
            fontSize: "0.9375rem",
          }}
        >
          {error}
        </div>
      )}

      {result && (
        <section style={{ marginTop: "2.5rem", display: "flex", flexDirection: "column", gap: "1.25rem" }}>
          {result.warnings.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.6rem" }}>
              {result.warnings.map((w, i) => {
                // Informational notes (consent choice, quarantine) read as
                // calm info; anything else keeps the amber caution style.
                const info = w.includes("wasn't requested") || w.includes("quarantined as a security");
                return (
                  <div
                    key={i}
                    className="fade-in"
                    style={{
                      padding: "0.75rem 1.1rem",
                      borderRadius: "var(--radius-md)",
                      background: info
                        ? "var(--surface-1)"
                        : "color-mix(in srgb, var(--status-warning) 9%, var(--surface-1))",
                      border: info
                        ? "1px solid var(--border, rgba(255,255,255,0.12))"
                        : "1px solid color-mix(in srgb, var(--status-warning) 55%, transparent)",
                      lineHeight: 1.55,
                      fontSize: "0.875rem",
                      color: "var(--text-secondary)",
                    }}
                  >
                    {w}
                  </div>
                );
              })}
            </div>
          )}

          {jobId && (
            <div style={{ display: "flex", justifyContent: "flex-end" }}>
              <button type="button" className="btn-ghost" onClick={downloadRunRecord}>
                Download run record (JSON)
              </button>
            </div>
          )}

          <ScoreHero
            score={result.overall_score}
            tableName={result.table_name}
            scanSeconds={elapsedSeconds}
            rowsScanned={result.total_rows}
          />

          <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
            <StatTile label="Rows scanned" value={String(result.total_rows)} />
            <StatTile
              label="Rows affected"
              value={String(result.rows_affected ?? result.flagged_rows.length)}
              sublabel={
                result.total_rows > 0
                  ? `${(((result.rows_affected ?? result.flagged_rows.length) / result.total_rows) * 100).toFixed(1)}% of rows${
                      result.optional_blank_only_rows
                        ? ` · +${result.optional_blank_only_rows} with only an optional blank`
                        : ""
                    }`
                  : undefined
              }
              accent={(result.rows_affected ?? result.flagged_rows.length) > 0 ? "var(--status-warning)" : undefined}
            />
            <StatTile
              label="Scan time"
              value={elapsedSeconds > 0 ? formatDuration(elapsedSeconds) : "—"}
              sublabel={elapsedSeconds >= 1 && result.total_rows > 0 ? `${Math.round(result.total_rows / Math.max(elapsedSeconds, 0.001)).toLocaleString()} rows/s` : undefined}
            />
            <StatTile
              label="Reasoning cycles"
              value={result.llm_used ? String(result.semantic_iterations) : "—"}
              sublabel={result.llm_used ? "semantic passes run" : undefined}
            />
          </div>

          <CoveragePanel result={result} />

          {result.compilation && (
            <div className="card" style={{ padding: "1rem 1.25rem", fontSize: "0.875rem" }}>
              <strong style={{ color: "var(--text-primary)" }}>Custom instruction understood as:</strong>{" "}
              <span style={{ color: "var(--text-secondary)" }}>
                {result.compilation.kind === "failed"
                  ? `Could not compile this instruction (${result.compilation.error ?? "unknown error"}).`
                  : `${result.compilation.kind} rule — "${result.compilation.description}"`}
              </span>
            </div>
          )}

          <div className="card" style={{ padding: "1.25rem" }}>
            <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 0.75rem" }}>
              Metric breakdown
            </h2>
            {result.metric_scores.map((m) => {
              if (m.metric !== "accuracy") {
                return <MetricMeter key={m.metric} metric={m} />;
              }
              // AI review wasn't requested for this scan — the honest
              // reason accuracy is N/A isn't "nothing to reason over,"
              // it's "nobody asked for semantic reasoning." Surface
              // that distinctly, with the one-click path to get it,
              // right where the metric's value is clearest — see the
              // user-facing spec this implements in service/app/
              // version.py's 1.11.0 changelog entry.
              const aiReviewWasOff = result.ai_review_consent !== true;
              const basisLabel: Record<string, string> = {
                local_rules_only: "local rules only",
                local_rules_plus_ai: "local rules + AI review",
                ai_only: "AI review",
              };
              const mode = result.accuracy_basis?.mode;
              const basis = mode && basisLabel[mode] ? `Based on: ${basisLabel[mode]}` : null;
              const note = aiReviewWasOff
                ? `${basis ?? "AI review wasn't requested for this scan"}. Not comparable with AI-on scores.`
                : `${basis ?? "Based on AI review"} · ${result.semantic_rows_considered ?? 0} of ${result.total_rows} rows. Not comparable with AI-off scores.`;
              return (
                <MetricMeter
                  key={m.metric}
                  metric={m}
                  note={note}
                  action={
                    aiReviewWasOff && lastFormData && !loading
                      ? { label: "Run again with AI review", onClick: runAgainWithAiReview }
                      : undefined
                  }
                />
              );
            })}
          </div>

          <div className="card" style={{ padding: "1.25rem" }}>
            <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 0.75rem" }}>
              Flagged rows
            </h2>
            <FlaggedRowsTable rows={result.flagged_rows} />
          </div>

          <VersionFootnote versions={result.versions} />
        </section>
      )}
    </main>
  );
}
