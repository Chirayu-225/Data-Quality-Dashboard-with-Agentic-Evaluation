import json

from app.agent.stability import assess, confidence_spread, decision_signature


def flags(*items):
    return json.dumps({"flags": [{"row_index": i, "reason": r, "confidence": c} for i, r, c in items]})


def test_identical_runs():
    r = [flags((1, "bad", 0.9))] * 5
    s = assess(r)
    assert s.label == "identical" and s.text_identical_rate == 1.0 and s.max_confidence_spread == 0.0


def test_wording_varies_but_decisions_stable():
    r = [flags((1, "Negative amount", 0.95)), flags((1, "Negative amount value", 0.95))] * 3
    s = assess(r)
    assert s.label == "decisions stable, wording varies" and s.decision_identical_rate == 1.0


def test_different_rows_flagged_is_unstable():
    r = [flags((1, "x", 0.9))] * 4 + [flags((1, "x", 0.9), (2, "y", 0.9))]
    s = assess(r)
    assert s.label == "unstable decisions" and abs(s.decision_identical_rate - 0.8) < 1e-9


def test_too_few_runs_never_gets_a_stability_label():
    s = assess([flags((1, "x", 0.9))] * 3)
    assert s.label == "insufficient samples"


def test_failed_calls_shrink_the_sample_and_say_so():
    s = assess([flags((1, "x", 0.9))] * 4, attempted=6)
    assert s.label == "insufficient samples" and any("failed" in x for x in s.reasons)


def test_unparseable_replies_are_reported_not_hidden():
    s = assess(["nope"] * 5)
    assert s.label == "unparseable"
    s2 = assess([flags((1, "x", 0.9))] * 4 + ["not json"])
    assert s2.label == "unstable decisions"


def test_empty_flag_list_is_a_valid_stable_decision():
    s = assess(['{"flags": []}'] * 5)
    assert s.label == "identical" and decision_signature('{"flags": []}') == frozenset()


def test_confidence_spread_uses_only_items_present_in_every_run():
    a, b = flags((1, "x", 0.90), (2, "y", 0.5)), flags((1, "x", 0.98))
    assert abs(confidence_spread([a, b]) - 0.08) < 1e-9


def test_verification_label_change_counts_as_a_different_decision():
    a = json.dumps({"verifications": [{"row_index": 1, "label": "confirmed", "notes": ""}]})
    b = json.dumps({"verifications": [{"row_index": 1, "label": "rejected", "notes": ""}]})
    assert decision_signature(a) != decision_signature(b)
