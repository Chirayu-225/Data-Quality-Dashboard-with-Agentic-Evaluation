"""
Live determinism and stability check (real API calls, uses your free-tier quota).

Sends each of several realistic prompts N times to Groq and Gemini
(temperature=0, seed pinned, exactly what the app sends) and reports,
per provider and prompt:
  - text identical rate      (byte-identical replies)
  - decision identical rate  (same rows / values / labels flagged)
  - max confidence spread    (same item, different confidence)
then one stability label per provider.

The label describes these prompts on your account right now. It is not a
property of the model and says nothing about correctness. Fewer than 5
successful runs never gets a label.

Run from the service folder:
    python scripts\\check_live_determinism.py --runs 10

Options:
    --runs N        calls per prompt per provider (default 3; use 10 for a label)
    --structured    also send the strict JSON schema (sets LLM_STRUCTURED_OUTPUT=1)
    --out FILE      write the full results as JSON (counts and labels, plus replies);
                    saved after every prompt, so an interrupted run still keeps its data
    --only NAME     groq or gemini: check one provider
    --timeout SEC   give up on a single call after SEC seconds (default 60)

Keep LLM_REPLAY_CACHE_DIR UNSET for this script. The script clears it itself,
but a cache would replay one answer and hide all variance.
"""
import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # no PYTHONPATH needed

os.environ.pop("LLM_REPLAY_CACHE_DIR", None)

ap = argparse.ArgumentParser()
ap.add_argument("--runs", type=int, default=int(os.environ.get("DETERMINISM_CHECK_RUNS", "3")))
ap.add_argument("--structured", action="store_true")
ap.add_argument("--out")
ap.add_argument("--only", choices=["groq", "gemini"], help="check just one provider")
ap.add_argument("--prompts", help="comma list of: rows_with_problems,rows_all_clean,distinct_values (default: all)")
ap.add_argument("--gap", type=float, default=None, help="seconds to wait between calls to one provider (default: 13 for gemini, 0 for groq)")
ap.add_argument("--gemini-model", default=None, help="try a different Gemini model id instead of the app default")
ap.add_argument("--timeout", type=float, default=60.0, help="seconds to wait for one call before giving up on it")
args = ap.parse_args()
if args.structured:
    os.environ["LLM_STRUCTURED_OUTPUT"] = "1"

from app.agent import output_schemas  # noqa: E402
from app.agent import value_ai_review  # noqa: E402
from app.agent.llm_clients import GeminiClient, GroqClient  # noqa: E402
from app.agent.stability import assess  # noqa: E402

_ROW_SYSTEM = (
    "You are a data quality analyst. Respond with ONLY valid JSON, nothing else: "
    '{"flags": [{"row_index": <int>, "reason": "<short reason>", "confidence": <float 0-1>}]}'
)
PROMPTS = {
    "rows_with_problems": (
        _ROW_SYSTEM,
        "Table: leads\nColumns: email (string), amount (float)\n\n"
        '[UNTRUSTED DATA] row 0: {"email": "a@example.com", "amount": "250000"}\n'
        '[UNTRUSTED DATA] row 1: {"email": "not-an-email", "amount": "40"}\n'
        '[UNTRUSTED DATA] row 2: {"email": "b@example.com", "amount": "-15"}',
    ),
    "rows_all_clean": (
        _ROW_SYSTEM,
        "Table: leads\nColumns: email (string), amount (float)\n\n"
        '[UNTRUSTED DATA] row 0: {"email": "a@example.com", "amount": "250"}\n'
        '[UNTRUSTED DATA] row 1: {"email": "c@example.org", "amount": "40"}\n'
        '[UNTRUSTED DATA] row 2: {"email": "b@example.com", "amount": "15"}',
    ),
    "distinct_values": (
        value_ai_review._SYSTEM_PROMPT,
        json.dumps(
            {
                "column": "company",
                "values": ["Acme Corp", "Bletchley Systems", "Globex", "asdfghjkl", "N/A test", "Initech", "Zzxqv"],
            }
        ),
    ),
}

results: dict = {}
exit_code = 0


def _save():
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)


def _timed_call(client, system, user):
    """One call with a hard wait limit. A call that hangs is abandoned (its
    daemon thread is left behind) so the script keeps moving and reports it."""
    box = {}

    def target():
        try:
            box["reply"] = client.complete(system, user)
        except Exception as e:  # noqa: BLE001
            box["error"] = f"{type(e).__name__}: {e}"

    t = threading.Thread(target=target, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(args.timeout)
    took = time.monotonic() - t0
    if t.is_alive():
        return None, f"timed out after {args.timeout:.0f}s", took
    return box.get("reply"), box.get("error"), took


def run(label, make_client):
    global exit_code
    print(f"\n=== {label} ===")
    try:
        client = make_client()
    except Exception as e:  # noqa: BLE001
        print(f"SKIPPED - could not construct client: {type(e).__name__} - {e}")
        return
    per_prompt = {}
    all_ok = True
    quota_dead = False
    gap = args.gap if args.gap is not None else (13.0 if "Gemini" in label else 0.0)
    last_call = [0.0]
    for name, (system, user) in PROMPTS.items():
        if quota_dead:
            print(f"  {name:<20} skipped (daily quota exhausted earlier in this run)")
            continue
        replies, errors = [], []
        print(f"  {name:<20} ", end="", flush=True)
        for i in range(args.runs):
            wait = gap - (time.monotonic() - last_call[0])
            if wait > 0:
                time.sleep(wait)  # stay under the provider's requests-per-minute limit
            reply, err, took = _timed_call(client, system, user)
            last_call[0] = time.monotonic()
            if err or reply is None:
                errors.append(err or "no reply")
                print(f"x({took:.0f}s) ", end="", flush=True)
                if err and ("RESOURCE_EXHAUSTED" in err or "429" in err) and ("PerDay" in err or "daily" in err.lower()):
                    print("\n      DAILY QUOTA EXHAUSTED - stopping this provider. Retry after the reset.")
                    quota_dead = True
                    break
            else:
                replies.append(reply)
                print(f".({took:.0f}s) ", end="", flush=True)
        print()
        st = assess(replies, attempted=args.runs)
        per_prompt[name] = {"stability": st.__dict__, "errors": errors[:3], "replies": replies}
        spread = "n/a" if st.max_confidence_spread is None else f"{st.max_confidence_spread:.2f}"
        print(
            f"  {name:<20} runs={len(replies)}/{args.runs}  text-identical={st.text_identical_rate:.0%}  "
            f"decisions-identical={st.decision_identical_rate:.0%}  conf-spread={spread}  -> {st.label}"
        )
        for r in st.reasons:
            print(f"      note: {r}")
        results[label] = {"prompts": per_prompt}
        _save()
        for e in errors[:1]:
            print(f"      first error: {e}")
        all_ok &= bool(replies)
    # overall label: the weakest prompt decides
    order = ["no data", "unparseable", "insufficient samples", "unstable decisions",
             "decisions stable, wording varies", "identical"]
    labels = [p["stability"]["label"] for p in per_prompt.values()]
    overall = min(labels, key=order.index)
    print(f"  OVERALL ({label}): {overall}")
    results[label] = {"overall": overall, "prompts": per_prompt, "structured_rejected": getattr(client, "_structured_rejected", False)}
    if getattr(client, "_structured_rejected", False):
        print("  note: the provider REJECTED the strict schema; replies above are from the plain fallback.")


if args.prompts:
    wanted = [x.strip() for x in args.prompts.split(",")]
    unknown = [x for x in wanted if x not in PROMPTS]
    if unknown:
        sys.exit(f"unknown prompt(s): {unknown}; choose from {list(PROMPTS)}")
    PROMPTS = {k: v for k, v in PROMPTS.items() if k in wanted}
_providers = 2 if not args.only else 1
print(
    f"This will make up to {args.runs * len(PROMPTS)} calls PER provider "
    f"({args.runs * len(PROMPTS) * _providers} total). Gemini's free tier allowed 5 requests/minute and "
    f"20/day for gemini-3.6-flash when last checked, so Gemini calls are spaced 13s apart. "
    f"Use --prompts and --runs to stay under the daily limit."
)
print(f"runs per prompt: {args.runs}   strict JSON schema: {'ON' if output_schemas.enabled() else 'off'}")
if args.only != "gemini":
    run("Groq (generator)", GroqClient)
if args.only != "groq":
    run(
        "Gemini (verifier)",
        (lambda: GeminiClient(model=args.gemini_model)) if args.gemini_model else GeminiClient,
    )

_save()
if args.out:
    print(f"\nfull results written to {args.out}")

print(
    "\nReminder: neither provider promises determinism even with a fixed seed and temperature=0 "
    "(see docs/SCORING.md, 'Live determinism'). This measures your account and these models right now."
)
sys.exit(exit_code)
