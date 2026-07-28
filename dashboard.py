#!/usr/bin/env python3
"""
Plotly Dash dashboard for the portfolio-bot trade monitor.

Reads:
  * trades.json    — the signal event log (all-signals table)
  * positions.json — reconciled (account, portfolio, ticker) positions
                     (portfolio-summary cards + holdings pie)

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

import http.client as _http_client
import json
import os
import re
import socket as _socket
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yfinance as yf
from dash import ALL, Dash, ctx, dash_table, dcc, html, Input, Output, State
from dash.exceptions import PreventUpdate

import resolver

# Live IBKR paper-account reads (optional). Imported guarded so the dashboard
# still boots if ib_insync is absent; the IBKR tab then shows "Gateway offline".
try:
    import ibkr_connector as ibk
except Exception:        # noqa: BLE001 - any import failure -> feature disabled
    ibk = None

HOME = "/home/fbazsa/pilot_trader"
TRADES_FILE = "/home/fbazsa/pilot_trader/trades.json"
POSITIONS_FILE = "/home/fbazsa/pilot_trader/positions.json"
ORDERS_FILE = "/home/fbazsa/pilot_trader/data/orders.json"
EQUITY_FILE = "/home/fbazsa/pilot_trader/data/equity_curve.json"
BREAKER_FILE = "/home/fbazsa/pilot_trader/data/circuit_breaker.json"
STATE_FILE = "/home/fbazsa/pilot_trader/.monitor_state.json"
ENV_FILE = "/home/fbazsa/pilot_trader/.env"
STALE_HOURS = 8           # cron runs every 4h; >8h means a run was missed
REFRESH_MS = 60_000
PORT = 8051
DASH_CLIENT_ID = 20       # distinct from auto_trader (7) and test scripts (8-11)
DASH_IB_TIMEOUT = 8       # short connect deadline so the tab degrades fast
DOCKER_SOCKET = "/var/run/docker.sock"   # mounted into the container for stats/restart
DASH_CONTAINER = "pilot_trader_dashboard"
CONTAINER_STATS_TTL = 25  # cache container stats this many seconds
CRON_HOURS = [0, 4, 8, 12, 16, 20]       # monitor.py cron slots (UTC)

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

# --- API credits (GetXAPI) --------------------------------------------------
# GetXAPI exposes account credits at GET /account/me (-> credits_remaining).
# Anthropic has no remaining-credit-balance endpoint, so only GetXAPI is shown.
GETXAPI_BASE = "https://api.getxapi.com"
CREDITS_REFRESH_MS = 3_600_000   # 60 min — don't hammer the credits API
CREDITS_LOW_USD = 1.00           # below this, show the balance in red

# Anthropic spend telemetry written per run by monitor.log_cost().
COST_LOG_FILE = "/home/fbazsa/pilot_trader/data/cost_log.json"
# Reddit-miner Anthropic spend, written per run by scripts/reddit_miner.py
# (one record/run: {timestamp, in_tok, out_tok, total_usd}). Separate ledger so
# the Reddit cost is shown distinctly from the tweet-pipeline API costs.
REDDIT_COST_LOG_FILE = "/home/fbazsa/pilot_trader/data/reddit_cost_log.json"


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
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env(ENV_FILE)

# Cached GetXAPI credits: {balance: float|None, fetched_at: epoch|None, ok: bool}
_credits_cache = {"balance": None, "fetched_at": None, "ok": False}


def get_getxapi_credits():
    """Return the cached GetXAPI credits dict, refreshing at most every
    CREDITS_REFRESH_MS. Network failure leaves the last good value in place and
    flags ok=False so the UI can show a fetch error without blanking the card."""
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
    except (urllib.error.URLError, OSError, ValueError, TypeError):
        _credits_cache.update(fetched_at=now, ok=False)
    return _credits_cache


PORTFOLIO_LABELS = {"grok": "Grok", "claude": "Claude",
                    "deepseek": "DeepSeek", "chatgpt": "ChatGPT",
                    "gemini": "Gemini"}
# Account classification + null-portfolio fallback are shared with monitor/
# reconcile/auto_trader via accounts.py (single source of truth). Influencer
# accounts are kept entirely separate from the AI portfolio views (own tab).
from accounts import (ACCOUNT_DEFAULT_PF, INFLUENCER_ACCOUNTS,
                      MIRROR_PORTFOLIOS, NON_AI_ACCOUNTS)


def is_influencer(account):
    return account in INFLUENCER_ACCOUNTS


def ai_positions(positions):
    return [p for p in positions if p.get("account") not in NON_AI_ACCOUNTS]


def influencer_positions(positions):
    return [p for p in positions if is_influencer(p.get("account"))]


def _yf_symbol(ticker, asset_type):
    """yfinance needs a -USD suffix for crypto (BTC -> BTC-USD)."""
    if asset_type == "crypto" and ticker and "-" not in ticker:
        return f"{ticker}-USD"
    return ticker


# Analysis-only AI umbrellas whose model books are INDEPENDENT of the Autopilot
# bots: their model sub-tabs are labeled separately so the books never blend
# (e.g. @ralliesarena's Grok is a different $100K experiment from @grkportfolio's).
# Keyed `account|model`; the value is the display prefix. NOTE: @aifinancelabs is
# deliberately NOT here — it reports the SAME Autopilot books, which SHOULD merge.
UMBRELLA_TAG = {"ralliesarena": "Rallies"}


def _pf_key(account, base):
    """Model-grouping key. Independent umbrellas (UMBRELLA_TAG) are namespaced
    `account|model` so their books stay distinct from the mirrored bots."""
    if account in UMBRELLA_TAG and base and base != "unknown":
        return f"{account}|{base}"
    return base


def pf_of(p):
    base = p.get("portfolio") or ACCOUNT_DEFAULT_PF.get(p.get("account"), "unknown")
    return _pf_key(p.get("account"), base)


def pf_label(key):
    """Human label for a model-grouping key, including namespaced umbrella keys
    (`ralliesarena|grok` -> 'Rallies: Grok')."""
    if "|" in key:
        account, base = key.split("|", 1)
        return (f"{UMBRELLA_TAG.get(account, account)}: "
                f"{PORTFOLIO_LABELS.get(base, base.title())}")
    return PORTFOLIO_LABELS.get(key, key.title())


def _days_held(opened_date):
    if not opened_date:
        return None
    try:
        d = datetime.strptime(opened_date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except ValueError:
        return None

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
PIE_COLORS = ["#58a6ff", "#3fb950", "#bc8cff", "#d29922", "#e3b341",
              "#f85149", "#8cc665", "#39c5cf", "#ff7b72", "#79c0ff"]

# --- price cache (1h TTL) so the 60s dashboard refresh doesn't hammer Yahoo ---
PRICE_TTL = 3600
_price_cache = {}       # ticker -> (price_or_None, fetched_at)
_hist_cache = {}        # (ticker, date_str) -> (price_or_None, fetched_at)
_fetch_state = {"last": None}   # epoch of the most recent live Yahoo fetch

# Historical closes are IMMUTABLE (the close on a past date never changes), so
# they are persisted to disk and never re-fetched. Keyed "TICKER|YYYY-MM-DD".
DATA_DIR = "/home/fbazsa/pilot_trader/data"
PRICE_CACHE_FILE = os.path.join(DATA_DIR, "price_cache.json")


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
        os.makedirs(DATA_DIR, exist_ok=True)
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


def get_hist_close(ticker, date_str):
    if not date_str:
        return None
    # Immutable on-disk cache first (past closes never change).
    pkey = f"{ticker}|{date_str}"
    if pkey in _hist_persist:
        return _hist_persist[pkey]
    now = time.time()
    key = (ticker, date_str)
    hit = _hist_cache.get(key)
    if hit and (hit[0] is not None or now - hit[1] < PRICE_TTL):
        return hit[0]
    price = _fetch_hist_close(ticker, date_str)
    _hist_cache[key] = (price, now)
    # Persist only resolved closes for dates strictly in the past (immutable).
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if price is not None and date_str < today:
        with _hist_lock:
            _hist_persist[pkey] = price
        _save_hist_persist()
    return price


def warm_prices(symbols):
    """Batch-fetch current prices for many symbols in ONE yf.download call,
    populating the per-symbol cache. Falls back to single fetches on gaps."""
    now = time.time()
    need = sorted({s for s in symbols if s and not (
        _price_cache.get(s) and now - _price_cache[s][1] < PRICE_TTL)})
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


# --- return computation ------------------------------------------------------
def estimate_entry(ticker, entry_price, trade_date, fallback_date,
                   asset_type="stock"):
    # NaN-guard: pandas coerces a JSON null entry_price to truthy NaN, which
    # must not short-circuit the estimated-entry fallback.
    if entry_price and entry_price == entry_price:
        return entry_price, False
    date = trade_date or (fallback_date or "")[:10]
    return get_hist_close(_yf_symbol(ticker, asset_type), date), True


def compute_return(ticker, entry_price, trade_date, fallback_date,
                   asset_type="stock"):
    entry, estimated = estimate_entry(ticker, entry_price, trade_date,
                                      fallback_date, asset_type)
    if not entry:
        return None
    cur = get_price(_yf_symbol(ticker, asset_type))
    if not cur:
        return None
    return {"val": round((cur - entry) / entry * 100, 1),
            "estimated": estimated, "current": cur}


# --- daily price history (for the performance-over-time chart) ---------------
_series_cache = {}   # (ticker, start) -> (pandas Series date_str->close | None, ts)


def get_price_series(ticker, start_date):
    """Daily close Series (index = 'YYYY-MM-DD' strings) from start_date to now,
    cached 1h. None on failure."""
    now = time.time()
    key = (ticker, start_date)
    hit = _series_cache.get(key)
    if hit and now - hit[1] < PRICE_TTL:
        return hit[0]
    series = None
    try:
        hist = yf.Ticker(ticker).history(start=start_date)
        if not hist.empty:
            s = hist["Close"]
            s.index = s.index.strftime("%Y-%m-%d")
            series = s[~s.index.duplicated(keep="last")].sort_index()
    except Exception:
        series = None
    _series_cache[key] = (series, now)
    return series


def warm_series(symbols, start_date):
    """Batch-fetch daily close Series for many symbols in ONE yf.download call,
    populating _series_cache. Mirrors warm_prices; falls back to single fetches
    via get_price_series on the symbols the batch misses."""
    now = time.time()
    need = sorted({s for s in symbols if s and not (
        _series_cache.get((s, start_date))
        and now - _series_cache[(s, start_date)][1] < PRICE_TTL)})
    if not need:
        return
    _fetch_state["last"] = now
    close = None
    try:
        data = yf.download(need, start=start_date, progress=False, threads=True)
        if "Close" in data:
            close = data["Close"]
    except Exception:
        close = None
    for s in need:
        series = None
        try:
            if close is not None:
                col = close[s] if hasattr(close, "columns") and \
                    s in getattr(close, "columns", []) else close
                col = col.dropna()
                if len(col):
                    col.index = col.index.strftime("%Y-%m-%d")
                    series = col[~col.index.duplicated(keep="last")].sort_index()
        except Exception:
            series = None
        if series is not None:
            _series_cache[(s, start_date)] = (series, now)
        else:                    # batch missed this symbol — single fetch (self-caches)
            get_price_series(s, start_date)


_ohlc_cache = {}   # (symbol, start) -> (DataFrame[High,Low] | None, ts)


def get_ohlc(symbol, start_date):
    """Daily High/Low DataFrame (index = 'YYYY-MM-DD') from start_date to now,
    cached 1h. Used to resolve influencer calls against the price path."""
    now = time.time()
    key = (symbol, start_date)
    hit = _ohlc_cache.get(key)
    if hit and now - hit[1] < PRICE_TTL:
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


def _price_asof(series, date_str):
    """Last close on or before date_str (ISO strings sort lexicographically)."""
    if series is None:
        return None
    try:
        upto = series.loc[:date_str]
        return float(upto.iloc[-1]) if len(upto) else None
    except Exception:
        return None


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


TABLE_COLUMNS = [
    {"name": "DATE", "id": "date"},
    {"name": "ACCOUNT", "id": "account"},
    {"name": "PORTFOLIO", "id": "portfolio"},
    {"name": "TICKER", "id": "ticker"},
    {"name": "ACTION", "id": "signal_type"},
    {"name": "CONF", "id": "confidence"},
    {"name": "SIZE %", "id": "position_size_pct"},
    {"name": "ENTRY $", "id": "entry_price"},
    {"name": "RETURN %", "id": "return_pct"},
    {"name": "LINK", "id": "link", "presentation": "markdown"},
    {"name": "SUMMARY", "id": "reasoning"},
]

# Influencer (IncomeSharks) signals table — its own column set with the
# influencer-specific fields (asset type, stop loss, target).
INFLUENCER_TABLE_COLUMNS = [
    {"name": "DATE", "id": "date"},
    {"name": "TICKER", "id": "ticker"},
    {"name": "ASSET", "id": "asset_type"},
    {"name": "ACTION", "id": "signal_type"},
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


# --- portfolio summary + holdings -------------------------------------------
def spy_return_since(date_str):
    entry = get_hist_close("SPY", date_str)
    cur = get_price("SPY")
    if entry and cur:
        return round((cur - entry) / entry * 100, 1)
    return None


def _fmt_pct(v):
    return "n/a" if v is None else f"{v:+.1f}%"


def _color(v):
    if v is None:
        return C["dim"]
    return C["green"] if v > 0 else (C["red"] if v < 0 else C["dim"])


def _stat_line(label, value_span):
    return html.Div([
        html.Span(f"{label} ", style={"color": C["dim"]}),
        value_span,
    ], style={"fontSize": "0.82rem", "marginTop": "3px"})


def portfolio_cards(positions, selected=None):
    """Clickable per-portfolio summary cards. The card whose key == `selected`
    is highlighted (blue border); clicking a card drives the Holdings pie +
    position detail below (see the `pf-card` pattern-matching callback)."""
    open_pos = [p for p in positions if p.get("status") == "open"]
    by_pf = {}
    for p in open_pos:
        by_pf.setdefault(pf_of(p), []).append(p)

    cards = []
    for pf in sorted(by_pf):
        if pf == "unknown":          # unattributed umbrella tweets: no model card
            continue
        ps = by_pf[pf]
        rets, spy_rets = [], []
        for p in ps:
            r = compute_return(p["ticker"], p.get("entry_price"),
                               p.get("trade_date"),
                               (p.get("opened_at") or "")[:10],
                               p.get("asset_type", "stock"))
            d = p.get("trade_date") or (p.get("opened_at") or "")[:10]
            if r:
                rets.append((p["ticker"], r["val"]))
                # Benchmark SPY over the SAME window as this position's return;
                # a single min-date window overstates SPY for later entries.
                s = spy_return_since(d) if d else None
                if s is not None:
                    spy_rets.append(s)
        avg = round(sum(v for _, v in rets) / len(rets), 1) if rets else None
        best = max(rets, key=lambda x: x[1]) if rets else None
        worst = min(rets, key=lambda x: x[1]) if rets else None
        spy = round(sum(spy_rets) / len(spy_rets), 1) if spy_rets else None
        label = pf_label(pf)

        sel = (pf == selected)
        cards.append(html.Div(
            id={"type": "pf-card", "index": pf},
            n_clicks=0,
            style={
                "background": C["buy_bg"] if sel else C["card"],
                "border": (f"1px solid {C['blue']}" if sel
                           else f"1px solid {C['border']}"),
                "boxShadow": f"0 0 0 1px {C['blue']}" if sel else "none",
                "borderRadius": "8px", "padding": "14px 18px", "minWidth": "230px",
                "flex": "1", "cursor": "pointer",
                "transition": "border-color .15s, background .15s",
            },
            children=[
                html.Div(label, style={
                    "fontWeight": "bold", "fontSize": "0.95rem",
                    "color": C["text"], "marginBottom": "8px",
                    "borderBottom": f"1px solid {C['border']}", "paddingBottom": "6px",
                    "textTransform": "uppercase", "letterSpacing": "0.05em"}),
                _stat_line("open positions:",
                           html.Span(str(len(ps)), style={"color": C["text"],
                                                          "fontWeight": "bold"})),
                _stat_line("avg est. return:", html.Span([
                    html.Span(_fmt_pct(avg),
                              style={"color": _color(avg), "fontWeight": "bold"}),
                    html.Span(f"  vs S&P {_fmt_pct(spy)}",
                              style={"color": C["dim"], "fontSize": "0.72rem"}),
                ])),
                _stat_line("best:",
                           html.Span(f"{best[0]} {_fmt_pct(best[1])}" if best else "n/a",
                                     style={"color": _color(best[1] if best else None)})),
                _stat_line("worst:",
                           html.Span(f"{worst[0]} {_fmt_pct(worst[1])}" if worst else "n/a",
                                     style={"color": _color(worst[1] if worst else None)})),
            ],
        ))
    if not cards:
        return [html.Div("No open positions yet.", style={"color": C["dim"]})]
    return cards


def portfolios_in(positions):
    # Drop "unknown" — an AI-umbrella tweet (e.g. @ralliesarena) the LLM could not
    # attribute to a specific model has no model sub-tab of its own.
    pfs = sorted({pf_of(p) for p in positions} - {"unknown"}) if positions else []
    return pfs or ["grok", "claude", "deepseek"]


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


def position_detail_table(positions, portfolio):
    rows = []
    # Newest first: sort by trade_date (ISO dates sort chronologically), falling
    # back to opened_at; positions with no date sort to the bottom.
    ordered = sorted(positions,
                     key=lambda q: q.get("trade_date")
                     or (q.get("opened_at") or "")[:10] or "",
                     reverse=True)
    for p in ordered:
        if p.get("status") != "open" or pf_of(p) != portfolio:
            continue
        tdate = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
        tdate_disp = p.get("trade_date") or _local_date(p.get("opened_at")) or None
        atype = p.get("asset_type", "stock")
        sym = _yf_symbol(p["ticker"], atype)
        entry, est = estimate_entry(p["ticker"], p.get("entry_price"),
                                    p.get("trade_date"),
                                    (p.get("opened_at") or "")[:10], atype)
        cur = get_price(sym)
        ret = round((cur - entry) / entry * 100, 1) if (entry and cur) else None
        days = _days_held(tdate)
        size = p.get("size_pct")
        rows.append((
            (p["ticker"], C["blue"]),
            f"{size:.2f}%" if size is not None else "—",
            tdate_disp or "—",
            _money(entry) + ("*" if est and entry else ""),
            _money(cur),
            (_fmt_pct(ret), _color(ret)),
            str(days) if days is not None else "—",
        ))
    return _table(["Ticker", "Size %", "Trade Date", "Entry", "Current",
                   "Return %", "Days Held"], rows,
                  empty=f"No open positions for {pf_label(portfolio)}",
                  hide_sm={1, 2, 6})   # phones: drop size%/trade-date/days-held


def closed_trades_table(positions, portfolio, limit=5):
    """Recent closed trades for the SELECTED portfolio only (matches the pie +
    position detail). The Portfolio column is dropped -- every row is `portfolio`."""
    closed = [p for p in positions
              if p.get("status") == "closed" and pf_of(p) == portfolio]
    closed.sort(key=lambda p: p.get("closed_at") or "", reverse=True)
    rows = []
    for p in closed[:limit]:
        opened = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
        close_date = (p.get("closed_at") or "")[:10] or None
        # Display in Budapest local time; pricing below keeps the UTC dates.
        opened_disp = p.get("trade_date") or _local_date(p.get("opened_at")) or None
        close_disp = _local_date(p.get("closed_at")) or None
        atype = p.get("asset_type", "stock")
        sym = _yf_symbol(p["ticker"], atype)
        entry = p.get("entry_price")
        if not entry and opened:
            entry, _ = estimate_entry(p["ticker"], None, p.get("trade_date"),
                                      opened, atype)
        # exit price = close on the close date
        exit_px = get_hist_close(sym, close_date) if close_date else None
        ret = round((exit_px - entry) / entry * 100, 1) if (entry and exit_px) else None
        rows.append((
            (p["ticker"], C["blue"]),
            opened_disp or "—",
            close_disp or "—",
            _money(entry),
            _money(exit_px),
            (_fmt_pct(ret), _color(ret)),
        ))
    return _table(["Ticker", "Opened", "Closed", "Entry", "Exit", "Return %"],
                  rows, empty=f"No closed trades for {pf_label(portfolio)}",
                  hide_sm={1, 2})   # phones: drop opened/closed dates


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
            "signal_type": r.get("signal_type"),
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
                 resolver.CLOSED_LOSS: ("closed (loss)", "red")}


def influencer_resolutions(positions, account=None):
    """List of (position, resolution|None) for influencer calls, resolved
    against the realized price path. Includes calls the influencer EXPLICITLY
    closed (sell tweet): a target/stop hit inside the holding window counts as
    usual, otherwise the call is classified by realized return — excluding
    closed calls computed the win rate only over calls they hadn't talked
    about since. If `account` is given, restrict to that one handle."""
    out = []
    for p in influencer_positions(positions):
        status = p.get("status")
        if status not in ("open", "closed"):
            continue
        if account and p.get("account") != account:
            continue
        atype = p.get("asset_type") or "unknown"
        sym = _yf_symbol(p["ticker"], atype)
        tdate = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
        ohlc = get_ohlc(sym, tdate) if tdate else None
        if status == "open":
            out.append((p, resolver.resolve_position(p, ohlc)))
            continue
        cdate = (p.get("closed_at") or "")[:10] or None
        res = resolver.resolve_position(p, ohlc, until=cdate)
        if res is None and cdate:
            entry, _ = estimate_entry(p["ticker"], p.get("entry_price"),
                                      p.get("trade_date"),
                                      (p.get("opened_at") or "")[:10], atype)
            res = resolver.resolve_closed(p, entry,
                                          get_hist_close(sym, cdate), cdate)
        if res is not None:    # unpriceable closed calls are excluded entirely
            out.append((p, res))
    return out


def influencer_winrate_card(resolutions):
    s = resolver.win_stats([r for _, r in resolutions])
    wr = "n/a" if s["win_rate"] is None else f"{s['win_rate']:.0f}%"
    wr_color = C["dim"] if s["win_rate"] is None else (
        C["green"] if s["win_rate"] >= 50 else C["red"])
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderRadius": "8px", "padding": "12px 18px", "marginTop": "12px",
        "display": "inline-block"}, children=[
        html.Span(wr, style={"color": wr_color, "fontWeight": "bold",
                             "fontSize": "1.1rem"}),
        html.Span(f" win rate  ({s['decided']} calls resolved: "
                  f"{s['hit']} target / {s['stopped']} stopped / "
                  f"{s['closed_win'] + s['closed_loss']} closed) · "
                  f"{s['expired']} expired · {s['live']} live",
                  style={"color": C["dim"], "fontSize": "0.8rem"}),
    ])


# Per-influencer descriptor + accent color (left border) for the header card.
INFLUENCER_META = {
    "IncomeSharks": ("Stocks + crypto trade calls", C["blue"]),
    "CelalKucuker": ("Crypto-heavy trade calls", C["yellow"]),
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
        atype = p.get("asset_type") or "unknown"
        sym = _yf_symbol(p["ticker"], atype)
        entry = p.get("entry_price")
        if not entry:
            tdate = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
            entry = get_hist_close(sym, tdate) if tdate else None
        cur = get_price(sym)
        rr.append((p["ticker"], round((cur - entry) / entry * 100, 1)
                   if (entry and cur) else None))
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
                               f"{st['decided']} resolved"))
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
        sym = _yf_symbol(p["ticker"], atype)
        entry = p.get("entry_price")
        if not entry:
            tdate = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
            entry = get_hist_close(sym, tdate) if tdate else None
            est = entry is not None
        else:
            est = False
        cur = get_price(sym)
        ret = round((cur - entry) / entry * 100, 1) if (entry and cur) else None
        tdate = p.get("trade_date") or _local_date(p.get("opened_at")) or None
        if res:
            label, ckey = _STATUS_LABEL[res["status"]]
            status_cell = (label, C[ckey])
        else:
            status_cell = ("live", C["blue"])
        rows.append((
            (p["ticker"], C["blue"]),
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


def holdings_figure(positions, portfolio):
    """Pie of ALL open positions for the portfolio. Sized positions use their
    real weight + a color; unsized ones get a neutral-grey placeholder slice
    labeled 'TICKER ?' so the pie reflects the full book. The placeholder weight
    is the average disclosed weight (purely for visual sizing) and is NOT counted
    toward the disclosed-percentage label."""
    open_pos = [p for p in positions
                if pf_of(p) == portfolio and p["status"] == "open"]
    label = pf_label(portfolio)
    if not open_pos:
        fig = go.Figure(go.Pie(labels=["no open positions"], values=[1],
                               hole=0.45, textinfo="none",
                               marker=dict(colors=[C["border"]])))
    else:
        sized = [p for p in open_pos if p.get("size_pct") is not None]
        unsized = [p for p in open_pos if p.get("size_pct") is None]
        disclosed = sum(p["size_pct"] for p in sized)
        # placeholder weight ~ a typical holding so grey slices are visible
        placeholder = round(disclosed / len(sized), 2) if sized else 1.0

        labels, values, colors = [], [], []
        for i, p in enumerate(sized):
            labels.append(f"{p['ticker']} {p['size_pct']:.1f}%")
            values.append(p["size_pct"])
            colors.append(PIE_COLORS[i % len(PIE_COLORS)])
        for p in unsized:
            labels.append(f"{p['ticker']} ?")
            values.append(placeholder)
            colors.append(C["dim"])     # neutral grey for unknown weight

        fig = go.Figure(go.Pie(
            labels=labels, values=values, hole=0.45, sort=False,
            textinfo="label", textfont=dict(family=MONO, color=C["text"]),
            marker=dict(colors=colors, line=dict(color=C["bg"], width=2))))

    total = sum(p["size_pct"] for p in open_pos if p.get("size_pct") is not None)
    n_unsized = sum(1 for p in open_pos if p.get("size_pct") is None)
    suffix = f", {n_unsized} unsized" if n_unsized else ""
    fig.update_layout(
        title=dict(
            text=f"{label} — {len(open_pos)} open holdings "
                 f"({total:.1f}% of book disclosed{suffix})",
            font=dict(family=MONO, color=C["text"], size=14)),
        paper_bgcolor=C["card"], plot_bgcolor=C["card"],
        font=dict(family=MONO, color=C["text"]),
        # Fixed height + no legend so the pie's footprint is IDENTICAL for every
        # portfolio (1 holding or 12): the slice labels (textinfo="label") already
        # name each holding, and a variable-length legend was what shifted the
        # layout / squished the row between card selections.
        height=380, showlegend=False,
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


# Stable per-portfolio colors (+ S&P) shared by the timeseries and bar charts.
PF_COLORS = {"Grok": "#58a6ff", "Claude": "#bc8cff", "DeepSeek": "#3fb950",
             "ChatGPT": "#e3b341", "S&P 500": "#f85149",
             "My Paper": "#f0f6fc"}   # bright near-white: the paper mirror line


def _dark_chart(fig, title, h=360):
    fig.update_layout(
        title=dict(text=title, font=dict(family=MONO, color=C["text"], size=14)),
        paper_bgcolor=C["card"], plot_bgcolor=C["card"], height=h,
        font=dict(family=MONO, color=C["text"], size=11),
        # Legend horizontal, below the plot — never overlaps the chart area on
        # narrow/mobile screens (plotly can't be CSS-media-queried); bottom
        # margin grows to make room. _dark_chart is used only by the perf chart.
        legend=dict(orientation="h", yanchor="top", y=-0.18,
                    xanchor="left", x=0,
                    font=dict(color=C["text"]), title_text=""),
        margin=dict(l=50, r=20, t=50, b=72),
        xaxis=dict(gridcolor=C["border"], linecolor=C["border"]),
        yaxis=dict(gridcolor=C["border"], linecolor=C["border"], ticksuffix="%"),
    )
    return fig


def _norm_date(val):
    """Normalize an LLM-extracted date to a valid 'YYYY-MM-DD' string, or None.
    Pads month-only ('YYYY-MM' -> 'YYYY-MM-01'); rejects anything that won't
    parse. One bad date used to poison `start` and make yfinance throw for
    every ticker (incl. SPY), blanking the whole chart."""
    if not val:
        return None
    s = str(val)[:10]
    if len(s) == 7:            # 'YYYY-MM' -> first of month
        s += "-01"
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except ValueError:
        return None


def _perf_rows(positions):
    """Build cumulative equal-weight return % rows per portfolio + S&P 500 (list
    of {date, portfolio, return}). Shared by the AI-tab chart and the Overview
    chart. Returns (rows, start_date).

    CLOSED positions are included, with their return frozen at the exit-date
    price from the close date onward. Open-only made the chart survivorship-
    biased: every sold winner/loser vanished, retroactively rewriting the
    curve each time a portfolio exited something."""
    entries = []   # (portfolio_label, yf_symbol, entry, open_date, close_date|None)
    for p in positions:
        status = p.get("status")
        if status not in ("open", "closed"):
            continue
        if pf_of(p) == "unknown":    # unattributed umbrella tweets: no chart line
            continue
        atype = p.get("asset_type", "stock")
        entry, _ = estimate_entry(p["ticker"], p.get("entry_price"),
                                  p.get("trade_date"),
                                  (p.get("opened_at") or "")[:10], atype)
        od = _norm_date(p.get("trade_date")) or _norm_date((p.get("opened_at") or "")[:10])
        cd = _norm_date((p.get("closed_at") or "")[:10]) \
            if status == "closed" else None
        if entry and od:
            entries.append((pf_label(pf_of(p)),
                            _yf_symbol(p["ticker"], atype), entry, od, cd))
    if not entries:
        return [], None

    start = min(e[3] for e in entries)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idx = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start=start, end=today)]
    # Batch all daily-series fetches into ONE yf.download (incl. the SPY
    # benchmark) so the per-symbol get_price_series calls below hit warm cache
    # instead of N sequential Yahoo round-trips.
    warm_series({t for _, t, _, _, _ in entries} | {"SPY"}, start)
    series = {t: get_price_series(t, start) for _, t, _, _, _ in entries}

    rows = []
    for pf in sorted({e[0] for e in entries}):
        pf_entries = [e for e in entries if e[0] == pf]
        for d in idx:
            rets = []
            for _, t, entry, od, cd in pf_entries:
                if d < od:
                    continue
                # Closed positions freeze at their exit-date price: the
                # realized return keeps contributing instead of vanishing.
                px_d = _price_asof(series.get(t),
                                   cd if (cd and d > cd) else d)
                if px_d:
                    rets.append((px_d - entry) / entry * 100)
            if rets:
                rows.append({"date": d, "portfolio": pf,
                             "return": round(sum(rets) / len(rets), 2)})
    # S&P 500 benchmark. Try SPY (ETF) then ^GSPC (index) as a fallback.
    # Baseline off the FIRST available trading-day close (iloc[0]) — not the
    # close "as of start", which is empty when start lands on a weekend/holiday
    # and silently dropped the whole S&P line.
    spy = get_price_series("SPY", start)
    if spy is None or len(spy) == 0:
        spy = get_price_series("^GSPC", start)
    if spy is not None and len(spy):
        base = float(spy.iloc[0])
        for d in idx:
            v = _price_asof(spy, d)
            if v:
                rows.append({"date": d, "portfolio": "S&P 500",
                             "return": round((v - base) / base * 100, 2)})
    return rows, start


def _paper_perf_rows():
    """Cumulative return % of the PAPER MIRROR from data/equity_curve.json,
    indexed to the first recorded NetLiq point (list of {date, portfolio,
    return} with portfolio='My Paper')."""
    data = load_equity_curve()
    pts = [d for d in data if d.get("netliq")]
    if len(pts) < 1:
        return []
    base = pts[0]["netliq"]
    if not base:
        return []
    return [{"date": d["date"], "portfolio": "My Paper",
             "return": round((d["netliq"] - base) / base * 100, 2)} for d in pts]


def overview_figure(positions):
    """Unified normalized comparison: each AI portfolio + the PAPER MIRROR + S&P
    500, all as cumulative return %. The headline 'who's winning' chart."""
    rows, _ = _perf_rows(positions)
    rows = (rows or []) + _paper_perf_rows()
    if not rows:
        return _dark_chart(px.line(), "Normalized performance — no data yet")
    df = pd.DataFrame(rows)
    fig = px.line(df, x="date", y="return", color="portfolio",
                  color_discrete_map=PF_COLORS)
    fig.update_traces(selector=dict(name="S&P 500"), line=dict(dash="dash"))
    fig.update_traces(selector=dict(name="My Paper"),
                      line=dict(width=3.5))
    return _dark_chart(fig, "Normalized performance — portfolios vs paper mirror "
                            "vs S&P 500 (cumulative %)", h=420)


# initial scaffolding (AI portfolios only — influencers live in their own tab)
_portfolios = portfolios_in(ai_positions(load_positions()))

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
      /* click-to-expand strategy rows (no marker; row hover affordance) */
      details > summary { list-style: none; }
      details > summary::-webkit-details-marker { display: none; }
      details > summary:hover { background-color: #1c2330; }
      ::-webkit-scrollbar { width: 10px; height: 10px; }
      ::-webkit-scrollbar-track { background: #0d1117; }
      ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 5px; }
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
        /* portfolio summary cards stack full-width on phones */
        #portfolio-summary > div { min-width: 100% !important;
                                   flex-basis: 100% !important;
                                   max-width: 100% !important; }
        /* Holdings: pie + detail stack vertically, pie spans full width */
        .pie-row { flex-direction: column !important; }
        .pie-row > .pie-col { flex: 0 0 100% !important;
                              max-width: 100% !important; min-width: 0 !important; }
        /* phones: hide low-priority columns (marked .col-sm-hide), tighten
           cell padding, and stop headers wrapping ("TRADE DATE") so the key
           columns (ticker/return/current) fit a 390px screen without h-scroll.
           A table still too wide falls back to its wrapper's overflow-x:auto. */
        .col-sm-hide { display: none !important; }
        table th { white-space: nowrap !important; }
        table td, table th { padding-left: 6px !important;
                             padding-right: 6px !important; }
        /* monospace status bar: smaller so segments don't dominate the screen */
        #status-row-1, #status-row-2 { font-size: 0.62rem !important;
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


# --- IBKR paper portfolio (live from IB Gateway) ----------------------------
def load_orders():
    """Read the order ledger (data/orders.json); [] if missing/unreadable."""
    if os.path.exists(ORDERS_FILE):
        try:
            with open(ORDERS_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
    return []


# Cached singleton IB connection for the dashboard. Dash callbacks run in
# multiple worker threads; a fresh connect (same clientId) per 60s refresh
# collided when two threads/viewers overlapped. Instead we keep ONE connection
# (clientId DASH_CLIENT_ID) on a persistent loop and serialize all access with a
# lock, reconnecting only if the session dropped.
_IB_LOCK = threading.Lock()
_ib_state = {"ib": None, "loop": None}


def _ibkr_call(fn):
    """Run fn(ib) on the cached singleton connection under the lock. Reconnects
    if needed. Raises on failure (caller maps that to 'Gateway offline')."""
    if ibk is None:
        raise RuntimeError("ibkr_connector unavailable")
    import asyncio
    with _IB_LOCK:
        loop = _ib_state["loop"]
        if loop is None or loop.is_closed():
            loop = asyncio.new_event_loop()
            _ib_state["loop"] = loop
        asyncio.set_event_loop(loop)
        ib = _ib_state["ib"]
        if ib is None or not ib.isConnected():
            ib = ibk.connect(client_id=DASH_CLIENT_ID, timeout=DASH_IB_TIMEOUT)
            _ib_state["ib"] = ib
        return fn(ib)


def _ibkr_reset():
    """Drop the cached connection so the next call reconnects fresh."""
    with _IB_LOCK:
        ib = _ib_state.get("ib")
        if ib is not None:
            try:
                ib.disconnect()
            except Exception:
                pass
        _ib_state["ib"] = None


def ibkr_snapshot():
    """Live (account, positions) via the cached singleton, or None if offline.
    Never raises — failure resets the connection and returns None so the tab
    degrades to 'Gateway offline'."""
    try:
        return _ibkr_call(lambda ib: (ibk.get_account_value(ib),
                                      ibk.get_portfolio(ib)))
    except Exception:        # noqa: BLE001 - offline is a normal state here
        _ibkr_reset()
        return None


def _nan(v):
    return v is None or (isinstance(v, float) and v != v)


def _pnl_span(v, pct=False):
    """Signed, colored money (or percent) span; '—' for None/NaN."""
    if _nan(v):
        return html.Span("—", style={"color": C["dim"]})
    txt = f"{v:+.2f}%" if pct else f"${v:,.2f}"
    return html.Span(txt, style={"color": _color(v), "fontWeight": "bold"})


def ibkr_offline(detail="IB Gateway not reachable on 127.0.0.1:4002"):
    return html.Div([
        html.Span("● ", style={"color": C["red"], "fontWeight": "bold"}),
        html.Span("Gateway offline", style={"color": C["red"],
                                            "fontWeight": "bold"}),
        html.Div(detail, style={"color": C["dim"], "fontSize": "0.78rem",
                                "marginTop": "4px"}),
    ], style={"background": C["card"], "border": f"1px solid {C['border']}",
              "borderRadius": "8px", "padding": "14px 18px", "marginTop": "12px"})


def ibkr_account_card(acct):
    now = datetime.now(timezone.utc).astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return html.Div(
        style={"background": C["card"], "border": f"1px solid {C['border']}",
               "borderRadius": "8px", "padding": "14px 18px", "minWidth": "320px",
               "maxWidth": "460px", "marginTop": "12px"},
        children=[
            html.Div(f"Paper Account {acct.get('account', '')}", style={
                "fontWeight": "bold", "fontSize": "0.95rem", "color": C["text"],
                "marginBottom": "8px", "borderBottom": f"1px solid {C['border']}",
                "paddingBottom": "6px", "textTransform": "uppercase",
                "letterSpacing": "0.05em"}),
            _stat_line("net liquidation:",
                       html.Span(_money(acct.get("net_liquidation")),
                                 style={"color": C["text"], "fontWeight": "bold"})),
            _stat_line("available funds:",
                       html.Span(_money(acct.get("available_funds")),
                                 style={"color": C["text"]})),
            _stat_line("total cash:",
                       html.Span(_money(acct.get("total_cash")),
                                 style={"color": C["text"]})),
            _stat_line("today's P&L:", _pnl_span(acct.get("daily_pnl"))),
            _stat_line("total P&L since start:", _pnl_span(acct.get("total_pnl"))),
            html.Div(f"last updated {now} {DISPLAY_TZ.key.split('/')[-1]}",
                     style={"color": C["dim"], "fontSize": "0.7rem",
                            "marginTop": "8px"}),
        ],
    )


def ibkr_positions_table(positions):
    rows_data = []
    for p in positions:
        if p.get("sec_type") != "STK" or not p.get("position"):
            continue
        avg = p.get("avg_cost") or 0.0
        cur = p.get("market_price") or 0.0
        pnl_pct = ((cur - avg) / avg * 100) if avg else None
        rows_data.append((p, pnl_pct))
    # Sort by unrealized P&L descending.
    rows_data.sort(key=lambda x: (x[0].get("unrealized_pnl") or 0.0), reverse=True)
    rows = []
    for p, pnl_pct in rows_data:
        col = _color(pnl_pct)
        rows.append([
            (p["ticker"], C["blue"]),
            f"{p['position']:g}",
            _money(p.get("avg_cost")),
            _money(p.get("market_price")),
            _money(p.get("market_value")),
            (_fmt_pct(pnl_pct), col),
        ])
    return _table(["Ticker", "Qty", "Avg Cost", "Current", "Mkt Value",
                   "Unrealized P&L %"], rows, empty="No open positions.",
                  hide_sm={1, 2, 4})   # phones: drop qty/avg-cost/mkt-value


# Status -> color for the order tables (issues 5/9).
_STATUS_COLOR = {
    "filled": C["green"], "submitted": C["yellow"], "pending": C["yellow"],
    "rejected": C["red"], "cancelled": C["dim"], "failed": C["red"],
}


def ibkr_pending_table(orders):
    """Working orders waiting to execute — pending/submitted (incl. MOO orders
    resting for the next open). Read from the ledger, no IB needed (issue 5)."""
    pend = [o for o in orders if o.get("status") in ("pending", "submitted")]
    pend.sort(key=lambda o: o.get("timestamp") or "", reverse=True)
    rows = []
    for o in pend:
        qty = o.get("shares")
        qtxt = f"{qty:g}" if qty else f"${o.get('quantity', 0):,.0f} notional"
        rows.append([
            _iso_to_local(o.get("timestamp"), "%Y-%m-%d %H:%M"),
            (o.get("ticker", "?"), C["blue"]),
            o.get("action", "?"),
            qtxt,
            (o.get("ib_status") or "queued", C["yellow"]),
        ])
    return _table(["Placed", "Ticker", "Action", "Qty", "IB Status"], rows,
                  empty="No working orders (none waiting for the open).",
                  hide_sm={0})   # phones: drop placed-timestamp


def ibkr_history_table(orders, positions):
    """Last 20 orders of ANY status (filled/pending/submitted/rejected/
    cancelled/failed), color-coded by status (issue 9)."""
    price_by_tkr = {p["ticker"]: p.get("market_price") for p in positions
                    if p.get("market_price")}
    allo = sorted(orders,
                  key=lambda o: o.get("updated_at") or o.get("timestamp") or "",
                  reverse=True)
    rows = []
    for o in allo[:20]:
        tkr = o.get("ticker", "?")
        status = o.get("status", "?")
        fill = o.get("fill_price")
        qty = o.get("filled_qty") or o.get("shares") or "—"
        if fill:
            cur = price_by_tkr.get(tkr) or get_price(tkr)
            ret = ((cur - fill) / fill * 100) if (cur and fill) else None
        else:
            cur, ret = None, None
        date = _iso_to_local(o.get("updated_at") or o.get("timestamp"),
                             "%Y-%m-%d %H:%M")
        rows.append([
            date,
            (tkr, C["blue"]),
            o.get("action", "?"),
            str(qty),
            _money(fill) if fill else "—",
            _money(cur) if cur else "—",
            (_fmt_pct(ret), _color(ret)) if ret is not None else "—",
            (status, _STATUS_COLOR.get(status, C["dim"])),
        ])
    return _table(["Date", "Ticker", "Action", "Qty", "Fill Price",
                   "Current", "Return %", "Status"], rows,
                  empty="No orders yet.",
                  hide_sm={0, 3, 4})   # phones: drop date/qty/fill-price


# --- redesign: data helpers -------------------------------------------------
def load_equity_curve():
    """Read data/equity_curve.json (daily NetLiq points); [] if missing."""
    if os.path.exists(EQUITY_FILE):
        try:
            with open(EQUITY_FILE) as f:
                return json.load(f) or []
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _breaker_state():
    """Read data/circuit_breaker.json; {} if missing."""
    if os.path.exists(BREAKER_FILE):
        try:
            with open(BREAKER_FILE) as f:
                return json.load(f) or {}
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _json_safe(obj):
    """Recursively replace NaN floats with None so a value survives the
    dcc.Store JSON round-trip."""
    if isinstance(obj, float):
        return None if obj != obj else obj
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    return obj


def portfolio_kpis(positions):
    """Per-portfolio KPIs for the hero strip + leaderboard. Returns
    (list[{label, ret, spy, delta, n_open, top}], overall_spy)."""
    open_pos = [p for p in positions if p.get("status") == "open"]
    by = {}
    for p in open_pos:
        by.setdefault(pf_of(p), []).append(p)
    out, all_dates = [], []
    for pf in sorted(by):
        if pf == "unknown":          # unattributed umbrella tweets: skip KPI row
            continue
        ps, rets, spy_rets = by[pf], [], []
        for p in ps:
            r = compute_return(p["ticker"], p.get("entry_price"),
                               p.get("trade_date"), (p.get("opened_at") or "")[:10],
                               p.get("asset_type", "stock"))
            d = p.get("trade_date") or (p.get("opened_at") or "")[:10]
            if d:
                all_dates.append(d)
            if r:
                rets.append((p["ticker"], r["val"]))
                # Benchmark SPY over the SAME window as this position's return;
                # a single min-date window overstates SPY for later entries.
                s = spy_return_since(d) if d else None
                if s is not None:
                    spy_rets.append(s)
        avg = round(sum(v for _, v in rets) / len(rets), 1) if rets else None
        spy = round(sum(spy_rets) / len(spy_rets), 1) if spy_rets else None
        delta = (round(avg - spy, 1)
                 if (avg is not None and spy is not None) else None)
        top = max(rets, key=lambda x: x[1])[0] if rets else None
        out.append({"label": pf_label(pf), "ret": avg,
                    "spy": spy, "delta": delta, "n_open": len(ps), "top": top})
    overall_spy = spy_return_since(min(all_dates)) if all_dates else None
    return out, overall_spy


# --- redesign: components ---------------------------------------------------
def _sep():
    """Dim ' · ' separator between status-bar segments."""
    return html.Span(" · ", style={"color": C["border"]})


def _cost_sums():
    """(today, month, all-time) Anthropic spend from data/cost_log.json."""
    try:
        with open(COST_LOG_FILE) as f:
            log = json.load(f)
        if not isinstance(log, list):
            log = []
    except (json.JSONDecodeError, OSError):
        log = []
    now = datetime.now(timezone.utc)
    today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    d = m = t = 0.0
    for r in log:
        usd = r.get("total_usd") or 0.0
        ts = r.get("timestamp") or ""
        t += usd
        if ts[:7] == month:
            m += usd
        if ts[:10] == today:
            d += usd
    return d, m, t


def _reddit_cost_month():
    """Current-month Reddit-miner Anthropic spend (USD) from
    data/reddit_cost_log.json. 0.0 if the ledger is missing/corrupt (the segment
    just reads $0.00 until the next reddit_miner run writes a record)."""
    try:
        with open(REDDIT_COST_LOG_FILE) as f:
            log = json.load(f)
        if not isinstance(log, list):
            log = []
    except (json.JSONDecodeError, OSError):
        log = []
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return sum((r.get("total_usd") or 0.0) for r in log
               if (r.get("timestamp") or "")[:7] == month)


def _next_cron_run(now=None):
    """Next monitor cron slot (returns a UTC datetime)."""
    now = now or datetime.now(timezone.utc)
    for hh in CRON_HOURS:
        cand = now.replace(hour=hh, minute=0, second=0, microsecond=0)
        if cand > now:
            return cand
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                             microsecond=0)


def status_row_1(store):
    """Status bar row 1: live/stale + monitor last & next run + gateway (+halt).
    e.g. '● LIVE · monitor last run: 2026-06-03 14:02 CEST · next: 16:00 CEST
    (~14m) · Gateway connected'."""
    live_color, live_txt, last_txt = C["dim"], "NO DATA", "unknown"
    last = None
    try:
        with open(STATE_FILE) as f:
            last = json.load(f).get("_last_run")
    except (json.JSONDecodeError, OSError):
        pass
    if last:
        try:
            dt = datetime.fromisoformat(last)
            hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            last_txt = _to_local(dt, "%Y-%m-%d %H:%M %Z")
            if hours > STALE_HOURS:
                live_color, live_txt = C["red"], f"STALE {hours:.0f}h"
            else:
                live_color, live_txt = C["green"], "LIVE"
        except ValueError:
            pass
    nxt = _next_cron_run()
    next_local = _to_local(nxt, "%H:%M %Z")
    mins = (nxt - datetime.now(timezone.utc)).total_seconds() / 60
    gw_ok = bool(store) and not store.get("offline")
    gw_color = C["green"] if gw_ok else C["red"]
    gw_txt = "Gateway connected" if gw_ok else "Gateway offline"

    segs = [
        html.Span("● ", style={"color": live_color, "fontWeight": "bold"}),
        html.Span(live_txt, style={"color": live_color, "fontWeight": "bold"}),
        _sep(),
        html.Span("monitor last run: ", style={"color": C["dim"]}),
        html.Span(last_txt, style={"color": C["text"]}),
        _sep(),
        html.Span("next: ", style={"color": C["dim"]}),
        html.Span(f"{next_local} (~{mins:.0f}m)", style={"color": C["text"]}),
        _sep(),
        html.Span(gw_txt, style={"color": gw_color, "fontWeight": "bold"}),
        _sep(),
        html.Span("Mirroring: ", style={"color": C["dim"]}),
        html.Span(", ".join(PORTFOLIO_LABELS.get(p, p.title())
                            for p in sorted(MIRROR_PORTFOLIOS)),
                  style={"color": C["text"], "fontWeight": "bold"}),
    ]
    br = _breaker_state()
    if br.get("halted"):
        segs += [_sep(), html.Span(
            f"■ HALTED: {br.get('halt_reason', 'execution')}",
            style={"color": C["red"], "fontWeight": "bold"})]
    return segs


def status_row_2():
    """Status bar row 2: container CPU/RAM + GetXAPI credits + Reddit/API costs
    + prices. e.g. 'CPU 0.1% · RAM 278/384MB · GetXAPI: $9.83 credits · Reddit:
    $0.00/mo · API Costs: today $0.80 / mo $4.13 · Prices as of: 17:44 CEST
    (yfinance, 1h cache)'."""
    cs = container_stat()
    if cs.get("ok"):
        cpu = f"{cs['cpu_pct']:.1f}%" if cs["cpu_pct"] is not None else "--"
        rm, lim = cs["ram_mb"], cs["ram_limit_mb"]
        ram = (f"{rm:.0f}/{lim:.0f}MB" if (rm is not None and lim)
               else (f"{rm:.0f}MB" if rm is not None else "--"))
    else:
        cpu, ram = "--", "--"

    cr = get_getxapi_credits()
    bal = cr["balance"]
    if bal is None:
        bal_txt, bal_color = "n/a", C["dim"]
    else:
        bal_txt = f"${bal:,.2f} credits"
        bal_color = C["red"] if bal < CREDITS_LOW_USD else C["green"]

    d, m, _ = _cost_sums()
    pa = _fetch_state["last"]
    prices_txt = (
        _to_local(datetime.fromtimestamp(pa, timezone.utc), "%H:%M %Z")
        + " (yfinance, 1h cache)" if pa else "not fetched yet")

    return [
        html.Span(f"CPU {cpu}", style={"color": C["text"]}),
        _sep(),
        html.Span(f"RAM {ram}", style={"color": C["text"]}),
        _sep(),
        html.Span("GetXAPI: ", style={"color": C["dim"]}),
        html.Span(bal_txt, style={"color": bal_color, "fontWeight": "bold"}),
        _sep(),
        html.Span("Reddit: ", style={"color": C["dim"]}),
        html.Span(f"${_reddit_cost_month():.2f}/mo",
                  style={"color": C["text"]}),
        _sep(),
        html.Span("API Costs: ", style={"color": C["dim"]}),
        html.Span(f"today ${d:,.2f} / mo ${m:,.2f}",
                  style={"color": C["text"]}),
        _sep(),
        html.Span("Prices as of: ", style={"color": C["dim"]}),
        html.Span(prices_txt, style={"color": C["dim"]}),
    ]


def _kpi_tile(label, value, value_color, sub=None):
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderRadius": "8px", "padding": "10px 14px", "minWidth": "0",
        "flex": "1 1 140px"}, children=[
        html.Div(label, style={"color": C["dim"], "fontSize": "0.68rem",
                               "textTransform": "uppercase",
                               "letterSpacing": "0.05em",
                               "whiteSpace": "nowrap", "overflow": "hidden",
                               "textOverflow": "ellipsis"}),
        html.Div(value, style={"color": value_color, "fontSize": "1.15rem",
                               "fontWeight": "bold", "marginTop": "2px"}),
        html.Div(sub or "", style={"color": C["dim"], "fontSize": "0.7rem",
                                   "marginTop": "1px", "minHeight": "0.9rem"}),
    ])


def leaderboard_table(kpis):
    """Overview leaderboard: portfolio · return · vs SPY · #open · top holding,
    sorted by return desc."""
    rows = []
    for k in sorted(kpis, key=lambda x: (x["ret"] is not None, x["ret"] or -1e9),
                    reverse=True):
        rows.append([
            (k["label"], C["blue"]),
            (_fmt_pct(k["ret"]), _color(k["ret"])),
            (f"{k['delta']:+.1f}" if k["delta"] is not None else "—",
             _color(k["delta"])),
            str(k["n_open"]),
            (k["top"] or "—", C["text"]),
        ])
    return _table(["Portfolio", "Return", "vs S&P", "# Open", "Top holding"],
                  rows, empty="No open AI positions yet.",
                  hide_sm={3})   # phones: drop # open


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
    Missing, corrupt, or not-yet-generated file -> {} (the banner just doesn't
    render -- see _current_view_banner)."""
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
    date = (v.get("published") or "")[:10]
    levels = v.get("key_price_levels") or []
    themes = v.get("top_themes") or []
    src_tag = "  ·  local whisper" if v.get("transcript_source") == "whisper" else ""
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderLeft": f"3px solid {color}", "borderRadius": "8px",
        "padding": "12px 16px", "marginTop": "10px"}, children=[
        html.Div(style={"display": "flex", "justifyContent": "space-between",
                        "alignItems": "flex-start", "gap": "12px"}, children=[
            html.A(v.get("title") or v.get("video_id"), href=v.get("url"),
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


def _current_view_banner(view):
    """Rolling 'current stance' banner (see CURRENT_VIEW_SCHEMA in
    twitter_digest.py / youtube_monitor.py), shown above a feed's post/video
    cards. A full-border card (vs. the individual cards' left-accent border)
    so it reads as a distinct header rather than another entry in the list.
    Empty span if no view has been generated yet (e.g. right after the feed
    was added, before its first cron run that processed new posts)."""
    if not view:
        return html.Span()
    sent = (view.get("overall_sentiment") or "neutral").lower()
    color = _YT_SENTIMENT.get(sent, C["dim"])
    based_on = view.get("based_on") or {}
    count, from_date, to_date = (based_on.get("count"), based_on.get("from_date"),
                                 based_on.get("to_date"))
    stamp = (f"as of {to_date}  ·  based on {count} posts since {from_date}"
             if count else "")
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {color}",
        "borderRadius": "8px", "padding": "12px 16px", "marginTop": "10px"},
        children=[
        html.Div(style={"display": "flex", "justifyContent": "space-between",
                        "alignItems": "flex-start", "gap": "12px"}, children=[
            html.Span("CURRENT VIEW", style={"color": C["dim"],
                      "fontSize": "0.68rem", "fontWeight": "bold",
                      "letterSpacing": "0.06em"}),
            html.Span(sent.upper(), style={
                "background": color, "color": C["bg"], "borderRadius": "10px",
                "padding": "1px 10px", "fontSize": "0.66rem", "fontWeight": "bold",
                "whiteSpace": "nowrap", "letterSpacing": "0.04em"}),
        ]),
        html.Div(view.get("stance_summary") or "", style={"color": C["text"],
                 "fontSize": "0.84rem", "marginTop": "8px", "lineHeight": "1.5"}),
        (html.Div(view.get("shift_note"), style={"color": C["yellow"],
                  "fontSize": "0.76rem", "marginTop": "8px", "lineHeight": "1.4",
                  "fontStyle": "italic"})
         if view.get("shift_note") else html.Span()),
        (html.Div(stamp, style={"color": C["dim"], "fontSize": "0.68rem",
                  "marginTop": "8px"}) if stamp else html.Span()),
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
KENDRICK_FORECASTS_FILE = os.path.join(DATA_DIR, "kendrick_forecasts.json")
KI_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "ki_young_ju_current_view.json")
JOAO_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "joao_wedson_current_view.json")
DORKCHICKEN_CURRENT_VIEW_FILE = os.path.join(DATA_DIR, "dorkchicken_current_view.json")


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


def load_ki_current_view():
    return _load_current_view(KI_CURRENT_VIEW_FILE)


def load_joao_current_view():
    return _load_current_view(JOAO_CURRENT_VIEW_FILE)


def load_dorkchicken_current_view():
    return _load_current_view(DORKCHICKEN_CURRENT_VIEW_FILE)


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
    date = (p.get("created_at") or "")[:10]
    author = p.get("author")              # search feeds: the poster (varies)
    levels = p.get("key_levels") or []
    themes = p.get("top_themes") or []
    chart_color = _YT_SENTIMENT.get((p.get("chart_trend") or "neutral").lower(),
                                    C["dim"])
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderLeft": f"3px solid {color}", "borderRadius": "8px",
        "padding": "12px 16px", "marginTop": "10px"}, children=[
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


def twitter_section(summaries, limit=8, who="@ki_young_ju"):
    """Render the most recent `limit` post analyses as cards (newest first).
    Account-agnostic: the card helpers read fields off each record, so the same
    renderer serves every twitter_digest.py feed; `who` only sets the empty
    state."""
    if not summaries:
        return [html.Div(f"No {who} posts analyzed yet.",
                         style={"color": C["dim"], "fontSize": "0.8rem",
                                "marginTop": "8px"})]
    ordered = sorted(summaries, key=lambda r: r.get("created_at") or "",
                     reverse=True)
    return [_tw_card(p) for p in ordered[:limit]]


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
    tf = f.get("timeframe") or "—"
    first = (f.get("first_seen") or "")[:10]
    sources = f.get("sources") or []
    n = f.get("source_count") or len(sources)

    summary = html.Summary(className="kndr-summary", style={
        "display": "flex", "alignItems": "center", "gap": "10px",
        "cursor": "pointer", "listStyle": "none", "padding": "9px 2px"},
        children=[
        html.Span("▸", style={"color": C["dim"], "fontSize": "0.7rem",
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
        "padding": "0 12px", "marginTop": "8px"}, children=[summary, body])


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
    rows = _merge_kndr_rows(forecasts)
    price = [f for f in rows if _is_price_forecast(f)]
    flow_hidden = len(rows) - len(price)
    price.sort(key=lambda f: (f.get("source_count") or 0,
                              f.get("last_seen") or f.get("first_seen") or ""),
               reverse=True)
    shown, extra = price[:limit], max(0, len(price) - limit)
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


# --- Reddit trading-strategy miner output ---------------------------------
# Mirrors the YouTube section: read scripts/reddit_miner.py's JSON ledger and
# render it with the same GitHub-dark helpers as the rest of the app.
REDDIT_STRATEGIES_FILE = os.path.join(DATA_DIR, "reddit_strategies.json")

# Per-subreddit badge tint (any unlisted sub falls back to blue).
_REDDIT_SUB_COLOR = {
    "algotrading": C["blue"], "CryptoMarkets": C["orange"],
    "BitcoinMarkets": C["yellow"], "ethtrader": C["purple"],
    "technicalanalysis": C["green"], "CryptoCurrency": C["orange"],
}
# Shared grid template: the header and every row use it so columns line up.
_REDDIT_GRID = ("104px minmax(0,1.7fr) 56px 88px 84px minmax(0,1.5fr) "
                "46px 40px")
_RTH = {"color": C["dim"], "fontFamily": MONO, "fontSize": "0.66rem",
        "textTransform": "uppercase", "letterSpacing": "0.04em",
        "whiteSpace": "nowrap"}
_FILTER_LABEL = {"color": C["dim"], "fontSize": "0.7rem",
                 "textTransform": "uppercase", "letterSpacing": "0.05em",
                 "marginBottom": "4px"}


def load_reddit_strategies():
    """Strategies mined from Reddit (written by scripts/reddit_miner.py). Missing
    or corrupt file -> [] (the tab just shows 'no data')."""
    try:
        with open(REDDIT_STRATEGIES_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _conf_color(c):
    if not isinstance(c, (int, float)):
        return C["dim"]
    return C["green"] if c >= 0.8 else C["yellow"] if c >= 0.65 else C["red"]


def _reddit_badge(sub):
    color = _REDDIT_SUB_COLOR.get(sub, C["blue"])
    return html.Span(f"r/{sub}", style={
        "display": "inline-block", "background": C["bg"],
        "border": f"1px solid {color}", "color": color, "borderRadius": "10px",
        "padding": "1px 7px", "fontSize": "0.66rem", "fontWeight": "bold",
        "whiteSpace": "nowrap", "overflow": "hidden", "textOverflow": "ellipsis"})


def _reddit_chip(text):
    return html.Span(text, style={
        "display": "inline-block", "background": C["bg"],
        "border": f"1px solid {C['border']}", "borderRadius": "10px",
        "padding": "1px 7px", "margin": "0 4px 0 0", "fontSize": "0.64rem",
        "color": C["dim"], "whiteSpace": "nowrap"})


def _flag_chip(text):
    # Red-tinted, wraps (red-flag phrases are sentences, not one-word tags).
    return html.Span(text, style={
        "display": "inline-block", "background": C["bg"],
        "border": f"1px solid {C['red']}", "borderRadius": "10px",
        "padding": "1px 8px", "margin": "2px 5px 2px 0", "fontSize": "0.66rem",
        "color": C["red"], "whiteSpace": "normal", "lineHeight": "1.35"})


def _reddit_detail(rec):
    """Panel revealed when a row is expanded: summary + entry/exit + claimed
    performance + red flags + found_at."""
    def field(label, val):
        return html.Div([
            html.Span(f"{label}: ", style={"color": C["dim"],
                                           "fontWeight": "bold"}),
            html.Span(val or "—"),
        ], style={"fontSize": "0.76rem", "marginTop": "4px",
                  "lineHeight": "1.4", "color": C["text"]})
    found = (_iso_to_local(rec["found_at"], "%Y-%m-%d %H:%M")
             if rec.get("found_at") else "—")
    flags = rec.get("red_flags") or []
    return html.Div(style={
        "background": C["bg"], "borderBottom": f"1px solid {C['border']}",
        "padding": "10px 14px"}, children=[
        html.Div(rec.get("summary") or "", style={
            "fontSize": "0.8rem", "color": C["text"], "lineHeight": "1.45"}),
        field("Entry", rec.get("entry")),
        field("Exit", rec.get("exit")),
        field("Performance", rec.get("performance_claim")),
        (html.Div([html.Span("⚠ Red flags: ", style={
            "color": C["red"], "fontWeight": "bold", "fontSize": "0.72rem"})]
            + [_flag_chip(f) for f in flags], style={"marginTop": "8px"})
         if flags else html.Span()),
        html.Div(f"found {found}", style={"color": C["dim"],
                                          "fontSize": "0.68rem",
                                          "marginTop": "8px"}),
    ])


def _reddit_row(rec):
    conf = rec.get("confidence")
    conf_pct = f"{conf*100:.0f}%" if isinstance(conf, (int, float)) else "—"
    tags = rec.get("tags") or []
    title = rec.get("title") or rec.get("post_id") or "(untitled)"

    def icon(on):
        return html.Span("✓" if on else "·", style={
            "color": C["green"] if on else C["dim"], "textAlign": "center"})

    cells = [
        _reddit_badge(rec.get("subreddit") or "?"),
        html.A(title, href=_safe_href(rec.get("url")), target="_blank",
               rel="noopener noreferrer", style={
                   "color": C["text"], "textDecoration": "none",
                   "display": "block", "overflow": "hidden",
                   "textOverflow": "ellipsis", "whiteSpace": "nowrap"}),
        html.Span(conf_pct, style={"color": _conf_color(conf),
                                   "fontWeight": "bold"}),
        html.Span(rec.get("asset") or "—", style={
            "color": C["blue"], "overflow": "hidden",
            "textOverflow": "ellipsis", "whiteSpace": "nowrap"}),
        html.Span(rec.get("timeframe") or "—", style={"color": C["text"],
                                                      "whiteSpace": "nowrap"}),
        html.Div([_reddit_chip(t) for t in tags[:4]] or "—", style={
            "overflow": "hidden", "whiteSpace": "nowrap"}),
        icon(rec.get("has_backtest")),
        icon(rec.get("has_code")),
    ]
    summary = html.Summary(cells, style={
        "display": "grid", "gridTemplateColumns": _REDDIT_GRID, "gap": "10px",
        "alignItems": "center", "padding": "7px 10px", "cursor": "pointer",
        "fontSize": "0.78rem", "fontFamily": MONO,
        "borderBottom": f"1px solid {C['border']}", "listStyle": "none"})
    return html.Details([summary, _reddit_detail(rec)],
                        style={"background": C["card"]})


def reddit_strategy_table(rows):
    """Header + one expandable <details> row per strategy (click to expand)."""
    if not rows:
        return html.Div("No strategies match the current filters.",
                        style={"color": C["dim"], "fontSize": "0.8rem",
                               "padding": "10px 2px"})
    header = html.Div(
        [html.Div(h, style=_RTH) for h in
         ["subreddit", "title", "conf", "asset", "timeframe", "tags",
          "test", "code"]],
        style={"display": "grid", "gridTemplateColumns": _REDDIT_GRID,
               "gap": "10px", "padding": "6px 10px", "background": C["bg"],
               "borderBottom": f"1px solid {C['border']}"})
    return html.Div([header] + [_reddit_row(r) for r in rows], style={
        "border": f"1px solid {C['border']}", "borderRadius": "8px",
        "overflow": "hidden", "marginTop": "12px"})


def reddit_stat_cards(rows):
    """KPI strip over the filtered rows: total · avg confidence · #backtested ·
    #with-code (Hungarian labels, uppercased by _kpi_tile)."""
    n = len(rows)
    confs = [r["confidence"] for r in rows
             if isinstance(r.get("confidence"), (int, float))]
    avg = sum(confs) / len(confs) if confs else None
    bt = sum(1 for r in rows if r.get("has_backtest"))
    code = sum(1 for r in rows if r.get("has_code"))
    tiles = [
        _kpi_tile("Összes stratégia", str(n), C["text"]),
        _kpi_tile("Átlagos confidence",
                  f"{avg*100:.0f}%" if avg is not None else "—",
                  _conf_color(avg)),
        _kpi_tile("Backtestelt", str(bt), C["green"] if bt else C["dim"],
                  f"{bt/n*100:.0f}% of {n}" if n else None),
        _kpi_tile("Van kód", str(code), C["green"] if code else C["dim"],
                  f"{code/n*100:.0f}% of {n}" if n else None),
    ]
    return html.Div(tiles, style={"display": "flex", "flexWrap": "wrap",
                                  "gap": "10px", "marginTop": "10px"})


def ibkr_exposure_card(orders, store):
    """Exposure gauge: open BUY notional vs the $10k cap, + cash/invested split.
    Exposure is NET per ticker (buys minus sells, floored at 0) — mirrors
    order_manager._open_buy_notional, so closed positions don't count forever."""
    net = {}
    for o in orders:
        if o.get("status") not in ("pending", "filled"):
            continue
        q = o.get("quantity", 0) or 0
        t = o.get("ticker")
        net[t] = net.get(t, 0.0) + (q if o.get("action") == "BUY" else -q)
    open_buy = sum(v for v in net.values() if v > 0)
    cap = 10_000.0
    pct = min(open_buy / cap, 1.0) if cap else 0.0
    bar_color = C["green"] if pct < 0.8 else C["yellow"] if pct < 1.0 else C["red"]
    acct = (store or {}).get("account") or {}
    nl, cash = acct.get("net_liquidation"), acct.get("total_cash")
    invested = (nl - cash) if (nl and cash) else None
    inv_txt = (f"  ·  invested {_money(invested)} / cash {_money(cash)}"
               if invested is not None else "")
    return html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderRadius": "8px", "padding": "12px 16px", "marginTop": "4px"},
        children=[
            html.Div([
                html.Span("Open BUY exposure  ", style={"color": C["dim"],
                                                        "fontSize": "0.8rem"}),
                html.Span(f"{_money(open_buy)} / {_money(cap)} "
                          f"({pct*100:.0f}%)",
                          style={"color": bar_color, "fontWeight": "bold",
                                 "fontSize": "0.82rem"}),
                html.Span(inv_txt, style={"color": C["dim"],
                                          "fontSize": "0.72rem"}),
            ]),
            html.Div(style={"background": C["bg"], "borderRadius": "5px",
                            "height": "10px", "marginTop": "7px",
                            "border": f"1px solid {C['border']}",
                            "overflow": "hidden"},
                     children=html.Div(style={
                         "width": f"{pct*100:.1f}%", "height": "100%",
                         "background": bar_color})),
        ])


def ibkr_funnel(orders):
    """Order-outcome funnel from the ledger: total → filled / pending / rejected
    / cancelled."""
    n = len(orders)
    cnt = {s: sum(1 for o in orders if o.get("status") == s)
           for s in ("filled", "pending", "submitted", "rejected", "cancelled",
                     "failed")}
    working = cnt["pending"] + cnt["submitted"]
    parts = [("orders", n, C["text"]), ("filled", cnt["filled"], C["green"]),
             ("working", working, C["yellow"]),
             ("rejected", cnt["rejected"], C["red"]),
             ("cancelled", cnt["cancelled"], C["dim"])]
    children = []
    for i, (label, val, col) in enumerate(parts):
        if i:
            children.append(html.Span(" → ", style={"color": C["dim"]}))
        children.append(html.Span(f"{label} {val}",
                                   style={"color": col, "fontWeight": "bold"}))
    return html.Div(children, style={"fontSize": "0.82rem", "padding": "6px 2px"})


def ibkr_halt_banner():
    """Red banner if the circuit breaker has halted execution; else nothing."""
    br = _breaker_state()
    if not br.get("halted"):
        return html.Span("")
    return html.Div(
        f"■ EXECUTION HALTED — {br.get('halt_reason', 'circuit breaker')} "
        f"(resets next UTC day)",
        style={"background": C["sell_bg"], "color": C["red"],
               "border": f"1px solid {C['red']}", "borderRadius": "6px",
               "padding": "8px 12px", "marginTop": "8px", "fontWeight": "bold",
               "fontSize": "0.82rem"})


# --- system status bar: Docker container stats + restart (via Docker socket) -
# Same approach as ~/paper_trader/dashboard.py: talk to the Docker daemon over
# its Unix socket (mounted into the container) for true per-container CPU/RAM.
class _UnixSocketHTTPConn(_http_client.HTTPConnection):
    def __init__(self, unix_socket):
        super().__init__("localhost")
        self._unix_socket = unix_socket

    def connect(self):
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(self._unix_socket)
        self.sock = s


def _docker_req(method, path):
    try:
        conn = _UnixSocketHTTPConn(DOCKER_SOCKET)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        code = resp.status
        conn.close()
        return code, body
    except Exception:        # noqa: BLE001 - socket absent -> degrade gracefully
        return None, None


def _docker_get(path):
    code, body = _docker_req("GET", path)
    if body is None:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return None


_cstat = {"data": None, "ts": 0.0}
_cstat_lock = threading.Lock()


def container_stat(name=DASH_CONTAINER):
    """CPU% / RAM / uptime / restart-count for one container via the Docker API
    (cached CONTAINER_STATS_TTL s). ok=False if the socket is unavailable."""
    with _cstat_lock:
        if _cstat["data"] is not None and time.time() - _cstat["ts"] < CONTAINER_STATS_TTL:
            return _cstat["data"]
    s = _docker_get(f"/containers/{name}/stats?stream=false")
    info = _docker_get(f"/containers/{name}/json")
    res = {"name": name, "cpu_pct": None, "ram_mb": None, "ram_limit_mb": None,
           "ram_pct": None, "uptime_s": None, "restart_count": None, "ok": False}
    if isinstance(info, dict):
        rc = info.get("RestartCount")
        if isinstance(rc, int):
            res["restart_count"] = rc
        started = (info.get("State") or {}).get("StartedAt")
        if started:
            try:
                st = started.rstrip("Z")
                if "." in st:
                    head, frac = st.split(".", 1)
                    st = f"{head}.{frac[:6]}"
                dt = datetime.fromisoformat(st).replace(tzinfo=timezone.utc)
                res["uptime_s"] = (datetime.now(timezone.utc) - dt).total_seconds()
            except (ValueError, TypeError):
                pass
    if isinstance(s, dict):
        try:
            cpu, precpu = s.get("cpu_stats", {}), s.get("precpu_stats", {})
            mem = s.get("memory_stats", {})
            cd = ((cpu.get("cpu_usage", {}).get("total_usage", 0) or 0)
                  - (precpu.get("cpu_usage", {}).get("total_usage", 0) or 0))
            sd = ((cpu.get("system_cpu_usage", 0) or 0)
                  - (precpu.get("system_cpu_usage", 0) or 0))
            ncpu = cpu.get("online_cpus") or 1
            res["cpu_pct"] = (cd / sd) * ncpu * 100 if sd > 0 else 0.0
            raw = mem.get("usage", 0) or 0
            cache = (mem.get("stats") or {}).get("cache", 0) or 0
            used, lim = raw - cache, mem.get("limit", 0) or 0
            res["ram_mb"] = used / 1_048_576
            res["ram_limit_mb"] = lim / 1_048_576 if lim > 0 else None
            res["ram_pct"] = (used / lim) * 100 if lim > 0 else None
            res["ok"] = True
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    with _cstat_lock:
        _cstat.update(data=res, ts=time.time())
    return res


app.layout = html.Div(
    className="root-pad",
    style={"backgroundColor": C["bg"], "color": C["text"], "fontFamily": MONO,
           "minHeight": "100vh", "padding": "20px 26px"},
    children=[
        dcc.Store(id="ibkr-store"),
        dcc.Store(id="selected-pf", data=_portfolios[0]),
        dcc.Interval(id="interval", interval=REFRESH_MS, n_intervals=0),

        html.H2("Pilot Trader", style={"margin": 0, "color": C["text"],
                                       "fontFamily": MONO, "fontSize": "1.3rem"}),

        # System status bar — two clean rows in one panel:
        #   row 1: live/stale · monitor last+next run · gateway
        #   row 2: container CPU/RAM · GetXAPI credits · API costs · prices
        html.Div(style={"background": C["card"],
                        "border": f"1px solid {C['border']}",
                        "borderRadius": "8px", "marginTop": "8px",
                        "overflow": "hidden"}, children=[
            html.Div(id="status-row-1", style={
                "fontFamily": MONO, "fontSize": "0.74rem", "padding": "6px 12px",
                "display": "flex", "flexWrap": "wrap", "alignItems": "center",
                "lineHeight": "1.5",
                "borderBottom": f"1px solid {C['border']}"}),
            html.Div(id="status-row-2", style={
                "fontFamily": MONO, "fontSize": "0.74rem", "padding": "6px 12px",
                "display": "flex", "flexWrap": "wrap", "alignItems": "center",
                "lineHeight": "1.5"}),
        ]),

        html.Div(id="summary", style={"color": C["dim"], "fontSize": "0.76rem",
                                      "marginTop": "8px"}),

        # Top-level tabs: Overview (landing; includes the paper account) · AI
        # Portfolios · Influencers · Reddit. Wrapped in a horizontally
        # scrollable container so the bar never wraps/breaks on mobile.
        html.Div(style={"overflowX": "auto", "WebkitOverflowScrolling": "touch",
                        "marginTop": "12px"},
                 children=dcc.Tabs(
                     id="main-tabs", value="overview", mobile_breakpoint=0,
                     style={"display": "flex", "flexWrap": "nowrap"},
                     children=[
                         dcc.Tab(label="Overview", value="overview",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         dcc.Tab(label="AI Portfolios", value="ai",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         dcc.Tab(label="Influencers", value="influencers",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         dcc.Tab(label="Reddit stratégiák", value="reddit",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                     ])),

        # Overview tab (landing): normalized chart + leaderboard + the merged
        # My Paper Account view (live IB Gateway reads; ledger-backed tables
        # still render when the gateway is offline).
        html.Div(id="overview-section", children=[
            html.Div("Normalized Performance — portfolios vs paper mirror vs S&P",
                     style=_SECTION_H),
            dcc.Graph(id="overview-chart",
                      config={"displayModeBar": False, "responsive": True},
                      style={"width": "100%"}),
            html.Div("Leaderboard", style=_SECTION_H),
            html.Div(id="overview-leaderboard",
                     style={"marginTop": "4px", "overflowX": "auto"}),

            # --- My Paper Account (merged from the former tab) --------------
            html.Div(id="ibkr-halt"),
            html.Div("My Paper Account — Account Summary", style=_SECTION_H),
            html.Div(id="ibkr-account"),
            html.Div("Exposure", style=_SECTION_H),
            html.Div(id="ibkr-exposure", style={"marginTop": "4px"}),
            html.Div("Open Positions", style=_SECTION_H),
            html.Div(id="ibkr-positions",
                     style={"marginTop": "4px", "overflowX": "auto"}),
            html.Div("Pending / Working Orders", style=_SECTION_H),
            html.Div(id="ibkr-pending",
                     style={"marginTop": "4px", "overflowX": "auto"}),
            html.Div("Order Outcomes", style=_SECTION_H),
            html.Div(id="ibkr-funnel", style={"marginTop": "4px"}),
            html.Div("Order History (last 20)", style=_SECTION_H),
            html.Div(id="ibkr-history",
                     style={"marginTop": "4px", "overflowX": "auto"}),
        ]),

        html.Div(id="ai-section", style={"display": "none"}, children=[

        # Click a card to select a portfolio (one selected at a time); the
        # Holdings pie + position detail below reflect the selection.
        html.Div("Portfolio Summary  ", style=_SECTION_H),
        html.Div("click a portfolio card to view its holdings",
                 style={"color": C["dim"], "fontSize": "0.72rem",
                        "marginTop": "4px"}),
        html.Div(id="portfolio-summary",
                 style={"display": "flex", "flexWrap": "wrap", "gap": "14px",
                        "marginTop": "12px"}),

        # (Performance chart lives on the Overview tab — not duplicated here.)
        # Holdings of the SELECTED card. Pie (40%, fixed) + position detail
        # table (flexible) side by side; stacks vertically on mobile (.pie-row).
        html.Div("Holdings", style=_SECTION_H),
        html.Div(className="pie-row",
                 style={"display": "flex", "flexDirection": "row",
                        "alignItems": "flex-start", "gap": "20px",
                        "marginTop": "6px", "flexWrap": "wrap"},
                 children=[
                     html.Div(className="pie-col",
                              style={"flex": "0 0 40%", "maxWidth": "40%",
                                     "minWidth": "300px"},
                              children=[
                                  dcc.Graph(id="holdings-pie",
                                            config={"displayModeBar": False,
                                                    "responsive": True},
                                            style={"width": "100%",
                                                   "height": "380px"}),
                              ]),
                     html.Div(id="position-detail",
                              style={"flex": "1", "minWidth": "0",
                                     "overflowX": "auto"}),
                 ]),

        html.Div("Recent Closed Trades", style=_SECTION_H),
        html.Div(id="closed-trades", style={"marginTop": "4px",
                                            "overflowX": "auto"}),

        html.Div("All Signals", id="signals-header", style=_SECTION_H),
        html.Div([
            html.Span("Legend: ", style={"color": C["dim"]}),
            html.Span("*", style={"color": C["text"], "fontWeight": "bold"}),
            html.Span(" estimated entry (close on trade date)   ",
                      style={"color": C["dim"]}),
            html.Span("?", style={"color": C["dim"], "fontWeight": "bold"}),
            html.Span(" low/none confidence (excluded from positions.json)",
                      style={"color": C["dim"]}),
        ], style={"fontSize": "0.72rem", "marginTop": "8px"}),
        dash_table.DataTable(
            id="signals-table",
            columns=TABLE_COLUMNS,
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
                "whiteSpace": "normal", "height": "auto", "maxWidth": "520px",
            },
            style_data={"backgroundColor": C["bg"]},
            style_filter={"backgroundColor": C["card"], "color": C["text"]},
            style_cell_conditional=[
                {"if": {"column_id": "reasoning"}, "minWidth": "320px",
                 "color": C["dim"]},
            ],
            style_data_conditional=[
                {"if": {"filter_query": "{signal_type} = buy"},
                 "backgroundColor": C["buy_bg"]},
                {"if": {"filter_query": "{signal_type} = sell"},
                 "backgroundColor": C["sell_bg"]},
                {"if": {"filter_query": "{return_val} > 0",
                        "column_id": "return_pct"},
                 "color": C["green"], "fontWeight": "bold"},
                {"if": {"filter_query": "{return_val} < 0",
                        "column_id": "return_pct"},
                 "color": C["red"], "fontWeight": "bold"},
                {"if": {"column_id": "ticker"}, "color": C["blue"],
                 "fontWeight": "bold"},
                # Low/none-confidence signals (gated out of positions.json) are
                # dimmed; their confidence cell carries a "?" marker.
                {"if": {"filter_query": '{confidence} contains "?"'},
                 "color": C["dim"], "fontWeight": "normal"},
            ],
        ),

        ]),   # end ai-section

        # --- Influencers tab -- hidden until selected ------------------------
        html.Div(id="influencer-section", style={"display": "none"}, children=[
            # One sub-tab per influencer handle (IncomeSharks / CelalKucuker /
            # traderstewie): trade-call view with winrate + positions + signals.
            html.Div(style={"overflowX": "auto",
                            "WebkitOverflowScrolling": "touch"},
                     children=dcc.Tabs(
                         id="influencer-subtabs", value="IncomeSharks",
                         mobile_breakpoint=0,
                         style={"display": "flex", "flexWrap": "nowrap"},
                         children=[
                             dcc.Tab(label="IncomeSharks", value="IncomeSharks",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="CelalKucuker", value="CelalKucuker",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="traderstewie", value="traderstewie",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="Ben Cowen", value="BenCowen",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="Jesse Olson", value="JesseOlson",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="Ki Young Ju", value="KiYoungJu",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="Joao Wedson", value="JoaoWedson",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="DorkChicken", value="DorkChicken",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                             dcc.Tab(label="Geoff Kendrick", value="GeoffKendrick",
                                     style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         ])),

            # Per-influencer header card (handle · win-rate/holdings · open ·
            # best performer), shown for whichever sub-tab is selected.
            html.Div(id="influencer-header"),

            # Trade-call view (IncomeSharks / CelalKucuker).
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
                style_data_conditional=[
                    {"if": {"filter_query": "{signal_type} = buy"},
                     "backgroundColor": C["buy_bg"]},
                    {"if": {"filter_query": "{signal_type} = sell"},
                     "backgroundColor": C["sell_bg"]},
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
                html.Div(id="youtube-summaries", style={"marginTop": "4px"}),
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
                html.Div(id="jesse-summaries", style={"marginTop": "4px"}),
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
                html.Div(id="ki-summaries", style={"marginTop": "4px"}),
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
                html.Div(id="joao-summaries", style={"marginTop": "4px"}),
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
                html.Div(id="dorkchicken-summaries", style={"marginTop": "4px"}),
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
                html.Div(id="kendrick-summaries", style={"marginTop": "4px"}),
            ]),
        ]),

        # (My Paper Account is no longer a separate tab — its components now
        # render inside the Overview tab, fed by the same ibkr-store + ledger.)

        # --- Reddit strategies tab -- hidden until selected ------------------
        # Strategies mined by scripts/reddit_miner.py (data/reddit_strategies.json):
        # stat strip + filters + a click-to-expand table.
        html.Div(id="reddit-section", style={"display": "none"}, children=[
            html.Div("Reddit stratégiák — koncentrált kereskedési stratégiák "
                     "(r/algotrading + crypto/TA subok)", style=_SECTION_H),
            html.Div(id="reddit-stats"),
            html.Div(style={"display": "flex", "flexWrap": "wrap", "gap": "20px",
                            "alignItems": "flex-start", "marginTop": "18px"},
                     children=[
                html.Div(style={"flex": "1 1 280px", "minWidth": "240px"},
                         children=[
                    html.Div("Min. confidence", style=_FILTER_LABEL),
                    dcc.Slider(id="reddit-conf", min=0.5, max=1.0, step=0.05,
                               value=0.5,
                               marks={v: {"label": f"{int(v * 100)}%",
                                          "style": {"color": C["dim"],
                                                    "fontSize": "0.6rem"}}
                                      for v in [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]},
                               tooltip={"placement": "bottom",
                                        "always_visible": False}),
                ]),
                html.Div(style={"flex": "1 1 200px", "minWidth": "180px"},
                         children=[
                    html.Div("Subreddit", style=_FILTER_LABEL),
                    dcc.Dropdown(id="reddit-sub", placeholder="Mind",
                                 clearable=True),
                ]),
                html.Div(style={"flex": "1 1 200px", "minWidth": "180px"},
                         children=[
                    html.Div("Tag", style=_FILTER_LABEL),
                    dcc.Dropdown(id="reddit-tag", placeholder="Mind",
                                 clearable=True),
                ]),
            ]),
            html.Div(id="reddit-table"),
        ]),
    ],
)


@app.callback(
    Output("overview-section", "style"),
    Output("ai-section", "style"),
    Output("influencer-section", "style"),
    Output("reddit-section", "style"),
    Input("main-tabs", "value"),
)
def switch_main_tab(tab):
    show, hide = {"display": "block"}, {"display": "none"}
    order = {"overview": 0, "ai": 1, "influencers": 2, "reddit": 3}
    i = order.get(tab, 0)
    return tuple(show if j == i else hide for j in range(4))


@app.callback(
    Output("reddit-stats", "children"),
    Output("reddit-table", "children"),
    Output("reddit-sub", "options"),
    Output("reddit-tag", "options"),
    Input("interval", "n_intervals"),
    Input("reddit-conf", "value"),
    Input("reddit-sub", "value"),
    Input("reddit-tag", "value"),
)
def render_reddit(_n, min_conf, sub, tag):
    """Filter data/reddit_strategies.json by confidence + subreddit + tag, then
    render the KPI strip and the expandable table. Dropdown options are derived
    from the FULL set so a filter never hides its own current value. Refreshes on
    the shared 60s interval like every other tab."""
    strategies = load_reddit_strategies()
    subs = sorted({s.get("subreddit") for s in strategies if s.get("subreddit")})
    tags = sorted({t for s in strategies for t in (s.get("tags") or [])})
    sub_opts = [{"label": f"r/{s}", "value": s} for s in subs]
    tag_opts = [{"label": t, "value": t} for t in tags]
    mc = min_conf if isinstance(min_conf, (int, float)) else 0.5
    rows = [s for s in strategies
            if (s.get("confidence") or 0) >= mc
            and (not sub or s.get("subreddit") == sub)
            and (not tag or tag in (s.get("tags") or []))]
    rows.sort(key=lambda r: r.get("found_at") or "", reverse=True)
    return (reddit_stat_cards(rows), reddit_strategy_table(rows),
            sub_opts, tag_opts)


@app.callback(
    Output("ibkr-account", "children"),
    Output("ibkr-positions", "children"),
    Output("ibkr-pending", "children"),
    Output("ibkr-history", "children"),
    Output("ibkr-exposure", "children"),
    Output("ibkr-funnel", "children"),
    Output("ibkr-halt", "children"),
    Input("ibkr-store", "data"),
    Input("main-tabs", "value"),
)
def refresh_ibkr(store, tab):
    # Renders from the SHARED ibkr-store (no own IB connection); skips rendering
    # work when the tab isn't visible. The order tables + exposure + funnel + halt
    # are ledger/file-backed and render even when the gateway is offline. The
    # paper account now lives on the Overview tab (merged from its own tab).
    if tab != "overview":
        raise PreventUpdate
    orders = load_orders()
    halt = ibkr_halt_banner()
    funnel = ibkr_funnel(orders)
    pending = ibkr_pending_table(orders)
    if not store or store.get("offline"):
        return (ibkr_offline(), "", pending, ibkr_history_table(orders, []),
                ibkr_exposure_card(orders, store), funnel, halt)
    acct, positions = store.get("account") or {}, store.get("positions") or []
    return (ibkr_account_card(acct),
            ibkr_positions_table(positions),
            pending,
            ibkr_history_table(orders, positions),
            ibkr_exposure_card(orders, store),
            funnel, halt)


@app.callback(
    Output("ibkr-store", "data"),
    Input("interval", "n_intervals"),
)
def update_ibkr_store(_n):
    """ONE shared IB fetch per 60s on the singleton connection — feeds the
    status bar (always) and the Overview tab's paper-account view. Records a
    daily NetLiq point for the equity curve."""
    snap = ibkr_snapshot()
    if snap is None:
        return {"offline": True}
    acct, positions = snap
    try:
        ibk.snapshot_equity(acct.get("net_liquidation"), acct.get("account", ""))
    except Exception:        # noqa: BLE001 - snapshotting must never break the UI
        pass
    return _json_safe({"offline": False, "account": acct, "positions": positions})


@app.callback(
    Output("status-row-1", "children"),
    Output("status-row-2", "children"),
    Input("interval", "n_intervals"),
    Input("ibkr-store", "data"),
)
def refresh_status(_n, store):
    return status_row_1(store), status_row_2()


@app.callback(
    Output("overview-chart", "figure"),
    Output("overview-leaderboard", "children"),
    Input("interval", "n_intervals"),
    Input("main-tabs", "value"),
)
def refresh_overview(_n, tab):
    if tab != "overview":
        raise PreventUpdate
    positions = ai_positions(load_positions())
    # Warm the current-price cache for the leaderboard (the removed hero-strip
    # callback used to do this on the shared interval).
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in positions} | {"SPY"})
    kpis, _ = portfolio_kpis(positions)
    return overview_figure(positions), leaderboard_table(kpis)


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
    Output("kendrick-view", "style"),
    Output("influencer-pos-header", "children"),
    Output("influencer-sig-header", "children"),
    Input("influencer-subtabs", "value"),
)
def switch_influencer_subtab(account):
    show, hide = {"display": "block"}, {"display": "none"}
    if account == "BenCowen":           # YouTube analysis view, not a trade view
        return hide, show, hide, hide, hide, hide, hide, "", ""
    if account == "JesseOlson":         # YouTube analysis view, not a trade view
        return hide, hide, show, hide, hide, hide, hide, "", ""
    if account == "KiYoungJu":          # X analysis view, not a trade view
        return hide, hide, hide, show, hide, hide, hide, "", ""
    if account == "JoaoWedson":         # X analysis view, not a trade view
        return hide, hide, hide, hide, show, hide, hide, "", ""
    if account == "DorkChicken":        # X analysis view, not a trade view
        return hide, hide, hide, hide, hide, show, hide, "", ""
    if account == "GeoffKendrick":      # X topic-search analysis view
        return hide, hide, hide, hide, hide, hide, show, "", ""
    return (show, hide, hide, hide, hide, hide, hide,
            _influencer_header(f"{account} — Open Positions", account),
            _influencer_header(f"{account} — Signals", account))


@app.callback(
    Output("selected-pf", "data"),
    Input({"type": "pf-card", "index": ALL}, "n_clicks"),
    prevent_initial_call=True,
)
def select_pf(_clicks):
    """Set the active portfolio when a summary card is clicked. The cards are
    re-rendered every 60s (refresh), which resets their n_clicks to 0 and fires
    this callback with a 0/None value — those re-render events are ignored so
    the current selection survives; only a real click (n_clicks >= 1) updates
    it. ctx.triggered_id is the clicked card's pattern id -> its `index` = pf."""
    trig = ctx.triggered
    if not trig or not trig[0].get("value"):
        raise PreventUpdate
    return ctx.triggered_id["index"]


@app.callback(
    Output("signals-table", "data"),
    Output("summary", "children"),
    Output("portfolio-summary", "children"),
    Output("closed-trades", "children"),
    Output("signals-header", "children"),
    Input("interval", "n_intervals"),
    Input("selected-pf", "data"),
)
def refresh(_n, portfolio):
    df = load_trades()
    positions = ai_positions(load_positions())   # AI views exclude influencers
    # Fall back to the first card if the stored selection no longer exists
    # (e.g. a portfolio closed all its positions between refreshes).
    pfs = portfolios_in(positions)
    if portfolio not in pfs:
        portfolio = pfs[0]
    # Batch live-price fetch for every symbol in play (one yf.download).
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in positions} | {"SPY"})
    cards = portfolio_cards(positions, selected=portfolio)   # warm price cache
    closed = closed_trades_table(positions, portfolio)
    signals_header = f"{pf_label(portfolio)} — Signals"

    if not df.empty:
        df = df[~df["account"].isin(NON_AI_ACCOUNTS)]
        # Filter to the portfolio selected in the Holdings sub-tab (effective
        # portfolio = explicit value, else the account default — same as pf_of).
        # Effective grouping key per signal — must match pf_of()/_pf_key() so the
        # namespaced umbrella sub-tabs (e.g. "ralliesarena|grok") filter correctly.
        eff_pf = df.apply(
            lambda r: _pf_key(
                r["account"],
                r["portfolio"] if isinstance(r["portfolio"], str) and r["portfolio"]
                else ACCOUNT_DEFAULT_PF.get(r["account"], "unknown")),
            axis=1)
        df = df[eff_pf == portfolio]
    if df.empty:
        return ([], "No signals yet.", cards, closed, signals_header)

    df = df.sort_values("timestamp", ascending=False)
    # Mark low/none-confidence rows (they are excluded from positions.json).
    df["confidence"] = df["confidence"].apply(
        lambda c: f"{c} ?" if isinstance(c, str) and c in ("low", "none") else c)

    def row_return(r):
        tks = r.get("tickers")
        if not (isinstance(tks, list) and len(tks) == 1):
            return ("", None)
        # DataFrame rows carry NaN where trades.json had null -- coerce back to
        # None so the guards and the estimated-entry (*) fallback behave.
        ep = r.get("entry_price")
        ep = None if (ep is None or ep != ep) else ep
        td = r.get("trade_date")
        td = td if isinstance(td, str) and td else None
        if not ep and r.get("signal_type") != "buy":
            return ("", None)
        at = r.get("asset_type")
        res = compute_return(tks[0], ep, td, r["timestamp"][:10],
                             at if isinstance(at, str) else "stock")
        if not res:
            return ("", None)
        return (f"{res['val']:+.1f}%" + ("*" if res["estimated"] else ""),
                res["val"])

    returns = df.apply(row_return, axis=1)
    df["return_pct"] = [s for s, _ in returns]
    df["return_val"] = [v for _, v in returns]

    summary = (f"{len(df)} signals across {df['account'].nunique()} accounts · "
               f"{len(positions)} reconciled positions · "
               f"last tweet {_iso_to_local(df['timestamp'].iloc[0], '%Y-%m-%d %H:%M %Z')}")
    cols = [c["id"] for c in TABLE_COLUMNS] + ["return_val"]
    return (df[cols].to_dict("records"), summary, cards, closed, signals_header)


@app.callback(
    Output("holdings-pie", "figure"),
    Output("position-detail", "children"),
    Input("interval", "n_intervals"),
    Input("selected-pf", "data"),
)
def refresh_pie(_n, portfolio):
    positions = load_positions()
    pfs = portfolios_in(ai_positions(positions))
    if portfolio not in pfs:             # stale selection -> first card
        portfolio = pfs[0]
    return (holdings_figure(positions, portfolio),
            position_detail_table(positions, portfolio))


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
    Output("kendrick-summaries", "children"),
    Input("interval", "n_intervals"),
    Input("influencer-subtabs", "value"),
)
def refresh_influencers(_n, account):
    # Ben Cowen / Jesse Olson / Ki Young Ju / Joao Wedson / DorkChicken /
    # Geoff Kendrick are analysis-only views, not traders: no header card /
    # positions / signals — just the cards.
    if account == "BenCowen":
        children = ([_current_view_banner(load_youtube_current_view())]
                    + youtube_section(load_youtube_summaries()))
        return "", [], None, None, children, [], [], [], [], []
    if account == "JesseOlson":
        children = ([_current_view_banner(load_jesse_olson_current_view())]
                    + youtube_section(load_jesse_olson_summaries(),
                                      empty_label="Jesse Olson"))
        return "", [], None, None, [], children, [], [], [], []
    if account == "KiYoungJu":
        children = ([_current_view_banner(load_ki_current_view())]
                    + twitter_section(load_twitter_summaries()))
        return "", [], None, None, [], [], children, [], [], []
    if account == "JoaoWedson":
        children = ([_current_view_banner(load_joao_current_view())]
                    + twitter_section(load_joao_summaries(), who="@joao_wedson"))
        return ("", [], None, None, [], [], [], children, [], [])
    if account == "DorkChicken":
        children = ([_current_view_banner(load_dorkchicken_current_view())]
                    + twitter_section(load_dorkchicken_summaries(), who="@DorkChicken"))
        return ("", [], None, None, [], [], [], [], children, [])
    if account == "GeoffKendrick":
        return ("", [], None, None, [], [], [], [], [],
                kendrick_forecast_section(load_kendrick_forecasts()))
    positions = load_positions()
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in influencer_positions(positions)
                 if p.get("status") == "open"})
    resolutions = influencer_resolutions(positions, account=account)
    return (influencer_header_card(account, resolutions=resolutions),
            influencer_signals_data(load_trades(), account=account),
            influencer_positions_table(resolutions),
            influencer_winrate_card(resolutions),
            [], [], [], [], [], [])


# --- background cache warmer -------------------------------------------------
# yfinance is the dominant page-load cost and sits on the request path (every
# callback fires on load, no prevent_initial_call): a cold load pays ~13s of
# current-price fetches + ~16s of daily-series fetches. Yahoo throttles per-IP,
# so batching cuts request count but NOT wall-clock. The real fix is to pre-warm
# the shared in-memory caches OFF the request path on a 30-min loop (< the 1h
# PRICE_TTL so they never lapse to cold) — user page loads then hit warm cache
# and render immediately instead of blocking on Yahoo.
WARM_INTERVAL_S = 1800


def _warm_all():
    positions = load_positions()
    ai = ai_positions(positions)
    try:
        warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                     for p in ai} | {"SPY"})
    except Exception:
        pass
    try:
        overview_figure(ai)          # warms _series_cache via _perf_rows
    except Exception:
        pass
    infl = influencer_positions(positions)
    try:
        warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                     for p in infl if p.get("status") == "open"})
    except Exception:
        pass
    try:
        influencer_resolutions(positions)   # warms _ohlc_cache + hist closes
    except Exception:
        pass


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
