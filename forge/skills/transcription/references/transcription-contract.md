# Transcription Contract

Deterministic media-to-transcript output with a user-controlled correction
dictionary. Recognition runs on the llm-stack transcription service; the default
engine is Parakeet TDT v3.

## The Service

| | |
|---|---|
| Default base URL | `http://llms:8014` |
| Setting | `connectedServices.transcription` in `~/.pi-forge/agent/settings.json` |
| Environment | `FORGE_TRANSCRIPTION_URL`, `FORGE_TRANSCRIPTION_ENGINE`, `FORGE_TRANSCRIPTION_API`, `FORGE_TRANSCRIPTION_MODEL`, `FORGE_TRANSCRIPTION_TOKEN` |
| Flags | `--base-url`, `--engine` on `doctor` and `transcribe` |
| Auth | None by default. A configured token is sent as `Authorization: Bearer <token>`. |

`FORGE_TRANSCRIPTION_URL=""` turns the integration off for one process, the way
`FORGE_SEARXNG_URL=""` disables search.

### Wire protocol (`api`)

`connectedServices.transcription.api` (or `FORGE_TRANSCRIPTION_API`) selects how the
client talks to the server:

- **`sidecar`** (default) — the pi-forge async API: `POST /transcribe` answers `202`
  with a job id, and the transcript arrives from `/jobs/<id>`. This is what
  `http://llms:8014` speaks.
- **`openai`** — a single synchronous `POST /v1/audio/transcriptions` (mlx-audio and
  other OpenAI-compatible ASR servers). `model` (`FORGE_TRANSCRIPTION_MODEL`) is the
  OpenAI `model` form field; leave it empty to accept the server's default. The client
  requests `verbose_json` so `segments` (with real timestamps) come back and map to the
  same result envelope the sidecar produces — a server that returns only `{"text": …}`
  still works, degrading to one `0:00` block. These servers do no diarization, so the
  note is single-speaker (the same as the sidecar unless `--speaker` is passed), and
  the doctor accepts their `status: "healthy"` health reply.

No shipped backend setup selects this route: `single`, `distributed`, and
`distributed-parallel` all reach the transcription service on the llms box, which
speaks the sidecar API. It is there for a server of your own — set
`connectedServices.transcription.api` (or `FORGE_TRANSCRIPTION_API=openai`) yourself.

### Residency, and why the timeout is 900 s

The service loads a model on first request and releases it after 300 s idle, and
`/engines` reports `yield_mode: "asr"` — the ASR weights yield the GPU to the
model router that serves `embed`, `ocr`, `rank` and `task`. So:

- `resident: null` is the normal reading, not a fault.
- The first call after a quiet period pays a model load of roughly 25 s. When
  that happens the run records it in `warnings.md`, so a slow run is legible
  rather than mysterious.
- Nothing in forge asks for a model swap. The service arbitrates its own
  residency; this side only has to be patient and not retry into the pressure.

Decode itself is fast — Parakeet measures around 210× realtime on the host's
RTX 3090, so an hour of audio is about twenty seconds of work.

## Engines

`doctor` reports what the host actually has. Registered is not the same as
usable: an engine can be missing its runtime (`engine_unavailable`) or have no
model configured (`model_load_failed`), and `doctor` refuses the second case
before an upload is spent on it.

| Engine | Notes |
|---|---|
| `parakeet-v3` | The default. Fast (~210× realtime), ~1.9 GB VRAM. |
| `faster-whisper` | large-v3. ~20× slower, ~5.5 GB VRAM, better text normalization. |
| `canary-qwen` | Downloads ~2.5 GB on the host on first use. |
| `hf-asr` | Runtime present, no model configured — refused by `doctor`. |
| `router` | Requires `ASR` in the host's router members; not configured. |

**Text normalization differs by engine.** `faster-whisper` writes
"July 21st, 1969"; Parakeet has written "July twenty first, nineteen sixty nine"
on the same audio. When dates, figures or identifiers are parsed out of the
transcript downstream, pass `--engine faster-whisper` and accept it being slower.

The engine is pinned in configuration rather than left to the service's own
default, which changed from `faster-whisper` to `parakeet-v3` on 2026-08-09. An
engine that changes underneath a pipeline changes how its numbers are spelled.

## Inputs

- **Audio**: `.wav .mp3 .m4a .aac .flac .ogg .oga .opus .wma .aiff .aif`
- **Video**: `.mp4 .mov .mkv .webm .avi .m4v .mpg .mpeg .wmv .flv .3gp`
  (the video stream is dropped; only audio is transcribed)

### What is uploaded

A file is sent unchanged only when **all three** of these hold. Otherwise it is
converted to mono 16 kHz Opus with ffmpeg first, into `audio/normalized.opus`,
and the conversion is recorded in `warnings.md`.

1. **The format is one the service decodes** — `.wav .flac .mp3 .opus .ogg .oga
   .aiff .aif .aac`. The host decodes through torchaudio without ffmpeg
   bindings, so an MP4/ISO container (`.m4a`, `.mp4`, `.mov`, `.m4v`) fails
   server-side with `decode_failed` — and `.m4a` is what a phone voice memo is.
2. **The audio is mono.** The model takes a single channel and answers
   `decode_failed` for stereo at any sample rate in any of those formats
   (`Input shape mismatch ... torch.Size([1, 2, N])`). Sample rate itself does
   not matter; a 44.1 kHz mono file is accepted as it is. Channel count is read
   with `ffprobe`, and when that cannot be run the file is sent optimistically
   rather than converted blindly.
3. **It is under the 512 MB upload cap.**

All three were verified against the live service on 2026-08-10, not taken from
its documentation, which claims anything ffmpeg can decode will work.

In practice this means **ffmpeg is needed for most real recordings**: every
`.m4a` and every video container, and anything stereo, which most non-voice-memo
audio is. A mono `.wav`, `.mp3`, `.flac` or `.opus` under the cap needs nothing.

Opus at 24 kb/s mono is roughly 3 KB/s, so the cap works out to about 47 hours of
converted audio. A source still over the cap after conversion is refused rather
than truncated.


## Run Layout

The run directory is `forge-output/transcription/<source-stem>/`, or
`99 Meta/99.06 Workflows/Transcriptions/<source-stem>/` inside an Obsidian vault.
Its layout is the same either way:

```
<run-directory>/
  run_state.json               # item status, attempts, and next action
  run_events.jsonl             # fsynced transition journal
  audio/
    normalized.opus           # only when the source needed converting
  chunk_results/              # the committed recognition result
  remote_response.json        # the service's full envelope, verbatim
  raw_transcript.txt          # what the model heard, segments blank-line separated
  raw_segments.json           # [{start, end, text}] with seconds offsets
  raw_transcript.srt          # subtitle view for review
  corrected_transcript.md     # title + corrected body (cleanup input)
  corrected_transcript.txt    # corrected plain text
  corrections_log.csv         # correct, variant, category, count, offsets
  transcription_manifest.csv  # one row: source, hash, duration, engine, model, device, counts
  warnings.md                 # model load, format conversion, silence, etc.
```

`remote_response.json` is kept whole rather than reduced to its `text`. Its
`engine`, `model`, `capabilities`, `duration` and `timings` cost nothing to store
and are what makes a transcript reproducible — and what explains a missing field
instead of leaving a reader guessing whether it was a bug.

A compatible directory returns its completed result rather than re-transcribing.
An unrelated or legacy directory is refused — including a run made by the old
local-engine version of this skill, whose recorded options no longer match.
`refresh` archives an earlier media revision under `revisions/` before resetting.

## Long Audio

Above the service's threshold (900 s today) `/transcribe` answers `202` with a
job id instead of a transcript. The client polls that job to completion and
returns the same shape either way, so nothing here has to handle two. Audio is
never split locally: the service handles length itself, and splitting would
multiply the uploads and cut utterances at window boundaries.

## Word Timestamps

Requested by default, and it matters. NeMo engines return **no timeline at all**
unless word timestamps are asked for: `segments` degrade to the decoder's own
60-second windows, every subtitle cue spans a whole window, and the word list
comes back empty. The capability being advertised as available does not mean it
is supplied unasked.

`--no-word-timestamps` skips the work when only the text is wanted. Anything
time-aligned — the SRT, seeking, chunking for downstream summarisation — needs
them.

## Export to vault-transcripts

```
export <run-directory> [--output <path> | --inbox <directory>]
       [--speaker <label>] [--recorded-at <stamp>] [--force]
       [--vault <path>] [--project-dictionary <path>] [--no-dictionary]
```

`vault-transcripts` consumes what a voice app drops into the vault inbox, and
`export` writes a completed run in that shape: an italic `*MM:SS*` marker (or
`*H:MM:SS*` past an hour — that parser tells them apart by counting colons) and
the text, optionally under a bold speaker label. The dictionary is applied per
segment on the way out.

- **No speaker label by default.** `capabilities.diarization` is false on every
  engine here, so a "Speaker 1" would be an assertion nobody measured. A solo
  recording can be labelled with `--speaker`.
- **The filename carries the date.** `--inbox` writes
  `YYYYMMDD HHMMSS-<id>.md`, which is the stamp `vault-transcripts` reads a
  recording date from; the id is the first 8 characters of the source hash, so
  re-exporting the same recording overwrites rather than making a second note.
  Without `--inbox` the same name is returned as `suggested_filename`.
- **`--recorded-at` matters.** Without it the timestamp falls back to the source
  file's modification time, which is when this machine last touched the file —
  a copied or re-exported recording carries the copy's date. The result reports
  `date_source` and warns whenever it is not `recorded-at`.

An existing export is never silently replaced; pass `--force`.

After exporting, run `vault-transcripts` rather than `transcript-cleanup`: that
skill does its own cleaning, and running both would clean the same words twice.

## Recording Type → Cleanup Track

`transcribe --type` sets `recommended_track` in the result. Hand
`corrected_transcript.md` to `transcript-cleanup`:

| Type | Track | Cleanup output |
|---|---|---|
| lecture, interview, voice-note, other | faithful | `cleaned_transcript.md` |
| meeting, call | structured | `review_memo.md` |

Follow an explicit user request when it conflicts with the type default.

## Failures

The service reports typed errors; branch on the type, never on the message.

| type | status | retried? |
|---|---|---|
| `bad_request` | 400 | No — the request was wrong. |
| `too_large` | 413 | No — over the upload cap. |
| `unsupported_capability` | 422 | No — this engine cannot do it. |
| `engine_unavailable` | 503 | No — a runtime is missing; the `hint` says how to install it. |
| `decode_failed` | 500 | No — the same bytes fail the same way. |
| `model_load_failed` | 503 | Yes — usually CUDA pressure. |
| `timeout` / `upstream_error` | 504 / 502 | Yes. |

A failed run leaves the error in `run_state.json` and exits non-zero. `retry
<run-directory> --all-failed` resets it.

## Dictionary

### Storage and precedence

1. Global: `${PI_FORGE_HOME:-~/.pi-forge}/transcription/dictionary.json`.
2. Project: `.forge/transcription-dictionary.json` in the working directory, or
   `--project-dictionary <path>`.
3. Vault: `--vault <path>` merges that vault's lexicon note on top.

Merge is by the lowercased `correct` key: project entries override or extend
global ones. `--no-dictionary` skips correction entirely.

### Entry schema

```json
{
  "correct": "Kubernetes",
  "variants": ["cube are netties", "kubernetis", "k8s"],
  "category": "term",
  "case_sensitive": false,
  "whole_word": true
}
```

- `category`: `name | acronym | term` (organizational only).
- `case_sensitive`: match variants case-sensitively when true.
- `whole_word`: when true (default), variants match only on word boundaries;
  `dict add --substring` sets it false. Whitespace inside a multi-word variant
  matches flexibly (one or more spaces).

### Application

Variants are compiled longest-first so a short variant never shadows a longer
phrase, then substituted into the transcript. Every replacement is counted and
recorded in `corrections_log.csv` (with up to 50 character offsets per variant) —
corrections are always reviewable, never silent. The replacement is the `correct`
form verbatim; original casing of the matched variant is not preserved.

### Management commands

```
dict list   [--scope global|project|vault|merged] [--project-dictionary <path>] [--vault <path>]
dict add    --correct <form> --variant <misheard> [--variant ...]
            [--category name|acronym|term] [--case-sensitive] [--substring]
            [--scope global|project] [--project-dictionary <path>]
dict remove --correct <form> [--scope global|project] [--project-dictionary <path>]
dict apply  <transcript> --output <out> [--project-dictionary <path>] [--no-dictionary]
```

## Dependencies

- **ffmpeg / ffprobe** — required for `.m4a` and video sources, for stereo audio,
  and for anything over the upload cap, which between them covers most real
  recordings (`brew install ffmpeg` / `apt install ffmpeg`). `ffprobe` reads the
  channel count that decides whether conversion is needed, and fills in a
  duration the service did not report.
- **Nothing else.** The client is Python standard library, the model lives on the
  service, and `${PI_FORGE_HOME:-~/.pi-forge}/transcription` now holds only the
  correction dictionary.

Earlier versions of this skill built a per-platform virtualenv and cached a
~2.5 GB model under that directory. Those are no longer used; `doctor` reports
them as reclaimable if they are still on disk, and never deletes them.
