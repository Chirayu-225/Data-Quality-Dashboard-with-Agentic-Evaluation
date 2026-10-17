export interface MetricScoreOut {
  metric: string;
  /** null when `evaluated` is false — no applicable check (or, for
   * accuracy, no semantic pass) actually ran this scan. A missing
   * score must never be read as 100 — see service/app/scoring/scorer.py. */
  score: number | null;
  flagged_weight: number;
  total_rows: number;
  /** Whether at least one applicable check ran for this metric. False
   * means `score` is null. */
  evaluated: boolean;
}

export interface FlaggedRowOut {
  row_index: number;
  row_data: Record<string, unknown>;
  deterministic_reasons: string[];
  semantic_reason: string | null;
  semantic_confidence: number | null;
  verification_label: "confirmed" | "needs_review" | "rejected" | null;
  verification_notes: string | null;
  /** A second confidence figure, derived from structural facts about
   * how the flag was produced (see service/app/domain/confidence.py)
   * rather than trusting the raw self-reported number above as a
   * probability. This is a heuristic derivation, not statistical
   * calibration — null when there's no semantic flag on this row. */
  derived_confidence: number | null;
  /** False when this row's ONLY findings are blanks in columns nobody
   * expects to be filled (e.g. an optional notes column). It stays in
   * the list (completeness still counts it) but is not counted in
   * ScanResponse.rows_affected. Absent on older results. */
  counts_as_problem?: boolean;
}

export interface CompilationOut {
  kind: "structured" | "semantic" | "failed";
  description: string;
  error: string | null;
}

/** A row the deterministic prompt-injection backstop flagged as
 * looking like a hostile payload — a security event, not a
 * data-quality defect. This never contributes to any metric score.
 * The SAME row still appears in ScanResponse.flagged_rows too (a
 * security problem is never a reason for a row to disappear from the
 * ordinary review list) — this type is additive structured detail
 * (matched columns, severity) for a future dedicated security-alerts
 * UI, not the only place the row shows up.
 *
 * `severity`: "high" means the row was also QUARANTINED — its content
 * was structurally excluded from semantic reasoning entirely (real
 * containment, see service/app/agent/generator.py). "low" means the
 * matched language is ambiguous, ordinary-sounding business phrasing
 * ("pre-approved", "compliance team") — reported for visibility, but
 * the row stayed fully eligible for normal semantic review.
 *
 * Mirrors service/app/main.py's SecurityFinding. No UI surfaces this
 * list yet — that's a deliberate, separate follow-up ticket; this type
 * exists only so the response shape stays in sync with the backend. */
export interface SecurityFinding {
  row_index: number;
  row_data: Record<string, unknown>;
  matched_columns: string[];
  detail: string;
  severity: "high" | "low";
}

export interface ScanResponse {
  table_name: string;
  total_rows: number;
  overall_score: number;
  metric_scores: MetricScoreOut[];
  /** Includes rows the prompt-injection backstop flagged (both
   * severity tiers) — a security problem is never a reason for a row
   * to vanish from this list. Such a row's deterministic_reasons
   * entry is prefixed "SECURITY (...)" so it reads distinctly from an
   * ordinary quality reason; see security_findings below for the
   * structured version of the same fact. */
  flagged_rows: FlaggedRowOut[];
  semantic_iterations: number;
  llm_used: boolean;
  /** Echoes exactly what this scan's REQUEST asked for — distinct from
   * llm_used, which is whether AI review actually ran. The two can
   * disagree (consent=true, llm_used=false means something else
   * blocked it — see `warnings`). Optional because older cached
   * responses may not carry it; treat a missing value as false, the
   * same default the backend uses. */
  ai_review_consent?: boolean;
  compilation: CompilationOut | null;
  warnings: string[];
  /** Which detection/scoring/prompt logic produced this scorecard, and
   * (when an LLM ran) which models — see service/app/version.py.
   * Optional because older cached responses may not carry it. */
  versions?: Record<string, string>;
  /** Prompt-injection findings ("the Quarantine Model") — see
   * service/app/main.py's ScanResponse for the full rationale.
   * Includes BOTH severity tiers; quarantined_row_count below counts
   * only the "high" subset. Optional because older cached responses
   * may not carry these. */
  security_findings?: SecurityFinding[];
  /** How many of total_rows were QUARANTINED (high-confidence match)
   * and therefore excluded from every quality metric and from
   * semantic reasoning entirely. A low-confidence-only match is NOT
   * counted here — that row stays fully eligible for semantic review. */
  quarantined_row_count?: number;
  /** The actual denominator the semantic layer reasoned over this scan
   * (total_rows - quarantined_row_count) — explicit so it's never left
   * to the client to infer why coverage is less than total_rows. */
  semantic_rows_considered?: number;
  /** Rows with a real problem: flagged_rows minus rows whose only
   * finding is a blank in an optional column. Display-only, never feeds
   * the score. Absent on older results. */
  rows_affected?: number;
  optional_blank_only_rows?: number;
  /** Which rule decided what counts as an expected-filled column:
   * "required_columns" (configured) or "fill_rate_90". */
  blank_policy?: string;
  /** What left the server on this scan (counts only, never values). */
  data_egress?: DataEgress;
  /** Which detectors the accuracy number rests on. */
  accuracy_basis?: AccuracyBasis;
  /** What the local (no-AI) value review covered. Null if it did not run. */
  local_review?: LocalReviewSummary | null;
}

export interface DataEgress {
  mode?: "rows" | "values";
  calls_live?: number;
  calls_replayed?: number;
  from_replay?: boolean;
  partly_from_replay?: boolean;
  columns_shared?: string[];
  distinct_values_sent?: number;
  external_ai_used: boolean;
  providers_used: string[];
  rows_sent_to_provider: number;
  note: string;
}

export interface AccuracyBasis {
  mode: "local_rules_only" | "local_rules_plus_ai" | "ai_only" | "not_evaluated";
  detectors: string[];
  rows_considered: number;
  comparable_across_modes: boolean;
}

export interface LocalReviewSummary {
  columns_reviewed: string[];
  columns_skipped: Record<string, string>;
  distinct_values_scored: number;
  values_sent_externally: number;
}

/** A referential-integrity finding that genuinely spans two tables —
 * from_table holds the foreign key, to_table is what it's supposed to
 * reference. Mirrors service/app/main.py's CrossTableFinding. */
export interface CrossTableFinding {
  from_table: string;
  from_column: string;
  to_table: string;
  flagged_row_count: number;
}

/** A CONTENT-level inconsistency between a child row and the parent row
 * its foreign key already resolves to (e.g. a mismatched company name)
 * — as opposed to CrossTableFinding above, which is a structural fact
 * about a foreign key that doesn't resolve at all. Requires an LLM;
 * empty whenever one isn't configured. Mirrors
 * service/app/main.py's CrossTableSemanticFinding. */
export interface CrossTableSemanticFinding {
  from_table: string;
  from_row_index: number;
  to_table: string;
  to_row_index: number;
  fk_column: string;
  reason: string;
  confidence: number;
  verification_label: "confirmed" | "needs_review" | "rejected" | null;
  verification_notes: string | null;
  /** See FlaggedRowOut.derived_confidence — same idea, cross-table
   * findings start from a lower base rate (two-hop reasoning). */
  derived_confidence: number | null;
}

export interface SchemaScanResponse {
  tables: ScanResponse[];
  cross_table_findings: CrossTableFinding[];
  cross_table_semantic_findings: CrossTableSemanticFinding[];
  aggregate_score: number;
  total_tables: number;
  total_rows: number;
  llm_used: boolean;
  warnings: string[];
  /** See ScanResponse.versions — same idea, schema-wide. */
  versions?: Record<string, string>;
}

/** Mirrors service/app/agent/cost_estimate.py's ScanCostEstimate —
 * pure arithmetic, no LLM calls needed to produce this. */
export interface ScanCostEstimate {
  table_name: string;
  total_rows: number;
  batch_size: number;
  estimated_llm_calls: number;
  estimated_coverage_pct: number;
  estimated_input_tokens: number;
  estimated_cost_usd: number;
  note: string;
}

/** Mirrors service/app/main.py's ScanEstimateResponse (the
 * /scans/estimate endpoint). */
export interface ScanEstimateResponse {
  tables: ScanCostEstimate[];
  total_estimated_llm_calls: number;
  total_estimated_cost_usd: number;
  llm_configured: boolean;
  warnings: string[];
}

export const METRIC_LABELS: Record<string, string> = {
  completeness: "Completeness",
  uniqueness: "Uniqueness",
  validity: "Validity",
  consistency: "Consistency",
  accuracy: "Accuracy",
};

/** Status band for a 0-100 score, per the dataviz skill's fixed status
 * palette (good/warning/serious/critical) — never themed, always paired
 * with an icon + label, never color alone. */
export function statusForScore(score: number): {
  band: "good" | "warning" | "serious" | "critical";
  label: string;
  color: string;
} {
  if (score >= 90) return { band: "good", label: "Good", color: "var(--status-good)" };
  if (score >= 70) return { band: "warning", label: "Needs attention", color: "var(--status-warning)" };
  if (score >= 50) return { band: "serious", label: "Serious", color: "var(--status-serious)" };
  return { band: "critical", label: "Critical", color: "var(--status-critical)" };
}

// ---- Connect a source (/connect) ----
// The Connect screen is generated from the server's connector catalog
// (GET /connectors), so a new connector needs no frontend change.

export interface ConnectorField {
  key: string;
  label: string;
  kind: "text" | "number" | "password" | "textarea" | "select";
  required: boolean;
  secret: boolean;
  default: string | number | null;
  placeholder: string | null;
  help: string | null;
  options: { value: string; label: string }[];
  show_if: Record<string, string>;
}

export interface ConnectorInfo {
  key: string;
  label: string;
  category: string;
  description: string;
  status: "ready" | "planned";
  fields: ConnectorField[];
  /** "form": credentials typed in; "oauth": browser sign-in; "mixed": a field picks. */
  auth: "form" | "oauth" | "mixed";
  /** For "mixed": [field, value] that selects browser sign-in. */
  oauth_field: [string, string] | null;
  oauth_configured: boolean;
}

export interface ConnectorCatalog {
  connectors: ConnectorInfo[];
  unavailable: { module: string; reason: string }[];
  default_row_limit: number;
}

export interface SavedConnection {
  id: string;
  name: string;
  type: string;
  settings: Record<string, unknown>;
  account: string | null;
  has_secret: boolean;
  created_at: number;
}

export interface ConnectionTable {
  id: string;
  name: string;
  description: string | null;
  approx_rows: number | null;
  primary_key: string[];
  columns: { name: string; type: string }[];
  foreign_keys: { columns: string[]; references: string; referred_columns: string[] }[];
}

export interface ConnectionTablesResponse {
  connection: SavedConnection;
  row_limit: number;
  tables: ConnectionTable[];
}
