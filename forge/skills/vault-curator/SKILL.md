---
name: vault-curator
description: Research how a field catalogues its records and propose what that means for an Obsidian vault's schema - note types, domains, subdomains, wiki kinds, body tables - each proved against a candidate copy of the schema note. Use to start cataloguing something new, extend or review the schema against how a discipline works, or act on the organizer's schema suggestions. Adds rows additively; use vault-organizer to file notes, fix drift, or renumber.
---

# Vault Curator

`vault-organizer` files notes into the schema that exists. This skill proposes
what the schema should be, for a kind of thing the vault does not yet know how
to hold.

It is built around one measured weakness. The local non-thinking model, asked an
open question, answers with whatever the prompt made salient and silently omits
the rest — `docs/service-split-handoff.md` §2.1 measured four categories where a
reasoning model found eight. Left to itself it proposes "identification, habitat,
notes" for any subject on earth. So the enumeration of what a catalogue has to
decide is **shipped**, the field's actual practice is **fetched**, the moves are
a **closed set**, and every schema row is **proved** against a candidate copy of
the note before anyone reads it.

A run with no network proposes nothing and says so. That is the point: a schema
drafted out of a local model's training data is the thing this skill exists to
prevent.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or known configuration.
2. Check the environment first:

   ```bash
   python3 <skill-directory>/scripts/vault-curator.py doctor --vault <vault>
   ```

   This verifies the schema parses, reports the drift baseline, checks both
   reference files, runs `web-research doctor`, and probes both endpoints.

   **A `high` drift finding means stop.** A proposal is proved by showing it
   introduces no new high-severity drift; on a vault that already has one, that
   proof says nothing. Send the user to `vault-organizer drift` first.

   Also worth acting on before a long run: **web-research unreachable** (every
   run would refuse to propose) and **a `chat` endpoint that is reasoning**
   (drafting will be far slower and spend hidden tokens per call).
3. Run a proposal. There are three entry points and they answer different
   questions:

   ```bash
   # "I want to start cataloguing X."
   python3 <skill-directory>/scripts/vault-curator.py propose --vault <vault> \
       --subject "mineral specimens"

   # "The vault is already straining somewhere — where, and what would fix it?"
   python3 <skill-directory>/scripts/vault-curator.py propose --vault <vault> --from-vault

   # "Does what I already have match how the field actually works?"
   python3 <skill-directory>/scripts/vault-curator.py propose --vault <vault> \
       --refine wiki/animals
   ```

   `propose` never writes to the vault. It researches, reconciles, proves, and
   writes proposals into a run directory. Expect it to take a while: the deep
   research pass dominates, and there is one bounded model call per dimension in
   each of the practice and reconcile stages.

   `--research-dir <dir>` reuses an existing `web-research deep` run instead of
   researching again. `--no-web` skips research entirely, which makes the run
   propose nothing — it is for checking the machinery, not for producing a
   proposal.
4. Review with the user, ten at a time:

   ```bash
   python3 <skill-directory>/scripts/vault-curator.py review --vault <vault> \
       --run <run-dir> --limit 10 --offset 0
   ```

   Flagged proposals sort first. For each, give the move, the practice it
   implements, the claims behind that practice, and the exact row it would add.
   `report.md` is the readable version and `handoff.md` is the paste-ready
   migration doc.
5. Apply the ones the user names:

   ```bash
   python3 <skill-directory>/scripts/vault-curator.py apply --vault <vault> \
       --run <run-dir> --accept s-001,s-003 --dry-run
   python3 <skill-directory>/scripts/vault-curator.py apply --vault <vault> \
       --run <run-dir> --accept s-001,s-003 --reject s-002
   ```

   There is no `--accept-batch` and no `--all`. Every row is a definition
   entering the owner's own file, and the four `s-` ids in a run are not
   interchangeable the way twenty wiki drafts are.
6. Relay the run directory, the counts, what was held back, and the two
   follow-up commands `apply` prints.

## The moves

Reconciliation picks exactly one per practice, from this closed set. The three
that change the vault schema are the only ones the tool can apply itself.

| Move | What it means | Applied by |
| --- | --- | --- |
| `already-covered` | The vault already expresses this. The most common correct answer. | — |
| `topic-hub` | A non-routing hub note, where a new area should usually begin. | the user, by hand |
| `note-type` | One bullet under **Note types**. | `apply` |
| `domain` | One row in **Domains**, for records that are not reference cards. | `apply` |
| `subdomain` | One row under **Subdomains**. | `apply` |
| `source-kind` | One row in **Source kinds**. | `apply` |
| `capture-type` | One bullet in **Capture types**. | `apply` |
| `body-table` | A managed table section on an existing wiki kind, the way Phenology is. | patch file |
| `wiki-kind` | A new wiki kind: spec, template, source policy. | patch file |
| `naming` | A note-title convention. | the user, by hand |
| `refused` | The field wants something this vault's design does not offer. | — |

**`approved-property` is deliberately not a move.** The vault's property list is
global and closed: a new property would be inherited by every note type, and a
nested one is stripped the next time a note is filed. That is why phenology is a
body table. A field whose practice genuinely wants a property comes back as
`refused` with the argument attached, and the decision is the user's.

The **Project registry** is out of scope for the same kind of reason: registering
a project is a judgment about whether grouping files physically is useful, not a
fact about a field.

## What to relay to the user

- **Held-back proposals are the interesting output.** A move whose row would not
  parse, would collide, or would introduce drift is reported with the reason and
  dropped. Say which and why rather than only reporting successes.
- **"Not proposed" includes the good outcome.** `already-covered` is the answer
  the vault wants most of the time, and a run that mostly says so is a run that
  found a well-designed vault, not a run that failed.
- **A demoted route is a decision worth surfacing.** A `domain` or `subdomain`
  becomes a `topic-hub` when too few notes would fall there and the field's
  records do not accumulate — the schema's own "begin as a topic hub" rule. The
  proposal carries the note count it counted and the threshold it used.
- **Dimensions the research did not reach are listed by name.** A model that
  states a practice without citing a claim has it dropped, and the report says
  so. Thin research reads exactly like a field with no practice, and the two
  must not be confused.
- **A flagged proposal is the reviewer's doubt, not a verdict.** It is still
  proposed, with the objection attached, and it is the user's call.
- **Repo-side proposals are patch files, not edits.** A new wiki kind also needs
  three lines in `forge/lib/vault_wiki.py` that the tool deliberately does not
  write; `handoff.md` names them.

## Guarantees

- The schema note is only ever added to. No existing row's number, label, or
  definition can be reached by this skill, nothing is removed, nothing is
  renumbered, and **Approved properties** and the **Project registry** are never
  touched at all.
- Every proposed row is applied to a candidate copy of the schema note and the
  candidate is reparsed, revalidated for colliding derived paths, and
  re-drift-checked against the real vault. A row that fails any of that is never
  proposed.
- The accepted set is proved together as well as individually — two rows can each
  be legal and collide with each other.
- Numbers are chosen by code from the free slots in the parent registry, never by
  a model, and a subdomain's free list excludes the numbers domain-level projects
  already occupy in the same compiled namespace.
- `apply` re-proves against the schema note **as it is at that moment**, not as
  it was when the run was made, then backs the note up, writes to a temp file,
  reads it back, reparses it, and only then commits with `os.replace`.
- A practice with no claim id and no archived quote cannot reach a proposal.
- Nothing in this repository is written. Repo-side artifacts are patch files
  under the run's `patches/` directory.
- A rejected proposal is recorded in `.vault-curator/decisions.jsonl` and is not
  proposed again.

## Rules

- Never apply a proposal the user has not named. "Apply the sensible ones" is not
  an approval — list them and get the ids.
- Never edit the schema note by hand to make a proposal apply. If a row will not
  prove, that is the answer.
- Never present a `--no-web` run as a proposal. It cannot produce one.
- Never describe a held-back or dropped proposal as applied. `warnings` and the
  report's **Not proposed** section say what happened to each.
- Never claim a run was reviewed when `--no-verify` was passed.
- Never carry a number from the report into a hand edit without re-reading the
  registry; the free slot the tool chose was free when it looked.
- The Markdown schema note remains the sole source of truth.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

Read [references/schema-proposal-contract.md](references/schema-proposal-contract.md)
for the phase contract, the validation gate, the run layout, and the calibration
constants. The shipped enumeration is
[references/catalog-dimensions.json](references/catalog-dimensions.json) and the
per-field source policy is
[references/catalog-sources.json](references/catalog-sources.json).
