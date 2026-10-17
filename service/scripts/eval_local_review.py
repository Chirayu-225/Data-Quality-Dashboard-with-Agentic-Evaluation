"""Score the local (no-LLM) value reviewer against a planted-issue truth file.

Usage:  python scripts/eval_local_review.py data.csv truth.json

Reports recall on the planted semantic_mismatch rows and every flag that
lands on a row that is not planted for ANY issue type (false flags), plus
flags on trap rows. Nothing leaves the process.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.connectors.base import ConnectorConfig  # noqa
from app.connectors.csv_connector import CSVConnector
from app.agent.local_value_review import review_table_locally

def load(path):
    cfg = ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": str(path)})
    return CSVConnector(cfg).extract()[0]

if __name__ == "__main__":
    table = load(sys.argv[1])
    truth = json.load(open(sys.argv[2], encoding="utf-8"))
    planted = truth["planted_issue_rows"]
    # prompt-injection rows are quarantined upstream, never reviewed
    quarantined = set(planted.get("prompt_injection", []))
    res = review_table_locally(table, exclude_indices=quarantined)
    flagged = {f.row_index for f in res.flags}
    sem = set(planted["semantic_mismatch"])
    print(f"columns reviewed: {res.columns_reviewed}")
    print(f"columns skipped : {res.columns_skipped}")
    print(f"distinct values scored: {res.distinct_values_scored}   external values sent: {res.values_sent_externally}")
    print(f"semantic_mismatch recall: {len(sem & flagged)}/{len(sem)}")
    for i in sorted(sem):
        print("  ", "HIT " if i in flagged else "MISS", i, {k: table.rows[i][k] for k in ("first_name","last_name","company") if k in table.rows[i]})
    all_planted = {i for k, v in planted.items() if isinstance(v, list) for i in v}
    extra = sorted(flagged - all_planted)
    print(f"flags on rows planted for NO issue: {len(extra)}")
    for f in res.findings:
        if any(i in extra for i in f.row_indices):
            print("   FALSE?", f.column, repr(f.value), f.signals, f.confidence)
    other = sorted((flagged & all_planted) - sem)
    print(f"flags on rows planted for another issue type: {other}")
    traps = truth.get("traps_must_not_be_flagged", {})
    for k, rows in traps.items():
        print(f"  trap {k:22} flagged: {sorted(set(rows) & flagged)}")
