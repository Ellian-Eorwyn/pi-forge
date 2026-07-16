#!/usr/bin/env python3

"""Transcribe audio/video with NVIDIA Parakeet TDT v3, then apply a persistent
user correction dictionary. Outputs are designed to flow into the
transcript-cleanup skill. Originals are never modified.

The recognition engine is autoselected by platform:
  * Apple Silicon (macOS arm64) -> parakeet-mlx (fast, native MLX)
  * everything else, incl. Linux + NVIDIA -> NeMo (CUDA-accelerated)

Dependencies and models install into a durable managed environment under
${PI_FORGE_HOME:-~/.pi-forge}/transcription via `setup`, so updates
to the repository do not remove the local Parakeet model cache."""

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import run_state


# Per-backend engine configuration. Models download to the managed cache on
# first use or via `setup`; they are never committed to the repository.
BACKENDS = {
    "mlx": {
        "model": "mlx-community/parakeet-tdt-0.6b-v3",
        "import": "parakeet_mlx",
        "requirements": "requirements-mlx.txt",
        "label": "parakeet-mlx (Apple Silicon)",
    },
    "nemo": {
        "model": "nvidia/parakeet-tdt-0.6b-v3",
        "import": "nemo",
        "requirements": "requirements-nemo.txt",
        "label": "NVIDIA NeMo",
    },
}
MODEL_APPROX_DOWNLOAD = "~2.5 GB"

# Interpreters preferred for the managed venv, newest-compatible first. The
# very newest CPython often lacks ML wheels (e.g. mlx), so we avoid defaulting
# to whatever runs this script.
PREFERRED_INTERPRETERS = ["python3.13", "python3.12", "python3.11"]

AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aiff", ".aif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv", ".flv", ".3gp"}
MEDIA_EXTENSIONS = AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

TYPE_TRACKS = {
    "lecture": "faithful",
    "interview": "faithful",
    "voice-note": "faithful",
    "other": "faithful",
    "meeting": "structured",
    "call": "structured",
}
TRANSCRIPT_TYPES = sorted(TYPE_TRACKS)

DEFAULT_CHUNK_THRESHOLD = 600
DEFAULT_CHUNK_SECONDS = 480
TARGET_SAMPLE_RATE = 16000

MANIFEST_COLUMNS = [
    "source_path",
    "source_sha256",
    "source_format",
    "duration_seconds",
    "backend",
    "model",
    "device",
    "chunk_count",
    "segment_count",
    "correction_count",
    "raw_transcript",
    "corrected_transcript",
    "recommended_track",
    "warning_count",
]

CORRECTIONS_LOG_COLUMNS = ["correct", "variant", "category", "count", "offsets"]


def fail(message, exit_code=1):
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(exit_code)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tool_version(command, args):
    if shutil.which(command) is None:
        return None
    try:
        result = subprocess.run([command, *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    combined = f"{result.stdout}\n{result.stderr}".strip()
    return combined.splitlines()[0].strip() if combined else "available"


def run(command, **kwargs):
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False, **kwargs)
    except OSError as error:
        fail(f"could not run {command[0]}: {error}")
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        fail(f"{command[0]} failed: {detail}")
    return result


# ---------------------------------------------------------------------------
# Managed environment and backend selection
# ---------------------------------------------------------------------------

def pi_forge_home():
    return Path(os.environ.get("PI_FORGE_HOME", Path.home() / ".pi-forge"))


def transcription_home():
    override = os.environ.get("PI_FORGE_TRANSCRIPTION_HOME")
    if override:
        return Path(override).expanduser()
    return pi_forge_home() / "transcription"


def venv_dir(backend):
    # One venv per backend: parakeet-mlx cannot install on Linux and NeMo cannot
    # install on macOS, so they must never share an environment.
    return transcription_home() / f"venv-{backend}"


def venv_python(backend):
    bin_dir = "Scripts" if os.name == "nt" else "bin"
    exe = "python.exe" if os.name == "nt" else "python"
    candidate = venv_dir(backend) / bin_dir / exe
    return candidate if candidate.exists() else None


def models_dir():
    return transcription_home() / "models"


def hub_cache_dir():
    return models_dir() / "hub"


def requirements_path(backend):
    return Path(__file__).resolve().parent.parent / "requirements" / BACKENDS[backend]["requirements"]


def detect_platform_backend():
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin" and machine in {"arm64", "aarch64"}:
        return "mlx"
    return "nemo"


def select_backend(preference="auto"):
    if preference and preference != "auto":
        if preference not in BACKENDS:
            fail(f"unknown backend '{preference}'; expected one of {', '.join(BACKENDS)}")
        return preference
    return detect_platform_backend()


def find_interpreter():
    for name in PREFERRED_INTERPRETERS:
        found = shutil.which(name)
        if found:
            return found
    return sys.executable


def apply_model_cache_env(env=None):
    """Force model downloads into the durable managed cache."""
    target = os.environ if env is None else env
    target["HF_HOME"] = str(models_dir())
    target["HF_HUB_CACHE"] = str(hub_cache_dir())
    target["HUGGINGFACE_HUB_CACHE"] = str(hub_cache_dir())
    target["TRANSFORMERS_CACHE"] = str(hub_cache_dir())
    target.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return target


def model_cache_env():
    env = os.environ.copy()
    return apply_model_cache_env(env)


def ensure_model_cache_env():
    return apply_model_cache_env()


def model_cached(backend):
    if cached_model_path(backend):
        return True
    return False


def cached_model_path(backend):
    marker = "models--" + BACKENDS[backend]["model"].replace("/", "--")
    snapshots = hub_cache_dir() / marker / "snapshots"
    if not snapshots.is_dir():
        return None
    candidates = sorted(
        (path for path in snapshots.iterdir() if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for candidate in candidates:
        if backend == "mlx":
            if (candidate / "config.json").is_file() and (candidate / "model.safetensors").is_file():
                return candidate
        else:
            return candidate
    return None


def model_load_path(backend):
    return cached_model_path(backend) or BACKENDS[backend]["model"]


def backend_installed(backend):
    """True if the backend import resolves in that backend's managed venv (or,
    lacking a venv, in the current interpreter)."""
    module = BACKENDS[backend]["import"]
    python = venv_python(backend)
    if python:
        result = subprocess.run(
            [str(python), "-c", f"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('{module}') else 1)"],
            capture_output=True,
        )
        return result.returncode == 0
    return importlib.util.find_spec(module) is not None


def reexec_in_venv(backend):
    """Re-run this process under the backend's managed venv interpreter so its
    imports resolve, with model downloads pointed at the managed cache."""
    python = venv_python(backend)
    if python and Path(sys.executable).resolve() != python.resolve():
        os.execve(str(python), [str(python), os.path.abspath(__file__), *sys.argv[1:]], model_cache_env())


# ---------------------------------------------------------------------------
# Dictionary storage and application
# ---------------------------------------------------------------------------

def global_dictionary_path():
    return transcription_home() / "dictionary.json"


def project_dictionary_path(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (Path.cwd() / ".forge" / "transcription-dictionary.json").resolve()


def normalize_entry(entry):
    if not isinstance(entry, dict):
        return None
    correct = str(entry.get("correct", "")).strip()
    if not correct:
        return None
    variants = [str(value).strip() for value in entry.get("variants", []) if str(value).strip()]
    return {
        "correct": correct,
        "variants": sorted(set(variants), key=lambda value: (-len(value), value.lower())),
        "category": entry.get("category") or "term",
        "case_sensitive": bool(entry.get("case_sensitive", False)),
        "whole_word": bool(entry.get("whole_word", True)),
    }


def load_dictionary(path):
    path = Path(path)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        fail(f"could not read dictionary {path}: {error}")
    entries = data.get("entries", data) if isinstance(data, dict) else data
    if not isinstance(entries, list):
        fail(f"dictionary {path} must contain a list of entries")
    normalized = [normalize_entry(entry) for entry in entries]
    return [entry for entry in normalized if entry]


def save_dictionary(path, entries):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated": utc_now(),
        "entries": sorted(entries, key=lambda entry: entry["correct"].lower()),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def merge_dictionaries(global_entries, project_entries):
    merged = {entry["correct"].lower(): entry for entry in global_entries}
    for entry in project_entries:
        merged[entry["correct"].lower()] = entry
    return sorted(merged.values(), key=lambda entry: entry["correct"].lower())


def compile_corrections(entries):
    compiled = []
    for entry in entries:
        for variant in entry["variants"]:
            flags = 0 if entry["case_sensitive"] else re.IGNORECASE
            pattern = re.escape(variant)
            pattern = re.sub(r"\\\s+|\\ ", r"\\s+", pattern)
            if entry["whole_word"]:
                pattern = rf"(?<!\w){pattern}(?!\w)"
            compiled.append((variant, entry["correct"], entry["category"], re.compile(pattern, flags)))
    compiled.sort(key=lambda item: -len(item[0]))
    return compiled


def apply_corrections(text, entries):
    log = {}
    for variant, correct, category, regex in compile_corrections(entries):
        offsets = []

        def record(match):
            offsets.append(match.start())
            return correct

        text, count = regex.subn(record, text)
        if count:
            key = (correct, variant)
            existing = log.get(key, {"category": category, "count": 0, "offsets": []})
            existing["count"] += count
            existing["offsets"].extend(offsets)
            log[key] = existing
    rows = [
        {
            "correct": correct,
            "variant": variant,
            "category": value["category"],
            "count": value["count"],
            "offsets": ";".join(str(offset) for offset in value["offsets"][:50]),
        }
        for (correct, variant), value in log.items()
    ]
    rows.sort(key=lambda row: (-row["count"], row["correct"].lower()))
    return text, rows


def resolve_dictionary(args):
    if getattr(args, "no_dictionary", False):
        return [], {"global": None, "project": None}
    global_path = global_dictionary_path()
    project_path = project_dictionary_path(getattr(args, "project_dictionary", None))
    global_entries = load_dictionary(global_path)
    project_entries = load_dictionary(project_path)
    merged = merge_dictionaries(global_entries, project_entries)
    return merged, {
        "global": str(global_path) if global_entries else None,
        "project": str(project_path) if project_entries else None,
    }


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def torch_device():
    if importlib.util.find_spec("torch") is None:
        return None
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
            return "mps"
        return "cpu"
    except Exception:  # pragma: no cover - defensive
        return None


def backend_device(backend):
    if backend == "mlx":
        return "mps (Apple Silicon, MLX)"
    return torch_device() or "cpu"


def command_doctor(args):
    selected = select_backend(args.backend)
    ffmpeg = tool_version("ffmpeg", ["-version"])
    ffprobe = tool_version("ffprobe", ["-version"])
    selected_venv = venv_python(selected)
    backends = {
        name: {
            "venv": str(venv_dir(name)),
            "venv_ready": venv_python(name) is not None,
            "installed": backend_installed(name),
            "model": BACKENDS[name]["model"],
            "model_cached": model_cached(name),
        }
        for name in BACKENDS
    }
    report = {
        "platform": f"{platform.system()} {platform.machine()}",
        "selected_backend": selected,
        "backend_label": BACKENDS[selected]["label"],
        "model": BACKENDS[selected]["model"],
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "candidate_interpreter": find_interpreter(),
        "backends": backends,
        "model_cache": str(models_dir()),
        "model_cached": model_cached(selected),
        "global_dictionary": str(global_dictionary_path()),
        "project_dictionary": str(project_dictionary_path()),
        "remediation": [],
    }
    if not ffmpeg or not ffprobe:
        report["remediation"].append("Install ffmpeg (includes ffprobe): brew install ffmpeg  /  apt install ffmpeg")
    if not selected_venv or not backends[selected]["installed"]:
        report["remediation"].append(
            f"Install the {selected} engine and download the model: "
            f"python3 {Path(__file__).name} setup --backend {selected}"
        )
    if selected == "nemo" and selected_venv and backends["nemo"]["installed"] and torch_device() == "cpu":
        report["remediation"].append("No CUDA GPU detected; NeMo will run on CPU (correct but slow).")
    if not model_cached(selected):
        report["remediation"].append(
            f"Model {BACKENDS[selected]['model']} ({MODEL_APPROX_DOWNLOAD}) is not cached; "
            f"run setup or it downloads on first transcribe."
        )
    report["ready"] = bool(
        ffmpeg and ffprobe and selected_venv and backends[selected]["installed"] and model_cached(selected)
    )
    print(json.dumps(report, indent=2))
    if not report["ready"]:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# setup: build the managed venv, install a backend, download the model
# ---------------------------------------------------------------------------

def ensure_venv(backend):
    python = venv_python(backend)
    if python:
        return python
    interpreter = find_interpreter()
    venv_dir(backend).parent.mkdir(parents=True, exist_ok=True)
    print(f"Creating {backend} venv at {venv_dir(backend)} using {interpreter}...", file=sys.stderr)
    run([interpreter, "-m", "venv", str(venv_dir(backend))])
    python = venv_python(backend)
    if not python:
        fail(f"failed to create the {backend} venv")
    run([str(python), "-m", "pip", "install", "-U", "pip", "wheel"], env=model_cache_env())
    return python


def install_backend(python, backend):
    requirements = requirements_path(backend)
    if not requirements.is_file():
        fail(f"missing requirements file: {requirements}")
    print(f"Installing {BACKENDS[backend]['label']} from {requirements.name}...", file=sys.stderr)
    run([str(python), "-m", "pip", "install", "-r", str(requirements)], env=model_cache_env())


def download_model(python, backend):
    model = BACKENDS[backend]["model"]
    print(f"Downloading model {model} ({MODEL_APPROX_DOWNLOAD}) into {models_dir()}...", file=sys.stderr)
    models_dir().mkdir(parents=True, exist_ok=True)
    if backend == "mlx":
        code = (
            "from pathlib import Path; "
            "from parakeet_mlx import from_pretrained; "
            f"from_pretrained('{model}', cache_dir=Path({str(hub_cache_dir())!r})); print('ok')"
        )
    else:
        code = (
            "import nemo.collections.asr as asr; "
            f"asr.models.ASRModel.from_pretrained(model_name='{model}'); print('ok')"
        )
    run([str(python), "-c", code], env=model_cache_env())


def command_setup(args):
    if tool_version("ffmpeg", ["-version"]) is None:
        print("Warning: ffmpeg not found; install it before transcribing (brew/apt install ffmpeg).", file=sys.stderr)
    backends = list(BACKENDS) if args.backend == "all" else [select_backend(args.backend)]
    results = []
    for backend in backends:
        try:
            python = ensure_venv(backend)
            install_backend(python, backend)
            if not args.skip_download:
                download_model(python, backend)
            results.append(
                {
                    "backend": backend,
                    "venv": str(venv_dir(backend)),
                    "model": BACKENDS[backend]["model"],
                    "cached": model_cached(backend),
                    "status": "ok",
                }
            )
        except SystemExit as error:
            # With per-backend venvs, one backend failing (e.g. mlx on Linux,
            # nemo on macOS) never disturbs the others.
            if args.backend != "all":
                raise
            results.append({"backend": backend, "status": "failed", "error": str(error)})
            print(f"Warning: {backend} setup failed; continuing. ({error})", file=sys.stderr)
    result = {
        "model_cache": str(models_dir()),
        "backends": results,
        "ready": all(item.get("cached") for item in results) or args.skip_download,
    }
    print(json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

def probe_duration(path):
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def extract_audio(source, destination):
    run(
        [
            "ffmpeg", "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE), "-c:a", "pcm_s16le",
            str(destination),
        ]
    )


def split_audio(wav_path, chunk_dir, window_seconds):
    chunk_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(chunk_dir / "chunk_%04d.wav")
    run(
        [
            "ffmpeg", "-y", "-i", str(wav_path),
            "-f", "segment", "-segment_time", str(window_seconds),
            "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE), "-c:a", "pcm_s16le",
            pattern,
        ]
    )
    chunks = sorted(chunk_dir.glob("chunk_*.wav"))
    return [(chunk, index * window_seconds) for index, chunk in enumerate(chunks)]


def load_model(backend):
    if backend == "mlx":
        try:
            from parakeet_mlx import from_pretrained
        except Exception as error:
            fail(f"parakeet-mlx is unavailable. Run setup --backend mlx. (import error: {error})")
        model_path = model_load_path("mlx")
        if isinstance(model_path, Path):
            print(f"Loading {BACKENDS['mlx']['model']} from {model_path}...", file=sys.stderr)
            return from_pretrained(str(model_path), cache_dir=hub_cache_dir())
        print(f"Loading {BACKENDS['mlx']['model']}...", file=sys.stderr)
        return from_pretrained(model_path, cache_dir=hub_cache_dir())
    try:
        import nemo.collections.asr as nemo_asr
    except Exception as error:
        fail(f"NeMo is unavailable. Run setup --backend nemo. (import error: {error})")
    print(f"Loading {BACKENDS['nemo']['model']}...", file=sys.stderr)
    return nemo_asr.models.ASRModel.from_pretrained(model_name=BACKENDS["nemo"]["model"])


def segments_from_mlx(result):
    segments = []
    for sentence in getattr(result, "sentences", None) or []:
        text = (getattr(sentence, "text", "") or "").strip()
        if not text:
            continue
        segments.append(
            {"start": float(getattr(sentence, "start", 0.0)), "end": float(getattr(sentence, "end", 0.0)), "text": text}
        )
    if not segments:
        text = (getattr(result, "text", "") or "").strip()
        if text:
            segments.append({"start": 0.0, "end": 0.0, "text": text})
    return segments


def segments_from_nemo(hypothesis):
    if isinstance(hypothesis, list):
        hypothesis = hypothesis[0]
    timestamp = getattr(hypothesis, "timestamp", None)
    segments = []
    if isinstance(timestamp, dict) and timestamp.get("segment"):
        for item in timestamp["segment"]:
            text = (item.get("segment") or item.get("text") or "").strip()
            if not text:
                continue
            start = float(item.get("start", 0.0))
            end = float(item.get("end", item.get("start", 0.0)))
            segments.append({"start": start, "end": end, "text": text})
    if not segments:
        text = (getattr(hypothesis, "text", "") or "").strip()
        if text:
            segments.append({"start": 0.0, "end": 0.0, "text": text})
    return segments


def transcribe_chunks(backend, model, chunks):
    segments = []
    if backend == "nemo":
        paths = [str(path) for path, _ in chunks]
        try:
            outputs = model.transcribe(paths, timestamps=True)
        except TypeError:
            outputs = model.transcribe(paths)
        per_file = [segments_from_nemo(item) for item in outputs]
    else:
        per_file = [segments_from_mlx(model.transcribe(str(path))) for path, _ in chunks]
    for (_, offset), file_segments in zip(chunks, per_file):
        for segment in file_segments:
            segments.append(
                {"start": segment["start"] + offset, "end": segment["end"] + offset, "text": segment["text"]}
            )
    return segments


def format_timestamp(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    whole = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"


def write_srt(segments, path):
    lines = []
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{format_timestamp(segment['start'])} --> {format_timestamp(segment['end'])}")
        lines.append(segment["text"])
        lines.append("")
    run_state.atomic_write_text(path, "\n".join(lines))


def atomic_write_csv(path, fieldnames, rows):
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    run_state.atomic_write_text(path, buffer.getvalue())


def mark_chunk_started(state, item_id, attempt):
    item = next(value for value in state["items"] if value["id"] == item_id)
    item.update({"status": "in_progress", "attempts": attempt, "error": None})
    state.update({"status": "running", "phase": "transcribing", "nextAction": item_id})
    return state


def mark_chunk_complete(state, item_id, result_path):
    item = next(value for value in state["items"] if value["id"] == item_id)
    item.update({"status": "completed", "transient": False, "error": None, "resultPath": result_path})
    remaining = next((value["id"] for value in state["items"] if run_state.retryable_item(value)), None)
    state["nextAction"] = remaining or "assemble"
    return state


def mark_chunk_failed(state, item_id, error, transient):
    item = next(value for value in state["items"] if value["id"] == item_id)
    item.update({"status": "failed", "transient": transient, "error": error})
    state["nextAction"] = item_id if transient and item["attempts"] < run_state.DEFAULT_MAX_ATTEMPTS else "retry"
    return state


def complete_transcription(state, result):
    state.update({"status": "complete", "phase": "complete", "nextAction": None, "completion": result})
    return state


def assemble_chunk_segments(state):
    segments = []
    for item in state["items"]:
        if item.get("status") != "completed" or not item.get("resultPath"):
            raise ValueError(f"chunk is not committed: {item['id']}")
        result = json.loads(Path(item["resultPath"]).read_text(encoding="utf-8"))
        if result.get("sha256") != item["sha256"]:
            raise ValueError(f"chunk result hash mismatch: {item['id']}")
        segments.extend(result.get("segments", []))
    return segments


def command_transcribe(args):
    backend = select_backend(args.backend)
    reexec_in_venv(backend)  # run under the backend's venv so its imports resolve

    source = Path(args.media).expanduser().resolve()
    if not source.is_file():
        fail(f"media file does not exist: {source}")
    extension = source.suffix.lower()
    if extension not in MEDIA_EXTENSIONS:
        fail(f"unsupported media format {extension or '(none)'}; expected audio or video")
    if tool_version("ffmpeg", ["-version"]) is None or tool_version("ffprobe", ["-version"]) is None:
        fail("ffmpeg and ffprobe are required. Install with: brew install ffmpeg  /  apt install ffmpeg")

    source_hash = sha256(source)
    run_directory = Path(args.output).expanduser().resolve()
    configuration = {
        "workflow": "transcription",
        "command": "transcribe",
        "input": {"path": str(source), "sha256": source_hash},
        "options": {
            "backend": backend,
            "type": args.type,
            "language": args.language,
            "chunkThreshold": args.chunk_threshold,
            "chunkSeconds": args.chunk_seconds,
            "projectDictionary": str(Path(args.project_dictionary).expanduser().resolve()) if args.project_dictionary else None,
            "noDictionary": args.no_dictionary,
        },
    }
    if run_directory.exists():
        try:
            state = run_state.load_run_state(run_directory, "transcription")
            run_state.assert_compatible_run(state, configuration)
        except (OSError, ValueError) as error:
            fail(str(error))
        if state.get("status") == "complete" and state.get("completion"):
            print(json.dumps(state["completion"], indent=2))
            return
    else:
        run_directory.mkdir(parents=True)
        state = run_state.create_run_state("transcription", "transcribe", configuration["input"], configuration["options"], phase="normalizing", next_action="transcribe")
        run_state.initialize_run_state(run_directory, state)
    audio_dir = run_directory / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    warnings = list(state.get("warnings", []))

    wav_path = audio_dir / "normalized.wav"
    if not wav_path.is_file():
        print("Normalizing audio with ffmpeg...", file=sys.stderr)
        temporary_wav = audio_dir / ".normalized.wav.tmp"
        extract_audio(source, temporary_wav)
        os.replace(temporary_wav, wav_path)
    duration = probe_duration(wav_path)

    if state.get("items"):
        chunks = [(Path(item["path"]), item["offset"]) for item in state["items"]]
    elif duration and duration > args.chunk_threshold:
        print(f"Audio is {duration:.0f}s; splitting into {args.chunk_seconds}s windows...", file=sys.stderr)
        chunks = split_audio(wav_path, audio_dir / "chunks", args.chunk_seconds)
        warning = (
            f"Audio exceeded {args.chunk_threshold}s and was split into {len(chunks)} non-overlapping "
            f"{args.chunk_seconds}s windows; review wording at window boundaries."
        )
        if warning not in warnings:
            warnings.append(warning)
    else:
        chunks = [(wav_path, 0.0)]

    if not state.get("items"):
        def initialize_chunks(draft):
            draft["items"] = [
                {"id": f"chunk-{index:04d}", "path": str(path), "sha256": sha256(path), "offset": offset, "status": "pending", "attempts": 0, "transient": False, "error": None}
                for index, (path, offset) in enumerate(chunks, 1)
            ]
            draft["warnings"] = warnings
            draft["phase"] = "transcribing"
            draft["nextAction"] = "transcribe"
            return draft
        state = run_state.update_run_state(run_directory, initialize_chunks, {"type": "chunks_initialized", "chunks": len(chunks)})

    device = backend_device(backend)
    if backend == "nemo" and device == "cpu":
        warnings.append("Running NeMo on CPU (no CUDA GPU); transcription is correct but slow.")

    pending = [item for item in state["items"] if run_state.retryable_item(item)]
    model = load_model(backend) if pending else None
    if pending:
        print(f"Transcribing {len(pending)} remaining segment file(s) with {backend}...", file=sys.stderr)
    results_dir = run_directory / "chunk_results"
    results_dir.mkdir(exist_ok=True)
    with run_state.run_lock(run_directory):
        for snapshot in pending:
            item = snapshot
            while run_state.retryable_item(item):
                attempt = item.get("attempts", 0) + 1
                state = run_state.update_run_state(run_directory, lambda draft, item_id=item["id"], attempt=attempt: mark_chunk_started(draft, item_id, attempt), {"type": "item_started", "itemId": item["id"], "attempt": attempt})
                item = next(value for value in state["items"] if value["id"] == item["id"])
                try:
                    chunk_segments = transcribe_chunks(backend, model, [(Path(item["path"]), item["offset"])])
                    result_path = results_dir / f"{item['id']}.json"
                    run_state.atomic_write_json(result_path, {"chunkId": item["id"], "sha256": item["sha256"], "offset": item["offset"], "segments": chunk_segments})
                    state = run_state.update_run_state(run_directory, lambda draft, item_id=item["id"], path=str(result_path): mark_chunk_complete(draft, item_id, path), {"type": "item_completed", "itemId": item["id"], "attempt": attempt})
                    break
                except Exception as error:
                    transient = run_state.is_transient_failure(error)
                    state = run_state.update_run_state(run_directory, lambda draft, item_id=item["id"], error=str(error), transient=transient: mark_chunk_failed(draft, item_id, error, transient), {"type": "item_failed", "itemId": item["id"], "attempt": attempt, "transient": transient, "error": str(error)})
                    item = next(value for value in state["items"] if value["id"] == item["id"])
                    if not transient or attempt >= run_state.DEFAULT_MAX_ATTEMPTS:
                        break
    state = run_state.load_run_state(run_directory, "transcription")
    failed = [item for item in state["items"] if item["status"] == "failed"]
    if failed:
        fail(f"transcription has failed chunks; run retry: {', '.join(item['id'] for item in failed)}")
    try:
        segments = assemble_chunk_segments(state)
    except ValueError as error:
        fail(str(error))
    if not segments:
        warnings.append("The model produced no transcript text; the audio may be silent or unintelligible.")

    raw_text = "\n\n".join(segment["text"] for segment in segments).strip() + ("\n" if segments else "")
    run_state.atomic_write_text(run_directory / "raw_transcript.txt", raw_text)
    run_state.atomic_write_json(run_directory / "raw_segments.json", segments)
    write_srt(segments, run_directory / "raw_transcript.srt")

    entries, dictionary_sources = resolve_dictionary(args)
    corrected_text, correction_log = apply_corrections(raw_text, entries)
    correction_count = sum(row["count"] for row in correction_log)

    atomic_write_csv(run_directory / "corrections_log.csv", CORRECTIONS_LOG_COLUMNS, correction_log)

    track = TYPE_TRACKS.get(args.type, "faithful")
    markdown = f"# {source.stem}\n\n" + "\n\n".join(segment["text"] for segment in segments)
    markdown, _ = apply_corrections(markdown, entries)
    corrected_md_path = run_directory / "corrected_transcript.md"
    run_state.atomic_write_text(corrected_md_path, markdown.strip() + "\n")
    run_state.atomic_write_text(run_directory / "corrected_transcript.txt", corrected_text.strip() + "\n")

    manifest_row = {
        "source_path": str(source),
        "source_sha256": source_hash,
        "source_format": extension.lstrip("."),
        "duration_seconds": f"{duration:.2f}" if duration else "",
        "backend": backend,
        "model": BACKENDS[backend]["model"],
        "device": device,
        "chunk_count": len(chunks),
        "segment_count": len(segments),
        "correction_count": correction_count,
        "raw_transcript": "raw_transcript.txt",
        "corrected_transcript": "corrected_transcript.md",
        "recommended_track": track,
        "warning_count": len(warnings),
    }
    atomic_write_csv(run_directory / "transcription_manifest.csv", MANIFEST_COLUMNS, [manifest_row])

    warning_lines = "\n".join(f"- {warning}" for warning in warnings) or "- None."
    run_state.atomic_write_text(
        run_directory / "warnings.md", f"# Transcription Warnings\n\nGenerated {utc_now()}\n\n{warning_lines}\n"
    )

    result = {
        "source": str(source),
        "source_sha256": source_hash,
        "run_directory": str(run_directory),
        "duration_seconds": duration,
        "backend": backend,
        "model": BACKENDS[backend]["model"],
        "device": device,
        "chunk_count": len(chunks),
        "segment_count": len(segments),
        "correction_count": correction_count,
        "dictionary_sources": dictionary_sources,
        "type": args.type,
        "recommended_track": track,
        "outputs": {
            "raw_transcript": str(run_directory / "raw_transcript.txt"),
            "raw_segments": str(run_directory / "raw_segments.json"),
            "raw_srt": str(run_directory / "raw_transcript.srt"),
            "corrected_transcript_md": str(corrected_md_path),
            "corrected_transcript_txt": str(run_directory / "corrected_transcript.txt"),
            "corrections_log": str(run_directory / "corrections_log.csv"),
            "manifest": str(run_directory / "transcription_manifest.csv"),
            "warnings": str(run_directory / "warnings.md"),
        },
        "next_step": f"Run the transcript-cleanup skill on corrected_transcript.md using the '{track}' track.",
        "warnings": warnings,
    }
    run_state.update_run_state(
        run_directory,
        lambda draft: complete_transcription(draft, result),
        {"type": "run_completed", "chunks": len(chunks), "segments": len(segments)},
    )
    print(json.dumps(result, indent=2))


def command_status(args):
    run_directory = Path(args.run).expanduser().resolve()
    try:
        state = run_state.load_run_state(run_directory, "transcription")
    except (OSError, ValueError) as error:
        fail(str(error))
    source = Path(state["input"]["path"])
    current_hash = sha256(source) if source.is_file() else None
    counts = {status: sum(item.get("status") == status for item in state["items"]) for status in ("pending", "in_progress", "completed", "failed")}
    report = {
        "run": str(run_directory),
        "status": state["status"],
        "phase": state["phase"],
        "nextAction": state.get("nextAction"),
        "items": counts,
        "inputDrift": {"changed": current_hash is not None and current_hash != state["input"]["sha256"], "removed": current_hash is None},
    }
    print(json.dumps(report, indent=2))


def command_retry(args):
    run_directory = Path(args.run).expanduser().resolve()
    try:
        state = run_state.load_run_state(run_directory, "transcription")
    except (OSError, ValueError) as error:
        fail(str(error))
    targets = {args.item} if args.item else {item["id"] for item in state["items"] if item.get("status") == "failed"}
    if not targets:
        fail("no failed chunks selected")
    known = {item["id"] for item in state["items"]}
    unknown = targets - known
    if unknown:
        fail(f"unknown chunk id(s): {', '.join(sorted(unknown))}")

    def retry_items(draft):
        for item in draft["items"]:
            if item["id"] in targets:
                item.update({"status": "pending", "attempts": 0, "transient": False, "error": None})
        draft.update({"status": "running", "phase": "transcribing", "nextAction": sorted(targets)[0]})
        draft.pop("completion", None)
        return draft

    run_state.update_run_state(run_directory, retry_items, {"type": "items_retried", "itemIds": sorted(targets)})
    print(json.dumps({"run": str(run_directory), "retried": sorted(targets)}, indent=2))


def command_refresh(args):
    run_directory = Path(args.run).expanduser().resolve()
    try:
        state = run_state.load_run_state(run_directory, "transcription")
    except (OSError, ValueError) as error:
        fail(str(error))
    source = Path(state["input"]["path"])
    if not source.is_file():
        fail(f"source media is missing: {source}")
    current_hash = sha256(source)
    plan = state.get("refreshPlan")
    if not plan and current_hash == state["input"]["sha256"]:
        print(json.dumps({"run": str(run_directory), "refreshed": False}, indent=2))
        return
    if not plan:
        revision = len(state.get("history", [])) + 1
        revision_directory = run_directory / "revisions" / f"revision-{revision:04d}"
        names = [
            "audio", "chunk_results", "raw_transcript.txt", "raw_segments.json", "raw_transcript.srt",
            "corrected_transcript.md", "corrected_transcript.txt", "corrections_log.csv",
            "transcription_manifest.csv", "warnings.md",
        ]
        plan = {
            "revision": revision,
            "newSha256": current_hash,
            "revisionDirectory": str(revision_directory),
            "operations": [
                {"source": str(run_directory / name), "destination": str(revision_directory / name)}
                for name in names
                if (run_directory / name).exists()
            ],
        }
        state = run_state.update_run_state(
            run_directory,
            lambda draft: {**draft, "status": "running", "phase": "refreshing", "nextAction": "refresh", "refreshPlan": plan},
            {"type": "refresh_planned", "revision": revision, "newSha256": current_hash},
        )
    Path(plan["revisionDirectory"]).mkdir(parents=True, exist_ok=True)
    for operation in plan["operations"]:
        source_path = Path(operation["source"])
        destination = Path(operation["destination"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            continue
        if source_path.exists():
            os.replace(source_path, destination)

    def finish_refresh(draft):
        old_input = draft["input"]
        draft.setdefault("history", []).append({
            "input": old_input,
            "items": draft["items"],
            "revisionDirectory": plan["revisionDirectory"],
        })
        draft["input"] = {**old_input, "sha256": plan["newSha256"]}
        draft["currentRevision"] = plan["revision"] + 1
        draft["items"] = []
        draft["optionsFingerprint"] = run_state.configuration_fingerprint({
            "workflow": draft["workflow"], "command": draft["command"], "input": draft["input"], "options": draft["options"]
        })
        draft.update({"status": "running", "phase": "normalizing", "nextAction": "transcribe"})
        draft.pop("completion", None)
        draft.pop("refreshPlan", None)
        return draft

    updated = run_state.update_run_state(run_directory, finish_refresh, {"type": "input_refreshed", "revision": plan["revision"], "newSha256": plan["newSha256"]})
    print(json.dumps({"run": str(run_directory), "refreshed": True, "revision": plan["revision"], "nextAction": updated["nextAction"]}, indent=2))


# ---------------------------------------------------------------------------
# dict subcommands
# ---------------------------------------------------------------------------

def scope_path(scope, project_override=None):
    return global_dictionary_path() if scope == "global" else project_dictionary_path(project_override)


def command_dict_list(args):
    global_entries = load_dictionary(global_dictionary_path())
    project_entries = load_dictionary(project_dictionary_path(args.project_dictionary))
    if args.scope == "global":
        entries = global_entries
    elif args.scope == "project":
        entries = project_entries
    else:
        entries = merge_dictionaries(global_entries, project_entries)
    print(
        json.dumps(
            {
                "scope": args.scope,
                "global_dictionary": str(global_dictionary_path()),
                "project_dictionary": str(project_dictionary_path(args.project_dictionary)),
                "count": len(entries),
                "entries": entries,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def command_dict_add(args):
    if not args.variant:
        fail("provide at least one --variant for the correct form")
    path = scope_path(args.scope, args.project_dictionary)
    entries = load_dictionary(path)
    index = {entry["correct"].lower(): entry for entry in entries}
    key = args.correct.strip().lower()
    existing = index.get(key)
    merged_variants = set(existing["variants"]) if existing else set()
    merged_variants.update(variant.strip() for variant in args.variant if variant.strip())
    entry = normalize_entry(
        {
            "correct": args.correct,
            "variants": sorted(merged_variants),
            "category": args.category,
            "case_sensitive": args.case_sensitive,
            "whole_word": not args.substring,
        }
    )
    index[key] = entry
    save_dictionary(path, list(index.values()))
    print(json.dumps({"scope": args.scope, "path": str(path), "entry": entry}, indent=2, ensure_ascii=False))


def command_dict_remove(args):
    path = scope_path(args.scope, args.project_dictionary)
    entries = load_dictionary(path)
    key = args.correct.strip().lower()
    remaining = [entry for entry in entries if entry["correct"].lower() != key]
    if len(remaining) == len(entries):
        fail(f"no dictionary entry with correct form '{args.correct}' in {args.scope} scope")
    save_dictionary(path, remaining)
    print(json.dumps({"scope": args.scope, "path": str(path), "removed": args.correct}, indent=2))


def command_dict_apply(args):
    transcript = Path(args.transcript).expanduser().resolve()
    if not transcript.is_file():
        fail(f"transcript does not exist: {transcript}")
    output = Path(args.output).expanduser().resolve()
    if output == transcript:
        fail("output must differ from the input transcript")
    text = transcript.read_text(encoding="utf-8")
    entries, sources = resolve_dictionary(args)
    corrected, log = apply_corrections(text, entries)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(corrected, encoding="utf-8")
    print(
        json.dumps(
            {
                "input": str(transcript),
                "output": str(output),
                "dictionary_sources": sources,
                "correction_count": sum(row["count"] for row in log),
                "corrections": log,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------

def add_dictionary_arguments(subparser, include_no_dictionary=True):
    subparser.add_argument("--project-dictionary", help="Path to a project dictionary override.")
    if include_no_dictionary:
        subparser.add_argument("--no-dictionary", action="store_true", help="Skip dictionary corrections.")


def parser():
    root = argparse.ArgumentParser(
        description="Transcribe audio/video with Parakeet TDT v3 (autoselected backend) and a user dictionary."
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report backend, ffmpeg, venv, model, and dictionary status.")
    doctor.add_argument("--backend", choices=["auto", *BACKENDS], default="auto")
    doctor.set_defaults(handler=command_doctor)

    setup = subparsers.add_parser("setup", help="Build the managed venv, install a backend, and download the model.")
    setup.add_argument("--backend", choices=["auto", "all", *BACKENDS], default="auto")
    setup.add_argument("--skip-download", action="store_true", help="Install dependencies but do not fetch the model.")
    setup.set_defaults(handler=command_setup)

    transcribe = subparsers.add_parser("transcribe", help="Transcribe an audio or video file.")
    transcribe.add_argument("media")
    transcribe.add_argument("--output", required=True, help="Run directory to create.")
    transcribe.add_argument("--type", choices=TRANSCRIPT_TYPES, default="other", help="Recording type for routing.")
    transcribe.add_argument("--backend", choices=["auto", *BACKENDS], default="auto")
    transcribe.add_argument("--language", help="Optional language hint (Parakeet v3 is multilingual).")
    transcribe.add_argument("--chunk-threshold", type=int, default=DEFAULT_CHUNK_THRESHOLD,
                            help="Duration (s) above which audio is chunked.")
    transcribe.add_argument("--chunk-seconds", type=int, default=DEFAULT_CHUNK_SECONDS,
                            help="Window length (s) when chunking.")
    add_dictionary_arguments(transcribe)
    transcribe.set_defaults(handler=command_transcribe)

    status = subparsers.add_parser("status", help="Report resumable transcription state and input drift.")
    status.add_argument("run")
    status.add_argument("--json", action="store_true", help="Accepted for the shared run-state interface.")
    status.set_defaults(handler=command_status)

    retry = subparsers.add_parser("retry", help="Explicitly retry failed transcription chunks.")
    retry.add_argument("run")
    retry_group = retry.add_mutually_exclusive_group(required=True)
    retry_group.add_argument("--item", help="Chunk id to retry.")
    retry_group.add_argument("--all-failed", action="store_true", help="Retry all failed chunks.")
    retry.set_defaults(handler=command_retry)

    refresh = subparsers.add_parser("refresh", help="Adopt a changed source media revision while preserving prior artifacts.")
    refresh.add_argument("run")
    refresh.set_defaults(handler=command_refresh)

    dictionary = subparsers.add_parser("dict", help="Manage the user correction dictionary.")
    dict_sub = dictionary.add_subparsers(dest="dict_command", required=True)

    dict_list = dict_sub.add_parser("list", help="List dictionary entries.")
    dict_list.add_argument("--scope", choices=["global", "project", "merged"], default="merged")
    dict_list.add_argument("--project-dictionary", help="Path to a project dictionary override.")
    dict_list.set_defaults(handler=command_dict_list)

    dict_add = dict_sub.add_parser("add", help="Add or update a correction entry.")
    dict_add.add_argument("--correct", required=True, help="The correct spelling to produce.")
    dict_add.add_argument("--variant", action="append", default=[], help="A misheard spelling (repeatable).")
    dict_add.add_argument("--category", choices=["name", "acronym", "term"], default="term")
    dict_add.add_argument("--case-sensitive", action="store_true", help="Match variants case-sensitively.")
    dict_add.add_argument("--substring", action="store_true", help="Match anywhere, not only whole words.")
    dict_add.add_argument("--scope", choices=["global", "project"], default="global")
    dict_add.add_argument("--project-dictionary", help="Path to a project dictionary override.")
    dict_add.set_defaults(handler=command_dict_add)

    dict_remove = dict_sub.add_parser("remove", help="Remove a correction entry by its correct form.")
    dict_remove.add_argument("--correct", required=True)
    dict_remove.add_argument("--scope", choices=["global", "project"], default="global")
    dict_remove.add_argument("--project-dictionary", help="Path to a project dictionary override.")
    dict_remove.set_defaults(handler=command_dict_remove)

    dict_apply = dict_sub.add_parser("apply", help="Apply the dictionary to an existing transcript.")
    dict_apply.add_argument("transcript")
    dict_apply.add_argument("--output", required=True)
    add_dictionary_arguments(dict_apply)
    dict_apply.set_defaults(handler=command_dict_apply)

    return root


def main():
    ensure_model_cache_env()
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
