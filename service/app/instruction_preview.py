"""
Instruction preview — "here is what I understood, and here is what it
would flag," shown BEFORE a full scan commits to anything.

The compile step already existed inside a scan (RuleCompiler); what was
missing is letting the user see its result and its effect first. Two
cases, with very different costs, and the response says which one it is:

- structured: the instruction became an exact comparison rule. It is
  executed over EVERY row deterministically — no further LLM call, the
  count is exact, and the same rule will flag the same rows in the full
  scan. Cost: 1 LLM call (the compile; it sees only the schema and the
  instruction text, never row data).
- semantic: the instruction needs judgment, so there is no exact count.
  A SMALL sample of rows (default 20) is judged with the instruction, so
  the user can see the kind of thing it catches. The numbers describe
  that sample only and are labelled as such; they are not a prediction
  for the whole file. Cost: 1 compile call + 1 sample call, over masked
  rows with quarantined (prompt-injection) rows excluded, exactly as in
  a real scan.

Nothing here changes a score or stores anything.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from app.agent.llm_clients import LLMRequestTooLargeError, LLMUnavailableError
from app.agent.pii_masking import mask_table_for_llm
from app.agent.semantic_reasoning import SemanticReasoner
from app.canonical.models import CanonicalTable
from app.checks.injection_detection import PromptInjectionCheck
from app.rules.compiler import RuleCompiler
from app.rules.executor import execute_rule

DEFAULT_SAMPLE_ROWS = 20
MIN_SAMPLE_ROWS = 5
MAX_SAMPLE_ROWS = 45
MAX_EXAMPLES = 5


class PreviewExample(BaseModel):
    row_index: int
    row_data: dict
    why: str


class SemanticSampleSummary(BaseModel):
    rows_checked: int
    rows_flagged: int
    # False when the sample call failed or its reply could not be read —
    # then rows_flagged = 0 means "unknown", not "nothing found".
    reliable: bool


class InstructionPreview(BaseModel):
    kind: Literal["structured", "semantic", "failed", "unavailable"]
    # What the system understood, in one plain sentence — the thing the
    # user confirms. Always present.
    understood_as: str
    # The compiled rule as inspectable data (structured only).
    rule: dict | None = None
    total_rows: int
    # structured only: exact, over every row.
    matched_rows: int | None = None
    # semantic only: describes the sample, never the whole file.
    semantic_sample: SemanticSampleSummary | None = None
    examples: list[PreviewExample] = []
    # Honest cost, in LLM calls this preview made.
    llm_calls: int = 0
    # True when the preview is an exact result for the full scan
    # (structured); False when it is only a sample (semantic).
    exact: bool = False
    warnings: list[str] = []


def _unavailable(table: CanonicalTable, reason: str, warnings: list[str]) -> InstructionPreview:
    return InstructionPreview(
        kind="unavailable",
        understood_as=reason,
        total_rows=len(table.rows),
        warnings=warnings,
    )


def preview_instruction(
    table: CanonicalTable,
    instruction: str,
    llm,
    *,
    mask_salt: str,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    exclude_columns: set[str] | None = None,
) -> InstructionPreview:
    sample_rows = max(MIN_SAMPLE_ROWS, min(MAX_SAMPLE_ROWS, sample_rows))
    warnings: list[str] = []

    compilation = RuleCompiler(llm).compile(table, instruction)
    llm_calls = 1

    if compilation.kind == "failed":
        return InstructionPreview(
            kind="failed",
            understood_as=compilation.description,
            total_rows=len(table.rows),
            llm_calls=llm_calls,
            warnings=[
                "The instruction could not be turned into a rule. In a full scan it would "
                "still be passed to the AI reviewer as plain guidance, but this preview "
                "cannot show what that would flag."
            ]
            + ([compilation.error] if compilation.error else []),
        )

    if compilation.kind == "structured" and compilation.compiled_rule is not None:
        result = execute_rule(table, compilation.compiled_rule)
        flagged = result.flagged_row_indices
        return InstructionPreview(
            kind="structured",
            understood_as=compilation.description or compilation.compiled_rule.human_readable_description,
            rule=compilation.compiled_rule.model_dump(mode="json"),
            total_rows=len(table.rows),
            matched_rows=len(flagged),
            examples=[
                PreviewExample(
                    row_index=i,
                    row_data=table.display_row(i),
                    why=compilation.compiled_rule.human_readable_description,
                )
                for i in flagged[:MAX_EXAMPLES]
            ],
            llm_calls=llm_calls,
            exact=True,
            warnings=warnings,
        )

    # semantic
    guidance = compilation.semantic_instruction or instruction
    injection = PromptInjectionCheck().run(table)
    quarantined = {i for i in injection.flagged_row_indices if injection.row_severity.get(i, "high") == "high"}
    masked = mask_table_for_llm(table, exclude_columns or set(), salt=mask_salt)

    reliable = True
    flags = []
    rows_checked = 0
    try:
        reasoning = SemanticReasoner(llm, sample_size=sample_rows).run(
            masked, custom_instruction=guidance, exclude_indices=quarantined
        )
        llm_calls += 1
        rows_checked = reasoning.rows_sampled
        flags = reasoning.flags
        if reasoning.parse_failed:
            reliable = False
            warnings.append("The sample reply could not be read, so no flags are shown for it.")
            flags = []
    except (LLMUnavailableError, LLMRequestTooLargeError):
        reliable = False
        warnings.append("The AI reviewer was unavailable for the sample, so no flags are shown.")

    if quarantined:
        warnings.append(
            f"{len(quarantined)} row(s) look like a prompt-injection attempt and were left out of the sample."
        )
    valid_flags = [f for f in flags if 0 <= f.row_index < len(table.rows) and f.row_index not in quarantined]
    return InstructionPreview(
        kind="semantic",
        understood_as=compilation.description,
        total_rows=len(table.rows),
        semantic_sample=SemanticSampleSummary(
            rows_checked=rows_checked, rows_flagged=len(valid_flags) if reliable else 0, reliable=reliable
        ),
        examples=[
            PreviewExample(row_index=f.row_index, row_data=table.display_row(f.row_index), why=f.reason)
            for f in valid_flags[:MAX_EXAMPLES]
        ],
        llm_calls=llm_calls,
        exact=False,
        warnings=warnings
        + [
            f"This is a sample of {rows_checked} row(s), not a count for the whole file — "
            f"this instruction needs judgment, so only a full scan gives the real number."
        ]
        if reliable
        else warnings,
    )
