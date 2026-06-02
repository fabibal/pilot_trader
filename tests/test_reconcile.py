#!/usr/bin/env python3
"""Unit tests for reconcile.py — folding the trades event log into positions.

Covers the bugs fixed in this round: full vs partial sells, the trim-as-close
regression, null-portfolio key resolution, the confidence gate, and dedup of
repeated disclosures of the same holding.

Run:  .venv/bin/python -m pytest tests/test_reconcile.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reconcile  # noqa: E402


def _event(account, ticker, signal_type, ts, **kw):
    e = {
        "account": account,
        "source_type": kw.get("source_type", "portfolio"),
        "portfolio": kw.get("portfolio"),
        "tweet_id": kw.get("tweet_id", ts),
        "timestamp": ts,
        "signal_type": signal_type,
        "sell_kind": kw.get("sell_kind"),
        "confidence": kw.get("confidence", "high"),
        "tickers": [ticker],
        "asset_type": kw.get("asset_type", "stock"),
        "position_size_pct": kw.get("size"),
        "entry_price": kw.get("entry"),
        "stop_loss": kw.get("stop"),
        "target": kw.get("target"),
        "trade_date": kw.get("trade_date"),
        "url": "http://x",
    }
    return e


def _run(events):
    """Reconcile a synthetic event list; return {(account,pf,ticker): pos}."""
    d = tempfile.mkdtemp()
    tf, pf = os.path.join(d, "t.json"), os.path.join(d, "p.json")
    with open(tf, "w") as f:
        json.dump(events, f)
    result = reconcile.reconcile(tf, pf)
    return {(p["account"], p["portfolio"], p["ticker"]): p for p in result}


def test_buy_opens():
    pos = _run([_event("grkportfolio", "AAPL", "buy", "2026-01-01T00:00:00Z",
                       portfolio="grok", entry=100, size=5)])
    p = pos[("grkportfolio", "grok", "AAPL")]
    assert p["status"] == "open"
    assert p["entry_price"] == 100
    assert p["size_pct"] == 5


def test_sell_full_closes():
    pos = _run([
        _event("grkportfolio", "TSLA", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", entry=200),
        _event("grkportfolio", "TSLA", "sell", "2026-02-01T00:00:00Z",
               portfolio="grok", sell_kind="full"),
    ])
    assert pos[("grkportfolio", "grok", "TSLA")]["status"] == "closed"


def test_sell_partial_reduces_keeps_open():
    """Partial sell with a disclosed remaining size keeps the position open."""
    pos = _run([
        _event("grkportfolio", "MSFT", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", size=10),
        _event("grkportfolio", "MSFT", "sell", "2026-02-01T00:00:00Z",
               portfolio="grok", sell_kind="partial", size=4),
    ])
    p = pos[("grkportfolio", "grok", "MSFT")]
    assert p["status"] == "open"
    assert p["size_pct"] == 4


def test_trim_halves_when_no_size():
    """The old bug: a trim (partial sell) without a stated size must REDUCE,
    not fully close. Defaults to ~half."""
    pos = _run([
        _event("grkportfolio", "NVDA", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", size=8),
        _event("grkportfolio", "NVDA", "sell", "2026-02-01T00:00:00Z",
               portfolio="grok", sell_kind="partial"),
    ])
    p = pos[("grkportfolio", "grok", "NVDA")]
    assert p["status"] == "open"
    assert p["size_pct"] == 4.0


def test_null_portfolio_maps_to_account_default():
    """A null-portfolio aifinancelabs event must key to 'deepseek', and merge
    with an explicit-deepseek event into ONE position (no double-count)."""
    pos = _run([
        _event("aifinancelabs", "AMD", "buy", "2026-01-01T00:00:00Z",
               portfolio=None, entry=150),
        _event("aifinancelabs", "AMD", "position", "2026-01-05T00:00:00Z",
               portfolio="deepseek", size=6),
    ])
    assert ("aifinancelabs", "deepseek", "AMD") in pos
    assert ("aifinancelabs", None, "AMD") not in pos
    assert len(pos) == 1
    assert pos[("aifinancelabs", "deepseek", "AMD")]["size_pct"] == 6


def test_low_confidence_gated_out():
    """Low/none-confidence signals stay in trades.json but must not create or
    move a position."""
    pos = _run([
        _event("grkportfolio", "INTC", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", confidence="low"),
    ])
    assert pos == {}


def test_duplicate_disclosures_dedup_to_one_position():
    """Repeated disclosures of the same holding fold into one record (with all
    signals recorded)."""
    pos = _run([
        _event("grkportfolio", "AAPL", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", entry=100),
        _event("grkportfolio", "AAPL", "position", "2026-01-02T00:00:00Z",
               portfolio="grok", size=5),
        _event("grkportfolio", "AAPL", "position", "2026-01-03T00:00:00Z",
               portfolio="grok", size=7),
    ])
    assert len(pos) == 1
    p = pos[("grkportfolio", "grok", "AAPL")]
    assert p["status"] == "open"
    assert p["entry_price"] == 100
    assert p["size_pct"] == 7          # latest disclosure wins
    assert len(p["signals"]) == 3


def test_position_disclosure_on_open_updates_size_only():
    """A position-disclosure on an already-open position updates size but does
    not re-open or change opened_at."""
    pos = _run([
        _event("grkportfolio", "GOOG", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", entry=140, size=10),
        _event("grkportfolio", "GOOG", "position", "2026-01-10T00:00:00Z",
               portfolio="grok", size=6),
    ])
    p = pos[("grkportfolio", "grok", "GOOG")]
    assert p["status"] == "open"
    assert p["size_pct"] == 6
    assert p["entry_price"] == 140                 # unchanged
    assert p["opened_at"] == "2026-01-01T00:00:00Z"  # buy date, not disclosure


def test_position_disclosure_on_unknown_ticker_opens_new():
    """A position-disclosure for a ticker never bought still opens a held
    position (a current-holding disclosure implies it is held)."""
    pos = _run([
        _event("grkportfolio", "ORCL", "position", "2026-03-01T00:00:00Z",
               portfolio="grok", size=4),
    ])
    p = pos[("grkportfolio", "grok", "ORCL")]
    assert p["status"] == "open"
    assert p["size_pct"] == 4
    assert p["opened_at"] == "2026-03-01T00:00:00Z"


def test_reopen_after_close():
    """A new buy on a previously fully-closed position re-opens it and clears
    closed_at."""
    pos = _run([
        _event("grkportfolio", "AMZN", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", entry=180),
        _event("grkportfolio", "AMZN", "sell", "2026-02-01T00:00:00Z",
               portfolio="grok", sell_kind="full"),
        _event("grkportfolio", "AMZN", "buy", "2026-03-01T00:00:00Z",
               portfolio="grok", entry=190),
    ])
    p = pos[("grkportfolio", "grok", "AMZN")]
    assert p["status"] == "open"
    assert p["closed_at"] is None
    assert p["opened_at"] == "2026-03-01T00:00:00Z"   # re-open date


def test_duplicate_tweet_id_ignored():
    """A repeated event with the same tweet_id is folded once. Critically a
    duplicate PARTIAL sell must NOT halve size twice."""
    pos = _run([
        _event("grkportfolio", "NVDA", "buy", "2026-01-01T00:00:00Z",
               portfolio="grok", size=8, tweet_id="t1"),
        _event("grkportfolio", "NVDA", "sell", "2026-02-01T00:00:00Z",
               portfolio="grok", sell_kind="partial", tweet_id="t2"),
        _event("grkportfolio", "NVDA", "sell", "2026-02-01T00:00:00Z",
               portfolio="grok", sell_kind="partial", tweet_id="t2"),  # dup
    ])
    p = pos[("grkportfolio", "grok", "NVDA")]
    assert p["status"] == "open"
    assert p["size_pct"] == 4.0          # halved once, not twice (would be 2.0)
    assert len(p["signals"]) == 2        # dup not appended


def test_moninvestor_adding_is_position_update():
    """@moninvestor 'adding'/'keeping' maps (by the LLM) to a position signal;
    reconcile must treat it as a size update on the SAME held position, not a
    second position. Influencer portfolio stays null."""
    pos = _run([
        _event("moninvestor", "SOFI", "buy", "2026-04-01T00:00:00Z",
               source_type="influencer", portfolio=None, entry=10, size=3),
        _event("moninvestor", "SOFI", "position", "2026-04-20T00:00:00Z",
               source_type="influencer", portfolio=None, size=5),
    ])
    assert len(pos) == 1
    p = pos[("moninvestor", None, "SOFI")]
    assert p["status"] == "open"
    assert p["size_pct"] == 5
    assert p["entry_price"] == 10
    assert len(p["signals"]) == 2
