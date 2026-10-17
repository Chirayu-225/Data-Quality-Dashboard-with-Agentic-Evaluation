# DBCaaS Agentic Evaluation Report

> **Read this as a regression check, not an accuracy claim.** The synthetic datasets here do not contain the edge cases recent fixes target (leading-zero zip codes, legitimate `NA`/`None` values, mixed-type columns, PII inside free text), so a perfect score means nothing regressed on the planted issues — not that those cases are measured.

> **GROQ_API_KEY / GEMINI_API_KEY not set** — only the deterministic-only baseline ran (no LLM calls needed for that). Set both keys and rerun for detection accuracy on semantic-only issues, iteration-loop behavior, and custom-instruction reliability.

## crm_leads

Total rows: 150, planted issues: 48 (38 deterministic, 10 semantic-only)

**Deterministic baseline** (no LLM): precision 1.0, recall 1.0, F1 1.0 against the 38 deterministically-catchable planted issues.

*(LLM-dependent sections skipped — no API keys set)*

## ecommerce_orders

Total rows: 150, planted issues: 48 (38 deterministic, 10 semantic-only)

**Deterministic baseline** (no LLM): precision 1.0, recall 1.0, F1 1.0 against the 38 deterministically-catchable planted issues.

*(LLM-dependent sections skipped — no API keys set)*

## hr_employees

Total rows: 143, planted issues: 41 (31 deterministic, 10 semantic-only)

**Deterministic baseline** (no LLM): precision 1.0, recall 1.0, F1 1.0 against the 31 deterministically-catchable planted issues.

*(LLM-dependent sections skipped — no API keys set)*
