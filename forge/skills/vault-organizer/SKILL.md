---
name: vault-organizer
description: Organize an Obsidian vault or vault inbox from a human-maintained schema note - classify Markdown notes, file them into schema folders, normalize frontmatter, and find duplicates. Use to process the inbox, file loose notes, de-duplicate, or check the vault against its schema. Dry-runs by default and needs explicit approval before applying. Run vault-transcripts first on raw voice notes; use vault-connections for meaning-based search and linking.
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
   `FORGE_EMBEDDINGS_MODEL`); it is fingerprinted into runs and caches.

   Classification is one call per note, so it runs on the non-thinking service
   (`llms:8004`, model `chat`) — the agent's configured `connectedServices.chat`
   unless overridden. Doctor reports whether that endpoint actually answers
   without reasoning; if it warns that it is thinking, the endpoint is
   misconfigured and every note will cost hundreds of wasted tokens.
4. Run a dry run first (dry run is always the default):

   ```bash
   python3 <skill-directory>/scripts/vault-organizer.py vault --vault <vault>
   ```

   Use `--limit <n>` for a small trial before a whole-vault run. Progress is
   one stderr line per note with an ETA; stdout stays one JSON result.

   Classifications are then reviewed by the thinking model in batches of ~20,
   and anything it flags is re-classified individually with reasoning. That
   review is what makes fast bulk classification safe, so leave it on;
   `--no-verify` exists for when the thinking backend is down and the report
   will then say plainly that nothing was reviewed.
5. Read the structured JSON result and generated `report.md`. Report to the
   user: selected notes, duplicate groups (exact and near), duplicate pairs
   held for review, the Verification section (how many were reviewed, what was
   flagged and why, what was re-done and what needs their decision), proposed
   metadata updates and moves, notes routed to `00 Inbox` for review, schema
   suggestions, and the run directory.
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

## Attachment links

Moving notes leaves their relative image and PDF paths pointing at the old
layout — nothing in a classification run rewrites an embed. The `attachments`
mode audits and repairs those links. It is deterministic: no model, no
embeddings, and it runs with every endpoint down.

```bash
python3 <skill-directory>/scripts/vault-organizer.py attachments --vault <vault>
python3 <skill-directory>/scripts/vault-organizer.py attachments --vault <vault> --apply
```

Every asset embed is classified as one of:

- `resolves` — the target exists; left untouched.
- `repairable` — exactly one file of that name exists in the vault; rewritten to
  a `![[basename]]` wikilink, which Obsidian resolves vault-wide and which
  therefore survives the next reorganization.
- `ambiguous` — several files share the name. Reported, never guessed.
- `missing` — no such file anywhere. Stripped: an embed with alt text collapses
  to that text, and one that was the whole line or list item takes the line
  with it.

Embed syntax inside a code span or fenced block is documentation, not a link,
and is never matched. Run dry first and relay the counts; stripping is
irreversible in the note, so the run directory's `attachment_report.json` and
`attachment_report.md` record every embed *before* any edit, and every rewritten
note is copied to `backup/` under the run directory. Report ambiguous and
missing links to the user rather than presenting the run as a clean repair.

## Schema drift

Folder paths are compiled from the schema's `Number` and `Label` cells; nothing
reads folder names off disk. Filing creates a missing destination rather than
failing, so a schema saying `8.02 Organizations` while the notes live in `8.03
Organizations` silently grows a second folder on the next classification. The
`drift` mode checks for that, deterministically and with every endpoint down.

```bash
python3 <skill-directory>/scripts/vault-organizer.py drift --vault <vault>
```

Findings are ranked and the ranking is the point — a real vault shows ~20 raw
differences of which ~3 matter. **high** (`number_collision`, `label_moved`)
means a route and a folder disagree and filing will split notes across two
folders. **medium** is an undeclared folder holding notes. **low** and **info**
are empty or reserved slots, which are normal. Structure below a declared route
(`99.05 Attachments/Images`) is never reported.

`doctor` runs the same check and exits non-zero on a `high` finding; `organize`
lists every finding in the report's `## Schema Drift` section and refuses
`--apply` while a `high` one stands unless `--allow-schema-drift` is passed.

Each `high` finding names the cheaper side to change — the side holding fewer
notes, since renaming a folder moves notes and editing a row moves none.
**Relay the findings and ask the user which direction they want. Never choose
for them, and never pass an id they have not seen and named.**

```bash
python3 <skill-directory>/scripts/vault-organizer.py drift --vault <vault> --fix-schema <id>,<id>
```

`--fix-schema` changes only a `Number` cell in the named rows. It never renames,
moves, or deletes a folder and never adds a row, so folder-side corrections and
new registrations are reported for the user to make. The note is backed up, and
an edit that fails to re-parse or introduces new high-severity drift is rolled
back.

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
  report's Schema Suggestions to the user instead. This is not the same thing as
  `--fix-schema`, and the difference matters: a classification that will not fit
  the schema is the model wanting the schema changed, and the answer is always
  no. `--fix-schema` applies a drift correction the *user* named after seeing it
  in a report, to make the schema agree with folders that already exist. Never
  reach for it to make a note file somewhere.
- Tell the user the Markdown schema note remains the source of truth.
- Resuming with different options is refused by design; start a new run when
  the model, thresholds, limit, or schema changed.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

Read [references/vault-schema-contract.md](references/vault-schema-contract.md)
for the schema, routing, dedupe, run-state, validation, caching, and apply
contract.
