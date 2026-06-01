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
import json
import os
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
# Portfolio-bot accounts. @aifinancelabs is where the DeepSeek portfolio
# experiment is published (no standalone DeepSeek handle exists).
ACCOUNTS = ["grkportfolio", "theaiportfolios", "aifinancelabs", "IncomeSharks"]
# Account kind. "portfolio" = AI-run portfolio bot (its own trades / holdings).
# "influencer" = a human trader/influencer posting frequent trade calls on
# stocks AND crypto (e.g. @IncomeSharks). Influencer accounts bypass the
# reply-skip gate (every tweet+reply is sent to the LLM).
SOURCE_TYPE = {"IncomeSharks": "influencer"}
HOME = "/home/fbazsa/pilot_trader"
TRADES_FILE = os.path.join(HOME, "trades.json")
POSITIONS_FILE = os.path.join(HOME, "positions.json")
STATE_FILE = os.path.join(HOME, ".monitor_state.json")
ENV_FILE = os.path.join(HOME, ".env")
MONITOR_LOG = os.path.join(HOME, "monitor.log")
RAW_FILES = {  # used by --backfill; accounts without a snapshot are skipped
    "grkportfolio": os.path.join(HOME, "tweets_raw.json"),
    "theaiportfolios": os.path.join(HOME, "tweets_theaiportfolios.json"),
    "aifinancelabs": os.path.join(HOME, "tweets_aifinancelabs.json"),
    "IncomeSharks": os.path.join(HOME, "tweets_incomesharks.json"),
}
MAX_FETCH = 100
API_BASE = "https://api.twitter.com/2"
GETXAPI_BASE = "https://api.getxapi.com"
# tweets_and_replies (NOT the posts-only "tweets" endpoint) so sell signals
# disclosed in @-replies are included.
GETXAPI_TWEETS_PATH = "/twitter/user/tweets_and_replies"
GETXAPI_COST_PER_CALL = 0.001        # $/call (~20 tweets/page)

# Anthropic
MODEL = "claude-haiku-4-5-20251001"
TWITTER_COST_PER_TWEET = 0.005       # X API read
HAIKU_INPUT_PER_1M = 1.00            # $ / 1M input tokens
HAIKU_OUTPUT_PER_1M = 5.00           # $ / 1M output tokens

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
    "(B) HUMAN TRADER / INFLUENCER — @IncomeSharks. It posts FREQUENT trade "
    "ideas/calls on BOTH stocks AND crypto, and mixes pure analysis/opinion "
    "with actionable calls. It often states entry prices, stop losses, and price "
    "targets. For @IncomeSharks always set portfolio = null.\n"
    "Rules:\n"
    "- action: \"buy\" if the account bought/initiated/added OR (for an "
    "influencer) is calling a long entry / saying it is buying or holding long; "
    "\"sell\" if it sold/trimmed/exited/dumped OR is calling a short/exit; "
    "\"position\" if it discloses a current holding or weight without a fresh "
    "transaction; \"none\" for pure market commentary, opinion, analysis, "
    "questions, or replies about other people's trades that are NOT an "
    "actionable own trade/call.\n"
    "- ticker: the ticker symbol. For stocks resolve company names to US tickers "
    "(Broadcom->AVGO, ServiceNow->NOW, Micron->MU, etc.). For crypto use the "
    "common symbol (Bitcoin->BTC, Ethereum->ETH, Solana->SOL). null if no "
    "specific asset is the subject.\n"
    "- asset_type: \"stock\" for equities/ETFs, \"crypto\" for cryptocurrencies, "
    "\"unknown\" if unclear.\n"
    "- size_pct: position size as a percent of the portfolio/book if stated "
    "(e.g. 8.46), as a number. Do NOT use gain/return percentages. null if absent.\n"
    "- entry_price: the buy/entry price in dollars as a number, only if stated "
    "as an entry/purchase price (not a current price). null if absent.\n"
    "- stop_loss: the stop-loss price in dollars as a number if stated. null if absent.\n"
    "- target: the price target / take-profit in dollars as a number if stated. "
    "null if absent.\n"
    "- trade_date: the actual date the trade was made, as ISO YYYY-MM-DD, if the "
    "tweet states or implies one (e.g. 'I bought it April 7' => 2026-04-07; "
    "'bought in early April' => 2026-04-05; 'since May 4th' => 2026-05-04). This "
    "is the TRANSACTION date and is often EARLIER than the tweet date — resolve "
    "relative phrases against the tweet date given below and use its year. null "
    "if no trade date is stated or implied.\n"
    "- confidence: how confident you are this is a real actionable trade signal "
    "(\"high\", \"medium\", \"low\", or \"none\").\n"
    "- portfolio: for a portfolio bot, which model's portfolio the trade belongs "
    "to — \"grok\", \"claude\", \"deepseek\", or \"chatgpt\". null if unclear or "
    "for an influencer.\n"
    "- reasoning: one short sentence explaining the call.\n"
    "Return ONLY valid JSON matching the schema. No markdown, no preamble."
)

SIGNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": ["string", "null"]},
        "asset_type": {"type": "string", "enum": ["stock", "crypto", "unknown"]},
        "action": {"type": "string", "enum": ["buy", "sell", "position", "none"]},
        "size_pct": {"type": ["number", "null"]},
        "entry_price": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "target": {"type": ["number", "null"]},
        "trade_date": {"type": ["string", "null"]},
        "confidence": {"type": "string",
                       "enum": ["high", "medium", "low", "none"]},
        "portfolio": {"anyOf": [
            {"type": "string", "enum": ["grok", "claude", "deepseek", "chatgpt"]},
            {"type": "null"},
        ]},
        "reasoning": {"type": "string"},
    },
    "required": ["ticker", "asset_type", "action", "size_pct", "entry_price",
                 "stop_loss", "target", "trade_date",
                 "confidence", "portfolio", "reasoning"],
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

    def cost(self):
        return (self.input_tokens / 1_000_000 * HAIKU_INPUT_PER_1M
                + self.output_tokens / 1_000_000 * HAIKU_OUTPUT_PER_1M)


SELL_VERBS = ("sold", "dumped", "exited", "trimmed", "closed", "out of",
              "selling")


def is_reply(text):
    return text.lstrip().startswith("@")


def reply_has_sell_verb(text):
    """A reply worth keeping: it mentions a sell so we don't miss exits."""
    low = text.lower()
    return any(v in low for v in SELL_VERBS)


def record_from_parsed(account, tw, parsed):
    """Build a trades.json signal record from a parsed LLM result, or None if
    it isn't an actionable signal. Shared by the real-time and batch paths."""
    if not parsed:
        return None
    if parsed["action"] == "none" or not parsed.get("ticker"):
        return None
    return {
        "account": account,
        "source_type": SOURCE_TYPE.get(account, "portfolio"),
        "portfolio": parsed.get("portfolio"),
        "tweet_id": tw["id"],
        "timestamp": tw.get("created_at"),
        "signal_type": parsed["action"],
        "confidence": parsed["confidence"],
        "actionable": parsed["action"] in ("buy", "sell", "position"),
        "tickers": [parsed["ticker"]],
        "asset_type": parsed.get("asset_type", "unknown"),
        "position_size_pct": parsed["size_pct"],
        "entry_price": parsed["entry_price"],
        "stop_loss": parsed.get("stop_loss"),
        "target": parsed.get("target"),
        "trade_date": parsed.get("trade_date"),
        "reasoning": parsed["reasoning"],
        "url": f"https://x.com/{account}/status/{tw['id']}",
        "text": tw.get("text", ""),
    }


def build_signal(account, tw, interp):
    parsed = interp.extract(tw.get("text", ""), account,
                            (tw.get("created_at") or "")[:10])
    return record_from_parsed(account, tw, parsed)


def should_send_to_llm(text):
    """The pre-LLM gate: keep non-replies, and replies only if they mention a
    sell. Returns True if this tweet should be interpreted."""
    if is_reply(text):
        return reply_has_sell_verb(text)
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
    return {
        "id": str(tw.get("id")),
        "text": tw.get("text", ""),
        "created_at": created_iso,
        "is_reply": tw.get("isReply"),
    }


def fetch_getxapi(account, since_id=None):
    """Cursor-paginate GetXAPI. No since_id server-side, so stop once a page
    contains a tweet we've already seen. Returns (tweets, n_api_calls)."""
    collected, cursor, calls = [], None, 0
    while len(collected) < MAX_FETCH:
        params = {"userName": account}
        if cursor:
            params["cursor"] = cursor
        data = getxapi_get(
            f"{GETXAPI_BASE}{GETXAPI_TWEETS_PATH}?{urllib.parse.urlencode(params)}")
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
            state[account] = {"newest_id": newest}
    return tweets, len(tweets), calls


def backfill_batch(interp):
    """Interpret all local snapshot tweets via the Anthropic Batch API (50%
    cheaper). Submits one batch, polls until complete, then writes results.
    Rebuilds trades.json + positions.json from scratch (like --backfill)."""
    candidates, skipped = [], 0
    for account in ACCOUNTS:
        influencer = SOURCE_TYPE.get(account) == "influencer"
        for tw in load_json(RAW_FILES.get(account, ""), []):
            if not influencer and not should_send_to_llm(tw.get("text", "")):
                skipped += 1
                continue
            candidates.append((f"{account}__{tw['id']}", account, tw))

    if not candidates:
        print("No candidate tweets to batch.")
        return
    lookup = {cid: (account, tw) for cid, account, tw in candidates}
    requests = [
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=MODEL, max_tokens=300, system=_system_blocks(),
            output_config=_output_config(),
            messages=[_user_message(account, tw.get("text", ""),
                                    (tw.get("created_at") or "")[:10])]))
        for cid, account, tw in candidates
    ]

    batch = interp.client.messages.batches.create(requests=requests)
    print(f"Submitted batch {batch.id}: {len(requests)} requests "
          f"({skipped} replies skipped). Polling...")
    while True:
        b = interp.client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        rc = b.request_counts
        print(f"  status={b.processing_status} "
              f"processing={rc.processing} succeeded={rc.succeeded} "
              f"errored={rc.errored}")
        time.sleep(20)

    signals, in_tok, out_tok, errored = [], 0, 0, 0
    for result in interp.client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            errored += 1
            continue
        msg = result.result.message
        u = msg.usage
        in_tok += u.input_tokens + (u.cache_read_input_tokens or 0) + \
            (u.cache_creation_input_tokens or 0)
        out_tok += u.output_tokens
        account, tw = lookup[result.custom_id]
        rec = record_from_parsed(account, tw, _parse_json_text(msg.content))
        if rec:
            signals.append(rec)

    signals.sort(key=lambda r: r["timestamp"], reverse=True)
    write_json_atomic(TRADES_FILE, signals)
    reconcile(TRADES_FILE, POSITIONS_FILE)

    batch_cost = (in_tok / 1e6 * HAIKU_INPUT_PER_1M
                  + out_tok / 1e6 * HAIKU_OUTPUT_PER_1M) * 0.5   # 50% off
    realtime_cost = batch_cost * 2
    by = {}
    for s in signals:
        key = (s["account"], s["signal_type"])
        by[key] = by.get(key, 0) + 1
    print(f"\nBatch complete: {len(signals)} signals "
          f"({len(requests)} requests, {errored} errored).")
    for (acct, st), c in sorted(by.items()):
        print(f"  {acct:16} {st:9} {c}")
    print(f"\nTokens: input {in_tok}, output {out_tok}")
    print(f"Batch API cost: ${batch_cost:.4f} (50% off)")
    print(f"  vs real-time: ${realtime_cost:.4f}  -> saved ${realtime_cost - batch_cost:.4f}")
    print(f"Saved -> {TRADES_FILE} + {POSITIONS_FILE}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true",
                    help="interpret local tweets_*.json instead of fetching X")
    ap.add_argument("--backfill-batch", action="store_true",
                    help="like --backfill but via the Anthropic Batch API (50% "
                         "cheaper); submits one job and polls to completion")
    ap.add_argument("--source", choices=["official", "getxapi"], default="getxapi",
                    help="tweet backend: getxapi (default, tweets_and_replies) "
                         "or official X API")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch fresh + analyze, but write NOTHING (no trades.json, "
                         "positions.json, or state). Dumps would-be signals to "
                         "/tmp/pilot_dryrun_<source>.json for comparison.")
    args = ap.parse_args()

    load_env(ENV_FILE)
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
    # Backfill rebuilds trades.json from the local snapshots, so it starts from a
    # clean slate (otherwise dedup against prior signals would skip every tweet).
    # Dry-run also starts clean and ignores stored state so it fetches a fresh,
    # comparable batch from the live API (otherwise since_id would return ~nothing).
    existing = [] if (args.backfill or args.dry_run) else load_json(TRADES_FILE, [])
    seen_ids = {r["tweet_id"] for r in existing}
    run_state = {} if args.dry_run else state    # throwaway state in dry-run

    all_new, total_reads, total_skipped, total_sell_cand, total_calls = \
        [], 0, 0, 0, 0
    for account in ACCOUNTS:
        try:
            tweets, reads, calls = tweets_for_account(
                account, run_state, args.backfill, args.source)
        except urllib.error.HTTPError as e:
            print(f"[{account}] HTTP {e.code}: "
                  f"{e.read().decode('utf-8', 'replace')}", file=sys.stderr)
            continue
        total_reads += reads
        total_calls += calls
        new, skipped, sell_cand = 0, 0, 0
        for tw in tweets:
            if tw["id"] in seen_ids:
                continue
            text = tw.get("text", "")
            # Influencer accounts (e.g. @IncomeSharks) bypass the reply gate —
            # every tweet AND reply is sent to the LLM.
            if is_reply(text) and SOURCE_TYPE.get(account) != "influencer":
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
        print(f"[{account}] scanned {len(tweets)}, reply-skipped {skipped}, "
              f"reply-sell-candidate {sell_cand}, new signals {new}")

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
    print(f"LLM calls: {interp.calls}  "
          f"(input {interp.input_tokens} tok, output {interp.output_tokens} tok)")
    print(f"LLM cost this run: ${interp.cost():.4f}")
    if not args.backfill:
        if args.source == "getxapi":
            print(f"GetXAPI [{args.source}]: {total_reads} tweets in "
                  f"{total_calls} calls (${total_calls * GETXAPI_COST_PER_CALL:.4f})")
        else:
            print(f"Twitter reads [{args.source}]: {total_reads}  "
                  f"(${total_reads * TWITTER_COST_PER_TWEET:.4f})")


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
    except SystemExit:
        raise
    except BaseException as exc:   # log anything, then surface a non-zero exit
        _log_failure(exc)
        print(f"monitor.py FAILED: {exc!r} "
              f"(traceback appended to {MONITOR_LOG})", file=sys.stderr)
        sys.exit(1)
