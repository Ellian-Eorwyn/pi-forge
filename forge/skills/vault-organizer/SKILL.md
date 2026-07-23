---
name: vault-organizer
description: Organize an Obsidian vault or vault inbox from a human-maintained schema note - classify Markdown notes with the local model, replace frontmatter with schema-validated metadata, de-duplicate exact and near-duplicate notes into a recoverable quarantine, and move notes into derived domain/subdomain/project folders with restart-safe resumable runs. Use when the user asks to organize my Obsidian vault, process my vault inbox, migrate a messy notes folder into a vault, de-duplicate my notes, classify my vault notes, apply my vault schema, or clean invalid YAML frontmatter.
---

# Vault Organizer

Classify Markdown notes against the vault's human-maintained schema note and
produce a reviewable plan before anything changes. The Markdown schema note is
the sole source of truth; generated caches, indexes, and `schema.json`-style
artifacts are only accelerators. The model never invents schema values or
folders — it may only add advisory entries to the report's Schema Suggestions
section.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path.
2. Resolve the vault path from the user or known configuration, and confirm
   which flow the user wants:
   - `inbox`: recursive Markdown notes under `00 Inbox` (routine processing)
   - `vault`: every eligible Markdown note (the schema's "explicit migration
     command" — the only flow allowed to move already-filed notes)
3. Before a long run, verify the environment:

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py doctor --vault <vault>
   ```

   This checks the schema parses, the chat endpoint answers, and the
   embeddings endpoint answers. Set the embeddings model that is actually
   served (for example `--embeddings-model Qwen3-Embedding-4B` or
   `FORGE_EMBEDDINGS_MODEL`); it is fingerprinted into runs and caches. The
   default chat endpoint is the non-thinking `llms:8004`; to use a thinking
   backend instead pass `--base-url http://llms:8008/v1/chat/completions
   --think-prefill` so it skips reasoning tokens.
4. Run a dry run first (dry run is always the default):

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py vault --vault <vault>
   ```

   Use `--limit <n>` for a small trial before a whole-vault run. Progress is
   one stderr line per note with an ETA; stdout stays one JSON result.
5. Read the structured JSON result and generated `report.md`. Report to the
   user: selected notes, duplicate groups (exact and near), duplicate pairs
   held for review, proposed metadata updates and moves, notes routed to
   `00 Inbox` for review, schema suggestions, and the run directory.
6. Obtain explicit approval before any whole-vault `--apply`. For inbox mode,
   a direct instruction such as "process my inbox and apply it" is approval;
   otherwise present the dry run first.
7. Apply by resuming the same run so no classification work repeats:

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py vault --vault <vault> --apply --run <run-directory>
   ```

8. If a run is interrupted at any point, resume it with `--run
   <run-directory>` (same options); completed work is never redone. Check
   progress from another shell with:

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py status --run <run-directory>
   ```

9. Report the final structured counts and run directory.

## De-duplication guarantees

- Nothing is ever deleted. Duplicate losers move to
  `.vault-organizer/duplicates/<original-path>` inside the vault, byte-intact
  and recoverable; the plan records every winner/loser pairing.
- Exact duplicates (identical body after frontmatter and whitespace
  normalization) are resolved automatically; only the winner is classified.
- Near duplicates require embedding similarity at or above the auto threshold
  and near-total line containment; borderline pairs are only reported for
  review, never acted on.
- Inbox notes are also de-duplicated against the already-filed vault via a
  content index; a filed note always wins, and a richer inbox copy is held
  for review instead of quarantined.

## Rules

- Never manually edit the generated plan or frontmatter outside the script.
- Never claim review-required notes or review-band duplicate pairs were
  processed.
- Never overwrite a destination collision.
- Never modify the schema to make an invalid classification pass; relay the
  report's Schema Suggestions to the user instead.
- Tell the user the Markdown schema note remains the source of truth.
- Resuming with different options is refused by design; start a new run when
  the model, thresholds, limit, or schema changed.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

Read [references/vault-schema-contract.md](references/vault-schema-contract.md)
for the schema, routing, dedupe, run-state, validation, caching, and apply
contract.
