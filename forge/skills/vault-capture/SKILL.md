---
name: vault-capture
description: Turn a braindump into schema-valid notes in an Obsidian vault inbox - split unedited thinking into separate notes, give each a real title and schema frontmatter, and keep the user's own words rather than paraphrasing them. Use for stream-of-consciousness dumps, meeting scribbles, and idea lists that should become individual notes. Nothing enters the vault until the user approves the proposed notes.
---

# Vault Capture

The user says "make a note about this" and then talks through it. That input is
thinking, not a document: no structure, more than one subject, and half of it
scaffolding. This skill turns it into notes worth finding again — one or several
— and writes them to `00 Inbox`.

It is the counterpart to `vault-transcripts`, not a replacement. That skill
**preserves** a recording and cleans it. This one **synthesizes** notes out of
raw thinking, so the wording changes completely and the braindump is kept
verbatim underneath. If the input is a transcription app's export, run
`vault-transcripts` — this skill detects that shape and refuses it.

Everything runs on the local LAN endpoints. Nothing is sent anywhere. That is a
requirement, not a detail — a braindump is the least edited thing a person
writes.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or the injected vault context.
2. Check the environment before a first run:

   ```bash
   python3 <skill-directory>/scripts/vault-capture.py doctor --vault <vault>
   ```

   This reports whether the vault and inbox are writable, whether the schema note
   defines the vocabulary this skill writes (`status: raw`, `capture_type:
   generated`), and whether both endpoints answer. The `chat` check also reports
   whether that endpoint is actually non-thinking: splitting and drafting are one
   call each per unit, so a thinking endpoint there wastes hundreds of hidden
   tokens on every one.
3. **Write the user's words to a file, verbatim.** When the braindump arrived in
   chat, save exactly what they said to `forge-output/vault-capture/<slug>.md`
   before running anything — or, inside the vault, to
   `99 Meta/99.06 Workflows/Captures/<slug>.md`, at the absolute path the
   injected vault context names.
   Do not paraphrase, tidy, summarize, or reorder it —
   that file is what gets preserved in the note, and a cleaned-up copy makes the
   preservation a lie. Then pass the path.
4. Capture:

   ```bash
   python3 <skill-directory>/scripts/vault-capture.py capture <file.md> --vault <vault>
   ```

   Notes are written to `00 Inbox` straight away. This skill only ever creates
   new files — it never edits, moves, renames, or deletes anything — which is why
   it does not need an approval gate the way `vault-organizer` does. Use
   `--dry-run` for a first trial on a new vault, then rerun with `--run
   <run-directory>` to write without recomputing anything.
5. Read `report.md` and tell the user:
   - each note written, its title, its kind, and where it went
   - which note carries the braindump verbatim
   - notes held back and why — these were **not** written, and the reason is
     usually that a check found something in the draft that is not in the
     braindump
   - the Verification section: how many notes the thinking model reviewed, what
     it flagged, what was re-drafted, what it left for them
   - any notice about how the dump was divided
6. Offer `vault-organizer inbox` next, which files what this wrote, and then
   `vault-connections propose` to link it into the rest of the vault.

## Input

| Form | How |
| --- | --- |
| Typed or spoken in chat | Save verbatim to a file, pass the path |
| A file the user already has | Pass the path |
| Piped text | `--stdin` |
| Audio | Run the `transcription` skill first, then pass its corrected transcript |

Several braindumps can be captured in one run. Each is split and drafted
independently, and one bad input never blocks the others.

## Settings

| Flag | Default | Meaning |
| --- | --- | --- |
| `--max-notes` | `8` | Ceiling on how many notes one braindump becomes. |
| `--filename-pattern` | `topic` | The note title as the filename. `date-topic` prefixes today's date. |
| `--dry-run` | off | Plan, check, and verify without writing. |
| `--force` | off | Synthesize from input that looks like a transcript export. |
| `--voice` | the vault's | The voice-and-style note. |
| `--no-voice` | off | Disable the voice policy for this run. |
| `--profile` | the vault's | The personal-context register. Reaches the draft system prompt only — never the draft payload, which is fidelity-gated. |
| `--no-profile` | off | Disable personal context for this run. |
| `--no-exemplars` | off | Draft without showing the model the user's own notes. |

These are fingerprinted into a run, so a resumed run refuses to change them.

## How notes come to sound like the user

Two mechanisms, both optional and both degrading to nothing:

1. **The voice note** — `99 Meta/99.02 Schemas/0.01 Voice and Style.md`, the
   companion to the schema note. The schema says how notes are *structured*;
   this says how they are *written*. Sections: `## Global voice`,
   `## Per-type style`, `## Vocabulary`, `## Formatting`, `## Never do`. Rules
   may be scoped under `### Universal`, `### Owner-authored`, and
   `### Source-derived`. Capture always selects owner-authored mode because its
   input is the vault owner's braindump. The user writes the note; this skill
   reads it and never creates it. A vault without one captures exactly as before.
2. **Style examples** — before drafting, the nearest notes to this topic are
   pulled from the vault through `vault-connections search` and shown to the
   model as examples of how this person writes. Anything the pipeline itself
   wrote is excluded: a model shown its own output learns its own habits. The
   run journals which notes each draft was shown, in `exemplars.json`.

When the user reacts to a note's style — "too formal", "don't bullet my
journal", "stop calling me the user" — that is a rule worth keeping, and it goes
in the voice note rather than into the next prompt by hand:

```bash
python3 <skill-directory>/scripts/vault-capture.py preferences --vault <vault> --feedback "don't bullet my journal entries"
```

This **proposes** edits with ids and shows the note as it would read. Nothing is
written until the user names what they accept:

```bash
python3 <skill-directory>/scripts/vault-capture.py preferences --vault <vault> --run <run-directory> --accept p-001
```

The voice note is a note the user wrote, so editing it follows the same rule as
every other existing note: propose, show, and change nothing without being told
which proposals to take. The previous version is backed up into the run first,
and a note that changed since the proposal was made is refused rather than
overwritten. Accepted edits preserve frontmatter, scoped rules, and unrelated
human-authored sections.

Every kind but `draft` ends with a reflection. A journal keeps the cleaned
authorial account first and adds non-empty `Observations`, `Interpretations`,
`Open questions`, and `Connections`; `idea`, `task`, `question`, `reference`, and
`plan` get `Context`, `Open questions`, `Next steps`, and `Connections`. A
`draft` gets none — it is prose being composed, and machine commentary appended
to it damages the draft.

Each of those renders as a collapsed callout — `> [!reflection]- <Section>`, and
`> [!connections]- Connections` — so a reader can tell the machine's reading of a
braindump from their own thinking, which a `##` heading could not do. The model
still writes `##` and the deterministic checks still read `##`; the callouts go
on afterwards, once the note has passed them.
`forge/skills/vault-transcripts/references/loom-notes.css` styles these callout
types vault-wide, the same ones `vault-transcripts` writes. A vault without it
still reads correctly, since folding is Markdown rather than CSS.

Reflections source from the vault first, as wikilinks to notes that exist.
Material from outside the vault is admissible only as text the run actually read —
a link in the braindump, or a citation already imported into a vault note — and
such a connection begins `Outside vault:` and carries that source's URL. Nothing
is fetched at draft time, so a fact the model merely recalls cannot be checked and
holds the note, exactly as an invented link does. The original braindump remains
byte-identical under `# Braindump`.

## Rules

- Never paraphrase the braindump when saving it. The note preserves that file
  byte for byte, and the check that proves it will hold the note back if the
  text does not match.
- Notes are created, never overwritten. A taken name gets a numbered suffix, a
  note this run already wrote keeps its name on a rerun, and a note the user has
  since edited is left alone.
- Every note carries `capture_type: generated`. This is forced in code, not
  suggested to the model. If the vault's schema note does not define
  `generated`, the run fails rather than writing an unmarked note.
- Held notes are reported, not written. Relay them to the user with their
  reasons; do not rerun with different options to make them go away.
- Leave verification on. `--no-verify` is for when the thinking backend is down,
  and the report then says plainly that nothing was reviewed.
- Never edit notes in agent context. If the user wants a written note changed,
  that is a vault edit and goes through the skills that gate them.
- Never silently absorb style feedback. When the user corrects how a note reads,
  offer to turn it into a voice-note proposal; a rule they did not approve is a
  rule they cannot find later.
- "Make a note about X" with the content in chat is the user asking for this. A
  vague "can you deal with my notes" is not — ask what they want captured.

## Reference

`references/capture-note-format.md` — the note layout, the fidelity invariants,
the per-kind style, how splitting is decided, and what each deterministic check
catches. Read it before changing a prompt in `scripts/vault-capture.py`; the
draft prompt is a copy of that contract and the two have to agree.
