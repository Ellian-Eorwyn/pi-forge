# Constraint audit: where the skills violate C1–C5

Part 3 of `docs/decomposition-and-grounding-handoff.md`, run against the
constraints as they now stand in `docs/skill-architecture.md`.

**Scope.** 40 model call sites — 30 Python, 10 JavaScript — across 14 skills,
2 shared libraries, and 25 distinct stage labels. 35 system-prompt constants read
in full.

**Headline.** The codebase is in better shape on C1 and C4 than the handoff
guessed, and worse on C2/C5 than a reading of any individual skill suggests.
Almost every prompt says *never invent*; **one of thirty-five** says the thing
the evaluation actually measured — that what the model already knows is not
evidence. Those are not the same instruction, and a model does not experience
recall as inventing.

Two structural defects also turned up that nothing was checking.

---

## Tier A — the routing table does not join to the code

`forge/lib/forge_routing.py` teaches that a stage label is load-bearing. Seven of
the nine entries in its own `STAGES_HELD_ON_CHAT` name a label **no call site
ever passes**:

| Table entry | What the code actually passes | Where |
| --- | --- | --- |
| `verify-packet` | `verify`, `verify-repair` | `forge_verify.py:224,237`, `forge-verify.mjs:116,128` |
| `clean-document-chunk` | `clean-chunk` | `document-ingest.mjs:2895` |
| `ground-draft` | `draft-note` | `vault-capture.py:745` |
| `meeting-brief` | — | no production stage |
| `enumerate-items` | — | no production stage |
| `abstention-grounded` | — | no production stage |
| `summarize-report` | — | no production stage |

Two distinct problems wearing one costume:

1. **Three are real stages recorded under the wrong name.** The consequence is
   live, not cosmetic: `connectedServices.routing = {"verify-packet": "task"}`
   parses, validates, and silently does nothing, because `service_name_for`
   looks up the label the *call site* passes. The recorded reason for a decision
   also fails to join to the code it is about.
2. **Four are eval capabilities that were never stages.** `meeting-brief`,
   `enumerate-items`, `abstention-grounded`, and `summarize-report` measure
   whether a model can do a kind of thing. Filing them in a table called "stages
   held on chat" is a category error that makes the other entries harder to
   trust.

`STAGE_SERVICES` is clean — all six routed labels resolve to real call sites.

**Applied.** Renamed the three real stages to the labels the code passes; moved
the four capabilities to a new `CAPABILITIES_MEASURED` constant that says plainly
they are not stages. `forge-routing.mjs` updated in step.

The lasting part is the test rather than the rename:
`test_forge_routing.test_every_table_key_is_a_label_some_call_site_passes` scans
every skill and library for the literal, understanding the `f"{stage}-repair"`
construction that builds two of the routed labels dynamically. It found the
seventh entry (`summarize-report`) that the manual pass had missed, which is the
argument for having written it.

---

## Tier B — the recall clause is missing almost everywhere (C2, C5)

The measured intervention is `abstention-permission-removed`: strip the clause
telling the model that its own knowledge is not evidence and the omniscience
index falls **+0.72 → +0.06**. It is the single largest prompt effect in the
suite.

Across 35 prompt constants:

| | count |
| --- | ---: |
| prohibits inventing ("never invent", "never fabricate") | 11 / 35 |
| offers an explicit decline path (`needs_review`, empty array, "Not stated") | 11 / 35 |
| **says the model's own knowledge is not evidence** | **1 / 35** |

The one that has it is `vault-capture.DRAFT_SYSTEM`. Read in full, several others
carry it in substance under wording a regex misses, and those are correct as they
stand:

- `vault-transcripts.REFLECTION_SOURCE_RULES` — *"Never state a fact from outside
  the vault on your own authority. If you know something relevant and it is not
  in `outsideSources`, leave it out."* This is the best statement of the rule in
  the codebase.
- `vault-curator.PRACTICE_SYSTEM` — *"Say nothing rather than filling the gap
  from memory."*
- `vault-compose.DRAFT_SYSTEM` — *"Every specific … must come from the sources
  given for that block. If it is not there, do not write it."*
- `spreadsheet-analysis.ENRICHMENT_SYSTEM` — *"Use only what the row provides …
  return needs_review with a note rather than guessing."*

So the audit's job was to find prompts where the clause is genuinely absent *and*
nothing downstream catches the consequence. Ranked by that second half:

### B1. `forge_verify.VERDICT_CONTRACT` — highest value in the codebase

The verifier is told to flag an item that is *"actually wrong or unjustifiable on
the evidence shown"*, and is never told that what it already knows is not part of
that evidence. This is the one prompt every batch skill routes through:
`document-ingest`, `literature-extraction`, `personal-admin`,
`spreadsheet-analysis`, `project-extraction`, `skill-tuner`, `vault-*`.

A verifier approving an extraction because it *sounds right* is not hypothetical.
`docs/service-split-handoff.md` §7.4 records a review pass that approved an
extraction which tripled a balance and invented a deadline, because the reviewer
had paraphrases and no source. The clause is the direct counter, and it is
missing from both the Python and JavaScript copies.

### B2. `vault_classification.SYSTEM_INSTRUCTIONS`

Classifies notes against a schema with no statement that the note is the only
evidence. A note whose title names a well-known subject invites the model to
classify from what it knows about the subject rather than from the note. It has
a decline path (`needs_review`) but nothing pointing it at recall.

### B3. `literature-extraction.EXTRACTION_SYSTEM`

Has *"Never invent details the document does not support"* and *"an empty array
is the right answer"* — both halves except the one that names recall. Its output
becomes evidence tables and cited memos, so an unsupported extraction propagates
into a deliverable a person reads as sourced.

### B4. `project-extraction.WORKER_SYSTEM_PROMPT`

*"Never invent dates, owners, obligations, quotes, teams, workstreams, or
precedence"* — a strong list, no recall clause, and no decline path beyond
`screened_no_controls` (which the prompt restricts to a different situation).
Deliverables, dates, and risks are exactly the fields where a plausible
recalled answer survives review.

### B5. `spreadsheet-analysis.ENRICHMENT_SYSTEM`

Already the best-shaped prompt in the codebase on C1 (see Tier C) and it has both
other halves. But column enrichment is where recall is *most* tempting — a column
like "country of origin" or "publisher" is answerable from weights for many rows,
and the prompt's "use only what the row provides" reads as being about format
rather than about knowledge.

### B6. `reviewer-2.INVENTORY_SYSTEM`

Lists empirical claims in one paragraph. Grounded by construction — the paragraph
is right there — and it offers the empty list. Lowest of the six, included
because its `note` field ("what a reader would need in order to be convinced")
invites the model to supply what it thinks the literature says.

### Not changed, and why

- `vault-compose.OUTLINE_SYSTEM` plans titles and block structure; every specific
  it could invent is caught by `ungrounded_specifics` before the note is written.
- `skill-tuner`'s five prompts are gated by byte-exact quote verification against
  the rendered timeline. A fabricated quote costs a corrective retry and then a
  review mark. The gate is stronger than a clause.
- `vault-wiki.PLAN_SYSTEM` and `vault-curator.FRAME_SYSTEM` are recall-as-index
  by design — topic routing and query generation, which C5 explicitly licenses.
  `PLAN_SYSTEM`'s `disambiguator` is the one field worth watching, since it is a
  recalled fact ("for a Sanskrit term name the tradition") that shapes a search
  rather than entering a note.

---

## Tier C — decomposition (C1, C4): mostly already right

The handoff asked which prompts pack independent decisions into one response.
Checked all 35. The answer is that most multi-field responses are **dependent**
fields, which C4 says are one piece:

| Prompt | Fields | Verdict |
| --- | --- | --- |
| `vault_classification.SYSTEM_INSTRUCTIONS` | ~6 frontmatter properties | **genuinely independent — the known C1 offender** |
| `vault-transcripts` reflections | observations, interpretations, open questions, connections | dependent — interpretations rest on observations |
| `vault-curator.RECONCILE_SYSTEM` | move + 6 fields | dependent — every field is conditioned on the chosen move |
| `vault-curator.KIND_SYSTEM` | 3–6 sections | dependent — the set must be coherent and non-overlapping |
| `reviewer-2.INVENTORY_SYSTEM` | text, cited, note per claim | dependent — all three are about one claim |
| `vault-wiki.PLAN_SYSTEM` | topic, disambiguator, queries × N notes | dependent per note; batched across notes (see below) |
| `spreadsheet-analysis.ENRICHMENT_SYSTEM` | one cell | **already decomposed — the exemplar** |

`spreadsheet-analysis` is worth copying rather than fixing: *"You fill in one
spreadsheet column, one row at a time"*, with a per-cell `needs_review`. That is
C1 and per-item escalation built in from the start.

**The one real C1 violation remains `vault_classification`, and it must not be
changed yet.** Part 1 of the handoff — build `classify-one-property`, measure
per-property accuracy and seconds *per note*, decide row-wise against column-wise
prefill order — is the prerequisite. Changing it now would be the unmeasured
guess the handoff exists to prevent.

**An unresolved tension worth recording.** Batching independent items into one
response (`vault-wiki.PLAN_SYSTEM`, `forge_verify.build_packets`) is the opposite
of C1's failure-shape argument: one malformed response loses every item in the
packet. It is done deliberately, for the economics — full review coverage of 500
notes for ~25 thinking calls instead of 500. Both are right, and the resolution
is that C1 is about *decisions inside one item*, not about how many items share a
call. That distinction was implicit; it is now stated in `docs/skill-architecture.md`.

---

## Tier D — reduce before you reason: already the house pattern

Checked every skill that reaches the thinking tier. `document-ingest`,
`skill-tuner`, `literature-extraction`, `personal-admin`, `spreadsheet-analysis`,
`project-extraction`, and `vault-compose` all put a reduction between the corpus
and the reasoning model — six of them through `forge_verify`, which is what makes
the pattern uniform. No skill was found handing raw material to `think`.

The gap is on the other half of the rule: **what a script returns to the
session.** That is where the context economy is actually being spent, and
`SCRIPT_TOOL_CONTRACT.md` only started saying so this week. Sizing which scripts
exceed a sensible stdout budget needs a measurement pass over real run output,
not a reading of the code, so it is scoped as its own task rather than guessed at
here.

---

## Two live failures found in passing — not fixed, because both are yours to call

Both pre-date this audit (verified by stashing the routing changes and re-running),
and neither is reachable from `npm run check`, because **`npm run test:evals` is
not part of the check gate.** That is the reason they went unnoticed, and it is
the first thing to decide.

### 1. `clean-transcript-chunk-single` — RESOLVED, and the cause was not routing

The failing test said `transcript-cleanup-memo` belonged on the `small` tier
while the stage was routed to `think`. The first reading — that a routing
decision had outlived its evidence — was wrong. The per-model gate results are:

| model | passRate | silent | unstable |
| --- | ---: | ---: | ---: |
| **`think-27b`** | **1.000** | 0 | 0 |
| `task-9b` | 0.875 | 0 | 0 |
| `task-4b` | 0.625 | 0 | 0 |
| `chat-27b` (baseline) | 0.250 | 0 | 0 |

`think-27b` is the best model on this case by a clear margin. It was not being
out-ranked — it was **excluded from the candidate set entirely**, and the reason
had nothing to do with transcripts.

`scored.json` is written whole by each grading pass rather than added to. The
overnight MoE/9B pass graded `chat-27b` and the three new profiles; the previous
pass's `think-27b` and `task-4b` grades were moved to
`results/judge/_prior-grading-2026-08-05/`. `_recommendation` holds any *judged*
case where a model has no judge mean — "gates hold up, but the quality was never
graded" — so both models silently stopped being routing candidates everywhere,
and `routing_table` crowned the runner-up with no trace that it had done so.

The two passes are directly comparable by the harness's own criterion.
`merge-verdicts.calibrate` exists to answer exactly this, using `chat-27b` as the
model both passes graded, and states that a delta past ±0.5 means they cannot be
read together. The measured deltas are +0.01, −0.20, +0.03, −0.11.

**Fixed in two places:**

- `judge.scored()` now folds comparable archived passes back in — current grades
  always win for a model the current pass covered, archives are consulted only
  for models it did not cover at all, and anything folded in is recorded under
  `summary._mergedFrom` so a reader can see a mean did not come from the latest
  run. `judge.comparable()` implements the calibration criterion the merge tool
  documents.
- `report.routing_table` now records `betterButNotCandidates` on any case where a
  model scoring strictly higher was kept out of the candidate set. That is the
  shape of this bug, and it was invisible. Running it across the whole suite
  today flags one further case, `meeting-brief`, where the baseline outscores the
  winner.

`transcript-cleanup-memo` now resolves to `think-27b` at 1.000 on the `verify`
tier, the routing test passes, and the stage stays on `think` — with the evidence
agreeing rather than being overruled.

The general lesson is the one this whole pass keeps finding: **"not measured" was
indistinguishable from "measured, and worse."** Here it cost a 27B its place to a
9B on a case it had never lost.

Note that `results/` is gitignored, so a grading pass that drops models leaves no
diff to review. That is worth deciding about separately.

### 2. `ArchiveTests::test_the_vault_wins_when_it_still_has_the_fixture`

Fails with `'archive' != 'vault'`. Unrelated to routing or grounding; recorded
here only because it is the other thing the missing gate is hiding.

---

## Next steps, in order

**1. Put `test:evals` in the check gate.** Both defects this audit found were
sitting in a test suite `npm run check` does not run, and the whole suite takes
1.6 seconds. Blocked only by `ArchiveTests::test_the_vault_wins_when_it_still_has_the_fixture`,
which needs fixing or explicitly marking expected-fail first. Nothing else on this
list is worth doing before this one, because without it the next silent drift
lasts as long as these did.

**2. Measure `verify` before and after the clause.** Six prompts gained the
recall clause on the strength of `abstention-permission-removed`, which measured
its *removal* from a different prompt. That is the right direction with the wrong
specificity. `forge_verify.VERDICT_CONTRACT` is the one to check first — every
batch skill routes through it — and `verifier-seeded` already exists as its case.
Run it against the previous contract and the new one. If the clause does nothing
measurable there, the other five are worth re-examining rather than assumed.

**3. Handoff Part 1: `classify-one-property`.** Still the largest single
decomposition win available, and the only genuine C1 violation left: six
properties at ~0.85 each compounding to ~0.38 note-level. Build the case, measure
seconds per *note* rather than per call, and settle row-wise against column-wise
prefill order. Everything about `vault_classification` waits on this.

**4. Re-run `grounding-draft`.** Every result before `1d0eac08f` is unreliable,
and it is the evidence behind the `draft-note` entry in `STAGES_HELD_ON_CHAT`.
Cheap, and it unblocks trusting that entry.

**5. Give the expensive unmeasured stages cases.** Twenty-five labels default to
`chat` with nothing behind them, and most should stay there. The four where a
wrong tier costs the most work per run: `email-evidence` (`document-ingest`, once
per email), `extract` (`literature-extraction` and `personal-admin`, once per
document), `enrich` (`spreadsheet-analysis`, once per cell), and
`skill-tuner-extract` (once per chunk). Each needs a case before it can be routed.

**6. Measure script stdout, then budget it.** The half of *reduce before you
reason* that is still unenforced. Instrument real runs of the batch skills,
record stdout bytes per processed unit, and set a budget from what the data says
rather than from a guess. `SCRIPT_TOOL_CONTRACT.md` states the rule; nothing
checks it.

**7. Decide what a partial grading pass should do.** `judge.scored` now recovers
from one, but the underlying behaviour — a pass that grades four of six models
silently drops the other two — is still there, and `results/` is gitignored so it
leaves no diff to review. Either grade every model each pass, or make partial
coverage loud at write time.

## What needs a measurement before anyone touches it

- `vault_classification` decomposition — handoff Part 1, `classify-one-property`.
- Any change to the 25 unmeasured stage labels' routing. They default to `chat`
  and that is the correct state for a stage nothing has looked at.
- Script stdout budgets — needs run-output measurement, not code reading.
- `grounding-draft` results before commit `1d0eac08f` are unreliable; the case's
  scorer raised on every item. Re-run before citing it for anything here.
