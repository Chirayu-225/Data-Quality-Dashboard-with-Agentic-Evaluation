"use client";

import { useState } from "react";
import { ConnectPanel } from "../components/ConnectPanel";
import { ScanProgress } from "../components/ScanProgress";
import { SchemaResults } from "../components/SchemaResults";
import { PageHero } from "../components/PageHero";
import { useScanJob } from "../lib/useScanJob";
import { SchemaScanResponse } from "../lib/types";
import { AI_REVIEW_CONSENT_STORAGE_KEY } from "../lib/useAiReviewConsent";

export default function ConnectPage() {
  const [connId, setConnId] = useState<string | null>(null);
  const { result, loading, error, status: jobStatus, progress: jobProgress, elapsedSeconds, start, cancel } =
    useScanJob<SchemaScanResponse>(connId ? `/api/connections/${connId}/scan-jobs` : "/api/connections/none/scan-jobs");
  const [lastFormData, setLastFormData] = useState<FormData | null>(null);

  async function handleScan(formData: FormData) {
    setLastFormData(formData);
    window.scrollTo({ top: 0, behavior: "smooth" });
    await start(formData);
  }

  function runAgainWithAiReview() {
    if (!lastFormData) return;
    const formData = new FormData();
    for (const [key, value] of lastFormData.entries()) {
      if (key === "ai_review_consent") continue;
      formData.append(key, value as string | Blob);
    }
    formData.append("ai_review_consent", "true");
    try {
      sessionStorage.setItem(AI_REVIEW_CONSENT_STORAGE_KEY, "true");
    } catch {
      // best-effort only
    }
    handleScan(formData);
  }

  return (
    <main style={{ maxWidth: "980px", margin: "0 auto", padding: "3rem 1.5rem 5rem" }}>
      <PageHero
        eyebrow="Connect a source"
        title="Connect your data."
        accent="Don't upload it."
        subtitle="Point the scorecard at a database or your Google Drive, pick the tables, and get the same quality score, relationship checks and plain-English rules, without exporting a single file."
        note={
          <>
            The connection is read-only and rows are sampled. A real deployment should use a
            dedicated read-only database user.
          </>
        }
      />

      {loading && <ScanProgress status={jobStatus} progress={jobProgress} elapsedSeconds={elapsedSeconds} onCancel={cancel} />}

      <ConnectPanel loading={loading} onSelectConnection={setConnId} onScan={handleScan} />

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
        <SchemaResults
          result={result}
          elapsedSeconds={elapsedSeconds}
          loading={loading}
          onRunAgainWithAiReview={lastFormData ? runAgainWithAiReview : undefined}
        />
      )}
    </main>
  );
}
