"""Conservative classification of source events; no I/O or model calls."""
import copy
import re

from accounts import INFLUENCER_ACCOUNTS

SEMANTICS_VERSION = 3
EVENT_KINDS = ("setup", "entry", "add", "trim", "exit", "holding", "recap", "commentary", "review")
POSITION_KINDS = {"entry", "add", "trim", "exit", "holding"}
NON_TRADEABLE_TICKERS = {
    "NONE", "NULL", "NYMO", "NAMO", "COMPQ", "NASDAQ", "RUT", "SPX", "SPX500",
    "VIX", "DJI", "DJIA", "DXY", "XAUUSD", "XAGUSD", "US10Y", "US30", "NAS100",
    "GOLD", "SILVER", "BTCDOMINANCE", "BTC.D", "ETH.D", "USDT.D", "TOTAL", "TOTAL2", "TOTAL3",
}
_ACTION = {"entry": "open", "add": "add", "trim": "reduce", "exit": "close", "holding": "hold", "setup": "open"}
_RECAP = re.compile(r"\btop picks?\b.*\b(?:ytd|week\s*\d+|results?|performance|strategy)\b|\b(?:educational post|trade review)\b.*\b(?:we did|we traded|yesterday|today|this week)\b", re.I | re.S)
_BUY = re.compile(r"\b(?:bought|purchased|entered|initiated|re-?entered|shorted|buying|shorting|added|adding)\b", re.I)
_SELL = re.compile(r"\b(?:sold|exited|trimmed|reduced|covered|stopped out|stopping out)\b|\bclosed\s+(?:(?:the|my|our|this|a)\s+)?(?:position|trade|it|one|out|early myself)\b|\btook\s+(?:(?:all|some|partial)\s+)?(?:gains|profits)\b|\bbooked\s+(?:gains|profits|the trade|this one)\b|\b(?:took|taking) a loss\b", re.I)
_HOLD = re.compile(r"\bstill holding(?=\s*(?:[.!?;\n]|$|(?:this|it|them|\$)))|\b(?:i|we)(?:'m| am| are)?\s+(?:holding|long|short)\b|\bmy position\b|\bour position\b", re.I)
_FUTURE = re.compile(r"\b(?:setup|watch|watching|watchlist|keep an eye|targets?|breakout|look for|poised|ready|bull flag|bullish|bearish|consolidation|higher low|inside day|wedge|short squeeze|squeeze candidate|reversal|bounce)\b", re.I)
_SHORT_IDEA = re.compile(r"\b(?:short setup|short entry|short position|potential short|shorting|shorted|covered|covering)\b|\b(?:i|we)(?:'m| am| are)?\s+short\b", re.I)
_CONDITIONAL = re.compile(r"\b(?:if|would|could|should|might|will|can|may|prefer|not|never|didn't|haven't|hasn't|hadn't|hypothetical)\b", re.I)


def clean_ticker(ticker):
    return ticker.strip().lstrip("$").upper() if isinstance(ticker, str) else ""


def is_junk_ticker(ticker):
    t = clean_ticker(ticker)
    return not t or t in NON_TRADEABLE_TICKERS or t.endswith(".D") or t.startswith(("XAU", "XAG"))


def _executed(pattern, text, ticker=None, source_text=None):
    for match in pattern.finditer(text):
        prefix = re.split(r"[.!?;\n]", text[:match.start()])[-1][-70:]
        clause = re.split(r"[.!?;\n]", text[match.start():])[0]
        if re.search(r"\b(?:watchlist|watch list|fuel|interest|volume|million|data|win|dataset)\b", clause, re.I) and re.match(r"add", match.group(), re.I):
            continue
        if _CONDITIONAL.search(prefix):
            continue
        own = re.search(r"\b(?:i|we|my|our|myself|ourselves)\b", prefix + match.group(), re.I)
        # Bare verbs are accepted only at the beginning of their clause.
        if not own and prefix.strip() and not re.search(r"\b(?:stopped out|stopping out|took all gains off|taking a loss|still holding)\b", match.group(), re.I):
            continue
        if re.search(r"\b(?:has been undefeated|insider buying|institutional buying|holding this level|holding at a? ?\d+%|purchased domestically)\b", prefix + clause, re.I):
            continue
        if ticker:
            scope = prefix + match.group() + clause
            tags = {clean_ticker(t) for t in re.findall(r"\$([A-Za-z][A-Za-z0-9.]*)", scope)}
            all_tags = {clean_ticker(t) for t in re.findall(r"\$([A-Za-z][A-Za-z0-9.]*)", source_text or text)}
            if ticker not in tags and not re.search(r"\b" + re.escape(ticker) + r"\b", scope) and len(all_tags) > 1:
                continue
        return True
    return False


def quote_in_text(quote, text):
    return isinstance(quote, str) and bool(quote.strip()) and " ".join(quote.split()).casefold() in " ".join(text.split()).casefold()


def normalize_event(event):
    e = copy.deepcopy(event)
    text = e.get("text") or ""
    kind = e.get("event_kind")
    side = e.get("model_side") or e.get("side")
    side = side if side in ("long", "short") else "long"
    strict = e.get("account") in INFLUENCER_ACCOUNTS or bool(text)
    reason = None
    if text.lstrip().startswith("RT "):
        kind, reason = "commentary", "retweet_is_not_an_own_execution"
    elif _RECAP.search(text):
        kind, reason = "recap", "retrospective_result"
    elif strict:
        proof = e.get("execution_evidence")
        evidence_text = proof if quote_in_text(proof, text) else text
        ticker = clean_ticker((e.get("tickers") or [""])[0])
        buy, sell, hold = (_executed(pattern, evidence_text, ticker, text) for pattern in (_BUY, _SELL, _HOLD))
        if proof:
            # A clipped quote must not remove the source's negation or actor.
            buy = buy and _executed(_BUY, text, ticker, text)
            sell = sell and _executed(_SELL, text, ticker, text)
            hold = hold and _executed(_HOLD, text, ticker, text)
        if proof and not quote_in_text(proof, text):
            buy = sell = hold = False
            reason = "execution_evidence_not_in_source"
        if kind in ("setup", "commentary", "recap", "review"):
            buy = sell = hold = False
        if sell and re.search(r"still holding", text, re.I) and re.search(r"\baccounts?\b", text, re.I):
            kind, reason = "review", "multiple_accounts_have_different_position_states"
        elif sell:
            if re.search(r"\b(?:covered|covering)\b", text, re.I):
                side = "short"
            partial = e.get("sell_kind") == "partial" or bool(re.search(r"\b(?:half|partial|trimmed|reduced|scaled out|some profits|some gains)\b", text, re.I))
            kind = "trim" if partial else "exit"
        elif buy:
            if re.search(r"\b(?:shorted|shorting)\b", text, re.I):
                side = "short"
            kind = "add" if re.search(r"\b(?:added|adding)\b", text, re.I) else "entry"
        elif hold:
            kind = "holding"
            if re.search(r"\b(?:i|we)(?:'m| am| are)?\s+short\b", evidence_text, re.I):
                side = "short"
        elif kind == "recap":
            reason = reason or "retrospective_result"
        elif e.get("entry_status") == "review" or kind == "review":
            kind, reason = "review", reason or "execution_not_verified"
        elif re.search(r"\b(?:analyst|analysts|consensus)\b.{0,30}\btargets?\b", text, re.I) and not re.search(r"\b(?:watch|watchlist|setup|look for|poised|ready|breakout|pullback|consolidation|my target|our target)\b", text, re.I):
            kind, reason = "commentary", "third_party_target_without_own_setup"
        elif proof and re.search(r"\b(?:institutional|insider|domestically)\b", proof, re.I):
            kind, reason = "commentary", "third_party_activity"
        elif kind in POSITION_KINDS and proof and quote_in_text(proof, text) and (
                any(pattern.search(proof) for pattern in (_BUY, _SELL, _HOLD)) or
                re.search(r"\b(?:i|we|my|our)\b", proof, re.I) and not re.search(r"\b(?:think|believe|expect|hope|looks|printing|highs|target)\b", proof, re.I)):
            kind, reason = "review", "execution_language_or_actor_not_verified"
        elif kind not in ("setup", "commentary", "recap") and not proof and any(_executed(pattern, text) for pattern in (_BUY, _SELL, _HOLD)):
            kind, reason = "review", "execution_instrument_not_verified"
        elif _FUTURE.search(text) or kind == "setup" or e.get("entry_status") == "setup":
            kind, reason = "setup", reason or "prospective_idea_without_fill"
            if _SHORT_IDEA.search(text):
                side = "short"
        else:
            kind, reason = "commentary", reason or "no_execution_or_entry_setup"
    elif kind not in EVENT_KINDS:
        # Compatibility for retired portfolio records without source text.
        kind = {"buy": "entry", "sell": "trim" if e.get("sell_kind") == "partial" else "exit", "position": "holding"}.get(e.get("signal_type"), "review")
    if kind == "setup" and e.get("thread_context") and not _FUTURE.search(text) and e.get("chart_trend") not in ("bullish", "bearish"):
        content = re.sub(r"@\w+|https?://\S+", "", text).strip()
        price_only = bool(re.fullmatch(r"(?:below|above|around|near)?\s*\$?\d+(?:\.\d+)?", content, re.I))
        kind = "review" if price_only else "commentary"
        reason = "unlabelled_level_reply" if price_only else "thread_context_is_not_a_fresh_setup"
    # Short interest/short-term/squeezes do not mean shorting the security.
    if not _SHORT_IDEA.search(text) and re.search(r"\bshort(?:[ -]term| interest| squeeze)\b", text, re.I) and not re.search(r"\b(?:bearish|bear flag|breakdown)\b", text, re.I):
        side = "long"
    action = _ACTION.get(kind)
    state = "confirmed" if kind in POSITION_KINDS else "setup" if kind == "setup" else kind
    e.update(event_kind=kind, side=side if action else None, position_action=action,
             entry_status=state, semantics_version=SEMANTICS_VERSION,
             classification_reason=reason or "explicit_execution_in_source")
    if action:
        sell = (action in ("reduce", "close") and side == "long") or (action in ("open", "add") and side == "short")
        e["signal_type"] = "position" if action == "hold" else "sell" if sell else "buy"
    else:
        e["signal_type"] = "none"
    e["actionable"] = kind in POSITION_KINDS
    if kind not in POSITION_KINDS:
        e["execution_evidence"] = None
    if kind == "trim" and e.get("exit_fraction") is None and re.search(r"\bhalf\b", text, re.I):
        e["exit_fraction"] = 0.5
    e["tickers"] = list(dict.fromkeys(clean_ticker(t) for t in e.get("tickers", []) if not is_junk_ticker(t)))
    if kind not in ("entry", "add", "holding"):
        e["entry_price"] = None
    if kind in ("recap", "commentary", "review"):
        e["stop_loss"] = None
        e["target"] = None
    return e


def semantics(event):
    e = normalize_event(event)
    return e.get("side"), e.get("position_action"), e["entry_status"]
