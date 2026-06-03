#!/usr/bin/env python3
"""Idempotent order layer for the IBKR Grok mirror (LOGIC ONLY — no IBKR yet).

Implements the rules in IBKR_SPEC.md against a JSON ledger
(`data/orders.json`). It decides WHETHER and HOW MUCH to trade for a signal and
records the intended order with status="pending"; it does NOT talk to IBKR. The
connection layer (later) reads pending orders, converts USD notional to shares,
submits to IB Gateway, and flips status to filled/rejected.

Quantities are **USD notional** (see IBKR_SPEC.md §2), not shares.

Public API:
  check_already_actioned(signal_id)        -> bool
  risk_check(ticker, action, quantity)     -> (approved: bool, reason: str)
  queue_order(signal)                       -> order_id: str | None

All three accept an optional `path=` to the ledger file so tests can use a temp
ledger.
"""

import os
import uuid
from datetime import datetime, timezone

from reconcile import write_json_atomic

HOME = "/home/fbazsa/pilot_trader"
ORDERS_FILE = os.path.join(HOME, "data", "orders.json")

# --- spec constants (mirror IBKR_SPEC.md) ----------------------------------
MIRROR_PORTFOLIO = "grok"
MAX_TOTAL_EXPOSURE = 10_000.0          # §5 total book cap (USD)
MAX_POSITION_PCT = 0.10                # §2 max 10% per position
MAX_POSITION_USD = MAX_TOTAL_EXPOSURE * MAX_POSITION_PCT   # $1,000
MAX_ORDERS_PER_TICKER_PER_DAY = 1      # §5
ALLOWED_ASSET_TYPES = {"stock"}        # §6 stocks only
BUY_CONFIDENCE = {"high", "medium"}    # §3 thresholds

# §6 crypto guard for risk_check, which only sees the ticker (not asset_type).
# Defense-in-depth: queue_order also filters on the signal's asset_type.
CRYPTO_TICKERS = {"BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "AVAX", "LTC",
                  "BCH", "DOT", "LINK", "MATIC", "TRX", "USDT", "USDC"}

OPEN_STATUSES = ("pending", "filled")   # orders that count toward exposure


# --- ledger io --------------------------------------------------------------
def _load(path=ORDERS_FILE):
    if not os.path.exists(path):
        return []
    try:
        import json
        with open(path) as f:
            return json.load(f)
    except (ValueError, OSError):
        return []


def _save(orders, path=ORDERS_FILE):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    write_json_atomic(path, orders)


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _looks_like_crypto(ticker):
    t = (ticker or "").upper()
    return "-" in t or t in CRYPTO_TICKERS


# --- ledger queries ---------------------------------------------------------
def _net_notional(ticker, orders):
    """Current mirrored USD notional for a ticker = open BUYs minus SELLs."""
    net = 0.0
    for o in orders:
        if o["ticker"] != ticker or o["status"] not in OPEN_STATUSES:
            continue
        net += o["quantity"] if o["action"] == "BUY" else -o["quantity"]
    return max(net, 0.0)


def _open_buy_notional(orders):
    """Total open BUY notional across the book (toward MAX_TOTAL_EXPOSURE)."""
    return sum(o["quantity"] for o in orders
               if o["action"] == "BUY" and o["status"] in OPEN_STATUSES)


def _n_open_positions(orders):
    """Distinct tickers with a positive net mirrored notional."""
    tickers = {o["ticker"] for o in orders if o["status"] in OPEN_STATUSES}
    return sum(1 for t in tickers if _net_notional(t, orders) > 0)


# --- public API -------------------------------------------------------------
def check_already_actioned(signal_id, path=ORDERS_FILE):
    """True if an order has already been queued for this signal_id."""
    if signal_id is None:
        return False
    return any(o.get("signal_id") == signal_id for o in _load(path))


def get_order(order_id, path=ORDERS_FILE):
    """Return the ledger record for order_id, or None."""
    for o in _load(path):
        if o.get("order_id") == order_id:
            return o
    return None


def update_order_status(order_id, status, path=ORDERS_FILE, **fields):
    """Flip a queued order's status (and merge any extra fields, e.g. fill price /
    share count) atomically. Used by the IBKR connection layer after submission.
    Returns the updated record, or None if order_id is unknown."""
    orders = _load(path)
    updated = None
    for o in orders:
        if o.get("order_id") == order_id:
            o["status"] = status
            o.update(fields)
            o["updated_at"] = datetime.now(timezone.utc).isoformat()
            updated = o
            break
    if updated is not None:
        _save(orders, path)
    return updated


def risk_check(ticker, action, quantity, path=ORDERS_FILE):
    """Enforce IBKR_SPEC.md §5/§6 caps. Returns (approved, reason).

    - stocks only (rejects crypto-looking tickers)
    - max 1 order per ticker per UTC day
    - BUY only: per-position cap and total-exposure cap
    """
    orders = _load(path)

    if _looks_like_crypto(ticker):
        return False, f"{ticker} is crypto; stocks only (spec §6)"

    today = _today()
    todays = sum(1 for o in orders if o["ticker"] == ticker
                 and (o.get("timestamp") or "")[:10] == today)
    if todays >= MAX_ORDERS_PER_TICKER_PER_DAY:
        return False, (f"{ticker}: daily order limit reached "
                       f"({MAX_ORDERS_PER_TICKER_PER_DAY}/day, spec §5)")

    if action == "BUY":
        if quantity > MAX_POSITION_USD + 1e-9:
            return False, (f"${quantity:,.2f} exceeds per-position cap "
                           f"${MAX_POSITION_USD:,.2f} (spec §5)")
        if _open_buy_notional(orders) + quantity > MAX_TOTAL_EXPOSURE + 1e-9:
            return False, (f"would exceed total exposure cap "
                           f"${MAX_TOTAL_EXPOSURE:,.2f} (spec §5)")

    return True, "ok"


def queue_order(signal, path=ORDERS_FILE):
    """Validate a signal against the spec and, if it qualifies, append a
    pending order to the ledger. Returns the order_id, or None if the signal is
    not actionable / already actioned / fails risk checks.

    `signal` is a trades.json-style record (tickers[], signal_type, sell_kind,
    confidence, asset_type, tweet_id, ...).
    """
    sid = signal.get("tweet_id") or signal.get("signal_id")
    if check_already_actioned(sid, path):
        return None

    tickers = signal.get("tickers") or []
    if not tickers:
        return None
    ticker = tickers[0]
    st = signal.get("signal_type")
    asset_type = signal.get("asset_type")
    orders = _load(path)

    # --- decide action + USD notional from the spec --------------------------
    if st == "buy":
        if signal.get("confidence") not in BUY_CONFIDENCE:       # §3
            return None
        if asset_type not in ALLOWED_ASSET_TYPES:                # §6
            return None
        action = "BUY"
        n_open = _n_open_positions(orders) + 1                   # incl. this one
        quantity = round(min(MAX_TOTAL_EXPOSURE / n_open, MAX_POSITION_USD), 2)
    elif st == "sell":
        held = _net_notional(ticker, orders)
        if held <= 0:                                            # §4 no-op
            return None
        action = "SELL"
        if signal.get("sell_kind") == "partial":
            quantity = round(held * 0.5, 2)                      # reduce 50%
        else:
            quantity = round(held, 2)                            # full close
    else:
        # position-disclosures / none -> state, not an order (§3)
        return None

    approved, reason = risk_check(ticker, action, quantity, path)
    if not approved:
        return None

    order_id = "ord_" + uuid.uuid4().hex[:12]
    orders.append({
        "order_id": order_id,
        "signal_id": sid,
        "ticker": ticker,
        "action": action,
        "quantity": quantity,
        "status": "pending",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    _save(orders, path)
    return order_id
