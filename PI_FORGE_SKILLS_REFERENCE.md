# Pi-Forge Skills Reference

This file is a stable orientation guide for external interfaces. It should not
duplicate generated token counts or full command manuals.

Use these sources of truth:

- `FORGE_SKILLS.md`: generated launch-context and skill inventory report.
- `forge/CAPABILITIES.md`: compact startup capability index.
- `forge/skills/<name>/SKILL.md`: workflow, judgment, routing, review, evidence, and output standards.
- `forge/skills/<name>/manifest.json`: skill package boundary and real available scripts/tools.
- `forge/SCRIPT_TOOL_CONTRACT.md`: preferred contract for extracted executable tools.

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

The live skill inventory currently contains 14 capability workflows:

- `coding`: Inspect repositories and ship small, reviewable code changes.
- `document-ingest`: Normalize documents into structured text with provenance.
- `file-conversion`: Convert files between common working formats.
- `literature-extraction`: Extract structured evidence, claims, metadata, and citations from research documents.
- `organize-folder`: Sort messy folders through a reviewable manifest before making changes.
- `personal-admin`: Turn personal/admin documents into summaries, decisions, and action plans.
- `report-output`: Assemble polished deliverables from processed research or document outputs.
- `site-builder`: Build static websites from structured content folders.
- `spreadsheet-analysis`: Analyze, clean, validate, and enrich tabular datasets.
- `transcript-cleanup`: Clean raw transcripts into readable, structured documents.
- `transcription`: Transcribe audio/video, then correct and clean the transcript.
- `vault-handoff`: Prepare completed artifacts for pi-vault or Obsidian review.
- `web-collection`: Archive, organize, and preserve web sources.
- `web-research`: Perform quick web search and page-reading workflows for information lookup.

Regenerate the generated inventory after changing skill names, descriptions,
bodies, or visibility:

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

- `forge/extensions/pi-vault-client.ts`: provides `pi_vault_submit_artifact` for
  pending pi-vault proposal handoff.
- `pi-forge-mcp`: exposes deterministic MCP tools `forge_transcribe` and
  `forge_convert_files`.

## Common pipelines

- Web/document research: `web-collection` -> `document-ingest` -> `literature-extraction` -> `report-output`.
- Quick lookup: `web-research` -> final answer or downstream skill.
- Media processing: `transcription` -> `transcript-cleanup` -> `report-output` or `personal-admin`.
- Folder cleanup: `organize-folder` scan/plan -> user review -> apply.
- Static site output: processed source folder -> `site-builder`.

## Local defaults

The installed forge profile defaults to local services unless overridden:

- Primary LLM: `http://llms:8008` with model `code`.
- Embeddings: `http://llms:8005` with model `embed`.
- SearXNG: `http://llms/searxng`.
- Playwright endpoint for future tool extraction: `ws://llms/playwright/`.
