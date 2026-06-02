#!/usr/bin/env python3
"""
Historical audit of @CelalKucuker (ANALYSIS ONLY).

Reuses monitor.py's GetXAPI fetcher + LLM extraction (text Haiku + Sonnet
vision on chart photos), resolves each detected signal against historical
prices (yfinance), and writes a performance report.

DOES NOT touch trades.json / positions.json / .monitor_state.json.
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

import monitor

ACCOUNT = "CelalKucuker"
OUT_FILE = os.path.join(monitor.HOME, "results", "audit_CelalKucuker.json")
N_TWEETS = 200
HORIZON_DAYS = 45            # window to call a still-unresolved trade "expired"
TODAY = datetime.now(timezone.utc).date()

# Treat the audited handle as an influencer: bypass the @-reply gate and enable
# the Sonnet vision pass on chart photos (same handling as @IncomeSharks).
monitor.SOURCE_TYPE[ACCOUNT] = "influencer"
monitor.MAX_FETCH = N_TWEETS


def yf_symbol(ticker, asset_type):
    if not ticker:
        return None
    if asset_type == "crypto":
        return f"{ticker.upper()}-USD"
    return ticker.upper()


def first_on_or_after(df, d):
    """First row index/values with date >= d, else None."""
    sub = df[df.index.date >= d]
    return sub if len(sub) else None


def resolve_signal(sig, price_cache):
    """Resolve a signal to an outcome + return%. Mutates nothing in monitor."""
    ticker = sig["tickers"][0]
    st = sig["signal_type"]
    direction = "long" if st in ("buy", "position") else (
        "short" if st == "sell" else None)
    out = {
        "ticker": ticker, "asset_type": sig.get("asset_type"),
        "signal_type": st, "direction": direction,
        "entry_date": None, "entry_price": None,
        "target": sig.get("target") or sig.get("tp1"),
        "stop_loss": sig.get("stop_loss"),
        "outcome": "unresolved", "return_pct": None, "exit_price": None,
        "exit_date": None,
    }
    if direction is None:
        out["outcome"] = "non_directional"
        return out

    sym = yf_symbol(ticker, sig.get("asset_type"))
    entry_date = (sig.get("trade_date") or (sig.get("timestamp") or "")[:10])
    try:
        ed = datetime.strptime(entry_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        out["outcome"] = "no_entry_date"
        return out
    out["entry_date"] = entry_date

    df = price_cache.get(sym)
    if df is None or len(df) == 0:
        out["outcome"] = "no_price_data"
        return out

    # entry price: stated entry, else first close on/after entry date
    entry = sig.get("entry_price")
    after = first_on_or_after(df, ed)
    if entry is None:
        if after is None:
            out["outcome"] = "no_price_data"
            return out
        entry = float(after["Close"].iloc[0])
    out["entry_price"] = round(float(entry), 4)

    # forward path from entry date
    path = df[df.index.date >= ed]
    if len(path) == 0:
        out["outcome"] = "no_price_data"
        return out

    tgt = out["target"]
    stop = out["stop_loss"]
    age_days = (TODAY - ed).days

    # walk daily bars; check stop first (conservative), then target
    hit = None
    for ts, row in path.iterrows():
        hi, lo = float(row["High"]), float(row["Low"])
        d = ts.date()
        if direction == "long":
            if stop is not None and lo <= stop:
                hit = ("stopped_out", stop, d); break
            if tgt is not None and hi >= tgt:
                hit = ("hit_target", tgt, d); break
        else:  # short
            if stop is not None and hi >= stop:
                hit = ("stopped_out", stop, d); break
            if tgt is not None and lo <= tgt:
                hit = ("hit_target", tgt, d); break

    if hit:
        outcome, exit_px, exit_d = hit
        out["outcome"] = outcome
        out["exit_price"] = round(float(exit_px), 4)
        out["exit_date"] = exit_d.isoformat()
    else:
        # no level hit (or no levels defined) -> mark-to-market last close
        last_close = float(path["Close"].iloc[-1])
        out["exit_price"] = round(last_close, 4)
        out["exit_date"] = path.index[-1].date().isoformat()
        if tgt is None and stop is None:
            out["outcome"] = "no_levels"
        elif age_days >= HORIZON_DAYS:
            out["outcome"] = "expired"
        else:
            out["outcome"] = "still_open"

    px = out["exit_price"]
    if direction == "long":
        ret = (px - entry) / entry * 100
    else:
        ret = (entry - px) / entry * 100
    out["return_pct"] = round(ret, 2)
    return out


def main():
    monitor.load_env(monitor.ENV_FILE)
    if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("GETXAPI_KEY"):
        print("Missing ANTHROPIC_API_KEY or GETXAPI_KEY in .env", file=sys.stderr)
        sys.exit(1)

    interp = monitor.Interpreter()
    print(f"Fetching up to {N_TWEETS} tweets for @{ACCOUNT} ...", file=sys.stderr)
    tweets, calls = monitor.fetch_getxapi(ACCOUNT)   # since_id=None -> newest N
    print(f"  fetched {len(tweets)} tweets in {calls} GetXAPI calls",
          file=sys.stderr)

    signals = []
    for i, tw in enumerate(tweets, 1):
        sig = monitor.build_signal(ACCOUNT, tw, interp)   # text + vision
        if sig:
            signals.append(sig)
        if i % 25 == 0:
            print(f"  extracted {i}/{len(tweets)} ...", file=sys.stderr)
    print(f"  {len(signals)} signal-bearing tweets", file=sys.stderr)

    # --- prefetch price history per unique symbol -------------------------
    syms = {}
    for s in signals:
        sym = yf_symbol(s["tickers"][0], s.get("asset_type"))
        if sym:
            ed = s.get("trade_date") or (s.get("timestamp") or "")[:10]
            try:
                d = datetime.strptime(ed, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            syms[sym] = min(syms.get(sym, d), d)

    price_cache = {}
    for sym, start in syms.items():
        try:
            df = yf.Ticker(sym).history(
                start=(start - timedelta(days=3)).isoformat(),
                end=(TODAY + timedelta(days=1)).isoformat(),
                auto_adjust=True)
            price_cache[sym] = df if len(df) else None
        except Exception as e:                       # noqa: BLE001
            print(f"  [price err] {sym}: {e}", file=sys.stderr)
            price_cache[sym] = None

    # --- resolve outcomes -------------------------------------------------
    resolved = []
    for s in signals:
        r = resolve_signal(s, price_cache)
        r["tweet_id"] = s["tweet_id"]
        r["timestamp"] = s["timestamp"]
        r["url"] = s["url"]
        r["text"] = s["text"]
        r["confidence"] = s["confidence"]
        r["has_chart"] = s.get("has_chart")
        r["chart_trend"] = s.get("chart_trend")
        r["reasoning"] = s.get("reasoning")
        resolved.append(r)

    # --- metrics ----------------------------------------------------------
    directional = [r for r in resolved if r["direction"] in ("long", "short")]
    priced = [r for r in directional if r["return_pct"] is not None]
    target_resolved = [r for r in resolved
                       if r["outcome"] in ("hit_target", "stopped_out")]
    wins = [r for r in target_resolved if r["outcome"] == "hit_target"]
    win_rate = (len(wins) / len(target_resolved) * 100) if target_resolved else None

    returns = [r["return_pct"] for r in priced]
    avg_ret = round(sum(returns) / len(returns), 2) if returns else None
    best = max(priced, key=lambda r: r["return_pct"], default=None)
    worst = min(priced, key=lambda r: r["return_pct"], default=None)

    asset_breakdown = Counter(r["asset_type"] for r in resolved)
    ticker_freq = Counter(r["ticker"] for r in resolved if r["ticker"])
    outcome_breakdown = Counter(r["outcome"] for r in resolved)
    type_breakdown = Counter(r["signal_type"] for r in resolved)

    def slim(r):
        if not r:
            return None
        return {k: r[k] for k in ("ticker", "direction", "entry_date",
                "entry_price", "exit_price", "exit_date", "target",
                "stop_loss", "outcome", "return_pct", "url")}

    samples = [{
        "url": r["url"], "timestamp": r["timestamp"],
        "text": (r["text"] or "")[:280],
        "ticker": r["ticker"], "asset_type": r["asset_type"],
        "signal_type": r["signal_type"], "confidence": r["confidence"],
        "target": r["target"], "stop_loss": r["stop_loss"],
        "has_chart": r["has_chart"], "chart_trend": r["chart_trend"],
        "outcome": r["outcome"], "return_pct": r["return_pct"],
        "reasoning": r["reasoning"],
    } for r in resolved[:12]]

    report = {
        "account": ACCOUNT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "ANALYSIS ONLY. Not added to monitored ACCOUNTS. "
                "trades.json / positions.json / state untouched.",
        "horizon_days": HORIZON_DAYS,
        "tweets_fetched": len(tweets),
        "getxapi_calls": calls,
        "signals_detected": len(signals),
        "directional_signals": len(directional),
        "priced_signals": len(priced),
        "metrics": {
            "win_rate_pct": round(win_rate, 1) if win_rate is not None else None,
            "win_rate_basis": f"{len(wins)}/{len(target_resolved)} "
                              f"target-or-stop resolved trades",
            "avg_return_pct": avg_ret,
            "avg_return_basis": f"mean over {len(priced)} priced directional trades",
            "best_trade": slim(best),
            "worst_trade": slim(worst),
        },
        "asset_breakdown": dict(asset_breakdown),
        "signal_type_breakdown": dict(type_breakdown),
        "outcome_breakdown": dict(outcome_breakdown),
        "most_traded_tickers": ticker_freq.most_common(10),
        "llm_cost": {
            "haiku_text_usd": round(interp.cost(), 4),
            "sonnet_vision_usd": round(interp.vision_cost(), 4),
            "vision_calls": interp.vision_calls,
            "total_usd": round(interp.cost() + interp.vision_cost(), 4),
        },
        "getxapi_cost_usd": round(calls * monitor.GETXAPI_COST_PER_CALL, 4),
        "sample_signals": samples,
        "all_resolved": [slim(r) | {"outcome": r["outcome"]} for r in resolved],
    }

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    # compact stdout summary
    print(json.dumps({
        "account": ACCOUNT,
        "tweets_fetched": len(tweets),
        "signals_detected": len(signals),
        "directional": len(directional),
        "win_rate_pct": report["metrics"]["win_rate_pct"],
        "win_rate_basis": report["metrics"]["win_rate_basis"],
        "avg_return_pct": avg_ret,
        "best": slim(best),
        "worst": slim(worst),
        "asset_breakdown": dict(asset_breakdown),
        "signal_type_breakdown": dict(type_breakdown),
        "outcome_breakdown": dict(outcome_breakdown),
        "top_tickers": ticker_freq.most_common(8),
        "llm_cost_usd": report["llm_cost"]["total_usd"],
        "out_file": OUT_FILE,
    }, indent=2))


if __name__ == "__main__":
    main()
