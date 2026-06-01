#!/usr/bin/env python3
"""Fetch the last 10 tweets from @JoinAutopilot_ via X API v2."""

import os
import sys
import urllib.parse
import urllib.request
import json

# Token loaded from ~/pilot_trader/.env (X_BEARER_TOKEN) — never hardcoded.
ENV_FILE = "/home/fbazsa/pilot_trader/.env"
if os.path.exists(ENV_FILE):
    for _line in open(ENV_FILE):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))
BEARER_TOKEN = os.environ.get("X_BEARER_TOKEN", "")
USERNAME = "grkportfolio"
MAX_RESULTS = 50
COST_PER_TWEET = 0.005
KEYWORDS = ["buy", "sell", "portfolio", "picks", "June", "May",
            "rebalance", "holding", "position"]

API_BASE = "https://api.twitter.com/2"


def api_get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {BEARER_TOKEN}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def get_user_id(username):
    url = f"{API_BASE}/users/by/username/{urllib.parse.quote(username)}"
    data = api_get(url)
    if "data" not in data:
        raise RuntimeError(f"User lookup failed: {json.dumps(data)}")
    return data["data"]["id"]


def get_tweets(user_id, max_results):
    params = urllib.parse.urlencode({
        "max_results": max_results,
        "tweet.fields": "created_at,text",
    })
    url = f"{API_BASE}/users/{user_id}/tweets?{params}"
    return api_get(url)


def main():
    try:
        user_id = get_user_id(USERNAME)
        result = get_tweets(user_id, MAX_RESULTS)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} error: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    tweets = result.get("data", [])
    if not tweets:
        print(f"No tweets returned. Raw response: {json.dumps(result)}")
        sys.exit(0)

    lowered = [k.lower() for k in KEYWORDS]
    matched = 0
    for tw in tweets:
        text = tw.get("text", "")
        hits = [k for k in lowered if k in text.lower()]
        if not hits:
            continue
        matched += 1
        ts = tw.get("created_at", "(no timestamp)")
        print(f"--- Match {matched} ---")
        print(f"Time: {ts}")
        print(f"Keywords: {', '.join(hits)}")
        print(f"Text: {text}")
        print()

    read_count = len(tweets)
    cost = read_count * COST_PER_TWEET
    print(f"Tweets read: {read_count}")
    print(f"Tweets matching keywords: {matched}")
    print(f"Estimated API cost: ${cost:.4f} (at ${COST_PER_TWEET}/tweet)")


if __name__ == "__main__":
    main()
