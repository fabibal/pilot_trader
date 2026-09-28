"""Per-run spend telemetry shared by monitor.py and both digests.

Every pipeline run appends ONE row to data/cost_log.json; the dashboard's status
bar sums `total_usd` (LLM spend) per day and month and breaks it down by
`source`. GetXAPI spend rides along as `getxapi_usd` for reference. Rows written
before 2026-09-27 have no `source` (they are all monitor.py's).

Telemetry only: a failed write warns on stderr and never fails the run.
"""

import os
import sys
from datetime import datetime, timezone

from reconcile import write_json_atomic
from storage import ledger_lock, load_ledger

COST_LOG_FILE = "/home/fbazsa/pilot_trader/data/cost_log.json"


class RunCost:
    """Run-wide spend accumulator: every priced call site adds to it, and the
    run logs the total once at the end (see append_run)."""

    def __init__(self):
        self.llm_usd = 0.0
        self.getxapi_usd = 0.0


def append_run(source, llm_usd, getxapi_usd=0.0, path=COST_LOG_FILE, **extra):
    """Append one run's spend. `extra` carries source-specific fields (token
    counts). Returns True if written."""
    rec = {"timestamp": datetime.now(timezone.utc).isoformat(),
           "source": source, **extra,
           "total_usd": round(llm_usd, 6),
           "getxapi_usd": round(getxapi_usd, 6)}
    try:
        # Runs of different pipelines can overlap (manual reruns), so the
        # read/append/write is serialized like every other shared ledger.
        with ledger_lock(path, blocking=True):
            log = load_ledger(path, [])
            log.append(rec)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_json_atomic(path, log)
        print(f"Cost logged -> {path} ({len(log)} runs)")
        return True
    except (OSError, ValueError) as e:
        print(f"[cost-log] could not write {path}: {e}", file=sys.stderr)
        return False
