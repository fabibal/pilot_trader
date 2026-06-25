#!/usr/bin/env python3
"""
twitter_digest.py - recurring X/Twitter analysis digest (ANALYSIS ONLY).

Runs one or more analysis-only feeds. Each feed mirrors youtube_monitor.py's
"scrape -> LLM -> json" shape, but for tweets instead of video:

  GetXAPI posts-only endpoint (no @-replies)        -> recent posts
   -> filter: drop retweets, replies, configured skip-languages, and
      contentless posts; dedup by tweet id against the feed's summaries ledger
   -> Claude Sonnet structured analysis (sentiment / market view / levels /
      themes / summary), written IN HUNGARIAN (sentiment enum stays English)
        + optional Sonnet VISION pass on chart image(s) -- same mechanism as
          monitor.py's IncomeSharks chart pass (capped at MAX_VISION_IMAGES) --
          whose read is merged into the record
   -> the feed's summaries json   (one record per tweet, newest-first)

The summaries file is ALSO the dedup ledger: a tweet_id already present is
skipped, so reruns / the daily cron are idempotent (no reprocessing, no double
LLM spend).

Feeds (see FEEDS):
  - ki_young_ju (Ki Young Ju, CryptoQuant founder; BTC on-chain / macro) ->
      data/twitter_summaries.json. Korean-language posts are skipped.
  - joao_wedson (Joao Wedson, Alphractal founder; crypto on-chain / quant) ->
      data/joao_summaries.json.

ANALYSIS ONLY: neither account is in accounts.ACCOUNTS; neither is written to
trades.json / positions.json; neither is mirrored to IBKR. Surfaced on the
dashboard Influencers tab next to the Ben Cowen YouTube analysis.

  python twitter_digest.py                      # process new posts, ALL feeds
  python twitter_digest.py --feed joao_wedson   # only this feed
  python twitter_digest.py --limit 3            # cap new posts/feed (testing)
  python twitter_digest.py --feed ki_young_ju --force ID ...  # (re)process ids
  python twitter_digest.py --dry-run            # analyze + print, do NOT write
  python twitter_digest.py --no-vision          # skip the chart vision pass
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
from dataclasses import dataclass
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
HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")

# Sonnet 4.6 (NOT the tweet-signal pipeline's Haiku): we want a careful read of
# dense on-chain commentary, and the vision pass needs Sonnet anyway (Haiku can't
# take images). Same model the Cowen YouTube digest uses.
MODEL = "claude-sonnet-4-6"

# --- LLM analysis ---------------------------------------------------------
# The system prompt is shared across feeds EXCEPT for a 1-2 sentence persona
# prefix (who the author is + what they cover). build_*_system() prepends the
# per-feed persona to this shared body so every feed gets identical field
# definitions and the Hungarian-output contract.
ANALYSIS_BODY = (
    "Extract a concise, "
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

# Vision pass for attached chart images. Their charts are on-chain/price
# analytics (realized cap, cohorts, MVRV, flows, liquidation levels) rather than
# annotated trade setups, so we ask for a descriptive read, not entry/target/stop
# levels.
VISION_BODY = (
    "Using "
    "the chart AND the tweet text, describe only what is "
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


# --- feed registry --------------------------------------------------------
@dataclass(frozen=True)
class Feed:
    """One analysis-only X account. The summaries file is its dedup ledger."""
    key: str
    account: str
    display_name: str
    summaries_file: str
    analysis_persona: str          # 1-2 sentence "who + what they cover" prefix
    vision_persona: str            # 1 sentence chart-reader prefix
    skip_langs: frozenset = frozenset()
    # Fetch buffer. Daily cron + high-water dedup means a normal run stops early
    # after a few new posts; this only bounds catch-up after a missed run.
    max_fetch: int = 40

    @property
    def analysis_system(self):
        return self.analysis_persona + " " + ANALYSIS_BODY

    @property
    def vision_system(self):
        return self.vision_persona + " " + VISION_BODY


FEEDS = {f.key: f for f in [
    Feed(
        key="ki_young_ju",
        account="ki_young_ju",
        display_name="Ki Young Ju",
        summaries_file=os.path.join(DATA_DIR, "twitter_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @ki_young_ju (Ki Young Ju), "
            "founder and CEO of CryptoQuant. He is a data-driven Bitcoin on-chain "
            "/ macro analyst (realized cap, MVRV, holder cohorts, exchange & ETF "
            "flows, MSTR/Strategy treasury dynamics, market cycles)."),
        vision_persona=(
            "You read an on-chain or price chart image attached to a tweet by Ki "
            "Young Ju (CryptoQuant)."),
        skip_langs=frozenset({"ko"}),   # requirement: skip Korean-language posts
        max_fetch=40,                    # ~2.5 posts/week
    ),
    Feed(
        key="joao_wedson",
        account="joao_wedson",
        display_name="Joao Wedson",
        summaries_file=os.path.join(DATA_DIR, "joao_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @joao_wedson (Joao Wedson), "
            "founder of Alphractal, a crypto on-chain & quantitative analytics "
            "platform. He is a data-driven crypto on-chain / quant analyst (MVRV, "
            "NUPL, liquidation levels, buy/sell pressure delta, open interest, "
            "market cycles & fractals, on-chain flows and derivatives across "
            "Bitcoin and major altcoins)."),
        vision_persona=(
            "You read an on-chain, quant, or price chart image attached to a "
            "tweet by Joao Wedson (Alphractal)."),
        # He posts in English; the occasional non-English item is a retweet/reply
        # /bare promo link already dropped by the RT/reply/contentless filters, so
        # no language is skipped (Sonnet outputs Hungarian regardless of input).
        skip_langs=frozenset(),
        max_fetch=60,                    # ~6 posts/day -> ~10 days of headroom
    ),
]}


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


def fetch_posts(feed, seen_ids):
    """Cursor-paginate the GetXAPI posts-only endpoint for the feed's account.
    Stops early once a page contains a tweet we've already analyzed (high-water
    dedup) or once feed.max_fetch raw tweets are collected. Returns
    (raw_tweets, n_calls). Keeps the RAW GetXAPI payloads (not the normalized
    shape) because we need the `lang` and retweet flags that _normalize_getxapi
    drops."""
    collected, cursor, calls = [], None, 0
    while len(collected) < feed.max_fetch:
        params = {"userName": feed.account}
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
    return collected[:feed.max_fetch], calls


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


def select_candidates(raw, seen_ids, feed):
    """Apply the requirement filters: own posts only, no replies, no retweets,
    no skip-language posts, has content, not already seen. Newest-first."""
    out, batch_seen = [], set()
    for tw in raw:
        tid = str(tw.get("id"))
        # GetXAPI occasionally returns the same tweet twice in one feed; dedup
        # within the batch too so we don't pay for a second (collapsed) LLM call.
        if tid in seen_ids or tid in batch_seen:
            continue
        batch_seen.add(tid)
        author = ((tw.get("author") or {}).get("userName") or "").lower()
        if author and author != feed.account.lower():
            continue                                  # foreign tweet (safety)
        if tw.get("isReply") is True:
            continue                                  # reply
        if _is_retweet(tw):
            continue                                  # retweet
        if (tw.get("lang") or "") in feed.skip_langs:
            continue                                  # skipped language
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


def analyze_text(client, feed, date, text):
    """Sonnet text analysis. Returns (analysis_dict|None, in_tok, out_tok)."""
    user = (f"Posted by @{feed.account} ({feed.display_name})\n"
            f"Date: {date}\nPost:\n{text}")
    try:
        resp = client.messages.create(
            # Hungarian is token-heavier than English; his long-form essays need
            # headroom or the JSON truncates and fails to parse (600 was too low).
            model=MODEL, max_tokens=1000,
            system=[{"type": "text", "text": feed.analysis_system}],
            output_config={"format": {"type": "json_schema",
                                      "schema": ANALYSIS_SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
        return None, 0, 0
    return _parse_json(resp.content), _usage_in(resp), resp.usage.output_tokens


def analyze_chart(client, feed, media_urls, date, text):
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
        "text": f"Posted by @{feed.account} ({feed.display_name})\nDate: {date}\n"
                f"Tweet:\n{text}"}]
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=400,
            system=[{"type": "text", "text": feed.vision_system}],
            output_config={"format": {"type": "json_schema",
                                      "schema": VISION_SCHEMA}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        print(f"  [vision error] {type(e).__name__}: {e}", file=sys.stderr)
        return None, 0, 0
    return _parse_json(resp.content), _usage_in(resp), resp.usage.output_tokens


# --- main -----------------------------------------------------------------
def process(candidates, client, feed, allow_vision=True):
    """Analyze each candidate tweet; return (records, in_tok, out_tok)."""
    records, total_in, total_out = [], 0, 0
    for tw in candidates:
        n = _normalize_getxapi(tw)               # id, text, created_at(iso), media
        tid = n["id"]
        date = (n.get("created_at") or "")[:10]
        text = n.get("text", "")
        print(f"- {tid}  {text[:70].replace(chr(10), ' ')}")

        analysis, in_t, out_t = analyze_text(client, feed, date, text)
        total_in += in_t
        total_out += out_t
        if not analysis:
            print("    text analysis failed; skipping", file=sys.stderr)
            continue

        media = n.get("media") or []           # photo URLs (charts) on this post
        rec = {
            "tweet_id": tid,
            "created_at": n.get("created_at"),
            "url": tw.get("url") or f"https://x.com/{feed.account}/status/{tid}",
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
            chart, ci, co = analyze_chart(client, feed, media, date, text)
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


def run_feed(feed, client, args):
    """Process one feed end-to-end: fetch -> filter -> analyze -> write ledger."""
    print(f"\n=== {feed.display_name} (@{feed.account}) ===")
    summaries = load_json(feed.summaries_file, [])
    if not isinstance(summaries, list):
        summaries = []
    seen = {str(r.get("tweet_id")) for r in summaries}

    # --force bypasses dedup for the named ids. Scan the full window (empty
    # stop-set) so a forced id OLDER than the high-water mark is still reached;
    # the normal run stops early once it hits an already-seen post.
    force_ids = set(args.force or [])
    raw, calls = fetch_posts(feed, set() if force_ids else seen)
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
        candidates = select_candidates(raw, seen, feed)
        if args.limit is not None:
            candidates = candidates[:args.limit]

    if not candidates:
        print("No new posts to process.")
        return

    print(f"Processing {len(candidates)} post(s)"
          + (" [FORCE]" if force_ids else "") + ":")
    records, in_tok, out_tok = process(candidates, client, feed,
                                       allow_vision=not args.no_vision)

    cost = (in_tok / 1_000_000 * SONNET_INPUT_PER_1M
            + out_tok / 1_000_000 * SONNET_OUTPUT_PER_1M)
    print(f"Analyzed {len(records)} post(s); "
          f"Sonnet tokens in={in_tok} out={out_tok} (${cost:.4f})")

    if not records:
        return
    if args.dry_run:
        print("[dry-run] not writing; analysis below:")
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return

    # Merge: replace any re-forced ids, keep newest-first by created_at.
    by_id = {str(r["tweet_id"]): r for r in summaries}
    for r in records:
        by_id[str(r["tweet_id"])] = r
    merged = sorted(by_id.values(),
                    key=lambda r: r.get("created_at") or "", reverse=True)
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(feed.summaries_file, merged)
    print(f"Wrote {len(merged)} summaries -> {feed.summaries_file}")


def main():
    ap = argparse.ArgumentParser(description="X analysis digest (analysis only)")
    ap.add_argument("--feed", choices=sorted(FEEDS), default=None,
                    help="process only this feed (default: all feeds)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap how many NEW posts to process per feed this run")
    ap.add_argument("--force", nargs="+", metavar="TWEET_ID",
                    help="(re)process these specific tweet ids, ignoring dedup; "
                         "requires --feed")
    ap.add_argument("--dry-run", action="store_true",
                    help="analyze and print, but do not write the summaries file")
    ap.add_argument("--no-vision", action="store_true",
                    help="skip the Sonnet chart-image vision pass")
    args = ap.parse_args()

    if args.force and not args.feed:
        ap.error("--force requires --feed (one feed at a time)")

    for p in TELEGRAM_ENVS:                       # loads ANTHROPIC + GETXAPI + alerts
        load_env(p)
    if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("GETXAPI_KEY"):
        print("Missing ANTHROPIC_API_KEY or GETXAPI_KEY in .env", file=sys.stderr)
        sys.exit(1)

    feeds = [FEEDS[args.feed]] if args.feed else list(FEEDS.values())
    client = anthropic.Anthropic()
    failed = []
    for feed in feeds:
        try:
            run_feed(feed, client, args)
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception as exc:
            # Single-feed run: hard-fail (the top-level handler pages + exits).
            # All-feeds run: one feed's transient outage (GetXAPI drop, network
            # blip mid-LLM) must NOT starve the other feed for the whole 24h cron
            # tick -- log + page for this feed, keep going, then exit non-zero so
            # the failure still surfaces.
            if len(feeds) == 1:
                raise
            traceback.print_exc()
            failed.append(feed.key)
            try:
                notify_telegram(f"twitter_digest {feed.key} FAILED: {exc!r}")
            except Exception:
                pass
    if failed:
        print(f"feeds failed: {', '.join(failed)}", file=sys.stderr)
        sys.exit(1)


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
