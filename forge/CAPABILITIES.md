# pi-forge capabilities

pi-forge is a research, document-processing, file-organization, and data-analysis focused Pi profile.

Use this file as a startup capability index. Do not treat it as the full workflow manual. When a user task matches one of these capabilities, load the relevant skill instructions from `forge/skills/<name>/SKILL.md`.

Prefer deterministic scripts/tools for mechanical work such as parsing, conversion, extraction, validation, archiving, hashing, filesystem changes, and manifest generation. Use skill instructions for workflow judgment, review standards, evidence standards, and final output shape.

Batch capabilities use the shared restart-safe run contract in
`RUN_STATE_CONTRACT.md`. Repeating the same command and output path resumes a
compatible run; `status` reports frozen-snapshot drift and `refresh` explicitly
reconciles it.

## Built-in capabilities

- `coding`: Inspect repositories and ship small, reviewable code changes.
- `document-ingest`: Normalize documents into structured text with provenance.
- `file-conversion`: Convert files between common working formats with per-file checkpoints.
- `literature-extraction`: Extract structured evidence, claims, metadata, and citations from research documents.
- `organize-folder`: Sort messy folders through a reviewable manifest before making changes.
- `personal-admin`: Turn personal/admin documents into summaries, decisions, and action plans.
- `project-extraction`: Run or continuously refresh live project repositories with truthful packet/source coverage, reviewed Inbox intake, serial model-assisted extraction and reconciliation, source-backed controls, focused views, explicitly labeled drafts, and CSV/Mermaid/HTML Gantt outputs while preserving conflicts and human status ownership.
- `report-output`: Assemble polished deliverables from processed research or document outputs.
- `skill-builder`: Design, scaffold, validate, and audit portable Agent Skills.
- `site-builder`: Build static websites from structured content folders.
- `spreadsheet-analysis`: Analyze, clean, validate, and enrich tabular datasets.
- `transcript-cleanup`: Clean raw transcripts into readable, structured documents.
- `transcription`: Transcribe audio/video with per-chunk checkpoints, then correct and clean the transcript.
- `vault-handoff`: Prepare completed artifacts for pi-vault or Obsidian review.
- `vault-connections`: Search an Obsidian vault by meaning, propose note-to-note links for per-id approval into the `related` property, and maintain a wiki layer of concept/place/event entity notes.
- `vault-organizer`: Classify, de-duplicate, and organize Obsidian notes from a human-maintained vault schema note, with restart-safe resumable runs and a recoverable duplicate quarantine.
- `web-collection`: Archive, organize, and preserve web sources with per-URL checkpoints.
- `web-research`: Perform resumable quick, deep, or academic web research with URL/provider/iteration checkpoints, direct-first acquisition, local-first scheduling, embedding-ranked source triage, browser fallback/discovery, provenance, evidence, claims, and validation.
