# Pi-Forge Skills Reference

This file is a stable orientation guide for external interfaces. It should not
duplicate generated token counts or full command manuals.

Use these sources of truth:

- `FORGE_SKILLS.md`: generated launch-context and skill inventory report.
- `forge/CAPABILITIES.md`: compact startup capability index.
- `forge/skills/<name>/SKILL.md`: workflow, judgment, routing, review, evidence, and output standards.
- `forge/skills/<name>/manifest.json`: skill package boundary and real available scripts/tools.
- `forge/SCRIPT_TOOL_CONTRACT.md`: preferred contract for extracted executable tools.
- `forge/RUN_STATE_CONTRACT.md`: shared restart-safe run contract used by the
  batch capabilities (resume, `status` drift reporting, and `refresh`).

## Architecture boundary

pi-forge keeps judgment and execution separate:

- Skills are for judgment: task routing, standards, ambiguity handling,
  provenance expectations, review points, and final synthesis.
- Scripts/extensions/tools are for execution: fetching, conversion, extraction,
  validation, hashing, manifest generation, filesystem changes, and export.

Existing skill scripts may still expose command-oriented CLIs. Future extraction
should move repeatable operations toward structured JSON input/output as defined
in `forge/SCRIPT_TOOL_CONTRACT.md`.

## Built-in skills

<!-- forge:skills-list start -->

The live skill inventory currently contains 28 capability workflows, listed
here with the one-line descriptions maintained in `forge/CAPABILITIES.md`:

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
- `site-builder`: Build static websites from structured content folders.
- `skill-builder`: Design, scaffold, validate, and audit portable Agent Skills.
- `skill-tuner`: Mine a completed pi session log for pain points - error channels, truncation loops, compaction losses, retry loops, and the ambiguity and knowledge reliance visible only in the thinking narrative - into severity-ranked evidence with byte-exact quotes and entry locators, reviewed by the thinking service, then author a token-bounded skill-tuning report whose every claim cites appendix evidence ids; the session log stays read-only.
- `spreadsheet-analysis`: Analyze, clean, validate, and enrich tabular datasets.
- `transcript-cleanup`: Clean raw transcripts into readable, structured documents.
- `transcription`: Transcribe audio/video on the llm-stack speech-to-text service, then correct and clean the transcript.
- `vault-capture`: Turn an owner-authored typed or spoken braindump into schema-valid notes in `00 Inbox`, applying the vault's scoped voice policy, adding vault-first journal reflection, marking output `capture_type: generated`, and keeping the braindump verbatim; for transcription exports use `vault-transcripts` instead.
- `vault-compose`: Compose a note from a typed set of sources the run is holding — research claims, existing vault notes, or excerpts of the conversation — assembled in the block order the vault's note-format note declares and written under its voice policy, with every name, link, and wikilink checked against the sources the block cites so a note whose sections are each plausible cannot be collectively a collage; the provenance block is written in code, no `domain` is guessed, and nothing reaches the vault until the user accepts a proposal id.
- `vault-connections`: Search an Obsidian vault by meaning, propose note links for per-id approval, validate completed literature/meta-literature/deep-research runs, preserve imported report bodies, turn deep research into source-policy `Synthesis` notes with quotes and provenance (`--notes`), and create evidence-backed wiki notes from vault-owned templates.
- `vault-curator`: Research how a field actually catalogues its records through `web-research`, reconcile that against the compiled schema using a closed set of moves, and propose the smallest legal way to hold it — schema rows whose numbers are chosen by code and which are proved against a candidate copy of the schema note before they are shown, plus wiki kind specs, templates, and body-table sections as patch files; applying is per-id and additive only, and a run with no field sources proposes nothing.
- `vault-media`: Catalog books, films, television, music, and games as `work` notes under an `entertainment` domain, with metadata fetched from Open Library, MusicBrainz, TVmaze, Steam, TMDB, or IGDB rendered as a `Details` body table, and the owner's rating and thoughts copied verbatim beside it — a rating nobody gave is an absent key rather than a provider's score, and a drafted lead naming anything the fetched record does not contain is dropped rather than repaired; also keeps To Read/To Watch backlog list notes and promotes a row into a full note. Books read as scholarly sources stay `source` notes under the Sources root.
- `vault-naturalist`: Compile the region-scoped `Phenology` tables on animal, plant, and fungus wiki cards into a queryable index, report which species are expected in the owner's declared home region in a given month, and record a single field observation as a schema-valid note linked to its species card; deterministic throughout, and `sourced`/`inferred` windows stay distinguishable from `observed` ones.
- `vault-organizer`: Classify, de-duplicate, and organize Obsidian notes from a human-maintained vault schema note, with restart-safe resumable runs, a recoverable duplicate quarantine, asset-embed repair, an opt-in per-kind sources tree, deterministic date backfill from older copies of notes, schema drift detection that blocks applying when the schema and the folders on disk disagree, a `renumber` mode that shifts domain numbers and every folder derived from them without touching a note, and optional link-safe moves and property-vocabulary drift when the Obsidian CLI is available.
- `vault-projects`: Resolve a registered project into the closed set of files an agent may work from — everything in the project folder plus the sources, people, organizations, and wiki cards its hub note lists under `## Corpus`, with transcript pairs and embedded attachments closed in — and freeze that set as a `_corpus.json` manifest beside the hub so a project hands off whole without a file being duplicated; also drafts a hub from notes that already carry the project, packs a corpus into one budgeted context file, and reports unresolved links, ambiguous basenames, and stale manifests. Deterministic throughout, and the only vault write is its own manifest.
- `vault-transcripts`: Classify, rename, clean, and summarize raw transcripts while distinguishing owner memos/journals, personal exchanges, external sources, and unknown material; apply scoped voice only where valid, add introspective reflection to journals and working reflection to memos, cite only outside sources already in hand, and keep the original transcription verbatim as its own linked source note; clean speech into readable written prose without changing meaning, mark every generated section as a callout, and reprocess or reconcile notes the pipeline already wrote.
- `vault-wiki`: Install the seven vault-owned wiki entry templates, and expand thin wiki entity notes into cited reference cards from canonical sources (Stanford Encyclopedia of Philosophy, Wikipedia, and per-topic equivalents), rewriting only the sections the kind spec declares managed — owner-authored `Notes`, frontmatter, and every unclaimed heading are preserved byte-for-byte and enforced by a post-merge comparison — with batch approval, per-id approval for anything the reviewer flagged, and a per-run revert.
- `web-collection`: Archive, organize, and preserve web sources with per-URL checkpoints.
- `web-research`: Perform resumable quick, deep, or academic web research with URL/provider/iteration checkpoints, direct-first acquisition, local-first scheduling, embedding-ranked source triage, browser fallback/discovery, provenance, evidence, claims, and validation.

This block is generated. After adding, renaming, or re-describing a skill,
update `forge/CAPABILITIES.md` and run:

```bash
npm run forge:sync-reference
```

<!-- forge:skills-list end -->

Regenerate `FORGE_SKILLS.md`, the separate launch-context and token report,
after changing skill names, descriptions, bodies, or visibility:

```bash
npm run forge:skills-report
```

Validate manifests and package inclusion:

```bash
npm run check:forge-skill-manifests
npm run check:forge-package
```

## External execution surfaces

Skill-local scripts live under `forge/skills/<name>/scripts/` and are declared in
that skill's `manifest.json` only when they exist. Do not list future extraction
candidates as available tools.

The current extension/tool surfaces outside skill-local scripts are:

- `forge/extensions/web-research.ts`: provides `forge_web_search`,
  `forge_web_read`, `forge_deep_web_research`, `forge_web_discover`,
  `forge_academic_web_research`, and `forge_reference_lookup` for search, page
  extraction, iterative research with source provenance and validation
  artifacts, endpoint discovery, scholarly metadata search, and resolving a
  subject name to one reference source's entries.
- `pi-forge-mcp`: exposes deterministic MCP tools `forge_transcribe` and
  `forge_convert_files`.

The remaining extensions register session behavior rather than callable tools:

- `forge/extensions/inference-scheduling.ts`: reserves an interactive slot
  against the local inference queue so background work does not starve the
  interactive session.
- `forge/extensions/vault-context.ts`: detects an Obsidian vault working
  directory and injects its coordinates once per session; registers `/vault`.
- `forge/extensions/vault-workflow.ts`: drives the plan -> execute -> verify
  loop by switching session model and active tools per phase; registers
  `/plan`, `/execute`, `/verify`, and `/workflow`.

## Common pipelines

- Web/document research: `web-collection` -> `document-ingest` -> `literature-extraction` -> `report-output`.
- Quick lookup: `web-research research` -> final answer or downstream skill.
- Deep web research: `web-research deep` -> claim/evidence register -> `web-research validate` -> `report-output`.
- Academic sourcing: `web-research academic` -> canonical works and RIS export -> `literature-extraction` or `report-output`.
- Media processing: `transcription` -> `transcript-cleanup` -> `report-output` or `personal-admin`.
- Folder cleanup: `organize-folder` scan/plan -> user review -> apply.
- Static site output: processed source folder -> `site-builder`.
- Voice notes into a vault: `vault-transcripts` -> `vault-organizer` inbox processing.
- Braindump into a vault: `vault-capture` -> `vault-organizer` -> `vault-connections` link proposals.
- Research into vault notes: `web-research deep` -> `vault-connections import-run --notes` -> `vault-organizer`.
- Article peer review: `web-research deep` runs for the literature -> `reviewer-2` index/comment/render -> review copy in `00 Inbox` -> `vault-organizer`.
- Project tracking: `project-extraction` run/refresh -> reviewed Inbox intake -> CSV/Mermaid/HTML Gantt outputs.
- New skill packages: `skill-builder` scaffold -> validate -> `npm run check:forge-skill-manifests`.

## Local defaults

The installed forge profile defaults to local services unless overridden in
`~/.pi-forge/agent/settings.json`:

- Interactive agent: `http://llms:8008` with model `code` (thinking).
- Bulk per-file work (`connectedServices.chat`): `http://llms:8004` with model
  `chat` — the same weights served without thinking. Skills that process many
  files one at a time use this so they do not pay for reasoning per file.
- Judgment and verification (`connectedServices.think`): `http://llms:8008` with
  model `code`. Falls back to the chat service when not configured.
- Embeddings: `http://llms:8005` with model `embed`.
- SearXNG search: `connectedServices.searxng.baseUrl` defaults to `http://llms/searxng`.
- Playwright rendered browsing: `connectedServices.playwright.wsEndpoint` defaults to `ws://llms/playwright`.
- Stack state: `connectedServices.stackState.baseUrl` defaults to `http://llms:8078`.
  Read-only and entirely optional — it reports which weights each port is
  serving, the real per-slot context size, and why a service is down. Where it
  is absent every consumer falls back to the built-in constants, so an install
  that has no such API behaves exactly as it did before it existed. Disable with
  `PI_FORGE_SKIP_STACK_DISCOVERY=1` or an empty `FORGE_STACK_STATE_URL`.
