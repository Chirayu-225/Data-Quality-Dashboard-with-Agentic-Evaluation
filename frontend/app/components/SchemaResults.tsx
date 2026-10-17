"use client";

import { useEffect, useState } from "react";
import { ScoreHero } from "./ScoreHero";
import { StatTile } from "./StatTile";
import { CoverageBadge } from "./CoverageBadge";
import { CoveragePanel } from "./CoveragePanel";
import { CrossTableFindings } from "./CrossTableFindings";
import { CrossTableSemanticFindings } from "./CrossTableSemanticFindings";
import { FlaggedRowsTable } from "./FlaggedRowsTable";
import { VersionFootnote } from "./VersionFootnote";
import { formatDuration } from "../lib/format";
import { SchemaScanResponse } from "../lib/types";

interface Props {
  result: SchemaScanResponse;
  elapsedSeconds: number;
  loading: boolean;
  /** Present only when a "run again with AI review" is possible. */
  onRunAgainWithAiReview?: () => void;
}

/** The whole schema-scan result view, shared by the upload flow (/schema)
 * and the connected-database flow (/connect). */
export function SchemaResults({ result, elapsedSeconds, loading, onRunAgainWithAiReview }: Props) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  useEffect(() => {
    setExpanded(new Set(result.tables.map((t) => t.table_name)));
  }, [result]);

  function toggleTable(name: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  return (
    <section style={{ marginTop: "2.5rem", display: "flex", flexDirection: "column", gap: "1.25rem" }}>
      {result.warnings.length > 0 && (
        <div
          className="fade-in"
          style={{
            padding: "0.85rem 1.1rem",
            borderRadius: "var(--radius-md)",
            background: "color-mix(in srgb, var(--status-warning) 9%, var(--surface-1))",
            border: "1px solid color-mix(in srgb, var(--status-warning) 55%, transparent)",
            lineHeight: 1.55,
            fontSize: "0.875rem",
            color: "var(--text-secondary)",
          }}
        >
          {result.warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      <ScoreHero
        score={result.aggregate_score}
        tableName={`schema (${result.total_tables} tables)`}
        scanSeconds={elapsedSeconds}
        rowsScanned={result.total_rows}
      />

      {(() => {
        // Schema-wide proxy for the one consent flag this whole
        // request carried — every table's ScanResponse echoes the
        // same value (see service/app/main.py's per-table loop),
        // so the first table's is representative of the request.
        const aiReviewConsent = result.tables[0]?.ai_review_consent === true;
        const offReason = result.llm_used
          ? null
          : aiReviewConsent
            ? "no API keys configured"
            : "AI review not requested";
        return (
          <>
            <div style={{ display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
              <StatTile label="Tables scanned" value={String(result.total_tables)} />
              <StatTile label="Total rows" value={String(result.total_rows)} />
              <StatTile
                label="Scan time"
                value={elapsedSeconds > 0 ? formatDuration(elapsedSeconds) : "—"}
              />
              <StatTile
                label="Cross-table findings"
                value={String(result.cross_table_findings.length)}
                accent={result.cross_table_findings.length > 0 ? "var(--status-warning)" : undefined}
              />
              <StatTile
                label="Content mismatches"
                value={String(result.cross_table_semantic_findings.length)}
                sublabel={offReason ? `off — ${offReason}` : undefined}
                accent={result.cross_table_semantic_findings.length > 0 ? "var(--status-warning)" : undefined}
              />
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.75rem", flexWrap: "wrap" }}>
              <CoverageBadge
                ok={result.llm_used}
                label={result.llm_used ? "Semantic reasoning: on" : `Semantic reasoning: off (${offReason})`}
              />
              {!result.llm_used && !aiReviewConsent && onRunAgainWithAiReview && !loading && (
                <button
                  type="button"
                  onClick={onRunAgainWithAiReview}
                  style={{
                    all: "unset",
                    cursor: "pointer",
                    color: "var(--accent)",
                    fontSize: "0.8125rem",
                    fontWeight: 600,
                    textDecoration: "underline",
                  }}
                >
                  Run again with AI review
                </button>
              )}
            </div>
          </>
        );
      })()}

      <div className="card" style={{ padding: "1.25rem" }}>
        <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 0.75rem" }}>
          Cross-table findings
        </h2>
        <p style={{ fontSize: "0.8125rem", color: "var(--text-muted)", margin: "0 0 0.75rem" }}>
          Foreign keys that don&apos;t resolve against any uploaded table.
        </p>
        <CrossTableFindings findings={result.cross_table_findings} />
      </div>

      <div className="card" style={{ padding: "1.25rem" }}>
        <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 0.75rem" }}>
          Cross-table content mismatches
        </h2>
        <p style={{ fontSize: "0.8125rem", color: "var(--text-muted)", margin: "0 0 0.75rem" }}>
          Foreign keys that DO resolve, but whose linked records&apos; content looks
          inconsistent with each other.
        </p>
        <CrossTableSemanticFindings
          findings={result.cross_table_semantic_findings}
          llmUsed={result.llm_used}
        />
      </div>

      <div>
        <h2 style={{ fontSize: "1.0625rem", fontWeight: 600, color: "var(--text-primary)", margin: "0 0 0.875rem" }}>
          Per-table breakdown
        </h2>
        <div style={{ display: "flex", flexDirection: "column", gap: "0.875rem" }}>
          {result.tables.map((table) => {
            const isOpen = expanded.has(table.table_name);
            return (
              <div key={table.table_name} className="card" style={{ padding: "1.25rem" }}>
                <button
                  type="button"
                  onClick={() => toggleTable(table.table_name)}
                  style={{
                    all: "unset",
                    cursor: "pointer",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    width: "100%",
                  }}
                >
                  <span style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
                    <span style={{ fontSize: "1rem", fontWeight: 600, color: "var(--text-primary)" }}>
                      {table.table_name}
                    </span>
                    <span className="tabular" style={{ fontSize: "0.8125rem", color: "var(--text-muted)" }}>
                      {table.total_rows} rows &middot; {table.flagged_rows.length} flagged
                    </span>
                  </span>
                  <span style={{ display: "flex", alignItems: "center", gap: "0.75rem" }}>
                    <span className="tabular" style={{ fontSize: "1.0625rem", fontWeight: 700, color: "var(--text-primary)" }}>
                      {table.overall_score.toFixed(1)}
                    </span>
                    <span style={{ color: "var(--text-muted)", fontSize: "0.8125rem" }}>{isOpen ? "Hide" : "Show"}</span>
                  </span>
                </button>

                {isOpen && (
                  <div className="fade-in" style={{ marginTop: "1rem" }}>
                    {table.compilation && (
                      <div
                        style={{
                          padding: "0.75rem 1rem",
                          marginBottom: "1rem",
                          borderRadius: "var(--radius-sm)",
                          background: "var(--surface-2)",
                          fontSize: "0.8125rem",
                        }}
                      >
                        <strong style={{ color: "var(--text-primary)" }}>Instruction understood as:</strong>{" "}
                        <span style={{ color: "var(--text-secondary)" }}>
                          {table.compilation.kind === "failed"
                            ? `Could not compile (${table.compilation.error ?? "unknown error"}).`
                            : `${table.compilation.kind} rule — "${table.compilation.description}"`}
                        </span>
                      </div>
                    )}
                    <div style={{ marginBottom: "1rem" }}>
                      <CoveragePanel result={table} />
                    </div>
                    <FlaggedRowsTable rows={table.flagged_rows} />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>

      <VersionFootnote versions={result.versions} />
    </section>
  );
}
