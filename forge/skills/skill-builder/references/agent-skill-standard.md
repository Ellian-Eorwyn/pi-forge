# Agent Skill Standard

Use this reference when creating, revising, auditing, or validating Agent
Skills for Pi-Forge or other agent harnesses.

## Locations

Use the agent-agnostic layout for generated project and user skills:

```text
<project>/.agents/skills/<skill-name>/SKILL.md
~/.agents/skills/<skill-name>/SKILL.md
```

Pi also supports Pi-native skill locations and package-provided skills. Forge's
bundled distribution skills live under `forge/skills/<skill-name>/` so they can
ship inside the `@ellian-eorwyn/pi-forge` package. Do not hard-code
Claude-specific paths into core skill design.

## Structure

Only `SKILL.md` is required:

```text
<skill-name>/
├── SKILL.md
├── scripts/
├── references/
├── assets/
└── tests/
```

Use optional directories only when they carry real value:

- `scripts/`: deterministic or repetitive operations.
- `references/`: detailed instructions, schemas, provider docs, and specialized
  workflow branches.
- `assets/`: templates, sample files, output resources, icons, or files copied
  into deliverables.
- `tests/`: trigger examples, fixtures, validation scripts, expected outputs, or
  regression cases.

## Naming

The directory name and frontmatter `name` must match exactly.

Names must:

- use lowercase letters, digits, and hyphens only
- be 1-64 characters
- not start or end with a hyphen
- not contain consecutive hyphens

Good names identify coherent capabilities, such as `deep-research`,
`document-ingest`, `spreadsheet-analysis`, or `release-management`. Avoid vague
names such as `helper`, `general`, `tools`, `utilities`, or `misc`.

## Frontmatter

Every `SKILL.md` must begin with YAML frontmatter:

```yaml
---
name: skill-name
description: A precise explanation of what the skill does and when it should be used.
---
```

The description is routing metadata, not ordinary documentation. It should
explain what the skill does, when it should activate, realistic user language or
file/task triggers, and important exclusions when neighboring skills might
otherwise overlap.

Optional fields may include `license`, `compatibility`, `metadata`, and
`allowed-tools`. Pi-Forge-specific fields should be namespaced under metadata
instead of becoming global requirements.

## Progressive Disclosure

Design for three levels of context:

1. Discovery metadata: skill name, description, and location.
2. Core instructions: `SKILL.md`, loaded only after selection.
3. Supporting resources: references, scripts, and assets loaded only when the
   active branch needs them.

Keep `SKILL.md` compact. Default targets are under about 500 lines and under
about 5,000 tokens. Move detailed or rarely needed material into focused
reference files that are directly linked from `SKILL.md`.

Avoid deep reference chains. A skill should usually link directly to the
reference files it may need.

## Writing `SKILL.md`

Write operational instructions for an agent. Prefer direct, imperative language:

```markdown
1. Inspect the available inputs.
2. Determine which workflow branch applies.
3. Load only the references needed for that branch.
4. Use bundled scripts for deterministic processing.
5. Validate the result before returning it.
```

Use strict words such as `must`, `always`, and `never` only when the constraint
is genuinely universal. Explain the reason when doing so helps the agent
generalize the rule correctly.

Common sections to consider:

- Purpose
- Inputs
- Workflow
- Routing
- Tool and script usage
- Validation
- Completion criteria
- Edge cases
- Output contract

## Judgment Versus Deterministic Work

Use `SKILL.md` for interpretation, workflow selection, prioritization,
ambiguity handling, deciding which resources to load, synthesis, validation
logic, and output requirements.

Use `scripts/` for parsing, conversion, normalization, extraction,
deduplication, hashing, database creation, schema validation, repetitive API
requests, deterministic report assembly, and calculations.

Scripts should provide help text, validate inputs, return clear errors, use
stable exit codes, avoid silently discarding data, and produce machine-readable
output when useful.

## References and Assets

Each reference file should state what it contains, when it should be loaded, and
what decisions it supports. Do not duplicate the same rule across several
references; establish one authoritative location and link to it.

Use assets for materials consumed by outputs, not for critical behavioral
instructions.

## Testing

Validate at several levels:

- Structural validation: required files, frontmatter, name rules, referenced
  files, YAML shape, paths inside the skill directory, script runtimes, and
  Forge manifest validity when applicable.
- Trigger evaluation: positive examples, negative examples, and overlap checks
  against neighboring skills.
- Workflow evaluation: representative tasks, branch selection, resource loading,
  script usage, output contract compliance, and error handling.
- Regression testing: rerun prior trigger and workflow cases when a skill
  changes.

## Skill, Extension, or Repository Instruction

Use a skill for conditional procedures, specialized workflows, reusable
operational knowledge, and resources that should load only when relevant.

Use an extension for startup-time capabilities, new tools, event hooks,
lifecycle behavior, persistent UI, transport or protocol support, background
services, or runtime changes.

Use root `AGENTS.md` for stable instructions that apply broadly throughout a
repository. Do not put a long runtime implementation into prose when it belongs
in an extension or script.
