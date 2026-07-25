# Transcript Note Format

The contract for what `vault-transcripts` writes into a vault note, and how the
cleaned transcript is allowed to differ from the raw one. Part A restates the
fidelity invariants from `../../transcript-cleanup/references/faithful-cleanup.md`,
which remains their source. Parts A, B, and C are also embedded verbatim in
`CLEANUP_SYSTEM` in `scripts/vault-transcripts.py`; if you change one, change
both.

## Note layout

```markdown
---
type: meeting
status: raw
capture_type: meeting
---

> [!summary]
> One paragraph on what this recording was and what mattered in it.

<handwritten preamble, if the export had any, verbatim>

<cleaned transcript — headings ## or deeper>

# Transcript

<the original transcription, byte for byte>
```

Rules the script enforces, not suggestions:

- The generated section contains exactly one level-one heading, `# Transcript`.
  Everything the cleanup writes is `##` or deeper.
- Everything after `# Transcript` is the source body unchanged, including its
  handwritten preamble and any trailing text. The cleanup is a convenience; the
  transcription is the record.
- Frontmatter carries only `type`, `status: raw`, and `capture_type`, all
  validated against the vault's schema note. `vault-organizer` replaces this
  block when it files the note and reads these three as advisory hints, so they
  are accurate rather than complete. Domain, subdomain, project, and people are
  the organizer's judgment, not this skill's.
- A note under `--tiny-words` (120 by default) gets no summary. The descriptive
  filename already carries the gist of a two-sentence reminder.

One known consequence: `note_title` in `forge/lib/vault_schema.py` reads the
first level-one heading, so a processed note reports its title as "Transcript"
to other vault skills. The descriptive filename and the summary-first body are
what classification actually reads, so this costs nothing today — but it is the
reason to think twice before adding a second `#` heading.

## Part A — fidelity invariants (every recording type)

These outrank every style rule below.

```text
- Change as little wording as possible while making the transcript clean and readable.
- Preserve the speaker's intent, uncertainty, and nuance.
- Do not summarize. Output the full cleaned transcript.
```

- Keep hedges. "I think", "maybe", and "I don't know" are content, not noise.
- Never add facts, names, dates, conclusions, or certainty absent from the source.
- Never drop substance. Every point made must survive.
- Removing filler, false starts, stutters, and verbal scaffolding is the job. So
  is fixing transcription punctuation and casing.
- Leave garbled or uncertain passages visible rather than repairing them by
  guessing. Keep `[unclear]`-style markers.
- Timestamps are dropped from the cleaned text; they remain in the raw section.
- Tables only when the speaker is genuinely listing tabular data — never as
  decoration.

## Part B — style by recording type

| Type | Cleaned output |
| --- | --- |
| `memo` | Flowing first-person paragraphs. `##` headings only when the memo clearly moves between several distinct topics. No speaker labels. |
| `journal` | Chronological paragraphs, minimal intervention. Voice, emotion, and self-correction preserved; only obvious filler removed. |
| `conversation` | Dialogue as `**Name:** what they said` paragraphs, one per turn. |
| `therapy` | As `conversation`, at the highest fidelity of all. Hesitation and repetition that carries weight is kept. No clinical language, interpretation, or diagnosis that was not spoken. |
| `meeting` | `##` heading per topic. Closing `## Decisions` and `## Action Items` bullets **only** when the recording contains explicit decisions or assignments; `Unassigned` and `Not stated` rather than an inferred owner or deadline. |
| `lecture` | `##` and `###` headings following the material, the lecturer's own examples kept, audience questions as dialogue. |
| `other` | Treat as `memo` if one voice, `conversation` if several. |

A note under the tiny threshold gets punctuation, casing, and one short
paragraph. No headings, no lists, no restructuring.

## Part C — speakers

The transcriber re-labels every utterance and splits one voice into several
labels, so labels are an upper bound on how many people are talking, never an
answer. A 56-minute meeting in this corpus carries 640 speaker lines.

1. Consecutive blocks with the same label are merged into one turn
   **deterministically, in code**, before the model sees the chunk. This is not
   the model's job and it is not allowed to un-merge them.
2. A turn that continues the previous speaker's sentence mid-thought belongs to
   that speaker, even across a label change. This one is the model's judgment.
3. When the classifier reports one effective speaker, labels are dropped
   entirely and the recording is cleaned as a solo memo.
4. What a label is *called* is the `--speaker-policy` decision:

   | Policy | Behavior |
   | --- | --- |
   | `names` (default) | A real name when the transcript itself justifies it — someone was addressed by name, or introduced themselves — and the classifier was confident. Otherwise a role, otherwise `Speaker N` renumbered after merging. |
   | `roles` | Role words only (`Therapist`, `Interviewer`, `Instructor`), plus `--owner`'s own name. Never guesses at anyone else's name. |
   | `generic` | `Speaker N`, renumbered after merging. No relabelling. |

5. Labels the export already supplies as real names are kept under every policy.
   The source knew something the model would only be guessing at.
6. A name is never inferred from subject matter. Speaking about Gillian is not
   evidence that Gillian is speaking.

The raw section always keeps the original labels, whatever the policy did.

## What the checks enforce before a model ever reviews the result

Deterministic, exact, and free, so the thinking model's budget goes to judgment
instead of catching malformed output. Any failure holds the note back with its
original name and body, and puts it in the run's review queue.

| Check | Catches |
| --- | --- |
| Added words, on prose lines only | Invention. A word was either spoken or it was not; heading text is exempt because the editor authors structure. |
| Rare-word retention | A dropped passage. Long infrequent words are content; filler is short and common. |
| Cleaned/source length ratio | A cleanup that summarized instead of cleaning, or padded. |
| Sampled utterance containment | Passages that vanished, located by sliding window rather than by re-reading the whole file. |
| Exactly one `# ` heading, and it is `# Transcript` | A cleanup that wrote a document title. |
| Raw section byte-identical to the source body | Any drift in the thing that must not drift. |
| Preamble present in the generated section | Handwritten notes quietly eaten by the pipeline. |
| Surviving `*MM:SS*` lines | Timestamps left in the cleaned text. |
| Frontmatter keys and values against the schema note | Metadata the organizer would strip or reject. |
| Title charset, length, reserved names, and medium-words | Filenames that break Obsidian links or say nothing. |

After those pass, the thinking model reviews every note's type, title, summary,
and speaker naming against excerpts of the raw transcript, plus a sample of
utterances checked against the cleaned text. It can flag, and a flag is either
redone with reasoning or handed to a human — it never silently drops a result,
and an unreachable reviewer is reported as "not verified" rather than treated as
approval.
