#!/usr/bin/env python3
"""Unit tests for order_manager.py — the idempotent order layer (IBKR_SPEC.md).

Logic only; no IBKR. Quantities are USD notional. Each test uses a temp ledger.

Run:  .venv/bin/python -m pytest tests/test_order_manager.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import order_manager as om  # noqa: E402


def _sig(ticker, st, conf="high", sell_kind=None, asset="stock", tid="s1"):
    return {"tickers": [ticker], "signal_type": st, "confidence": conf,
            "sell_kind": sell_kind, "asset_type": asset, "tweet_id": tid}


def _seed(path, orders):
    """Write a ledger of pre-existing orders directly (bypasses queue logic)."""
    with open(path, "w") as f:
        json.dump(orders, f)


def _buy(ticker, qty, status="filled", ts="2026-01-01T00:00:00+00:00", sid=None):
    return {"order_id": "seed_" + ticker, "signal_id": sid or ("seed_" + ticker),
            "ticker": ticker, "action": "BUY", "quantity": qty,
            "status": status, "timestamp": ts}


# --- queue_order: BUY happy path -------------------------------------------
def test_queue_buy_creates_pending(tmp_path):
    p = str(tmp_path / "orders.json")
    oid = om.queue_order(_sig("AAPL", "buy", tid="t1"), path=p)
    assert oid is not None
    led = json.load(open(p))
    assert len(led) == 1
    o = led[0]
    assert o["ticker"] == "AAPL" and o["action"] == "BUY"
    assert o["status"] == "pending"
    assert o["signal_id"] == "t1"
    # first position: 10% cap binds (min(10000/1, 1000) == 1000)
    assert o["quantity"] == om.MAX_POSITION_USD


# --- idempotency ------------------------------------------------------------
def test_already_actioned_blocks_requeue(tmp_path):
    p = str(tmp_path / "orders.json")
    assert om.check_already_actioned("t1", path=p) is False
    om.queue_order(_sig("AAPL", "buy", tid="t1"), path=p)
    assert om.check_already_actioned("t1", path=p) is True
    # same signal again -> no new order
    assert om.queue_order(_sig("AAPL", "buy", tid="t1"), path=p) is None
    assert len(json.load(open(p))) == 1


# --- signal thresholds ------------------------------------------------------
def test_low_confidence_buy_rejected(tmp_path):
    p = str(tmp_path / "orders.json")
    assert om.queue_order(_sig("AAPL", "buy", conf="low", tid="t1"), path=p) is None
    assert om.queue_order(_sig("AAPL", "buy", conf="none", tid="t2"), path=p) is None
    assert not os.path.exists(p) or json.load(open(p)) == []


def test_position_signal_not_actionable(tmp_path):
    p = str(tmp_path / "orders.json")
    assert om.queue_order(_sig("AAPL", "position", tid="t1"), path=p) is None


# --- asset filter (stocks only, §6) ----------------------------------------
def test_crypto_buy_rejected(tmp_path):
    p = str(tmp_path / "orders.json")
    # via signal asset_type
    assert om.queue_order(_sig("BTC", "buy", asset="crypto", tid="t1"), path=p) is None
    # via risk_check ticker heuristic (known symbol and -USD form)
    assert om.risk_check("BTC", "BUY", 100, path=p)[0] is False
    assert om.risk_check("BTC-USD", "BUY", 100, path=p)[0] is False
    assert om.risk_check("AAPL", "BUY", 100, path=p)[0] is True


# --- risk caps (§5) ---------------------------------------------------------
def test_per_position_cap(tmp_path):
    p = str(tmp_path / "orders.json")
    approved, reason = om.risk_check("AAPL", "BUY", 1500, path=p)
    assert approved is False and "per-position" in reason
    assert om.risk_check("AAPL", "BUY", 1000, path=p)[0] is True


def test_daily_order_limit(tmp_path):
    p = str(tmp_path / "orders.json")
    # one order for AAPL dated today
    _seed(p, [_buy("AAPL", 1000, status="pending", ts=om._today() + "T09:00:00+00:00")])
    approved, reason = om.risk_check("AAPL", "BUY", 500, path=p)
    assert approved is False and "daily order limit" in reason
    # a different ticker is unaffected
    assert om.risk_check("MSFT", "BUY", 500, path=p)[0] is True


def test_total_exposure_cap(tmp_path):
    p = str(tmp_path / "orders.json")
    # 9,500 of open BUY notional already on the book
    _seed(p, [_buy(f"S{i}", 950) for i in range(10)])
    assert om._open_buy_notional(json.load(open(p))) == 9500
    assert om.risk_check("NEW", "BUY", 1000, path=p)[0] is False   # 10,500 > cap
    assert om.risk_check("NEW", "BUY", 400, path=p)[0] is True     # 9,900 ok


# --- sizing regimes (§2) ----------------------------------------------------
def test_equal_weight_binds_above_ten_positions(tmp_path):
    p = str(tmp_path / "orders.json")
    # 12 existing held names -> 13th buy sized by equal-weight, not the 10% cap
    _seed(p, [_buy(f"H{i}", 100) for i in range(12)])
    oid = om.queue_order(_sig("AAPL", "buy", tid="t1"), path=p)
    assert oid is not None
    new = [o for o in json.load(open(p)) if o["signal_id"] == "t1"][0]
    assert new["quantity"] == round(om.MAX_TOTAL_EXPOSURE / 13, 2)  # 769.23


# --- sell rules (§4) --------------------------------------------------------
def test_full_sell_closes(tmp_path):
    p = str(tmp_path / "orders.json")
    _seed(p, [_buy("AAPL", 1000)])                      # held from a prior day
    oid = om.queue_order(_sig("AAPL", "sell", sell_kind="full", tid="t9"), path=p)
    assert oid is not None
    sell = [o for o in json.load(open(p)) if o["signal_id"] == "t9"][0]
    assert sell["action"] == "SELL" and sell["quantity"] == 1000.0


def test_partial_sell_reduces_50(tmp_path):
    p = str(tmp_path / "orders.json")
    _seed(p, [_buy("AAPL", 1000)])
    oid = om.queue_order(_sig("AAPL", "sell", sell_kind="partial", tid="t9"), path=p)
    sell = [o for o in json.load(open(p)) if o["signal_id"] == "t9"][0]
    assert sell["quantity"] == 500.0


def test_sell_without_holding_is_noop(tmp_path):
    p = str(tmp_path / "orders.json")
    assert om.queue_order(_sig("TSLA", "sell", sell_kind="full", tid="t1"), path=p) is None
