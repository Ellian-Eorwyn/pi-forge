# Capture Note Format

The contract for what `vault-capture` writes into a vault note, and how a
drafted note is allowed to differ from the braindump it came from. Part A is
embedded verbatim in `DRAFT_SYSTEM` in `scripts/vault-capture.py`; if you change
one, change both.

This skill is the counterpart to `vault-transcripts`, and the difference is the
whole design. That skill **preserves** a recording and cleans it, so its
invariant is that almost every word survives. This one **synthesizes** notes out
of raw thinking, so wording is expected to change completely and the invariant
is about substance instead.

## Note layout

```markdown
---
type: task
status: raw
capture_type: generated
---

The gasket on the espresso machine is cracked around the rim and leaks on a
double shot. A replacement needs ordering before the weekend, and the grinder
can be descaled on the same trip.

Worth checking whether the warranty covers the gasket first — if it does, none
of this costs anything.

# Braindump

<the braindump, byte for byte>
```

Rules the script enforces, not suggestions:

- The generated section contains no level-one heading. The only `# ` in the file
  is `# Braindump`, and only in the primary note.
- Everything after `# Braindump` is the source text unchanged. The drafts are a
  convenience; what the person actually said is the record.
- **One note per dump carries the braindump** — the primary, chosen
  deterministically as the longest surviving draft. Its siblings carry
  `related: ["[[<primary>]]"]`, so the original is one click away from any of
  them. A single-note capture is just the primary, which is the common case.
- Frontmatter carries only `type`, `status: raw`, and `capture_type: generated`,
  all validated against the vault's schema note. `vault-organizer` replaces this
  block when it files the note and reads `type` as an advisory hint, so it is
  accurate rather than complete. Domain, subdomain, project, and people are the
  organizer's judgment, not this skill's.
- `capture_type: generated` is forced in code, never taken from the model. A
  vault whose schema note does not define `generated` cannot be captured into at
  all: the run fails rather than writing an unmarked note. The words are the
  user's; the note is the machine's, and the vault says so.
- Titles pass through `safe_title` in `forge/lib/vault_schema.py`, the one place
  every vault skill names notes. A title may never contain `#`, `^`, `[`, `]`,
  or `|`: Obsidian cannot resolve a `[[wikilink]]` to such a note and the file
  will not sync to mobile.
- Notes are created, never overwritten. A name already taken in `00 Inbox` gets
  a numbered suffix, and a note this run already wrote keeps its original name
  on a rerun instead of being written twice.

## Part A — fidelity invariants

These outrank every style rule below.

```text
- Every idea, task, question, decision, and factual detail in the braindump appears in exactly one note.
- Never add a fact, name, date, number, link, or commitment the braindump does not contain.
- Preserve uncertainty. "I think", "maybe", and "I'm not sure" are content, not noise.
- The ideas are the person's; the wording is yours. Write what they meant, in clean prose.
```

- Keep the person's own terms. If they call it "the gasket thing", it is the
  gasket thing — a note they cannot search for in their own vocabulary is a note
  they will not find.
- Leave open questions open. Tidying "I'm not sure whether to replace it" into
  "replace it" is the most damaging thing this skill can do, because it is
  invisible afterwards.
- Dropping repetition, false starts, and thinking-out-loud scaffolding is the
  job. Dropping a point is not.

## Part B — style by kind

| Kind | Drafted output |
| --- | --- |
| `idea` | Flowing paragraphs. What the thought is, what it is for, what is unresolved. |
| `task` | A sentence of context, then bullets for the actual things to do. Blockers stay attached to the item they block. |
| `journal` | Chronological paragraphs, minimal intervention. Voice, feeling, and self-correction preserved. |
| `question` | The question stated plainly first, then what is already known or suspected. Never answered by the note. |
| `reference` | The facts, tightly. Names, numbers, links, and where they came from. |
| `draft` | The text being drafted, with the surrounding thinking kept separate from it. |
| `plan` | Steps in order, with dates and dependencies where the braindump gave them and `not stated` where it did not. |

Headings (`##` or deeper) only when the note genuinely moves between parts.
Bullets only for things that are really a list. A short note gets neither.

## Splitting

The split stage decides how many notes a dump becomes, and it is the weakest
joint in the pipeline: a non-thinking model asked "how many notes is this"
answers "one" almost every time. `SPLIT_SYSTEM` therefore names each kind of
thing a braindump can contain and asks the model to check for each one
separately, which is the enumeration finding from `docs/service-split-handoff.md`
§2.1 applied here.

Split when the parts would be looked for separately later. Keep together what
only makes sense read as one thing. Never split one line of thinking in half.
`--max-notes` (8 by default) is the ceiling, and the thinking model reviews the
division separately from the notes themselves.

## What the checks enforce before a model ever reviews the result

Deterministic, exact, and free, so the thinking model's budget goes to judgment
instead of catching malformed output. Anything in the first group holds the note
back: it is reported with its reason and not written.

| Check | Catches |
| --- | --- |
| No level-one heading, no frontmatter, no `# Braindump` in a draft | A draft that tried to author the note's structure |
| Invented names, mid-sentence | A person, product, or place the braindump never mentioned |
| Invented links | A URL the model supplied from its own memory |
| Primary note ends with the braindump byte-for-byte | Any drift in the thing that must not drift |
| Frontmatter keys and values against the schema note | Metadata the organizer would strip or reject |
| Title charset, length, and reserved names | Filenames that break Obsidian links or say nothing |

The second group cannot prove a problem, so it travels to the reviewer as
`notices` rather than throwing away a note that is probably fine:

| Notice | Why it is not a hold |
| --- | --- |
| Invented numbers | Rewording legitimately turns "a couple" into "2" |
| Capitalized words opening a sentence with no root in the braindump | Position alone does not distinguish a name from an ordinary word |
| Low retention of the braindump's distinctive vocabulary | Synthesis compresses; only a collapse is interesting |

After those pass, the thinking model reviews every note against the **full
braindump** — invention, dropped substance, false certainty, a title that names
the medium instead of the subject, a wrong kind — and separately reviews whether
the notes together cover the dump. It can flag, and a flag is either re-drafted
with reasoning or handed to a human. It never silently drops a result, and an
unreachable reviewer is reported as "not verified" rather than treated as
approval.
