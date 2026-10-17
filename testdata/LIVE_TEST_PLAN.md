# Live test plan: leads_300.csv

Use `testdata/leads_300.csv`, AI review ON. Treat the numbers as "was each
issue type caught at all", not accuracy estimates (the file has a raised
share of semantic issues, so precision does not reflect real-world rates).

Before: restart the backend (so the new code loads) and the frontend. Keep
the backend console visible; every step below needs its log lines.

## Phase 1 - shared budget, both scans run to completion
1. Open two browser tabs on the app. In each, upload leads_300.csv with AI
   review on and press Run scan, within a few seconds of each other.
2. About halfway through, refresh ONE tab. It should reload into the same
   scan (the address bar has `?job=...`) and keep going.
3. Let both finish. Expect: no provider 429s, each scan taking roughly twice
   as long as it would alone (they share one budget), no unreadable batches.
4. Save each result: `curl http://localhost:8000/scans/jobs/<JOB_ID> -o
   testdata/result_A.json` (and `result_B.json`). The JOB_ID is in the URL.

Collect from the backend console: every line containing `llm usage`,
`semantic batch`, `verifier chunk`, `unparseable`, `429`, or `quota`.
The `llm usage provider=gemini` lines are the verifier's.

## Phase 2 - cancel and abandon (cheap)
a. Start one scan; as soon as the first progress appears, press Cancel scan.
   Expect "Scan cancelled", and in the console no further `llm usage` lines
   after at most one more call.
b. Start another scan and CLOSE the tab. Wait 4 minutes, then open
   `http://localhost:3000/?job=<JOB_ID>`: it should say the scan was
   auto-cancelled. The console should show no calls after the first minutes.

## Phase 3 - expiry and busy limit (shortened settings)
Stop the backend. In PowerShell, in the service folder:
  $env:SCAN_JOB_TTL_SECONDS="60"; $env:SCAN_JOB_MAX_PENDING="1"
then start the backend as usual.
- Busy: start a scan (AI review on), then immediately start a second in the
  other tab. Expect "server is busy" (HTTP 429). Cancel the first scan.
- Expiry: run a scan with AI review OFF (costs no quota); after it
  finishes wait over 60 seconds, then refresh. Expect "results expired,
  run again" (HTTP 410), not a generic error.
Afterwards close that terminal and restart the backend WITHOUT those two
variables so the defaults (3600 / 8) are back.

## Scoring
Put result_A.json in testdata/ and tell me. I run
`python score_vs_truth.py result_A.json leads_300_truth.json` on your machine.
Deterministic baseline for comparison: duplicate_email 10/10,
amount_sentinel 15/15, amount_garbage 4/4, blank_required 6/6,
prompt_injection 3/3, amount_outlier 3/3, phone_format_duplicate 0/5,
semantic_mismatch 0/10. Those last two are what AI review must add.
