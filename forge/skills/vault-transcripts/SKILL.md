---
name: vault-transcripts
description: Process raw voice-note and meeting transcripts sitting in an Obsidian vault inbox - give each recording a real title, clean the speech-to-text artifacts without rewriting the speaker's meaning, write a summary, and add advisory frontmatter. Use before vault-organizer processes the inbox, so the organizer classifies a named, cleaned note. For recordings not yet transcribed use transcription; for transcripts outside a vault use transcript-cleanup.
---

# Vault Transcripts

A transcription app drops files like `20260724 131748-9788991C.md` into the vault
inbox: no frontmatter, no headings, no paragraphs, and a filename that says
nothing about the recording. This skill gives each one a name, a summary, and a
readable body, and leaves the original transcription in place underneath.

Run it **before** `vault-organizer` inbox processing. This skill decides what a
recording is called and how it reads; the organizer decides where it belongs and
replaces the frontmatter with its own classification.

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
   and defines the vocabulary this skill writes, and both endpoints answer. The
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
6. Offer to run `vault-organizer inbox` next, which files the cleaned notes.

## Settings

These four are the user's taste, not defaults to re-litigate each run. They are
fingerprinted into a run, so a resumed run refuses to change them.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--filename-pattern` | `date-type-topic` | `2026-07-24 - Therapy - Facing Family Dynamics.md`. Also `date-topic` and `date-time-topic`. |
| `--summary-style` | `callout` | A `> [!summary]` callout. Also `paragraph` and `heading`. |
| `--speaker-policy` | `names` | Real names where the transcript justifies them, roles otherwise. Also `roles` and `generic`. See the reference doc. |
| `--tiny-words` / `--tiny-summary` | `120` / `omit` | Under 120 words: light cleanup, no summary. |

`--owner <name>` is only consulted by `--speaker-policy roles`, where it lets the
recorder's own name through while other names stay generic.

## Rules

- Dry run is the default and `--apply` needs the user's approval.
- Nothing is ever deleted. Duplicates go to `.vault-transcripts/duplicates/`,
  every rewritten note is copied into the run's `backup/` first, and renames are
  journaled to `renames.jsonl`.
- The original transcription is always preserved verbatim under `# Transcript`.
  If a check cannot prove that, the note is held rather than written.
- Notes held for review keep their original name and body. Relay them to the
  user; do not resolve them by rerunning with different options.
- Never touch a pair that shares a recording id but differs in content. One is
  usually a truncated re-export, and sometimes the truncated one is the copy
  carrying the user's handwritten notes. Neither contains the other.
- Leave verification on. `--no-verify` is for when the thinking backend is down,
  and the report then says plainly that nothing was reviewed.
- Re-running is safe: a processed note has frontmatter, so it is skipped rather
  than cleaned twice.

## Reference

`references/transcript-note-format.md` — the note layout, the fidelity
invariants, the per-type cleanup style, the speaker rules, and what each
deterministic check catches. Read it before changing a prompt in
`scripts/vault-transcripts.py`; the cleanup prompt is a copy of that contract and
the two have to agree.
