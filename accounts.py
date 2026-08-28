#!/usr/bin/env python3
"""Single source of truth for monitored-account configuration.

Previously ACCOUNT_DEFAULT_PF was defined three times (reconcile.py,
auto_trader.py, dashboard.py) and the influencer sets DISAGREED between
reconcile.py and dashboard.py — adding an account meant four edits and a silent
failure mode. Everything identity/classification-related now lives here;
module-specific knobs (poll intervals, raw-file paths, caps) stay where they
are used.

The AI portfolio bots (@grkportfolio, @theaiportfolios, @aifinancelabs,
@ralliesarena) were removed 2026-08-27: GetXAPI 404'd the two Autopilot
handles since 2026-08-08/08-12 (see git history / CLAUDE.md), leaving the IBKR
mirror with zero input, so the whole "portfolio kind" account family — and the
auto_trader.py/ibkr_connector.py/order_manager.py mirror it fed — was retired
rather than left dark indefinitely. Every remaining account is "influencer".
"""

# Monitored handles. All are "influencer" kind (human trader; no reply gate,
# extra fields) — see the module docstring for why "portfolio" kind is gone.
ACCOUNTS = ["IncomeSharks", "traderstewie"]

SOURCE_TYPE = {"IncomeSharks": "influencer", "traderstewie": "influencer"}

# Accounts fetched from the POSTS-ONLY endpoint (no @-replies in the thread).
POSTS_ONLY_ACCOUNTS = {"traderstewie"}

# Account -> default portfolio when the LLM left portfolio null. No account is
# portfolio-kind any more (see module docstring), so this is permanently empty;
# kept (rather than deleted) so reconcile.pf_of()'s generic fallback still has
# something to import without a structural change to that working logic.
ACCOUNT_DEFAULT_PF = {}

# Human trade-call accounts (win-rate / TP-stop resolution views).
INFLUENCER_ACCOUNTS = {"IncomeSharks", "traderstewie"}
# Everything that is not an AI portfolio bot. These accounts never key a
# portfolio in positions.json (portfolio stays null by design).
NON_AI_ACCOUNTS = set(INFLUENCER_ACCOUNTS)
