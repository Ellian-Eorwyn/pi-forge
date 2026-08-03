# Project Corpus Contract

A project's **corpus** is the closed set of files an agent working on that
project may read. It is defined by two things and nothing else: the project's
folder, and the `## Corpus` section of the project's hub note.

This document is the canonical specification. The vault-facing summary an agent
reads at work time is `99 Meta/99.08 Agent Rules/Project corpus rules.md`; the
implementation is `forge/lib/vault_corpus.py`.

## Why it is shaped this way

Handing an agent a folder works because the folder answers "what may I read".
A vault breaks that answer in one specific place: a source cited by two projects
lives once in the sources tree, so the project folder is no longer the whole
project. Copying it in would restore the folder handoff at the cost of the one
property that makes a vault a vault — one copy of everything.

So the hub note carries the rest of the answer. It is the note a person opens to
see what the project *is*, and the same links they maintain for their own sake
are what an agent resolves. One artifact, two readers, no drift: a corpus that
is wrong is visibly wrong to the human who maintains it.

## Hub identity

For a project registered as `[[X]]` in the schema's **Project registry**, the hub
is the note at `<compiled project folder>/X.md` carrying `type: project` and
`project: "[[X]]"`.

- Exactly one hub per project. `doctor` reports `hub_missing` when it is absent.
- Other `type: project` notes in the folder are allowed and are ordinary
  members. `doctor` reports `extra_project_note` so a folder never appears to
  have two definitions of its own scope.

## Membership rules

Applied in this order. Everything else is out.

**1. Folder rule.** Every file under the project folder, recursively, is a
member with role `folder`. Includes non-Markdown. Excludes `_corpus.json`,
dotfiles, `.DS_Store`, symlinks, and any directory marked `.forge-workspace` —
that marker means machine artifacts from a run, which are never members.

**2. Hub rule.** Inside the hub's `## Corpus` section (an H2, matched by its
exact heading text), each `###` subsection names a **role** and each bullet
contributes its **first non-embed wikilink** as a member of that role. The role
is the subsection heading slugified: `### Working notes` → `working-notes`.

Recommended subsections, though the vocabulary is the owner's: `People`,
`Organizations`, `Sources`, `Wiki`, `Working notes`, `Deliverables`.

Listing a file that is already in the folder upgrades its role from `folder`;
it is never a second member.

**3. Transcript closure.** A Markdown member with a heading whose text is
exactly `Transcript` containing **exactly one** wikilink pulls that target in
with role `transcript`, `via: closure`. This is the vault's two-note transcript
pattern: a processed note and its verbatim source are one document in two files.
More than one link means the section is being used for something else, so
nothing is pulled and `doctor` reports `transcript_ambiguous`.

**4. Attachment closure.** An `![[embed]]` in a Markdown member that resolves to
a **non-Markdown** file joins with role `attachment`. Markdown and `.base`
embeds never close — that would be transitive expansion — but `doctor` reports
`markdown_embed` so a note that should have been listed gets noticed.

**5. Nothing else.** Not `related`, not `parent`, not links in member bodies.
Closures do not cascade: a transcript pulled in by rule 3 is not itself read for
further links.

## Annotation

Everything after the first ` — ` (space, em dash, space) on a bullet is human
annotation. It is carried into the manifest verbatim and never parsed, so an
annotation may safely mention other notes:

```markdown
- [[Suits - 2005 - The Grasshopper]] — core theory text; supersedes [[Old Draft]]
```

`Old Draft` is **not** a member. The member is the first link before the
separator.

## Exclusion

An optional `### Excluded` subsection removes in-folder files from implicit
membership, by wikilink or by a backticked folder-relative path:

```markdown
### Excluded
- `Private venting.md` — personal, not handoff material
```

An exclusion matching nothing is `dead_exclusion`, an error: a stale exclusion
reads as protection that is not there.

## Link resolution

Tried in order: exact vault-relative path (with or without `.md`), then unique
basename, then a note's `aliases`. Matching is case-insensitive.

A basename owned by more than one note is **ambiguous** and resolves to nothing.
This is deliberate. Picking one would make what an agent may read depend on walk
order, and the wrong pick is invisible. Qualify the link with its folder:
`[[05 Academic/5.01 Dissertation/Note|Note]]`.

Unresolved and ambiguous links are both errors, and `emit` refuses to write a
manifest while either exists.

## The manifest

`emit --apply` writes `<project folder>/_corpus.json`. It is machine-owned,
regenerable, and overwritten in place, atomically.

It is **not Markdown** on purpose. The vault's approved-property list is closed
and unapproved frontmatter keys are deleted on rewrite, so a `corpus:` property
would not survive; a JSON file beside the hub is invisible to classification and
survives everything.

Every path is vault-relative. `readme` and `rules_note` are how an agent that has
never heard of pi-forge learns the rule from the folder alone.

```json
{
  "version": 1,
  "readme": "Machine-generated project corpus. Agents: work ONLY from the paths in members[]…",
  "rules_note": "99 Meta/99.08 Agent Rules/Project corpus rules.md",
  "generated": "2026-08-03T18:00:00+00:00",
  "project": "Article 2",
  "project_value": "[[Article 2]]",
  "hub": "05 Academic/5.01 Dissertation/5.01.02 Article 2/Article 2.md",
  "folder": "05 Academic/5.01 Dissertation/5.01.02 Article 2",
  "hub_sha256": "…",
  "schema_hash": "…",
  "members": [
    {
      "path": "10 Sources/10.01 Book/Academic/Dissertation/Suits - 2005 - The Grasshopper.md",
      "role": "sources",
      "via": "hub",
      "title": "Suits - 2005 - The Grasshopper",
      "type": "source",
      "source_kind": "book",
      "annotation": "core theory text",
      "sha256": "…"
    }
  ],
  "unresolved": [],
  "excluded": [],
  "counts": { "members": 103, "by_role": { "folder": 22, "sources": 77 } }
}
```

### Staleness

Staleness is **membership drift**: a member added, removed, or re-roled, or a
changed `hub_sha256` or `schema_hash`. A member whose content changed is
reported as `changed` and is *not* staleness — that is the normal state of a
vault being worked in, and calling it stale would make the check cry wolf.

`doctor` reports it; `emit --apply` is always the fix. Never hand-edit the
manifest.

## Worked example

```markdown
---
type: project
status: active
domain: academic
subdomain: dissertation
project: "[[Article 2]]"
---
# Article 2

> [!summary]
> Utopian imagination in tabletop role-playing games — the literature corpus,
> the synthesis, and the argument being built from them.

Where the work stands, what is next, and what a person picking this up in six
months would need to know. Prose here may link anywhere; only `## Corpus`
defines scope.

## Corpus

Everything in this folder is part of the project already. Listed below is what
lives elsewhere in the vault.

### Sources
- [[Suits and Hurka - 2005 - The Grasshopper]] — core theory text; ch. 3 grounds the utopia argument
- [[Kawitzky - 2020 - Magic Circles]] — tabletop utopias

### Wiki
- [[Utopia]] — definitional anchor

### People
- [[Committee Chair]] — advises this corpus

### Excluded
- `Private venting.md` — personal

## Notes

Owner-authored. Never written by a skill.
```

## Two headings that look alike

`## Sources` at H2 is the note format's reserved citation section. `### Sources`
at H3 **inside `## Corpus`** is a membership role. They are different levels in
different places and the parser only ever reads inside `## Corpus`.

## Commands

| Command | Reads | Writes |
| --- | --- | --- |
| `list` | registry, hubs, manifests | nothing |
| `resolve --project X` | the corpus | nothing |
| `doctor [--project X]` | the corpus and its manifest | nothing |
| `emit --project X [--apply]` | the corpus | `_corpus.json`, only with `--apply` |
| `pack --project X [--budget N]` | the corpus | a context file in the workflow root |
| `draft-hub --project X` | notes carrying `project: "[[X]]"` | a draft in the workflow root |

Every command is deterministic. No model is called and nothing is fetched.
`emit --apply` is the only vault write, and `_corpus.json` is the only file it
writes.
