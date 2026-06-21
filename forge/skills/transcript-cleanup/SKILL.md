---
name: transcript-cleanup
description: Clean, structure, summarize, and transform raw transcripts from meetings, interviews, calls, lectures, speaker recordings, voice notes, dictated notes, or speech-to-text exports. Use for pasted transcript text and .txt, .md, or .docx transcript files when producing faithful full-transcript cleanup, meeting notes, coherent narratives, action extraction, research memos, personal notes, or publication-ready summaries.
---

# Transcript Cleanup

Preserve meaning and speaker intent while making transcription damage,
interpretation, and uncertainty visible.

## Workflow

0. If the source is an audio or video recording rather than text, first run it
   through the `transcription` skill (`$transcription`) to produce a corrected
   transcript, then clean that output here.
1. Identify the input, output track, requested mode, desired fidelity, and
   whether the user wants the original embedded.
2. For a file, preserve the source and record its absolute path and SHA-256.
   Read `.txt` and `.md` directly. For `.docx`, run:

   ```bash
   node <skill-directory>/scripts/extract-transcript.mjs <input.docx> <new-output.md>
   ```

   Resolve `<skill-directory>` from the loaded `SKILL.md` path. The helper also
   supports `.txt` and `.md`. Always use a new explicit output path. Carry its
   DOCX warning into the final output or completion report.
3. For pasted text, identify the source as `Pasted session input`. Do not invent
   a path, filename, date, or checksum.
4. Create a dedicated output directory under
   `forge-output/transcript-cleanup/<source-stem>/`. If it exists, use the next
   numbered suffix. Do not overwrite existing output.
5. Follow the selected output track below. Create separate artifacts only when
   requested.
6. Review the result against the source before finishing. Report omissions,
   ambiguity, extraction problems, and requested sections with no evidence.

## Choose an Output Track

Use **faithful cleanup** for personal notes, lectures, interviews, dictated
thoughts, single-speaker recordings, and requests to clean the full transcript.
Default to the **Filler Cleanup** preset and write `cleaned_transcript.md`. Do
not summarize. Generate a descriptive title, but keep tags and YAML frontmatter
disabled unless requested. Read
[references/faithful-cleanup.md](references/faithful-cleanup.md) and apply its
prompt contract, toggles, and presets.

Use **structured memo** for meetings, calls centered on decisions or actions,
research synthesis, and explicit requests for summaries, decisions, questions,
or follow-ups. Write `review_memo.md` using the structure below.

If the source type and request conflict, follow the requested output. For
example, clean a meeting verbatim when the user requests a full cleaned
transcript, and structure a lecture when the user requests a research memo.

## Cleanup Rules

- Remove obvious duplicate fragments, false starts, filler, and speech-to-text
  artifacts only when the selected options permit it and meaning is unchanged.
- Preserve substantive wording, timestamps, speaker intent, disagreements, and
  qualifications.
- Do not assign names to unidentified speakers. Use `Speaker 1`, `Speaker 2`,
  or `[Uncertain speaker]`, and explain uncertain attribution.
- Preserve uncertain or unintelligible language with markers such as
  `[unclear]`, `[inaudible]`, or `[uncertain: possible wording]`.
- Quote only words supported by the source. Retain timestamps or speaker labels
  as locators when available.
- Record an action owner or deadline only when stated. Otherwise use
  `Unassigned` or `Not stated`.
- Label interpretations and inferred follow-ups; do not present them as
  transcript facts.

## Structured Modes

- **Meeting notes:** emphasize summary, decisions, actions, owners, deadlines,
  open questions, and follow-ups.
- **Action extraction:** emphasize explicit commitments and separately label
  inferred next steps.
- **Research memo:** organize claims, evidence, themes, definitions, and gaps.
- **Publication-ready summary:** allow stronger rewriting for clarity while
  preserving claims and listing material editorial choices.

## Structured Memo

Use these headings in `review_memo.md`. Keep every heading; write `None stated`
or `Not applicable` rather than inventing content.

```markdown
# Transcript Review Memo

## Source and Provenance
## Extraction Warnings
## Cleaned Transcript
## Summary
## Action Items
## Decisions
## Open Questions
## Key Quotes
## Topics
## Uncertainties and Review Notes
```

Under `Source and Provenance`, record the mode, cleanup fidelity, source type,
source path and checksum when available, and whether the original was embedded.
Keep the cleaned transcript separate from generated sections.

Do not copy or embed the unchanged original by default. When requested, append
it under `## Original Transcript (Unchanged)` and state whether it came from a
file or pasted session input.
