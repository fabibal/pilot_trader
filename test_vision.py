#!/usr/bin/env python3
"""
Vision-enhancement test for the IncomeSharks chart-image extraction.

Runs the real pipeline over the local IncomeSharks snapshot
(tweets_incomesharks.json) and reports:
  - how many tweets carry a chart photo,
  - how many resulting signals were IMPROVED by the Sonnet vision pass
    (a chart-only level filled a gap the text pass left null), and
  - the actual token cost split: Haiku text vs Sonnet vision.

It only spends vision tokens on influencer tweets that (a) pass the cheap
Haiku text pass as a real signal AND (b) carry a photo, mirroring monitor.py.

Run: /home/fbazsa/pilot_trader/.venv/bin/python test_vision.py
"""

import copy

import monitor

SNAPSHOT = "tweets_incomesharks.json"
ACCOUNT = "IncomeSharks"
CHART_FIELDS = ("ticker", "stop_loss", "target", "tp1", "tp2", "chart_notes")


def main():
    monitor.load_env(monitor.ENV_FILE)
    tweets = monitor.load_json(SNAPSHOT, [])
    with_media = [t for t in tweets if t.get("media")]
    print(f"Snapshot: {len(tweets)} tweets, {len(with_media)} with a chart photo\n")

    interp = monitor.Interpreter()
    improved, vision_signals, examples = 0, 0, []

    for tw in with_media:
        text = tw.get("text", "")
        tweet_date = (tw.get("created_at") or "")[:10]
        parsed = interp.extract(text, ACCOUNT, tweet_date)
        if not parsed or parsed["action"] == "none" or not parsed.get("ticker"):
            # Text pass found no signal -> monitor would still try vision only if
            # parsed is non-null; but a dropped record can't "improve", skip cost.
            continue
        before = {k: copy.copy(parsed.get(k)) for k in CHART_FIELDS}
        chart = interp.extract_chart(tw["media"], text, ACCOUNT, tweet_date)
        if chart is None:
            continue
        vision_signals += 1
        did = monitor.merge_chart(parsed, chart)
        if did:
            improved += 1
            gained = {k: parsed.get(k) for k in CHART_FIELDS
                      if before.get(k) != parsed.get(k)}
            if len(examples) < 6:
                examples.append((parsed.get("ticker"), text[:60], gained))

    print(f"Vision calls: {vision_signals} signal-bearing photo tweets analyzed")
    print(f"Signals improved by vision: {improved}/{vision_signals}\n")
    for tic, snippet, gained in examples:
        print(f"  [{tic}] {snippet!r}")
        print(f"      + {gained}")

    text_cost = interp.cost()
    vision_cost = interp.vision_cost()
    # What the same vision tweets would have cost if we had (illegally) tried to
    # run the TEXT pass on Sonnet too -- the per-token premium of vision.
    sonnet_in = monitor.SONNET_INPUT_PER_1M / monitor.HAIKU_INPUT_PER_1M
    print(f"\nCost (actual, this run):")
    print(f"  Haiku text  : {interp.calls} calls, "
          f"{interp.input_tokens} in / {interp.output_tokens} out tok  "
          f"${text_cost:.4f}")
    print(f"  Sonnet vision: {interp.vision_calls} calls, "
          f"{interp.vision_input_tokens} in / {interp.vision_output_tokens} out tok  "
          f"${vision_cost:.4f}")
    print(f"  TOTAL: ${text_cost + vision_cost:.4f}")
    print(f"\nSonnet input is {sonnet_in:.1f}x Haiku's per-token rate "
          f"(${monitor.SONNET_INPUT_PER_1M}/MTok vs "
          f"${monitor.HAIKU_INPUT_PER_1M}/MTok); vision adds "
          f"${vision_cost:.4f} on top of the ${text_cost:.4f} text baseline.")


if __name__ == "__main__":
    main()
