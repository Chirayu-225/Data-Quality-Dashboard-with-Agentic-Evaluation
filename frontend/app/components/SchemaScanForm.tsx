"use client";

import { FormEvent, useState } from "react";
import { FileTrigger } from "./FileTrigger";
import { AiReviewToggle } from "./AiReviewToggle";
import { useAiReviewConsent } from "../lib/useAiReviewConsent";

interface Props {
  onSubmit: (formData: FormData) => void;
  loading: boolean;
}

interface TableSlot {
  key: string;
  file: File | null;
  instruction: string;
}

let slotCounter = 0;
function newSlot(): TableSlot {
  slotCounter += 1;
  return { key: `slot-${slotCounter}`, file: null, instruction: "" };
}

/** Strips the extension the same way the backend does (Path(filename).stem)
 * so a slot's instruction is keyed to the table name the server will
 * actually assign — "leads.csv" -> "leads". */
function stem(filename: string): string {
  const idx = filename.lastIndexOf(".");
  return idx > 0 ? filename.slice(0, idx) : filename;
}

export function SchemaScanForm({ onSubmit, loading }: Props) {
  const [slots, setSlots] = useState<TableSlot[]>([newSlot(), newSlot()]);
  const [aiReviewConsent, setAiReviewConsent] = useAiReviewConsent();

  const filledCount = slots.filter((s) => s.file !== null).length;
  const canSubmit = filledCount >= 2 && !loading;

  function updateSlot(key: string, patch: Partial<TableSlot>) {
    setSlots((prev) => prev.map((s) => (s.key === key ? { ...s, ...patch } : s)));
  }

  function addSlot() {
    setSlots((prev) => [...prev, newSlot()]);
  }

  function removeSlot(key: string) {
    setSlots((prev) => (prev.length <= 2 ? prev : prev.filter((s) => s.key !== key)));
  }

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!canSubmit) return;

    const formData = new FormData();
    const instructions: Record<string, string> = {};

    for (const slot of slots) {
      if (!slot.file) continue;
      formData.append("files", slot.file);
      if (slot.instruction.trim()) {
        instructions[stem(slot.file.name)] = slot.instruction.trim();
      }
    }
    if (Object.keys(instructions).length > 0) {
      formData.append("custom_instructions", JSON.stringify(instructions));
    }

    // Same explicit-every-request convention as ScanForm — see
    // useAiReviewConsent.ts. Applies to every table's single-table
    // semantic reasoning AND the cross-table semantic pass alike;
    // there's no separate toggle per table or per pass.
    formData.append("ai_review_consent", String(aiReviewConsent));

    onSubmit(formData);
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="card fade-in"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "1rem",
        padding: "1.5rem",
        maxWidth: "640px",
      }}
    >
      <div>
        <p style={{ fontSize: "0.875rem", color: "var(--text-secondary)", margin: "0 0 0.2rem" }}>
          Upload 2 or more related CSVs — each is scanned on its own, plus checked for
          foreign-key references that don&apos;t resolve against any other uploaded table.
        </p>
        <p style={{ fontSize: "0.8125rem", color: "var(--text-muted)", margin: 0 }}>
          A per-table instruction only applies to that one table — an instruction that spans
          tables (e.g. &quot;flag leads without a matching account&quot;) isn&apos;t supported yet.
        </p>
      </div>

      <div style={{ display: "flex", flexDirection: "column", gap: "0.875rem" }}>
        {slots.map((slot, i) => (
          <div
            key={slot.key}
            style={{
              border: "1px solid var(--gridline)",
              borderRadius: "var(--radius-sm)",
              padding: "0.875rem",
              background: "var(--surface-2)",
              display: "flex",
              flexDirection: "column",
              gap: "0.6rem",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
              <span style={{ fontSize: "0.75rem", color: "var(--text-muted)", fontWeight: 600 }}>
                Table {i + 1}
                {slot.file && (
                  <span className="tabular" style={{ color: "var(--text-secondary)", fontWeight: 400 }}>
                    {" "}
                    — {stem(slot.file.name)}
                  </span>
                )}
              </span>
              {slots.length > 2 && (
                <button
                  type="button"
                  onClick={() => removeSlot(slot.key)}
                  style={{
                    all: "unset",
                    cursor: "pointer",
                    color: "var(--text-muted)",
                    fontSize: "0.75rem",
                  }}
                >
                  Remove
                </button>
              )}
            </div>

            <FileTrigger
              id={slot.key}
              hint="Choose a CSV file"
              file={slot.file}
              accept=".csv"
              onChange={(f) => updateSlot(slot.key, { file: f })}
            />

            <input
              type="text"
              className="input"
              placeholder="Optional instruction for this table only (plain English)"
              style={{ fontSize: "0.8125rem" }}
              value={slot.instruction}
              onChange={(e) => updateSlot(slot.key, { instruction: e.target.value })}
            />
          </div>
        ))}
      </div>

      <AiReviewToggle checked={aiReviewConsent} onChange={setAiReviewConsent} />

      <button
        type="button"
        onClick={addSlot}
        style={{
          all: "unset",
          cursor: "pointer",
          color: "var(--accent)",
          fontSize: "0.8125rem",
          alignSelf: "flex-start",
        }}
      >
        + Add another table
      </button>

      <button type="submit" className="btn-primary" disabled={!canSubmit} style={{ padding: "0.65rem 1rem", marginTop: "0.25rem" }}>
        {loading ? "Running schema scan..." : `Run schema scan (${filledCount} table${filledCount === 1 ? "" : "s"})`}
      </button>
    </form>
  );
}
