#!/usr/bin/env python3
"""
check_model_deprecations.py - monthly check for Gemini model shutdown dates.

Fetches Google's official deprecations page (the plain-text markdown variant,
`.md.txt` -- stable, tiny, no HTML parsing needed) and checks every Gemini
model this pipeline actually calls RIGHT NOW (read live from monitor.py /
youtube_monitor.py / twitter_digest.py's own MODEL constants -- never a
separately hand-maintained list, so this can't drift out of sync with
production config). Logs a warning if any tracked model is within WARN_DAYS
of its announced shutdown date, or already past it.

  python scripts/check_model_deprecations.py            # normal run
  python scripts/check_model_deprecations.py --dry-run  # compatibility flag; all output is local

Requires no notification credentials. Output goes to stdout/stderr.
"""
import argparse
import re
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, "/home/fbazsa/pilot_trader")
import monitor
import youtube_monitor
import twitter_digest

DEPRECATIONS_URL = "https://ai.google.dev/gemini-api/docs/deprecations.md.txt"
WARN_DAYS = 60  # monthly polling must leave time to act after a new announcement
_MODEL_CELL_RE = re.compile(r"^`([^`]+)`$")


def fetch_deprecations():
    """Return {model_name: (shutdown_date|None, replacement|None)} parsed from
    Google's deprecations table. A row with no announced shutdown date (the
    common case for a healthy model) maps to (None, None) -- callers treat
    that as "nothing to warn about", matching Google's own convention."""
    req = urllib.request.Request(DEPRECATIONS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8")
    return parse_deprecations(text)


def parse_deprecations(text):
    """Reject document drift instead of reporting an unparsed table as healthy."""

    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not (line.startswith("|") and line.endswith("|")):
            continue
        cells = [c.strip() for c in line[1:-1].split("|")]
        if len(cells) != 4:
            continue
        model_m = _MODEL_CELL_RE.match(cells[0])
        if not model_m:
            continue  # header/separator/"Preview models" rows have no backtick-wrapped name
        shutdown_date = None
        if "no shutdown" not in cells[2].lower():
            try:
                shutdown_date = datetime.strptime(cells[2], "%B %d, %Y").date()
            except ValueError:
                raise ValueError(f"Unrecognized shutdown date for {model_m.group(1)}: {cells[2]}") from None
        repl_m = _MODEL_CELL_RE.match(cells[3])
        out[model_m.group(1)] = (shutdown_date, repl_m.group(1) if repl_m else None)
    if not out:
        raise ValueError("No model rows found in deprecations document")
    return out


def models_in_use():
    """Every model called by an active pipeline, read from its constants."""
    return sorted({
        monitor.MODEL, monitor.VISION_MODEL,
        youtube_monitor.MODEL,
        twitter_digest.MODEL, twitter_digest.TRIAGE_MODEL,
    })


def main():
    ap = argparse.ArgumentParser(description="Monthly Gemini model deprecation check")
    ap.add_argument("--dry-run", action="store_true",
                    help="compatibility flag; checks only print local diagnostics")
    args = ap.parse_args()

    deprecations = fetch_deprecations()
    today = datetime.now(timezone.utc).date()
    in_use = models_in_use()
    missing = set(in_use) - deprecations.keys()
    if missing:
        message = f"Model status unknown (missing from deprecations table): {', '.join(sorted(missing))}"
        print(message, file=sys.stderr)
        sys.exit(1)

    warnings = []
    for model in in_use:
        shutdown_date, replacement = deprecations.get(model, (None, None))
        if shutdown_date is None:
            continue
        days_left = (shutdown_date - today).days
        if days_left <= WARN_DAYS:
            warnings.append((model, shutdown_date, days_left, replacement))

    if not warnings:
        print(f"OK: {len(in_use)} model(s) checked ({', '.join(in_use)}), "
              f"none within {WARN_DAYS} days of a known shutdown date.")
        return

    lines = ["Gemini model deprecation warning:"]
    for model, shutdown_date, days_left, replacement in warnings:
        status = f"OVERDUE by {-days_left}d" if days_left < 0 else f"{days_left}d left"
        repl_note = f" -> migrate to {replacement}" if replacement else " (no replacement listed yet)"
        lines.append(f"- {model} shuts down {shutdown_date.isoformat()} ({status}){repl_note}")
    message = "\n".join(lines)
    print(message)

    sys.exit(1)  # scheduled checks must expose an impending shutdown to cron


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
