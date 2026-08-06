---
name: skill-builder
description: Design, create, revise, audit, validate, and package Agent Skills for Pi-Forge and other agent harnesses. Use to build a new skill, update an existing SKILL.md, validate skill structure, add trigger tests, compare overlap with neighboring skills, or choose between a skill, script, extension, reference, and AGENTS.md instruction. Not for ordinary coding or research unless the task is turning it into a reusable skill.
---

# Skill Builder

Build portable, discoverable, context-efficient Agent Skills. Treat skills as
workflow judgment and bundled resources as deterministic support.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check the helper
   before relying on it:

   ```bash
   node <skill-directory>/scripts/skill-builder.mjs doctor
   ```

   Add `--json` for machine-readable output.
2. Read [references/agent-skill-standard.md](references/agent-skill-standard.md)
   before designing or auditing a skill.
3. Inventory existing skills before creating a new one:

   ```bash
   node <skill-directory>/scripts/skill-builder.mjs inventory --root <skills-root>
   ```

   Check for overlapping names, trigger language, and neighboring capabilities.
4. Decide the right implementation surface:
   - Use a skill for conditional workflow guidance, routing, validation
     expectations, and reusable task procedures.
   - Use `scripts/` for deterministic parsing, extraction, conversion,
     validation, hashing, report assembly, and repetitive API calls.
   - Use `references/` for detailed rules, schemas, provider notes, and
     workflow branches that should load only when needed.
   - Use `assets/` for templates or files consumed by outputs.
   - Use an extension for startup-time tools, event hooks, background services,
     UI, transport, or runtime changes.
   - Use `AGENTS.md` only for stable instructions that apply broadly to the
     repository or profile.

   Declare only directories that exist. A `manifest.json` pointing at a
   directory the skill never grew is a promise the validator has to warn about
   forever.
5. Design any model call against
   [docs/skill-architecture.md](../../../docs/skill-architecture.md), which has
   the layer model and the full checklist. The questions that decide the shape:
   - Does one call make more than one independent decision? Split it — unless a
     later decision needs an earlier one to be right, in which case it is one
     piece. Do not split synthesis.
   - Does the prompt need a fact the model would have to recall? Fetch it, or
     write it as a question for review. Every prompt carrying a source also says
     to answer only from it and to decline otherwise.
   - Does the stage label name what the call *is*, rather than what helper it
     borrows? Routing keys on that string, and an unnamed stage runs on `chat`.
   - Would the thinking model otherwise read raw material? Reduce it on `chat`
     into exact quotes with locators first, then judge those in batches.
6. Scaffold only after the shape is clear:

   ```bash
   node <skill-directory>/scripts/skill-builder.mjs scaffold <name> \
     --target project \
     --root <project-root> \
     --resources scripts,references,tests
   ```

   Non-Forge project skills default to `<project>/.agents/skills/<name>/`.
   User skills default to `~/.agents/skills/<name>/`. Forge-bundled skills live
   under `forge/skills/<name>/` and need `manifest.json` plus
   `agents/openai.yaml`.
7. Write `SKILL.md` as a compact operational map. Put trigger language and
   exclusions in the frontmatter `description`; the body loads only after the
   skill is selected.
8. Add positive and negative trigger examples under `tests/triggers.json` when
   trigger behavior matters. Use `check-triggers` to compare against neighboring
   skills:

   ```bash
   node <skill-directory>/scripts/skill-builder.mjs check-triggers <skill-dir> \
     --against <skills-root>
   ```
9. Validate before completion:

   ```bash
   node <skill-directory>/scripts/skill-builder.mjs validate <skill-dir>
   ```

   Use `--strict` when warnings should block acceptance.

## Output Contract

When creating or revising a skill, report:

- skill location and target scope
- frontmatter name and description
- optional directories and scripts created
- trigger examples added or missing
- overlap or broad-description warnings
- validation command and result

Do not claim a skill is ready until structural validation passes.
