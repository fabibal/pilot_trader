#!/usr/bin/env python3
"""
youtube_monitor.py - recurring YouTube channel analysis digest (ANALYSIS ONLY).

Runs one or more channels. Each channel mirrors twitter_digest.py's
"scrape -> LLM -> json" shape, but for video instead of tweets:

  YouTube RSS feed (free, no API key)            -> new video IDs + titles
   -> Gemini native video understanding, agentic processing (Gemini fetches
      the YouTube URL itself -- no local download, no transcript/captions)
   -> Gemini structured analysis (sentiment / BTC outlook / levels / themes)
   -> the channel's summaries json  (one record per video, newest-first)

2026-09-03: switched from transcript (captions + local Whisper fallback) to
native video with agentic processing (media_processing=AGENTIC), after a
live-tested comparison (memory: agentic-video-mode-2026-09) showed 91.6%
fewer tokens / 58% cheaper / 48% faster than static native video on a fixed
model, landing close to the old transcript pipeline's cost while reading
on-screen chart/ticker detail transcripts structurally cannot capture. The
transcript/Whisper failure modes (captions IpBlocked, Post-Live Manifestless)
were specific to THIS box fetching YouTube content itself (captions API /
yt-dlp audio download); native video hands the URL to Gemini, which fetches
it server-side, so that whole class of failure no longer applies. Whatever
native video CAN fail on gets the same self-heal-by-retry treatment every
other failure mode here already uses: log, don't mark the video seen, next
cron run retries. youtube-transcript-api/faster-whisper/yt-dlp stay installed
but unused (see CLAUDE.md venv note) -- same call already made for ib_insync
post-IBKR-mirror-removal.

The summaries file is also the dedup ledger: a video_id already present is
skipped, so reruns / cron are idempotent (no reprocessing, no double LLM
spend).

Channels (see CHANNELS):
  - cowen (Benjamin Cowen; quantitative BTC/ETH/macro analyst) ->
      data/youtube_summaries.json. Scripted, dense single-narrator analysis.
      Checked daily.
  - jesse_olson (Jesse Olson, "The Market Sniper"; swing trader, live-streamed
      chart analysis) -> data/jesse_olson_summaries.json. Informal live
      trading-stream format (rambling, chart navigation, Discord/course
      plugs) -- the persona prompt tells Gemini to filter that noise and to
      NOT invent price levels he only alludes to as being posted in his paid
      Discord. Each upload session posts twice (the main video + a duplicate
      suffixed with a mobile/shorts marker in the title); drop_shorts_dupes
      filters the duplicate out before it's ever processed, so it isn't
      double-analyzed or double-billed. Checked daily (same cron run as
      Cowen, see CLAUDE.md's cron table) even though he only posts ~weekly --
      the dedup ledger means an extra daily check with nothing new costs one
      free RSS fetch, not an LLM call, so there's no reason to special-case
      his schedule. His sessions run long (~45-75+ min); agentic processing
      is specifically marketed as strongest on long-form video, but this is
      the first channel here to exercise that at his length in production --
      watch early runs. The shared 09:00 UTC slot is well before his
      ~15:00+ UTC livestream, so a checked video is always from a prior day.
      Pre-2026-09-03 this mattered because YouTube sometimes hadn't finished
      post-live processing yet ("Post-Live Manifestless", CLAUDE.md); native
      video is fetched by Gemini server-side rather than via this box's own
      yt-dlp/captions calls, so that specific mechanism likely no longer
      applies, but it isn't proven over many runs yet -- any video-fetch
      error self-heals the same way (stays unseen, retried next run).

ANALYSIS ONLY: neither channel is in accounts.ACCOUNTS; neither is written to
trades.json / positions.json; neither is mirrored to IBKR.

  python youtube_monitor.py                       # process new videos, ALL channels
  python youtube_monitor.py --channel jesse_olson  # only this channel
  python youtube_monitor.py --limit 3              # cap new videos/channel (testing)
  python youtube_monitor.py --channel cowen --force ID ...  # (re)process ids
  python youtube_monitor.py --dry-run              # analyze + print, do NOT write
"""
import argparse
import json
import os
import re
import sys
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
import sentiment_history
# Reuse monitor.py's small, already-tested helpers (env/json loaders, Telegram
# alerting) so this stays DRY and consistent with the rest of the pipeline.
from monitor import (load_env, load_json, notify_telegram, TELEGRAM_ENVS,
                     GEMINI_THINKING, _unescape_strings, _looks_mangled,
                     _first_sentence, GeminiTally, alert_gemini_outage)

# --- config ---------------------------------------------------------------
HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")

# Every Gemini call this run, across all channels. analyze()/generate_current_view()
# each swallow their own APIError, so this is the only thing that can tell a
# genuinely quiet run from a blind one -- main() pages on it. See
# monitor.GeminiTally.
LLM_TALLY = GeminiTally()
WATCH_URL = "https://www.youtube.com/watch?v={vid}"

# Gemini 3.5 Flash was the model through 2026-09-03; switched to 3.7 Flash the
# same day the pipeline moved from transcript to native video, since agentic
# video processing needs it (see the module docstring and memory
# agentic-video-mode-2026-09) and it's also strictly cheaper per token. Shared
# across channels -- same model/pricing for both, only the prompt persona
# differs. Also used by generate_current_view() (text-only, unaffected by the
# video switch) so the whole file runs on one model.
# max_output_tokens stays high (was sized for thinking + JSON sharing the
# budget) -- harmless headroom now, no cost since only tokens actually
# generated are billed. Confirmed empirically fine even with agentic mode's
# 25k+ tool-navigation tokens (they don't count against this cap).
# 2026-07-13 cost review: GEMINI_DEEP_THINKING (dynamic) stays OFF -- live A/B
# on a real Cowen transcript showed it burned ~1,350 invisible thinking tokens
# (77% of output) for no measurable quality gain. thinking_budget=0 does NOT
# suppress agentic mode's tool-navigation tokens (confirmed 2026-09-03 --
# those come from the tool_call/tool_response loop agentic processing itself
# runs, not ordinary chain-of-thought), so it's not a cost lever here, but it
# doesn't hurt either and matches this project's blanket "pin thinking off"
# convention.
# Batch Mode (50% off) evaluated and rejected here --
# at 1-2 calls/day the absolute saving is ~$0.01-0.03/video (~$0.5-1/mo), not
# worth the async submit/poll/expiry machinery, and 2026 reports (GitHub
# googleapis/python-genai#2221/#1482) show batch jobs sometimes stuck in
# PENDING for 24-96h+ -- unacceptable for a daily-cron freshness expectation.
# Context caching not applicable to the video path (each call's video content
# is unique per video, nothing repeats to cache).
MODEL = "gemini-3.7-flash"
INPUT_PER_1M = 0.75                  # $ / 1M input tokens (gemini-3.7-flash,
                                     # introductory pricing through 2026-12-31)
OUTPUT_PER_1M = 3.75                 # $ / 1M output tokens, incl. thinking tokens

# --- LLM analysis -----------------------------------------------------------
# The system prompt is shared across channels EXCEPT for a persona prefix
# (who the channel owner is + what format/caveats apply). Channel.analysis_system
# prepends the per-channel persona to this shared body so every channel gets
# identical field definitions and the Hungarian-output contract. Mirrors
# twitter_digest.py's ANALYSIS_BODY / Feed.analysis_persona split.
ANALYSIS_BODY = (
    "You are given the video directly (visuals + audio), not a transcript.\n"
    "Extract a concise, structured read of THIS video. Price levels may be "
    "spoken OR shown on-screen only (e.g. drawn on a chart) -- read both. "
    "Never invent levels or claims that are not actually shown or discussed.\n"
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
# the video itself, so it's a cheap text-only second-pass read, not a
# re-analysis.
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
    "- shift_note: ALWAYS exactly ONE sentence, IN HUNGARIAN -- NEVER an empty "
    "string, and NEVER meta-commentary about whether his view moved. If his "
    "stance visibly shifted somewhere across the window, THAT shift is the "
    "sentence: name it (e.g. turned more cautious, flipped bullish). If it "
    "did not shift, write the single sharpest CONCRETE point of where he "
    "stands right now instead -- the price level, catalyst or condition he is "
    "waiting on. Do NOT write 'unchanged', 'consistent', 'no shift', 'held "
    "his view' or any equivalent, and do not merely restate stance_summary "
    "-- this sentence is read on its own, without it.\n"
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
    # duplicate before it's ever analyzed (see _drop_shorts_duplicates).
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
            "You analyze a YouTube video by Benjamin Cowen, a "
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
            "You analyze a YouTube video by Jesse Olson "
            "(\"The Market Sniper\"), a swing trader who live-streams Bitcoin/"
            "crypto chart analysis (support/resistance, RSI, MACD, EMA/SMA, "
            "divergence, retest patterns, price targets).\n"
            "The video is an informal LIVE-STREAMED trading session, not a "
            "scripted analysis: expect rambling, live chart navigation, "
            "community shout-outs, and repeated plugs for his paid Discord / "
            "course / affiliate tools -- ignore all of that promotional "
            "content when extracting the analysis fields below. He frequently "
            "refers to 'exact targets' or specific levels as being posted in "
            "his Discord WITHOUT restating or drawing the number anywhere on "
            "camera -- if a level is only alluded to like that and never "
            "actually shown or stated with a number, do NOT invent or infer "
            "it; leave it out of key_price_levels entirely."),
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


# --- Analysis -----------------------------------------------------------
def _parse_json(resp):
    try:
        return _unescape_strings(json.loads(resp.text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


# Gemini has occasionally mangled accented Hungarian characters in long
# free-text fields on past models (see monitor._looks_mangled); a retry of
# the identical call usually comes back clean. Kept as a safety net for
# gemini-3.7-flash too, though not yet specifically confirmed necessary there.
_MANGLED_MAX_ATTEMPTS = 3


def analyze(client, channel, video):
    """Send the video directly to Gemini (native video, agentic processing --
    see the module docstring) and return (analysis_dict, in_tok, out_tok).
    analysis_dict is None on API/parse error; the video stays unseen and is
    retried next run (same self-heal pattern as every other failure mode
    here)."""
    video_part = genai_types.Part(
        file_data=genai_types.FileData(file_uri=video["url"],
                                       mime_type="video/mp4"),
        media_processing=genai_types.MediaProcessing.AGENTIC,
    )
    text_part = genai_types.Part(
        text=f"Video title: {video['title']}\n\n"
             "Analyze this video per the schema.")
    contents = [video_part, text_part]
    in_tok = out_tok = 0
    parsed = None
    for attempt in range(_MANGLED_MAX_ATTEMPTS):
        try:
            resp = client.models.generate_content(
                model=MODEL,
                contents=contents,
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
            LLM_TALLY.fail()
            return None, in_tok, out_tok
        LLM_TALLY.ok()
        u = resp.usage_metadata
        in_tok += u.prompt_token_count or 0
        out_tok += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        parsed = _parse_json(resp)
        if not (parsed and _looks_mangled(parsed)):
            break
        if attempt == _MANGLED_MAX_ATTEMPTS - 1:
            print("  [give-up] analyze still mangled after "
                  f"{_MANGLED_MAX_ATTEMPTS} attempts; dropping (the video stays "
                  "unseen and is retried next run)", file=sys.stderr)
            return None, in_tok, out_tok
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
    per-video fields, not the video itself. Returns (view_dict|None, in_tok,
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
            LLM_TALLY.fail()
            return None, in_tok, out_tok
        LLM_TALLY.ok()
        u = resp.usage_metadata
        in_tok += u.prompt_token_count or 0
        out_tok += (u.candidates_token_count or 0) + (u.thoughts_token_count or 0)
        parsed = _parse_json(resp)
        if not (parsed and _looks_mangled(parsed)):
            break
        if attempt == _MANGLED_MAX_ATTEMPTS - 1:
            print("  [give-up] generate_current_view still mangled after "
                  f"{_MANGLED_MAX_ATTEMPTS} attempts; keeping the previous view",
                  file=sys.stderr)
            return None, in_tok, out_tok
        print("  [retry] mangled Hungarian text in generate_current_view, "
              "retrying", file=sys.stderr)
    if not parsed:
        return None, in_tok, out_tok
    if not (parsed.get("shift_note") or "").strip():
        parsed["shift_note"] = _first_sentence(parsed.get("stance_summary"))
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
def process(videos, client, channel):
    """Analyze each video dict directly from native video; return
    (records, in_tok, out_tok)."""
    records, total_in, total_out = [], 0, 0
    for v in videos:
        print(f"- {v['video_id']}  {v['title'][:70]}")
        analysis, in_tok, out_tok = analyze(client, channel, v)
        total_in += in_tok
        total_out += out_tok
        if not analysis:
            print("    video analysis failed; skipping", file=sys.stderr)
            continue
        records.append({
            "video_id": v["video_id"],
            "title": v["title"],
            "published": v["published"],
            "url": v["url"],
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "analysis_source": "video",
            **analysis,
        })
        print(f"    native video -> {analysis['overall_sentiment']}")
    return records, total_in, total_out


def run_channel(channel, client, args):
    """Process one channel end-to-end: fetch -> filter -> analyze (native
    video) -> write ledger -> (maybe) regenerate the rolling current view."""
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
    records, in_tok, out_tok = process(todo, client, channel)

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
            # ...and append it to the shared, never-overwritten history log.
            sentiment_history.append_view(channel.key, view)
            print(f"Current view: {view['overall_sentiment']} "
                  f"(based on {view['based_on']['count']} videos); "
                  f"tokens in={cv_in} out={cv_out} (${cv_cost:.4f}) "
                  f"-> {channel.current_view_file}")
        else:
            # New videos landed but the synthesis failed, so the on-disk view (and
            # the Consensus row built from it) silently keeps describing an older
            # window. Say so loudly -- this used to be a no-op.
            print(f"  [current-view FAILED] {channel.key}: kept the previous "
                  f"{channel.current_view_file} -- it is now stale vs the ledger",
                  file=sys.stderr)


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
    # A wholesale Gemini failure never raises (each call site swallows its own
    # APIError), so without this the run exits 0 having written nothing and the
    # dashboard keeps serving the previous cards. Mirrors twitter_digest.main().
    if not args.dry_run:
        alert_gemini_outage(LLM_TALLY, "youtube_monitor")

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
