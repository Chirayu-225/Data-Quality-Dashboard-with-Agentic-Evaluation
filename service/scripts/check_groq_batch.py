"""One-call diagnostic for the "empty reply" problem. Sends ONE real
semantic-review batch (the production system prompt, PII-masked rows) to
Groq and prints why the reply looks the way it does. Costs a few thousand
tokens, not a full scan.

Run from the service folder (GROQ_API_KEY must be set, as for the app):
    python scripts/check_groq_batch.py ../testdata/leads_300.csv
    python scripts/check_groq_batch.py ../testdata/leads_300.csv --effort low
    python scripts/check_groq_batch.py ../testdata/leads_300.csv --rows 15

Prints: finish_reason, token usage, reply length, reasoning length. Row
content is never printed.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.llm_clients import _deterministic_seed  # noqa: E402
from app.agent.pii_masking import mask_table_for_llm  # noqa: E402
from app.agent.semantic_reasoning import _SYSTEM_PROMPT, _build_user_prompt  # noqa: E402
from app.config import groq_api_key  # noqa: E402
from app.connectors.base import ConnectorConfig  # noqa: E402
from app.connectors.csv_connector import CSVConnector  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("csv")
ap.add_argument("--rows", type=int, default=45, help="batch size (production default 45)")
ap.add_argument("--effort", choices=["low", "medium", "high"], default=None)
ap.add_argument("--model", default="openai/gpt-oss-20b")
a = ap.parse_args()

table = CSVConnector(ConnectorConfig(tenant_id="t", source_id="s", settings={"file_path": a.csv})).extract()[0]
masked = mask_table_for_llm(table, set(), salt="diag")
sample = [(i, masked.rows[i]) for i in range(min(a.rows, len(masked.rows)))]
user_prompt = _build_user_prompt(masked, sample, None)

from groq import Groq  # noqa: E402

kwargs = dict(
    model=a.model,
    messages=[{"role": "system", "content": _SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
    temperature=0,
    seed=_deterministic_seed(_SYSTEM_PROMPT, user_prompt),
)
if a.effort:
    kwargs["reasoning_effort"] = a.effort

print(f"model={a.model} rows={len(sample)} effort={a.effort or 'default'} prompt_chars={len(user_prompt)}")
try:
    resp = Groq(api_key=groq_api_key()).chat.completions.create(**kwargs)
except Exception as e:  # noqa: BLE001
    print("CALL FAILED:", type(e).__name__, str(e)[:400])
    sys.exit(1)

ch = resp.choices[0]
u = resp.usage
content = ch.message.content or ""
reasoning = getattr(ch.message, "reasoning", None) or ""
print(f"finish_reason={ch.finish_reason}")
print(f"prompt_tokens={getattr(u, 'prompt_tokens', None)} completion_tokens={getattr(u, 'completion_tokens', None)}")
print(f"reply_chars={len(content)} reasoning_chars={len(reasoning)}")
print("reply starts:", repr(content[:120]))
