"""
FastAPI entrypoint.

Deliberately stateless for now, per the current phase: DB/auth/
multi-tenancy are on hold, so `/scans` takes a CSV (and optional
related CSVs) straight off the request, runs the existing pipeline
in-memory, and returns the result directly — nothing is persisted,
no scan history, no tenant isolation. That's a real, known limitation
(anyone who can reach this service can run a scan), not an oversight;
it's what makes the frontend/eval work testable against a live HTTP
service right now, ahead of the DB/auth phase which will wrap this
same pipeline logic rather than replace it.
"""

from __future__ import annotations

import time
import secrets
import tempfile
from app import config as app_config
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.agent.cross_table_reasoning import (
    CrossTableVerificationLabel,
    CrossTableVerifier,
    resolve_fk_pairs,
    run_cross_table_semantic_with_iteration,
)
from app.agent.cost_estimate import ScanCostEstimate, estimate_scan_cost
from app.agent.generator import GeneratorAgent
from app.agent.pii_masking import mask_table_for_llm
from app.domain.confidence import derive_confidence
from app.jobs import (
    STAGE_ORDER,
    JobQueueFull,
    cancel_job,
    create_job,
    get_job,
    progress_callback_for,
    stage_callback_for,
    was_expired,
)
from app.jobs import submit as submit_job
from app.version import (
    CONFIDENCE_DERIVATION_VERSION,
    CROSS_TABLE_PROMPT_VERSION,
    DETECTION_VERSION,
    SCORING_VERSION,
    SEMANTIC_PROMPT_VERSION,
)
from app.canonical.models import CanonicalTable, ScanContext
from app.comparison import ComparisonError, ComparisonResult, compare_tables
from app.checks.referential_integrity import _FK_SUFFIX, _PK_COLUMN, _related_table_name
from app.config import (
    ConfigError,
    gemini_api_key,
    groq_api_key,
    max_semantic_reasoning_rows,
    max_upload_bytes,
    psi_critical_threshold,
    psi_warn_threshold,
)
from app.connectors.base import ConnectorConfig
from app.connectors.csv_connector import CSVConnector
from app.domain.rows_affected import compute_rows_affected
from app.instruction_preview import (
    DEFAULT_SAMPLE_ROWS,
    InstructionPreview,
    preview_instruction,
)
from app.scoring.scorer import compute_scorecard

app = FastAPI(title="DBCaaS Python Service")

# Wide open for local dev (Next.js on :3000) — this needs to be locked
# down to real allowed origins once this is ever deployed, same
# tracked gap as auth: nothing here enforces who can call this yet.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1 MB per chunk


def _read_upload_with_size_cap(upload: UploadFile, max_bytes: int) -> bytes:
    """Reads an upload in chunks, raising a 413 the moment the running
    total exceeds `max_bytes` — a file over the cap is never fully
    buffered into memory at all, unlike a bare `upload.file.read()`
    (the previous approach), which reads the entire file before any
    size check could happen. See app/config.py's `max_upload_bytes`
    for the default and how to override it."""
    upload.file.seek(0)
    total = 0
    chunks: list[bytes] = []
    while True:
        chunk = upload.file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"'{upload.filename}' is over the {max_bytes // (1024 * 1024)} MB upload "
                f"limit (MAX_UPLOAD_BYTES) — split it into smaller files, or raise the limit "
                f"on the server if this dataset genuinely needs to be larger.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _table_from_upload(upload: UploadFile, tenant_id: str, source_id: str) -> CanonicalTable:
    """Writes the upload to a temp file so the existing CSVConnector
    (which is file-path-based) can be reused as-is rather than forked
    into an in-memory variant. The connector's own filename-derived
    table_name would be the random temp-file name, though, which
    would silently break the referential-integrity name heuristic
    (an "accounts.csv" upload needs table_name == "accounts") — so
    it's overridden afterward from the ORIGINAL uploaded filename."""
    content = _read_upload_with_size_cap(upload, max_upload_bytes())
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".csv", delete=False) as tmp:
        tmp.write(content)
        tmp_path = tmp.name
    try:
        config = ConnectorConfig(
            tenant_id=tenant_id, source_id=source_id, settings={"file_path": tmp_path}
        )
        table = CSVConnector(config).extract()[0]
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    table.table_name = Path(upload.filename or "table").stem
    return table


# Custom instructions are meant to be a sentence or two of plain English
# ("flag anything that seems off for this industry"), not an essay — the
# compiler prompt and the semantic-reasoning prompt both inline this text
# verbatim, so an unbounded upload could blow up prompt size/cost. 4000
# chars is generous (roughly 700-800 words) for what this field is for;
# anything longer gets truncated with a warning rather than silently
# dropped or hard-rejected.
_MAX_INSTRUCTION_CHARS = 4000


class InstructionFileError(ValueError):
    """Raised when an uploaded custom-instruction file can't be read as text."""


def _extract_instruction_text(upload: UploadFile) -> str:
    """Reads the full text out of an uploaded .txt or .docx custom-
    instruction file. .txt is a plain decode; .docx needs python-docx
    since the format is a zipped XML bundle, not plain text. Anything
    else is rejected up front rather than passed through and silently
    mis-parsed."""
    filename = upload.filename or "instruction"
    suffix = Path(filename).suffix.lower()
    content = upload.file.read()

    if suffix == ".txt" or suffix == "":
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return content.decode("latin-1")
            except UnicodeDecodeError as exc:
                raise InstructionFileError(
                    f"Couldn't read '{filename}' as text (not valid UTF-8 or Latin-1)."
                ) from exc

    if suffix == ".docx":
        try:
            import docx  # python-docx
        except ImportError as exc:
            raise InstructionFileError(
                "The server is missing the python-docx package needed to read "
                ".docx files — install it or upload a .txt file instead."
            ) from exc

        import io

        try:
            document = docx.Document(io.BytesIO(content))
        except Exception as exc:  # python-docx raises assorted low-level errors on bad files
            raise InstructionFileError(
                f"Couldn't read '{filename}' as a Word document — it may be corrupted "
                "or not actually a .docx file."
            ) from exc

        paragraphs = [p.text for p in document.paragraphs]
        return "\n".join(paragraphs)

    raise InstructionFileError(
        f"Unsupported custom-instruction file type '{suffix or '(none)'}' — upload a .txt or .docx file."
    )


class FlaggedRowOut(BaseModel):
    row_index: int
    row_data: dict
    deterministic_reasons: list[str]
    semantic_reason: str | None = None
    semantic_confidence: float | None = None
    verification_label: str | None = None
    verification_notes: str | None = None
    # A second, independently-derived confidence figure for the
    # semantic flag, alongside the raw self-reported one above — see
    # app/domain/confidence.py for why the raw number alone isn't
    # trustworthy as a probability. None when there's no semantic flag
    # on this row at all (nothing to derive it from).
    derived_confidence: float | None = None
    # False when this row's ONLY findings are blanks in columns nobody
    # expects to be filled (see app/domain/rows_affected.py). The row
    # stays in the list — it is still a real finding for completeness —
    # but it does not count toward `rows_affected`.
    counts_as_problem: bool = True


class SecurityFinding(BaseModel):
    """A row the deterministic prompt-injection backstop
    (app.checks.injection_detection) flagged as looking like a hostile
    payload — a SEPARATE bucket from quality findings on purpose: it
    never contributes to any metric score (see scorer.py's exclusion
    of this check_name). It is NOT a substitute for flagged_rows —
    the same row still appears there too (see ScanResponse.flagged_rows'
    own note below), so this list is additive detail for a future
    security-alerts UI, not the only place the row shows up.

    `severity` is "high" or "low" (see injection_detection.py's
    two-tier design): "high" means the row was also QUARANTINED —
    excluded from semantic reasoning entirely, real containment, not
    just a label (see quarantined_row_count below and
    GeneratorAgent.run's containment logic). "low" means the language
    matched is ambiguous, ordinary-sounding business phrasing
    ("pre-approved", "compliance team") — reported for visibility, but
    the row was NOT excluded from semantic review over it alone.
    Frontend surfacing of this list is a follow-up ticket — this field
    exists so the execution layer is functionally secure first."""

    row_index: int
    row_data: dict
    matched_columns: list[str]
    detail: str
    severity: str  # "high" (quarantined) | "low" (reported only)


class ScanResponse(BaseModel):
    table_name: str
    total_rows: int
    overall_score: float
    metric_scores: list[dict]
    # Includes rows the prompt-injection backstop flagged (both
    # severity tiers) — a row's problem being a security one rather
    # than a quality one is never a reason for it to vanish from the
    # human-facing review list. deterministic_reasons on such a row is
    # prefixed "SECURITY (...)" so it's distinguishable at a glance;
    # see security_findings below for the structured version of the
    # same fact (matched columns, severity) meant for a future
    # dedicated UI.
    flagged_rows: list[FlaggedRowOut]
    semantic_iterations: int
    llm_used: bool
    # Echoes exactly what the CALLER asked for this scan — distinct
    # from llm_used, which is whether AI review actually ran. The two
    # can disagree: consent=true, llm_used=false means something else
    # blocked it (keys not configured server-side, or this table was
    # over the semantic-reasoning row cap without the opt-in flag — see
    # `warnings` for which). consent=false, llm_used is then always
    # false too — see _configure_llms. A client uses this pair, not
    # string-matching `warnings`, to decide whether to show "AI review
    # wasn't requested — run again with AI review" (consent was simply
    # never given) versus some other explanation.
    ai_review_consent: bool = False
    compilation: dict | None = None
    warnings: list[str] = []
    # Which prompt/scoring logic produced this scorecard — see
    # app/version.py. Answers "why did this score change after an
    # evaluation-logic change?" without needing any persistence layer;
    # the version travels with the response itself.
    versions: dict[str, str] = {}
    # Prompt-injection findings (the "Quarantine Model") — kept as
    # explicit fields rather than something the client must infer from
    # total_rows vs. semantic_iterations' coverage. Includes BOTH
    # severity tiers (see SecurityFinding.severity); quarantined_row_count
    # below counts only the "high" subset, since that's the only one
    # actually excluded from semantic reasoning.
    security_findings: list[SecurityFinding] = []
    # How many of total_rows were QUARANTINED (high-confidence match —
    # see injection_detection.py) and therefore never reasoned over by
    # the semantic layer and never scored under any quality metric. A
    # low-confidence-only match is NOT counted here, since that row
    # stays fully eligible for normal semantic review.
    quarantined_row_count: int = 0
    # The actual count of rows the semantic layer RELIABLY reasoned
    # over this scan — not just total_rows - quarantined_row_count.
    # Also short of that when an opt-in LLM-call cap, a provider
    # outage, or an unparseable batch stopped coverage early (see
    # `warnings` for which, if any, applied). Exposed explicitly, the
    # same "never make the caller derive a fact" convention as
    # derived_confidence above and semantic_coverage_warning below —
    # so a client sees directly why coverage doesn't match total_rows,
    # rather than having to notice security_findings exists and
    # subtract its length itself (which would have been wrong anyway
    # whenever the gap wasn't only quarantine).
    semantic_rows_considered: int = 0
    # Config-driven requiredness (see scorer.py's module docstring,
    # "Bug 4") — "configured" when the caller supplied a non-empty
    # `required_columns` list this scan, "no_required_fields_configured"
    # when completeness fell back to an unweighted overall fill rate
    # across every column instead. Lets a client label the completeness
    # number honestly rather than implying requiredness was configured
    # when nothing was ever declared.
    completeness_mode: str = "no_required_fields_configured"
    # Fill rate (0-100) per column that exists on the table but was NOT
    # named in `required_columns` — informational only, never scored.
    # Empty in the unweighted-fallback mode, since every column already
    # contributes to that one unweighted number and there's no
    # required/non-required split left to report separately.
    non_required_fill_rates: dict[str, float] = {}
    # Rows with a real problem — flagged_rows minus those whose only
    # finding is a blank in an optional column. Answers "how many rows
    # need attention?", which len(flagged_rows) does not: that list also
    # holds optional-blank rows, because completeness counts them.
    # Display-only; never feeds the score. See app/domain/rows_affected.py.
    # Data-egress ledger: what actually left this server for an external
    # provider on THIS scan. Counts only, never values. `external_ai_used`
    # false means nothing left the machine.
    data_egress: dict | None = None
    # Which detectors the ACCURACY metric rests on, so an AI-off score is
    # never read as comparable to an AI-on one. See docs/SCORING.md.
    accuracy_basis: dict | None = None
    # What the local (no-LLM) value review did, when it ran: which columns
    # it judged, which it skipped and why, how many distinct values.
    local_review: dict | None = None
    rows_affected: int = 0
    optional_blank_only_rows: int = 0
    # Which rule decided what counts as an expected-filled column:
    # "required_columns" (caller configured it) or "fill_rate_90".
    blank_policy: str = "fill_rate_90"


def _resolve_custom_instruction(
    custom_instruction: str | None,
    custom_instruction_file: UploadFile | None,
    warnings: list[str],
) -> str | None:
    """Shared precedence logic for /scans and /scans/schema: an uploaded
    instruction file wins over typed text when both are somehow present,
    with the truncation/empty/unsupported-type checks applied either way."""
    if custom_instruction_file is None or not custom_instruction_file.filename:
        return custom_instruction

    if custom_instruction and custom_instruction.strip():
        warnings.append(
            "Both a typed custom instruction and an instruction file were provided — "
            "the file was used and the typed text was ignored."
        )
    try:
        extracted = _extract_instruction_text(custom_instruction_file)
    except InstructionFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    extracted = extracted.strip()
    if not extracted:
        raise HTTPException(
            status_code=400,
            detail=f"'{custom_instruction_file.filename}' didn't contain any readable text.",
        )
    if len(extracted) > _MAX_INSTRUCTION_CHARS:
        warnings.append(
            f"The uploaded instruction file was {len(extracted)} characters — truncated to "
            f"the first {_MAX_INSTRUCTION_CHARS} characters."
        )
        extracted = extracted[:_MAX_INSTRUCTION_CHARS]
    return extracted


def _current_versions(llm, verifier_llm) -> dict[str, str]:
    """Duck-typed on `_model` (both GroqClient and GeminiClient carry
    it) rather than importing those classes here, so this stays
    agnostic to which concrete client was configured. Model identifiers
    are omitted entirely when no LLM is configured, rather than showing
    a misleading "no model" placeholder — llm_used already says that
    clearly elsewhere in the response."""
    versions = {
        "detection_version": DETECTION_VERSION,
        "scoring_version": SCORING_VERSION,
        "confidence_derivation_version": CONFIDENCE_DERIVATION_VERSION,
    }
    if llm is not None:
        versions["semantic_prompt_version"] = SEMANTIC_PROMPT_VERSION
        versions["cross_table_prompt_version"] = CROSS_TABLE_PROMPT_VERSION
        model = getattr(llm, "_model", None)
        if model:
            versions["generator_model"] = model
    if verifier_llm is not None:
        verifier_model = getattr(verifier_llm, "_model", None)
        if verifier_model:
            versions["verifier_model"] = verifier_model
    return versions


def _validated_ai_mode(value: str) -> str:
    v = (value or "rows").strip().lower()
    if v not in ("rows", "values"):
        raise HTTPException(status_code=400, detail="ai_review_mode must be 'rows' or 'values'.")
    return v


def _accuracy_basis(llm, verifier_llm, result) -> dict:
    local_ran = getattr(result, "local_review", None) is not None
    ai_ran = llm is not None and result.semantic_rows_considered > 0
    detectors: list[str] = []
    if local_ran:
        detectors.append("local_value_review")
    if getattr(result, "value_ai_review", None) is not None:
        detectors.append("llm_value_review")
    elif llm is not None:
        detectors.append("llm_generator")
        if verifier_llm is not None:
            detectors.append("llm_verifier")
    if local_ran and ai_ran:
        mode = "local_rules_plus_ai"
    elif local_ran:
        mode = "local_rules_only"
    elif ai_ran:
        mode = "ai_only"
    else:
        mode = "not_evaluated"
    return {
        "mode": mode,
        "detectors": detectors,
        "rows_considered": result.semantic_rows_considered,
        "comparable_across_modes": False,
    }


def _call_counts(*clients) -> tuple[int, int]:
    """(live, replayed) call totals across the given LLM clients."""
    live = sum(getattr(c, "live_calls", 0) for c in clients if c is not None)
    replayed = sum(getattr(c, "replayed_calls", 0) for c in clients if c is not None)
    return live, replayed


def _replay_fields(baseline: tuple[int, int], llm, verifier_llm) -> dict:
    """Per-scan live vs replayed call counts (current totals minus the
    snapshot taken before the scan ran, so a client reused across scans
    is still counted per scan)."""
    live_now, replayed_now = _call_counts(llm, verifier_llm)
    live = max(live_now - baseline[0], 0)
    replayed = max(replayed_now - baseline[1], 0)
    return {
        "calls_live": live,
        "calls_replayed": replayed,
        "from_replay": replayed > 0 and live == 0,
        "partly_from_replay": replayed > 0 and live > 0,
    }


def _data_egress_ledger(llm, verifier_llm, result, replay: dict | None = None) -> dict:
    """What left this server on this scan. Counts only, never values.
    The deterministic checks and the local value review never send
    anything anywhere, so with AI review off this is always zero."""
    external = llm is not None
    value_ai = getattr(result, "value_ai_review", None)
    if value_ai is not None:
        # Value-level mode: only distinct values from allowed columns left the
        # server. Counts only here, never the values.
        sent_something = value_ai.calls_made > 0
        if replay is not None and sent_something:
            sent_something = replay["calls_live"] > 0
        return {
            **(replay or {}),
            "external_ai_used": sent_something,
            "mode": "values",
            "providers_used": ["generator"] if sent_something else [],
            "rows_sent_to_provider": 0,
            "columns_shared": value_ai.columns_shared,
            "columns_refused": value_ai.columns_refused,
            "distinct_values_sent": value_ai.distinct_values_sent,
            "values_truncated": value_ai.values_truncated,
            "calls_made": value_ai.calls_made,
            "note": (
                "Only distinct values from the listed columns were sent to the AI provider. "
                "No rows, no other columns."
                if sent_something
                else (
                    "No data left this server on this scan: every AI answer was replayed from the local cache."
                    if replay and replay["from_replay"]
                    else "No data left this server."
                )
            ),
        }
    if replay is not None and external and replay["from_replay"]:
        # Every call was answered from the local replay cache: nothing was sent.
        return {
            **replay,
            "external_ai_used": False,
            "mode": "rows",
            "providers_used": [],
            "rows_sent_to_provider": 0,
            "note": "No data left this server on this scan: every AI answer was replayed from the local cache.",
        }
    providers: list[str] = []
    if llm is not None:
        providers.append("generator")
    if verifier_llm is not None and result.semantic_flags:
        providers.append("verifier")
    return {
        **(replay or {}),
        "external_ai_used": external,
        "mode": "rows",
        "providers_used": providers,
        # Rows (masked: email/phone replaced, plus any excluded columns)
        # that went into external prompts. 0 unless AI review ran.
        "rows_sent_to_provider": result.semantic_rows_considered if external else 0,
        "note": (
            "No data left this server."
            if not external
            else "Masked row data was sent to the configured AI providers for this scan."
        ),
    }


def _local_review_summary(result) -> dict | None:
    lr = getattr(result, "local_review", None)
    if lr is None:
        return None
    return {
        "columns_reviewed": lr.columns_reviewed,
        "columns_skipped": lr.columns_skipped,
        "distinct_values_scored": lr.distinct_values_scored,
        "values_sent_externally": lr.values_sent_externally,
    }


def _run_table_scan(
    table: CanonicalTable,
    context: ScanContext | None,
    *,
    llm,
    verifier_llm,
    custom_instruction: str | None,
    max_semantic_llm_calls: int | None,
    required_columns: list[str] | None = None,
    exclude_columns: set[str] | None = None,
    ai_review_consent: bool = False,
    mask_salt: str = "",
    progress_callback=None,
    stage_callback=None,
    local_review: bool | None = None,
    ai_review_mode: str = "rows",
    shareable_columns: set[str] | None = None,
) -> tuple[ScanResponse, object]:
    """Runs the full detection pipeline for ONE table and shapes the
    result into a ScanResponse. Shared by /scans (a single table, no
    schema context) and /scans/schema (N tables, each scanned against
    the same shared context so referential-integrity checks can see
    every other uploaded table). Returns the response alongside the raw
    GeneratorAgent result, since /scans/schema needs the raw
    check_results afterward to extract cross-table findings — reshaping
    that out of the already-built ScanResponse would mean re-parsing
    the human-readable `detail` strings instead of using the structured
    data that's already sitting right there.
    """
    _replay_baseline = _call_counts(llm, verifier_llm)
    result = GeneratorAgent().run(
        table,
        context,
        llm=llm,
        custom_instruction=custom_instruction,
        verifier_llm=verifier_llm,
        max_semantic_llm_calls=max_semantic_llm_calls,
        exclude_columns=exclude_columns,
        mask_salt=mask_salt,
        progress_callback=progress_callback,
        stage_callback=stage_callback,
        local_review=app_config.local_review_enabled() if local_review is None else local_review,
        ai_mode=ai_review_mode,
        shareable_columns=shareable_columns,
    )
    if stage_callback is not None:
        stage_callback("score", "running")
    scorecard = compute_scorecard(result, table, required_columns=required_columns)
    if stage_callback is not None:
        stage_callback(
            "score",
            "done",
            {
                "metrics_evaluated": sum(1 for m in scorecard.metric_scores if m.evaluated),
                "metrics_total": len(scorecard.metric_scores),
            },
        )

    warnings: list[str] = []
    if result.semantic_coverage_warning:
        warnings.append(result.semantic_coverage_warning)

    if required_columns:
        unknown_required = [c for c in required_columns if c not in table.column_names()]
        if unknown_required:
            warnings.append(
                f"required_columns named column(s) not present on this table, ignored: "
                f"{', '.join(unknown_required)}."
            )

    not_evaluated = [m.metric.value for m in scorecard.metric_scores if not m.evaluated]
    if not_evaluated:
        warnings.append(
            f"Not evaluated this scan (no applicable check ran): {', '.join(not_evaluated)}. "
            f"These are excluded from overall_score, not counted as 100."
        )

    quarantined_row_count = len(result.quarantined_row_indices)
    if quarantined_row_count > 0:
        warnings.append(
            f"{quarantined_row_count} row(s) were quarantined as a security threat "
            f"(high-confidence prompt-injection text detected). They are listed under "
            f"Security, excluded from every quality metric, and were never sent to "
            f"an AI model."
        )
    # semantic_rows_considered can also fall short of
    # total_rows - quarantined_row_count for reasons that have nothing
    # to do with quarantine — an opt-in LLM-call cap, a provider
    # outage, or an unparseable batch. result.semantic_coverage_warning
    # already explains those in prose when they happen; this covers the
    # gap only when that warning ISN'T already present, so a caller
    # never sees an unexplained shortfall.
    if (
        llm is not None
        and result.semantic_rows_considered < len(table.rows) - quarantined_row_count
        and not result.semantic_coverage_warning
    ):
        warnings.append(
            f"Only {result.semantic_rows_considered} of {len(table.rows)} row(s) were "
            f"reliably reasoned over by the semantic layer this scan."
        )

    injection_check_result = next(
        (r for r in result.check_results if r.check_name == "prompt_injection_check"), None
    )
    security_findings: list[SecurityFinding] = []
    if injection_check_result is not None:
        # Both severity tiers are reported here — not just the
        # quarantined subset — so a low-confidence match (still a real
        # signal worth a human's attention) isn't invisible just
        # because it didn't clear the bar for containment.
        for idx in injection_check_result.flagged_row_indices:
            severity = injection_check_result.row_severity.get(idx, "high")
            security_findings.append(
                SecurityFinding(
                    row_index=idx,
                    row_data=table.display_row(idx),
                    matched_columns=injection_check_result.flagged_fields.get(idx, []),
                    detail=injection_check_result.detail,
                    severity=severity,
                )
            )

    det_reasons_by_row: dict[int, list[str]] = {}
    for check_result in result.check_results:
        for idx in check_result.flagged_row_indices:
            if check_result.check_name == "prompt_injection_check":
                # Distinctly labeled rather than skipped — see
                # FlaggedRowOut/ScanResponse.flagged_rows' own note:
                # a row must stay visible in the ordinary review list
                # even when its problem is a security one.
                severity = check_result.row_severity.get(idx, "high")
                det_reasons_by_row.setdefault(idx, []).append(
                    f"SECURITY ({severity}-confidence prompt-injection match): {check_result.detail}"
                )
            else:
                # A row-specific reason (see CheckResult.row_reasons)
                # wins when the check populated one — falls back to the
                # whole check's aggregate `detail` string for a check
                # that doesn't (every check except DuplicateCheck today).
                reason_text = check_result.row_reasons.get(idx, check_result.detail)
                det_reasons_by_row.setdefault(idx, []).append(
                    f"{check_result.check_name}: {reason_text}"
                )

    semantic_by_row = {f.row_index: f for f in result.semantic_flags}
    verified_by_row = {v.row_index: v for v in result.verified_flags}

    affected = compute_rows_affected(
        result.check_results,
        set(semantic_by_row),
        result.flagged_row_indices,
        table,
        required_columns,
    )

    flagged_rows: list[FlaggedRowOut] = []
    for idx in result.flagged_row_indices:
        sem = semantic_by_row.get(idx)
        ver = verified_by_row.get(idx)
        flagged_rows.append(
            FlaggedRowOut(
                row_index=idx,
                row_data=table.display_row(idx),
                deterministic_reasons=det_reasons_by_row.get(idx, []),
                semantic_reason=sem.reason if sem else None,
                semantic_confidence=sem.confidence if sem else None,
                verification_label=ver.label.value if ver else None,
                verification_notes=ver.verifier_notes if ver else None,
                derived_confidence=(
                    derive_confidence(
                        "semantic_single_table",
                        ver.label.value if ver else None,
                        ver.verification_failed if ver else False,
                    )
                    if sem
                    else None
                ),
                counts_as_problem=idx in affected.problem_rows,
            )
        )

    response = ScanResponse(
        table_name=table.table_name,
        total_rows=len(table.rows),
        overall_score=scorecard.overall_score,
        metric_scores=[m.model_dump() for m in scorecard.metric_scores],
        flagged_rows=flagged_rows,
        semantic_iterations=result.semantic_iterations,
        llm_used=llm is not None,
        ai_review_consent=ai_review_consent,
        compilation=result.compilation.model_dump() if result.compilation else None,
        warnings=warnings,
        versions=_current_versions(llm, verifier_llm),
        security_findings=security_findings,
        quarantined_row_count=quarantined_row_count,
        semantic_rows_considered=result.semantic_rows_considered,
        completeness_mode=scorecard.completeness_mode,
        non_required_fill_rates=scorecard.non_required_fill_rates,
        data_egress=_data_egress_ledger(
            llm, verifier_llm, result,
            _replay_fields(_replay_baseline, llm, verifier_llm) if llm is not None else None,
        ),
        local_review=_local_review_summary(result),
        accuracy_basis=_accuracy_basis(llm, verifier_llm, result),
        rows_affected=len(affected.problem_rows),
        optional_blank_only_rows=len(affected.optional_blank_only_rows),
        blank_policy=affected.policy,
    )
    return response, result


def _configure_llms(
    warnings: list[str], ai_review_consent: bool
) -> tuple[object | None, object | None]:
    """Wires up Groq/Gemini clients only when BOTH conditions hold:
    the server has API keys configured, AND this specific request
    gave explicit consent to AI review (`ai_review_consent=true`).
    Shared by /scans and /scans/schema so both endpoints' behavior
    stays identical.

    This is a true opt-in, default OFF — per the product decision
    behind it: a notice shown after a scan has already sent row data
    to Groq/Gemini informs a user of something that already happened,
    which many enterprise security reviews treat as a disqualifying
    finding. The choice has to be made BEFORE anything leaves the
    server, not after.

    Two distinct reasons AI review doesn't run, kept as two distinct
    warnings so a caller (and a human reading the response) is never
    left guessing which one applies:
    - Keys not configured on the server at all — a deployment gap,
      unrelated to anything this specific request asked for.
    - Keys ARE configured, but this request didn't pass
      ai_review_consent=true — a per-scan consent choice the caller
      didn't make, not a configuration problem. Distinguishing this
      from the first case is the whole point: a caller seeing this
      warning knows AI review is available and simply wasn't asked
      for, not that something is broken or unset server-side."""
    try:
        groq_api_key()
        gemini_api_key()
    except ConfigError:
        warnings.append(
            "GROQ_API_KEY/GEMINI_API_KEY not set on the server — ran deterministic "
            "checks only. Semantic reasoning, verification, and custom instructions "
            "all need an LLM and were skipped."
        )
        return None, None

    if not ai_review_consent:
        warnings.append(
            "AI review wasn't requested for this scan, so no data was sent to any "
            "external provider. Deterministic checks and the local value review "
            "ran on this server. Turn on AI review to also send masked row data "
            "to Groq and Gemini for deeper semantic checks and custom "
            "instructions. API keys ARE configured on the server; this is a "
            "per-scan consent choice, not a configuration gap."
        )
        return None, None

    from app.agent.llm_clients import GeminiClient, GroqClient

    return GroqClient(), GeminiClient()


def _llm_for_table(
    table: CanonicalTable,
    llm,
    verifier_llm,
    *,
    allow_large_semantic_scan: bool,
    warnings: list[str],
) -> tuple[object | None, object | None]:
    """Row-count gate on semantic reasoning, separate from the
    max_semantic_llm_calls budget cap: full-table LLM coverage makes
    cost scale with file size, so a table over
    max_semantic_reasoning_rows() runs deterministic checks ONLY by
    default, unless the caller explicitly opts in with
    allow_large_semantic_scan=true. This is an API-level safeguard, not
    a scoring or detection-logic change — a table under the limit (the
    common case) is completely unaffected, same as max_llm_calls never
    changing behavior when it isn't hit."""
    if llm is None:
        return None, None
    limit = max_semantic_reasoning_rows()
    if len(table.rows) > limit and not allow_large_semantic_scan:
        warnings.append(
            f"'{table.table_name}' has {len(table.rows)} rows, over the {limit}-row default "
            f"for semantic reasoning (MAX_SEMANTIC_REASONING_ROWS) — ran deterministic checks "
            f"only for this table to keep LLM cost bounded. Pass allow_large_semantic_scan=true "
            f"to opt in to full semantic coverage anyway."
        )
        return None, None
    return llm, verifier_llm


class ScanEstimateResponse(BaseModel):
    tables: list[ScanCostEstimate]
    total_estimated_llm_calls: int
    total_estimated_cost_usd: float
    llm_configured: bool
    warnings: list[str] = []


@app.post("/scans/compare", response_model=ComparisonResult)
def compare_versions(
    old_file: UploadFile = File(...),
    new_file: UploadFile = File(...),
    key_column: str = Form(default=""),
    psi_warn: float | None = Form(default=None),
    psi_critical: float | None = Form(default=None),
) -> ComparisonResult:
    """Compares two versions of a table: schema changes (with rename
    proposals), key-matched row changes, volume, null-rate and PSI drift,
    and a format-normalized content hash. Deterministic and statistical
    only: no LLM is called and no row data leaves the process. Stateless;
    nothing is stored. Rows are matched by `key_column` (auto-detected
    from an id-style column when omitted); see app/comparison/compare.py
    for what is and is not claimed."""
    old = _table_from_upload(old_file, tenant_id="api", source_id="api")
    new = _table_from_upload(new_file, tenant_id="api", source_id="api")
    try:
        return compare_tables(
            old,
            new,
            key_column=key_column.strip() or None,
            psi_warn=psi_warn if psi_warn is not None else psi_warn_threshold(),
            psi_critical=psi_critical if psi_critical is not None else psi_critical_threshold(),
        )
    except ComparisonError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/scans/instruction-preview", response_model=InstructionPreview)
async def instruction_preview(
    file: UploadFile = File(...),
    custom_instruction: str | None = Form(default=None),
    custom_instruction_file: UploadFile | None = File(default=None),
    sample_rows: int = Form(
        default=DEFAULT_SAMPLE_ROWS,
        description="How many rows to judge when the instruction needs AI judgment "
        "(clamped to 5-45). Ignored for an instruction that compiles to an exact rule.",
    ),
    ai_review_consent: bool = Form(
        default=False,
        description="Same per-request opt-in as /scans. Compiling the instruction sends "
        "only the schema and the instruction text; judging a sample sends masked rows.",
    ),
    exclude_columns: str | None = Form(default=None),
) -> InstructionPreview:
    """Shows what a custom instruction would do BEFORE a full scan: what
    it was understood as, and either an exact match count (when it
    compiles to a rule) or what a small sample of rows would flag (when
    it needs AI judgment). Makes 1-2 LLM calls, never scores or stores
    anything. See app/instruction_preview.py."""
    warnings: list[str] = []
    instruction = _resolve_custom_instruction(custom_instruction, custom_instruction_file, warnings)
    if not instruction or not instruction.strip():
        raise HTTPException(status_code=400, detail="Provide a custom instruction to preview.")

    table = _table_from_upload(file, tenant_id="api", source_id="api")
    llm, _verifier = _configure_llms(warnings, ai_review_consent)
    if llm is None:
        return InstructionPreview(
            kind="unavailable",
            understood_as="AI review is off or not configured, so the instruction cannot be previewed.",
            total_rows=len(table.rows),
            warnings=warnings,
        )
    preview = preview_instruction(
        table,
        instruction.strip(),
        llm,
        mask_salt=secrets.token_hex(16),
        sample_rows=sample_rows,
        exclude_columns=set(_parse_required_columns(exclude_columns) or []),
    )
    preview.warnings = warnings + preview.warnings
    return preview


@app.post("/scans/estimate", response_model=ScanEstimateResponse)
async def estimate_scan(
    files: list[UploadFile] = File(...),
    max_semantic_llm_calls: int | None = Form(default=None),
    allow_large_semantic_scan: bool = Form(default=False),
) -> ScanEstimateResponse:
    """Pure arithmetic, no LLM calls — shows the user what a scan
    WOULD cost before they commit to running one. Works for a single
    file (the same shape /scans takes) or multiple (the same shape
    /scans/schema takes); this endpoint doesn't care which, since the
    estimate is per-table either way. Returns a real, non-zero
    llm_configured=False signal rather than silently estimating calls
    that would actually be skipped — an estimate should never claim
    coverage the server can't actually deliver."""
    warnings: list[str] = []
    tables = [
        _table_from_upload(f, tenant_id="api", source_id="api") for f in files if f.filename
    ]
    if not tables:
        raise HTTPException(status_code=400, detail="At least one file is required.")

    try:
        groq_api_key()
        gemini_api_key()
        llm_configured = True
    except ConfigError:
        llm_configured = False
        warnings.append(
            "GROQ_API_KEY/GEMINI_API_KEY not set on the server — semantic reasoning "
            "would be skipped entirely, so the real scan would cost 0 LLM calls "
            "regardless of the estimate below."
        )

    estimates = [
        estimate_scan_cost(
            t,
            max_llm_calls=max_semantic_llm_calls,
            allow_large_semantic_scan=allow_large_semantic_scan,
        )
        for t in tables
    ]
    from app.config import OBSERVED_TOKENS_PER_SEMANTIC_CALL, groq_tokens_per_day

    total_calls = sum(e.estimated_llm_calls for e in estimates)
    projected = total_calls * OBSERVED_TOKENS_PER_SEMANTIC_CALL
    if llm_configured and projected > groq_tokens_per_day():
        warnings.append(
            f"Projected ~{projected:,} Groq tokens ({total_calls} calls x ~"
            f"{OBSERVED_TOKENS_PER_SEMANTIC_CALL:,} observed per call) exceeds the "
            f"{groq_tokens_per_day():,} tokens/day free-tier cap. The scan would stop "
            f"early with partial AI coverage. Use a smaller file, set "
            f"max_semantic_llm_calls, or upgrade the Groq tier."
        )
    return ScanEstimateResponse(
        tables=estimates,
        total_estimated_llm_calls=sum(e.estimated_llm_calls for e in estimates) if llm_configured else 0,
        total_estimated_cost_usd=round(sum(e.estimated_cost_usd for e in estimates), 4) if llm_configured else 0.0,
        llm_configured=llm_configured,
        warnings=warnings,
    )


def _parse_required_columns(raw: str | None) -> list[str] | None:
    """Comma-separated column names from a Form field — no persisted
    schema/config layer exists yet (deliberately frozen for this
    phase), so this is the interim source of truth for "is this column
    required" that COMPLETENESS's cell-level scoring needs (see
    scorer.py's module docstring, Bug 4). Blank/whitespace-only entries
    are dropped rather than producing a spurious empty-string
    "required column"."""
    if raw is None or not raw.strip():
        return None
    columns = [c.strip() for c in raw.split(",") if c.strip()]
    return columns or None


@dataclass
class _SingleTableScanInputs:
    """Everything /scans needs to actually run the scan, after upload
    parsing and LLM configuration — the part of run_scan's body that's
    identical whether the caller wants the answer synchronously
    (/scans) or via a background job (/scans/jobs). Factored out
    specifically so those two endpoints can't drift apart on upload
    handling, consent gating, or masking — only on how they invoke
    _run_table_scan with the result."""

    table: CanonicalTable
    context: ScanContext | None
    llm: object
    verifier_llm: object
    custom_instruction: str | None
    warnings: list[str]


def _prepare_single_table_scan(
    file: UploadFile,
    related_files: list[UploadFile] | None,
    custom_instruction: str | None,
    custom_instruction_file: UploadFile | None,
    allow_large_semantic_scan: bool,
    ai_review_consent: bool,
) -> _SingleTableScanInputs:
    warnings: list[str] = []
    custom_instruction = _resolve_custom_instruction(custom_instruction, custom_instruction_file, warnings)

    table = _table_from_upload(file, tenant_id="api", source_id="api")
    related_tables = [
        _table_from_upload(f, tenant_id="api", source_id="api")
        for f in (related_files or [])
        if f.filename
    ]
    context = ScanContext(tables=[table, *related_tables]) if related_tables else None

    llm, verifier_llm = _configure_llms(warnings, ai_review_consent)
    llm, verifier_llm = _llm_for_table(
        table, llm, verifier_llm, allow_large_semantic_scan=allow_large_semantic_scan, warnings=warnings
    )
    if llm is None:
        custom_instruction = None  # nothing can compile or reason over it without an LLM

    return _SingleTableScanInputs(
        table=table,
        context=context,
        llm=llm,
        verifier_llm=verifier_llm,
        custom_instruction=custom_instruction,
        warnings=warnings,
    )


@app.post("/scans", response_model=ScanResponse)
async def run_scan(
    file: UploadFile = File(...),
    related_files: list[UploadFile] | None = File(default=None),
    custom_instruction: str | None = Form(default=None),
    custom_instruction_file: UploadFile | None = File(default=None),
    max_semantic_llm_calls: int | None = Form(default=None),
    allow_large_semantic_scan: bool = Form(
        default=False,
        description="Opt-in to run semantic reasoning over a table above "
        "MAX_SEMANTIC_REASONING_ROWS (default 5000). Without this, an over-limit table "
        "runs deterministic checks only, with a warning — see app/config.py.",
    ),
    required_columns: str | None = Form(
        default=None,
        description="Comma-separated column names that are required for this scan. "
        "Drives completeness scoring (see ScanResponse.completeness_mode) — omit to "
        "score an unweighted overall fill rate instead.",
    ),
    ai_review_consent: bool = Form(
        default=False,
        description="Explicit per-scan opt-in to AI review. Default OFF: without this, "
        "the scan runs deterministic checks only and semantic reasoning, verification, "
        "and custom instructions are all skipped, even if the server has API keys "
        "configured. When true, row data is sent to Groq and Gemini — email/phone "
        "columns are masked first (see exclude_columns to mask additional columns).",
    ),
    exclude_columns: str | None = Form(
        default=None,
        description="Comma-separated column names to exclude from AI review entirely "
        "(e.g. a name column) — masked the same way email/phone columns already are "
        "automatically. Has no effect when ai_review_consent is false.",
    ),
    ai_review_mode: str = Form(
        default="rows",
        description="'rows' (default) sends masked rows to the AI. 'values' sends only distinct "
        "values from the columns named in shareable_columns, never rows. Person-name, email "
        "and phone columns are refused even if named.",
    ),
    shareable_columns: str | None = Form(
        default=None,
        description="Comma-separated columns allowed to be shared with the AI in 'values' mode.",
    ),
) -> ScanResponse:
    """Synchronous scan — the whole request blocks until the scan
    finishes. Fine for a small/medium table, but a large one running
    full semantic reasoning can take many minutes (100+ paced LLM
    calls) — a caller that can't afford to hold a connection open that
    long should use /scans/jobs instead, which returns a job id
    immediately and lets the caller poll /scans/jobs/{job_id}."""
    inputs = _prepare_single_table_scan(
        file, related_files, custom_instruction, custom_instruction_file,
        allow_large_semantic_scan, ai_review_consent,
    )
    response, _result = _run_table_scan(
        inputs.table,
        inputs.context,
        llm=inputs.llm,
        verifier_llm=inputs.verifier_llm,
        custom_instruction=inputs.custom_instruction,
        max_semantic_llm_calls=max_semantic_llm_calls,
        required_columns=_parse_required_columns(required_columns),
        exclude_columns=set(_parse_required_columns(exclude_columns) or []),
        ai_review_consent=ai_review_consent,
        ai_review_mode=_validated_ai_mode(ai_review_mode),
        shareable_columns=set(_parse_required_columns(shareable_columns) or []),
        # Generated fresh per request, not derived from the data or any
        # fixed secret — see pii_masking.py's module docstring on why a
        # per-request salt is what keeps a value-keyed placeholder
        # correlatable WITHIN this scan while staying meaningless
        # across different scans or a provider's own logs. One table
        # here, so there's no cross-table correlation need, but the
        # same helper and the same salt convention as /scans/schema.
        mask_salt=secrets.token_hex(16) if inputs.llm is not None else "",
    )
    response.warnings = inputs.warnings + response.warnings
    return response


class ScanJobSubmitResponse(BaseModel):
    job_id: str


def _create_job_or_429():
    try:
        return create_job()
    except JobQueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc), headers={"Retry-After": "30"}) from exc


class ScanJobProgressOut(BaseModel):
    rows_considered: int
    total_rows: int
    rows_unreadable: int = 0


class StageOut(BaseModel):
    """One pipeline stage. `counts` are integers only (never row
    content). `started_at`/`finished_at` are seconds since job creation."""

    id: str
    status: str  # "pending" | "running" | "done" | "skipped"
    started_at: float | None = None
    finished_at: float | None = None
    counts: dict[str, int] = {}
    reason: str | None = None


class StageEventOut(BaseModel):
    t: float
    stage: str
    state: str  # "running" | "progress" | "done" | "skipped"
    counts: dict[str, int] | None = None
    reason: str | None = None
    table: str | None = None


class ScanJobStatusResponse(BaseModel):
    job_id: str
    status: str  # "queued" | "running" | "done" | "error" | "cancelled"
    progress: ScanJobProgressOut
    # Always present, in pipeline order; stages not reached yet are "pending".
    # For a multi-table schema job a stage reflects the table most recently
    # processed (each event in `events` is tagged with its table).
    stages: list[StageOut] = []
    # Only with ?events=true: the capped timeline, used to record a run for
    # a labelled replay. Omitted by default to keep polls small.
    events: list[StageEventOut] | None = None
    # Seconds from submission to now (running) or to the finish (terminal).
    # Server-side so a refreshed tab shows the true duration, and the final
    # figure is exact rather than "when the browser last polled".
    elapsed_seconds: float = 0.0
    # Only populated once status == "done" — the exact same shape
    # /scans itself returns, so a caller that switches from the
    # synchronous endpoint to the job endpoint doesn't need to parse
    # anything differently once the result is in hand.
    result: dict | None = None  # ScanResponse (/scans/jobs) or SchemaScanResponse (/scans/schema/jobs), as JSON
    # Only populated once status == "error" — includes a traceback
    # (see app/jobs.py's run_in_background) since this is an internal
    # tool today, not a hardened public API; a real production version
    # would want to NOT leak that to an untrusted caller.
    error: str | None = None


@app.post("/scans/jobs", response_model=ScanJobSubmitResponse)
async def submit_scan_job(
    file: UploadFile = File(...),
    related_files: list[UploadFile] | None = File(default=None),
    custom_instruction: str | None = Form(default=None),
    custom_instruction_file: UploadFile | None = File(default=None),
    max_semantic_llm_calls: int | None = Form(default=None),
    allow_large_semantic_scan: bool = Form(default=False),
    required_columns: str | None = Form(default=None),
    ai_review_consent: bool = Form(default=False),
    exclude_columns: str | None = Form(default=None),
    ai_review_mode: str = Form(default="rows"),
    shareable_columns: str | None = Form(default=None),
) -> ScanJobSubmitResponse:
    """The async alternative to /scans, for a table big enough that a
    synchronous request risks a proxy/browser/platform timeout (see
    app/jobs.py's module docstring for the concrete numbers). Upload
    parsing and LLM configuration happen HERE, synchronously, before
    this endpoint returns — both are fast (no network calls) and need
    the request's own UploadFile objects, which aren't valid anymore
    once this request ends. Only the slow part (the actual scan) runs
    in the background; by the time it starts, everything it needs is
    already a plain in-memory CanonicalTable, with no dependency on
    the original request at all.

    Poll GET /scans/jobs/{job_id} for status, progress, and — once
    status is "done" — the exact same ScanResponse shape /scans itself
    returns."""
    inputs = _prepare_single_table_scan(
        file, related_files, custom_instruction, custom_instruction_file,
        allow_large_semantic_scan, ai_review_consent,
    )
    job = _create_job_or_429()
    mask_salt = secrets.token_hex(16) if inputs.llm is not None else ""
    required_columns_list = _parse_required_columns(required_columns)
    exclude_columns_set = set(_parse_required_columns(exclude_columns) or [])
    ai_mode = _validated_ai_mode(ai_review_mode)
    shareable_set = set(_parse_required_columns(shareable_columns) or [])

    def _execute() -> dict:
        response, _result = _run_table_scan(
            inputs.table,
            inputs.context,
            llm=inputs.llm,
            verifier_llm=inputs.verifier_llm,
            custom_instruction=inputs.custom_instruction,
            max_semantic_llm_calls=max_semantic_llm_calls,
            required_columns=required_columns_list,
            exclude_columns=exclude_columns_set,
            ai_review_consent=ai_review_consent,
            ai_review_mode=ai_mode,
            shareable_columns=shareable_set,
            mask_salt=mask_salt,
            progress_callback=progress_callback_for(job.job_id),
            stage_callback=stage_callback_for(job.job_id),
        )
        response.warnings = inputs.warnings + response.warnings
        return response.model_dump()

    submit_job(job.job_id, _execute)
    return ScanJobSubmitResponse(job_id=job.job_id)


def _job_or_error(job_id: str, touch: bool = False):
    job = get_job(job_id, touch=touch)
    if job is None:
        if was_expired(job_id):
            raise HTTPException(
                status_code=410,
                detail="This scan's results expired and were deleted from the server "
                "(finished jobs are kept for a limited time). Please run the scan again.",
            )
        raise HTTPException(
            status_code=404,
            detail=f"No job found for id '{job_id}' — either it never existed, or the server "
            f"process restarted since it was submitted (jobs are in-memory only, not "
            f"persisted; see app/jobs.py).",
        )
    return job


def _job_response(job, include_events: bool = False) -> ScanJobStatusResponse:
    stages = [
        StageOut(id=sid, **job.stages[sid]) if sid in job.stages else StageOut(id=sid, status="pending")
        for sid in STAGE_ORDER
    ]
    return ScanJobStatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        progress=ScanJobProgressOut(**job.progress.as_dict()),
        stages=stages,
        events=[StageEventOut(**e) for e in job.events] if include_events else None,
        elapsed_seconds=round(
            max(0.0, (job.finished_at if job.finished_at is not None else time.monotonic()) - job.created_at), 2
        ),
        result=job.result,
        error=job.error,
    )


@app.get("/scans/jobs/{job_id}", response_model=ScanJobStatusResponse)
async def get_scan_job(job_id: str, events: bool = False) -> ScanJobStatusResponse:
    # touch=True: a poll is the heartbeat that keeps the job from being
    # auto-cancelled as abandoned (app/jobs.py::_cancel_abandoned_locked).
    return _job_response(_job_or_error(job_id, touch=True), include_events=events)


@app.delete("/scans/jobs/{job_id}", response_model=ScanJobStatusResponse)
async def cancel_scan_job(job_id: str) -> ScanJobStatusResponse:
    """Cancels a queued or running scan so it stops spending provider
    quota (at most one in-flight call finishes). Idempotent; a finished
    job is returned unchanged."""
    _job_or_error(job_id)
    job = cancel_job(job_id)
    if job is None:  # evicted between the two calls
        _job_or_error(job_id)
    return _job_response(job)


class CrossTableFinding(BaseModel):
    """A referential-integrity problem that genuinely spans two tables
    (as opposed to the check's own "fallback mode", which just flags
    blank FK-shaped values with nothing to verify them against — that
    case stays inside the owning table's own warnings, not here, since
    it isn't actually a cross-table fact). from_table is the table that
    holds the foreign key; to_table is the table it's supposed to
    reference."""

    from_table: str
    from_column: str
    to_table: str
    flagged_row_count: int


class CrossTableSemanticFinding(BaseModel):
    """A CONTENT-level inconsistency between a child row and the parent
    row its foreign key already resolves to — e.g. the child's company
    name doesn't match the parent account's name. Distinct from
    CrossTableFinding above, which is a structural fact (the FK doesn't
    resolve at all); this only ever runs on pairs that DID resolve,
    since an unresolved pair has nothing meaningful to compare content
    against. Requires an LLM — empty whenever one isn't configured."""

    from_table: str
    from_row_index: int
    to_table: str
    to_row_index: int
    fk_column: str
    reason: str
    confidence: float
    verification_label: str | None = None
    verification_notes: str | None = None
    # See FlaggedRowOut.derived_confidence — same idea, cross-table
    # variant (a lower base rate — see app/domain/confidence.py).
    derived_confidence: float | None = None


class SchemaScanResponse(BaseModel):
    tables: list[ScanResponse]
    cross_table_findings: list[CrossTableFinding]
    cross_table_semantic_findings: list[CrossTableSemanticFinding] = []
    # Row-count-weighted mean of each table's overall_score, so a
    # 5-row lookup table with a bad score doesn't drag the schema-wide
    # number down as much as a 5,000-row table with the same score —
    # a straight unweighted average would treat those as equally
    # important, which misrepresents how much of the actual data is
    # affected.
    aggregate_score: float
    total_tables: int
    total_rows: int
    llm_used: bool
    warnings: list[str] = []
    # Schema-wide echo of the same versions every per-table ScanResponse
    # already carries (see ScanResponse.versions) — duplicated at this
    # level too since aggregate_score and cross_table_findings are
    # schema-wide facts that deserve their own version trace without
    # requiring a caller to dig into tables[0].versions to find it.
    versions: dict[str, str] = {}


def _extract_cross_table_findings(
    table: CanonicalTable, context: ScanContext, result
) -> list[CrossTableFinding]:
    """Reads the referential_integrity_check's structured flagged_fields
    (row_index -> [column names]) rather than parsing its human-readable
    `detail` string, so this doesn't break if that string's wording ever
    changes. Only counts a column as a genuine cross-table finding when
    the related table it names was actually present in this scan's
    context — a bare blank-FK flag with no related table to check
    against (the check's own "fallback mode") isn't a cross-table fact
    and is left for that table's own deterministic warnings instead."""
    check_result = next(
        (r for r in result.check_results if r.check_name == "referential_integrity_check"), None
    )
    if check_result is None:
        return []

    fk_columns = [
        name
        for name in table.column_names()
        if name.endswith(_FK_SUFFIX) and name != _PK_COLUMN
    ]

    findings: list[CrossTableFinding] = []
    for fk_col in fk_columns:
        related_name = _related_table_name(fk_col)
        if context.get_table(related_name) is None:
            continue  # fallback mode — no related table, nothing cross-table to report
        count = sum(1 for cols in check_result.flagged_fields.values() if fk_col in cols)
        if count > 0:
            findings.append(
                CrossTableFinding(
                    from_table=table.table_name,
                    from_column=fk_col,
                    to_table=related_name,
                    flagged_row_count=count,
                )
            )
    return findings


def _find_fk_relationships(
    tables: list[CanonicalTable], context: ScanContext
) -> list[tuple[CanonicalTable, CanonicalTable, str]]:
    """Every (child_table, parent_table, fk_column) triple where the
    parent table named by the FK-suffix heuristic is actually present
    in this scan — the same relationships _extract_cross_table_findings
    reports structurally, surfaced here as table objects instead of
    counts so the cross-table semantic pass knows exactly which table
    pairs to reason over."""
    relationships: list[tuple[CanonicalTable, CanonicalTable, str]] = []
    for table in tables:
        fk_columns = [
            name for name in table.column_names() if name.endswith(_FK_SUFFIX) and name != _PK_COLUMN
        ]
        for fk_col in fk_columns:
            related_name = _related_table_name(fk_col)
            parent = context.get_table(related_name)
            if parent is not None:
                relationships.append((table, parent, fk_col))
    return relationships


@dataclass
class _SchemaScanInputs:
    """Parsed/validated inputs for a multi-table scan — the part of
    /scans/schema that's identical whether it runs synchronously or as
    a background job (see _SingleTableScanInputs for the same split on
    /scans). Holds only plain in-memory data, no UploadFile objects."""

    tables: list[CanonicalTable]
    context: ScanContext
    llm: object
    verifier_llm: object
    instructions_map: dict[str, str]
    required_columns_map: dict[str, list[str]]
    exclude_columns_map: dict[str, list[str]]
    warnings: list[str]
    mask_salt: str
    allow_large_semantic_scan: bool
    ai_review_consent: bool


def _prepare_schema_scan(
    files: list[UploadFile],
    custom_instructions: str | None,
    required_columns: str | None,
    exclude_columns: str | None,
    allow_large_semantic_scan: bool,
    ai_review_consent: bool,
    tables_override: list[CanonicalTable] | None = None,
    extra_warnings: list[str] | None = None,
) -> _SchemaScanInputs:
    """`tables_override` lets a saved database connection supply
    already-extracted tables instead of uploaded files; everything after
    that point is the identical pipeline."""
    warnings: list[str] = list(extra_warnings or [])

    if tables_override is None and len(files) < 2:
        raise HTTPException(
            status_code=400,
            detail="/scans/schema needs at least 2 tables — for a single table, use /scans instead.",
        )

    instructions_map: dict[str, str] = {}
    if custom_instructions:
        import json

        try:
            parsed = json.loads(custom_instructions)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="custom_instructions must be a JSON object mapping table name -> instruction text.",
            ) from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise HTTPException(
                status_code=400,
                detail="custom_instructions must be a JSON object of {table_name: instruction} string pairs.",
            )
        instructions_map = parsed

    required_columns_map: dict[str, list[str]] = {}
    if required_columns:
        import json

        try:
            parsed_required = json.loads(required_columns)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="required_columns must be a JSON object mapping table name -> "
                "comma-separated required column names.",
            ) from exc
        if not isinstance(parsed_required, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed_required.items()
        ):
            raise HTTPException(
                status_code=400,
                detail="required_columns must be a JSON object of "
                "{table_name: \"col1,col2\"} string pairs.",
            )
        required_columns_map = {
            k: cols for k, v in parsed_required.items() if (cols := _parse_required_columns(v))
        }

    exclude_columns_map: dict[str, list[str]] = {}
    if exclude_columns:
        import json

        try:
            parsed_exclude = json.loads(exclude_columns)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail="exclude_columns must be a JSON object mapping table name -> "
                "comma-separated column names.",
            ) from exc
        if not isinstance(parsed_exclude, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed_exclude.items()
        ):
            raise HTTPException(
                status_code=400,
                detail="exclude_columns must be a JSON object of "
                "{table_name: \"col1,col2\"} string pairs.",
            )
        exclude_columns_map = {
            k: cols for k, v in parsed_exclude.items() if (cols := _parse_required_columns(v))
        }

    tables = (
        tables_override
        if tables_override is not None
        else [_table_from_upload(f, tenant_id="api", source_id="api") for f in files if f.filename]
    )
    context = ScanContext(tables=tables)

    unmatched = set(instructions_map) - {t.table_name for t in tables}
    if unmatched:
        warnings.append(
            f"custom_instructions named table(s) not present in this upload, ignored: {', '.join(sorted(unmatched))}"
        )
    unmatched_required = set(required_columns_map) - {t.table_name for t in tables}
    if unmatched_required:
        warnings.append(
            f"required_columns named table(s) not present in this upload, ignored: "
            f"{', '.join(sorted(unmatched_required))}"
        )

    llm, verifier_llm = _configure_llms(warnings, ai_review_consent)
    # ONE salt for the whole request — every table's single-table pass
    # AND the cross-table pass below all mask with this same salt, so a
    # value shared between a child row and its linked parent row (or
    # between two different tables entirely) still correlates. See
    # pii_masking.py's module docstring for why a per-call salt would
    # silently break that correlation.
    mask_salt = secrets.token_hex(16) if llm is not None else ""

    return _SchemaScanInputs(
        tables=tables,
        context=context,
        llm=llm,
        verifier_llm=verifier_llm,
        instructions_map=instructions_map,
        required_columns_map=required_columns_map,
        exclude_columns_map=exclude_columns_map,
        warnings=warnings,
        mask_salt=mask_salt,
        allow_large_semantic_scan=allow_large_semantic_scan,
        ai_review_consent=ai_review_consent,
    )


@app.post("/scans/schema", response_model=SchemaScanResponse)
async def run_schema_scan(
    files: list[UploadFile] = File(...),
    # JSON object mapping table_name (the uploaded file's stem, same
    # convention as the single-table /scans endpoint) to a plain-English
    # instruction for THAT table only — e.g. {"leads": "flag anything
    # that seems off for this industry"}. Deliberately per-table, not
    # cross-table: an instruction like "flag leads without a matching
    # account" would need the server to infer which tables and join key
    # it's even talking about, which is schema-relationship inference,
    # not instruction compiling — out of scope here, same as the
    # embedding-based schema-agnostic column matching was deferred
    # earlier. File-upload instructions (.txt/.docx) stay single-table,
    # via /scans, for the same reason: there's no clean per-table
    # multi-file-upload pairing UI yet.
    custom_instructions: str | None = Form(default=None),
    max_semantic_llm_calls: int | None = Form(default=None),
    # Same per-table row-count opt-in as /scans — see _llm_for_table.
    # Applies to each table's OWN single-table semantic reasoning, not
    # the cross-table pass below (bounded by FK-pair count, not row
    # count, so it isn't the same cost-scaling concern).
    allow_large_semantic_scan: bool = Form(default=False),
    # Explicit, opt-in cost ceiling on the cross-table semantic pass —
    # same "caller decides, method never silently caps" convention as
    # max_semantic_llm_calls above. None means full coverage of every
    # resolved cross-table pair, same default as everything else here.
    max_cross_table_llm_calls: int | None = Form(default=None),
    # JSON object mapping table_name -> comma-separated required column
    # names, same per-table-not-cross-table convention as
    # custom_instructions above — see _parse_required_columns and
    # scorer.py's module docstring (Bug 4) for what this drives.
    required_columns: str | None = Form(default=None),
    ai_review_consent: bool = Form(
        default=False,
        description="Explicit per-scan opt-in to AI review, same true-opt-in/default-OFF "
        "gate as /scans — see that endpoint's description. Applies to every table's "
        "single-table semantic reasoning AND the cross-table semantic pass alike; there "
        "is no separate consent flag per table or per pass.",
    ),
    # JSON object mapping table_name -> comma-separated column names to
    # exclude from AI review, same per-table-not-cross-table convention
    # as required_columns/custom_instructions above. A column excluded
    # on a CHILD table this way is also masked when that table's rows
    # appear as the CHILD side of a cross-table pair below (and
    # likewise for a PARENT table's own exclusions on its side) — each
    # table's own exclusion list follows it into every pass its rows
    # appear in, single-table or cross-table.
    exclude_columns: str | None = Form(default=None),
) -> SchemaScanResponse:
    """Synchronous multi-table scan. For large tables prefer
    /scans/schema/jobs (submit + poll) — see app/jobs.py."""
    inputs = _prepare_schema_scan(
        files, custom_instructions, required_columns, exclude_columns,
        allow_large_semantic_scan, ai_review_consent,
    )
    return _execute_schema_scan(inputs, max_semantic_llm_calls, max_cross_table_llm_calls)


@app.post("/scans/schema/jobs", response_model=ScanJobSubmitResponse)
async def submit_schema_scan_job(
    files: list[UploadFile] = File(...),
    custom_instructions: str | None = Form(default=None),
    max_semantic_llm_calls: int | None = Form(default=None),
    allow_large_semantic_scan: bool = Form(default=False),
    max_cross_table_llm_calls: int | None = Form(default=None),
    required_columns: str | None = Form(default=None),
    ai_review_consent: bool = Form(default=False),
    exclude_columns: str | None = Form(default=None),
) -> ScanJobSubmitResponse:
    """Async variant of /scans/schema — the slowest request in the
    system (every table's semantic pass plus the cross-table pass).
    Same submit/poll contract as /scans/jobs; poll the same
    GET /scans/jobs/{job_id}, whose `result` is a SchemaScanResponse
    here. Responds 429 when the job queue is full."""
    inputs = _prepare_schema_scan(
        files, custom_instructions, required_columns, exclude_columns,
        allow_large_semantic_scan, ai_review_consent,
    )
    job = _create_job_or_429()
    callback = progress_callback_for(job.job_id)

    def _execute() -> dict:
        return _execute_schema_scan(
            inputs,
            max_semantic_llm_calls,
            max_cross_table_llm_calls,
            progress_callback=callback,
            job_id=job.job_id,
        ).model_dump()

    submit_job(job.job_id, _execute)
    return ScanJobSubmitResponse(job_id=job.job_id)


def _execute_schema_scan(
    inputs: _SchemaScanInputs,
    max_semantic_llm_calls: int | None,
    max_cross_table_llm_calls: int | None,
    progress_callback=None,
    job_id: str | None = None,
) -> SchemaScanResponse:
    """The slow half of /scans/schema — every table's scan plus the
    cross-table semantic pass. `progress_callback(rows_considered,
    total_rows)`, when given, reports progress aggregated across ALL
    tables: rows from tables already finished, plus the current
    table's own semantic coverage so far."""
    tables = inputs.tables
    context = inputs.context
    llm = inputs.llm
    verifier_llm = inputs.verifier_llm
    instructions_map = inputs.instructions_map
    required_columns_map = inputs.required_columns_map
    exclude_columns_map = inputs.exclude_columns_map
    warnings = inputs.warnings
    mask_salt = inputs.mask_salt
    allow_large_semantic_scan = inputs.allow_large_semantic_scan
    ai_review_consent = inputs.ai_review_consent

    table_responses: list[ScanResponse] = []
    cross_table_findings: list[CrossTableFinding] = []
    # Each table's quarantined row indices, keyed by table_name — built
    # during the per-table loop below, then used to filter the
    # cross-table semantic pass afterward so a pair touching a
    # quarantined row on EITHER side (child or parent) never reaches an
    # LLM prompt. No changes needed to cross_table_reasoning.py itself:
    # filtering happens entirely here, at the orchestration layer,
    # reusing resolve_fk_pairs/run_cross_table_semantic_with_iteration
    # unmodified.
    quarantined_by_table: dict[str, set[int]] = {}
    grand_total_rows = sum(len(t.rows) for t in tables)
    finished_rows = 0
    for table in tables:
        table_llm, table_verifier_llm = _llm_for_table(
            table, llm, verifier_llm, allow_large_semantic_scan=allow_large_semantic_scan, warnings=warnings
        )
        instruction = instructions_map.get(table.table_name) if table_llm is not None else None
        response, result = _run_table_scan(
            table,
            context,
            llm=table_llm,
            verifier_llm=table_verifier_llm,
            custom_instruction=instruction,
            max_semantic_llm_calls=max_semantic_llm_calls,
            required_columns=required_columns_map.get(table.table_name),
            exclude_columns=set(exclude_columns_map.get(table.table_name, [])),
            ai_review_consent=ai_review_consent,
            mask_salt=mask_salt,
            stage_callback=(
                stage_callback_for(job_id, table.table_name) if job_id is not None else None
            ),
            progress_callback=(
                (lambda considered, _total, unreadable=0, _offset=finished_rows: progress_callback(
                    _offset + considered, grand_total_rows, unreadable
                ))
                if progress_callback is not None
                else None
            ),
        )
        finished_rows += len(table.rows)
        if progress_callback is not None:
            progress_callback(finished_rows, grand_total_rows)
        table_responses.append(response)
        cross_table_findings.extend(_extract_cross_table_findings(table, context, result))
        quarantined_by_table[table.table_name] = set(result.quarantined_row_indices)

    # Cross-table SEMANTIC pass — content-level judgment on pairs whose
    # FK already resolves, as opposed to the structural check above
    # (which only ever looks at pairs that DON'T resolve). Only runs
    # with an LLM configured, same gating as every other semantic path
    # in this service; silently produces nothing otherwise, same as a
    # custom_instruction does without an LLM.
    cross_table_semantic_findings: list[CrossTableSemanticFinding] = []
    if llm is not None:
        # Masked once per table, reused for every FK relationship that
        # table takes part in — same "mask the row content, never the
        # row-index math" split as the single-table path (generator.py).
        # resolve_fk_pairs below deliberately still runs over the REAL
        # `tables`, not these masked copies: it matches real fk/pk
        # string values to find resolved pairs, which a placeholder
        # value would break if an id-like column ever carried a
        # semantic_hint (it doesn't today, but this keeps pair
        # resolution correct regardless).
        masked_tables_by_name = {
            t.table_name: mask_table_for_llm(
                t, set(exclude_columns_map.get(t.table_name, [])), salt=mask_salt
            )
            for t in tables
        }
        for child, parent, fk_col in _find_fk_relationships(tables, context):
            pairs = resolve_fk_pairs(child, parent, fk_col)
            masked_child = masked_tables_by_name.get(child.table_name, child)
            masked_parent = masked_tables_by_name.get(parent.table_name, parent)
            child_quarantined = quarantined_by_table.get(child.table_name, set())
            parent_quarantined = quarantined_by_table.get(parent.table_name, set())
            if child_quarantined or parent_quarantined:
                # Containment on the cross-table path too: a pair
                # touching a quarantined row on EITHER side is dropped
                # before it ever reaches the LLM, not merely excluded
                # from scoring afterward. resolve_fk_pairs and
                # run_cross_table_semantic_with_iteration themselves
                # are untouched — this filter is the entire fix.
                pairs = [
                    (c, p)
                    for c, p in pairs
                    if c not in child_quarantined and p not in parent_quarantined
                ]
            if not pairs:
                continue
            flags, _calls, coverage_warning = run_cross_table_semantic_with_iteration(
                masked_child, masked_parent, pairs, llm, max_llm_calls=max_cross_table_llm_calls
            )
            if coverage_warning:
                warnings.append(coverage_warning)
            if not flags:
                continue

            if verifier_llm is not None:
                verified = CrossTableVerifier(verifier_llm).verify(masked_child, masked_parent, flags)
                verified_by_key = {(v.from_row_index, v.to_row_index): v for v in verified}
            else:
                verified_by_key = {}

            for f in flags:
                v = verified_by_key.get((f.from_row_index, f.to_row_index))
                if v is not None and v.label == CrossTableVerificationLabel.REJECTED:
                    continue  # an explicitly rejected claim isn't surfaced as a finding
                cross_table_semantic_findings.append(
                    CrossTableSemanticFinding(
                        from_table=child.table_name,
                        from_row_index=f.from_row_index,
                        to_table=parent.table_name,
                        to_row_index=f.to_row_index,
                        fk_column=fk_col,
                        reason=f.reason,
                        confidence=f.confidence,
                        verification_label=v.label.value if v else None,
                        verification_notes=v.verifier_notes if v else None,
                        derived_confidence=derive_confidence(
                            "semantic_cross_table",
                            v.label.value if v else None,
                            v.verification_failed if v else False,
                        ),
                    )
                )

    total_rows = sum(t.total_rows for t in table_responses)
    aggregate_score = (
        sum(t.overall_score * t.total_rows for t in table_responses) / total_rows
        if total_rows > 0
        else 100.0
    )

    return SchemaScanResponse(
        tables=table_responses,
        cross_table_findings=cross_table_findings,
        cross_table_semantic_findings=cross_table_semantic_findings,
        aggregate_score=round(aggregate_score, 2),
        total_tables=len(table_responses),
        total_rows=total_rows,
        llm_used=llm is not None,
        warnings=warnings,
        versions=_current_versions(llm, verifier_llm),
    )



# ---------------------------------------------------------------------------
# Connect a source: saved connections of ANY registered connector type.
# The endpoints below are connector-agnostic; what exists is decided by
# app/connectors/registry.py. Adding a connector never edits this block.
# ---------------------------------------------------------------------------

import logging  # noqa: E402
import os as _os  # noqa: E402
from typing import Any, Literal  # noqa: E402
from urllib.parse import urlencode as _urlencode  # noqa: E402

from fastapi import Depends, Header  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

from app import oauth_flow as _oauth  # noqa: E402
from app.connections import STORE as _CONNECTIONS, ConnectionStoreFull  # noqa: E402
from app.connectors.registry import ConnectorSpec, get_registry  # noqa: E402
from app.connectors.source import ConnectorError, TabularSource  # noqa: E402
from app.connectors.sql_connector import DIALECTS as _SQL_DIALECTS, default_row_limit  # noqa: E402

_log_conn = logging.getLogger("app.connections")


def _require_access_token(x_access_token: str | None = Header(default=None)) -> None:
    """Opt-in shared secret for the connection endpoints. Unset means open
    (local PoC use); set API_ACCESS_TOKEN before exposing this anywhere."""
    expected = _os.environ.get("API_ACCESS_TOKEN", "").strip()
    if expected and not secrets.compare_digest(x_access_token or "", expected):
        raise HTTPException(status_code=401, detail="Missing or wrong access token.")


def _spec_or_404(key: str) -> ConnectorSpec:
    spec = get_registry().get(key)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"No connector called '{key}' is installed.")
    return spec


def _call_connector(fn, *args, **kwargs):
    """Runs one connector call. A user-actionable ConnectorError becomes a
    400 with its message; any other exception (a connector bug) becomes a
    502 with a generic message, so one broken connector can't take the API
    down or leak internals."""
    try:
        return fn(*args, **kwargs)
    except ConnectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        _log_conn.exception("connector failed unexpectedly")
        raise HTTPException(status_code=502, detail="The connector failed unexpectedly. Other sources are unaffected.") from None


class ConnectionCreate(BaseModel):
    """New shape: {type, name, settings}. The first SQL-only shape
    ({dialect, host, port, ...}) is still accepted unchanged."""

    name: str = ""
    type: str | None = None
    settings: dict[str, Any] | None = None
    # legacy SQL fields
    dialect: Literal["mysql", "postgresql", "sqlite"] | None = None
    host: str = ""
    port: int | None = None
    database: str = ""
    username: str = ""
    password: str | None = None
    schema_name: str | None = None
    sqlite_file: str | None = None
    row_limit: int | None = None

    def resolved(self) -> tuple[str, dict[str, Any]]:
        if self.type:
            return self.type, dict(self.settings or {})
        if not self.dialect:
            raise HTTPException(status_code=400, detail="Say which connector to use ('type').")
        raw = {
            "host": self.host.strip(), "port": self.port, "database": self.database.strip(),
            "username": self.username.strip(), "password": self.password,
            "schema": self.schema_name, "sqlite_file": self.sqlite_file,
        }
        return self.dialect, raw


class OAuthStart(BaseModel):
    name: str = ""
    settings: dict[str, Any] | None = None


def _source_for(conn_id: str) -> tuple[TabularSource, Any]:
    saved = _CONNECTIONS.get(conn_id)
    if saved is None:
        raise HTTPException(status_code=404, detail="Unknown or expired connection.")
    spec = get_registry().get(saved.type)
    if spec is None:
        raise HTTPException(status_code=409, detail=f"The '{saved.type}' connector is not available on this server.")
    source = _call_connector(spec.build, saved.connector_settings(), source_id=saved.id)
    return source, saved


@app.get("/connectors")
def list_connectors() -> dict:
    """Catalog that drives the Connect screen: every installed connector
    with its form fields, plus planned ones and any that failed to load."""
    reg = get_registry()
    return {
        "connectors": reg.catalog(),
        "unavailable": [{"module": p.module, "reason": p.reason} for p in reg.problems],
        "default_row_limit": default_row_limit(),
    }


@app.get("/connections/dialects")
def connection_dialects() -> dict:
    """Kept for the first SQL-only client."""
    return {"dialects": list(_SQL_DIALECTS), "default_row_limit": default_row_limit()}


def _save(spec: ConnectorSpec, name: str, public: dict, secret: dict) -> dict:
    probe = _call_connector(spec.build, {**public, **secret})
    try:
        _call_connector(probe.check)
        account = probe.account_label()
    finally:
        probe.close()
    try:
        saved = _CONNECTIONS.add(name=name, type=spec.key, settings=public, secrets=secret, account=account)
    except ConnectionStoreFull:
        raise HTTPException(status_code=429, detail="Too many saved connections; delete one first.") from None
    return saved.public()


@app.post("/connections", dependencies=[Depends(_require_access_token)])
def create_connection(body: ConnectionCreate) -> dict:
    """Tests the connection first; one that cannot connect is never saved.
    The response never contains a password, key or token."""
    key, raw = body.resolved()
    spec = _spec_or_404(key)
    if spec.uses_oauth({**{f.key: f.default for f in spec.fields if f.default is not None}, **raw}):
        raise HTTPException(status_code=400, detail="This source signs in through the browser: use the sign-in button.")
    public, secret = _call_connector(spec.split_settings, raw)
    if not body.type:
        # legacy SQL body: keep its password and row_limit even where the
        # spec has no such field (e.g. SQLite), exactly as before
        if body.password:
            secret.setdefault("password", body.password)
        if body.row_limit:
            public["row_limit"] = body.row_limit
    default_name = (f"{public.get('database')} @ {public.get('host')}" if public.get("host") else spec.label)
    return _save(spec, body.name or default_name, public, secret)


@app.post("/connectors/{key}/oauth/start", dependencies=[Depends(_require_access_token)])
def start_oauth(key: str, body: OAuthStart) -> dict:
    spec = _spec_or_404(key)
    if spec.oauth is None:
        raise HTTPException(status_code=400, detail=f"{spec.label} does not use browser sign-in.")
    raw = dict(body.settings or {})
    if spec.oauth.when:
        raw[spec.oauth.when[0]] = spec.oauth.when[1]
    public, _secret = _call_connector(spec.split_settings, raw)
    from app.connectors.gdrive_connector import pkce_pair  # generic PKCE helper

    verifier, challenge = pkce_pair()
    pending = _oauth.PENDING.create(connector_key=key, name=body.name or spec.label,
                                    settings=public, code_verifier=verifier)
    url = _call_connector(spec.oauth.authorization_url, state=pending.state,
                          redirect_uri=_oauth.redirect_uri(), code_challenge=challenge)
    return {"authorization_url": url}


@app.get("/connectors/oauth/callback")
def oauth_callback(state: str = "", code: str = "", error: str = "") -> RedirectResponse:
    """The provider sends the browser here. Always redirects back to the
    Connect screen, with ?connection=<id> or ?oauth_error=<message>."""
    def back(**params: str) -> RedirectResponse:
        return RedirectResponse(f"{_oauth.frontend_url()}/connect?{_urlencode(params)}", status_code=303)

    pending = _oauth.PENDING.pop(state) if state else None
    if pending is None:
        return back(oauth_error="That sign-in link expired or was already used. Please try again.")
    if error or not code:
        return back(oauth_error="Sign-in was cancelled.")
    spec = get_registry().get(pending.connector_key)
    if spec is None or spec.oauth is None:
        return back(oauth_error="That connector is no longer available.")
    try:
        secret = _call_connector(spec.oauth.exchange, code=code, redirect_uri=_oauth.redirect_uri(),
                                 code_verifier=pending.code_verifier)
        saved = _save(spec, pending.name, pending.settings, secret)
    except HTTPException as exc:
        return back(oauth_error=str(exc.detail))
    return back(connection=saved["id"])


@app.get("/connections", dependencies=[Depends(_require_access_token)])
def list_connections() -> dict:
    return {"connections": [c.public() for c in _CONNECTIONS.list()]}


@app.delete("/connections/{conn_id}", dependencies=[Depends(_require_access_token)])
def delete_connection(conn_id: str) -> dict:
    saved = _CONNECTIONS.get(conn_id)
    if saved is None or not _CONNECTIONS.delete(conn_id):
        raise HTTPException(status_code=404, detail="Unknown or expired connection.")
    spec = get_registry().get(saved.type)
    if spec is not None:
        try:
            spec.build(saved.connector_settings(), source_id=saved.id).revoke()
        except Exception:  # noqa: BLE001 - revoking is best effort
            _log_conn.info("revoke failed for a %s connection", saved.type)
    return {"deleted": True}


@app.get("/connections/{conn_id}/tables", dependencies=[Depends(_require_access_token)])
def connection_tables(conn_id: str) -> dict:
    source, saved = _source_for(conn_id)
    try:
        tables = _call_connector(source.list_tables)
    finally:
        source.close()
    return {
        "connection": saved.public(),
        "row_limit": saved.settings.get("row_limit") or default_row_limit(),
        "tables": [
            {
                "id": t.id,
                "name": t.name,
                "description": t.description,
                "approx_rows": t.approx_rows,
                "primary_key": t.primary_key,
                "columns": [{"name": c.name, "type": c.type} for c in t.columns],
                "foreign_keys": [
                    {"columns": f.columns, "references": f.referred_table, "referred_columns": f.referred_columns}
                    for f in t.foreign_keys
                ],
            }
            for t in tables
        ],
    }


def _connection_scan_inputs(
    conn_id: str,
    tables: str,
    custom_instructions: str | None,
    required_columns: str | None,
    exclude_columns: str | None,
    allow_large_semantic_scan: bool,
    ai_review_consent: bool,
    row_limit: int | None,
) -> _SchemaScanInputs:
    import json

    try:
        ids = json.loads(tables)
    except json.JSONDecodeError:
        ids = None
    if not isinstance(ids, list) or not ids or not all(isinstance(n, str) for n in ids):
        raise HTTPException(status_code=400, detail="tables must be a JSON list of table ids, at least one.")
    if len(ids) > 25:
        raise HTTPException(status_code=400, detail="Pick at most 25 tables per scan.")
    source, _saved = _source_for(conn_id)
    try:
        extracted = _call_connector(source.extract_selected, list(dict.fromkeys(ids)), row_limit=row_limit)
    finally:
        source.close()
    names = [e.table.table_name for e in extracted]
    if len(set(names)) != len(names):
        raise HTTPException(status_code=400, detail="Two selected tables have the same name; pick one of them.")
    notes: list[str] = []
    for e in extracted:
        notes.extend(e.warnings())
    return _prepare_schema_scan(
        [], custom_instructions, required_columns, exclude_columns,
        allow_large_semantic_scan, ai_review_consent,
        tables_override=[e.table for e in extracted], extra_warnings=notes,
    )


@app.post("/connections/{conn_id}/scan", response_model=SchemaScanResponse, dependencies=[Depends(_require_access_token)])
def scan_connection(
    conn_id: str,
    tables: str = Form(..., description="JSON list of table ids to scan."),
    custom_instructions: str | None = Form(default=None),
    max_semantic_llm_calls: int | None = Form(default=None),
    allow_large_semantic_scan: bool = Form(default=False),
    max_cross_table_llm_calls: int | None = Form(default=None),
    required_columns: str | None = Form(default=None),
    ai_review_consent: bool = Form(default=False),
    exclude_columns: str | None = Form(default=None),
    row_limit: int | None = Form(default=None),
) -> SchemaScanResponse:
    inputs = _connection_scan_inputs(
        conn_id, tables, custom_instructions, required_columns, exclude_columns,
        allow_large_semantic_scan, ai_review_consent, row_limit,
    )
    return _execute_schema_scan(inputs, max_semantic_llm_calls, max_cross_table_llm_calls)


@app.post("/connections/{conn_id}/scan/jobs", response_model=ScanJobSubmitResponse, dependencies=[Depends(_require_access_token)])
def submit_connection_scan_job(
    conn_id: str,
    tables: str = Form(..., description="JSON list of table ids to scan."),
    custom_instructions: str | None = Form(default=None),
    max_semantic_llm_calls: int | None = Form(default=None),
    allow_large_semantic_scan: bool = Form(default=False),
    max_cross_table_llm_calls: int | None = Form(default=None),
    required_columns: str | None = Form(default=None),
    ai_review_consent: bool = Form(default=False),
    exclude_columns: str | None = Form(default=None),
    row_limit: int | None = Form(default=None),
) -> ScanJobSubmitResponse:
    """Same submit/poll contract as /scans/schema/jobs: poll
    GET /scans/jobs/{job_id}."""
    inputs = _connection_scan_inputs(
        conn_id, tables, custom_instructions, required_columns, exclude_columns,
        allow_large_semantic_scan, ai_review_consent, row_limit,
    )
    job = _create_job_or_429()
    callback = progress_callback_for(job.job_id)

    def _execute() -> dict:
        return _execute_schema_scan(
            inputs, max_semantic_llm_calls, max_cross_table_llm_calls,
            progress_callback=callback, job_id=job.job_id,
        ).model_dump()

    submit_job(job.job_id, _execute)
    return ScanJobSubmitResponse(job_id=job.job_id)
