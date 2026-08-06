# Skill architecture

pi-forge separates a capability into four layers. The split exists so that each
piece of work is done by the thing that does it best: prose for judgment, a
routed model call for the parts that need a model, and a deterministic script for
everything that does not.

## 1. Profile/startup layer

Compact identity and capability index. It tells the agent what pi-forge can do
without loading every full workflow manual. `forge/AGENTS.md` sets profile-wide
standards, and `forge/CAPABILITIES.md` is the short startup capability index.

This layer is paid on every session, before the first word of the request.
`npm run forge:skills-report` measures it into `FORGE_SKILLS.md`. A line added
here should be worth its cost on sessions that never use the capability it
describes; if it is not, it belongs in a `SKILL.md`.

## 2. Skill layer

Workflow judgment: when to use a capability, what process to follow, what
standards apply, how to handle ambiguity, and what final output should look
like. `SKILL.md` files should hold routing, review points, evidence standards,
provenance expectations, citation rules, output formats, and safety rules.

Forge-bundled skills live in `forge/skills/<name>/` so the npm package can ship
them through `forge/package.json`. Newly generated project or user skills should
prefer the agent-agnostic `.agents/skills/<name>/SKILL.md` or
`~/.agents/skills/<name>/SKILL.md` layout unless the goal is to add another
Forge-bundled capability.

## 3. Model-call layer

The stage-labelled calls a skill's script makes into the inference stack. This
layer is not visible in `SKILL.md` and not deterministic like `scripts/`, and it
is where most of a run's time, cost, and failure modes live.

**A stage label is load-bearing, not a log field.** `forge_routing.service_name_for`
keys on the `task="..."` string a call site passes to `forge_llm.call`, and a
label named in no table runs on `chat`. A call that reuses another stage's helper
silently inherits that stage's route — this has already happened twice in
`vault-connections`. When adding a call, ask what stage it *is*, not what
function it borrows.

**What each tier is shaped for:**

- **`task`** — one decision, or one faithful near-copy transform, over text it
  was handed. Emitting a *value* is its shape; composing prose a human reads is
  not, and neither is anything drawing on its own knowledge.
- **`chat`** — synthesis, breadth, and anything the model must answer without a
  source in front of it.
- **`think`** — constrained rewrites and segmentation. Thinking trades recall for
  precision: it converges rather than sweeps, so it is wrong for any task needing
  breadth.

**Which stages actually route where, and the measurement behind each, live in
`forge/lib/forge_routing.py`.** That file is the table; this is only the shape.
Do not restate its numbers here — this repository already keeps three tables of
truth about skills (`CAPABILITIES.md`, `SKILL.md` frontmatter, and
`workflow-categories.json`), and the `check:forge-*` scripts exist because those
three drifted. Routing does not need a fourth.

Three states, deliberately distinguishable in that file:

- in `STAGE_SERVICES` — measured, and the answer was to move it.
- in `STAGES_HELD_ON_CHAT` — measured, and the answer was no. The reason is
  recorded because it will be re-proposed.
- in neither — **unmeasured**. Not "fine on the default". Nothing has looked.

`forge/evals/tests/test_evals.py` refuses a stage routed somewhere the latest
report does not support, so a routing decision cannot outlive its measurement.

## 4. Script/tool layer

Mechanical execution: parsing, conversion, fetching, archiving, validation,
hashing, manifest generation, and other repeatable operations. Skill manifests
declare real available scripts/tools; do not list desired future tools as if
they exist.

Deterministic checks belong *before* any model call, not after. They are free and
exact, and running them first means the model's budget goes on judgment rather
than on catching malformed JSON.

## Grounding: what recall is for

**Model knowledge is an index, not a source.**

Recall is genuinely good at the thing it is good at, and the profile depends on
it: naming a concept so it can be searched for, proposing where to look,
generating query terms and synonyms, recognizing a term of art, noticing that
something expected is missing, and choosing which skill to reach for. None of
that asserts anything, and all of it is how the work gets pointed in the right
direction.

What it must never do is *supply* a fact. Recall reaches the output only as an
explicit thing to check, never as a claim:

```markdown
<!-- not allowed, even hedged -->
The method, likely due to Feld (1982), recurs throughout the transcripts.

<!-- allowed -->
## To verify
- No source in the corpus attributes this method. It is usually credited to
  Feld; check before this goes in the body.
```

A hedge in a draft loses its hedge downstream. A question does not become a fact
by being copied.

The mechanical test: **could this claim survive the model's weights being
replaced?** If it could only come from recall, it is fetched, or it is a question
under its own heading, or it is dropped.

`vault_compose.ungrounded_specifics` already enforces exactly this for one skill,
and it is the implementation to copy when another skill needs the same gate. Its
own summary of the rule is the clearest statement of it: *a fact the model merely
recalls has no unit, so it cannot ground, and a note asserting it is held.* Note
how it grades — names, links, and wikilinks with no root hold the note back;
numbers and uncertain names go to the reviewer instead. Not every ungrounded
specific has to block, but every one has to surface.

### Why this is the enabler, not a separate concern

Recall quality is the one axis that collapses with model size. `docs/moe-9b-eval-results.md`
measured the same questions with and without a source in the prompt. With one,
the worst profile scores 0.67 and most clear 0.94. Without one, the 4B is
**negative** (−0.17 — it asserts something false more often than it is right) and
even the thinking 27B falls to 0.54.

So a prompt that expects the model to supply facts is a design error, not a
model choice — and rooting a request in given information is precisely what makes
the rest of this document safe to apply. Decomposition and smaller models are
only available on work that does not lean on recall.

**The permission clause is load-bearing.** A prompt handing over a source must
also say to answer only from it and to decline otherwise. Removing that clause
drops the same measurement from +0.72 to +0.06. Do not trim it for brevity.

## Reduce before you reason

The thinking model should not be reading raw corpora, and neither should the
session. Both have a bounded window, and both spend it on whatever is put in
front of them.

The shape, already implemented twice. `skill-tuner` mines every chunk on `chat`,
reviews all of it on `think` and escalates the flags, merges and bounds the
survivors into an authoring context, and only then writes the report.
`document-ingest` extracts exact-quote evidence per email on `chat`, then
verifies and composes the cited digest on `think`. In both, the model that
reasons never sees the corpus:

1. **Bulk read on `chat`**, once per unit, stateless. One file, one email, one
   chunk per call.
2. **The reduction is condensed but original** — exact quotes carrying source
   locators and ids, verified byte-exact against the source before anything is
   recorded. Never paraphrase. A paraphrase moves the grounding problem
   downstream instead of solving it, and the reasoning stage can no longer tell
   what the source actually said.
3. **`think` reasons over the reduction, in batches.** `forge_verify.build_packets`
   already does this: packets bounded by item count and serialized size, so full
   coverage of a large run costs tens of thinking calls rather than hundreds.
4. **Escalation is per-item and rare.** That is where the reasoning budget goes.
5. **Stages stay contiguous.** Interleaving a verification call between two bulk
   calls swaps the prompt prefix on both servers every time; running all the bulk
   work and then all the review keeps each prefix cache warm.

The same discipline applies to what a script prints. A script's stdout is spent
directly out of the session's context window, so it returns counts, ids, paths,
warnings, and the exceptions — and writes the full material to a declared
artifact whose path it names. Per-item detail belongs in the run directory. See
`SCRIPT_TOOL_CONTRACT.md`.

## Decomposition

**Difficulty scales with how many independent choices are packed into one
response, not with how hard each choice is.** Six labelings at 0.85 accuracy make
an all-or-nothing response correct 0.85⁶ ≈ 38% of the time, which is roughly what
the classification case actually scores.

Decomposition also changes the *shape* of failure. One malformed response loses
all six properties; six responses lose one, and the other five are still usable.
`needsReview` becomes per-property, escalation becomes per-property, and a retry
costs one call.

But **do not decompose for its own sake.** The test: *does this piece need to see
the others to be right?* If no, split it. If yes, it is one piece. Synthesis is
the counter-example — a meeting brief is valuable precisely because it is a
cross-cutting view, and splitting it destroys the thing being asked for.

## Rule of thumb

- If it tells the agent how to reason or decide, keep it in `SKILL.md`.
- If it performs a repeatable operation, move it toward `scripts/` or an extension/tool.
- If it is background documentation, put it in `references/`.
- If it is a template or file consumed by an output, put it in `assets/`.
- If one model call makes more than one independent decision, split it — unless a
  later decision needs an earlier one to be right.
- If a prompt needs a fact the model would have to recall, fetch it, or write it
  as a question under a to-verify heading.
- If `think` would otherwise read the raw material, reduce it on `chat` first.

## Example: web collection

`SKILL.md` says:

- when to use web collection
- how to preserve provenance
- what artifacts should be produced
- how to summarize collected sources

`scripts/` holds the extracted tools — `fetch_url.mjs`, `archive_page.mjs`,
`html_to_markdown.mjs`, `extract_metadata.mjs` — with shared helpers in
`web_tool_common.mjs`. The `web-collection.mjs` workflow CLI remains the
capability-level entrypoint.

## Example: organize folder

`SKILL.md` says:

- inspect before changing
- produce a reviewable manifest
- avoid destructive changes
- ask before applying risky operations

`scripts/` holds `scan_folder.py`, `generate_manifest.py`, `apply_manifest.py`,
and `hash_files.py`, with shared helpers in `organize_tool_common.py` and
coverage in `test_organize_folder.py`. The `organize-folder.py` workflow CLI
remains the capability-level entrypoint.

## Checklist

For each skill:

- [ ] Keep `SKILL.md` concise and procedural.
- [ ] Move mechanical details into scripts when implementation exists.
- [ ] Move long background notes into `references/`.
- [ ] Add or update `manifest.json`, declaring only directories that exist.
- [ ] Show worked examples for common use cases in `SKILL.md` or `references/`.
- [ ] Add tests for scripts, not prose.

For each model call:

- [ ] Does this single call make more than one independent decision? Split it,
      unless a later decision needs an earlier one.
- [ ] Does every prompt carrying a source also carry the answer-only-from-it and
      decline-otherwise clause?
- [ ] Does any prompt require a fact that is not in its own context — including a
      prompt whose excerpt is truncated where the answer would be?
- [ ] Does the stage label name what this call *is*, rather than what helper it
      borrows?
- [ ] Is the stage in `STAGE_SERVICES`, in `STAGES_HELD_ON_CHAT`, or unmeasured?
      Say which; do not guess.
- [ ] Does `think` see a reduction rather than the raw corpus?
- [ ] Is the script's stdout bounded, with bulk material written to an artifact
      whose path it names?
