#!/usr/bin/env python3
"""
check_model_deprecations.py - monthly check for Gemini model shutdown dates.

Fetches Google's official deprecations page (the plain-text markdown variant,
`.md.txt` -- stable, tiny, no HTML parsing needed) and checks every Gemini
model this pipeline actually calls RIGHT NOW (read live from monitor.py /
youtube_monitor.py / twitter_digest.py's own MODEL constants -- never a
separately hand-maintained list, so this can't drift out of sync with
production config). Pages Telegram if any tracked model is within WARN_DAYS
of its announced shutdown date, or already past it.

  python scripts/check_model_deprecations.py            # normal run
  python scripts/check_model_deprecations.py --dry-run  # print, don't alert

Requires TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID (loaded via monitor.TELEGRAM_ENVS,
same as every other pipeline component). Run with the project venv:
/home/fbazsa/pilot_trader/.venv/bin/python scripts/check_model_deprecations.py
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
from monitor import load_env, notify_telegram, TELEGRAM_ENVS

DEPRECATIONS_URL = "https://ai.google.dev/gemini-api/docs/deprecations.md.txt"
WARN_DAYS = 30
_MODEL_CELL_RE = re.compile(r"^`([^`]+)`$")


def fetch_deprecations():
    """Return {model_name: (shutdown_date|None, replacement|None)} parsed from
    Google's deprecations table. A row with no announced shutdown date (the
    common case for a healthy model) maps to (None, None) -- callers treat
    that as "nothing to warn about", matching Google's own convention."""
    req = urllib.request.Request(DEPRECATIONS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        text = resp.read().decode("utf-8")

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
                pass  # unrecognized date format -- skip rather than guess
        repl_m = _MODEL_CELL_RE.match(cells[3])
        out[model_m.group(1)] = (shutdown_date, repl_m.group(1) if repl_m else None)
    return out


def models_in_use():
    """Every Gemini model string this pipeline actually calls, read live so
    this list can never go stale. scripts/reddit_miner.py's MODEL is always
    monitor.MODEL by construction (`MODEL = EXTRACT_MODEL`), so it needs no
    separate entry here."""
    return sorted({
        monitor.MODEL, monitor.VISION_MODEL,
        youtube_monitor.MODEL,
        twitter_digest.MODEL, twitter_digest.TRIAGE_MODEL,
    })


def main():
    ap = argparse.ArgumentParser(description="Monthly Gemini model deprecation check")
    ap.add_argument("--dry-run", action="store_true",
                    help="print findings, do not send a Telegram alert")
    args = ap.parse_args()

    for p in TELEGRAM_ENVS:
        load_env(p)

    deprecations = fetch_deprecations()
    today = datetime.now(timezone.utc).date()
    in_use = models_in_use()

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

    if not args.dry_run:
        notify_telegram(message)


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:                  # log + page, then non-zero exit
        try:
            for p in TELEGRAM_ENVS:
                load_env(p)
            notify_telegram(f"check_model_deprecations.py FAILED: {exc!r}")
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
