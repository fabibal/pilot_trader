#!/usr/bin/env python3
"""Automatic execution glue for the pilot_trader AI-portfolio mirror.

Called from monitor.py at the end of each live run (and runnable standalone).
Reads trades.json, picks the qualifying NEW signals from the mirrored AI
portfolios (grok, claude, deepseek), and runs each through the idempotent order
layer (order_manager) into the IBKR paper account (ibkr_connector). Every
decision is logged to auto_trader.log; every executed trade pings Telegram.

Qualifying signal (IBKR_SPEC.md):
  - effective portfolio in {grok, claude, deepseek}   (MIRROR_PORTFOLIOS)
  - signal_type in {buy, sell}        (position recaps are state, not orders)
  - asset_type == stock               (no crypto)
  - buys: confidence in {high, medium}
  - timestamp date >= SINCE_DATE      (spec §8: don't back-fill old positions)
  - not already actioned              (idempotency via order_manager)

Note: order_manager keys orders on TICKER, not portfolio (IBKR_SPEC §1), so the
same ticker called by two portfolios collapses to one consolidated position (the
2nd same-day buy hits the 1-order-per-ticker-per-day cap).

Sizing, the $10k/$1k caps, the 1-order-per-ticker-per-day limit and the
skip-existing logic all live in order_manager.queue_order() / risk_check().

  run(no_trade=False) -> summary dict
"""

import json
import os
from datetime import datetime, timezone

import monitor                      # reuse load_env / _send_telegram / TRADES_FILE
import order_manager as om
import ibkr_connector as ibk

HOME = monitor.HOME
LOG_FILE = os.path.join(HOME, "auto_trader.log")
TRADES_FILE = monitor.TRADES_FILE

# Spec §8: act only on signals detected from 2026-06-02 onward (the mirror
# starts from an empty ledger; we do NOT buy into the pre-existing book).
SINCE_DATE = "2026-06-02"

# Account -> default portfolio when the LLM left portfolio null (mirrors
# pf_of() in dashboard.py). aifinancelabs posts carry the portfolio explicitly
# from the tweet text (grok/claude/deepseek/chatgpt), so they resolve regardless
# of this fallback.
ACCOUNT_DEFAULT_PF = {"grkportfolio": "grok", "theaiportfolios": "claude",
                      "aifinancelabs": "deepseek"}

# AI portfolios we mirror to the paper account. ChatGPT is intentionally excluded
# (no standalone handle; only surfaces via @aifinancelabs text).
MIRROR_PORTFOLIOS = {"grok", "claude", "deepseek"}


# --- logging ----------------------------------------------------------------
def _log(msg):
    """Append a UTC-stamped line to auto_trader.log and echo to stdout (so it
    also lands in monitor.log under cron)."""
    line = f"{datetime.now(timezone.utc).isoformat()}  {msg}"
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(f"[auto_trader] {msg}")


# --- signal filter ----------------------------------------------------------
def _effective_pf(sig):
    return sig.get("portfolio") or ACCOUNT_DEFAULT_PF.get(sig.get("account"))


def _qualifies(sig):
    if _effective_pf(sig) not in MIRROR_PORTFOLIOS:      # grok/claude/deepseek
        return False
    st = sig.get("signal_type")
    if st not in ("buy", "sell"):                        # not a recap/none
        return False
    if sig.get("asset_type") != "stock":                 # stocks only
        return False
    if st == "buy" and sig.get("confidence") not in om.BUY_CONFIDENCE:
        return False
    if (sig.get("timestamp") or "")[:10] < SINCE_DATE:   # spec §8 cutoff
        return False
    return True


def _telegram(text):
    monitor._send_telegram(text)


def _notify_executed(sig, res):
    """Telegram for an executed/queued trade, e.g.
    '🟢 BOUGHT 5x AVGO @ $459.50 (paper) | signal: grok high conf'."""
    pf = _effective_pf(sig) or "?"
    conf = sig.get("confidence") or "?"
    tag = f"{pf} {conf} conf" if sig.get("signal_type") == "buy" else pf
    n = res["shares"]
    tkr = res["ticker"]
    if res["status"] == "filled":
        verb = "BOUGHT" if res["action"] == "BUY" else "SOLD"
        emoji = "🟢" if res["action"] == "BUY" else "🔴"
        _telegram(f"{emoji} {verb} {n}x {tkr} @ ${res['fill_price']:.2f} "
                  f"(paper) | signal: {tag}")
    elif res["status"] == "submitted":
        verb = "BUY" if res["action"] == "BUY" else "SELL"
        _telegram(f"🟡 QUEUED {verb} {n}x {tkr} MarketOrder "
                  f"(paper, fills at next open) | signal: {tag}")


# --- main entry -------------------------------------------------------------
def run(no_trade=False, trades_path=None):
    """Execute qualifying new AI-portfolio signals. Returns a summary dict."""
    for p in monitor.TELEGRAM_ENVS:
        monitor.load_env(p)

    signals = monitor.load_json(trades_path or TRADES_FILE, [])
    candidates = [s for s in signals
                  if _qualifies(s)
                  and not om.check_already_actioned(
                      s.get("tweet_id") or s.get("signal_id"))]

    summary = {"candidates": len(candidates), "queued": 0, "filled": 0,
               "submitted": 0, "rejected": 0, "failed": 0}

    if not candidates:
        _log("no actionable new AI-portfolio signals this run")
        return summary

    _log(f"{len(candidates)} candidate AI-portfolio signal(s); "
         f"no_trade={no_trade}")

    ibh = None
    if not no_trade:
        try:
            ibh = ibk.connect()
            acct = ibk.get_account_value(ibh)
            _log(f"connected to IB Gateway — account {acct['account']} "
                 f"NetLiq ${acct['net_liquidation']:,.0f} "
                 f"market_open={ibk.is_market_open()}")
        except ibk.IBKRError as e:
            # Gateway unreachable: queue nothing so the next run retries cleanly.
            _log(f"FAILED to connect to IB Gateway, skipping execution: {e}")
            monitor.notify_telegram(f"auto_trader: IB Gateway unreachable, "
                                    f"{len(candidates)} signal(s) deferred")
            summary["failed"] = len(candidates)
            return summary

    try:
        for sig in candidates:
            tkr = (sig.get("tickers") or ["?"])[0]
            pf = _effective_pf(sig)
            sid = sig.get("tweet_id") or sig.get("signal_id")
            order_id = om.queue_order(sig)
            if order_id is None:
                _log(f"REJECTED [{pf}] {sig.get('signal_type','?').upper()} {tkr} "
                     f"(failed threshold/risk/sizing or no-op) signal={sid}")
                summary["rejected"] += 1
                continue
            summary["queued"] += 1
            order = om.get_order(order_id)
            if no_trade:
                _log(f"QUEUED (no-trade) [{pf}] {order['action']} {tkr} "
                     f"${order['quantity']:,.2f} order={order_id} signal={sid}")
                continue

            res = ibk.execute_order(order, ib=ibh)
            status = res["status"]
            summary[status] = summary.get(status, 0) + 1
            if status == "filled":
                _log(f"FILLED [{pf}] {res['action']} {res['shares']}x {tkr} @ "
                     f"${res['fill_price']:.2f} order={order_id} signal={sid}")
                _notify_executed(sig, res)
            elif status == "submitted":
                _log(f"SUBMITTED [{pf}] {res['action']} {res['shares']}x {tkr} "
                     f"(MarketOrder, {res['detail']}) order={order_id} "
                     f"signal={sid}")
                _notify_executed(sig, res)
            elif status == "rejected":
                _log(f"REJECTED-BY-IB {res['action']} {tkr}: {res['detail']} "
                     f"order={order_id} signal={sid}")
            else:
                _log(f"FAILED {res['action']} {tkr}: {res['detail']} "
                     f"order={order_id} signal={sid}")
    finally:
        if ibh is not None:
            ibh.disconnect()

    _log(f"run summary: {summary}")
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Execute new AI-portfolio signals (grok/claude/deepseek) on "
                    "IBKR paper.")
    ap.add_argument("--no-trade", action="store_true",
                    help="queue orders but do NOT submit to IBKR")
    a = ap.parse_args()
    run(no_trade=a.no_trade)
