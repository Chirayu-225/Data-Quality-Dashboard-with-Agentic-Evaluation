# Data Quality Pipeline: Design Decisions v1 (for critique)

Status: proposed, nothing implemented from this document yet.
Product context: a data-quality scorecard (working title). Today it is stateless: one CSV upload in, a scorecard and row-level findings out. Deterministic checks always run. An opt-in AI review uses Groq as a semantic detector and Gemini as an independent verifier. Scoring has five metrics plus an overall score, with "not evaluated is never 100" and a quarantine for prompt-injection rows.

## 1. What the research concluded

Six research parts were reviewed (Uber, Google, Amazon, Netflix, Airbnb, LinkedIn, Meta, plus a data-contracts follow-up). Common conclusions:

- Defense in depth: deterministic assertions, statistical anomaly detection, cross-system reconciliation, integrity checks, operational monitoring, lineage and security evidence. No single detector.
- Gate before publishing (Write-Audit-Publish, data contracts, dead letter queues) and score afterwards so quality has an owner.
- Findings should be structured objects. Evidence from independent signals is combined, and one anomaly is never presented as proof of compromise.
- Quality is not security: a tampered value can pass every quality check, and a matching checksum says nothing about semantic quality.
- Many capabilities (drift, volume trends, freshness trends, version comparison, calibration) need history or a second data version.

## 2. Decisions

| # | Decision | Notes |
|---|---|---|
| D1 | Keep the Groq semantic detector. It becomes one detector among deterministic, statistical, integrity and semantic. Measurable properties (nulls, duplicates, ranges, drift, row counts, hashes) are detected deterministically or statistically, not by the LLM. | The research says LLMs should not be the sole authority on quantitative properties. It does not say to remove them. |
| D2 | Gemini stays as verifier but must corroborate independently: it receives the deterministic and statistical evidence, not only Groq's flag. Verifier agreement becomes an Eval V2 metric. | Today it sees only the flag and the row. |
| D3 | Keep an overall score, but always show its components and its coverage ("based on 4 of 7 areas"). It stays capped at the worst evaluated area. Not evaluated is never 100. Governance/owner is N/A for an uploaded CSV. | Avoids a healthy-looking number when most areas were never checked. |
| D4 | Completeness is shown as three separate measures: column null rate, cell fill rate, and source-to-target row ratio (N/A unless a reference file is supplied). | The research uses "completeness" for all three. |
| D5 | The current outlier metric is renamed "Statistical outliers". "Consistency" is reserved for referential integrity, cross-table reconciliation and rule contradictions. This bumps the scoring version. | **Assumed, not yet confirmed.** |
| D6 | Severity (impact) and deviation (distance from a baseline, 0 to 4 style) are separate fields. Until baselines exist, deviation is only distance beyond the IQR fence within the table. | Uber's "severity" is really deviation. |
| D7 | PSI thresholds default to 0.10 (warning) and 0.20 (critical) and are configurable. | Sources disagreed (0.25 vs 0.2). They are heuristics. |
| D8 | Alert display may be grouped, but every underlying finding is kept. | Grouping must not hide a signal. |
| D9 | The claim that static contracts give "zero false positives" is dropped. Eval V2 will measure false positives per deterministic check. | Our own history has sentinel and zip-code false positives. |
| D10 | A claims register labels each research claim as sourced, unsourced or wrong. Only sourced claims are cited in docs or UI. Unsourced figures (downtime multipliers, a silent-failure target) ship, if at all, as configurable defaults labelled "our definition". | |
| D11 | Airbnb's DQ Score dimensions are Accuracy, Reliability (Timeliness), Stewardship, Usability, on a 0 to 100 scale with Poor/Okay/Good/Great buckets. | Verified against a repost of Airbnb's post, not the primary page. Part 6 of the research (validity, lineage tier) was wrong. |
| D12 | Uber's Argos is a real-time metrics alerting system, not a data-quality system. It is not cited as one. | Verified. The Pinot/Kafka detail was not confirmed. |
| D13 | Naming: our injection detection is prompt-injection text in cells. It is not "adversarial ingestion" (bots, Sybil, poisoning) and must not be marketed as detecting poisoning. "Quarantine" for us means excluded from scoring but still visible, not blocked from publishing. | Documented to avoid overclaiming. |
| D14 | Data contracts are parked until last. For an upload-based product a contract could only be an optional spec to validate against, since there is no producer to enforce on. | |

Taxonomy (proposed, not confirmed): score on the six classical dimensions (accuracy, completeness, consistency, timeliness, uniqueness, validity). Use the seven metric families only to group detectors. Lineage is evidence, never a scored dimension.

## 3. Target architecture

```
Source -> Connector -> Profiler
   -> Deterministic | Statistical | Semantic (LLM) | Integrity   (detectors)
   -> Finding model (detector_type, metric, observed, expected, threshold, evidence)
   -> Evidence fusion -> Gemini corroboration
   -> Confidence + severity (+ deviation)
   -> Overall score with components and coverage
   -> Quarantine / incident
```

## 4. Revised phases

1. Extend the finding model (detector_type, metric, observed, expected, threshold). Separate detector from verifier. No behavior change; tests prove it.
2. Deterministic profiling of every column (counts, rates, ranges, cardinality, quantiles).
3. Two-upload comparison: schema diff, volume change, PSI/KS/JS/Wasserstein/chi-square, correlation change, normalized hash comparison. No database needed.
4. Persistence of profiles, findings, versions and hashes. Prerequisites: auth and tenant isolation, and profiles stored as aggregates and masked tokens (top-K values on an email column are raw PII). Update the data-handling doc.
5. Time-series baselines and seasonal anomaly detection on stored history.
6. Reconciliation across sources, once connectors exist.
7. Lineage and access signals, only when a connector actually supplies them. Never inferred for a plain CSV.
8. Agentic investigation over the accumulated evidence.

## 5. What I most want critiqued

- Is keeping an LLM detector inside an evidence-fusion design defensible, or does it undermine the "deterministic first" principle?
- Is "overall score with coverage and a cap" honest enough, or should the headline number go?
- Is it right to build two-upload comparison before persistence?
- Calibration: with only synthetic datasets, is a per-finding-type calibration measure meaningful, or just fitting our own fixtures?
- Hashing: normalized per-row hashes plus a file hash. Anything this misses?
- Renaming "Consistency": does that confuse users coming from the six-dimension literature?

## 6. Known open items

- The 300-row live test against Groq and Gemini has not run since the rate-limit and parser fixes. Those fixes are only verified against mocks.
- Determinism of the LLM calls is unmeasured.
- Duplicate detection does not yet use name or phone keys. Optional columns are penalized when no required columns are configured.
- Several claims in the research remain unverified (see D10).
