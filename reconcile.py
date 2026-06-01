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
  - position       -> updates size (and opens it if not already open, since a
                      current-holding disclosure implies the position is held)

Run standalone:  python reconcile.py
Or import reconcile() from monitor.py after each fetch.
"""

import json
import os
import tempfile

HOME = "/home/fbazsa/pilot_trader"
TRADES_FILE = os.path.join(HOME, "trades.json")
POSITIONS_FILE = os.path.join(HOME, "positions.json")


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
    for e in events:
        tickers = e.get("tickers") or []
        if not tickers:
            continue
        ticker = tickers[0]
        key = (e.get("account"), e.get("portfolio"), ticker)
        pos = positions.get(key)
        if pos is None:
            pos = {
                "account": e.get("account"),
                "source_type": e.get("source_type", "portfolio"),
                "portfolio": e.get("portfolio"),
                "ticker": ticker,
                "asset_type": e.get("asset_type", "unknown"),
                "status": None,
                "entry_price": None,
                "stop_loss": None,
                "target": None,
                "size_pct": None,
                "trade_date": None,
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

        st = e.get("signal_type")
        if st == "buy":
            if pos["status"] != "open":
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
            pos["status"] = "closed"
            pos["closed_at"] = e.get("timestamp")
        elif st == "position":
            if pos["status"] is None:           # disclosure implies it's held
                pos["status"] = "open"
                pos["opened_at"] = e.get("timestamp")
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
