#!/usr/bin/env python3
"""
youtube_monitor.py - daily Benjamin Cowen YouTube analysis.

Pipeline (mirrors monitor.py's "scrape -> LLM -> json" shape, but for video):

  YouTube RSS feed (free, no API key)            -> new video IDs + titles
   -> transcript via youtube-transcript-api (free, PRIMARY)
        fallback: faster-whisper small int8 on yt-dlp audio (LOCAL, rare)
   -> Claude Haiku structured analysis (sentiment / BTC outlook / levels / themes)
   -> data/youtube_summaries.json  (one record per video, newest-first)

The summaries file is also the dedup ledger: a video_id already present is
skipped, so reruns / the daily cron are idempotent (no reprocessing, no double
LLM spend). Designed to run unattended from cron at 09:00 UTC.

  python youtube_monitor.py                 # process any new videos in the feed
  python youtube_monitor.py --limit 3       # cap new videos this run (testing)
  python youtube_monitor.py --force ID ...  # (re)process specific video IDs
  python youtube_monitor.py --dry-run       # analyze + print, do NOT write file
  python youtube_monitor.py --no-whisper    # disable the local Whisper fallback

Whisper is a fallback only: Cowen's videos almost always have YouTube captions,
so the local transcription path (faster-whisper + yt-dlp, both CPU-only here)
is exercised rarely. It is lazily imported so the common path stays light.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import anthropic

from reconcile import write_json_atomic
# Reuse monitor.py's small, already-tested helpers (env/json loaders, Telegram
# alerting) so this stays DRY and consistent with the rest of the pipeline.
from monitor import load_env, load_json, notify_telegram, TELEGRAM_ENVS

# --- config ---------------------------------------------------------------
CHANNEL_ID = "UCRvqjQPSeaWn-uEx-w0XOIg"        # @benjaminjcowen
RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={CHANNEL_ID}"
WATCH_URL = "https://www.youtube.com/watch?v={vid}"

HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")
SUMMARIES_FILE = os.path.join(DATA_DIR, "youtube_summaries.json")

# Anthropic (same cheap text model the tweet pipeline uses).
MODEL = "claude-haiku-4-5-20251001"
HAIKU_INPUT_PER_1M = 1.00            # $ / 1M input tokens
HAIKU_OUTPUT_PER_1M = 5.00           # $ / 1M output tokens
# Cap transcript length sent to Haiku. Cowen videos run ~10-30k chars; a long
# stream could be far bigger. ~60k chars ~= 15k tokens, a safe per-video bound.
MAX_TRANSCRIPT_CHARS = 60_000

# Local Whisper fallback (CPU-only box, no GPU): small int8 is the accuracy/RAM
# sweet spot. ~480MB model, ~2GB RAM, downloads to ~/.cache on first use.
WHISPER_MODEL = "small"
WHISPER_COMPUTE = "int8"
AUDIO_DOWNLOAD_TIMEOUT_S = 600       # yt-dlp audio fetch hard cap

# --- LLM analysis ---------------------------------------------------------
ANALYSIS_SYSTEM = (
    "You analyze the transcript of a YouTube video by Benjamin Cowen, a "
    "quantitative cryptocurrency analyst focused on Bitcoin, Ethereum, BTC "
    "dominance, market cycles (bull/bear), risk, and macro. Extract a concise, "
    "structured read of THIS video.\n"
    "The transcript is auto-captioned or machine-transcribed, so expect errors: "
    "'bare market' means 'bear market', spoken numbers ('ninety thousand') mean "
    "'$90,000', and tickers may be mis-heard. Interpret intelligently; never "
    "invent levels or claims that are not actually discussed.\n"
    "Fields:\n"
    "- overall_sentiment: his NET directional stance on crypto/BTC in this video "
    "-- 'bullish', 'bearish', or 'neutral' (use neutral for mixed/cautious/"
    "range-bound).\n"
    "- btc_outlook: 1-2 sentences on his Bitcoin view / prediction in this video.\n"
    "- key_price_levels: specific price levels he actually mentions, each a short "
    "string WITH context, e.g. 'BTC support ~$90k', '$110k resistance', "
    "'ETH/BTC 0.05'. Empty list if none are given.\n"
    "- top_themes: 3-5 short topic phrases capturing what the video is about.\n"
    "- summary: a 3-4 sentence plain-English summary of his overall message.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_sentiment": {"type": "string",
                              "enum": ["bullish", "bearish", "neutral"]},
        "btc_outlook": {"type": "string"},
        "key_price_levels": {"type": "array", "items": {"type": "string"}},
        "top_themes": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["overall_sentiment", "btc_outlook", "key_price_levels",
                 "top_themes", "summary"],
    "additionalProperties": False,
}


# --- YouTube RSS ----------------------------------------------------------
_NS = {"a": "http://www.w3.org/2005/Atom",
       "yt": "http://www.youtube.com/xml/schemas/2015"}


def fetch_feed():
    """Return the channel's recent uploads as a list of dicts (newest first):
    {video_id, title, published, url}. RSS is free and key-less; YouTube returns
    the latest ~15 entries (uploads + shorts)."""
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        root = ET.fromstring(resp.read())
    videos = []
    for e in root.findall("a:entry", _NS):
        vid = e.findtext("yt:videoId", namespaces=_NS)
        if not vid:
            continue
        videos.append({
            "video_id": vid,
            "title": (e.findtext("a:title", namespaces=_NS) or "").strip(),
            "published": e.findtext("a:published", namespaces=_NS),
            "url": WATCH_URL.format(vid=vid),
        })
    return videos


# --- Transcript -----------------------------------------------------------
def get_transcript(video_id, allow_whisper=True):
    """Return (text, source). source is 'youtube' (captions), 'whisper' (local
    transcription), or None when both paths fail. The captions path is primary;
    Whisper is the rare fallback when a video has no usable transcript."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        fetched = YouTubeTranscriptApi().fetch(video_id, languages=["en"])
        snippets = fetched.to_raw_data()
        text = " ".join(s["text"] for s in snippets).strip()
        if text:
            return text, "youtube"
    except Exception as e:                       # noqa: BLE001 - any failure -> fallback
        print(f"  [transcript] captions unavailable ({type(e).__name__}): "
              f"{str(e)[:120]}", file=sys.stderr)
    if not allow_whisper:
        return None, None
    print("  [transcript] falling back to local Whisper...")
    text = whisper_transcribe(video_id)
    return (text, "whisper") if text else (None, None)


def whisper_transcribe(video_id):
    """Download the video's audio (yt-dlp) and transcribe locally with
    faster-whisper (small int8, CPU). Returns the text, or None on any failure.
    Heavy deps are imported lazily so the common captions path never pays for
    them."""
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("  [whisper] faster-whisper not installed; skipping fallback",
              file=sys.stderr)
        return None

    tmpdir = tempfile.mkdtemp(prefix="ytaudio-")
    try:
        url = WATCH_URL.format(vid=video_id)
        out = os.path.join(tmpdir, "%(id)s.%(ext)s")
        cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-playlist",
               "-f", "bestaudio/best", "-o", out, url]
        try:
            subprocess.run(cmd, check=True, timeout=AUDIO_DOWNLOAD_TIMEOUT_S)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
                FileNotFoundError) as e:
            print(f"  [yt-dlp] audio download failed: {e}", file=sys.stderr)
            return None
        files = [f for f in os.listdir(tmpdir)]
        if not files:
            print("  [yt-dlp] no audio file produced", file=sys.stderr)
            return None
        audio = os.path.join(tmpdir, files[0])
        try:
            model = WhisperModel(WHISPER_MODEL, device="cpu",
                                 compute_type=WHISPER_COMPUTE)
            segments, _info = model.transcribe(audio, language="en")
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return text or None
        except Exception as e:                   # noqa: BLE001 - isolate transcription
            print(f"  [whisper] transcription failed: {e}", file=sys.stderr)
            return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# --- Analysis -------------------------------------------------------------
def _parse_json(content_blocks):
    block = next((b.text for b in content_blocks if b.type == "text"), None)
    if not block:
        return None
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


def analyze(client, title, transcript):
    """Send the transcript to Haiku and return (analysis_dict, in_tok, out_tok).
    analysis_dict is None on API/parse error."""
    text = transcript
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + "\n...[transcript truncated]"
    user = f"Video title: {title}\n\nTranscript:\n{text}"
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=700,
            system=[{"type": "text", "text": ANALYSIS_SYSTEM}],
            output_config={"format": {"type": "json_schema",
                                      "schema": ANALYSIS_SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
        return None, 0, 0
    in_tok = resp.usage.input_tokens + (resp.usage.cache_read_input_tokens or 0) \
        + (resp.usage.cache_creation_input_tokens or 0)
    return _parse_json(resp.content), in_tok, resp.usage.output_tokens


# --- main -----------------------------------------------------------------
def process(videos, client, allow_whisper=True):
    """Transcribe + analyze each video dict; return (records, in_tok, out_tok)."""
    records, total_in, total_out = [], 0, 0
    for v in videos:
        print(f"- {v['video_id']}  {v['title'][:70]}")
        text, source = get_transcript(v["video_id"], allow_whisper=allow_whisper)
        if not text:
            print("    no transcript (captions + Whisper both failed); skipping",
                  file=sys.stderr)
            continue
        analysis, in_tok, out_tok = analyze(client, v["title"], text)
        total_in += in_tok
        total_out += out_tok
        if not analysis:
            print("    analysis failed; skipping", file=sys.stderr)
            continue
        records.append({
            "video_id": v["video_id"],
            "title": v["title"],
            "published": v["published"],
            "url": v["url"],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "transcript_source": source,
            "transcript_chars": len(text),
            **analysis,
        })
        print(f"    {source} transcript, {len(text)} chars -> "
              f"{analysis['overall_sentiment']}")
    return records, total_in, total_out


def main():
    ap = argparse.ArgumentParser(description="Benjamin Cowen YouTube analysis")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many NEW videos to process this run")
    ap.add_argument("--force", nargs="+", metavar="VIDEO_ID",
                    help="(re)process these specific video IDs, ignoring dedup")
    ap.add_argument("--dry-run", action="store_true",
                    help="analyze and print, but do not write the summaries file")
    ap.add_argument("--no-whisper", action="store_true",
                    help="disable the local Whisper fallback")
    args = ap.parse_args()

    for p in TELEGRAM_ENVS:                       # loads ANTHROPIC_API_KEY + alerts
        load_env(p)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Add it to ~/pilot_trader/.env "
              "and re-run.", file=sys.stderr)
        sys.exit(1)

    summaries = load_json(SUMMARIES_FILE, [])
    if not isinstance(summaries, list):
        summaries = []
    seen = {r.get("video_id") for r in summaries}

    feed = fetch_feed()
    print(f"Feed: {len(feed)} videos in channel RSS")

    if args.force:
        wanted = set(args.force)
        by_id = {v["video_id"]: v for v in feed}
        todo = []
        for vid in args.force:
            todo.append(by_id.get(vid, {
                "video_id": vid, "title": vid, "published": None,
                "url": WATCH_URL.format(vid=vid)}))
    else:
        todo = [v for v in feed if v["video_id"] not in seen]
        if args.limit is not None:
            todo = todo[:args.limit]

    if not todo:
        print("No new videos to process.")
        return

    print(f"Processing {len(todo)} video(s)"
          + (" [FORCE]" if args.force else "") + ":")
    client = anthropic.Anthropic()
    records, in_tok, out_tok = process(todo, client,
                                       allow_whisper=not args.no_whisper)

    cost = in_tok / 1_000_000 * HAIKU_INPUT_PER_1M \
        + out_tok / 1_000_000 * HAIKU_OUTPUT_PER_1M
    print(f"\nAnalyzed {len(records)} video(s); "
          f"Haiku tokens in={in_tok} out={out_tok} (${cost:.4f})")

    if not records:
        return
    if args.dry_run:
        print("\n[dry-run] not writing; analysis below:")
        print(json.dumps(records, indent=2))
        return

    # Merge: replace any re-forced ids, then keep newest-first by published.
    by_id = {r["video_id"]: r for r in summaries}
    for r in records:
        by_id[r["video_id"]] = r
    merged = sorted(by_id.values(),
                    key=lambda r: r.get("published") or "", reverse=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(SUMMARIES_FILE, merged)
    print(f"Wrote {len(merged)} summaries -> {SUMMARIES_FILE}")


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:                  # log + page, then non-zero exit
        try:
            for p in TELEGRAM_ENVS:
                load_env(p)
            notify_telegram(f"youtube_monitor.py FAILED: {exc!r}")
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
