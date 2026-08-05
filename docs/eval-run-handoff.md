# Handoff: running the full eval suite

For an agent driving `forge/evals` through its three suites. Ellie starts each
stage manually, so **stop and report after each one** — do not roll on to the
next suite on your own.

Read `forge/evals/README.md` first; it is the contract. This covers what to run,
in what order, and what will mislead you about the results.

---

## The question you are answering

Which pi-forge stages can move off the 27B, and which cannot. Every case drives
a stage a skill actually delegates, so a gate failure is a fact about production
rather than about a benchmark.

Two numbers decide it, in this order:

1. **Stability.** An item that flips between attempts of the same model never
   clears. Measured on this stack, `abstention-closed-book` has one genuinely
   unstable item and that is a finding, not noise to average away.
2. **Silent failures** — output that passed every deterministic gate and a
   grader still marked unfaithful. A gated failure is the pipeline working; a
   silent one is nothing seeing anything wrong. One vetoes a handoff.

Everything else — pass rates, metrics, speed — is read against the **baseline**
(`chat-27b`), never against perfection. Several fixtures have two defensible
answers, and demanding a clean sweep would let those veto every case.

---

## Before you start

```bash
cd /Users/ellie/Documents/GitHub/pi-forge
python3 -m pytest forge/evals/tests -q          # expect 107 passed
python3 forge/evals/run.py freeze --check       # expect 0 needing attention
python3 forge/evals/run.py archive --check      # expect 0 needing attention
python3 forge/evals/run.py models               # which entries are runnable now
```

`freeze --check` reports one **orphan** (`transcript-dc-meeting`). That is
informational and does not block a run. If it reports anything `missing`,
`drifted` or `stale`, stop and say so — do not reach for `--allow-drift`, which
measures against fixtures that are not the pinned ones.

Runnable today: **`chat-27b`** (baseline), **`task-4b`**, **`think-27b`**.
`task-9b`, `moe-35a3b` and `chat-27b-q4` are registered but their weights are
not loaded; `run` will refuse them, which is correct. Loading them is a manual
step in the llm-stack UI — the suite never writes to the stack.

### Never do these

- **`--allow-mismatch`.** It runs anyway when the endpoint is serving weights the
  entry does not name, and the result is labelled with a model that did not
  produce it. This is the failure the whole `served` block exists to prevent.
- **`freeze --repin`** unless Ellie asks. It makes every earlier result
  incomparable with every later one.
- Concurrent runs against the same endpoint. They contend for the GPU and the
  timings become meaningless.

---

## Stage 1 — `quick` (~5 minutes for all three models)

Two cases, 24 items: `abstention-grounded` and `abstention-closed-book`. Gates
only, no long inputs. This is the smoke test — it proves the endpoints, the
fixtures and the scoring all work before anything expensive runs.

```bash
python3 forge/evals/run.py run --model chat-27b  --suite quick --stabilize 3
python3 forge/evals/run.py run --model task-4b   --suite quick --stabilize 3
python3 forge/evals/run.py run --model think-27b --suite quick --stabilize 3
python3 forge/evals/run.py report --models chat-27b,task-4b,think-27b
```

**What to report:**

- The Omniscience Index per model on both cases, and the split between the
  grounded and closed-book halves. The gap between them is the finding: a model
  can be scrupulous about a source it was given and confabulate freely without
  one.
- Any unstable items, by id, with what the model said on each attempt.
- Whether `think-27b` finished at all, and its `finishReason`s.

**Expected, from runs already on disk** (`chat-27b` and `task-4b`, `--repeat 3`):

| | `abstention-grounded` | `abstention-closed-book` |
| --- | --- | --- |
| `chat-27b` | 12/12 | 10/12, index +0.72 |
| `task-4b` | 10/12 | 4/12 |

If `chat-27b` comes in far below that, something changed — say so rather than
carrying on. `c4` (a nonexistent ASHRAE cooling class) is the known unstable
item: the model cannot decide whether to abstain on it. Do not "fix" that.

`think-27b` has **never been run on the current cases**. Expect it to be slow and
watch for `finishReason: length` — if you see it, the `outputHeadroom` of 12000
is too small for that case, which is a budget problem and not a model result.

**Then stop.**

---

## Stage 2 — `standard` (~35 min `chat-27b`, ~25 min `task-4b`, ~90 min `think-27b`)

Seventeen cases, 156 items: everything except the 80k long-context rung.

```bash
python3 forge/evals/run.py run --model chat-27b  --suite standard --stabilize 3
python3 forge/evals/run.py run --model task-4b   --suite standard --stabilize 3
python3 forge/evals/run.py run --model think-27b --suite standard --stabilize 3
python3 forge/evals/run.py report --models chat-27b,task-4b,think-27b
```

`--stabilize 3` runs once, works out which cases a single item could have
decided, and repeats only those. It is not optional on a stage this size:
8 of 12 cases moved between two identical runs when this was last measured.

`lcr-80k` will record `n/a` for `task-4b` — 80k of prompt does not fit its
65,538-token ceiling. **That is not a failure and must not be reported as one.**
The report prints `n/a` and the reason; keep that distinction in your summary.

**What to report:**

- The **stage routing table** from the report, verbatim. That is the deliverable:
  per pi-forge stage, the smallest model that cleared it and the measured speedup.
- Every case where `task-4b` cleared and every case where it did not, with the
  gate that failed.
- Unstable items by id. A case with any unstable item cannot carry a verdict.
- The speed columns: prefill and decode tok/s, tokens per item. A stage that
  clears on the 4B but is slower there is not a win.

**Known weak spots — report them as such, do not chase them:**

- **`enumeration-breadth` has a poor baseline.** `quotesVerbatim` passes 2 of 8
  on `chat-27b`, with ~9 unverifiable quotes per item. This predates the current
  work. Its gates cannot carry a routing verdict until someone looks at whether
  the model is fabricating quotes or the check is too strict about whitespace.
- **`chat-27b` is at ceiling on the long-context rungs** (index 0.90–1.00 at
  every distance). Those rungs will discriminate for smaller models — `task-4b`
  scored 0.60 at 48k — but say nothing about the 27B.
- **`meeting-brief`'s abstention half is under-powered.** `abstainedCorrectly`
  has a base of about four items. Read `factRecall`, `inventedNumbers` and
  `trapsHit` as that case's real instruments.

### The judged half

Eight of the eighteen cases are judged, because no gate settles whether a
cleaned transcript still sounds like the person who spoke it:
`braindump-split`, `doc-cleanup-ocr`, `grounding-draft`, `meeting-brief`,
`summary-report`, `summary-transcript`, `transcript-cleanup-meeting`,
`transcript-cleanup-memo`.

```bash
python3 forge/evals/run.py judge --models chat-27b,task-4b,think-27b
# grade results/judge/*.md blind, write verdicts.json, then:
python3 forge/evals/run.py score --verdicts forge/evals/results/judge/verdicts.json
```

The bundle never names which model wrote which output, and `results/judge/key.json`
is the unblinding. **Do not read the key before grading.** If you grade the
bundle yourself, say so in the report — you wrote the reference keys for
`meeting-brief`, so you are not a disinterested reader of that case.

`doc-cleanup-ocr` has still never been graded by anyone.

**Then stop.**

---

## Stage 3 — `full` (~55 min `chat-27b`, ~35 min `task-4b`, ~2 h `think-27b`)

Adds `lcr-80k`, the largest long-context rung.

```bash
python3 forge/evals/run.py run --model chat-27b  --suite full --stabilize 3
python3 forge/evals/run.py run --model think-27b --suite full --stabilize 3
python3 forge/evals/run.py run --model task-4b   --suite full --stabilize 3   # lcr-80k records n/a
python3 forge/evals/run.py report --models chat-27b,task-4b,think-27b --out /tmp/eval-report.md
```

**What to report:**

- The three long-context rungs side by side per model — 48k, 60k, 80k. The
  comparison across rungs is the point: the evidence is identical at every rung
  and only the distance between the two anchor documents changes, so a drop is
  attributable to distance rather than to having been shown less.
- The full stage routing table and the routing recommendation.
- Total wall time and cost per model.

### The 95k ceiling — do not raise the rungs

Context is compressed above ~95,000 tokens on this deployment. The anchors sit
at the two **ends** of the corpus, so compression eats the padding between them
and leaves the evidence adjacent: an earlier 110k rung scored 10/10 that way,
measuring an easier task than its own label while looking like a clean pass.

Every rung is sized to stay under it, including the thinking tier's 12,000-token
headroom (80,000 + 12,000 + 256 = 92,256). `tests/test_evals.py` fails if any
rung × model combination would cross it. If you want a bigger rung you need a
backend change first, not a bigger number in `RUNGS`.

**Then stop.**

---

## Calibration — already done, do not redo unless scoring changes

Each new case has a variant that strips something it claims to measure. All were
run on `chat-27b` on 2026-08-04:

| Variant | Effect | |
| --- | --- | --- |
| `abstention-permission-removed` | index **+0.72 → +0.06**, 5 of 12 items | detects |
| `long-context-no-abstention` | index **1.00 → 0.70**, exactly 2 items | detects |
| `enumeration-clause-removed` | `itemTypesCovered` **10.6 → 8.1**, down on 7 of 8 | detects |
| `meeting-brief-no-abstention` | right direction, one item on a base of four | weak |

**Re-run them if you change any scorer.** A suite that stops detecting a change
known to matter makes every later null result uninterpretable.

Every one of those `compare` runs reported `p = 1.0, underpowered` on pass/fail
while the metric moved by a third or more. **Read the metric deltas, not the
pass rate.** A gate is a floor; the metric is the instrument.

---

## Traps, all of which have already cost a wrong conclusion

- **A single-pass comparison on an unstable case is noise.** A variant appeared
  to *improve* `abstention-closed-book` by three items; all three were the items
  that flip on their own. Both arms need `--repeat`/`--stabilize` before
  `compare` can exclude them.
- **A null result is a claim about the variant before it is a claim about the
  case.** Two calibrations moved nothing because they stripped a *justification*
  rather than the instruction it justified, and a warning that four of the
  questions repeat in their own text. Check what the prompt still says.
- **Temperature 0 is not determinism here.**
- **A model id is a label someone typed.** `:8004` and `:8007` each serve one of
  several registered entries. `run` refuses on a mismatch; when nothing could be
  checked it warns and records `attributionUnconfirmed` on the result document.
  If you see that warning, put it in the report.
- **`task` shares a router with `embed`/`ocr`/`rank` at `MODEL_ROUTER_MAX=1`**
  (~6 s swap). No case touches embeddings; keep it that way.
- **A case that does not fit a model is `n/a`, not `0/n`.** Never merge them.

## If a fixture goes missing

The vault is a working notebook and notes get refiled — four fixtures were
already unreachable from their pinned paths. `freeze` falls back to
`~/.pi-forge/eval-sources` and marks any fixture it had to fall back for. If
that archive is also missing something, `run.py archive` rebuilds what it can
from the vault and from frozen copies. Do not edit `fixtures.json` to point at a
new path without telling Ellie: a re-pin makes old results incomparable.

## One open question for Ellie, not for you to decide

Four braindump fixtures (`braindump-merge`, `braindump-todo`, `braindump-voice`,
`raw-asr-piforge`) are pi-forge tooling notes that `vault-organizer` filed under
`10.03 Transcript/Personal/Journal` — a denied prefix. They currently resolve
from the archive, so nothing is blocked, but the misfiling is real and the
fixtures point at paths the vault no longer has.
