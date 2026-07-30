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

   This verifies the schema note parses, reports whether the schema has a `wiki`
   domain yet, and probes both endpoints. Pair judgments are one call each, so
   they run on the non-thinking service (`llms:8004`, model `chat`) — the
   agent's configured `connectedServices.chat` unless overridden. Doctor warns
   if that endpoint is actually reasoning.
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

   For a **deep-research** run, add `--notes` when the user wants the research
   as notes rather than as one report:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py import-run <run-directory> --vault <vault> --notes
   ```

   This groups the run's claims into one note per subtopic, writes each a
   source-policy introduction under `## Synthesis`, and renders the rest
   deterministically: a `## Findings`
   list of the claims with the quotes behind them, a `## Sources` list of URLs,
   and a `## Provenance` block naming the source run, its fingerprint, and the
   claim ids. Every note is a proposal like any other, forced to
   `capture_type: generated`, and capped by `--notes-limit` (6 by default).

   Three things are worth relaying to the user:
   - No claim is dropped. Anything the grouping missed lands in a final "Further
     Findings" note.
   - A claim the source run's own reviewer flagged is left out of the note body
     and listed under `## Provenance` as excluded.
   - The notes are reviewed on the thinking model. A flagged note is still
     proposed, with the objection in a callout at the top — it is the user's
     decision, not the reviewer's.

   `--notes` needs no templates, so it works in a vault that has not written its
   wiki templates yet; the wiki half is skipped with a warning naming the exact
   paths it wanted.
6. Propose connections. Start with `--limit` on a first run so the user sees the
   shape of the output before committing to a long batch:

   ```bash
   python3 <skill-directory>/scripts/vault-connections.py propose --vault <vault> --limit 40
   ```

   Proposals are then reviewed by the thinking model, which marks the ones it
   doubts and sorts them first so they land in the first ten you show. This is
   annotation only — nothing is ever dropped, and the human still decides every
   proposal. `--no-verify` skips it; an unreachable reviewer leaves proposals
   unannotated with a warning.

6. **Review with the user, ten at a time.** This is the point of the skill — do
   not dump the whole list. For each proposal give both note titles, the
   strength, and the one-line reason. Mention when a proposal was flagged in
   review and what the objection was. Ask which ones to apply. Then:

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
- `--voice <path>` selects the policy note and `--no-voice` disables it.
- `--profile <path>` selects the personal-context register and `--no-profile`
  disables it. The always-tier reaches the byte-stable judgment system prompt;
  cards triggered by a pair go in that pair's user message, gated by the union of
  both notes' routes. It replaced a hardcoded paragraph of one owner's biography
  in `CONNECTION_SYSTEM`, so judgments now improve with the register rather than
  being wrong for everyone else. See
  `../vault-transcripts/references/personal-context-format.md`.
  Source-derived policy applies only to generated wiki definitions and research
  synthesis. Search, link judgment, frontmatter-only edits, imported bodies,
  and deterministic provenance never receive owner-voice imitation.
- Generated critique, when a workflow actually produces it, belongs under a
  separate `## Critique`; it is never blended into source description.
- Imported wiki notes use vault-owned templates from the schema-compiled
  `meta/templates` route. The templates `vault-wiki` installs are richer than the
  five fields this skill fills, so any placeholder left unfilled here is dropped
  along with the heading it would have emptied — an imported note never carries a
  literal `{{key_works}}` or an empty `## Key Works`.
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
- Never create or edit a wiki template as a side effect of any command here. The
  only writer is `vault-wiki template-install`, which the vault owner runs
  deliberately and which refuses to overwrite a template they have modified
  unless `--force` is passed. From this skill, report the exact schema-compiled
  path and say that command installs it.
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
