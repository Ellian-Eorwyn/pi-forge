# Packaging and Install

How the `transcription` skill ships and what each target installs at setup time.

## What is committed vs. installed

| Committed to the repo | Installed at `setup` time (not committed) |
|---|---|
| `SKILL.md`, `agents/openai.yaml` | The managed venvs under `${PI_FORGE_HOME:-~/.local/share/pi-vault}/transcription/venv-<backend>/` |
| `scripts/transcription.py` | The Parakeet model (~2.5 GB) under `${PI_FORGE_HOME:-~/.local/share/pi-vault}/transcription/models/hub/` |
| `references/*.md` | Backend packages (parakeet-mlx, or NeMo + CUDA PyTorch) |
| `requirements/requirements-{mlx,nemo}.txt` | |

The model and venvs are intentionally **not** vendored: they are large and
platform-specific. `setup` downloads exactly what the host needs. `.gitignore`
keeps `__pycache__/` and any stray venv/model dirs out of the repo.
The managed transcription directory is outside the installed repository
checkout, so normal pi-forge updates do not delete the downloaded model.

## Install per platform

Both targets first need ffmpeg (`brew install ffmpeg` / `apt install ffmpeg`).

**Apple Silicon (macOS arm64)** — uses parakeet-mlx:

```bash
python3 scripts/transcription.py setup            # autoselects mlx
python3 scripts/transcription.py doctor           # expect "ready": true
```

**Linux + NVIDIA** — uses NeMo (CUDA PyTorch resolves for the host):

```bash
python3 scripts/transcription.py setup            # autoselects nemo
python3 scripts/transcription.py doctor
```

**Prepare both** (mixed fleet / CI image): `setup --backend all`. Each backend
installs into its own venv; the one that cannot install on the current platform
is reported as failed without affecting the other.

A compatible interpreter (python3.11–3.13) is chosen for the venv automatically;
the newest CPython is avoided when it lacks ML wheels.

## Reproducible pins

`requirements-mlx.txt` is pinned exactly. `requirements-nemo.txt` pins
`nemo_toolkit[asr]` but lets its large transitive tree (including the CUDA torch
build) resolve on the target host. For byte-for-byte reproducibility on the
Linux image, run `pip freeze` inside `venv-nemo` after `setup` and commit the
result as a fully-pinned `requirements-nemo.lock.txt`.

## Offline / air-gapped installs

`setup` needs network access once to fetch packages and the model. To pre-stage,
run `setup` on a connected machine of the same platform, then copy
`~/.local/share/pi-vault/transcription/models/` to the target's matching path (set
`PI_FORGE_TRANSCRIPTION_HOME` if relocating the whole transcription directory,
or `PI_FORGE_HOME` if relocating all pi-forge state). Backend packages can be
mirrored with `pip download -r requirements/requirements-<backend>.txt`.
