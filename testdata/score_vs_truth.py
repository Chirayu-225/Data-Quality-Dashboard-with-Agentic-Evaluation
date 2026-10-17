"""Compare a saved scan result with the planted-issue truth file.

Usage:  python score_vs_truth.py result.json leads_300_truth.json

`result.json` is either the raw /scans response or the
GET /scans/jobs/<id> body (the scan is under "result").

Read this as a "was each issue type caught AT ALL" check. Precision on
a file with a deliberately raised share of semantic issues does not
reflect real-world rates, so only recall per type and the trap
false-positive counts are meaningful here."""
import json, sys
from collections import Counter

res = json.load(open(sys.argv[1], encoding="utf-8"))
res = res.get("result", res) if isinstance(res.get("result", None), dict) else res
truth = json.load(open(sys.argv[2], encoding="utf-8"))

flags = {}
for fr in res["flagged_rows"]:
    flags.setdefault(fr["row_index"], []).append(fr)

def det_checks(i):
    out = set()
    for fr in flags.get(i, []):
        for r in fr.get("deterministic_reasons") or []:
            out.add(r.split(":")[0].strip())
    return out

def semantic(i):
    return any(fr.get("semantic_reason") for fr in flags.get(i, []))

def security(i):
    return any("SECURITY" in " ".join(fr.get("deterministic_reasons") or []) for fr in flags.get(i, []))

quarantined = {f.get("row_index") for f in (res.get("security_findings") or [])}

print(f"rows={res['total_rows']}  overall={res['overall_score']}  llm_used={res['llm_used']}")
print(f"semantic_rows_considered={res.get('semantic_rows_considered')}  quarantined={res.get('quarantined_row_count')}")
for w in res.get("warnings") or []:
    print("WARNING:", w[:300])
print("versions:", res.get("versions"))
print()
print(f"{'issue type':26} {'planted':>7} {'caught':>7} {'recall':>7}  caught-by")
planted = truth["planted_issue_rows"]
all_planted = set()
for kind, rows in planted.items():
    if not isinstance(rows, list):
        continue
    all_planted.update(rows)
    caught, by = 0, Counter()
    # null_check is the correct catch for blank/sentinel cells, but for
    # every other type it is just the blank-notes noise and must not
    # count as having caught the planted issue.
    ignore = set() if kind in ("blank_required", "amount_sentinel") else {"null_check"}
    for i in rows:
        checks = det_checks(i) - ignore
        hit = bool(checks) or semantic(i) or i in quarantined or security(i)
        caught += hit
        for c in checks:
            by[c] += 1
        if semantic(i):
            by["semantic"] += 1
        if i in quarantined or security(i):
            by["security"] += 1
    n = len(rows)
    print(f"{kind:26} {n:7d} {caught:7d} {caught / n:7.0%}  {dict(by)}")

print("\nTRAPS (rows that must NOT be flagged), flags other than the blank-notes null_check:")
for kind, rows in truth["traps_must_not_be_flagged"].items():
    bad = [i for i in rows if i not in all_planted and (det_checks(i) - {"null_check"} or semantic(i))]
    print(f"  {kind:24} {len(rows):4d} rows   falsely flagged: {len(bad)}  {bad[:8]}")

unplanted = [i for i in flags if i not in all_planted and (det_checks(i) - {"null_check"} or semantic(i))]
print(f"\nUnplanted rows flagged by something other than null_check: {len(unplanted)} {sorted(unplanted)[:15]}")
print("(null_check on unplanted rows is the blank-optional-column noise, reported separately)")
