#!/usr/bin/env python3
"""
Historical audit of @crypto_birb (ANALYSIS ONLY).

Superset of audit_account.py: fetches 200 POSTS (replies dropped), runs the
monitor.py LLM extraction (Haiku text + Sonnet vision on chart photos),
resolves each signal vs historical prices (yfinance), and additionally scores
the account's signature: cycle TOP/BOTTOM timing and TP/SL level accuracy.

Writes results/audit_crypto_birb.json.

DOES NOT touch trades.json / positions.json / .monitor_state.json, and does
NOT add the handle to monitor.ACCOUNTS (only mutates in-memory SOURCE_TYPE so
the vision pass runs, exactly like audit_account.py).
"""

import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

import monitor

ACCOUNT = "crypto_birb"
OUT_FILE = os.path.join(monitor.HOME, "results", f"audit_{ACCOUNT}.json")
N_TWEETS = 200
HORIZON_DAYS = 45            # window to call a still-unresolved trade "expired"
TIMING_WINDOW_DAYS = 21      # +/- window to locate the local top/bottom
NEAR_EXTREME_PCT = 5.0       # "good timing" threshold vs the local extreme
TODAY = datetime.now(timezone.utc).date()

# Treat as influencer so the Sonnet vision pass runs on chart photos. In-memory
# only; does NOT add the handle to ACCOUNTS.
monitor.SOURCE_TYPE[ACCOUNT] = "influencer"
monitor.MAX_FETCH = N_TWEETS


def is_reply_tweet(tw):
    return bool(tw.get("is_reply")) or monitor.is_reply(tw.get("text", ""))


# Known crypto bases so an "unknown" asset_type still maps to the -USD pair
# instead of colliding with an equities ticker (e.g. bare "BTC" on yfinance is
# the Grayscale Bitcoin ETF, NOT bitcoin). crypto_birb is essentially all-crypto.
CRYPTO_BASES = {
    "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "DOGE", "AVAX", "DOT", "LINK",
    "MATIC", "LTC", "BCH", "ATOM", "ARB", "OP", "SUI", "APT", "TON", "TRX",
    "NEAR", "INJ", "SEI", "TIA", "PEPE", "SHIB", "WIF", "ENA", "ONDO",
}


def yf_symbol(ticker, asset_type):
    if not ticker:
        return None
    base = ticker.upper()
    # strip pair suffixes the LLM sometimes keeps (BTCUSD, ETHUSDT, SOLPERP)
    for suf in ("USDT", "USDC", "PERP", "USD"):
        if base.endswith(suf) and len(base) > len(suf):
            base = base[: -len(suf)]
            break
    if asset_type == "crypto" or base in CRYPTO_BASES:
        return f"{base}-USD"
    return base


def first_on_or_after(df, d):
    sub = df[df.index.date >= d]
    return sub if len(sub) else None


def resolve_signal(sig, price_cache):
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

    entry = sig.get("entry_price")
    after = first_on_or_after(df, ed)
    if entry is None:
        if after is None:
            out["outcome"] = "no_price_data"
            return out
        entry = float(after["Close"].iloc[0])
    out["entry_price"] = round(float(entry), 4)

    path = df[df.index.date >= ed]
    if len(path) == 0:
        out["outcome"] = "no_price_data"
        return out

    tgt = out["target"]
    stop = out["stop_loss"]
    age_days = (TODAY - ed).days

    hit = None
    for ts, row in path.iterrows():
        hi, lo = float(row["High"]), float(row["Low"])
        d = ts.date()
        if direction == "long":
            if stop is not None and lo <= stop:
                hit = ("stopped_out", stop, d); break
            if tgt is not None and hi >= tgt:
                hit = ("hit_target", tgt, d); break
        else:
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


def compute_timing(r, price_cache):
    """Locate the local top/bottom within +/- TIMING_WINDOW_DAYS of entry and
    score how close the entry price/date was to it.

    For a LONG (buy/position) the ideal is buying the local BOTTOM; for a SHORT
    (sell) the ideal is selling the local TOP. Returns dict or None."""
    if r["direction"] not in ("long", "short") or r["entry_price"] is None:
        return None
    sym = yf_symbol(r["ticker"], r["asset_type"])
    df = price_cache.get(sym)
    if df is None or len(df) == 0:
        return None
    try:
        ed = datetime.strptime(r["entry_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    lo_d = ed - timedelta(days=TIMING_WINDOW_DAYS)
    hi_d = ed + timedelta(days=TIMING_WINDOW_DAYS)
    win = df[(df.index.date >= lo_d) & (df.index.date <= hi_d)]
    if len(win) == 0:
        return None
    entry = r["entry_price"]

    bottom_px = float(win["Low"].min())
    bottom_d = win["Low"].idxmin().date()
    top_px = float(win["High"].max())
    top_d = win["High"].idxmax().date()

    if r["direction"] == "long":     # bottom call
        extreme_px, extreme_d, kind = bottom_px, bottom_d, "bottom"
        # % entry sits ABOVE the local bottom (0 = perfect bottom buy)
        dist_pct = (entry - bottom_px) / bottom_px * 100 if bottom_px else None
    else:                            # short -> top call
        extreme_px, extreme_d, kind = top_px, top_d, "top"
        # % entry sits BELOW the local top (0 = perfect top sell)
        dist_pct = (top_px - entry) / top_px * 100 if top_px else None

    return {
        "call_kind": kind,
        "extreme_price": round(extreme_px, 4),
        "extreme_date": extreme_d.isoformat(),
        "entry_vs_extreme_pct": round(dist_pct, 2) if dist_pct is not None else None,
        "days_from_extreme": (ed - extreme_d).days,
        "near_extreme": (dist_pct is not None and abs(dist_pct) <= NEAR_EXTREME_PCT),
        "window_truncated": bool(win.index.date.min() > lo_d or win.index.date.max() < hi_d),
    }


def main():
    monitor.load_env(monitor.ENV_FILE)
    if not os.environ.get("ANTHROPIC_API_KEY") or not os.environ.get("GETXAPI_KEY"):
        print("Missing ANTHROPIC_API_KEY or GETXAPI_KEY in .env", file=sys.stderr)
        sys.exit(1)

    interp = monitor.Interpreter()
    print(f"Fetching up to {N_TWEETS} tweets for @{ACCOUNT} ...", file=sys.stderr)
    fetched, calls = monitor.fetch_getxapi(ACCOUNT)
    posts = [t for t in fetched if not is_reply_tweet(t)]
    # dedup by tweet id (reposts / page overlap) so counts/metrics aren't inflated
    tweets, seen = [], set()
    for t in posts:
        tid = t.get("id") or t.get("id_str") or t.get("tweet_id")
        if tid in seen:
            continue
        seen.add(tid)
        tweets.append(t)
    print(f"  fetched {len(fetched)} tweets in {calls} GetXAPI calls; "
          f"{len(posts)} posts after dropping replies; "
          f"{len(tweets)} unique after dedup", file=sys.stderr)

    signals = []
    for i, tw in enumerate(tweets, 1):
        sig = monitor.build_signal(ACCOUNT, tw, interp)
        if sig:
            signals.append(sig)
        if i % 25 == 0:
            print(f"  extracted {i}/{len(tweets)} ...", file=sys.stderr)
    print(f"  {len(signals)} signal-bearing posts", file=sys.stderr)

    # prefetch price history per unique symbol (extra lookback for timing) -----
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
                start=(start - timedelta(days=TIMING_WINDOW_DAYS + 5)).isoformat(),
                end=(TODAY + timedelta(days=1)).isoformat(),
                auto_adjust=True)
            price_cache[sym] = df if len(df) else None
        except Exception as e:                       # noqa: BLE001
            print(f"  [price err] {sym}: {e}", file=sys.stderr)
            price_cache[sym] = None

    # resolve outcomes + timing ----------------------------------------------
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
        r["timing"] = compute_timing(r, price_cache)
        resolved.append(r)

    # core metrics ------------------------------------------------------------
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

    # --- FOCUS: cycle top/bottom timing -------------------------------------
    timed = [r for r in directional if r.get("timing")]
    longs = [r for r in timed if r["direction"] == "long"]
    shorts = [r for r in timed if r["direction"] == "short"]

    def _avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    bottom_dist = [r["timing"]["entry_vs_extreme_pct"] for r in longs]
    top_dist = [r["timing"]["entry_vs_extreme_pct"] for r in shorts]
    near_bottoms = [r for r in longs if r["timing"]["near_extreme"]]
    near_tops = [r for r in shorts if r["timing"]["near_extreme"]]

    timing_focus = {
        "explainer": (
            "entry_vs_extreme_pct = how far the entry price sat from the local "
            f"{TIMING_WINDOW_DAYS}-day extreme (0=perfect). LONG scored vs the "
            "local BOTTOM (buy-the-dip), SHORT vs the local TOP. "
            f"near_extreme = within {NEAR_EXTREME_PCT}%."),
        "bottom_calls": len(longs),
        "avg_entry_above_bottom_pct": _avg(bottom_dist),
        "median_entry_above_bottom_pct": (
            round(float(pd.Series(bottom_dist).median()), 2) if bottom_dist else None),
        "bottom_calls_within_5pct": len(near_bottoms),
        "bottom_hit_rate_pct": (
            round(len(near_bottoms) / len(longs) * 100, 1) if longs else None),
        "top_calls": len(shorts),
        "avg_entry_below_top_pct": _avg(top_dist),
        "median_entry_below_top_pct": (
            round(float(pd.Series(top_dist).median()), 2) if top_dist else None),
        "top_calls_within_5pct": len(near_tops),
        "top_hit_rate_pct": (
            round(len(near_tops) / len(shorts) * 100, 1) if shorts else None),
    }

    # --- FOCUS: TP / SL level accuracy --------------------------------------
    with_target = [r for r in directional if r["target"] is not None
                   and r["entry_price"]]
    with_stop = [r for r in directional if r["stop_loss"] is not None
                 and r["entry_price"]]
    targets_reached = [r for r in with_target if r["outcome"] == "hit_target"]
    stops_hit = [r for r in with_stop if r["outcome"] == "stopped_out"]

    def _tgt_dist(r):
        e = r["entry_price"]
        if r["direction"] == "long":
            return (r["target"] - e) / e * 100
        return (e - r["target"]) / e * 100

    def _stop_dist(r):
        e = r["entry_price"]
        if r["direction"] == "long":
            return (e - r["stop_loss"]) / e * 100
        return (r["stop_loss"] - e) / e * 100

    level_focus = {
        "explainer": (
            "Targets/stops parsed from text+chart. *_reached = price actually "
            "touched the level on the forward daily path. distance_pct = level's "
            "distance from entry (ambition of the target / width of the stop)."),
        "targets_set": len(with_target),
        "targets_reached": len(targets_reached),
        "target_reach_rate_pct": (
            round(len(targets_reached) / len(with_target) * 100, 1)
            if with_target else None),
        "avg_target_distance_pct": _avg([_tgt_dist(r) for r in with_target]),
        "stops_set": len(with_stop),
        "stops_hit": len(stops_hit),
        "stop_hit_rate_pct": (
            round(len(stops_hit) / len(with_stop) * 100, 1) if with_stop else None),
        "avg_stop_distance_pct": _avg([_stop_dist(r) for r in with_stop]),
        "avg_reward_risk_ratio": _avg([
            _tgt_dist(r) / _stop_dist(r)
            for r in directional
            if r["target"] is not None and r["stop_loss"] is not None
            and r["entry_price"] and _stop_dist(r) > 0]),
    }

    def slim(r):
        if not r:
            return None
        base = {k: r[k] for k in ("ticker", "asset_type", "direction",
                "entry_date", "entry_price", "exit_price", "exit_date",
                "target", "stop_loss", "outcome", "return_pct", "url")}
        if r.get("timing"):
            base["timing"] = r["timing"]
        return base

    samples = [{
        "url": r["url"], "timestamp": r["timestamp"],
        "text": (r["text"] or "")[:280],
        "ticker": r["ticker"], "asset_type": r["asset_type"],
        "signal_type": r["signal_type"], "confidence": r["confidence"],
        "target": r["target"], "stop_loss": r["stop_loss"],
        "has_chart": r["has_chart"], "chart_trend": r["chart_trend"],
        "outcome": r["outcome"], "return_pct": r["return_pct"],
        "timing": r.get("timing"),
        "reasoning": r["reasoning"],
    } for r in resolved[:12]]

    report = {
        "account": ACCOUNT,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "note": "ANALYSIS ONLY (posts only, replies dropped). Not added to "
                "monitored ACCOUNTS. trades.json / positions.json / state "
                "untouched.",
        "horizon_days": HORIZON_DAYS,
        "timing_window_days": TIMING_WINDOW_DAYS,
        "tweets_fetched": len(fetched),
        "posts_analyzed": len(tweets),
        "replies_dropped": len(fetched) - len(posts),
        "duplicate_posts_dropped": len(posts) - len(tweets),
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
        "cycle_timing_focus": timing_focus,
        "tp_sl_accuracy_focus": level_focus,
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
        "all_resolved": [slim(r) for r in resolved],
    }

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump(report, f, indent=2)

    print(json.dumps({
        "account": ACCOUNT,
        "tweets_fetched": len(fetched),
        "posts_analyzed": len(tweets),
        "signals_detected": len(signals),
        "directional": len(directional),
        "win_rate_pct": report["metrics"]["win_rate_pct"],
        "win_rate_basis": report["metrics"]["win_rate_basis"],
        "avg_return_pct": avg_ret,
        "cycle_timing_focus": timing_focus,
        "tp_sl_accuracy_focus": level_focus,
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
