"""Local value review: the no-LLM semantic tier.

These tests pin down what it flags, what it must NOT flag (legitimate
values that merely look unusual), and the privacy property that makes it
the default: it never reaches a network or an LLM client.
"""
import io

import pytest
from fastapi.testclient import TestClient

from app.agent.generator import GeneratorAgent
from app.agent.local_value_review import review_table_locally
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType
from app.main import app

client = TestClient(app)

FIRST = ["Aisha", "Wei", "Maria", "John", "Yuki", "Kwame", "Priya", "Lars", "Ahmed", "Sofia"]
LAST = ["Clark", "Nguyen", "Garcia", "Smith", "Tanaka", "Mensah", "Patel", "Berg", "Khan", "Rossi"]
COMPANY = ["Orion Textiles", "Atlas Foods", "Blue Metals", "Titan Health", "Nova Labs"]


def _table(rows_extra: list[dict] | None = None, n: int = 40) -> CanonicalTable:
    rows = [
        {
            "first_name": FIRST[i % 10],
            "last_name": LAST[(i * 3) % 10],
            "company": COMPANY[i % 5],
            "email": f"user{i}@example.com",
            "notes": "follow up next quarter",
            "country_code": "NA" if i % 8 == 0 else "US",
        }
        for i in range(n)
    ]
    for extra in rows_extra or []:
        rows.append({**rows[0], **extra})
    return CanonicalTable(
        tenant_id="t",
        source_id="s",
        table_name="leads",
        columns=[
            ColumnSchema(name="first_name", type=ColumnType.STRING),
            ColumnSchema(name="last_name", type=ColumnType.STRING),
            ColumnSchema(name="company", type=ColumnType.STRING),
            ColumnSchema(name="email", type=ColumnType.STRING, semantic_hint="email"),
            ColumnSchema(name="notes", type=ColumnType.STRING),
            ColumnSchema(name="country_code", type=ColumnType.STRING),
        ],
        rows=rows,
    )


def _flagged(table, **kw):
    return {f.row_index for f in review_table_locally(table, **kw).flags}


def test_clean_table_has_no_flags():
    assert _flagged(_table()) == set()


@pytest.mark.parametrize(
    "field,value",
    [
        ("company", "zzzzzz"),          # repeated character
        ("company", "qwerty inc"),      # keyboard row
        ("company", "test test test"),  # placeholder + repeated token
        ("last_name", "99999"),         # digits in a text column
        ("first_name", "J0hn"),         # digit inside a word
        ("last_name", "Smith#"),        # symbol in a name column
        ("first_name", "Billing Dept"), # role word in a person-name column
        ("company", "Lorem Ipsum"),     # placeholder words
    ],
)
def test_flags_obvious_bad_values(field, value):
    t = _table([{field: value}])
    assert len(t.rows) - 1 in _flagged(t)


def test_role_word_only_flagged_in_person_name_columns():
    # "Sales" is a perfectly good word inside a company name.
    t = _table([{"company": "Sales Force Partners"}])
    assert len(t.rows) - 1 not in _flagged(t)


def test_legitimate_unusual_values_are_not_flagged():
    t = _table(
        [
            {"first_name": "Nguyen-Smith"},      # hyphenated
            {"last_name": "O'Brien"},            # apostrophe
            {"company": "Nova Labs"},            # existing value
            {"first_name": "Kwame"},             # rare but plausible
        ]
    )
    assert _flagged(t) == set()


def test_valid_iso_code_and_shared_values_are_not_flagged():
    # "NA" (Namibia) repeats across rows; common values are normal.
    t = _table()
    assert sum(1 for r in t.rows if r["country_code"] == "NA") >= 3
    assert _flagged(t) == set()


def test_free_text_and_email_columns_are_skipped_with_a_reason():
    res = review_table_locally(_table([{"notes": "asdfgh qwerty"}]))
    assert "notes" in res.columns_skipped
    assert "email" in res.columns_skipped
    assert "notes" not in res.columns_reviewed
    assert res.flags == []


def test_quarantined_rows_are_never_reviewed():
    t = _table([{"company": "zzzzzz"}])
    bad = len(t.rows) - 1
    assert bad in _flagged(t)
    assert bad not in _flagged(t, exclude_indices={bad})


def test_every_row_holding_a_bad_value_is_flagged():
    t = _table([{"company": "xxxxx"}, {"company": "xxxxx"}])
    n = len(t.rows)
    assert {n - 2, n - 1} <= _flagged(t)


def test_nothing_is_reported_as_sent_externally():
    res = review_table_locally(_table([{"company": "zzzzzz"}]))
    assert res.values_sent_externally == 0
    assert res.distinct_values_scored > 0


def test_generator_runs_local_review_when_ai_is_off():
    t = _table([{"company": "asdfgh"}])
    events = []
    result = GeneratorAgent().run(
        t, local_review=True, stage_callback=lambda *a: events.append(a)
    )
    assert any(f.row_index == len(t.rows) - 1 for f in result.semantic_flags)
    assert result.semantic_iterations == 1
    assert result.semantic_rows_considered == len(t.rows)
    assert result.local_review is not None and result.local_review.values_sent_externally == 0
    semantic_done = [e for e in events if e[0] == "semantic" and e[1] == "done"]
    assert semantic_done and "no data left" in (semantic_done[0][3] or "")


def test_generator_without_local_review_is_unchanged():
    result = GeneratorAgent().run(_table([{"company": "asdfgh"}]))
    assert result.semantic_flags == [] and result.semantic_iterations == 0
    assert result.local_review is None


def _csv(rows):
    out = io.StringIO()
    import csv

    csv.writer(out).writerows(rows)
    return out.getvalue().encode()


def test_api_ai_off_reports_local_review_and_zero_egress(monkeypatch):
    monkeypatch.setenv("LOCAL_REVIEW", "1")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rows = [["first_name", "company"]] + [[FIRST[i % 10], COMPANY[i % 5]] for i in range(30)]
    rows.append(["Maria", "asdfgh"])
    resp = client.post("/scans", files={"file": ("leads.csv", io.BytesIO(_csv(rows)), "text/csv")})
    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_used"] is False
    assert body["data_egress"]["external_ai_used"] is False
    assert body["data_egress"]["rows_sent_to_provider"] == 0
    assert body["local_review"]["values_sent_externally"] == 0
    assert body["local_review"]["distinct_values_scored"] > 0
    flagged = {r["row_index"]: r for r in body["flagged_rows"]}
    assert 30 in flagged and flagged[30]["semantic_reason"]
    accuracy = next(m for m in body["metric_scores"] if m["metric"] == "accuracy")
    assert accuracy["evaluated"] is True


def test_api_local_review_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("LOCAL_REVIEW", "0")
    rows = [["first_name", "company"]] + [[FIRST[i % 10], COMPANY[i % 5]] for i in range(30)]
    rows.append(["Maria", "asdfgh"])
    resp = client.post("/scans", files={"file": ("leads.csv", io.BytesIO(_csv(rows)), "text/csv")})
    body = resp.json()
    assert body["local_review"] is None
    assert body["semantic_iterations"] == 0


def test_api_reports_accuracy_basis_for_each_mode(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    rows = [["first_name", "company"]] + [[FIRST[i % 10], COMPANY[i % 5]] for i in range(30)]
    body_bytes = _csv(rows)
    monkeypatch.setenv("LOCAL_REVIEW", "1")
    b = client.post("/scans", files={"file": ("l.csv", io.BytesIO(body_bytes), "text/csv")}).json()
    assert b["accuracy_basis"]["mode"] == "local_rules_only"
    assert b["accuracy_basis"]["detectors"] == ["local_value_review"]
    assert b["accuracy_basis"]["comparable_across_modes"] is False
    monkeypatch.setenv("LOCAL_REVIEW", "0")
    b = client.post("/scans", files={"file": ("l.csv", io.BytesIO(body_bytes), "text/csv")}).json()
    assert b["accuracy_basis"]["mode"] == "not_evaluated"


def test_real_world_names_are_not_flagged():
    """Surnames/names that resemble the heuristics' vocabulary or character
    patterns but are ordinary: occupational surnames, words that are also
    placeholders, consonant clusters, diacritics, repeated-part names."""
    names = [
        "Baker", "Cook", "Taylor", "Dean", "Judge", "Bishop", "Major", "Service", "Sales", "Bar", "Foo",
        "Sample", "Wertheim", "Wertz", "Liberty", "Haverty", "Doherty", "Karstula", "Mei Mei", "Tom Tom",
        "Szczepański", "Przybylski", "Nguyễn", "Llewellyn", "Gruffydd", "Dvořák", "Ndlovu", "Mbeki",
        "Þórsdóttir", "Hämäläinen", "Çelik", "O'Sullivan", "Al-Rashid", "van der Berg", "de la Cruz",
        "张伟", "김민준", "सुब्रमण्यम", "Баранов",
    ]
    t = _table([{"last_name": n} for n in names])
    assert _flagged(t) == set()


def test_repeated_word_needs_three_parts_to_flag():
    t = _table([{"company": "Yum Yum"}, {"company": "go go go"}])
    n = len(t.rows)
    flagged = _flagged(t)
    assert (n - 2) not in flagged and (n - 1) in flagged
