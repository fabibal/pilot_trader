import sys
sys.path.insert(0, "/home/fbazsa/pilot_trader")
from youtube_transcript_api import YouTubeTranscriptApi
f = YouTubeTranscriptApi().fetch("D_QN8lMYAP4", languages=["en","en-US"])
snips = list(f)
print(f"total snippets={len(snips)} last_end={snips[-1].start+snips[-1].duration:.0f}s")
# what the 60k-char cap cuts off
full = " ".join(s.text for s in snips)
kept = full[:60000]
print(f"full={len(full)} chars; cap cuts {len(full)-60000} chars")
# find the timestamp where the 60k cap lands
acc = 0
for s in snips:
    acc += len(s.text) + 1
    if acc >= 60000:
        print(f"--> 60k cap lands at t={s.start:.0f}s ({s.start/60:.1f} min) of {snips[-1].start:.0f}s")
        break
print("\n=== 3250-3400s (the linked t=3289s region) ===")
for s in snips:
    if 3250 <= s.start <= 3400:
        print(f"[{int(s.start//60):02d}:{int(s.start%60):02d}] {s.text}")
