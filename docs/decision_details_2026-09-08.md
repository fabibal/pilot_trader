# Decision details and feature retirement — 2026-09-08

**Subsequent implementation:** see [recovery and cycle changes](recovery_cycles_2026-09-08.md). The following is the pre-fix decision record.

This follow-up inspected actual source, trades, positions, summary ledgers and cron logs. The three decision items below were not changed. Historical audit findings are not evidence by themselves.

## 1. Earlier cycles disappear from position statistics

Code: `reconcile.py:124` keys positions by account/portfolio/ticker, not trading cycle. At lines 164–184, a buy after closure reuses that dictionary and resets the old cycle's entry/stop/target/open/close fields. `dashboard.py:610` resolves the resulting positions, rather than reconstructing each historical cycle from signals.

Actual traderstewie AEHR records:

| UTC date | Tweet ID | Stored event |
|---|---|---|
| April 29 | 2049634587173863467 | buy; stop 74.71; target 100 |
| May 13 | 2054616414900932926 | position update; target 122.5 |
| August 3 | 2084103071026778231 | full sell; analyst recap says week 30 stopped out at -8.16% |
| September 1 | 2094822672777801772 | new buy; target 90 |
| September 4 | 2095930046540316855 | partial sell |

Evidence: `trades.json:25151`, `trades.json:5835`, `trades.json:441`; current `positions.json:9378`. The current single AEHR row is open from September 1 with target 90 and no old stop or closing date. Signals and raw trade records survive, but the earlier closed cycle is not a separate input to position-based statistics.

This demonstrates omitted cycle state, not a verified -8.16% investment return: the percentage is the analyst's retrospective claim, and the existing grouping may already conflate distinct earlier trades. A corrected win rate cannot be responsibly calculated without reconstructing and validating cycles. Bias can go either direction: both old wins and losses can disappear.

Proposed fix, not implemented: keep a current-position view but introduce immutable cycle IDs and a cycle ledger; closing finalizes a cycle, reopening creates another. Compute statistics from cycles. Replay stored events into a separate candidate ledger for review before migration. Decision: prioritize historically complete performance statistics now, and choose how to handle retrospective recaps, overlapping setups and ambiguous dates.

## 2. Short entries are conflated with exits

`monitor.py:148` explicitly maps short calls and exits to the same `sell` action. `reconcile.py:194` only handles sells when an existing position is open; a sell can reduce/close that holding but cannot open a short. `resolver.py` has price-geometry direction inference, which cannot recover a short cycle that reconciliation never created.

Actual example: `trades.json:25788`, tweet 2032273256137548277, March 13 UTC, traderstewie AXTI. Stored text describes a **potential** short setup, stop $52, targets $37–35. Extraction stores `signal_type=sell`, `sell_kind=null`, stop 52, target 37. There is no preceding AXTI holding to exit. The later April 7 buy (2041634386362429885, `trades.json:25672`) becomes the current open position (`positions.json:9605`), target 55. March's short idea has no separate resolved cycle.

Impact: short ideas can be omitted; a short call made while a long is open can incorrectly close that long. This specific tweet is conditional, so it does NOT prove an actual filled short or a missed realized profit.

Proposed fix, not implemented: separate `side=long|short` from `action=open|add|reduce|close`, and distinguish conditional setups from confirmed entries. Carry direction through reconciliation, resolver and displayed return calculations. Reclassify ambiguous historical sell records from their stored text; never globally turn sells into shorts. Decision: whether to score conditional ideas at all, and what entry/activation rule to use when no fill price is given.

## 3. Ingestion windows

Actual crontab confirms monitor every four hours, X digests daily 09:30 UTC, YouTube daily 09:00 UTC.

| Pipeline | Retrieval limit | Risk |
|---|---|---|
| Trade monitor | 100 raw tweets/account, `monitor.py:889` | More than 100 arrivals between successful fetches can hide older posts. Encountering an ID at/below the saved watermark also ends pagination. |
| X digests | 40 raw posts for Ki Young Ju, DonAlt, Cowen X; 60 for Joao, DorkChicken, DaanCrypto, Glassnode, Truecrypto, Kendrick | `twitter_digest.py:716` and `:748` stop at the cap OR a page containing an analyzed ID. Older failed posts can be stranded after newer successes; an out-of-order/pinned seen post can stop pagination early. Search feeds share this risk. |
| YouTube | Latest approximately 15 RSS entries, `youtube_monitor.py:317` | A video unavailable or failing analysis can leave RSS before recovery. Shorts/duplicate uploads consume feed slots before local deduplication. |

These are post-count limits, not fixed time windows. Nominal source comments estimate Joao 60/~6 raw posts/day ≈10 days, Daan 60/~5 ≈12 days, Truecrypto 60/~9.4 ≈6.4 days. Bursts and early termination can make these much shorter. Monitor overflow could occur in a single four-hour interval; daily digest latency is ordinarily up to one day even without loss. YouTube runway is 15 divided by raw uploads/day, not necessarily 15 days. No guaranteed safe outage length follows from these averages.

Confirmed missing records: the following seven Joao IDs appear in processing attempts around the historical Gemini outage, but are absent from today's `data/joao_summaries.json`:

- July 8: 2075003999304994866, 2074999865952911870, 2074947191186092343, 2074909734885285942.
- July 9: 2075320805429256228, 2075263478349099346, 2075243464942399558.

`twitter_digest.log:677` through 684 includes all seven and ends with `Analyzed 0 post(s)`. Dates above are decoded from X snowflake IDs. They include market posts about SOL liquidations, ZEC liquidations and S&P/M2, but also a recruiting post; seven missing summaries is not seven lost trading signals. Historical 403/429 errors support failed analysis, and bounded recovery is consistent with permanent omission. Logs do not uniquely prove which pagination/retention condition prevented each retry; do not attribute all seven specifically to the current cap. No comparably concrete monitor/YouTube loss was established. Never-fetched content cannot be proven absent from local ledgers alone.

The earlier audit's monitor pending-tweet spool protects already-fetched failed text payloads; it does not recover never-fetched overflow. X/YouTube do not have an equivalent durable raw-payload retry queue. Proposed fix: persist fetched input before analysis, retry failures independently of discovery, and paginate/backfill with overlap and explicit completeness limits. Decision: desired retention/catch-up guarantee and acceptable extra scraper/LLM spend.

## 4. Model/thinking compatibility

Current code, not the initial project description:

| Pipeline | Actual model | Thinking configuration |
|---|---|---|
| Monitor text extraction | gemini-2.5-flash-lite | EXTRACT_THINKING, budget 0 |
| Kendrick triage | gemini-2.5-flash-lite | EXTRACT_THINKING, budget 0 |
| Monitor chart vision | gemini-3.7-flash | GEMINI_THINKING, budget 0 |
| X post/chart/current-view and Kendrick representative analysis | gemini-3.7-flash | GEMINI_THINKING, budget 0 |
| YouTube native AGENTIC video and current view | gemini-3.7-flash | GEMINI_THINKING, budget 0 |

Locations: `monitor.py:92`, `:114`, `:436`, `:474`; `twitter_digest.py:157`, `:927`, `:978`, `:1113`, `:1264`; `youtube_monitor.py:134`, `:405`, `:481`. All configured feed deep-thinking flags are false. Dynamic budget -1 remains defined but is not selected by active feeds. Neither gemini-3.5-flash-lite nor gemini-3.6-flash is an active model choice.

Recent logs show successful native YouTube analysis/current-view generation and X digest processing; no current invalid-thinking-configuration outage was established. Historical project-denied/quota errors are not evidence of thinking incompatibility. This review did not make paid live model calls.

Google's current thinking documentation describes thinking levels for Gemini 3.x, while this code uses GenerateContent ThinkingConfig budgets. It lists minimal as an option for 3.5 Flash-Lite and 3.6 Flash, and low/medium/high for 3.7 Flash. Thus the earlier blanket claim that every 3.5/3.6 model needs at least low is too broad. Documentation/endpoint differences warrant an exact model+SDK+GenerateContent smoke test before a swap, not an untested change to a locally successful setting. AGENTIC media processing is a separate setting.

Sources checked: https://ai.google.dev/gemini-api/docs/thinking and https://ai.google.dev/gemini-api/docs/video-understanding .

Decision: no demonstrated outage requiring an emergency switch; choose whether to fund a small compatibility smoke-test matrix now or make it a prerequisite for the next model/SDK change. All model names, thinking settings and ingestion logic were left unchanged in this follow-up.

## 5. Retirement completed and validated

Removed the Reddit miner, its dashboard tab/callback/cards/cost display, local strategy/cost/seen data and log, project credential/reference entries, and its actual host cron job. Other projects' cron jobs were preserved byte-for-byte; pre-edit crontab backup: `/tmp/pilot-crontab-before-retirement-nwzjabxt.bak`.

Removed Telegram senders, CLI test option, notification call sites and cross-project credential loading from monitor, X, YouTube and model-deprecation checker. Outages remain visible through stderr, local logs and exit codes. Shared credentials belonging to other projects were not removed. Historical audit/log mentions remain historical evidence; no active project feature depends on them.

Validation: **80 tests passed**, one existing Dash DataTable deprecation warning. Relative to the earlier 82, two miner tests and one retired-tab parameter were removed; one new dashboard retirement/tab-routing test was added. Docker image rebuilt and container recreated successfully; container reports healthy, and `/`, `/_dash-layout`, `/_dash-dependencies` return HTTP 200. The running layout/dependency endpoints contain no Reddit references.
