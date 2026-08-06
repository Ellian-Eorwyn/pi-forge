# Handoff: decomposition and grounding as design constraints

For an agent picking this up cold. The job has three parts: **settle one open
question by measurement**, **write the constraints where agents will read them**,
and **audit the skills for where they are being violated**. Do them in that
order — the audit's recommendations depend on the measurement.

## The constraints

These come out of the model evaluation in `docs/moe-9b-eval-results.md` and the
run before it. Each is stated with the evidence, because a constraint whose
evidence is lost gets argued away in six months.

### C1. One decision per request

**Difficulty scales with how many independent choices are packed into one
response, not with how hard each choice is.**

Two cases in the suite are both "categorize against a clear definition":

| | Decisions per call | Best result |
| --- | --- | --- |
| `connection-judgment` | 1 (binary) | `task-4b` **16/16** — beating both 27Bs |
| `classify-notes` | ~6 frontmatter properties at once | everyone **2–5/8** |

But per-property accuracy on the second is 0.75–0.89. At 0.85 across six
properties, 0.85⁶ ≈ 0.38 — which is the ~3/8 actually observed. The classifier
is not bad at labeling. It is bad at doing six labelings in one breath.

Decomposition also changes the *shape* of failure, which matters as much as the
rate. One malformed response currently loses all six properties; six responses
lose one property, and the other five are still usable. `needsReview` becomes
per-property, escalation becomes per-property, and a retry costs one call.

### C2. Every request rooted in given information

**With a source in the prompt, model size barely matters. Without one, only the
dense 27B is safe.** Omniscience index (right answers minus confident wrong
ones), same questions:

| | `chat` | `think` | `task-4b` | `task-9b` | `moe` | `moe-think` |
| --- | --- | --- | --- | --- | --- | --- |
| with a source | 1.00 | 1.00 | 0.67 | 0.83 | 1.00 | 0.94 |
| from own knowledge | 0.67 | 0.54 | **−0.17** | 0.17 | 0.00 | 0.28 |

The 4B is *negative* closed-book: it asserts something false more often than it
is right. So a prompt that expects the model to supply facts is a design error,
not a model choice.

**The instruction is load-bearing, not decorative.** The
`abstention-permission-removed` variant strips the clause telling the model to
decline when it is not confident, and the index falls from +0.72 to +0.06. Every
prompt that hands over a source must also say to answer only from it and to
decline otherwise. Do not trim that clause for brevity.

One caution when reading these numbers: `abstained` in the eval counts *wrong*
declines — declining an unanswerable question scores as `correct`. So
`abstained: 0.00` across every model is a good result, not evidence the
permission goes unused.

### C3. Smallest capable model per piece, capability first

Speed is the tiebreak, never the reason. The routing rule already implemented in
`forge/lib/forge_routing.py` is: gate pass rate, then zero silent failures, then
speed. Some speed loss is acceptable to keep capability; the reverse is not.

What the profiles are actually for:

- **`task` (4B)** — one decision, or one faithful near-copy transform, over text
  it was handed. Best of six at diarized cleanup (7/8 where both 27Bs score ≤1/8)
  and pair judgment (16/16). Poor at anything needing its own knowledge,
  breadth, or composed prose (judged usability 2.91, a full point low). Emitting
  *values* is exactly its shape; writing prose a human reads is not.
- **`chat` (27B dense)** — synthesis, breadth, and anything closed-book. Best
  fact recall over a two-hour meeting (0.34 against the next model's 0.25) and
  best closed-book index (0.67).
- **`think`** — narrow: constrained rewrites and segmentation. **Thinking trades
  recall for precision** — on enumeration it raises item *types* covered
  (10.1 → 13.0) while halving items produced (32.9 → 19.0). It converges rather
  than sweeps, so it is wrong for any task that needs breadth, and it loses
  closed-book recall badly (10/12 → 5/12).

**Ellie's standing exception: summaries stay on `chat` (27B).** Gates show every
model at 8/8 on short-source summarization, including the 4B, so this is not a
gate decision — Ellie finds the 27B's summaries more complete and better at
surfacing the details that matter to her. Do not "optimize" it to a smaller
model on the strength of the pass rate.

### C4. Decompose to the smallest piece that does not lose capability

Not everything should be split. Synthesis is the counter-example: `meeting-brief`
asks a model to read a whole meeting and produce one brief, and the dense
non-thinking 27B wins outright because the value *is* the cross-cutting view.
Splitting it would destroy the thing being measured.

The test to apply: **does this piece need to see the others to be right?** If no,
split it. If yes, it is one piece.

## Part 1 — Settle this by measurement before recommending anything

**Question: can `task-4b` classify one property at a time as well as `chat-27b`,
and is it actually faster in wall-clock terms once it is N calls instead of one?**

Ellie's hypothesis is that it can and it is. It is plausible and it is not yet
measured. `classify-notes` today is one call per note with the whole schema; per
property, with only the relevant schema slice, is a different task and nobody
has run it.

Build a new eval case, `classify-one-property`, in `forge/evals/cases/`. Follow
the contract in `forge/evals/README.md` under "Adding a case" — import the real
skill via `harness.load_skill("vault-organizer")` and reuse
`forge/lib/vault_classification.py`'s own schema handling rather than writing a
new prompt, or the case measures something production would not accept.

Shape:

- For each fixture note × each routing property, one call carrying the note, the
  schema slice for that property alone, and the abstention clause from C2.
- Score per-property correctness against the same ground truth `classify-notes`
  uses, so the two are directly comparable.
- Report: `perPropertyCorrect`, `needsReview`, tokens per call, seconds per
  *note* (summed across its property calls, not per call), and a reconstructed
  note-level pass rate — all properties correct — to compare against
  `classify-notes`'s 2–5/8.
- Run `chat-27b`, `task-4b`, and `think-27b`.

**Adopt only if** `task-4b`'s per-property accuracy is within a small tolerance
of `chat-27b`'s *and* the reconstructed note-level rate beats today's whole-note
number. Per-property accuracy that is merely equal is not enough — the whole
point is that note-level correctness rises.

### The implementation detail that decides whether this is fast or slow

Six calls per note means the note is prefilled six times unless the prompt is
ordered for it. That could easily make decomposition *slower*, which would sink
the idea for the wrong reason.

`forge_llm.call` sends `cache_prompt: true`, and llama.cpp matches on prefix. So:

- **Put the stable, note-specific content first and the property question last.**
  Process **row-wise** — all properties for one note, back to back — so the note
  prefills once and each property question is a short cached-prefix suffix.
- **Column-wise (one property across all notes) is the wrong order here**, even
  though it is the right order for the byte-stable system prefix that
  `docs/service-split-handoff.md` §2.2 requires of batch work. Those two
  disciplines conflict for this stage; measure both orders before choosing, and
  record which won and why.
- If routing to `task`, remember its router is `MODEL_ROUTER_MAX=1` shared with
  `embed`/`ocr`/`rank`. Many small calls are fine; alternating with embeddings
  costs ~6s of model swap each time. Do not interleave.

Measure seconds per *note*, not per call. Six fast calls that each re-prefill a
2,000-token note is a regression however good the per-call latency looks.

## Part 2 — Write the constraints where agents will read them

Two homes, both existing:

- **`forge/AGENTS.md`** — this is the profile agents load at startup. Add the
  constraints as a section beside `## Provenance and Interpretation` and
  `## Reproducible Work`, which already carry the adjacent rules ("Never invent
  missing details", "Use the model for judgment, synthesis, cleanup, and
  drafting"). C2 is a sharpening of a rule already there; say so rather than
  duplicating it. Keep it short — this is startup context and every line is paid
  on every session.
- **`docs/skill-architecture.md`** — the layer model and the migration checklist
  live here. Add the decomposition rule to the "Rule of thumb" list and a line
  to the migration checklist, e.g. *"Does any single model call make more than
  one independent decision? Split it."*

Do not create a new top-level doc for this. The constraints belong next to the
rules they refine, and a third location is a third thing to fall out of date.

## Part 3 — Audit the skills and extensions

Find the places these constraints are violated, ranked by how much a fix would
buy. Start from the model call sites — there are ~44, and `forge_routing.py`'s
`STAGE_SERVICES` / `STAGES_HELD_ON_CHAT` already name the stages that have been
measured.

What to look for:

1. **Multi-decision calls (C1).** Any prompt asking for several independent
   fields in one response. The known one is `vault_classification.py` — the whole
   note and the whole schema, all frontmatter at once. Look for others: check
   `vault-capture`'s draft stage, `document-ingest`'s evidence extraction,
   `literature-extraction`'s item extraction, `project-extraction`'s packet
   extraction. For each, say how many independent decisions the response
   contains, and whether they are genuinely independent or need each other
   (C4 — if a later field depends on an earlier one, it is one piece).
2. **Ungrounded requests (C2).** Any prompt that asks the model for a fact not
   in its own context, or that omits the answer-only-from-the-source clause.
   `vault-curator` researches schema proposals and is the most likely offender by
   design; check that its claims are traceable to fetched sources rather than to
   the model. Also check every prompt that carries an *excerpt* — if the excerpt
   is truncated, the model is being asked to fill a gap it cannot see.
3. **Stage/model mismatches (C3).** Stages doing composition on `task`, or
   breadth work on `think`. `forge_routing.STAGES_HELD_ON_CHAT` records what has
   been measured and why; anything not in either table is unmeasured, and the
   audit should say so rather than guess.

Deliver a ranked list: file, stage, which constraint, what the change is, and
what evidence supports it. **Do not make the changes in the same pass.** Several
of these stages have no eval case, so a change would be unmeasured — flag those
as "needs a case first" rather than editing them.

## Things that will trip you up

- **`grounding-draft` results are unreliable** in every run before commit
  `1d0eac08f` — its scorer raised `AttributeError` on every item and recorded the
  exception as a note while still reporting pass rates. Re-run it before citing
  it. There is a task chip for this.
- **A stage label is load-bearing.** `forge_routing` keys on the `task="..."`
  string a call site passes, so a call that reuses another stage's helper
  inherits its route. Two calls in `vault-connections` did exactly that. When
  adding a call, ask what stage it *is*, not what function it borrows.
- **Do not decompose for its own sake.** C4 exists because the temptation is to
  split everything. Summarization and synthesis are single pieces. So is
  anything where the model needs the whole to judge the part.
- **An unmeasured improvement is a guess.** The suite exists so that routing and
  prompt changes are decided by evidence. If a recommendation has no case behind
  it, say that plainly in the audit rather than implying it is supported.
