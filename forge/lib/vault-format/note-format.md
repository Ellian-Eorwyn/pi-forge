---
type: system
status: active
domain: meta
subdomain: schemas
capture_type: manual
---

# Note Format

What a note in this vault looks like. [[0.01 Voice and Style]] governs the words;
this note governs their arrangement — which blocks a note is built from, what each
one means, and which of them a machine may write.

Read this before writing a note or authoring a template.
[[0.00 Template Blueprint]] is the same rules as a file you can copy.

## Block grammar

A note is assembled from the blocks below, in this order. Every block is optional
except the title; none is ever reordered. A note that needs only a title and three
paragraphs is a finished note, and reaching for blocks it does not need is the most
common way to make one worse.

```
frontmatter          schema-controlled; see 0.00 Vault Schema
# Title              exactly one level-one heading
> [!summary]         the lead — what this note is
<body>               prose and ## sections, per 0.01 Voice and Style
> [!key]             the claims worth carrying away
> [!define]          terms this note stipulates
> [!evidence]        sourced material, with its citation
> [!caution]         limits and misuse
> [!question]        what is still open
> [!reflection]-     generated interpretation
> [!connections]-    links out
> [!provenance]-     how this note was made
## Sources           a plain bullet list of links
## Notes             owner-authored; never written, never read
[^1]: …              footnote definitions, unheaded, at the end of the file
```

The apparatus blocks sit at the end because they are about the note rather than
part of it. A reader scrolling for content should reach the end of the content
before reaching the machinery.

Footnote definitions trail the last heading with no heading of their own. Obsidian
hoists them into its own rendered footnote area, so putting them under `## Sources`
leaves a visibly empty heading in reading mode.

### Block order

The same grammar, in the form a generator can read. Row order is block order, and
`vault_format.py` checks these rows against the fence above — two statements of one
thing in a single file drift silently otherwise, and the fence is the half a person
actually reads.

`Written by` says who may put content in a block. `schema` is serialized from
frontmatter rather than authored. `machine` marks apparatus a hand-written note
does not get. `owner` is off limits in both directions: never written, never read.

| Block | Syntax | Required | Written by | Means |
| --- | --- | --- | --- | --- |
| `frontmatter` | `frontmatter` | no | schema | schema-controlled; see 0.00 Vault Schema |
| `title` | `# Title` | yes | either | exactly one level-one heading |
| `summary` | `> [!summary]` | no | either | the lead — what this note is |
| `body` | `<body>` | no | either | prose and ## sections, per 0.01 Voice and Style |
| `key` | `> [!key]` | no | either | the claims worth carrying away |
| `define` | `> [!define]` | no | either | terms this note stipulates |
| `evidence` | `> [!evidence]` | no | either | sourced material, with its citation |
| `caution` | `> [!caution]` | no | either | limits and misuse |
| `question` | `> [!question]` | no | either | what is still open |
| `reflection` | `> [!reflection]-` | no | machine | generated interpretation |
| `connections` | `> [!connections]-` | no | machine | links out |
| `provenance` | `> [!provenance]-` | no | machine | how this note was made |
| `sources` | `## Sources` | no | either | a plain bullet list of links |
| `notes` | `## Notes` | no | owner | owner-authored; never written, never read |
| `footnotes` | `[^1]: …` | no | either | footnote definitions, unheaded, at the end of the file |

## Callout registry

The vault's whole visual vocabulary. Colour carries epistemic status; the quiet
register carries apparatus. Adding a row here means also adding it to
`.obsidian/snippets/loom-notes.css` and to `forge/lib/vault_reflection.py`, and
`vault_format.py` checks that the three agree.

| Callout | Means | Accent | Icon | Folded | Not for |
| --- | --- | --- | --- | --- | --- |
| `summary` | Orientation — what this note is | cyan `--color-cyan-rgb` | `lucide-align-left` | no | A second summary lower down; a note has one lead |
| `key` | The load-bearing claims; the skim target | blue `--color-blue-rgb` | `lucide-key` | no | Restating the lead in bullets |
| `define` | A stipulated meaning or term boundary | yellow `--color-yellow-rgb` | `lucide-book-open` | no | A term the note only mentions |
| `evidence` | Sourced and checkable, carries a citation | green `--color-green-rgb` | `lucide-quote` | no | A claim with no source this note actually read |
| `reflection` | Generated interpretation | purple `--color-purple-rgb` | `lucide-sparkles` | yes | Anything the owner wrote |
| `question` | Acknowledged uncertainty, still open | pink `--color-pink-rgb` | `lucide-help-circle` | no | A rhetorical question; a question the note answers |
| `caution` | Limits, misuse, contested ground | orange `--color-orange-rgb` | `lucide-alert-triangle` | no | Ordinary qualifications, which belong in the prose |
| `connections` | Relations to other notes | quiet `--callout-quote` | `lucide-link` | yes | A link that belongs in the sentence it supports |
| `provenance` | How this note was made | quiet `--callout-quote` | `lucide-history` | yes | Anything a reader needs in order to understand the note |

`summary` also answers to `abstract` and `tldr`; `question` to `help` and `faq`;
`caution` to `warning` and `attention`. Those are Obsidian's own aliases and they
render identically, because teaching a reader a distinction that does not exist is
worse than having no distinction.

Red is unclaimed on purpose. Obsidian's built-in `danger`, `error`, `failure`, and
`bug` keep it, so a genuine problem still reads as one. `quote`, `success`, `info`,
`tip`, `todo`, and `example` keep their stock rendering and stock meanings.

### Choosing a block

A callout marks a **kind of information**, never emphasis. The test is whether a
reader scanning for that kind of thing would want to find this. If a passage is
merely important, it is prose — bold it if it must stand out.

Three or more callouts in a short note is a sign the note is really a list of
fragments. One or two, with the rest as prose, is the normal shape.

`evidence` and `provenance` are the two that carry an obligation.
`evidence` must name a source the note actually has; `provenance` must be accurate
about what made the note, and a note written by hand does not get one.

## Headings

`##` for a note's own structure, `###` beneath it. `#` is the title and appears
once.

- Sentence case. No emoji, no numbering, no trailing colons.
- A heading names what is under it, not what the note is doing (`Distinctions`,
  not `Some distinctions worth noting`).
- Headings are the ownership boundary: a generator finds its section by the
  visible heading text, so renaming a heading moves the section out of reach.
  Aliases exist for this reason — a note saying `Key Ideas` where a spec says
  `Key Points` is recognised and updated in place rather than growing a duplicate.
- `## Sources`, `## Notes`, and `## Corpus` are the reserved names. `## Notes` is
  owner-authored: never written, never read, never quoted back. `## Corpus`
  appears only on a project hub, where it is both the human map of the project
  and the machine-readable definition of what an agent may read — see
  [[Project corpus rules]]. Renaming it silently empties a project's scope.

Headings carry a left rule from `loom-notes.css` so a section start is visible
while scrolling. That treatment is uniform because CSS cannot read a heading's
text — Obsidian exposes no attribute for it outside Publish — so no heading can be
coloured by what it says. This is why per-kind identity lives in callouts.

## What a machine may write

- **Frontmatter** is schema-controlled, except the human-owned properties in
  [[0.00 Vault Schema]]. `cssclasses` is human-owned: no tool sets it, and a
  rewrite carries whatever value the note already has.
- **The owner's words are the record.** Generated material goes in a callout or
  under its own heading, never blended into preserved language.
- **`## Notes` is untouchable**, in both directions.
- **A section is never moved or renamed** by a generator. Reordering a note the
  owner arranged is a change they did not ask for.

## Never do

- Never use inline HTML for styling. No `<span style=`, no `<div class=`. The CSS
  layer has uncontested control of appearance and keeping it that way is what
  makes a vault-wide change possible at all.
- Never write a literal colour. Everything derives from theme variables, so the
  active theme and light/dark follow for free.
- Never rely on colour alone. Every block carries an icon and a title word, so it
  survives greyscale, colour blindness, and the snippet being switched off.
- Never use a construct that stops reading correctly with the snippets off.
  Folding is the `-` after the callout type, which is Markdown, not CSS.
- Never invent a callout type. Add it to the registry above first, or use prose.
- Never use a callout for emphasis, decoration, or to break up a wall of text. The
  fix for a wall of text is paragraphs.

## Per-type shapes

Prose style for each of these is in [[0.01 Voice and Style]]; what follows is only
the arrangement.

| Type | Shape |
| --- | --- |
| `note` | Lead, then prose. `key` when the note has claims worth extracting; `define` when it stipulates a term. Headings only once it moves between parts. |
| `task` | The action first, in plain prose, then bullets. No lead callout — a task short enough to act on does not need summarising. |
| `journal` | The owner's language first and unaltered, then `reflection` sections in the order `Observations`, `Interpretations`, `Open questions`, then `connections`. |
| `source` | The source on its own terms. `evidence` for what it establishes, `## Synthesis` and `## Critique` only when they contribute something. Never a `reflection`. |
| `project` | Lead, then purpose, state, next actions, decisions, risks. `question` for what is genuinely undecided. A registered project's hub adds `## Corpus` after the prose and before `## Notes`: `###` subsections by role, each bullet a link and an em-dash line on why it belongs. Files already in the project folder are members without being listed. See [[Project corpus rules]]. |
| `concept` / wiki card | Lead as `summary` — `vault-wiki` writes it as `[!abstract]`, which is the same callout — then the kind's sections from `wiki-kinds.json` as `##` headings, then `## Sources`, `## Notes`, footnotes. A card is skimmed, so it stays short. |
| `index` | A hub. `> [!hero]` and `> [!card]` from [[Dashboard editing rules]] apply here and nowhere else, and require `cssclasses: [loom-dashboard]`. |

## Where this is implemented

Three files implement this note, and `vault_format.py` checks they agree with it:

- `.obsidian/snippets/loom-notes.css` — the registry's accents and icons.
- `forge/lib/vault_reflection.py` — the code that emits callouts.
- `99 Meta/99.03 Templates/` — the templates, which must use only registered blocks.

A callout that appears in one of those and not in this note is a defect, not a
local exception.
