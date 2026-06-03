# Dashboard Redesign Proposal (proposal only — not implemented)

Scope: `dashboard.py` (Dash app, port 8051). Goal: surface the questions that
actually matter, demote plumbing, add a few high-value visualizations, and make
it usable on a phone. No code changes here — this is the design.

---

## 0. What the dashboard is *for* (the lens for every decision below)

The user's real questions, in priority order:
1. **Is my paper mirror making money, and how does it track vs the S&P?**
2. **Which AI portfolio (Grok / Claude / DeepSeek) is winning, vs SPY?**
3. **What is the system doing right now** (open positions, working/MOO orders,
   any halt)?
4. **Is anything broken** (data stale, gateway offline, circuit breaker tripped)?
5. Drill-down: individual signals, holdings, influencer calls.

The current layout answers #5 first and #1 last. The redesign inverts that.

---

## 1. Information hierarchy — the core problem

**Today:** the header is dominated by *plumbing* — GetXAPI credit balance, API
$ costs (today/mo/total), prices-as-of, monitor freshness. These render *before*
any performance number. The single most important number — the paper account's
P&L — lives in the **third tab**, which only loads when clicked. "Performance vs
S&P" is mid-scroll in tab 1. There is no single glance that says who's winning.

**Proposed hierarchy (top → bottom):**

1. **Status bar** (always visible, very top): small pills — `● LIVE` /
   `⚠ DATA STALE`, `● Gateway connected/offline`, `■ HALTED (reason)` if the
   circuit breaker tripped. One row, color-coded. This is the "is anything
   broken" answer, zero-click.
2. **Hero scoreboard** (always visible, under the status bar): the 4-6 numbers
   that matter — Paper NetLiq + total P&L + today P&L; each portfolio's return
   vs SPY; SPY itself. A horizontal KPI strip.
3. **Tabs** (drill-down): Overview · AI Portfolios · Influencers · My Paper
   Account.
4. **System/plumbing** (API costs, GetXAPI credits, last-run, prices-as-of)
   demoted to a collapsible "System status" disclosure or a thin footer.

---

## 2. Layout — tab by tab

### New tab: **Overview** (default landing)
A cross-cutting "scoreboard" so the answer to "who's winning" is the first thing
seen. Contents:
- The KPI strip (also pinned above tabs, but expanded here).
- ONE normalized comparison chart: Grok / Claude / DeepSeek / **My Paper Mirror**
  / SPY, all indexed to 100 at start. (Today the perf chart shows portfolios vs
  SPY but NOT the paper account — that's the gap.)
- A compact "leaderboard" table: portfolio · return · vs-SPY delta · # open · top
  holding · 7d sparkline.

### **AI Portfolios** tab (declutter the 5-section scroll)
Today it stacks: Portfolio Summary cards → Perf chart → Holdings (sub-tabs, pie +
detail) → Closed trades → All Signals (giant table). Proposed:
- **Responsive card grid** of portfolio cards (CSS `grid auto-fit minmax(260px)`).
  Richer cards: return vs SPY with delta, # positions, top holding, a tiny
  equity sparkline, and a "last activity" timestamp.
- **Click a card to cross-filter** the whole tab to that portfolio (replaces the
  separate Holdings sub-tabs — fewer clicks, clearer mental model).
- Below: Holdings donut + position table for the selected portfolio.
- "Closed trades" and "All signals" moved into a **collapsible "Detail" accordion**
  at the bottom (they're reference data, not headline). All-signals table gets a
  search box and sticky header.

### **Influencers** tab (mostly fine; small wins)
- Keep per-handle sub-tabs, but add a tiny **win-rate trend sparkline** next to
  the single win-rate number, and a one-line "X open calls · Y resolved · Z%
  hit" header per handle.
- Long single tables → same responsive row-card treatment on mobile.

### **My Paper Account** (rename "IBKR Paper Portfolio" — shorter, clearer)
Today: Account card → Open Positions → Pending → Order History. Proposed:
- **Top: account hero row** (NetLiq, today P&L, total P&L vs baseline) + an
  **exposure gauge**: a progress bar of open BUY notional vs the $10k cap, and
  cash-vs-invested donut. (Caps are core to this system; show them.)
- **Connection pill** + manual "Refresh now" button (instead of the whole card
  flipping to "Gateway offline"; the ledger-backed tables stay visible offline,
  which the code already supports).
- Open Positions, Pending/Working Orders (MOO), Order History — keep, but add an
  **order lifecycle funnel** (candidates → queued → filled / rejected / deferred)
  so the circuit-breaker/daily-cap behavior is visible.

---

## 3. Missing visualizations worth adding (ranked)

1. **Unified normalized equity curves** — all 3 portfolios + paper mirror + SPY
   on one chart. Highest value; directly answers the core question.
2. **Paper-mirror equity curve from real NetLiq history.** ⚠️ Data gap: no NetLiq
   time series is stored today. Needs a small daily snapshot (e.g. append
   NetLiq to `data/equity_curve.json` from the existing cron). Without it, the
   mirror's true performance can't be charted.
3. **Exposure / caps gauge** — open notional vs $10k, # positions, cash vs
   invested. Cheap, and makes the risk model legible.
4. **Mirror-fidelity panel** — given the known "one-directional accumulation /
   divergence" risk, show: positions the bot holds vs positions we mirrored, and
   % of bot trades captured. Surfaces drift before it gets bad.
5. **Closed-trade return distribution** (histogram) — replaces best/worst text
   with the actual spread; add median + win-rate.
6. **Signal-flow timeline** — buys/sells/position-recaps over time as a small
   stacked bar. Makes the spec's "sparse buy flow" visible.
7. **Circuit-breaker / halt indicator** — now that execution can halt (3 rejects /
   5-orders-day / 5% drawdown), the dashboard must show it. Currently invisible.
8. **Order lifecycle funnel** (see My Paper Account).

---

## 4. Mobile friendliness (currently desktop-only)

Problems: no `<meta viewport>`; 8-column DataTables (Order History, All Signals)
force horizontal scroll; cards use `minWidth` 230-320px; fixed `padding 20px
26px`; monospace everywhere; top tab bar with long labels.

Proposals:
- Add `<meta name="viewport" content="width=device-width, initial-scale=1">` to
  the custom `index_string` head.
- **Responsive tables → row-cards under a breakpoint:** below ~640px, render each
  table row as a stacked mini-card (label: value pairs) instead of columns. At
  minimum, mark secondary columns hide-on-mobile and keep 3-4 key columns.
- **CSS grid `auto-fit / minmax`** for all card rows so they reflow 1-col on
  phones, N-col on desktop (replaces flex+minWidth).
- **KPI strip** becomes a horizontally-scrollable chip row on narrow screens.
- **Bottom tab bar** on mobile (thumb-reachable); shorten labels (AI · Infl ·
  Account).
- Larger touch targets; collapse "System status" behind a disclosure by default.

---

## 5. Overall UX

- **Persistent status bar + KPI strip** across all tabs (context never lost).
- **Legend for markers:** `*` = estimated entry, `? ` = low/none confidence.
  Currently only explained in code comments.
- **Loading/empty states:** the IBKR tab connect takes seconds and the table 60s
  refresh rebuilds large tables — show spinners/skeletons, not blanks.
- **Timezone:** everything is Budapest local; label it once in the status bar.
- **Typography:** keep monospace for numbers/tickers/tables; use a sans-serif for
  labels and prose (monospace prose hurts scanability). Bump the dim text
  (`#8b949e`) contrast for small captions.
- **Unified alerts:** stale-data, gateway-offline, and halt all become pills in
  the one status bar (today they're scattered / missing).
- **Manual refresh** button + "updated Xs ago" so the user isn't guessing.
- **Reduce churn:** 60s full-rebuild of big tables is heavy; consider
  partial/targeted updates or a longer interval for reference tables.

---

## 6. Mockups

### Desktop — Overview (new landing)
```
┌───────────────────────────────────────────────────────────────────────┐
│ ● LIVE   ● Gateway connected   ⏱ updated 12s ago   🕒 times: CET        │  status bar
├───────────────────────────────────────────────────────────────────────┤
│ PAPER NetLiq        Grok          Claude        DeepSeek       S&P 500   │  KPI strip
│ $999,076  ▾ -0.1%   +4.2% (+1.1)  +6.8% (+3.7)  -1.3% (-4.4)  +3.1%      │
│ today +$12          vs SPY ▲       vs SPY ▲      vs SPY ▾                 │
├───────────────────────────────────────────────────────────────────────┤
│  [ Overview ]  AI Portfolios   Influencers   My Paper Account            │  tabs
├───────────────────────────────────────────────────────────────────────┤
│  Normalized performance (indexed = 100)                                  │
│  110┤                                          ╭─ Claude                 │
│  105┤                            ╭────╮   ╭────╯  ── SPY                  │
│  100┤────────────────────────────╯    ╰───╯       ╌╌ Paper mirror        │
│   95┤                                              ·· DeepSeek           │
│     └──────────────────────────────────────────────────────────         │
│  LEADERBOARD                                                             │
│  Portfolio  Return   vs SPY   #Open  Top holding   7d                    │
│  Claude     +6.8%    +3.7     12     NVDA          ▁▂▄▆▇                  │
│  Grok       +4.2%    +1.1      9     AVGO          ▂▃▄▄▅                  │
│  DeepSeek   -1.3%    -4.4      6     MU            ▇▆▄▃▂                  │
└───────────────────────────────────────────────────────────────────────┘
```

### Desktop — My Paper Account
```
┌───────────────────────────────────────────────────────────────────────┐
│ ACCOUNT  DUQ#####     ● connected     [ Refresh now ]                   │
│ NetLiq $999,076   today +$12   total P&L -$924 (vs baseline $1,000,000)  │
│ Exposure  ▓▓▓▓▓░░░░░  $3,150 / $10,000      Cash 96%  ◔  Invested 4%      │
├───────────────────────────────────────────────────────────────────────┤
│ OPEN POSITIONS                          PENDING / WORKING (MOO)          │
│ Tkr  Qty  Avg   Now    P&L%             Placed  Tkr  Act  Qty  IB status │
│ AVGO  2  455   461   +1.3% ▲            08:00   NVDA BUY  3   PreSubmit   │
│ MU    7  128   125   -2.3% ▾            (waiting for the open)           │
├───────────────────────────────────────────────────────────────────────┤
│ ORDER FUNNEL   candidates 4 → queued 3 → filled 2 · rejected 1 · defer 0 │
│ ORDER HISTORY (all statuses, color-coded) …                             │
└───────────────────────────────────────────────────────────────────────┘
```

### Mobile (~390px)
```
┌─────────────────────────┐
│ ● LIVE  ● GW  ⏱12s       │  status (scrolls horiz)
├─────────────────────────┤
│ PAPER $999,076  ▾-0.1%   │  KPI cards stack 1-col
│ today +$12               │
├─────────────────────────┤
│ Claude  +6.8%  vs SPY ▲  │
│ Grok    +4.2%  vs SPY ▲  │
│ DeepSeek -1.3% vs SPY ▾  │
├─────────────────────────┤
│ [perf chart, full width] │
├─────────────────────────┤
│ Position — AVGO          │  table row → card
│ qty 2 · avg 455 · 461    │
│ P&L +1.3% ▲              │
├─────────────────────────┤
│  AI  ·  Infl  ·  Acct    │  bottom tab bar
└─────────────────────────┘
```

---

## 7. Suggested sequencing (when we build it)

1. **Status bar + KPI strip + rename IBKR tab** — highest value/effort ratio;
   no new data needed.
2. **Daily NetLiq snapshot** (`data/equity_curve.json`) — unlocks the mirror
   equity curve. Small cron/monitor addition.
3. **Unified comparison chart + Overview tab.**
4. **Exposure gauge + order funnel + halt indicator** (My Paper Account).
5. **Responsive tables (row-cards) + viewport meta + grid layout** — mobile.
6. Distribution histogram, mirror-fidelity, signal timeline (nice-to-haves).

Open question for the user: keep 3 tabs + persistent hero, or add a 4th
"Overview" landing tab? (I lean: persistent hero strip + a new Overview tab.)
```
