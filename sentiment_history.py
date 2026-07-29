#!/usr/bin/env python3
"""Append-only history of the digest feeds' rolling CURRENT VIEW syntheses.

twitter_digest.py / youtube_monitor.py OVERWRITE `data/<feed>_current_view.json`
on every regeneration, so the dashboard banner always shows the latest stance but
the previous one is lost -- there is no way to see that a feed went bearish ->
neutral last week. This module keeps a compact record of every distinct view
alongside those files. It does not change them: the banner and the Consensus
panel still read the per-feed current_view files, this is purely an additional
log (the substrate for a future sentiment timeline).

ONE shared file rather than one per feed: every consumer wants all sources
together, and the two writers never run concurrently (cron: youtube_monitor
09:00 UTC, twitter_digest 09:30 UTC). A read-modify-write race is therefore only
possible when a manual run overlaps a cron run, and it costs one lost record,
not a corrupt file (writes go through write_json_atomic) -- not worth a lock.

Records are keyed by source + the window they summarize. Re-running a feed with
--force regenerates a view over the IDENTICAL window (same `based_on`), which is
a re-synthesis rather than a new data point: it REPLACES that source's last
record instead of appending, so the timeline stays one row per distinct window
and reruns are idempotent.

Size: ~5 regenerations/day across all 7 feeds in steady state at ~450 bytes a
record (measured; the Hungarian shift_note dominates) -> ~0.8 MB/year, and
MAX_PER_SOURCE caps the file at ~1.6 MB no matter how long it runs. Nothing
reads it on the dashboard request path.
"""

import json
import os

from reconcile import write_json_atomic

HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")
HISTORY_FILE = os.path.join(DATA_DIR, "sentiment_history.json")

# Records kept per source, oldest dropped first. 500 is >1 year for the feeds
# that regenerate daily (joao_wedson, daancrypto, cowen) and many years for the
# slow ones (donalt, ki_young_ju, jesse_olson).
MAX_PER_SOURCE = 500


def _record(source, view):
    """The compact subset of a view we keep: enough to plot a sentiment timeline
    and explain each point, without the full stance_summary prose."""
    based_on = view.get("based_on") or {}
    return {
        "source": source,
        "sentiment": (view.get("overall_sentiment") or "neutral").lower(),
        "generated_at": view.get("generated_at"),
        "based_on": {
            "count": based_on.get("count"),
            "from_date": based_on.get("from_date"),
            "to_date": based_on.get("to_date"),
        },
        "shift_note": view.get("shift_note") or "",
    }


def load_history():
    """Every recorded view in write order (oldest first). Missing or corrupt
    file -> [] (the log just starts over; it is never on a critical path)."""
    try:
        with open(HISTORY_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _trim(history):
    """Keep at most MAX_PER_SOURCE records per source, dropping each source's
    oldest first and preserving overall write order."""
    keep, counts = [], {}
    for r in reversed(history):
        src = r.get("source")
        counts[src] = counts.get(src, 0) + 1
        if counts[src] <= MAX_PER_SOURCE:
            keep.append(r)
    keep.reverse()
    return keep


def append_view(source, view):
    """Record `source`'s freshly generated `view`; returns True if written.

    Never raises: the digest run that produced the view has already written the
    summaries and current_view files by the time this is called, so a failure
    here must not fail the run."""
    try:
        history = load_history()
        rec = _record(source, view)
        prior = [i for i, r in enumerate(history) if r.get("source") == source]
        if prior and history[prior[-1]].get("based_on") == rec["based_on"]:
            history[prior[-1]] = rec        # same window -> re-synthesis
        else:
            history.append(rec)
            history = _trim(history)
        os.makedirs(DATA_DIR, exist_ok=True)
        write_json_atomic(HISTORY_FILE, history)
        return True
    except Exception as e:                  # noqa: BLE001 - log-only, see above
        print(f"  [sentiment-history] not recorded: {type(e).__name__}: {e}")
        return False
