# Skill architecture

pi-forge uses a three-layer architecture:

## 1. Profile/startup layer

Compact identity and capability index. It tells the agent what pi-forge can do
without loading every full workflow manual. `forge/AGENTS.md` sets profile-wide
standards, and `forge/CAPABILITIES.md` is the short startup capability index.

## 2. Skill layer

Workflow judgment: when to use a capability, what process to follow, what
standards apply, how to handle ambiguity, and what final output should look
like. `SKILL.md` files should hold routing, review points, evidence standards,
provenance expectations, citation rules, output formats, and safety rules.

## 3. Script/tool layer

Mechanical execution: parsing, conversion, fetching, archiving, validation,
hashing, manifest generation, and other repeatable operations. Skill manifests
declare real available scripts/tools; do not list desired future tools as if
they exist.

Rule of thumb:

- If it tells the agent how to reason or decide, keep it in `SKILL.md`.
- If it performs a repeatable operation, move it toward `scripts/` or an extension/tool.
- If it is background documentation, put it in `references/`.
- If it defines input/output shape, put it in `schemas/`.

## Example: web collection

`SKILL.md` says:

- when to use web collection
- how to preserve provenance
- what artifacts should be produced
- how to summarize collected sources

`scripts/` contains extracted tools such as:

- `fetch_url.mjs`
- `archive_page.mjs`
- `html_to_markdown.mjs`
- `extract_metadata.mjs`

The existing `web-collection.mjs` workflow CLI remains the capability-level
entrypoint.

## Example: organize folder

`SKILL.md` says:

- inspect before changing
- produce a reviewable manifest
- avoid destructive changes
- ask before applying risky operations

`scripts/` contains extracted tools such as:

- `scan_folder.py`
- `generate_manifest.py`
- `apply_manifest.py`
- `hash_files.py`

The existing `organize-folder.py` workflow CLI remains the capability-level
entrypoint.

## Migration checklist

For each skill:

- [ ] Keep `SKILL.md` concise and procedural.
- [ ] Move mechanical details into scripts when implementation exists.
- [ ] Move long background notes into `references/`.
- [ ] Add or update `manifest.json`.
- [ ] Add schemas for structured script input/output where useful.
- [ ] Add examples for common use cases.
- [ ] Add tests for scripts, not prose.
