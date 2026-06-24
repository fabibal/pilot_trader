#!/usr/bin/env python3
"""
twitter_digest.py - recurring X/Twitter analysis digest (ANALYSIS ONLY).

Currently configured for @ki_young_ju (Ki Young Ju, founder/CEO of CryptoQuant;
data-driven Bitcoin on-chain / macro analyst). Mirrors youtube_monitor.py's
"scrape -> LLM -> json" shape, but for tweets instead of video:

  GetXAPI posts-only endpoint (no @-replies)        -> recent posts
   -> filter: drop retweets, Korean-language posts, and contentless posts;
      dedup by tweet id against the summaries ledger
   -> Claude Sonnet structured analysis (sentiment / market view / levels /
      themes / summary), written IN HUNGARIAN (sentiment enum stays English)
        + optional Sonnet VISION pass on chart image(s) -- same mechanism as
          monitor.py's IncomeSharks chart pass (capped at MAX_VISION_IMAGES) --
          whose read is merged into the record
   -> data/twitter_summaries.json   (one record per tweet, newest-first)

The summaries file is ALSO the dedup ledger: a tweet_id already present is
skipped, so reruns / the twice-weekly cron are idempotent (no reprocessing,
no double LLM spend).

ANALYSIS ONLY: @ki_young_ju is NOT in accounts.ACCOUNTS, is never written to
trades.json / positions.json, and is never mirrored to IBKR. Surfaced on the
dashboard Influencers tab next to the Ben Cowen YouTube analysis.

  python twitter_digest.py                 # process any new posts
  python twitter_digest.py --limit 3       # cap new posts this run (testing)
  python twitter_digest.py --force ID ...  # (re)process specific tweet ids
  python twitter_digest.py --dry-run       # analyze + print, do NOT write file
  python twitter_digest.py --no-vision     # skip the chart vision pass
"""
import argparse
import http.client
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
from datetime import datetime, timezone

import anthropic

from reconcile import write_json_atomic
# Reuse monitor.py's tested helpers (env/json loaders, GetXAPI client + tweet
# normalizer, the base64 image-block fetch, Telegram alerting) so this stays DRY
# and consistent with the rest of the pipeline.
import monitor
from monitor import (load_env, load_json, notify_telegram, TELEGRAM_ENVS,
                     getxapi_get, _normalize_getxapi, _image_block,
                     GETXAPI_BASE, GETXAPI_POSTS_PATH, GETXAPI_COST_PER_CALL,
                     MAX_VISION_IMAGES, SONNET_INPUT_PER_1M, SONNET_OUTPUT_PER_1M)

# --- config ---------------------------------------------------------------
ACCOUNT = "ki_young_ju"
DISPLAY_NAME = "Ki Young Ju"

HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")
SUMMARIES_FILE = os.path.join(DATA_DIR, "twitter_summaries.json")

# Sonnet 4.6 (NOT the tweet-signal pipeline's Haiku): we want a careful read of
# dense on-chain commentary, and the vision pass needs Sonnet anyway (Haiku can't
# take images). Same model the Cowen YouTube digest uses.
MODEL = "claude-sonnet-4-6"

# Fetch buffer. At ~2.5 posts/week and a twice-weekly cron, ~1-3 new posts land
# per run; 2 posts-only pages (~40) is ample headroom even after a missed run.
MAX_FETCH = 40
SKIP_LANGS = {"ko"}                   # requirement: skip Korean-language posts

# --- LLM analysis ---------------------------------------------------------
ANALYSIS_SYSTEM = (
    "You analyze a single X/Twitter post by @ki_young_ju (Ki Young Ju), founder "
    "and CEO of CryptoQuant. He is a data-driven Bitcoin on-chain / macro "
    "analyst (realized cap, MVRV, holder cohorts, exchange & ETF flows, "
    "MSTR/Strategy treasury dynamics, market cycles). Extract a concise, "
    "structured read of THIS post.\n"
    "Posts range from terse one-line chart captions to long-form essays; when a "
    "post is short, summarize only what it actually says -- never invent levels, "
    "metrics, or claims that are not in the text. If a chart image is attached, a "
    "separate vision pass handles it; analyze the TEXT here.\n"
    "Fields:\n"
    "- overall_sentiment: his NET directional stance toward Bitcoin / the market "
    "in THIS post -- the EXACT English enum value 'bullish', 'bearish', or "
    "'neutral' (neutral for mixed/cautious/range-bound/non-directional). KEEP "
    "THIS IN ENGLISH.\n"
    "- market_view: 1-2 sentences on his Bitcoin / market view or thesis in this "
    "post, IN HUNGARIAN.\n"
    "- key_levels: specific price levels or on-chain figures he actually states, "
    "each a short string WITH context, e.g. 'BTC realized price ~$53k', "
    "'$308B inflow', 'MVRV 2.6x'. Empty list if none are given.\n"
    "- top_themes: 2-4 short topic phrases capturing what the post is about, IN "
    "HUNGARIAN.\n"
    "- summary: a 2-3 sentence summary of the post's message, IN HUNGARIAN.\n"
    "LANGUAGE: write market_view, top_themes, and summary in fluent HUNGARIAN "
    "(magyarul). overall_sentiment MUST remain the exact English enum value (it "
    "drives dashboard logic); ticker symbols, metric names, and prices stay "
    "as-is.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "overall_sentiment": {"type": "string",
                              "enum": ["bullish", "bearish", "neutral"]},
        "market_view": {"type": "string"},
        "key_levels": {"type": "array", "items": {"type": "string"}},
        "top_themes": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["overall_sentiment", "market_view", "key_levels",
                 "top_themes", "summary"],
    "additionalProperties": False,
}

# Vision pass for attached chart images. His charts are on-chain/price analytics
# (realized cap, cohorts, MVRV, flows) rather than annotated trade setups, so we
# ask for a descriptive read, not entry/target/stop levels.
VISION_SYSTEM = (
    "You read an on-chain or price chart image attached to a tweet by Ki Young "
    "Ju (CryptoQuant). Using the chart AND the tweet text, describe only what is "
    "actually shown -- the metric/series plotted and the trend it implies. Do not "
    "invent values.\n"
    "Fields:\n"
    "- chart_trend: the directional read the chart implies for Bitcoin / the "
    "metric -- the EXACT English enum value 'bullish', 'bearish', or 'neutral'. "
    "KEEP THIS IN ENGLISH.\n"
    "- chart_summary: ONE or two sentences, IN HUNGARIAN, describing what the "
    "chart shows (which metric, the notable level or move). Stay factual.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "chart_trend": {"type": "string",
                        "enum": ["bullish", "bearish", "neutral"]},
        "chart_summary": {"type": "string"},
    },
    "required": ["chart_trend", "chart_summary"],
    "additionalProperties": False,
}


# --- GetXAPI fetch --------------------------------------------------------
# GetXAPI is an unaffiliated scraper with no SLA; it intermittently truncates
# responses mid-body (http.client.IncompleteRead) or drops the connection. Retry
# transient read/network errors with a short linear backoff so a momentary blip
# doesn't fail the daily run (and page Telegram). A sustained outage still raises
# after the final attempt; permanent HTTP client errors (4xx except 429, e.g. a
# bad key) raise immediately without burning retries.
GETXAPI_RETRIES = 3
GETXAPI_BACKOFF_S = 3
_TRANSIENT_NET = (http.client.HTTPException, OSError)   # OSError covers URLError


def _getxapi_get_retry(url):
    last = None
    for attempt in range(1, GETXAPI_RETRIES + 1):
        try:
            return getxapi_get(url)
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise                                  # permanent -> don't retry
            last = e
        except _TRANSIENT_NET as e:
            last = e
        if attempt < GETXAPI_RETRIES:
            wait = GETXAPI_BACKOFF_S * attempt
            print(f"  [getxapi] transient {type(last).__name__} "
                  f"(attempt {attempt}/{GETXAPI_RETRIES}); retrying in {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise last


def fetch_posts(seen_ids):
    """Cursor-paginate the GetXAPI posts-only endpoint for @ki_young_ju. Stops
    early once a page contains a tweet we've already analyzed (high-water dedup)
    or once MAX_FETCH raw tweets are collected. Returns (raw_tweets, n_calls).
    Keeps the RAW GetXAPI payloads (not the normalized shape) because we need the
    `lang` and retweet flags that _normalize_getxapi drops."""
    collected, cursor, calls = [], None, 0
    while len(collected) < MAX_FETCH:
        params = {"userName": ACCOUNT}
        if cursor:
            params["cursor"] = cursor
        data = _getxapi_get_retry(
            f"{GETXAPI_BASE}{GETXAPI_POSTS_PATH}?{urllib.parse.urlencode(params)}")
        calls += 1
        batch = data.get("tweets", [])
        if not batch:
            break
        hit_seen = False
        for tw in batch:
            collected.append(tw)
            if str(tw.get("id")) in seen_ids:
                hit_seen = True
        if hit_seen or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return collected[:MAX_FETCH], calls


def _is_retweet(tw):
    """True for a retweet (no original authored text)."""
    if tw.get("isRetweet") is True or tw.get("retweeted") is True:
        return True
    return tw.get("text", "").lstrip().startswith("RT @")


def _has_analyzable_content(tw):
    """False for contentless posts (e.g. profile-pic change, bare link) -- text
    is empty once URLs/mentions are stripped AND there is no media to read. Such
    posts can't be summarized, so we skip the (wasted) LLM call rather than emit
    a junk record."""
    if [m for m in (tw.get("media") or []) if m.get("type") == "photo"]:
        return True
    words = [w for w in tw.get("text", "").split()
             if not w.startswith(("http://", "https://", "@"))]
    return bool(words)


def select_candidates(raw, seen_ids):
    """Apply the requirement filters: own posts only, no replies, no retweets,
    no Korean, has content, not already seen. Newest-first."""
    out, batch_seen = [], set()
    for tw in raw:
        tid = str(tw.get("id"))
        # GetXAPI occasionally returns the same tweet twice in one feed; dedup
        # within the batch too so we don't pay for a second (collapsed) LLM call.
        if tid in seen_ids or tid in batch_seen:
            continue
        batch_seen.add(tid)
        author = ((tw.get("author") or {}).get("userName") or "").lower()
        if author and author != ACCOUNT.lower():
            continue                                  # foreign tweet (safety)
        if tw.get("isReply") is True:
            continue                                  # reply
        if _is_retweet(tw):
            continue                                  # retweet
        if (tw.get("lang") or "") in SKIP_LANGS:
            continue                                  # Korean
        if not _has_analyzable_content(tw):
            continue                                  # contentless
        out.append(tw)
    # newest first by numeric id (snowflake ids sort chronologically)
    out.sort(key=lambda t: int(t["id"]) if str(t.get("id")).isdigit() else 0,
             reverse=True)
    return out


# --- LLM calls ------------------------------------------------------------
def _parse_json(content_blocks):
    block = next((b.text for b in content_blocks if b.type == "text"), None)
    if not block:
        return None
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


def _usage_in(resp):
    return (resp.usage.input_tokens + (resp.usage.cache_read_input_tokens or 0)
            + (resp.usage.cache_creation_input_tokens or 0))


def analyze_text(client, account, date, text):
    """Sonnet text analysis. Returns (analysis_dict|None, in_tok, out_tok)."""
    user = f"Posted by @{account} ({DISPLAY_NAME})\nDate: {date}\nPost:\n{text}"
    try:
        resp = client.messages.create(
            # Hungarian is token-heavier than English; his long-form essays need
            # headroom or the JSON truncates and fails to parse (600 was too low).
            model=MODEL, max_tokens=1000,
            system=[{"type": "text", "text": ANALYSIS_SYSTEM}],
            output_config={"format": {"type": "json_schema",
                                      "schema": ANALYSIS_SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
        return None, 0, 0
    return _parse_json(resp.content), _usage_in(resp), resp.usage.output_tokens


def analyze_chart(client, media_urls, account, date, text):
    """Sonnet vision pass over the post's chart image(s) (capped at
    MAX_VISION_IMAGES, same as the IncomeSharks pass). Returns
    (chart_dict|None, in_tok, out_tok); None if no image could be fetched."""
    blocks = []
    for url in media_urls[:MAX_VISION_IMAGES]:
        b = _image_block(url)
        if b:
            blocks.append(b)
    if not blocks:
        return None, 0, 0
    content = blocks + [{
        "type": "text",
        "text": f"Posted by @{account} ({DISPLAY_NAME})\nDate: {date}\n"
                f"Tweet:\n{text}"}]
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=400,
            system=[{"type": "text", "text": VISION_SYSTEM}],
            output_config={"format": {"type": "json_schema",
                                      "schema": VISION_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        print(f"  [vision error] {type(e).__name__}: {e}", file=sys.stderr)
        return None, 0, 0
    return _parse_json(resp.content), _usage_in(resp), resp.usage.output_tokens


# --- main -----------------------------------------------------------------
def process(candidates, client, allow_vision=True):
    """Analyze each candidate tweet; return (records, in_tok, out_tok)."""
    records, total_in, total_out = [], 0, 0
    for tw in candidates:
        n = _normalize_getxapi(tw)               # id, text, created_at(iso), media
        tid = n["id"]
        date = (n.get("created_at") or "")[:10]
        text = n.get("text", "")
        print(f"- {tid}  {text[:70].replace(chr(10), ' ')}")

        analysis, in_t, out_t = analyze_text(client, ACCOUNT, date, text)
        total_in += in_t
        total_out += out_t
        if not analysis:
            print("    text analysis failed; skipping", file=sys.stderr)
            continue

        media = n.get("media") or []           # photo URLs (charts) on this post
        rec = {
            "tweet_id": tid,
            "created_at": n.get("created_at"),
            "url": tw.get("url") or f"https://x.com/{ACCOUNT}/status/{tid}",
            "text": text,
            "lang": tw.get("lang"),
            "analyzed_at": datetime.now(timezone.utc).isoformat(),
            "media": media,                    # persisted so the dashboard can
                                               # show the chart image(s) inline
            "has_chart": False,
            "chart_trend": None,
            "chart_summary": None,
            **analysis,
        }

        if media and allow_vision:
            chart, ci, co = analyze_chart(client, media, ACCOUNT, date, text)
            total_in += ci
            total_out += co
            if chart:
                rec["has_chart"] = True
                rec["chart_trend"] = chart.get("chart_trend")
                rec["chart_summary"] = chart.get("chart_summary")

        records.append(rec)
        tag = "  +chart" if rec["has_chart"] else ""
        print(f"    -> {rec['overall_sentiment']}{tag}")
    return records, total_in, total_out


def main():
    ap = argparse.ArgumentParser(description="@ki_young_ju X analysis digest")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many NEW posts to process this run")
    ap.add_argument("--force", nargs="+", metavar="TWEET_ID",
                    help="(re)process these specific tweet ids, ignoring dedup")
    ap.add_argument("--dry-run", action="store_true",
                    help="analyze and print, but do not write the summaries file")
    ap.add_argument("--no-vision", action="store_true",
                    help="skip the Sonnet chart-image vision pass")
    args = ap.parse_args()

    for p in TELEGRAM_ENVS:                       # loads ANTHROPIC + GETXAPI + alerts
        load_env(p)
    if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("GETXAPI_KEY"):
        print("Missing ANTHROPIC_API_KEY or GETXAPI_KEY in .env", file=sys.stderr)
        sys.exit(1)

    summaries = load_json(SUMMARIES_FILE, [])
    if not isinstance(summaries, list):
        summaries = []
    seen = {str(r.get("tweet_id")) for r in summaries}

    # --force bypasses dedup for the named ids. Scan the full window (empty
    # stop-set) so a forced id OLDER than the high-water mark is still reached;
    # the normal run stops early once it hits an already-seen post.
    force_ids = set(args.force or [])
    raw, calls = fetch_posts(set() if force_ids else seen)
    print(f"Fetched {len(raw)} raw posts in {calls} GetXAPI call(s) "
          f"(${calls * GETXAPI_COST_PER_CALL:.4f})")

    if force_ids:
        by_id = {str(t.get("id")): t for t in raw}
        candidates = [by_id[i] for i in force_ids if i in by_id]
        missing = force_ids - {str(t.get("id")) for t in candidates}
        if missing:
            print(f"  [force] not found in fetch window: {sorted(missing)}",
                  file=sys.stderr)
    else:
        candidates = select_candidates(raw, seen)
        if args.limit is not None:
            candidates = candidates[:args.limit]

    if not candidates:
        print("No new posts to process.")
        return

    print(f"Processing {len(candidates)} post(s)"
          + (" [FORCE]" if force_ids else "") + ":")
    client = anthropic.Anthropic()
    records, in_tok, out_tok = process(candidates, client,
                                       allow_vision=not args.no_vision)

    cost = (in_tok / 1_000_000 * SONNET_INPUT_PER_1M
            + out_tok / 1_000_000 * SONNET_OUTPUT_PER_1M)
    print(f"\nAnalyzed {len(records)} post(s); "
          f"Sonnet tokens in={in_tok} out={out_tok} (${cost:.4f})")

    if not records:
        return
    if args.dry_run:
        print("\n[dry-run] not writing; analysis below:")
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return

    # Merge: replace any re-forced ids, keep newest-first by created_at.
    by_id = {str(r["tweet_id"]): r for r in summaries}
    for r in records:
        by_id[str(r["tweet_id"])] = r
    merged = sorted(by_id.values(),
                    key=lambda r: r.get("created_at") or "", reverse=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(SUMMARIES_FILE, merged)
    print(f"Wrote {len(merged)} summaries -> {SUMMARIES_FILE}")


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:                   # log + page, then non-zero exit
        try:
            for p in TELEGRAM_ENVS:
                load_env(p)
            notify_telegram(f"twitter_digest.py FAILED: {exc!r}")
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
