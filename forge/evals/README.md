# Model evaluation suite

Which skill stages can run on a different model, and what breaks when they do.

Every case drives a stage a pi-forge skill actually delegates: it builds the
prompt with that skill's own builder and scores the reply with that skill's own
gate. Nothing here reimplements a prompt or a check, so a case that passed its
gate is a case production would have accepted.

```bash
python3 forge/evals/run.py freeze
python3 forge/evals/run.py models                     # what each endpoint is serving now
python3 forge/evals/run.py doctor --model task-4b
python3 forge/evals/run.py run --model task-4b        # the standard suite
python3 forge/evals/run.py report --models chat-27b,task-4b
```

Eighteen cases, 166 items. `--suite quick` is a few minutes, `standard` is the
default, `full` adds the cases whose cost only makes sense when a decision rests
on them. Rough single-pass wall time on this deployment:

| | quick | standard | full |
| --- | --- | --- | --- |
| `chat-27b` | ~4 min | ~35 min | ~55 min |
| `task-4b` | ~3 min | ~25 min | ~35 min (two rungs n/a) |
| `think-27b` | ~12 min | ~90 min | ~2 h |

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
| `meeting-brief` | long-form reasoning | a whole two-hour meeting read in one call, against a reference key |
| `lcr-48k` / `lcr-80k` / `lcr-110k` | long context | one answer from two documents, at three distances |
| `abstention-grounded` | abstention | half the questions the source does not answer |
| `abstention-closed-book` | abstention | knowledge against confabulation, with no source at all |

Seven are also judged: gates cannot settle whether a cleaned transcript still
sounds like the person who spoke it.

### Suites and applicability

A case declares `TIER` (`quick`, `standard`, `full`) and the suites nest. An
explicit `--cases` list is obeyed exactly and ignores `--suite`, because a
selection flag that silently drops something you named is worse than no flag.

A case also declares what context it needs. When a model cannot fit one, the
case is **skipped and recorded as skipped** — `n/a` in the report, never `0/8`.
"This model has 65k and the case needs 113k" and "this model got it wrong" are
different findings, and conflating them is how a suite reports a small model
failing at something nobody put to it.

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

### What the calibration runs found

Each new case has a variant that strips something it claims to measure. Run them
before trusting a null from that case.

Run on `chat-27b`, 2026-08-04:

| Variant | Case | Effect | Verdict |
| --- | --- | --- | --- |
| `abstention-permission-removed` | `abstention-closed-book` | Omniscience Index **+0.72 → +0.06**; 5 items of 12 | **detects** |
| `long-context-no-abstention` | `lcr-48k` | Index **1.00 → 0.70**; exactly 2 items — the number of unanswerable questions in a rung | **detects** |
| `enumeration-clause-removed` | `enumeration-breadth` | `itemTypesCovered` **10.6 → 8.1**, down on 7 of 8 items; `items` 29.6 → 27.1 | **detects** |
| `meeting-brief-no-abstention` | `meeting-brief` | `abstainedCorrectly` 0.50 → 0.375; right direction, one item | **weak** |

Note the pass/fail column in each `compare` said `p = 1.0, underpowered` while the
metrics moved by a third or more. That is the point of leading with metrics.

`meeting-brief`'s abstention half is under-powered by construction:
`abstainedCorrectly` is only defined for items where the model produced an action
matching a `notStated` entry, and the keys carry one or two of those each, so the
statistic has a base of about four. Treat `factRecall`, `inventedNumbers` and
`trapsHit` as that case's real instruments, and read the abstention number as a
hint. Adding `notStated` entries would fix it and means re-reading the
transcripts.

Two things those runs taught, both of which cost a wrong conclusion first:

**A variant must strip an instruction, not an explanation.** The first
`abstention-permission-removed` removed the sentence *"a confident wrong answer
is worse than no answer"* and moved nothing across 12 items. That sentence
justifies the rule; the rule itself — *"write 'I don't know' whenever you are not
confident"* — was still in the prompt. Pointing the variant at the rule dropped
the index by two thirds immediately.

**A variant must strip something the prompt is the only source of.** The
long-context calibration originally removed the warning that several documents
describe the same project at different times, and moved nothing on two models at
three sizes. Four of the ten questions say *"the earlier report"* or *"the later
report"* in their own text, so the warning was redundant and removing it left the
instruction standing. The replacement strips the permission to answer "Not
stated", and exactly two items move — the exact number of unanswerable questions
in a rung.

The pattern in both: **a null result is a claim about the variant before it is a
claim about the case.** Check what the prompt still says before concluding the
case is blind.

## The three newest cases

**`meeting-brief`** is the one exception to the rule above, and knowingly so.
`vault-transcripts` chunks a meeting at 12,000 characters and summarizes it from
the chunk summaries, so no stage in pi-forge ever reads a long meeting whole —
which is exactly the gap. Its prompt is therefore assembled here rather than
imported, though the faithfulness rules and the "Unassigned / Not stated" clause
are pulled out of the skill's own prompt and the case refuses to build if they
stop being there. **A pass here is evidence about the model, not the guarantee
the other cases give that production would have accepted the output.** If the
case earns its keep, the prompt should graduate into the skill.

It scores against reference keys — 190 facts and 32 traps across eight meetings,
each fact carrying the verbatim line that supports it, so the key is checkable
rather than merely trusted. Two tests enforce that: every quote must appear in
its transcript, and must share a content word with the claim it is attached to.
Five keys are committed; three cover internal meetings that name real people and
live in a gitignored `expectations/.private/`, exactly as `.frozen/` already
works. A clone without them runs five items and says so.

**The `lcr-*` rungs** hold the evidence constant and vary only the distance
between the two documents that carry it, so a drop from 48k to 110k is
attributable to distance rather than to having been shown less. The padding is
same-project on purpose: unrelated filler would let a model find the answer by
looking for the only document that mentions the subject.

**The `abstention-*` pair** scores the way AA-Omniscience does — a wrong answer
costs exactly what a right one earns, and declining scores zero. Accuracy would
rank a confident guesser above a model that admits ignorance, which is backwards
for every skill here. The grounded half should drive routing; the closed-book
half explains its results, since a model can be scrupulous about a source it was
given and confabulate freely without one.

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
- **`task` has a 65,538-token context**, half the `chat` slot. It was recorded
  as 32,768 for months after the backend moved, which quietly under-budgeted
  every task-tier prompt; `add-model` exists so that number is read off the
  server instead of remembered. Every case that a model *can* run is checked
  against that model's ceiling by `tests/test_evals.py`.
- **Prefill is paid once per distinct prompt.** Cases that share a long prefix
  and vary only the tail — the long-context rungs, `abstention-grounded` — cost
  one prefill for the set. Measured: a 48k rung prefills in 33 s on `chat-27b`
  and then answers each question in under two. This is the same byte-stability
  discipline `docs/service-split-handoff.md` §2.2 requires of every skill.
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

### The source archive

The vault is a working notebook. Notes get filed, renamed and reclassified, and
a fixture whose note has moved is a case that cannot run — four of them were
already unreachable because `vault-organizer` filed them somewhere else.

```bash
python3 forge/evals/run.py archive          # copy every fixture's source
python3 forge/evals/run.py archive --check  # verify it against the pinned hashes
```

The archive lives at `~/.pi-forge/eval-sources` (override with
`FORGE_EVAL_ARCHIVE`), holds about 2 MB, and stores the **source** bytes rather
than the excerpted ones, so a fixture can still be re-excerpted or re-pinned
from it. `freeze` reads the vault first and falls back to the archive, marking
any fixture it had to fall back for — the vault always wins when it still has
the note, so the archive can never mask a deliberate edit.

It is **outside the repository**, not gitignored inside it: these are real
notes, and a gitignore is one `git add -f` away from publishing them.
`archive_root` refuses a path inside either the repository or the vault, since a
backup inside the thing it is backing up is not one. The deny-list applies here
too — a backup must not become the route by which refused material re-enters.

`freeze` hard-refuses anything under `harness.DENIED_PREFIXES` — therapy notes,
health records, personal-context cards, and the folder holding live software
licence keys. `tests/test_evals.py` asserts no fixture points into them. The
list covers the report tree as well as the transcript tree: `10.04 Report/
Administration/Health` holds surgical records, and the denial has to follow the
material rather than the folder name it was first written against.

A path rule is not the whole of it. `meeting-kickoff` is excerpted from the call
to order because its first eight minutes are pre-meeting small talk carrying
third-party medical detail — a colonoscopy, a medication reaction, childbirth —
about people who are not the vault owner. Nothing in the deny-list would have
caught that, and none of it is meeting content.

`freeze` also reports **orphans**: a file in `.frozen/` that no entry points at.
It is left over from a fixture set that has moved on, and nothing re-checks it,
so a case that still names it would read unpinned content outside the drift
check. Reported, never deleted.

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

Do not write the entry by hand:

```bash
python3 forge/evals/run.py add-model --url http://llms:8007/v1 --model task --id task-9b --write
```

It reads `contextTokens`, `expectParams`, `expectQuant`, `expectModelPath` and
`sizeGiB` off the live endpoint, and probes where the reasoning goes. Hand-written
entries are how the registry rots: `contextTokens` was wrong by half for months,
and `expectParams` was asserted by two entries and enforced on neither.

`run.py models` then shows every entry against what each endpoint is serving
right now, and which are runnable. Several entries may name one URL — the task
router and the primary backend both swap weights behind a fixed port — so
swapping weights means changing them in the llm-stack UI and re-running `models`
to confirm. The suite never writes to the stack.

The fields, if you are reading one:

- into a separate `reasoning_content` field (`:8007`) → set
  `chatTemplateKwargs: {"enable_thinking": false}`, or the reply arrives with
  empty `content` and nothing to parse. `doctor` names that failure specifically.
- into visible `content` (`:8008`) → set `outputHeadroom` to cover the preamble,
  or every reply ends mid-thought.

Hosted models take `apiKeyEnv`, and the key is read from the environment, never
stored here. `tier`, `family`, `sizeGiB` and `coResident` are read by the report
only; `coResident` means the weights fit in the VRAM left over while the bulk
tier is loaded, so the model could serve a stage *alongside* it rather than
instead of it.

An entry should assert an identity that its endpoint can actually check. The
proxy ports report parameter count and quantization but no launch arguments, so
`expectModelPath` can never fire there; the router ports report argv. When
nothing could be checked, `run` says so and labels the results unconfirmed
rather than refusing — and the warning rides on the result document, because the
console scrolls away and the numbers outlive it.

Run `doctor` before spending a run on a new entry, and check the first case's
`finishReason` — a case that truncated is a budget problem, not a result.

The suite deliberately ignores `connectedServices`: results that depend on local
settings are not comparable between runs or between machines.

## Known divergence

`forge/skills/project-extraction/scripts/project-extraction.py` re-implements
the service-resolution ladder locally instead of calling `forge_llm`, so it does
not pick up `contextTokens` or `chatTemplateKwargs` and cannot be pointed at a
model with either. No case covers it.
