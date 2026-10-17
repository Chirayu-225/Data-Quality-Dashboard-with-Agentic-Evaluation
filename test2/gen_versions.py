"""Generates leads_v1/v2/v3.csv: forged data with KNOWN changes between versions,
plus versions_truth.json. Synthetic by design: "real scans of forged data with known changes"."""
import csv, json, math, random

SEED = 20261004
rng = random.Random(SEED)
N1 = 1000

FIRST = ["James","Mary","Robert","Patricia","John","Jennifer","Michael","Linda","David","Elizabeth",
 "William","Barbara","Richard","Susan","Joseph","Jessica","Thomas","Sarah","Charles","Karen",
 "Daniel","Nancy","Matthew","Betty","Anthony","Sandra","Mark","Margaret","Steven","Emily",
 "Priya","Arjun","Ananya","Rohan","Wei","Mei","Hiroshi","Yuki","Carlos","Sofia","Mateo","Lucia",
 "Omar","Fatima","Lars","Ingrid","Pierre","Camille","Kwame","Ama","Vikram","Divya","Kavya","Rahul"]
LAST = ["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Martinez","Wilson",
 "Anderson","Taylor","Moore","Jackson","Martin","Lee","Thompson","White","Harris","Clark",
 "Lewis","Walker","Young","King","Wright","Scott","Nguyen","Hill","Green","Baker","Sharma",
 "Patel","Iyer","Reddy","Chen","Wang","Tanaka","Silva","Costa","Khan","Berg","Dubois","Mensah",
 "Fischer","Rossi","Nair","Menon","Gupta","Kapoor","Rao"]
PRE = ["North","Blue","Summit","Apex","Iron","Pacific","Atlas","Bright","Cedar","Delta","Falcon",
 "Granite","Harbor","Keystone","Meridian","Nova","Orion","Pioneer","Redwood","Sterling","Titan","Zenith"]
SUF = ["Logistics","Manufacturing","Analytics","Foods","Energy","Systems","Health","Labs",
 "Industries","Supply","Robotics","Freight","Pharma","Metals","Software"]
NOTES = ["interested in renewal","requested pricing","follow up next quarter","sent brochure",
 "asked about bulk discount","budget approved","wants a demo","needs security review",
 "expanding to new site","trial extended"]

companies = sorted({f"{rng.choice(PRE)} {rng.choice(SUF)}" for _ in range(400)})[:150]
def domain(c): return c.lower().replace(" ", "") + ".com"

used_people, used_emails, used_phones = set(), set(), set()
def person():
    while True:
        f, l, c = rng.choice(FIRST), rng.choice(LAST), rng.choice(companies)
        if (f, l, c) not in used_people:
            used_people.add((f, l, c)); return f, l, c
def email_for(f, l, c):
    base, k = f"{f.lower()}.{l.lower()}", 2
    e = f"{base}@{domain(c)}"
    while e in used_emails: e = f"{base}{k}@{domain(c)}"; k += 1
    used_emails.add(e); return e
def phone():
    while True:
        d = "555" + f"{rng.randint(0, 9999999):07d}"
        if d not in used_phones:
            used_phones.add(d)
            return rng.choice([f"{d[:3]}-{d[3:6]}-{d[6:]}", f"({d[:3]}) {d[3:6]}-{d[6:]}", f"+1 {d[:3]} {d[3:6]} {d[6:]}"])
def zip_code(): return f"0{rng.randint(1000,9999)}" if rng.random() < 0.2 else str(rng.randint(10000, 99999))

V1_COUNTRY = (["US","CA","GB","IN","DE"], [70,10,8,8,4])
ADD_COUNTRY = (["US","CA","GB","IN","DE"], [4,1,0,75,20])   # deliberate category drift
DISCOUNT = ["Volume","Seasonal","Loyalty","Promo","None"]

def make_row(rid, country_dist, amount_fn, phone_blank_p=0.0):
    f, l, c = person()
    return {"id": rid, "first_name": f, "last_name": l, "company": c, "email": email_for(f, l, c),
            "phone": "" if rng.random() < phone_blank_p else phone(),
            "amount": str(amount_fn()), "country_code": rng.choices(*country_dist)[0],
            "discount_type": rng.choice(DISCOUNT), "zip": zip_code(),
            "notes": rng.choice(NOTES) if rng.random() < 0.7 else ""}

v1_amount = lambda: int(rng.lognormvariate(math.log(12000), 0.6))
add_amount = lambda: int(rng.lognormvariate(math.log(32000), 0.5))   # deliberate amount drift

# ---------------- v1 ----------------
v1 = {i: make_row(i, V1_COUNTRY, v1_amount) for i in range(1, N1 + 1)}
ids = list(v1); rng.shuffle(ids)
issues = {k: [] for k in ["duplicate_email","amount_sentinel","amount_garbage","blank_required",
                          "semantic_mismatch","amount_outlier"]}
it = iter(ids)
for _ in range(10):
    dup = next(it); orig = next(it)
    if dup < orig: dup, orig = orig, dup
    v1[dup]["email"] = v1[orig]["email"]; issues["duplicate_email"].append(dup)
for _ in range(20): i = next(it); v1[i]["amount"] = rng.choice(["N/A","-","null"]); issues["amount_sentinel"].append(i)
for _ in range(8): i = next(it); v1[i]["amount"] = rng.choice(["abc","??","twelve thousand"]); issues["amount_garbage"].append(i)
for _ in range(15): i = next(it); v1[i][rng.choice(["email","company"])] = ""; issues["blank_required"].append(i)
MIS = [("first_name","Wire Transfer Dept"),("company","asdfgh"),("first_name","Accounts Payable"),
       ("last_name","00000"),("company","test test test"),("first_name","DO NOT USE"),("company","qwerty llc"),("company","xxxxx")]
for k in range(8): i = next(it); v1[i][MIS[k][0]] = MIS[k][1]; issues["semantic_mismatch"].append(i)
for _ in range(4): i = next(it); v1[i]["amount"] = "99999999"; issues["amount_outlier"].append(i)
issue_ids = {i for v in issues.values() for i in v} | {i for i in ids if any(v1[i]["email"] == v1[d]["email"] for d in issues["duplicate_email"])}
clean_ids = [i for i in it]  # remaining, untouched rows

# ---------------- v2 ----------------
v2 = {i: dict(r) for i, r in v1.items()}
removed_v2 = sorted(clean_ids[:50]); pool = clean_ids[50:]
for i in removed_v2: del v2[i]
modified_v2 = {}
for i in pool[:40]:
    v2[i]["amount"] = str(int(int(v2[i]["amount"]) * rng.uniform(1.1, 1.5))); modified_v2[i] = ["amount"]
for i in pool[40:50]:
    r = v2[i]; used_emails.discard(r["email"])
    r["email"] = email_for(r["first_name"], r["last_name"], "newdomain"); modified_v2[i] = ["email"]
for i in pool[50:60]:
    v2[i]["company"] = rng.choice([c for c in companies if c != v2[i]["company"]]); modified_v2[i] = ["company"]
added_v2 = list(range(N1 + 1, N1 + 251))
for i in added_v2: v2[i] = make_row(i, ADD_COUNTRY, add_amount, phone_blank_p=0.3)
untouched_v2 = sorted(set(v2) - set(added_v2) - set(modified_v2))

# ---------------- v3 (schema change vs v2) ----------------
remaining_pool = pool[60:]
removed_v3 = sorted(remaining_pool[:10])
INDUSTRY = {"Logistics":"Transportation","Freight":"Transportation","Manufacturing":"Industrial","Industries":"Industrial",
 "Metals":"Industrial","Robotics":"Industrial","Analytics":"Technology","Systems":"Technology","Software":"Technology",
 "Labs":"Healthcare","Health":"Healthcare","Pharma":"Healthcare","Foods":"Food & Beverage","Energy":"Energy","Supply":"Wholesale"}
v3 = {}
for i, r in v2.items():
    if i in removed_v3: continue
    amt = r["amount"]
    v3[i] = {"id": i, "company": r["company"], "industry": INDUSTRY[r["company"].split()[-1]] if r["company"] in companies else "",
             "first_name": r["first_name"], "last_name": r["last_name"], "email": r["email"], "phone_number": r["phone"],
             "amount": f"${int(amt):,}.00" if amt.isdigit() else amt,
             "country_code": r["country_code"], "discount_type": r["discount_type"], "notes": r["notes"]}

# ---------------- write ----------------
V12_COLS = ["id","first_name","last_name","company","email","phone","amount","country_code","discount_type","zip","notes"]
V3_COLS = ["id","company","industry","first_name","last_name","email","phone_number","amount","country_code","discount_type","notes"]
def write(path, rows, cols, shuffle):
    order = list(rows.values())
    if shuffle: rng.shuffle(order)
    else: order.sort(key=lambda r: r["id"])
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols); w.writeheader(); w.writerows(order)
    return [r["id"] for r in order]
order1 = write("leads_v1.csv", v1, V12_COLS, False)
order2 = write("leads_v2.csv", v2, V12_COLS, True)
order3 = write("leads_v3.csv", v3, V3_COLS, True)

# ---------------- reference drift numbers ----------------
EPS = 1e-4
def psi(p, q): return sum((pi - qi) * math.log(pi / qi) for pi, qi in zip(p, q))
def cat_psi(a, b):
    cats = sorted(set(a) | set(b))
    p = [max(a.count(c) / len(a), EPS) for c in cats]; q = [max(b.count(c) / len(b), EPS) for c in cats]
    return round(psi(q, p), 4)
def num_psi(a, b, bins=10):
    a = sorted(a); edges = [a[int(len(a) * k / bins)] for k in range(1, bins)]
    def hist(x):
        h = [0] * bins
        for v in x: h[sum(v >= e for e in edges)] += 1
        return [max(c / len(x), EPS) for c in h]
    return round(psi(hist(b), hist(a)), 4)
def nums(rows): return [int(r["amount"]) for r in rows.values() if r["amount"].isdigit() and r["amount"] != "99999999"]
def col(rows, c): return [r[c] for r in rows.values()]
def null_rate(rows, c): return round(sum(1 for r in rows.values() if r[c] == "") / len(rows), 4)

truth = {
  "disclosure": "Synthetic (forged) data with known, deliberately planted changes. Scans of these files are real; the data is not.",
  "generator_seed": SEED,
  "key_column": "id (stable across all versions; prefer ids over row indices, since v2 and v3 are shuffled)",
  "row_order": {"v1": "sorted by id", "v2": "shuffled", "v3": "shuffled", "purpose": "comparison must match rows by key, never by position"},
  "row_counts": {"v1": len(v1), "v2": len(v2), "v3": len(v3)},
  "v1_quality_issues_by_id": {k: sorted(v) for k, v in issues.items()},
  "v1_note": "v1 quality issues carry forward unchanged into v2 (none of the issue rows were removed or modified)",
  "v1_to_v2": {
    "removed_ids": removed_v2, "added_ids": added_v2,
    "modified": {"count": len(modified_v2), "changed_fields_by_id": {str(k): v for k, v in sorted(modified_v2.items())}},
    "unchanged_count": len(untouched_v2),
    "expected_summary": f"{len(untouched_v2)} unchanged, {len(modified_v2)} modified, {len(added_v2)} added, {len(removed_v2)} removed",
    "planted_drift": {
      "country_code": "added rows use a different mix (IN 75%, DE 20%, US 4%) vs v1 (US 70%)",
      "amount": "added rows drawn from a higher distribution (median ~32k vs ~12k); 40 existing rows raised 10-50%",
      "phone_null_rate": "30% of added rows have blank phone; existing rows unchanged"},
    "control_columns_expected_stable": ["discount_type"],
    "reference_metrics": {
      "psi_method": "PSI = sum((new-old)*ln(new/old)); epsilon 1e-4 floor; numeric uses 10 quantile bins from v1, excludes non-numeric and 99999999 outliers",
      "psi_country_code": cat_psi(col(v1,"country_code"), col(v2,"country_code")),
      "psi_amount": num_psi(nums(v1), nums(v2)),
      "psi_discount_type_control": cat_psi(col(v1,"discount_type"), col(v2,"discount_type")),
      "phone_null_rate": {"v1": null_rate(v1,"phone"), "v2": null_rate(v2,"phone")},
      "volume_change_pct": round(100 * (len(v2) - len(v1)) / len(v1), 1)},
    "note": "reference PSI values depend on binning choices; compare direction and band (below 0.10 / 0.10-0.20 / above 0.20), not exact digits"},
  "v2_to_v3": {
    "schema_changes": {
      "renamed": {"phone": "phone_number"}, "added_columns": ["industry"], "removed_columns": ["zip"],
      "format_change": {"amount": "plain integers became currency strings like $12,345.00; underlying values unchanged"},
      "column_order": "changed (company and industry moved next to id)"},
    "rename_detection_hint": "phone and phone_number share ~100% of their values across matched ids; a rename detector should propose this rather than report one removed and one added column",
    "removed_ids": removed_v3, "added_ids": [],
    "value_changes": "none intended beyond the amount formatting; after normalizing currency, amount values match v2 exactly",
    "industry_note": "industry is derived from the company name suffix; semantic-mismatch rows with junk company names get a blank industry"},
}
with open("versions_truth.json", "w", encoding="utf-8") as fh: json.dump(truth, fh, indent=2)
print(json.dumps({k: truth[k] for k in ["row_counts"]}, indent=1))
print(truth["v1_to_v2"]["expected_summary"]); print(json.dumps(truth["v1_to_v2"]["reference_metrics"], indent=1))
