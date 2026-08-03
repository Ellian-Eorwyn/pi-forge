---
name: vault-projects
description: Resolve a vault project into the closed set of files an agent may work from, and freeze it as a _corpus.json manifest beside the project's hub note. Use to hand a project to an agent, ask what belongs to a project or what its corpus is, reach its shared sources without duplicating them, draft or check a project hub, or pack a project into one context file. Not for filing notes (vault-organizer) or vault-wide search (vault-connections).
---

# Vault Projects

Handing an agent a folder works because the folder answers "what may I read". A
vault breaks that answer in one place: a source cited by two projects lives once
in the sources tree, so the folder is no longer the whole project. Copying it in
would restore the handoff at the cost of the thing that makes a vault worth
having — one copy of everything.

This skill keeps the folder handoff and gives up nothing. The project's hub note
carries a `## Corpus` section the owner maintains by hand — the sources, people,
organizations, and wiki cards that belong to the work, each with a line saying
why — and that section is also the machine-readable definition of scope. `emit`
freezes the resolution into `_corpus.json` beside the hub, so an agent that has
never heard of pi-forge can open the folder, read one file, and know every path
it is allowed to read.

Everything here is deterministic: a folder walk, one hub parse, and a header read
per member. No model is called and nothing is fetched.

## The three ideas

**One artifact, two readers.** The hub is what a person opens to see what the
project is, and the links they maintain for their own sake are exactly what an
agent resolves. A separate machine-readable scope file would drift from the human
one within a month; this cannot, because a corpus that is wrong is visibly wrong
to the person maintaining it.

**Closed world, and it says so when it is short.** Membership is the project
folder plus what the hub lists, plus two closures that reunite documents split
across two files. Nothing follows `related`, nothing walks the link graph, and an
ambiguous link resolves to nothing rather than to a guess. An agent that needs
something absent from the corpus says so; it does not go looking.

**The body is the contract.** Corpus semantics live in the hub's `## Corpus`
section and in a non-Markdown manifest — never in frontmatter. The vault's
approved-property list is closed and unapproved keys are deleted on rewrite, so a
`corpus:` property would silently vanish on the next organizer run.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or known configuration.
2. See where every project stands:

   ```bash
   python3 <skill-directory>/scripts/vault-projects.py list --vault <vault>
   ```

   Reports each registered project, whether it has a hub note, and whether its
   manifest is current. Projects without a hub are the work queue.
3. Check one project, or all of them:

   ```bash
   python3 <skill-directory>/scripts/vault-projects.py doctor --vault <vault> --project "Article 2"
   ```

   Reports missing hubs, a hub with no `## Corpus` section, links that do not
   resolve or that match two notes, exclusions matching nothing, transcript
   sections naming more than one note, notes embedded but not listed, and stale
   manifests. **Relay the errors and stop.** Every one of them is a decision the
   owner makes in the hub note; this skill never edits a hub.
4. Look at what resolves before freezing it:

   ```bash
   python3 <skill-directory>/scripts/vault-projects.py resolve --vault <vault> --project "Article 2"
   ```
5. Freeze the manifest:

   ```bash
   python3 <skill-directory>/scripts/vault-projects.py emit --vault <vault> --project "Article 2"
   ```

   A dry run by default: it prints the manifest and what changed since the last
   one. Add `--apply` to write `_corpus.json`. A corpus with an unresolved or
   ambiguous link refuses to freeze rather than freezing a wrong answer.

## Starting a hub from nothing

`draft-hub` builds a skeleton from what the vault already knows. Without
`--apply` it writes only to the workflow root, so the draft can be read first:

```bash
python3 <skill-directory>/scripts/vault-projects.py draft-hub --vault <vault> --project "Article 2"
```

With `--apply` it puts the hub where the owner actually works — the project
folder — because a hub in a run directory is a hub nobody browses, and the
`## Corpus` section only earns its keep by living in the note they already open.
What it does there depends on what is already present:

- **No hub** — writes `<project folder>/<Project>.md`.
- **A hub without `## Corpus`** — inserts only that section, before `## Notes`
  when that heading exists, since `## Notes` is owner-authored and always last.
  Every other line of the note is left exactly as it was.
- **A hub that already has `## Corpus`** — refuses, and says so. Regenerating
  would discard annotations someone wrote by hand.

It seeds `### Sources`, `### People`, `### Organizations`, and `### Wiki` from
every note in the vault that already carries `project: "[[Article 2]]"` and lives
outside the project folder, each as a bullet with an empty annotation for the
owner to fill in. Notes already in the folder are left out on purpose: they are
members by position and listing them adds nothing.

Where two notes in the vault answer to one basename, the draft writes the full
path rather than the bare name. A bare link to a contested name is not a link the
resolver will accept, so writing the path now is the difference between a hub
that resolves the first time it is used and one that has to be repaired by hand.

The draft is a starting point, not an answer. Read it with the owner, cut what
does not belong, write the annotations, then place the finished note in
`00 Inbox` and let `vault-organizer inbox --reuse-frontmatter` file it — the
frontmatter is already canonical, so that path costs no model call.

## Packing a project for a model

```bash
python3 <skill-directory>/scripts/vault-projects.py pack --vault <vault> --project "Article 2" --budget 100000
```

Concatenates every Markdown member, hub first, into one file under the workflow
root, with a per-file header naming its path and role. The budget is in tokens,
estimated at four characters each; members that do not fit are named in the
result rather than silently dropped. The output folder is marked
`.forge-workspace`, so the pack is never classified, filed, or counted as a
member of the corpus it copies.

Prefer handing over the folder and its `_corpus.json` when the agent can read
files. Packing is for a model that only takes text.

## What to relay to the user

- **Projects with no hub are the headline.** A project without a hub note has no
  corpus beyond its own folder, which means every shared source it depends on is
  invisible to an agent. Say how many, and name them.
- **Name the unresolved link, not the count.** `[[Kawitzky - 2020]]` failing to
  resolve is usually a renamed or never-created note, and the owner knows which
  the moment they see the title.
- **An ambiguous link is a vault problem, not a corpus problem.** Two notes
  share a basename; report both paths so the owner can decide which the hub
  meant and qualify the link.
- **Report a stale manifest as one line and offer the rerun.** Staleness is
  normal after a filing pass; it means the manifest lists a different set than
  the vault does now, not that anything is broken.
- **Never present a resolved corpus as complete when errors were reported.** A
  corpus that dropped two links is a corpus that will make an agent answer "not
  in the project" about something that is.

## Rules

- Never edit a hub note. Its `## Corpus` section is the owner's, and a skill that
  edits the definition of its own scope is not a boundary.
- Never write anything into the vault except `_corpus.json`, and only with
  `--apply`.
- Never add a project to the schema's project registry, or any other schema row.
  Report what is missing and let the user decide.
- Never resolve an ambiguous link by picking one. Report both paths.
- Never expand membership beyond the folder, the hub's `## Corpus` section, and
  the two closures. Not `related`, not `parent`, not links found in member
  bodies.
- Never copy a source into a project folder to make the folder complete. That is
  the duplication this skill exists to prevent.
- Never treat a run directory's machine artifacts as corpus members. Extraction
  runs live in the workflow root; a hub cites them as provenance in prose,
  outside `## Corpus`.
- Keep stdout machine-readable; diagnostics belong on stderr.

## Reference

The membership rules, the annotation and exclusion syntax, the manifest shape,
and the staleness definition are specified in
`references/project-corpus-contract.md`. The resolver is
`forge/lib/vault_corpus.py`. The rule an agent reads at work time lives in the
vault at `99 Meta/99.08 Agent Rules/Project corpus rules.md`.
