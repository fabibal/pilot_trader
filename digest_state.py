"""Recover rolling-view synthesis independently of new-post ingestion."""

import hashlib
import json
import os

from reconcile import write_json_atomic
from storage import load_ledger
import sentiment_history

# Folded into every view's input fingerprint: bump it when the CURRENT VIEW
# schema or prompt changes, and each feed's view is re-synthesized on its next
# run even though its posts did not change. 2 = structured btc_levels.
CURRENT_VIEW_VERSION = 2

# The CURRENT VIEW's structured BTC levels, shared by both digests: the
# dashboard's BTC LEVELS map draws exactly these. Prose parsing could not tell
# a support from a price print ("$87,000 Bitcoin!") or an abandoned level.
BTC_LEVELS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "low": {"type": "number"},
            "high": {"type": "number"},
            "role": {"type": "string",
                     "enum": ["support", "resistance", "target", "invalidation"]},
            "note": {"type": "string"},
            "date": {"type": "string"},
        },
        "required": ["low", "role", "note", "date"],
        "additionalProperties": False,
    },
}


def btc_levels_prompt(unit):
    """The btc_levels field instructions; `unit` is 'posts' or 'videos'."""
    return (
        "- btc_levels: the Bitcoin (BTC/USD) price levels he is CURRENTLY "
        f"watching, taken ONLY from numbers the {unit} actually state (the text, "
        "its key levels or its chart reading) -- never invent, estimate or round "
        "a level he did not give. One object per level: low = the level in US "
        "dollars as a plain number (83000, not '83k'); high = the upper bound "
        "ONLY when he names a zone or range (omit it for a single level); role = "
        "'support', 'resistance', 'target' (a price he expects BTC to reach) or "
        "'invalidation' (a level whose break would negate his thesis); note = at "
        "most 8 words IN HUNGARIAN saying what the level is; date = YYYY-MM-DD of "
        f"the newest of the {unit} that states it. EXCLUDE prices that only "
        "report where BTC is or went ('$87,000 Bitcoin!', 'hanging around $76K', "
        "'touched $86k'), levels he has abandoned or that later "
        f"{unit} superseded, historical references, and levels of any other "
        "asset. At most 8, most important first; an empty list if he names "
        "none.\n")


def refresh_current_view(client, source, summaries, select_window, generate,
                         input_rate, output_rate, run_cost=None):
    """Regenerate source's CURRENT VIEW when its input window changed. The
    synthesis spend is added to `run_cost` (a cost_log.RunCost), failed
    attempts included."""
    if not source.current_view_file or not summaries:
        return
    window = select_window(summaries)
    if not window:
        return
    fingerprint = hashlib.sha256(json.dumps(
        [CURRENT_VIEW_VERSION, window], sort_keys=True,
        ensure_ascii=False).encode("utf-8")).hexdigest()
    # A view is derived data and may be regenerated if corrupt. Source ledgers
    # must instead fail closed (load_ledger at the pipeline entry point).
    try:
        previous = load_ledger(source.current_view_file, {})
    except (ValueError, OSError):
        previous = {}
    usable = (previous.get("overall_sentiment") in ("bullish", "bearish", "neutral", "mixed")
              and isinstance(previous.get("stance_summary"), str)
              and bool(previous["stance_summary"].strip()))
    if usable and previous.get("input_fingerprint") == fingerprint:
        return
    # Preserve valid legacy views until their input actually changes.
    if usable and not previous.get("input_fingerprint"):
        newest_analysis = max((r.get("analyzed_at") or "") for r in window)
        dates = sorted((r.get("published") or r.get("created_at") or "")[:10]
                       for r in window)
        expected = {"count": len(window), "from_date": dates[0], "to_date": dates[-1]}
        if (newest_analysis and (previous.get("generated_at") or "") >= newest_analysis
                and previous.get("based_on") == expected):
            return
    view, in_tok, out_tok = generate(client, source, summaries)
    cost = (in_tok * input_rate + out_tok * output_rate) / 1_000_000
    print(f"Current-view tokens in={in_tok} out={out_tok} (${cost:.4f})")
    if run_cost is not None:
        run_cost.llm_usd += cost
    if not view:
        raise RuntimeError(f"{source.key}: current view failed; saved summaries will be retried next run")
    view["input_fingerprint"] = fingerprint
    os.makedirs(os.path.dirname(source.current_view_file) or ".", exist_ok=True)
    write_json_atomic(source.current_view_file, view)
    sentiment_history.append_view(source.key, view)
    print(f"Current view: {view['overall_sentiment']} -> {source.current_view_file}")
