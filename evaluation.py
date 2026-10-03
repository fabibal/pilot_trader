"""Daily-bar publication/observation replay with explicit execution assumptions."""
from datetime import datetime, time, timedelta, timezone
from statistics import mean, median
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
HORIZON_SESSIONS = 5
ROUNDTRIP_COST_BPS = 20


def instant(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        return None


def session_open(day, asset_type="stock"):
    d = datetime.fromisoformat(str(day)[:10]).date()
    return datetime.combine(d, time(0) if asset_type == "crypto" else time(9, 30),
                            timezone.utc if asset_type == "crypto" else NEW_YORK)


def replay(position, ohlc, *, observed=False, benchmark=None, now=None,
           horizon=HORIZON_SESSIONS, cost_bps=ROUNDTRIP_COST_BPS):
    """Hypothetical next-open entry and Nth completed-session close.

    Conditional ideas are scored as ideas with an assumed unconditional entry;
    this does not claim the author's trigger fired, or an actual trade occurred.
    Missing OHLC and incomplete windows remain in the sample with a status.
    """
    field = "first_observed_at" if observed else "published_at"
    available = position.get(field) or (position.get("opened_at") if not observed else None)
    if not available and not observed:
        available = ((position.get("signals") or [{}])[0]).get("timestamp")
    dt = instant(available)
    if not dt:
        return {"status": "observation_unknown" if observed else "time_unknown"}
    if position.get("asset_type") not in ("stock", "crypto"):
        return {"status": "instrument_unknown"}
    if ohlc is None or ohlc.empty or not {"Open", "Close", "High", "Low"} <= set(ohlc.columns):
        return {"status": "unpriced"}
    now = now or datetime.now(timezone.utc)
    atype = position.get("asset_type") or "stock"
    bars, started = [], None
    for day, row in ohlc.sort_index().iterrows():
        opened = session_open(day, atype)
        if opened <= dt:
            continue
        if started is None and opened <= now:
            started = (str(day)[:10], row.get("Open"))
        complete_at = opened + (timedelta(days=1) if atype == "crypto" else timedelta(hours=6, minutes=30))
        if complete_at > now:
            continue
        values = [row.get(k) for k in ("Open", "Close", "High", "Low")]
        if any(v is None or v != v or v <= 0 for v in values):
            return {"status": "unpriced"}  # never silently skip a session
        bars.append((str(day)[:10], row))
        if len(bars) == horizon:
            break
    if len(bars) < horizon:
        # The entry open of a window still running (not entry_date: that key
        # marks a finished window for deduplicate_replays).
        pending = {"status": "pending", "completed_sessions": len(bars)}
        if started and started[1] == started[1] and started[1] and started[1] > 0:
            pending.update(started_date=started[0], started_price=float(started[1]))
        return pending
    entry_day, entry_bar = bars[0]
    exit_day, exit_bar = bars[-1]
    entry = float(entry_bar["Open"])
    sign = -1 if position.get("side") == "short" else 1
    gross = (float(exit_bar["Close"]) / entry - 1) * 100 * sign
    adverse = (min(float(r["Low"]) for _, r in bars) / entry - 1) * 100 if sign == 1 else (1 - max(float(r["High"]) for _, r in bars) / entry) * 100
    result = dict(status="scored", entry_date=entry_day, exit_date=exit_day,
                  entry_price=entry, exit_price=float(exit_bar["Close"]), gross_pct=gross,
                  net_pct=gross - cost_bps / 100, adverse_pct=adverse, cost_bps=cost_bps,
                  conditional=bool(position.get("entry_trigger")))
    if benchmark is not None and not benchmark.empty:
        frame = {str(d)[:10]: r for d, r in benchmark.iterrows()}
        if entry_day in frame and exit_day in frame:
            bentry, bexit = frame[entry_day].get("Open"), frame[exit_day].get("Close")
            if bentry and bexit and bentry == bentry and bexit == bexit:
                result["benchmark_pct"] = (bexit / bentry - 1) * 100 * sign
                result["excess_pp"] = gross - result["benchmark_pct"]
    return result


def replay_stats(results):
    statuses = {status: sum(r["status"] == status for r in results)
                for status in {r["status"] for r in results}}
    scored = [r for r in results if r["status"] == "scored"]
    returns = [r["net_pct"] for r in scored]
    gains, losses = sum(v for v in returns if v > 0), -sum(v for v in returns if v < 0)
    return dict(total=len(results), scored=len(scored), statuses=statuses,
        positive=sum(v > 0 for v in returns), mean_net_pct=mean(returns) if returns else None,
        median_net_pct=median(returns) if returns else None,
        mean_gross_pct=mean(r["gross_pct"] for r in scored) if scored else None,
        worst_pct=min(returns) if returns else None, best_pct=max(returns) if returns else None,
        profit_factor=gains / losses if losses else None,
        benchmark_n=sum("benchmark_pct" in r for r in scored),
        mean_benchmark_pct=mean(r["benchmark_pct"] for r in scored if "benchmark_pct" in r)
            if any("benchmark_pct" in r for r in scored) else None)


def deduplicate_replays(items):
    """Keep the first idea per ticker while its fixed window is outstanding."""
    kept, until, pending = [], {}, set()
    for position, result in sorted(items, key=lambda it: it[0].get("published_at") or it[0].get("opened_at") or ""):
        symbol = (position.get("ticker"), position.get("side"))
        if symbol in pending:
            continue
        if result.get("entry_date") and result["entry_date"] <= until.get(symbol, ""):
            continue
        kept.append((position, result))
        if result.get("exit_date"):
            until[symbol] = result["exit_date"]
        if result["status"] == "pending":
            pending.add(symbol)
    return kept
