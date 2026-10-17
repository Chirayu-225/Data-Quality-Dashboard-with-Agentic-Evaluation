"""One-call check of the 'valid code that looks like a placeholder' rule on
a DIFFERENT file than the 300-row test (so the rule is not tuned to it).
Builds a small synthetic customer table, sends ONE batch to Groq with the
production prompt, and prints which rows were flagged.

    python scripts/check_iso_rule.py

Expect: rows with country 'NA', 'IN', 'NO' NOT flagged; the row with
country 'Atlantis' flagged. Row content is synthetic, nothing real is sent."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.llm_clients import GroqClient  # noqa: E402
from app.agent.semantic_reasoning import SemanticReasoner  # noqa: E402
from app.canonical.models import CanonicalTable, ColumnSchema, ColumnType  # noqa: E402

countries = ["UK", "IN", "US", "NA", "NO", "CA", "DE", "NA", "IN", "FR", "UK", "US", "Atlantis", "IN", "NO", "US", "NA", "DE", "UK", "CA"]
rows = [{"id": str(i + 1), "name": f"Customer {i + 1}", "country": c} for i, c in enumerate(countries)]
table = CanonicalTable(
    tenant_id="t", source_id="s", table_name="customers",
    columns=[ColumnSchema(name=n, type=ColumnType.STRING) for n in ("id", "name", "country")],
    rows=rows,
)
result = SemanticReasoner(GroqClient(), sample_size=len(rows)).run(table)
flagged = {f.row_index: f.reason for f in result.flags}
print("flagged:", flagged)
for i, c in enumerate(countries):
    if c in ("NA", "IN", "NO") and i in flagged:
        print(f"FALSE FLAG on row {i} ({c}): {flagged[i]}")
print("Atlantis flagged:", countries.index("Atlantis") in flagged)
