# Skill architecture

pi-forge uses a three-layer architecture:

1. Profile/startup layer
   - Compact identity and capability index.
   - Knows what pi-forge can do.
   - Does not contain full workflow manuals.

2. Skill layer
   - Workflow judgment.
   - When to use the capability.
   - Evidence/provenance standards.
   - Review rules.
   - Output expectations.

3. Script/tool layer
   - Mechanical execution.
   - Parsing, conversion, fetching, archiving, validation, hashing, manifest generation, and other deterministic operations.

Rule of thumb:

- If it tells the agent how to reason or decide, keep it in `SKILL.md`.
- If it performs a repeatable operation, move it toward `scripts/`.
- If it is background documentation, put it in `references/`.
- If it defines input/output shape, put it in `schemas/`.

## Migration checklist

For each skill:

- [ ] Keep `SKILL.md` concise and procedural.
- [ ] Move mechanical details into scripts when implementation exists.
- [ ] Move long background notes into `references/`.
- [ ] Add or update `manifest.json`.
- [ ] Add schemas for structured script input/output where useful.
- [ ] Add examples for common use cases.
- [ ] Add tests for scripts, not prose.
