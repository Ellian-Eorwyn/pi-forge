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
`skill-builder`, `transcription`, `vault-handoff`. These are either deterministic
tooling with no model call, or single-judgment agent authoring where §3.1's rule
applies — deliverables stay agent-authored.

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
