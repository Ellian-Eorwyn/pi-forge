---
name: project-extraction
description: Extract refreshable, source-backed project controls from grants, awards, proposals, scopes of work, contracts, amendments, work plans, reports, presentations, meetings, interviews, correspondence, budgets, and mixed project folders. Use when users need deliverables, obligations, requirements, dates, milestones, tasks, decisions, actions, risks, issues, dependencies, stakeholders, acceptance criteria, reporting cadence, or proposal checklists tracked across documents. Use document-ingest first for PDFs, DOCX, PPTX, media, or other raw inputs. Do not use for research-claim synthesis or polished publication output; route those to literature-extraction or report-output.
---

# Project Extraction

Build a reviewable project-control workspace without confusing proposed work,
awarded obligations, deliverables, tasks, or human-reported status.

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

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Read
   [references/project-control-contract.md](references/project-control-contract.md)
   before extraction or reconciliation.
2. Check the local workflow and initialize a run directory. Repeating the same
   command and output resumes a compatible marked run:

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py doctor
   python3 <skill-directory>/scripts/project-extraction.py init <inputs...> \
     --output <new-directory> [--title "Project name"]
   ```

   Use `status <run> --json` to inspect the frozen source snapshot without
   changing it, and `retry <run> --item <packet-id>|--all-failed` for explicit
   permanent-failure retry.

3. Process exactly one bounded packet at a time. Read the returned packet,
   classify its document role, and write a JSON object containing
   `documentRole` and `items`. Keep direct quotes and source locators.

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py next <run>
   python3 <skill-directory>/scripts/project-extraction.py record <run> \
     --packet-id <id> --items-file <working/items.json>
   ```

   Record `needs_review`, `skipped`, or `failed` with `--status` and `--note`
   when a packet cannot be extracted. Never hide an unread packet behind an
   empty success.
4. Reconcile the evidence into canonical controls. Process each review packet
   once, preserving suitable existing control IDs. Every evidence ID must be
   referenced by one control or dispositioned as `contextual`, `duplicate`,
   `superseded`, or `conflicting`.

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py reconcile <run>
   python3 <skill-directory>/scripts/project-extraction.py next-review <run>
   python3 <skill-directory>/scripts/project-extraction.py record-review <run> \
     --review-file <working/review.json>
   ```

5. Build the registers. Author every scaffolded Markdown section from the
   source-backed controls and the separate human-maintained status overlay.

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py build <run> \
     [--as-of YYYY-MM-DD]
   ```

6. Validate before completion:

   ```bash
   python3 <skill-directory>/scripts/project-extraction.py validate <run> \
     --fix-hints --json
   ```

## Refresh

Run `refresh <run>` after source files change. Unchanged revisions retain their
packets and reviewed controls. Process queued packets, then repeat reconcile,
review, build, and validate. Removed revisions remain in source history but
leave current registers. `project_status.csv` is never replaced with inferred
status; rows affected by changed or removed controls are marked for human
review.

## Safety

- Preserve source files and their hashes. Do not read from reserved
  `Ingest/`, `Originals/`, or `Generated/` folders during normal discovery.
- Keep document role and commitment level explicit. Never infer that document
  type establishes legal precedence or silently select a controlling source.
- Keep deliverables, milestones, tasks, requirements, and acceptance criteria
  distinct even when one source describes them together.
- Normalize only unambiguous absolute dates. Retain relative, recurring, and
  conditional date language, triggers, and offsets without fabricating dates.
- Treat grant and contract output as document organization, not legal advice.
