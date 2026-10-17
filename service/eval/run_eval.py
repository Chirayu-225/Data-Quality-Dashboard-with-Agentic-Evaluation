"""
Agentic evaluation harness — runs the real GeneratorAgent pipeline
against the forged datasets in eval/datasets/ and scores it against
their known ground truth, instead of eyeballing demo output.

Three things this measures, per the scope agreed for this pass:

1. DETECTION ACCURACY — precision/recall/F1 of flagged_row_indices
   against each dataset's planted issues, split into "deterministic"
   (checks alone should catch these) and "semantic-only" (nothing but
   the LLM layer can catch these) so a weak semantic layer can't hide
   behind strong deterministic coverage.

2. ITERATION-LOOP BEHAVIOR UNDER REAL LOAD — these tables are 143-150
   rows, well past the sample size (20), so unlike the unit tests
   (which use tiny tables and fake LLMs to force specific iteration
   counts), this reports what the loop ACTUALLY does against a real
   model on realistically-sized, realistically-messy data:
   semantic_iterations per run, and whether multiple cycles actually
   surfaced additional true positives or just spent extra calls.

3. CUSTOM-INSTRUCTION RELIABILITY — a small battery of plain-English
   instructions per dataset, run through RuleCompiler directly (cheap:
   one LLM call each, no full scan needed to judge classification
   accuracy), checked against a hand-labeled expected kind
   (structured / semantic / should-fail-on-hallucinated-field).

Two runtime modes:
  - No GROQ_API_KEY / GEMINI_API_KEY set: only section 1's
    DETERMINISTIC-ONLY baseline runs (no LLM calls needed for that —
    it's pure Check execution). Sections needing the LLM print a clear
    "skipped — set API keys" notice and are left out of the report
    rather than silently reporting zeros.
  - Both keys set: the full three-section eval runs for real, against
    real Groq + Gemini calls. This costs real API usage — small
    (roughly a dozen calls total across 3 datasets) but not free.

Run from service/, with keys set if you want the full eval:
    PYTHONPATH=. python3 eval/run_eval.py
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.agent.generator import GeneratorAgent, ScanResult
from app.canonical.models import CanonicalTable, ScanContext
from app.connectors.base import ConnectorConfig
from app.connectors.csv_connector import CSVConnector
from app.rules.compiler import RuleCompiler

DATASETS_DIR = Path(__file__).parent / "datasets"

# Batch-size sweep support: set SEMANTIC_SAMPLE_SIZE to override the
# semantic reasoner's default rows-per-LLM-call (20) for this run, e.g.
#   SEMANTIC_SAMPLE_SIZE=100 PYTHONPATH=. python3 eval/run_eval.py
# Output files get a suffix per size (eval_report_ss100.md, etc.) so
# sweeping 20 -> 50 -> 100 -> 200 never overwrites or resume-skips a
# previous size's results — each size's numbers are directly
# comparable side by side afterward, in their own files.
_SAMPLE_SIZE_OVERRIDE = os.environ.get("SEMANTIC_SAMPLE_SIZE")
_SAMPLE_SIZE: int | None = int(_SAMPLE_SIZE_OVERRIDE) if _SAMPLE_SIZE_OVERRIDE else None
_FILE_SUFFIX = f"_ss{_SAMPLE_SIZE}" if _SAMPLE_SIZE is not None else ""
REPORT_PATH = Path(__file__).parent / f"eval_report{_FILE_SUFFIX}.md"
RESULTS_JSON_PATH = Path(__file__).parent / f"eval_results{_FILE_SUFFIX}.json"

_HAS_KEYS = bool(os.environ.get("GROQ_API_KEY")) and bool(os.environ.get("GEMINI_API_KEY"))

# --- Result caching, so iterating on ONE axis (e.g. sweeping batch size)
# never re-spends budget on sections that axis can't affect ---
#
# The resume logic above (RESULTS_JSON_PATH) already avoids re-running a
# dataset that already succeeded AT THIS SAMPLE SIZE. But the
# instruction battery doesn't depend on sample size at all — it's a
# direct RuleCompiler call, unrelated to the semantic reasoner's batch
# size — so sweeping 20 -> 50 -> 100 -> 200 was re-paying for the exact
# same 3-9 compiler calls per dataset, every single sweep step, for a
# result that literally cannot change with sample size. That's the
# single highest-leverage waste identified: iterating on the thing
# you're actually trying to tune (batch size) was burning the daily
# token budget on the thing you're not tuning (compiler classification).
#
# _CACHE_PATH is deliberately NOT suffixed per sample size — one cache,
# shared across every SEMANTIC_SAMPLE_SIZE value, since its contents
# don't vary with that setting.
_CACHE_PATH = Path(__file__).parent / "eval_cache.json"

# Bump this whenever a prompt that affects LLM output changes (the
# compiler's system prompt, the semantic reasoner's prompt, etc.) — it's
# stored alongside every cached/resumed result below, so a prompt change
# invalidates old entries instead of silently reusing output that was
# never actually produced by the prompt currently in the code.
_PROMPT_VERSION = 1


def _dataset_content_hash(case: "DatasetCase") -> str:
    """Hash of the raw CSV bytes behind a dataset case. Both the
    instruction-battery cache and the full-agentic resume check use
    this (together with _PROMPT_VERSION) to make sure a cached/resumed
    result actually still corresponds to the dataset and prompt on
    disk right now — editing a dataset or a prompt invalidates any
    prior result instead of silently reusing a stale one."""
    h = hashlib.sha256()
    for csv_name in [case.primary_csv, *case.related_csvs]:
        h.update((DATASETS_DIR / f"{csv_name}.csv").read_bytes())
    return h.hexdigest()[:16]


def _load_instruction_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}  # corrupt or unreadable — just start fresh, not fatal


def _cached_instruction_battery(cache: dict, case: "DatasetCase", content_hash: str) -> list | None:
    entry = cache.get(case.name)
    if not entry:
        return None
    if entry.get("content_hash") != content_hash or entry.get("prompt_version") != _PROMPT_VERSION:
        return None  # dataset or prompt changed since this was cached — stale, don't reuse
    return entry.get("results")


def _store_instruction_battery(cache: dict, case: "DatasetCase", content_hash: str, results: list) -> None:
    cache[case.name] = {
        "content_hash": content_hash,
        "prompt_version": _PROMPT_VERSION,
        "results": results,
    }
    _CACHE_PATH.write_text(json.dumps(cache, indent=2))


def _load_table(csv_name: str) -> CanonicalTable:
    config = ConnectorConfig(tenant_id="eval", source_id="eval", settings={"file_path": str(DATASETS_DIR / f"{csv_name}.csv")})
    tables = CSVConnector(config).extract()
    return tables[0]


@dataclass
class DatasetCase:
    name: str
    primary_csv: str
    related_csvs: list[str]
    ground_truth: dict
    # (instruction text, expected kind, short label)
    instruction_battery: list[tuple[str, str, str]] = field(default_factory=list)


def _prf1(predicted: set[int], actual: set[int], universe_size: int) -> dict:
    tp = len(predicted & actual)
    fp = len(predicted - actual)
    fn = len(actual - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "true_positives": tp, "false_positives": fp, "false_negatives": fn,
        "precision": round(precision, 3), "recall": round(recall, 3), "f1": round(f1, 3),
        "predicted_count": len(predicted), "actual_count": len(actual), "universe_size": universe_size,
    }


def _cases() -> list[DatasetCase]:
    cases = []
    for name, primary, related in [
        ("crm_leads", "leads", ["accounts"]),
        ("ecommerce_orders", "orders", ["customers"]),
        ("hr_employees", "employees", []),
    ]:
        gt = json.loads((DATASETS_DIR / f"{name}_ground_truth.json").read_text())
        cases.append(DatasetCase(name=name, primary_csv=primary, related_csvs=related, ground_truth=gt))

    # Hand-labeled instruction batteries: (instruction, expected compiler
    # "kind", short label). A mix of clearly-structured, clearly-semantic,
    # and one instruction referencing a field that doesn't exist in the
    # schema.
    #
    # That third case's expected kind is "semantic", NOT "failed" — this
    # was originally mislabeled "failed" in this harness before a live
    # run against real Groq caught the mismatch. Re-reading the
    # compiler's own system prompt (app/rules/compiler.py) settled it:
    # "If the instruction refers to something not in the schema, treat
    # it as SEMANTIC instead." "failed" is reserved for when the model
    # itself misbehaves — emits kind=structured but names a bad field
    # anyway, which _parse_structured then catches — not the documented,
    # correct response to a hallucinated-field instruction. Routing a
    # hallucinated field to semantic guidance (safe, no fabricated rule)
    # rather than "failed" (a dead end for the user) is the intended,
    # safer behavior, and the real Groq run confirmed the compiler does
    # exactly that consistently across all three datasets.
    cases[0].instruction_battery = [
        ("Flag any lead with an amount greater than 100000", "structured", "structured: numeric threshold"),
        ("Flag anything that seems inconsistent with this account's industry", "semantic", "semantic: judgment call"),
        ("Flag leads where the moon_phase field is waning", "semantic", "semantic: field not in schema, safe fallback"),
    ]
    cases[1].instruction_battery = [
        ("Flag any order where amount is less than 0", "structured", "structured: numeric comparison"),
        ("Flag orders where the product doesn't match its stated category", "semantic", "semantic: judgment call"),
        ("Flag orders where shipping_carrier is FedEx", "semantic", "semantic: field not in schema, safe fallback"),
    ]
    cases[2].instruction_battery = [
        ("Flag any employee with salary greater than 500000", "structured", "structured: numeric threshold"),
        ("Flag anyone whose job title doesn't fit their department", "semantic", "semantic: judgment call"),
        ("Flag employees where the security_clearance field is missing", "semantic", "semantic: field not in schema, safe fallback"),
    ]
    return cases


def _run_deterministic_baseline(table: CanonicalTable, context: ScanContext | None, gt: dict) -> dict:
    result = GeneratorAgent().run(table, context)
    predicted = set(result.flagged_row_indices)
    deterministic_actual = set(gt["deterministic_rows"])
    return {
        "flagged_row_indices": sorted(predicted),
        "vs_deterministic_issues": _prf1(predicted, deterministic_actual, gt["total_rows"]),
        # Sanity check: the deterministic-only run should NOT catch
        # semantic-only rows — if it does, either a check overreached
        # or a "semantic-only" row accidentally trips a deterministic
        # check (a dataset design bug worth knowing about).
        "false_catches_on_semantic_only_rows": sorted(predicted & set(gt["semantic_only_rows"])),
    }


def _run_full_agentic(table: CanonicalTable, context: ScanContext | None, gt: dict):
    # This harness calls GeneratorAgent directly, bypassing main.py's
    # HTTP layer entirely — so there's no literal ai_review_consent
    # flag to pass (that gate lives in main.py's _configure_llms, not
    # in GeneratorAgent itself). Constructing GroqClient()/GeminiClient()
    # here IS this harness's explicit, deliberate equivalent of
    # consent: running an evaluation against real row data against the
    # real semantic-reasoning pipeline is the whole point of this
    # script, run by a developer who already knows that's what it
    # does — not an accidental bypass of the API's default-OFF gate,
    # which exists to protect an unattended caller who never made that
    # choice.
    from app.agent.llm_clients import GeminiClient, GroqClient

    start = time.monotonic()
    result: ScanResult = GeneratorAgent().run(
        table,
        context,
        llm=GroqClient(),
        verifier_llm=GeminiClient(),
        semantic_sample_size=_SAMPLE_SIZE,
    )
    elapsed = time.monotonic() - start

    predicted = set(result.flagged_row_indices)
    all_actual = set(gt["all_issue_rows"])
    semantic_only_actual = set(gt["semantic_only_rows"])

    return {
        "elapsed_seconds": round(elapsed, 2),
        "semantic_iterations": result.semantic_iterations,
        "semantic_flags_raised": len(result.semantic_flags),
        "verified_flags": len(result.verified_flags),
        "flagged_row_indices": sorted(predicted),
        "vs_all_issues": _prf1(predicted, all_actual, gt["total_rows"]),
        # Recall specifically on the rows that ONLY the semantic layer
        # could ever catch — this isolates the LLM layer's contribution
        # from the deterministic checks' contribution to the headline
        # "vs_all_issues" number above.
        "semantic_only_recall": round(
            len(predicted & semantic_only_actual) / len(semantic_only_actual), 3
        ) if semantic_only_actual else None,
        # Was previously silently dropped — a scan that stopped early
        # (semantic_iterations=0 with zero flags, say) looked identical
        # to "the table was genuinely clean" without this. Surfacing
        # the real underlying exception text here is what a rerun needs
        # to actually diagnose a failure instead of guessing at it.
        "semantic_coverage_warning": result.semantic_coverage_warning,
    }


def _run_instruction_battery(table: CanonicalTable, battery: list[tuple[str, str, str]]):
    from app.agent.llm_clients import GroqClient

    compiler = RuleCompiler(GroqClient())
    results = []
    for instruction, expected_kind, label in battery:
        compilation = compiler.compile(table, instruction)
        results.append({
            "label": label,
            "instruction": instruction,
            "expected_kind": expected_kind,
            "actual_kind": compilation.kind,
            "matched_expectation": compilation.kind == expected_kind,
            "description": compilation.description,
            # Previously dropped — a "failed" kind's actual cause
            # (malformed model output vs. LLMUnavailableError vs. an
            # unknown field) was invisible in the report; this is
            # CompilationResult's own error field, unfiltered.
            "error": compilation.error,
        })
    return results


def _dataset_already_done(dataset_result: dict | None, content_hash: str) -> bool:
    """A dataset counts as done from a prior run only if it has no
    recorded error, — when keys are set now — actually has the
    LLM-dependent sections filled in, AND was produced against the same
    dataset content and prompt version as right now. This is what makes
    a rerun after a crash cheap: a dataset that already succeeded is
    skipped entirely (no repeat API calls), one that errored, was only
    ever run without keys, or was run against a dataset/prompt that has
    since changed is redone."""
    if not dataset_result or dataset_result.get("error"):
        return False
    if _HAS_KEYS and "full_agentic" not in dataset_result:
        return False
    if _HAS_KEYS and (
        dataset_result.get("content_hash") != content_hash
        or dataset_result.get("prompt_version") != _PROMPT_VERSION
    ):
        return False
    return True


def _report_for_dataset(case: DatasetCase, dataset_result: dict) -> list[str]:
    lines: list[str] = [f"## {case.name}", ""]
    lines.append(f"Total rows: {case.ground_truth['total_rows']}, planted issues: "
                 f"{len(case.ground_truth['all_issue_rows'])} "
                 f"({len(case.ground_truth['deterministic_rows'])} deterministic, "
                 f"{len(case.ground_truth['semantic_only_rows'])} semantic-only)")
    lines.append("")

    if dataset_result.get("error"):
        lines.append(f"**Run failed partway through:** {dataset_result['error']}")
        lines.append("")
        if "deterministic_baseline" not in dataset_result:
            return lines  # nothing else completed for this dataset

    baseline = dataset_result.get("deterministic_baseline")
    if baseline:
        b = baseline["vs_deterministic_issues"]
        lines.append(
            f"**Deterministic baseline** (no LLM): precision {b['precision']}, recall {b['recall']}, "
            f"F1 {b['f1']} against the {b['actual_count']} deterministically-catchable planted issues."
        )
        if baseline["false_catches_on_semantic_only_rows"]:
            lines.append(
                f"  - Note: also flagged {len(baseline['false_catches_on_semantic_only_rows'])} "
                f"semantic-only row(s) via deterministic checks alone — worth checking why."
            )
        lines.append("")

    if "full_agentic" in dataset_result:
        fa = dataset_result["full_agentic"]
        a = fa["vs_all_issues"]
        lines.append(
            f"**Full agentic run** (Groq generator + Gemini verifier, {fa['elapsed_seconds']}s): "
            f"precision {a['precision']}, recall {a['recall']}, F1 {a['f1']} against all "
            f"{a['actual_count']} planted issues. Semantic-only recall: {fa['semantic_only_recall']} "
            f"({fa['semantic_flags_raised']} semantic flags raised, {fa['verified_flags']} verified, "
            f"{fa['semantic_iterations']} reasoning cycle(s) run)."
        )
        if fa.get("semantic_coverage_warning"):
            lines.append(f"  - **Coverage warning:** {fa['semantic_coverage_warning']}")
        lines.append("")
    elif not dataset_result.get("error"):
        lines.append("*(LLM-dependent sections skipped — no API keys set)*")
        lines.append("")

    if "instruction_battery" in dataset_result:
        lines.append("**Custom-instruction battery:**")
        lines.append("")
        for r in dataset_result["instruction_battery"]:
            mark = "correct" if r["matched_expectation"] else "**MISMATCH**"
            line = (
                f"  - {r['label']} ({mark}): \"{r['instruction']}\" -> "
                f"expected `{r['expected_kind']}`, got `{r['actual_kind']}`"
            )
            if r.get("error"):
                line += f" — {r['error']}"
            lines.append(line)
        lines.append("")

    return lines


def _write_reports(all_results: dict, cases: list[DatasetCase]) -> None:
    """Rebuilds and overwrites both output files from all_results as it
    currently stands — called after EVERY dataset (success or failure),
    not just once at the end, so a crash partway through never loses
    results a prior dataset already earned."""
    report_lines: list[str] = ["# DBCaaS Agentic Evaluation Report", ""]
    report_lines.append(
        "> **Read this as a regression check, not an accuracy claim.** The synthetic datasets "
        "here do not contain the edge cases recent fixes target (leading-zero zip codes, "
        "legitimate `NA`/`None` values, mixed-type columns, PII inside free text), so a perfect "
        "score means nothing regressed on the planted issues — not that those cases are measured.\n"
    )
    if not _HAS_KEYS:
        report_lines.append(
            "> **GROQ_API_KEY / GEMINI_API_KEY not set** — only the deterministic-only "
            "baseline ran (no LLM calls needed for that). Set both keys and rerun for "
            "detection accuracy on semantic-only issues, iteration-loop behavior, and "
            "custom-instruction reliability.\n"
        )
    for case in cases:
        dataset_result = all_results["datasets"].get(case.name)
        if dataset_result is None:
            continue  # not reached yet this run
        report_lines.extend(_report_for_dataset(case, dataset_result))

    RESULTS_JSON_PATH.write_text(json.dumps(all_results, indent=2))
    REPORT_PATH.write_text("\n".join(report_lines))


def main() -> None:
    cases = _cases()
    all_results: dict = {"has_llm_keys": _HAS_KEYS, "datasets": {}}

    from app.agent.generator import _DEFAULT_PRODUCTION_SAMPLE_SIZE
    effective_sample_size = _SAMPLE_SIZE if _SAMPLE_SIZE is not None else _DEFAULT_PRODUCTION_SAMPLE_SIZE
    print(f"Semantic reasoner batch size: {effective_sample_size} rows/call"
          f"{' (override via SEMANTIC_SAMPLE_SIZE)' if _SAMPLE_SIZE is None else ' (from SEMANTIC_SAMPLE_SIZE)'}")
    print(f"Writing to: {REPORT_PATH.name} / {RESULTS_JSON_PATH.name}")

    # Resume support: if a previous run already got partway through and
    # left a results file, pick it up rather than starting from zero —
    # this is what makes a retry after a transient API error (rate
    # limit, "server busy", etc.) cheap instead of re-spending every
    # already-successful dataset's worth of API calls.
    if RESULTS_JSON_PATH.exists():
        try:
            prior = json.loads(RESULTS_JSON_PATH.read_text())
            all_results["datasets"] = prior.get("datasets", {})
        except (json.JSONDecodeError, OSError):
            pass  # corrupt or unreadable — just start fresh, not fatal

    instruction_cache = _load_instruction_cache()
    cache_hits = 0

    for case in cases:
        content_hash = _dataset_content_hash(case)
        prior_result = all_results["datasets"].get(case.name)
        if _dataset_already_done(prior_result, content_hash):
            print(f"\n=== {case.name} === (already completed in a prior run — skipping, no API calls spent)")
            continue

        print(f"\n=== {case.name} ===")
        dataset_result: dict = {}
        try:
            table = _load_table(case.primary_csv)
            related_tables = [_load_table(name) for name in case.related_csvs]
            context = ScanContext(tables=[table, *related_tables]) if related_tables else None

            print("  running deterministic-only baseline...")
            baseline = _run_deterministic_baseline(table, context, case.ground_truth)
            dataset_result["deterministic_baseline"] = baseline
            print(f"    vs deterministic issues: {baseline['vs_deterministic_issues']}")
            if baseline["false_catches_on_semantic_only_rows"]:
                print(f"    NOTE: deterministic checks also caught semantic-only rows: {baseline['false_catches_on_semantic_only_rows']}")

            if _HAS_KEYS:
                print("  running full agentic pipeline (real Groq + Gemini)...")
                dataset_result["full_agentic"] = _run_full_agentic(table, context, case.ground_truth)
                dataset_result["content_hash"] = content_hash
                dataset_result["prompt_version"] = _PROMPT_VERSION
                print(f"    semantic_iterations={dataset_result['full_agentic']['semantic_iterations']}, "
                      f"vs all issues: {dataset_result['full_agentic']['vs_all_issues']}, "
                      f"semantic-only recall: {dataset_result['full_agentic']['semantic_only_recall']}")
                if dataset_result["full_agentic"].get("semantic_coverage_warning"):
                    print(f"    COVERAGE WARNING: {dataset_result['full_agentic']['semantic_coverage_warning']}")

                # Instruction battery is independent of SEMANTIC_SAMPLE_SIZE —
                # it never needs to be re-run just because a batch-size sweep
                # is in progress, only when the dataset or the compiler
                # prompt actually changes.
                cached_battery = _cached_instruction_battery(instruction_cache, case, content_hash)
                if cached_battery is not None:
                    cache_hits += 1
                    print("  instruction battery: reusing cached result "
                          "(dataset + prompt unchanged since last run — no API calls spent)")
                    dataset_result["instruction_battery"] = cached_battery
                else:
                    print("  running custom-instruction battery...")
                    dataset_result["instruction_battery"] = _run_instruction_battery(table, case.instruction_battery)
                    _store_instruction_battery(instruction_cache, case, content_hash, dataset_result["instruction_battery"])
                for r in dataset_result["instruction_battery"]:
                    status = "OK" if r["matched_expectation"] else "MISMATCH"
                    print(f"    [{status}] {r['label']}: expected={r['expected_kind']} actual={r['actual_kind']}")
        except Exception as exc:  # noqa: BLE001 - deliberately broad: a
            # transient API failure (rate limit, "server busy", model
            # error) on ANY dataset must not crash the whole run and
            # lose every other dataset's results — record it, save
            # what we have, and move on to the next dataset instead.
            dataset_result["error"] = f"{type(exc).__name__}: {exc}"
            print(f"    FAILED: {dataset_result['error']}")
            print("    (results for datasets already completed this run are still saved — rerun to retry just this one)")

        all_results["datasets"][case.name] = dataset_result
        _write_reports(all_results, cases)  # save immediately, not just at the end

    print(f"\nWrote {REPORT_PATH} and {RESULTS_JSON_PATH}")
    if cache_hits:
        print(f"Instruction battery reused from cache for {cache_hits} dataset(s) — "
              f"{cache_hits * 3} fewer compiler calls spent this run (see {_CACHE_PATH.name}).")
    failed = [name for name, r in all_results["datasets"].items() if r.get("error")]
    if failed:
        print(f"Datasets that failed and still need a retry: {', '.join(failed)} — just rerun this script, "
              f"already-completed datasets will be skipped automatically.")


if __name__ == "__main__":
    main()
