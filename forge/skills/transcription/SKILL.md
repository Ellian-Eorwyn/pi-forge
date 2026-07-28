---
name: transcription
description: Transcribe audio or video, then correct and clean the result. Use for mp3, wav, m4a, mp4, and other recordings of lectures, interviews, meetings, and voice notes, producing a timestamped raw transcript plus a corrected readable version with speaker labels where they can be identified. For transcripts you already have, use transcript-cleanup; for voice notes already inside an Obsidian vault, use vault-transcripts.
---

# Transcription

Turn a recording into a transcript with a local speech-to-text engine, fix the
predictable recognition errors with a dictionary the user controls, then hand
the corrected transcript to `transcript-cleanup`. A deterministic script handles
audio extraction, recognition, and dictionary correction; you supply the
judgment — recording type, dictionary curation, and the final cleanup. Preserve
the source, keep recognized text separate from interpretation, and make every
correction visible.

The recognition engine is **autoselected by platform**: parakeet-mlx on Apple
Silicon (fast, native MLX), NVIDIA NeMo elsewhere (CUDA on Linux). Both run the
Parakeet TDT v3 model locally — no audio leaves the machine. Dependencies and
the model install into a managed virtual environment under
`${PI_FORGE_HOME:-~/.pi-forge}/transcription`, outside the installed
repository checkout, so updates do not remove the local model cache.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check local
   capabilities first — the engine, model, and ffmpeg are heavy dependencies:

   ```bash
   python3 <skill-directory>/scripts/transcription.py doctor
   ```

   `doctor` reports the autoselected backend, whether the managed venv and model
   are installed, the exact managed model cache path, and remediation. If
   `ready` is false, run `setup` once to build the environment and download the
   ~2.5 GB model for this platform (or `--backend all` to prepare both engines
   for packaging):

   ```bash
   python3 <skill-directory>/scripts/transcription.py setup   # add --backend all to fetch both
   ```

   Also install `ffmpeg` if missing (`brew install ffmpeg` / `apt install
   ffmpeg`). On Linux with no CUDA GPU, NeMo runs on CPU — correct but slow; say
   so up front. See [references/packaging.md](references/packaging.md) for
   per-platform install and packaging details.

2. Confirm the **recording type** with the user (lecture, interview, meeting,
   call, voice-note, other). It routes the downstream cleanup track. Then
   transcribe into a run directory:

   ```bash
   python3 <skill-directory>/scripts/transcription.py transcribe <media> \
     --output forge-output/transcription/<source-stem> --type <type>
   ```

   Inside an Obsidian vault pass the vault workflow root instead —
   `99 Meta/99.06 Workflows/Transcriptions/<source-stem>` — at the absolute path
   the injected vault context names.

   The script preserves the source (records its SHA-256), normalizes
   audio with ffmpeg, chunks long recordings, writes `raw_transcript.txt`,
   `raw_segments.json`, `raw_transcript.srt`, applies the dictionary into
   `corrected_transcript.md` / `.txt` with a `corrections_log.csv`, and prints a
   JSON result including the `recommended_track` and a `next_step`.
   Repeating the same command resumes at the first uncommitted audio chunk and
   deterministically reassembles the final transcript from committed chunk
   results. Use `status <run-directory> --json` to inspect progress, `refresh
   <run-directory>` to adopt changed source media while archiving the previous
   revision, and `retry <run-directory> --item chunk-0001` or `--all-failed`
   for explicit retry.

3. Read [references/transcription-contract.md](references/transcription-contract.md)
   for the run layout, dictionary schema, and type→track mapping.

4. **Chain into cleanup.** Invoke the `transcript-cleanup` skill on
   `corrected_transcript.md` using the recommended track: faithful cleanup for
   lecture / interview / voice-note / other, structured memo for meeting / call.
   Follow the user's request if it conflicts with the type default.

5. Review the corrected transcript and `corrections_log.csv` against the source.
   Carry transcription warnings (CPU speed, chunk boundaries, silent audio) into
   the completion report. Where the model likely misheard a proper noun the
   dictionary did not catch, mark it and **offer to add it** (see below) — do not
   silently invent the correct spelling.

## User Correction Dictionary

The dictionary fixes recognition errors deterministically: each entry maps a
`correct` form to its known misheard `variants` (names, acronyms, jargon). It is
stored globally at `${PI_FORGE_HOME:-~/.pi-forge}/transcription/dictionary.json` and accumulates
across jobs; an optional per-project file
(`.forge/transcription-dictionary.json`, or `--project-dictionary <path>`)
overrides or extends it. Corrections are applied with word-boundary and
case rules and **every replacement is logged** — never silent.

Pass `--vault <path>` to merge an Obsidian vault's
`99 Meta/99.02 Schemas/0.02 Speakers and Terms.md` on top of both. That note is
the same glossary the vault skills use, so a term recorded in either place is
corrected everywhere; use it whenever the recording is destined for a vault.

Grow it with use. When the user confirms a misheard term, add it:

```bash
python3 <skill-directory>/scripts/transcription.py dict add \
  --correct "Kubernetes" --variant "cube are netties" --variant "kubernetis" \
  --category term [--scope global|project] [--case-sensitive] [--substring]
python3 <skill-directory>/scripts/transcription.py dict list --scope merged [--vault <path>]
```

Use `--category name|acronym|term`. Default matching is whole-word and
case-insensitive; pass `--substring` to match inside words and `--case-sensitive`
when case matters (e.g. an acronym that collides with a common word). To re-apply
the dictionary to a transcript you already produced or edited without
re-transcribing:

```bash
python3 <skill-directory>/scripts/transcription.py dict apply <transcript> --output <out>
```

Only add entries the user confirms. Do not guess a spelling for an unfamiliar
name; mark it uncertain and ask.

## Safety and Output Rules

- Preserve the source recording. It is referenced by path and SHA-256, never
  modified. A compatible run directory is resumed; unrelated or legacy
  directories are refused.
- Keep recognized text separate from summary, analysis, and interpretation. The
  raw transcript is what the model heard; the corrected transcript only swaps in
  user-confirmed spellings, logged in `corrections_log.csv`.
- Mark uncertainty honestly: silent or unintelligible audio, low-confidence
  passages, and window boundaries in chunked long recordings. Do not invent text
  the model did not produce.
- The engine runs locally; no audio leaves the machine. Report the device
  (CPU/GPU), duration, chunk count, and correction count on completion.
