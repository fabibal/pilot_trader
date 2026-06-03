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

import os
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import order_manager as om

HOST = "127.0.0.1"
PORT = 4002
DEFAULT_CLIENT_ID = 7
FILL_TIMEOUT_S = 30          # how long to wait for a fill (spec: 30s)

MAX_TOTAL_EXPOSURE = om.MAX_TOTAL_EXPOSURE   # $10,000
MAX_POSITION_USD = om.MAX_POSITION_USD       # $1,000

_NY = ZoneInfo("America/New_York")


class IBKRError(RuntimeError):
    """Connection / execution failure surfaced to the caller."""


# --- connection -------------------------------------------------------------
def connect(client_id=DEFAULT_CLIENT_ID):
    """Connect to IB Gateway paper on 127.0.0.1:4002. Raises IBKRError on
    failure (gateway down, port closed, login incomplete)."""
    try:
        from ib_insync import IB
    except ImportError as e:
        raise IBKRError(f"ib_insync not installed: {e}")
    ib = IB()
    try:
        ib.connect(HOST, PORT, clientId=client_id, timeout=20)
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


# --- market hours -----------------------------------------------------------
def is_market_open(now=None):
    """US regular session check (Mon-Fri 09:30-16:00 America/New_York).
    Holiday calendar is NOT modelled (spec open question) — a market order
    submitted on a holiday is simply held by IB until the next session."""
    now = (now or datetime.now(tz=_NY)).astimezone(_NY)
    if now.weekday() >= 5:                      # Sat/Sun
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
    total_cash} for the paper account."""
    ib, owns = _maybe_connect(ib)
    try:
        accts = ib.managedAccounts()
        acct = accts[0] if accts else ""
        summ = {r.tag: r.value for r in ib.accountSummary(acct)}
        f = lambda k: float(summ.get(k, "nan"))
        return {
            "account": acct,
            "net_liquidation": f("NetLiquidation"),
            "available_funds": f("AvailableFunds"),
            "buying_power": f("BuyingPower"),
            "total_cash": f("TotalCashValue"),
        }
    finally:
        if owns:
            ib.disconnect()


def _stock_exposure(ib):
    """Sum of current LONG stock market value (toward MAX_TOTAL_EXPOSURE)."""
    return sum(p["market_value"] for p in get_portfolio(ib)
               if p["sec_type"] == "STK" and (p["market_value"] or 0) > 0)


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
      failed    — exception before/around submission; ledger -> 'failed'
    """
    from ib_insync import MarketOrder

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
        price = _price(ib, contract)
        shares = max(1, round(notional / price))
        result["shares"] = shares

        # Total-exposure guard for buys (spec §5): current long stock MV + this
        # order's notional must stay under the book cap.
        if action == "BUY":
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
        mo = MarketOrder(action, shares)
        moo = not is_market_open()
        if moo:
            mo.tif = "OPG"
        trade = ib.placeOrder(contract, mo)

        # Wait for a fill up to FILL_TIMEOUT_S during RTH. Outside RTH the MOO
        # order can't fill until the open, so wait only briefly for it to reach
        # an accepted/resting (or rejected) state, then return.
        wait_cap = 5.0 if moo else FILL_TIMEOUT_S
        waited = 0.0
        while waited < wait_cap:
            ib.sleep(1.0)
            waited += 1.0
            if trade.isDone() or trade.orderStatus.status == "Filled":
                break

        st = trade.orderStatus.status
        filled = trade.orderStatus.filled or 0
        avg = trade.orderStatus.avgFillPrice or 0.0
        err = next((l for l in trade.log if l.errorCode), None)
        result["filled_qty"] = filled
        result["detail"] = f"ib_status={st}" + (" (MOO)" if moo else "")

        if st == "Filled" and filled > 0:
            result["status"] = "filled"
            result["fill_price"] = round(float(avg), 4)
            _ledger("filled", fill_price=result["fill_price"],
                    filled_qty=filled, shares=shares,
                    ib_order_id=trade.order.orderId, ib_status=st)
        elif st in ("Cancelled", "ApiCancelled", "Inactive") or (
                err and filled == 0):
            reason = (f"{err.message} (code {err.errorCode})" if err else st)
            result["status"] = "rejected"
            result["detail"] = f"ib_status={st}: {reason}"
            _ledger("rejected", reject_reason=result["detail"],
                    ib_order_id=trade.order.orderId, ib_status=st)
        else:
            # PreSubmitted / Submitted: accepted but not yet filled (held for the
            # open when market is closed, or still working). Keep ledger pending.
            result["status"] = "submitted"
            _ledger("pending", shares=shares, ib_order_id=trade.order.orderId,
                    ib_status=st)
        return result
    except IBKRError:
        raise
    except Exception as e:
        result["status"] = "failed"
        result["detail"] = f"{e!r}"
        _ledger("failed", reject_reason=repr(e))
        return result
    finally:
        if owns:
            ib.disconnect()


if __name__ == "__main__":
    # Smoke test: print account + portfolio.
    _ib = connect()
    try:
        import json
        print("market_open:", is_market_open())
        print("account:", json.dumps(get_account_value(_ib), indent=2))
        print("portfolio:", json.dumps(get_portfolio(_ib), indent=2))
    finally:
        _ib.disconnect()
