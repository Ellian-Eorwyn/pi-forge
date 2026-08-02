---
name: skill-tuner
description: Mine a completed pi session log for pain points - errors, silent failures, output truncation, ambiguity the model had to reason through, retry loops, backend limits - and author a bounded, evidence-cited Markdown report for improving the skills the session used. Use when a session went badly or to tune skills from real transcripts. Extraction, review, and authoring all run on the local services; the session log is read-only.
---

# Skill Tuner

Turn one recorded agent session into a report another model can act on: every
pain point cited back to exact log entries, every recommendation aimed at making
a small local model punch above its weight - clearer instructions, less
ambiguity, smaller deterministic steps, less reliance on model knowledge and
reasoning.

## Natural Language Routing

Use this skill when the user asks why a session went badly, wants a session log
or transcript of an agent run analyzed for friction, or wants skills improved
from real usage evidence. The input is a pi session log (format v3, the
`.jsonl` files pi writes per session) - not a voice transcript
(`vault-transcripts`) and not a research document (`literature-extraction`).

## Command Card

- `doctor`: probe the chat, think, and embeddings services.
- `init <session.jsonl|dir> --output <run-directory>`: parse, render the elided timeline, run the deterministic scan, chunk, and freeze the input snapshot. `--report-budget-tokens` (default 16384) and `--chunk-chars` (default 48000) are frozen into the run.
- `status <run-directory> --json`: durable progress, evidence counts, input drift.
- `extract <run-directory>`: mine every pending chunk on the chat service, serially so open threads chain across chunk boundaries. `--limit <n>` for a trial.
- `verify <run-directory>`: one batched review of all evidence on the thinking service; flags are escalated with the reviewer's objection and the chunk in context.
- `synthesize <run-directory>`: deterministic merge and ranking into `synthesis/groups.json` plus a bounded `authoring_context.md`; `--no-embeddings` skips the advisory clusters.
- `report <run-directory>`: author the report sections on the thinking service under explicit character budgets and assemble `report.md` with its evidence appendix.
- `validate <run-directory>`: deterministic gates - budget, resolvable citations, appendix consistency, verification honesty. Marks the run complete when clean.
- `retry <run-directory> --item <chunk-id>|--all-failed`: requeue `needs_review` chunks; clears the generated report artifacts so later stages rebuild.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path and check the
   services:

   ```bash
   python3 <skill-directory>/scripts/skill-tuner.py doctor
   ```

2. Use an output directory under `forge-output/skill-tuner/<session-id>/`.
   Session logs are agent infrastructure, not vault content, so this skill
   always writes to `forge-output` - never to a vault workflow root. If the
   directory holds a compatible run, `init` resumes it; a changed session file
   is refused (a session log is immutable - drift means analyzing a different
   file, which is a new run, so there is deliberately no `refresh` command).

   ```bash
   python3 <skill-directory>/scripts/skill-tuner.py init <session.jsonl> --output <run-directory>
   ```

   Read the result: entry count, chunks, deterministic seeds, and which skills
   the session touched. `scan.json` is worth a glance before extraction - it is
   the ground truth the model findings must explain.

3. Run the pipeline, in order, each stage resumable:

   ```bash
   python3 <skill-directory>/scripts/skill-tuner.py extract <run-directory>
   python3 <skill-directory>/scripts/skill-tuner.py verify <run-directory>
   python3 <skill-directory>/scripts/skill-tuner.py synthesize <run-directory>
   python3 <skill-directory>/scripts/skill-tuner.py report <run-directory>
   python3 <skill-directory>/scripts/skill-tuner.py validate <run-directory>
   ```

   Extraction is stateless per chunk on the non-thinking service: the contract
   plus that chunk, with only a compact brief and the open threads carried
   between calls. Quotes are checked byte-exact against the rendered timeline
   before anything is recorded; a fabricated quote costs one corrective retry
   and then the chunk is marked `needs_review`. Progress is one stderr line per
   unit; stdout is one JSON result per command.

4. Read `report.md` and report its outcomes honestly:

   - The **Verification and Coverage** section leads. When it says
     **Not verified**, every finding is unreviewed extraction output - say so,
     and do not present the report as reviewed.
   - Items listed as needing human review were flagged and could not be
     corrected; read the cited timeline entries yourself before trusting or
     discarding them. Dropped items stay listed with the reviewer's reason -
     review never silently removes evidence.
   - Empty categories mean the session showed nothing for them or extraction
     missed them; treat absence as unexamined, not as a clean bill.

5. A chunk stuck in `needs_review` can be requeued with `retry`, or mined by
   hand: read the chunk file under `chunks/`, write items following
   [references/extraction-contract.md](references/extraction-contract.md), and
   re-run from `verify`. The contract file mirrors the prompt constants in
   `scripts/skill-tuner.py` - if you change one, change both.

## Feeding improvements back

The report is input for improving skills, not a work order. Route the actual
changes to the right place: `skill-builder` for editing a skill's SKILL.md and
contracts, `coding` for script changes. Every recommendation names a
`change_type` (instruction_clarification, decomposition, deterministic_guard,
contract_tightening, backend_config, new_reference, new_tool) and cites the
evidence, so the improving model can judge each one against the appendix.

For a suite over many sessions, run one directory per session;
`evidence.jsonl` carries `sessionId` on every item so a future aggregator can
concatenate runs without re-mining.

## Safety and Output Rules

- The session log is read-only; the skill writes only inside its own run
  directory. `retry` deletes only generated artifacts of this run (sections,
  report) so later stages rebuild them.
- Never present an unverified or partially reviewed report as reviewed, and
  never drop a flagged item silently.
- Quotes in the report are verbatim from the rendered timeline; elided payload
  middles are uncitable by construction.
- Severity and category vocabularies are closed; the report's method section
  defines them so the report stands alone for a reader who never saw the
  session.
