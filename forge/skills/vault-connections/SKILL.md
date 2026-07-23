---
name: vault-connections
description: Search an Obsidian vault by meaning rather than filename, propose links between notes for the user to approve one by one, and maintain a wiki layer of concept/place/event/term/work entity notes. Use when the user asks to search my vault, find notes about a topic, suggest connections between my notes, link related notes, fill in the related property, turn unresolved wikilinks into notes, create a concept note, or connect wiki notes to the notes that mention them.
---

# Vault Connections

The companion to `vault-organizer`. The organizer decides where a note *lives*;
this decides what a note is *connected to*. It never moves, renames, deletes, or
reclassifies a note, and it never edits a note body — the only write is appending
quoted wikilinks to the `related` frontmatter property, plus creating new wiki
stub notes.

Nothing is written without the user naming the proposal ids they approve.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or known configuration.
2. Check the environment before a long run:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py doctor --vault <vault>
   ```

   This verifies the schema note parses, reports whether the schema has a `wiki`
   domain yet, and probes both endpoints. The default chat endpoint is the
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
5. Propose connections. Start with `--limit` on a first run so the user sees the
   shape of the output before committing to a long batch:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py propose --vault <vault> --limit 40
   ```

6. **Review with the user, ten at a time.** This is the point of the skill — do
   not dump the whole list. For each proposal give both note titles, the
   strength, and the one-line reason. Ask which ones to apply. Then:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py apply --vault <vault> --run <run-dir> --accept c-001,c-004,c-007 --reject c-002,c-003
   ```

   Pass the declined ids to `--reject` in the same call, so they are recorded and
   never proposed again. Use `--dry-run` first if the user wants to see the exact
   edits before anything is written. Continue to the next ten.
7. Maintain the wiki layer:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py wiki --vault <vault>
   ```

   This proposes stub notes for unresolved wikilink targets, and proposes links
   from existing wiki notes into the notes that correspond to them. Review and
   apply through the same accept/reject loop.
8. Report the run directory and the final counts.

## Guarantees

- Only `related` is ever written, and only by appending. Every other property,
  every unapproved key, the body, the BOM, and the line endings are preserved
  byte-for-byte.
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
- Accepted and rejected pairs are recorded in `.vault-connections/decisions.jsonl`
  and are never proposed again.

## Rules

- Never hand-edit `related`, proposals, or the decisions ledger outside the script.
- Never apply a proposal the user has not named. "Apply the strong ones" is not an
  approval — list them and get the ids.
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
