#!/usr/bin/env python3
"""
Test the GetXAPI Twitter/X API: fetch the last 10 tweets from @grkportfolio.

Docs: https://docs.getxapi.com/docs/users/user-tweets
  GET https://api.getxapi.com/twitter/user/tweets?userName=<handle>
  Auth: Authorization: Bearer <GETXAPI_KEY>
  Returns ~20 tweets/page in {tweets:[...], has_more, next_cursor}.

API key is read from ~/pilot_trader/.env as GETXAPI_KEY.
"""

import json
import os
import sys
import urllib.parse
import urllib.request

ENV_FILE = "/home/fbazsa/pilot_trader/.env"
API_BASE = "https://api.getxapi.com"
USERNAME = "grkportfolio"
WANT = 10
COST_PER_CALL = 0.001          # $/call, ~20 tweets per call


def load_env(path):
    if os.path.exists(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_tweets(username, key):
    params = urllib.parse.urlencode({"userName": username})
    url = f"{API_BASE}/twitter/user/tweets?{params}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def main():
    load_env(ENV_FILE)
    key = os.environ.get("GETXAPI_KEY")
    if not key:
        print("GETXAPI_KEY is not set. Add it to ~/pilot_trader/.env "
              "(GETXAPI_KEY=...) and re-run.", file=sys.stderr)
        sys.exit(1)

    try:
        data = get_tweets(USERNAME, key)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code} error: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    tweets = data.get("tweets", [])
    if not tweets:
        print(f"No tweets returned. Raw response: {json.dumps(data)[:500]}")
        sys.exit(0)

    calls = 1                            # one page covers the 10 we want
    tweets = tweets[:WANT]
    for i, tw in enumerate(tweets, 1):
        ts = tw.get("createdAt", "(no timestamp)")
        text = tw.get("text", "")
        print(f"--- Tweet {i} ---")
        print(f"Time: {ts}")
        print(f"Text: {text}")
        print()

    print(f"Total tweets shown: {len(tweets)}")
    print(f"API calls made: {calls}")
    print(f"Estimated API cost: ${calls * COST_PER_CALL:.4f} "
          f"(${COST_PER_CALL}/call, ~20 tweets/call)")


if __name__ == "__main__":
    main()
