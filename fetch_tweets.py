#!/usr/bin/env python3
"""Fetch up to 100 recent tweets and save raw JSON.

Usage: python3 fetch_tweets.py [username] [out_path]
"""

import json
import os
import sys
import urllib.parse
import urllib.request

USERNAME = sys.argv[1] if len(sys.argv) > 1 else "grkportfolio"
TARGET = 100
OUT = sys.argv[2] if len(sys.argv) > 2 else f"/home/fbazsa/pilot_trader/tweets_{USERNAME}.json"
API_BASE = "https://api.twitter.com/2"
ENV_FILE = "/home/fbazsa/pilot_trader/.env"


def _load_env():
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {os.environ['X_BEARER_TOKEN']}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main():
    _load_env()
    uid = api_get(f"{API_BASE}/users/by/username/{USERNAME}")["data"]["id"]
    collected = []
    token = None
    while len(collected) < TARGET:
        params = {
            "max_results": min(100, max(5, TARGET - len(collected))),
            "tweet.fields": "created_at,text,referenced_tweets,public_metrics",
        }
        if token:
            params["pagination_token"] = token
        url = f"{API_BASE}/users/{uid}/tweets?{urllib.parse.urlencode(params)}"
        data = api_get(url)
        collected.extend(data.get("data", []))
        token = data.get("meta", {}).get("next_token")
        if not token:
            break
    collected = collected[:TARGET]
    with open(OUT, "w") as f:
        json.dump(collected, f, indent=2)
    print(f"Saved {len(collected)} tweets to {OUT}")


if __name__ == "__main__":
    main()
