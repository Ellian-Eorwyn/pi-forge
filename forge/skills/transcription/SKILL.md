---
name: transcription
description: Transcribe an audio or video file to text with NVIDIA Parakeet TDT v3, then deterministically correct names, acronyms, and terms with a persistent user dictionary before cleanup. Use for .mp3, .wav, .m4a, .flac, .ogg, .opus and .mp4, .mov, .mkv, .webm, .avi and similar recordings — lectures, interviews, meetings, calls, voice notes, dictation — when the user wants speech turned into a transcript, optionally routed straight into transcript cleanup. Also use to manage the correction dictionary (add or list misheard names, acronyms, and jargon) or to re-apply it to an existing transcript.
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
   transcribe into a new run directory:

   ```bash
   python3 <skill-directory>/scripts/transcription.py transcribe <media> \
     --output forge-output/transcription/<source-stem> --type <type>
   ```

   The script preserves the source (records its SHA-256), normalizes
   audio with ffmpeg, chunks long recordings, writes `raw_transcript.txt`,
   `raw_segments.json`, `raw_transcript.srt`, applies the dictionary into
   `corrected_transcript.md` / `.txt` with a `corrections_log.csv`, and prints a
   JSON result including the `recommended_track` and a `next_step`.

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

Grow it with use. When the user confirms a misheard term, add it:

```bash
python3 <skill-directory>/scripts/transcription.py dict add \
  --correct "Kubernetes" --variant "cube are netties" --variant "kubernetis" \
  --category term [--scope global|project] [--case-sensitive] [--substring]
python3 <skill-directory>/scripts/transcription.py dict list --scope merged
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
  modified. Outputs go to a new run directory; never overwrite one.
- Keep recognized text separate from summary, analysis, and interpretation. The
  raw transcript is what the model heard; the corrected transcript only swaps in
  user-confirmed spellings, logged in `corrections_log.csv`.
- Mark uncertainty honestly: silent or unintelligible audio, low-confidence
  passages, and window boundaries in chunked long recordings. Do not invent text
  the model did not produce.
- The engine runs locally; no audio leaves the machine. Report the device
  (CPU/GPU), duration, chunk count, and correction count on completion.
