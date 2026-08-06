# Model comparison

Baseline: **Qwen3.6-27B-Q6_K (chat, non-thinking)** (`chat-27b`)

## Deterministic gates

Pass rate is items where every gate the case runs came back clean.

| Case | Dimension | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `abstention-closed-book` | abstention | 10/12 | 5/12 | 5/12 | 6/12 | 7/12 | 6/12 |
| `abstention-grounded` | abstention | 12/12 | 10/12 | 12/12 | 12/12 | 11/12 | 11/12 |
| `braindump-split` | segmentation | 4/8 | 5/8 | 7/8 | 2/8 | 6/8 | 7/8 |
| `classify-hard` | categorization | 3/8 | 2/8 | 0/8 | 0/8 | 2/8 | 1/8 |
| `classify-notes` | categorization | 3/8 | 4/8 | 5/8 | 3/8 | 2/8 | 4/8 |
| `connection-judgment` | pair-judgment | 14/16 | 16/16 | 15/16 | 15/16 | 16/16 | 15/16 |
| `doc-cleanup-ocr` | document-cleanup | 2/8 | 3/8 | 2/8 | 3/8 | 0/8 | 2/8 |
| `enumeration-breadth` | enumeration | 3/8 | 1/8 | 0/8 | 0/8 | 0/8 | 0/8 |
| `grounding-draft` | grounding | 3/8 | 5/8 | 3/8 | 0/8 | 0/8 | 0/8 |
| `lcr-48k` | long-context | 10/10 | 8/10 | 10/10 | 9/10 | 9/10 | 9/10 |
| `lcr-60k` | long-context | 9/10 | 8/10 | 10/10 | 8/10 | 10/10 | 10/10 |
| `lcr-80k` | long-context | 9/10 | n/a | 10/10 | 8/10 | n/a | 10/10 |
| `meeting-brief` | long-form-reasoning | 6/8 | 2/8 | 5/8 | 1/8 | 2/8 | 4/8 |
| `summary-report` | summarization | 8/8 | 6/8 | 8/8 | 8/8 | 7/8 | 8/8 |
| `summary-transcript` | summarization | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 | 8/8 |
| `transcript-cleanup-meeting` | faithful-cleanup | 1/8 | 7/8 | 0/8 | 0/8 | 5/8 | 0/8 |
| `transcript-cleanup-memo` | faithful-cleanup | 2/8 | 5/8 | 8/8 | 1/8 | 7/8 | 2/8 |
| `verifier-seeded` | verification | 7/8 | 6/8 | 7/8 | 2/8 | 7/8 | 7/8 |

`n/a` — not run, and not a failure:

- `task-4b` / `lcr-80k` — needs about 82,000 tokens of context, task-4b has 65,538
- `task-9b` / `lcr-80k` — needs about 82,000 tokens of context, task-9b has 65,792

## Metrics

### `abstention-closed-book`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| abstained | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| correct | 0.83 | 0.42 | 0.77 | 0.50 | 0.58 | 0.64 |
| incorrect | 0.17 | 0.58 | 0.23 | 0.50 | 0.42 | 0.36 |
| omniscienceIndex | 0.67 | -0.17 | 0.54 | 0.00 | 0.17 | 0.28 |

### `abstention-grounded`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| abstained | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| correct | 1.00 | 0.83 | 1.00 | 1.00 | 0.92 | 0.97 |
| incorrect | 0.00 | 0.17 | 0.00 | 0.00 | 0.08 | 0.03 |
| omniscienceIndex | 1.00 | 0.67 | 1.00 | 1.00 | 0.83 | 0.94 |

### `braindump-split`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| kindOverlap | 0.51 | 0.40 | 0.48 | 0.55 | 0.52 | 0.48 |
| noteCount | 3.50 | 2.62 | 3.25 | 5.96 | 2.88 | 3.00 |
| outsideRangeBy | 0.75 | 0.38 | 0.12 | 2.29 | 0.25 | 0.12 |

### `classify-hard`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| needsReview | 0.00 | 0.00 | 0.04 | 0.04 | 0.00 | 0.00 |
| routingPropertiesCorrect | 0.60 | 0.57 | 0.57 | 0.55 | 0.62 | 0.55 |

### `classify-notes`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| needsReview | 0.08 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| routingPropertiesCorrect | 0.89 | 0.85 | 0.84 | 0.84 | 0.75 | 0.85 |

### `connection-judgment`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| falseNegative | 0.12 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| falsePositive | 0.00 | 0.00 | 0.04 | 0.02 | 0.00 | 0.04 |
| trueNegative | 0.50 | 0.50 | 0.46 | 0.48 | 0.50 | 0.46 |
| truePositive | 0.38 | 0.50 | 0.50 | 0.50 | 0.50 | 0.50 |

### `doc-cleanup-ocr`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| addedWords | 4.41 | 2.12 | 1.64 | 6.08 | 5.12 | 2.22 |
| headings | 5.82 | 6.88 | 7.36 | 8.33 | 9.62 | 6.91 |
| pageMarkersLeft | 2.05 | 3.00 | 2.95 | 3.12 | 3.38 | 3.43 |
| wordRatio | 1.27 | 1.17 | 1.26 | 1.37 | 1.41 | 1.24 |

### `enumeration-breadth`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| fabricatedQuotes | 5.25 | 4.67 | 3.00 | 9.00 | 14.60 | 3.71 |
| itemTypesCovered | 10.12 | 6.33 | 13.04 | 13.32 | 11.20 | 14.88 |
| items | 32.88 | 52.00 | 18.96 | 40.63 | 47.60 | 17.92 |
| quotedItems | 32.88 | 31.00 | 18.74 | 39.95 | 47.60 | 15.08 |

### `grounding-draft`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| bodyWords | 341.38 | 359.88 | 328.43 | — | — | — |
| coverage | 0.59 | 0.62 | 0.56 | — | — | — |
| inventedLinks | 0.25 | 0.38 | 0.04 | — | — | — |
| inventedNames | 0.79 | 0.00 | 0.30 | — | — | — |
| inventedNumbers | 0.00 | 0.00 | 0.00 | — | — | — |

### `lcr-48k`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| abstained | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| citedCorrectly | 0.80 | 0.80 | 0.80 | 0.80 | 0.80 | 0.80 |
| correct | 1.00 | 0.80 | 1.00 | 0.90 | 0.90 | 0.97 |
| incorrect | 0.00 | 0.20 | 0.00 | 0.10 | 0.10 | 0.03 |
| omniscienceIndex | 1.00 | 0.60 | 1.00 | 0.80 | 0.80 | 0.93 |

### `lcr-60k`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| abstained | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| citedCorrectly | 0.80 | 0.80 | 0.80 | 0.80 | 0.80 | 0.80 |
| correct | 0.90 | 0.80 | 1.00 | 0.87 | 1.00 | 1.00 |
| incorrect | 0.10 | 0.20 | 0.00 | 0.13 | 0.00 | 0.00 |
| omniscienceIndex | 0.80 | 0.60 | 1.00 | 0.73 | 1.00 | 1.00 |

### `lcr-80k`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| abstained | 0.00 | — | 0.00 | 0.00 | — | 0.00 |
| citedCorrectly | 0.80 | — | 0.80 | 0.80 | — | 0.80 |
| correct | 0.90 | — | 1.00 | 0.87 | — | 1.00 |
| incorrect | 0.10 | — | 0.00 | 0.13 | — | 0.00 |
| omniscienceIndex | 0.80 | — | 1.00 | 0.73 | — | 1.00 |

### `meeting-brief`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| abstainedCorrectly | 0.67 | 0.00 | 0.33 | 0.60 | 1.00 | 0.83 |
| actions | 4.00 | 1.20 | 2.50 | 3.74 | 3.40 | 3.12 |
| briefWords | 418.14 | 148.40 | 225.00 | 374.83 | 395.60 | 267.83 |
| decisions | 2.43 | 1.00 | 2.12 | 2.78 | 4.20 | 2.25 |
| factRecall | 0.34 | 0.11 | 0.21 | 0.22 | 0.22 | 0.25 |
| factsCovered | 8.29 | 3.00 | 5.38 | 5.35 | 4.80 | 6.04 |
| factsInKey | 24.00 | 23.00 | 23.75 | 23.83 | 21.60 | 23.75 |
| inventedNumbers | 0.14 | 0.00 | 0.00 | 0.30 | 0.00 | 0.08 |
| trapsHit | 0.00 | 0.00 | 0.00 | 0.13 | 0.00 | 0.00 |

### `summary-report`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| sourceTermsCovered | 0.03 | 0.04 | 0.03 | 0.03 | 0.03 | 0.03 |
| summaryWords | 97.00 | 112.75 | 82.62 | 81.83 | 100.88 | 80.25 |
| wordsOverTarget | 10.00 | 24.62 | 0.00 | 1.33 | 11.75 | 0.00 |

### `summary-transcript`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| sourceTermsCovered | 0.26 | 0.23 | 0.24 | 0.22 | 0.24 | 0.20 |
| summaryWords | 93.50 | 84.25 | 75.62 | 72.17 | 79.50 | 74.08 |
| wordsOverTarget | 6.50 | 4.88 | 0.00 | 0.21 | 1.12 | 0.12 |

### `transcript-cleanup-meeting`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| chunkSummaryWords | 51.00 | 34.88 | 42.75 | 48.04 | 46.62 | 41.67 |
| inventedWords | 3.12 | 0.12 | 5.00 | 17.54 | 0.12 | 10.29 |
| rareWordRetention | 0.89 | 0.99 | 0.88 | 0.71 | 0.89 | 0.86 |
| repairedOk | 0.71 | 1.00 | 0.50 | 0.00 | 0.67 | 0.59 |
| wordRatio | 0.79 | 0.97 | 0.72 | 0.56 | 0.90 | 0.74 |

### `transcript-cleanup-memo`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| chunkSummaryWords | 35.75 | 32.25 | 38.88 | 38.79 | 34.38 | 38.88 |
| inventedWords | 4.38 | 0.62 | 0.00 | 8.46 | 0.12 | 0.83 |
| rareWordRetention | 0.97 | 1.00 | 0.98 | 0.99 | 1.00 | 0.99 |
| repairedOk | 0.83 | 1.00 | — | 0.38 | 1.00 | 0.92 |
| wordRatio | 0.80 | 0.92 | 0.84 | 0.74 | 0.97 | 0.86 |

### `verifier-seeded`

| Metric | `chat-27b` | `task-4b` | `think-27b` | `moe-35a3b` | `task-9b` | `moe-35a3b-think` |
| --- | --- | --- | --- | --- | --- | --- |
| defectsCaught | 1.75 | 1.88 | 1.75 | 1.75 | 1.88 | 1.75 |
| defectsMissed | 0.12 | 0.00 | 0.12 | 0.12 | 0.00 | 0.12 |
| falseFlags | 0.00 | 0.25 | 0.00 | 0.04 | 0.12 | 0.00 |
| precision | 1.00 | 0.92 | 1.00 | 0.99 | 0.96 | 1.00 |
| recall | 0.94 | 1.00 | 0.94 | 0.94 | 1.00 | 0.94 |
| soundItems | 1.62 | 1.62 | 1.62 | 1.62 | 1.62 | 1.62 |

## What was measured

Read from the endpoint at run time, not from configuration. Thinking is
reported as observed rather than as requested, because the two have disagreed.

| Model | Weights | Params | Quant | Thinking requested | Thinking observed |
| --- | --- | --- | --- | --- | --- |
| `chat-27b` | — | 27.32 | Q6_K | — | 18/230 items, median 0 hidden tok |
| `task-4b` | Qwen3.5-4B-UD-Q6_K_XL.gguf | 4.21 | Q6_K | off | 15/412 items, median 0 hidden tok |
| `think-27b` | — | 27.32 | Q6_K | — | 386/386 items, median 2012 hidden tok |
| `moe-35a3b` | Qwen3.6-35B-A3B-UD-Q6_K.gguf | 35.51 | Q6_K | — | 19/458 items, median 0 hidden tok |
| `task-9b` | Qwen3.5-9B-Q4_K_M.gguf | 9.20 | Q4_K - Medium | off | 18/404 items, median 0 hidden tok |
| `moe-35a3b-think` | Qwen3.6-35B-A3B-UD-Q6_K.gguf | 35.51 | Q6_K | — | 466/466 items, median 2282 hidden tok |

## How the failures fail

The number that should decide a handoff is **silent**: output that passed every
deterministic check and was still graded unfaithful. A gated failure is the
pipeline working. A silent one is the pipeline not seeing anything wrong.
`unknown` is prose in a judged case that nobody has graded — not a clean bill.

| Model | Gated | Silent | Unknown | Clean |
| --- | --- | --- | --- | --- |
| `chat-27b` | 52 | **5** | 0 | 109 |
| `task-4b` | 55 | **0** | 41 | 60 |
| `think-27b` | 51 | **0** | 41 | 74 |
| `moe-35a3b` | 80 | **5** | 0 | 81 |
| `task-9b` | 57 | **7** | 0 | 92 |
| `moe-35a3b-think` | 62 | **10** | 0 | 94 |

Silent failures on `chat-27b`:
- meeting-brief/meeting-vpp-panel (faithfulness 2)
- meeting-brief/meeting-vpp-dr (faithfulness 3)
- summary-report/report-gemini-dc (faithfulness 2)
- summary-report/report-claude-work (faithfulness 2)
- summary-transcript/raw-asr-piforge (faithfulness 2)

Silent failures on `moe-35a3b`:
- meeting-brief/meeting-vpp-intro (faithfulness 2)
- summary-report/report-arpae-q6 (faithfulness 3)
- summary-report/report-gemini-dc (faithfulness 2)
- summary-report/report-claude-work (faithfulness 2)
- summary-transcript/raw-asr-piforge (faithfulness 2)

Silent failures on `task-9b`:
- meeting-brief/meeting-vpp-panel (faithfulness 2)
- summary-report/report-calnext (faithfulness 3)
- summary-report/report-datacenter (faithfulness 2)
- summary-report/report-gemini-dc (faithfulness 2)
- summary-report/report-claude-work (faithfulness 2)
- summary-report/report-claude-work2 (faithfulness 2)
- summary-transcript/raw-asr-piforge (faithfulness 2)

Silent failures on `moe-35a3b-think`:
- meeting-brief/meeting-lbnl (faithfulness 3)
- summary-report/report-arpae-q6 (faithfulness 2)
- summary-report/report-arpae-q8 (faithfulness 2)
- summary-report/report-claude-dc (faithfulness 2)
- summary-report/report-gemini-dc (faithfulness 3)
- summary-report/report-claude-work (faithfulness 2)
- summary-transcript/transcript-context (faithfulness 3)
- summary-transcript/transcript-retrieval (faithfulness 3)
- summary-transcript/raw-asr-piforge (faithfulness 2)
- transcript-cleanup-memo/raw-asr-piforge (faithfulness 2)

## Cost

**Read the per-attempt column, not wall time.** `--stabilize` repeats the cases a
single item could have decided, and it repeats *different* cases for each model, so
the wall times below cover different amounts of work and are not comparable to each
other. Attempts is what actually ran; fixtures is how many distinct questions were
asked. Dividing wall time by fixtures across models is how a model that repeated
twice as often reads as half as fast.

Wall time is measured on an otherwise idle GPU, so it is a latency figure, not
throughput. Tokens per attempt is the one that scales: a model generating half again
as much finishes a single item faster and a 500-note batch slower. A `task` batch
also pays roughly 6s of router swap whenever it alternates with `embed`.

| Model | Fixtures | Attempts | Generated tokens | Tokens/attempt | **s/attempt** | Items/min | Prefill tok/s | Decode tok/s | Hidden reasoning | Wall time |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `chat-27b` | 166 | 230 | 124,225 | 540 | **10.8** | 5.5 | 1355 | 65.7 | 8,057 | 2495s |
| `task-4b` | 156 | 412 | 353,927 | 859 | **9.5** | 6.3 | 6044 | 127.8 | 7,951 | 3902s |
| `think-27b` | 166 | 386 | 1,224,957 | 3,173 | **50.2** | 1.2 | 1366 | 67.2 | 1,022,430 | 19376s |
| `moe-35a3b` | 166 | 458 | 274,827 | 600 | **6.1** | 9.8 | 3995 | 128.8 | 7,424 | 2792s |
| `task-9b` | 156 | 404 | 191,367 | 474 | **8.0** | 7.5 | 4089 | 112.4 | 7,215 | 3221s |
| `moe-35a3b-think` | 166 | 466 | 1,629,784 | 3,497 | **36.1** | 1.7 | 3958 | 125.9 | 1,411,114 | 16836s |

## Graded quality

Blind means, 1-5.

| Model | Voice | Faithfulness | Coverage | Usability | Graded |
| --- | --- | --- | --- | --- | --- |
| `chat-27b` | 3.73 | 4.08 | 4.11 | 3.69 | 64 |
| `moe-35a3b` | 3.27 | 3.83 | 3.80 | 3.44 | 64 |
| `moe-35a3b-think` | 3.73 | 4.28 | 4.08 | 3.88 | 64 |
| `task-9b` | 3.70 | 4.20 | 3.75 | 3.38 | 64 |

## Stage routing

The model that should run each stage, ranked by capability,
and what running it there costs.

Ratios carry the absolute per-item difference beside them, because a ratio on a five-second
base makes a cheap upgrade look expensive: "10.5x slower" and "+15s per item" are the same
fact and lead to opposite decisions.

| Stage | Case | Runs on | vs baseline |
| --- | --- | --- | --- |
| `vault-curator` / abstention | `abstention-closed-book` | `chat-27b` | baseline |
| `vault-transcripts` / abstention | `abstention-grounded` | `moe-35a3b` | 7.5x faster (−1.9s/item) |
| `vault-capture` / segmentation | `braindump-split` | `moe-35a3b-think` | 3.5x slower (+20.5s/item) |
| `vault-organizer` / categorization | `classify-hard` | nothing cleared it | — |
| `vault-organizer` / categorization | `classify-notes` | `think-27b` | 20.3x slower (+36.2s/item) |
| `vault-connections` / pair-judgment | `connection-judgment` | `task-4b` | 3.3x faster (−1.2s/item) |
| `document-ingest` / document-cleanup | `doc-cleanup-ocr` | nothing cleared it | — |
| `literature-extraction` / enumeration | `enumeration-breadth` | nothing cleared it | — |
| `vault-capture` / grounding | `grounding-draft` | nothing cleared it | — |
| `vault-projects` / long-context | `lcr-48k` | `chat-27b` | baseline |
| `vault-projects` / long-context | `lcr-60k` | `moe-35a3b-think` | 6.8x slower (+10.5s/item) |
| `vault-projects` / long-context | `lcr-80k` | `moe-35a3b-think` | 5.6x slower (+10.7s/item) |
| `vault-transcripts` / long-form-reasoning | `meeting-brief` | nothing cleared it | — |
| `vault-transcripts` / summarization | `summary-report` | nothing cleared it | — |
| `vault-transcripts` / summarization | `summary-transcript` | nothing cleared it | — |
| `vault-transcripts` / faithful-cleanup | `transcript-cleanup-meeting` | `task-9b` | 1.4x faster (−8.1s/item) |
| `vault-transcripts` / faithful-cleanup | `transcript-cleanup-memo` | `task-9b` | 1.7x faster (−2.5s/item) |
| `forge_verify` / verification | `verifier-seeded` | `task-9b` | 1.0x faster (−0.1s/item) |

## Routing recommendation

### `task-4b`

**Safe to route here** (every gate clean, quality held):
- `connection-judgment` — 16/16 vs 14/16
- `lcr-60k` — 8/10 vs 9/10
- `verifier-seeded` — 6/8 vs 7/8

**Not decided:**
- `braindump-split` — gates hold up (5/8 vs 4/8), but the quality was never graded
- `classify-hard` — 2/8 vs 3/8; neither model does this well enough to route anywhere
- `classify-notes` — 4/8 vs 3/8; neither model does this well enough to route anywhere
- `doc-cleanup-ocr` — 3/8 vs 2/8; neither model does this well enough to route anywhere
- `grounding-draft` — gates hold up (5/8 vs 3/8), but the quality was never graded
- `lcr-80k` — not run: needs about 82,000 tokens of context, task-4b has 65,538
- `summary-transcript` — gates hold up (8/8 vs 8/8), but the quality was never graded
- `transcript-cleanup-meeting` — gates hold up (7/8 vs 1/8), but the quality was never graded
- `transcript-cleanup-memo` — gates hold up (5/8 vs 2/8), but the quality was never graded

**Keep on the baseline:**
- `abstention-closed-book` — 5/12 vs 10/12 items clean, 42% below the baseline
- `abstention-grounded` — 10/12 vs 12/12 items clean, 17% below the baseline
- `enumeration-breadth` — 1/8 vs 3/8 items clean, 25% below the baseline
- `lcr-48k` — 8/10 vs 10/10 items clean, 20% below the baseline
- `meeting-brief` — 2/8 vs 6/8 items clean, 50% below the baseline
- `summary-report` — 6/8 vs 8/8 items clean, 25% below the baseline

### `think-27b`

**Safe to route here** (every gate clean, quality held):
- `abstention-grounded` — 12/12 vs 12/12
- `classify-notes` — 5/8 vs 3/8
- `lcr-48k` — 10/10 vs 10/10
- `lcr-60k` — 10/10 vs 9/10
- `lcr-80k` — 10/10 vs 9/10
- `verifier-seeded` — 7/8 vs 7/8

**Not decided:**
- `abstention-closed-book` — 6 item(s) flipped between attempts (c1, c12, c2); the result is not repeatable
- `braindump-split` — gates hold up (7/8 vs 4/8), but the quality was never graded
- `classify-hard` — 5 item(s) flipped between attempts (classify-demand-response, classify-lecture, classify-mycology); the result is not repeatable
- `connection-judgment` — 1 item(s) flipped between attempts (figure-ian-hacking+classify-piforge-memo); the result is not repeatable
- `doc-cleanup-ocr` — 4 item(s) flipped between attempts (manual-ocr-chunk#2, manual-ocr-chunk#3, manual-ocr-chunk#4); the result is not repeatable
- `enumeration-breadth` — 2 item(s) flipped between attempts (report-calnext, report-datacenter); the result is not repeatable
- `grounding-draft` — 4 item(s) flipped between attempts (braindump-todo, braindump-voice, braindump-weather); the result is not repeatable
- `meeting-brief` — gates hold up (5/8 vs 6/8), but the quality was never graded
- `summary-report` — gates hold up (8/8 vs 8/8), but the quality was never graded
- `summary-transcript` — gates hold up (8/8 vs 8/8), but the quality was never graded
- `transcript-cleanup-meeting` — 0/8 vs 1/8; neither model does this well enough to route anywhere
- `transcript-cleanup-memo` — gates hold up (8/8 vs 2/8), but the quality was never graded

### `moe-35a3b`

**Safe to route here** (every gate clean, quality held):
- `abstention-grounded` — 12/12 vs 12/12
- `lcr-48k` — 9/10 vs 10/10

**Not decided:**
- `braindump-split` — 3 item(s) flipped between attempts (braindump-requirements, braindump-todo, braindump-voice); the result is not repeatable
- `classify-hard` — 2 item(s) flipped between attempts (classify-lecture, classify-mycology); the result is not repeatable
- `classify-notes` — 3 item(s) flipped between attempts (classify-calnext, classify-piforge-memo, classify-reification); the result is not repeatable
- `connection-judgment` — 1 item(s) flipped between attempts (figure-ian-hacking+classify-piforge-memo); the result is not repeatable
- `doc-cleanup-ocr` — 2 item(s) flipped between attempts (manual-ocr-chunk#2, manual-ocr-chunk#7); the result is not repeatable
- `enumeration-breadth` — 1 item(s) flipped between attempts (report-datacenter); the result is not repeatable
- `lcr-60k` — 1 item(s) flipped between attempts (q4); the result is not repeatable
- `lcr-80k` — 1 item(s) flipped between attempts (q4); the result is not repeatable
- `meeting-brief` — 3 item(s) flipped between attempts (meeting-aio, meeting-kickoff, meeting-vpp-panel); the result is not repeatable
- `transcript-cleanup-meeting` — 0/8 vs 1/8; neither model does this well enough to route anywhere
- `transcript-cleanup-memo` — 3 item(s) flipped between attempts (raw-asr-piforge, transcript-export, transcript-vaultmanager); the result is not repeatable
- `verifier-seeded` — 4 item(s) flipped between attempts (packet-1, packet-2, packet-4); the result is not repeatable

**Keep on the baseline:**
- `abstention-closed-book` — 6/12 vs 10/12 items clean, 33% below the baseline
- `grounding-draft` — 0/8 vs 3/8 items clean, 38% below the baseline
- `summary-report` — 8/8 vs 8/8 on the gates, but 3 item(s) passed every check and were still graded unfaithful. Nothing downstream would catch that.
- `summary-transcript` — 8/8 vs 8/8 on the gates, but 1 item(s) passed every check and were still graded unfaithful. Nothing downstream would catch that.

### `task-9b`

**Safe to route here** (every gate clean, quality held):
- `abstention-grounded` — 11/12 vs 12/12
- `braindump-split` — 6/8 vs 4/8
- `connection-judgment` — 16/16 vs 14/16
- `lcr-48k` — 9/10 vs 10/10
- `lcr-60k` — 10/10 vs 9/10
- `transcript-cleanup-meeting` — 5/8 vs 1/8
- `transcript-cleanup-memo` — 7/8 vs 2/8
- `verifier-seeded` — 7/8 vs 7/8

**Not decided:**
- `classify-hard` — 2/8 vs 3/8; neither model does this well enough to route anywhere
- `classify-notes` — 2/8 vs 3/8; neither model does this well enough to route anywhere
- `lcr-80k` — not run: needs about 82,000 tokens of context, task-9b has 65,792

**Keep on the baseline:**
- `abstention-closed-book` — 7/12 vs 10/12 items clean, 25% below the baseline
- `doc-cleanup-ocr` — 0/8 vs 2/8 items clean, 25% below the baseline
- `enumeration-breadth` — 0/8 vs 3/8 items clean, 38% below the baseline
- `grounding-draft` — 0/8 vs 3/8 items clean, 38% below the baseline
- `meeting-brief` — 2/8 vs 6/8 on the gates, but 1 item(s) passed every check and were still graded unfaithful. Nothing downstream would catch that.
- `summary-report` — 7/8 vs 8/8 on the gates, but 5 item(s) passed every check and were still graded unfaithful. Nothing downstream would catch that.
- `summary-transcript` — 8/8 vs 8/8 on the gates, but 1 item(s) passed every check and were still graded unfaithful. Nothing downstream would catch that.

### `moe-35a3b-think`

**Safe to route here** (every gate clean, quality held):
- `braindump-split` — 7/8 vs 4/8
- `lcr-60k` — 10/10 vs 9/10
- `lcr-80k` — 10/10 vs 9/10
- `verifier-seeded` — 7/8 vs 7/8

**Not decided:**
- `abstention-closed-book` — 3 item(s) flipped between attempts (c2, c6, c8); the result is not repeatable
- `abstention-grounded` — 1 item(s) flipped between attempts (g5); the result is not repeatable
- `classify-hard` — 2 item(s) flipped between attempts (classify-demand-response, classify-website); the result is not repeatable
- `classify-notes` — 2 item(s) flipped between attempts (classify-kuhn, classify-tomatoes); the result is not repeatable
- `connection-judgment` — 1 item(s) flipped between attempts (figure-ian-hacking+classify-piforge-memo); the result is not repeatable
- `doc-cleanup-ocr` — 6 item(s) flipped between attempts (manual-ocr-chunk, manual-ocr-chunk#2, manual-ocr-chunk#3); the result is not repeatable
- `enumeration-breadth` — 2 item(s) flipped between attempts (report-calnext, report-datacenter); the result is not repeatable
- `lcr-48k` — 1 item(s) flipped between attempts (q8); the result is not repeatable
- `meeting-brief` — 4 item(s) flipped between attempts (meeting-aio, meeting-vpp-dr, meeting-vpp-panel); the result is not repeatable
- `transcript-cleanup-meeting` — 2 item(s) flipped between attempts (transcript-brattle#1, transcript-vpp-chunk#4); the result is not repeatable
- `transcript-cleanup-memo` — 5 item(s) flipped between attempts (transcript-context, transcript-knowledgebase, transcript-retrieval); the result is not repeatable

**Keep on the baseline:**
- `grounding-draft` — 0/8 vs 3/8 items clean, 38% below the baseline
- `summary-report` — 8/8 vs 8/8 on the gates, but 5 item(s) passed every check and were still graded unfaithful. Nothing downstream would catch that.
- `summary-transcript` — 8/8 vs 8/8 on the gates, but 3 item(s) passed every check and were still graded unfaithful. Nothing downstream would catch that.

