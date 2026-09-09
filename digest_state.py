"""Recover rolling-view synthesis independently of new-post ingestion."""

import hashlib
import json
import os

from reconcile import write_json_atomic
from storage import load_ledger
import sentiment_history


def refresh_current_view(client, source, summaries, select_window, generate,
                         input_rate, output_rate):
    if not source.current_view_file or not summaries:
        return
    window = select_window(summaries)
    if not window:
        return
    fingerprint = hashlib.sha256(json.dumps(
        window, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
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
    if not view:
        raise RuntimeError(f"{source.key}: current view failed; saved summaries will be retried next run")
    view["input_fingerprint"] = fingerprint
    os.makedirs(os.path.dirname(source.current_view_file) or ".", exist_ok=True)
    write_json_atomic(source.current_view_file, view)
    sentiment_history.append_view(source.key, view)
    print(f"Current view: {view['overall_sentiment']} -> {source.current_view_file}")
