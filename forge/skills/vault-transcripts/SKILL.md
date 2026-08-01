---
name: vault-transcripts
description: Process raw voice-note and meeting transcripts in an Obsidian vault inbox - give each recording a real title, clean speech-to-text artifacts without rewriting the speaker's meaning, summarize, and keep the recording as its own linked source note. Use before vault-organizer processes the inbox; use split to separate recordings from notes processed earlier. For untranscribed recordings use transcription; outside a vault use transcript-cleanup.
---

# Vault Transcripts

A transcription app drops files like `20260724 131748-9788991C.md` into the vault
inbox: no frontmatter, no headings, no paragraphs, and a filename that says
nothing about the recording. This skill gives each one a name, a summary, and a
readable body, and keeps the original transcription beside it as a source note
the new note links to.

Run it **before** `vault-organizer` inbox processing. This skill decides what a
recording is called and how it reads; the organizer decides where it belongs and
replaces the frontmatter with its own classification.

Both the processed note and the recording's own note carry `date` — the day of
the recording, taken from the filename or the spoken date, not the day the
transcript was processed. Today's date is used only when neither is known.
`date` is human-owned, so the organizer carries it forward but can never supply
it; a note that leaves this skill without one never gets one.

Everything runs on the local LAN endpoints. Nothing is sent anywhere. That is a
requirement, not a detail — this inbox contains therapy sessions and private
conversations.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path, and the vault
   path from the user or the injected vault context.
2. Check the environment before a long run:

   ```bash
   python3 <skill-directory>/scripts/vault-transcripts.py doctor --vault <vault>
   ```

   This reports whether the vault and inbox are writable, the schema note parses
   and defines the vocabulary this skill writes, the optional voice note parses
   and which stages select owner/source/no voice, and both endpoints answer. The
   `chat` check also reports whether that endpoint is actually non-thinking:
   cleanup is one call per chunk, so a thinking endpoint there wastes hundreds of
   hidden tokens on every chunk.
3. Dry run first — dry run is always the default:

   ```bash
   python3 <skill-directory>/scripts/vault-transcripts.py process --vault <vault>
   ```

   Use `--limit 5` for a first trial on an inbox this skill has not seen. One
   stderr line per unit with an ETA; stdout stays a single JSON result.
4. Read `report.md` and tell the user:
   - proposed renames, old name to new name, with each recording's type and length
   - the summaries, so they can judge whether the model understood the recordings
   - notes held for review and why (each keeps its original name and body)
   - exact duplicates queued for the recoverable quarantine
   - pairs that share a recording id but differ in content — these need the
     user's decision and this skill will not touch them
   - the Verification section: how many notes the thinking model reviewed, what
     it flagged, what was redone, what it left for them
   - the run directory
5. Get explicit approval, then apply, resuming the same run so nothing is
   recomputed:

   ```bash
   python3 <skill-directory>/scripts/vault-transcripts.py process --vault <vault> --apply --run <run-directory>
   ```

   "Process my voice notes and apply it" is approval. A vague "sort out my inbox"
   is not.
6. Offer to run `vault-organizer inbox` next, which files the cleaned notes —
   both halves of each pair, the note and its recording.

## Re-exports of recordings the vault already has

A transcription app re-exports: the same recording arrives in the inbox again,
sometimes a byte or two different, long after the note made from it was filed.
Processing those a second time grows a near-twin of every note the vault already
had. `reconcile` finds them by comparing the recording's text — never the
filename, which drifts — against every recording already in the vault.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py reconcile --vault <vault>
```

A match moves to the recoverable quarantine at `.vault-transcripts/duplicates/`,
byte-intact and journaled. Anything that does not match stays exactly where it is
and is listed for the user: it may be a genuinely new recording, or it may be an
edited one worth looking at. Deterministic and offline, dry-run by default;
relay both lists and **get approval before `--apply`**.

## Reprocessing notes the pipeline already wrote

The recording never changes; what the pipeline made of it does. When the cleanup
register or the note layout changes, `reprocess` regenerates the summary,
reflection, and cleaned text of every filed transcript note from its recording.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py reprocess --vault <vault>
```

It selects filed notes with frontmatter and a `# Transcript` marker, reading the
recording inline or through the link `split` left. **The inbox is left alone** —
those notes are `process`'s input. **Therapy recordings are excluded** by their
filename label, and a note the classifier reads as therapy while its name says
otherwise is held rather than reprocessed: the exclusion is allowed to be
over-cautious and never the reverse.

Three things survive untouched, and none of them are the pipeline's to decide:
the note's **name**, which every wikilink in the vault points at; its
**frontmatter**, byte for byte, because that is the organizer's classification;
and the **recording section**, reattached exactly as found.

Read `reprocess-report.md` — it shows each summary before and after, which is the
thing worth judging — then **get approval and rerun with `--apply --run
<run-directory>`**. Every rewritten note is backed up first.

## Splitting notes processed before the pair existed

Processing writes two notes: the note made from the recording, and the recording
under its own name, which the note links to. Notes processed before that keep
the recording inline under `# Transcript`. `split` moves it out, deterministically
and with every endpoint down — no model reads either half, because what the
recording is about was decided when the note was first processed.

```bash
python3 <skill-directory>/scripts/vault-transcripts.py split --vault <vault>
```

It plans one split per note that has frontmatter and a `# Transcript` marker
still holding text; already-split notes, raw inbox exports, and notes with no
marker are left alone. The recording's note is written straight into the sources
tree, inheriting the note's own `domain` and `subdomain` — a domain the schema
does not define holds the note back rather than being guessed at. A note that is
itself `type: source` becomes `type: note`: the recording it now points at is the
source.

Read `split-report.md`, relay the counts and the type conversions, **get explicit
approval, then `--apply`**. Every rewritten note is copied to `backup/` under the
run directory first, and the recording is written before the note is rewritten,
so an interruption leaves the original intact rather than the recording lost.

## Settings

These four are the user's taste, not defaults to re-litigate each run. They are
fingerprinted into a run, so a resumed run refuses to change them.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--filename-pattern` | `date-type-topic` | `2026-07-24 - Therapy - Facing Family Dynamics.md`. Also `date-topic` and `date-time-topic`. |
| `--summary-style` | `callout` | A `> [!summary]` callout. Also `paragraph` and `heading`. |
| `--speaker-policy` | `names` | Real names where the transcript or the roster justifies them, roles otherwise. Also `roles` and `generic`. See the reference doc. |
| `--tiny-words` / `--tiny-summary` | `120` / `omit` | Under 120 words: light cleanup, no summary. |
| `--voice` / `--no-voice` | vault policy / off | Select a policy note or explicitly disable it. |
| `--lexicon` / `--no-lexicon` | vault note / off | Select a speakers-and-terms note or disable corrections and the roster. |
| `--profile` / `--no-profile` | vault register / off | Select a personal-context register or disable the layer. |

`--owner <name>` is only consulted by `--speaker-policy roles`, where it lets the
recorder's own name through while other names stay generic.

## Terms and speakers

`99 Meta/99.02 Schemas/0.02 Speakers and Terms.md` holds two tables the owner
edits. `## Terms` maps a correct spelling to the forms the transcriber produces;
`## Speakers` marks who turns up in recordings. Both are optional, and the
standalone `transcription` dictionary merges underneath the terms table.

Every person note under the directory's contacts folder joins the roster
automatically, at `sometimes`, with its role compiled from the note — so the
table only needs rows for what a note cannot say:

| `Appears` | Meaning |
| --- | --- |
| `always` | Offered on every recording. For the handful of recurring voices — a partner, a therapist, a standing one-to-one — where nobody says a name aloud. |
| `sometimes` | The default. Offered only when the recording mentions their name, an alias, or something close enough to be that name misheard. |
| `never` | Never proposed as a speaker. Their name still gets spelled correctly when they are talked *about*. |

Report the Lexicon section when it appears: what was corrected in code, who was
named from the roster, and the spellings the model fixed that are not recorded
yet. Offer to add those with `transcription`'s `dict add`, or as a row in the
note — either lands where the next run will find it.

## Rules

- Dry run is the default and `--apply` needs the user's approval.
- Nothing is ever deleted. Duplicates go to `.vault-transcripts/duplicates/`,
  every rewritten note is copied into the run's `backup/` first, and renames are
  journaled to `renames.jsonl`.
- A processed note is written in place first and renamed afterwards, in two
  journalled steps, because a rename can rewrite links inside other notes and
  would otherwise invalidate their planning hashes. This is the rename that
  matters: a recording arrives named for when it happened and leaves named for
  what it says, and a changed basename is exactly what Obsidian's basename
  resolution cannot paper over. When Obsidian 1.12.7+ is running and
  "Automatically update internal links" is on, the rename goes through its CLI so
  every inbound link follows the note — each linking note backed up first,
  verified after, restored if anything but a link line changed. Otherwise the old
  name is left behind in whatever pointed at it, exactly as before, and the run
  says so. `--link-rewrite {auto,off,require}` chooses; see `docs/obsidian-cli.md`.
- The original transcription is always preserved verbatim under `# Transcript`.
  If a check cannot prove that, the note is held rather than written.
- The register is spoken-to-written: filler, false starts, repeated phrases, and
  unambiguous circumlocutions come out, and the speaker's voice, meaning, and
  meaningful hedges stay. **Therapy is the exception** and keeps the older,
  stricter contract — nothing condensed, weighted hesitation preserved.
- Everything the pipeline generates is a callout, above the speaker's words: the
  summary open, reflections and connections collapsed. `references/loom-notes.css`
  styles them; without it notes still read correctly.
- Notes held for review keep their original name and body. Relay them to the
  user; do not resolve them by rerunning with different options.
- Never touch a pair that shares a recording id but differs in content. One is
  usually a truncated re-export, and sometimes the truncated one is the copy
  carrying the user's handwritten notes. Neither contains the other.
- Leave verification on. `--no-verify` is for when the thinking backend is down,
  and the report then says plainly that nothing was reviewed.
- Re-running is safe: a processed note has frontmatter, so it is skipped rather
  than cleaned twice.
- Apply owner-authored policy only to a single-speaker `memo` or `journal`.
  Never imitate the owner's prose in meetings, conversations, or therapy
  sessions. Apply source-derived policy to lectures and confidently identified
  podcasts, videos, webinars, and other external sources. Hold ambiguous
  classification for review rather than guessing owner authorship.
- The roster names voices; it never counts them. An `always` cue reading "the
  second voice in home recordings" says who a second voice is when there is one,
  and the model otherwise reads it as a promise that there is one — splitting a
  solo memo's sign-off off under that name and holding the note. An owner-authored
  memo or journal that comes back with two speakers is therefore classified once
  more with the roster withheld, and only a second voice found unprompted holds it.
- Clean external sources as structured full content: remove filler and
  redundancy, regroup related passages, and add headings while preserving every
  substantive claim, example, qualification, and disagreement.
- After an owner memo's or journal's cleaned text, add its reflection, then the
  raw transcript. A journal gets non-empty `## Observations`,
  `## Interpretations`, `## Open questions`, and `## Connections`; a memo gets
  `## Context`, `## Open questions`, `## Next steps`, and `## Connections`. No
  other recording type gets a reflection, and reflection is excluded from
  transcript-fidelity comparisons.
- Reflection sources from the vault first. Material from outside the vault is
  admissible only as text this run actually read — a link in the recording, or a
  citation already imported into a vault note — and such a connection begins
  `Outside vault:` and carries that source's URL. Nothing is fetched at
  reflection time, so a fact the model merely recalls cannot be checked: it is
  dropped from the reflection and reported as dropped.

## Reference

`references/transcript-note-format.md` — the note layout, the fidelity
invariants, the per-type cleanup style, the speaker rules, and what each
deterministic check catches. Read it before changing a prompt in
`scripts/vault-transcripts.py`; the cleanup prompt is a copy of that contract and
the two have to agree.

## Personal context

`99 Meta/99.02 Schemas/0.03 Personal Context.md` is a register of small cards
about the owner — people, history, reading, health, working preferences. Each row
names a card note; only that note's `## Context` bullets ever enter a prompt.
Cards are capped at 700 characters each, so they stay condensed by construction.

Two gates decide whether a card may be injected, and they are asymmetric:

| Column | Meaning |
| --- | --- |
| `Tier` | `always` (every prompt the card is allowed in), `when-relevant` (only when the material contains a literal trigger), `on-request` (never automatic). |
| `Scope` | Whose material the card may sit beside: `universal`, `owner-authored`, `source-derived`. Blank means `owner-authored`. |
| `Applies` | Blank means anywhere the scope allows. Naming routes means the card is refused everywhere the pipeline has not *positively established* one of them. |

This skill asserts a route only for `journal` and `therapy` recordings, via
`TYPE_TO_ROUTES`. A meeting, conversation, lecture, or memo asserts nothing, so
every route-gated card is refused there with no per-card configuration — which is
how clinical and life-history material stays out of a work meeting.

The layer reaches the **summary** and **journal reflection** calls only. It is
deliberately absent from cleanup and classification: cleanup runs behind
`check_chunk`, which rejects a chunk containing words the source did not, so a
card naming someone would invite the model to write that name and the gate would
then discard the chunk. Classification is the call that *decides* the recording
type and material role, so nothing is established yet.

A missing, malformed, or unresolvable register never fails a run — it warns and
the run proceeds without the layer. Report the profile warnings when `doctor` or
a run surfaces them, and offer to add a card or a trigger when the model clearly
lacked context it could have had.
