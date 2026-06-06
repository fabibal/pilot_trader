#!/usr/bin/env python3
"""Historical, READ-ONLY audit of a single X account (default @Truecrypto).

Analysis only. Does NOT touch trades.json / positions.json / .monitor_state.json
and does NOT add the account to monitor's ACCOUNTS. It reuses monitor.py's proven
fetch + signal extraction + vision pipeline, then layers on:
  1. per-post sentiment (bullish/bearish/neutral + confidence) for EVERY post
  2. an LLM market-view summary (BTC / alts / overall stance)
  3. top-5 recurring themes
  4. signal-quality stats (win rate + return) on the actual trade calls

Writes a full JSON report to results/audit_<account>.json.
"""
import json
import os
import statistics
import sys
import time
import urllib.parse

sys.path.insert(0, "/home/fbazsa/pilot_trader")
import monitor  # noqa: E402  (proven fetch/extract/vision helpers, read-only use)

ACCOUNT = sys.argv[1] if len(sys.argv) > 1 else "Truecrypto"
TARGET = int(os.environ.get("AUDIT_TARGET", "200"))
OUT = f"/home/fbazsa/pilot_trader/results/audit_{ACCOUNT}.json"

# Load API keys read-only (setdefault), same precedence monitor.main() uses.
for _p in (monitor.ENV_FILE, monitor.PAPER_ENV, monitor.SHARED_ENV, monitor.HOME_ENV):
    monitor.load_env(_p)

import anthropic  # noqa: E402
import yfinance as yf  # noqa: E402

# --- combined signal + sentiment extraction --------------------------------
# Reuse monitor's exact signal prompt/schema so extraction behaviour is
# identical to production, then bolt on two sentiment fields in the SAME call
# (one Haiku call per post instead of two).
SENTIMENT_ADDENDUM = (
    "- overall_sentiment: the post's overall stance on the crypto market or the "
    "asset it discusses: \"bullish\", \"bearish\", or \"neutral\". Judge this "
    "for EVERY post, including pure macro commentary/analysis where action is "
    "\"none\".\n"
    "- sentiment_confidence: how strongly that sentiment is expressed: "
    "\"high\", \"medium\", or \"low\".\n"
)
AUDIT_SYSTEM = monitor.EXTRACTION_SYSTEM.replace(
    "- reasoning: one short sentence explaining the call.\n",
    SENTIMENT_ADDENDUM + "- reasoning: one short sentence explaining the call.\n",
)
AUDIT_SCHEMA = json.loads(json.dumps(monitor.SIGNAL_SCHEMA))  # deep copy
AUDIT_SCHEMA["properties"]["overall_sentiment"] = {
    "type": "string", "enum": ["bullish", "bearish", "neutral"]}
AUDIT_SCHEMA["properties"]["sentiment_confidence"] = {
    "type": "string", "enum": ["high", "medium", "low"]}
AUDIT_SCHEMA["required"] += ["overall_sentiment", "sentiment_confidence"]


# --- fetch: up to TARGET POSTS (no replies) --------------------------------
def fetch_posts(account, target):
    """Cursor-paginate GetXAPI's posts-only endpoint. Returns (tweets, calls).
    Drops @-replies and any tweet not authored by `account` (RTs/foreign)."""
    collected, cursor, calls, seen = [], None, 0, set()
    while len(collected) < target:
        params = {"userName": account}
        if cursor:
            params["cursor"] = cursor
        url = (f"{monitor.GETXAPI_BASE}{monitor.GETXAPI_POSTS_PATH}"
               f"?{urllib.parse.urlencode(params)}")
        try:
            data = monitor.getxapi_get(url)
        except Exception as e:  # noqa: BLE001
            print(f"  [fetch error] {e}", file=sys.stderr)
            break
        calls += 1
        batch = data.get("tweets", [])
        if not batch:
            break
        for tw in batch:
            n = monitor._normalize_getxapi(tw)
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            if n.get("is_reply") or monitor.is_reply(n.get("text", "")):
                continue
            if monitor.foreign_author(account, n):
                continue
            collected.append(n)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor:
            break
    return collected[:target], calls


# --- per-post extraction (signal + sentiment + vision) ---------------------
def extract_post(interp, account, tw):
    text = tw.get("text", "")
    tweet_date = (tw.get("created_at") or "")[:10]
    try:
        resp = interp.client.messages.create(
            model=monitor.MODEL,
            max_tokens=400,
            system=[{"type": "text", "text": AUDIT_SYSTEM}],
            output_config={"format": {"type": "json_schema",
                                      "schema": AUDIT_SCHEMA}},
            messages=[monitor._user_message(account, text, tweet_date)],
        )
    except anthropic.APIError as e:
        print(f"  [llm error] {type(e).__name__}: {e}", file=sys.stderr)
        return None, False
    interp.calls += 1
    interp.input_tokens += resp.usage.input_tokens + \
        (resp.usage.cache_read_input_tokens or 0) + \
        (resp.usage.cache_creation_input_tokens or 0)
    interp.output_tokens += resp.usage.output_tokens
    parsed = monitor._parse_json_text(resp.content)
    if not parsed:
        return None, False
    # Vision pass on any chart photo, mirroring the influencer pipeline.
    chart_used = False
    if tw.get("media"):
        chart = interp.extract_chart(tw["media"], text, account, tweet_date)
        if chart:
            monitor.merge_chart(parsed, chart)
            if parsed.get("action") == "none":
                monitor.promote_with_chart(parsed, chart)
            chart_used = True
    return parsed, chart_used


# --- prices (read-only yfinance, self-contained) ---------------------------
_series = {}


def normalize_ticker(ticker, asset_type):
    """Crypto analysts quote exchange symbols ($BTC, BTCUSD, ZECUSDT). Strip the
    leading $ and any quote-currency suffix to a base symbol, and infer crypto
    when a quote suffix was present. Returns (base, asset_type)."""
    if not ticker:
        return None, asset_type
    t = ticker.strip().lstrip("$").upper()
    for suf in ("USDT", "USDC", "PERP", "USD"):
        if t.endswith(suf) and len(t) > len(suf):
            t, asset_type = t[:-len(suf)], "crypto"
            break
    return t, asset_type


def yf_symbol(ticker, asset_type):
    if asset_type == "crypto" and ticker and "-" not in ticker:
        return f"{ticker}-USD"
    return ticker


def price_series(symbol, start_date):
    key = (symbol, start_date)
    if key in _series:
        return _series[key]
    s = None
    try:
        hist = yf.Ticker(symbol).history(start=start_date)
        if not hist.empty:
            cl = hist["Close"]
            cl.index = cl.index.strftime("%Y-%m-%d")
            s = cl[~cl.index.duplicated(keep="last")].sort_index()
    except Exception:  # noqa: BLE001
        s = None
    _series[key] = s
    return s


def price_asof(s, date_str):
    if s is None:
        return None
    try:
        upto = s.loc[:date_str]
        return float(upto.iloc[-1]) if len(upto) else None
    except Exception:  # noqa: BLE001
        return None


def signal_quality(records):
    """Return-based win rate on actionable buy/sell calls with a resolvable
    price. buy = long (profit if price rose); sell = bearish/short call
    (profit if price fell). Estimated entry = close on/before the signal date
    when no explicit entry price was stated."""
    resolved, unresolved = [], []
    for r in records:
        if r["signal_type"] not in ("buy", "sell"):
            continue
        raw_ticker = (r.get("tickers") or [None])[0]
        ticker, atype = normalize_ticker(raw_ticker, r.get("asset_type") or "unknown")
        if not ticker:
            unresolved.append({"ticker": raw_ticker, "why": "no ticker",
                               "url": r["url"]})
            continue
        sig_date = (r.get("trade_date") or (r.get("timestamp") or "")[:10])
        if not sig_date:
            unresolved.append({"ticker": raw_ticker, "why": "no date",
                               "url": r["url"]})
            continue
        # Crypto-macro account: probe ambiguous (unknown) tickers as crypto; if
        # there is no -USD price series they fall through to "no price data".
        probe = atype if atype in ("crypto", "stock") else "crypto"
        sym = yf_symbol(ticker, probe)
        # pad start so the as-of lookup has a bar at/just before sig_date
        start = (sig_date[:8] + "01")
        s = price_series(sym, start)
        if s is None or not len(s):
            unresolved.append({"ticker": ticker, "why": "no price data",
                               "url": r["url"]})
            continue
        stated = r.get("entry_price")
        entry = stated if isinstance(stated, (int, float)) else price_asof(s, sig_date)
        current = float(s.iloc[-1])
        if not entry or entry <= 0:
            unresolved.append({"ticker": ticker, "why": "no entry price",
                               "url": r["url"]})
            continue
        raw = (current - entry) / entry
        ret = raw if r["signal_type"] == "buy" else -raw  # short flips sign
        resolved.append({
            "ticker": ticker, "asset_type": atype, "direction": r["signal_type"],
            "signal_date": sig_date, "entry_price": round(entry, 4),
            "entry_estimated": not isinstance(stated, (int, float)),
            "current_price": round(current, 4),
            "return_pct": round(ret * 100, 2), "win": ret > 0,
            "confidence": r.get("confidence"), "url": r["url"],
        })
    rets = [x["return_pct"] for x in resolved]
    wins = sum(1 for x in resolved if x["win"])
    summary = {
        "n_calls_total": sum(1 for r in records
                             if r["signal_type"] in ("buy", "sell")),
        "n_resolved": len(resolved), "n_unresolved": len(unresolved),
        "win_rate_pct": round(wins / len(resolved) * 100, 1) if resolved else None,
        "avg_return_pct": round(statistics.mean(rets), 2) if rets else None,
        "median_return_pct": round(statistics.median(rets), 2) if rets else None,
        "best": max(resolved, key=lambda x: x["return_pct"]) if resolved else None,
        "worst": min(resolved, key=lambda x: x["return_pct"]) if resolved else None,
    }
    return summary, resolved, unresolved


# --- market-view + themes synthesis (one Sonnet call) ----------------------
SYNTH_SYSTEM = (
    "You are a markets analyst summarizing one crypto commentator's recent X "
    "posts. Using ONLY the posts provided, output JSON with:\n"
    "- market_view_summary: 3-5 sentences on the author's CURRENT stance — what "
    "he thinks about Bitcoin (BTC), altcoins, and the overall crypto market "
    "right now. Be specific; cite his recurring claims, not generic filler.\n"
    "- key_themes: an array of exactly 5 short strings naming his most recurring "
    "narratives/themes (e.g. \"BTC dominance\", \"alt season timing\").\n"
    "Return ONLY valid JSON. No markdown, no preamble."
)
SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "market_view_summary": {"type": "string"},
        "key_themes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["market_view_summary", "key_themes"],
    "additionalProperties": False,
}


def synthesize(interp, account, posts):
    digest = "\n".join(
        f"[{(p['created_at'] or '')[:10]}] {(p.get('text') or '')[:240]}"
        for p in posts)
    try:
        resp = interp.client.messages.create(
            model=monitor.VISION_MODEL,  # Sonnet: better synthesis
            max_tokens=600,
            system=[{"type": "text", "text": SYNTH_SYSTEM}],
            output_config={"format": {"type": "json_schema",
                                      "schema": SYNTH_SCHEMA}},
            messages=[{"role": "user",
                       "content": f"Author: @{account}\nRecent posts (newest "
                                  f"first):\n{digest}"}],
        )
    except anthropic.APIError as e:
        print(f"  [synth error] {type(e).__name__}: {e}", file=sys.stderr)
        return None
    interp.vision_calls += 1
    interp.vision_input_tokens += resp.usage.input_tokens + \
        (resp.usage.cache_read_input_tokens or 0) + \
        (resp.usage.cache_creation_input_tokens or 0)
    interp.vision_output_tokens += resp.usage.output_tokens
    return monitor._parse_json_text(resp.content)


# --- main ------------------------------------------------------------------
def _records_from_report(report):
    """Rebuild minimal signal records from a saved report's per_post list so
    signal_quality can be recomputed without re-calling any LLM."""
    recs = []
    for p in report["per_post"]:
        s = p.get("signal")
        if not s:
            continue
        recs.append({
            "signal_type": s["action"], "tickers": [s["ticker"]],
            "asset_type": s["asset_type"], "entry_price": s.get("entry_price"),
            "trade_date": s.get("trade_date"),
            "timestamp": p["date"], "url": p["url"], "confidence": s["confidence"],
        })
    return recs


def recompute_signal_quality():
    """Reload the saved report, recompute ONLY signal_quality with the current
    ticker normalizer, rewrite the report. No API spend."""
    with open(OUT) as f:
        report = json.load(f)
    recs = _records_from_report(report)
    summary, resolved, unresolved = signal_quality(recs)
    report["signal_quality"]["summary"] = summary
    report["signal_quality"]["resolved_calls"] = sorted(
        resolved, key=lambda x: x["return_pct"], reverse=True)
    report["signal_quality"]["unresolved_calls"] = unresolved
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Recomputed signal_quality: resolved={summary['n_resolved']} "
          f"unresolved={summary['n_unresolved']} "
          f"win_rate={summary['win_rate_pct']}% avg={summary['avg_return_pct']}%")
    for c in report["signal_quality"]["resolved_calls"]:
        print(f"  {c['direction']:4} {c['ticker']:6} {c['signal_date']} "
              f"entry={c['entry_price']} now={c['current_price']} "
              f"ret={c['return_pct']:+}% win={c['win']}")


def main():
    t0 = time.time()
    print(f"Fetching up to {TARGET} posts from @{ACCOUNT} (no replies)...")
    posts, calls = fetch_posts(ACCOUNT, TARGET)
    print(f"  got {len(posts)} posts in {calls} GetXAPI calls "
          f"(${calls * monitor.GETXAPI_COST_PER_CALL:.4f})")
    if not posts:
        print("No posts fetched; aborting.", file=sys.stderr)
        sys.exit(1)

    interp = monitor.Interpreter()
    per_post, records = [], []
    sent_tally = {"bullish": 0, "bearish": 0, "neutral": 0}
    n_charts = 0
    for i, tw in enumerate(posts, 1):
        parsed, chart_used = extract_post(interp, ACCOUNT, tw)
        if chart_used:
            n_charts += 1
        if i % 25 == 0:
            print(f"  extracted {i}/{len(posts)}")
        sentiment = (parsed or {}).get("overall_sentiment") or "neutral"
        sent_tally[sentiment] = sent_tally.get(sentiment, 0) + 1
        rec = monitor.record_from_parsed(ACCOUNT, tw, parsed) if parsed else None
        if rec:
            records.append(rec)
        per_post.append({
            "id": tw["id"],
            "date": (tw.get("created_at") or "")[:10],
            "url": f"https://x.com/{ACCOUNT}/status/{tw['id']}",
            "has_chart": bool(tw.get("media")),
            "text": (tw.get("text") or "")[:280],
            "overall_sentiment": sentiment,
            "sentiment_confidence": (parsed or {}).get("sentiment_confidence"),
            "signal": None if not rec else {
                "action": rec["signal_type"], "ticker": (rec["tickers"] or [None])[0],
                "asset_type": rec["asset_type"], "confidence": rec["confidence"],
                "sell_kind": rec.get("sell_kind"), "entry_price": rec.get("entry_price"),
                "target": rec.get("target"), "stop_loss": rec.get("stop_loss"),
                "trade_date": rec.get("trade_date"), "tp1": rec.get("tp1"),
                "tp2": rec.get("tp2"), "chart_trend": rec.get("chart_trend"),
            },
        })

    print("Synthesizing market view + themes (Sonnet)...")
    synth = synthesize(interp, ACCOUNT, posts) or {}
    print("Computing signal quality (yfinance)...")
    sq_summary, resolved, unresolved = signal_quality(records)

    report = {
        "account": ACCOUNT,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "note": "READ-ONLY audit. Not added to monitored ACCOUNTS; no "
                "trades.json/positions.json/state mutation.",
        "fetch": {"posts_requested": TARGET, "posts_fetched": len(posts),
                  "getxapi_calls": calls,
                  "date_range": [per_post[-1]["date"] if per_post else None,
                                 per_post[0]["date"] if per_post else None]},
        "sentiment": {
            "distribution": sent_tally,
            "n_posts": len(posts),
            "net_bias": ("bullish" if sent_tally["bullish"] > sent_tally["bearish"]
                         else "bearish" if sent_tally["bearish"] > sent_tally["bullish"]
                         else "neutral"),
        },
        "market_view_summary": synth.get("market_view_summary"),
        "key_themes": synth.get("key_themes"),
        "signal_quality": {
            "methodology": "buy=long, sell=bearish/short; return = current vs "
                           "stated-or-estimated entry (close on/before signal "
                           "date); win = positive return as of now.",
            "summary": sq_summary,
            "resolved_calls": sorted(resolved, key=lambda x: x["return_pct"],
                                     reverse=True),
            "unresolved_calls": unresolved,
        },
        "cost_usd": {
            "getxapi": round(calls * monitor.GETXAPI_COST_PER_CALL, 4),
            "haiku_text": round(interp.cost(), 4),
            "sonnet_vision_and_synth": round(
                interp.vision_input_tokens / 1e6 * 3.0
                + interp.vision_output_tokens / 1e6 * 15.0, 4),
            "vision_calls": interp.vision_calls,
            "charts_analyzed": n_charts,
        },
        "per_post": per_post,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nWrote {OUT} in {time.time() - t0:.0f}s")
    # Compact console summary
    print("\n===== AUDIT SUMMARY =====")
    print(f"@{ACCOUNT}: {len(posts)} posts "
          f"[{report['fetch']['date_range'][0]} -> {report['fetch']['date_range'][1]}]")
    print(f"Sentiment: {sent_tally}  net={report['sentiment']['net_bias']}")
    print(f"Signal calls: total={sq_summary['n_calls_total']} "
          f"resolved={sq_summary['n_resolved']} "
          f"win_rate={sq_summary['win_rate_pct']}% "
          f"avg_return={sq_summary['avg_return_pct']}%")
    print("Themes: " + "; ".join(synth.get("key_themes") or []))


if __name__ == "__main__":
    if os.environ.get("AUDIT_RECOMPUTE") == "1":
        recompute_signal_quality()
    else:
        main()
