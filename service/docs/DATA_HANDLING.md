# Data handling — what leaves this server, to whom, and when

This is the precise, written answer item 5 of the roadmap called for: an
enterprise buyer evaluating this product will ask exactly this question, and
"it's generally fine" is not an acceptable answer to a security review. Every
claim below is either a fact about this codebase (verifiable by reading the
file named) or a provider policy with its source quoted — nothing here is an
assumption.

## The short version

Nothing leaves this server for ANY scan unless the caller explicitly passes
`ai_review_consent=true` on that specific request. Without it, every scan
runs deterministic checks only — no network call to Groq or Gemini, no row
data transmitted anywhere, regardless of whether the server has API keys
configured. With consent, email- and phone-hinted column values (and any
column the caller separately asks to exclude) are replaced with placeholders
before a single row leaves the server.

## When data leaves the server

Two independent gates, both must be open:

1. **The server has `GROQ_API_KEY`/`GEMINI_API_KEY` configured.** If not, AI
   review can't run for anyone, consent or not (`app/main.py`'s
   `_configure_llms`).
2. **This specific request passed `ai_review_consent=true`** (a `Form` field
   on `/scans` and `/scans/schema`). This is a true opt-in — default `false`
   — not a notice shown after the fact. A request that omits it gets
   deterministic checks only, and the server never even constructs an LLM
   client (see `tests/test_main_api.py`'s
   `test_scan_without_consent_never_constructs_an_llm_client_even_with_keys_configured`,
   which asserts exactly that — a hard guarantee, not a claim).

When both gates are open, row data from the uploaded table(s) is sent to:

- **Groq** (`openai/gpt-oss-20b`, via `app/agent/llm_clients.py::GroqClient`) —
  for single-table semantic reasoning (`app/agent/semantic_reasoning.py`),
  cross-table semantic reasoning (`app/agent/cross_table_reasoning.py`), and
  custom-instruction compilation (`app/rules/compiler.py`, which sends only
  the schema and the instruction text, never row data).
- **Gemini** (via `app/agent/llm_clients.py::GeminiClient`) — for verifying
  flags Groq raised (`app/agent/verifier.py`,
  `CrossTableVerifier` in `cross_table_reasoning.py`). Verification sees the
  same masked row content as the original flag, nothing more.

Neither provider is contacted for anything else this service does —
deterministic checks (null/format/duplicate/outlier/referential-integrity/
type-consistency) all run locally, no network call involved.

## What's masked before it leaves, regardless of consent

Consent controls *whether* data leaves; masking controls *what's in it* when
it does — the two are independent (`app/agent/pii_masking.py`'s module
docstring). Before any row reaches a Groq or Gemini prompt, two layers run:

**Structural (whole-cell) masking.** Any column the CSV connector recognizes
as `email` or `phone` (by column name — see
`csv_connector.py::_infer_semantic_hint`), plus any additional column the
caller names via `exclude_columns` (comma-separated on `/scans`, a JSON
`{table_name: "col1,col2"}` map on `/scans/schema` — e.g. a `first_name`
column the connector has no hint for on its own), has its ENTIRE cell value
replaced with a placeholder.

**Embedded (substring) masking — a backstop for every other text column.**
Every string column NOT already structurally masked, and not hinted as a
`date` column, is also scanned for email- and phone-*shaped* substrings, and
only the matched portion is replaced — the rest of the cell's text is left
alone. This exists specifically because the structural layer only catches a
column whose NAME suggests PII: a column literally named `mobile`, `tel`, or
`contact` (which the name-based hint heuristic doesn't recognize), or a
free-text `notes`/`description` field with an email or phone number typed
into it, would otherwise go out in the clear. This backstop is deliberately
cheap (a regex, not a PII-detection model) and imperfect — it does not claim
to catch every possible PII shape, only the common email/phone case cheaply.
Date-hinted columns are excluded from this scrub entirely (caught by a test
during this fix): a plain date like `2024-01-01` has enough digits and
separators to false-positive match the phone pattern, and a date isn't PII
in the first place.

**Placeholders are keyed by VALUE, not by row.** An earlier version of this
masking made each placeholder a function of the row index alone (`EMAIL_7`
for whatever row 7's email happened to be) — a real defect, not a design
choice: it meant the exact same email appearing in two different rows
produced two different, uncorrelatable tokens, destroying the semantic
layer's ability to notice "these two records share a contact," one of the
strongest duplicate/fraud signals in this kind of data. Every placeholder is
now `sha256(salt + lowercased real value)`, truncated to a short hex suffix
(e.g. `EMAIL_a1b2c3d4e5`) — so the SAME real value, anywhere in one scan
(any column, any row, either side of a cross-table pair), always produces
the SAME token, while a different value never collides with it. `salt` is
generated fresh per scan request (`main.py`'s `secrets.token_hex(16)`) and
is not derived from the data or any fixed secret, so a token's meaning never
carries across two different scans, and the real value can't be recovered
from the token without that scan's own salt, which never leaves the server.

Phone numbers are normalized before hashing (extension dropped, digits only,
last 10 digits compared), so `+1 (555) 123-4567`, `555-123-4567` and
`5551234567` share one token — formatting varies most in phone fields and
duplicate detection across formats is the point. The embedded phone scrub
requires 7-15 digits and rejects dates, IP addresses, dotted version
numbers and order/invoice ids (a near-miss corpus in
`tests/test_pii_masking.py` pins this). Where a shape is genuinely
ambiguous it errs toward masking: a leaked phone number is worse than a
masked order id. Not covered: card numbers and other long digit runs.

A blank cell stays blank; masking only ever replaces a real value, never
manufactures one.

This masking is unconditional and automatic for email/phone (both layers) —
there's no way to opt out of it short of not consenting to AI review at all.
It is NOT reversible by this service after the fact: the placeholder, not
the real value, is what's actually transmitted.

**What masking does NOT cover, by design: names.** A `first_name`,
`last_name`, `full_name`, or `company` column is sent to the LLM as-is
unless the caller explicitly lists it in `exclude_columns`. This is a
deliberate scope choice, not an oversight: some of the semantic checks this
product exists to run — does this contact's name plausibly belong to this
account, is this a likely duplicate under a different email — genuinely need
real names to make that judgment, and masking every name by default would
quietly break those checks for every caller. A deployment that wants names
masked anyway can pass them via `exclude_columns` on every request; this
service will not default to it.

What masking does **not** otherwise cover: any column with no
email/phone-shaped content and not named in `exclude_columns` goes to the
LLM provider as-is — an `address` column with no email/phone substring
inside it, a `job_title`, and so on. If a dataset carries sensitive content
in a shape neither masking layer recognizes, `exclude_columns` is the
mechanism to protect it; nothing masks it automatically beyond what's
described above.

## Retention of results on this server

A finished job's result - including `flagged_rows` and
`security_findings` with the REAL, UNMASKED `row_data` - is held in this
process's memory for up to SCAN_JOB_TTL_SECONDS (default one hour) after
the job finishes, then evicted lazily on the next job access. Nothing is
written to disk by the job store, but anyone holding the job id can read
the result during that window (the 256-bit id is the only access
control; there is no authentication yet), and a process crash or restart
discards it. Lower SCAN_JOB_TTL_SECONDS for sensitive data.

## What the deterministic checks and the final report see

Masking applies ONLY to the copy of the table built for an LLM prompt
(`GeneratorAgent.run`'s `masked_table`, built fresh per scan). Every
deterministic check, the final score, and the `row_data` shown back to the
caller in `flagged_rows`/`security_findings` all use the real, unmasked
table — masking never affects what a human reviewing this product's own
output sees, only what leaves the server for a third party.

## Provider data-use terms, as published (checked 2026-10-03)

These are the providers' own statements, not this project's interpretation
of them. Terms can and do change — re-verify against the live pages before
relying on this for a real compliance decision; this is informational, not
legal advice (see the standard caveat at the end of this document).

**Groq** — from Groq's own data-retention documentation
(`console.groq.com/docs/your-data`):

> "By default, Groq does not retain customer data for inference requests."

Data is retained beyond that only when a feature that requires persistence
is used (batch processing, fine-tuning — neither of which this service
uses), or for up to 30 days for system reliability and abuse monitoring. The
page draws no distinction between free and paid tiers — the same retention
policy is stated to apply to all API usage. The page does not make any
explicit statement about using API request data to train models, in either
direction. Groq also offers an account-level "Zero Data Retention" setting
that removes even the 30-day abuse-monitoring retention, at the cost of
disabling features that depend on it.

**Gemini** — from Google's Gemini API Additional Terms of Service
(`ai.google.dev/gemini-api/terms`), which explicitly distinguishes **unpaid**
(free-tier, which is what this service uses by default — no billing account
configured) from **paid** services:

> Unpaid services: "Google uses the content you submit to the Services and
> any generated responses to provide, improve, and develop Google products
> and services and machine learning technologies, including Google's
> enterprise features, products, and services..."
>
> Paid services: "Google doesn't use your prompts (including associated
> system instructions, cached content, and files such as images, videos, or
> documents) or responses to improve our products..."

In other words: **on the free tier this service is configured to use by
default, Google's own terms state that submitted prompts and responses MAY
be used to improve Google's products and ML technologies** — materially
different from the paid tier's explicit no-use commitment. Anyone deploying
this service against real customer data should treat this as a concrete,
provider-stated reason to move to a paid Gemini tier (or an equivalent
data-processing agreement) before doing so, not an assumption this project
is making on their behalf.

## What this means for a real deployment

- The default configuration (both providers' free tiers) is appropriate for
  development, demos, and evaluation against synthetic or non-sensitive
  data — not for real customer PII at the free tier's stated terms, Gemini's
  especially.
- `ai_review_consent` defaults to `false` specifically so that nothing above
  happens by accident — a deployment that never flips it on never sends
  anything anywhere, full stop.
- Moving to each provider's paid tier (and, for Gemini, getting the explicit
  "paid services" no-training commitment in writing for the account in use)
  is the concrete next step before running this against real customer data
  with AI review enabled.
- This document should be re-verified against the providers' live terms
  pages whenever `DETECTION_VERSION` bumps for an item-5-adjacent change, and
  at minimum before any production deployment — provider terms are not under
  this project's control and can change independently of this codebase.

*This document describes technical behavior and quotes publicly published
provider terms; it is not legal advice. A deployment handling real customer
data should have its own legal/compliance review of both providers' current
terms, independent of this document.*
