---
name: vault-organizer
description: Organize an Obsidian vault or vault inbox by replacing invalid YAML frontmatter with schema-validated metadata and moving Markdown notes into domain, subdomain, and project folders. Use when the user asks to organize my Obsidian vault, process my vault inbox, classify my vault notes, normalize Obsidian properties, apply my vault schema, sort notes according to frontmatter, or clean invalid YAML frontmatter.
---

# Vault Organizer

Classify Markdown notes against the vault's human-maintained schema note and
produce a reviewable plan before anything changes. The Markdown schema note is
the source of truth; generated cache files are only accelerators.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path.
2. Resolve the vault path from the user or known configuration. Confirm whether
   the user requested inbox processing or whole-vault processing:
   - `inbox`: recursive Markdown notes under `00 Inbox`
   - `vault`: all eligible Markdown notes in the vault
3. Run a dry run first:

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py inbox --vault <vault>
   ```

   or:

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py vault --vault <vault>
   ```

4. Read the structured JSON result and generated `report.md`. Report selected
   notes, proposed metadata updates, proposed moves, review-required notes,
   failures, warnings, and the run directory.
5. Obtain explicit approval before any whole-vault `--apply`. For inbox mode,
   a direct user instruction such as "process my inbox and apply it" is approval;
   otherwise present the dry run first.
6. Rerun the same command with `--apply` only after approval:

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py inbox --vault <vault> --apply
   ```

7. Report the final structured counts and run directory.

## Rules

- Never manually edit the generated plan or frontmatter outside the script.
- Never claim review-required notes were processed.
- Never overwrite a destination collision.
- Never modify the schema to make an invalid classification pass.
- Tell the user that the Markdown schema note remains the source of truth.
- Use `--limit <n>` for small whole-vault trial runs.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

Read [references/vault-schema-contract.md](references/vault-schema-contract.md)
for the schema, routing, validation, caching, and apply contract.
