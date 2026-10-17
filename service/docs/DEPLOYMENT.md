# Deployment notes

Constraints that matter the moment someone changes how this service is run.

## Run exactly ONE server process

Async scan jobs (`POST /scans/jobs`, `POST /scans/schema/jobs`,
`GET /scans/jobs/{id}`) live in the memory of the process that accepted
them (`app/jobs.py`). Under `uvicorn --workers N`, `gunicorn -w N`, or
several replicas behind a load balancer, a job submitted to one process
returns **404 when polled on another**. This is the setting most likely
to be changed "for performance", so it is stated plainly:

- Use a single process: `uvicorn app.main:app --port 8000`.
- Scale a single process with the worker pool below, not process count.
- Running multiple processes needs a shared job store (Redis or a DB
  table) — a code change, not a config flag.

Jobs are also lost on restart; a poller then gets a 404, which the
frontend reports as "the server restarted while the scan was running".

## Job-queue limits (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `SCAN_JOB_WORKERS` | 2 | Jobs executing at once (bounded thread pool) |
| `SCAN_JOB_MAX_PENDING` | 8 | Queued + running jobs allowed; beyond this, 429 + `Retry-After` |
| `SCAN_JOB_TTL_SECONDS` | 3600 | How long a finished job's result stays retrievable |

Job ids are 256-bit random tokens. With no auth, the id is the only
access control on a result (which contains unmasked flagged rows), so it
must stay unguessable and should be treated as a secret.

## No auth yet

Anyone who can reach the service can scan and can fetch any job whose id
they hold. Keep it off the public internet until the auth phase lands.

## Rate limits are process-wide

The tokens-per-minute budget and the daily-quota circuit breaker live at
module level in `app/agent/llm_clients.py`, keyed by (provider, hash of
API key) - NOT per client or per job. Concurrent scans share one Groq
budget, so SCAN_JOB_WORKERS > 1 does not multiply your TPM. When a
provider reports its per-DAY token cap exhausted, the breaker opens
(for the provider's own "try again in ..." duration, default 30 min) and
every scan on this server stops calling that provider immediately with a
clear coverage warning instead of retrying into a wall. Limits: this is
per process (hence the single-process rule above), and concurrent jobs
still draw down the same daily cap, so they exhaust it proportionally
faster. Env: GROQ_TOKENS_PER_MINUTE, GROQ_TOKENS_PER_DAY (warning only).

## Job lifecycle: cancel, expiry, resume

`DELETE /scans/jobs/{id}` cancels a queued job (it never starts) or a
running one (marked cancelled at once; the worker stops at the next
batch boundary, so at most one provider call is still in flight). A
finished job is expired after SCAN_JOB_TTL_SECONDS: polling then returns
**410** ("results expired, run again"), distinct from **404** (unknown id
or server restarted). Expiry tombstones hold ids only and are themselves
dropped after 24 h. The browser keeps the job id in the page URL
(`?job=...`), so refresh/reopen re-attaches to the same job; closing the
tab does not cancel a scan by itself. Instead a HEARTBEAT handles abandoned
scans: every poll refreshes the job, and a queued/running job nobody has
polled for SCAN_JOB_ABANDON_SECONDS (default 180, 0 disables) is
auto-cancelled. A refreshed tab resumes polling within seconds and is
unaffected; a closed tab never polls again, so its scan stops spending
quota. Cancellation is cooperative and reaches every phase: all provider
calls (semantic, verification, cross-table, rule compilation) and pacing
sleeps consult the job's cancel flag (app/cancellation.py), so at most one
in-flight call completes. A cancelled job keeps NO partial results - a
partial report could be mistaken for a real score.

## Stage and count events

`GET /scans/jobs/{id}` also returns `stages`: the pipeline in fixed order
(`checks`, `instruction`, `semantic`, `verify`, `score`), each with a status
(`pending` / `running` / `done` / `skipped`), integer `counts` (checks done,
rows considered, flags raised, verifier labels, metrics evaluated), offsets in
seconds since job creation, and a plain-language `reason` when a stage was
skipped (for example "AI review not enabled for this scan"). Counts only;
no row content is ever placed in a stage or event. Add `?events=true` to also
get the capped timeline (400 events, oldest progress ticks dropped first, state
changes always kept). That timeline is what a recorded run is replayed from, and a
replay must be labelled as a recording. For a multi-table schema job a stage
shows the table most recently processed, and each event carries its `table`.
Cancel reaches the deterministic phase too: the stage callback raises on cancel
between checks.

## Rows affected

`ScanResponse.rows_affected` counts rows with a real problem. `flagged_rows`
is longer because `null_check` flags a blank in ANY column (completeness has to
see them), so a mostly-empty optional column such as `notes` would otherwise
make a file look far worse than it is (live 300-row test: 116 flagged rows, 50
with a real problem). A row is marked `counts_as_problem=false` when its only
findings are blanks in columns nobody expects to be filled; it stays in the
list. With `required_columns` configured, exactly those columns decide.
Otherwise a column counts as expected-filled when at least 90% of rows have a
value (a threshold chosen by judgment, not tuned on labelled data); the response
names the rule in `blank_policy`. Display only: it never changes a score.

## Instruction preview

`POST /scans/instruction-preview` (multipart: `file`, `custom_instruction` or
`custom_instruction_file`, `ai_review_consent`, optional `sample_rows` 5-45,
default 20) shows what a custom instruction would do before a full scan.
An instruction that compiles to a rule is run over every row with no further
LLM call, so `matched_rows` is exact (`exact: true`) and it is 1 call in total
(the compile sees only the schema and the instruction text). An instruction
needing judgment is tried on a small masked sample, with prompt-injection
quarantined rows excluded as in a real scan; the result is labelled a sample
(`exact: false`) because it is not a count for the whole file, and costs 2 calls.
A failed or unreadable sample says "unknown", never "nothing found". Without
consent or keys it returns `kind: "unavailable"` and makes no call.

## Local value review (default semantic tier)

With AI review off, a scan still runs a local value review: no LLM, no
network, nothing leaves the process. Each text column is reduced to its
distinct values, each value is scored with independent heuristics
(keyboard mashing, placeholder words, record-status markers, role words
in a person-name column, repeated tokens, shape that deviates from the
rest of the column), and findings map back to every row holding the
value. Free-text, id, numeric, email, phone and date columns are skipped
and the response says which and why (`local_review.columns_skipped`).

Every scan response carries `data_egress`: whether any external provider
was used, which, and how many rows went to it. With AI review off it is
always `external_ai_used: false`.

`LOCAL_REVIEW=0` turns the tier off and restores the pre-1.21 AI-off
behavior (deterministic checks only, accuracy not evaluated).

Measured limits (scripts/eval_local_review.py): on the development file
(whose bad values the word lists were written after seeing) it caught
10/10 planted values with 0 false flags; on a held-out file written
afterwards with different values it caught 5/10 with 0 false flags. The
held-out figure is the honest one. It cannot judge anything that needs
world knowledge or row context.
