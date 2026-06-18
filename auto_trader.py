#!/usr/bin/env python3
"""Automatic execution glue for the pilot_trader AI-portfolio mirror.

Called from monitor.py at the end of each live run (and runnable standalone).
Reads trades.json, picks the qualifying NEW signals from the mirrored AI
portfolio (grok only — see MIRROR_PORTFOLIOS), and runs each through the
idempotent order layer (order_manager) into the IBKR paper account
(ibkr_connector). Analysis-only umbrellas (NO_MIRROR_ACCOUNTS) are excluded. Every
decision is logged to auto_trader.log; every executed trade pings Telegram.

Qualifying signal (IBKR_SPEC.md):
  - effective portfolio in MIRROR_PORTFOLIOS   (currently: grok only)
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

import fcntl
import json
import os
from datetime import datetime, timezone

import monitor                      # reuse load_env / _send_telegram / TRADES_FILE
import order_manager as om
import ibkr_connector as ibk

try:                               # ticker allowlist (issue 6); optional dep
    import yfinance as yf
except Exception:                  # noqa: BLE001
    yf = None

HOME = monitor.HOME
LOG_FILE = os.path.join(HOME, "auto_trader.log")
TRADES_FILE = monitor.TRADES_FILE

# Spec §8: act only on signals detected from 2026-06-02 onward (the mirror
# starts from an empty ledger; we do NOT buy into the pre-existing book).
SINCE_DATE = "2026-06-02"

# Stale-BUY guard: a rejected buy is never marked actioned, so it retries every
# run — e.g. a re-buy of a ticker we already hold is rejected daily by the
# one-active-position rule, and the moment a sell closes that position the
# weeks-old signal would fire a re-buy the source portfolio never intended.
# Legitimate deferrals (daily cap, gateway down, ticker-validation retry) all
# resolve within ~a day, so a buy older than this is dead, not deferred.
# SELLS are exempt: a late sell reads the ACTUAL IB position and no-ops if
# it's already closed, while skipping it could strand a real exit.
MAX_BUY_AGE_DAYS = 3

# Account-identity config is shared via accounts.py (single source of truth).
# aifinancelabs posts carry the portfolio explicitly from the tweet text
# (grok/claude/deepseek/chatgpt), so they resolve regardless of the fallback.
from accounts import (ACCOUNT_DEFAULT_PF, MIRROR_PORTFOLIOS,  # noqa: E402
                      NO_MIRROR_ACCOUNTS)

# --- circuit breaker (issue 4) ---------------------------------------------
DATA_DIR = os.path.join(HOME, "data")
BREAKER_FILE = os.path.join(DATA_DIR, "circuit_breaker.json")
LOCK_FILE = os.path.join(DATA_DIR, "auto_trader.lock")
MAX_CONSECUTIVE_REJECTS = 3        # halt after N IB rejects in a row
MAX_ORDERS_PER_DAY = 5             # across all tickers, per UTC day
DAILY_LOSS_LIMIT_PCT = 0.05        # halt if NetLiq drops >5% from day start


def _utc_today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_breaker():
    return monitor.load_json(BREAKER_FILE, {})


def _save_breaker(state):
    os.makedirs(DATA_DIR, exist_ok=True)
    monitor.write_json_atomic(BREAKER_FILE, state)


def _orders_today_count():
    """Orders recorded in the ledger today (all tickers, UTC day) that are
    live or executed. Cancelled no-ops / IB rejects / skips no longer consume
    the day's budget — a noisy day of no-ops used to block real trades."""
    today = _utc_today()
    return sum(1 for o in om._load()
               if o.get("status") in om.OPEN_STATUSES
               and (o.get("timestamp") or "")[:10] == today)


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
_TICKER_OK_CACHE = set()           # tickers confirmed valid (positives only)


def _valid_us_equity(ticker):
    """Issue 6: best-effort allowlist — is `ticker` a real US equity/ETF?
    Confirmed-valid tickers are cached. Returns False for a clearly bogus or
    non-equity symbol (so it's deferred, NOT marked actioned, and retried). Fails
    OPEN on a yfinance outage (returns True) so a transient gap never blocks a
    real signal; execute_order still rejects an unqualifiable ticker downstream."""
    t = (ticker or "").upper().strip()
    if not t:
        return False
    if yf is None or t in _TICKER_OK_CACHE:
        return True
    try:
        info = yf.Ticker(t).info or {}
    except Exception:              # noqa: BLE001 - yfinance flaky -> fail open
        return True
    qt = (info.get("quoteType") or "").upper()
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    if qt in ("CRYPTOCURRENCY", "CURRENCY", "INDEX"):
        return False
    valid = qt in ("EQUITY", "ETF") or price is not None
    if valid:
        _TICKER_OK_CACHE.add(t)
    return valid


def _effective_pf(sig):
    return sig.get("portfolio") or ACCOUNT_DEFAULT_PF.get(sig.get("account"))


def _qualifies(sig):
    if sig.get("account") in NO_MIRROR_ACCOUNTS:         # analysis-only umbrella
        return False                                     # (e.g. ralliesarena)
    if _effective_pf(sig) not in MIRROR_PORTFOLIOS:      # grok only
        return False
    st = sig.get("signal_type")
    if st not in ("buy", "sell"):                        # not a recap/none
        return False
    if sig.get("asset_type") != "stock":                 # stocks only
        return False
    # medium+ confidence for buys AND sells: a low/none-confidence (possibly
    # hallucinated) sell must not liquidate a real position. Gated sells are
    # alerted via Telegram (see _gated_sell handling in _run_locked) so a real
    # exit hiding in one is reviewed by a human, not silently dropped.
    if sig.get("confidence") not in om.BUY_CONFIDENCE:
        return False
    ts = sig.get("timestamp") or ""
    if ts[:10] < SINCE_DATE:                             # spec §8 cutoff
        return False
    if st == "buy":                                      # stale-buy guard
        try:
            age_d = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(ts)).days
        except (ValueError, TypeError):
            return False
        if age_d >= MAX_BUY_AGE_DAYS:
            return False
    return True


def _gated_sell(sig):
    """A sell that fails ONLY _qualifies' confidence gate: dangerous to act on
    (least-trusted extraction, real liquidation), but dangerous to drop
    silently (it may be a real exit). These are skipped + Telegram-alerted."""
    return (sig.get("signal_type") == "sell"
            and sig.get("account") not in NO_MIRROR_ACCOUNTS
            and sig.get("confidence") not in om.BUY_CONFIDENCE
            and _effective_pf(sig) in MIRROR_PORTFOLIOS
            and sig.get("asset_type") == "stock"
            and (sig.get("timestamp") or "")[:10] >= SINCE_DATE)


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
    """Issue 7: acquire an exclusive run lock (flock) so two instances (cron +
    a manual run) can't execute concurrently — that would double-order or clash
    on clientId 7. If the lock is held, skip this run. Delegates to _run_locked."""
    os.makedirs(DATA_DIR, exist_ok=True)
    lockf = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        _log("another auto_trader instance holds the lock; skipping this run")
        lockf.close()
        return {"candidates": 0, "skipped_locked": True}
    try:
        return _run_locked(no_trade=no_trade, trades_path=trades_path)
    finally:
        try:
            fcntl.flock(lockf, fcntl.LOCK_UN)
        finally:
            lockf.close()


def _run_locked(no_trade=False, trades_path=None):
    """Execute qualifying new AI-portfolio signals. Returns a summary dict."""
    for p in monitor.TELEGRAM_ENVS:
        monitor.load_env(p)

    signals = monitor.load_json(trades_path or TRADES_FILE, [])
    candidates = [s for s in signals
                  if _qualifies(s)
                  and not om.check_already_actioned(
                      s.get("tweet_id") or s.get("signal_id"))]

    summary = {"candidates": len(candidates), "queued": 0, "filled": 0,
               "submitted": 0, "rejected": 0, "cancelled": 0, "failed": 0}

    # Low/none-confidence sells: skip (record_skip dedups via the ledger so
    # this fires ONCE per signal) and page the human to review the tweet.
    for s in signals:
        sid = s.get("tweet_id") or s.get("signal_id")
        if _gated_sell(s) and not om.check_already_actioned(sid):
            tkr = (s.get("tickers") or ["?"])[0]
            om.record_skip(s, f"sell gated: confidence "
                              f"{s.get('confidence')!r} below medium")
            _log(f"GATED [{_effective_pf(s)}] SELL {tkr} "
                 f"confidence={s.get('confidence')} — NOT executed; "
                 f"review manually signal={sid}")
            monitor.notify_telegram(
                f"low-confidence SELL {tkr} ({_effective_pf(s)}) NOT executed "
                f"— review: {s.get('url')}")
            summary["gated_sells"] = summary.get("gated_sells", 0) + 1

    # Pending orders must be handled even on a run with no new signals: orders
    # WITH an ib_order_id (e.g. MOO resting for the open) need reconciling, and
    # orders WITHOUT one (deferred sells, --no-trade queues) need (re)submitting.
    pending_exists = any(o.get("status") == "pending" for o in om._load())
    if not candidates and not pending_exists:
        _log("no actionable new signals and no pending orders to reconcile")
        return summary

    if candidates:
        _log(f"{len(candidates)} candidate AI-portfolio signal(s); "
             f"no_trade={no_trade}")

    ibh = None
    breaker = {}
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
            if candidates:
                monitor.notify_telegram(f"auto_trader: IB Gateway unreachable, "
                                        f"{len(candidates)} signal(s) deferred")
            summary["failed"] = len(candidates)
            return summary

        # Issue 2: reconcile pending MOO orders that filled/cancelled at the open
        # BEFORE processing new signals (so exposure + history reflect reality).
        try:
            rec = ibk.reconcile_open_orders(ibh)
            if rec["checked"]:
                _log(f"reconciled pending orders: {rec}")
        except Exception as e:        # reconciliation must never block trading
            _log(f"reconcile_open_orders failed (non-fatal): {e!r}")

        # Issue 4: circuit breaker — daily reset + daily-loss-limit halt.
        breaker = _load_breaker()
        if breaker.get("date") != _utc_today():
            breaker = {"date": _utc_today(), "consecutive_rejects": 0,
                       "day_start_netliq": acct.get("net_liquidation"),
                       "halted": False, "halt_reason": None}
            _save_breaker(breaker)
        nl, start = acct.get("net_liquidation"), breaker.get("day_start_netliq")
        if (not breaker.get("halted") and start and nl and nl == nl
                and start == start and nl < start * (1 - DAILY_LOSS_LIMIT_PCT)):
            breaker["halted"] = True
            breaker["halt_reason"] = (
                f"daily loss limit: NetLiq ${nl:,.0f} down "
                f">{DAILY_LOSS_LIMIT_PCT*100:.0f}% from day start ${start:,.0f}")
            _save_breaker(breaker)
            _log(f"HALT — {breaker['halt_reason']}")
            monitor.notify_telegram(
                f"⚠️ pilot_trader: execution halted — daily loss limit "
                f"(NetLiq ${nl:,.0f} vs day start ${start:,.0f})")

    # If halted (this run's loss check or a prior trigger today), do NOT queue or
    # place anything — skip so signals retry after the daily reset.
    if breaker.get("halted") and not no_trade:
        _log(f"execution halted ({breaker.get('halt_reason')}); skipping "
             f"{len(candidates)} candidate(s) until daily reset")
        summary["halted"] = True
        if ibh is not None:
            ibh.disconnect()
        return summary

    # (Re)submit ledger orders that were queued but never reached IB: sells
    # deferred while their BUY was still resting (status stayed 'pending' with
    # no ib_order_id), and anything queued during a --no-trade run. Without
    # this they sat 'pending' forever — reconcile only matches orders that
    # HAVE an ib_order_id. Runs after reconcile_open_orders, so a deferred
    # sell executes as soon as its buy is reconciled to 'filled'.
    if not no_trade and ibh is not None:
        for o in om._load():
            if o.get("status") != "pending" or o.get("ib_order_id"):
                continue
            try:
                res = ibk.execute_order(o, ib=ibh)
                _log(f"retried unsubmitted {o['action']} {o['ticker']} "
                     f"order={o['order_id']}: {res['status']} ({res['detail']})")
                summary["retried"] = summary.get("retried", 0) + 1
            except Exception as e:    # retry must never block new signals
                _log(f"retry of {o.get('order_id')} failed (non-fatal): {e!r}")

    daily_cap_alerted = False
    try:
        for sig in candidates:
            tkr = (sig.get("tickers") or ["?"])[0]
            pf = _effective_pf(sig)
            sid = sig.get("tweet_id") or sig.get("signal_id")

            # Issue 6: ticker allowlist — skip a bogus/unknown symbol BEFORE
            # queuing so it isn't marked actioned (it defers + retries).
            if not _valid_us_equity(tkr):
                _log(f"REJECTED [{pf}] {sig.get('signal_type','?').upper()} {tkr} "
                     f"(not a recognized US equity) signal={sid}")
                summary["rejected"] += 1
                continue

            # Issue 4: global daily order cap — defer the rest to the next UTC day
            # (don't queue, so they aren't marked actioned and retry tomorrow).
            if not no_trade and _orders_today_count() >= MAX_ORDERS_PER_DAY:
                if not daily_cap_alerted:
                    daily_cap_alerted = True
                    _log(f"daily order cap reached ({MAX_ORDERS_PER_DAY}/day); "
                         f"deferring remaining candidate(s)")
                    monitor.notify_telegram(
                        f"pilot_trader: daily order cap ({MAX_ORDERS_PER_DAY}) "
                        f"reached; remaining signals deferred to tomorrow")
                summary["deferred"] = summary.get("deferred", 0) + 1
                continue

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
                breaker["consecutive_rejects"] = 0
            elif status == "submitted":
                _log(f"SUBMITTED [{pf}] {res['action']} {res['shares']}x {tkr} "
                     f"(MarketOrder, {res['detail']}) order={order_id} "
                     f"signal={sid}")
                _notify_executed(sig, res)
                breaker["consecutive_rejects"] = 0
            elif status == "cancelled":
                _log(f"CANCELLED (no-op) [{pf}] {res['action']} {tkr}: "
                     f"{res['detail']} order={order_id} signal={sid}")
            elif status == "deferred":
                # SELL whose BUY is still resting: ledger stays pending; the
                # unsubmitted-order retry re-executes it after the buy fills.
                _log(f"DEFERRED [{pf}] {res['action']} {tkr}: {res['detail']} "
                     f"order={order_id} signal={sid}")
            elif status == "rejected":
                _log(f"REJECTED-BY-IB {res['action']} {tkr}: {res['detail']} "
                     f"order={order_id} signal={sid}")
                breaker["consecutive_rejects"] = (
                    breaker.get("consecutive_rejects", 0) + 1)
            else:   # failed (exception / gateway hiccup) — neutral for the streak
                _log(f"FAILED {res['action']} {tkr}: {res['detail']} "
                     f"order={order_id} signal={sid}")

            # Persist the breaker and halt after N consecutive IB rejects.
            _save_breaker(breaker)
            if breaker.get("consecutive_rejects", 0) >= MAX_CONSECUTIVE_REJECTS:
                breaker["halted"] = True
                breaker["halt_reason"] = (
                    f"{MAX_CONSECUTIVE_REJECTS} consecutive IB rejects")
                _save_breaker(breaker)
                _log(f"HALT — {breaker['halt_reason']}")
                monitor.notify_telegram(
                    "⚠️ pilot_trader: execution halted after "
                    f"{MAX_CONSECUTIVE_REJECTS} consecutive rejects")
                summary["halted"] = True
                break
    finally:
        if ibh is not None:
            ibh.disconnect()

    _log(f"run summary: {summary}")
    return summary


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Execute new AI-portfolio signals (grok only) on "
                    "IBKR paper.")
    ap.add_argument("--no-trade", action="store_true",
                    help="queue orders but do NOT submit to IBKR")
    a = ap.parse_args()
    run(no_trade=a.no_trade)
