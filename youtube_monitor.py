#!/usr/bin/env python3
"""
youtube_monitor.py - recurring YouTube channel analysis digest (ANALYSIS ONLY).

Runs one or more channels. Each channel mirrors twitter_digest.py's
"scrape -> LLM -> json" shape, but for video instead of tweets:

  YouTube RSS feed (free, no API key)            -> new video IDs + titles
   -> transcript via youtube-transcript-api (free, PRIMARY)
        fallback: faster-whisper small int8 on yt-dlp audio (LOCAL, rare-ish)
   -> Gemini structured analysis (sentiment / BTC outlook / levels / themes)
   -> the channel's summaries json  (one record per video, newest-first)

The summaries file is also the dedup ledger: a video_id already present is
skipped, so reruns / cron are idempotent (no reprocessing, no double LLM
spend).

Channels (see CHANNELS):
  - cowen (Benjamin Cowen; quantitative BTC/ETH/macro analyst) ->
      data/youtube_summaries.json. Scripted, dense single-narrator analysis.
      Checked daily; whisper fallback is rare (captions are almost always
      present).
  - jesse_olson (Jesse Olson, "The Market Sniper"; swing trader, live-streamed
      chart analysis) -> data/jesse_olson_summaries.json. Informal live
      trading-stream format (rambling, chart navigation, Discord/course
      plugs) -- the persona prompt tells Gemini to filter that noise and to
      NOT invent price levels he only alludes to as being posted in his paid
      Discord. Each upload session posts twice (the main video + a duplicate
      suffixed with a mobile/shorts marker in the title); drop_shorts_dupes
      filters the duplicate out before it ever reaches transcript fetch, so
      it isn't double-processed or double-billed. Checked daily (same cron
      run as Cowen, see CLAUDE.md's cron table) even though he only posts
      ~weekly -- the dedup ledger means an extra daily check with nothing new
      costs one free RSS fetch, not an LLM call, so there's no reason to
      special-case his schedule. His sessions run long (~45-75+ min) and his
      freshest upload sometimes has captions not yet ready, so whisper
      fallback -- while still the exception, not the rule -- fires more
      often here than for Cowen; the shared 09:00 UTC slot is well before
      his ~15:00+ UTC livestream, so a checked video is always from a prior
      day, past YouTube's post-live processing window (see CLAUDE.md's
      Post-Live-Manifestless gotcha).

ANALYSIS ONLY: neither channel is in accounts.ACCOUNTS; neither is written to
trades.json / positions.json; neither is mirrored to IBKR.

  python youtube_monitor.py                       # process new videos, ALL channels
  python youtube_monitor.py --channel jesse_olson  # only this channel
  python youtube_monitor.py --limit 3              # cap new videos/channel (testing)
  python youtube_monitor.py --channel cowen --force ID ...  # (re)process ids
  python youtube_monitor.py --dry-run              # analyze + print, do NOT write
  python youtube_monitor.py --no-whisper           # disable the local Whisper fallback

Whisper is a fallback only: captions are the primary path for both channels,
so the local transcription path (faster-whisper + yt-dlp, both CPU-only here)
is lazily imported so the common path stays light.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors

from reconcile import write_json_atomic
# Reuse monitor.py's small, already-tested helpers (env/json loaders, Telegram
# alerting) so this stays DRY and consistent with the rest of the pipeline.
from monitor import (load_env, load_json, notify_telegram, TELEGRAM_ENVS,
                     GEMINI_THINKING, _unescape_strings, _looks_mangled)

# --- config ---------------------------------------------------------------
HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")
WATCH_URL = "https://www.youtube.com/watch?v={vid}"

# Gemini 3.5 Flash (NOT the tweet pipeline's cheap extraction-tier model):
# both channels' content needs a careful read (Cowen: dense multi-level
# technical analysis; Jesse Olson: noisy live-stream narration that needs
# real filtering), and Gemini reads it markedly better. Shared across
# channels -- same model/pricing for both, only the prompt persona differs.
# max_output_tokens stays high (was sized for thinking + JSON sharing the
# budget) -- harmless headroom now, no cost since only tokens actually
# generated are billed.
# 2026-07-13 cost review: GEMINI_DEEP_THINKING (dynamic) turned OFF here --
# live A/B on a real Cowen transcript showed it burned ~1,350 invisible
# thinking tokens (77% of output) for no measurable quality gain over
# thinking off (same sentiment/themes/price levels). Model itself (3.5 Flash)
# stays as-is, only the thinking toggle changed.
# Batch Mode (50% off) evaluated and rejected here --
# at 1-2 calls/day the absolute saving is ~$0.01-0.03/video (~$0.5-1/mo), not
# worth the async submit/poll/expiry machinery, and 2026 reports (GitHub
# googleapis/python-genai#2221/#1482) show batch jobs sometimes stuck in
# PENDING for 24-96h+ -- unacceptable for a daily-cron freshness expectation.
# Context caching also evaluated and rejected: ANALYSIS_BODY is far under
# every documented minimum (Gemini's implicit/explicit caching needs >=2048
# tokens on 2.5 Flash, >=4096 on 3.5 Flash; this prompt is ~1 order of
# magnitude smaller). Revisit only if either changes materially.
MODEL = "gemini-3.5-flash"
INPUT_PER_1M = 1.50                  # $ / 1M input tokens (gemini-3.5-flash)
OUTPUT_PER_1M = 9.00                 # $ / 1M output tokens, incl. thinking tokens
# Cap transcript length sent to the model. Cowen videos run ~10-30k chars;
# Jesse Olson's live-stream sessions run notably longer (~40-55k chars
# observed, occasionally more) since he narrates while charting live rather
# than delivering a scripted take. Both stay comfortably under this cap in
# practice, but ~60k chars ~= 15k tokens is a safe per-video bound either way.
MAX_TRANSCRIPT_CHARS = 60_000

# Local Whisper fallback (CPU-only box, no GPU): small int8 is the accuracy/RAM
# sweet spot. ~480MB model, ~2GB RAM, downloads to ~/.cache on first use.
WHISPER_MODEL = "small"
WHISPER_COMPUTE = "int8"
AUDIO_DOWNLOAD_TIMEOUT_S = 600       # yt-dlp audio fetch hard cap (download
                                     # only; transcription time is unbounded
                                     # and scales with video length -- see
                                     # jesse_olson's CHANNELS entry above for
                                     # why this fires more often for him)
# yt-dlp needs a JS runtime (deno) for YouTube extraction; without one it warns
# and some formats degrade. deno is installed user-locally here, but cron's PATH
# is minimal and won't include it — so we add this dir to the subprocess env.
DENO_BIN_DIR = os.path.expanduser("~/.deno/bin")

# --- LLM analysis -----------------------------------------------------------
# The system prompt is shared across channels EXCEPT for a persona prefix
# (who the channel owner is + what format/caveats apply). Channel.analysis_system
# prepends the per-channel persona to this shared body so every channel gets
# identical field definitions and the Hungarian-output contract. Mirrors
# twitter_digest.py's ANALYSIS_BODY / Feed.analysis_persona split.
ANALYSIS_BODY = (
    "Extract a concise, structured read of THIS video.\n"
    "The transcript is auto-captioned or machine-transcribed, so expect errors: "
    "'bare market' means 'bear market', spoken numbers ('ninety thousand') mean "
    "'$90,000', and tickers may be mis-heard. Interpret intelligently; never "
    "invent levels or claims that are not actually discussed.\n"
    "Fields:\n"
    "- overall_sentiment: his NET directional stance on crypto/BTC in this video "
    "-- the EXACT English enum value 'bullish', 'bearish', or 'neutral' (use "
    "neutral for mixed/cautious/range-bound). KEEP THIS IN ENGLISH.\n"
    "- btc_outlook: 1-2 sentences on his Bitcoin view / prediction, IN HUNGARIAN.\n"
    "- key_price_levels: specific price levels he actually mentions, each a short "
    "string WITH context, e.g. 'BTC support ~$90k', '$110k resistance', "
    "'ETH/BTC 0.05'. Empty list if none are given.\n"
    "- top_themes: 3-5 short topic phrases capturing what the video is about, IN "
    "HUNGARIAN.\n"
    "- summary: a 3-4 sentence summary of his overall message, IN HUNGARIAN.\n"
    "LANGUAGE: write btc_outlook, top_themes, and summary in fluent HUNGARIAN "
    "(magyarul). overall_sentiment MUST remain the exact English enum value "
    "(it drives dashboard logic); ticker symbols and prices stay as-is.\n"
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

# --- rolling "current view" synthesis --------------------------------------
# Once per run, IF new videos were processed, synthesize a short rolling
# stance from the channel's recent video history -- shown as a banner above
# the individual video cards on the dashboard, so "what does he think right
# now" doesn't require manually reading and merging several cards. Runs over
# the already-distilled per-video fields (btc_outlook/summary/top_themes), not
# the full transcript, so it's a cheap second-pass read, not a re-analysis.
# Mirrors twitter_digest.py's identical feature for the X analysis digests.
CURRENT_VIEW_DAYS = 14           # day-based floor
CURRENT_VIEW_MIN_POSTS = 8       # extend by count when the day window is thin
                                 # (e.g. jesse_olson's ~weekly cadence puts ~2
                                 # videos in 14 days -> falls back to "last 8",
                                 # i.e. roughly a 2-month rolling window)
CURRENT_VIEW_MAX_POSTS = 25      # hard cap so a busy stretch doesn't blow out
                                 # the prompt

CURRENT_VIEW_BODY = (
    "Below is a list of his recent analyzed videos, OLDEST FIRST, each as "
    "\"[date] sentiment | themes -- outlook\". Synthesize his CURRENT overall "
    "stance across this window -- do not just rehash the newest video.\n"
    "Fields:\n"
    "- overall_sentiment: his NET stance across THESE videos -- the EXACT "
    "English enum value 'bullish', 'bearish', 'neutral', or 'mixed'. Use "
    "'mixed' when the videos genuinely disagree or pull in different "
    "directions (not merely cautious -- that is 'neutral'). KEEP THIS IN "
    "ENGLISH.\n"
    "- stance_summary: 2-4 sentences, IN HUNGARIAN, on his current overall "
    "view/thesis across the window -- what he is focused on and where he "
    "leans. Weigh the whole window, not just the newest video.\n"
    "- shift_note: ONE sentence, IN HUNGARIAN, ONLY if his stance visibly "
    "shifted somewhere across the window (e.g. turned more cautious, flipped "
    "bullish). Empty string if there is no clear shift.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

CURRENT_VIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_sentiment": {"type": "string",
                              "enum": ["bullish", "bearish", "neutral", "mixed"]},
        "stance_summary": {"type": "string"},
        "shift_note": {"type": "string"},
    },
    "required": ["overall_sentiment", "stance_summary", "shift_note"],
    "additionalProperties": False,
}


# --- channel registry -------------------------------------------------------
@dataclass(frozen=True)
class Channel:
    """One YouTube channel. The summaries file is its dedup ledger."""
    key: str
    channel_id: str
    display_name: str
    summaries_file: str
    persona: str                    # "who + format" prefix (see ANALYSIS_BODY)
    current_view_file: str = None   # None skips the CURRENT VIEW feature
    # Some channels (observed: jesse_olson) post each upload session twice --
    # the main video, and a near-identical clip suffixed with a mobile/shorts
    # marker in the title -- within seconds/minutes of each other. Filter the
    # duplicate before it reaches transcript fetch (see _drop_shorts_duplicates).
    drop_shorts_dupes: bool = False

    @property
    def rss_url(self):
        return ("https://www.youtube.com/feeds/videos.xml"
                f"?channel_id={self.channel_id}")

    @property
    def analysis_system(self):
        return self.persona + " " + ANALYSIS_BODY

    @property
    def current_view_system(self):
        return self.persona + " " + CURRENT_VIEW_BODY


CHANNELS = {c.key: c for c in [
    Channel(
        key="cowen",
        channel_id="UCRvqjQPSeaWn-uEx-w0XOIg",           # @benjaminjcowen
        display_name="Benjamin Cowen",
        summaries_file=os.path.join(DATA_DIR, "youtube_summaries.json"),
        persona=(
            "You analyze the transcript of a YouTube video by Benjamin Cowen, a "
            "quantitative cryptocurrency analyst focused on Bitcoin, Ethereum, "
            "BTC dominance, market cycles (bull/bear), risk, and macro."),
        current_view_file=os.path.join(DATA_DIR, "youtube_current_view.json"),
    ),
    Channel(
        key="jesse_olson",
        channel_id="UCtuoqGiIHBGMRmTGeXVrf9g",            # @jesseolsoninc
        display_name="Jesse Olson",
        summaries_file=os.path.join(DATA_DIR, "jesse_olson_summaries.json"),
        persona=(
            "You analyze the transcript of a YouTube video by Jesse Olson "
            "(\"The Market Sniper\"), a swing trader who live-streams Bitcoin/"
            "crypto chart analysis (support/resistance, RSI, MACD, EMA/SMA, "
            "divergence, retest patterns, price targets).\n"
            "The video is an informal LIVE-STREAMED trading session, not a "
            "scripted analysis: expect rambling, live chart navigation, "
            "community shout-outs, and repeated plugs for his paid Discord / "
            "course / affiliate tools -- ignore all of that promotional "
            "content when extracting the analysis fields below. He frequently "
            "refers to 'exact targets' or specific levels as being posted in "
            "his Discord WITHOUT restating the number on camera -- if a level "
            "is only alluded to like that and never actually stated with a "
            "number in the transcript, do NOT invent or infer it; leave it "
            "out of key_price_levels entirely."),
        current_view_file=os.path.join(DATA_DIR, "jesse_olson_current_view.json"),
        drop_shorts_dupes=True,
    ),
]}


# --- YouTube RSS ------------------------------------------------------------
_NS = {"a": "http://www.w3.org/2005/Atom",
       "yt": "http://www.youtube.com/xml/schemas/2015"}

# Trailing mobile-phone marker YouTube shows on jesse_olson's shorts/mobile
# duplicate upload (observed: U+1F4F1, always preceded by a space). Stripping
# it normalizes "Title X 📱" back to "Title X" so it matches the main video's
# title for grouping in _drop_shorts_duplicates.
_SHORTS_SUFFIX_RE = re.compile(r"\s*\U0001F4F1\s*$")


def fetch_feed(channel):
    """Return the channel's recent uploads as a list of dicts (newest first):
    {video_id, title, published, url}. RSS is free and key-less; YouTube returns
    the latest ~15 entries (uploads + shorts)."""
    req = urllib.request.Request(channel.rss_url,
                                 headers={"User-Agent": "Mozilla/5.0"})
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
    if channel.drop_shorts_dupes:
        videos = _drop_shorts_duplicates(videos)
    return videos


def _drop_shorts_duplicates(videos):
    """Collapse a session's paired upload (main video + mobile/shorts-marker
    duplicate, see _SHORTS_SUFFIX_RE) into just the main video. Groups entries
    by title with the trailing marker stripped; keeps the non-suffixed entry
    when both are present in this batch, else keeps whichever one is (e.g. the
    main video's pair already scrolled out of the RSS window)."""
    by_key = {}
    for v in videos:
        stripped = _SHORTS_SUFFIX_RE.sub("", v["title"]).strip()
        by_key.setdefault(stripped, []).append(v)
    keep = []
    for stripped, group in by_key.items():
        main = [v for v in group if v["title"].strip() == stripped]
        keep.append(main[0] if main else group[0])
    keep.sort(key=lambda v: v["published"] or "", reverse=True)
    return keep


# --- Transcript -------------------------------------------------------------
def get_transcript(video_id, allow_whisper=True):
    """Return (text, source). source is 'youtube' (captions), 'whisper' (local
    transcription), or None when both paths fail. The captions path is primary;
    Whisper is the fallback when a video has no usable transcript yet."""
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
        # --remote-components ejs:github lets deno run yt-dlp's EJS challenge
        # solver (fetched once from the yt-dlp/ejs GitHub release, then cached),
        # which YouTube's "n challenge" now requires for full format coverage.
        cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-playlist",
               "--remote-components", "ejs:github",
               "-f", "bestaudio/best", "-o", out, url]
        # Put the deno JS runtime on PATH for yt-dlp even under cron's minimal env.
        env = os.environ.copy()
        if os.path.isdir(DENO_BIN_DIR) and DENO_BIN_DIR not in env.get("PATH", ""):
            env["PATH"] = DENO_BIN_DIR + os.pathsep + env.get("PATH", "")
        try:
            subprocess.run(cmd, check=True, timeout=AUDIO_DOWNLOAD_TIMEOUT_S,
                           env=env)
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


# --- Analysis -----------------------------------------------------------
def _parse_json(resp):
    try:
        return _unescape_strings(json.loads(resp.text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


# gemini-3.5-flash occasionally mangles accented Hungarian characters in long
# free-text fields (see monitor._looks_mangled); a retry of the identical
# call usually comes back clean.
_MANGLED_MAX_ATTEMPTS = 3


def analyze(client, channel, title, transcript):
    """Send the transcript to the model (Gemini) and return
    (analysis_dict, in_tok, out_tok). analysis_dict is None on API/parse error."""
    text = transcript
    if len(text) > MAX_TRANSCRIPT_CHARS:
        text = text[:MAX_TRANSCRIPT_CHARS] + "\n...[transcript truncated]"
    user = f"Video title: {title}\n\nTranscript:\n{text}"
    in_tok = out_tok = 0
    parsed = None
    for attempt in range(_MANGLED_MAX_ATTEMPTS):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=channel.analysis_system,
                    response_mime_type="application/json",
                    response_json_schema=ANALYSIS_SCHEMA,
                    thinking_config=GEMINI_THINKING,
                    max_output_tokens=6000,
                ),
            )
        except genai_errors.APIError as e:
            print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
            return None, in_tok, out_tok
        u = resp.usage_metadata
        in_tok += u.prompt_token_count or 0
        out_tok += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        parsed = _parse_json(resp)
        if not (parsed and _looks_mangled(parsed)
                and attempt < _MANGLED_MAX_ATTEMPTS - 1):
            break
        print("  [retry] mangled Hungarian text in analyze, retrying",
              file=sys.stderr)
    return parsed, in_tok, out_tok


def _select_current_view_window(summaries):
    """Pick the records that feed the rolling-view synthesis: everything from
    the last CURRENT_VIEW_DAYS days, extended by count if that's too thin,
    capped at CURRENT_VIEW_MAX_POSTS. `summaries` must be newest-first (as
    merged/written by run_channel()); returns a newest-first list."""
    if not summaries:
        return []
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=CURRENT_VIEW_DAYS)).strftime("%Y-%m-%d")
    window = [r for r in summaries if (r.get("published") or "")[:10] >= cutoff]
    if len(window) < CURRENT_VIEW_MIN_POSTS:
        window = summaries[:CURRENT_VIEW_MIN_POSTS]
    return window[:CURRENT_VIEW_MAX_POSTS]


def _current_view_entry_text(r):
    """One video's already-distilled fields, formatted as a single prompt line."""
    line = (f"[{(r.get('published') or '')[:10]}] "
            f"{r.get('overall_sentiment') or 'neutral'}")
    themes = r.get("top_themes") or []
    if themes:
        line += " | " + ", ".join(themes)
    line += f" -- {r.get('btc_outlook') or ''}"
    return line


def generate_current_view(client, channel, summaries):
    """Synthesize a rolling 'current stance' from the channel's recent video
    history (see _select_current_view_window) -- fed the already-distilled
    per-video fields, not the full transcript. Returns (view_dict|None, in_tok,
    out_tok); None if there is nothing to synthesize from or the call fails."""
    window = _select_current_view_window(summaries)
    if not window:
        return None, 0, 0
    entries = "\n".join(_current_view_entry_text(r) for r in reversed(window))
    user = f"Recent {channel.display_name} videos:\n{entries}"
    in_tok = out_tok = 0
    parsed = None
    for attempt in range(_MANGLED_MAX_ATTEMPTS):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=channel.current_view_system,
                    response_mime_type="application/json",
                    response_json_schema=CURRENT_VIEW_SCHEMA,
                    thinking_config=GEMINI_THINKING,
                    max_output_tokens=800,
                ),
            )
        except genai_errors.APIError as e:
            print(f"  [current-view error] {type(e).__name__}: {e}", file=sys.stderr)
            return None, in_tok, out_tok
        u = resp.usage_metadata
        in_tok += u.prompt_token_count or 0
        out_tok += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        parsed = _parse_json(resp)
        if not (parsed and _looks_mangled(parsed)
                and attempt < _MANGLED_MAX_ATTEMPTS - 1):
            break
        print("  [retry] mangled Hungarian text in generate_current_view, "
              "retrying", file=sys.stderr)
    if not parsed:
        return None, in_tok, out_tok
    dates = sorted((r.get("published") or "")[:10] for r in window
                   if r.get("published"))
    view = {
        **parsed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "based_on": {
            "count": len(window),
            "from_date": dates[0] if dates else None,
            "to_date": dates[-1] if dates else None,
        },
    }
    return view, in_tok, out_tok


# --- main -----------------------------------------------------------------
def process(videos, client, channel, allow_whisper=True):
    """Transcribe + analyze each video dict; return (records, in_tok, out_tok)."""
    records, total_in, total_out = [], 0, 0
    for v in videos:
        print(f"- {v['video_id']}  {v['title'][:70]}")
        text, source = get_transcript(v["video_id"], allow_whisper=allow_whisper)
        if not text:
            print("    no transcript (captions + Whisper both failed); skipping",
                  file=sys.stderr)
            continue
        analysis, in_tok, out_tok = analyze(client, channel, v["title"], text)
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


def run_channel(channel, client, args):
    """Process one channel end-to-end: fetch -> filter -> transcribe -> analyze
    -> write ledger -> (maybe) regenerate the rolling current view."""
    print(f"\n=== {channel.display_name} ===")
    summaries = load_json(channel.summaries_file, [])
    if not isinstance(summaries, list):
        summaries = []
    seen = {r.get("video_id") for r in summaries}

    feed = fetch_feed(channel)
    print(f"Feed: {len(feed)} videos in channel RSS")

    if args.force:
        by_id = {v["video_id"]: v for v in feed}
        todo = [by_id.get(vid, {
                    "video_id": vid, "title": vid, "published": None,
                    "url": WATCH_URL.format(vid=vid)})
                for vid in args.force]
    else:
        todo = [v for v in feed if v["video_id"] not in seen]
        if args.limit is not None:
            todo = todo[:args.limit]

    if not todo:
        print("No new videos to process.")
        return

    print(f"Processing {len(todo)} video(s)"
          + (" [FORCE]" if args.force else "") + ":")
    records, in_tok, out_tok = process(todo, client, channel,
                                       allow_whisper=not args.no_whisper)

    cost = in_tok / 1_000_000 * INPUT_PER_1M \
        + out_tok / 1_000_000 * OUTPUT_PER_1M
    print(f"\nAnalyzed {len(records)} video(s); "
          f"Gemini tokens in={in_tok} out={out_tok} (${cost:.4f})")

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
    write_json_atomic(channel.summaries_file, merged)
    print(f"Wrote {len(merged)} summaries -> {channel.summaries_file}")

    if channel.current_view_file:
        view, cv_in, cv_out = generate_current_view(client, channel, merged)
        if view:
            cv_cost = (cv_in / 1_000_000 * INPUT_PER_1M
                       + cv_out / 1_000_000 * OUTPUT_PER_1M)
            write_json_atomic(channel.current_view_file, view)
            print(f"Current view: {view['overall_sentiment']} "
                  f"(based on {view['based_on']['count']} videos); "
                  f"tokens in={cv_in} out={cv_out} (${cv_cost:.4f}) "
                  f"-> {channel.current_view_file}")


def main():
    ap = argparse.ArgumentParser(description="YouTube channel analysis digest")
    ap.add_argument("--channel", choices=sorted(CHANNELS), default=None,
                    help="process only this channel (default: all channels)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many NEW videos to process per channel this run")
    ap.add_argument("--force", nargs="+", metavar="VIDEO_ID",
                    help="(re)process these specific video IDs, ignoring dedup; "
                         "requires --channel")
    ap.add_argument("--dry-run", action="store_true",
                    help="analyze and print, but do not write the summaries file")
    ap.add_argument("--no-whisper", action="store_true",
                    help="disable the local Whisper fallback")
    args = ap.parse_args()

    if args.force and not args.channel:
        ap.error("--force requires --channel (one channel at a time)")

    for p in TELEGRAM_ENVS:                       # loads GOOGLE_API_KEY + alerts
        load_env(p)
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is not set. Add it to ~/pilot_trader/.env "
              "and re-run.", file=sys.stderr)
        sys.exit(1)

    channels = [CHANNELS[args.channel]] if args.channel else list(CHANNELS.values())
    client = genai.Client()
    failed = []
    for channel in channels:
        try:
            run_channel(channel, client, args)
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as exc:
            # Single-channel run: hard-fail (the top-level handler pages +
            # exits). All-channels run: one channel's transient outage must
            # NOT starve the other channel for the whole cron tick -- log +
            # page for this channel, keep going, then exit non-zero so the
            # failure still surfaces. Mirrors twitter_digest.py's main().
            if len(channels) == 1:
                raise
            traceback.print_exc()
            failed.append(channel.key)
            try:
                notify_telegram(f"youtube_monitor {channel.key} FAILED: {exc!r}")
            except Exception:
                pass
    if failed:
        print(f"channels failed: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


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
