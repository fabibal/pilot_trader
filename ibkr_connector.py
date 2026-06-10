#!/usr/bin/env python3
"""IBKR connection layer for the pilot_trader Grok mirror.

Talks to IB Gateway (paper) on 127.0.0.1:4002 via ib_insync. Reads a *pending*
order produced by order_manager.queue_order() (USD notional), converts it to a
whole-share MarketOrder, submits it, waits for a fill, and flips the ledger
status via order_manager. Also exposes paper portfolio + account readers.

Per IBKR_SPEC.md: STOCKS ONLY, max $10,000 total exposure, max $1,000/position.
order_manager already enforces these at queue time; the guards here are
defense-in-depth at the point of execution.

ib_insync is imported lazily inside connect() so that importing this module
(e.g. from monitor.py via auto_trader) never fails if the lib/gateway is absent.

Public API:
  connect(client_id=7)                  -> IB
  execute_order(order, ib=None)         -> dict (status/fill_price/shares/...)
  get_portfolio(ib=None)                -> list[dict]
  get_account_value(ib=None)            -> dict
  is_market_open(now=None)              -> bool
"""

import json
import os
from datetime import datetime, time as dtime, timezone
from zoneinfo import ZoneInfo

import order_manager as om

HOST = "127.0.0.1"
PORT = 4002
DEFAULT_CLIENT_ID = 7
FILL_TIMEOUT_S = 30          # how long to wait for a fill (spec: 30s)
STARTING_CAPITAL = 1_000_000.0   # fallback baseline if the file is unavailable
BASELINE_FILE = os.path.join(os.path.dirname(om.ORDERS_FILE) or ".",
                             "account_baseline.json")
EQUITY_FILE = os.path.join(os.path.dirname(om.ORDERS_FILE) or ".",
                           "equity_curve.json")

MAX_TOTAL_EXPOSURE = om.MAX_TOTAL_EXPOSURE   # $10,000
MAX_POSITION_USD = om.MAX_POSITION_USD       # $1,000

_NY = ZoneInfo("America/New_York")


class IBKRError(RuntimeError):
    """Connection / execution failure surfaced to the caller."""


# --- connection -------------------------------------------------------------
def connect(client_id=DEFAULT_CLIENT_ID, timeout=20):
    """Connect to IB Gateway paper on 127.0.0.1:4002. Raises IBKRError on
    failure (gateway down, port closed, login incomplete). `timeout` is the
    connect deadline (lower it for latency-sensitive callers like the dashboard)."""
    try:
        from ib_insync import IB
    except ImportError as e:
        raise IBKRError(f"ib_insync not installed: {e}")
    ib = IB()
    try:
        ib.connect(HOST, PORT, clientId=client_id, timeout=timeout)
    except Exception as e:   # ib_insync raises bare Exceptions / asyncio errors
        raise IBKRError(f"cannot connect to IB Gateway {HOST}:{PORT} "
                        f"(is it logged in?): {e!r}")
    if not ib.isConnected():
        raise IBKRError(f"IB Gateway {HOST}:{PORT} did not establish a session")
    return ib


def _maybe_connect(ib):
    """Return (ib, owns) — connect a throwaway session if caller passed none."""
    if ib is not None:
        return ib, False
    return connect(), True


# NYSE full-day closures for 2026 (regular holidays + observed). Early-close
# half-days (e.g. day after Thanksgiving) are NOT listed — those are caught at
# runtime by the Error-10349 -> MOO fallback in execute_order.
NYSE_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Jr. Day
    "2026-02-16",  # Washington's Birthday
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-06-19",  # Juneteenth
    "2026-07-03",  # Independence Day (observed; Jul 4 is a Saturday)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
}


# --- market hours -----------------------------------------------------------
def is_market_open(now=None):
    """US regular session check: Mon-Fri 09:30-16:00 America/New_York, excluding
    NYSE holidays (NYSE_HOLIDAYS_2026). Early-close half-days are not modelled
    here; an order placed after a half-day close is rejected by IB (10349) and
    execute_order resubmits it as Market-On-Open."""
    now = (now or datetime.now(tz=_NY)).astimezone(_NY)
    if now.weekday() >= 5:                      # Sat/Sun
        return False
    if now.strftime("%Y-%m-%d") in NYSE_HOLIDAYS_2026:
        return False
    return dtime(9, 30) <= now.time() <= dtime(16, 0)


# --- pricing ----------------------------------------------------------------
def _stock(ib, ticker):
    from ib_insync import Stock
    c = Stock(ticker.upper(), "SMART", "USD")
    ib.qualifyContracts(c)
    if not c.conId:
        raise IBKRError(f"could not qualify stock contract for {ticker}")
    return c


def _price(ib, contract):
    """Best available USD price for share conversion: live/delayed ask, then
    last/close, then the most recent historical close. Raises if all fail."""
    ib.reqMarketDataType(3)        # 3 = delayed if no live data subscription
    tkr = ib.reqMktData(contract, "", snapshot=False, regulatorySnapshot=False)
    price = None
    try:
        for _ in range(16):        # up to ~8s for ticks to arrive
            ib.sleep(0.5)
            for p in (tkr.ask, tkr.last, tkr.close, tkr.marketPrice()):
                if p is not None and p == p and p > 0:   # not None / not NaN
                    price = float(p)
                    break
            if price:
                break
    finally:
        ib.cancelMktData(contract)
    if price:
        return price
    bars = ib.reqHistoricalData(contract, "", "2 D", "1 hour", "TRADES",
                                useRTH=False)
    if bars:
        return float(bars[-1].close)
    raise IBKRError(f"no price available for {contract.symbol}")


# --- portfolio / account readers -------------------------------------------
def get_portfolio(ib=None):
    """Current paper positions with market value.
    Returns list of {ticker, sec_type, position, avg_cost, market_price,
    market_value, unrealized_pnl}."""
    ib, owns = _maybe_connect(ib)
    try:
        ib.reqMarketDataType(3)
        items = ib.portfolio()
        out = []
        for it in items:
            out.append({
                "ticker": it.contract.symbol,
                "sec_type": it.contract.secType,
                "position": it.position,
                "avg_cost": it.averageCost,
                "market_price": it.marketPrice,
                "market_value": it.marketValue,
                "unrealized_pnl": it.unrealizedPNL,
            })
        return out
    finally:
        if owns:
            ib.disconnect()


def get_account_value(ib=None):
    """Return {account, net_liquidation, available_funds, buying_power,
    total_cash, daily_pnl, unrealized_pnl, realized_pnl, baseline_netliq,
    total_pnl} for the paper account. total_pnl = NetLiquidation - baseline,
    where baseline is recorded once on first connect (issue 10). The daily/
    unrealized/realized P&L come from reqPnL (best-effort: NaN if unavailable)."""
    ib, owns = _maybe_connect(ib)
    try:
        accts = ib.managedAccounts()
        acct = accts[0] if accts else ""
        summ = {r.tag: r.value for r in ib.accountSummary(acct)}
        f = lambda k: float(summ.get(k, "nan"))
        netliq = f("NetLiquidation")

        # Daily / unrealized / realized P&L via reqPnL (needs a moment to stream).
        daily = unreal = real = float("nan")
        try:
            pnl = ib.reqPnL(acct)
            ib.sleep(2.0)
            daily, unreal, real = pnl.dailyPnL, pnl.unrealizedPnL, pnl.realizedPnL
            ib.cancelPnL(acct)
        except Exception:
            pass

        baseline = _account_baseline(acct, netliq)
        return {
            "account": acct,
            "net_liquidation": netliq,
            "available_funds": f("AvailableFunds"),
            "buying_power": f("BuyingPower"),
            "total_cash": f("TotalCashValue"),
            "daily_pnl": daily,
            "unrealized_pnl": unreal,
            "realized_pnl": real,
            "baseline_netliq": baseline,
            "total_pnl": (netliq - baseline
                          if netliq == netliq else float("nan")),
        }
    finally:
        if owns:
            ib.disconnect()


def snapshot_equity(netliq, account="", path=None):
    """Record at most ONE NetLiquidation point per UTC day to
    data/equity_curve.json (a daily equity curve for the paper-mirror chart).
    Updates the day's point if it already exists. Best-effort; never raises.
    Called at the end of each dashboard refresh that reaches IB."""
    path = path or EQUITY_FILE
    if not (netliq and netliq == netliq):
        return
    try:
        data = []
        if os.path.exists(path):
            with open(path) as f:
                data = json.load(f) or []
            if not isinstance(data, list):
                data = []
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        pt = {"date": today, "netliq": round(float(netliq), 2),
              "account": account, "ts": datetime.now(timezone.utc).isoformat()}
        if data and data[-1].get("date") == today:
            data[-1] = pt                      # update today's point
        else:
            data.append(pt)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except (OSError, ValueError):
        pass


def _account_baseline(account, netliq):
    """Issue 10: the account's baseline NetLiquidation for total-P&L, recorded
    ONCE on first connect to data/account_baseline.json (instead of a hardcoded
    $1M). Returns the stored baseline, creating it from the current NetLiq if the
    file is absent. Falls back to STARTING_CAPITAL on any IO error."""
    try:
        if os.path.exists(BASELINE_FILE):
            with open(BASELINE_FILE) as f:
                b = (json.load(f) or {}).get("baseline_netliq")
            if b:
                return float(b)
        if netliq and netliq == netliq:        # first connect: record it
            os.makedirs(os.path.dirname(BASELINE_FILE) or ".", exist_ok=True)
            with open(BASELINE_FILE, "w") as f:
                json.dump({"account": account, "baseline_netliq": float(netliq),
                           "recorded_at": datetime.now(_NY).isoformat()},
                          f, indent=2)
            return float(netliq)
    except (OSError, ValueError):
        pass
    return STARTING_CAPITAL


def _stock_exposure(ib):
    """Sum of current LONG stock market value (toward MAX_TOTAL_EXPOSURE)."""
    return sum(p["market_value"] for p in get_portfolio(ib)
               if p["sec_type"] == "STK" and (p["market_value"] or 0) > 0)


def _held_shares(ib, ticker):
    """Net shares currently held at IB for a stock `ticker` (0.0 if none)."""
    t = (ticker or "").upper()
    return sum(p.position for p in ib.positions()
               if p.contract.secType == "STK" and p.contract.symbol.upper() == t)


def _place_and_wait(ib, contract, action, shares, moo):
    """Place a MarketOrder (DAY, or Market-On-Open when `moo`) and wait for a
    terminal/resting state. Returns (trade, status, filled, avg_price, err_log)."""
    from ib_insync import MarketOrder
    mo = MarketOrder(action, shares)
    if moo:
        mo.tif = "OPG"
    trade = ib.placeOrder(contract, mo)
    # During RTH wait up to FILL_TIMEOUT_S for a fill; a MOO can't fill until the
    # open, so wait only briefly for it to reach a resting/rejected state.
    wait_cap = 5.0 if moo else FILL_TIMEOUT_S
    waited = 0.0
    while waited < wait_cap:
        ib.sleep(1.0)
        waited += 1.0
        st = trade.orderStatus.status
        if st == "Filled":
            break
        if st in ("Cancelled", "ApiCancelled", "Inactive"):
            # ⚠️ This paper account bounces DAY market orders with Error 10349
            # ("Order TIF was set to DAY based on order preset") and then
            # SELF-RECOVERS and fills during RTH. So keep polling through a 10349
            # bounce — only a cancel WITHOUT 10349 is genuinely terminal.
            codes = {l.errorCode for l in trade.log if l.errorCode}
            if 10349 not in codes:
                break
    st = trade.orderStatus.status
    filled = trade.orderStatus.filled or 0
    avg = trade.orderStatus.avgFillPrice or 0.0
    err = next((l for l in trade.log if l.errorCode), None)
    return trade, st, filled, avg, err


# --- order execution --------------------------------------------------------
def execute_order(order, ib=None):
    """Execute one pending order dict from order_manager.

    `order` keys: ticker, action ('BUY'/'SELL'), quantity_usd (or 'quantity'),
    signal_id, order_id (optional — when present the ledger status is updated).

    Converts USD notional -> whole shares at the live ask, submits a MarketOrder
    (IB holds it to the next open when the market is closed), waits up to 30s for
    a fill, and writes the result back to data/orders.json via order_manager.

    Returns a dict: {status, ticker, action, shares, fill_price, filled_qty,
    notional, order_id, market_open, detail}. `status` is one of:
      filled    — fully filled (fill_price set)
      submitted — accepted but not filled within the window (held for open /
                  still working); ledger stays 'pending'
      rejected  — IB rejected the order; ledger -> 'rejected'
      cancelled — no-op (e.g. SELL with no live position); ledger -> 'cancelled'
      deferred  — SELL with no live shares but a pending BUY in the ledger;
                  ledger stays 'pending' so auto_trader retries next run
      failed    — exception before/around submission; ledger -> 'failed'

    SELL share sizing reads the ACTUAL IB position (full=100%, partial=50% of
    held shares), never notional/price; BUY converts USD notional at the ask.
    """
    ticker = (order.get("ticker") or "").upper()
    action = (order.get("action") or "").upper()
    notional = float(order.get("quantity_usd") or order.get("quantity") or 0)
    order_id = order.get("order_id")
    signal_id = order.get("signal_id")
    result = {"status": "failed", "ticker": ticker, "action": action,
              "shares": 0, "fill_price": None, "filled_qty": 0,
              "notional": notional, "order_id": order_id,
              "market_open": is_market_open(), "detail": ""}

    def _ledger(status, **fields):
        if order_id:
            try:
                om.update_order_status(order_id, status, **fields)
            except Exception as e:        # ledger write must never crash a trade
                result["detail"] += f" [ledger-warn: {e}]"

    # Asset / size guards (defense-in-depth; order_manager already enforces).
    if om._looks_like_crypto(ticker):
        result["detail"] = f"{ticker} looks like crypto; stocks only (spec §6)"
        _ledger("rejected", reject_reason=result["detail"])
        result["status"] = "rejected"
        return result
    if action == "BUY" and notional > MAX_POSITION_USD + 1e-9:
        result["detail"] = (f"${notional:,.2f} exceeds per-position cap "
                            f"${MAX_POSITION_USD:,.2f}")
        _ledger("rejected", reject_reason=result["detail"])
        result["status"] = "rejected"
        return result

    ib, owns = _maybe_connect(ib)
    try:
        contract = _stock(ib, ticker)

        if action == "SELL":
            # Sell the ACTUAL IB position, never notional/price (spec §4):
            # full -> 100% of held shares, partial -> 50%. Never oversell.
            held = _held_shares(ib, ticker)
            if held <= 0:
                # No live shares, but the ledger still has a PENDING BUY for
                # this ticker (e.g. an MOO resting for the open): the position
                # just hasn't filled yet. Cancelling here would spend the sell
                # signal forever (idempotency) and orphan the position once the
                # buy fills. Leave the ledger 'pending' (no ib_order_id), so
                # auto_trader's unsubmitted-order retry re-executes it after
                # the buy is reconciled.
                if om.has_pending_buy(ticker):
                    result["detail"] = (f"no live {ticker} shares yet but a "
                                        f"BUY is still pending — deferring sell")
                    result["status"] = "deferred"
                    return result
                result["detail"] = f"no live {ticker} position to sell (no-op)"
                _ledger("cancelled", reject_reason=result["detail"])
                result["status"] = "cancelled"
                return result
            sell_kind = (order.get("sell_kind") or "full").lower()
            if sell_kind == "partial":
                shares = min(int(held), max(1, int(round(held * 0.5))))
            else:
                shares = int(held)               # full close
            result["shares"] = shares
            result["detail"] = f"selling {shares}/{int(held)} held ({sell_kind})"
        else:  # BUY: USD notional -> whole shares at the live ask
            price = _price(ib, contract)
            # Floor, never round: round() could exceed the per-position cap by
            # up to ~50% (e.g. $1000/$650 -> 2 shares = $1300), and max(1, ...)
            # forced 1 share of a >$1k stock (5x+ the cap). If even one share
            # doesn't fit the notional, reject — the cap can never admit it.
            shares = int(notional // price)
            if shares < 1:
                result["detail"] = (f"one share at ${price:,.2f} exceeds the "
                                    f"${notional:,.2f} position notional")
                _ledger("rejected", reject_reason=result["detail"])
                result["status"] = "rejected"
                return result
            result["shares"] = shares
            # Total-exposure guard (spec §5): long stock MV + this order's
            # notional must stay under the book cap.
            exposure = _stock_exposure(ib)
            if exposure + shares * price > MAX_TOTAL_EXPOSURE + 1.0:
                result["detail"] = (f"would exceed total exposure cap "
                                    f"${MAX_TOTAL_EXPOSURE:,.0f} "
                                    f"(open ${exposure:,.0f} + "
                                    f"${shares * price:,.0f})")
                _ledger("rejected", reject_reason=result["detail"])
                result["status"] = "rejected"
                return result

        # Spec §7: market order at the NEXT regular-session open. A plain DAY
        # market order submitted outside RTH is rejected by IB (Error 10349), so
        # when the market is closed we send a Market-On-Open (TIF=OPG) order that
        # rests for the opening auction. During RTH a normal DAY market order
        # fills immediately.
        moo = not is_market_open()
        trade, st, filled, avg, err = _place_and_wait(ib, contract, action,
                                                      shares, moo)

        # Holiday/half-day guard: our calendar can miss an early close or an
        # unlisted holiday. If a DAY order ended UNFILLED with Error 10349 (the
        # bounce did NOT self-recover, i.e. the market really is closed),
        # resubmit as Market-On-Open rather than dropping the signal as
        # 'rejected' (which idempotency would never retry). A 10349 that DID
        # recover to Filled during RTH skips this (st == 'Filled', filled > 0).
        if (not moo and filled == 0 and st != "Filled"
                and err is not None and getattr(err, "errorCode", 0) == 10349):
            _log_retry = "DAY hit 10349 (market closed) -> resubmit MOO"
            moo = True
            trade, st, filled, avg, err = _place_and_wait(ib, contract, action,
                                                          shares, True)
            result["detail"] = _log_retry + f"; ib_status={st}"
        else:
            result["detail"] = f"ib_status={st}" + (" (MOO)" if moo else "")
        result["market_open"] = not moo
        result["filled_qty"] = filled

        if st == "Filled" and filled > 0:
            result["status"] = "filled"
            result["fill_price"] = round(float(avg), 4)
            _ledger("filled", fill_price=result["fill_price"],
                    filled_qty=filled, shares=shares,
                    ib_order_id=trade.order.orderId,
                    ib_perm_id=trade.order.permId, ib_status=st)
        elif (st in ("Cancelled", "ApiCancelled", "Inactive") and filled == 0) \
                or (err and filled == 0):
            # filled == 0 guard: a cancel AFTER a partial fill must not be
            # recorded as 'rejected' — shares WERE traded. It falls through to
            # the 'submitted' branch (ledger pending + ib ids) and the next
            # reconcile_open_orders pass settles its true fate.
            reason = (f"{err.message} (code {err.errorCode})" if err else st)
            result["status"] = "rejected"
            result["detail"] = f"ib_status={st}: {reason}"
            _ledger("rejected", reject_reason=result["detail"],
                    ib_order_id=trade.order.orderId,
                    ib_perm_id=trade.order.permId, ib_status=st)
        else:
            # PreSubmitted / Submitted: accepted but not yet filled (held for the
            # open when market is closed, or still working). Keep ledger pending.
            result["status"] = "submitted"
            _ledger("pending", shares=shares, ib_order_id=trade.order.orderId,
                    ib_perm_id=trade.order.permId, ib_status=st)
        return result
    except IBKRError as e:
        # Order-level problem (e.g. unqualifiable ticker, no price) — reject it
        # cleanly rather than crashing the run. The connection is already open
        # at this point, so this is never a connect failure.
        result["status"] = "rejected"
        result["detail"] = f"{e}"
        _ledger("rejected", reject_reason=str(e))
        return result
    except Exception as e:
        result["status"] = "failed"
        result["detail"] = f"{e!r}"
        _ledger("failed", reject_reason=repr(e))
        return result
    finally:
        if owns:
            ib.disconnect()


# --- reconciliation ---------------------------------------------------------
def reconcile_open_orders(ib=None):
    """Poll IB for the fate of ledger orders still marked 'pending' (mainly
    Market-On-Open orders that filled at the open after execute_order returned).
    Flips each to filled / cancelled in data/orders.json. Returns a summary.

    Matches on ib_order_id against the current session's trades (ib.trades()
    covers open + done-this-session), falling back to permId when orderId is 0:
    after a Gateway restart IB reports prior-session orders with orderId=0, so
    permId is the only stable handle across the restart. Orders matching neither
    are left pending for the weekly position reconcile to flag."""
    orders = om._load()
    pending = [o for o in orders
               if o.get("status") == "pending" and o.get("ib_order_id")]
    summary = {"checked": len(pending), "filled": 0, "cancelled": 0,
               "still_pending": 0, "unmatched": 0}
    if not pending:
        return summary

    ib, owns = _maybe_connect(ib)
    try:
        ib.reqAllOpenOrders()
        ib.sleep(1.0)
        trades = ib.trades()
        # orderId first; permId fallback. Skip orderId 0 (ambiguous — every
        # orphaned prior-session order reports 0, so it must not key the index).
        by_oid = {t.order.orderId: t for t in trades if t.order.orderId}
        by_perm = {t.order.permId: t for t in trades if t.order.permId}
        # Orders that REACHED A TERMINAL STATE before a Gateway restart are in
        # neither open orders nor this session's trades — an MOO that filled at
        # the open and was then orphaned by the watchdog cycling the Gateway
        # stayed 'pending' forever. The completed-orders snapshot still reports
        # them; key by permId (orderId is 0 across restarts). Session trades
        # win on key collision (they carry live orderStatus).
        try:
            for t in ib.reqCompletedOrders(apiOnly=False):
                pid = t.order.permId
                if pid and pid not in by_perm:
                    by_perm[pid] = t
        except Exception:        # noqa: BLE001 - older gateways; non-fatal
            pass
        for o in pending:
            t = by_oid.get(o.get("ib_order_id"))
            if t is None and o.get("ib_perm_id"):
                t = by_perm.get(o.get("ib_perm_id"))
            if t is None:
                summary["unmatched"] += 1
                continue
            st = t.orderStatus.status
            filled = t.orderStatus.filled or 0
            if not filled:
                # Completed-order snapshots may carry the fill on the order
                # itself (filledQuantity) rather than orderStatus. Guard
                # against IB's UNSET_DECIMAL sentinel / NaN.
                fq = getattr(t.order, "filledQuantity", None)
                if isinstance(fq, (int, float)) and fq == fq and 0 < fq < 1e9:
                    filled = fq
            avg = t.orderStatus.avgFillPrice or 0.0
            if st == "Filled" and filled > 0:
                om.update_order_status(o["order_id"], "filled",
                                       fill_price=round(float(avg), 4),
                                       filled_qty=filled, ib_status=st)
                summary["filled"] += 1
            elif st in ("Cancelled", "ApiCancelled", "Inactive"):
                if filled > 0:
                    # Cancelled AFTER a partial fill: shares WERE traded.
                    # Marking it 'cancelled' would zero its ledger exposure
                    # and let a re-buy double the real position. Record it as
                    # filled (conservative: full quantity stays in exposure);
                    # the weekly position reconcile cross-checks share counts.
                    om.update_order_status(o["order_id"], "filled",
                                           fill_price=round(float(avg), 4),
                                           filled_qty=filled, ib_status=st,
                                           reject_reason=f"reconcile: partial "
                                                         f"fill then {st}")
                    summary["filled"] += 1
                else:
                    om.update_order_status(o["order_id"], "cancelled",
                                           ib_status=st,
                                           reject_reason=f"reconcile: {st}")
                    summary["cancelled"] += 1
            else:
                summary["still_pending"] += 1
        return summary
    finally:
        if owns:
            ib.disconnect()


def reconcile_positions(ib=None, log_path=None):
    """Compare expected net shares per ticker (from FILLED ledger orders) against
    actual IB positions; append any divergence to reconcile.log. Returns the list
    of divergences [{ticker, expected, actual, diff}]. (Weekly cron job, §8.)"""
    log_path = log_path or os.path.join(os.path.dirname(om.ORDERS_FILE) or ".",
                                        "..", "reconcile.log")
    orders = om._load()
    expected = {}
    for o in orders:
        if o.get("status") != "filled":
            continue
        q = o.get("filled_qty") or o.get("shares") or 0
        expected[o["ticker"]] = expected.get(o["ticker"], 0.0) + (
            q if o["action"] == "BUY" else -q)

    ib, owns = _maybe_connect(ib)
    try:
        actual = {}
        for p in ib.positions():
            if p.contract.secType == "STK":
                actual[p.contract.symbol.upper()] = (
                    actual.get(p.contract.symbol.upper(), 0.0) + p.position)
        tickers = set(expected) | set(actual)
        diverged = []
        for t in sorted(tickers):
            e = round(expected.get(t, 0.0), 4)
            a = round(actual.get(t, 0.0), 4)
            if abs(e - a) > 1e-6:
                diverged.append({"ticker": t, "expected": e, "actual": a,
                                 "diff": round(a - e, 4)})
        ts = datetime.now(_NY).isoformat()
        try:
            with open(log_path, "a") as f:
                if diverged:
                    for d in diverged:
                        f.write(f"{ts}  DIVERGENCE {d['ticker']}: "
                                f"ledger={d['expected']} ib={d['actual']} "
                                f"diff={d['diff']}\n")
                else:
                    f.write(f"{ts}  OK: ledger matches IB "
                            f"({len(tickers)} tickers)\n")
        except OSError:
            pass
        return diverged
    finally:
        if owns:
            ib.disconnect()


if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(description="IBKR connector utilities.")
    _ap.add_argument("--reconcile-orders", action="store_true",
                     help="poll IB and flip pending ledger orders to filled/cancelled")
    _ap.add_argument("--reconcile-positions", action="store_true",
                     help="compare ledger filled positions vs IB; log divergence")
    _args = _ap.parse_args()
    if _args.reconcile_orders:
        print("reconcile_open_orders:", reconcile_open_orders())
        raise SystemExit(0)
    if _args.reconcile_positions:
        div = reconcile_positions()
        print("divergences:", div if div else "none")
        raise SystemExit(0)
    # Default smoke test: print account + portfolio.
    _ib = connect()
    try:
        import json
        print("market_open:", is_market_open())
        print("account:", json.dumps(get_account_value(_ib), indent=2))
        print("portfolio:", json.dumps(get_portfolio(_ib), indent=2))
    finally:
        _ib.disconnect()
