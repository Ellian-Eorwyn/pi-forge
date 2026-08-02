# skill-tuner extraction contract

<!-- This file mirrors the prompt and schema constants in scripts/skill-tuner.py.
If you change one, change both. -->

## Evidence item schema

Every evidence item has exactly these fields:

- `item_type` - one of: tool_error, silent_failure, output_truncation, context_loss, ambiguity, knowledge_reliance, retry_loop, wasted_work, backend_limit, environment_mismatch, missing_guardrail, user_correction
- `severity` - one of: blocker, major, minor, papercut
- `attribution` - `{"skill": <a skill the session touched, or null>, "layer": <one of: skill, backend, harness, crosscutting, unknown>}`
- `text` - what happened and why it was friction (required, nonblank)
- `direct_quotes` - a short verbatim quote copied character-for-character from the rendered timeline, or null; never from inside an `⟦ELIDED N OF TOTAL CHARS⟧` marker, and never the scan seeds' `detail`/`excerpt` metadata
- `locator` - `{"line": <the L-number of the timeline entry>}`; the L-number is the entry's line in the source session log
- `interpretation` - explicit | inferred | unclear
- `confidence` - high | medium | low
- `seed_ids` - ids of the deterministic scan findings this item explains, or `[]`
- `change_type` - one of: instruction_clarification, decomposition, deterministic_guard, contract_tightening, backend_config, new_reference, new_tool
- `recommendation_hint` - one sentence on the fix, or null
- `notes` - optional clarification, or null

At most 20 items per chunk. Each chunk response also carries
`open_threads` (at most 12 notes of at most 200 characters on issues
still unfolding at the chunk boundary) and `chunk_summary` (at most two sentences,
500 characters).

## Deterministic guarantees around the prompts

- Quote fragments - split on quote marks, ellipses, newlines, semicolons, and
  sentence boundaries - of 12+ characters are each verified byte-exact
  (whitespace- and smart-quote-insensitive) against the rendered timeline before an
  extraction is recorded; a violation costs one corrective retry, then the chunk is
  recorded `needs_review`. Per-sentence matching means a quote whose real sentences
  were reordered still verifies.
- An `item_type` echoing a seed kind with one honest translation is normalized
  deterministically (`compaction` -> `context_loss`, `repeated_user_text` ->
  `user_correction`) with a note; stall kinds stay validation errors because their
  friction class depends on cause.
- A quote copied from the scan seeds' `detail`/`excerpt` metadata is salvaged
  rather than failed: the quote becomes null with a note, and the claim keeps its
  `seed_ids` corroboration.
- A quote that exists in the timeline but not in the cited entry relocates the locator
  deterministically and appends a note; it does not burn the retry.
- Evidence ids match `\bp\d{6}\b` and are never reused, even across retries.
- Every path, flag, and identifier a recommendation names in backticks is checked
  against what the session demonstrated. A path grounds when all its literal segments
  of 4+ characters appear as real path segments and at least one is 6+ characters
  (so a bare `wiki` cannot ground a fabricated tree); numbered vault names are indexed
  with and without their number prefix. Ungrounded paths and flags cost one rewrite
  request and are then listed under **Unverified Recommendation Details**; ungrounded
  identifiers are advisory. The report's own controlled vocabulary is never flagged.
  `--ground-root` adds directories a path may also resolve against.
- Diagnoses are verified against their evidence; recommendations are not, and the
  report says so in its method section.
- The report is capped at the configured token budget using ceil(characters / 4);
  the default is 16384 tokens (65536 characters).

## EXTRACT_SYSTEM (chat service, one call per chunk)

```
You mine one chunk of a rendered agent-session timeline for pain points that made the
session slower, wronger, or more confusing than it needed to be, so that skill
instructions can be improved for a small local model.

Return exactly one JSON object and nothing else:
{"items": [...], "open_threads": [...], "chunk_summary": "<= 2 sentences"}

Every item has exactly these fields:
- "item_type": one of tool_error, silent_failure, output_truncation, context_loss,
  ambiguity, knowledge_reliance, retry_loop, wasted_work, backend_limit,
  environment_mismatch, missing_guardrail, user_correction
- "severity": "blocker", "major", "minor", or "papercut"
- "attribution": {"skill": <a skill named in sessionBrief.skillsSeen, or null>,
  "layer": "skill", "backend", "harness", "crosscutting", or "unknown"}
- "text": what happened and why it was friction (required, nonblank)
- "direct_quotes": a short verbatim quote copied character-for-character from the
  timeline chunk, or null. Never quote from inside an ELIDED marker.
- "locator": {"line": <the L-number of the timeline entry the quote or event is in>}
- "interpretation": "explicit" when the timeline shows it directly, "inferred" when you
  concluded it from the timeline, "unclear" when the timeline is ambiguous
- "confidence": "high", "medium", or "low"
- "seed_ids": ids of the deterministic findings in this chunk this item explains, or []
- "change_type": one of instruction_clarification, decomposition, deterministic_guard,
  contract_tightening, backend_config, new_reference, new_tool
- "recommendation_hint": one sentence on the fix, or null
- "notes": optional clarification, or null

Rules:
- The deterministic findings under "seeds" were already detected by a script. Your job
  with them is context and attribution - what caused them, which skill they belong to,
  what they cost - not rediscovery. Do not restate a seed without adding cause,
  attribution, or consequence.
- Work through the item types in order and ask what this chunk shows for each one
  before moving on. Do not stop at the tool errors, truncations, and retry loops the
  seeds already name: chunks also carry ambiguity the model had to reason through,
  knowledge_reliance where it used world knowledge a reference file could encode,
  wasted_work, missing_guardrail, and user_correction - these live in the thinking
  blocks and the narrative, and no seed will point at them.
- "direct_quotes" must be copied from the timelineChunk text itself. The seed "detail"
  and "excerpt" strings are scan metadata, not timeline text - quoting them fails
  verification. Quote the timeline entry the seed points at instead, or use null.
- "item_type" describes the friction, not the seed. A seed's kind is not an item_type:
  a compaction seed is context_loss, a repeated_user_text seed is usually
  user_correction, and a stall seed is wasted_work when time was lost redoing or
  waiting on avoidable work, or backend_limit when the backend itself was slow.
- Never invent events the chunk does not show; label inference as "inferred".
- An empty items array is the right answer for an uneventful chunk.
- At most 20 items; prefer the highest-severity ones.
- "open_threads": at most 12 short notes (<= 200 characters each) on issues still
  unfolding at the chunk boundary. Copy forward the incoming open_threads that are
  still open, close the ones this chunk resolves, and add new ones.
```

## VERIFY_SYSTEM (thinking service, batched; the verdict contract is appended by forge_verify)

```
You are reviewing pain-point evidence mined from an agent-session timeline by a faster
model without reasoning. Each item shows its claim, its verbatim quote, its locator,
and its deterministic corroboration (a script-detected seed, or a note that it is
narrative-derived).
Flag an item when it is actually wrong: the quote or seed does not support the claim,
the skill attribution contradicts the locator's context, the item_type is wrong for
what the evidence shows, or the severity is inflated beyond what the evidence supports.
Do not flag an item for phrasing, for a severity you would nudge one step, or for being
small - papercuts are in scope. Do not flag a narrative-derived item merely because no
seed corroborates it; ambiguity and knowledge-reliance findings come from the narrative
by design and are judged on the quoted evidence alone. Do not flag an inferred item for
being inferred when it is labeled "inferred" and is plausible on the evidence shown.
```

## ESCALATE_SYSTEM (thinking service, one flagged item per call)

```
A reviewer rejected one pain-point evidence item mined from an agent-session timeline.
You see the objection, the original item, and the full timeline chunk it came from.
Return exactly one JSON object and nothing else: either the corrected item, with the
same fields as the original (item_type, severity, attribution, text, direct_quotes,
locator, interpretation, confidence, seed_ids, change_type, recommendation_hint,
notes), or {"drop": true, "reason": "<why the item is unsupportable>"} when the
timeline does not support any version of it.
A direct_quotes value must appear character-for-character in the timeline chunk, and
never from inside an ELIDED marker. Address the reviewer's objection specifically.
```

## REDUCE_SYSTEM (thinking service, only when the evidence digest exceeds the authoring budget)

```
You compress pain-point evidence records into a memo for a report author who will not
see the originals. Preserve every [p######] evidence id attached to any fact you keep,
exactly as written; never invent ids. Keep counts and severities honest. Prefer keeping
the highest-severity and most repeated issues. Group related records. Stay under the
character budget stated in the request. Return only the memo text.
```

## AUTHOR_SYSTEM (thinking service, one call per report section)

```
You write one section of a skill-tuning report about a recorded agent session, for a
reader - human or model - who never saw the session. The deeper purpose is helping a
small local model punch above its weight: turning observed friction into clearer
instructions, less ambiguity, smaller deterministic steps, and less reliance on model
knowledge and reasoning.

Rules:
- Every substantive claim cites evidence ids in square brackets, like [p000042]. Use
  only ids present in the provided evidence; never invent ids.
- For each issue: state what happened, why it hurts a small non-thinking local model,
  and the recommended change, naming its change_type and severity.
- Start each issue with a "### " heading. Never emit "## " headings - the report
  assembles those. The executive summary uses no headings at all.
- Stay under the character budget stated in the request. Plain Markdown, no
  placeholders, no code fences around the whole section.
- Recommendations must be concrete enough to act on: name the skill file, prompt
  clause, guard, or config knob to change when the evidence shows it.
Return only the section body text.
```
