# Gemini model review, September 2026

Keep `gemini-2.5-flash-lite` for signal extraction and Kendrick triage, and
`gemini-3.7-flash` for text analysis, vision, current views and native video.
Accuracy takes priority over response time. The newer candidates did not
establish a quality advantage on this project's sample.

## Current models and available candidates

| Model | Role / candidate | Standard input / output USD per million tokens |
|---|---|---|
| `gemini-2.5-flash-lite` | Current signal extraction and triage | 0.10 / 0.40 |
| `gemini-3.7-flash` | Current analysis, vision and agentic video | 0.75 / 3.75 |
| `gemini-3.8-flash` | Newer stable Flash candidate | 0.75 / 3.75 |
| `gemini-3.5-flash-lite` | Newer extraction candidate | 0.30 / 2.50 |
| `gemini-3.1-pro-preview` | Stronger Pro candidate | 2.00 / 12.00 up to 200k input tokens; 4.00 / 18.00 above that |

The 3.7 and 3.8 Flash prices are introductory through December 31, 2026;
Google lists 1.50 / 7.50 starting January 1, 2027. None of the current
models has an announced shutdown date. Google continues serving 2.5 to
existing users. Sources: [models](https://ai.google.dev/gemini-api/docs/models),
[pricing](https://ai.google.dev/gemini-api/docs/pricing),
[deprecations](https://ai.google.dev/gemini-api/docs/deprecations).

The installed `google-genai` 2.22.0 can call the candidates. Live probes
confirmed `thinking_budget=0` works with 3.8 Flash, while 3.5 Flash-Lite
accepts `thinking_level="minimal"`. Pro rejects `AGENTIC` video processing;
its successful comparison used static video at 0.2 FPS and low thinking.
Google documents agentic video support for Flash 3.8, 3.7, 3.6 and 3.5
Flash-Lite: [video understanding](https://ai.google.dev/gemini-api/docs/video-understanding#agentic-video-understanding).

## Live comparison

Real inputs, existing schemas and production prompts were used without
writing candidate outputs to production ledgers. Token costs include
reported thinking and tool input. Each cell below is the cost of the whole
sample, not one item.

| Workload | Current model | Candidate | Observation |
|---|---|---|---|
| 12 posts across six X feeds | 3.7: $0.01521 | 3.8: $0.01556 | 12/12 valid JSON for both; no detected Hungarian corruption; sentiment agreed on 11/12 |
| Four Glassnode image inputs | 3.7: $0.00651 | 3.8: $0.00560 | All four chart sentiment values agreed |
| 12 signal extraction inputs | 2.5 Lite: $0.00247 | 3.5 Lite: $0.00986 | Both 12/12 valid; action differed on three ambiguous cases; no established quality gain |
| Latest Cowen video | 3.7: $0.02547 | 3.8: $0.02526 | Broad thesis and named levels agreed |
| 48-minute MakeItCount video, 10 chapters | 3.7: $0.16060 | 3.8: $0.14921 | Both preserved the supplied chapters; 3.8 substituted incorrect project names |
| Same MakeItCount video, static Pro | 3.7 result above | Pro: $0.25521 | Project names were correct, but the Clarity Act account was materially less faithful |

The disputed MakeItCount chapter was checked against the original Hungarian
captions. At approximately 35:51 and 37:24 the speaker names Uniswap and
Aerodrome; 3.8 instead named Hyperliquid and Lighter. The original 3.7 result
kept the correct names. At approximately 4:41–5:07 the speaker distinguishes
failure of the law from failure to open its debate; Pro described the act
as defeated in the Senate. Reference:
[original video](https://www.youtube.com/watch?v=vVzpvE2Wr3Q).

The 3.8 sentiment disagreement was on a very short, context-poor X post.
Schema validity and agreement are compatibility checks, not an accuracy
score. This is a small sample and does not rank the models universally.
It provides insufficient evidence to replace the current production models.

Successful comparison calls with reported usage cost approximately $0.67.
The first five MakeItCount summaries plus their rolling current view cost
approximately $0.42. These are measured samples, not monthly projections.

Scope was subsequently narrowed at the user's request: keep only the two
latest uploads and process new uploads going forward; the historical pending
queue was cleared. The costs above describe calls already made before that
correction.
