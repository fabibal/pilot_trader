#!/usr/bin/env python3
"""
Reconcile the trades.json event log into a position-state model (positions.json).

trades.json is an append-only log of tweet-derived signals; the same holding is
disclosed many times. This folds those events, in chronological order, into one
record per (account, portfolio, ticker):

    {status: open|closed, entry_price, size_pct, trade_date, opened_at,
     closed_at, signals: [...]}

Rules:
  - buy            -> opens the position (sets opened_at / entry / trade_date)
  - sell           -> closes it (sets closed_at)
  - position       -> updates size (and opens it if it was NEVER opened, since a
                      current-holding disclosure implies the position is held).
                      It does NOT reopen a closed position -- disclosures are
                      often recaps of old holdings, so only a buy re-opens.

Run standalone:  python reconcile.py
Or import reconcile() from monitor.py after each fetch.
"""

import json
import os
import tempfile

# Account-to-portfolio fallback, applied at STORAGE time so the position key
# matches what the dashboard shows (avoids a null-portfolio record and a
# resolved-portfolio record for the same holding being counted twice).
# NON_AI_ACCOUNTS (all influencer accounts) never key a
# portfolio — this previously used a local, INCOMPLETE influencer set that
# disagreed with dashboard.py.
from accounts import ACCOUNT_DEFAULT_PF, NON_AI_ACCOUNTS

HOME = "/home/fbazsa/pilot_trader"
TRADES_FILE = os.path.join(HOME, "trades.json")
POSITIONS_FILE = os.path.join(HOME, "positions.json")
# Signals at this confidence are logged to trades.json but must NOT move
# position state (see confidence gate).
GATED_CONFIDENCE = {"low", "none"}

# Non-tradeable pseudo-tickers — market indices / commodities / forex / crypto
# dominance the LLM sometimes lifts from macro commentary (e.g. CelalKucuker).
# They are NOT positions; reject them so they never enter positions.json.
NON_TRADEABLE_TICKERS = {
    "BTCDOMINANCE", "BTC.D", "ETH.D", "USDT.D", "TOTAL", "TOTAL2", "TOTAL3",
    "DXY", "XAUUSD", "XAGUSD", "SPX", "SPX500", "VIX", "DJI", "DJIA",
    "US10Y", "US30", "NAS100", "GOLD", "SILVER",
}


def is_junk_ticker(ticker):
    """True for index/commodity/forex/dominance pseudo-tickers that aren't real
    positions (BTCDOMINANCE, XAUUSD, DXY, BTC.D, ...). Explicit blocklist plus a
    couple of unambiguous patterns; conservative to avoid rejecting real equities."""
    t = (ticker or "").upper().strip()
    if not t:
        return True
    if t in NON_TRADEABLE_TICKERS:
        return True
    if t.endswith(".D"):                # crypto dominance (BTC.D, ETH.D, ...)
        return True
    if t.startswith(("XAU", "XAG")):    # gold / silver spot pairs (XAUUSD, ...)
        return True
    return False


def pf_of(account, portfolio):
    """Resolve the effective portfolio for keying. Influencers keep null."""
    if account in NON_AI_ACCOUNTS:
        return None
    return portfolio or ACCOUNT_DEFAULT_PF.get(account)


def write_json_atomic(path, data):
    """Write JSON via temp file + os.replace so a crash can't corrupt the file."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def reconcile(trades_file=TRADES_FILE, positions_file=POSITIONS_FILE):
    with open(trades_file) as f:
        events = json.load(f)

    # chronological order so opens precede closes
    events.sort(key=lambda e: e.get("timestamp", ""))

    positions = {}
    seen_tweet_ids = set()
    for e in events:
        tickers = e.get("tickers") or []
        if not tickers:
            continue
        # Idempotency guard: each tweet is one signal. monitor.py already dedups
        # by tweet_id before writing trades.json, but fold defensively here too —
        # a duplicate partial-sell would otherwise halve size_pct twice.
        tid = e.get("tweet_id")
        if tid is not None:
            if tid in seen_tweet_ids:
                continue
            seen_tweet_ids.add(tid)
        # Confidence gate: low/none-confidence signals stay in trades.json but
        # must not move position state.
        if (e.get("confidence") or "").lower() in GATED_CONFIDENCE:
            continue
        ticker = tickers[0]
        # Drop index/commodity/forex pseudo-tickers (not real positions).
        if is_junk_ticker(ticker):
            continue
        account = e.get("account")
        portfolio = pf_of(account, e.get("portfolio"))
        key = (account, portfolio, ticker)
        pos = positions.get(key)
        if pos is None:
            pos = {
                "account": account,
                "source_type": e.get("source_type", "portfolio"),
                "portfolio": portfolio,
                "ticker": ticker,
                "asset_type": e.get("asset_type", "unknown"),
                "status": None,
                "entry_price": None,
                "stop_loss": None,
                "target": None,
                "size_pct": None,
                "trade_date": None,
                "holding_thesis": None,
                "opened_at": None,
                "closed_at": None,
                "signals": [],
            }
            positions[key] = pos

        pos["signals"].append({
            "tweet_id": e.get("tweet_id"),
            "signal_type": e.get("signal_type"),
            "timestamp": e.get("timestamp"),
            "confidence": e.get("confidence"),
            "url": e.get("url"),
        })

        # asset_type: upgrade from "unknown" to a concrete value when seen.
        if e.get("asset_type") and e["asset_type"] != "unknown":
            pos["asset_type"] = e["asset_type"]

        # holding_thesis: keep the most recent stated conviction reason.
        if e.get("holding_thesis"):
            pos["holding_thesis"] = e["holding_thesis"]

        st = e.get("signal_type")
        if st == "buy":
            if pos["status"] != "open":
                # A buy that RE-opens a previously closed position starts a
                # fresh trade cycle: the prior cycle's entry/stop/target/size
                # must not leak into it (they made re-entry returns compute
                # off the OLD entry price).
                if pos["status"] == "closed":
                    pos["entry_price"] = None
                    pos["trade_date"] = None
                    pos["stop_loss"] = None
                    pos["target"] = None
                    pos["size_pct"] = None
                pos["status"] = "open"
                pos["opened_at"] = e.get("timestamp")
                pos["closed_at"] = None        # re-opened after a prior close
            if pos["entry_price"] is None and e.get("entry_price"):
                pos["entry_price"] = e["entry_price"]
            if pos["trade_date"] is None and e.get("trade_date"):
                pos["trade_date"] = e["trade_date"]
            if e.get("position_size_pct") is not None:
                pos["size_pct"] = e["position_size_pct"]
            if e.get("stop_loss") is not None:
                pos["stop_loss"] = e["stop_loss"]
            if e.get("target") is not None:
                pos["target"] = e["target"]
        elif st == "sell":
            # A sell only acts on a position ALREADY observed as open. A sell
            # that is the first event for a ticker (no prior buy/position seen)
            # is not evidence of a holding we can price or size, so it must NOT
            # materialize a phantom open/closed record; and a sell never reopens
            # or mutates a closed cycle (only a buy re-opens).
            if pos["status"] == "open":
                if e.get("sell_kind") == "partial":
                    # Partial sell (trim/scale-out): stays open, update remaining
                    # size (disclosed, else assume ~half trimmed).
                    if e.get("position_size_pct") is not None:
                        pos["size_pct"] = e["position_size_pct"]
                    elif pos.get("size_pct") is not None:
                        pos["size_pct"] = round(pos["size_pct"] / 2, 2)
                else:                            # full exit closes it
                    pos["status"] = "closed"
                    pos["closed_at"] = e.get("timestamp")
        elif st == "position":
            # Only a never-seen ticker opens here; a CLOSED position stays
            # closed (disclosures recap old holdings — only a buy re-opens).
            if pos["status"] is None:           # disclosure implies it's held
                pos["status"] = "open"
                pos["opened_at"] = e.get("timestamp")
            # A recap must not mutate a CLOSED position's sold-cycle fields.
            if pos["status"] != "closed":
                if e.get("position_size_pct") is not None:
                    pos["size_pct"] = e["position_size_pct"]
                if pos["entry_price"] is None and e.get("entry_price"):
                    pos["entry_price"] = e["entry_price"]
                if pos["trade_date"] is None and e.get("trade_date"):
                    pos["trade_date"] = e["trade_date"]
                if e.get("stop_loss") is not None:
                    pos["stop_loss"] = e["stop_loss"]
                if e.get("target") is not None:
                    pos["target"] = e["target"]

    result = sorted(positions.values(),
                    key=lambda p: (p["account"], p["ticker"]))
    write_json_atomic(positions_file, result)
    return result


def main():
    positions = reconcile()
    n_open = sum(1 for p in positions if p["status"] == "open")
    n_closed = sum(1 for p in positions if p["status"] == "closed")
    print(f"Reconciled -> {len(positions)} positions "
          f"({n_open} open, {n_closed} closed) -> {POSITIONS_FILE}")


if __name__ == "__main__":
    main()
