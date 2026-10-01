#!/usr/bin/env python3
"""
Reconcile the trades.json event log into a position-state model (positions.json).

trades.json is an append-only log of tweet-derived signals; the same holding is
disclosed many times. This folds those events, in chronological order, into one
current record per (account, portfolio, ticker, side), with prior_cycles
retaining closed cycles. Conditional/review signals have separate records:

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

import copy
import hashlib
from signal_semantics import normalize_event, is_junk_ticker, NON_TRADEABLE_TICKERS
import json
import os
import tempfile
from storage import single_writer

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
            json.dump(data, f, indent=2, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def fold_events(events):
    """Pure replay; each setup keeps its own identity and levels over time."""
    positions, seen = {}, set()
    for original in sorted(events, key=lambda e: e.get("timestamp") or ""):
        e = normalize_event(original)
        if (e.get("confidence") or "").lower() in GATED_CONFIDENCE:
            continue
        for ticker in e.get("tickers", []):
            kind = e["event_kind"]
            if kind == "commentary":
                continue
            account, side = e.get("account"), e.get("side")
            portfolio = pf_of(account, e.get("portfolio"))
            tid = e.get("tweet_id") or e.get("timestamp")
            identity = (account, tid, ticker, kind)
            if identity in seen:
                continue
            seen.add(identity)
            action, state = e.get("position_action"), e["entry_status"]
            separate = kind in ("setup", "review", "recap")
            key = (account, portfolio, ticker, side)
            if separate:
                key += (str(tid),)
            pos = positions.get(key)
            history = pos.get("prior_cycles", []) if pos else []
            if pos and pos["status"] == "closed" and action in ("open", "add"):
                history = history + [{k: copy.deepcopy(v) for k, v in pos.items() if k != "prior_cycles"}]
                pos = None
            if pos is None:
                pos = dict(
                    schema_version=3,
                    cycle_id=hashlib.sha256(repr((key, tid)).encode()).hexdigest()[:24],
                    account=account, portfolio=portfolio, ticker=ticker, side=side,
                    source_type=e.get("source_type", "portfolio"), asset_type=e.get("asset_type", "unknown"),
                    entry_status=state, event_kind=kind, status=None, prior_cycles=history,
                    entry_price=None, stop_loss=None, target=None, size_pct=None, trade_date=None,
                    opened_at=None, closed_at=None, first_observed_at=None, holding_thesis=None,
                    signals=[], level_history=[], exit_fills=[], remaining_fraction=1.0,
                    pnl_comparable=True, level_sources={},
                )
                positions[key] = pos
            pos["signals"].append({k: e.get(k) for k in
                ("tweet_id", "signal_id", "signal_type", "event_kind", "timestamp", "first_observed_at", "confidence", "url")})
            if e.get("asset_type") not in (None, "unknown"):
                pos["asset_type"] = e["asset_type"]
            if e.get("holding_thesis"):
                pos["holding_thesis"] = e["holding_thesis"]
            if separate:
                pos.update(status=state, published_at=e.get("timestamp"), first_observed_at=e.get("first_observed_at"),
                           entry_trigger=e.get("entry_trigger"), classification_reason=e.get("classification_reason"))
                for field in ("entry_price", "stop_loss", "target", "trade_date", "level_sources"):
                    pos[field] = e.get(field)
                continue
            if action in ("open", "add", "hold"):
                if pos["status"] is None:
                    pos.update(status="open", opened_at=e.get("timestamp"), first_observed_at=e.get("first_observed_at"))
                    pos["entry_price"] = e.get("entry_price")
                    pos["trade_date"] = e.get("trade_date")
                elif pos["status"] == "closed":
                    continue  # a holding disclosure does not reopen a cycle
                elif action == "add" and e.get("entry_price") != pos["entry_price"]:
                    pos["pnl_comparable"] = False  # unknown size cannot determine average cost
                if e.get("position_size_pct") is not None:
                    pos["size_pct"] = e["position_size_pct"]
                for field in ("target", "stop_loss"):
                    if e.get(field) is not None:
                        if pos[field] is None:
                            pos[field] = e[field]
                        pos["current_" + field] = e[field]
                if e.get("target") is not None or e.get("stop_loss") is not None:
                    pos["level_history"].append(dict(
                        effective_at=e.get("timestamp"), observed_at=e.get("first_observed_at"),
                        target=e.get("target"), stop_loss=e.get("stop_loss"), signal_id=e.get("signal_id"),
                    ))
                if e.get("level_sources"):
                    pos["level_sources"].update(e["level_sources"])
            elif action in ("reduce", "close"):
                if pos["status"] != "open":
                    # Keep the unpaired exit visible without inventing an entry.
                    pos.update(status="review", entry_status="review", classification_reason="unmatched_exit")
                    continue
                fraction = e.get("exit_fraction") if action == "reduce" else 1.0
                if not isinstance(fraction, (int, float)) or isinstance(fraction, bool) or not 0 < fraction <= 1:
                    fraction = None
                remaining = pos["remaining_fraction"]
                weight = remaining * fraction if remaining is not None and fraction is not None else None
                pos["exit_fills"].append(dict(price=e.get("exit_price"), fraction=weight,
                    timestamp=e.get("timestamp"), first_observed_at=e.get("first_observed_at"),
                    signal_id=e.get("signal_id"), level_source=(e.get("level_sources") or {}).get("exit_price")))
                if action == "close":
                    pos.update(status="closed", closed_at=e.get("timestamp"), remaining_fraction=0.0)
                else:
                    pos["remaining_fraction"] = remaining - weight if weight is not None else None
                    if e.get("position_size_pct") is not None:
                        pos["size_pct"] = e["position_size_pct"]
                    elif pos.get("size_pct") is not None and fraction is not None:
                        pos["size_pct"] = round(pos["size_pct"] * (1 - fraction), 4)
    return sorted(positions.values(), key=lambda p: (p.get("account") or "", p["ticker"], p["cycle_id"]))


def reconcile(trades_file=TRADES_FILE, positions_file=POSITIONS_FILE):
    with open(trades_file) as f:
        events = json.load(f)
    result = fold_events(events)
    write_json_atomic(positions_file, result)
    return result


@single_writer(lambda: TRADES_FILE)
def main():
    positions = reconcile()
    n_open = sum(1 for p in positions if p["status"] == "open")
    n_closed = sum(1 for p in positions if p["status"] == "closed")
    print(f"Reconciled -> {len(positions)} positions "
          f"({n_open} open, {n_closed} closed) -> {POSITIONS_FILE}")


if __name__ == "__main__":
    main()
