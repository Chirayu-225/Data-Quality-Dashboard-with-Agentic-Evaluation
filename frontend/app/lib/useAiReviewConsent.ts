"use client";

import { useEffect, useState } from "react";

// Per-browser-SESSION convenience only — the backend's own consent gate
// (ai_review_consent) is a true per-REQUEST opt-in with no server-side
// memory of a prior choice (see service/app/main.py's _configure_llms and
// the product decision behind it: persistence is frozen for this phase,
// and even once it exists, consent is meant to become an org-level policy
// with a per-scan override, never something silently inherited from a
// cookie). sessionStorage here exists purely so a user flipping between
// the single-table and schema-scan forms, or re-running a scan, doesn't
// have to re-flip the toggle every time within one browser tab — it is
// NEVER a substitute for sending the flag explicitly on every request.
export const AI_REVIEW_CONSENT_STORAGE_KEY = "dbcaas:ai-review-consent";
const STORAGE_KEY = AI_REVIEW_CONSENT_STORAGE_KEY;

export function useAiReviewConsent(): [boolean, (next: boolean) => void] {
  const [consent, setConsent] = useState(false);

  useEffect(() => {
    try {
      setConsent(sessionStorage.getItem(STORAGE_KEY) === "true");
    } catch {
      // sessionStorage unavailable (private browsing, disabled storage,
      // etc.) — fall back to the real default (false, AI review off)
      // rather than crashing the form.
    }
  }, []);

  function update(next: boolean) {
    setConsent(next);
    try {
      sessionStorage.setItem(STORAGE_KEY, String(next));
    } catch {
      // Best-effort convenience only — the explicit flag still goes on
      // this and every future request via FormData regardless of
      // whether it could be remembered for next time.
    }
  }

  return [consent, update];
}
