# Packaging and Install

How the `transcription` skill ships, and what a host needs before it can be used.

## There is nothing to install

The skill is entirely committed: `SKILL.md`, `scripts/transcription.py`,
`references/*.md`, `agents/openai.yaml`, `manifest.json`. The client is Python
standard library, and recognition happens on the llm-stack transcription service
rather than locally, so there is no virtual environment to build, no wheel to
resolve, and no model to download.

`.gitignore` keeps `__pycache__/` out of the repo. Nothing else is generated.

The only local state is the correction dictionary at
`${PI_FORGE_HOME:-~/.pi-forge}/transcription/dictionary.json`, which lives
outside the installed repository checkout so `pi-forge-update` does not remove
it. `PI_FORGE_TRANSCRIPTION_HOME` relocates that directory;
`PI_FORGE_HOME` relocates all pi-forge state.

## What a host does need

1. **The service, reachable.** `http://llms:8014` by default. Confirm with:

   ```bash
   python3 scripts/transcription.py doctor      # expect "ready": true
   ```

   Point it elsewhere with `FORGE_TRANSCRIPTION_URL`, a `--base-url` flag, or
   `connectedServices.transcription.baseUrl` in the agent settings.

2. **ffmpeg and ffprobe.** Needed for `.m4a` and every video container (which
   the service cannot decode itself), for stereo audio (the model takes mono),
   and for any file over the 512 MB upload cap. Between them that is most real
   recordings; only a *mono* `.wav`, `.mp3`, `.flac` or `.opus` under the cap is
   sent untouched.

   ```bash
   brew install ffmpeg     # macOS
   apt install ffmpeg      # Debian/Ubuntu
   ```

   `doctor` reports whether it is present and what it would be needed for.

The skill installs identically on macOS, Linux and Windows: there is no
platform-specific path any more, because there is no platform-specific engine.

## Air-gapped installs

The skill cannot work without reaching a transcription service — recognition is
not local. An air-gapped deployment needs the llm-stack transcription sidecar
running on a host it can reach, and `FORGE_TRANSCRIPTION_URL` pointed at it.
Nothing needs to be pre-staged on the pi-forge side.

## Migrating from the local-engine version

Earlier versions ran Parakeet locally through parakeet-mlx (Apple Silicon) or
NeMo (Linux/NVIDIA), in per-backend virtualenvs with a ~2.5 GB model cache. Those
are gone: the `setup` command, the `requirements/` directory, and the `--backend`
flag no longer exist.

The old install is not removed automatically — the dictionary shares that
directory, and deleting several gigabytes is the user's call. `doctor` reports it
as reclaimable with its size when it is still on disk:

```bash
rm -rf ~/.pi-forge/transcription/models ~/.pi-forge/transcription/venv-mlx ~/.pi-forge/transcription/venv-nemo
```

`dictionary.json` sits alongside those and must be kept.

Run directories made by the old version are refused rather than resumed: their
recorded options name a local backend and a chunking window that no longer
exist, so a compatible-run check would be comparing incomparable things.
`refresh` does not help — it adopts changed *media*, not changed options.
Transcribe into a new run directory instead; the old one keeps its artifacts.
