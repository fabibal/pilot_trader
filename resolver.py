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

from datetime import datetime, timedelta, timezone
from evaluation import instant, session_open

EXPIRY_DAYS = 30
HIT_TARGET = "hit_target"
STOPPED_OUT = "stopped_out"
EXPIRED = "expired"
# Calls the influencer explicitly CLOSED (a sell tweet) without target/stop
# resolving first: classified by realized return. Excluding these biased the
# win rate — it was computed only over calls they hadn't talked about since.
CLOSED_WIN = "closed_win"
CLOSED_LOSS = "closed_loss"
# The call's own levels were already on the wrong side of its entry when it was
# made (see levels_inconsistent): excluded from the win rate like a live call.
INCONSISTENT = "inconsistent"
UNPRICED = "unpriced"
CLOSED_FLAT = "closed_flat"


def _date(s):
    try:
        return datetime.strptime((s or "")[:10], "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    except (ValueError, TypeError):
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


def levels_inconsistent(pos, entry):
    """True when the call's target or stop was already beyond its entry when
    the call was made: a long with target <= entry or stop >= entry, or the
    mirror image for a short. The price path would "hit" such a level on the
    very first bar, so the outcome says nothing about the call. Typical causes:
    an extraction error (a $31 target on a ~$228 stock), a breakout trigger
    read as a stop, or a recap quoting old levels."""
    if not entry:
        return False
    target, stop = pos.get("target"), pos.get("stop_loss")
    if is_long(pos, entry):
        return ((target is not None and target <= entry)
                or (stop is not None and stop >= entry))
    return ((target is not None and target >= entry)
            or (stop is not None and stop <= entry))


def resolve_position(pos, ohlc, until=None, entry=None):
    """Return {status, date, price} or None if the call is still live.

    `ohlc`: a pandas DataFrame indexed by 'YYYY-MM-DD' with High/Low columns,
    covering trade_date onward (or None if unavailable). status is one of
    hit_target / stopped_out / expired / inconsistent.

    `until`: optional 'YYYY-MM-DD' bound — walk the price path only up to this
    date. Used for calls the influencer explicitly closed: a target/stop hit
    INSIDE the holding window still counts, but the expiry rule does not apply
    (the caller classifies an unresolved closed call via resolve_closed).

    `entry`: the price the call was made at (stated or estimated). When given,
    a call whose levels were already past it resolves as inconsistent instead
    of as a day-one target hit or stop-out."""
    if pos.get("entry_status") in ("setup", "review", "legacy", "recap", "commentary"):
        return None
    entry = entry or pos.get("entry_price")
    if not entry:
        return {"status": UNPRICED, "date": None, "price": None}
    if ohlc is None or ohlc.empty:
        return {"status": UNPRICED, "date": None, "price": None}
    if levels_inconsistent(pos, entry):
        return {"status": INCONSISTENT, "date": None, "price": None}
    target = pos.get("target")
    stop = pos.get("stop_loss")
    tdate = pos.get("evaluation_date") or pos.get("trade_date") or (pos.get("opened_at") or "")[:10]
    td = _date(tdate)
    if not td:
        return None
    age_days = (datetime.now(timezone.utc) - td).days
    expiry_date = (td + timedelta(days=EXPIRY_DAYS)).strftime("%Y-%m-%d")
    available = instant(pos.get("evaluation_available_at") or pos.get("first_observed_at") or pos.get("opened_at"))
    close_time = instant(pos.get("closed_at")) if until else None
    changes = sorted(pos.get("level_history") or [], key=lambda h: h.get("effective_at") or "")

    if (target is not None or stop is not None) and ohlc is not None \
            and not ohlc.empty:
        long = is_long(pos)
        for day, row in ohlc.sort_index().iterrows():
            if str(day)[:10] < td.strftime("%Y-%m-%d"):
                continue
            if until and str(day)[:10] > until:
                break
            opened = session_open(day, pos.get("asset_type") or "stock")
            if available and opened <= available:
                continue
            # Daily OHLC cannot separate pre/post intraday exit or level update.
            if close_time and opened <= close_time < opened + timedelta(hours=6, minutes=30):
                continue
            ambiguous_update = False
            for change in changes:
                changed = instant(change.get("observed_at") or change.get("effective_at"))
                if not changed:
                    continue
                if changed < opened:
                    if change.get("target") is not None:
                        target = change["target"]
                    if change.get("stop_loss") is not None:
                        stop = change["stop_loss"]
                elif opened <= changed < opened + timedelta(hours=6, minutes=30):
                    ambiguous_update = True
            if ambiguous_update:
                continue
            # An expired open call must not turn into a winner months later.
            # Explicit closes retain their documented holding-window policy.
            if until is None and str(day)[:10] >= expiry_date:
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
                px = row.get("Open")
                fill = min(stop, px) if long and px else max(stop, px) if not long and px else stop
                return {"status": STOPPED_OUT, "date": str(day),
                        "price": fill}
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
    if pos.get("entry_status") in ("setup", "review", "legacy", "recap", "commentary"):
        return None
    if not entry or not exit_px:
        return None
    long = is_long(pos, entry)
    win = exit_px > entry if long else exit_px < entry
    return {"status": CLOSED_FLAT if exit_px == entry else CLOSED_WIN if win else CLOSED_LOSS,
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
    inconsistent = sum(1 for r in resolutions if r and r["status"] == INCONSISTENT)
    unpriced = sum(1 for r in resolutions if r and r["status"] == UNPRICED)
    flat = sum(1 for r in resolutions if r and r["status"] == CLOSED_FLAT)
    live = sum(1 for r in resolutions if r is None)
    decided = hit + stopped + closed_win + closed_loss + flat
    win_rate = round((hit + closed_win) / decided * 100, 1) if decided else None
    return {"hit": hit, "stopped": stopped, "expired": expired,
            "closed_win": closed_win, "closed_loss": closed_loss, "live": live,
            "inconsistent": inconsistent, "decided": decided,
            "win_rate": win_rate, "unpriced": unpriced, "closed_flat": flat,
            "total": len(resolutions), "coverage_pct": round(decided / len(resolutions) * 100, 1) if resolutions else None}


def is_long(pos, entry=None):
    if pos.get("side") in ("long", "short"):
        return pos["side"] == "long"
    return _is_long(entry or pos.get("entry_price"), pos.get("target"), pos.get("stop_loss"))


def return_pct(pos, entry, price):
    if not entry or not price:
        return None
    return round((price - entry) / entry * 100 * (1 if is_long(pos, entry) else -1), 1)
