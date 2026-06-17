#!/usr/bin/env python3
"""
reddit_miner.py - daily Reddit trading-strategy miner.

Pipeline (mirrors monitor.py's "scrape -> LLM -> json" shape, for Reddit posts):

  RedditAPI top-of-week posts per subreddit       -> candidate posts
   -> claude-haiku-4-5 structured analysis (is this a CONCRETE strategy?),
      submitted as ONE Batch API job per run (50% cheaper than synchronous)
   -> data/reddit_strategies.json  (append-only event log, one record/strategy)

A post counts as a strategy only if it clearly carries >=3 of: a specific entry
condition, a specific exit condition, a named asset/ticker, a timeframe, a
backtest result / win rate, code or a formula, risk-management details. The
Haiku call applies that rule and returns is_strategy + confidence + the
structured fields.

Dedup: .seen_reddit_ids.json holds every post_id already analyzed (strategy or
not) -- the same high-water idea monitor.py uses for tweet ids, so reruns / the
daily cron never re-send a post to the LLM. Only NEW posts cost tokens.

  python scripts/reddit_miner.py                        # default subs, top/week
  python scripts/reddit_miner.py --limit 5              # cap posts/sub (testing)
  python scripts/reddit_miner.py --subreddits algotrading CryptoMarkets
  python scripts/reddit_miner.py --min-confidence 0.7   # stricter keep gate
  python scripts/reddit_miner.py --dry-run              # analyze + print, no write

Requires REDDITAPI_TOKEN + ANTHROPIC_API_KEY in ~/pilot_trader/.env. Run with the
project venv: /home/fbazsa/pilot_trader/.venv/bin/python scripts/reddit_miner.py
"""
import argparse
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

# reddit_miner lives in scripts/; put the repo root on sys.path so the shared
# monitor.py / reconcile.py helpers import cleanly when run as
# `python scripts/reddit_miner.py` (same approach as scripts/audit_truecrypto.py).
sys.path.insert(0, "/home/fbazsa/pilot_trader")
from reconcile import write_json_atomic                       # noqa: E402
from monitor import (load_env, load_json, notify_telegram,    # noqa: E402
                     TELEGRAM_ENVS)

# --- config ----------------------------------------------------------------
HOME = "/home/fbazsa/pilot_trader"
DATA_DIR = os.path.join(HOME, "data")
STRATEGIES_FILE = os.path.join(DATA_DIR, "reddit_strategies.json")
# Seen-id ledger (every analyzed post_id, strategy or not) so the LLM never
# re-scores a post -- the Reddit analogue of monitor's .monitor_state.json.
SEEN_FILE = os.path.join(HOME, ".seen_reddit_ids.json")

# RedditAPI (unofficial, key-gated; no SLA -- parse defensively, like GetXAPI).
REDDITAPI_BASE = "https://api.redditapis.com"
REDDITAPI_POSTS_PATH = "/api/reddit/posts"
DEFAULT_SUBREDDITS = ["algotrading", "CryptoMarkets", "BitcoinMarkets",
                      "ethtrader", "technicalanalysis"]
DEFAULT_LIMIT = 25                    # posts/sub (RedditAPI page size)
SORT = "top"
TIMEFRAME = "week"                    # top-of-week: high-signal, low-noise

# Anthropic. Haiku 4.5 (the same text model the tweet pipeline uses) -- cheap,
# and strategy detection is a structured text-classification task it handles
# well. Vision is never needed here (text posts only).
MODEL = "claude-haiku-4-5-20251001"
HAIKU_INPUT_PER_1M = 1.00             # $ / 1M input tokens
HAIKU_OUTPUT_PER_1M = 5.00            # $ / 1M output tokens
# Cap post body sent to the model; long DD write-ups can run huge. ~16k chars
# ~= 4k tokens, ample for the strategy details while bounding per-post cost.
MAX_POST_CHARS = 16_000
# Keep gate: a strategy is written only if confidence >= this (override with
# --min-confidence). is_strategy already encodes the >=3-criteria rule.
DEFAULT_MIN_CONFIDENCE = 0.6

# Batch API: Haiku requests are billed 50% cheaper, but asynchronous -- we submit
# ONE job for all new posts and block-poll it to completion (same shape as
# monitor.py's tweet batch). Reddit's top/week posts stay fetchable for days, so
# a run whose batch times out just re-batches them next time -- no real-time
# fallback needed (unlike monitor's tweets, which advance past a high-water id).
BATCH_POLL_INTERVAL_S = 20            # seconds between batch status polls
BATCH_MAX_WAIT_S = 55 * 60            # defer to next run if a batch exceeds this

# --- LLM analysis ----------------------------------------------------------
ANALYSIS_SYSTEM = (
    "You triage a post from a trading subreddit (algorithmic trading, stocks, "
    "crypto, technical analysis). The bar is REPLICABILITY: we only keep "
    "strategies specific enough that a competent quant could code them up and "
    "run a backtest using ONLY this post -- with ZERO follow-up questions to the "
    "author. Vague market opinion, price predictions, news, memes, beginner "
    "questions, screenshots with no rules, and 'trust me / DM me' brags are NOT "
    "what we want.\n"
    "STEP 1 -- is_strategy: true if the post describes a rule-based, repeatable "
    "trading METHOD (an entry idea plus an exit / management idea on some asset "
    "or universe), even if under-specified. False for pure commentary, a price "
    "prediction, a question, a one-off 'I bought X today' call with no reusable "
    "rule, or a promo.\n"
    "STEP 2 -- replicable: the REAL filter. Set true ONLY if EVERY rule needed "
    "to trade it mechanically is fully and unambiguously stated in the post:\n"
    "  * ENTRY -- an exact, testable trigger naming the actual indicators, "
    "parameters and thresholds (e.g. 'go long when RSI(14) crosses above 30 "
    "while price > EMA(200)'). NOT 'buy when oversold', 'when it looks strong', "
    "'on the breakout', 'when the trend is up'.\n"
    "  * EXIT -- a concrete take-profit AND a stop / exit rule, as a number, %, "
    "ATR multiple, indicator cross, or time stop. NOT 'sell when weak' or 'take "
    "profit along the way'.\n"
    "  * UNIVERSE -- which asset(s), or exactly how they are screened / "
    "selected.\n"
    "  * TIMEFRAME -- the bar size / holding horizon.\n"
    "  * Every parameter the rules reference (lookbacks, bands, filters, and "
    "sizing if it changes the signal) must have a stated value.\n"
    "If ANY of these is missing, vague, or would make you GUESS or ask the "
    "author, set replicable = false. A headline backtest result with no rules is "
    "NOT replicable; an idea 'optimized by a genetic algorithm / ML' whose final "
    "rules are not printed is NOT replicable. Code or a closed-form formula "
    "posted in full DOES count as fully specified.\n"
    "STEP 3 -- missing_for_replication: the specific things you would have to ask "
    "the author to actually code / backtest it (e.g. 'exact RSI period', "
    "'stop-loss rule', 'which coins', 'entry threshold'). Empty list IFF "
    "replicable is true.\n"
    "Fields:\n"
    "- is_strategy, replicable: bools per the steps above.\n"
    "- confidence: 0.0-1.0 that this is a real strategy AND replicable exactly "
    "as written. Keep it BELOW 0.5 whenever replicable is false.\n"
    "- replication_effort: work to implement -- 'low' (drop-in rules or posted "
    "code), 'medium' (a few indicators to wire up), 'high' (many parts, custom "
    "data, or ML).\n"
    "- summary: 1-2 sentences stating the mechanical strategy (asset, entry, "
    "exit, timeframe). If not a strategy, say what the post is instead.\n"
    "- entry: the entry trigger, quoted precisely from the post, else null.\n"
    "- exit: the exit / stop / target rule, precisely, else null.\n"
    "- asset: primary ticker / symbol in UPPERCASE, or the universe in a few "
    "words, else null.\n"
    "- asset_class: 'stock', 'crypto', 'forex', 'futures', 'options', 'multi', "
    "or 'unknown'.\n"
    "- direction: 'long', 'short', 'both', or 'none'.\n"
    "- timeframe: the trading timeframe / bar size, else null.\n"
    "- indicators: the concrete indicators + parameters the rules use (e.g. "
    "'RSI(14)', 'EMA(200)', 'ATR(14)', 'MACD(12,26,9)'); empty list if none "
    "named.\n"
    "- has_backtest: true if a backtest result or win rate is given.\n"
    "- has_code: true if code, pseudocode, or an explicit formula is present.\n"
    "- performance_claim: the author's claimed edge, verbatim-ish (e.g. 'win "
    "rate 62% over 1,200 trades', '+504% 5y on trained coins', 'Sharpe 1.8'), "
    "else null.\n"
    "- red_flags: reasons to distrust it -- e.g. 'no out-of-sample test', "
    "'overfit / curve-fit', 'in-sample results only', 'no sample size', "
    "'paywalled / promo', 'cherry-picked screenshots', 'survivorship bias', "
    "'look-ahead'. Empty list if none.\n"
    "- tags: 3-6 short lowercase topic tags (e.g. 'mean-reversion', 'rsi', "
    "'breakout', 'options', 'risk-management').\n"
    "When in doubt about whether a rule is fully specified, treat it as NOT "
    "replicable. Return ONLY valid JSON matching the schema. No markdown."
)

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "is_strategy": {"type": "boolean"},
        "replicable": {"type": "boolean"},
        "confidence": {"type": "number"},
        "replication_effort": {"type": "string",
                               "enum": ["low", "medium", "high"]},
        "summary": {"type": "string"},
        "entry": {"type": ["string", "null"]},
        "exit": {"type": ["string", "null"]},
        "asset": {"type": ["string", "null"]},
        "asset_class": {"type": "string",
                        "enum": ["stock", "crypto", "forex", "futures",
                                 "options", "multi", "unknown"]},
        "direction": {"type": "string",
                      "enum": ["long", "short", "both", "none"]},
        "timeframe": {"type": ["string", "null"]},
        "indicators": {"type": "array", "items": {"type": "string"}},
        "has_backtest": {"type": "boolean"},
        "has_code": {"type": "boolean"},
        "performance_claim": {"type": ["string", "null"]},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
        "missing_for_replication": {"type": "array",
                                    "items": {"type": "string"}},
    },
    "required": ["is_strategy", "replicable", "confidence",
                 "replication_effort", "summary", "entry", "exit", "asset",
                 "asset_class", "direction", "timeframe", "indicators",
                 "has_backtest", "has_code", "performance_claim", "red_flags",
                 "tags", "missing_for_replication"],
    "additionalProperties": False,
}


def signal_count(a):
    """Count the strategy signals visible in the schema (6 of the prompt's 7
    criteria; the 7th -- risk-management -- is judged by the model and surfaced
    via tags). Stored on each record for transparency into the >=3 rule; NOT a
    hard gate (the model's is_strategy is authoritative, as it also weighs
    risk-management)."""
    return (sum(bool(a.get(k)) for k in ("entry", "exit", "asset", "timeframe"))
            + bool(a.get("has_backtest")) + bool(a.get("has_code")))


def _parse_json(content_blocks):
    block = next((b.text for b in content_blocks if b.type == "text"), None)
    if not block:
        return None
    try:
        return json.loads(block)
    except json.JSONDecodeError:
        return None


def _system_blocks():
    # cache_control marks the (fixed) system prompt for prompt caching. NOTE:
    # Haiku 4.5's minimum cacheable prefix is 4096 tokens and ANALYSIS_SYSTEM is
    # only ~530, so this is a harmless no-op today (the cost line confirms
    # cache read/write stay 0); it activates automatically only if the prompt
    # later grows past 4096. Same marker monitor.py carries on its text prompt.
    return [{"type": "text", "text": ANALYSIS_SYSTEM,
             "cache_control": {"type": "ephemeral"}}]


def _output_config():
    return {"format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA}}


def _user_message(post):
    body = post.get("body") or ""
    if len(body) > MAX_POST_CHARS:
        body = body[:MAX_POST_CHARS] + "\n...[post truncated]"
    return {"role": "user",
            "content": (f"Subreddit: r/{post['subreddit']}\n"
                        f"Title: {post['title']}\n\n"
                        f"Body:\n{body or '(no body text)'}")}


def _build_batch_requests(candidates):
    """candidates: list of (custom_id, post). One Batch API Request per post,
    with the same model / system prompt / json_schema as the former sync call."""
    return [
        # 800 (not 500): the replicability schema can emit long
        # missing_for_replication + red_flags lists; too low a cap truncates the
        # JSON and the result fails to parse.
        Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=MODEL, max_tokens=800, system=_system_blocks(),
            output_config=_output_config(), messages=[_user_message(post)]))
        for cid, post in candidates
    ]


def _poll_batch(client, batch_id, max_wait_s=None):
    """Block-poll a submitted batch until processing_status == 'ended'. Returns
    True when ended, or False if max_wait_s elapsed first (None = wait forever).
    Same loop as monitor.py's _poll_batch."""
    waited = 0
    while True:
        b = client.messages.batches.retrieve(batch_id)
        if b.processing_status == "ended":
            return True
        if max_wait_s is not None and waited >= max_wait_s:
            return False
        rc = b.request_counts
        print(f"  status={b.processing_status} processing={rc.processing} "
              f"succeeded={rc.succeeded} errored={rc.errored}")
        time.sleep(BATCH_POLL_INTERVAL_S)
        waited += BATCH_POLL_INTERVAL_S


def _collect_batch(client, batch_id, lookup):
    """Stream a completed batch's results. Returns (parsed, errored, in_tok,
    out_tok, cache_read, cache_create): parsed is [(post, analysis_dict_or_None),
    ...] for SUCCEEDED requests (None when the JSON failed to parse); errored is
    the custom_ids whose request did not succeed -- left un-seen so the next run
    re-batches them. in_tok folds the cache tokens in (for cost); cache_read /
    cache_create are also returned SEPARATELY so the cost line can confirm
    whether prompt caching actually fired."""
    parsed, errored = [], []
    in_tok = out_tok = cache_read = cache_create = 0
    for result in client.messages.batches.results(batch_id):
        if result.result.type != "succeeded":
            errored.append(result.custom_id)
            continue
        msg = result.result.message
        u = msg.usage
        cr = u.cache_read_input_tokens or 0
        cc = u.cache_creation_input_tokens or 0
        in_tok += u.input_tokens + cr + cc
        out_tok += u.output_tokens
        cache_read += cr
        cache_create += cc
        parsed.append((lookup[result.custom_id], _parse_json(msg.content)))
    return parsed, errored, in_tok, out_tok, cache_read, cache_create


def _cache_line(cache_read, cache_create):
    """One-line cache telemetry for the cost summary. Both stay 0 until
    ANALYSIS_SYSTEM exceeds Haiku's 4096-token cache minimum (see _system_blocks);
    the next real run will show non-zero here if caching ever starts firing."""
    note = ("  (no-op: prompt < Haiku 4096-tok minimum)"
            if cache_read == 0 and cache_create == 0 else "")
    return f"  cache: read={cache_read} write={cache_create}{note}"


def submit_batch(client, candidates):
    """Submit all new posts as ONE Batch API job and block-poll it (bounded by
    BATCH_MAX_WAIT_S). Returns _collect_batch's 6-tuple (parsed, errored, in_tok,
    out_tok, cache_read, cache_create), or None if the batch did not finish in
    time -- the caller then defers (writes nothing; the posts stay un-seen and
    are re-batched next run). Mirrors monitor.py's collect_live_batch, minus the
    tweet-only vision pass."""
    lookup = {cid: post for cid, post in candidates}
    batch = client.messages.batches.create(
        requests=_build_batch_requests(candidates))
    print(f"Submitted batch {batch.id}: {len(candidates)} requests. "
          f"Polling (max {BATCH_MAX_WAIT_S // 60} min)...")
    if not _poll_batch(client, batch.id, BATCH_MAX_WAIT_S):
        # Cancel the abandoned batch so its tokens aren't billed for results we
        # never collect (the next run submits a fresh one) -- same guard as
        # monitor.py.
        try:
            client.messages.batches.cancel(batch.id)
        except Exception as e:                        # noqa: BLE001 - best-effort
            print(f"  (cancel of abandoned batch failed: {e})", file=sys.stderr)
        print(f"Batch {batch.id} did not finish within {BATCH_MAX_WAIT_S // 60} "
              f"min - deferring to the next run.", file=sys.stderr)
        notify_telegram(f"reddit_miner batch {batch.id} exceeded "
                        f"{BATCH_MAX_WAIT_S // 60}min; cancelled + deferred")
        return None
    return _collect_batch(client, batch.id, lookup)


# --- RedditAPI fetch -------------------------------------------------------
def _as_list(payload):
    """RedditAPI's exact envelope is not contractually fixed (unofficial), so
    accept the common shapes: a bare list, {posts|results|items: [...]}, or
    Reddit's native {data: {children: [{data: {...}}]}}."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("posts", "results", "items"):
        if isinstance(payload.get(key), list):
            return payload[key]
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("children"), list):
        return [c.get("data", c) for c in data["children"]]
    return []


def _first(d, *keys):
    """First non-empty value among keys, else None (field-name variance guard)."""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return None


def _to_iso(ts):
    """Reddit timestamps are usually epoch seconds; pass ISO strings through."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat()
    return str(ts)


def _normalize(raw, subreddit):
    """Map one RedditAPI post object to our flat record shape, tolerating
    field-name variance. Returns None if it has no usable id."""
    if not isinstance(raw, dict):
        return None
    pid = _first(raw, "id", "post_id", "name")
    if not pid:
        return None
    pid = str(pid).split("_", 1)[-1]          # strip Reddit "t3_" fullname prefix
    author = _first(raw, "author") or "unknown"
    if isinstance(author, dict):
        author = author.get("name") or "unknown"
    permalink = _first(raw, "permalink")
    url = _first(raw, "url", "link") or (
        f"https://www.reddit.com{permalink}" if permalink
        else f"https://redd.it/{pid}")
    return {
        "post_id": pid,
        "subreddit": subreddit,
        "author": author,
        "title": (_first(raw, "title") or "").strip(),
        "url": url,
        "upvotes": _first(raw, "ups", "score", "upvotes") or 0,
        "posted_at": _to_iso(_first(raw, "created_utc", "created", "created_at")),
        "body": _first(raw, "selftext", "self_text", "body", "text",
                       "content") or "",
    }


def fetch_posts(subreddit, token, limit):
    """Return normalized post dicts for one subreddit (in RedditAPI's top order).
    Raises urllib HTTPError/URLError on a transport failure so the caller can
    skip just this subreddit, matching monitor.py's per-account handling."""
    qs = urllib.parse.urlencode({"subreddit": subreddit, "sort": SORT,
                                 "t": TIMEFRAME, "limit": limit})
    req = urllib.request.Request(
        f"{REDDITAPI_BASE}{REDDITAPI_POSTS_PATH}?{qs}",
        headers={"Authorization": f"Bearer {token}",
                 "User-Agent": "pilot_trader/reddit_miner"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.load(resp)
    out = []
    for raw in _as_list(payload):
        p = _normalize(raw, subreddit)
        if p:
            out.append(p)
    return out


# --- main ------------------------------------------------------------------
def gather_candidates(subreddits, token, seen, limit):
    """Fetch each subreddit and return [(custom_id, post), ...] for posts not in
    `seen` (deduped by id across subs). custom_id = '<sub>__<post_id>' -- unique
    and within the Batch API's id charset."""
    candidates, queued = [], set()
    for sub in subreddits:
        try:
            posts = fetch_posts(sub, token, limit)
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"[r/{sub}] fetch failed ({getattr(e, 'code', e)}); skipping",
                  file=sys.stderr)
            continue
        new = 0
        for p in posts:
            pid = p["post_id"]
            if pid in seen or pid in queued:
                continue
            queued.add(pid)
            candidates.append((f"{sub}__{pid}", p))
            new += 1
        print(f"[r/{sub}] {len(posts)} posts, {new} new")
    return candidates


def build_records(parsed, min_confidence):
    """Filter batch results to qualifying strategies and return (records,
    n_analyzed). The gate is REPLICABILITY: is_strategy AND replicable AND
    confidence >= min_confidence. Strategies that are NOT replicable (the loose
    old >=3-signals bar kept many of these) are logged with their gaps and
    dropped. Parse failures are dropped (still marked seen)."""
    records, analyzed = [], 0
    for post, analysis in parsed:
        if not analysis:
            print(f"  [{post['post_id']}] parse failed; skipping",
                  file=sys.stderr)
            continue
        analyzed += 1
        if not analysis.get("is_strategy"):
            continue
        if not analysis.get("replicable"):
            gaps = "; ".join(analysis.get("missing_for_replication") or [])
            print(f"  [{post['post_id']}] strategy but NOT replicable"
                  + (f" (needs: {gaps[:100]})" if gaps else "") + "; skipped")
            continue
        conf = analysis.get("confidence") or 0.0
        if conf < min_confidence:
            print(f"  [{post['post_id']}] replicable but low confidence "
                  f"({conf:.2f} < {min_confidence}); skipped")
            continue
        records.append({
            "post_id": post["post_id"],
            "subreddit": post["subreddit"],
            "author": post["author"],
            "title": post["title"],
            "url": post["url"],
            "upvotes": post["upvotes"],
            "posted_at": post["posted_at"],
            "found_at": datetime.now(timezone.utc).isoformat(),
            "signal_count": signal_count(analysis),
            **analysis,
        })
        flags = analysis.get("red_flags") or []
        print(f"  [{post['post_id']}] STRATEGY "
              f"[{analysis.get('replication_effort', '?')} effort] "
              f"conf={conf:.2f} {analysis.get('asset') or '?'}"
              + (f" flags={len(flags)}" if flags else "")
              + f": {analysis['summary'][:64]}")
    return records, analyzed


def main():
    ap = argparse.ArgumentParser(description="Reddit trading-strategy miner")
    ap.add_argument("--subreddits", nargs="+", metavar="SUB",
                    default=DEFAULT_SUBREDDITS,
                    help="subreddits to mine (default: %(default)s)")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                    help="posts to fetch per subreddit (default: %(default)s)")
    ap.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
                    help="min model confidence to keep a strategy "
                         "(default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="submit + analyze the batch and print, but do NOT write "
                         "the strategies / seen files")
    args = ap.parse_args()

    for p in TELEGRAM_ENVS:            # loads ANTHROPIC_API_KEY + REDDITAPI_TOKEN
        load_env(p)
    missing = [k for k in ("ANTHROPIC_API_KEY", "REDDITAPI_TOKEN")
               if not os.environ.get(k)]
    if missing:
        print(f"Missing required env var(s): {', '.join(missing)}. Add them to "
              f"~/pilot_trader/.env and re-run.", file=sys.stderr)
        sys.exit(1)
    token = os.environ["REDDITAPI_TOKEN"]

    subreddits = [s.strip().strip("/").split("/")[-1] for s in args.subreddits]
    subreddits = [s for s in subreddits if s]

    seen_list = load_json(SEEN_FILE, [])
    seen = set(seen_list) if isinstance(seen_list, list) else set()
    existing = load_json(STRATEGIES_FILE, [])
    if not isinstance(existing, list):
        existing = []

    print(f"Mining {len(subreddits)} subreddit(s) top/{TIMEFRAME}, "
          f"limit {args.limit}/sub; {len(seen)} ids already seen")
    candidates = gather_candidates(subreddits, token, seen, args.limit)
    if not candidates:
        print("No new posts to analyze.")
        return

    client = anthropic.Anthropic()    # reads ANTHROPIC_API_KEY from env
    result = submit_batch(client, candidates)
    if result is None:                # batch timed out -> deferred, nothing written
        print("Batch did not complete; no writes this run.", file=sys.stderr)
        return
    parsed, errored, in_tok, out_tok, cache_read, cache_create = result
    if errored:
        print(f"  batch: {len(errored)} request(s) errored; left un-seen for "
              f"the next run.", file=sys.stderr)

    records, analyzed = build_records(parsed, args.min_confidence)
    # Mark seen only the posts the batch returned a result for; errored ones stay
    # un-seen so the next run re-batches them.
    newly_seen = {post["post_id"] for post, _ in parsed}
    # Batch Haiku is billed at 50% of the synchronous rate.
    cost = (in_tok / 1_000_000 * HAIKU_INPUT_PER_1M
            + out_tok / 1_000_000 * HAIKU_OUTPUT_PER_1M) * 0.5

    if args.dry_run:
        print(f"\n[dry-run] {len(parsed)} result(s), analyzed {analyzed}, "
              f"{len(records)} strateg(ies); no files written.")
        if records:
            print(json.dumps(records, indent=2))
        print(f"Batch Haiku tokens in={in_tok} out={out_tok} "
              f"(${cost:.4f} at 50% batch rate)")
        print(_cache_line(cache_read, cache_create))
        return

    merged = existing + records
    merged.sort(key=lambda r: r.get("found_at") or "", reverse=True)
    seen |= newly_seen
    os.makedirs(DATA_DIR, exist_ok=True)
    write_json_atomic(STRATEGIES_FILE, merged)
    write_json_atomic(SEEN_FILE, sorted(seen))

    # summary
    print(f"\nAnalyzed {analyzed} post(s); found {len(records)} strateg(ies) "
          f"(stored total: {len(merged)}).")
    by_sub = {}
    for r in records:
        by_sub[r["subreddit"]] = by_sub.get(r["subreddit"], 0) + 1
    for sub, c in sorted(by_sub.items()):
        print(f"  r/{sub:18} {c}")
    print(f"Batch Haiku tokens in={in_tok} out={out_tok} "
          f"(${cost:.4f} at 50% batch rate)")
    print(_cache_line(cache_read, cache_create))
    print(f"Wrote {len(merged)} strategies -> {STRATEGIES_FILE}")
    print(f"Seen ledger: {len(seen)} ids -> {SEEN_FILE}")


if __name__ == "__main__":
    try:
        main()
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as exc:                   # log + page, then non-zero exit
        try:
            for p in TELEGRAM_ENVS:
                load_env(p)
            notify_telegram(f"reddit_miner.py FAILED: {exc!r}")
        except Exception:
            pass
        traceback.print_exc()
        sys.exit(1)
