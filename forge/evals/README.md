# Model evaluation suite

Which skill stages can run on a different model, and what breaks when they do.

Every case drives a stage a pi-forge skill actually delegates: it builds the
prompt with that skill's own builder and scores the reply with that skill's own
gate. Nothing here reimplements a prompt or a check, so a case that passed its
gate is a case production would have accepted.

```bash
python3 forge/evals/run.py freeze
python3 forge/evals/run.py doctor --model task-9b
python3 forge/evals/run.py run --model task-9b
python3 forge/evals/run.py report --models chat-27b,task-9b
```

Twelve cases, 47 items, roughly 3-8 minutes per model depending on how fast it
generates.

## Commands

| | |
| --- | --- |
| `freeze [--check] [--repin]` | materialize fixtures from the vault; `--check` reports drift, `--repin` accepts it as the new baseline |
| `doctor --model ID` | probe one endpoint before spending a run on it |
| `run --model ID [--cases a,b] [--repeat N]` | run the suite, write `results/<model>/` |
| `judge --models a,b,c` | build the blind comparison bundle |
| `score --verdicts PATH` | merge graded verdicts and unblind them |
| `report [--models a,b] [--baseline ID]` | the comparison table and the routing call |

## What is measured

| Case | Dimension | Stage under test |
| --- | --- | --- |
| `classify-notes` | categorization | `vault-organizer` classifier, eight notes with unambiguous filing |
| `classify-hard` | categorization | the same, where the schema rather than the topic decides |
| `transcript-cleanup-memo` | faithful cleanup | `vault-transcripts` cleanup, single speaker |
| `transcript-cleanup-meeting` | faithful cleanup | the same, four diarized speakers |
| `summary-transcript` | summarization | one paragraph, ≤120 words, from speech |
| `summary-report` | summarization | the same contract over a document forty times longer |
| `doc-cleanup-ocr` | document cleanup | `document-ingest` structural cleanup of corrupted PDF extraction |
| `grounding-draft` | grounding | `vault-capture` draft, scored for invented names and links |
| `braindump-split` | segmentation | how many notes a braindump is |
| `enumeration-breadth` | enumeration | `literature-extraction`: how many of fifteen item types get covered |
| `connection-judgment` | pair judgment | should these two notes be linked, against real `related:` links |
| `verifier-seeded` | verification | can this model do `think`-tier review, on a set with planted defects |

Six of the twelve are also judged: gates cannot settle whether a cleaned
transcript still sounds like the person who spoke it.

## Testing a change before you make it

A **variant** is a declarative patch in `variants/*.json` applied to a case's
built items — response format, token budget, temperature, a system-prompt suffix
or a clause stripped out. It never edits a skill. That is the point: it answers
"would this prompt be better" *before* anything in `forge/skills` changes.

```bash
python3 forge/evals/run.py run --model task-4b --variant explicit-json-schema
python3 forge/evals/run.py compare --model task-4b --to explicit-json-schema
```

`compare` is paired — both arms ran the same frozen fixtures — and reports metric
deltas alongside an exact two-sided McNemar on the pass/fail pairs. It excludes
items that flipped on their own, since an item that changes without a change
cannot testify about one.

**Read the metrics, not the pass rate.** Calibrating against a change with a
known effect proved why. `variants/enumeration-clause-removed.json` strips the
clause `docs/service-split-handoff.md` §2.1 measured as worth 17 items / 4 types
→ 41 / 12. Removing it moved `itemTypesCovered` from **9 to 4** and item count
from **66 to 29** — the documented degradation, reproduced. The pass/fail
comparison saw one discordant pair and reported **p = 1.0, underpowered**. A gate
is a floor; the metric is the instrument.

That calibration is the prerequisite for trusting any variant result. If the
suite cannot see a change known to matter, a null result from it means nothing.

## Reading a result

Three tiers, in decreasing order of how much they should be trusted.

0. **Stability** comes first. Measured on this stack, **8 of 12 cases moved
   between two runs of the same model** with nothing changed. `run --stabilize 3`
   runs once, computes which cases a single item could have decided, and repeats
   only those. An item that flips between attempts never clears the routing gate,
   and a case with fewer than 8 fixtures is labelled indicative only.
1. **Gates** are the skills' own deterministic checks. A gate failure is a fact.
2. **Metrics** are ground-truth comparisons — destination match, item-type
   coverage, flag precision and recall. Sound, but several fixtures have more
   than one defensible answer, which is why the report compares against the
   baseline rather than against perfection.
3. **Judged scores** are a reader's opinion, blind to which model wrote what.

`repairedOk` appears on cases that model a skill's corrective retry. First-shot
quality is what a model is like; post-repair is what the pipeline would deliver.
Both are reported because they answer different questions.

**Failure severity is what "conservative" means.** The report splits failures
into `gated` (a deterministic check caught it — the pipeline working), `silent`
(passed every check and a grader still marked it unfaithful — the failure nothing
downstream sees), and `unknown` (prose in a judged case nobody graded, which is
not a clean bill). A silent failure vetoes a handoff; gated ones are tolerated
within the usual margin. On the first graded run both models had **zero silent
failures** — every unfaithful output was also gate-caught, which is evidence the
gate layer is sound rather than evidence about either model.

Every failure keeps its reply under `raw`. A model that returned `[]` to a real
report and one that returned malformed JSON both read as "failed" in the
summary, and only the text tells them apart.

## Things about this deployment that change how results read

- **`response_format: {"type": "json_object"}` is a no-op here.** Verified
  2026-08-03 against both `:8004` and `:8007`: a request that sets it and asks
  for plain prose gets plain prose. Every skill that passes it is relying on the
  model following the instruction, not on a grammar backstopping it. This is why
  a model can produce a perfectly good summary and still fail its case.
- **`task` shares a router with `embed`, `ocr`, and `rank` at
  `MODEL_ROUTER_MAX=1`.** Measured swap: ~6 s from `embed` to `task`. No case
  calls the embeddings endpoint, and any embedding-derived input is precomputed
  at freeze time, or the suite would be timing the router.
- **`task` has a 32,768-token context**, a quarter of the `chat` slot. Every
  prompt in the suite is checked against the smallest ceiling in `models.json`
  by `tests/test_evals.py`, so a case that overruns is caught before a run.
- **`:8008` reasons in *visible* content.** There is no think block to strip, so
  a `max_tokens` sized for the answer alone gets a reply that is all preamble and
  ends mid-thought. Measured at ~1,900 tokens of preamble on both a
  classification and a pair judgment — near enough constant regardless of task
  size, which is why `outputHeadroom` is added rather than multiplied. Without
  it, `think-27b` scored 0 on six cases for reasons that had nothing to do with
  the model. Production sets no `max_tokens` at all on the think tier; the cap
  here is a safety rail against a runaway repetition loop, not a fidelity claim.
- **Temperature 0 is not determinism.** One item flipped between a parse failure
  and a pass across two runs of the same model. Use `--repeat 3` on anything a
  decision rests on.

## What a result records about the model

A model id in `models.json` is a label someone typed; the weights behind a port
can be swapped without it. So every result document carries a `served` block
read from the endpoint at run time: parameter count, quantization, size, context,
the gguf path, the full launch argv, and the parsed flags — plus the stack's own
preset text where the endpoint exposes it. A run is self-describing.

An entry can also assert its identity with `expectParams` and/or
`expectModelPath`. `run` and `doctor` compare those against what is actually
loaded and refuse to start on a mismatch (`--allow-mismatch` overrides).
Parameter counts are compared with 10% tolerance, so a requant does not trip it
but a different model does.

This exists because it was missing once: a run recorded as `task-9b` was later
found to have possibly been served by a 4B, with no way to tell after the fact.
Results predating this cannot be re-attributed — treat their model labels as
unconfirmed.

Two entries may name the same URL when a router swaps weights behind it; only
whichever is loaded will pass its check.

## Fixtures

Real vault notes, pinned by sha256, materialized into the gitignored `.frozen/`.
This repository is public and the notes are not, so only the pointer is
committed. `run` refuses to start on a drifted fixture: the vault moves, and a
benchmark that moves with it compares nothing.

`freeze` hard-refuses anything under `harness.DENIED_PREFIXES` — therapy notes,
health records, personal-context cards, and the folder holding live software
licence keys. `tests/test_evals.py` asserts no fixture points into them.

## Adding a case

One module in `cases/`, named for the case with underscores. It exposes:

```python
DIMENSION = "..."          # grouping in the report
SKILL = "..."              # whose stage this is
JUDGE = False              # does its output go to the blind bundle

def items() -> list[dict]                       # {id, messages, max_tokens?, ...}
def score(item, content, record=None) -> dict   # {ok, gates, metrics, notes, output?}
def repair(item, content, scored) -> messages   # optional: the skill's corrective retry
def judge_context(item_id) -> dict              # required when JUDGE; {instruction, source, reference}
```

Import the skill with `harness.load_skill("vault-capture")` and use its real
prompt constants and gate functions. If you find yourself copying a prompt into
a case, the case is measuring the wrong thing.

Modules starting with `_` are shared helpers, not cases.

## Adding a model

An entry in `models.json` with `url`, `model`, and `contextTokens`. Then, for a
reasoning backend, one of two things depending on where its reasoning goes:

- into a separate `reasoning_content` field (`:8007`) → set
  `chatTemplateKwargs: {"enable_thinking": false}`, or the reply arrives with
  empty `content` and nothing to parse. `doctor` names that failure specifically.
- into visible `content` (`:8008`) → set `outputHeadroom` to cover the preamble,
  or every reply ends mid-thought.

Hosted models take `apiKeyEnv`, and the key is read from the environment, never
stored here.

Run `doctor` before spending a run on a new entry, and check the first case's
`finishReason` — a case that truncated is a budget problem, not a result.

The suite deliberately ignores `connectedServices`: results that depend on local
settings are not comparable between runs or between machines.

## Known divergence

`forge/skills/project-extraction/scripts/project-extraction.py` re-implements
the service-resolution ladder locally instead of calling `forge_llm`, so it does
not pick up `contextTokens` or `chatTemplateKwargs` and cannot be pointed at a
model with either. No case covers it.
