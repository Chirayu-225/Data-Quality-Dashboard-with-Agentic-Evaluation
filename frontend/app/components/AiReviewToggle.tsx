"use client";

interface Props {
  checked: boolean;
  onChange: (next: boolean) => void;
}

/** Off by default, every time — see useAiReviewConsent.ts and
 * service/app/main.py's _configure_llms for the product decision this
 * implements: a true per-scan opt-in, not a notice shown after a scan has
 * already sent row data to Groq and Gemini. The user makes this choice
 * BEFORE a scan runs, which is the whole point of putting it here rather
 * than only explaining it after the fact in the results. */
export function AiReviewToggle({ checked, onChange }: Props) {
  return (
    <div
      style={{
        display: "flex",
        gap: "0.75rem",
        alignItems: "flex-start",
        padding: "0.75rem 0.9rem",
        borderRadius: "var(--radius-sm)",
        background: "var(--surface-1)",
        border: "1px solid var(--border)",
      }}
    >
      <label
        htmlFor="ai-review-consent"
        style={{
          position: "relative",
          display: "inline-flex",
          width: "2.25rem",
          height: "1.3rem",
          flexShrink: 0,
          marginTop: "0.1rem",
          cursor: "pointer",
        }}
      >
        <input
          id="ai-review-consent"
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          style={{ position: "absolute", opacity: 0, width: "100%", height: "100%", margin: 0, cursor: "pointer" }}
        />
        <span
          aria-hidden
          style={{
            position: "absolute",
            inset: 0,
            borderRadius: "999px",
            background: checked ? "var(--accent)" : "var(--surface-2)",
            border: "1px solid var(--border)",
            transition: "background 0.15s ease",
          }}
        />
        <span
          aria-hidden
          style={{
            position: "absolute",
            top: "2px",
            left: checked ? "calc(100% - 1.1rem - 2px)" : "2px",
            width: "1.1rem",
            height: "1.1rem",
            borderRadius: "50%",
            background: "var(--surface-2)",
            boxShadow: "0 1px 2px rgba(0,0,0,0.25)",
            transition: "left 0.15s ease",
          }}
        />
      </label>
      <div>
        <label
          htmlFor="ai-review-consent"
          style={{ fontSize: "0.875rem", fontWeight: 600, color: "var(--text-primary)", cursor: "pointer" }}
        >
          AI review {checked ? "on" : "off"}
        </label>
        <p style={{ fontSize: "0.78rem", color: "var(--text-muted)", margin: "0.2rem 0 0", lineHeight: 1.45 }}>
          Off by default. When on, row data is sent to Groq and Gemini for semantic
          reasoning and verification — email and phone columns are masked with a
          placeholder first; everything else in the row goes as-is. See{" "}
          <code style={{ fontSize: "0.75rem" }}>docs/DATA_HANDLING.md</code> for the full
          account of what's sent, to whom, and under what provider terms.
        </p>
      </div>
    </div>
  );
}
