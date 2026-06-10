#!/usr/bin/env python3
"""
Resolve influencer (IncomeSharks) trade calls against the realized price path.

A call carries an entry plus a stop_loss and/or target. Walking the daily
high/low since the trade date tells us whether the target was hit, the stop was
taken out, or neither happened within a window (expired). This is what makes the
Influencers tab evaluative: it yields a win rate instead of an ever-growing list
of stale open calls.

Pure logic here; the dashboard supplies an OHLC fetcher (so caching / yfinance
batching live in one place).
"""

from datetime import datetime, timezone

EXPIRY_DAYS = 30
HIT_TARGET = "hit_target"
STOPPED_OUT = "stopped_out"
EXPIRED = "expired"
# Calls the influencer explicitly CLOSED (a sell tweet) without target/stop
# resolving first: classified by realized return. Excluding these biased the
# win rate — it was computed only over calls they hadn't talked about since.
CLOSED_WIN = "closed_win"
CLOSED_LOSS = "closed_loss"


def _date(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def _is_long(entry, target, stop):
    """Infer call direction. Long when the target is above the reference and the
    stop below it. Defaults to long when there isn't enough to tell."""
    if entry and target:
        return target > entry
    if entry and stop:
        return stop < entry
    if target and stop:
        return target > stop
    return True


def resolve_position(pos, ohlc, until=None):
    """Return {status, date, price} or None if the call is still live.

    `ohlc`: a pandas DataFrame indexed by 'YYYY-MM-DD' with High/Low columns,
    covering trade_date onward (or None if unavailable). status is one of
    hit_target / stopped_out / expired.

    `until`: optional 'YYYY-MM-DD' bound — walk the price path only up to this
    date. Used for calls the influencer explicitly closed: a target/stop hit
    INSIDE the holding window still counts, but the expiry rule does not apply
    (the caller classifies an unresolved closed call via resolve_closed)."""
    target = pos.get("target")
    stop = pos.get("stop_loss")
    tdate = pos.get("trade_date") or (pos.get("opened_at") or "")[:10]
    td = _date(tdate)
    if not td:
        return None
    age_days = (datetime.now(timezone.utc) - td).days

    if (target is not None or stop is not None) and ohlc is not None \
            and not ohlc.empty:
        long = _is_long(pos.get("entry_price"), target, stop)
        for day, row in ohlc.iterrows():
            if until and str(day)[:10] > until:
                break
            hi, lo = row.get("High"), row.get("Low")
            if hi is None or lo is None or hi != hi or lo != lo:  # NaN guard
                continue
            if long:
                hit = target is not None and hi >= target
                stopped = stop is not None and lo <= stop
            else:
                hit = target is not None and lo <= target
                stopped = stop is not None and hi >= stop
            # If both trip on the same bar, treat as stopped (conservative).
            if stopped:
                return {"status": STOPPED_OUT, "date": str(day),
                        "price": stop}
            if hit:
                return {"status": HIT_TARGET, "date": str(day),
                        "price": target}

    if until is None and age_days >= EXPIRY_DAYS:
        return {"status": EXPIRED, "date": None, "price": None}
    return None


def resolve_closed(pos, entry, exit_px, closed_date):
    """Resolution for a call the influencer explicitly closed where no
    target/stop hit inside the holding window: classify by realized return.
    Returns {status: closed_win|closed_loss, date, price}, or None when the
    entry or exit price is unknown (the caller should then exclude the call
    rather than pollute the live count)."""
    if not entry or not exit_px:
        return None
    long = _is_long(entry, pos.get("target"), pos.get("stop_loss"))
    win = exit_px > entry if long else exit_px < entry
    return {"status": CLOSED_WIN if win else CLOSED_LOSS,
            "date": closed_date, "price": exit_px}


def win_stats(resolutions):
    """Aggregate a list of resolve_position()/resolve_closed() results
    (None = still live). Explicitly-closed calls count as decided: wins are
    target hits + profitable closes, losses are stop-outs + losing closes."""
    hit = sum(1 for r in resolutions if r and r["status"] == HIT_TARGET)
    stopped = sum(1 for r in resolutions if r and r["status"] == STOPPED_OUT)
    expired = sum(1 for r in resolutions if r and r["status"] == EXPIRED)
    closed_win = sum(1 for r in resolutions if r and r["status"] == CLOSED_WIN)
    closed_loss = sum(1 for r in resolutions if r and r["status"] == CLOSED_LOSS)
    live = sum(1 for r in resolutions if r is None)
    decided = hit + stopped + closed_win + closed_loss
    win_rate = round((hit + closed_win) / decided * 100, 1) if decided else None
    return {"hit": hit, "stopped": stopped, "expired": expired,
            "closed_win": closed_win, "closed_loss": closed_loss, "live": live,
            "decided": decided, "win_rate": win_rate}
