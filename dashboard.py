#!/usr/bin/env python3
"""
Plotly Dash dashboard for the influencer trade-call / analysis-digest monitor.

Reads:
  * trades.json    — the signal event log (influencer signals + resolutions)
  * positions.json — reconciled (account, ticker) positions
  * data/*_summaries.json, *_current_view.json — the YouTube/X analysis digests

Current/historical prices come from yfinance (cached 1h). Returns use the
position's entry_price when known, else estimate entry from the close on the
actual trade_date (marked "*"). Auto-refreshes every 60s.

Visual style matches the other dashboards on this host (paper_trader /
polymarket_bot): GitHub-dark palette, monospace, #161b22 cards on a #0d1117
background.

Served on port 8051 (host exposure controlled in docker-compose.yml).
Run with the project venv:
    /home/fbazsa/pilot_trader/.venv/bin/python dashboard.py
"""

import glob
import json
import os
import re
import threading
import time
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf
from dash import (Dash, dash_table, dcc, html, Input, Output, State, ALL,
                  no_update)

import resolver

HOME = "/home/fbazsa/pilot_trader"
TRADES_FILE = "/home/fbazsa/pilot_trader/trades.json"
POSITIONS_FILE = "/home/fbazsa/pilot_trader/positions.json"
STATE_FILE = "/home/fbazsa/pilot_trader/.monitor_state.json"
ENV_FILE = "/home/fbazsa/pilot_trader/.env"
STALE_HOURS = 8           # cron runs every 4h; >8h means a run was missed
REFRESH_MS = 60_000
PORT = 8051

# All stored timestamps are UTC (ISO with +00:00, or naive UTC epochs); the
# dashboard DISPLAYS everything in Budapest local time (CET/CEST, UTC+1/+2).
DISPLAY_TZ = ZoneInfo("Europe/Budapest")


def _to_local(dt, fmt):
    """Format a datetime (UTC-aware, or naive-assumed-UTC) in Budapest time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DISPLAY_TZ).strftime(fmt)


def _iso_to_local(iso, fmt):
    """Format a UTC ISO-8601 timestamp string in Budapest time."""
    return _to_local(datetime.fromisoformat(iso), fmt)


def _local_date(val):
    """Date (YYYY-MM-DD) in Budapest. A full ISO timestamp is converted (the
    day can shift vs UTC); a bare date with no time component is returned as-is
    (a date alone has no instant to convert)."""
    if not val:
        return val
    if "T" in val:
        return _iso_to_local(val, "%Y-%m-%d")
    return val[:10]


def _local_stamp(val):
    """'YYYY-MM-DD HH:MM' in Budapest for an ISO timestamp; a bare date or an
    unparseable value falls back to its date part."""
    if not val or "T" not in val:
        return (val or "")[:10]
    try:
        return _iso_to_local(val, "%Y-%m-%d %H:%M")
    except ValueError:
        return val[:10]

# --- API credits (GetXAPI) --------------------------------------------------
# GetXAPI exposes account credits at GET /account/me (-> credits_remaining).
# Anthropic has no remaining-credit-balance endpoint, so only GetXAPI is shown.
GETXAPI_BASE = "https://api.getxapi.com"
CREDITS_REFRESH_MS = 3_600_000   # 60 min — don't hammer the credits API
CREDITS_LOW_USD = 1.00           # below this, show the balance in red

# LLM spend telemetry written per run by monitor.log_cost().
COST_LOG_FILE = "/home/fbazsa/pilot_trader/data/cost_log.json"


def _load_env(path):
    """Minimal .env loader (dashboard runs without the monitor's anthropic dep,
    so we don't import monitor.load_env). Only sets vars not already present."""
    if not os.path.exists(path):
        return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # The dashboard needs only the read-only credit display's credential.
        if k.strip() == "GETXAPI_KEY":
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ENV_FILE)

# Cached GetXAPI credits: {balance: float|None, fetched_at: epoch|None, ok: bool}
_credits_cache = {"balance": None, "fetched_at": None, "ok": False}

# Balance snapshots [(epoch, balance)] -> credits_days_left(). Kept 14 days in
# the writable cache dir so a restart doesn't reset the burn-rate window.
CREDITS_HISTORY_FILE = "/home/fbazsa/pilot_trader/data/cache/getxapi_credits.json"
CREDITS_HISTORY_DAYS = 14


def _load_credit_history():
    try:
        with open(CREDITS_HISTORY_FILE) as f:
            return [(float(t), float(b)) for t, b in json.load(f)]
    except (OSError, ValueError, TypeError):
        return []


_credit_history = _load_credit_history()


def _record_credits(balance, now):
    cutoff = now - CREDITS_HISTORY_DAYS * 86400
    _credit_history[:] = ([(t, b) for t, b in _credit_history if t >= cutoff]
                          + [(now, balance)])
    try:
        os.makedirs(os.path.dirname(CREDITS_HISTORY_FILE), exist_ok=True)
        tmp = CREDITS_HISTORY_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_credit_history, f)
        os.replace(tmp, CREDITS_HISTORY_FILE)
    except OSError:
        pass


def get_getxapi_credits():
    """Return the cached GetXAPI credits dict, refreshing at most every
    CREDITS_REFRESH_MS. Network failure leaves the last good value in place and
    flags ok=False so the UI can show a fetch error without blanking the card.
    Called by the background warmer; the status bar only reads the cache."""
    now = time.time()
    fa = _credits_cache["fetched_at"]
    if fa is not None and now - fa < CREDITS_REFRESH_MS / 1000:
        return _credits_cache
    # Claim the refresh slot up front: concurrent callbacks landing during a
    # slow 15s fetch then serve the cached value instead of stampeding GetXAPI.
    _credits_cache["fetched_at"] = now
    key = os.environ.get("GETXAPI_KEY")
    if not key:
        _credits_cache.update(fetched_at=now, ok=False)
        return _credits_cache
    try:
        req = urllib.request.Request(
            GETXAPI_BASE + "/account/me",
            headers={"Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        _credits_cache.update(balance=float(data.get("credits_remaining")),
                              fetched_at=now, ok=True)
        _record_credits(_credits_cache["balance"], now)
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        _credits_cache.update(fetched_at=now, ok=False)
    return _credits_cache


# Account classification is shared with monitor/reconcile via accounts.py
# (single source of truth).
from accounts import INFLUENCER_ACCOUNTS


def is_influencer(account):
    return account in INFLUENCER_ACCOUNTS


# Extracted "tickers" that are not instruments: an LLM placeholder, and the
# NYSE McClellan Oscillator (a breadth indicator traderstewie charts).
_NOT_TICKERS = {"NONE", "NYMO"}


def _is_ticker(ticker):
    return bool(ticker) and ticker.lstrip("$").upper() not in _NOT_TICKERS


def influencer_positions(positions):
    return [p for p in positions if is_influencer(p.get("account"))
            and _is_ticker(p.get("ticker"))]


# Yahoo lists a coin whose ticker an older coin already took under a numbered
# symbol: TICKER-USD is then another coin (SKY-USD is Skycoin at $0.014, not
# Sky at $0.076; ARB-USD trades at $0.0006, Arbitrum at $0.23) or does not exist
# (UNI, GRT, HYPE, SUI, MORPHO). STRK-USD happens to carry Starknet's price
# today although Yahoo names it Strike, so it is pinned to the explicit symbol.
# Indices take a caret. All checked against Yahoo on 2026-09-27.
_YF_CRYPTO_ALIASES = {
    "STRK": "STRK22691-USD", "SKY": "SKY33038-USD", "GRT": "GRT6719-USD",
    "ARB": "ARB11841-USD", "UNI": "UNI7083-USD", "MORPHO": "MORPHO34104-USD",
    "SUI": "SUI20947-USD", "HYPE": "HYPE32196-USD", "SOLANA": "SOL-USD",
}
_YF_ALIASES = {"RUT": "^RUT", "COMPQ": "^IXIC"}


def _yf_symbol(ticker, asset_type):
    """yfinance symbol for a ticker: crypto takes a -USD suffix (BTC ->
    BTC-USD), with the collision/index aliases above; a leading $ is dropped."""
    t = (ticker or "").lstrip("$")
    key = t.upper()
    if key in _YF_ALIASES:
        return _YF_ALIASES[key]
    if asset_type == "crypto" and t and "-" not in t:
        return _YF_CRYPTO_ALIASES.get(key, f"{t}-USD")
    return t

# GitHub-dark palette — matches paper_trader/dashboard.py and polymarket_bot.
C = {
    "bg":     "#0d1117",
    "card":   "#161b22",
    "border": "#30363d",
    "text":   "#e6edf3",
    "dim":    "#8b949e",
    "green":  "#3fb950",
    "red":    "#f85149",
    "blue":   "#58a6ff",
    "yellow": "#d29922",
    "purple": "#bc8cff",
    "orange": "#e3b341",
    "buy_bg":  "#0d3321",     # subtle dark-green row tint
    "sell_bg": "#2d1118",     # subtle dark-red row tint
}
MONO = "'Consolas', 'SF Mono', 'Menlo', monospace"

# --- price cache: the background warmer refreshes it off the request path
# (see _warm_loop); PRICE_TTL is only the request path's fallback expiry. ---
PRICE_TTL = 3600
# A past date with no close (dead or unlisted symbol) is retried this rarely.
HIST_MISS_TTL = 6 * 3600
_price_cache = {}       # ticker -> (price_or_None, fetched_at)
_hist_cache = {}        # (ticker, date_str) -> (price_or_None, fetched_at)
_fetch_state = {"last": None}   # epoch of the most recent live Yahoo fetch

# Historical closes are IMMUTABLE (the close on a past date never changes), so
# they are persisted to disk and never re-fetched. Keyed "TICKER|YYYY-MM-DD".
# data/cache is the one writable mount in the otherwise read-only container
# (docker-compose.yml): it holds only this derived cache, never ledgers.
DATA_DIR = "/home/fbazsa/pilot_trader/data"
CACHE_DIR = os.path.join(DATA_DIR, "cache")
PRICE_CACHE_FILE = os.path.join(CACHE_DIR, "price_cache.json")


def _load_hist_persist():
    try:
        with open(PRICE_CACHE_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


_hist_persist = _load_hist_persist()   # "TICKER|DATE" -> close (float)
# Warmer thread and request threads both mutate/dump the dict; without the lock
# json.dump can hit "dictionary changed size during iteration" (not an OSError).
_hist_lock = threading.Lock()


def _save_hist_persist():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = PRICE_CACHE_FILE + ".tmp"
        with _hist_lock:
            with open(tmp, "w") as f:
                json.dump(_hist_persist, f)
            os.replace(tmp, PRICE_CACHE_FILE)
    except OSError:
        pass


def _fetch_price(ticker):
    _fetch_state["last"] = time.time()
    try:
        tk = yf.Ticker(ticker)
        price = None
        try:
            price = tk.fast_info.get("last_price")
        except Exception:
            price = None
        if not price:
            hist = tk.history(period="1d")
            if not hist.empty:
                price = float(hist["Close"].iloc[-1])
        return float(price) if price else None
    except Exception:
        return None


def get_price(ticker):
    now = time.time()
    hit = _price_cache.get(ticker)
    if hit and now - hit[1] < PRICE_TTL:
        return hit[0]
    price = _fetch_price(ticker)
    _price_cache[ticker] = (price, now)
    return price


def _fetch_hist_close(ticker, date_str):
    _fetch_state["last"] = time.time()
    try:
        start = datetime.strptime(date_str, "%Y-%m-%d")
        tk = yf.Ticker(ticker)
        hist = tk.history(start=start.strftime("%Y-%m-%d"),
                          end=(start + timedelta(days=1)).strftime("%Y-%m-%d"))
        if hist.empty:   # weekend/holiday — widen to next few trading days
            hist = tk.history(start=start.strftime("%Y-%m-%d"),
                              end=(start + timedelta(days=5)).strftime("%Y-%m-%d"))
        if not hist.empty:
            return float(hist["Close"].iloc[0])
        return None
    except Exception:
        return None


def get_hist_close(ticker, date_str, max_age=PRICE_TTL):
    if not date_str:
        return None
    # Immutable on-disk cache first (past closes never change).
    pkey = f"{ticker}|{date_str}"
    if pkey in _hist_persist:
        return _hist_persist[pkey]
    now = time.time()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = (ticker, date_str)
    hit = _hist_cache.get(key)
    if hit:
        # A past date that had no close stays unpriced: retry it rarely, not
        # on every pass (dead symbols cost up to seconds per lookup).
        ttl = HIST_MISS_TTL if hit[0] is None and date_str < today else max_age
        if now - hit[1] < ttl:
            return hit[0]
    price = _fetch_hist_close(ticker, date_str)
    _hist_cache[key] = (price, now)
    # Persist only resolved closes for dates strictly in the past (immutable).
    if price is not None and date_str < today:
        with _hist_lock:
            _hist_persist[pkey] = price
        _save_hist_persist()
    return price


def warm_prices(symbols, max_age=PRICE_TTL):
    """Batch-fetch current prices for many symbols in ONE yf.download call,
    populating the per-symbol cache. Falls back to single fetches on gaps.
    Symbols cached less than `max_age` seconds ago are kept."""
    now = time.time()
    need = sorted({s for s in symbols if s and not (
        _price_cache.get(s) and now - _price_cache[s][1] < max_age)})
    if not need:
        return
    _fetch_state["last"] = now
    close = None
    try:
        data = yf.download(need, period="2d", progress=False, threads=True)
        if "Close" in data:
            close = data["Close"]
    except Exception:
        close = None
    for s in need:
        price = None
        try:
            if close is not None:
                col = close[s] if hasattr(close, "columns") and \
                    s in getattr(close, "columns", []) else close
                col = col.dropna()
                if len(col):
                    price = float(col.iloc[-1])
        except Exception:
            price = None
        if price is None:        # batch missed this symbol — try it alone
            price = _fetch_price(s)
        _price_cache[s] = (price, now)


# --- entry price -------------------------------------------------------------
# A stated entry further than this from the trade date's close is a recap of an
# old fill, not the price the call was made at.
RECAP_TOLERANCE = 0.30


def _trade_date(p):
    return p.get("trade_date") or (p.get("opened_at") or "")[:10] or None


def _entry_for(p, max_age=PRICE_TTL):
    """(entry, estimated) for a call. The stated entry_price wins unless it is
    more than RECAP_TOLERANCE away from the close on the trade date: then the
    tweet quoted an old fill (IncomeSharks' 'HOOD from $8' on a day HOOD closed
    at $84.84) and the call is priced from that close instead, marked
    estimated like any call without a stated entry."""
    sym = _yf_symbol(p.get("ticker"), p.get("asset_type") or "unknown")
    tdate = _trade_date(p)
    close = get_hist_close(sym, tdate, max_age=max_age) if tdate else None
    stated = p.get("entry_price")
    # NaN-guard: pandas coerces a JSON null entry_price to truthy NaN.
    if isinstance(stated, (int, float)) and stated == stated and stated > 0:
        if close and abs(stated / close - 1) > RECAP_TOLERANCE:
            return close, True
        return stated, False
    return close, close is not None


_ohlc_cache = {}   # (symbol, start) -> (DataFrame[High,Low] | None, ts)


def get_ohlc(symbol, start_date, max_age=PRICE_TTL):
    """Daily High/Low DataFrame (index = 'YYYY-MM-DD') from start_date to now,
    kept `max_age` seconds. Used to resolve influencer calls against the price
    path and to grade the Kendrick forecasts."""
    now = time.time()
    key = (symbol, start_date)
    hit = _ohlc_cache.get(key)
    if hit and now - hit[1] < max_age:
        return hit[0]
    df = None
    try:
        hist = yf.Ticker(symbol).history(start=start_date)
        if not hist.empty:
            hist = hist[["High", "Low"]].copy()
            hist.index = hist.index.strftime("%Y-%m-%d")
            df = hist[~hist.index.duplicated(keep="last")].sort_index()
    except Exception:
        df = None
    _ohlc_cache[key] = (df, now)
    return df


# --- data loading ------------------------------------------------------------
def load_trades():
    if not os.path.exists(TRADES_FILE):
        return pd.DataFrame()
    try:
        with open(TRADES_FILE) as f:
            rows = json.load(f)
    except (json.JSONDecodeError, OSError):
        return pd.DataFrame()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["date"] = df["timestamp"].apply(_local_date)
    df["ticker"] = df["tickers"].apply(
        lambda t: ", ".join(t) if isinstance(t, list) else "")
    df["link"] = df["url"].apply(lambda u: f"[↗ tweet]({u})" if u else "")
    return df


def load_positions():
    if not os.path.exists(POSITIONS_FILE):
        return []
    try:
        with open(POSITIONS_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


# Influencer (IncomeSharks) signals table — its own column set with the
# influencer-specific fields (asset type, stop loss, target).
INFLUENCER_TABLE_COLUMNS = [
    {"name": "DATE", "id": "date"},
    {"name": "TICKER", "id": "ticker"},
    {"name": "ASSET", "id": "asset_type"},
    {"name": "ACTION", "id": "signal_type"},
    {"name": "SIDE", "id": "side"},
    {"name": "ENTRY STATUS", "id": "entry_status"},
    {"name": "CONF", "id": "confidence"},
    {"name": "ENTRY $", "id": "entry_price"},
    {"name": "STOP $", "id": "stop_loss"},
    {"name": "TARGET $", "id": "target"},
    {"name": "TP1 $", "id": "tp1"},
    {"name": "TP2 $", "id": "tp2"},
    {"name": "TREND", "id": "chart_trend"},
    {"name": "CHART NOTES", "id": "chart_notes"},
    {"name": "TWEET", "id": "link", "presentation": "markdown"},
]


def _fmt_pct(v):
    return "n/a" if v is None else f"{v:+.1f}%"


def _color(v):
    if v is None:
        return C["dim"]
    return C["green"] if v > 0 else (C["red"] if v < 0 else C["dim"])


# --- styled html tables (dark theme) ----------------------------------------
_TH = {"color": C["dim"], "fontFamily": MONO, "fontSize": "0.68rem",
       "textTransform": "uppercase", "letterSpacing": "0.04em",
       "textAlign": "left", "padding": "6px 10px",
       "borderBottom": f"1px solid {C['border']}"}
_TD = {"color": C["text"], "fontFamily": MONO, "fontSize": "0.78rem",
       "textAlign": "left", "padding": "5px 10px",
       "borderBottom": f"1px solid {C['border']}"}


def _table(headers, rows, empty="No data", hide_sm=None):
    """rows: list of cells; each cell is str or (text, color).
    hide_sm: optional iterable of column indices hidden on phones (<=760px) via
    the .col-sm-hide CSS class, so wide tables fit a 390px screen."""
    if not rows:
        return html.Div(empty, style={"color": C["dim"], "fontSize": "0.8rem",
                                      "padding": "8px 2px"})
    hide_sm = set(hide_sm or ())

    def cls(i):
        return "col-sm-hide" if i in hide_sm else None

    head = html.Thead(html.Tr([html.Th(h, style=_TH, className=cls(i))
                               for i, h in enumerate(headers)]))
    body = []
    for r in rows:
        tds = []
        for i, c in enumerate(r):
            if isinstance(c, tuple):
                tds.append(html.Td(c[0], style={**_TD, "color": c[1]},
                                   className=cls(i)))
            else:
                tds.append(html.Td(c, style=_TD, className=cls(i)))
        body.append(html.Tr(tds))
    return html.Table([head, html.Tbody(body)],
                      style={"borderCollapse": "collapse", "width": "100%",
                             "marginTop": "10px"})


def _money(v):
    return f"${v:,.2f}" if v else "—"


# --- influencer (IncomeSharks) views ----------------------------------------
def influencer_signals_data(df, account=None):
    """Rows for the influencer signals DataTable (most recent first). If
    `account` is given, restrict to that one handle; else all influencers."""
    if df.empty:
        return []
    accts = {account} if account else INFLUENCER_ACCOUNTS
    sub = df[df["account"].isin(accts)].copy()
    if sub.empty:
        return []
    sub = sub.sort_values("timestamp", ascending=False)

    def _m(v):
        if not isinstance(v, (int, float)) or v != v or not v:  # v!=v catches NaN
            return "—"
        return f"${v:,.2f}"

    def _s(v):  # string cell: NaN (float, truthy) and empty -> em dash
        return v if isinstance(v, str) and v else "—"

    rows = []
    for _, r in sub.iterrows():
        rows.append({
            "date": r.get("date"),
            "ticker": r.get("ticker"),
            "asset_type": r.get("asset_type") or "unknown",
            "signal_type": _s(r.get("position_action")) if isinstance(r.get("position_action"), str) else r.get("signal_type"),
            "side": _s(r.get("side")),
            "entry_status": _s(r.get("entry_status")),
            "confidence": r.get("confidence"),
            "entry_price": _m(r.get("entry_price")),
            "stop_loss": _m(r.get("stop_loss")),
            "target": _m(r.get("target")),
            "tp1": _m(r.get("tp1")),
            "tp2": _m(r.get("tp2")),
            "chart_trend": _s(r.get("chart_trend")),
            "chart_notes": _s(r.get("chart_notes")),
            "link": r.get("link") or "",
        })
    return rows


_STATUS_LABEL = {resolver.HIT_TARGET: ("target hit", "green"),
                 resolver.STOPPED_OUT: ("stopped out", "red"),
                 resolver.EXPIRED: ("expired", "dim"),
                 resolver.CLOSED_WIN: ("closed (win)", "green"),
                 resolver.CLOSED_LOSS: ("closed (loss)", "red"),
                 resolver.INCONSISTENT: ("bad levels", "yellow")}


def influencer_resolutions(positions, account=None, max_age=PRICE_TTL):
    """List of (position, resolution|None) for influencer calls, resolved
    against the realized price path. Includes calls the influencer EXPLICITLY
    closed (sell tweet): a target/stop hit inside the holding window counts as
    usual, otherwise the call is classified by realized return — excluding
    closed calls computed the win rate only over calls they hadn't talked
    about since. An open call whose levels were already past its entry
    resolves as inconsistent (excluded from the win rate). If `account` is
    given, restrict to that one handle."""
    out = []
    cycles = [cycle for position in positions
              for cycle in [*position.get("prior_cycles", []), position]]
    for p in influencer_positions(cycles):
        status = p.get("status")
        if status not in ("open", "closed"):
            continue
        if account and p.get("account") != account:
            continue
        sym = _yf_symbol(p["ticker"], p.get("asset_type") or "unknown")
        tdate = _trade_date(p)
        ohlc = get_ohlc(sym, tdate, max_age=max_age) if tdate else None
        entry, _ = _entry_for(p, max_age=max_age)
        if status == "open":
            out.append((p, resolver.resolve_position(p, ohlc, entry=entry)))
            continue
        cdate = (p.get("closed_at") or "")[:10] or None
        res = resolver.resolve_position(p, ohlc, until=cdate, entry=entry)
        # The influencer's own close still classifies the call by realized
        # return when the path says nothing: no target/stop hit inside the
        # holding window, or levels that were inconsistent from the start.
        if cdate and (res is None or res["status"] == resolver.INCONSISTENT):
            res = resolver.resolve_closed(
                p, entry, get_hist_close(sym, cdate, max_age=max_age), cdate)
        if res is not None:    # unpriceable closed calls are excluded entirely
            out.append((p, res))
    return out


# A no-stop caveat only earns a spot on the card once it's the dominant case:
# most influencers set a stop on most calls, so this stays silent for them.
NO_STOP_CAVEAT_PCT = 50


def _win_rate_caveats(resolutions):
    """Diagnostics that make win_stats()'s exclusions visible on the dashboard:
    how many calls were dropped from the ratio (expired/live) and how many of
    those are currently underwater, plus how many calls structurally cannot
    ever resolve to a loss because no stop_loss was set. Current-price lookups
    only -- resolver.py still owns the win/loss classification itself."""
    excluded = [p for p, r in resolutions
                if r is None or r["status"] == resolver.EXPIRED]
    priced = underwater = 0
    for p in excluded:
        entry, _ = _entry_for(p)
        ret = resolver.return_pct(
            p, entry, get_price(_yf_symbol(p["ticker"],
                                           p.get("asset_type") or "unknown")))
        if ret is not None:     # side-aware: a short is underwater when up
            priced += 1
            if ret < 0:
                underwater += 1
    no_stop = sum(1 for p, _r in resolutions if p.get("stop_loss") is None)
    inconsistent = sum(1 for _p, r in resolutions
                       if r and r["status"] == resolver.INCONSISTENT)
    return {"excluded": len(excluded), "excluded_priced": priced,
            "excluded_underwater": underwater, "no_stop": no_stop,
            "inconsistent": inconsistent, "total": len(resolutions)}


def influencer_winrate_card(resolutions):
    s = resolver.win_stats([r for _, r in resolutions])
    c = _win_rate_caveats(resolutions)
    wr = "n/a" if s["win_rate"] is None else f"{s['win_rate']:.0f}%"
    wr_color = C["dim"] if s["win_rate"] is None else (
        C["green"] if s["win_rate"] >= 50 else C["red"])

    lines = [html.Div(children=[
        html.Span(wr, style={"color": wr_color, "fontWeight": "bold",
                             "fontSize": "1.1rem"}),
        html.Span(f" win rate  ({s['decided']} decided: "
                  f"{s['hit']} target / {s['stopped']} stopped / "
                  f"{s['closed_win'] + s['closed_loss']} closed)",
                  style={"color": C["dim"], "fontSize": "0.8rem"}),
    ])]

    if c["excluded"]:
        uw_txt = (f"{c['excluded_underwater'] / c['excluded_priced'] * 100:.0f}%"
                  if c["excluded_priced"] else "n/a")
        lines.append(html.Div(
            f"+ {c['excluded']} more calls excluded (expired/live) · "
            f"{uw_txt} of those are currently negative vs entry",
            style={"color": C["dim"], "fontSize": "0.76rem", "marginTop": "4px"}))

    if c["inconsistent"]:
        lines.append(html.Div(
            f"+ {c['inconsistent']} calls excluded for bad levels (target or "
            f"stop already past the entry when called -- they would count as "
            f"a day-one hit or stop-out)",
            style={"color": C["dim"], "fontSize": "0.76rem", "marginTop": "4px"}))

    if c["total"] and c["no_stop"] / c["total"] * 100 >= NO_STOP_CAVEAT_PCT:
        lines.append(html.Div(
            f"{c['no_stop'] / c['total'] * 100:.0f}% of calls have no "
            f"stop-loss set (only an explicit losing close can record "
            f"a loss for those calls)",
            style={"color": C["yellow"], "fontSize": "0.76rem",
                   "marginTop": "4px", "fontStyle": "italic"}))

    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderRadius": "8px", "padding": "12px 18px", "marginTop": "12px",
        "display": "inline-block"}, children=lines)


# Per-influencer descriptor + accent color (left border) for the header card.
INFLUENCER_META = {
    "IncomeSharks": ("Stocks + crypto trade calls", C["blue"]),
    "traderstewie": ("US equity swing setups", C["green"]),
}


def _hdr_metric(label, value, color, sub=None):
    return html.Div(children=[
        html.Div(label, style={"color": C["dim"], "fontSize": "0.62rem",
                               "textTransform": "uppercase",
                               "letterSpacing": "0.06em"}),
        html.Div(value, style={"color": color, "fontSize": "1.05rem",
                               "fontWeight": "bold"}),
        html.Div(sub or "", style={"color": C["dim"], "fontSize": "0.64rem",
                                   "minHeight": "0.8rem"}),
    ])


def _influencer_returns(account, resolutions):
    """(ticker, return%) for each open call of `account`."""
    rr = []
    for p, _res in (resolutions or []):
        if p.get("status") != "open":   # resolutions include closed calls
            continue
        entry, _ = _entry_for(p)
        cur = get_price(_yf_symbol(p["ticker"], p.get("asset_type") or "unknown"))
        rr.append((p["ticker"], resolver.return_pct(p, entry, cur)))
    return rr


def influencer_header_card(account, resolutions=None):
    """Per-influencer header card: @handle + descriptor + win rate + open call
    count + best performer. A left accent border in the handle's color makes
    each visually distinct."""
    desc, accent = INFLUENCER_META.get(account, ("", C["blue"]))
    rr = _influencer_returns(account, resolutions)
    valid = [(t, r) for t, r in rr if r is not None]
    best = max(valid, key=lambda x: x[1]) if valid else None

    metrics = []
    st = resolver.win_stats([r for _, r in (resolutions or [])])
    wr = st["win_rate"]
    wr_txt = "n/a" if wr is None else f"{wr:.0f}%"
    wr_color = C["dim"] if wr is None else (
        C["green"] if wr >= 50 else C["red"])
    metrics.append(_hdr_metric("win rate", wr_txt, wr_color,
                               f"{st['decided']} decided, "
                               f"{st['expired'] + st['live'] + st['inconsistent']}"
                               f" excl."))
    metrics.append(_hdr_metric("open calls", str(len(rr)), C["text"]))
    metrics.append(_hdr_metric("best", best[0] if best else "—",
                               _color(best[1] if best else None),
                               _fmt_pct(best[1]) if best else None))

    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderLeft": f"4px solid {accent}", "borderRadius": "8px",
        "padding": "14px 18px", "marginTop": "12px", "display": "flex",
        "flexWrap": "wrap", "alignItems": "center", "gap": "28px"}, children=[
        html.Div(style={"minWidth": "180px"}, children=[
            html.A(f"@{account}", href=f"https://x.com/{account}", target="_blank",
                   rel="noopener noreferrer",
                   style={"color": accent, "fontWeight": "bold",
                          "fontSize": "1.15rem", "textDecoration": "none"}),
            html.Div(desc, style={"color": C["dim"], "fontSize": "0.72rem",
                                  "marginTop": "2px"}),
        ]),
        html.Div(metrics, style={"display": "flex", "flexWrap": "wrap",
                                 "gap": "28px"}),
    ])


def influencer_positions_table(resolutions):
    """OPEN influencer calls (stocks AND crypto) with their resolution status.
    Closed calls feed the win-rate stats but are not listed here."""
    rows = []
    for p, res in resolutions:
        if p.get("status") != "open":
            continue
        atype = p.get("asset_type") or "unknown"
        entry, est = _entry_for(p)
        cur = get_price(_yf_symbol(p["ticker"], atype))
        ret = resolver.return_pct(p, entry, cur)
        tdate = p.get("trade_date") or _local_date(p.get("opened_at")) or None
        if res:
            label, ckey = _STATUS_LABEL[res["status"]]
            status_cell = (label, C[ckey])
        else:
            status_cell = ("live", C["blue"])
        rows.append((
            (p["ticker"] + (" SHORT" if p.get("side") == "short" else ""), C["blue"]),
            atype,
            tdate or "—",
            _money(entry) + ("*" if est and entry else ""),
            _money(cur),
            (_fmt_pct(ret), _color(ret)),
            _money(p.get("stop_loss")),
            _money(p.get("target")),
            status_cell,
        ))
    rows.sort(key=lambda r: r[2], reverse=True)
    return _table(["Ticker", "Asset", "Trade Date", "Entry", "Current",
                   "Return %", "Stop", "Target", "Status"], rows,
                  empty="No open influencer positions",
                  hide_sm={1, 2, 6, 7})   # phones: drop asset/date/stop/target


app = Dash(__name__)
app.title = "Pilot Trader — Signal Monitor"

# Dark theme: Dash's default <body> is white, and the DataTable's filter inputs
# render light-on-light, so inject CSS into <head>.
app.index_string = """<!DOCTYPE html>
<html>
  <head>
    {%metas%}<meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{%title%}</title>{%favicon%}{%css%}
    <style>
      body { background-color: #0d1117; margin: 0; }
      * { box-sizing: border-box; }
      a { color: #58a6ff; text-decoration: none; }
      a:hover { text-decoration: underline; }
      .dash-table-container .dash-spreadsheet-container .dash-filter input {
        background-color: #161b22 !important; color: #e6edf3 !important;
        border: 1px solid #30363d !important;
      }
      /* dcc.Dropdown (react-select) -> GitHub-dark; covers legacy .Select-*
         and react-select v3+ .Select__* class conventions. */
      .Select-control, .Select__control,
      .Select-menu-outer, .Select__menu, .VirtualizedSelectOption {
        background-color: #161b22 !important; color: #e6edf3 !important;
        border-color: #30363d !important;
      }
      .Select-value-label, .Select__single-value, .Select-placeholder,
      .Select__placeholder, .Select-input > input, .Select__input input {
        color: #e6edf3 !important;
      }
      .Select-option, .Select__option {
        background-color: #161b22 !important; color: #e6edf3 !important;
      }
      .Select-option.is-focused, .Select__option--is-focused,
      .VirtualizedSelectFocusedOption {
        background-color: #30363d !important; color: #e6edf3 !important;
      }
      .is-focused:not(.is-open) > .Select-control,
      .Select__control--is-focused { border-color: #58a6ff !important; }
      /* click-to-expand rows (Kendrick forecasts, setups, table views): no
         native marker, row hover affordance, the ▸ glyph turns when open */
      details > summary { list-style: none; }
      details > summary::-webkit-details-marker { display: none; }
      details > summary:hover { background-color: #1c2330; }
      details[open] > summary .caret { display: inline-block;
                                       transform: rotate(90deg); }
      .open-view:hover { text-decoration: underline; }
      /* "ÚJ" badge: set by assets/new_badges.js on anything newer than this
         browser's previous look at the same view */
      .is-new { position: relative; }
      .is-new::before { content: "ÚJ"; position: absolute; top: -8px;
                        right: 14px; z-index: 1; background: #58a6ff;
                        color: #0d1117; font-size: 0.6rem; font-weight: bold;
                        letter-spacing: 0.06em; padding: 1px 7px;
                        border-radius: 8px; }
      /* Consensus chart marks. Classes, not inline styles: a chart is hundreds
         of repeated nodes and Dash ships every node's inline style. Hover
         lifts the mark; the column / 18px ring is the hit target. */
      .bb-col { position: relative; flex: 1 1 0; min-width: 0; height: 100%; }
      .bb-bar { position: absolute; left: 0; right: 0; }
      .bb-bar.pos { bottom: 50%; background: #3fb950; border-radius: 3px 3px 0 0; }
      .bb-bar.neg { top: 50%; background: #f85149; border-radius: 0 0 3px 3px; }
      .hs-cell { width: 7px; height: 7px; border-radius: 50%;
                 background: #8b949e; }
      .hs-bullish { background: #3fb950; }
      .hs-bearish { background: #f85149; }
      .hs-mixed { background: #d29922; }
      .lvl-hit { position: absolute; top: 50%; transform: translate(-50%, -50%);
                 width: 18px; height: 18px; display: flex; align-items: center;
                 justify-content: center; z-index: 1; }
      .lvl-dot { width: 8px; height: 8px; border-radius: 50%; background: #58a6ff;
                 box-shadow: 0 0 0 2px #161b22; }
      .bb-col:hover .bb-bar, .lvl-hit:hover .lvl-dot { filter: brightness(1.35); }
      .twin { color: #8b949e; font-size: 0.66rem; line-height: 1.5;
              margin: 6px 0 0; white-space: pre; overflow-x: auto; }
      ::-webkit-scrollbar { width: 10px; height: 10px; }
      ::-webkit-scrollbar-track { background: #0d1117; }
      ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 5px; }
      /* Pinned header (title, status bar, sub-tabs). Needs its own opaque
         background or the scrolling view shows through it, and no ancestor may
         set overflow, or sticky silently stops working. */
      .sticky-head { position: sticky; top: 0; z-index: 10;
                     background-color: #0d1117; padding: 8px 0 4px;
                     border-bottom: 1px solid #30363d; }
      /* --- influencer sub-tab bar ---------------------------------------- */
      /* When the sub-tabs outgrow one desktop row (there were 14), dcc.Tabs injects
           .tab { flex: 1 1 0; min-width: 0 }
         so each tab SHRINKS below its own label width; with white-space:nowrap
         the labels then bleed into their neighbours and read as one word
         ("IncomeSharkstraderstewieCowen(YT)"). Stop the shrink, space the
         tabs apart, and let the strip wrap onto a second row -- which also
         needs .tab-parent (overflow:hidden by default) to stop clipping it.
         These rules must stay ABOVE the max-width:760px block below: that block
         re-forces nowrap + touch-scroll so phones keep ONE swipeable strip, and
         at equal specificity the later source wins. */
      .subtabs-parent { overflow: visible !important; }
      .subtabs-strip { flex-wrap: wrap !important; gap: 6px !important; }
      .subtabs-strip .tab { flex: 0 0 auto !important;
                            min-width: auto !important;
                            white-space: nowrap !important; }
      /* --- responsive / mobile ------------------------------------------ */
      html, body { -webkit-text-size-adjust: 100%; }
      @media (max-width: 760px) {
        .root-pad { padding: 12px 12px !important; }
        /* tab + sub-tab bars: make the bar itself a horizontal touch-scroll
           strip. dcc.Tabs injects (via JS, as inline-equivalent CSS):
             .tab-parent  { overflow: hidden; }      <- CLIPS the row
             .tab         { flex: 1 1 0; min-width: 0; } <- tabs shrink, text cut
           so tabs got squished/clipped with nothing to scroll. We override with
           !important: stop the parent clipping, let .tab-container scroll-x on
           touch, and stop tabs shrinking so they overflow and the strip scrolls.
           Scrollbar hidden (WebKit/FF/IE) but swipe still works. */
        .tab-parent { overflow: visible !important; }
        .tab-container {
          flex-direction: row !important;
          flex-wrap: nowrap !important;
          overflow-x: auto !important;
          overflow-y: hidden !important;
          -webkit-overflow-scrolling: touch !important;
          scrollbar-width: none !important;        /* Firefox */
          -ms-overflow-style: none !important;      /* old Edge/IE */
        }
        .tab-container::-webkit-scrollbar { display: none !important;
                                            width: 0 !important; height: 0 !important; }
        .tab-container .tab {
          flex: 0 0 auto !important;
          min-width: auto !important;
          white-space: nowrap !important;
          padding: 9px 12px !important; min-height: 40px !important;
          font-size: 0.78rem !important;
        }
        /* Consensus rows stack on phones: source / as-of / basis on the left,
           the view chip + history on the right, and the STANCE prose full
           width below -- instead of a 780px grid whose STANCE column starts
           off-screen. The column header only fits the desktop grid. */
        .cons-inner { min-width: 0 !important; }
        .cons-head { display: none !important; }
        .cons-row { grid-template-columns: minmax(0, 1fr) auto !important;
                    grid-template-areas: "src view" "asof view" "basis view"
                                         "stance stance" !important;
                    row-gap: 2px !important; }
        .cons-row > .cons-src { grid-area: src; }
        .cons-row > .cons-view { grid-area: view; }
        .cons-row > .cons-asof { grid-area: asof; }
        .cons-row > .cons-basis { grid-area: basis; }
        .cons-row > .cons-stance { grid-area: stance; margin-top: 6px; }
        .cons-charts { grid-template-columns: minmax(0, 1fr) !important; }
        /* phones: hide low-priority columns (marked .col-sm-hide), tighten
           cell padding, and stop headers wrapping ("TRADE DATE") so the key
           columns (ticker/return/current) fit a 390px screen without h-scroll.
           A table still too wide falls back to its wrapper's overflow-x:auto. */
        .col-sm-hide { display: none !important; }
        table th { white-space: nowrap !important; }
        table td, table th { padding-left: 6px !important;
                             padding-right: 6px !important; }
        /* monospace status bar: smaller so segments don't dominate the screen */
        #status-row { font-size: 0.62rem !important;
                      padding: 5px 8px !important;
                      line-height: 1.45 !important; }
        /* Kendrick forecast rows: stack on phones so target prices never
           truncate ("$3,5..." -> full "$3,500"). The summary wraps and the
           targets take their own full-width line (asset+direction on line 1,
           targets on line 2, meta below). Desktop keeps the 1-line ellipsis. */
        .kndr-summary { flex-wrap: wrap !important; row-gap: 5px !important; }
        .kndr-targets { flex: 1 1 100% !important; min-width: 0 !important;
                        white-space: normal !important; overflow: visible !important;
                        text-overflow: clip !important; }
      }
    </style>
  </head>
  <body>
    {%app_entry%}
    <footer>{%config%}{%scripts%}{%renderer%}</footer>
  </body>
</html>"""

_SECTION_H = {"color": C["text"], "fontFamily": MONO, "fontSize": "0.95rem",
              "textTransform": "uppercase", "letterSpacing": "0.06em",
              "borderBottom": f"1px solid {C['border']}", "paddingBottom": "6px",
              "marginTop": "28px"}
_TAB_STYLE = {"backgroundColor": C["bg"], "color": C["dim"],
              "border": f"1px solid {C['border']}", "fontFamily": MONO,
              "padding": "11px 16px", "minHeight": "44px", "whiteSpace": "nowrap",
              "display": "flex", "alignItems": "center", "justifyContent": "center"}
_TAB_SELECTED = {"backgroundColor": C["card"], "color": C["text"],
                 "border": f"1px solid {C['border']}",
                 "borderTop": f"2px solid {C['blue']}", "fontFamily": MONO,
                 "padding": "11px 16px", "minHeight": "44px",
                 "whiteSpace": "nowrap", "display": "flex",
                 "alignItems": "center", "justifyContent": "center"}


# --- redesign: components ---------------------------------------------------
def _sep():
    """' · ' separator between status-bar segments (#status-row keeps the
    spaces: it is a flex row, which would otherwise trim them)."""
    return html.Span(" · ", style={"color": C["dim"]})


def _cost_sums():
    """LLM spend from data/cost_log.json across every pipeline: (today, month,
    {source: month total}). Rows logged before per-source logging have no
    source; they are all monitor.py's."""
    try:
        with open(COST_LOG_FILE) as f:
            log = json.load(f)
        if not isinstance(log, list):
            log = []
    except (json.JSONDecodeError, OSError):
        log = []
    now = datetime.now(timezone.utc)
    today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    d = m = 0.0
    by_source = {}
    for r in log:
        usd = r.get("total_usd") or 0.0
        ts = r.get("timestamp") or ""
        if ts[:7] == month:
            m += usd
            src = r.get("source") or "monitor"
            by_source[src] = by_source.get(src, 0.0) + usd
        if ts[:10] == today:
            d += usd
    return d, m, by_source


def credits_days_left(history=None):
    """Days until the GetXAPI balance runs out at the last week's burn rate,
    from the balance snapshots get_getxapi_credits() records. Only the stretch
    after the latest top-up (a balance increase) counts; None until that
    stretch spans a day and the balance actually fell."""
    pts = list(_credit_history if history is None else history)
    if not pts:
        return None
    pts = [(t, b) for t, b in pts if t >= pts[-1][0] - 7 * 86400]
    start = 0
    for i in range(1, len(pts)):
        if pts[i][1] > pts[i - 1][1]:
            start = i
    (t0, b0), (t1, b1) = pts[start], pts[-1]
    if t1 - t0 < 86400 or b1 >= b0:
        return None
    return b1 / ((b0 - b1) / ((t1 - t0) / 86400))


def _monitor_stale():
    """Hours since monitor.py's last successful run if that exceeds
    STALE_HOURS, or "no data" if it can't be read; None while healthy."""
    try:
        with open(STATE_FILE) as f:
            last = datetime.fromisoformat(json.load(f)["_last_run"])
    except (OSError, ValueError, KeyError, TypeError):
        return "no data"
    hours = (datetime.now(timezone.utc) - last).total_seconds() / 3600
    return hours if hours > STALE_HOURS else None


def status_row():
    """GetXAPI credits (+ runway), LLM spend of every pipeline and price
    freshness, led by a red monitor STALE warning only when monitor.py has
    missed its runs. Reads caches only: the credits are fetched by the
    background warmer, never on a page load."""
    stale = _monitor_stale()
    warn = [] if stale is None else [
        html.Span("● monitor STALE " + (stale if isinstance(stale, str)
                                        else f"{stale:.0f}h"),
                  style={"color": C["red"], "fontWeight": "bold"}),
        _sep()]
    bal = _credits_cache["balance"]
    if bal is None:
        bal_txt, bal_color = "n/a", C["dim"]
    else:
        bal_txt = f"${bal:,.2f} credits"
        bal_color = C["red"] if bal < CREDITS_LOW_USD else C["green"]
    days = credits_days_left()
    runway = [] if days is None or bal is None else [html.Span(
        f" (~{days:,.0f}d left)",
        style={"color": C["red"] if days < 14 else C["dim"]})]

    d, m, by_source = _cost_sums()
    breakdown = " · ".join(f"{src} ${usd:,.2f}" for src, usd in
                           sorted(by_source.items(), key=lambda kv: -kv[1]))
    pa = _fetch_state["last"]
    prices_txt = (
        _to_local(datetime.fromtimestamp(pa, timezone.utc), "%H:%M %Z")
        + " (yfinance, 30-min refresh)" if pa else "not fetched yet")

    return warn + [
        html.Span("GetXAPI: ", style={"color": C["dim"]}),
        html.Span(bal_txt, style={"color": bal_color, "fontWeight": "bold"}),
        *runway,
        _sep(),
        html.Span("LLM: ", style={"color": C["dim"]}),
        html.Span(f"today ${d:,.2f} / mo ${m:,.2f}",
                  title=f"This month by pipeline: {breakdown or 'none'}",
                  style={"color": C["text"], "cursor": "help"}),
        _sep(),
        html.Span("Prices as of: ", style={"color": C["dim"]}),
        html.Span(prices_txt, style={"color": C["dim"]}),
    ]


# --- YouTube (Benjamin Cowen) analysis ------------------------------------
YT_SUMMARIES_FILE = os.path.join(DATA_DIR, "youtube_summaries.json")
YT_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "youtube_current_view.json")
_YT_SENTIMENT = {"bullish": C["green"], "bearish": C["red"], "neutral": C["dim"],
                 "mixed": C["yellow"]}


def load_youtube_summaries():
    """Benjamin Cowen video analyses (written by youtube_monitor.py). Missing or
    corrupt file -> [] (the section just shows 'no data')."""
    try:
        with open(YT_SUMMARIES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _load_current_view(path):
    """Rolling 'current view' synthesis written by twitter_digest.py /
    youtube_monitor.py for one feed (see CURRENT_VIEW_SCHEMA in either file).
    Missing, corrupt, or not-yet-generated file -> {} (consensus_section then
    renders a dimmed 'no view generated yet' row -- see _consensus_row)."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_youtube_current_view():
    return _load_current_view(YT_CURRENT_VIEW_FILE)


# --- YouTube (Jesse Olson / "The Market Sniper") analysis -----------------
JESSE_SUMMARIES_FILE = os.path.join(DATA_DIR, "jesse_olson_summaries.json")
JESSE_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "jesse_olson_current_view.json")


def load_jesse_olson_summaries():
    """Jesse Olson (Market Sniper) video analyses (written by
    youtube_monitor.py --channel jesse_olson)."""
    return _load_summaries(JESSE_SUMMARIES_FILE)


def load_jesse_olson_current_view():
    return _load_current_view(JESSE_CURRENT_VIEW_FILE)


def _yt_chip(text, color=None):
    return html.Span(text, style={
        "display": "inline-block", "background": C["bg"],
        "border": f"1px solid {C['border']}", "borderRadius": "10px",
        "padding": "1px 8px", "margin": "2px 4px 2px 0", "fontSize": "0.68rem",
        "color": color or C["dim"]})


def _yt_card(v):
    sent = (v.get("overall_sentiment") or "neutral").lower()
    color = _YT_SENTIMENT.get(sent, C["dim"])
    date = _local_stamp(v.get("published"))
    levels = v.get("key_price_levels") or []
    themes = v.get("top_themes") or []
    src_tag = "  ·  local whisper" if v.get("transcript_source") == "whisper" else ""
    # overflowWrap: an unbreakable token (a URL) must wrap inside the card, not
    # widen the page; data-ts feeds the "ÚJ" badge (assets/new_badges.js).
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderLeft": f"3px solid {color}", "borderRadius": "8px",
        "padding": "12px 16px", "marginTop": "10px",
        "overflowWrap": "anywhere"}, **{"data-ts": v.get("published") or ""},
        children=[
        html.Div(style={"display": "flex", "justifyContent": "space-between",
                        "alignItems": "flex-start", "gap": "12px"}, children=[
            html.A(v.get("title") or v.get("video_id"), href=_safe_href(v.get("url")),
                   target="_blank", rel="noopener noreferrer",
                   style={"color": C["text"], "fontWeight": "bold",
                          "fontSize": "0.9rem", "textDecoration": "none"}),
            html.Span(sent.upper(), style={
                "background": color, "color": C["bg"], "borderRadius": "10px",
                "padding": "1px 10px", "fontSize": "0.66rem", "fontWeight": "bold",
                "whiteSpace": "nowrap", "letterSpacing": "0.04em"}),
        ]),
        html.Div(f"{date}{src_tag}", style={"color": C["dim"],
                                            "fontSize": "0.68rem",
                                            "marginTop": "3px"}),
        html.Div(v.get("summary") or "", style={"color": C["text"],
                                                 "fontSize": "0.8rem",
                                                 "marginTop": "8px",
                                                 "lineHeight": "1.45"}),
        html.Div([html.Span("BTC outlook: ", style={"color": C["dim"],
                                                     "fontWeight": "bold"}),
                  html.Span(v.get("btc_outlook") or "—")],
                 style={"color": C["text"], "fontSize": "0.76rem",
                        "marginTop": "8px", "lineHeight": "1.4"}),
        (html.Div([html.Span("Levels: ", style={"color": C["dim"],
                                                "fontSize": "0.7rem"})]
                  + [_yt_chip(s, C["blue"]) for s in levels],
                  style={"marginTop": "8px"}) if levels else html.Span()),
        (html.Div([html.Span("Themes: ", style={"color": C["dim"],
                                                "fontSize": "0.7rem"})]
                  + [_yt_chip(t) for t in themes],
                  style={"marginTop": "6px"}) if themes else html.Span()),
    ])


def youtube_section(summaries, limit=5, empty_label="Benjamin Cowen"):
    """Render the most recent `limit` video analyses as cards (newest first)."""
    if not summaries:
        return [html.Div(f"No {empty_label} videos analyzed yet.",
                         style={"color": C["dim"], "fontSize": "0.8rem",
                                "marginTop": "8px"})]
    ordered = sorted(summaries, key=lambda r: r.get("published") or "",
                     reverse=True)
    return [_yt_card(v) for v in ordered[:limit]]


# --- Twitter analysis digest (@ki_young_ju) -------------------------------
# Same analysis-only pattern as the YouTube/Cowen section, but for X posts
# (written by twitter_digest.py): per-post Sonnet read + optional chart vision,
# Hungarian prose, English sentiment enum. NEVER traded. Reuses the YouTube card
# helpers (_yt_chip / _YT_SENTIMENT) since the visual language is identical.
TW_SUMMARIES_FILE = os.path.join(DATA_DIR, "twitter_summaries.json")
JOAO_SUMMARIES_FILE = os.path.join(DATA_DIR, "joao_summaries.json")
DORKCHICKEN_SUMMARIES_FILE = os.path.join(DATA_DIR, "dorkchicken_summaries.json")
DAANCRYPTO_SUMMARIES_FILE = os.path.join(DATA_DIR, "daancrypto_summaries.json")
DONALT_SUMMARIES_FILE = os.path.join(DATA_DIR, "donalt_summaries.json")
COWEN_X_SUMMARIES_FILE = os.path.join(DATA_DIR, "cowen_x_summaries.json")
GLASSNODE_SUMMARIES_FILE = os.path.join(DATA_DIR, "glassnode_summaries.json")
TRUECRYPTO_SUMMARIES_FILE = os.path.join(DATA_DIR, "truecrypto_summaries.json")
KENDRICK_FORECASTS_FILE = os.path.join(DATA_DIR, "kendrick_forecasts.json")
KI_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "ki_young_ju_current_view.json")
JOAO_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "joao_wedson_current_view.json")
DORKCHICKEN_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "dorkchicken_current_view.json")
DAANCRYPTO_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "daancrypto_current_view.json")
DONALT_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "donalt_current_view.json")
COWEN_X_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "cowen_x_current_view.json")
GLASSNODE_CURRENT_VIEW_FILE = os.path.join(DATA_DIR,
                                           "glassnode_current_view.json")
TRUECRYPTO_CURRENT_VIEW_FILE = os.path.join(DATA_DIR,
                                            "truecrypto_current_view.json")


def _load_summaries(path):
    """Post analyses written by twitter_digest.py for one feed. Missing or
    corrupt file -> [] (the section just shows 'no data')."""
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def load_twitter_summaries():
    """@ki_young_ju post analyses."""
    return _load_summaries(TW_SUMMARIES_FILE)


def load_joao_summaries():
    """@joao_wedson (Alphractal) post analyses."""
    return _load_summaries(JOAO_SUMMARIES_FILE)


def load_dorkchicken_summaries():
    """@DorkChicken (crypto/macro TA) post analyses."""
    return _load_summaries(DORKCHICKEN_SUMMARIES_FILE)


def load_daancrypto_summaries():
    """@DaanCrypto (crypto TA) post analyses."""
    return _load_summaries(DAANCRYPTO_SUMMARIES_FILE)


def load_donalt_summaries():
    """@DonAlt (crypto trader/TA) post analyses."""
    return _load_summaries(DONALT_SUMMARIES_FILE)


def load_cowen_x_summaries():
    """@benjamincowen (macro/BTC) post analyses -- the X side of the same
    analyst the Ben Cowen YouTube sub-tab covers."""
    return _load_summaries(COWEN_X_SUMMARIES_FILE)


def load_glassnode_summaries():
    """@glassnode (on-chain + derivatives analytics) post analyses."""
    return _load_summaries(GLASSNODE_SUMMARIES_FILE)


def load_truecrypto_summaries():
    """@Truecrypto (BTC price structure/range trading) post analyses --
    require_market_signal filtered, so this is already just the subset with
    a real number in it."""
    return _load_summaries(TRUECRYPTO_SUMMARIES_FILE)


def load_ki_current_view():
    return _load_current_view(KI_CURRENT_VIEW_FILE)


def load_joao_current_view():
    return _load_current_view(JOAO_CURRENT_VIEW_FILE)


def load_dorkchicken_current_view():
    return _load_current_view(DORKCHICKEN_CURRENT_VIEW_FILE)


def load_daancrypto_current_view():
    return _load_current_view(DAANCRYPTO_CURRENT_VIEW_FILE)


def load_donalt_current_view():
    return _load_current_view(DONALT_CURRENT_VIEW_FILE)


def load_cowen_x_current_view():
    return _load_current_view(COWEN_X_CURRENT_VIEW_FILE)


def load_glassnode_current_view():
    return _load_current_view(GLASSNODE_CURRENT_VIEW_FILE)


def load_truecrypto_current_view():
    return _load_current_view(TRUECRYPTO_CURRENT_VIEW_FILE)


def load_kendrick_forecasts():
    """Standard Chartered / Geoff Kendrick forecast ledger (one row per forecast,
    written by twitter_digest.py's forecast-ledger mode). The file is a dict
    {seen_ids, forecasts} -- return just the forecasts list."""
    try:
        with open(KENDRICK_FORECASTS_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    fc = data.get("forecasts") if isinstance(data, dict) else data
    return fc if isinstance(fc, list) else []


def _tw_title(text):
    """First line of the post, collapsed + trimmed to a single-line card title."""
    line = " ".join((text or "").split())
    return (line[:90] + "…") if len(line) > 90 else (line or "post")


def _safe_href(u):
    """Hrefs/srcs sourced from ledgers or third-party APIs render on a PUBLIC
    page -- allow only web URLs so a poisoned field can't become javascript:."""
    return u if (isinstance(u, str)
                 and u.startswith(("http://", "https://"))) else None


def _tw_images(media):
    """Inline chart thumbnail(s) for a post. Small/medium (capped width, not full
    width); loads Twitter's lightweight `?name=small` variant and links to the
    full-res image. Empty span when the post had no images (card unchanged)."""
    media = [u for u in (media or []) if _safe_href(u)]
    if not media:
        return html.Span()
    return html.Div([
        html.A(html.Img(src=f"{u}?name=small",
                        style={"maxWidth": "100%", "maxHeight": "190px",
                               "borderRadius": "6px", "display": "block",
                               "border": f"1px solid {C['border']}"}),
               href=u, target="_blank", rel="noopener noreferrer",
               style={"display": "block", "maxWidth": "300px"})
        for u in media[:4]
    ], style={"display": "flex", "flexWrap": "wrap", "gap": "8px",
              "marginTop": "8px"})


def _tw_card(p):
    sent = (p.get("overall_sentiment") or "neutral").lower()
    color = _YT_SENTIMENT.get(sent, C["dim"])
    date = _local_stamp(p.get("created_at"))
    author = p.get("author")              # search feeds: the poster (varies)
    levels = p.get("key_levels") or []
    themes = p.get("top_themes") or []
    chart_color = _YT_SENTIMENT.get((p.get("chart_trend") or "neutral").lower(),
                                    C["dim"])
    # overflowWrap: a contract address in a title (`ethereum:0xa12c...`) must
    # wrap inside the card, not widen the page on a phone.
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderLeft": f"3px solid {color}", "borderRadius": "8px",
        "padding": "12px 16px", "marginTop": "10px",
        "overflowWrap": "anywhere"}, **{"data-ts": p.get("created_at") or ""},
        children=[
        html.Div(style={"display": "flex", "justifyContent": "space-between",
                        "alignItems": "flex-start", "gap": "12px"}, children=[
            html.A(_tw_title(p.get("text")), href=_safe_href(p.get("url")),
                   target="_blank", rel="noopener noreferrer",
                   style={"color": C["text"], "fontWeight": "bold",
                          "fontSize": "0.9rem", "textDecoration": "none"}),
            html.Span(sent.upper(), style={
                "background": color, "color": C["bg"], "borderRadius": "10px",
                "padding": "1px 10px", "fontSize": "0.66rem", "fontWeight": "bold",
                "whiteSpace": "nowrap", "letterSpacing": "0.04em"}),
        ]),
        html.Div(([html.A(f"@{author}", href=f"https://x.com/{author}",
                          target="_blank", rel="noopener noreferrer",
                          style={"color": C["blue"], "textDecoration": "none",
                                 "fontWeight": "bold"}),
                   html.Span("  ·  ")] if author else [])
                 + [html.Span(f"{date}"
                              f"{'  ·  chart' if p.get('has_chart') else ''}")],
                 style={"color": C["dim"], "fontSize": "0.68rem",
                        "marginTop": "3px"}),
        html.Div(p.get("summary") or "", style={"color": C["text"],
                                                 "fontSize": "0.8rem",
                                                 "marginTop": "8px",
                                                 "lineHeight": "1.45"}),
        html.Div([html.Span("Piaci nézet: ", style={"color": C["dim"],
                                                     "fontWeight": "bold"}),
                  html.Span(p.get("market_view") or "—")],
                 style={"color": C["text"], "fontSize": "0.76rem",
                        "marginTop": "8px", "lineHeight": "1.4"}),
        (html.Div([html.Span("Chart: ", style={"color": chart_color,
                                               "fontWeight": "bold"}),
                   html.Span(p.get("chart_summary") or "")],
                  style={"color": C["text"], "fontSize": "0.74rem",
                         "marginTop": "8px", "lineHeight": "1.4"})
         if p.get("has_chart") and p.get("chart_summary") else html.Span()),
        _tw_images(p.get("media") or []),
        (html.Div([html.Span("Szintek: ", style={"color": C["dim"],
                                                 "fontSize": "0.7rem"})]
                  + [_yt_chip(s, C["blue"]) for s in levels],
                  style={"marginTop": "8px"}) if levels else html.Span()),
        (html.Div([html.Span("Témák: ", style={"color": C["dim"],
                                               "fontSize": "0.7rem"})]
                  + [_yt_chip(t) for t in themes],
                  style={"marginTop": "6px"}) if themes else html.Span()),
    ])


def twitter_section(summaries, limit=8, who="@ki_young_ju", days=14):
    """Render every post analysis from the last `days` days as cards (newest
    first), but never fewer than the latest `limit`, so a slow feed still shows
    its last few posts. A fixed last-N alone covered only ~2 days of a busy feed.
    Account-agnostic: the card helpers read fields off each record, so the same
    renderer serves every twitter_digest.py feed; `who` only sets the empty
    state."""
    if not summaries:
        return [html.Div(f"No {who} posts analyzed yet.",
                         style={"color": C["dim"], "fontSize": "0.8rem",
                                "marginTop": "8px"})]
    ordered = sorted(summaries, key=lambda r: r.get("created_at") or "",
                     reverse=True)
    cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    recent = sum((p.get("created_at") or "")[:10] >= cutoff for p in ordered)
    return [_tw_card(p) for p in ordered[:max(limit, recent)]]


# --- Kendrick / Standard Chartered forecast ledger ------------------------
# Unlike the per-post feeds above, kendrick_sc is a forecast LEDGER: one research
# call (echoed by 20-30 outlets) is deduplicated into a single row. So it renders
# as a compact, expandable table (one row per forecast) rather than a card stream;
# the source count IS the signal (how widely the call was picked up).
def _dedupe_targets(targets):
    """Collapse target phrasings that mean the same thing so a row never shows a
    price twice: same normalized value ('$40K'=='$40,000') or differing only by
    commas/spacing/case ('$3,500'=='$3500'). Keeps the first (headline) phrasing."""
    seen, out = set(), []
    for t in targets or []:
        v = _kndr_target_value(t)
        k = f"p{v:g}" if v is not None else t.replace(",", "").replace(" ", "").lower()
        if k and k not in seen:
            seen.add(k)
            out.append(t)
    return out


# Flow/size magnitudes (ETF inflows, AUM, market cap, TVL, "$X billion") are NOT
# price targets; a row whose headline is one of those (e.g. "XRP $4-$8 billion in
# ETF inflows") is hidden from the price table. Mirrors twitter_digest's keys so
# this render-time merge is a no-op once the ledger has been re-clustered.
_KNDR_NONPRICE_RE = re.compile(
    r"inflow|outflow|aum|tvl|market\s*cap|mcap|\bvolume\b|liquidity|"
    r"trillion|billion|\bbn\b", re.I)
_KNDR_PRICE_MULT = {"k": 1e3, "m": 1e6}    # thousand/million can be a price
_KNDR_SIZE_SUFFIX = {"b", "bn", "t"}       # billion/trillion = cap/flow, not price


def _kndr_target_value(t):
    """Float magnitude of a per-coin price target, or None if it is not a plain
    price (mirror of twitter_digest._target_value, so the render-time merge is a
    no-op once the ledger has been re-clustered). The number is read from the
    START (after an optional '$') so 'Q4 2025' is not a price; flow/size phrases,
    billion/trillion magnitudes ('$2.7T', '$5bn') and '50x' -> None;
    '$40k'/'$40,000' -> 40000; '$.5' -> 0.5."""
    s = (t or "").strip().lower()
    if not s or _KNDR_NONPRICE_RE.search(s):
        return None
    m = re.match(r"\$?\s*(\d[\d,]*(?:\.\d+)?|\.\d+)\s*(bn|[kmbt])?", s)
    if not m:
        return None
    suf = m.group(2)
    if not suf:
        # '$150-200k': the magnitude suffix trails the SECOND bound but applies
        # to both -- without this a $150,000-$200,000 range would read as $150.
        m2 = re.match(r"\$?\s*[\d.,]+\s*(?:-|–|—|to)\s*\$?\s*[\d.,]+\s*"
                      r"(bn|[kmbt])\b", s)
        if m2:
            suf = m2.group(1)
    if suf in _KNDR_SIZE_SUFFIX:
        return None
    try:
        v = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if suf:
        v *= _KNDR_PRICE_MULT[suf]
    elif re.search(r"\dx\b", s):
        return None
    return v


def _kndr_headline(f):
    return next((t for t in (f.get("targets") or []) if t and t.strip()), "")


def _is_price_forecast(f):
    """True if ANY of the row's targets is a parseable PRICE -- a mixed legacy
    row can lead with a flow phrase yet still carry genuine price calls (the
    6-source XRP row: inflow headline + '$8–$12.50'). Pure flow/size rows
    (ETF-inflow estimates etc.) stay excluded from the price table."""
    return any(_kndr_target_value(t) is not None
               for t in (f.get("targets") or []))


def _kndr_identity(f):
    """Asset + normalized headline price target -- same scheme as twitter_digest's
    _forecast_identity so legacy split rows ('$40K' vs '$40,000') collapse."""
    asset = (f.get("asset") or "?").upper()
    h = _kndr_headline(f)
    v = _kndr_target_value(h)
    if v is not None:
        return f"{asset}|p{v:g}"
    tok = h.strip().lower().replace(" ", "")[:16]
    return f"{asset}|{tok}" if tok else f"{asset}|d{(f.get('direction') or 'na')}"


def _merge_kndr_rows(forecasts):
    """Collapse rows that are the same asset+price target (a legacy ledger splits
    one call across '2030'/'unspecified' timeframes). Reach (source_count) is
    summed minus observable overlap; the most-reported row supplies the narrative."""
    by = {}
    for f in sorted(forecasts, key=lambda r: r.get("source_count") or 0,
                    reverse=True):
        k = _kndr_identity(f)
        d = by.get(k)
        if d is None:
            d = dict(f)
            d["targets"] = list(f.get("targets") or [])
            by[k] = d
            continue
        ids = {s.get("tweet_id") for s in d.get("sources") or []}
        overlap = sum(1 for s in (f.get("sources") or [])
                      if s.get("tweet_id") in ids)
        d["source_count"] = ((d.get("source_count") or 0)
                             + (f.get("source_count") or 0) - overlap)
        for t in f.get("targets") or []:
            if t not in d["targets"]:
                d["targets"].append(t)
        if (f.get("last_seen") or "") > (d.get("last_seen") or ""):
            d["last_seen"] = f.get("last_seen")
        if f.get("first_seen") and (not d.get("first_seen")
                                    or f["first_seen"] < d["first_seen"]):
            d["first_seen"] = f["first_seen"]
        if d.get("timeframe") in (None, "", "unspecified") \
                and f.get("timeframe") not in (None, "", "unspecified"):
            d["timeframe"] = f["timeframe"]
    return list(by.values())


def _kendrick_rows(forecasts, limit=12):
    """(shown, hidden, flow_hidden): the merged PRICE forecasts the Kendrick
    tab lists (most-reported first, capped at `limit`), how many more price
    forecasts the cap hides, and how many flow/size rows were left out."""
    rows = _merge_kndr_rows(forecasts)
    price = [f for f in rows if _is_price_forecast(f)]
    price.sort(key=lambda f: (f.get("source_count") or 0,
                              f.get("last_seen") or f.get("first_seen") or ""),
               reverse=True)
    return price[:limit], max(0, len(price) - limit), len(rows) - len(price)


# --- forecast grading: live price and the move still needed, or the verdict --
# One daily High/Low history per asset serves every row. It starts a year before
# the earliest deadline the ledger names (2024), so an expired call is graded
# over its final 12 months.
KNDR_HISTORY_START = "2023-01-01"
# Ledger "assets" that are themes or fiat, not a priceable coin.
_KNDR_NOT_ASSETS = {"RWA", "DEFI", "DEFI ASSETS", "STABLECOIN", "STABLECOINS",
                    "UNK", "USD", "HKD", "USDC"}


def _kndr_symbol(asset):
    a = (asset or "").strip().upper()
    if not a or a in _KNDR_NOT_ASSETS or not re.fullmatch(r"[A-Z0-9]+", a):
        return None
    return _yf_symbol(a, "crypto")


def _kndr_deadline(timeframe):
    """Dec 31 of the year a timeframe names ('2030', 'end-2025'); None for
    'unspecified', 'long-term' and relative spans ('3 years')."""
    m = re.search(r"\b(20\d\d)\b", timeframe or "")
    return date(int(m.group(1)), 12, 31) if m else None


def _kndr_progress(f, target, max_age=PRICE_TTL):
    """Where a price forecast stands: {"state": "hit", "date"} once the price
    reached the target (within the deadline's final year, or since the call was
    first seen), {"state": "missed"} when the deadline passed without it, else
    {"state": "open", "current", "to_go"} (% move still needed). None when the
    asset has no Yahoo price."""
    sym = _kndr_symbol(f.get("asset"))
    if not sym or not target:
        return None
    today = datetime.now(timezone.utc).date()
    deadline = _kndr_deadline(f.get("timeframe"))
    start = (f.get("first_seen") or today.isoformat())[:10]
    if deadline:
        start = min(start, (deadline - timedelta(days=365)).isoformat())
    end = min(deadline, today).isoformat() if deadline else today.isoformat()
    up = (f.get("direction") or "up").lower() != "down"
    ohlc = get_ohlc(sym, KNDR_HISTORY_START, max_age=max_age)
    have_path = ohlc is not None and not ohlc.empty
    if have_path:
        window = ohlc.loc[start:end]
        reached = window["High"] >= target if up else window["Low"] <= target
        if reached.any():
            return {"state": "hit", "date": window.index[reached.values][0]}
    if deadline and deadline < today:
        return {"state": "missed"} if have_path else None
    cur = get_price(sym)
    if not cur:
        return None
    return {"state": "open", "current": cur, "to_go": (target / cur - 1) * 100}


def _fmt_price(v):
    """Compact price for badges: $84.6K / $2,690 / $2.69 / $0.325."""
    if v >= 10_000:
        return f"${v / 1000:,.1f}K"
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.3g}"


def _kndr_progress_span(prog):
    if not prog:
        return html.Span()
    if prog["state"] == "hit":
        txt, color = f"✓ elérve {prog['date']}", C["green"]
        tip = "ezen a napon érte el az árfolyam a célt"
    elif prog["state"] == "missed":
        txt, color = "✗ nem teljesült", C["red"]
        tip = "a határidő lejárt, az árfolyam nem érte el a célt"
    else:
        txt = f"most {_fmt_price(prog['current'])} · {prog['to_go']:+,.0f}%"
        color, tip = C["dim"], "aktuális ár · a célig hiányzó elmozdulás"
    return html.Span(txt, title=tip, style={
        "color": color, "fontSize": "0.72rem", "whiteSpace": "nowrap",
        "flex": "0 0 auto"})


def _kendrick_row(f):
    sent = (f.get("overall_sentiment") or "neutral").lower()
    color = _YT_SENTIMENT.get(sent, C["dim"])
    direction = (f.get("direction") or "").lower()
    arrow = "▲" if direction == "up" else "▼" if direction == "down" else "→"
    arrow_color = (C["green"] if direction == "up"
                   else C["red"] if direction == "down" else C["dim"])
    asset = f.get("asset") or "?"
    # Show only targets consistent with the row's own identity: a price-keyed
    # row hides stray foreign levels a legacy merge baked in ('$150K' on the
    # $60K bear row); a flow-headlined row shown for its price calls displays
    # exactly those price targets.
    tlist = _dedupe_targets(f.get("targets"))
    hv = _kndr_target_value(_kndr_headline(f))
    if hv is not None:
        tlist = [t for t in tlist if _kndr_target_value(t) == hv] or tlist
    else:
        tlist = [t for t in tlist if _kndr_target_value(t) is not None] or tlist
    targets = "  ·  ".join(tlist) or "—"
    # The row is graded on its headline price (or its first price target when
    # the headline is a flow phrase).
    tv = hv if hv is not None else next(
        (v for v in map(_kndr_target_value, tlist) if v is not None), None)
    progress = _kndr_progress_span(_kndr_progress(f, tv) if tv else None)
    tf = f.get("timeframe") or "—"
    first = (f.get("first_seen") or "")[:10]
    sources = f.get("sources") or []
    n = f.get("source_count") or len(sources)

    summary = html.Summary(className="kndr-summary", style={
        "display": "flex", "alignItems": "center", "gap": "10px",
        "cursor": "pointer", "listStyle": "none", "padding": "9px 2px"},
        children=[
        html.Span("▸", className="caret",
                  style={"color": C["dim"], "fontSize": "0.7rem",
                         "flex": "0 0 auto"}),
        html.Span(asset, style={
            "background": C["bg"], "color": C["blue"],
            "border": f"1px solid {C['border']}", "borderRadius": "6px",
            "padding": "2px 9px", "fontWeight": "bold", "fontFamily": MONO,
            "fontSize": "0.8rem", "minWidth": "54px", "textAlign": "center",
            "flex": "0 0 auto"}),
        html.Span(arrow, style={"color": arrow_color, "fontWeight": "bold",
                               "flex": "0 0 auto"}),
        html.Span(targets, className="kndr-targets", style={
            "color": C["text"], "fontWeight": "bold", "fontSize": "0.82rem",
            "flex": "1 1 auto", "minWidth": "0", "overflow": "hidden",
            "textOverflow": "ellipsis", "whiteSpace": "nowrap"}),
        progress,
        html.Span(tf, style={"color": C["dim"], "fontSize": "0.74rem",
                            "whiteSpace": "nowrap", "flex": "0 0 auto"}),
        html.Span(f"{n} src", title="accounts that reported this call",
                  style={"background": C["bg"], "color": C["text"],
                         "border": f"1px solid {C['border']}",
                         "borderRadius": "10px", "padding": "1px 8px",
                         "fontSize": "0.68rem", "whiteSpace": "nowrap",
                         "flex": "0 0 auto"}),
        html.Span(first, style={"color": C["dim"], "fontSize": "0.68rem",
                               "whiteSpace": "nowrap", "minWidth": "74px",
                               "textAlign": "right", "flex": "0 0 auto"}),
        html.Span(style={"width": "8px", "height": "8px", "borderRadius": "50%",
                        "background": color, "flex": "0 0 auto"}),
    ])

    levels = f.get("key_levels") or []
    themes = f.get("top_themes") or []
    src_links = []
    for s in sources[:12]:
        src_links.append(html.A(f"@{s.get('author')}", href=_safe_href(s.get("url")),
            target="_blank", rel="noopener noreferrer",
            style={"color": C["blue"], "textDecoration": "none",
                   "fontSize": "0.72rem", "marginRight": "12px"}))
    if n > len(sources[:12]):
        src_links.append(html.Span(f"+{n - len(sources[:12])} more",
            style={"color": C["dim"], "fontSize": "0.72rem"}))

    chart_color = _YT_SENTIMENT.get((f.get("chart_trend") or "neutral").lower(),
                                    C["dim"])
    body = html.Div(style={"padding": "2px 6px 12px",
                           "borderTop": f"1px solid {C['border']}"}, children=[
        html.Div(f.get("summary") or "", style={
            "color": C["text"], "fontSize": "0.8rem", "marginTop": "8px",
            "lineHeight": "1.45"}),
        (html.Div([html.Span("Piaci nézet: ", style={"color": C["dim"],
            "fontWeight": "bold"}), html.Span(f.get("market_view") or "—")],
            style={"color": C["text"], "fontSize": "0.76rem", "marginTop": "8px",
                   "lineHeight": "1.4"}) if f.get("market_view") else html.Span()),
        (html.Div([html.Span("Chart: ", style={"color": chart_color,
            "fontWeight": "bold"}), html.Span(f.get("chart_summary") or "")],
            style={"color": C["text"], "fontSize": "0.74rem", "marginTop": "8px",
                   "lineHeight": "1.4"})
         if f.get("has_chart") and f.get("chart_summary") else html.Span()),
        _tw_images(f.get("media") or []),
        (html.Div([html.Span("Szintek: ", style={"color": C["dim"],
            "fontSize": "0.7rem"})] + [_yt_chip(s, C["blue"]) for s in levels],
            style={"marginTop": "8px"}) if levels else html.Span()),
        (html.Div([html.Span("Témák: ", style={"color": C["dim"],
            "fontSize": "0.7rem"})] + [_yt_chip(t) for t in themes],
            style={"marginTop": "6px"}) if themes else html.Span()),
        html.Div([html.Span("Források: ", style={"color": C["dim"],
            "fontSize": "0.7rem"})] + src_links, style={"marginTop": "10px"}),
    ])

    return html.Details(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderLeft": f"3px solid {color}", "borderRadius": "8px",
        "padding": "0 12px", "marginTop": "8px"},
        **{"data-ts": f.get("first_seen") or ""}, children=[summary, body])


def kendrick_forecast_section(forecasts, limit=12):
    """Render the SC / Kendrick forecast ledger as a compact list of expandable
    rows, one per PRICE forecast. Near-identical calls are collapsed (asset +
    normalized target), pure flow/size calls (ETF inflows, AUM, market cap) are
    hidden, and rows are ranked by reach so only the most widely reported calls
    show. A footer notes anything hidden so the cap is never silent."""
    if not forecasts:
        return [html.Div("No Standard Chartered / Kendrick forecasts captured "
                         "yet.", style={"color": C["dim"], "fontSize": "0.8rem",
                                        "marginTop": "8px"})]
    shown, extra, flow_hidden = _kendrick_rows(forecasts, limit)
    children = [_kendrick_row(f) for f in shown]
    notes = []
    if extra:
        notes.append(f"+{extra} kevésbé jegyzett előrejelzés elrejtve")
    if flow_hidden:
        notes.append(f"{flow_hidden} flow/méret-becslés (pl. ETF-beáramlás) kihagyva")
    if notes:
        children.append(html.Div(" · ".join(notes), style={
            "color": C["dim"], "fontSize": "0.7rem", "marginTop": "10px",
            "textAlign": "center"}))
    return [html.Div(children)]


# --- Consensus: cross-feed CURRENT VIEW summary ----------------------------
# One row per analysis-only digest feed that produces a rolling CURRENT VIEW
# (twitter_digest.py / youtube_monitor.py), so "who is bearish right now" is one
# glance instead of seven sub-tab clicks. The rows read the per-feed
# *_current_view.json files; data/sentiment_history.json (every view ever
# generated) adds each row's recent-view strip and the balance chart above.
#
# All ten read the same market today, so one tally is meaningful; rows are
# still grouped by asset class, because tallying a crypto stance together with an
# equities stance would be nonsense the day a non-crypto feed is added.
#
# Cowen appears TWICE by design (YT + X): the two feeds cover the same analyst
# but not the same content -- the videos are long-form cycle work, the tweets are
# same-day macro reactions -- and seeing them disagree is itself informative. It
# does mean one person carries 2 of 9 votes in the tally.
_CONSENSUS_SOURCES = [
    # label, asset class, current-view loader, what based_on.count counts,
    # influencer-subtabs value of the feed's card view (clicking the label
    # opens it; these views have no tab of their own, see the tab bar)
    ("Cowen (YT)",  "crypto", load_youtube_current_view,     "videos", "BenCowen"),
    ("Cowen (X)",   "crypto", load_cowen_x_current_view,     "posts",  "CowenX"),
    ("Jesse Olson", "crypto", load_jesse_olson_current_view, "videos", "JesseOlson"),
    ("Ki Young Ju", "crypto", load_ki_current_view,          "posts",  "KiYoungJu"),
    ("Joao Wedson", "crypto", load_joao_current_view,        "posts",  "JoaoWedson"),
    ("DorkChicken", "crypto", load_dorkchicken_current_view, "posts",  "DorkChicken"),
    ("DaanCrypto",  "crypto", load_daancrypto_current_view,  "posts",  "DaanCrypto"),
    ("DonAlt",      "crypto", load_donalt_current_view,      "posts",  "DonAlt"),
    ("Glassnode",   "crypto", load_glassnode_current_view,   "posts",  "Glassnode"),
    ("Truecrypto",  "crypto", load_truecrypto_current_view,  "posts",  "Truecrypto"),
]

# Card view -> the registry key its views are logged under in
# sentiment_history.json (Cowen's YouTube channel and X feed are separate keys).
_HISTORY_KEY = {"BenCowen": "cowen", "CowenX": "cowen_x",
                "JesseOlson": "jesse_olson", "KiYoungJu": "ki_young_ju",
                "JoaoWedson": "joao_wedson", "DorkChicken": "dorkchicken",
                "DaanCrypto": "daancrypto", "DonAlt": "donalt",
                "Glassnode": "glassnode", "Truecrypto": "truecrypto"}
SENTIMENT_HISTORY_FILE = os.path.join(DATA_DIR, "sentiment_history.json")
HISTORY_STRIP_LEN = 10     # recent views shown under each row's chip
BALANCE_DAYS = 60          # span of the bull-bear balance chart


def _load_sentiment_history():
    """Every CURRENT VIEW ever generated (sentiment_history.py), write order.
    Missing or corrupt file -> [] (the strip and the chart just disappear)."""
    try:
        with open(SENTIMENT_HISTORY_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _history_by_source(history):
    """{source: [(generated_at, sentiment), ...] oldest first}."""
    out = {}
    for r in history:
        if isinstance(r, dict) and r.get("source"):
            out.setdefault(r["source"], []).append(
                (r.get("generated_at") or "", (r.get("sentiment") or "").lower()))
    for recs in out.values():
        recs.sort()
    return out


def _history_run(recs, current):
    """(since, previous) for a source whose view is now `current`: the date its
    current sentiment took over (first record of the unbroken run at the end of
    its history) and the sentiment before that run. Either may be None."""
    i = len(recs) - 1
    while i >= 0 and recs[i][1] == current:
        i -= 1
    since = recs[i + 1][0][:10] if i + 1 < len(recs) else None
    return since, (recs[i][1] if i >= 0 else None)


def _history_strip(recs):
    """The last HISTORY_STRIP_LEN views, oldest -> newest: one dot per view in
    the sentiment's color (.hs-* in the <style>), all on one line. Each dot's
    tooltip and the "since · was" note under it carry the same in words."""
    return html.Div([html.Span(className=f"hs-cell hs-{sent}",
                               title=f"{_local_date(ts) or '?'}: {sent}")
                     for ts, sent in recs[-HISTORY_STRIP_LEN:]],
                    style={"display": "flex", "alignItems": "center",
                           "gap": "3px", "marginTop": "6px"})


def _daily_balance(by_source, keys, days=BALANCE_DAYS, today=None):
    """[(date, bullish, bearish, other)] for each of the last `days` days: every
    source's latest view as of that day (carried forward), counted by
    sentiment. Leading days before any view existed are dropped."""
    today = today or datetime.now(timezone.utc).date()
    series = [by_source[k] for k in keys if k in by_source]
    out = []
    for back in range(days - 1, -1, -1):
        day = (today - timedelta(days=back)).isoformat()
        counts = {"bullish": 0, "bearish": 0, "other": 0}
        for recs in series:
            last = None
            for ts, sent in recs:
                if ts[:10] > day:
                    break
                last = sent
            if last is not None:
                counts[last if last in counts else "other"] += 1
        out.append((day, counts["bullish"], counts["bearish"], counts["other"]))
    while out and not any(out[0][1:]):
        out.pop(0)
    return out


_CHART_CARD = {"background": C["card"], "border": f"1px solid {C['border']}",
               "borderRadius": "8px", "padding": "10px 14px", "minWidth": "0"}
_CHART_TITLE = {"color": C["text"], "fontSize": "0.72rem", "fontWeight": "bold",
                "letterSpacing": "0.04em"}
_CHART_NOTE = {"color": C["dim"], "fontSize": "0.64rem"}


def _table_twin(summary, headers, rows):
    """The collapsed table view every chart ships with (every value reachable
    without hovering), as one preformatted block: a 60-row html.Table would
    be ~300 nodes in the payload."""
    cols = list(zip(headers, *rows))
    widths = [max(len(str(c)) for c in col) for col in cols]
    text = "\n".join("  ".join(str(c).rjust(w) if i else str(c).ljust(w)
                               for i, (c, w) in enumerate(zip(line, widths)))
                     for line in [headers, *rows])
    return html.Details([
        html.Summary([html.Span("▸ ", className="caret"), summary],
                     style={**_CHART_NOTE, "cursor": "pointer", "marginTop": "6px"}),
        html.Pre(text, className="twin")], style={"marginTop": "2px"})


def sentiment_balance_chart(by_source):
    """Bull-bear balance over time: per day, bullish minus bearish sources
    (latest view carried forward). Diverging columns around a zero line --
    position carries the sign, green/red only repeats it."""
    rows = _daily_balance(by_source, list(_HISTORY_KEY.values()))
    if not rows:
        return None
    scale = max(b + r + o for _, b, r, o in rows) or 1
    cols = []
    for day, bull, bear, other in rows:
        net = bull - bear
        cols.append(html.Div(
            className="bb-col",
            title=f"{day}: {bull} bullish · {bear} bearish · {other} neutral/mixed",
            children=html.Div(
                className="bb-bar " + ("pos" if net > 0 else "neg"),
                style={"height": f"{abs(net) / scale * 50:.1f}%"}) if net else None))
    day, bull, bear, other = rows[-1]
    week = rows[-8] if len(rows) >= 8 else rows[0]
    axis = {**_CHART_NOTE, "lineHeight": "1"}
    plot = html.Div(style={"display": "flex", "gap": "6px", "marginTop": "8px"},
                    children=[
        html.Div([html.Div(f"+{scale}", style=axis), html.Div("0", style=axis),
                  html.Div(f"-{scale}", style=axis)],
                 style={"display": "flex", "flexDirection": "column",
                        "justifyContent": "space-between", "height": "72px",
                        "textAlign": "right", "minWidth": "22px"}),
        html.Div(style={"position": "relative", "flex": "1 1 auto",
                        "height": "72px", "minWidth": "0"}, children=[
            html.Div(style={"position": "absolute", "left": 0, "right": 0,
                            "top": "50%", "height": "1px",
                            "background": C["border"]}),
            html.Div(cols, style={"position": "relative", "zIndex": 1,
                                  "display": "flex", "gap": "2px",
                                  "height": "100%"}),
        ]),
    ])
    return html.Div(style=_CHART_CARD, children=[
        html.Div([html.Span("SENTIMENT BALANCE", style=_CHART_TITLE),
                  html.Span(f"  now {bull - bear:+d}  ({bull} bullish · {bear} "
                            f"bearish · {other} other)  ·  7d ago "
                            f"{week[1] - week[2]:+d}", style=_CHART_NOTE)]),
        plot,
        html.Div([html.Span(rows[0][0]), html.Span(day)],
                 style={**_CHART_NOTE, "display": "flex",
                        "justifyContent": "space-between",
                        "marginLeft": "28px", "marginTop": "3px"}),
        _table_twin("daily counts", ["Date", "Bullish", "Bearish", "Other", "Net"],
                    [(d, str(b), str(r), str(o), f"{b - r:+d}")
                     for d, b, r, o in reversed(rows)]),
    ])


# BTC price levels named in the CURRENT VIEW prose: "83 000 dolláros",
# "85 000–88 000 dollár", "58 ezer dolláros", "~$82.8k". A dollar unit is
# required, so quantities ("100 000 BTC") and indicator periods ("50 hetes") are
# ignored; the band around the live price drops other assets' prices (ETH 2800).
_LVL_NUM = r"\d{1,3}(?:[   .,]\d{3})+|\d+(?:[.,]\d+)?"
_LVL_SUF = r"(?:k\b|ezer\b)?"
_LVL_RE = re.compile(
    rf"\$\s*(?P<a>{_LVL_NUM})\s*(?P<ak>{_LVL_SUF})"
    rf"|(?P<b>{_LVL_NUM})\s*(?P<bk>{_LVL_SUF})"
    rf"(?:\s*(?:[–—-]|és|to)\s*\$?\s*(?P<c>{_LVL_NUM})\s*(?P<ck>{_LVL_SUF}))?"
    rf"\s*(?:dollár|USD|\$)", re.I)
_LVL_BAND = (0.5, 1.6)     # kept levels, as multiples of the live BTC price


def _level_value(num, suffix):
    s = num.replace(" ", " ").replace(" ", " ")
    if re.fullmatch(r"\d{1,3}(?:[ .,]\d{3})+", s):      # thousands-grouped
        v = float(re.sub(r"[ .,]", "", s))
    else:
        v = float(s.replace(",", "."))
    return v * 1000 if (suffix or "").lower() in ("k", "ezer") else v


def extract_btc_levels(text, price):
    """Sorted distinct BTC levels in `text` within _LVL_BAND of `price`."""
    found = []
    for m in _LVL_RE.finditer(text or ""):
        if m.group("a"):
            found.append(_level_value(m.group("a"), m.group("ak")))
            continue
        ck = m.group("ck")     # "85–88 ezer": the unit covers both bounds
        found.append(_level_value(m.group("b"), m.group("bk") or ck))
        if m.group("c"):
            found.append(_level_value(m.group("c"), ck))
    lo, hi = (_LVL_BAND[0] * price, _LVL_BAND[1] * price) if price \
        else (20_000, 500_000)
    return sorted({round(v) for v in found if lo <= v <= hi})


def _nice_step(span):
    for step in (1_000, 2_000, 2_500, 5_000, 10_000, 20_000, 25_000, 50_000):
        if span / step <= 6:
            return step
    return 100_000


def btc_level_map(sources):
    """Every BTC level the current views name, on one price axis, one row per
    source, with the live price as a vertical line. `sources`: [(label, view)].
    """
    price = get_price("BTC-USD")
    rows = [(label, lv) for label, view in sources
            if (lv := extract_btc_levels(" ".join(
                [view.get("shift_note") or "", view.get("stance_summary") or ""]),
                price))]
    if not rows:
        return None
    vals = [v for _, lv in rows for v in lv] + ([price] if price else [])
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.06 or hi * 0.05
    lo, hi = lo - pad, hi + pad

    def x(v):
        return f"{(v - lo) / (hi - lo) * 100:.2f}%"

    now_line = ([html.Div(style={"position": "absolute", "left": x(price),
                                 "top": 0, "bottom": 0, "width": "1px",
                                 "background": C["text"], "opacity": 0.7})]
                if price else [])
    label_w = "96px"
    grid = {"display": "grid", "gridTemplateColumns": f"{label_w} minmax(0,1fr)",
            "alignItems": "center"}
    lines = []
    for label, lv in rows:
        dots = [html.Span(
            className="lvl-hit", style={"left": x(v)},
            title=f"{label}: ${v:,.0f}"
                  + (f" ({(v / price - 1) * 100:+.1f}% vs now)" if price else ""),
            children=html.Span(className="lvl-dot"))
            for v in lv]
        lines.append(html.Div(style=grid, children=[
            html.Div(label, style={"color": C["dim"], "fontSize": "0.66rem",
                                   "whiteSpace": "nowrap", "overflow": "hidden",
                                   "textOverflow": "ellipsis"}),
            html.Div(style={"position": "relative", "height": "20px"}, children=[
                html.Div(style={"position": "absolute", "left": 0, "right": 0,
                                "top": "50%", "height": "1px",
                                "background": C["border"]}),
                *now_line, *dots]),
        ]))
    step = _nice_step(hi - lo)
    ticks = range(int(lo // step + 1) * step, int(hi) + 1, step)
    lines.append(html.Div(style=grid, children=[
        html.Div(),
        html.Div(style={"position": "relative", "height": "14px"}, children=[
            *now_line,
            *[html.Span(f"${t / 1000:,.0f}K" if t % 1000 == 0 else f"${t / 1000:,.1f}K",
                        style={**_CHART_NOTE, "position": "absolute",
                               "left": x(t), "transform": "translateX(-50%)",
                               "top": "2px", "whiteSpace": "nowrap"})
              for t in ticks]]),
    ]))
    flat = sorted(((v, label) for label, lv in rows for v in lv), reverse=True)
    return html.Div(style=_CHART_CARD, children=[
        html.Div([html.Span("BTC LEVELS IN THE CURRENT VIEWS", style=_CHART_TITLE),
                  html.Span(f"  now ${price:,.0f} (line)" if price else
                            "  live price unavailable", style=_CHART_NOTE)]),
        html.Div(lines, style={"marginTop": "8px"}),
        _table_twin("level list", ["Level", "Source", "vs now"],
                    [(f"${v:,.0f}", label,
                      f"{(v / price - 1) * 100:+.1f}%" if price else "—")
                     for v, label in flat]),
    ])


# Decisive-first ordering for the tally line only ("4 neutral · 2 mixed ·
# 1 bearish" reads better than a random dict order); row order itself is by
# recency, see consensus_section's `order`.
_CONSENSUS_RANK = {"bearish": 0, "bullish": 1, "mixed": 2, "neutral": 3}

# Staleness is measured from the NEWEST POST behind a view, not from when it was
# synthesized: a view regenerated this morning can still rest on three-week-old
# posts (slow feeds like @DonAlt), and that is exactly what makes two feeds'
# stances non-comparable if you don't show it.
_STALE_WARN_D = 3        # older than this -> yellow
_STALE_BAD_D = 7         # this old or more -> red
_CONSENSUS_HEADER_STYLE = {"color": C["dim"], "fontFamily": MONO,
                           "fontSize": "0.66rem", "textTransform": "uppercase",
                           "letterSpacing": "0.04em", "whiteSpace": "nowrap"}
_CONSENSUS_GRID = "128px 84px 148px 84px minmax(0,1fr)"


def _consensus_age_days(to_date):
    """Days since `to_date` (YYYY-MM-DD); None if absent/unparseable."""
    try:
        d = datetime.strptime(to_date, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    return (datetime.now(timezone.utc).date() - d).days


def _one_line(text):
    """Collapse any whitespace/newlines to single spaces so the cell wraps on
    its own width instead of on the LLM's line breaks."""
    return " ".join((text or "").split())


def _consensus_tally(views):
    """'4 neutral · 2 mixed · 1 bearish' for one group, decisive sentiments
    first, with any feed that has no view yet counted separately."""
    counts = {}
    for v in views:
        sent = (v.get("overall_sentiment") or "").lower()
        key = sent if sent in _CONSENSUS_RANK else "n/a"
        counts[key] = counts.get(key, 0) + 1
    order = sorted(counts, key=lambda s: _CONSENSUS_RANK.get(s, 9))
    return " · ".join(f"{counts[s]} {s}" for s in order)


def _consensus_head():
    cells = ["SOURCE", "VIEW", "AS OF", "BASIS", "STANCE"]
    return html.Div(className="cons-head",
                    style={"display": "grid",
                           "gridTemplateColumns": _CONSENSUS_GRID,
                           "gap": "10px", "padding": "0 12px 6px",
                           "borderBottom": f"1px solid {C['border']}"},
                    children=[html.Div(c, style=_CONSENSUS_HEADER_STYLE) for c in cells])


def _open_view_button(text, target, style):
    """A link-styled button that switches influencer-subtabs to `target` (see
    the "open-view" clientside callback). Used for the Consensus SOURCE labels,
    which are the only way into the per-feed card views, and for the back link
    out of them."""
    return html.Button(text, id={"type": "open-view", "view": target},
                       n_clicks=0, className="open-view", style={
                           "background": "none", "border": "none",
                           "padding": 0, "cursor": "pointer",
                           "fontFamily": MONO, "textAlign": "left",
                           "color": C["blue"], **style})


def _view_cell(sent, color, recs):
    """Sentiment chip, then (with history) the recent-view strip and since
    when the current sentiment holds / what it replaced."""
    chip = html.Span(sent.upper() if sent else "—", style={
        "background": color, "color": C["bg"], "borderRadius": "10px",
        "padding": "1px 8px", "fontSize": "0.64rem", "fontWeight": "bold",
        "letterSpacing": "0.04em", "whiteSpace": "nowrap"})
    if not recs or not sent:
        return [chip]
    since, previous = _history_run(recs, sent)
    note = " · ".join(filter(None, [
        f"since {since[5:]}" if since else None,
        f"was {previous}" if previous else None]))
    return [chip, _history_strip(recs),
            html.Div(note, style={"color": C["dim"], "fontSize": "0.6rem",
                                  "marginTop": "3px", "whiteSpace": "nowrap"})]


def _consensus_row(label, view, unit="posts", target=None, recs=None):
    """One feed's stance: sentiment chip (+ its recent-view strip from
    sentiment_history), how old the underlying posts are, how many fed the
    synthesis, and its full stance -- shift_note (the sharpest concrete point /
    the visible shift, never a "no change" placeholder) as a bold lead-in,
    followed by the full stance_summary paragraph. Always shown in full, no
    click needed: this is the one place that lets you compare every feed's
    actual reasoning, not just a one-line headline, without leaving the tab."""
    sent = (view.get("overall_sentiment") or "").lower()
    color = _YT_SENTIMENT.get(sent, C["dim"])
    based_on = view.get("based_on") or {}
    to_date, count = based_on.get("to_date"), based_on.get("count")
    age = _consensus_age_days(to_date)
    age_color = (C["dim"] if age is None or age <= _STALE_WARN_D
                 else C["red"] if age >= _STALE_BAD_D else C["yellow"])
    as_of = f"{to_date}  ·  {age}d" if to_date and age is not None else "—"
    headline = _one_line(view.get("shift_note"))
    stance = _one_line(view.get("stance_summary"))
    if stance:
        stance_cell = html.Div([
            (html.Div(headline, style={"color": C["text"], "fontWeight": "bold",
                      "fontSize": "0.76rem", "lineHeight": "1.4"})
             if headline else html.Span()),
            html.Div(stance, style={"color": C["text"], "fontSize": "0.76rem",
                     "lineHeight": "1.5",
                     "marginTop": "4px" if headline else "0"}),
        ])
    else:
        stance_cell = html.Div(
            headline or ("no view generated yet" if not view else "—"),
            style={"color": C["dim"] if not view else C["text"],
                   "fontSize": "0.76rem", "lineHeight": "1.4"})
    # Rows top-align, not centre: the stance cell is shown IN FULL and wraps to
    # several lines, which would otherwise leave the chip and dates floating
    # mid-row. The cons-* classes restack the row on phones (see <style>);
    # data-ts marks a view regenerated since the last visit (new_badges.js).
    return html.Div(className="cons-row", **{"data-ts": view.get("generated_at") or ""},
                    style={
        "display": "grid", "gridTemplateColumns": _CONSENSUS_GRID, "gap": "10px",
        "alignItems": "start", "padding": "10px 12px",
        "borderBottom": f"1px solid {C['border']}"}, children=[
        html.Div(className="cons-src", children=
                 _open_view_button(label, target, {"fontSize": "0.78rem",
                                                   "fontWeight": "bold"})
                 if target else
                 html.Span(label, style={"color": C["text"], "fontSize": "0.78rem",
                                         "fontWeight": "bold"})),
        html.Div(_view_cell(sent, color, recs), className="cons-view"),
        html.Div(as_of, className="cons-asof",
                 style={"color": age_color, "fontFamily": MONO,
                        "fontSize": "0.7rem", "whiteSpace": "nowrap"}),
        html.Div(f"{count} {unit}" if count else "—", className="cons-basis",
                 style={"color": C["dim"], "fontSize": "0.7rem",
                        "whiteSpace": "nowrap"}),
        html.Div(stance_cell, className="cons-stance", style={"minWidth": "0"}),
    ])


def consensus_section():
    """Cross-feed CURRENT VIEW summary — see the _CONSENSUS_SOURCES comment.
    A feed whose view has not been generated yet still gets a row (dimmed), so a
    silently broken digest is visible rather than just absent."""
    # Freshest first: rows are sorted purely by AS OF recency (age ascending),
    # not by sentiment. A view with no parseable to_date sorts last.
    def order(label_view):
        view = label_view[1]
        age = _consensus_age_days((view.get("based_on") or {}).get("to_date"))
        return 999 if age is None else age

    rows = [(label, cls, loader() or {}, unit, target)
            for label, cls, loader, unit, target in _CONSENSUS_SOURCES]
    by_source = _history_by_source(_load_sentiment_history())
    children, ordered = [], []
    for cls in dict.fromkeys(r[1] for r in rows):
        group = sorted([(label, v, unit, target)
                        for label, c, v, unit, target in rows if c == cls],
                       key=order)
        ordered += [(label, v) for label, v, _, _ in group]
        children.append(html.Div([
            html.Span(cls.upper(), style={
                "color": C["text"], "fontSize": "0.72rem", "fontWeight": "bold",
                "letterSpacing": "0.06em"}),
            html.Span(f"  ·  {len(group)} sources  ·  "
                      f"{_consensus_tally([v for _, v, _, _ in group])}",
                      style={"color": C["dim"], "fontSize": "0.72rem"}),
        ], style={"padding": "14px 12px 8px"}))
        children.append(_consensus_head())
        children.extend(
            _consensus_row(label, v, unit, target,
                           by_source.get(_HISTORY_KEY.get(target)))
            for label, v, unit, target in group)
    children.append(html.Div(
        "Each row is that feed's rolling CURRENT VIEW in full. AS OF is the "
        "newest post the view rests on, not when it was generated. The dots "
        f"under a chip are that feed's last {HISTORY_STRIP_LEN} views, oldest "
        "first (green bullish, grey neutral, yellow mixed, red bearish; hover "
        "for the date). Click a source for its individual post/video cards.",
        style={"color": C["dim"], "fontSize": "0.68rem", "padding": "10px 12px",
               "lineHeight": "1.45"}))
    charts = [c for c in (sentiment_balance_chart(by_source),
                          btc_level_map(ordered)) if c is not None]
    out = [html.Div(charts, className="cons-charts", style={
        "display": "grid", "gridTemplateColumns": "repeat(2, minmax(0, 1fr))",
        "gap": "10px", "marginTop": "10px"})] if charts else []
    # minWidth keeps the four fixed columns (444px + gaps) from squeezing the
    # STANCE column on a narrow desktop window (the card then scrolls); phones
    # drop it and restack each row instead (.cons-inner in the <style>).
    return out + [html.Div(html.Div(children, className="cons-inner",
                                    style={"minWidth": "780px"}), style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderRadius": "8px", "marginTop": "10px", "overflowX": "auto"})]


app.layout = html.Div(
    className="root-pad",
    style={"backgroundColor": C["bg"], "color": C["text"], "fontFamily": MONO,
           "minHeight": "100vh", "padding": "20px 26px"},
    children=[
        dcc.Interval(id="interval", interval=REFRESH_MS, n_intervals=0),
        # The URL hash names the open view (#DaanCrypto): reload, bookmarks
        # and the browser's Back button all land on it (see the "view-url"
        # clientside callback).
        dcc.Location(id="url", refresh=False),
        # Fingerprint of the data behind the views (file mtimes + the last
        # warm pass). The heavy view callbacks fire on ITS change, not on
        # every interval tick: the data changes a few times a day at most.
        dcc.Store(id="data-version"),

        # Status bar + sub-tab bar stay pinned while the view below scrolls;
        # see .sticky-head in the <style>.
        html.Div(className="sticky-head", children=[

        # Status bar: GetXAPI credits · LLM spend · prices. whiteSpace pre:
        # a flex row trims each segment's edge spaces ("GetXAPI:$4.18").
        html.Div(style={"background": C["card"],
                        "border": f"1px solid {C['border']}",
                        "borderRadius": "8px",
                        "overflow": "hidden"}, children=[
            html.Div(id="status-row", style={
                "fontFamily": MONO, "fontSize": "0.74rem", "padding": "6px 12px",
                "display": "flex", "flexWrap": "wrap", "alignItems": "center",
                "lineHeight": "1.5", "whiteSpace": "pre"}),
        ]),

            # Sub-tabs: the Consensus panel, the influencer trade-call accounts
            # (IncomeSharks / traderstewie) and Geoff Kendrick (a forecast
            # ledger with no CURRENT VIEW, so no Consensus row). The other
            # analysis-digest feeds have no tab: their card views are opened by
            # clicking the feed's SOURCE name on Consensus (or by their URL
            # hash), which sets this component's value to one no Tab carries.
            html.Div(style={"overflowX": "auto", "marginTop": "8px",
                            "WebkitOverflowScrolling": "touch"},
                     children=dcc.Tabs(
                         id="influencer-subtabs", value="Consensus",
                         mobile_breakpoint=0,
                         # Wraps to a second row on desktop if it ever outgrows
                         # one; see .subtabs-strip in the <style>.
                         parent_className="subtabs-parent",
                         className="subtabs-strip",
                         style={"display": "flex", "flexWrap": "wrap"},
                         children=[
                             dcc.Tab(label="Consensus", value="Consensus",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="IncomeSharks", value="IncomeSharks",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="traderstewie", value="traderstewie",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="Geoff Kendrick", value="GeoffKendrick",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         ])),
        ]),   # end sticky-head

            # Consensus view: every digest feed's CURRENT VIEW side by side.
            # Unlike the per-feed views below it needs no show/hide style output —
            # its callback returns [] for every other sub-tab, and an empty div
            # takes no space. data-view (here and on each card list below)
            # scopes the "ÚJ" badges per view (assets/new_badges.js).
            html.Div(id="consensus-panel", **{"data-view": "Consensus"}),

            # Per-influencer header card (handle · win-rate/holdings · open ·
            # best performer), shown for whichever sub-tab is selected.
            html.Div(id="influencer-header"),

            # Trade-call view (IncomeSharks / traderstewie).
            html.Div(id="influencer-trade-view", children=[
            html.Div(id="influencer-pos-header", style=_SECTION_H),
            html.Div(id="influencer-winrate"),
            html.Div(id="influencer-positions", style={"marginTop": "4px",
                                                       "overflowX": "auto"}),

            html.Div(id="influencer-sig-header", style=_SECTION_H),
            dash_table.DataTable(
                id="influencer-signals",
                columns=INFLUENCER_TABLE_COLUMNS,
                sort_action="native",
                filter_action="none",
                page_size=25,
                markdown_options={"link_target": "_blank"},
                style_table={"overflowX": "auto", "marginTop": "10px"},
                style_header={
                    "backgroundColor": C["card"], "color": C["text"],
                    "fontWeight": "bold", "fontFamily": MONO, "fontSize": "11px",
                    "border": f"1px solid {C['border']}", "textAlign": "left",
                    "letterSpacing": "0.04em",
                },
                style_cell={
                    "backgroundColor": C["bg"], "color": C["text"],
                    "fontFamily": MONO, "fontSize": "12px", "textAlign": "left",
                    "border": f"1px solid {C['border']}", "padding": "6px 8px",
                    "whiteSpace": "normal", "height": "auto", "maxWidth": "420px",
                },
                style_data={"backgroundColor": C["bg"]},
                # Keep the date on one line ("2026-09-" / "25" otherwise).
                style_cell_conditional=[
                    {"if": {"column_id": "date"}, "whiteSpace": "nowrap"},
                ],
                # Tint entries green and exits red: legacy rows carry
                # buy/sell, schema-2 rows their position_action.
                style_data_conditional=[
                    *({"if": {"filter_query": f"{{signal_type}} = {a}"},
                       "backgroundColor": C["buy_bg"]}
                      for a in ("buy", "open", "add")),
                    *({"if": {"filter_query": f"{{signal_type}} = {a}"},
                       "backgroundColor": C["sell_bg"]}
                      for a in ("sell", "reduce", "close")),
                    {"if": {"column_id": "ticker"}, "color": C["blue"],
                     "fontWeight": "bold"},
                ],
            ),
            ]),   # end influencer-trade-view

            # Ben Cowen view: YouTube video analysis cards (analysis only — he is
            # not a trader we mirror, so no positions/signals/win-rate here).
            html.Div(id="youtube-view", style={"display": "none"}, children=[
                html.Div(["YouTube Analysis — Benjamin Cowen",
                          html.A("→ channel",
                                 href="https://www.youtube.com/channel/UCRvqjQPSeaWn-uEx-w0XOIg/videos",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="youtube-summaries", style={"marginTop": "4px"},
                         **{"data-view": "BenCowen"}),
            ]),

            # Jesse Olson view: YouTube video analysis cards (analysis only —
            # "The Market Sniper" is a swing trader, not one we mirror, so no
            # positions/signals/win-rate here — same pattern as Ben Cowen).
            html.Div(id="jesse-view", style={"display": "none"}, children=[
                html.Div(["YouTube Analysis — Jesse Olson (Market Sniper)",
                          html.A("→ channel",
                                 href="https://www.youtube.com/channel/UCtuoqGiIHBGMRmTGeXVrf9g/videos",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="jesse-summaries", style={"marginTop": "4px"},
                         **{"data-view": "JesseOlson"}),
            ]),

            # Ki Young Ju view: X/Twitter post analysis cards (analysis only —
            # CryptoQuant founder, BTC on-chain macro; never traded/mirrored).
            html.Div(id="ki-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — Ki Young Ju (CryptoQuant)",
                          html.A("→ @ki_young_ju",
                                 href="https://x.com/ki_young_ju",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="ki-summaries", style={"marginTop": "4px"},
                         **{"data-view": "KiYoungJu"}),
            ]),

            # Joao Wedson view: X/Twitter post analysis cards (analysis only —
            # Alphractal founder, crypto on-chain/quant; never traded/mirrored).
            html.Div(id="joao-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — Joao Wedson (Alphractal)",
                          html.A("→ @joao_wedson",
                                 href="https://x.com/joao_wedson",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="joao-summaries", style={"marginTop": "4px"},
                         **{"data-view": "JoaoWedson"}),
            ]),

            # DorkChicken view: X/Twitter post analysis cards (analysis only —
            # crypto/macro TA, chart-pattern & cycle-fractal reads; never
            # traded/mirrored).
            html.Div(id="dorkchicken-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — DorkChicken",
                          html.A("→ @DorkChicken",
                                 href="https://x.com/DorkChicken",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="dorkchicken-summaries", style={"marginTop": "4px"},
                         **{"data-view": "DorkChicken"}),
            ]),

            # DaanCrypto view: X/Twitter post analysis cards (analysis only —
            # crypto TA: moving averages, Fibonacci, market structure/liquidity,
            # ETF flows, dominance; never traded/mirrored).
            html.Div(id="daancrypto-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — DaanCrypto",
                          html.A("→ @DaanCrypto",
                                 href="https://x.com/DaanCrypto",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="daancrypto-summaries", style={"marginTop": "4px"},
                         **{"data-view": "DaanCrypto"}),
            ]),

            # DonAlt view: X/Twitter post analysis cards (analysis only --
            # crypto trader/TA: BTC support/resistance with occasional
            # explicit invalidation levels; never traded/mirrored).
            html.Div(id="donalt-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — DonAlt",
                          html.A("→ @DonAlt",
                                 href="https://x.com/DonAlt",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="donalt-summaries", style={"marginTop": "4px"},
                         **{"data-view": "DonAlt"}),
            ]),

            # Cowen (X) view: X/Twitter post analysis cards (analysis only --
            # the same analyst as the Cowen (YT) sub-tab, but his short-form
            # macro side: Fed path, long-end yields, DXY, liquidity cycles;
            # never traded/mirrored).
            html.Div(id="cowen-x-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — Benjamin Cowen",
                          html.A("→ @benjamincowen",
                                 href="https://x.com/benjamincowen",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="cowen-x-summaries", style={"marginTop": "4px"},
                         **{"data-view": "CowenX"}),
            ]),

            # Glassnode view: X/Twitter post analysis cards (analysis only --
            # the only COMPANY account in the digest: on-chain + derivatives
            # desk reads, options/vol positioning, ETF flows, holder cohorts;
            # never traded/mirrored).
            html.Div(id="glassnode-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — Glassnode",
                          html.A("→ @glassnode",
                                 href="https://x.com/glassnode",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="glassnode-summaries", style={"marginTop": "4px"},
                         **{"data-view": "Glassnode"}),
            ]),

            # Truecrypto view: X/Twitter post analysis cards (analysis only —
            # BTC price structure/range trading, ETF flows; never
            # traded/mirrored). require_market_signal filtered upstream, so
            # this only ever shows posts with a real number in them.
            html.Div(id="truecrypto-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — Truecrypto",
                          html.A("→ @Truecrypto",
                                 href="https://x.com/Truecrypto",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="truecrypto-summaries", style={"marginTop": "4px"},
                         **{"data-view": "Truecrypto"}),
            ]),

            # Geoff Kendrick view: X/Twitter TOPIC-SEARCH analysis cards (analysis
            # only — every account's coverage of Standard Chartered's Geoff
            # Kendrick crypto research; never traded/mirrored). Multi-author, so
            # each card shows the poster and the header links to the X search.
            html.Div(id="kendrick-view", style={"display": "none"}, children=[
                html.Div(["Twitter Analysis — Geoff Kendrick / Standard Chartered",
                          html.A("→ search",
                                 href="https://x.com/search?q=%22Geoff%20Kendrick%22&f=live",
                                 target="_blank", rel="noopener noreferrer",
                                 style={"color": C["blue"], "marginLeft": "14px",
                                        "textTransform": "none",
                                        "letterSpacing": "normal",
                                        "textDecoration": "none",
                                        "fontWeight": "normal",
                                        "fontSize": "0.8rem"})],
                         style=_SECTION_H),
                html.Div(id="kendrick-summaries", style={"marginTop": "4px"},
                         **{"data-view": "GeoffKendrick"}),
            ]),


    ],
)




@app.callback(
    Output("status-row", "children"),
    Input("interval", "n_intervals"),
)
def refresh_status(_n):
    return status_row()


def _data_version():
    """Changes whenever a file the views render changes (trades, positions,
    every ledger / view / history under data/) or a warm pass refreshed the
    prices. data/cache is not included: it only caches prices."""
    paths = [TRADES_FILE, POSITIONS_FILE,
             *glob.glob(os.path.join(DATA_DIR, "*.json"))]
    newest = 0
    for path in paths:
        try:
            newest = max(newest, os.stat(path).st_mtime_ns)
        except OSError:
            pass
    return f"{len(paths)}:{newest}:{_warm_state['done']}"


@app.callback(
    Output("data-version", "data"),
    Input("interval", "n_intervals"),
    State("data-version", "data"),
)
def refresh_data_version(_n, current):
    """Polled every REFRESH_MS; the view callbacks below only re-render (and
    re-send up to ~450KB of cards) when this actually changes."""
    version = _data_version()
    return no_update if version == current else version


def _influencer_header(title, account):
    """Section title plus a clickable '→ @handle' link to the X profile
    (opens in a new tab). The parent header div uppercases the title via CSS;
    the link overrides textTransform so the handle keeps its original case."""
    return [
        title,
        html.A(f"→ @{account}", href=f"https://x.com/{account}",
               target="_blank", rel="noopener noreferrer",
               style={"color": C["blue"], "marginLeft": "14px",
                      "textTransform": "none", "letterSpacing": "normal",
                      "textDecoration": "none", "fontWeight": "normal"}),
    ]


@app.callback(
    Output("influencer-trade-view", "style"),
    Output("youtube-view", "style"),
    Output("jesse-view", "style"),
    Output("ki-view", "style"),
    Output("joao-view", "style"),
    Output("dorkchicken-view", "style"),
    Output("daancrypto-view", "style"),
    Output("donalt-view", "style"),
    Output("cowen-x-view", "style"),
    Output("glassnode-view", "style"),
    Output("truecrypto-view", "style"),
    Output("kendrick-view", "style"),
    Output("influencer-pos-header", "children"),
    Output("influencer-sig-header", "children"),
    Input("influencer-subtabs", "value"),
)
def switch_influencer_subtab(account):
    panels = ["trades", "BenCowen", "JesseOlson", "KiYoungJu", "JoaoWedson",
              "DorkChicken", "DaanCrypto", "DonAlt", "CowenX", "Glassnode",
              "Truecrypto", "GeoffKendrick"]
    selected = "trades" if account in INFLUENCER_ACCOUNTS else account
    styles = tuple({"display": "block" if panel == selected else "none"}
                   for panel in panels)
    headers = ((_influencer_header(f"{account} — Open Positions", account),
                _influencer_header(f"{account} — Signals", account))
               if selected == "trades" else ("", ""))
    return styles + headers


@app.callback(
    Output("influencer-header", "children"),
    Output("influencer-signals", "data"),
    Output("influencer-positions", "children"),
    Output("influencer-winrate", "children"),
    Output("youtube-summaries", "children"),
    Output("jesse-summaries", "children"),
    Output("ki-summaries", "children"),
    Output("joao-summaries", "children"),
    Output("dorkchicken-summaries", "children"),
    Output("daancrypto-summaries", "children"),
    Output("donalt-summaries", "children"),
    Output("cowen-x-summaries", "children"),
    Output("glassnode-summaries", "children"),
    Output("truecrypto-summaries", "children"),
    Output("kendrick-summaries", "children"),
    Input("data-version", "data"),
    Input("influencer-subtabs", "value"),
)
def refresh_influencers(_version, account):
    # Cowen (YT) / Cowen (X) / Jesse Olson / Ki Young Ju / Joao Wedson /
    # DorkChicken / DaanCrypto / DonAlt / Glassnode / Truecrypto / Geoff
    # Kendrick are analysis-only views, not traders: no header card /
    # positions / signals — just the cards.
    if account == "Consensus" or account not in {
        *INFLUENCER_ACCOUNTS, "BenCowen", "JesseOlson", "KiYoungJu",
        "JoaoWedson", "DorkChicken", "DaanCrypto", "DonAlt", "CowenX",
        "Glassnode", "Truecrypto", "GeoffKendrick",
    }:
        return "", [], None, None, [], [], [], [], [], [], [], [], [], [], []
    if account == "BenCowen":
        children = youtube_section(load_youtube_summaries())
        return "", [], None, None, children, [], [], [], [], [], [], [], [], [], []
    if account == "JesseOlson":
        children = youtube_section(load_jesse_olson_summaries(),
                                    empty_label="Jesse Olson")
        return "", [], None, None, [], children, [], [], [], [], [], [], [], [], []
    if account == "KiYoungJu":
        children = twitter_section(load_twitter_summaries())
        return "", [], None, None, [], [], children, [], [], [], [], [], [], [], []
    if account == "JoaoWedson":
        children = twitter_section(load_joao_summaries(), who="@joao_wedson")
        return ("", [], None, None, [], [], [], children, [], [], [], [], [], [], [])
    if account == "DorkChicken":
        children = twitter_section(load_dorkchicken_summaries(), who="@DorkChicken")
        return ("", [], None, None, [], [], [], [], children, [], [], [], [], [], [])
    if account == "DaanCrypto":
        children = twitter_section(load_daancrypto_summaries(), who="@DaanCrypto")
        return ("", [], None, None, [], [], [], [], [], children, [], [], [], [], [])
    if account == "DonAlt":
        children = twitter_section(load_donalt_summaries(), who="@DonAlt")
        return ("", [], None, None, [], [], [], [], [], [], children, [], [], [], [])
    if account == "CowenX":
        children = twitter_section(load_cowen_x_summaries(), who="@benjamincowen")
        return ("", [], None, None, [], [], [], [], [], [], [], children, [], [], [])
    if account == "Glassnode":
        children = twitter_section(load_glassnode_summaries(), who="@glassnode")
        return ("", [], None, None, [], [], [], [], [], [], [], [], children, [], [])
    if account == "Truecrypto":
        children = twitter_section(load_truecrypto_summaries(), who="@Truecrypto")
        return ("", [], None, None, [], [], [], [], [], [], [], [], [], children, [])
    if account == "GeoffKendrick":
        return ("", [], None, None, [], [], [], [], [], [], [], [], [], [],
                kendrick_forecast_section(load_kendrick_forecasts()))
    positions = load_positions()
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type") or "unknown")
                 for p in influencer_positions(positions)
                 if p.get("status") == "open"})
    resolutions = influencer_resolutions(positions, account=account)
    return (influencer_header_card(account, resolutions=resolutions),
            influencer_signals_data(load_trades(), account=account),
            html.Div([influencer_positions_table(resolutions),
                      _setups_block(positions, account)]),
            influencer_winrate_card(resolutions),
            [], [], [], [], [], [], [], [], [], [], [])


def _setups_block(positions, account):
    """The account's setup / needs-review calls: listed for reference, never in
    the win rate. Newest first, each with its date and tweet, so repeated
    setups on one ticker stay distinguishable."""
    items = []
    for p in positions:
        if p.get("account") == account and p.get("status") in ("setup", "review"):
            sig = (p.get("signals") or [{}])[-1]
            items.append((sig.get("timestamp") or "", p, _safe_href(sig.get("url"))))
    if not items:
        return html.Span()
    items.sort(key=lambda it: it[0], reverse=True)
    rows = [(_local_date(ts) or "—", (p["ticker"], C["blue"]),
             p.get("side") or "long", p["status"], _money(p.get("target")),
             _money(p.get("stop_loss")),
             html.A("↗ tweet", href=url, target="_blank",
                    rel="noopener noreferrer") if url else "—")
            for ts, p, url in items]
    return html.Details([
        html.Summary([html.Span("▸ ", className="caret"),
                      f"Setups / needs review ({len(items)}) — excluded from "
                      f"win rate"],
                     style={"color": C["dim"], "fontSize": "0.78rem",
                            "cursor": "pointer", "padding": "8px 2px"}),
        _table(["Date", "Ticker", "Side", "Status", "Target", "Stop", "Tweet"],
               rows, hide_sm={2, 5}),
    ], style={"marginTop": "6px"})


@app.callback(
    Output("consensus-panel", "children"),
    Input("data-version", "data"),
    Input("influencer-subtabs", "value"),
)
def refresh_consensus(_version, account):
    """Kept separate from refresh_influencers deliberately: that callback already
    fans 15 outputs across 12 branches, and this panel shares none of them.
    On a feed view opened from a Consensus row it renders only the back link:
    those views have no tab to highlight or to click back from."""
    if account in {target for *_, target in _CONSENSUS_SOURCES}:
        return [html.Div(_open_view_button("← Consensus", "Consensus",
                                           {"fontSize": "0.8rem"}),
                         style={"marginTop": "12px"})]
    if account != "Consensus":
        return []
    return [html.Div("Consensus — current stance across every analysis feed",
                     style=_SECTION_H)] + consensus_section()


# Every view the URL hash may name.
_VIEWS = sorted({"Consensus", "GeoffKendrick", *INFLUENCER_ACCOUNTS,
                 *(target for *_, target in _CONSENSUS_SOURCES)})

# View <-> URL hash, plus the Consensus SOURCE labels / back link. One callback
# owns influencer-subtabs.value and url.hash because each feeds the other: a
# tab click or an open-view click writes the hash (a history entry, so Back
# works); a page load or Back/Forward reads it. Clientside so a click can also
# scroll to the top: the Consensus panel is long, and without it a click on a
# lower row lands mid-way down the (shorter) feed view. Re-rendered open-view
# buttons arrive with n_clicks 0 and must not count as a click.
app.clientside_callback(
    """
    function(hash, _clicks, value) {
        const nu = dash_clientside.no_update;
        const views = %s;
        const trig = dash_clientside.callback_context.triggered
            .filter(t => t.prop_id !== '.');
        const click = trig.find(t => t.prop_id.startsWith('{') && t.value);
        if (click) {
            const id = click.prop_id.slice(0, click.prop_id.lastIndexOf('.'));
            const view = JSON.parse(id).view;
            window.scrollTo(0, 0);
            return [view, '#' + view];
        }
        if (trig.some(t => t.prop_id === 'influencer-subtabs.value')) {
            return [nu, '#' + value];
        }
        if (trig.length && trig.every(t => t.prop_id.startsWith('{'))) {
            return [nu, nu];
        }
        const wanted = decodeURIComponent((hash || '').replace(/^#/, ''));
        const view = views.includes(wanted) ? wanted : 'Consensus';
        return [view === value ? nu : view, nu];
    }
    """ % json.dumps(_VIEWS),
    Output("influencer-subtabs", "value"),
    Output("url", "hash"),
    Input("url", "hash"),
    Input({"type": "open-view", "view": ALL}, "n_clicks"),
    Input("influencer-subtabs", "value"),
)


# --- background cache warmer -------------------------------------------------
# yfinance is the dominant cost of the trade and Kendrick views, and Yahoo
# throttles per-IP, so batching cuts request count but NOT wall-clock. The fix
# is keeping the shared in-memory caches warm OFF the request path: every
# WARM_INTERVAL_S a pass re-fetches everything the views read that is older
# than WARM_MAX_AGE, i.e. the previous pass's data. Entries thus never get near
# the request path's PRICE_TTL and a page load never waits on Yahoo. (The
# original loop refreshed only EXPIRED entries, which left a cold window every
# hour: a page load right after expiry paid the refetch.)
WARM_INTERVAL_S = 1800
WARM_MAX_AGE = WARM_INTERVAL_S // 2
_warm_state = {"done": None}    # epoch of the last completed pass


def _warm_all():
    try:     # the status bar only reads the cached balance
        get_getxapi_credits()
    except Exception:
        pass
    positions = load_positions()
    try:
        kendrick, _, _ = _kendrick_rows(load_kendrick_forecasts())
        kndr_syms = {s for s in map(_kndr_symbol, (f.get("asset") for f in kendrick))
                     if s}
    except Exception:
        kndr_syms = set()
    try:
        warm_prices({_yf_symbol(p["ticker"], p.get("asset_type") or "unknown")
                     for p in influencer_positions(positions)
                     if p.get("status") == "open"}
                    | kndr_syms | {"BTC-USD"}, max_age=WARM_MAX_AGE)
    except Exception:
        pass
    try:
        # every cycle's OHLC path and entry/exit closes (incl. the recap check)
        influencer_resolutions(positions, max_age=WARM_MAX_AGE)
    except Exception:
        pass
    for sym in kndr_syms:
        try:
            get_ohlc(sym, KNDR_HISTORY_START, max_age=WARM_MAX_AGE)
        except Exception:
            pass
    _warm_state["done"] = time.time()


def _warm_loop():
    while True:
        _warm_all()
        time.sleep(WARM_INTERVAL_S)


if __name__ == "__main__":
    # Pre-warm price/series/OHLC caches in the background so page loads don't
    # block on ~30s of Yahoo round-trips (see _warm_loop above).
    threading.Thread(target=_warm_loop, daemon=True, name="cache-warmer").start()
    # Bind 0.0.0.0 INSIDE the container so Docker's port proxy can reach it;
    # host exposure is set by the publish mapping in docker-compose.yml.
    app.run(host="0.0.0.0", port=PORT, debug=False)
