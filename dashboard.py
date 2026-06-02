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

import json
import os
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yfinance as yf
from dash import Dash, dash_table, dcc, html, Input, Output

import resolver

HOME = "/home/fbazsa/pilot_trader"
TRADES_FILE = "/home/fbazsa/pilot_trader/trades.json"
POSITIONS_FILE = "/home/fbazsa/pilot_trader/positions.json"
STATE_FILE = "/home/fbazsa/pilot_trader/.monitor_state.json"
ENV_FILE = "/home/fbazsa/pilot_trader/.env"
STALE_HOURS = 8           # cron runs every 4h; >8h means a run was missed
REFRESH_MS = 60_000
PORT = 8051

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


def credits_cards():
    """Small header info card(s) for API credit balances. GetXAPI only —
    Anthropic exposes no remaining-balance endpoint. Balance turns red below
    CREDITS_LOW_USD; a failed fetch shows the last value with an error note."""
    c = get_getxapi_credits()
    bal = c["balance"]
    if bal is None:
        val_txt, val_color = "n/a", C["dim"]
    else:
        val_txt = f"${bal:,.2f}"
        val_color = C["red"] if bal < CREDITS_LOW_USD else C["green"]
    if c["fetched_at"]:
        checked = datetime.fromtimestamp(
            c["fetched_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    else:
        checked = "never"
    note = "" if c["ok"] else "  (fetch error — last known)"

    card = html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderRadius": "6px", "padding": "6px 12px", "display": "inline-block"},
        children=[
            html.Span("GetXAPI Credits: ", style={"color": C["dim"]}),
            html.Span(val_txt, style={"color": val_color, "fontWeight": "bold"}),
            html.Span(note, style={"color": C["red"], "fontSize": "0.7rem"}),
        ])
    return html.Div(style={
        "display": "flex", "flexWrap": "wrap", "gap": "10px",
        "alignItems": "center", "marginTop": "8px", "fontSize": "0.8rem"},
        children=[
            card,
            html.Span(f"checked {checked}",
                      style={"color": C["dim"], "fontSize": "0.7rem"}),
        ])


def api_costs_card():
    """Header card for Anthropic (Haiku+Sonnet) spend: today / this month /
    all-time, summed from data/cost_log.json (written per run by monitor.py).
    Shows '$0.00' when the log is absent/empty."""
    try:
        with open(COST_LOG_FILE) as f:
            log = json.load(f)
        if not isinstance(log, list):
            log = []
    except (json.JSONDecodeError, OSError):
        log = []

    now = datetime.now(timezone.utc)
    today, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    d_sum = m_sum = t_sum = 0.0
    for r in log:
        usd = r.get("total_usd") or 0.0
        ts = r.get("timestamp") or ""
        t_sum += usd
        if ts[:7] == month:
            m_sum += usd
        if ts[:10] == today:
            d_sum += usd

    def part(label, val):
        return html.Span([
            html.Span(f"{label} ", style={"color": C["dim"]}),
            html.Span(f"${val:,.2f}",
                      style={"color": C["text"], "fontWeight": "bold"}),
        ], style={"marginRight": "12px"})

    card = html.Div(style={
        "background": C["card"], "border": f"1px solid {C['border']}",
        "borderRadius": "6px", "padding": "6px 12px", "display": "inline-block"},
        children=[
            html.Span("API Costs  ", style={"color": C["dim"]}),
            part("today", d_sum), part("mo", m_sum), part("total", t_sum),
        ])
    note = "" if log else "  (no runs logged yet)"
    return html.Div(style={
        "display": "flex", "flexWrap": "wrap", "gap": "10px",
        "alignItems": "center", "marginTop": "6px", "fontSize": "0.8rem"},
        children=[
            card,
            html.Span(f"{len(log)} runs{note}",
                      style={"color": C["dim"], "fontSize": "0.7rem"}),
        ])
PORTFOLIO_LABELS = {"grok": "Grok", "claude": "Claude",
                    "deepseek": "DeepSeek", "chatgpt": "ChatGPT"}
# Merge null/"unknown" portfolio into the most likely one based on the posting
# account (grkportfolio->grok, theaiportfolios->claude, aifinancelabs->deepseek).
ACCOUNT_DEFAULT_PF = {"grkportfolio": "grok", "theaiportfolios": "claude",
                      "aifinancelabs": "deepseek"}
# Human trader / influencer accounts. Kept entirely separate from the AI
# portfolio views (own tab); excluded from the portfolio cards/charts.
INFLUENCER_ACCOUNTS = {"IncomeSharks", "CelalKucuker"}
# Long-term conviction accounts (@moninvestor): live in the Influencers tab in
# their own "Long-term Holdings" section, but are NOT mixed into the IncomeSharks
# trade-call tables.
LONGTERM_ACCOUNTS = {"moninvestor"}
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
    df["date"] = df["timestamp"].str.slice(0, 10)
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
    {"name": "ACCOUNT", "id": "account"},
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
            tdate or "—",
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
            opened or "—",
            close_date or "—",
            _money(entry),
            _money(exit_px),
            (_fmt_pct(ret), _color(ret)),
        ))
    return _table(["Ticker", "Portfolio", "Opened", "Closed", "Entry",
                   "Exit", "Return %"], rows, empty="No closed trades yet")


# --- influencer (IncomeSharks) views ----------------------------------------
def influencer_signals_data(df):
    """Rows for the influencer signals DataTable (most recent first)."""
    if df.empty:
        return []
    sub = df[df["account"].isin(INFLUENCER_ACCOUNTS)].copy()
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
            "account": r.get("account") or "—",
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


def influencer_resolutions(positions):
    """List of (position, resolution|None) for every open influencer call,
    resolved against its realized price path."""
    out = []
    for p in influencer_positions(positions):
        if p.get("status") != "open":
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
        tdate = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
        if res:
            label, ckey = _STATUS_LABEL[res["status"]]
            status_cell = (label, C[ckey])
        else:
            status_cell = ("live", C["blue"])
        rows.append((
            (p["ticker"], C["blue"]),
            p.get("account") or "—",
            atype,
            tdate or "—",
            _money(entry) + ("*" if est and entry else ""),
            _money(cur),
            (_fmt_pct(ret), _color(ret)),
            _money(p.get("stop_loss")),
            _money(p.get("target")),
            status_cell,
        ))
    rows.sort(key=lambda r: r[3], reverse=True)
    return _table(["Ticker", "Account", "Asset", "Trade Date", "Entry", "Current",
                   "Return %", "Stop", "Target", "Status"], rows,
                  empty="No open influencer positions")


def longterm_holdings_table(positions):
    """@moninvestor long-term conviction holdings. No TP/stop columns; shows the
    one-line thesis and sorts by return % descending."""
    rows = []
    for p in longterm_positions(positions):
        if p.get("status") != "open":
            continue
        atype = p.get("asset_type", "stock") or "stock"
        sym = _yf_symbol(p["ticker"], atype)
        tdate = p.get("trade_date") or (p.get("opened_at") or "")[:10] or None
        entry, est = estimate_entry(p["ticker"], p.get("entry_price"),
                                    p.get("trade_date"),
                                    (p.get("opened_at") or "")[:10], atype)
        cur = get_price(sym)
        ret = round((cur - entry) / entry * 100, 1) if (entry and cur) else None
        days = _days_held(tdate)
        rows.append((
            (
                (p["ticker"], C["blue"]),
                _money(entry) + ("*" if est and entry else ""),
                _money(cur),
                (_fmt_pct(ret), _color(ret)),
                str(days) if days is not None else "—",
                p.get("holding_thesis") or "—",
            ),
            ret if ret is not None else float("-inf"),   # sort key
        ))
    rows.sort(key=lambda r: r[1], reverse=True)
    return _table(["Ticker", "Entry $", "Current $", "Return %", "Days Held",
                   "Thesis"], [r[0] for r in rows],
                  empty="No open @moninvestor holdings")


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
             "ChatGPT": "#e3b341", "Unknown": "#8b949e", "S&P 500": "#f85149"}


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


def performance_figure(positions):
    """Cumulative equal-weight return % per portfolio vs S&P 500, from the first
    open date to today, using daily price history."""
    entries = []   # (portfolio_label, yf_symbol, entry, open_date)
    for p in positions:
        if p.get("status") != "open":
            continue
        atype = p.get("asset_type", "stock")
        entry, _ = estimate_entry(p["ticker"], p.get("entry_price"),
                                  p.get("trade_date"),
                                  (p.get("opened_at") or "")[:10], atype)
        od = p.get("trade_date") or (p.get("opened_at") or "")[:10]
        if entry and od:
            entries.append((PORTFOLIO_LABELS.get(pf_of(p), pf_of(p).title()),
                            _yf_symbol(p["ticker"], atype), entry, od))
    if not entries:
        return _dark_chart(px.line(), "Performance vs S&P 500 — no dated positions")

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
    if not rows:
        return _dark_chart(px.line(), "Performance vs S&P 500 — no price data")
    df = pd.DataFrame(rows)
    fig = px.line(df, x="date", y="return", color="portfolio",
                  color_discrete_map=PF_COLORS)
    fig.update_traces(selector=dict(name="S&P 500"), line=dict(dash="dash"))
    return _dark_chart(fig, "Cumulative return % vs S&P 500 (equal-weight, est.)")


# initial scaffolding (AI portfolios only — influencers live in their own tab)
_portfolios = portfolios_in(ai_positions(load_positions()))

app = Dash(__name__)
app.title = "Pilot Trader — Signal Monitor"

# Dark theme: Dash's default <body> is white, and the DataTable's filter inputs
# render light-on-light, so inject CSS into <head>.
app.index_string = """<!DOCTYPE html>
<html>
  <head>
    {%metas%}<title>{%title%}</title>{%favicon%}{%css%}
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

app.layout = html.Div(
    style={"backgroundColor": C["bg"], "color": C["text"], "fontFamily": MONO,
           "minHeight": "100vh", "padding": "20px 26px"},
    children=[
        html.Div([
            html.H2("Pilot Trader — Signal Monitor",
                    style={"margin": 0, "color": C["text"], "fontFamily": MONO,
                           "fontSize": "1.35rem"}),
            html.Div(id="summary", style={"color": C["dim"], "fontSize": "0.8rem",
                                          "marginTop": "5px"}),
            html.Div(id="prices-asof", style={"color": C["dim"],
                                              "fontSize": "0.72rem", "marginTop": "2px"}),
            html.Div(id="freshness"),
            html.Div(id="api-credits", children=credits_cards()),
            html.Div(id="api-costs", children=api_costs_card()),
        ]),
        dcc.Interval(id="interval", interval=REFRESH_MS, n_intervals=0),
        # Separate, slow interval so the credits API is polled ~hourly, not 60s.
        dcc.Interval(id="credits-interval", interval=CREDITS_REFRESH_MS,
                     n_intervals=0),

        # Top-level split: AI Portfolios vs Influencers (two distinct worlds).
        dcc.Tabs(id="main-tabs", value="ai", style={"marginTop": "14px"},
                 children=[
                     dcc.Tab(label="AI Portfolios", value="ai",
                             style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                     dcc.Tab(label="Influencers", value="influencers",
                             style=_TAB_STYLE, selected_style=_TAB_SELECTED),
                 ]),

        html.Div(id="ai-section", children=[

        html.Div("Portfolio Summary", style=_SECTION_H),
        html.Div(id="portfolio-summary",
                 style={"display": "flex", "flexWrap": "wrap", "gap": "14px",
                        "marginTop": "12px"}),

        html.Div("Performance vs S&P 500", style=_SECTION_H),
        dcc.Graph(id="perf-timeseries", config={"displayModeBar": False}),

        html.Div("Holdings", style=_SECTION_H),
        dcc.Tabs(id="portfolio-tabs", value=_portfolios[0],
                 children=[dcc.Tab(label=PORTFOLIO_LABELS.get(pf, pf.title()),
                                   value=pf, style=_TAB_STYLE,
                                   selected_style=_TAB_SELECTED)
                           for pf in _portfolios]),
        dcc.Graph(id="holdings-pie", config={"displayModeBar": False}),
        html.Div(id="position-detail", style={"marginTop": "6px"}),

        html.Div("Recent Closed Trades", style=_SECTION_H),
        html.Div(id="closed-trades", style={"marginTop": "4px"}),

        html.Div("All Signals", style=_SECTION_H),
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

        # --- Influencers tab (IncomeSharks + CelalKucuker) -- hidden until selected ---
        html.Div(id="influencer-section", style={"display": "none"}, children=[
            html.Div("Influencers — Open Positions", style=_SECTION_H),
            html.Div(id="influencer-winrate"),
            html.Div(id="influencer-positions", style={"marginTop": "4px"}),

            html.Div("Influencers — Signals", style=_SECTION_H),
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

            # --- @moninvestor long-term conviction holdings ------------------
            html.Div("moninvestor — Long-term Holdings", style=_SECTION_H),
            html.Div(id="longterm-holdings", style={"marginTop": "4px"}),
        ]),
    ],
)


@app.callback(
    Output("ai-section", "style"),
    Output("influencer-section", "style"),
    Input("main-tabs", "value"),
)
def switch_main_tab(tab):
    show, hide = {"display": "block"}, {"display": "none"}
    if tab == "influencers":
        return hide, show
    return show, hide


@app.callback(
    Output("api-credits", "children"),
    Output("api-costs", "children"),
    Input("credits-interval", "n_intervals"),
)
def refresh_credits(_n):
    return credits_cards(), api_costs_card()


@app.callback(
    Output("signals-table", "data"),
    Output("summary", "children"),
    Output("portfolio-summary", "children"),
    Output("prices-asof", "children"),
    Output("closed-trades", "children"),
    Output("freshness", "children"),
    Input("interval", "n_intervals"),
)
def refresh(_n):
    df = load_trades()
    positions = ai_positions(load_positions())   # AI views exclude influencers
    # Batch live-price fetch for every symbol in play (one yf.download).
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in positions} | {"SPY"})
    cards = portfolio_cards(positions)   # uses the now-warm price cache
    closed = closed_trades_table(positions)

    if not df.empty:
        df = df[~df["account"].isin(NON_AI_ACCOUNTS)]
    if df.empty:
        return ([], "No signals yet.", cards, _asof_text(), closed,
                freshness_banner())

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
               f"last tweet {df['timestamp'].iloc[0][:16]}Z")
    cols = [c["id"] for c in TABLE_COLUMNS] + ["return_val"]
    return (df[cols].to_dict("records"), summary, cards, _asof_text(), closed,
            freshness_banner())


def freshness_banner():
    """Red 'DATA STALE' banner when monitor.py's last successful run is older
    than STALE_HOURS; otherwise a dim last-update line."""
    last = None
    try:
        with open(STATE_FILE) as f:
            last = json.load(f).get("_last_run")
    except (json.JSONDecodeError, OSError):
        last = None
    if not last:
        return html.Span("")
    try:
        dt = datetime.fromisoformat(last)
        hours = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
    except ValueError:
        return html.Span("")
    when = dt.strftime("%Y-%m-%d %H:%M UTC")
    if hours > STALE_HOURS:
        return html.Div(
            f"⚠ DATA STALE — last update {hours:.0f}h ago ({when})",
            style={"background": C["sell_bg"], "color": C["red"],
                   "border": f"1px solid {C['red']}", "borderRadius": "6px",
                   "padding": "6px 12px", "marginTop": "8px",
                   "fontWeight": "bold", "fontSize": "0.82rem",
                   "display": "inline-block"})
    return html.Div(f"monitor last run {when} ({hours:.1f}h ago)",
                    style={"color": C["dim"], "fontSize": "0.72rem",
                           "marginTop": "4px"})


def _asof_text():
    ts = _fetch_state["last"]
    if not ts:
        return "Prices as of: (not fetched yet)"
    when = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"Prices as of: {when} · yfinance, cached up to 1h"


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
    Output("perf-timeseries", "figure"),
    Input("interval", "n_intervals"),
)
def refresh_charts(_n):
    return performance_figure(ai_positions(load_positions()))


@app.callback(
    Output("influencer-signals", "data"),
    Output("influencer-positions", "children"),
    Output("influencer-winrate", "children"),
    Output("longterm-holdings", "children"),
    Input("interval", "n_intervals"),
)
def refresh_influencers(_n):
    positions = load_positions()
    warm_prices({_yf_symbol(p["ticker"], p.get("asset_type", "stock"))
                 for p in influencer_positions(positions) + longterm_positions(positions)
                 if p.get("status") == "open"})
    resolutions = influencer_resolutions(positions)
    return (influencer_signals_data(load_trades()),
            influencer_positions_table(resolutions),
            influencer_winrate_card(resolutions),
            longterm_holdings_table(positions))


if __name__ == "__main__":
    # Bind 0.0.0.0 INSIDE the container so Docker's port proxy can reach it;
    # host exposure is set by the publish mapping in docker-compose.yml.
    app.run(host="0.0.0.0", port=PORT, debug=False)
