#!/usr/bin/env python3
"""Preview/apply a reversible source-grounded review without paid model calls.

Original ledgers are copied into a timestamped local archive before applying.
Only missing own posts from local snapshots are recovered; no account history
is replaced by the shorter snapshots. Unknown observation times stay unknown.
"""
import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from accounts import INFLUENCER_ACCOUNTS
from monitor import _grounded_price
from reconcile import fold_events, write_json_atomic
from signal_semantics import normalize_event, is_junk_ticker
from storage import ledger_lock

NUMBER = r"(\d[\d,]*(?:\.\d+)?)([kKmM]?)"
LABELS = {
    "target": r"\b(?:first\s+)?targets?\s*[:@-]?\s*\$?\s*" + NUMBER,
    "stop_loss": r"\b(?:firm\s+)?stop(?:\s+loss)?\s*(?:at\s+|is\s+)?[:@-]?\s*\$?\s*" + NUMBER,
    "entry_price": r"\b(?:bought|purchased|entered|shorted)\b[^.!\n]{0,50}?\bat\s*\$" + NUMBER,
    "exit_price": r"\b(?:sold|exited|covered|trimmed)\b[^.!\n]{0,50}?\bat\s*\$" + NUMBER,
}


def review_row(row):
    e = normalize_event(row)
    if row.get("prompt_version") == "signal-v3":
        for field, source in (row.get("level_sources") or {}).items():
            if field in LABELS and source.get("source") == "text":
                e[field] = _grounded_price(e.get(field), source.get("evidence"), field, row.get("text") or "")
        return e
    sources = {}
    for field, pattern in LABELS.items():
        m = re.search(pattern, row.get("text") or "", re.I)
        value = float(m[1].replace(",", "")) * {"k": 1000, "m": 1_000_000}.get(m[2].lower(), 1) if m else None
        e[field] = _grounded_price(value, m[0], field, row.get("text") or "") if m else None
        if e[field] is not None:
            sources[field] = dict(source="text", evidence=m[0])
    e["level_sources"] = sources
    e["published_at"] = e.get("published_at") or e.get("timestamp")
    e["first_observed_at"] = row.get("first_observed_at")
    e["historical_review_version"] = 3
    # A second normalization applies category constraints to recovered levels.
    return normalize_event(e)


def reviewed_history(events, snapshots):
    reviewed, recovered = [], []
    seen = {(e.get("account"), e.get("tweet_id")) for e in events}
    for e in events:
        reviewed.append(review_row(e) if e.get("account") in INFLUENCER_ACCOUNTS else e)
    for account, posts in snapshots.items():
        for tw in posts:
            tid = str(tw.get("id") or "")
            if not tid or (account, tid) in seen or (tw.get("author") or account).lower() != account.lower():
                continue
            seen.add((account, tid))
            symbols = list(dict.fromkeys(s for s in re.findall(r"\$([A-Z][A-Z0-9.]{0,9})\b", tw.get("text", "")) if not is_junk_ticker(s)))
            for symbol in symbols:
                raw = dict(account=account, source_type="influencer", portfolio=None,
                    tweet_id=tid, timestamp=tw.get("created_at"), text=tw.get("text", ""), tickers=[symbol],
                    asset_type="stock" if account == "traderstewie" else "unknown", confidence="medium",
                    signal_type="none", url=f"https://x.com/{account}/status/{tid}",
                    media=tw.get("media", []), first_observed_at=None, recovery_source="local_snapshot")
                e = review_row(raw)
                if e["event_kind"] != "commentary":
                    e["signal_id"] = f"{tid}:{symbol}:{e['event_kind']}"
                    reviewed.append(e); recovered.append(e)
    return sorted(reviewed, key=lambda e: e.get("timestamp") or "", reverse=True), recovered


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    trades, positions = ROOT / "trades.json", ROOT / "positions.json"
    with ledger_lock(trades):
        original = trades.read_bytes()
        events = json.loads(original)
        snapshots = {a: json.loads(p.read_text()) for a in INFLUENCER_ACCOUNTS
                     if (p := ROOT / f"tweets_{a.lower()}.json").exists()}
        reviewed, recovered = reviewed_history(events, snapshots)
        candidate = fold_events(reviewed)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        folder = ROOT / "data" / "signal_review" / stamp
        folder.mkdir(parents=True)
        write_json_atomic(folder / "candidate_trades.json", reviewed)
        write_json_atomic(folder / "candidate_positions.json", candidate)
        summary = dict(applied=args.apply, original_sha256=hashlib.sha256(original).hexdigest(),
            original_events=len(events), reviewed_events=len(reviewed), recovered=len(recovered),
            recovered_exit_ids=[e["tweet_id"] for e in recovered if e["event_kind"] in ("exit", "trim")],
            accounts={a: dict(Counter(p["status"] for p in candidate if p.get("account") == a)) for a in INFLUENCER_ACCOUNTS})
        if args.apply:
            shutil.copy2(trades, folder / "original_trades.json")
            if positions.exists():
                shutil.copy2(positions, folder / "original_positions.json")
            write_json_atomic(trades, reviewed)
            write_json_atomic(positions, candidate)
        write_json_atomic(folder / "summary.json", summary)
        print(json.dumps(summary, indent=2))
        print("Saved local review:", folder.relative_to(ROOT))


if __name__ == "__main__":
    main()
