---
name: transcription
description: Transcribe audio or video, then correct and clean the result. Use for mp3, wav, m4a, mp4, and other recordings of lectures, interviews, meetings, and voice notes, producing a timestamped raw transcript plus a corrected readable version with speaker labels where they can be identified. For transcripts you already have, use transcript-cleanup; for voice notes already inside an Obsidian vault, use vault-transcripts.
---

# Transcription

Turn a recording into a transcript with a speech-to-text service, fix the
predictable recognition errors with a dictionary the user controls, then hand
the corrected transcript to `transcript-cleanup`. A deterministic script handles
upload, recognition, and dictionary correction; you supply the judgment —
recording type, dictionary curation, and the final cleanup. Preserve the source,
keep recognized text separate from interpretation, and make every correction
visible.

Recognition runs on the **llm-stack transcription service** (`http://llms:8014`
by default), the same host that serves the chat, embedding, and OCR models.
Nothing is installed locally: no virtual environment, no model download. The
default engine is Parakeet TDT v3, and `faster-whisper`, `canary-qwen` and
others are available by name. Only the correction dictionary is local, under
`${PI_FORGE_HOME:-~/.pi-forge}/transcription`, so updates do not remove it.

## Workflow

1. Resolve this skill directory from the loaded `SKILL.md` path. Check the
   service first — everything downstream depends on it being reachable:

   ```bash
   python3 <skill-directory>/scripts/transcription.py doctor
   ```

   `doctor` reports the configured service and engine, whether `/health`
   answers, which engines the host has and whether the requested one actually
   has a model, and remediation. If `ready` is false, read `remediation`: an
   unreachable host, a turned-off service, and an engine with no model are three
   different problems.

   `resident: null` in the report is **normal, not a fault**. The service
   unloads the model after 300 s idle so it does not hold VRAM the rest of the
   stack needs, which means the first transcription after a quiet period spends
   about 25 s loading weights before decoding starts. That load is reported in
   `warnings.md` when it happens.

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

   The script preserves the source (records its SHA-256), uploads it, writes
   `raw_transcript.txt`, `raw_segments.json`, `raw_transcript.srt` and the
   service's full response as `remote_response.json`, applies the dictionary
   into `corrected_transcript.md` / `.txt` with a `corrections_log.csv`, and
   prints a JSON result including the `recommended_track` and a `next_step`.
   Long audio is handled by the service as a background job and polled to
   completion; nothing needs splitting here. Repeating the same command returns
   the completed result rather than re-transcribing. Use `status <run-directory>
   --json` to inspect progress, `refresh <run-directory>` to adopt changed
   source media while archiving the previous revision, and `retry
   <run-directory> --all-failed` for explicit retry.

   Add `--engine faster-whisper` when the recording's **dates, figures or
   identifiers matter downstream**: Parakeet has written "July twenty first,
   nineteen sixty nine" where Whisper writes "July 21st, 1969". It is roughly
   20× slower, which on an hour of audio is minutes rather than seconds.

3. Read [references/transcription-contract.md](references/transcription-contract.md)
   for the run layout, dictionary schema, engine table, and type→track mapping.

4. **If the recording belongs in an Obsidian vault, export it instead** of going
   on to cleanup. `vault-transcripts` reads voice-app exports out of the vault
   inbox, and `export` writes the run in exactly that shape:

   ```bash
   python3 <skill-directory>/scripts/transcription.py export <run-directory> \
     --inbox "<vault>/00 Inbox" --recorded-at 2026-08-10T15:51:00 [--speaker "Ellie"]
   ```

   Pass `--recorded-at` whenever the recording time is known — without it the
   date falls back to the file's modification time, which is when this machine
   last touched the file rather than when the recording was made, and the result
   says so in `warnings`. There is no `--speaker` default because the service
   does no diarization on any engine: naming a speaker is the user's call, not a
   guess to make on their behalf. Then run `vault-transcripts`, and stop — that
   skill does its own cleanup, so `transcript-cleanup` is not also needed.

5. **Chain into cleanup.** Invoke the `transcript-cleanup` skill on
   `corrected_transcript.md` using the recommended track: faithful cleanup for
   lecture / interview / voice-note / other, structured memo for meeting / call.
   Follow the user's request if it conflicts with the type default.

6. Review the corrected transcript and `corrections_log.csv` against the source.
   Carry transcription warnings (model load, format conversion, silent audio)
   into the completion report. Where the model likely misheard a proper noun the
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

- **Audio leaves this machine.** It is uploaded to the configured transcription
  service and nowhere else — by default `llms`, on the user's own network, over
  plain HTTP with the network as the trust boundary. Say so before transcribing
  anything the user has treated as sensitive, and check what `doctor` reports as
  the service URL if there is any doubt about where it is going.
- Preserve the source recording. It is referenced by path and SHA-256, never
  modified. A compatible run directory is resumed; unrelated or legacy
  directories are refused.
- Keep recognized text separate from summary, analysis, and interpretation. The
  raw transcript is what the model heard; the corrected transcript only swaps in
  user-confirmed spellings, logged in `corrections_log.csv`.
- Mark uncertainty honestly: silent or unintelligible audio, low-confidence
  passages, and a synthetic timeline if the service reports one. Do not invent
  text the model did not produce.
- Report the engine, model, device and duration on completion. They are recorded
  in the manifest and in `remote_response.json`, which is what makes a stored
  transcript reproducible later.
