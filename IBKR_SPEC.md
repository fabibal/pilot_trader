# IBKR Mirroring Spec — pilot_trader

Status: **DESIGN / logic-only.** No IBKR connection exists yet. This document is
the contract that `order_manager.py` (the idempotent order layer) implements and
tests against. Live execution (IB Gateway on port 4002) is a later, separate step.

Grounded in `positions.json` / `trades.json` as of 2026-06-02.

## 1. What we mirror
- **Portfolio: Grok only.** Source of truth = signals whose effective portfolio
  resolves to `grok` (see `pf_of()` / `ACCOUNT_DEFAULT_PF`).
- **Consolidate across accounts.** ⚠️ A Grok holding can be disclosed by BOTH
  `@grkportfolio` AND `@aifinancelabs` (which posts Grok updates). Today AVGO, MU,
  and SOC each appear under multiple `(account, portfolio, ticker)` keys. The
  mirror treats `(grok, ticker)` as ONE logical position; the order layer keys on
  `ticker`, not on the account that posted it.

## 2. Position sizing
- **Equal-weight across open mirrored positions**, hard-capped per name.
- **Max 10% per position** (`MAX_POSITION_PCT = 0.10`).
- Target notional per buy = `min(MAX_TOTAL_EXPOSURE / n_open_positions,
  MAX_TOTAL_EXPOSURE * MAX_POSITION_PCT)` where `n_open_positions` counts the
  position being opened. At <=10 names the 10% cap binds ($1,000); above 10 the
  equal-weight slice binds.
- **Quantity in the logic layer is USD notional**, not shares. Share conversion
  (notional / live ask) happens at the IBKR-connection layer, not here.

## 3. Signal thresholds (what triggers an order)
- **BUY orders: only `signal_type == "buy"` with `confidence in {high, medium}`.**
- `position` disclosures (current-holding recaps) do **NOT** trigger orders — they
  are state, not a fresh transaction. ⚠️ Grok posts mostly `position` recaps
  (17 of 19 recent signals) and only 2 actual `buy`s, so **expect sparse buy flow**
  under this rule. This is intended (we mirror new decisions, not recaps).
- `low` / `none` confidence never trigger (mirrors reconcile's confidence gate).

## 4. Sell rules
- **Full sell** (`signal_type == "sell"`, `sell_kind == "full"`) → close the
  position (sell 100% of current mirrored notional).
- **Partial sell / trim** (`sell_kind == "partial"`) → **reduce by 50%** of current
  mirrored notional.
- A sell for a ticker we do not currently hold in the ledger is a no-op.

## 5. Risk caps
- **Max total exposure: $10,000** (`MAX_TOTAL_EXPOSURE`). Sum of open (pending +
  filled) BUY notional may not exceed this.
- **Max position: $1,000** (10% of total).
- **Max 1 order per ticker per UTC day** (`MAX_ORDERS_PER_TICKER_PER_DAY = 1`).
- All caps enforced in `risk_check()` before an order is queued.

## 6. Asset filter
- **Stocks only.** `asset_type == "stock"` required; crypto (BTC/ETH/SOL, or any
  `*-USD` symbol) is rejected. Grok is currently all-stock, so this is non-binding
  today but enforced for safety.

## 7. Entry / order timing
- **Market order at the next regular-session open** after a signal is detected.
  We do NOT use the bot's stated/estimated entry price (only 4 of 17 open Grok
  positions even have a real entry — the rest are estimates). Mirroring is
  directional, executed at our own fill, not theirs.

## 8. Existing positions
- **SKIP all pre-existing Grok positions.** The mirror acts only on **new signals
  detected from 2026-06-02 onward**. We do not back-fill or buy into the current
  17 open names. The ledger starts empty; the first order is the first qualifying
  new buy.

## 9. Idempotency
- Each tweet/signal = at most one order. `check_already_actioned(signal_id)`
  blocks re-queuing the same signal across runs (the cron fires every 4h and the
  same holding is disclosed repeatedly). Backed by `data/orders.json`.

## Open questions / not yet decided
- Rebalancing of existing mirrored names as `n_open` grows (currently we size at
  entry and do not rebalance).
- How to reconcile a mirrored fill against later partial-sell percentages when our
  fill size differs from the bot's disclosed weight.
- Handling of after-hours signal detection vs next-open execution windows / holidays.
