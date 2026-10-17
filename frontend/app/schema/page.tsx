"use client";

import { useState } from "react";
import { SchemaScanForm } from "../components/SchemaScanForm";
import { ScanProgress } from "../components/ScanProgress";
import { useScanJob } from "../lib/useScanJob";
import { HowItWorks } from "../components/HowItWorks";
import { PageHero } from "../components/PageHero";
import { AboutPanel } from "../components/AboutPanel";
import { SchemaResults } from "../components/SchemaResults";
import { SchemaScanResponse } from "../lib/types";
import { AI_REVIEW_CONSENT_STORAGE_KEY } from "../lib/useAiReviewConsent";

export default function SchemaScanPage() {
  const { result, loading, error, status: jobStatus, progress: jobProgress, elapsedSeconds, start, cancel } =
    useScanJob<SchemaScanResponse>("/api/scan-schema-jobs");
  const [lastFormData, setLastFormData] = useState<FormData | null>(null);

  async function handleSubmit(formData: FormData) {
    setLastFormData(formData);
    // Bring the live timer and progress card into view right away.
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
    handleSubmit(formData);
  }

  return (
    <main style={{ maxWidth: "980px", margin: "0 auto", padding: "3rem 1.5rem 5rem" }}>
      <PageHero
        eyebrow="Schema scan"
        title="Scan the whole schema."
        accent="Catch what links break."
        subtitle="Upload several related tables. Each gets its own quality score, plus a check for foreign keys that don't resolve against any other table you uploaded."
        note={
          <>
            Every resolved foreign key also gets a content check: does the child row&apos;s data
            look consistent with the parent record it points to (e.g. a contact&apos;s company
            name matching its linked account)? An instruction that spans tables by name (e.g.
            &quot;flag leads without a matching account&quot;) still isn&apos;t supported. That
            requires inferring which tables and join key free text is even referring to, a
            separate and much larger problem than judging a pair the system already knows is
            linked.
          </>
        }
      />

      {loading && <ScanProgress status={jobStatus} progress={jobProgress} elapsedSeconds={elapsedSeconds} onCancel={cancel} />}

      <div className="workspace">
        <SchemaScanForm onSubmit={handleSubmit} loading={loading} />
        <AboutPanel kind="schema" />
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
