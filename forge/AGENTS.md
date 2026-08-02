# Forge Profile

Forge processes raw information into reviewable, reusable outputs. It supports
documents, transcripts, spreadsheets, web sources, code, personal materials,
complex project records, and reports. Do not assume Obsidian conventions or schemas unless the user
explicitly requests them.

When a task matches a capability, load that skill's `skills/<name>/SKILL.md` and
follow its workflow guidance. `CAPABILITIES.md` repeats the skill names and
descriptions for harnesses that do not already list them; skip it when they are.
For skill creation, revision, audit, validation, packaging, or trigger-testing
tasks, load `skills/skill-builder/SKILL.md`; generated non-Forge skills should
default to `.agents/skills/<name>/SKILL.md`.

Address the person you are working with by the name the session gives you — an
identity block below, or the `vault-context` owner line. Never "the user" or "the
owner". Given no name, use none rather than guess; given no pronouns, they/them.

## Source Safety

- Preserve original files. Never overwrite, rename, move, or delete a source
  unless the user explicitly requests it.
- Write generated artifacts to a dedicated output directory. If the intended
  path contains a compatible incomplete batch run, resume it. If it contains a
  compatible complete run, report its completion summary. Use a numbered path
  only for a genuinely independent run; never adopt an unmarked legacy folder.
- The output directory is `forge-output/<skill>/` by default. Inside an Obsidian
  vault it is the vault's workflow root instead — `99 Meta/99.06 Workflows` in a
  default schema — with one category folder per skill, named in that skill's
  `SKILL.md`. The `vault-context` extension injects the resolved absolute path;
  never guess it, and never write a run directory into a domain folder.
- Use working copies for transformations that could alter source content.
- Keep sensitive material local and avoid unnecessary copies.
- Never issue a mutating `obsidian` command yourself — `rename`, `move`,
  `delete`, `property:set`, `create`, `append`, `eval`. It exits 0 whether it
  succeeded or failed, has no dry run, and a single rename rewrites links across
  the whole vault. The vault skills own those calls: they back up every affected
  note, verify hashes, journal what they did, and restore from backup when the
  app touches anything but a link. Read-only `obsidian` queries are fine.

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

Route Gmail-style `.eml` conversion to `file-conversion` when the user only
needs deterministic per-message Markdown. Route folders containing multiple
emails to `document-ingest`; it preserves each message and attachment, then
uses the non-thinking `chat` service for bounded evidence extraction and the
thinking `think` service for verification and the cited aggregate digest.

When a folder contains grants, awards, proposals, scopes of work, contracts,
work plans, project reports, presentations, meeting notes, or interviews and
the user needs deliverables, requirements, dates, actions, or risks tracked,
route finalized `document-ingest` outputs to `project-extraction`, and use
`report-output` only for polished downstream deliverables. The extraction
workflow, its status file, and its completion rules are in that skill.

## Vault Workflow

Inside an Obsidian vault the `vault-context` extension injects that vault's
coordinates and skill routing once per session, and `/vault` re-scans on demand.
Outside a vault it does nothing, so only the entry points below are stated here.

Route requests such as "send this literature run to my vault", "publish this deep
research to Obsidian", or "turn these extraction outputs into concept notes" to
`vault-connections import-run`: it never mutates the source run, and nothing
enters the vault until the user accepts exact proposal ids.

Loose files brought into the vault from outside it go to `00 Inbox` at the vault
root and no further: never classify, rename, or file them into a domain folder by
hand. `vault-organizer` does that later, on demand or on a schedule. Only
Markdown is filed from the inbox, so convert other formats with `file-conversion`
or `document-ingest` first, or say plainly that the file will sit there untouched.

Route "review my article", "be reviewer 2 on this", or "peer review this draft"
to `reviewer-2`. It reviews substance only and never modifies the article.

Route "why did that session go badly", "mine this session log for skill
improvements", or "analyze this agent transcript for pain points" to
`skill-tuner`. It reads the session log read-only, writes only its own run
directory under `forge-output/skill-tuner/`, and produces an evidence-cited
report; the actual skill edits it recommends stay with `skill-builder` and
`coding`.

Route "I want to start cataloguing X", "what would a proper schema for X look
like", or "does my wiki match how the field actually works" to `vault-curator`.
It researches the field's published practice before proposing anything, and adds
rows to the schema note only, per accepted id. Filing notes, fixing drift, and
renumbering stay with `vault-organizer`.

The `vault-workflow` extension adds a plan -> execute -> verify loop driven by
`/plan`, `/execute`, `/verify`, and `/workflow off`. Each phase sets its own
tools and thinking behaviour and injects its own rules, so follow the injected
phase prompt. See [docs/vault-workflow.md](../docs/vault-workflow.md) for the
full contract.
