---
name: project-extraction
description: Extract, search, and continuously refresh source-backed project controls, focused team views, schedules, and Gantt charts from grants, awards, proposals, scopes of work, contracts, amendments, work plans, reports, presentations, meetings, interviews, correspondence, budgets, and mixed project folders. Use when users need questions answered from an existing extraction, relevant project documents or controls found quickly, new files processed from a marked Inbox, or deliverables, obligations, requirements, dates, milestones, tasks, decisions, actions, risks, dependencies, stakeholders, acceptance criteria, reporting cadence, proposal checklists, team scope, or timelines tracked across documents. Use document-ingest first for raw files. Do not use for research-claim synthesis or polished publication output; route those to literature-extraction or report-output.
---

# Project Extraction

Build a reviewable project-control workspace without confusing proposed work,
awarded obligations, deliverables, tasks, or human-reported status.
New runs use schema version 2. Version-1 directories are read-only legacy
artifacts and require a new extraction rather than in-place migration.

## Routing

Use finalized Markdown/text from `document-ingest` and CSV exports from
`spreadsheet-analysis`. When invoked after folder ingest, write the run to:

```text
<source-folder>/Generated/Project-Extraction
```

If the folder includes recordings, complete `transcription` and
`transcript-cleanup` before this workflow. Use `report-output` only after the
registers are valid when polished DOCX, HTML, briefings, or slide outlines are
requested.

## Runtime preflight

Automatic background processing is opt-in and requires a llama.cpp-compatible
chat endpoint with two reserved slots. Configure
`connectedServices.chat.scheduling` with `enabled: true`, interactive slot `0`,
background slot `1`, and the desired idle/yield limits. The deployment must
retain idle-slot prompt caches and provide enough total context for the
interactive and worker slots. Run `doctor --json --probe-slot` before corpus
work. Never share slot 0 or silently fall back to one slot.

```json
{
  "scheduling": {
    "enabled": true,
    "interactiveSlot": 0,
    "backgroundSlot": 1,
    "idleGraceMs": 2000,
    "yieldMs": 1000,
    "backgroundOutputTokens": 4096
  }
}
```

The external llama.cpp deployment needs at least 164k total effective context,
prompt caching, nonzero host cache RAM, context checkpoints, and idle-slot
caching. A 262,144-token deployment can keep the interactive context below
128k while reserving roughly 36k prompt capacity for the worker. Stack-manager
configuration is outside this skill and must be completed separately.

Separate slots protect each prompt cache. Cancellation when an interactive
lease appears is best effort; it cannot guarantee proxy-level compute priority
during an already-running prefill.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Read
   [references/project-control-contract.md](references/project-control-contract.md)
   before extraction or reconciliation.
2. Before every project action, run `inbox-status`. If files or an active batch
   are present, run `inbox-sync` and follow its document-ingest review action
   until publication succeeds. Then run the returned project `process` action
   before relying on refreshed controls. Search may use the last completed index
   while intake is incomplete, but must report the warning.

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py inbox-status <run>
   python3 <skill-directory>/scripts/project-extraction.py inbox-sync <run>
   ```

   New single-folder runs create `<project-root>/Inbox/`. Configure existing
   multi-root runs with `inbox-sync <run> --inbox <project-root>/Inbox`.
   Successful intake publishes cleaned Markdown under `Sources/Inbox/`, archives
   originals under `Originals/Inbox/`, and refreshes the project. Never read
   unprocessed Inbox files as project evidence.
3. Check the local workflow and initialize a run directory. Repeating the same
   command and output resumes a compatible marked run:

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py doctor --json --probe-slot
   python3 <skill-directory>/scripts/project-extraction.py init <inputs...> \
     --output <new-directory> [--title "Project name"] \
     [--focus "scope"] [--team NAME] [--workstream NAME]
   ```

   Use `status <run> --json` to inspect the frozen source snapshot without
   changing it, and `retry <run> --item <packet-id>|--all-failed` for explicit
   permanent-failure retry.

4. Start the durable worker. It screens, extracts, reconciles, builds, indexes,
   and validates serially. Foreground processing is useful for diagnosis;
   `--background` returns immediately. Inspect compact status and control it
   between model calls:

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py process <run> --background
   python3 <skill-directory>/scripts/project-extraction.py status <run> --json
   python3 <skill-directory>/scripts/project-extraction.py pause <run>
   python3 <skill-directory>/scripts/project-extraction.py resume <run>
   python3 <skill-directory>/scripts/project-extraction.py stop-after-current <run>
   ```

   The worker uses stable stage prefixes, slot 1, prompt caching, approximately
   112,000-character initial packets, and response usage to calibrate toward
   32,768 source tokens within the configured prompt ceiling. Embeddings may
   rank relevance and flag duplicates, but cannot decide semantic identity.
5. Retain `next`, `record`, `reconcile`, `next-review`, and `record-review` for
   diagnosis. Every packet requires one structured disposition; generic
   unprocessed skips are invalid. Quotes must match frozen packet text.

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py reconcile <run>
   python3 <skill-directory>/scripts/project-extraction.py next-review <run>
   python3 <skill-directory>/scripts/project-extraction.py record-review <run> \
     --review-file <working/review.json>
   ```

6. Build and validate produce registers, briefs, metrics, a hybrid search index,
   and Gantt CSV, Mermaid Markdown, and accessible HTML. Only source-backed
   exact dates and human forecasts are scheduled; everything else remains
   unscheduled.

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py build <run> \
     [--as-of YYYY-MM-DD]
   ```

7. Derive reusable focused views from a completed comprehensive run:

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py focus <run> \
     --output <view-directory> --team NAME [--workstream NAME]
   ```

   Focused runs still screen the frozen inventory and include direct matches,
   dependency closure, shared milestones, decisions, risks, and reporting
   obligations. Derived views report gaps when the parent was already scoped.

## Search

Build or repair an index on any completed version-2 extraction without
re-extracting its documents, then retrieve controls, evidence, and source
passages:

```bash
python3 <skill-directory>/scripts/project-extraction.py index <run>
python3 <skill-directory>/scripts/project-extraction.py search <run> \
  --query "What approval is required before publication?" --limit 10
python3 <skill-directory>/scripts/project-extraction.py show <run> \
  --hit-id <hit-id> [--full-source]
```

Return ranked hits first. Use `show` on the strongest source-backed hits and
load full documents only when retrieved passages are insufficient or ambiguous.
Answer with source paths, locators, and control/evidence IDs. Embeddings improve
ranking when available; lexical search remains valid when they are not. Never
use similarity to decide authority, merge controls, or replace direct evidence.

## Refresh

Run `refresh <run>` after source files change. Unchanged revisions retain their
packets and reviewed controls. Process queued packets, then repeat reconcile,
review, build, and validate. Removed revisions remain in source history but
leave current registers. `project_status.csv` is never replaced with inferred
status; rows affected by changed or removed controls are marked for human
review.

## Safety

- Preserve source files and their hashes. Do not read from reserved top-level
  `Inbox/`, `Ingest/`, `Originals/`, or `Generated/` folders during discovery.
- Keep document role and commitment level explicit. Never infer that document
  type establishes legal precedence or silently select a controlling source.
- Keep deliverables, milestones, tasks, requirements, and acceptance criteria
  distinct even when one source describes them together.
- Preserve packet-specific quotes and locators when larger calls contain
  multiple compatible packets.
- Normalize only unambiguous absolute dates. Retain relative, recurring, and
  conditional date language, triggers, and offsets without fabricating dates.
- Treat grant and contract output as document organization, not legal advice.
