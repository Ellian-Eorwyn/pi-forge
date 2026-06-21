#!/usr/bin/env python3

"""Transcribe audio/video with NVIDIA Parakeet TDT v3, then apply a persistent
user correction dictionary. Outputs are designed to flow into the
transcript-cleanup skill. Originals are never modified.

The recognition engine is autoselected by platform:
  * Apple Silicon (macOS arm64) -> parakeet-mlx (fast, native MLX)
  * everything else, incl. Linux + NVIDIA -> NeMo (CUDA-accelerated)

Dependencies and models install into a managed environment under
~/.pi-forge/transcription via `setup`, so the skill is self-contained and
packageable."""

import argparse
import csv
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


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


def model_cache_env():
    """Force model downloads into the managed, packageable cache."""
    env = os.environ.copy()
    env["HF_HOME"] = str(models_dir())
    env.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    return env


def model_cached(backend):
    marker = "models--" + BACKENDS[backend]["model"].replace("/", "--")
    hub = models_dir() / "hub"
    if hub.exists() and (hub / marker).exists():
        return True
    # Fall back to the default HF cache if setup has not relocated it yet.
    default_hub = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    return default_hub.exists() and (default_hub / marker).exists()


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
        code = f"from parakeet_mlx import from_pretrained; from_pretrained('{model}'); print('ok')"
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
        print(f"Loading {BACKENDS['mlx']['model']}...", file=sys.stderr)
        return from_pretrained(BACKENDS["mlx"]["model"])
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
    path.write_text("\n".join(lines), encoding="utf-8")


def unique_run_directory(path):
    path = Path(path)
    if not path.exists():
        return path
    parent, stem = path.parent, path.name
    index = 2
    while True:
        candidate = parent / f"{stem}-{index}"
        if not candidate.exists():
            return candidate
        index += 1


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

    run_directory = unique_run_directory(Path(args.output).expanduser().resolve())
    audio_dir = run_directory / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)

    warnings = []
    source_hash = sha256(source)

    wav_path = audio_dir / "normalized.wav"
    print("Normalizing audio with ffmpeg...", file=sys.stderr)
    extract_audio(source, wav_path)
    duration = probe_duration(wav_path)

    if duration and duration > args.chunk_threshold:
        print(f"Audio is {duration:.0f}s; splitting into {args.chunk_seconds}s windows...", file=sys.stderr)
        chunks = split_audio(wav_path, audio_dir / "chunks", args.chunk_seconds)
        warnings.append(
            f"Audio exceeded {args.chunk_threshold}s and was split into {len(chunks)} non-overlapping "
            f"{args.chunk_seconds}s windows; review wording at window boundaries."
        )
    else:
        chunks = [(wav_path, 0.0)]

    device = backend_device(backend)
    if backend == "nemo" and device == "cpu":
        warnings.append("Running NeMo on CPU (no CUDA GPU); transcription is correct but slow.")

    model = load_model(backend)
    print(f"Transcribing {len(chunks)} segment file(s) with {backend}...", file=sys.stderr)
    segments = transcribe_chunks(backend, model, chunks)
    if not segments:
        warnings.append("The model produced no transcript text; the audio may be silent or unintelligible.")

    raw_text = "\n\n".join(segment["text"] for segment in segments).strip() + ("\n" if segments else "")
    (run_directory / "raw_transcript.txt").write_text(raw_text, encoding="utf-8")
    (run_directory / "raw_segments.json").write_text(
        json.dumps(segments, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    write_srt(segments, run_directory / "raw_transcript.srt")

    entries, dictionary_sources = resolve_dictionary(args)
    corrected_text, correction_log = apply_corrections(raw_text, entries)
    correction_count = sum(row["count"] for row in correction_log)

    with (run_directory / "corrections_log.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CORRECTIONS_LOG_COLUMNS)
        writer.writeheader()
        writer.writerows(correction_log)

    track = TYPE_TRACKS.get(args.type, "faithful")
    markdown = f"# {source.stem}\n\n" + "\n\n".join(segment["text"] for segment in segments)
    markdown, _ = apply_corrections(markdown, entries)
    corrected_md_path = run_directory / "corrected_transcript.md"
    corrected_md_path.write_text(markdown.strip() + "\n", encoding="utf-8")
    (run_directory / "corrected_transcript.txt").write_text(corrected_text.strip() + "\n", encoding="utf-8")

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
    with (run_directory / "transcription_manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        writer.writerow(manifest_row)

    warning_lines = "\n".join(f"- {warning}" for warning in warnings) or "- None."
    (run_directory / "warnings.md").write_text(
        f"# Transcription Warnings\n\nGenerated {utc_now()}\n\n{warning_lines}\n", encoding="utf-8"
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
    print(json.dumps(result, indent=2))


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
    args = parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
