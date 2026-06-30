# Transcription Contract

Deterministic media-to-transcript output with a user-controlled correction
dictionary. The engine is Parakeet TDT v3, autoselected by platform; ffmpeg
handles audio extraction and chunking.

## Backend Autoselection

| Platform | Backend | Model | Device |
|---|---|---|---|
| macOS arm64 (Apple Silicon) | parakeet-mlx | `mlx-community/parakeet-tdt-0.6b-v3` | MLX / Metal |
| Linux / other (NVIDIA) | NeMo | `nvidia/parakeet-tdt-0.6b-v3` | CUDA (CPU fallback) |

Selection is automatic; override with `--backend mlx|nemo` on `doctor`,
`transcribe`, and `setup`. Both backends produce the same output contract.

## Inputs

- **Audio**: `.wav .mp3 .m4a .aac .flac .ogg .oga .opus .wma .aiff .aif`
- **Video**: `.mp4 .mov .mkv .webm .avi .m4v .mpg .mpeg .wmv .flv .3gp`
  (the video stream is dropped; only audio is transcribed)

ffmpeg normalizes every input to 16 kHz mono PCM WAV before recognition.

## Run Layout

```
forge-output/transcription/<source-stem>/
  audio/
    normalized.wav            # 16 kHz mono working copy
    chunks/                   # only when the recording was split
  raw_transcript.txt          # what the model heard, segments blank-line separated
  raw_segments.json           # [{start, end, text}] with seconds offsets
  raw_transcript.srt          # subtitle view for review
  corrected_transcript.md     # title + corrected body (cleanup input)
  corrected_transcript.txt    # corrected plain text
  corrections_log.csv         # correct, variant, category, count, offsets
  transcription_manifest.csv  # one row: source, hash, duration, model, device, counts
  warnings.md                 # CPU speed, chunk boundaries, silence, etc.
```

Never overwrite an existing run directory; the script appends a numbered suffix.

## Long Audio

Recordings longer than `--chunk-threshold` seconds (default 600) are split into
fixed `--chunk-seconds` windows (default 480) with no overlap, transcribed
independently, and stitched with each segment's start time offset by its window.
Boundary wording can be imperfect; this is recorded in `warnings.md`.

## Recording Type → Cleanup Track

`transcribe --type` sets `recommended_track` in the result. Hand
`corrected_transcript.md` to `transcript-cleanup`:

| Type | Track | Cleanup output |
|---|---|---|
| lecture, interview, voice-note, other | faithful | `cleaned_transcript.md` |
| meeting, call | structured | `review_memo.md` |

Follow an explicit user request when it conflicts with the type default.

## Dictionary

### Storage and precedence

1. Global: `${PI_FORGE_HOME:-~/.pi-forge}/transcription/dictionary.json`.
2. Project: `.forge/transcription-dictionary.json` in the working directory, or
   `--project-dictionary <path>`.

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
dict list   [--scope global|project|merged] [--project-dictionary <path>]
dict add    --correct <form> --variant <misheard> [--variant ...]
            [--category name|acronym|term] [--case-sensitive] [--substring]
            [--scope global|project] [--project-dictionary <path>]
dict remove --correct <form> [--scope global|project] [--project-dictionary <path>]
dict apply  <transcript> --output <out> [--project-dictionary <path>] [--no-dictionary]
```

## Managed Environment

`setup` builds a self-contained install under `${PI_FORGE_HOME:-~/.pi-forge}/transcription`
(honors `$PI_FORGE_TRANSCRIPTION_HOME`, then `$PI_FORGE_HOME`). This directory
is durable local state outside the installed repository checkout, so
`pi-forge-update` does not remove the venvs, model cache, or dictionary:

```
~/.pi-forge/transcription/
  venv-mlx/         # parakeet-mlx environment (built on Apple Silicon)
  venv-nemo/        # NeMo environment (built on Linux/NVIDIA)
  models/           # HF_HOME; models/hub is the Hugging Face cache
  dictionary.json   # global correction dictionary
```

```
setup [--backend auto|all|mlx|nemo] [--skip-download]
```

Each backend gets its **own** venv — parakeet-mlx cannot install on Linux and
NeMo cannot install on macOS, so they must never share an environment. `setup`
creates the backend's venv, installs its pinned requirements, and downloads the
model into the shared `models/hub/` cache. `--backend all` attempts both and
reports per-backend status; one backend failing on a platform never disturbs the
other. `transcribe` re-executes itself under the selected backend's venv so its
imports resolve regardless of which `python3` invokes it. The script exports
`HF_HOME`, `HF_HUB_CACHE`, `HUGGINGFACE_HUB_CACHE`, and `TRANSFORMERS_CACHE` to
the managed cache before setup, doctor, and transcribe, and the MLX backend also
passes the cache path directly to `parakeet_mlx`. `doctor` reports each
backend's venv, install, model-cache, and device status.

## Dependencies

- **ffmpeg / ffprobe** — audio decode, probing, chunking (`brew install ffmpeg`
  / `apt install ffmpeg`). The only system-level dependency.
- **Backend packages** — pinned per backend and installed into the managed venv
  by `setup`, not into system Python:
  - `requirements/requirements-mlx.txt` → `parakeet-mlx` (Apple Silicon).
  - `requirements/requirements-nemo.txt` → `nemo_toolkit[asr]` + CUDA PyTorch,
    resolved on the Linux/NVIDIA host at install time.
- The model (~2.5 GB) is never committed; `setup` (or first `transcribe`)
  downloads it into the managed `models/hub/` cache.
