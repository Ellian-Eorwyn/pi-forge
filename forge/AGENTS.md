# Forge Profile

Forge processes raw information into reviewable, reusable outputs. It supports
documents, transcripts, spreadsheets, web sources, code, personal materials,
complex project records, and reports. Do not assume Obsidian conventions or schemas unless the user
explicitly requests them.

Use `CAPABILITIES.md` as the compact capability index. Do not load every full
skill workflow into context at startup. When a task matches a capability, load
the relevant `skills/<name>/SKILL.md` file and follow its workflow guidance.
For skill creation, revision, audit, validation, packaging, or trigger-testing
tasks, load `skills/skill-builder/SKILL.md`; generated non-Forge skills should
default to `.agents/skills/<name>/SKILL.md`.

## Source Safety

- Preserve original files. Never overwrite, rename, move, or delete a source
  unless the user explicitly requests it.
- Write generated artifacts to a dedicated output directory. If the intended
  path contains a compatible incomplete batch run, resume it. If it contains a
  compatible complete run, report its completion summary. Use a numbered path
  only for a genuinely independent run; never adopt an unmarked legacy folder.
- Use working copies for transformations that could alter source content.
- Keep sensitive material local and avoid unnecessary copies.

## Provenance and Interpretation

- Record source paths or URLs, access dates for web sources, and SHA-256 hashes
  for local files when practical.
- Keep extracted source content separate from summaries, analysis, and drafts.
- Distinguish source facts, generated interpretation, and suggested next steps.
- Mark uncertainty, extraction damage, missing information, and assumptions
  explicitly. Never invent missing details.

## Reproducible Work

- Prefer deterministic scripts for repetitive extraction, conversion, and data
  transformations. Use the model for judgment, synthesis, cleanup, and drafting.
- Skills are for workflow judgment and output standards. Scripts/tools are for
  mechanical parsing, conversion, fetching, validation, hashing, filesystem
  operations, and manifest generation.
- Keep detailed reference material out of startup context; load it only when the
  selected skill asks for it.
- For batches, report every processed, skipped, failed, and review-needed item.
- Batch workflows follow `RUN_STATE_CONTRACT.md`: keep `run_state.json` and an
  fsynced `run_events.jsonl`, commit one bounded unit at a time, report input
  drift with `status`, and require explicit `refresh` before reconciling it.
- Log transformations and make lossy operations visible.
- Keep outputs readable by both people and future agents.

When a folder contains grants, awards, proposals, scopes of work, contracts,
work plans, project reports, presentations, meeting notes, or interviews and
the user needs deliverables, requirements, dates, actions, or risks tracked,
route finalized document-ingest outputs to `project-extraction`. Keep its
`project_status.csv` human-maintained. Use its `run` command for initialization
or resume, then Inbox intake, serial foreground extraction, model-assisted
reconciliation, build, and validation. Background mode is opt-in and must use
the cooperative inference lease. Artifacts do not imply completion: only a
successful validation transition does. Partial builds must use `--draft` and
retain coverage warnings. It can produce focused team/workstream views and
source-backed Gantt outputs. For questions about an existing
extraction, use its hybrid search first and load full source documents only
when retrieved passages are insufficient. Use `report-output` only for polished
downstream deliverables.

## Vault Workflow

The `vault-workflow` extension adds a plan -> execute -> verify loop for changes
to an Obsidian vault, driven by the single local model. The user drives phases
with `/plan`, `/execute`, `/verify` (and `/workflow off`); each phase sets the
tools and thinking behaviour, so follow the injected phase prompt:

- **plan** — read-only. Interview the user with the questionnaire tool until the
  goal is unambiguous, ground the plan in the real vault and schema note, write a
  detailed numbered plan, and ask for approval. Make no changes.
- **execute** — full vault tools, one change at a time. Dry-run first, show the
  result, and wait for an explicit "yes" before any `--apply` or file write.
  Prefer the vetted skills; run `vault-organizer` with
  `--base-url http://llms:8008/v1/chat/completions --think-prefill`. Run
  `vault-organizer.py doctor` after editing the schema note. Never delete notes;
  keep every path inside the vault.
- **verify** — read-only. Check the result against the plan (`doctor`, `status`,
  grep, read `report.md`) and report what passed, what did not, and follow-ups.

The vault schema note (`99 Meta/99.02 Schemas/0.00 Vault Schema.md`) is the sole
source of truth for that vault's folders and frontmatter. See
[docs/vault-workflow.md](../docs/vault-workflow.md) for the full contract.
