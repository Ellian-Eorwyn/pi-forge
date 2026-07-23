# Changelog

## [Unreleased]

### Added

- Added a packaged restart-safe batch run contract and shared Python/JavaScript state helpers, with resumable document ingestion, file conversion, web collection/research, transcription chunks, transactional finalization, input refresh/retry controls, and context-bounded literature synthesis and deliverable queues.
- Added a refreshable `project-extraction` skill with source-backed evidence, reconciled control registers, a human-maintained status overlay, project-aware document routing, and PPTX OOXML ingestion.
- Added direct-first web research acquisition with stage artifacts, cache metadata, structured extraction, and explicit endpoint discovery.
- Added local-first deep web research scheduling with serialized model calls, cached SearXNG searches, embedding-ranked chunks, batched evidence extraction, and scheduler/ranking artifacts.
- Added settings-backed SearXNG and Playwright connected service defaults plus quick web search/read tools.
- Added the published `@ellian-eorwyn/pi-forge` package surface for no-clone installs and updates.
- Added opt-in cache-aware local inference scheduling with isolated interactive and project-extraction slots, activity leases, worker preemption, adaptive larger batches, focused views, extraction metrics, and source-backed Gantt outputs.
- Added hybrid search and resumable Inbox intake for live project-extraction repositories.
- Added a serial foreground project-extraction orchestrator, truthful coverage gates and labeled drafts, adaptive truncation recovery, model-assisted reconciliation, and Zoom JSON transcript provenance intake.
- Added a `vault-context` extension that recognizes an Obsidian vault at or above the working directory and injects its coordinates — root, schema note, note count, index state — once per session, so vault sessions start knowing which skill answers which question. Adds `/vault`; does nothing outside a vault.
- Added a `vault-connections` skill: hybrid semantic search over an Obsidian vault, per-id reviewed connection proposals merged additively into the `related` frontmatter property, and a wiki entity layer that turns unresolved wikilinks into schema-routed stub notes and backfills links to them. Extracted the shared schema compiler into `lib/vault_schema.py` so it and `vault-organizer` derive identical folder paths.
