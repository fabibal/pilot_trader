#!/usr/bin/env python3
"""
twitter_digest.py - recurring X/Twitter analysis digest (ANALYSIS ONLY).

Runs one or more analysis-only feeds. Each feed mirrors youtube_monitor.py's
"scrape -> LLM -> json" shape, but for tweets instead of video:

  GetXAPI posts-only endpoint (no @-replies)        -> recent posts
   -> filter: drop retweets, replies, configured skip-languages, and
      contentless posts; dedup by tweet id against the feed's summaries ledger
   -> Gemini structured analysis (sentiment / market view / levels /
      themes / summary), written IN HUNGARIAN (sentiment enum stays English)
        + optional Gemini VISION pass on chart image(s) -- same mechanism as
          monitor.py's IncomeSharks chart pass (capped at MAX_VISION_IMAGES) --
          whose read is merged into the record
   -> the feed's summaries json   (one record per tweet, newest-first)

The summaries file is ALSO the dedup ledger: a tweet_id already present is
skipped, so reruns / the daily cron are idempotent (no reprocessing, no double
LLM spend).

A feed is either a USER timeline (one account's posts) or a TOPIC SEARCH (every
account's posts matching a GetXAPI advanced-search query). Most feeds emit one
record per post; a FORECAST-LEDGER feed (kendrick_sc) instead emits one record
per forecast (see the "forecast-ledger mode" block + run_forecast_feed()).

Feeds (see FEEDS):
  - ki_young_ju (Ki Young Ju, CryptoQuant founder; BTC on-chain / macro) ->
      data/twitter_summaries.json. Korean-language posts are skipped.
  - joao_wedson (Joao Wedson, Alphractal founder; crypto on-chain / quant) ->
      data/joao_summaries.json.
  - dorkchicken (DorkChicken; crypto/macro technical analyst -- chart-pattern
      and historical-cycle comparisons, no explicit trade calls) ->
      data/dorkchicken_summaries.json.
  - daancrypto (DaanCrypto; crypto technical analyst -- moving averages,
      Fibonacci, market structure/liquidity clusters, ETF flows, dominance,
      plus recurring risk-management/psychology posts; no explicit trade
      calls) -> data/daancrypto_summaries.json.
  - donalt (DonAlt; crypto trader/TA -- high-timeframe BTC support/resistance
      with occasional explicit conditional entries + invalidation levels,
      mixed with off-topic banter/CT drama; very low frequency) ->
      data/donalt_summaries.json.
  - cowen_x (Benjamin Cowen, Into The Cryptoverse founder; the X side of the
      same analyst youtube_monitor.py's 'cowen' channel covers -- macro-first
      and short-form here: Fed path, long-end yields, DXY, liquidity/labor
      cycles, BTC vs midterm-year seasonality, plus ITC promo posts) ->
      data/cowen_x_summaries.json. Key is cowen_x, NOT cowen: sentiment_history
      shares one key namespace with the YouTube channel registry.
  - glassnode (Glassnode; on-chain + derivatives analytics COMPANY account --
      options/vol positioning, futures basis & funding, ETF flows, holder
      cohorts and cost-basis models. The only company account here: ~7% of its
      posts are product/corporate news, handled by the persona prompt like
      DonAlt's off-topic banter) -> data/glassnode_summaries.json.
  - truecrypto (Truecrypto; crypto trader -- BTC price structure/range
      trading, ETF flows) -> data/truecrypto_summaries.json. Added
      2026-09-03. ~68% of his volume is off-topic (motivational filler,
      stock/macro, trading-psychology anecdotes, paid-tool promo), so this
      is the first feed with require_market_signal=True: a stricter pre-LLM
      gate (_has_market_signal) that only lets a post through if its text
      carries a concrete number AND a crypto keyword -- everything else
      never reaches Gemini at all.
  - kendrick_sc (TOPIC SEARCH + FORECAST LEDGER): Standard Chartered / Geoff
      Kendrick crypto price calls, deduplicated into one row per forecast ->
      data/kendrick_forecasts.json ({seen_ids, forecasts}). No single account is
      dedicated to his calls and each call is echoed by 20-30 outlets, so we
      search the topic, Gemini-triage each post into (asset, direction, target,
      timeframe), cluster echoes by (asset, normalized price target), and Gemini-read one
      representative per new forecast. English only; source count = reach.

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
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors

from reconcile import write_json_atomic
import sentiment_history
# Reuse monitor.py's tested helpers (env/json loaders, GetXAPI client + tweet
# normalizer, the Gemini image-block fetch, Telegram alerting) so this stays DRY
# and consistent with the rest of the pipeline.
import monitor
from monitor import (load_env, load_json, notify_telegram, TELEGRAM_ENVS,
                     getxapi_get, _normalize_getxapi, _image_block,
                     GETXAPI_BASE, GETXAPI_POSTS_PATH, GETXAPI_COST_PER_CALL,
                     MAX_VISION_IMAGES, GEMINI_INPUT_PER_1M, GEMINI_OUTPUT_PER_1M,
                     GEMINI_THINKING, GEMINI_DEEP_THINKING,
                     MODEL as EXTRACT_MODEL, EXTRACT_INPUT_PER_1M,
                     EXTRACT_OUTPUT_PER_1M, EXTRACT_THINKING, _first_sentence,
                     GeminiTally, alert_gemini_outage)

# --- config ---------------------------------------------------------------
HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")

# Every Gemini call this run, across all feeds. Each helper below swallows its
# own APIError so one bad post cannot starve the rest of the feed, so this is
# the only thing that can tell "quiet run" from "we were blind" -- main() pages
# on it at the end. See monitor.GeminiTally.
LLM_TALLY = GeminiTally()

# GetXAPI advanced-search endpoint, used by TOPIC SEARCH feeds. monitor.py only
# ever fetches user timelines, so (unlike GETXAPI_POSTS_PATH) this path is not
# imported from there. $0.001/call, ~20 tweets/page.
GETXAPI_SEARCH_PATH = "/twitter/tweet/advanced_search"

# Gemini 3.5 Flash (a step up from the cheap gemini-2.5-flash-lite the
# tweet-signal pipeline and this file's own Kendrick triage use): we want a
# careful read of dense on-chain commentary, and the vision pass needs a
# multimodal model anyway. Same model the Cowen YouTube digest uses. Both calls
# pin thinking_config=GEMINI_THINKING (budget 0) — Gemini 3.5 Flash thinks by
# default, which would eat the tight max_output_tokens and truncate the JSON output.
# 2026-07-13 cost review: Batch Mode (50% off) evaluated and rejected here --
# at this feed set's real volume the saving is single-digit $/mo, not worth
# the async submit/poll/expiry machinery this file doesn't have, and 2026
# reports (GitHub googleapis/python-genai#2221/#1482) show batch jobs
# sometimes stuck in PENDING for 24-96h+ -- unacceptable for a daily-cron
# freshness expectation. Context caching also evaluated and rejected: every
# feed's analysis_system is 400-450 tokens, far under every documented
# minimum (Gemini's implicit/explicit caching needs >=2048 tokens on 2.5
# Flash, >=4096 on 3.5 Flash). Revisit only if either changes materially.
MODEL = "gemini-3.5-flash"

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


# --- rolling "current view" synthesis -------------------------------------
# Once per run, IF new posts were processed, synthesize a short rolling stance
# from the feed's recent history -- shown as a banner above the individual
# post cards on the dashboard, so "what does he think right now" doesn't
# require manually reading and merging several cards. Runs over the
# already-distilled per-post fields (market_view/summary/top_themes), not raw
# tweet text, so it's a cheap second-pass read, not a re-analysis.
CURRENT_VIEW_DAYS = 14           # day-based floor
CURRENT_VIEW_MIN_POSTS = 8       # thin/slow feeds (e.g. ki_young_ju) extend by
                                 # count when the day window is too sparse
CURRENT_VIEW_MAX_POSTS = 25      # hard cap so a firehose feed (joao_wedson)
                                 # doesn't blow out the prompt

CURRENT_VIEW_BODY = (
    "Below is a list of his recent analyzed posts, OLDEST FIRST, each as "
    "\"[date] sentiment | themes -- market view\". Synthesize his CURRENT "
    "overall stance across this window -- do not just rehash the newest "
    "post.\n"
    "Fields:\n"
    "- overall_sentiment: his NET stance across THESE posts -- the EXACT "
    "English enum value 'bullish', 'bearish', 'neutral', or 'mixed'. Use "
    "'mixed' when the posts genuinely disagree or pull in different "
    "directions (not merely cautious -- that is 'neutral'). KEEP THIS IN "
    "ENGLISH.\n"
    "- stance_summary: 2-4 sentences, IN HUNGARIAN, on his current overall "
    "view/thesis across the window -- what he is focused on and where he "
    "leans. Weigh the whole window, not just the newest post.\n"
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


# --- forecast-ledger mode (kendrick_sc) -----------------------------------
# kendrick_sc is a TOPIC SEARCH where ONE research event (a Standard Chartered /
# Geoff Kendrick price call) is echoed by 20-30 outlets. Per-post cards would be
# 20-30 near-duplicates, so this feed runs in FORECAST-LEDGER mode: a cheap Gemini
# triage extracts the forecast(s) from each new post, posts are CLUSTERED by
# (asset, normalized price target -- so '$40K' and '$40,000' are one row while
# ETH $4,000 vs $40,000 stay distinct), and only a genuinely NEW forecast
# triggers the (expensive)
# Gemini read of one representative post. Echoes of a known forecast just append a
# source + bump the count. One ledger row per forecast; the source count IS the
# signal (how widely the call was picked up). Dedup is semantic (the forecast),
# not textual (text_sig), so differently-worded reports of one call still merge.
TRIAGE_MODEL = EXTRACT_MODEL          # cheap gate (gemini-2.5-flash-lite); the
                                     # analysis-tier MODEL is only used for new forecasts

TRIAGE_BODY = (
    "You triage a single X/Twitter post for whether it states or relays a "
    "SPECIFIC crypto PRICE FORECAST or PRICE TARGET attributed to Standard "
    "Chartered or its analyst Geoff Kendrick (Head of Digital Assets Research).\n"
    "Set relevant=true ONLY if the post conveys such a call (an asset + a "
    "directional view, with or without a numeric target/timeframe). Set "
    "relevant=false for posts that merely name-drop Standard Chartered or "
    "Kendrick without a crypto price call -- generic market recaps, ads, "
    "unrelated banking news, or a trader's own opinion that does not relay an "
    "SC/Kendrick call.\n"
    "For EACH distinct forecast in the post, return:\n"
    "- asset: the crypto TICKER in uppercase (BTC, ETH, XRP, AAVE, UNI, ...), "
    "not the full name.\n"
    "- direction: 'up', 'down', or 'neutral'.\n"
    "- target: the price target or magnitude AS STATED, short (e.g. '$3,500', "
    "'50x', '$200k', '$1M'); empty string if none is given.\n"
    "- timeframe: the horizon, normalized to a 4-digit year when one is given or "
    "implied ('by 2030'->'2030', 'end of 2025'->'2025'); else a short token "
    "such as 'unspecified'.\n"
    "A post may contain MULTIPLE DISTINCT forecasts (e.g. UNI to $100 AND AAVE "
    "to $3,500) -> return one object each. BUT if a post lays out a multi-year "
    "PATH or trajectory toward a single headline target for ONE asset (e.g. "
    "yearly waypoints $1k/2026 ... $3.5k/2030), return ONLY the headline/final "
    "target for that asset, not each intermediate year. If relevant=false, "
    "return an empty forecasts list.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "forecasts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "asset": {"type": "string"},
                "direction": {"type": "string",
                              "enum": ["up", "down", "neutral"]},
                "target": {"type": "string"},
                "timeframe": {"type": "string"},
            },
            "required": ["asset", "direction", "target", "timeframe"],
            "additionalProperties": False,
        }},
    },
    "required": ["relevant", "forecasts"],
    "additionalProperties": False,
}

SOURCES_CAP = 60          # max sources stored per forecast (source_count is exact)
SEEN_IDS_CAP = 1500       # bound the triaged-id high-water set


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
    only_langs: frozenset = frozenset()   # if non-empty, keep ONLY these langs
    # TOPIC SEARCH feeds set `query` (a GetXAPI advanced-search query) instead of
    # tracking one account. The feed then ingests EVERY account's posts matching
    # the query and `account` is unused for fetching. `product` is the search
    # ordering: 'Latest' (newest-first) is required for the high-water stop.
    query: str = None
    product: str = "Latest"
    # Forecast-ledger mode (search feeds only): cluster echoes of one research
    # event into a single forecast row instead of one card per post. See the
    # "forecast-ledger mode" block above and run_forecast_feed().
    forecast_ledger: bool = False
    # Opt this feed's TEXT analysis into Gemini dynamic thinking (the chart
    # vision pass stays disabled regardless). Only worth it for dense, low-volume
    # reads -- kept off for the high-volume per-tweet feeds (ki/joao) where it adds
    # cost without much gain. analyze_text gives max_output_tokens room when this is set.
    deep_thinking: bool = False
    # Fetch buffer. Daily cron + high-water dedup means a normal run stops early
    # after a few new posts; this only bounds catch-up after a missed run.
    max_fetch: int = 40
    # Sibling file for the rolling "current view" synthesis (see the
    # "rolling current view synthesis" block above). None (default, e.g.
    # kendrick_sc) skips the feature -- forecast-ledger feeds already have
    # their own "current state" view: the forecast table itself.
    current_view_file: str = None
    # Opt-in stricter pre-LLM gate (see _has_market_signal): text must carry a
    # concrete number (price/%/level) AND a crypto keyword, or the post is
    # dropped before ever reaching Gemini. Per-feed, default False -- every
    # other feed keeps using the plain _has_analyzable_content check. Added
    # for truecrypto (see its persona comment): ~68% of his volume is
    # off-topic motivational/promo content _has_analyzable_content can't tell
    # apart from real market commentary since it has real text either way.
    require_market_signal: bool = False

    @property
    def is_search(self):
        return bool(self.query)

    @property
    def analysis_system(self):
        return self.analysis_persona + " " + ANALYSIS_BODY

    @property
    def vision_system(self):
        return self.vision_persona + " " + VISION_BODY

    @property
    def current_view_system(self):
        return self.analysis_persona + " " + CURRENT_VIEW_BODY


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
        current_view_file=os.path.join(DATA_DIR, "ki_young_ju_current_view.json"),
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
        # no language is skipped (Gemini outputs Hungarian regardless of input).
        skip_langs=frozenset(),
        max_fetch=60,                    # ~6 posts/day -> ~10 days of headroom
        current_view_file=os.path.join(DATA_DIR, "joao_wedson_current_view.json"),
    ),
    Feed(
        key="dorkchicken",
        account="DorkChicken",
        display_name="DorkChicken",
        summaries_file=os.path.join(DATA_DIR, "dorkchicken_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @DorkChicken, a crypto/macro "
            "technical analyst covering Bitcoin and altcoins (moving averages, "
            "RSI divergence, Bollinger Bands, Gaussian Channel, BTC dominance) "
            "with frequent historical-cycle fractal comparisons (e.g. 2022 vs "
            "the current cycle), plus occasional traditional-market charts "
            "(equities, gold). His posts are structural/technical reads, not "
            "specific entry/exit trade calls."),
        vision_persona=(
            "You read a price or technical-indicator chart image attached to a "
            "tweet by DorkChicken (crypto/macro TA)."),
        # Posts in English only (see module docstring eval); no skip needed.
        skip_langs=frozenset(),
        max_fetch=60,                    # bursty (dormant stretches then ~4
                                         # posts/day) -- bounds catch-up after a gap
        current_view_file=os.path.join(DATA_DIR, "dorkchicken_current_view.json"),
    ),
    Feed(
        key="daancrypto",
        account="DaanCrypto",
        display_name="DaanCrypto",
        summaries_file=os.path.join(DATA_DIR, "daancrypto_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @DaanCrypto, a crypto "
            "technical analyst covering Bitcoin and Ethereum (moving averages, "
            "Fibonacci retracements, market structure and liquidity clusters, "
            "the Bull Market Support Band, ETF flows, BTC/ETH dominance) plus "
            "altcoins and occasional macro (oil, FOMC) commentary. He also "
            "posts recurring trading-psychology and risk-management notes. His "
            "posts are structural/technical level-watching, not specific "
            "entry/exit trade calls."),
        vision_persona=(
            "You read a price or technical-indicator chart image attached to "
            "a tweet by DaanCrypto (crypto TA)."),
        # Posts in English only (see module docstring eval); no skip needed.
        skip_langs=frozenset(),
        max_fetch=60,                    # ~5 posts/day -> ~12 days of headroom
        current_view_file=os.path.join(DATA_DIR, "daancrypto_current_view.json"),
    ),
    Feed(
        key="donalt",
        account="DonAlt",
        display_name="DonAlt",
        summaries_file=os.path.join(DATA_DIR, "donalt_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @DonAlt, a crypto trader "
            "and technical analyst covering Bitcoin (high-timeframe support/"
            "resistance, monthly and weekly close levels, conditional "
            "buy-the-dip zones with explicit invalidation) plus occasional "
            "TSLA/MSTR commentary. Many of his posts are off-topic banter, "
            "humor, or crypto-Twitter drama rather than market analysis -- if "
            "a post has no market content, summarize only that, do not force "
            "a market read onto it."),
        vision_persona=(
            "You read a price chart image attached to a tweet by DonAlt "
            "(crypto trader focused on Bitcoin support/resistance)."),
        # Posts in English only (see module docstring eval); no skip needed.
        skip_langs=frozenset(),
        max_fetch=40,                    # very low frequency, ~1 own post/9 days
        current_view_file=os.path.join(DATA_DIR, "donalt_current_view.json"),
    ),
    Feed(
        # NOT "cowen": sentiment_history.py keys its log on the registry key and
        # both registries share that namespace, so this must not collide with
        # youtube_monitor.CHANNELS["cowen"] (the same person's video digest).
        key="cowen_x",
        account="benjamincowen",         # @intocryptoverse does NOT exist (404)
        display_name="Benjamin Cowen",
        summaries_file=os.path.join(DATA_DIR, "cowen_x_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @benjamincowen (Benjamin "
            "Cowen), founder of Into The Cryptoverse (ITC) and a quantitative "
            "macro analyst (PhD engineering, ex-NASA/Sandia). On X he is "
            "MACRO-FIRST and much shorter-form than in his videos: Fed policy "
            "path and rate decisions, the long end of the curve (10Y/30Y "
            "yields), DXY, liquidity / business / labor cycles, and how "
            "Bitcoin behaves against those -- including midterm-year "
            "seasonality and comparisons to prior cycles -- plus scepticism "
            "of popular narratives (e.g. 'Bitcoin lags M2', soft macro "
            "indicators). He also posts ITC conference and speaking "
            "announcements, and bare links to his own YouTube videos. If a "
            "post is promotion or an announcement with no market content, "
            "summarize only that -- do NOT force a market read onto it."),
        vision_persona=(
            "You read a macro or price chart image (yields, DXY, Fed path, "
            "Bitcoin cycle comparisons) attached to a tweet by Benjamin Cowen "
            "(Into The Cryptoverse)."),
        # 50-post sample 2026-08-02: 36 own posts all lang=en, 0 non-English.
        skip_langs=frozenset(),
        max_fetch=40,                    # ~0.9 own posts/day + ~26% retweets
                                         # -> ~3 weeks of catch-up headroom
        current_view_file=os.path.join(DATA_DIR, "cowen_x_current_view.json"),
    ),
    Feed(
        # The only COMPANY account in the registry. Evaluated 2026-08-02 against
        # the marketing-funnel risk: of 83 top-level posts only ~7% were
        # corporate/product news and 57% carried an explicit figure IN the tweet,
        # so the free feed stands on its own even though the long-form research
        # the links point at is partly paywalled (we never follow the links).
        key="glassnode",
        account="glassnode",
        display_name="Glassnode",
        summaries_file=os.path.join(DATA_DIR, "glassnode_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @glassnode, the on-chain "
            "and derivatives analytics company. Posts are desk-style data "
            "reads, not trade calls, and lean on: OPTIONS/VOLATILITY "
            "(implied-volatility term structure, 25-delta skew, put/call "
            "ratio, gamma zones, DVOL, open interest), FUTURES (funding "
            "rates, annualized basis, liquidations), ETF FLOWS, and ON-CHAIN "
            "COHORTS (long/short-term holders, realized profit/loss, "
            "cost-basis and price models such as True Market Mean / Active "
            "Investors Mean / STH cost basis, accumulation trend score, "
            "supply in profit or loss, dormancy), plus their own named "
            "frameworks (Bitcoin Vector, Altcoin Cycle Signal, Market Pulse, "
            "Week On-Chain). Two things to handle: (1) some posts are "
            "COMPANY news -- product launches, funding rounds, conference "
            "talks, hiring, infrastructure/latency notes. If a post has no "
            "market content, summarize only that, do NOT force a market read "
            "onto it. (2) their copy often renders the word bitcoin as the "
            "token 'bitcoin:native' -- read it as plain 'bitcoin'/BTC and "
            "never treat it as a ticker, product or chain name."),
        vision_persona=(
            "You read a Glassnode metric chart image (on-chain, options, "
            "futures or ETF-flow data, usually their dark-themed studio "
            "charts) attached to a tweet by @glassnode."),
        # 97-post sample 2026-08-02: 100% English.
        skip_langs=frozenset(),
        max_fetch=60,                    # ~1.1 own posts/day + ~16% retweets
        current_view_file=os.path.join(DATA_DIR, "glassnode_current_view.json"),
    ),
    Feed(
        # A prior 2026-06 audit (scripts/audit_truecrypto.py, since removed
        # in the pre-public cleanup) ran this account through monitor.py's
        # ACTIONABLE-signal schema and found almost nothing (7 calls / 200
        # posts, 86.5% neutral) -- the wrong bar; this analysis-only digest
        # pattern didn't exist yet then. Re-evaluated fresh 2026-09-03
        # against THIS pattern: 25-post sample, only 32% genuine crypto
        # content (32% pure motivational filler unrelated to markets, 16%
        # off-topic stock/macro, 12% trading-psychology anecdotes, 8%
        # explicit promo for his own paid "True Vibration" tool). Initially
        # recommended against adding on that noise/cost ratio -- reconsidered
        # after designing require_market_signal (see the Feed field and
        # _has_market_signal): with it, only the ~20% that's genuine BTC
        # content with a real number reaches Gemini at all, and 2
        # spot-checked price claims ("low-$60Ks to $78K in 3 days, above
        # $81K" and "break $80K...rejected, lose $77K...bought back") both
        # matched real BTC-USD data closely. One overstated claim found too
        # (his pinned tweet's "8x since the Nov 2022 low" was really ~7.3x)
        # -- not egregious, but real; the persona below does not give
        # promotional posts a pass just because they cite a number.
        key="truecrypto",
        account="Truecrypto",
        display_name="Truecrypto",
        summaries_file=os.path.join(DATA_DIR, "truecrypto_summaries.json"),
        analysis_persona=(
            "You analyze a single X/Twitter post by @Truecrypto, a crypto "
            "trader focused on Bitcoin price structure and range trading "
            "(support/resistance, monthly-open levels, liquidity sweeps, "
            "regime/cycle shifts) and BTC/ETH ETF flow data. Most of his "
            "volume is NOT market content: frequent generic motivational/"
            "self-improvement posts unrelated to markets, trading-psychology "
            "anecdotes, and recurring promotion of his own paid tool ('True "
            "Vibration'). If a post is promotional -- even one that states a "
            "price or percentage while pitching the tool -- treat it as "
            "promo and summarize only that; do NOT let the presence of a "
            "number turn a promotional post into a market read. If a post "
            "has no real market content, summarize only that."),
        vision_persona=(
            "You read a Bitcoin price or technical-level chart image "
            "attached to a tweet by Truecrypto (crypto trader)."),
        skip_langs=frozenset(),          # 25-post sample 2026-09-03: 100% en
        max_fetch=60,                    # ~5 originals/day + ~4.4 RT/day raw
        current_view_file=os.path.join(DATA_DIR, "truecrypto_current_view.json"),
        require_market_signal=True,      # ~68% of his volume is off-topic/
                                         # promo filler _has_analyzable_content
                                         # can't catch (real text either way)
    ),
    Feed(
        key="kendrick_sc",
        account="",                      # topic search: no single timeline
        display_name="Geoff Kendrick / Standard Chartered",
        summaries_file=os.path.join(DATA_DIR, "kendrick_forecasts.json"),
        forecast_ledger=True,            # one row per forecast, not per post
        # deep_thinking was True (adaptive thinking on the forecast read) until
        # a 2026-07-13 cost review: live A/B on real Cowen/tweet content showed
        # dynamic thinking burned ~65-75% of output tokens on invisible
        # reasoning with no measurable quality gain over thinking off. Kept
        # False here; the mechanism stays available for a future feed that
        # needs it.
        deep_thinking=False,
        # No account is dedicated to his calls; reputable outlets cover him only
        # incidentally amid huge volume. So search the TOPIC and let every
        # account's coverage flow in -- ~100% relevant, ~2-6 unique en posts/day.
        query=('"Geoff Kendrick" OR ("Standard Chartered" '
               '(bitcoin OR BTC OR crypto OR XRP OR ethereum OR ETH OR AAVE))'),
        product="Latest",
        only_langs=frozenset({"en"}),    # English coverage only (drops the many
                                         # zh/es/vi/fr reposts of each headline)
        analysis_persona=(
            "You analyze a single X/Twitter post about Geoff Kendrick (Head of "
            "Digital Assets Research at Standard Chartered) and/or Standard "
            "Chartered's crypto research. Posts come from news outlets, analysts, "
            "and traders relaying his price targets, forecasts, and research notes "
            "on Bitcoin, XRP, AAVE, and other digital assets. Read the post as the "
            "analyst commentary it relays: 'his' below refers to the directional "
            "view expressed in the post (typically Standard Chartered's / "
            "Kendrick's call)."),
        vision_persona=(
            "You read a price or on-chain chart image attached to a post relaying "
            "Standard Chartered / Geoff Kendrick crypto research."),
        max_fetch=60,                    # bursty; bounds catch-up after a gap
    ),
]}


# --- GetXAPI fetch --------------------------------------------------------
# GetXAPI is an unaffiliated scraper with no SLA; it intermittently truncates
# responses mid-body (http.client.IncompleteRead) or drops the connection. Retry
# transient read/network errors with a short linear backoff so a momentary blip
# doesn't fail the daily run (and page Telegram). A sustained outage still raises
# after the final attempt; permanent HTTP client errors (4xx except 429, e.g. a
# bad key) raise immediately without burning retries.
# 2026-07-13: bumped 3->5 -- kendrick_sc's fetch_search (advanced-search
# endpoint) twice exhausted all 3 attempts (both times failing attempts 1 AND
# 2 before the fatal 3rd) and paged Telegram with "IncompleteRead(10 bytes
# read)", while fetch_posts's timeline endpoint has always recovered within 2
# attempts in the same window -- the search endpoint's payloads appear bigger
# and/or less reliably gzip-negotiated. Mirrors monitor.getxapi_get_retry,
# bumped there too rather than diverging the two.
GETXAPI_RETRIES = 5
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


def fetch_search(feed, seen_ids):
    """Cursor-paginate the GetXAPI advanced-search endpoint for the feed's query
    (product=Latest -> newest-first). Same high-water stop as fetch_posts: stop
    once a page contains an already-analyzed id, or once max_fetch raw tweets are
    collected. Mirrors fetch_posts; only the path/params differ (q/product vs
    userName). Returns (raw_tweets, n_calls)."""
    collected, cursor, calls = [], None, 0
    while len(collected) < feed.max_fetch:
        params = {"q": feed.query, "product": feed.product}
        if cursor:
            params["cursor"] = cursor
        data = _getxapi_get_retry(
            f"{GETXAPI_BASE}{GETXAPI_SEARCH_PATH}?{urllib.parse.urlencode(params)}")
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


def _content_sig(text):
    """Normalized content fingerprint for collapsing verbatim reposts: drop URLs,
    @mentions, and an 'RT @x:' prefix, lowercase, strip non-alphanumerics, collapse
    whitespace, take the first 120 chars. Two accounts relaying the same headline
    map to the same signature, so the search feed analyzes each story once."""
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"^\s*RT @\w+:", "", t)
    t = re.sub(r"@\w+", "", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return re.sub(r"\s+", " ", t).strip()[:120]


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


# Stricter gate for Feed.require_market_signal=True (see truecrypto). Text
# must carry a concrete number AND a crypto keyword. Evaluated 2026-09-03 on
# a real 25-post sample: 25/25 pass _has_analyzable_content (has real text
# either way -- can't tell a motivational quote from a price call), vs 5/25
# passing this gate, all 5 genuine BTC content with real, spot-checked-
# accurate levels. A looser number-only version (no keyword requirement)
# passed 9/25 -- it let general stock/macro posts (Apple, Broadcom, Brent
# crude) through too, since those also carry $/% figures; the keyword
# requirement is what filters those back out.
_PRICE_RE = re.compile(r"\$\s?[\d,]+(?:\.\d+)?\s?[kKmMbBtT]?\b")
_PCT_RE = re.compile(r"\b\d+(?:\.\d+)?\s?%")
_BARE_LEVEL_RE = re.compile(r"\b\d{2,3}[kK]\b|\b\d{4,}(?:\.\d+)?\b")
_CRYPTO_KW_RE = re.compile(r"bitcoin|\bbtc\b|ethereum|\beth\b|crypto|altcoin",
                           re.I)


def _has_market_signal(tw):
    """Unlike _has_analyzable_content, a photo does NOT auto-pass here --
    deliberately: a promo post can carry screenshots with no price in the
    caption (observed: a 4-photo product-promo post from truecrypto with no
    $/%/keyword match at all), and the whole point of this gate is to keep
    that kind of post from ever reaching Gemini. Tradeoff: a genuine
    chart-only post with an unlabeled level and no numeric caption text also
    gets dropped -- an accepted false-negative for a cost/noise-conscious
    gate, not an oversight."""
    text = tw.get("text", "")
    has_num = bool(_PRICE_RE.search(text) or _PCT_RE.search(text)
                   or _BARE_LEVEL_RE.search(text))
    has_kw = bool(_CRYPTO_KW_RE.search(text))
    return has_num and has_kw


def select_candidates(raw, seen_ids, feed, seen_sigs=frozenset()):
    """Apply the requirement filters: no replies, no retweets, no skip-language
    posts (or, if only_langs is set, ONLY those langs), has content, not already
    seen. User feeds additionally require the post be the account's own; search
    feeds keep any author but collapse near-identical reposts (the same headline
    from many accounts) by content signature vs this batch AND the ledger.
    Newest-first."""
    out, batch_seen, batch_sigs = [], set(), set()
    for tw in raw:
        tid = str(tw.get("id"))
        # GetXAPI occasionally returns the same tweet twice in one feed; dedup
        # within the batch too so we don't pay for a second (collapsed) LLM call.
        if tid in seen_ids or tid in batch_seen:
            continue
        batch_seen.add(tid)
        author = ((tw.get("author") or {}).get("userName") or "").lower()
        # User feed: only the account's own posts (a thread can carry foreign
        # replies). Search feed: any account may match the query.
        if not feed.is_search and author and author != feed.account.lower():
            continue                                  # foreign tweet (safety)
        if tw.get("isReply") is True:
            continue                                  # reply
        if _is_retweet(tw):
            continue                                  # retweet
        lang = tw.get("lang") or ""
        if lang in feed.skip_langs:
            continue                                  # skipped language
        # Language-agnostic tags stay: Twitter marks media-only posts 'qme',
        # short ticker/number-heavy text 'und'/'zxx' -- often English content.
        if (feed.only_langs and lang not in feed.only_langs
                and lang not in ("", "und", "qme", "zxx")):
            continue                                  # not an allowed language
        gate = _has_market_signal if feed.require_market_signal else _has_analyzable_content
        if not gate(tw):
            continue                                  # contentless / no market signal
        # Plain search feeds collapse verbatim reposts by text signature.
        # Forecast-ledger feeds skip this -- they dedup SEMANTICALLY (by the
        # extracted forecast), so differently-worded reports of one call still
        # merge, and a shared boilerplate prefix must NOT drop a distinct call.
        if feed.is_search and not feed.forecast_ledger:
            sig = _content_sig(tw.get("text", ""))
            if sig and (sig in seen_sigs or sig in batch_sigs):
                continue                              # verbatim repost of a story
            if sig:
                batch_sigs.add(sig)
        out.append(tw)
    # newest first by numeric id (snowflake ids sort chronologically)
    out.sort(key=lambda t: int(t["id"]) if str(t.get("id")).isdigit() else 0,
             reverse=True)
    return out


# --- LLM calls ------------------------------------------------------------
# Some images/prompts hit monitor._looks_mangled at a high rate (seen 2/3
# retries still mangled on one chart) -- worth a couple extra attempts over
# letting garbled Hungarian text reach the ledger.
_MANGLED_MAX_ATTEMPTS = 3


def _parse_gemini_json(resp):
    try:
        return monitor._unescape_strings(json.loads(resp.text))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _gemini_usage(resp):
    u = resp.usage_metadata
    return (u.prompt_token_count or 0,
            (u.candidates_token_count or 0) + (u.thoughts_token_count or 0))


def analyze_text(gemini_client, feed, date, text, author):
    """Gemini text analysis. Returns (analysis_dict|None, in_tok, out_tok)."""
    user = (f"Posted by @{author} ({feed.display_name})\n"
            f"Date: {date}\nPost:\n{text}")
    # Hungarian is token-heavier than English; long-form essays need headroom or
    # the JSON truncates (600 was too low -> 1000). deep_thinking feeds also spend
    # part of max_output_tokens on dynamic thinking, so they get a much larger budget.
    deep = feed.deep_thinking
    in_tok = out_tok = 0
    parsed = None
    # A retry of the identical call usually comes back clean (see
    # monitor._looks_mangled), so on a mangled hit we just ask again rather
    # than storing garbled Hungarian text.
    for attempt in range(_MANGLED_MAX_ATTEMPTS):
        try:
            resp = gemini_client.models.generate_content(
                model=MODEL,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=feed.analysis_system,
                    response_mime_type="application/json",
                    response_json_schema=ANALYSIS_SCHEMA,
                    thinking_config=GEMINI_DEEP_THINKING if deep else GEMINI_THINKING,
                    max_output_tokens=6000 if deep else 1000,
                ),
            )
        except genai_errors.APIError as e:
            print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
            LLM_TALLY.fail()
            return None, in_tok, out_tok
        LLM_TALLY.ok()
        i, o = _gemini_usage(resp)
        in_tok, out_tok = in_tok + i, out_tok + o
        parsed = _parse_gemini_json(resp)
        if not (parsed and monitor._looks_mangled(parsed)):
            break
        if attempt == _MANGLED_MAX_ATTEMPTS - 1:
            print("  [give-up] analyze_text still mangled after "
                  f"{_MANGLED_MAX_ATTEMPTS} attempts; dropping (the post stays "
                  "unseen and is retried next run)", file=sys.stderr)
            return None, in_tok, out_tok
        print("  [retry] mangled Hungarian text in analyze_text, retrying",
              file=sys.stderr)
    return parsed, in_tok, out_tok


def analyze_chart(gemini_client, feed, media_urls, date, text, author):
    """Gemini vision pass over the post's chart image(s) (capped at
    MAX_VISION_IMAGES, same as the IncomeSharks pass). Returns
    (chart_dict|None, in_tok, out_tok); None if no image could be fetched."""
    parts = [p for p in (_image_block(u) for u in media_urls[:MAX_VISION_IMAGES])
             if p]
    if not parts:
        return None, 0, 0
    contents = parts + [f"Posted by @{author} ({feed.display_name})\nDate: {date}\n"
                        f"Tweet:\n{text}"]
    in_tok = out_tok = 0
    parsed = None
    # See analyze_text: a retry of the identical call usually comes back
    # clean, so on a mangled hit we just ask again.
    for attempt in range(_MANGLED_MAX_ATTEMPTS):
        try:
            resp = gemini_client.models.generate_content(
                model=MODEL,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=feed.vision_system,
                    response_mime_type="application/json",
                    response_json_schema=VISION_SCHEMA,
                    thinking_config=GEMINI_THINKING,
                    max_output_tokens=400,
                ),
            )
        except genai_errors.APIError as e:
            print(f"  [vision error] {type(e).__name__}: {e}", file=sys.stderr)
            LLM_TALLY.fail()
            return None, in_tok, out_tok
        LLM_TALLY.ok()
        i, o = _gemini_usage(resp)
        in_tok, out_tok = in_tok + i, out_tok + o
        parsed = _parse_gemini_json(resp)
        if not (parsed and monitor._looks_mangled(parsed)):
            break
        if attempt == _MANGLED_MAX_ATTEMPTS - 1:
            print("  [give-up] analyze_chart still mangled after "
                  f"{_MANGLED_MAX_ATTEMPTS} attempts; dropping the chart pass "
                  "(the post is still stored, without chart fields)",
                  file=sys.stderr)
            return None, in_tok, out_tok
        print("  [retry] mangled Hungarian text in analyze_chart, retrying",
              file=sys.stderr)
    return parsed, in_tok, out_tok


# --- main -----------------------------------------------------------------
def process(candidates, gemini_client, feed, allow_vision=True):
    """Analyze each candidate tweet; return (records, in_tok, out_tok)."""
    records, total_in, total_out = [], 0, 0
    for tw in candidates:
        n = _normalize_getxapi(tw)               # id, text, created_at(iso), media
        tid = n["id"]
        # User feed: author == feed.account. Search feed: the actual poster, which
        # varies per tweet -- thread it into the prompt, url, and record.
        author = n.get("author") or feed.account
        date = (n.get("created_at") or "")[:10]
        text = n.get("text", "")
        print(f"- {tid}  {('@' + author + ' ') if feed.is_search else ''}"
              f"{text[:70].replace(chr(10), ' ')}")

        analysis, in_t, out_t = analyze_text(gemini_client, feed, date, text, author)
        total_in += in_t
        total_out += out_t
        if not analysis:
            print("    text analysis failed; skipping", file=sys.stderr)
            continue

        media = n.get("media") or []           # photo URLs (charts) on this post
        rec = {
            "tweet_id": tid,
            "created_at": n.get("created_at"),
            "url": tw.get("url") or f"https://x.com/{author}/status/{tid}",
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
        if feed.is_search:                     # who posted it + dedup fingerprint
            rec["author"] = author             # (the ledger's cross-run seen_sigs)
            rec["text_sig"] = _content_sig(text)

        if media and allow_vision:
            chart, ci, co = analyze_chart(gemini_client, feed, media, date, text, author)
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


def _select_current_view_window(summaries):
    """Pick the records that feed the rolling-view synthesis: everything from
    the last CURRENT_VIEW_DAYS days, extended by count if that's too thin
    (slow feeds), capped at CURRENT_VIEW_MAX_POSTS (firehose feeds). `summaries`
    must be newest-first (as merged/written by run_feed); returns a
    newest-first list."""
    if not summaries:
        return []
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=CURRENT_VIEW_DAYS)).strftime("%Y-%m-%d")
    window = [r for r in summaries if (r.get("created_at") or "")[:10] >= cutoff]
    if len(window) < CURRENT_VIEW_MIN_POSTS:
        window = summaries[:CURRENT_VIEW_MIN_POSTS]
    return window[:CURRENT_VIEW_MAX_POSTS]


def _current_view_entry_text(r):
    """One post's already-distilled fields, formatted as a single prompt line."""
    line = (f"[{(r.get('created_at') or '')[:10]}] "
            f"{r.get('overall_sentiment') or 'neutral'}")
    themes = r.get("top_themes") or []
    if themes:
        line += " | " + ", ".join(themes)
    line += f" -- {r.get('market_view') or ''}"
    if r.get("chart_summary"):
        line += f" (chart: {r.get('chart_trend') or 'neutral'} -- {r['chart_summary']})"
    return line


def generate_current_view(gemini_client, feed, summaries):
    """Synthesize a rolling 'current stance' from the feed's recent history
    (see _select_current_view_window) -- fed the already-distilled per-post
    fields, not raw tweet text. Returns (view_dict|None, in_tok, out_tok);
    None if there is nothing to synthesize from or the call fails."""
    window = _select_current_view_window(summaries)
    if not window:
        return None, 0, 0
    entries = "\n".join(_current_view_entry_text(r) for r in reversed(window))
    user = f"Posts by @{feed.account} ({feed.display_name}):\n{entries}"
    in_tok = out_tok = 0
    parsed = None
    for attempt in range(_MANGLED_MAX_ATTEMPTS):
        try:
            resp = gemini_client.models.generate_content(
                model=MODEL,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=feed.current_view_system,
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
        i, o = _gemini_usage(resp)
        in_tok, out_tok = in_tok + i, out_tok + o
        parsed = _parse_gemini_json(resp)
        if not (parsed and monitor._looks_mangled(parsed)):
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
    dates = sorted((r.get("created_at") or "")[:10] for r in window
                   if r.get("created_at"))
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


# --- forecast-ledger pipeline (kendrick_sc) -------------------------------
def _norm_timeframe(tf):
    """Normalize a forecast horizon into a clustering token: a 4-digit 20xx year
    when present, else a short lowercased token, else 'unspecified'."""
    s = (tf or "").strip().lower()
    if not s:
        return "unspecified"
    m = re.search(r"\b(20[2-9]\d)\b", s)
    return m.group(1) if m else s[:16]


# Flow/size magnitudes (ETF inflows, AUM, market cap, TVL, "$X billion") are NOT
# price targets and must not seed -- or key -- a row in this PRICE-forecast
# ledger: "XRP $4-$8 billion in ETF inflows" is reach, not a price call.
_NONPRICE_RE = re.compile(
    r"inflow|outflow|aum|tvl|market\s*cap|mcap|\bvolume\b|liquidity|"
    r"trillion|billion|\bbn\b", re.I)
_PRICE_MULT = {"k": 1e3, "m": 1e6}     # thousand/million CAN be a per-coin price
_SIZE_SUFFIX = {"b", "bn", "t"}        # billion/trillion = cap/flow, never a price


def _target_value(t):
    """Parse a price-target string to a float magnitude for clustering, or None
    when it is not a plain per-coin price. The number is read from the START of
    the string (after an optional '$') so timeframes like 'Q4 2025' don't parse
    as a price; flow/size phrases and billion/trillion magnitudes ('$2.7T',
    '$5bn') and '50x' all -> None; a range -> its first number;
    '$40k'/'$40,000' -> 40000.0; sub-dollar '$.5' -> 0.5."""
    s = (t or "").strip().lower()
    if not s or _NONPRICE_RE.search(s):
        return None
    m = re.match(r"\$?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(bn|[kmbt])?", s)
    if not m:
        return None
    suf = m.group(2)
    if not suf:
        # '$150-200k': the magnitude suffix trails the SECOND bound but applies
        # to both -- without this a $150,000-$200,000 range would read as $150.
        m2 = re.match(r"\$?\s*[\d.,]+\s*(?:-|–|—|to)\s*\$?\s*[\d.,]+\s*"
                      r"(bn|[kmbt])\b", s)
        if m2:
            suf = m2.group(1)
    if suf in _SIZE_SUFFIX:          # billion/trillion is a size, not a price
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if suf:
        v *= _PRICE_MULT[suf]
    elif re.search(r"\dx\b", s):     # "50x" is a multiplier, not a price level
        return None
    return v


def _forecast_identity(asset, target, direction=None):
    """Cluster identity for an SC/Kendrick call: ASSET + its NORMALIZED price
    target, so '$40K' and '$40,000' collapse into one row while ETH $4,000 and
    ETH $40,000 stay distinct. Timeframe is deliberately NOT in the key -- the
    same call is relayed with inconsistent horizons ('2030' vs 'unspecified'),
    which is exactly what split near-identical forecasts across rows. Non-price
    targets fall back to a text token; a target-less call to asset+direction."""
    v = _target_value(target)
    if v is not None:
        return f"{asset}|p{v:g}"
    tok = (target or "").strip().lower().replace(" ", "")[:16]
    return f"{asset}|{tok}" if tok else f"{asset}|d{(direction or 'na')}"


def _row_identity(f):
    """Identity for an EXISTING ledger row: asset + its headline (first) target."""
    targets = f.get("targets") or []
    headline = next((t for t in targets if t and t.strip()), "")
    return _forecast_identity((f.get("asset") or "").upper().strip(),
                              headline, f.get("direction"))


def _authority(tw):
    """Sort key for picking the most authoritative source in a cluster: verified
    first, then followers, then longer (more detailed) text."""
    a = tw.get("author") or {}
    return (1 if (a.get("isBlueVerified") or a.get("isVerified")) else 0,
            a.get("followers") or 0, len(tw.get("text") or ""))


def _source_of(tw, n):
    a = tw.get("author") or {}
    return {
        "author": n.get("author") or a.get("userName"),
        "url": tw.get("url") or f"https://x.com/{n.get('author')}/status/{n['id']}",
        "followers": a.get("followers") or 0,
        "verified": bool(a.get("isBlueVerified") or a.get("isVerified")),
        "tweet_id": n["id"],
        "created_at": n.get("created_at"),
    }


def triage_forecasts(gemini_client, text):
    """Gemini gate: does this post relay an SC/Kendrick crypto forecast, and
    which? Returns (result_dict|None, in_tok, out_tok)."""
    try:
        resp = gemini_client.models.generate_content(
            model=TRIAGE_MODEL,
            contents=text,
            config=genai_types.GenerateContentConfig(
                system_instruction=TRIAGE_BODY,
                response_mime_type="application/json",
                response_json_schema=TRIAGE_SCHEMA,
                thinking_config=EXTRACT_THINKING,
                max_output_tokens=600,
            ),
        )
    except genai_errors.APIError as e:
        print(f"  [triage error] {type(e).__name__}: {e}", file=sys.stderr)
        LLM_TALLY.fail()
        return None, 0, 0
    LLM_TALLY.ok()
    in_tok, out_tok = _gemini_usage(resp)
    return _parse_gemini_json(resp), in_tok, out_tok


def _merge_sources(rec, sources, targets):
    """Fold new sources/targets into a forecast record. source_count is a running
    total (each tweet is triaged once ever -> no double count); the stored sources
    list is capped to the most authoritative SOURCES_CAP. best_source = the single
    highest-authority source seen."""
    have = {s["tweet_id"] for s in rec["sources"]}
    for s in sources:
        if s["tweet_id"] in have:
            continue
        have.add(s["tweet_id"])
        rec["sources"].append(s)
        rec["source_count"] = rec.get("source_count", 0) + 1
        ca = s.get("created_at") or ""
        if ca and ca > (rec.get("last_seen") or ""):
            rec["last_seen"] = ca
        if ca and ca < (rec.get("first_seen") or ca):
            rec["first_seen"] = ca
    for t in targets:
        if t and t not in rec["targets"]:
            rec["targets"].append(t)
    rec["sources"].sort(key=lambda s: (bool(s.get("verified")),
                                       s.get("followers") or 0), reverse=True)
    if rec["sources"]:
        b = rec["sources"][0]
        rec["best_source"] = {"author": b["author"], "url": b["url"]}
    rec["sources"] = rec["sources"][:SOURCES_CAP]


def _new_forecast(key, cluster, sources, rep, analysis, chart):
    """Build a fresh forecast record from a cluster + the Gemini read of its
    representative post (analysis/chart may be None if the LLM call failed)."""
    cas = [s["created_at"] for s in sources if s.get("created_at")]
    analysis = analysis or {}
    rec = {
        "key": key, "asset": cluster["asset"], "direction": cluster["direction"],
        "timeframe": cluster["timeframe"], "targets": [],
        "first_seen": min(cas) if cas else rep.get("created_at"),
        "last_seen": max(cas) if cas else rep.get("created_at"),
        "source_count": 0, "sources": [], "best_source": None,
        "rep_author": rep.get("author"),
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "media": rep.get("media") or [],
        "has_chart": False, "chart_trend": None, "chart_summary": None,
        "overall_sentiment": analysis.get("overall_sentiment"),
        "market_view": analysis.get("market_view"),
        "summary": analysis.get("summary"),
        "key_levels": analysis.get("key_levels") or [],
        "top_themes": analysis.get("top_themes") or [],
    }
    if chart:
        rec["has_chart"] = True
        rec["chart_trend"] = chart.get("chart_trend")
        rec["chart_summary"] = chart.get("chart_summary")
    _merge_sources(rec, sources, cluster["targets"])
    return rec


def _load_ledger_strict(path, default):
    """Ledger loader for the dedup-critical files: an ABSENT file is a fresh
    start, but a file that exists yet fails to read/parse must abort the feed
    (the per-feed isolation handler pages Telegram) -- silently proceeding with
    an empty ledger would re-analyze everything and the next atomic write would
    permanently erase all prior history."""
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:      # raises on unreadable/corrupt
        return json.load(f)


def _load_forecast_ledger(path):
    """Forecast ledger file is a dict {seen_ids, forecasts} (NOT a bare list like
    the post-feed ledgers). Missing -> empty skeleton; corrupt -> raises."""
    data = _load_ledger_strict(path, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("seen_ids", [])
    data.setdefault("forecasts", [])
    return data


def _merge_into(dst, src):
    """Fold forecast row `src` into `dst` (same identity). Callers fold the
    lower-reach row into the higher-reach one, so dst's narrative survives;
    src's sources/targets are absorbed, the seen window widened, and gaps filled.
    source_count stays a reach total: dst + src minus the overlap we can see."""
    before = dst.get("source_count", 0)
    have = {s["tweet_id"] for s in dst.get("sources") or []}
    overlap = sum(1 for s in (src.get("sources") or []) if s.get("tweet_id") in have)
    # Only absorb src targets that normalize to dst's own identity -- a legacy
    # row can carry foreign strings (a '$150K' bull target on a $60K bear row)
    # that must not leak into the merged row's displayed targets.
    dkey = dst.get("key") or _row_identity(dst)
    dasset = (dst.get("asset") or "").upper().strip()
    keep = [t for t in (src.get("targets") or [])
            if _forecast_identity(dasset, t, dst.get("direction")) == dkey]
    _merge_sources(dst, src.get("sources") or [], keep)
    dst["source_count"] = before + (src.get("source_count", 0) - overlap)
    if src.get("first_seen") and (not dst.get("first_seen")
                                  or src["first_seen"] < dst["first_seen"]):
        dst["first_seen"] = src["first_seen"]
    if src.get("last_seen") and (not dst.get("last_seen")
                                 or src["last_seen"] > dst["last_seen"]):
        dst["last_seen"] = src["last_seen"]
    if dst.get("timeframe") in (None, "", "unspecified") \
            and src.get("timeframe") not in (None, "", "unspecified"):
        dst["timeframe"] = src["timeframe"]
    for k in ("market_view", "summary", "overall_sentiment", "chart_trend",
              "chart_summary", "rep_author", "analyzed_at"):
        if not dst.get(k) and src.get(k):
            dst[k] = src[k]
    if not dst.get("has_chart") and src.get("has_chart"):
        dst["has_chart"] = True
        dst["media"] = dst.get("media") or src.get("media") or []
    for k in ("key_levels", "top_themes"):
        cur = dst.setdefault(k, list(dst.get(k) or []))
        for x in src.get(k) or []:
            if x not in cur:
                cur.append(x)


def _recluster_forecasts(rows):
    """Re-key existing rows onto the current identity scheme (asset + normalized
    price target) and MERGE any that now collide -- migrates a legacy
    {asset|timeframe} ledger and is idempotent on already-clean data. Returns a
    dict keyed by identity; the highest-reach row in each group is the base, so
    its narrative is kept."""
    out = {}
    for f in sorted(rows, key=lambda r: r.get("source_count") or 0, reverse=True):
        key = _row_identity(f)
        if key in out:
            _merge_into(out[key], f)
        else:
            g = dict(f)
            g["key"] = key
            # own copies of the lists _merge_into appends to, so folding a later
            # row never mutates the original input row's shared lists.
            for lk in ("sources", "targets", "key_levels", "top_themes", "media"):
                if g.get(lk) is not None:
                    g[lk] = list(g[lk])
            out[key] = g
    return out


def _write_forecast_ledger(feed, ledger, forecasts, seen):
    """Persist the forecast ledger newest-first with a capped seen-id high-water."""
    ledger["forecasts"] = sorted(forecasts.values(),
                                 key=lambda f: f.get("last_seen") or "",
                                 reverse=True)
    ledger["seen_ids"] = sorted(
        seen, key=lambda x: int(x) if x.isdigit() else 0)[-SEEN_IDS_CAP:]
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(feed.summaries_file, ledger)


def run_forecast_feed(feed, gemini_client, args):
    """kendrick_sc pipeline: Gemini triage -> cluster by (asset, price target) ->
    Gemini read of one representative per NEW forecast; echoes just add a source.
    One ledger row per forecast (vs one per post)."""
    print(f"\n=== {feed.display_name} (forecast ledger; search) ===")
    ledger = _load_forecast_ledger(feed.summaries_file)
    seen = set(map(str, ledger["seen_ids"]))
    # Re-cluster on load: migrates a legacy {asset|timeframe} ledger onto the
    # current asset+price-target identity and collapses any rows that now merge.
    forecasts = _recluster_forecasts(ledger["forecasts"])
    migrated = len(forecasts) != len(ledger["forecasts"])

    force_ids = set(args.force or [])
    raw, calls = fetch_search(feed, set() if force_ids else seen)
    print(f"Fetched {len(raw)} raw posts in {calls} GetXAPI call(s) "
          f"(${calls * GETXAPI_COST_PER_CALL:.4f})")

    if force_ids:
        by_id = {str(t.get("id")): t for t in raw}
        candidates = [by_id[i] for i in force_ids if i in by_id]
    else:
        candidates = select_candidates(raw, seen, feed)
        if args.limit is not None:
            candidates = candidates[:args.limit]
    if not candidates:
        print("No new posts to process.")
        if migrated and not args.dry_run:   # persist the on-load re-cluster
            _write_forecast_ledger(feed, ledger, forecasts, seen)
            print(f"Re-clustered ledger -> {len(forecasts)} forecasts")
        return

    # 1) Gemini triage every new post; a post is marked seen only once triage
    #    SUCCEEDED -- a transient API/parse failure leaves the id unseen so the
    #    next daily run re-triages it (the fetch window covers a 24h retry).
    triaged, tri_in, tri_out = [], 0, 0
    for tw in candidates:
        n = _normalize_getxapi(tw)
        res, i_t, o_t = triage_forecasts(gemini_client, n.get("text", ""))
        tri_in += i_t
        tri_out += o_t
        if res is None:
            continue
        seen.add(n["id"])
        if res.get("relevant") and res.get("forecasts"):
            triaged.append((tw, n, res["forecasts"]))
    print(f"Triaged {len(candidates)} (Gemini); {len(triaged)} carry a forecast")

    # 2) cluster echoes of one call by (asset + normalized price target); the
    #    timeframe is kept only for display, never for keying (it is too noisy).
    clusters = {}
    for tw, n, fcs in triaged:
        for fc in fcs:
            asset = (fc.get("asset") or "").upper().strip()
            # Gemini occasionally returns a contract address or a phrase instead
            # of a ticker; a 42-char hex "asset" must not become a public row.
            if (not asset or len(asset) > 12
                    or re.match(r"0X[0-9A-F]{6,}", asset)):
                continue
            t = (fc.get("target") or "").strip()
            key = _forecast_identity(asset, t, fc.get("direction"))
            tf = _norm_timeframe(fc.get("timeframe"))
            c = clusters.setdefault(key, {"asset": asset,
                                          "direction": fc.get("direction"),
                                          "timeframe": tf, "targets": [],
                                          "tweets": []})
            if t and t not in c["targets"]:
                c["targets"].append(t)
            if c["timeframe"] in ("", "unspecified") and tf not in ("", "unspecified"):
                c["timeframe"] = tf      # prefer a concrete year for display
            c["tweets"].append((tw, n))

    # 3) new forecast -> Gemini a representative; known forecast -> add sources
    ana_in = ana_out = 0
    new_keys, upd_keys = [], []
    for key, c in clusters.items():
        sources = [_source_of(tw, n) for tw, n in c["tweets"]]
        if key in forecasts:
            _merge_sources(forecasts[key], sources, c["targets"])
            upd_keys.append(key)
            continue
        # A non-price-keyed cluster ('50x', a range rephrase) whose raw target
        # already sits on an existing row of the same asset is an ECHO of that
        # call, not a new forecast -- credit its reach there instead of minting
        # a duplicate row and paying a duplicate Gemini read.
        if c["targets"] and _target_value(c["targets"][0]) is None:
            norm = {t.strip().lower().replace(" ", "") for t in c["targets"]}
            alt = next((k for k, f in forecasts.items()
                        if f.get("asset") == c["asset"] and any(
                            (t or "").strip().lower().replace(" ", "") in norm
                            for t in f.get("targets") or [])), None)
            if alt:
                _merge_sources(forecasts[alt], sources, [])
                upd_keys.append(alt)
                continue
        rep_tw, rep = max(c["tweets"], key=lambda p: _authority(p[0]))
        date = (rep.get("created_at") or "")[:10]
        analysis, a_i, a_o = analyze_text(gemini_client, feed, date,
                                          rep.get("text", ""),
                                          rep.get("author") or "")
        ana_in += a_i
        ana_out += a_o
        if analysis is None:
            # Transient Gemini failure: creating the row now would freeze it
            # narrative-less forever (echoes never re-read). Un-see the
            # cluster's posts so the next run re-triages and re-reads it.
            for _, nn in c["tweets"]:
                seen.discard(nn["id"])
            print(f"  SKIP {key}: analysis failed, retrying next run")
            continue
        chart, media = None, rep.get("media") or []
        if media and not args.no_vision:
            chart, ci, co = analyze_chart(gemini_client, feed, media, date,
                                          rep.get("text", ""),
                                          rep.get("author") or "")
            ana_in += ci
            ana_out += co
        forecasts[key] = _new_forecast(key, c, sources, rep, analysis, chart)
        new_keys.append(key)
        print(f"  NEW {key}: {forecasts[key]['source_count']} src "
              f"-> {forecasts[key].get('overall_sentiment')}")
    for key in upd_keys:
        print(f"  +src {key}: now {forecasts[key]['source_count']}")

    hcost = tri_in / 1e6 * EXTRACT_INPUT_PER_1M + tri_out / 1e6 * EXTRACT_OUTPUT_PER_1M
    scost = ana_in / 1e6 * GEMINI_INPUT_PER_1M + ana_out / 1e6 * GEMINI_OUTPUT_PER_1M
    print(f"Triage Gemini in={tri_in} out={tri_out} (${hcost:.4f}); "
          f"Analysis Gemini in={ana_in} out={ana_out} (${scost:.4f}); "
          f"{len(new_keys)} new, {len(upd_keys)} updated")

    if args.dry_run:
        ordered = sorted(forecasts.values(),
                         key=lambda f: f.get("last_seen") or "", reverse=True)
        print("[dry-run] not writing; ledger preview (newest first):")
        print(json.dumps(ordered, indent=2, ensure_ascii=False)[:5000])
        return
    _write_forecast_ledger(feed, ledger, forecasts, seen)
    print(f"Wrote {len(forecasts)} forecasts -> {feed.summaries_file}")


def run_feed(feed, gemini_client, args):
    """Process one feed end-to-end: fetch -> filter -> analyze -> write ledger."""
    if feed.forecast_ledger:                 # kendrick_sc: forecast rows, not posts
        return run_forecast_feed(feed, gemini_client, args)
    where = f"search: {feed.query}" if feed.is_search else f"@{feed.account}"
    print(f"\n=== {feed.display_name} ({where}) ===")
    summaries = _load_ledger_strict(feed.summaries_file, [])
    if not isinstance(summaries, list):
        summaries = []
    seen = {str(r.get("tweet_id")) for r in summaries}
    # Search feeds also dedup by content signature (verbatim reposts of a story
    # under distinct ids); the ledger persists text_sig for cross-run collapse.
    seen_sigs = ({r.get("text_sig") for r in summaries if r.get("text_sig")}
                 if feed.is_search else frozenset())

    # --force bypasses dedup for the named ids. Scan the full window (empty
    # stop-set) so a forced id OLDER than the high-water mark is still reached;
    # the normal run stops early once it hits an already-seen post.
    force_ids = set(args.force or [])
    fetch = fetch_search if feed.is_search else fetch_posts
    raw, calls = fetch(feed, set() if force_ids else seen)
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
        candidates = select_candidates(raw, seen, feed, seen_sigs)
        if args.limit is not None:
            candidates = candidates[:args.limit]

    if not candidates:
        print("No new posts to process.")
        return

    print(f"Processing {len(candidates)} post(s)"
          + (" [FORCE]" if force_ids else "") + ":")
    records, in_tok, out_tok = process(candidates, gemini_client, feed,
                                       allow_vision=not args.no_vision)

    cost = (in_tok / 1_000_000 * GEMINI_INPUT_PER_1M
            + out_tok / 1_000_000 * GEMINI_OUTPUT_PER_1M)
    print(f"Analyzed {len(records)} post(s); "
          f"Gemini tokens in={in_tok} out={out_tok} (${cost:.4f})")

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

    if feed.current_view_file:
        view, cv_in, cv_out = generate_current_view(gemini_client, feed, merged)
        if view:
            cv_cost = (cv_in / 1_000_000 * GEMINI_INPUT_PER_1M
                       + cv_out / 1_000_000 * GEMINI_OUTPUT_PER_1M)
            write_json_atomic(feed.current_view_file, view)
            # ...and append it to the shared, never-overwritten history log.
            sentiment_history.append_view(feed.key, view)
            print(f"Current view: {view['overall_sentiment']} "
                  f"(based on {view['based_on']['count']} posts); "
                  f"tokens in={cv_in} out={cv_out} (${cv_cost:.4f}) "
                  f"-> {feed.current_view_file}")
        else:
            # New posts landed but the synthesis failed, so the on-disk view (and
            # the Consensus row built from it) silently keeps describing an older
            # window. Say so loudly -- this used to be a no-op.
            print(f"  [current-view FAILED] {feed.key}: kept the previous "
                  f"{feed.current_view_file} -- it is now stale vs the ledger",
                  file=sys.stderr)


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
                    help="skip the Gemini chart-image vision pass")
    args = ap.parse_args()

    if args.force and not args.feed:
        ap.error("--force requires --feed (one feed at a time)")

    for p in TELEGRAM_ENVS:                       # loads GOOGLE + GETXAPI + alerts
        load_env(p)
    if not os.environ.get("GOOGLE_API_KEY") or not os.environ.get("GETXAPI_KEY"):
        print("Missing GOOGLE_API_KEY or GETXAPI_KEY in .env", file=sys.stderr)
        sys.exit(1)

    feeds = [FEEDS[args.feed]] if args.feed else list(FEEDS.values())
    gemini_client = genai.Client()
    failed = []
    for feed in feeds:
        try:
            run_feed(feed, gemini_client, args)
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
    # A wholesale Gemini failure never raises (each call site swallows its own
    # APIError), so without this the run exits 0 having written nothing and the
    # dashboard just keeps serving yesterday's cards. --dry-run is exempt: it is
    # a manual probe, not the cron tick the dashboard depends on.
    if not args.dry_run:
        alert_gemini_outage(LLM_TALLY, "twitter_digest")

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
