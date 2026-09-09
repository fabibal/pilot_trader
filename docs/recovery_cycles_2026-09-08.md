# Recovery, trading cycles and direction — implemented 2026-09-08

Authorized follow-up to `decision_details_2026-09-08.md` (which describes the pre-fix findings).

## Changes

- X digest feeds, including Kendrick's two-stage pipeline, and YouTube now save selected raw inputs to `<summaries_file>.pending.json` before any LLM processing or `--limit` truncation. Successful output is atomically committed before acknowledging the queue. Interrupted acknowledgement is safe: committed ledger IDs are reconciled on the next run. Dry runs do not mutate queues. Existing filtering and Hungarian corruption retry/give-up behavior remain.
- A discovery failure does not prevent saved X/YouTube inputs from being processed. The job raises after processing them, so the scraper/RSS outage still produces a failing cron run.
- X/monitor pagination scans the entire configured overlap window instead of stopping at an already-known/pinned post. The final API page is retained in full instead of silently discarding its tail. Repeated cursors cannot create endless pagination. Reaching the cap with more available content emits a warning.
- Reconciliation schema 2 preserves closed cycles in `prior_cycles` with deterministic cycle IDs. Keeping the current cycle and history together in the atomically replaced positions file avoids a two-file consistency problem. Dashboard performance calculations expand the preserved cycles; the open-position table still shows current holdings only.
- New Gemini extraction explicitly separates `side`, `position_action`, and `entry_status`. Confirmed shorts open independent short holdings; covers close/reduce those holdings without affecting longs. Resolver and influencer return calculations respect explicit direction.
- Prospective setups cannot move confirmed holdings or enter performance statistics. Ambiguous legacy short/cover sell records are retained as review items; explicitly prospective short language is kept as setup. Legacy buy records containing potential/watching/watchlist language are conservatively set aside for review. Other historical inferred holdings are marked `legacy`, not relabelled confirmed. The dashboard displays excluded setups/review records and new signal direction/status fields.

## Data migration

Replayed the existing event log under its writer lock after backing up both trades and positions to:

`data/backups/cycle-migration-20260908T183836Z/`

Raw trades were verified unchanged. Across currently monitored accounts, six closed cycles are now preserved. Their current rows changed from 106 (91 open, 6 closed, 9 without established holding state) to 110 (87 open, 6 closed, 9 without holding state, 7 review, 1 setup). The total file includes retired accounts as before: 288 rows and 16 archived cycles overall.

AXTI's March 13 potential short now appears as a setup, separate from its April long. AEHR retains an earlier closed cycle separately from its September reentry. Its older prospective buy also appears as a review item; the existing later position disclosure establishes the earlier holding. These are reconstructed signal histories, not newly verified exchange fills or independently audited returns.

## Validation and limits

90 offline tests pass, including cycle replay determinism, old/new cycle separation, short opening/covering alongside an unchanged long, setup exclusion, archived-cycle dashboard scoring, pinned-post pagination, queue restart/dry-run behavior, X/YouTube recovery after content leaves discovery, RSS outage with pending work, and Kendrick triage recovery. One pre-existing Dash DataTable deprecation warning remains.

Discovery is still bounded at the configured 40/60/100 raw-post windows (plus the retained final page), and YouTube still discovers via its finite RSS feed. The queue protects fetched inputs; it cannot recover never-fetched content. The seven historically missing Joao summaries were not reconstructed from truncated log snippets or invented. Scanning the complete overlap window costs more scraper calls on quiet feeds, but previously summarized posts are still excluded before LLM analysis. No paid LLM smoke tests or historical re-extraction were performed.

Legacy classification is deliberately conservative and incomplete; stored prose cannot establish every real fill or every ambiguous trading cycle. Review items remain available rather than being assigned speculative wins/losses. Model/thinking settings are unchanged. Reddit and Telegram remain retired.
