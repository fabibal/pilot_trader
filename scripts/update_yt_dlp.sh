#!/bin/bash
# Weekly yt-dlp refresh for youtube_monitor.py's Whisper fallback. yt-dlp breaks
# often as YouTube changes its extraction; the captions path is primary, but when
# it is IP-blocked the run falls back to yt-dlp bestaudio + faster-whisper, so a
# stale yt-dlp silently degrades that path. Idempotent: a no-op when current.
# Installed in the host crontab as:
#   0 7 * * 0 /home/fbazsa/pilot_trader/scripts/update_yt_dlp.sh >> /home/fbazsa/pilot_trader/yt_dlp_update.log 2>&1
# Only pip is needed here; deno (yt-dlp's runtime n-challenge dep) is not.

set -u
PY="${HOME}/pilot_trader/.venv/bin/python"
ts() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }

echo "===== $(ts) yt-dlp update start ====="
"${PY}" -m pip install --upgrade yt-dlp || { echo "$(ts) ERROR: pip upgrade failed"; exit 1; }
echo "$(ts) SUCCESS: yt-dlp now $("${PY}" -m yt_dlp --version 2>/dev/null || echo '?')"
echo "===== $(ts) yt-dlp update done ====="
