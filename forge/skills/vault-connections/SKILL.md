---
name: vault-connections
description: Search an Obsidian vault by meaning, propose links for per-id review, publish completed literature or deep-research runs into the inbox, and create schema-routed wiki entity notes from research evidence or unresolved links. Use when the user asks to search my vault, suggest connections, fill related, send or import a literature or research run to my vault, turn research outputs into concept or term notes, create a wiki note, or resolve wikilinks.
---

# Vault Connections

The companion to `vault-organizer`. The organizer decides where existing notes
live; this searches and connects them, and publishes reviewed copies of completed
research outputs. It never moves, renames, deletes, or replaces an existing note.
Existing-note writes only append quoted wikilinks to `related`. Accepted imports
create new inbox or wiki files.

Nothing is written without the user naming the proposal ids they approve.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or known configuration.
2. Check the environment before a long run:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py doctor --vault <vault>
   ```

   This verifies the schema note parses, reports wiki-template readiness without
   making missing templates a general doctor failure, and probes both endpoints.
   The default chat endpoint is the
   non-thinking `llms:8004`; for a thinking backend add
   `--base-url http://llms:8008/v1/chat/completions --think-prefill`.
3. Build or refresh the index. Every command does this on its own, so run it
   explicitly only for the first pass on a large vault:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py index --vault <vault>
   ```

4. Answer questions about the vault with `search` before reading whole notes:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py search --vault <vault> "<query>"
   ```

   Results are hybrid-ranked. Read the full note only when the snippets are
   insufficient.
5. When the user asks to send a completed literature, meta-literature, or deep
   research run to the vault, use `import-run`:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py import-run <run-directory> --vault <vault>
   ```

   The defaults propose the primary report artifacts for `00 Inbox` and
   evidence-backed `concept,term` wiki notes. Use `--include-artifact
   <run-relative.md>` for another Markdown artifact, `--wiki-kinds` to select
   from `concept,practice,place,event,term,work,figure`, `--title-prefix` to
   override the derived filename prefix, and `--limit` to cap wiki candidates.
   The command invokes the source workflow's validator in read-only mode and
   fails closed on an incomplete run or a missing selected-kind template.
6. Propose connections. Start with `--limit` on a first run so the user sees the
   shape of the output before committing to a long batch:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py propose --vault <vault> --limit 40
   ```

7. **Review with the user, ten at a time.** This is the point of the skill — do
   not dump the whole list. For each proposal give both note titles, the
   strength, and the one-line reason. Ask which ones to apply. Then:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py apply --vault <vault> --run <run-dir> --accept c-001,c-004,c-007 --reject c-002,c-003
   ```

   Pass the declined ids to `--reject` in the same call, so they are recorded and
   never proposed again. Use `--dry-run` first if the user wants to see the exact
   edits before anything is written. Continue to the next ten.
8. Maintain the unresolved-link wiki layer:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py wiki --vault <vault>
   ```

   This proposes stub notes for unresolved wikilink targets, and proposes links
   from existing wiki notes into the notes that correspond to them. Review and
   apply through the same accept/reject loop.
9. Report the run directory and the final counts.

## Guarantees

- Existing-note writes only append `related`. Every other property, every
  unapproved key, the body, the BOM, and line endings are preserved byte-for-byte.
- Imported reports preserve their Markdown body exactly while replacing old
  frontmatter with canonical schema-ordered metadata. They are proposed for
  `00 Inbox`; no source-run file is changed.
- Imported wiki notes use vault-owned templates from the schema-compiled
  `meta/templates` route. Pi-Forge documents the required template shape but
  never creates or edits those templates.
- A note with no frontmatter, or with an unclosed frontmatter block, is refused
  and reported — never given frontmatter. Run `vault-organizer` on those first.
- Both notes in an approved pair are linked to each other.
- A wiki stub is never created when a note with that basename already exists
  anywhere in the vault; Obsidian would resolve the link ambiguously. The
  collision is reported so the user can link to the existing note instead.
- People and organizations found among unresolved links are reported as
  `08 Directory` candidates and never created here. A link matching a registered
  project is reported as a missing project note, never turned into a wiki note.
- Every rewritten note is backed up under the run directory first, and every
  operation is journaled. Re-applying the same ids is a no-op.
- Apply verifies the reviewed proposal manifest and original note hashes;
  research imports are additionally bound to the vault used to generate them.
- Accepted and rejected pairs and source-run-scoped import proposals are recorded
  in `.vault-connections/decisions.jsonl`.

## Rules

- Never hand-edit `related`, proposals, or the decisions ledger outside the script.
- Never apply a proposal the user has not named. "Apply the strong ones" is not an
  approval — list them and get the ids.
- Never create a missing `Wiki Concept.md`, `Wiki Practice.md`, `Wiki Place.md`,
  `Wiki Event.md`, `Wiki Term.md`, `Wiki Work.md`, or `Wiki Figure.md` template.
  Report the exact schema-compiled path and let the vault owner create it.
- Never claim a refused or skipped note was updated; the `warnings` array says
  what was skipped and why.
- Never add a `wiki` domain, subdomain, or note type to the schema note yourself.
  If `doctor` reports the schema has no `wiki` domain, tell the user which rows
  the schema note needs and let them decide.
- The Markdown schema note remains the sole source of truth. Everything under
  `.vault-connections/` is generated state.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

Read [references/vault-connections-contract.md](references/vault-connections-contract.md)
for the storage layout, ranking, candidate selection, merge semantics, wiki
routing, and apply contract.
