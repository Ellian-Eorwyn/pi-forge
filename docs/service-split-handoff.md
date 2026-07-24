# Finishing the thinking/non-thinking split

This is a handoff for whoever continues the work started on `feat/vault-connections`
(commits `2f9e976e`…`a186cbf4`). The routing and verification layers are built and
proven; three batch skills still route every per-file operation through the agent's
own thinking context. This document says what to do, what to copy, and — most
importantly — what had to change about *prompting* to keep quality when work moved
off the thinking model.

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

## 3. Remaining work

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

- `forge_embeddings.py` still defaults its model name to `Qwen3-Embedding-0.6B`
  while `connectedServices.embeddings` says `embed`. The server ignores the name,
  so this is cosmetic — but the name keys embedding caches and run fingerprints,
  so changing it forces a re-embed. Align it deliberately or leave it alone.
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
