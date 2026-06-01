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
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import yfinance as yf
from dash import Dash, dash_table, dcc, html, Input, Output

TRADES_FILE = "/home/fbazsa/pilot_trader/trades.json"
POSITIONS_FILE = "/home/fbazsa/pilot_trader/positions.json"
REFRESH_MS = 60_000
PORT = 8051
PORTFOLIO_LABELS = {"grok": "Grok", "claude": "Claude",
                    "deepseek": "DeepSeek", "chatgpt": "ChatGPT"}
# Merge null/"unknown" portfolio into the most likely one based on the posting
# account (grkportfolio->grok, theaiportfolios->claude, aifinancelabs->deepseek).
ACCOUNT_DEFAULT_PF = {"grkportfolio": "grok", "theaiportfolios": "claude",
                      "aifinancelabs": "deepseek"}


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
    now = time.time()
    key = (ticker, date_str)
    hit = _hist_cache.get(key)
    if hit and (hit[0] is not None or now - hit[1] < PRICE_TTL):
        return hit[0]
    price = _fetch_hist_close(ticker, date_str)
    _hist_cache[key] = (price, now)
    return price


# --- return computation ------------------------------------------------------
def estimate_entry(ticker, entry_price, trade_date, fallback_date):
    if entry_price:
        return entry_price, False
    date = trade_date or (fallback_date or "")[:10]
    return get_hist_close(ticker, date), True


def compute_return(ticker, entry_price, trade_date, fallback_date):
    entry, estimated = estimate_entry(ticker, entry_price, trade_date, fallback_date)
    if not entry:
        return None
    cur = get_price(ticker)
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
                               (p.get("opened_at") or "")[:10])
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
        entry, est = estimate_entry(p["ticker"], p.get("entry_price"),
                                    p.get("trade_date"),
                                    (p.get("opened_at") or "")[:10])
        cur = get_price(p["ticker"])
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
        entry = p.get("entry_price")
        if not entry and opened:
            entry, _ = estimate_entry(p["ticker"], None, p.get("trade_date"), opened)
        # exit price = close on the close date
        exit_px = get_hist_close(p["ticker"], close_date) if close_date else None
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
    entries = []   # (portfolio_label, ticker, entry, open_date)
    for p in positions:
        if p.get("status") != "open":
            continue
        entry, _ = estimate_entry(p["ticker"], p.get("entry_price"),
                                  p.get("trade_date"), (p.get("opened_at") or "")[:10])
        od = p.get("trade_date") or (p.get("opened_at") or "")[:10]
        if entry and od:
            entries.append((PORTFOLIO_LABELS.get(pf_of(p), pf_of(p).title()),
                            p["ticker"], entry, od))
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


# initial scaffolding
_portfolios = portfolios_in(load_positions())

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
        ]),
        dcc.Interval(id="interval", interval=REFRESH_MS, n_intervals=0),

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
            ],
        ),
    ],
)


@app.callback(
    Output("signals-table", "data"),
    Output("summary", "children"),
    Output("portfolio-summary", "children"),
    Output("prices-asof", "children"),
    Output("closed-trades", "children"),
    Input("interval", "n_intervals"),
)
def refresh(_n):
    df = load_trades()
    positions = load_positions()
    cards = portfolio_cards(positions)   # also warms the price cache
    closed = closed_trades_table(positions)

    if df.empty:
        return [], "No signals yet.", cards, _asof_text(), closed

    df = df.sort_values("timestamp", ascending=False)

    def row_return(r):
        tks = r.get("tickers")
        if not (isinstance(tks, list) and len(tks) == 1):
            return ("", None)
        if not r.get("entry_price") and r.get("signal_type") != "buy":
            return ("", None)
        res = compute_return(tks[0], r.get("entry_price"),
                             r.get("trade_date"), r["timestamp"][:10])
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
    return (df[cols].to_dict("records"), summary, cards, _asof_text(), closed)


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
    return performance_figure(load_positions())


if __name__ == "__main__":
    # Bind 0.0.0.0 INSIDE the container so Docker's port proxy can reach it;
    # host exposure is set by the publish mapping in docker-compose.yml.
    app.run(host="0.0.0.0", port=PORT, debug=False)
