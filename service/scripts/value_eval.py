"""Generate a FRESH evaluation file for the local value reviewer, run the
reviewer on it, and report recall per category and false flags, each with
a 95% Wilson interval.

    python scripts/value_eval.py --seed 101 [--out-dir DIR] [--no-run]

Rules of use (they are what keep the number honest):
- A seed you have looked at the misses of is no longer held out. After
  changing any rule, use a NEW seed.
- Clean rows come from the Faker library across ~110 locales (names,
  companies, cities, job titles in many scripts), i.e. NOT from the
  author's own word lists, plus a few dozen hand-listed traps that are
  known to resemble the heuristics (occupational surnames, repeated-part
  names, consonant-cluster names).
- Planted values are grouped by what a heuristic can plausibly see:
    structural   - detectable from the characters alone
    vocabulary   - detectable only because the word is in a built-in list
                   (recall here is high BY CONSTRUCTION; do not quote it
                   as generalisation)
    needs_context- template phrases, plausible fake names, single
                   characters, wrong-field values: expected to be missed
  Quote per-category numbers, never one blended percentage.

Requires: pip install faker
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from faker import Faker  # noqa: E402
from faker.config import AVAILABLE_LOCALES  # noqa: E402

KEY_ROWS = ["qwertyuiop", "asdfghjkl", "zxcvbnm"]
SYLL = ["zor", "blax", "quin", "tex", "vex", "dyn", "mar", "lum", "kor", "zen", "ax", "ion", "pri", "vol", "tra", "nex"]
TRAP_NAMES = [
    # occupational / word surnames
    "Baker", "Cook", "Taylor", "Dean", "Judge", "Bishop", "Major", "Marshall", "Page", "Bird", "Fox",
    "Service", "Sales", "Bar", "Foo", "Sample", "Temp", "Demo", "Church", "Pope", "Knight", "King",
    # repeated parts / short syllables
    "Mei Mei", "Lulu", "Gao Gao", "Bora Bora", "Ah Ah", "Wang Wang", "Tom Tom",
    # consonant clusters / diacritics from many traditions
    "Szczepański", "Przybylski", "Krzyżanowski", "Wójcik", "Nguyễn", "Trương", "Llewellyn", "Gruffydd",
    "Rhys", "Wynne", "Pwyll", "Dvořák", "Tsvetkov", "Ndlovu", "Mbeki", "Nkrumah", "Ng", "Xu", "Wertheim",
    "Wertz", "Liberty", "Subramaniam", "Chakraborty", "Krishnamurthy", "Gopalakrishnan", "Þórsdóttir",
    "Hämäläinen", "Çelik", "O'Sullivan", "Al-Rashid", "Bin Salman", "van der Berg", "de la Cruz", "McGregor",
]
STRUCT_COLS = ["first_name", "last_name", "company", "city", "job_title"]
PERSON_COLS = ["first_name", "last_name"]


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def planted_pool(rng: random.Random, fk: dict) -> list[tuple[str, str, str, str]]:
    """(category, kind, column, value) — about 110 entries."""
    out: list[tuple[str, str, str, str]] = []

    def add(cat, kind, col_choices, value):
        out.append((cat, kind, rng.choice(col_choices), value))

    for _ in range(10):  # keyboard rows
        row = rng.choice(KEY_ROWS)
        row = row[::-1] if rng.random() < .3 else row
        i = rng.randint(0, len(row) - 6); L = rng.randint(5, min(9, len(row) - i))
        add("keyboard_mash", "structural", STRUCT_COLS[:4], row[i:i + L])
    for _ in range(10):  # repeated chars
        ch = rng.choice("abcdefghijklmnopqrstuvwxyz0123456789xz"); add("repeated_chars", "structural", STRUCT_COLS[:4], ch * rng.randint(4, 8))
    for _ in range(10):  # digits only
        add("digits_only", "structural", ["first_name", "last_name", "company", "city"], "".join(rng.choice("0123456789") for _ in range(rng.randint(4, 7))))
    for _ in range(10):  # digits inside a word (leet)
        base = fk["en_US"].first_name(); sw = {"o": "0", "i": "1", "e": "3", "a": "4", "s": "5"}
        v = "".join(sw.get(c.lower(), c) if rng.random() < .5 else c for c in base)
        v = v if v != base else base[:-1] + "0"
        add("digit_in_word", "structural", PERSON_COLS, v)
    for _ in range(10):  # stray symbols
        base = fk["en_US"].last_name(); pos = rng.randint(1, len(base) - 1)
        add("symbol_corruption", "structural", PERSON_COLS + ["company"], base[:pos] + rng.choice("#@$%^*=<>|~") + base[pos:])
    for _ in range(10):  # placeholder / test words (vocabulary)
        add("placeholder_words", "vocabulary", ["first_name", "last_name", "company"],
            rng.choice(["test account", "dummy user", "placeholder", "lorem ipsum", "tbd", "unknown", "testing", "test test test", "dummy dummy dummy", "unknown unknown"]))
    for _ in range(10):  # status markers (vocabulary)
        add("status_markers", "vocabulary", ["first_name", "last_name", "company"],
            rng.choice(["do not use", "deleted", "obsolete", "void", "inactive", "cancelled", "archived", "deprecated", "invalid entry", "duplicate record"]))
    for _ in range(10):  # role / department in a person-name column (vocabulary)
        add("role_in_person_name", "vocabulary", PERSON_COLS,
            rng.choice(["Billing Department", "Accounts Receivable", "Helpdesk", "HR Team", "Sales Team", "Customer Support", "Payroll", "Procurement", "Admin", "Finance Team"]))
    for _ in range(10):  # template phrases (needs context)
        add("template_phrase", "needs_context", ["first_name", "last_name", "company"],
            rng.choice(["Company Name Here", "Enter name", "Your Name", "First Name", "Last Name", "Name Surname", "Insert company", "No Name", "Customer", "Contact Person"]))
    for _ in range(10):  # plausible-sounding fake names (needs world knowledge)
        add("plausible_fake", "needs_context", ["company"], "".join(rng.sample(SYLL, 2)).capitalize() + rng.choice([" Industries", " Holdings", " Dynamics", " Group"]))
    for _ in range(5):  # single characters
        add("single_char", "needs_context", PERSON_COLS, rng.choice(["Q", "X", "-", "?", "."]))
    for _ in range(5):  # wrong-field values
        add("wrong_field", "needs_context", ["first_name", "company", "city"], rng.choice(["john.smith@acme.com", "555-0142", "https://acme.example", "12/03/2024"]))
    return out


def build(seed: int, n_rows: int = 700):
    rng = random.Random(seed)
    locs = [l for l in AVAILABLE_LOCALES if l not in ("fr_QC", "la")]
    fk = {l: Faker(l) for l in locs}
    for i, f in enumerate(fk.values()):
        f.seed_instance(seed * 1000 + i)

    def clean_row(i):
        f = fk[rng.choice(locs)]
        r = {"id": str(i + 1), "first_name": f.first_name(), "last_name": f.last_name(),
             "company": f.company(), "city": f.city(), "job_title": f.job()}
        if rng.random() < 0.12:
            r["last_name" if rng.random() < .6 else "first_name"] = rng.choice(TRAP_NAMES)
        return r

    rows = [clean_row(i) for i in range(n_rows)]
    pool = planted_pool(rng, fk)
    idxs = rng.sample(range(n_rows), len(pool))
    planted = []
    for i, (cat, kind, col, val) in zip(idxs, pool):
        rows[i][col] = val
        planted.append({"row": i, "category": cat, "kind": kind, "column": col, "value": val})
    return rows, planted


def run(rows, planted, seed):
    from app.agent.local_value_review import review_table_locally
    from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType

    cols = [ColumnSchema(name=n, type=ColumnType.STRING) for n in rows[0]]
    cols[0] = ColumnSchema(name="id", type=ColumnType.INTEGER)
    t = CanonicalTable(tenant_id="t", source_id="s", table_name="eval", columns=cols,
                       rows=[{**r, "id": int(r["id"])} for r in rows])
    res = review_table_locally(t)
    flagged_rows = {f.row_index for f in res.flags}
    planted_rows = {p["row"] for p in planted}
    cats: dict[str, list] = {}
    for p in planted:
        cats.setdefault((p["kind"], p["category"]), []).append(p)

    print(f"seed={seed}  rows={len(rows)}  planted={len(planted)}  clean={len(rows)-len(planted)}")
    print(f"columns reviewed: {res.columns_reviewed}\nskipped: {list(res.columns_skipped)}")
    print(f"values sent externally: {res.values_sent_externally}\n")
    print(f"{'kind':14}{'category':22}{'caught':>8}   95% interval")
    tot = {}
    for (kind, cat), ps in sorted(cats.items()):
        k = sum(1 for p in ps if p["row"] in flagged_rows); n = len(ps)
        lo, hi = wilson(k, n)
        print(f"{kind:14}{cat:22}{k:>4}/{n:<3}   {lo*100:4.0f}%-{hi*100:3.0f}%")
        a = tot.setdefault(kind, [0, 0]); a[0] += k; a[1] += n
    print()
    for kind, (k, n) in tot.items():
        lo, hi = wilson(k, n)
        print(f"  {kind:14} {k}/{n}   {lo*100:.0f}%-{hi*100:.0f}%")
    clean_flagged = sorted(flagged_rows - planted_rows)
    n_clean = len(rows) - len(planted)
    lo, hi = wilson(len(clean_flagged), n_clean)
    print(f"\nFALSE FLAGS: {len(clean_flagged)} of {n_clean} clean rows   95% interval {lo*100:.1f}%-{hi*100:.1f}%")
    seen = set()
    for f in res.findings:
        if any(i in clean_flagged for i in f.row_indices) and (f.column, f.value) not in seen:
            seen.add((f.column, f.value)); print("   ", f.column, repr(f.value), f.signals)
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--no-run", action="store_true")
    a = ap.parse_args()
    rows, planted = build(a.seed)
    if a.out_dir:
        d = Path(a.out_dir); d.mkdir(parents=True, exist_ok=True)
        with open(d / f"value_eval_{a.seed}.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
        (d / f"value_eval_{a.seed}_truth.json").write_text(json.dumps({"seed": a.seed, "planted": planted}, ensure_ascii=False, indent=1), encoding="utf-8")
    if not a.no_run:
        run(rows, planted, a.seed)
