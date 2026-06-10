#!/usr/bin/env python3
"""Single source of truth for monitored-account configuration.

Previously ACCOUNT_DEFAULT_PF was defined three times (reconcile.py,
auto_trader.py, dashboard.py) and the influencer sets DISAGREED between
reconcile.py and dashboard.py — adding an account meant four edits and a silent
failure mode. Everything identity/classification-related now lives here;
module-specific knobs (poll intervals, raw-file paths, caps) stay where they
are used.
"""

# Monitored handles. "portfolio" kind = AI bot posting its own trades;
# "influencer" = human trader (no reply gate, extra fields).
ACCOUNTS = ["grkportfolio", "theaiportfolios", "aifinancelabs", "IncomeSharks",
            "moninvestor", "CelalKucuker", "traderstewie", "PelosiTracker"]

SOURCE_TYPE = {"IncomeSharks": "influencer", "moninvestor": "influencer",
               "CelalKucuker": "influencer", "traderstewie": "influencer",
               "PelosiTracker": "influencer"}

# Posting style hint passed to the LLM. "conviction_long" = slow long-term
# holders: "adding"/"keeping" are holding updates, not new buys.
ACCOUNT_STYLE = {"moninvestor": "conviction_long",
                 "PelosiTracker": "conviction_long"}

# Accounts fetched from the POSTS-ONLY endpoint (no @-replies in the thread).
POSTS_ONLY_ACCOUNTS = {"traderstewie", "PelosiTracker"}

# Account -> default portfolio when the LLM left portfolio null.
ACCOUNT_DEFAULT_PF = {"grkportfolio": "grok", "theaiportfolios": "claude",
                      "aifinancelabs": "deepseek"}

# Human trade-call accounts (win-rate / TP-stop resolution views).
INFLUENCER_ACCOUNTS = {"IncomeSharks", "CelalKucuker", "traderstewie"}
# Long-term conviction accounts (holdings view, no TP/stop).
LONGTERM_ACCOUNTS = {"moninvestor", "PelosiTracker"}
# Everything that is not an AI portfolio bot. These accounts never key a
# portfolio in positions.json (portfolio stays null by design).
NON_AI_ACCOUNTS = INFLUENCER_ACCOUNTS | LONGTERM_ACCOUNTS

# AI portfolios mirrored to the IBKR paper account. ChatGPT is intentionally
# excluded (no standalone handle; only surfaces via @aifinancelabs text).
MIRROR_PORTFOLIOS = {"grok", "claude", "deepseek"}
