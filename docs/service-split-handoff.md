# Finishing the thinking/non-thinking split

**Status: the conversions in §3 are done.** They landed in `b41c1884`…`f6f1a37e`,
along with a JavaScript client the plan did not originally scope. §7 records what
each one measured, what turned out to be wrong in the guidance below, and what is
deliberately left unconverted. Read §7 before §3, which is kept as written so the
before-and-after stays legible.

This was a handoff for whoever continued the work started on
`feat/vault-connections` (commits `2f9e976e`…`a186cbf4`). The routing and
verification layers were built and proven; three batch skills still routed every
per-file operation through the agent's own thinking context. This document says
what to do, what to copy, and — most importantly — what had to change about
*prompting* to keep quality when work moved off the thinking model.

Read this before the code. The mechanical part is easy and the prompting part is
where the quality actually lives.

---

## 1. What already exists

One local model, served twice, both live at once:

| Service (`connectedServices`) | Endpoint | Model | Role |
| --- | --- | --- | --- |
| `chat` | `http://llms:8004/v1/chat/completions` | `chat` | every per-file batch call |
| `think` | `http://llms:8008/v1/chat/completions` | `code` | verification, review, judgment |
| `embeddings` | `http://llms:8005/v1/embeddings` | `embed` | candidate narrowing, near-dupe skip |

Measured on this deployment: **:8008 spends ~410 hidden reasoning tokens on every
call regardless of difficulty**, even to answer with one word, where :8004 spends 2.
Two inbox notes classified in 4.2s against :8004 versus 56.9s against :8008, with
identical destinations.

**"Served twice" means two proxy profiles, not two servers.** Both ports forward to
one `llama-server` (`chat-backend-dense`, `127.0.0.1:8010`) and differ only in
thinking on/off, temperature, and reasoning stream mode. Two consequences follow.
Slot numbers are shared, so both services carry a scheduling block and both pin the
background slot — leaving either unpinned lets bulk work land on slot 0 and evict
the interactive session's prefix cache. And the context ceiling on any one request
is a *slot*: the backend runs `--ctx-size 262144 --parallel 2`, llama.cpp divides
that evenly, so **131,072 tokens** is the real limit, exported as
`SLOT_CONTEXT_TOKENS` and enforced by a preflight check in `forge_llm.call`.
Confirmed by oversending — the server returns `exceed_context_size_error` with
`"n_ctx": 131072`. `/props` and `/slots` are disabled on this backend, so
oversending is the only way to read the number back.

That cost is invisible in the response body — llama.cpp strips the think block
server-side and returns no `reasoning_content`. The only evidence is
`timings.predicted_n` against the visible content length. If you ever need to prove
which config an endpoint is running, that is the check, and
`forge_llm.hidden_token_count` already implements it.

### Building blocks to reuse, not reinvent

- **`forge/lib/forge_llm.py`** — the only way a Python skill should reach a chat
  endpoint. `resolve_service("chat")` and `resolve_think_or_chat()` layer explicit
  argument → environment → `connectedServices` → default. `call_json_with_retry`
  handles transport, retries, think-tag stripping, and JSON extraction.
  `service_doctor(service, expect_non_thinking=True)` is the endpoint health check.
  The `.mjs` equivalent is `resolveConnectedServices()` in
  `forge/lib/connected-services.mjs`.
- **`forge/lib/stack_state.py`** (and `stack-state.mjs`) — read-only client for
  the deployment's state API at `http://llms:8078/api/v1/`. Strictly optional and
  never on the request path: it tells `service_doctor` which weights are behind a
  port and why one is down, tells the installer the real per-slot context size
  instead of a hardcoded constant, and puts the backend's identity plus the
  stack's own warnings on the first `model_call` record of a run. Every function
  returns `None` when the stack cannot be read, and every caller must carry on as
  though it never existed — that is the path any install other than this one
  takes. `PI_FORGE_SKIP_STACK_DISCOVERY=1` turns it off; tests set it.
- **`forge/lib/forge_verify.py`** — batched review. `verify_packets` enforces exact
  id coverage and requires a reason on every flag, with one corrective retry that
  shows the model what it broke. `escalate` redoes flagged items individually.
  Verdicts are journaled, so a resumed run reviews nothing twice.
- **Worked examples**, in increasing order of how closely you should copy them:
  `vault-organizer.py` (`verify_classifications`), `project-extraction.py` (the
  mature background worker with leases and preemption), and
  **`literature-extraction.py` (`command_process`) — this is the template for the
  remaining conversions.**

### Non-negotiable invariants

- Bulk work is **script-side HTTP with a byte-stable system prefix**. Not the
  agent's conversation, not spawned `pi` subprocesses. The subagent example
  extension stays uninstalled.
- Run **all** bulk calls, then **all** review calls. Never interleave: alternating
  between the two servers swaps the prompt prefix on both and throws away the
  cache the whole design is built on.
- Deterministic checks run **before** the model verifier, so the thinking budget
  goes to judgment rather than to catching malformed JSON.
- An unreachable verifier must **never** read as approval. Say "not verified" in
  the report.
- Verification may flag, escalate, and hand something to a human. It may never
  silently drop a result.

---

## 2. The prompting changes that preserved quality

This is the part that does not transfer from reading the diff. Moving work to the
non-thinking model does not degrade it uniformly — it degrades in one specific,
predictable way, and the fix is in the prompt rather than in the model choice.

### 2.1 A non-thinking model does not enumerate categories on its own

The finding that mattered most. In `literature-extraction`, the same corpus,
the same contract, the same temperature:

| | items | item types covered | quotes | fabricated quotes |
| --- | ---: | ---: | ---: | ---: |
| thinking (:8008) | 17 | 8 | 17 | 0 |
| non-thinking (:8004), original prompt | 17 | **4** | 17 | 0 |
| non-thinking (:8004), after the fix | **41** | **12** | 40 | 0 |

Volume and quote fidelity were already identical. What the non-thinking model lost
was *breadth*: it settled for the obvious categories (claims, findings, methods,
limitations) and never considered definitions, data sources, variables,
populations, technologies, policies, cited works, research gaps, or connections.
A reasoning model walks that list silently in its scratchpad. A non-thinking model
answers with whatever the prompt made salient.

The fix was four lines appended to the system prompt:

```text
- Work through the item types in order and ask what the document offers for each one before
  moving on. Do not stop at claims, findings, methods, and limitations: documents also carry
  definitions, data sources, variables, populations, technologies, policies, cited works,
  research gaps, and connections to other work.
```

Note what it does: it names the categories that get *skipped*, not just the full
list (the full list was already in the schema section and was not enough). Making
the omission explicit is what worked.

**Apply this to every remaining conversion.** If output looks thin after moving a
task off the thinking backend, suspect a missing enumeration in the prompt before
concluding the model is too weak. Check coverage across whatever the skill's
categorical axis is — `fact_type` in personal-admin, the enrichment dimensions in
spreadsheet-analysis — not just row or item counts, which will look fine.

### 2.2 Statelessness means the prompt carries the whole task

Each call gets the contract plus one file, and nothing else. Anything the agent
used to "just know" from earlier turns has to be in the system prompt or the
per-item payload. In practice that meant spelling out things that were previously
implicit: what an empty result means and that it is a legitimate answer, that
inference must be labelled rather than presented as fact, and that a quote must be
copied rather than reconstructed.

Keep the system prompt **byte-stable across every call in a run**. Put the
per-document variation entirely in the user message. This is what keeps the
server's prefix cache warm; a system prompt that interpolates the filename or an
index silently destroys it.

### 2.3 Tell the model the objection when you escalate

Escalation is not a plain retry. The thinking model is given what the reviewer
objected to and asked to reconsider:

- `vault-organizer` passes `repair: {reviewer_objection, previous_metadata}`.
- `literature-extraction` appends the objection to `customInstructions`.

Both are meaningfully better than re-asking the original question, because the
failure mode being corrected is usually a judgment call rather than a formatting
slip.

### 2.4 Make the verifier's bar explicit, or it will rubber-stamp

Every verify prompt says what *does not* justify a flag. Without that clause the
reviewer drifts toward flagging stylistic preferences, which floods escalation
with work that changes nothing:

- organizer: *"A defensible filing is 'ok' even if you would have chosen
  differently; taste is not an error."*
- connections: *"A genuine connection is 'ok' even if it is obvious or modest."*
- literature: *"Do not flag a document for being thin if the source genuinely
  offers little."*

### 2.5 Prefer a deterministic check to a prompt instruction

The strongest quality control in the whole pilot is not a prompt at all. Evidence
quotes are checked against the source text before anything is recorded
(`quote_violations` in `literature-extraction.py`): a paraphrase can be argued
about, but a quotation either appears in the document or is fabricated. Across the
live runs, 40 quoted items, zero fabrications — and when a stub deliberately
fabricated one, the retry caught it.

Matching is whitespace- and smart-quote-insensitive and runs per fragment, because
the contract allows several short quotes in one field. Fragments under 12
characters are skipped to avoid false positives.

Look for the equivalent invariant in each remaining skill before writing any
prompt rule (see the per-skill notes below).

---

## 3. Remaining work — all done; see §7

Order matters: each is a smaller step from the one before it.

### 3.1 `personal-admin` — closest to the pilot, do it first

Nearly identical in shape to `literature-extraction`: `command_next` /
`command_record`, `load_results`, `next_pending`, and a `normalize_facts` validator
that calls `fail()` on the first problem.

1. Split `normalize_facts` into `validate_facts(raw) -> (facts, errors)` plus a
   thin `normalize_facts` that calls `fail(errors[0])`. The worker needs the error
   list to feed back; the CLI keeps its current behavior. This is exactly the
   `validate_items`/`normalize_items` split in `literature-extraction.py`.
2. Add `command_process` modelled on `literature-extraction.py:command_process`,
   with `record_extraction`'s equivalent shared by the CLI and worker so both go
   through one journal path.
3. Deterministic checks before the verifier: `due_date` must parse as a date, and
   any `value` that looks like a reference or account number must appear verbatim
   in the source. That second one is this skill's analogue of quote-exactness and
   is worth more than any prompt rule.
4. Enumeration clause naming the `fact_type` values that get skipped (see §2.1).
   Verify coverage across `fact_type`, not just fact counts.
5. Deliverables stay agent-authored.

### 3.2 `spreadsheet-analysis` row enrichment

Different shape: `command_row_next` returns one row (`rowId`, `input`,
`outputColumn`) and `command_row_record` commits one value. The loop is otherwise
the same.

- One stateless call per row; the user's enrichment instruction and the column
  contract go in the byte-stable system prompt, the row payload in the user
  message.
- **Use embeddings here.** Rows are frequently near-identical, and the organizer's
  near-dupe pattern (`NEAR_DUPE_AUTO = 0.97`) applies directly: cluster rows,
  extract once per cluster, propagate the value to members with recorded
  provenance so it stays reviewable. This is the biggest available saving in the
  whole remaining set and does not exist yet.
- Verification: sample-plus-all-flagged rather than all rows. Row enrichment is
  high-volume and low-variance, so full coverage buys less here than elsewhere.
  This is a deliberate departure from the verify-all default — say so in the
  report.

### 3.3 `document-ingest` chunk review — assess before converting

The one genuinely uncertain conversion. Review units are `chunk` and `vision-page`
(`next-review` / `record-review-unit`). Before writing anything, answer: **does
reviewing chunk N require chunk N−1?** Nothing in the current code passes
neighbouring context, but the reviewing agent has it implicitly, which is exactly
the sort of dependency that disappears silently when a task goes stateless.

- If chunks are independent: convert like the others.
- If they are not: include a bounded rolling brief of the previous chunk in the
  user message. Still cache-friendly, since the system prompt is unchanged.
- Vision pages stay as they are — that path is multimodal.
- Final document-metadata review stays agent-side; it is one judgment call per
  document, not bulk work.

### 3.4 Optional cleanups

- ~~`forge_embeddings.py` still defaults its model name to
  `Qwen3-Embedding-0.6B`~~ — done; see §7.7.
- `project-extraction`'s `post_chat_json` predates `forge_llm` and duplicates it.
  It is the most battle-tested client in the repo (threaded, preemptible,
  lease-aware); only consolidate if you are prepared to port preemption carefully.

---

## 4. The gate each conversion must pass

The pilot's A/B, repeated per skill. Do not enable a worker by default until it
passes.

1. Same corpus through both paths: the new worker on `chat`, and the same worker
   pointed at `think` with `--base-url http://llms:8008/v1/chat/completions
   --model code` as the quality baseline.
2. Compare **output counts, coverage across the skill's categorical axis, and
   spot-checked evidence fidelity**. Counts alone will not reveal the enumeration
   problem in §2.1 — coverage is what exposes it.
3. Run the skill's own `validate --json` on both and compare.
4. Confirm zero fabricated quotes/values by re-checking against sources
   independently of the extraction path.
5. Keep the manual `next`/`record` path working as the fallback and as the control
   for the next comparison.

---

## 5. Traps already hit, so you do not hit them again

- **Tests will reach real endpoints.** Verification talks to a second service that
  no existing fixture configured, so adding it silently pointed suites at live
  :8008 — one suite went from 4s to 163s. Both vault test helpers now append
  `--no-verify` unless a test passes `--think-url`, and Python suites default
  `PI_FORGE_AGENT_DIR=/nonexistent-agent-directory` so endpoint resolution cannot
  pick up the developer's own settings. Do the same for any new worker.
- **`forge-skills.test.mjs` sets `FORGE_BASE_CHAT_URL` globally** to a dead port.
  Environment beats settings by design, so a test proving settings-based
  resolution must *remove* that variable, not merely override it — and a helper
  that merges over `os.environ` will silently re-add it.
- **`run_script(..., environment=...)` replaces the inherited environment
  outright** in the organizer suite, precisely so a test can prove behavior in the
  *absence* of a variable.
- **A journal that enforces one record per item will reject escalation.**
  `literature-extraction` marks re-extractions `supersedes: true`, and
  `load_results` replaces the earlier record only when that flag is present, so an
  accidental duplicate still fails loudly.
- **Background calls need a writable agent directory.** `forge_llm` degrades to a
  foreground call when the lease directory cannot be created, rather than dying —
  the work matters more than the cooperative scheduling.
- **Resuming refuses changed endpoints.** A test that restarts a stub server on a
  new port cannot resume a run; keep one endpoint alive across both invocations.
- **The organizer's classification cache keys on model and URL**, so any endpoint
  change invalidates it and forces one full re-classification. Expected, but worth
  saying out loud in a release note.

---

## 6. Verifying the whole thing still works

```bash
npm run check
```

Then, against live endpoints:

```bash
python3 forge/skills/vault-organizer/scripts/vault-organizer.py doctor --vault <vault>
```

`doctor` should report the chat service as `reachable, non-thinking` with
`hiddenTokens: 0`. If it warns that the bulk endpoint is thinking, routing is
misconfigured and every file is costing a few hundred wasted tokens — that warning
is the single most useful signal in this whole system, because nothing else in the
response body reveals it.

`web-research doctor --json` now runs the same probe and reports it as
`chatProbe`, so a JavaScript install can be checked without a vault.

---

## 7. What actually happened

Everything in §3 is done. What follows is what the work measured and where the
guidance above turned out to be wrong.

### 7.1 Corrections to this document

- **§3.4 overstated the `project-extraction` risk.** Consolidating
  `post_chat_json` did not require porting preemption, because
  `forge_llm._post_preemptible` already *was* that port — same worker thread,
  same 1s lease refresh, same connection close, same `InterruptedError`. The only
  real delta was journaling, and `forge_llm.call` already returned the row
  `inference_schedule.jsonl` wants. One capability was genuinely missing:
  `allow_preemption=False`, which the slot probe needs so a diagnostic is not
  abandoned by the first interactive turn. `forge_llm.call` takes that flag now.
- **§3.4 also mis-describes `project-extraction` as unverified.** Reconciliation
  and relationship review already ran on `think`. The real gap was that nothing
  reviewed the *extraction*: quote exactness is enforced when a packet is
  recorded, so what went unchecked was the judgment a verbatim quote cannot
  settle — who owes an obligation, and whether something merely discussed was
  recorded as committed. That review exists now.
- **§3.3 had an unstated prerequisite.** `document-ingest` is `.mjs`, and there
  was no JavaScript equivalent of `forge_llm.py`. `forge-llm.mjs` and
  `forge-verify.mjs` exist for that reason, and `web-collection` and
  `web-research` moved onto them too.
- **§3.2's `NEAR_DUPE_AUTO = 0.97` does not transfer.** That threshold asks "is
  this the same text twice"; reusing an answer across rows asks "do these rows
  deserve the same answer", which paraphrases satisfy much lower. Three genuine
  duplicate groups in a support-ticket sheet sat between 0.945 and 0.964, so 0.97
  reused nothing at all. 0.85–0.92 found exactly those groups with no false
  merge; 0.80 began merging distinct problems. The default is 0.92.

### 7.2 What the A/B gates measured

| Skill | Bulk on `chat` | Baseline on `think` | Coverage |
| --- | --- | --- | --- |
| `personal-admin` | 44.7s, 40 facts | 3m49s, 44 facts | 8 of 8 fact types on both |
| `spreadsheet-analysis` | 2.7s, 5 model calls | 49.8s, 10 calls | identical values on every row |
| `document-ingest` | 3 chunks, 49s | — | one consistent outline |

§2.1 held everywhere it was tested. The enumeration clause naming the fact types
that get skipped is what kept `personal-admin` at full breadth; without checking
coverage rather than counts, nothing would have shown the difference.

### 7.3 Where §2.1 does *not* apply

Naming a **formatting** dimension backfires. Row enrichment's one inconsistency
against the thinking baseline was a stray trailing period, and adding a prompt
clause asking for consistent punctuation took that from one cell in ten to six:
it made the model attend to punctuation without giving it a rule.

Category coverage and format consistency are different problems. Naming what gets
skipped fixes coverage. Format belongs to §2.5 — a deterministic pass, which can
also see every row at once, where a stateless call never can.

### 7.4 Two defects the conversions exposed

- **A verifier with no evidence rubber-stamps.** `personal-admin`'s first review
  pass approved a seeded extraction that tripled a balance, invented a five-day
  deadline, and reversed who owed a referral code. The payload carried
  paraphrased facts and no document. `literature-extraction` gets away with this
  because `direct_quotes` carries evidence inline; a skill whose items are
  paraphrases must send the source. Check this when adding any new review.
- **Escalation had no resume guard.** A flag verdict lives in the journal
  forever, so every resumed run bought a fresh reasoning-model call for every
  item ever flagged — 85s per resume that should cost nothing.
  `forge_verify.escalate` now returns recorded outcomes marked `resumed`, and
  callers must skip re-committing them. `forge-verify.mjs` does the same.

Related: a rejected *reused* answer is a rejected grouping, not a wording
complaint. Propagating a replacement value repeats the mistake in every row that
copied it; the group is broken up and each row re-answered instead.

### 7.5 Chunks are not independent

The question §3.3 left open has an answer: **yes, chunk N depends on chunk N−1**,
and the dependency is heading depth. A four-section report cleaned statelessly
came back with sections three and four demoted a level — chunk 1 set the depth
and every later chunk re-derived its own. Carrying the headings used so far fixes
it completely, and is far cheaper than the previous chunk's text.

Paragraphs were never at risk: the splitter breaks on blank lines, so no
sentence, list, or table is cut mid-structure.

### 7.6 Deliberately not converted

`coding`, `file-conversion`, `organize-folder`, `report-output`, `site-builder`,
`skill-builder`, `transcription`. These are either deterministic tooling with no
model call, or single-judgment agent authoring where §3.1's rule applies —
deliverables stay agent-authored.

The one open candidate is **`transcript-cleanup`'s faithful-cleanup track**. A
long faithful cleanup *is* per-segment bulk work done in the agent's context, and
it has a strong deterministic invariant available (substantive-token retention
against the source, the same shape as quote exactness). It is left alone because
it changes a user-visible quality track rather than an internal pipeline, so the
§4 gate matters more there than anywhere else.

### 7.7 Loose ends

- `forge_embeddings` now defaults its model name to `embed`, matching
  `connectedServices.embeddings`. The server ignores the name, but it keys the
  vault embedding caches: **an existing cache is invalidated and re-embeds once.**
- `web-collection`'s LLM link filter is reachable only through an interactive
  readline prompt, so its guard against following URLs the page never linked to
  has no automated test. Making `spider` scriptable would fix both.

## 8. The third tier, and per-stage routing

The split this document describes is `chat` vs `think`, chosen once per command.
`forge/evals` then measured stages rather than commands, and found the two facts
that broke that arrangement:

- **A single command wants both directions at once.** `vault-transcripts` cleanup
  is the case: on diarized multi-speaker material the small model is the only one
  that clears the gate (7/8 against the baseline's 1/8) while the thinking model
  scores 0/8, rewriting and compressing away a quarter of the transcript. On a
  single voice that reverses exactly — thinking takes 2/8 to 8/8 and invented
  words from 4.38 to 0.00. Same stage, opposite models, decided per chunk by the
  speaker count.
- **Rebuilding a service dict by hand lost the fields that make a non-default
  backend work.** Six sites did it, dropping `contextTokens` and
  `chatTemplateKwargs`, so a preflight would pass a prompt at twice a small
  backend's real ceiling and no `enable_thinking: false` would be sent. Both are
  invisible until a service points somewhere other than this deployment — which
  is exactly what routing does. `forge_llm.service_from_args` is the fix; use it
  rather than composing a dict from `args.base_url`.

So there are now three named services and a table. `task` (:8007) is **off by
default**: unlike `chat` and `think`, which are two request-shaping profiles in
front of one llama-server, it is a separate backend behind a router at
`MODEL_ROUTER_MAX=1` shared with `embed`/`ocr`/`rank`, so a stage that alternates
with embeddings pays a swap each time. Moving up to `think` costs latency and
nothing else; moving out to `task` costs a swap.

`forge/lib/forge_routing.py` (and its `.mjs` twin) maps a stage label to a
service. The label is the `task=` argument call sites already passed and that was
only ever journaled. Anything not in the table runs on `chat`, because a stage
nobody measured is a stage with no evidence behind moving it.

### What this corrects about §2.1

§2.1 says a non-thinking model does not enumerate categories on its own, and that
telling it to walk the list explicitly took it from 4 item types to 12. That
holds. What the eval adds is that **the fix does not transfer by making the model
bigger or by turning reasoning on**: on `enumeration-breadth` the thinking profile
covers more item types (13.0 against 10.1) and returns far fewer items (19.0
against 32.9), and neither profile clears the case. Enumeration breadth is a
prompt property, not a model property.

### The trap this arrangement introduces

A stage label is now load-bearing, so a call that reuses another stage's
transport inherits its route. Two calls in `vault-connections` did exactly this —
grouping claims into topic notes and composing a note summary both went through
the classifier's helper — and would have silently moved to the thinking model at
20x the cost because the *classifier* moved there. They now pass their own stage
names. When adding a call, ask what stage it is, not what function it borrows.

A stage that resolves to a disabled or unconfigured service falls back to `chat`
and says so: the resolved service carries `fallback`, and `routing_record()`
reports `routedTo` beside `ranOn`. A run whose stage silently ran somewhere other
than intended is the failure this whole arrangement has to make impossible.

### 8.1 Routing to `think` disables the verification it is checked by

Found while wiring §8, and it is the sharpest consequence of the whole
arrangement. **Every skill verifies on `think`.** So the moment a bulk stage is
*also* routed there, the reviewer is the model that wrote the thing:

- `vault-capture` splits a braindump, then asks `think` whether the notes cover
  the braindump — a review of the split.
- `vault-transcripts` cleans a chunk, then asks `think` to compare the cleaned
  text against the raw transcript — a review of the cleanup.
- `vault-organizer` classifies, then asks `think` to review the classification,
  then escalates flagged notes back to `think`.

Routing to `task` has the opposite effect and is strictly better than what
existed before: a 4B doing the work and a 27B reviewing it is *more* independent
than the 27B-non-thinking / 27B-thinking pair, which was always the same weights.

`verify_packets` now takes `produced_by` — one service for the batch, or
`{item id: service}` where a stage routes per item — and marks each verdict
`independent`. A flag from a non-independent reviewer still counts and is still
escalated: a reasoning pass over its own output can catch a contract violation.
An **"ok" stops reading as approval**, and `independence_warning()` puts that in
the run report. This is the existing rule — an unreachable verifier must never
read as approval — applied to a verifier that is present but not impartial.

`classify-note` is held on `chat` for this reason among others: its whole
verification is of the classification, so routing it would leave nothing
checking the organizer at all. `summarize-transcript` is held despite the report
clearing it for `task`, because `summary-report` is not and the two share a
contract.

### 8.2 Two traps this cost before they were caught

**A routed stage that cannot reach its service used to kill the run.** `think`
previously carried only verification, and an unreachable one degraded politely.
Once a bulk stage routes there, the same dead endpoint takes down work that used
to complete. `forge_routing.disable_unreachable()` probes each routed service
once at the start of a run and pins the dead ones to `chat` with a warning that
says the substitution happened — the same shape as the verifier's own
degradation, and better than the marginally-worse model it routed away from.

**Routing must never override a service the caller named.** The first wiring had
`call_json` prefer the route over its `service` argument, which silently sent
`vault-capture`'s escalation — the path whose entire purpose is redoing a flagged
note *with reasoning* — to the bulk model. The convention is now uniform: pass a
service to insist on it, pass `None` to ask for the stage's route.
