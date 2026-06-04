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
from dash import Dash, dash_table, dcc, html, Input, Output
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
                    "deepseek": "DeepSeek", "chatgpt": "ChatGPT"}
# Merge null/"unknown" portfolio into the most likely one based on the posting
# account (grkportfolio->grok, theaiportfolios->claude, aifinancelabs->deepseek).
ACCOUNT_DEFAULT_PF = {"grkportfolio": "grok", "theaiportfolios": "claude",
                      "aifinancelabs": "deepseek"}
# Human trader / influencer accounts. Kept entirely separate from the AI
# portfolio views (own tab); excluded from the portfolio cards/charts.
INFLUENCER_ACCOUNTS = {"IncomeSharks", "CelalKucuker", "traderstewie"}
# Long-term conviction accounts (@moninvestor, @PelosiTracker): live in the
# Influencers tab in their own "Long-term Holdings" sub-tab, but are NOT mixed
# into the trade-call tables.
LONGTERM_ACCOUNTS = {"moninvestor", "PelosiTracker"}
# Everything that is not an AI portfolio bot (excluded from AI cards/charts).
NON_AI_ACCOUNTS = INFLUENCER_ACCOUNTS | LONGTERM_ACCOUNTS


def is_influencer(account):
    return account in INFLUENCER_ACCOUNTS


def is_longterm(account):
    return account in LONGTERM_ACCOUNTS


def ai_positions(positions):
    return [p for p in positions if p.get("account") not in NON_AI_ACCOUNTS]


def influencer_positions(positions):
    return [p for p in positions if is_influencer(p.get("account"))]


def longterm_positions(positions):
    return [p for p in positions if is_longterm(p.get("account"))]


def _yf_symbol(ticker, asset_type):
    """yfinance needs a -USD suffix for crypto (BTC -> BTC-USD)."""
    if asset_type == "crypto" and ticker and "-" not in ticker:
        return f"{ticker}-USD"
    return ticker


def pf_of(p):
    return p.get("portfolio") or ACCOUNT_DEFAULT_PF.get(p.get("account"), "unknown")


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


def _save_hist_persist():
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = PRICE_CACHE_FILE + ".tmp"
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
    if entry_price:
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
    {"name": "TWEET", "id": "text"},
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


def portfolio_cards(positions):
    open_pos = [p for p in positions if p.get("status") == "open"]
    by_pf = {}
    for p in open_pos:
        by_pf.setdefault(pf_of(p), []).append(p)

    cards = []
    for pf in sorted(by_pf):
        ps = by_pf[pf]
        rets, dates = [], []
        for p in ps:
            r = compute_return(p["ticker"], p.get("entry_price"),
                               p.get("trade_date"),
                               (p.get("opened_at") or "")[:10],
                               p.get("asset_type", "stock"))
            d = p.get("trade_date") or (p.get("opened_at") or "")[:10]
            if d:
                dates.append(d)
            if r:
                rets.append((p["ticker"], r["val"]))
        avg = round(sum(v for _, v in rets) / len(rets), 1) if rets else None
        best = max(rets, key=lambda x: x[1]) if rets else None
        worst = min(rets, key=lambda x: x[1]) if rets else None
        spy = spy_return_since(min(dates)) if dates else None
        label = PORTFOLIO_LABELS.get(pf, pf.title())

        cards.append(html.Div(
            style={
                "background": C["card"], "border": f"1px solid {C['border']}",
                "borderRadius": "8px", "padding": "14px 18px", "minWidth": "230px",
                "flex": "1",
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
    pfs = sorted({pf_of(p) for p in positions}) if positions else []
    return pfs or ["grok", "claude", "deepseek"]


# --- styled html tables (dark theme) ----------------------------------------
_TH = {"color": C["dim"], "fontFamily": MONO, "fontSize": "0.68rem",
       "textTransform": "uppercase", "letterSpacing": "0.04em",
       "textAlign": "left", "padding": "6px 10px",
       "borderBottom": f"1px solid {C['border']}"}
_TD = {"color": C["text"], "fontFamily": MONO, "fontSize": "0.78rem",
       "textAlign": "left", "padding": "5px 10px",
       "borderBottom": f"1px solid {C['border']}"}


def _table(headers, rows, empty="No data"):
    """rows: list of cells; each cell is str or (text, color)."""
    if not rows:
        return html.Div(empty, style={"color": C["dim"], "fontSize": "0.8rem",
                                      "padding": "8px 2px"})
    head = html.Thead(html.Tr([html.Th(h, style=_TH) for h in headers]))
    body = []
    for r in rows:
        tds = []
        for c in r:
            if isinstance(c, tuple):
                tds.append(html.Td(c[0], style={**_TD, "color": c[1]}))
            else:
                tds.append(html.Td(c, style=_TD))
        body.append(html.Tr(tds))
    return html.Table([head, html.Tbody(body)],
                      style={"borderCollapse": "collapse", "width": "100%",
                             "marginTop": "10px"})


def _money(v):
    return f"${v:,.2f}" if v else "—"


def position_detail_table(positions, portfolio):
    rows = []
    for p in positions:
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
    # sort by size desc (None last)
    rows.sort(key=lambda r: float(r[1][:-1]) if r[1].endswith("%") else -1,
              reverse=True)
    return _table(["Ticker", "Size %", "Trade Date", "Entry", "Current",
                   "Return %", "Days Held"], rows,
                  empty=f"No open positions for {PORTFOLIO_LABELS.get(portfolio, portfolio)}")


def closed_trades_table(positions, limit=5):
    closed = [p for p in positions if p.get("status") == "closed"]
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
            PORTFOLIO_LABELS.get(pf_of(p), pf_of(p)),
            opened_disp or "—",
            close_disp or "—",
            _money(entry),
            _money(exit_px),
            (_fmt_pct(ret), _color(ret)),
        ))
    return _table(["Ticker", "Portfolio", "Opened", "Closed", "Entry",
                   "Exit", "Return %"], rows, empty="No closed trades yet")


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
                 resolver.EXPIRED: ("expired", "dim")}


def influencer_resolutions(positions, account=None):
    """List of (position, resolution|None) for every open influencer call,
    resolved against its realized price path. If `account` is given, restrict
    to that one handle."""
    out = []
    for p in influencer_positions(positions):
        if p.get("status") != "open":
            continue
        if account and p.get("account") != account:
            continue
        atype = p.get("asset_type") or "unknown"
        sym = _yf_symbol(p["ticker"], atype)
        tdate = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
        ohlc = get_ohlc(sym, tdate) if tdate else None
        out.append((p, resolver.resolve_position(p, ohlc)))
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
                  f"{s['hit']} target / {s['stopped']} stopped) · "
                  f"{s['expired']} expired · {s['live']} live",
                  style={"color": C["dim"], "fontSize": "0.8rem"}),
    ])


# Per-influencer descriptor + accent color (left border) for the header card.
INFLUENCER_META = {
    "IncomeSharks": ("Stocks + crypto trade calls", C["blue"]),
    "CelalKucuker": ("Crypto-heavy trade calls", C["yellow"]),
    "traderstewie": ("US equity swing setups", C["green"]),
    "moninvestor":  ("Long-term conviction holds", C["purple"]),
    "PelosiTracker": ("Pelosi disclosed trades", C["orange"]),
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


def _influencer_returns(account, resolutions, positions, is_lt):
    """(ticker, return%) for each open call/holding of `account`."""
    rr = []
    if is_lt:
        for p in longterm_positions(positions or []):
            if p.get("status") != "open" or p.get("account") != account:
                continue
            atype = p.get("asset_type", "stock") or "stock"
            entry, _ = estimate_entry(p["ticker"], p.get("entry_price"),
                                      p.get("trade_date"),
                                      (p.get("opened_at") or "")[:10], atype)
            cur = get_price(_yf_symbol(p["ticker"], atype))
            rr.append((p["ticker"], round((cur - entry) / entry * 100, 1)
                       if (entry and cur) else None))
    else:
        for p, _res in (resolutions or []):
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


def influencer_header_card(account, resolutions=None, positions=None):
    """Per-influencer header card: @handle + descriptor + win rate (trade-call)
    or holdings count (long-term) + open count + best performer. A left accent
    border in the handle's color makes each visually distinct."""
    desc, accent = INFLUENCER_META.get(account, ("", C["blue"]))
    is_lt = account in LONGTERM_ACCOUNTS
    rr = _influencer_returns(account, resolutions, positions, is_lt)
    valid = [(t, r) for t, r in rr if r is not None]
    best = max(valid, key=lambda x: x[1]) if valid else None

    metrics = []
    if not is_lt:
        st = resolver.win_stats([r for _, r in (resolutions or [])])
        wr = st["win_rate"]
        wr_txt = "n/a" if wr is None else f"{wr:.0f}%"
        wr_color = C["dim"] if wr is None else (
            C["green"] if wr >= 50 else C["red"])
        metrics.append(_hdr_metric("win rate", wr_txt, wr_color,
                                   f"{st['decided']} resolved"))
        metrics.append(_hdr_metric("open calls", str(len(rr)), C["text"]))
    else:
        metrics.append(_hdr_metric("holdings", str(len(rr)), C["text"]))
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
    """Influencer calls (stocks AND crypto) with their resolution status."""
    rows = []
    for p, res in resolutions:
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
                  empty="No open influencer positions")


def longterm_holdings_table(positions, account=None):
    """Long-term conviction holdings (no TP/stop columns; shows the one-line
    thesis, sorted by Held Since desc — newest additions first). If `account`
    is given, restrict to that one handle; else all long-term accounts."""
    rows = []
    for p in longterm_positions(positions):
        if p.get("status") != "open":
            continue
        if account and p.get("account") != account:
            continue
        atype = p.get("asset_type", "stock") or "stock"
        sym = _yf_symbol(p["ticker"], atype)
        # "Held since" date: the stated actual trade_date when known (most
        # accurate for a long-term hold), else the first-disclosure date
        # (opened_at, in Budapest), flagged with * to mark it as estimated.
        # Days Held derives from the SAME value so the two columns agree.
        td = p.get("trade_date")
        since_date = td or _local_date(p.get("opened_at"))
        since_cell = (since_date + "*") if (since_date and not td) else \
            (since_date or "—")
        entry, est = estimate_entry(p["ticker"], p.get("entry_price"),
                                    p.get("trade_date"),
                                    (p.get("opened_at") or "")[:10], atype)
        cur = get_price(sym)
        ret = round((cur - entry) / entry * 100, 1) if (entry and cur) else None
        days = _days_held(since_date)
        rows.append((
            (
                (p["ticker"], C["blue"]),
                _money(entry) + ("*" if est and entry else ""),
                _money(cur),
                (_fmt_pct(ret), _color(ret)),
                since_cell,
                str(days) if days is not None else "—",
                p.get("holding_thesis") or "—",
            ),
            since_date or "",   # sort key: ISO date sorts chronologically
        ))
    # Newest entries first (ISO YYYY-MM-DD sorts lexicographically = by date);
    # rows missing a date ("") sink to the bottom.
    rows.sort(key=lambda r: r[1], reverse=True)
    label = f"@{account}" if account else "long-term"
    return _table(["Ticker", "Entry $", "Current $", "Return %", "Held Since",
                   "Days Held", "Thesis"], [r[0] for r in rows],
                  empty=f"No open {label} holdings")


def holdings_figure(positions, portfolio):
    """Pie of ALL open positions for the portfolio. Sized positions use their
    real weight + a color; unsized ones get a neutral-grey placeholder slice
    labeled 'TICKER ?' so the pie reflects the full book. The placeholder weight
    is the average disclosed weight (purely for visual sizing) and is NOT counted
    toward the disclosed-percentage label."""
    open_pos = [p for p in positions
                if pf_of(p) == portfolio and p["status"] == "open"]
    label = PORTFOLIO_LABELS.get(portfolio, portfolio)
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
        legend=dict(font=dict(color=C["text"])),
        margin=dict(l=20, r=20, t=50, b=20),
    )
    return fig


# Stable per-portfolio colors (+ S&P) shared by the timeseries and bar charts.
PF_COLORS = {"Grok": "#58a6ff", "Claude": "#bc8cff", "DeepSeek": "#3fb950",
             "ChatGPT": "#e3b341", "Unknown": "#8b949e", "S&P 500": "#f85149",
             "My Paper": "#f0f6fc"}   # bright near-white: the paper mirror line


def _dark_chart(fig, title, h=360):
    fig.update_layout(
        title=dict(text=title, font=dict(family=MONO, color=C["text"], size=14)),
        paper_bgcolor=C["card"], plot_bgcolor=C["card"], height=h,
        font=dict(family=MONO, color=C["text"], size=11),
        legend=dict(font=dict(color=C["text"]), title_text=""),
        margin=dict(l=50, r=20, t=50, b=40),
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
    chart. Returns (rows, start_date)."""
    entries = []   # (portfolio_label, yf_symbol, entry, open_date)
    for p in positions:
        if p.get("status") != "open":
            continue
        atype = p.get("asset_type", "stock")
        entry, _ = estimate_entry(p["ticker"], p.get("entry_price"),
                                  p.get("trade_date"),
                                  (p.get("opened_at") or "")[:10], atype)
        od = _norm_date(p.get("trade_date")) or _norm_date((p.get("opened_at") or "")[:10])
        if entry and od:
            entries.append((PORTFOLIO_LABELS.get(pf_of(p), pf_of(p).title()),
                            _yf_symbol(p["ticker"], atype), entry, od))
    if not entries:
        return [], None

    start = min(e[3] for e in entries)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    idx = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start=start, end=today)]
    series = {t: get_price_series(t, start) for _, t, _, _ in entries}

    rows = []
    for pf in sorted({e[0] for e in entries}):
        pf_entries = [e for e in entries if e[0] == pf]
        for d in idx:
            rets = []
            for _, t, entry, od in pf_entries:
                if d < od:
                    continue
                px_d = _price_asof(series.get(t), d)
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
      ::-webkit-scrollbar { width: 10px; height: 10px; }
      ::-webkit-scrollbar-track { background: #0d1117; }
      ::-webkit-scrollbar-thumb { background: #30363d; border-radius: 5px; }
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
              "padding": "6px 14px"}
_TAB_SELECTED = {"backgroundColor": C["card"], "color": C["text"],
                 "border": f"1px solid {C['border']}",
                 "borderTop": f"2px solid {C['blue']}", "fontFamily": MONO,
                 "padding": "6px 14px"}


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
                   "Unrealized P&L %"], rows, empty="No open positions.")


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
                  empty="No working orders (none waiting for the open).")


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
                  empty="No orders yet.")


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
        ps, rets, dates = by[pf], [], []
        for p in ps:
            r = compute_return(p["ticker"], p.get("entry_price"),
                               p.get("trade_date"), (p.get("opened_at") or "")[:10],
                               p.get("asset_type", "stock"))
            d = p.get("trade_date") or (p.get("opened_at") or "")[:10]
            if d:
                dates.append(d)
                all_dates.append(d)
            if r:
                rets.append((p["ticker"], r["val"]))
        avg = round(sum(v for _, v in rets) / len(rets), 1) if rets else None
        spy = spy_return_since(min(dates)) if dates else None
        delta = (round(avg - spy, 1)
                 if (avg is not None and spy is not None) else None)
        top = max(rets, key=lambda x: x[1])[0] if rets else None
        out.append({"label": PORTFOLIO_LABELS.get(pf, pf.title()), "ret": avg,
                    "spy": spy, "delta": delta, "n_open": len(ps), "top": top})
    overall_spy = spy_return_since(min(all_dates)) if all_dates else None
    return out, overall_spy


# --- redesign: components ---------------------------------------------------
def _sep():
    """Dim ' · ' separator between status-bar segments."""
    return html.Span("  ·  ", style={"color": C["border"]})


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
    ]
    br = _breaker_state()
    if br.get("halted"):
        segs += [_sep(), html.Span(
            f"■ HALTED: {br.get('halt_reason', 'execution')}",
            style={"color": C["red"], "fontWeight": "bold"})]
    return segs


def status_row_2():
    """Status bar row 2: container CPU/RAM + GetXAPI credits + API costs + prices.
    e.g. 'Dashboard: CPU 0.1% · RAM 278/384MB · GetXAPI: $9.83 credits · API
    Costs: today $0.80 / mo $4.13 / total $4.13 · Prices as of: 17:44 CEST
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

    d, m, t = _cost_sums()
    pa = _fetch_state["last"]
    prices_txt = (
        _to_local(datetime.fromtimestamp(pa, timezone.utc), "%H:%M %Z")
        + " (yfinance, 1h cache)" if pa else "not fetched yet")

    return [
        html.Span("Dashboard: ", style={"color": C["dim"]}),
        html.Span(f"CPU {cpu}", style={"color": C["text"]}),
        _sep(),
        html.Span(f"RAM {ram}", style={"color": C["text"]}),
        _sep(),
        html.Span("GetXAPI: ", style={"color": C["dim"]}),
        html.Span(bal_txt, style={"color": bal_color, "fontWeight": "bold"}),
        _sep(),
        html.Span("API Costs: ", style={"color": C["dim"]}),
        html.Span(f"today ${d:,.2f} / mo ${m:,.2f} / total ${t:,.2f}",
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


def hero_strip(store, kpis, overall_spy):
    """Always-visible KPI strip: paper account headline + each portfolio vs SPY
    + S&P. The 'are we winning' answer at a glance."""
    tiles = []
    acct = (store or {}).get("account") or {}
    nl = acct.get("net_liquidation")
    today, total = acct.get("daily_pnl"), acct.get("total_pnl")
    if (store or {}).get("offline"):
        tiles.append(_kpi_tile("Paper Account", "offline", C["red"],
                               "IB Gateway unreachable"))
    else:
        nl_txt = _money(nl) if nl else "—"
        sub = []
        if today is not None:
            sub.append(f"today {today:+,.0f}")
        if total is not None:
            sub.append(f"total {total:+,.0f}")
        tiles.append(_kpi_tile("Paper NetLiq", nl_txt,
                               _color(total if total is not None else 0),
                               " · ".join(sub) or None))
    for k in kpis:
        v = k["ret"]
        sub = (f"vs S&P {k['delta']:+.1f}" if k["delta"] is not None else
               (f"S&P {_fmt_pct(k['spy'])}" if k["spy"] is not None else None))
        tiles.append(_kpi_tile(k["label"], _fmt_pct(v), _color(v), sub))
    tiles.append(_kpi_tile("S&P 500", _fmt_pct(overall_spy), _color(overall_spy),
                           "vs year start (2026)"))
    return html.Div(tiles, style={
        "display": "flex", "flexWrap": "wrap", "gap": "10px",
        "marginTop": "10px"})


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
                  rows, empty="No open AI positions yet.")


def ibkr_exposure_card(orders, store):
    """Exposure gauge: open BUY notional vs the $10k cap, + cash/invested split."""
    open_buy = sum(o.get("quantity", 0) for o in orders
                   if o.get("action") == "BUY"
                   and o.get("status") in ("pending", "filled"))
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
    style={"backgroundColor": C["bg"], "color": C["text"], "fontFamily": MONO,
           "minHeight": "100vh", "padding": "20px 26px"},
    children=[
        dcc.Store(id="ibkr-store"),
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

        # Hero KPI strip (across every tab).
        html.Div(id="hero-strip"),
        html.Div(id="summary", style={"color": C["dim"], "fontSize": "0.76rem",
                                      "marginTop": "8px"}),

        # Top-level tabs: Overview (landing) · My Paper Account · AI Portfolios ·
        # Influencers.
        dcc.Tabs(id="main-tabs", value="overview", style={"marginTop": "12px"},
                 children=[
                     dcc.Tab(label="Overview", value="overview",
                             style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                     dcc.Tab(label="My Paper Account", value="ibkr",
                             style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                     dcc.Tab(label="AI Portfolios", value="ai",
                             style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                     dcc.Tab(label="Influencers", value="influencers",
                             style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                 ]),

        # Overview tab (landing): unified normalized chart + leaderboard.
        html.Div(id="overview-section", children=[
            html.Div("Normalized Performance — portfolios vs paper mirror vs S&P",
                     style=_SECTION_H),
            dcc.Graph(id="overview-chart", config={"displayModeBar": False}),
            html.Div("Leaderboard", style=_SECTION_H),
            html.Div(id="overview-leaderboard", style={"marginTop": "4px"}),
        ]),

        html.Div(id="ai-section", style={"display": "none"}, children=[

        html.Div("Portfolio Summary", style=_SECTION_H),
        html.Div(id="portfolio-summary",
                 style={"display": "flex", "flexWrap": "wrap", "gap": "14px",
                        "marginTop": "12px"}),

        # (Performance chart lives on the Overview tab — not duplicated here.)
        html.Div("Holdings", style=_SECTION_H),
        dcc.Tabs(id="portfolio-tabs", value=_portfolios[0],
                 children=[dcc.Tab(label=PORTFOLIO_LABELS.get(pf, pf.title()),
                                   value=pf, style=_TAB_STYLE,
                                   selected_style=_TAB_SELECTED)
                           for pf in _portfolios]),
        # Pie (40%, fixed) + position detail table (flexible) side by side in one
        # row. No flex-wrap, so they never stack vertically; minWidth:0 lets the
        # table shrink/overflow within its column instead of forcing a wrap.
        html.Div(style={"display": "flex", "flexDirection": "row",
                        "alignItems": "flex-start", "gap": "20px",
                        "marginTop": "6px"},
                 children=[
                     html.Div(style={"flex": "0 0 40%", "maxWidth": "40%"},
                              children=[
                                  dcc.Graph(id="holdings-pie",
                                            config={"displayModeBar": False},
                                            style={"width": "100%"}),
                              ]),
                     html.Div(id="position-detail",
                              style={"flex": "1", "minWidth": "0",
                                     "overflowX": "auto"}),
                 ]),

        html.Div("Recent Closed Trades", style=_SECTION_H),
        html.Div(id="closed-trades", style={"marginTop": "4px"}),

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
                {"if": {"column_id": "text"}, "minWidth": "320px",
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
            # One sub-tab per influencer handle. Trade-call accounts
            # (IncomeSharks/CelalKucuker/traderstewie) use the trade-call view
            # (winrate + positions + signals); conviction_long accounts
            # (moninvestor/PelosiTracker) use the long-term holdings view.
            dcc.Tabs(id="influencer-subtabs", value="IncomeSharks",
                     children=[
                         dcc.Tab(label="IncomeSharks", value="IncomeSharks",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         dcc.Tab(label="CelalKucuker", value="CelalKucuker",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         dcc.Tab(label="traderstewie", value="traderstewie",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         dcc.Tab(label="moninvestor", value="moninvestor",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                         dcc.Tab(label="PelosiTracker", value="PelosiTracker",
                                 style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                     ]),

            # Per-influencer header card (handle · win-rate/holdings · open ·
            # best performer), shown for whichever sub-tab is selected.
            html.Div(id="influencer-header"),

            # Trade-call view (IncomeSharks / CelalKucuker).
            html.Div(id="influencer-trade-view", children=[
            html.Div(id="influencer-pos-header", style=_SECTION_H),
            html.Div(id="influencer-winrate"),
            html.Div(id="influencer-positions", style={"marginTop": "4px"}),

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

            # Long-term holdings view (moninvestor). No TP/stop columns.
            html.Div(id="longterm-view", style={"display": "none"}, children=[
                html.Div(id="longterm-header", style=_SECTION_H),
                html.Div(id="longterm-holdings", style={"marginTop": "4px"}),
            ]),
        ]),

        # --- My Paper Account tab -- hidden until selected -------------------
        # Live reads from IB Gateway (127.0.0.1:4002) via the shared ibkr-store;
        # degrades to "Gateway offline" (ledger-backed tables still render).
        html.Div(id="ibkr-section", style={"display": "none"}, children=[
            html.Div(id="ibkr-halt"),
            html.Div("Account Summary", style=_SECTION_H),
            html.Div(id="ibkr-account"),
            html.Div("Exposure", style=_SECTION_H),
            html.Div(id="ibkr-exposure", style={"marginTop": "4px"}),
            html.Div("Open Positions", style=_SECTION_H),
            html.Div(id="ibkr-positions", style={"marginTop": "4px"}),
            html.Div("Pending / Working Orders", style=_SECTION_H),
            html.Div(id="ibkr-pending", style={"marginTop": "4px"}),
            html.Div("Order Outcomes", style=_SECTION_H),
            html.Div(id="ibkr-funnel", style={"marginTop": "4px"}),
            html.Div("Order History (last 20)", style=_SECTION_H),
            html.Div(id="ibkr-history", style={"marginTop": "4px"}),
        ]),
    ],
)


@app.callback(
    Output("overview-section", "style"),
    Output("ai-section", "style"),
    Output("influencer-section", "style"),
    Output("ibkr-section", "style"),
    Input("main-tabs", "value"),
)
def switch_main_tab(tab):
    show, hide = {"display": "block"}, {"display": "none"}
    order = {"overview": 0, "ai": 1, "influencers": 2, "ibkr": 3}
    i = order.get(tab, 0)
    return tuple(show if j == i else hide for j in range(4))


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
    # are ledger/file-backed and render even when the gateway is offline.
    if tab != "ibkr":
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
    """ONE shared IB fetch per 60s on the singleton connection — feeds the hero
    strip + status bar (always) and the My Paper Account tab. Records a daily
    NetLiq point for the equity curve (item 2)."""
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
    Output("hero-strip", "children"),
    Input("interval", "n_intervals"),
    Input("ibkr-store", "data"),
)
def refresh_topbar(_n, store):
    positions = ai_positions(load_positions())
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in positions} | {"SPY"})
    kpis, _ = portfolio_kpis(positions)
    # S&P tile is YEAR-TO-DATE (since 2026-01-01), labelled accordingly; the
    # per-portfolio 'vs S&P' deltas stay window-matched to each open date.
    ytd_spy = spy_return_since("2026-01-01")
    return hero_strip(store, kpis, ytd_spy)


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
    Output("longterm-view", "style"),
    Output("influencer-pos-header", "children"),
    Output("influencer-sig-header", "children"),
    Output("longterm-header", "children"),
    Input("influencer-subtabs", "value"),
)
def switch_influencer_subtab(account):
    show, hide = {"display": "block"}, {"display": "none"}
    if account in LONGTERM_ACCOUNTS:
        return (hide, show, "", "",
                _influencer_header(f"{account} — Long-term Holdings", account))
    return (show, hide,
            _influencer_header(f"{account} — Open Positions", account),
            _influencer_header(f"{account} — Signals", account),
            "")


@app.callback(
    Output("signals-table", "data"),
    Output("summary", "children"),
    Output("portfolio-summary", "children"),
    Output("closed-trades", "children"),
    Output("signals-header", "children"),
    Input("interval", "n_intervals"),
    Input("portfolio-tabs", "value"),
)
def refresh(_n, portfolio):
    df = load_trades()
    positions = ai_positions(load_positions())   # AI views exclude influencers
    # Batch live-price fetch for every symbol in play (one yf.download).
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in positions} | {"SPY"})
    cards = portfolio_cards(positions)   # uses the now-warm price cache
    closed = closed_trades_table(positions)
    signals_header = f"{PORTFOLIO_LABELS.get(portfolio, portfolio.title())} — Signals"

    if not df.empty:
        df = df[~df["account"].isin(NON_AI_ACCOUNTS)]
        # Filter to the portfolio selected in the Holdings sub-tab (effective
        # portfolio = explicit value, else the account default — same as pf_of).
        eff_pf = df["portfolio"].where(
            df["portfolio"].notna(),
            df["account"].map(ACCOUNT_DEFAULT_PF))
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
        if not r.get("entry_price") and r.get("signal_type") != "buy":
            return ("", None)
        at = r.get("asset_type")
        res = compute_return(tks[0], r.get("entry_price"),
                             r.get("trade_date"), r["timestamp"][:10],
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
    Input("portfolio-tabs", "value"),
)
def refresh_pie(_n, portfolio):
    positions = load_positions()
    return (holdings_figure(positions, portfolio),
            position_detail_table(positions, portfolio))


@app.callback(
    Output("influencer-header", "children"),
    Output("influencer-signals", "data"),
    Output("influencer-positions", "children"),
    Output("influencer-winrate", "children"),
    Output("longterm-holdings", "children"),
    Input("interval", "n_intervals"),
    Input("influencer-subtabs", "value"),
)
def refresh_influencers(_n, account):
    positions = load_positions()
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in influencer_positions(positions) + longterm_positions(positions)
                 if p.get("status") == "open"})
    # Long-term accounts (moninvestor/PelosiTracker) show only the holdings
    # view; the trade-call outputs are hidden.
    if account in LONGTERM_ACCOUNTS:
        return (influencer_header_card(account, positions=positions),
                [], None, None,
                longterm_holdings_table(positions, account=account))
    resolutions = influencer_resolutions(positions, account=account)
    return (influencer_header_card(account, resolutions=resolutions),
            influencer_signals_data(load_trades(), account=account),
            influencer_positions_table(resolutions),
            influencer_winrate_card(resolutions),
            None)


if __name__ == "__main__":
    # Bind 0.0.0.0 INSIDE the container so Docker's port proxy can reach it;
    # host exposure is set by the publish mapping in docker-compose.yml.
    app.run(host="0.0.0.0", port=PORT, debug=False)
