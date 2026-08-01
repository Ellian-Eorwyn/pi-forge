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
- `document-ingest`: Normalize documents and Gmail-style EML batches into structured text with source maps, exact-quote evidence, split chat/think verification, and a cited aggregate email digest.
- `file-conversion`: Convert files, including deterministic EML-to-Markdown with preserved attachment manifests, between common working formats with per-file checkpoints.
- `literature-extraction`: Extract structured evidence, claims, metadata, and citations from research documents.
- `literature-library`: Turn a citation file or academic run into a library of actual documents - bibliographic `Author - Date - Title` filenames, open-access-first PDF acquisition with conservative per-publisher rate limits, credential-free institutional deferral, and clean Markdown conversion.
- `organize-folder`: Sort messy folders through a reviewable manifest before making changes.
- `personal-admin`: Turn personal/admin documents into summaries, decisions, and action plans.
- `project-extraction`: Run or continuously refresh live project repositories with truthful packet/source coverage, reviewed Inbox intake, serial model-assisted extraction and reconciliation, source-backed controls, focused views, explicitly labeled drafts, and CSV/Mermaid/HTML Gantt outputs while preserving conflicts and human status ownership.
- `report-output`: Assemble polished deliverables from processed research or document outputs.
- `reviewer-2`: Peer-review a scholarly article note as a separate review copy in `00 Inbox` - anchored critique callouts on research gaps, evidence, logic, theory, and structure, each carrying its fix and citations resolved against real `web-research` runs, plus a ranked meta review and revision plan; never modifies the article.
- `skill-builder`: Design, scaffold, validate, and audit portable Agent Skills.
- `site-builder`: Build static websites from structured content folders.
- `spreadsheet-analysis`: Analyze, clean, validate, and enrich tabular datasets.
- `transcript-cleanup`: Clean raw transcripts into readable, structured documents.
- `transcription`: Transcribe audio/video with per-chunk checkpoints, then correct and clean the transcript.
- `vault-capture`: Turn an owner-authored typed or spoken braindump into schema-valid notes in `00 Inbox`, applying the vault's scoped voice policy, adding vault-first journal reflection, marking output `capture_type: generated`, and keeping the braindump verbatim; for transcription exports use `vault-transcripts` instead.
- `vault-handoff`: Prepare completed artifacts for pi-vault or Obsidian review.
- `vault-connections`: Search an Obsidian vault by meaning, propose note links for per-id approval, validate completed literature/meta-literature/deep-research runs, preserve imported report bodies, turn deep research into source-policy `Synthesis` notes with quotes and provenance (`--notes`), and create evidence-backed wiki notes from vault-owned templates.
- `vault-organizer`: Classify, de-duplicate, and organize Obsidian notes from a human-maintained vault schema note, with restart-safe resumable runs, a recoverable duplicate quarantine, asset-embed repair, an opt-in per-kind sources tree, deterministic date backfill from older copies of notes, schema drift detection that blocks applying when the schema and the folders on disk disagree, and optional link-safe moves and property-vocabulary drift when the Obsidian CLI is available.
- `vault-transcripts`: Classify, rename, clean, and summarize raw transcripts while distinguishing owner memos/journals, personal exchanges, external sources, and unknown material; apply scoped voice only where valid, add introspective reflection to journals and working reflection to memos, cite only outside sources already in hand, and keep the original transcription verbatim as its own linked source note; clean speech into readable written prose without changing meaning, mark every generated section as a callout, and reprocess or reconcile notes the pipeline already wrote.
- `vault-wiki`: Install the seven vault-owned wiki entry templates, and expand thin wiki entity notes into cited reference cards from canonical sources (Stanford Encyclopedia of Philosophy, Wikipedia, and per-topic equivalents), rewriting only the sections the kind spec declares managed — owner-authored `Notes`, frontmatter, and every unclaimed heading are preserved byte-for-byte and enforced by a post-merge comparison — with batch approval, per-id approval for anything the reviewer flagged, and a per-run revert.
- `web-collection`: Archive, organize, and preserve web sources with per-URL checkpoints.
- `web-research`: Perform resumable quick, deep, or academic web research with URL/provider/iteration checkpoints, direct-first acquisition, local-first scheduling, embedding-ranked source triage, browser fallback/discovery, provenance, evidence, claims, and validation.
