#!/usr/bin/env python3
"""
Multi-account trade-signal monitor for X portfolio bots, with LLM interpretation.

Pipeline per tweet:
  1. Skip @-replies (almost never the account's own new trade) — saves API cost.
  2. Every remaining tweet is sent to the Anthropic API (claude-haiku-4-5-20251001)
     which extracts ticker / action / size / entry price / trade date / portfolio
     / confidence as schema-constrained JSON (always valid).
  3. Signals with action == "none" or a null ticker are discarded.
  4. Kept signals are tagged with their source account, written to
     ~/pilot_trader/trades.json (deduped by tweet id), then reconciled into
     ~/pilot_trader/positions.json.

Tweet backend (--source): defaults to GetXAPI via its tweets_and_replies
endpoint (cheap, untruncated text, and INCLUDES @-replies so reply-disclosed
sells are not missed). --source official uses the X API v2 instead.

Run modes:
  python monitor.py                 # GetXAPI (default), fetch + interpret
  python monitor.py --source official
  python monitor.py --backfill      # re-interpret local snapshots (no fetch)
  python monitor.py --dry-run       # fetch + analyze, write nothing

Requires ANTHROPIC_API_KEY + the source's key (GETXAPI_KEY or X_BEARER_TOKEN),
loaded from ~/pilot_trader/.env. Run with the project venv:
/home/fbazsa/pilot_trader/.venv/bin/python monitor.py
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import traceback
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from reconcile import reconcile, write_json_atomic

# --- config ---------------------------------------------------------------
# X bearer token is loaded from .env (X_BEARER_TOKEN) — never hardcoded.
# Account identity/classification config is shared across the pipeline and
# lives in accounts.py (single source of truth). @aifinancelabs is where the
# DeepSeek portfolio experiment is published (no standalone DeepSeek handle).
from accounts import ACCOUNTS, SOURCE_TYPE, POSTS_ONLY_ACCOUNTS
# Slow accounts: fetch only if the last fetch was more than N hours ago (instead
# of an exact-hour cron match, which fails entirely if that one cron run fails).
# Absent => polled on every run (4h cron). The 3 Autopilot AI portfolios
# (grok/claude/deepseek — they rebalance ~monthly, so 4h polling is wasteful) are
# ~twice a day. ralliesarena is an AI portfolio too but posts daily trade
# updates, so it is left ABSENT here (polled every run) like the influencers.
# ⚠️ Use 11h, NOT 12h: a real fetch records last_fetch a few seconds AFTER the
# cron slot (GetXAPI latency), so a 16:00->04:00 gap measures ~11h59m58s. With a
# 12h threshold that is `< 12` => the 04:00 run SKIPS and the phase drifts. 11h
# leaves a full ~1h cushion over the real ~12h gap, so the 2x/day cadence is
# stable. The phase (WHICH two slots) is set by each account's seed last_fetch in
# .monitor_state.json; the AI portfolios are seeded to the 04:00/16:00 UTC slots
# so fresh data is guaranteed by the 04:00 run (finishes ~05:00 UTC = 07:00
# Budapest, before the 08:00 Budapest target).
POLL_MIN_INTERVAL_H = {"grkportfolio": 11, "theaiportfolios": 11,
                       "aifinancelabs": 11}
HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")
COST_LOG_FILE = os.path.join(DATA_DIR, "cost_log.json")
TRADES_FILE = os.path.join(HOME, "trades.json")
POSITIONS_FILE = os.path.join(HOME, "positions.json")
STATE_FILE = os.path.join(HOME, ".monitor_state.json")
ENV_FILE = os.path.join(HOME, ".env")
# Telegram alert credentials live in paper_trader's .env (shared bot). We also
# look at the home .env. All loaded read-only via setdefault, so pilot_trader's
# own .env still wins.
PAPER_ENV = "/home/fbazsa/paper_trader/.env"
SHARED_ENV = "/home/fbazsa/.env.shared"
HOME_ENV = "/home/fbazsa/.env"
TELEGRAM_ENVS = (ENV_FILE, PAPER_ENV, SHARED_ENV, HOME_ENV)
MONITOR_LOG = os.path.join(HOME, "monitor.log")
STALE_ALERT_HOURS = 8        # alert if the last successful run is older than this
RAW_FILES = {  # used by --backfill; accounts without a snapshot are skipped
    "grkportfolio": os.path.join(HOME, "tweets_raw.json"),
    "theaiportfolios": os.path.join(HOME, "tweets_theaiportfolios.json"),
    "aifinancelabs": os.path.join(HOME, "tweets_aifinancelabs.json"),
    "IncomeSharks": os.path.join(HOME, "tweets_incomesharks.json"),
    "CelalKucuker": os.path.join(HOME, "tweets_CelalKucuker.json"),
    "traderstewie": os.path.join(HOME, "tweets_traderstewie.json"),
    "ralliesarena": os.path.join(HOME, "tweets_ralliesarena.json"),
}
MAX_FETCH = 100
API_BASE = "https://api.twitter.com/2"
GETXAPI_BASE = "https://api.getxapi.com"
# tweets_and_replies (NOT the posts-only "tweets" endpoint) so sell signals
# disclosed in @-replies are included. POSTS_ONLY_ACCOUNTS use the "tweets"
# (Posts tab) endpoint instead, which excludes @-replies entirely.
GETXAPI_TWEETS_PATH = "/twitter/user/tweets_and_replies"
GETXAPI_POSTS_PATH = "/twitter/user/tweets"
GETXAPI_COST_PER_CALL = 0.001        # $/call (~20 tweets/page)

# Anthropic
MODEL = "claude-haiku-4-5-20251001"
TWITTER_COST_PER_TWEET = 0.005       # X API read
HAIKU_INPUT_PER_1M = 1.00            # $ / 1M input tokens
HAIKU_OUTPUT_PER_1M = 5.00           # $ / 1M output tokens

# Vision model for chart-image analysis (Haiku 4.5 does NOT accept images).
# Only used for influencer tweets that carry a chart photo. Pricier per token,
# so it runs as an ADD-ON to the cheap Haiku text pass, never as a replacement.
# (claude-sonnet-4-20250514 is retired on this account; sonnet-4-6 is the
# current vision-capable Sonnet at the same $3/$15 per-MTok pricing.)
VISION_MODEL = "claude-sonnet-4-6"
SONNET_INPUT_PER_1M = 3.00           # $ / 1M input tokens
SONNET_OUTPUT_PER_1M = 15.00         # $ / 1M output tokens
MAX_VISION_IMAGES = 2                # cap images/tweet to bound vision cost

# Batch API (--batch live runs + --backfill-batch). 50% cheaper than real-time,
# but asynchronous: a live run SUBMITS one job and blocks-polls it to completion.
BATCH_POLL_INTERVAL_S = 20           # seconds between batch status polls
BATCH_MAX_WAIT_S = 55 * 60           # live runs defer to next cron if it exceeds
                                     # this (cron spacing is 4h, user accepts <=60m)

# --- LLM extraction --------------------------------------------------------
# No pre-filter: every tweet is sent to Claude, which decides what is a signal.
EXTRACTION_SYSTEM = (
    "You extract trade signals from tweets posted by trading accounts. Given one "
    "tweet (and the handle that posted it), decide whether it reports an "
    "actionable trade (or a currently-held position) and extract the details.\n"
    "There are two kinds of accounts:\n"
    "(A) AUTOMATED PORTFOLIO BOTS — they post their OWN executed trades / "
    "holdings. Account-to-portfolio map: @grkportfolio = the Grok portfolio; "
    "@theaiportfolios = the Claude portfolio; @aifinancelabs = an umbrella lab "
    "account that posts updates for several model portfolios (Grok, Claude, "
    "DeepSeek, ChatGPT) — for its tweets, infer the portfolio from the text "
    "(e.g. 'DeepSeek's portfolio...' => deepseek).\n"
    "(B) HUMAN TRADER / INFLUENCER — @IncomeSharks, @CelalKucuker and "
    "@traderstewie. They post FREQUENT trade ideas/calls on stocks and/or "
    "crypto, and mix pure analysis/opinion with actionable calls. They often "
    "state entry prices, stop losses, and price targets. @CelalKucuker is "
    "CRYPTO-HEAVY: most of its calls are on cryptocurrencies (e.g. BTC, XRP, "
    "XLM, SUI, SOL, EIGEN) — set asset_type = \"crypto\" for these and use the "
    "common symbol. @traderstewie is a US STOCK/ETF swing trader (e.g. SOXL, "
    "AEHR, LWLG, INTC, RKLB) — set asset_type = \"stock\" for its calls. For "
    "these influencers always set portfolio = null.\n"
    "OWN VIEW ONLY: extract a signal only when the ACCOUNT HOLDER is stating "
    "THEIR OWN trade or stance. If the tweet is reacting to, quoting, "
    "questioning, or disagreeing with SOMEONE ELSE'S call or price prediction "
    "(e.g. 'I hope we don't see $1400', 'they say it goes to X', replying to "
    "another user's forecast), that is NOT the account's own signal: set "
    "action = \"none\". A price someone else predicted is never a target or "
    "stop for the account.\n"
    "Rules:\n"
    "- action: \"buy\" if the account bought/initiated/added OR (for an "
    "influencer) is calling a long entry / saying it is buying or holding long; "
    "\"sell\" if it sold/trimmed/exited/dumped OR is calling a short/exit; "
    "\"position\" if it discloses a current holding or weight without a fresh "
    "transaction; \"none\" for pure market commentary, opinion, analysis, "
    "questions, or replies about other people's trades that are NOT an "
    "actionable own trade/call.\n"
    "- sell_kind: only when action is \"sell\". \"full\" if the WHOLE position "
    "was exited/closed/dumped/sold out. \"partial\" if it was only reduced "
    "(\"trimmed\", \"reduced\", \"scaled out\", \"took partial/some profits\", "
    "\"sold half\", \"lightened\"). null when action is not \"sell\" or the "
    "extent is unclear (treat unclear as full only if the wording clearly means "
    "a complete exit, else partial).\n"
    "- ticker: the ticker symbol. For stocks resolve company names to US tickers "
    "(Broadcom->AVGO, ServiceNow->NOW, Micron->MU, etc.). For crypto use the "
    "common symbol (Bitcoin->BTC, Ethereum->ETH, Solana->SOL). null if no "
    "specific asset is the subject.\n"
    "- asset_type: \"stock\" for equities/ETFs, \"crypto\" for cryptocurrencies, "
    "\"unknown\" if unclear.\n"
    "- size_pct: position size as a percent of the portfolio/book if stated "
    "(e.g. 8.46), as a number BETWEEN 0 AND 100. It must be an explicit "
    "percentage (a \"%\" sign or wording like \"X% of the book/portfolio\"). "
    "Do NOT use gain/return percentages. NEVER put a dollar allocation here: "
    "\"took our $50,000 and bought $PGY\" or \"deployed $5k into NVDA\" states "
    "DOLLARS, not a percent, so size_pct = null. null if absent.\n"
    "- entry_price: the PER-SHARE buy/entry price in dollars as a number, only "
    "if stated as an entry/purchase price per share (not a current price, and "
    "NOT a total dollar amount deployed — \"bought $50,000 of PGY\" is a total "
    "spend, not an entry price, so entry_price = null). null if absent.\n"
    "- stop_loss: the stop-loss price in dollars as a number if stated. null if absent.\n"
    "- target: the price target / take-profit in dollars as a number if stated. "
    "null if absent.\n"
    "- For crypto price levels, ALWAYS output full dollar values. If the tweet "
    "uses shorthand like \"54-50\" or \"144K\", convert to full numbers (54000, "
    "50000, 144000). NEVER output a stop_loss or target below 1000 for BTC, ETH, "
    "or other major crypto.\n"
    "- trade_date: the actual date the trade was made, as ISO YYYY-MM-DD, if the "
    "tweet states or implies one (e.g. 'I bought it April 7' => 2026-04-07; "
    "'bought in early April' => 2026-04-05; 'since May 4th' => 2026-05-04). This "
    "is the TRANSACTION date and is often EARLIER than the tweet date — resolve "
    "relative phrases against the tweet date given below and use its year. null "
    "if no trade date is stated or implied.\n"
    "- confidence: how confident you are this is a real actionable trade signal "
    "(\"high\", \"medium\", \"low\", or \"none\").\n"
    "- portfolio: for a portfolio bot, which model's portfolio the trade belongs "
    "to — \"grok\", \"claude\", \"deepseek\", \"chatgpt\", or \"gemini\". Umbrella "
    "feeds name the model explicitly (e.g. 'CLAUDE JUST BOUGHT', 'Gemini bought') "
    "— attribute per tweet. null if unclear or for an influencer.\n"
    "- holding_thesis: for a buy or position, ONE short sentence stating WHY "
    "the account likes/holds the asset (the bull case / conviction reason), if "
    "stated or clearly implied. null if none is given.\n"
    "- reasoning: one short sentence explaining the call.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": ["string", "null"]},
        "asset_type": {"type": "string", "enum": ["stock", "crypto", "unknown"]},
        "action": {"type": "string", "enum": ["buy", "sell", "position", "none"]},
        "sell_kind": {"anyOf": [
            {"type": "string", "enum": ["full", "partial"]},
            {"type": "null"},
        ]},
        "size_pct": {"type": ["number", "null"]},
        "entry_price": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "target": {"type": ["number", "null"]},
        "trade_date": {"type": ["string", "null"]},
        "holding_thesis": {"type": ["string", "null"]},
        "confidence": {"type": "string",
                       "enum": ["high", "medium", "low", "none"]},
        "portfolio": {"anyOf": [
            {"type": "string",
             "enum": ["grok", "claude", "deepseek", "chatgpt", "gemini"]},
            {"type": "null"},
        ]},
        "reasoning": {"type": "string"},
    },
    "required": ["ticker", "asset_type", "action", "sell_kind", "size_pct",
                 "entry_price", "stop_loss", "target", "trade_date",
                 "holding_thesis", "confidence", "portfolio", "reasoning"],
    "additionalProperties": False,
}

# --- Chart-image (vision) extraction --------------------------------------
# Influencer tweets frequently attach an annotated chart whose levels are NOT
# in the tweet text (TP/stop lines drawn on the chart). This schema captures
# what is readable FROM THE IMAGE so it can backfill the text extraction.
VISION_SYSTEM = (
    "You read an annotated stock/crypto price chart image attached to a "
    "trader's tweet. Using the chart AND the tweet text, extract only what is "
    "actually visible/marked on the chart. Do not invent levels.\n"
    "- ticker: the symbol shown on the chart (e.g. on the axis/title), null if "
    "not visible.\n"
    "- tp1: the first/nearest take-profit or upside target price marked on the "
    "chart, as a number. null if none is drawn.\n"
    "- tp2: a second, higher take-profit/target price if a second one is "
    "marked, as a number. null if absent.\n"
    "- stop_loss: the stop-loss / invalidation price marked on the chart, as a "
    "number. null if none is drawn.\n"
    "- trend: the overall setup direction the chart implies: \"bullish\", "
    "\"bearish\", or \"neutral\".\n"
    "- chart_notes: ONE short sentence describing the technical setup shown "
    "(pattern, key level, breakout/breakdown).\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": ["string", "null"]},
        "tp1": {"type": ["number", "null"]},
        "tp2": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "trend": {"type": "string",
                  "enum": ["bullish", "bearish", "neutral"]},
        "chart_notes": {"type": ["string", "null"]},
    },
    "required": ["ticker", "tp1", "tp2", "stop_loss", "trend", "chart_notes"],
    "additionalProperties": False,
}


# Shared request shape for both the real-time and Batch API paths.
# NOTE: Haiku 4.5's minimum cacheable prefix is 4096 tokens; this system prompt
# is smaller, so the cache_control marker is harmless future-proofing only.
def _system_blocks():
    return [{"type": "text", "text": EXTRACTION_SYSTEM,
             "cache_control": {"type": "ephemeral"}}]


def _output_config():
    return {"format": {"type": "json_schema", "schema": SIGNAL_SCHEMA}}


def _user_message(account, text, tweet_date):
    return {"role": "user",
            "content": f"Posted by @{account}\nTweet date: {tweet_date}\n"
                       f"Tweet:\n{text}"}


def _image_block(url):
    """Download a Twitter media photo and return an Anthropic image content
    block (base64), or None on any failure. `name=small` keeps the download
    (and the vision token cost) modest while staying legible for chart levels."""
    try:
        req = urllib.request.Request(url + "?name=small",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"  [media error] {url}: {e}", file=sys.stderr)
        return None
    media_type = "image/png" if url.lower().endswith(".png") else "image/jpeg"
    return {"type": "image",
            "source": {"type": "base64", "media_type": media_type,
                       "data": base64.standard_b64encode(raw).decode("ascii")}}


def _parse_json_text(content_blocks):
    text_block = next((b.text for b in content_blocks if b.type == "text"), None)
    if not text_block:
        return None
    try:
        return json.loads(text_block)
    except json.JSONDecodeError:
        return None


class Interpreter:
    """Wraps the Anthropic client and tracks token usage for cost reporting."""

    def __init__(self):
        self.client = anthropic.Anthropic()   # reads ANTHROPIC_API_KEY from env
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        # Vision (Sonnet) usage tracked separately for cost reporting.
        self.vision_calls = 0
        self.vision_input_tokens = 0
        self.vision_output_tokens = 0
        # Batch API (Haiku) usage — billed at 50%, tracked apart from real-time.
        self.batch_input_tokens = 0
        self.batch_output_tokens = 0

    def extract(self, text, account, tweet_date):
        """Return the parsed signal dict, or None on API/parse error."""
        try:
            resp = self.client.messages.create(
                model=MODEL,
                max_tokens=300,
                system=_system_blocks(),
                output_config=_output_config(),
                messages=[_user_message(account, text, tweet_date)],
            )
        except anthropic.APIError as e:
            print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
            return None

        self.calls += 1
        self.input_tokens += resp.usage.input_tokens + \
            (resp.usage.cache_read_input_tokens or 0) + \
            (resp.usage.cache_creation_input_tokens or 0)
        self.output_tokens += resp.usage.output_tokens
        return _parse_json_text(resp.content)

    def extract_chart(self, media_urls, text, account, tweet_date):
        """Run a Sonnet vision pass over the tweet's chart image(s). Returns the
        parsed chart dict, or None if no image could be fetched / on error."""
        blocks = []
        for url in media_urls[:MAX_VISION_IMAGES]:
            b = _image_block(url)
            if b:
                blocks.append(b)
        if not blocks:
            return None
        content = blocks + [{
            "type": "text",
            "text": f"Posted by @{account}\nTweet date: {tweet_date}\n"
                    f"Tweet:\n{text}"}]
        try:
            resp = self.client.messages.create(
                model=VISION_MODEL,
                max_tokens=300,
                system=[{"type": "text", "text": VISION_SYSTEM}],
                output_config={"format": {"type": "json_schema",
                                          "schema": CHART_SCHEMA}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIError as e:
            print(f"  [vision error] {type(e).__name__}: {e}", file=sys.stderr)
            return None
        self.vision_calls += 1
        self.vision_input_tokens += resp.usage.input_tokens + \
            (resp.usage.cache_read_input_tokens or 0) + \
            (resp.usage.cache_creation_input_tokens or 0)
        self.vision_output_tokens += resp.usage.output_tokens
        return _parse_json_text(resp.content)

    def cost(self):
        return (self.input_tokens / 1_000_000 * HAIKU_INPUT_PER_1M
                + self.output_tokens / 1_000_000 * HAIKU_OUTPUT_PER_1M)

    def vision_cost(self):
        return (self.vision_input_tokens / 1_000_000 * SONNET_INPUT_PER_1M
                + self.vision_output_tokens / 1_000_000 * SONNET_OUTPUT_PER_1M)

    def batch_cost(self):
        return (self.batch_input_tokens / 1_000_000 * HAIKU_INPUT_PER_1M
                + self.batch_output_tokens / 1_000_000 * HAIKU_OUTPUT_PER_1M) * 0.5


# Exit-phrasing detector for the reply gate. Word-boundary regex, not bare
# substrings: "cut" as a substring matched "exeCUTe"/"hairCUT". Generous on
# purpose — this gate protects the SELL side of the IBKR mirror, and a false
# positive only costs one Haiku call.
SELL_PATTERN = re.compile(
    r"\b(sold|selling|sell|dumped|dumping|exited|exiting|exit|"
    r"trimmed|trimming|closed|closing|liquidated|liquidating|"
    r"out of|cut|stopped out|scaled out|scaling out|reduced|reducing|"
    r"took (?:some )?profits?|taking (?:some )?profits?|take profits?)\b",
    re.IGNORECASE)

# Retweet detector for the cross-account dedup gate. A retweet is a verbatim
# copy of another account's tweet, so when the ORIGINAL author is itself a
# monitored account we already ingest that tweet directly — re-interpreting the
# RT only creates a duplicate signal (e.g. @aifinancelabs and @theaiportfolios
# both RT-ing @grkportfolio's FTAI post = 3 identical Grok positions). The drop
# is SCOPED to monitored authors: RTs of NON-monitored accounts are KEPT, since
# they can be the sole source of a signal (e.g. an external account breaking a
# Grok buy @grkportfolio never posted itself). .match() anchors at string start.
RETWEET_PATTERN = re.compile(r"RT @(\w+):", re.IGNORECASE)
_MONITORED_LC = {a.lower() for a in ACCOUNTS}


def is_reply(text):
    return text.lstrip().startswith("@")


def tweet_is_reply(tw):
    """True if the tweet is a reply. Prefers the source's explicit is_reply
    flag (GetXAPI sets it via _normalize_getxapi); falls back to the text
    heuristic for the official API / old snapshots. The heuristic alone
    misfired both ways: a fresh tweet that merely BEGINS with a mention
    ("@NVIDIA is a buy") was dropped as a reply, and replies whose text
    doesn't start with "@" bypassed the gate."""
    ir = tw.get("is_reply")
    if isinstance(ir, bool):
        return ir
    return is_reply(tw.get("text", ""))


def foreign_author(account, tw):
    """True if the tweet was authored by someone OTHER than the monitored
    account. tweets_and_replies returns the whole conversation thread, so a
    follower's reply can ride along; without this it would be extracted as the
    account's own signal. Author is compared case-insensitively. When the field
    is absent (old snapshots / --source official) we keep the tweet (return
    False) so existing behaviour is preserved."""
    author = tw.get("author")
    return bool(author) and author.lower() != account.lower()


def reply_has_sell_verb(text):
    """A reply worth keeping: it mentions a sell so we don't miss exits."""
    return bool(SELL_PATTERN.search(text or ""))


def is_duplicate_retweet(text):
    """True if `text` is a retweet of a MONITORED account — a cross-account
    duplicate of a tweet we ingest from the original author directly, so it is
    dropped pre-LLM. Retweets of NON-monitored accounts return False (kept):
    they can be the sole source of a signal. See RETWEET_PATTERN."""
    m = RETWEET_PATTERN.match((text or "").lstrip())
    return bool(m) and m.group(1).lower() in _MONITORED_LC


def slow_fetch_skip(account, state, now):
    """Pacing gate for POLL_MIN_INTERVAL_H accounts. Returns (skip, age_h).

    skip=True when the account's last fetch was less than its min interval ago.
    A never-fetched account (no last_fetch) is NOT skipped — so a missed cron
    run can't strand it, and the first run seeds last_fetch. Unknown timestamps
    fail open (do not skip)."""
    min_h = POLL_MIN_INTERVAL_H.get(account)
    if min_h is None:
        return False, None
    last = (state.get(account) or {}).get("last_fetch")
    if not last:
        return False, None
    try:
        age_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600
    except (ValueError, TypeError):
        return False, None
    return (age_h < min_h), age_h


def merge_chart(parsed, chart):
    """Fold a vision/chart extraction into the text-extracted signal dict.
    Image data only FILLS GAPS — text-stated values win. Chart-only fields
    (tp1/tp2/trend/chart_notes) are added. Returns True if anything improved."""
    if not parsed or not chart:
        return False
    improved = False
    # ticker / stop_loss: backfill only when text gave nothing.
    if not parsed.get("ticker") and chart.get("ticker"):
        parsed["ticker"] = chart["ticker"]
        improved = True
    if parsed.get("stop_loss") is None and chart.get("stop_loss") is not None:
        parsed["stop_loss"] = chart["stop_loss"]
        improved = True
    # target: text `target`, else the chart's first take-profit.
    if parsed.get("target") is None and chart.get("tp1") is not None:
        parsed["target"] = chart["tp1"]
        improved = True
    # Chart-only enrichments (always recorded when present).
    for k in ("tp1", "tp2", "trend", "chart_notes"):
        if chart.get(k) is not None:
            parsed[k] = chart[k]
            if k in ("tp1", "tp2", "chart_notes"):
                improved = True
    return improved


def promote_with_chart(parsed, chart):
    """When the text pass found no actionable signal but the tweet carries a
    chart, let a clearly-directional chart promote it to a trade. Requires a
    ticker (from text or backfilled from the chart by merge_chart) and a
    non-neutral trend. Returns True if it promoted."""
    if not parsed or not chart:
        return False
    if parsed.get("action") != "none" or not parsed.get("ticker"):
        return False
    trend = chart.get("trend")
    if trend == "bullish":
        parsed["action"] = "buy"
    elif trend == "bearish":
        parsed["action"] = "sell"
        parsed["sell_kind"] = "full"
    else:
        return False
    # Chart-only calls are inherently softer; medium keeps them out of the
    # low/none confidence gate so they still register as positions.
    if parsed.get("confidence") in (None, "none", "low"):
        parsed["confidence"] = "medium"
    note = chart.get("chart_notes")
    parsed["reasoning"] = ("Chart-only signal: " + note) if note else \
        "Chart-only signal (promoted from annotated chart)."
    return True


def _mentions_asset(parsed, text):
    """Heuristic gate for the influencer chart pass on non-actionable text:
    run vision only when an asset is plausibly in play (text named a ticker,
    or the tweet carries a cashtag) to bound vision spend."""
    if parsed and parsed.get("ticker"):
        return True
    return "$" in (text or "")


def _sane_size_pct(v):
    """Deterministic backstop against dollar-amounts-as-percent leakage
    (e.g. "took our $50,000 and bought $PGY" -> size_pct=50000). A position
    weight is only meaningful in (0, 100]; anything else is a misextraction,
    so null it. Independent of the LLM/schema so it always holds."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    return v if 0 < v <= 100 else None


def _crypto_ref(*vals):
    """Largest positive numeric value among the magnitude-reference candidates
    (entry_price, then the take-profit levels tp1/tp2), or None if none qualify.
    Recap tweets usually carry no entry_price but DO carry chart tp1/tp2 in full
    dollars, so those backstop the reference when entry is absent."""
    nums = [v for v in vals
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0]
    return max(nums) if nums else None


def _sane_crypto_level(level, ref, asset_type):
    """Backstop for $K-shorthand misparse on crypto price levels: a tweet that
    writes "54-50" or "144K" for $54K/$50K/$144K is sometimes extracted as the
    bare 50 / 144. For crypto, if a stop_loss/target is below a hundredth of the
    reference magnitude (entry price, else a tp1/tp2 take-profit level) it is
    almost certainly a dropped-thousands error, so scale it back up by 1000.
    The threshold is ref/100, not ref/10: the error is always a factor of 1000
    (so a bad value lands near ref/1000, well under ref/100), while a take-profit
    can legitimately sit 10x+ above a stop — ref/10 would false-positive on a
    cheap coin with a far target. No-op without a positive reference or when the
    value already looks sane. Independent of the LLM so it always holds."""
    if asset_type != "crypto":
        return level
    if not isinstance(level, (int, float)) or isinstance(level, bool) or level <= 0:
        return level
    if not isinstance(ref, (int, float)) or isinstance(ref, bool) or ref <= 0:
        return level
    return level * 1000 if level < ref / 100 else level


def record_from_parsed(account, tw, parsed):
    """Build a trades.json signal record from a parsed LLM result, or None if
    it isn't an actionable signal. Shared by the real-time and batch paths."""
    if not parsed:
        return None
    if parsed["action"] == "none" or not parsed.get("ticker"):
        return None
    parsed["size_pct"] = _sane_size_pct(parsed.get("size_pct"))
    _asset = parsed.get("asset_type", "unknown")
    _ref = _crypto_ref(parsed.get("entry_price"),
                       parsed.get("tp1"), parsed.get("tp2"))
    _stop = _sane_crypto_level(parsed.get("stop_loss"), _ref, _asset)
    _target = _sane_crypto_level(parsed.get("target"), _ref, _asset)
    return {
        "account": account,
        "source_type": SOURCE_TYPE.get(account, "portfolio"),
        "portfolio": parsed.get("portfolio"),
        "tweet_id": tw["id"],
        "timestamp": tw.get("created_at"),
        "signal_type": parsed["action"],
        "sell_kind": parsed.get("sell_kind"),
        "confidence": parsed["confidence"],
        "actionable": parsed["action"] in ("buy", "sell", "position"),
        "tickers": [parsed["ticker"]],
        "asset_type": parsed.get("asset_type", "unknown"),
        "position_size_pct": parsed["size_pct"],
        "entry_price": parsed["entry_price"],
        "stop_loss": _stop,
        "target": _target,
        "trade_date": parsed.get("trade_date"),
        "holding_thesis": parsed.get("holding_thesis"),
        "reasoning": parsed["reasoning"],
        # Chart-image (vision) enrichments — populated only for influencer
        # tweets that carried an analyzable chart photo (else null).
        "tp1": parsed.get("tp1"),
        "tp2": parsed.get("tp2"),
        "chart_trend": parsed.get("trend"),
        "chart_notes": parsed.get("chart_notes"),
        "has_chart": bool(tw.get("media")),
        "url": f"https://x.com/{account}/status/{tw['id']}",
        "text": tw.get("text", ""),
    }


def build_signal(account, tw, interp):
    tweet_date = (tw.get("created_at") or "")[:10]
    text = tw.get("text", "")
    parsed = interp.extract(text, account, tweet_date)
    # Influencer chart-image pass (Sonnet vision). Runs when the tweet carries a
    # photo AND either (a) the text pass already yielded an actionable signal —
    # vision backfills its chart-only levels — or (b) the text was NOT actionable
    # but an asset is plausibly in play (ticker/cashtag): the annotated chart may
    # BE the signal, so a clearly-directional chart can promote it to a trade.
    actionable = parsed and parsed["action"] != "none" and parsed.get("ticker")
    if (SOURCE_TYPE.get(account) == "influencer" and tw.get("media")
            and (actionable or _mentions_asset(parsed, text))):
        chart = interp.extract_chart(tw["media"], text, account, tweet_date)
        merge_chart(parsed, chart)
        if not actionable:
            promote_with_chart(parsed, chart)
    return record_from_parsed(account, tw, parsed)


def should_send_to_llm(tw):
    """The pre-LLM gate: keep non-replies, and replies only if they mention a
    sell. Returns True if this tweet should be interpreted."""
    if tweet_is_reply(tw):
        return reply_has_sell_verb(tw.get("text", ""))
    return True


# --- env / io --------------------------------------------------------------
def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


# --- Twitter fetch ---------------------------------------------------------
def api_get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {os.environ['X_BEARER_TOKEN']}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def fetch_tweets(uid, since_id=None):
    collected, token = [], None
    while len(collected) < MAX_FETCH:
        params = {
            "max_results": min(100, max(5, MAX_FETCH - len(collected))),
            "tweet.fields": "created_at,text,referenced_tweets",
        }
        if since_id:
            params["since_id"] = since_id
        if token:
            params["pagination_token"] = token
        data = api_get(f"{API_BASE}/users/{uid}/tweets?{urllib.parse.urlencode(params)}")
        batch = data.get("data", [])
        collected.extend(batch)
        token = data.get("meta", {}).get("next_token")
        if not token or not batch:
            break
    return collected[:MAX_FETCH]


# --- GetXAPI fetch (alternative backend, ~100x cheaper) --------------------
def getxapi_get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {os.environ['GETXAPI_KEY']}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def _normalize_getxapi(tw):
    """Map a GetXAPI tweet to the official-API shape the rest of the code uses.

    Critically, GetXAPI's `createdAt` is the legacy Twitter format
    ('Mon Jun 01 00:46:36 +0000 2026'); convert it to ISO 8601 so downstream
    string-sorting and [:10] date slicing keep working."""
    created = tw.get("createdAt")
    try:
        created_iso = parsedate_to_datetime(created).astimezone(
            timezone.utc).isoformat() if created else None
    except (TypeError, ValueError):
        created_iso = None
    # Photo URLs only (skip videos/gifs): used for chart-image vision analysis.
    media = [m.get("url") for m in (tw.get("media") or [])
             if m.get("type") == "photo" and m.get("url")]
    return {
        "id": str(tw.get("id")),
        "text": tw.get("text", ""),
        "created_at": created_iso,
        "is_reply": tw.get("isReply"),
        # author handle of THIS tweet. tweets_and_replies returns the whole
        # conversation thread, so a reply may be authored by another user; we
        # filter on this so foreign replies aren't extracted as the account's
        # own signals (see foreign_author()).
        "author": (tw.get("author") or {}).get("userName"),
        "media": media,
    }


def fetch_getxapi(account, since_id=None):
    """Cursor-paginate GetXAPI. No since_id server-side, so stop once a page
    contains a tweet we've already seen. Returns (tweets, n_api_calls)."""
    collected, cursor, calls = [], None, 0
    path = (GETXAPI_POSTS_PATH if account in POSTS_ONLY_ACCOUNTS
            else GETXAPI_TWEETS_PATH)
    while len(collected) < MAX_FETCH:
        params = {"userName": account}
        if cursor:
            params["cursor"] = cursor
        data = getxapi_get(
            f"{GETXAPI_BASE}{path}?{urllib.parse.urlencode(params)}")
        calls += 1
        batch = data.get("tweets", [])
        if not batch:
            break
        hit_old = False
        for tw in batch:
            n = _normalize_getxapi(tw)
            collected.append(n)
            if since_id and n["id"].isdigit() and int(n["id"]) <= int(since_id):
                hit_old = True
        if hit_old or not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return collected[:MAX_FETCH], calls


def tweets_for_account(account, state, backfill, source):
    """Return (tweets, twitter_reads, api_calls). Backfill reads local snapshots."""
    if backfill:
        return load_json(RAW_FILES.get(account, ""), []), 0, 0
    since_id = state.get(account, {}).get("newest_id")
    if source == "getxapi":
        tweets, calls = fetch_getxapi(account, since_id=since_id)
    else:
        uid = api_get(f"{API_BASE}/users/by/username/{account}")["data"]["id"]
        tweets = fetch_tweets(uid, since_id=since_id)
        calls = 0          # official is billed per tweet, not per call
    if tweets:
        newest = str(max(int(t["id"]) for t in tweets if str(t["id"]).isdigit()))
        if not since_id or int(newest) > int(since_id):
            # update in place so sibling keys (e.g. last_fetch) survive
            state.setdefault(account, {})["newest_id"] = newest
    return tweets, len(tweets), calls


def _build_batch_requests(candidates):
    """candidates: list of (custom_id, account, tw). Returns one Batch API Request
    per tweet for the Haiku text pass — identical params to real-time extract()."""
    return [
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=MODEL, max_tokens=300, system=_system_blocks(),
            output_config=_output_config(),
            messages=[_user_message(account, tw.get("text", ""),
                                    (tw.get("created_at") or "")[:10])]))
        for cid, account, tw in candidates
    ]


def _poll_batch(interp, batch_id, max_wait_s=None):
    """Block-poll a submitted batch until processing_status == 'ended'. Returns
    True when ended, or False if max_wait_s elapsed first (None = wait forever,
    used by the latency-insensitive --backfill-batch)."""
    waited = 0
    while True:
        b = interp.client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            return True
        if max_wait_s is not None and waited >= max_wait_s:
            return False
        rc = b.request_counts
        print(f"  status={b.processing_status} processing={rc.processing} "
              f"succeeded={rc.succeeded} errored={rc.errored}")
        time.sleep(BATCH_POLL_INTERVAL_S)
        waited += BATCH_POLL_INTERVAL_S


def _collect_batch(interp, batch_id, lookup):
    """Stream a completed batch's results. Returns (parsed, errored) where parsed
    is a list of (account, tw, parsed_dict) and errored is the list of custom_ids
    whose requests did not succeed. Accumulates batch token usage on interp
    (billed at 50%). Records are TEXT-only; callers add the vision pass."""
    parsed, errored = [], []
    for result in interp.client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            errored.append(result.custom_id)
            continue
        msg = result.result.message
        u = msg.usage
        interp.batch_input_tokens += u.input_tokens + \
            (u.cache_read_input_tokens or 0) + (u.cache_creation_input_tokens or 0)
        interp.batch_output_tokens += u.output_tokens
        account, tw = lookup[result.custom_id]
        parsed.append((account, tw, _parse_json_text(msg.content)))
    return parsed, errored


def _vision_enrich(interp, account, tw, parsed):
    """Run the real-time influencer chart pass (Sonnet vision) on a batch-parsed
    result, mirroring build_signal's back half so batch mode keeps the same
    chart enrichment as the live path. Mutates `parsed` in place."""
    actionable = parsed and parsed["action"] != "none" and parsed.get("ticker")
    if (SOURCE_TYPE.get(account) == "influencer" and tw.get("media")
            and (actionable or _mentions_asset(parsed, tw.get("text", "")))):
        chart = interp.extract_chart(tw["media"], tw.get("text", ""),
                                     account, (tw.get("created_at") or "")[:10])
        merge_chart(parsed, chart)
        if not actionable:
            promote_with_chart(parsed, chart)


def collect_live_batch(interp, candidates):
    """Submit live-run candidates as ONE Batch API job, block-poll it (bounded by
    BATCH_MAX_WAIT_S), then build signal records. The influencer chart-vision pass
    still runs real-time, so batch mode produces the SAME records as the live path
    at half the Haiku cost. Returns the new records, or None if the batch did not
    finish in time (caller must defer: don't persist state, retry next run)."""
    lookup = {cid: (account, tw) for cid, account, tw in candidates}
    batch = interp.client.messages.batches.create(
        requests=_build_batch_requests(candidates))
    print(f"Submitted batch {batch.id}: {len(candidates)} requests. "
          f"Polling (max {BATCH_MAX_WAIT_S // 60} min)...")
    if not _poll_batch(interp, batch.id, BATCH_MAX_WAIT_S):
        # Cancel the abandoned batch: the next run re-fetches these tweets and
        # submits a FRESH batch, so letting the old one run to completion would
        # bill the same tokens twice (its results are never collected).
        try:
            interp.client.messages.batches.cancel(batch.id)
        except Exception as e:                      # noqa: BLE001 - best-effort
            print(f"  (cancel of abandoned batch failed: {e})", file=sys.stderr)
        print(f"Batch {batch.id} did not finish within {BATCH_MAX_WAIT_S // 60} "
              f"min - deferring signals to the next run.", file=sys.stderr)
        notify_telegram(f"batch {batch.id} exceeded "
                        f"{BATCH_MAX_WAIT_S // 60}min; cancelled + deferred to next run")
        return None
    parsed, errored = _collect_batch(interp, batch.id, lookup)
    # Errored requests would otherwise be LOST FOREVER: newest_id has already
    # advanced past these tweets, so they are never refetched. Retry each one
    # real-time (full Haiku price; the errored set is normally tiny). A retry
    # that still fails matches the non-batch live path's exposure (extract()
    # returning None) — accepted there too.
    if errored:
        print(f"  batch: {len(errored)} request(s) errored; retrying "
              f"real-time.", file=sys.stderr)
        for cid in errored:
            account, tw = lookup[cid]
            p = interp.extract(tw.get("text", ""), account,
                               (tw.get("created_at") or "")[:10])
            parsed.append((account, tw, p))
    records = []
    for account, tw, p in parsed:
        _vision_enrich(interp, account, tw, p)
        rec = record_from_parsed(account, tw, p)
        if rec:
            records.append(rec)
    return records


def backfill_batch(interp):
    """Interpret all local snapshot tweets via the Anthropic Batch API (50%
    cheaper). Submits one batch, polls until complete, then writes results.
    Rebuilds trades.json + positions.json from scratch (like --backfill)."""
    candidates, skipped = [], 0
    for account in ACCOUNTS:
        influencer = SOURCE_TYPE.get(account) == "influencer"
        for tw in load_json(RAW_FILES.get(account, ""), []):
            if foreign_author(account, tw):       # drop other users' thread replies
                skipped += 1
                continue
            if account in POSTS_ONLY_ACCOUNTS and tw.get("is_reply"):
                skipped += 1
                continue
            if is_duplicate_retweet(tw.get("text", "")):  # RT of monitored acct
                skipped += 1
                continue
            if not influencer and not should_send_to_llm(tw):
                skipped += 1
                continue
            candidates.append((f"{account}__{tw['id']}", account, tw))

    if not candidates:
        print("No candidate tweets to batch.")
        return
    lookup = {cid: (account, tw) for cid, account, tw in candidates}
    batch = interp.client.messages.batches.create(
        requests=_build_batch_requests(candidates))
    print(f"Submitted batch {batch.id}: {len(candidates)} requests "
          f"({skipped} replies skipped). Polling...")
    _poll_batch(interp, batch.id)                 # backfill: wait as long as needed

    # Backfill stays TEXT-only (no vision pass), matching the prior behaviour.
    parsed, errored = _collect_batch(interp, batch.id, lookup)
    signals = []
    for account, tw, p in parsed:
        rec = record_from_parsed(account, tw, p)
        if rec:
            signals.append(rec)

    signals.sort(key=lambda r: r["timestamp"], reverse=True)
    write_json_atomic(TRADES_FILE, signals)
    reconcile(TRADES_FILE, POSITIONS_FILE)

    batch_cost = interp.batch_cost()
    realtime_cost = batch_cost * 2
    by = {}
    for s in signals:
        key = (s["account"], s["signal_type"])
        by[key] = by.get(key, 0) + 1
    print(f"\nBatch complete: {len(signals)} signals "
          f"({len(candidates)} requests, {len(errored)} errored).")
    for (acct, st), c in sorted(by.items()):
        print(f"  {acct:16} {st:9} {c}")
    print(f"\nTokens: input {interp.batch_input_tokens}, "
          f"output {interp.batch_output_tokens}")
    print(f"Batch API cost: ${batch_cost:.4f} (50% off)")
    print(f"  vs real-time: ${realtime_cost:.4f}  -> saved ${realtime_cost - batch_cost:.4f}")
    print(f"Saved -> {TRADES_FILE} + {POSITIONS_FILE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="interpret local tweets_*.json instead of fetching X")
    ap.add_argument("--backfill-batch", action="store_true",
                    help="like --backfill but via the Anthropic Batch API (50%% "
                         "cheaper); submits one job and polls to completion")
    ap.add_argument("--batch", action="store_true",
                    help=f"live incremental run via the Anthropic Batch API (50%% "
                         f"cheaper Haiku text pass). Fetches fresh tweets, submits "
                         f"ONE batch and block-polls it to completion (up to "
                         f"{BATCH_MAX_WAIT_S // 60} min), then writes/reconciles/"
                         f"trades as usual. Signals are delayed by the batch "
                         f"turnaround.")
    ap.add_argument("--source", choices=["official", "getxapi"], default="getxapi",
                    help="tweet backend: getxapi (default, tweets_and_replies) "
                         "or official X API")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch fresh + analyze, but write NOTHING (no trades.json, "
                         "positions.json, or state). Dumps would-be signals to "
                         "/tmp/pilot_dryrun_<source>.json for comparison.")
    ap.add_argument("--account", choices=ACCOUNTS,
                    help="restrict to ONE account. With --backfill, rebuilds only "
                         "that account's signals in trades.json, preserving all "
                         "others. Without it, just limits the live fetch.")
    ap.add_argument("--test-telegram", action="store_true",
                    help="send a test alert to confirm Telegram is wired, then exit.")
    ap.add_argument("--no-trade", action="store_true",
                    help="do NOT execute trades on IBKR after the run (auto_trader "
                         "still queues orders in no-trade mode for inspection).")
    args = ap.parse_args()

    # Telegram self-test: load creds from the env files and send one message.
    if args.test_telegram:
        for p in TELEGRAM_ENVS:
            load_env(p)
        ok, detail = _send_telegram(
            "✅ pilot_trader: P2 health check - all systems operational")
        print(f"Telegram test: {'DELIVERED' if ok else 'FAILED'}  ({detail})")
        sys.exit(0 if ok else 1)

    load_env(ENV_FILE)
    load_env(PAPER_ENV)   # Telegram creds (setdefault: pilot .env still wins)
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. Add it to ~/pilot_trader/.env "
              "(ANTHROPIC_API_KEY=sk-ant-...) and re-run.", file=sys.stderr)
        sys.exit(1)
    if not args.backfill and not args.backfill_batch:
        need = "GETXAPI_KEY" if args.source == "getxapi" else "X_BEARER_TOKEN"
        if not os.environ.get(need):
            print(f"{need} is not set. Add it to ~/pilot_trader/.env and re-run.",
                  file=sys.stderr)
            sys.exit(1)

    interp = Interpreter()
    if args.backfill_batch:
        backfill_batch(interp)
        return

    state = load_json(STATE_FILE, {})
    if not (args.backfill or args.dry_run):
        _staleness_alert(state)   # page if prior runs were missed/failed
    # Backfill rebuilds trades.json from the local snapshots, so it starts from a
    # clean slate (otherwise dedup against prior signals would skip every tweet).
    # Dry-run also starts clean and ignores stored state so it fetches a fresh,
    # comparable batch from the live API (otherwise since_id would return ~nothing).
    # --account --backfill rebuilds ONE account: drop its old signals, keep every
    # OTHER account's, then re-interpret it from its snapshot. A live --account
    # run must NOT drop (the incremental since_id fetch won't re-add history), so
    # it just restricts the loop and adds incrementally.
    if args.account and args.backfill:
        existing = [r for r in load_json(TRADES_FILE, [])
                    if r.get("account") != args.account]
    else:
        existing = [] if (args.backfill or args.dry_run) \
            else load_json(TRADES_FILE, [])
    seen_ids = {r["tweet_id"] for r in existing}
    run_state = {} if args.dry_run else state    # throwaway state in dry-run
    accounts = [args.account] if args.account else ACCOUNTS

    all_new, total_reads, total_skipped, total_sell_cand, total_calls = \
        [], 0, 0, 0, 0
    candidates = []          # (custom_id, account, tw) accumulator for --batch
    now = datetime.now(timezone.utc)
    for account in accounts:
        # Slow accounts (POLL_MIN_INTERVAL_H): skip on a live run if fetched too
        # recently. Backfill/dry-run/explicit --account always process.
        if not (args.backfill or args.dry_run) and not args.account:
            skip, age_h = slow_fetch_skip(account, run_state, now)
            if skip:
                print(f"[{account}] skipped (last fetch {age_h:.1f}h ago "
                      f"< {POLL_MIN_INTERVAL_H[account]}h min)")
                continue
        # High-water mark of tweet ids already PROCESSED in a prior run. GetXAPI
        # returns the full latest page every run (no server-side since_id), so
        # without this every non-signal tweet on the page is re-sent to the LLM
        # each run (only SIGNAL-bearing tweets land in trades.json -> seen_ids).
        # newest_id is advanced inside tweets_for_account below, so capture it
        # first. Live mode only: backfill/dry-run must scan the whole snapshot.
        prior_newest = None
        if not (args.backfill or args.dry_run):
            prior_newest = (run_state.get(account) or {}).get("newest_id")
        try:
            tweets, reads, calls = tweets_for_account(
                account, run_state, args.backfill, args.source)
        except urllib.error.HTTPError as e:
            print(f"[{account}] HTTP {e.code}: "
                  f"{e.read().decode('utf-8', 'replace')}", file=sys.stderr)
            continue
        # Record the fetch time so POLL_MIN_INTERVAL_H accounts can pace
        # themselves off "last fetch", not an exact cron hour.
        if not (args.backfill or args.dry_run):
            run_state.setdefault(account, {})["last_fetch"] = now.isoformat()
        total_reads += reads
        total_calls += calls
        new, skipped, sell_cand, foreign, seen, retweet = 0, 0, 0, 0, 0, 0
        for tw in tweets:
            if tw["id"] in seen_ids:
                continue
            # Already processed in a prior run (id at/below the high-water mark):
            # skip BEFORE the LLM so non-signal tweets aren't re-interpreted.
            if (prior_newest and str(tw["id"]).isdigit()
                    and int(tw["id"]) <= int(prior_newest)):
                seen += 1
                continue
            # Drop thread replies authored by OTHER users (tweets_and_replies
            # returns the whole conversation). Without this a follower's reply
            # is mis-extracted as the account's own signal.
            if foreign_author(account, tw):
                foreign += 1
                continue
            # Posts-only accounts: drop ANY reply (incl. the account's own
            # self-thread replies) so only original posts are extracted.
            if account in POSTS_ONLY_ACCOUNTS and tw.get("is_reply"):
                skipped += 1
                continue
            text = tw.get("text", "")
            # Drop retweets of OTHER monitored accounts (verbatim dupes of a
            # tweet we ingest from the original author directly). Runs for ALL
            # accounts, including influencers, before their reply-gate bypass.
            if is_duplicate_retweet(text):
                retweet += 1
                continue
            # Influencer accounts (e.g. @IncomeSharks) bypass the reply gate —
            # every tweet AND reply is sent to the LLM.
            if tweet_is_reply(tw) and SOURCE_TYPE.get(account) != "influencer":
                # Replies are skipped UNLESS they mention a sell — those we keep
                # so we don't miss exits disclosed in @-reply threads.
                if reply_has_sell_verb(text):
                    sell_cand += 1
                else:
                    skipped += 1
                    continue
            if args.batch:
                # Defer the LLM call: queue the tweet for one bulk Batch API job
                # submitted after every account is fetched. `new` counts queued
                # tweets here (actual signals are resolved post-batch).
                candidates.append((f"{account}__{tw['id']}", account, tw))
                seen_ids.add(tw["id"])
                new += 1
            else:
                sig = build_signal(account, tw, interp)
                if sig:
                    all_new.append(sig)
                    seen_ids.add(tw["id"])
                    new += 1
        total_skipped += skipped
        total_sell_cand += sell_cand
        label = "queued for batch" if args.batch else "new signals"
        print(f"[{account}] scanned {len(tweets)}, already-seen-skipped {seen}, "
              f"foreign-author-skipped {foreign}, retweet-skipped {retweet}, "
              f"reply-skipped {skipped}, "
              f"reply-sell-candidate {sell_cand}, {label} {new}")

    # --batch: now run the single bulk job. A timeout leaves state unwritten so
    # the next run refetches+resubmits these tweets (no signals are lost).
    if args.batch:
        if candidates:
            result = collect_live_batch(interp, candidates)
            if result is None:
                print("Batch did not complete; no writes this run.",
                      file=sys.stderr)
                return
            all_new = result
        else:
            print("No candidate tweets to batch.")

    merged = existing + all_new
    merged.sort(key=lambda r: r["timestamp"], reverse=True)
    if args.dry_run:
        out = f"/tmp/pilot_dryrun_{args.source}.json"
        write_json_atomic(out, all_new)
        print(f"\n[DRY-RUN] no writes to trades.json / positions.json / state.")
        print(f"[DRY-RUN] would add {len(all_new)} signals; "
              f"would-be signals dumped to {out}")
    else:
        write_json_atomic(TRADES_FILE, merged)    # atomic: temp + os.replace
        reconcile(TRADES_FILE, POSITIONS_FILE)    # fold events -> positions.json
        if not args.backfill:
            # Heartbeat: the dashboard reads this to flag stale data if a cron
            # run stops succeeding.
            state["_last_run"] = datetime.now(timezone.utc).isoformat()
            write_json_atomic(STATE_FILE, state)

    # summary
    by = {}
    for s in all_new:
        key = (s["account"], s["signal_type"])
        by[key] = by.get(key, 0) + 1
    print(f"\nNew signals: {len(all_new)} (stored total: {len(merged)})")
    for (acct, st), c in sorted(by.items()):
        print(f"  {acct:16} {st:9} {c}")
    print(f"\nreply-skipped (no API call): {total_skipped}  |  "
          f"reply-sell-candidate (sent to LLM): {total_sell_cand}")
    print(f"LLM text calls (Haiku): {interp.calls}  "
          f"(input {interp.input_tokens} tok, output {interp.output_tokens} tok)"
          f"  ${interp.cost():.4f}")
    if interp.batch_input_tokens or interp.batch_output_tokens:
        print(f"LLM batch text (Haiku, 50% off): input "
              f"{interp.batch_input_tokens} tok, output "
              f"{interp.batch_output_tokens} tok  ${interp.batch_cost():.4f}")
    print(f"LLM vision calls (Sonnet): {interp.vision_calls}  "
          f"(input {interp.vision_input_tokens} tok, "
          f"output {interp.vision_output_tokens} tok)  ${interp.vision_cost():.4f}")
    print(f"LLM cost this run: "
          f"${interp.cost() + interp.batch_cost() + interp.vision_cost():.4f}")
    if not args.backfill:
        if args.source == "getxapi":
            print(f"GetXAPI [{args.source}]: {total_reads} tweets in "
                  f"{total_calls} calls (${total_calls * GETXAPI_COST_PER_CALL:.4f})")
        else:
            print(f"Twitter reads [{args.source}]: {total_reads}  "
                  f"(${total_reads * TWITTER_COST_PER_TWEET:.4f})")

    # Append per-run cost telemetry (skip dry-run writes and zero-LLM runs).
    if not args.dry_run and (interp.calls or interp.vision_calls
                             or interp.batch_input_tokens):
        log_cost(interp)

    # Automatic execution: hand off to auto_trader (AI mirror -> IBKR paper) on
    # every live run — NOT only when this run produced new signals: auto_trader
    # also reconciles resting MOO orders and retries deferred sells/unsubmitted
    # orders, which must not wait for the next run that happens to find a new
    # signal. It returns cheaply (no IB connect) when there is nothing to do.
    # Never on dry-run/backfill. A failure here must NOT fail the monitor run.
    if not args.dry_run and not args.backfill:
        try:
            import auto_trader
            auto_trader.run(no_trade=args.no_trade)
        except Exception as e:                       # noqa: BLE001 - isolate
            print(f"[auto_trader] execution step failed: {e!r}", file=sys.stderr)


def log_cost(interp):
    """Append this run's token usage + cost to data/cost_log.json. Telemetry
    only: a write failure (e.g. data/ owned by the Docker user) prints a warning
    but never breaks the run."""
    rec = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # Fold batch Haiku tokens into the haiku_* totals (dashboard sums these);
        # total_usd already discounts the batch portion via interp.batch_cost().
        "haiku_input_tok": interp.input_tokens + interp.batch_input_tokens,
        "haiku_output_tok": interp.output_tokens + interp.batch_output_tokens,
        "sonnet_input_tok": interp.vision_input_tokens,
        "sonnet_output_tok": interp.vision_output_tokens,
        "total_usd": round(
            interp.cost() + interp.batch_cost() + interp.vision_cost(), 6),
    }
    try:
        log = load_json(COST_LOG_FILE, [])
        if not isinstance(log, list):
            log = []
        log.append(rec)
        os.makedirs(DATA_DIR, exist_ok=True)
        write_json_atomic(COST_LOG_FILE, log)
        print(f"Cost logged -> {COST_LOG_FILE} ({len(log)} runs)")
    except OSError as e:
        print(f"[cost-log] could not write {COST_LOG_FILE}: {e}", file=sys.stderr)


def _send_telegram(text):
    """Send a raw Telegram message. Returns (ok, detail). ok reflects Telegram's
    own {"ok": true} response, so it confirms DELIVERY, not just a 200."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False, "creds not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        return bool(body.get("ok")), f"telegram ok={body.get('ok')}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return False, f"send failed: {e}"


def notify_telegram(reason):
    """Best-effort alert: '⚠️ pilot_trader: <reason> at <ts>'. No-ops silently
    if creds are not configured."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    ok, detail = _send_telegram(f"⚠️ pilot_trader: {reason} at {ts}")
    if not ok and "not configured" not in detail:
        print(f"[telegram] {detail}", file=sys.stderr)
    return ok


def _staleness_alert(state):
    """If the last successful run is older than STALE_ALERT_HOURS, page Telegram.
    Runs at the start of each live run, so missed/failed prior runs are caught."""
    last = state.get("_last_run")
    if not last:
        return
    try:
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(last)).total_seconds() / 3600
    except (ValueError, TypeError):
        return
    if age_h > STALE_ALERT_HOURS:
        notify_telegram(f"data stale — last successful run {age_h:.0f}h ago "
                        f"(>{STALE_ALERT_HOURS}h)")


def _log_failure(exc):
    """Append a FAILED entry with full traceback to monitor.log."""
    ts = datetime.now(timezone.utc).isoformat()
    try:
        with open(MONITOR_LOG, "a") as f:
            f.write(f"\n===== FAILED {ts} =====\n")
            f.write("".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write("=====================\n")
    except OSError:
        pass


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        # Clean operator exits, NOT failures: argparse --help / usage errors raise
        # SystemExit, and Ctrl-C raises KeyboardInterrupt. Neither should write a
        # FAILED block or page Telegram — just propagate the exit code.
        raise
    except BaseException as exc:   # log anything else, then surface a non-zero exit
        _log_failure(exc)
        # Env may not have loaded if main() crashed early; load creds here too.
        try:
            load_env(ENV_FILE)
            load_env(PAPER_ENV)
            notify_telegram(f"monitor.py FAILED: {exc!r}")
        except Exception:
            pass
        print(f"monitor.py FAILED: {exc!r} "
              f"(traceback appended to {MONITOR_LOG})", file=sys.stderr)
        sys.exit(1)
