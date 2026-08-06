---
name: vault-compose
description: Compose a vault note from material already in hand - a web-research run, existing notes, or the conversation you are having - in the block order and voice the vault declares, with every name and link checked against its sources. Use for "make a note out of this research", "combine these notes", "turn what we talked about into a note". Proposes with ids; writes nothing until the user accepts one.
---

# Vault Compose

The other note-writing skills each take one kind of input: `vault-capture` splits
a braindump, `vault-transcripts` cleans a recording, `vault-wiki` fills a
template. This one takes a **source set** — any mix of conversation excerpts,
vault notes, and research claims — and writes a note built from the blocks
`99 Meta/99.02 Schemas/0.04 Note Format.md` declares.

Three shapes reduce to one run spec, differing only in the `kind` of their
sources:

| Intent | Sources | For |
| --- | --- | --- |
| `research` | `web-claim` | A deep-research run becoming a note. |
| `synthesis` | `vault-note` | Several existing notes becoming a new one. |
| `conversation` | `chat` | What you just talked about becoming a note. |

Everything runs on the local LAN endpoints. Nothing is sent anywhere.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   from the user or the injected vault context.
2. Check the environment before a first run:

   ```bash
   python3 <skill-directory>/scripts/vault-compose.py doctor --vault <vault>
   ```

   This reports whether the inbox is writable, whether the schema defines the
   capture types this skill writes, whether the note-format note declares a
   `### Block order` table — **it cannot compose without one** — and whether the
   `chat` endpoint answers and is actually non-thinking.
3. **Write the source set to a file.** Every unit's `text` is what the grounding
   check reads, so it must be the material *verbatim*, not your summary of it.
   For a conversation, copy what was actually said. Save the spec to
   `<workflow root>/Composed Notes/<slug>/run-spec.json` — the absolute path the
   injected vault context names — or `forge-output/vault-compose/` outside a
   vault.
4. Compose:

   ```bash
   python3 <skill-directory>/scripts/vault-compose.py compose --spec <run-spec.json> --vault <vault>
   ```

5. Read `report.md`, which renders each proposed note in full, and relay to the
   user: each note's id, title, destination and blocks; anything held for review
   and why; and what the reviewer flagged.
6. Write only what the user names:

   ```bash
   python3 <skill-directory>/scripts/vault-compose.py apply --vault <vault> --run <run-directory> --accept n-001
   ```

7. Offer `vault-organizer inbox` next, which files what this wrote.

## The run spec

```json
{
  "version": 1,
  "intent": "synthesis",
  "request": "what the user asked for, in their words",
  "noteType": "note",
  "titleHint": null,
  "date": "2026-08-05",
  "maxNotes": 3,
  "sources": [
    {
      "kind": "vault-note",
      "label": "Codebook Consistency",
      "text": "<the note's body, verbatim>",
      "wikilink": "[[Codebook Consistency]]",
      "origin": {"path": "04 Technology/Codebook Consistency.md"}
    }
  ]
}
```

For `research`, name the run instead of transcribing it — the claims and the
quotes under them are already on disk, and a spec that restated them would be a
spec that could restate them wrong:

```json
{"version": 1, "intent": "research", "request": "...", "researchRun": "<deep-research run directory>", "sources": []}
```

Each surviving claim becomes one source: the claim's own wording followed by the
quotes supporting it, carrying the URL those quotes came from. A claim the
research run's *own* reviewer flagged is dropped, and so is one whose every quote
was flagged — a claim with nothing behind it is exactly what a research note
should not repeat. `claimLimit` caps how many are offered; `includeUnsupported`
keeps the unsupported ones, marked.

`label` is what a citation is credited to. `wikilink` and `url` are what that
unit *licenses* the note to link: a `[[link]]` or URL no source carries holds the
note back. `origin` is for the provenance block and is never read by a check.

## How a note comes to look like the vault's

- **The block grammar** — `0.04 Note Format.md` declares which blocks exist, in
  what order, and which a machine may write. The outline call picks from that
  list; the renderer emits them in the declared order and refuses any block the
  vault does not declare or that is owner-authored. A vault that has not written
  a `### Block order` table gets a clear error, not a guess.
- **The voice note** — `0.01 Voice and Style.md`, the same policy `vault-capture`
  and `vault-transcripts` read, in owner-authored mode.
- **The per-type shape** — the `## Per-type shapes` row for the note's `type`,
  which says how that kind of note arranges its blocks.

## What holds a note back

Everything below is deterministic and runs before the reviewer:

- A name, number, link, or `[[wikilink]]` that is not in the sources **that block
  cites**. A block may not borrow a specific from a source it never cited — that
  is what stops a note whose sections are each plausible from being collectively
  a collage.
- Callout syntax, frontmatter, or a `#` title written by the drafter. Those are
  added by the renderer once a block has passed its checks; a block that writes
  its own puts every check at the mercy of the model getting `>` prefixes right.
- A note under 40 words, or over 1200 — past that it is really several notes, and
  `maxNotes` is how to ask for them.
- Anything `check_grammar` calls an error against the vault's own block order.

A held note is reported, never written. Relay the reasons; do not rerun with
different options to make them go away.

## Rules

- Nothing is written until the user names an id. This skill proposes.
- Notes are created, never overwritten. A taken name gets a numbered suffix.
- `capture_type` records the **channel** — `chat` for a conversation, `generated`
  for research and synthesis — and the mandatory `> [!provenance]-` block records
  that a machine wrote the note. One property cannot answer both.
- The provenance block is written in code, never by the model. `0.04` requires it
  to be accurate about what made the note, and a model cannot be accurate
  about that.
- No `domain` is written. Guessing one buries a note where nothing looks for it;
  `vault-organizer` reads the note and decides.
- Never edit an existing note here. If the user wants one changed, that is a
  vault edit and goes through the skills that gate them.
- Never put your own summary in a source unit's `text`. The moment the text is
  yours, the grounding check is checking the model against itself.

## Reference

`references/compose-note-format.md` — the run spec in full, what each stage does,
and what each deterministic check catches.
