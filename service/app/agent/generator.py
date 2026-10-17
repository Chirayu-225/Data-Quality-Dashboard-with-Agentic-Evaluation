"""
Generator agent — planner + tool execution.

This is deliberately the FIRST increment of the agent, with no LLM
involved yet. The "plan" step here is rule-based: ask every registered
check whether it applies to this table (via `applies_to()`), and run
the ones that do. This is intentionally the same orchestration Phase 1
would have needed for a standalone scan endpoint — building it here
avoids building it twice.

The LLM semantic reasoning layer gets added as a second increment on
top of this, once API keys are wired in. When that happens, `plan()`
is the method that grows — swapping "run every applicable check" for
"reason about which checks matter and whether semantic judgment is
also needed" — everything downstream (execute, result aggregation)
stays the same, which is the point of separating these steps.
"""

from __future__ import annotations

import logging

from typing import Callable

from pydantic import BaseModel

from app.agent.llm_clients import LLMClient, LLMRequestTooLargeError, LLMUnavailableError
from app.agent.pii_masking import mask_table_for_llm
from app.agent.local_value_review import LocalReviewResult, review_table_locally
from app.agent.value_ai_review import ValueAIResult, review_values_with_ai
from app.agent.semantic_reasoning import SemanticFlag, SemanticReasoner
from app.agent.verifier import VerificationLabel, VerifiedFlag, Verifier
from app.canonical.models import CanonicalTable, ScanContext
from app.checks.base import Check, CheckResult, all_checks
from app.rules.compiler import CompilationResult, RuleCompiler
from app.rules.executor import execute_rule

# Semantic coverage policy: by default, the semantic layer reasons
# over EVERY row of the table, not a bounded sample. This was a
# deliberate redesign, not the original design — an earlier version
# capped at 3 cycles of 20 rows and only kept going past the first
# cycle if that cycle's flag ratio looked suspicious. A real
# evaluation run against forged ground-truth data (see
# eval/run_eval.py) showed what that actually meant in practice: on a
# 150-row table, whole regions were structurally never reasoned over
# at all, every single run, regardless of iteration count — not a
# tuning problem, a coverage ceiling. Since genuine LLM semantic
# judgment (not just deterministic rule-checking) is the product's
# stated differentiator against Validity/RingLead/Cloudingo/Insycle, a
# detection layer that only ever looks at part of the data undercuts
# that claim no matter how well the sampling is tuned. `max_llm_calls`
# below is the explicit, opt-in way a caller trades coverage for cost
# on a very large table — the method itself never silently caps.

# Default rows-per-batch for a real scan (distinct from
# SemanticReasoner's own bare-constructor default of 20, which exists
# for tests/direct use and is left alone). This number comes from a
# live batch-size sweep (eval/run_eval.py) against 3 forged datasets:
# size 20 (the original guess) gave recall 0.756-0.896; size 100 hit
# Groq's per-request token cap and auto-shrank (see
# LLMRequestTooLargeError in llm_clients.py) down to roughly 48-50
# rows/call in practice, and recall at that landing size was as good
# or better across all 3 datasets (0.854-1.0), likely because bigger
# batches give the model more rows to cross-reference against each
# other, which its own prompt asks it to do. 45 is a deliberately
# round, slightly conservative pick near that observed landing zone —
# not the literal number any one run converged on, since that varies
# by dataset content length and this needs to hold up across datasets
# this project hasn't tested yet. The shrink-on-413 safety net stays
# in place regardless, so an unusually verbose dataset still degrades
# gracefully instead of failing outright.
_DEFAULT_PRODUCTION_SAMPLE_SIZE = 45

_log = logging.getLogger(__name__)


class ScanResult(BaseModel):
    """The generator agent's output for one table: every deterministic
    check that ran (including a compiled custom rule, if one applied),
    any semantic flags from the LLM layer, any verification results,
    the compilation outcome for a custom instruction (if one was
    given, so a caller can show "here's what I understood" even when
    the eventual UI for confirm-before-run doesn't exist yet), and a
    de-duplicated union of every flagged row index.

    When verification ran, REJECTED semantic flags are excluded from
    `flagged_row_indices` — an unverified claim from the generator
    alone is not treated the same as a check-passed result. NEEDS_REVIEW
    flags stay included; they're uncertain, not dismissed.

    `semantic_coverage_warning` is set (non-None) only when the LLM
    provider became unavailable partway through covering the table —
    every retry on some batch failed, so the loop stopped early rather
    than crash the whole scan. Deterministic results and whatever
    semantic coverage was gathered before that point are still real
    and still returned; this field is what tells a caller the semantic
    layer's coverage is honestly incomplete, not silently partial."""

    table_name: str
    source_id: str
    check_results: list[CheckResult]
    semantic_flags: list[SemanticFlag] = []
    # Present only when the local (no-LLM) value review ran. Carries the
    # column/value counts the egress ledger and UI report; never row data.
    local_review: LocalReviewResult | None = None
    value_ai_review: ValueAIResult | None = None
    verified_flags: list[VerifiedFlag] = []
    compilation: CompilationResult | None = None
    flagged_row_indices: list[int]
    semantic_iterations: int = 0
    semantic_coverage_warning: str | None = None
    # Rows the deterministic prompt-injection backstop
    # (app/checks/injection_detection.py) flagged with at least one
    # HIGH-confidence manipulation signal — a SUBSET of
    # flagged_row_indices, not a separate universe (a quarantined row
    # is still visible in the ordinary review list; it just never
    # contributes to any quality score — see app/scoring/scorer.py,
    # which skips this check_name entirely when building Findings —
    # and is structurally excluded from ever being sampled into a
    # semantic-reasoning batch, see `run` below). A row whose ONLY
    # injection-check match was LOW-confidence (ambiguous business
    # language — see injection_detection.py's module docstring) is
    # NOT in this list: it's still reported (a low-severity
    # SecurityFinding, see main.py) but stays fully eligible for
    # normal semantic review, since an ambiguous phrase alone must
    # never be a free pass out of the one layer that catches what a
    # regex can't.
    quarantined_row_indices: list[int] = []
    # The real count of rows the semantic layer reliably reasoned over
    # this scan — see _run_semantic_with_iteration's own docstring for
    # exactly what's excluded (quarantined rows never sampled; rows a
    # budget cap or provider outage never got to; rows whose batch came
    # back unparseable). This is the ONLY honest denominator for
    # ACCURACY (see app/scoring/scorer.py) — `total_rows -
    # quarantined_row_count` alone still overcounts whenever coverage
    # stopped early or a batch failed to parse, silently crediting an
    # unreasoned row as "evaluated and clean." Stays 0 when no LLM was
    # configured at all (no semantic pass ever ran).
    semantic_rows_considered: int = 0

    @property
    def total_flagged(self) -> int:
        return len(self.flagged_row_indices)


class GeneratorAgent:
    """Plan + execute, over the deterministic check registry, plus an
    optional semantic reasoning layer when an LLM client is supplied.
    Without an `llm`, this behaves exactly as the rule-based-only
    version did — the semantic layer is additive, never required."""

    def plan(self, table: CanonicalTable, context: ScanContext | None = None) -> list[Check]:
        """Which deterministic checks apply to this table. Still
        rule-based — every registered check gets asked `applies_to()`.
        This governs the deterministic layer only; the semantic layer
        below always runs (on a sample) when an LLM client is given,
        since judgment calls aren't a yes/no 'does this apply'
        question the way a deterministic check's relevance is."""
        instances = [check_cls() for check_cls in all_checks()]
        return [c for c in instances if c.applies_to(table, context)]

    def execute(
        self,
        table: CanonicalTable,
        checks: list[Check],
        context: ScanContext | None = None,
        on_check_done: Callable[[int, int], None] | None = None,
    ) -> list[CheckResult]:
        """Run each planned check as a 'tool' and collect its result.
        `on_check_done(done, total)`, when given, is called after each."""
        results: list[CheckResult] = []
        for check in checks:
            results.append(check.run(table, context))
            if on_check_done is not None:
                on_check_done(len(results), len(checks))
        return results

    def _run_semantic_with_iteration(
        self,
        table: CanonicalTable,
        llm: LLMClient,
        instruction: str | None,
        max_llm_calls: int | None = None,
        sample_size: int | None = None,
        quarantined_indices: set[int] | None = None,
        progress_callback: Callable[..., None] | None = None,
    ) -> tuple[list[SemanticFlag], int, str | None, int]:
        """Batches through the semantic reasoner until every row in the
        table has been sampled at least once — full coverage by
        default, not a bounded look. Each cycle explicitly excludes
        every index already sampled, so every batch is genuinely new
        rows the layer hasn't judged yet; the loop stops only once
        nothing fresh is left to offer (or the optional cost cap below
        is hit).

        `max_llm_calls`, when given, is an explicit opt-in ceiling on
        how many batches run — a caller's deliberate cost/latency
        tradeoff for a very large table, never something this method
        decides on its own. None (the default) means no ceiling: every
        row gets reasoned over.

        If a batch's LLM call fails every retry attempt
        (`LLMUnavailableError` — see `LLMClient.complete_with_retry`),
        the loop stops there rather than crashing the whole scan.
        Whatever flags and coverage earlier batches already produced
        are kept and returned as-is; the third return value carries a
        human-readable warning explaining coverage stopped early, so
        the caller can be honest about it rather than presenting
        partial coverage as if it were complete.

        Verification still happens exactly once, over the union of
        everything every batch in this loop produced, by the caller.

        `sample_size`, when given, overrides SemanticReasoner's default
        rows-per-batch (20) — this is the knob for the empirical
        batch-size sweep against the eval harness: run the same
        dataset at a few sizes, compare precision/recall, pick the
        largest size where detection quality hasn't measurably
        dropped, rather than guessing a number.

        `quarantined_indices`, when given, seeds `sampled_indices` up
        front rather than changing `total_rows`. These are rows the
        deterministic prompt-injection check flagged with a
        HIGH-confidence manipulation signal specifically (see
        app/checks/injection_detection.py's two-tier design) — they
        must never be sent to an LLM prompt, full stop, not merely
        excluded from the score. A LOW-confidence-only match is
        deliberately NOT in this set — see GeneratorAgent.run's own
        comment on why an ambiguous phrase must not blind semantic
        review. Pre-seeding accomplishes containment
        (SemanticReasoner.run's `exclude_indices` guarantees a
        quarantined row is never selected into any batch) while also
        keeping the loop's own termination condition correct: since
        `total_rows` already implicitly expects every row to be
        accounted for exactly once, a quarantined row counting as
        "already accounted for" from the very first iteration is what
        makes `len(sampled_indices) < total_rows` become false at the
        right point, instead of looping forever hunting for rows it's
        mathematically forbidden to ever pick. `total_rows` itself is
        deliberately left as the table's real row count — see `run()`
        below for how the gap this creates (fewer rows actually
        reasoned over than the file contains) is surfaced honestly to
        the API caller rather than hidden by shrinking this number.

        The 4th return value, `rows_reliably_considered`, is the count
        `progress_callback`, when given, is called `(rows_sampled_so_far,
        total_rows)` after EVERY batch — including a batch whose model
        response failed to parse, since that batch still consumed real
        wall-clock time the caller's progress bar should reflect, even
        though it didn't yield reliable flags. This is this project's
        only hook for a long scan's progress — see app/jobs.py, which is
        the actual reason this exists: a 5,000-row semantic scan can be
        a hundred-plus paced LLM calls (many minutes), and a caller
        polling a job needs SOMETHING better than silence the whole
        time. Never raises on its own account; a callback that itself
        raises is a caller bug, not something this method guards against
        today.

        this method actually knows to be true: rows that came back
        from a real, successfully-parsed batch. It deliberately
        EXCLUDES the pre-seeded quarantined rows (never actually
        sampled — see above), any row a batch never got to before an
        opt-in `max_llm_calls` cap or a provider outage stopped the
        loop early, and any row whose batch came back unparseable
        (`parse_failed` — see SemanticReasoningResult). A prior version
        of this code let the caller derive ACCURACY's denominator as
        `total_rows - quarantined_row_count` alone, which is what this
        return value replaces: that formula still credited a
        budget-capped, outage-interrupted, or unparseable row as
        "reasoned over and clean," which is exactly Bug 1's failure
        mode reproduced one level down. Use THIS number, not a
        re-derived one, as the real semantic denominator."""
        reasoner = SemanticReasoner(llm, **({"sample_size": sample_size} if sample_size else {}))
        all_flags: list[SemanticFlag] = []
        sampled_indices: set[int] = set(quarantined_indices or ())
        # Rows that came back from a genuinely successful, parsed batch
        # — the only rows this method can honestly call "reasoned over
        # and clean (or flagged)". See rows_reliably_considered below.
        reliably_reasoned_indices: set[int] = set()
        # Rows a batch attempted but whose model output didn't parse —
        # tracked SEPARATELY from sampled_indices below. These rows
        # still count toward "attempted" so the loop terminates rather
        # than retrying the exact same unparseable batch forever, but
        # they are NOT treated as "reasoned over and clean" — see the
        # coverage_warning built after the loop.
        unparsed_indices: set[int] = set()
        calls_made = 0
        total_rows = len(table.rows)
        coverage_warning: str | None = None

        while len(sampled_indices) < total_rows:
            if max_llm_calls is not None and calls_made >= max_llm_calls:
                break

            try:
                result = reasoner.run(table, instruction, exclude_indices=sampled_indices)
            except LLMUnavailableError as exc:
                rows_left = total_rows - len(sampled_indices)
                coverage_warning = (
                    f"Semantic reasoning stopped early after {calls_made} batch(es) "
                    f"({len(sampled_indices)}/{total_rows} rows covered) — the LLM "
                    f"provider was unavailable after retrying: {exc}. {rows_left} "
                    f"row(s) were not reasoned over this scan."
                )
                break
            except LLMRequestTooLargeError as exc:
                # SemanticReasoner already tried shrinking its own batch
                # down to its floor size before giving up — reaching
                # here means even that floor was rejected as too large
                # (e.g. one row's own content is unusually huge), a
                # genuinely different situation from "provider is
                # down." Same fail-safe treatment either way: stop,
                # keep whatever coverage earlier batches already
                # produced, and say so honestly rather than pretending
                # this table has no more issues than what was found.
                rows_left = total_rows - len(sampled_indices)
                coverage_warning = (
                    f"Semantic reasoning stopped early after {calls_made} batch(es) "
                    f"({len(sampled_indices)}/{total_rows} rows covered) — even the "
                    f"smallest batch size was rejected as too large by the provider: "
                    f"{exc}. {rows_left} row(s) were not reasoned over this scan."
                )
                break
            calls_made += 1

            if result.rows_sampled == 0:
                # Defensive — exclude_indices already guarantees this
                # shouldn't happen while sampled_indices < total_rows,
                # but never spend a call's result on nothing to show
                # for it, and never loop forever if it somehow does.
                break

            _log.info(
                "semantic batch %d: rows=%d flags=%d parse_failed=%s",
                calls_made, result.rows_sampled, len(result.flags), result.parse_failed,
            )
            all_flags.extend(result.flags)
            sampled_indices.update(result.sampled_indices)
            if result.parse_failed:
                unparsed_indices.update(result.sampled_indices)
            else:
                reliably_reasoned_indices.update(result.sampled_indices)

            if progress_callback is not None:
                progress_callback(len(reliably_reasoned_indices), total_rows, len(unparsed_indices))

        if unparsed_indices:
            unparsed_note = (
                f"{len(unparsed_indices)} row(s) were sampled by the semantic layer but the "
                f"model's response for that batch couldn't be parsed — those rows were NOT "
                f"reliably judged and should not be read as confirmed-clean just because no "
                f"flag was returned for them."
            )
            coverage_warning = (
                f"{coverage_warning} {unparsed_note}" if coverage_warning else unparsed_note
            )

        return all_flags, calls_made, coverage_warning, len(reliably_reasoned_indices)

    def run(
        self,
        table: CanonicalTable,
        context: ScanContext | None = None,
        llm: LLMClient | None = None,
        custom_instruction: str | None = None,
        verifier_llm: LLMClient | None = None,
        max_semantic_llm_calls: int | None = None,
        semantic_sample_size: int | None = None,
        exclude_columns: set[str] | None = None,
        mask_salt: str = "",
        progress_callback: Callable[..., None] | None = None,
        stage_callback: Callable[..., None] | None = None,
        local_review: bool = False,
        ai_mode: str = "rows",
        shareable_columns: set[str] | None = None,
    ) -> ScanResult:
        """Plan then execute the deterministic checks. If `llm` is
        given:
        - No `custom_instruction`: autonomous semantic reasoning runs,
          same as before — the agent decides what looks wrong on its
          own.
        - `custom_instruction` given: it's compiled first. A
          STRUCTURED result is executed deterministically (precise,
          cheap, no semantic pass needed for it) and its flags are
          added directly to check_results — no LLM judgment call
          required for something a rule can already express exactly.
          A SEMANTIC result passes the compiler's refined description
          to the reasoning layer as guidance. A FAILED compilation
          falls back to passing the user's raw instruction straight
          to the reasoning layer, best-effort, rather than doing
          nothing with it.

        Semantic reasoning covers the WHOLE table by default (see
        `_run_semantic_with_iteration`) — `max_semantic_llm_calls`
        is the explicit opt-in to cap that for cost/latency on a very
        large table; leave it None for full coverage.

        If `verifier_llm` is ALSO given, semantic flags (autonomous or
        guided — never the deterministic custom-rule path, which needs
        no verification) get checked before counting.

        `exclude_columns`, when given, names additional columns (on top
        of the automatic email/phone masking every LLM-facing call
        already gets — see pii_masking.py) to mask out of every row
        before it reaches a semantic-reasoning or verification prompt.
        This governs ONLY what the LLM sees: the deterministic checks
        above and the real `table` this method returns are completely
        unaffected — a caller-excluded column still scores and still
        shows its real value in the final report, it just never leaves
        the server in a prompt.

        `mask_salt`, when given, is passed straight through to
        `mask_table_for_llm` — see that function's docstring for why
        this needs to be the SAME value across every table and every
        pass of one scan request (main.py generates it once per
        request): a value-keyed placeholder only preserves the
        duplicate/fraud correlation signal masking exists to protect
        when the same real value always hashes to the same token,
        which requires the same salt throughout.

        `progress_callback`, when given, is passed straight through to
        `_run_semantic_with_iteration` — see that method's docstring.
        Never called at all when semantic reasoning doesn't run (no
        `llm`, or the custom instruction compiled to a fully
        deterministic rule)."""
        def emit(stage: str, state: str, counts: dict[str, int] | None = None, reason: str | None = None) -> None:
            # Counts and short reasons only, never row content. The
            # callback may raise JobCancelled; let it propagate.
            if stage_callback is not None:
                stage_callback(stage, state, counts, reason)

        checks = self.plan(table, context)
        emit("checks", "running", {"checks_total": len(checks), "checks_done": 0, "rows": len(table.rows)})
        results = self.execute(
            table,
            checks,
            context,
            on_check_done=lambda done, total: emit("checks", "progress", {"checks_done": done, "checks_total": total}),
        )

        # The prompt-injection backstop is a security check, not a
        # data-quality one — scorer.py independently skips this
        # check_name entirely when building Findings, so NOTHING it
        # flags ever dilutes or fakes a quality score, regardless of
        # what follows here. But its flagged rows are NOT hidden from
        # the ordinary `flagged` union below — a row must never
        # silently disappear from the human-facing review list just
        # because its problem was a security one rather than a quality
        # one (a prior version of this code excluded it entirely here,
        # which meant a row whose ONLY issue was an injection match
        # could leave the report looking clean; fixed by including it
        # below like any other check's flags).
        #
        # `quarantined` is a NARROWER, separate set: only rows carrying
        # at least one HIGH-confidence match (see injection_detection.py's
        # module docstring on the two-tier design) — these are excluded
        # from semantic-reasoning sampling below, real containment, not
        # just a scoring exclusion. A LOW-confidence-only match (ordinary
        # business language like "pre-approved") is reported (visible in
        # `flagged`, and as a low-severity SecurityFinding — see main.py)
        # but does NOT pull the row out of semantic review — an
        # over-broad match here must never become a free pass around
        # the one detection layer built to catch what a regex can't.
        quarantined: set[int] = set()
        flagged: set[int] = set()
        for r in results:
            flagged.update(r.flagged_row_indices)
            if r.check_name == "prompt_injection_check":
                quarantined.update(
                    idx for idx in r.flagged_row_indices if r.row_severity.get(idx, "high") == "high"
                )

        semantic_flags: list[SemanticFlag] = []
        verified_flags: list[VerifiedFlag] = []
        compilation: CompilationResult | None = None
        semantic_iterations = 0
        semantic_coverage_warning: str | None = None
        semantic_rows_considered = 0

        emit(
            "checks",
            "done",
            {
                "checks_run": len(results),
                "rows_flagged": len(flagged),
                "rows_quarantined": len(quarantined),
            },
        )

        local_result: LocalReviewResult | None = None
        value_ai_result: ValueAIResult | None = None
        if llm is None:
            reason = "AI review not enabled for this scan"
            emit("instruction", "skipped", reason=reason if custom_instruction else "no custom instruction")
            if local_review:
                # Default semantic tier: judged value-by-value in this
                # process. No LLM, no network, nothing leaves the server.
                emit("semantic", "running", {"rows_total": len(table.rows), "rows_considered": 0})
                local_result = review_table_locally(table, exclude_indices=quarantined)
                semantic_flags = list(local_result.flags)
                semantic_iterations = 1
                semantic_rows_considered = local_result.rows_considered if local_result.columns_reviewed else 0
                flagged.update(f.row_index for f in semantic_flags)
                emit(
                    "semantic",
                    "done",
                    {
                        "rows_considered": semantic_rows_considered,
                        "flags_raised": len(semantic_flags),
                        "iterations": 1,
                        "distinct_values_scored": local_result.distinct_values_scored,
                    },
                    reason="local value review - no data left this machine",
                )
                emit("verify", "skipped", reason="local review has no external verifier")
            else:
                emit("semantic", "skipped", reason=reason)
                emit("verify", "skipped", reason=reason)
        else:
            # Built once per scan, not per batch — every call that
            # sends row content to an LLM (semantic reasoning,
            # verification) reads from this masked copy; the
            # compiler call just below and the deterministic
            # custom-rule execution it can trigger both keep using the
            # REAL `table`, since the compiler never sees row data at
            # all (only schema + the instruction text — see
            # RuleCompiler.compile) and a deterministic rule must
            # never evaluate a placeholder in place of a real value.
            masked_table = mask_table_for_llm(table, exclude_columns, salt=mask_salt)
            effective_instruction = custom_instruction

            if custom_instruction is None:
                emit("instruction", "skipped", reason="no custom instruction")
            else:
                emit("instruction", "running")
                compilation = RuleCompiler(llm).compile(table, custom_instruction)
                emit("instruction", "done", {"rule_compiled": 1 if compilation.kind != "failed" else 0},
                     reason=f"compiled as {compilation.kind}")
                if compilation.kind == "structured":
                    rule_result = execute_rule(table, compilation.compiled_rule)
                    results.append(rule_result)
                    flagged.update(rule_result.flagged_row_indices)
                    effective_instruction = None  # fully handled — no semantic pass needed
                elif compilation.kind == "semantic":
                    effective_instruction = compilation.semantic_instruction
                # "failed" -> effective_instruction stays as the raw
                # custom_instruction, best-effort fallback.

            # Run semantic reasoning unless the instruction was fully
            # handled deterministically above. Covers both paths that
            # can reach it — autonomous (no custom_instruction at all)
            # and instruction-guided (compiled to "semantic" or fell
            # back after a failed compilation) — through the one
            # shared `effective_instruction` value.
            if custom_instruction is None or effective_instruction is not None:
                # None means "no explicit override" — resolved here
                # (not left to SemanticReasoner's own bare-constructor
                # default of 20) so both an omitted argument AND an
                # explicit `semantic_sample_size=None` (e.g. the eval
                # harness's SEMANTIC_SAMPLE_SIZE-unset case) land on the
                # same evidence-based production default.
                effective_sample_size = (
                    semantic_sample_size
                    if semantic_sample_size is not None
                    else _DEFAULT_PRODUCTION_SAMPLE_SIZE
                )
                emit("semantic", "running", {"rows_total": len(table.rows), "rows_considered": 0})

                def _semantic_progress(considered: int, total: int, unreadable: int = 0) -> None:
                    emit(
                        "semantic",
                        "progress",
                        {"rows_considered": considered, "rows_total": total, "rows_unreadable": unreadable},
                    )
                    if progress_callback is not None:
                        progress_callback(considered, total, unreadable)

                if ai_mode == "values":
                    # Value-level AI review: only distinct values from columns the
                    # caller explicitly allowed leave the server. No rows, no
                    # neighbouring columns. Reads the REAL table, because it sends
                    # no row content; personal-data columns are refused inside.
                    value_ai_result = review_values_with_ai(
                        table,
                        llm,
                        shareable_columns=set(shareable_columns or set()),
                        exclude_indices=quarantined,
                        guidance=effective_instruction,
                    )
                    semantic_flags = list(value_ai_result.flags)
                    semantic_iterations = max(1, value_ai_result.calls_made)
                    semantic_rows_considered = value_ai_result.rows_considered
                    semantic_coverage_warning = None
                    if value_ai_result.unparseable_calls or value_ai_result.unavailable_calls:
                        semantic_coverage_warning = (
                            f"Value-level AI review: {value_ai_result.unparseable_calls} unparseable and "
                            f"{value_ai_result.unavailable_calls} failed call(s); those values were not judged."
                        )
                    if not value_ai_result.columns_shared:
                        semantic_coverage_warning = (
                            "Value-level AI review sent nothing: no shareable column was allowed or eligible. "
                            "Name columns in shareable_columns to include them."
                        )
                else:
                    semantic_flags, semantic_iterations, semantic_coverage_warning, semantic_rows_considered = (
                        self._run_semantic_with_iteration(
                            masked_table,
                            llm,
                            effective_instruction,
                            max_llm_calls=max_semantic_llm_calls,
                            sample_size=effective_sample_size,
                            quarantined_indices=quarantined,
                            progress_callback=_semantic_progress,
                        )
                    )
                emit(
                    "semantic",
                    "done",
                    {
                        "rows_considered": semantic_rows_considered,
                        "flags_raised": len(semantic_flags),
                        "iterations": semantic_iterations,
                    },
                )

                if local_review:
                    # Local value review runs alongside the LLM pass; its
                    # findings are added for rows the LLM did not already
                    # flag, so enabling AI never loses a local finding.
                    local_result = review_table_locally(table, exclude_indices=quarantined)
                    already = {f.row_index for f in semantic_flags}
                    semantic_flags = semantic_flags + [f for f in local_result.flags if f.row_index not in already]
                    semantic_iterations = max(semantic_iterations, 1)

                if ai_mode == "values":
                    # The row-level verifier would need row content, which this
                    # mode exists to avoid sending. Flags stay unverified (they
                    # score at the unverified weight) until a value-level
                    # verifier exists.
                    emit("verify", "skipped", reason="value-level mode sends no row context, so no row verifier")
                    flagged.update(f.row_index for f in semantic_flags)
                elif verifier_llm is not None and semantic_flags:
                    # Verifier.verify already fails safe internally
                    # per-chunk (NEEDS_REVIEW on an unavailable chunk,
                    # not a crash) — nothing more to catch here. Uses
                    # masked_table too — the verifier's own prompt
                    # carries each flagged row's "actual values" (see
                    # verifier.py's _build_user_prompt), which is
                    # exactly the row content pii_masking.py exists to
                    # protect; it needs no real values to do its job,
                    # only to check a claim against whatever it's shown.
                    emit("verify", "running", {"flags_to_verify": len(semantic_flags)})
                    verified_flags = Verifier(verifier_llm).verify(masked_table, semantic_flags)
                    emit(
                        "verify",
                        "done",
                        {
                            label.value: sum(1 for v in verified_flags if v.label == label)
                            for label in VerificationLabel
                        },
                    )
                    flagged.update(
                        v.row_index
                        for v in verified_flags
                        if v.label != VerificationLabel.REJECTED
                    )
                else:
                    emit(
                        "verify",
                        "skipped",
                        reason="no independent verifier configured" if verifier_llm is None else "no flags to verify",
                    )
                    flagged.update(f.row_index for f in semantic_flags)
            else:
                emit("semantic", "skipped", reason="instruction handled by a deterministic rule")
                emit("verify", "skipped", reason="nothing from the AI layer to verify")

        return ScanResult(
            table_name=table.table_name,
            source_id=table.source_id,
            check_results=results,
            semantic_flags=semantic_flags,
            verified_flags=verified_flags,
            compilation=compilation,
            flagged_row_indices=sorted(flagged),
            semantic_iterations=semantic_iterations,
            semantic_coverage_warning=semantic_coverage_warning,
            quarantined_row_indices=sorted(quarantined),
            semantic_rows_considered=semantic_rows_considered,
            local_review=local_result,
            value_ai_review=value_ai_result,
        )
