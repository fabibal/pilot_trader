#!/usr/bin/env python3
"""
Multi-account trade-signal monitor for X trade-call accounts, with LLM
interpretation.

Pipeline per tweet:
  1. Every tweet (including @-replies — sells are often disclosed there) is
     sent to Gemini (gemini-2.5-flash-lite) which extracts ticker / action /
     size / entry price / trade date / portfolio / confidence as
     schema-constrained JSON (always valid).
  2. Signals with action == "none" or a null ticker are discarded.
  3. Kept signals are tagged with their source account, written to
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

Requires GOOGLE_API_KEY + the source's key (GETXAPI_KEY or X_BEARER_TOKEN),
loaded from ~/pilot_trader/.env. Run with the project venv:
/home/fbazsa/pilot_trader/.venv/bin/python monitor.py
"""

import argparse
import gzip
import html
import http.client
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google import genai
from google.genai import types as genai_types
from google.genai import errors as genai_errors

from reconcile import reconcile, write_json_atomic

# --- config ---------------------------------------------------------------
# X bearer token is loaded from .env (X_BEARER_TOKEN) — never hardcoded.
# Account identity/classification config is shared across the pipeline and
# lives in accounts.py (single source of truth).
from accounts import ACCOUNTS, SOURCE_TYPE, POSTS_ONLY_ACCOUNTS
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
    "IncomeSharks": os.path.join(HOME, "tweets_incomesharks.json"),
    "traderstewie": os.path.join(HOME, "tweets_traderstewie.json"),
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

# Text extraction. gemini-2.5-flash-lite replaced Haiku here 2026-07 (Anthropic
# credit balance depleted): cheap, stable (not the shut-down preview variant),
# and schema-constrained JSON via response_json_schema works the same way.
# 2026-07-13: briefly pinned to gemini-2.0-flash because 2.5-flash-lite is
# paid-tier-only and GOOGLE_API_KEY's project ("My First Project") had no
# linked Cloud Billing account (an AI Studio prepay credit balance alone does
# NOT enable paid tier) -- reverted back now that billing is linked.
# thinking_budget=0 is explicit even though 2.5 Flash-Lite defaults to thinking
# off, so a future Google default change can't silently eat max_output_tokens.
MODEL = "gemini-2.5-flash-lite"
EXTRACT_THINKING = genai_types.ThinkingConfig(thinking_budget=0)
TWITTER_COST_PER_TWEET = 0.005       # X API read
EXTRACT_INPUT_PER_1M = 0.10          # $ / 1M input tokens
EXTRACT_OUTPUT_PER_1M = 0.40         # $ / 1M output tokens

# Vision model for chart-image analysis (Haiku 4.5 does NOT accept images).
# Only used for influencer tweets that carry a chart photo. Pricier per token,
# so it runs as an ADD-ON to the cheap Haiku text pass, never as a replacement.
# (Gemini 3.5 Flash via Google AI Studio / google-genai — replaced Sonnet 5 here
# 2026-07: multimodal + json-schema structured output, no Anthropic vision spend.
# thinking_budget=0 mirrors the old "thinking disabled" behavior — Gemini 3.5
# Flash also thinks by default (medium), which would eat the tight max_output_tokens
# budgets meant for JSON only.)
VISION_MODEL = "gemini-3.5-flash"
GEMINI_THINKING = genai_types.ThinkingConfig(thinking_budget=0)     # off
# Opt-in dynamic thinking for the dense, low-volume analysis reads where the
# reasoning pays off (YouTube/Cowen TA, Kendrick forecasts) — NOT vision, NOT the
# high-volume per-tweet feeds (ki/joao). Callers that use it MUST give
# max_output_tokens room for thinking + JSON or the json output truncates.
GEMINI_DEEP_THINKING = genai_types.ThinkingConfig(thinking_budget=-1)   # dynamic
GEMINI_INPUT_PER_1M = 1.50           # $ / 1M input tokens (gemini-3.5-flash)
GEMINI_OUTPUT_PER_1M = 9.00          # $ / 1M output tokens, incl. thinking tokens
MAX_VISION_IMAGES = 2                # cap images/tweet to bound vision cost

# --- LLM extraction --------------------------------------------------------
# No pre-filter: every tweet is sent to Claude, which decides what is a signal.
EXTRACTION_SYSTEM = (
    "You extract trade signals from tweets posted by trading accounts. Given one "
    "tweet (and the handle that posted it), decide whether it reports an "
    "actionable trade (or a currently-held position) and extract the details.\n"
    "Every monitored account is a HUMAN TRADER / INFLUENCER — currently "
    "@IncomeSharks and @traderstewie. They post FREQUENT trade ideas/calls on "
    "stocks and/or crypto, and mix pure analysis/opinion with actionable calls. "
    "They often state entry prices, stop losses, and price targets. "
    "@traderstewie is a US STOCK/ETF swing trader (e.g. SOXL, AEHR, LWLG, INTC, "
    "RKLB) — set asset_type = \"stock\" for its calls. Always set "
    "portfolio = null (no monitored account posts on behalf of a named model "
    "portfolio).\n"
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
    "- portfolio: always null — no monitored account posts on behalf of a "
    "named model portfolio.\n"
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


def _image_block(url):
    """Download a Twitter media photo and return a Gemini image Part, or None
    on any failure. `name=small` keeps the download (and the vision token cost)
    modest while staying legible for chart levels."""
    try:
        req = urllib.request.Request(url + "?name=small",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError) as e:
        print(f"  [media error] {url}: {e}", file=sys.stderr)
        return None
    media_type = "image/png" if url.lower().endswith(".png") else "image/jpeg"
    return genai_types.Part.from_bytes(data=raw, mime_type=media_type)


def _unescape_strings(obj):
    """Recursively html.unescape() every string in a parsed JSON value.
    Gemini occasionally emits HTML-named-entity text (e.g. "&eacute;" for
    "e") instead of the literal character -- harmless no-op on already-clean
    text, so applied unconditionally. Shared by twitter_digest.py and
    youtube_monitor.py, both of which get Hungarian free text back from
    gemini-3.5-flash."""
    if isinstance(obj, str):
        return html.unescape(obj)
    if isinstance(obj, list):
        return [_unescape_strings(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _unescape_strings(v) for k, v in obj.items()}
    return obj


def _first_sentence(text):
    """The leading sentence of a free-text field, whitespace-collapsed. Used by
    both digests as the stand-in when a CURRENT VIEW synthesis comes back
    without its one-line headline: the dashboard's Consensus panel shows that
    field on its own, so a blunt restatement of the stance carries more than a
    canned placeholder would."""
    s = " ".join((text or "").split())
    m = re.match(r".+?[.!?](?=\s|$)", s)
    return m.group(0) if m else s


# gemini-3.5-flash occasionally (probabilistically -- a retry of the
# identical call often comes back clean) mangles accented non-ASCII
# characters in long free-text fields. Observed forms, all specific to
# Hungarian-output feeds (twitter_digest.py, youtube_monitor.py):
#  - the UTF-8 continuation byte survives with its high bit stripped,
#    landing mid-word as stray punctuation: "veszteseg" -> "vesztes)g".
#  - a raw C0 control byte in its place: "n\x01gy".
#  - an ASCII escape-like marker + bare letter: "realiz'alt", "%evek".
#  - a bogus numeric-entity-shaped placeholder: "T#1;masz", "elt#3;rő".
#    Not a real HTML entity (no leading "&", so html.unescape() in
#    _unescape_strings correctly ignores it) and NOT a stable per-char
#    code -- the same digit stands in for different letters within one
#    string (#3; covers 'ó', 'í', 'ö', 'é' and 'á' in a single summary) --
#    so it's detected-for-retry like the others rather than decoded.
#  - one or more accented characters silently replaced by a literal TAB
#    (0x09) with no other artifact: "\ts" for "és", "mozg\ttlagot" for
#    "mozgóátlagot" (moving average, accusative). Found 2026-07-29 in
#    generate_current_view() output, undetected because _MANGLED_CTRL_RE
#    used to carve \x09/\x0a/\x0d out of the C0 range on the assumption
#    they were legitimate whitespace -- they are not: every field these
#    regexes guard is single-paragraph prose, so a tab/CR/LF mid-string is
#    exactly as unambiguous a corruption signal as any other control byte.
#  - the same silent replacement but with a plain SPACE (0x20) instead of a
#    TAB: "elm elt" for "elmúlt", "v rakoz   ll pontot" for "várakozó
#    álláspontot", " s" for "és". Found 2026-07-29 in daancrypto's
#    generate_current_view() output; widening _MANGLED_CTRL_RE above does
#    NOT cover it -- a space is not a control byte, and mid-string it is
#    indistinguishable from a word separator. What gives it away is the
#    stray one-letter word fragments it strews through the text, so that is
#    what _MANGLED_FRAG_RE matches: a lone lowercase letter other than
#    a/e/s, the only ones that legitimately stand alone in Hungarian or
#    English prose (verified: 0 hits across every existing ledger, 28 in the
#    corrupted file). The substitution never fires just once, so a real hit
#    always leaves several fragments even though any single one of them
#    could in principle be innocent.
#  - a "%" + 3 digits with no closing marker: "%141ll%141spontja" for
#    "álláspontja", "v%151gig" for "végig". Found 2026-08-02 in daancrypto's
#    generate_current_view() output. Neither existing alternative caught it:
#    "#\d{1,3};" needs the trailing ";", and letter-punct-letter needs a
#    LETTER after the "%", not a digit. Unlike the "#\d;" placeholder this one
#    is a stable per-char code (octal of the codepoint minus 128: á -> 141),
#    but it is still detected-for-retry rather than decoded -- one clean retry
#    is cheaper than trusting a reverse-engineered encoding. Must be anchored
#    to an ADJACENT LETTER: a bare "%\d{2,3}" also matches the Turkish
#    percent-first notation ("%70 olasılıkla", "%100 kârla") that @CelalKucuker
#    (removed 2026-08-27) used to tweet and monitor.py's own extraction quoted
#    back into trades.json (47 such hits in the existing ledger, vs 0 for the
#    anchored form).
# None of these are legitimate mid-word occurrences in Hungarian or English
# prose, so a hit is an unambiguous corruption signal.
_MANGLED_RE = re.compile(
    "[A-Za-zÀ-ÿ][)!:<#%][A-Za-zÀ-ÿ]"
    "|[A-Za-z]'[aeiouAEIOU]"
    "|#\\d{1,3};"
    "|[A-Za-zÀ-ÿ]%\\d{3}|%\\d{3}[A-Za-zÀ-ÿ]")
_MANGLED_CTRL_RE = re.compile(r"[\x00-\x1f]")   # full C0 range, no carve-outs
_MANGLED_FRAG_RE = re.compile(r"(?<!\S)[b-df-rt-z](?!\S)")  # lowercase except a/e/s


def _looks_mangled(obj):
    if isinstance(obj, str):
        return bool(_MANGLED_RE.search(obj) or _MANGLED_CTRL_RE.search(obj)
                    or _MANGLED_FRAG_RE.search(obj))
    if isinstance(obj, list):
        return any(_looks_mangled(v) for v in obj)
    if isinstance(obj, dict):
        return any(_looks_mangled(v) for v in obj.values())
    return False


def _parse_gemini_json(resp):
    try:
        return json.loads(resp.text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _gemini_usage(resp):
    u = resp.usage_metadata
    return (u.prompt_token_count or 0,
            (u.candidates_token_count or 0) + (u.thoughts_token_count or 0))


class Interpreter:
    """Wraps the Gemini client and tracks token usage for cost reporting."""

    def __init__(self):
        self.gemini_client = genai.Client()   # reads GOOGLE_API_KEY from env
        self.calls = 0
        self.errors = 0
        self.input_tokens = 0
        self.output_tokens = 0
        # Vision usage tracked separately for cost reporting (different model).
        self.vision_calls = 0
        self.vision_input_tokens = 0
        self.vision_output_tokens = 0

    def extract(self, text, account, tweet_date):
        """Return the parsed signal dict, or None on API/parse error."""
        user = (f"Posted by @{account}\nTweet date: {tweet_date}\n"
                f"Tweet:\n{text}")
        try:
            resp = self.gemini_client.models.generate_content(
                model=MODEL,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=EXTRACTION_SYSTEM,
                    response_mime_type="application/json",
                    response_json_schema=SIGNAL_SCHEMA,
                    thinking_config=EXTRACT_THINKING,
                    max_output_tokens=300,
                ),
            )
        except genai_errors.APIError as e:
            print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
            self.errors += 1
            return None

        self.calls += 1
        in_tok, out_tok = _gemini_usage(resp)
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        return _parse_gemini_json(resp)

    def extract_chart(self, media_urls, text, account, tweet_date):
        """Run a Gemini vision pass over the tweet's chart image(s). Returns the
        parsed chart dict, or None if no image could be fetched / on error."""
        parts = [p for p in (_image_block(u)
                             for u in media_urls[:MAX_VISION_IMAGES]) if p]
        if not parts:
            return None
        contents = parts + [f"Posted by @{account}\nTweet date: {tweet_date}\n"
                            f"Tweet:\n{text}"]
        try:
            resp = self.gemini_client.models.generate_content(
                model=VISION_MODEL,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    system_instruction=VISION_SYSTEM,
                    response_mime_type="application/json",
                    response_json_schema=CHART_SCHEMA,
                    thinking_config=GEMINI_THINKING,
                    max_output_tokens=300,
                ),
            )
        except genai_errors.APIError as e:
            print(f"  [vision error] {type(e).__name__}: {e}", file=sys.stderr)
            return None
        self.vision_calls += 1
        in_tok, out_tok = _gemini_usage(resp)
        self.vision_input_tokens += in_tok
        self.vision_output_tokens += out_tok
        return _parse_gemini_json(resp)

    def cost(self):
        return (self.input_tokens / 1_000_000 * EXTRACT_INPUT_PER_1M
                + self.output_tokens / 1_000_000 * EXTRACT_OUTPUT_PER_1M)

    def vision_cost(self):
        return (self.vision_input_tokens / 1_000_000 * GEMINI_INPUT_PER_1M
                + self.vision_output_tokens / 1_000_000 * GEMINI_OUTPUT_PER_1M)


# Exit-phrasing detector for the reply gate below (currently unreachable: every
# monitored account is "influencer" kind, which already bypasses that gate —
# kept in case a non-influencer account is ever added back). Word-boundary
# regex, not bare substrings: "cut" as a substring matched "exeCUTe"/"hairCUT".
# Generous on purpose — a false positive only costs one Gemini call.
SELL_PATTERN = re.compile(
    r"\b(sold|selling|sell|dumped|dumping|exited|exiting|exit|"
    r"trimmed|trimming|closed|closing|liquidated|liquidating|"
    r"out of|cut|stopped out|scaled out|scaling out|reduced|reducing|"
    r"took (?:some )?profits?|taking (?:some )?profits?|take profits?)\b",
    re.IGNORECASE)

# Retweet detector for the cross-account dedup gate. A retweet is a verbatim
# copy of another account's tweet, so when the ORIGINAL author is itself a
# monitored account we already ingest that tweet directly — re-interpreting the
# RT only creates a duplicate signal. The drop is SCOPED to monitored authors:
# RTs of NON-monitored accounts are KEPT, since they can be the sole source of
# a signal. .match() anchors at string start.
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
    # Influencer chart-image pass (Gemini vision). Runs when the tweet carries a
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
    # Request gzip: the uncompressed body (~25KB w/ Content-Length) is truncated
    # mid-stream by the upstream Caddy proxy (200 OK headers sent, then the body
    # connection drops -> IncompleteRead). The gzip body is ~6KB and chunked
    # (no Content-Length), so it survives the flaky link. Vary: Accept-Encoding.
    req.add_header("Accept-Encoding", "gzip")
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
    return json.loads(body)


# GetXAPI is an unaffiliated scraper with no SLA: its upstream proxy
# intermittently truncates responses mid-body (http.client.IncompleteRead) or
# drops the connection (RemoteDisconnected). Without retry, ONE such blip on any
# account aborts the whole monitor run and the state is left unwritten, so the
# next cron retries from scratch -- a flaky proxy then keeps the pipeline down
# for many cycles (observed: 6 consecutive failed runs / ~24h). Retry transient
# read/network errors with a short linear backoff; permanent 4xx (except 429)
# raise immediately. Mirrors twitter_digest._getxapi_get_retry.
# 2026-07-13: bumped 3->5 after twitter_digest's kendrick_sc (search endpoint,
# bigger/less-reliably-compressed payloads than this module's timeline fetch)
# twice exhausted 3 attempts and paged Telegram. This module's own timeline
# fetch has never needed more than 2 of its 3 attempts in practice, so the
# higher ceiling costs it nothing -- kept in sync rather than diverging the
# two "mirrored" retry wrappers.
GETXAPI_RETRIES = 5
GETXAPI_BACKOFF_S = 3
_TRANSIENT_NET = (http.client.HTTPException, OSError)   # OSError covers URLError


def getxapi_get_retry(url):
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
        # GetXAPI sometimes returns text HTML-escaped (e.g. "S&amp;P500");
        # unescape so it doesn't leak into the LLM prompt, the stored ledger,
        # and the dashboard.
        "text": html.unescape(tw.get("text", "")),
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
        data = getxapi_get_retry(
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="interpret local tweets_*.json instead of fetching X")
    ap.add_argument("--backfill-batch", action="store_true",
                    help="alias for --backfill (kept for the existing crontab; "
                         "the Anthropic Batch API this once used is retired)")
    ap.add_argument("--batch", action="store_true",
                    help="accepted for the existing crontab; a no-op now (Gemini "
                         "calls always run real-time -- see CLAUDE.md)")
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
    args = ap.parse_args()
    if args.backfill_batch:            # --backfill-batch is now just --backfill
        args.backfill = True
    if args.batch:
        print("note: --batch no longer submits a batch job (Gemini calls run "
              "real-time); flag kept for crontab compatibility.")

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
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY is not set. Add it to ~/pilot_trader/.env "
              "and re-run.", file=sys.stderr)
        sys.exit(1)
    if not args.backfill:
        need = "GETXAPI_KEY" if args.source == "getxapi" else "X_BEARER_TOKEN"
        if not os.environ.get(need):
            print(f"{need} is not set. Add it to ~/pilot_trader/.env and re-run.",
                  file=sys.stderr)
            sys.exit(1)

    interp = Interpreter()

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
    fetch_failures = []      # accounts whose GetXAPI fetch failed after retries
    attempted = 0            # accounts that reached the fetch (not slow-skipped)
    now = datetime.now(timezone.utc)
    for account in accounts:
        # High-water mark of tweet ids already PROCESSED in a prior run. GetXAPI
        # returns the full latest page every run (no server-side since_id), so
        # without this every non-signal tweet on the page is re-sent to the LLM
        # each run (only SIGNAL-bearing tweets land in trades.json -> seen_ids).
        # newest_id is advanced inside tweets_for_account below, so capture it
        # first. Live mode only: backfill/dry-run must scan the whole snapshot.
        prior_newest = None
        if not (args.backfill or args.dry_run):
            prior_newest = (run_state.get(account) or {}).get("newest_id")
        attempted += 1
        try:
            tweets, reads, calls = tweets_for_account(
                account, run_state, args.backfill, args.source)
        except urllib.error.HTTPError as e:
            print(f"[{account}] HTTP {e.code}: "
                  f"{e.read().decode('utf-8', 'replace')}", file=sys.stderr)
            fetch_failures.append(account)
            continue
        except (http.client.HTTPException, OSError) as e:
            # Exhausted-retry transient GetXAPI error (IncompleteRead /
            # RemoteDisconnected) on ONE account: skip just this account so a
            # single flaky upstream doesn't abort the whole run (which would
            # discard candidates already extracted from earlier accounts and
            # leave state unwritten). Mirrors twitter_digest's per-feed isolation.
            print(f"[{account}] fetch failed after retries "
                  f"({type(e).__name__}: {e}); skipping account", file=sys.stderr)
            fetch_failures.append(account)
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
            sig = build_signal(account, tw, interp)
            if sig:
                all_new.append(sig)
                seen_ids.add(tw["id"])
                new += 1
        total_skipped += skipped
        total_sell_cand += sell_cand
        print(f"[{account}] scanned {len(tweets)}, already-seen-skipped {seen}, "
              f"foreign-author-skipped {foreign}, retweet-skipped {retweet}, "
              f"reply-skipped {skipped}, "
              f"reply-sell-candidate {sell_cand}, new signals {new}")

    # If EVERY attempted account failed to fetch, this is a total GetXAPI outage:
    # defer the whole run (leave state unwritten so the next run refetches via the
    # high-water gate) and alert, instead of writing a fresh _last_run that would
    # mask the outage as a healthy run on the dashboard.
    if attempted and len(fetch_failures) == attempted:
        msg = (f"all {attempted} account fetch(es) failed after retries "
               f"({', '.join(fetch_failures)}) -- deferring run")
        print(f"ERROR: {msg}", file=sys.stderr)
        if not args.dry_run:
            notify_telegram(f"monitor: {msg}")
        return
    if fetch_failures:
        print(f"WARNING: {len(fetch_failures)}/{attempted} account(s) failed "
              f"fetch after retries, continuing with the rest: "
              f"{', '.join(fetch_failures)}", file=sys.stderr)

    # extract() swallows genai_errors.APIError per-call and returns None, so a
    # total Gemini outage (bad key, denied project, exhausted quota) still lets
    # the run "succeed" and refresh _last_run -- the staleness alert never
    # fires. Mirror the GetXAPI-outage alert above: every attempted call failed
    # and none succeeded this run.
    if interp.errors and not interp.calls and not args.dry_run:
        notify_telegram(
            f"monitor: all {interp.errors} Gemini text-extraction call(s) "
            f"failed this run -- check GOOGLE_API_KEY/quota")

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
    print(f"LLM text calls (Gemini {MODEL}): {interp.calls}  "
          f"(input {interp.input_tokens} tok, output {interp.output_tokens} tok)"
          f"  ${interp.cost():.4f}")
    print(f"LLM vision calls (Gemini): {interp.vision_calls}  "
          f"(input {interp.vision_input_tokens} tok, "
          f"output {interp.vision_output_tokens} tok)  ${interp.vision_cost():.4f}")
    print(f"LLM cost this run: "
          f"${interp.cost() + interp.vision_cost():.4f}")
    if not args.backfill:
        if args.source == "getxapi":
            print(f"GetXAPI [{args.source}]: {total_reads} tweets in "
                  f"{total_calls} calls (${total_calls * GETXAPI_COST_PER_CALL:.4f})")
        else:
            print(f"Twitter reads [{args.source}]: {total_reads}  "
                  f"(${total_reads * TWITTER_COST_PER_TWEET:.4f})")

    # Append per-run cost telemetry (skip dry-run writes and zero-LLM runs).
    if not args.dry_run and (interp.calls or interp.vision_calls):
        log_cost(interp)


def log_cost(interp):
    """Append this run's token usage + cost to data/cost_log.json. Telemetry
    only: a write failure (e.g. data/ owned by the Docker user) prints a warning
    but never breaks the run."""
    rec = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "extract_input_tok": interp.input_tokens,
        "extract_output_tok": interp.output_tokens,
        "vision_input_tok": interp.vision_input_tokens,
        "vision_output_tok": interp.vision_output_tokens,
        "total_usd": round(interp.cost() + interp.vision_cost(), 6),
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


class GeminiTally:
    """Run-wide tally of Gemini calls that succeeded vs ones whose APIError was
    swallowed at the call site.

    Every Gemini helper in this project catches APIError per call and returns
    None so one bad post/video cannot kill a whole run. The cost is that a TOTAL
    outage (depleted prepay credits, revoked key, denied project) looks exactly
    like a quiet run: nothing is written, nothing raises, and the only trace is
    per-item stderr noise nobody reads. Counting both outcomes lets a run decide
    at the end whether it was quiet or blind. Used by the digests via
    alert_gemini_outage(); monitor.py's own check is inline on Interpreter."""

    def __init__(self):
        self.calls = 0
        self.errors = 0

    def ok(self):
        self.calls += 1

    def fail(self):
        self.errors += 1


# A run where every single call failed is unambiguous. A run that is merely
# mostly-failing is judged on rate, but only once enough calls have happened
# that the rate means something -- otherwise one flaky call on a 1-post night
# pages at 100%.
OUTAGE_MIN_CALLS = 5
OUTAGE_FAIL_RATE = 0.8


def alert_gemini_outage(tally, who):
    """Page Telegram when a run's Gemini calls failed wholesale. Returns True if
    it alerted. Mirrors the inline guard monitor.main() runs after extraction."""
    total = tally.calls + tally.errors
    if not tally.errors:
        return False
    total_outage = not tally.calls
    mostly = total >= OUTAGE_MIN_CALLS and tally.errors / total >= OUTAGE_FAIL_RATE
    if not (total_outage or mostly):
        return False
    scope = f"all {tally.errors}" if total_outage else f"{tally.errors}/{total}"
    msg = (f"{who}: {scope} Gemini call(s) failed this run "
           f"-- check GOOGLE_API_KEY/quota")
    print(f"ERROR: {msg}", file=sys.stderr)
    notify_telegram(msg)
    return True


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
