#!/usr/bin/env python3

"""Transcribe audio/video through the llm-stack speech-to-text service, then
apply a persistent user correction dictionary. Outputs are designed to flow into
the transcript-cleanup skill. Originals are never modified.

Recognition happens on the `llms` host, which serves Parakeet TDT v3 and four
other engines at `http://llms:8014`. Nothing is installed locally: there is no
virtualenv, no model download, and ffmpeg is needed only for video or for a file
over the service's upload cap. See `forge/lib/forge_transcribe.py` for the
client and for why its timeouts are what they are.

The correction dictionary is local and durable, under
${PI_FORGE_HOME:-~/.pi-forge}/transcription, so updates to the repository do not
remove it."""

import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
from vault_schema import ensure_workspace_marker  # noqa: E402
import forge_transcribe
import run_state
import vault_lexicon
from vault_lexicon import (  # noqa: F401  (re-exported for callers and tests)
    apply_corrections,
    compile_corrections,
    merge_dictionaries,
    normalize_entry,
)


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

# Formats the service decodes for itself, verified against the live service on
# 2026-08-10 rather than taken from its documentation. The host decodes audio
# through torchaudio without ffmpeg bindings, so anything in an MP4/ISO
# container -- .m4a, .mp4, .mov, .m4v -- comes back `decode_failed`, and .m4a is
# precisely what a phone voice memo is.
#
# Format is only half of it: the model takes a mono signal and answers
# `decode_failed` ("Input shape mismatch ... torch.Size([1, 2, N])") for stereo
# at any sample rate, in any of these formats. So a file is sent as it is only
# when it is both in this set and already mono; everything else is stripped to
# mono Opus locally first.
DIRECT_UPLOAD_EXTENSIONS = {".wav", ".flac", ".mp3", ".opus", ".ogg", ".oga", ".aiff", ".aif", ".aac"}

TARGET_SAMPLE_RATE = 16000
# Speech-rate Opus, for a source that cannot be sent as it is. At about 3 KB/s
# this fits roughly 47 hours inside the service's 512 MB cap, and Opus is in the
# set above. Checked against the same clip sent as WAV: identical transcript.
COMPRESSED_BITRATE = "24k"

# The whole file is one unit of work now: the service handles long audio itself,
# and splitting it locally would multiply the uploads and defeat that. The
# run-state item list is kept because it is what makes a run resumable and
# `retry`/`status`/`refresh` work; it simply has one member.
SINGLE_ITEM_ID = "chunk-0001"

MANIFEST_COLUMNS = [
    "source_path",
    "source_sha256",
    "source_format",
    "duration_seconds",
    "backend",
    "engine",
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
# Local state: the correction dictionary
# ---------------------------------------------------------------------------

def pi_forge_home():
    return Path(os.environ.get("PI_FORGE_HOME", Path.home() / ".pi-forge"))


def transcription_home():
    override = os.environ.get("PI_FORGE_TRANSCRIPTION_HOME")
    if override:
        return Path(override).expanduser()
    return pi_forge_home() / "transcription"


def global_dictionary_path():
    return transcription_home() / "dictionary.json"


def project_dictionary_path(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    return (Path.cwd() / ".forge" / "transcription-dictionary.json").resolve()


def load_dictionary(path):
    """Shared loader, reported the way this CLI reports everything else."""
    try:
        return vault_lexicon.load_dictionary(path)
    except vault_lexicon.UserError as error:
        fail(str(error))


def save_dictionary(path, entries):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "updated": utc_now(),
        "entries": sorted(entries, key=lambda entry: entry["correct"].lower()),
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def vault_terms(vault):
    """Terms from a vault's lexicon note, so the two skills share one glossary.

    The vault note is the ergonomic surface -- it is editable in Obsidian from
    anywhere -- and this skill can reach it whenever a vault is named.
    """
    if not vault:
        return []
    root = Path(vault).expanduser()
    if not root.is_dir():
        fail(f"vault root does not exist: {root}")
    try:
        path = vault_lexicon.resolve_lexicon_path(root)
        if path is None:
            return []
        return vault_lexicon.parse_lexicon_note(path.read_text(encoding="utf-8"))["terms"]
    except (vault_lexicon.UserError, OSError) as error:
        fail(f"could not read the vault lexicon: {error}")


def resolve_dictionary(args):
    if getattr(args, "no_dictionary", False):
        return [], {"global": None, "project": None, "vault": None}
    global_path = global_dictionary_path()
    project_path = project_dictionary_path(getattr(args, "project_dictionary", None))
    global_entries = load_dictionary(global_path)
    project_entries = load_dictionary(project_path)
    vault_entries = vault_terms(getattr(args, "vault", None))
    merged = merge_dictionaries(merge_dictionaries(global_entries, project_entries), vault_entries)
    return merged, {
        "global": str(global_path) if global_entries else None,
        "project": str(project_path) if project_entries else None,
        "vault": str(getattr(args, "vault", None)) if vault_entries else None,
    }


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def resolve_service(args):
    return forge_transcribe.resolve_transcription(
        base_url=getattr(args, "base_url", None),
        engine=getattr(args, "engine", None),
        api=getattr(args, "api", None),
        model=getattr(args, "model", None),
    )


def orphaned_local_install():
    """The pre-service install, if it is still on disk.

    Earlier versions of this skill built a per-platform virtualenv and cached a
    ~2.5 GB model under the same home the dictionary lives in. Nothing uses them
    now. They are reported so the space is findable, and never deleted: the
    dictionary shares that directory, and it is not this script's call to remove
    gigabytes from someone's disk.
    """
    home = transcription_home()
    leftovers = [path for path in (home / "models", home / "venv-mlx", home / "venv-nemo") if path.exists()]
    if not leftovers:
        return None
    total = 0
    for path in leftovers:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    return {"paths": [str(path) for path in leftovers], "bytes": total}


def command_doctor(args):
    service = resolve_service(args)
    report = {
        "service": {
            "baseUrl": service["baseUrl"],
            "engine": service["engine"],
            "tokenConfigured": bool(service["token"]),
            "timeoutSeconds": service["timeoutSeconds"],
        },
        "enabled": service["enabled"],
        "health": None,
        "engine_available": False,
        "ffmpeg": tool_version("ffmpeg", ["-version"]),
        "global_dictionary": str(global_dictionary_path()),
        "project_dictionary": str(project_dictionary_path()),
        "remediation": [],
    }

    if not service["enabled"]:
        report["remediation"].append(
            "The transcription service is turned off. Set connectedServices.transcription.enabled, "
            "or unset FORGE_TRANSCRIPTION_URL if it is set to an empty value."
        )
        report["ready"] = False
        print(json.dumps(report, indent=2))
        raise SystemExit(1)

    health = forge_transcribe.health(service)
    report["health"] = health.get("status") if isinstance(health, dict) else None
    # The sidecar answers "ok"; an OpenAI-API server (mlx-audio) answers "healthy".
    # Both mean the same thing, so accept either rather than false-flagging the
    # host as down when transcription would in fact work.
    if report["health"] not in ("ok", "healthy"):
        report["remediation"].append(
            f"No answer from {service['baseUrl']}/health. Check the host is up and reachable "
            f"(curl -sf {service['baseUrl']}/health), or point FORGE_TRANSCRIPTION_URL elsewhere."
        )
        report["ready"] = False
        print(json.dumps(report, indent=2))
        raise SystemExit(1)

    engines = forge_transcribe.engines(service) or {}
    report["active_engine"] = engines.get("active_engine")
    report["device"] = engines.get("device")
    # `resident: null` is the normal reading, not a fault: the weights are
    # unloaded when idle and reloaded on demand. Reported so a slow first call
    # is legible rather than surprising.
    report["resident"] = engines.get("resident")
    report["idle_unload_seconds"] = engines.get("idle_unload_seconds")
    report["router"] = engines.get("router")
    report["engines"] = [item.get("id") for item in engines.get("engines", []) if isinstance(item, dict)]

    available, reason = forge_transcribe.engine_status(engines, service["engine"])
    report["engine_available"] = available
    if not available:
        report["engine_reason"] = reason
        report["remediation"].append(
            f"{reason}. Pick another with --engine, or set connectedServices.transcription.engine."
        )

    report["direct_upload_formats"] = sorted(DIRECT_UPLOAD_EXTENSIONS)
    if not report["ffmpeg"]:
        report["remediation"].append(
            "ffmpeg is not installed. Recordings in "
            f"{', '.join(sorted(DIRECT_UPLOAD_EXTENSIONS))} are sent to the service as they are, but "
            "anything else — including .m4a voice memos and every video container — has to be converted "
            "first, and so does any file over "
            f"{forge_transcribe.UPLOAD_CAP_BYTES // 1024 // 1024} MB. Install it with "
            "brew install ffmpeg / apt install ffmpeg."
        )

    orphaned = orphaned_local_install()
    if orphaned:
        report["orphaned_local_install"] = orphaned
        report["remediation"].append(
            f"The old local engine install is still on disk ({orphaned['bytes'] / 1e9:.1f} GB): "
            f"{', '.join(orphaned['paths'])}. Nothing uses it; delete it when you want the space."
        )

    report["ready"] = bool(available)
    print(json.dumps(report, indent=2))
    if not report["ready"]:
        raise SystemExit(1)


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------

def probe_duration(path):
    if shutil.which("ffprobe") is None:
        return None
    result = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def compress_audio(source, destination):
    """Strip to mono 16 kHz Opus, for a source that cannot be sent as it is."""
    run(
        [
            "ffmpeg", "-y", "-i", str(source),
            "-vn", "-ac", "1", "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "libopus", "-b:a", COMPRESSED_BITRATE,
            # Named explicitly because the destination is a `.tmp` file written
            # before an atomic rename, and ffmpeg picks its muxer from the
            # extension otherwise.
            "-f", "opus",
            str(destination),
        ]
    )


def probe_channels(path):
    """How many audio channels the source has, or ``None`` if unknowable."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=channels",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, check=False,
        )
    except OSError:
        return None
    try:
        return int(result.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def upload_reason(source):
    """Why this source cannot be sent as it is, or ``None`` if it can.

    Three independent reasons, and the message has to name which, because they
    have nothing to do with each other: the host's decoder, the model's input
    shape, and the size of this particular file.
    """
    if source.suffix.lower() not in DIRECT_UPLOAD_EXTENSIONS:
        return f"the service cannot decode {source.suffix.lower()} directly"
    channels = probe_channels(source)
    if channels is not None and channels != 1:
        return f"the model takes mono and this is {channels}-channel audio"
    if source.stat().st_size > forge_transcribe.UPLOAD_CAP_BYTES:
        return "the file is over the service's upload cap"
    return None


def prepare_upload(source, audio_dir):
    """The file to send, and any warning that preparing it produced.

    A format the service reads goes as it is, so a WAV is never re-encoded to
    reach a decoder that would have accepted it. Everything else is stripped to
    mono 16 kHz Opus first.
    """
    reason = upload_reason(source)
    if reason is None:
        return source, None

    if tool_version("ffmpeg", ["-version"]) is None:
        fail(
            f"{source.name} cannot be uploaded as it is because {reason}, so its audio must be "
            "converted first, and ffmpeg is not installed (brew install ffmpeg / apt install ffmpeg)."
        )

    audio_dir.mkdir(parents=True, exist_ok=True)
    compressed = audio_dir / "normalized.opus"
    if not compressed.is_file():
        print(f"Converting {source.name} to Opus with ffmpeg ({reason})...", file=sys.stderr)
        temporary = audio_dir / ".normalized.opus.tmp"
        compress_audio(source, temporary)
        os.replace(temporary, compressed)

    compressed_size = compressed.stat().st_size
    if compressed_size > forge_transcribe.UPLOAD_CAP_BYTES:
        fail(
            f"even compressed, {source.name} is {compressed_size / 1024 / 1024:.0f} MB, over the "
            f"{forge_transcribe.UPLOAD_CAP_BYTES // 1024 // 1024} MB upload cap. Split the recording and "
            "transcribe the parts separately."
        )
    warning = f"Audio was converted to mono 16 kHz Opus before upload because {reason}."
    return compressed, warning


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


def is_transient(error):
    """Whether retrying this failure could plausibly help.

    A `TranscribeError` already knows: `engine_unavailable` never fixes itself,
    and `run_state.is_transient_failure` would retry it anyway because it keys
    off `http 5xx` appearing in the message.
    """
    if isinstance(error, forge_transcribe.TranscribeError):
        return error.transient
    return run_state.is_transient_failure(error)


def command_transcribe(args):
    service = resolve_service(args)
    if not service["enabled"]:
        fail("the transcription service is turned off; run doctor for details")

    source = Path(args.media).expanduser().resolve()
    if not source.is_file():
        fail(f"media file does not exist: {source}")
    extension = source.suffix.lower()
    if extension not in MEDIA_EXTENSIONS:
        fail(f"unsupported media format {extension or '(none)'}; expected audio or video")

    source_hash = sha256(source)
    run_directory = Path(args.output).expanduser().resolve()
    configuration = {
        "workflow": "transcription",
        "command": "transcribe",
        "input": {"path": str(source), "sha256": source_hash},
        "options": {
            "service": service["baseUrl"],
            "engine": service["engine"],
            "type": args.type,
            "language": args.language,
            "wordTimestamps": not args.no_word_timestamps,
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
        # An unmarked run under the vault's workflow root is counted,
        # classified, filed and embedded as notes.
        ensure_workspace_marker(run_directory)
        state = run_state.create_run_state("transcription", "transcribe", configuration["input"], configuration["options"], phase="preparing", next_action="transcribe")
        run_state.initialize_run_state(run_directory, state)

    warnings = list(state.get("warnings", []))
    audio_dir = run_directory / "audio"
    upload_path, preparation_warning = prepare_upload(source, audio_dir)
    if preparation_warning and preparation_warning not in warnings:
        warnings.append(preparation_warning)

    if not state.get("items"):
        def initialize_item(draft):
            draft["items"] = [
                {
                    "id": SINGLE_ITEM_ID,
                    "path": str(upload_path),
                    "sha256": sha256(upload_path),
                    "offset": 0.0,
                    "status": "pending",
                    "attempts": 0,
                    "transient": False,
                    "error": None,
                }
            ]
            draft["warnings"] = warnings
            draft["phase"] = "transcribing"
            draft["nextAction"] = "transcribe"
            return draft
        state = run_state.update_run_state(run_directory, initialize_item, {"type": "items_initialized", "items": 1})

    results_dir = run_directory / "chunk_results"
    results_dir.mkdir(exist_ok=True)
    envelope = None
    pending = [item for item in state["items"] if run_state.retryable_item(item)]

    def report_wait(update):
        print(
            f"Queued as job {update['job_id']} (status: {update['status']}"
            + (f", estimated {update['estimated_seconds']:.0f}s" if update.get("estimated_seconds") else "")
            + "); waiting...",
            file=sys.stderr,
        )

    with run_state.run_lock(run_directory):
        for snapshot in pending:
            item = snapshot
            while run_state.retryable_item(item):
                attempt = item.get("attempts", 0) + 1
                state = run_state.update_run_state(run_directory, lambda draft, item_id=item["id"], attempt=attempt: mark_chunk_started(draft, item_id, attempt), {"type": "item_started", "itemId": item["id"], "attempt": attempt})
                item = next(value for value in state["items"] if value["id"] == item["id"])
                try:
                    print(
                        f"Transcribing {Path(item['path']).name} on {service['baseUrl']} "
                        f"with {service['engine']}...",
                        file=sys.stderr,
                    )
                    envelope = forge_transcribe.transcribe(
                        service,
                        item["path"],
                        language=args.language,
                        word_timestamps=not args.no_word_timestamps,
                        on_wait=report_wait,
                    )
                    segments = forge_transcribe.segments_from_result(envelope)
                    result_path = results_dir / f"{item['id']}.json"
                    run_state.atomic_write_json(result_path, {"chunkId": item["id"], "sha256": item["sha256"], "offset": item["offset"], "segments": segments})
                    run_state.atomic_write_json(run_directory / "remote_response.json", envelope)
                    state = run_state.update_run_state(run_directory, lambda draft, item_id=item["id"], path=str(result_path): mark_chunk_complete(draft, item_id, path), {"type": "item_completed", "itemId": item["id"], "attempt": attempt})
                    break
                except Exception as error:
                    transient = is_transient(error)
                    detail = str(error)
                    hint = getattr(error, "hint", None)
                    if hint:
                        detail = f"{detail} ({hint})"
                    state = run_state.update_run_state(run_directory, lambda draft, item_id=item["id"], error=detail, transient=transient: mark_chunk_failed(draft, item_id, error, transient), {"type": "item_failed", "itemId": item["id"], "attempt": attempt, "transient": transient, "error": detail})
                    item = next(value for value in state["items"] if value["id"] == item["id"])
                    if not transient or attempt >= run_state.DEFAULT_MAX_ATTEMPTS:
                        break

    state = run_state.load_run_state(run_directory, "transcription")
    failed = [item for item in state["items"] if item["status"] == "failed"]
    if failed:
        detail = "; ".join(f"{item['id']}: {item.get('error')}" for item in failed)
        fail(f"transcription failed: {detail}")
    try:
        segments = assemble_chunk_segments(state)
    except ValueError as error:
        fail(str(error))
    if not segments:
        warnings.append("The service produced no transcript text; the audio may be silent or unintelligible.")

    stored = run_directory / "remote_response.json"
    if envelope is None and stored.is_file():
        envelope = json.loads(stored.read_text(encoding="utf-8"))
    envelope = envelope or {}

    duration = envelope.get("duration") or probe_duration(upload_path)
    load_seconds = forge_transcribe.load_seconds(envelope)
    if load_seconds:
        warnings.append(
            f"The service spent {load_seconds:.0f}s loading the model before decoding: the ASR weights had "
            "been unloaded, having yielded the GPU to the model router. Decode time is the rest."
        )
    if envelope.get("degraded"):
        warnings.append("The service reported a synthetic timeline (degraded); segment times are not real boundaries.")
    capabilities = envelope.get("capabilities") or {}
    if not args.no_word_timestamps and capabilities.get("word_timestamps") is False:
        warnings.append(
            f"Engine {envelope.get('engine')} cannot produce word timestamps, so segment boundaries are "
            "decode windows rather than utterances."
        )

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
        "backend": "llm-stack",
        "engine": envelope.get("engine") or service["engine"],
        "model": envelope.get("model") or "",
        "device": envelope.get("device") or "",
        "chunk_count": len(state["items"]),
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
        "backend": "llm-stack",
        "service": service["baseUrl"],
        "engine": envelope.get("engine") or service["engine"],
        "model": envelope.get("model") or "",
        "device": envelope.get("device") or "",
        "capabilities": capabilities,
        "timings": envelope.get("timings") or {},
        "chunk_count": len(state["items"]),
        "segment_count": len(segments),
        "correction_count": correction_count,
        "dictionary_sources": dictionary_sources,
        "type": args.type,
        "recommended_track": track,
        "outputs": {
            "raw_transcript": str(run_directory / "raw_transcript.txt"),
            "raw_segments": str(run_directory / "raw_segments.json"),
            "raw_srt": str(run_directory / "raw_transcript.srt"),
            "remote_response": str(stored),
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
        {"type": "run_completed", "segments": len(segments)},
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
        fail("no failed items selected")
    known = {item["id"] for item in state["items"]}
    unknown = targets - known
    if unknown:
        fail(f"unknown item id(s): {', '.join(sorted(unknown))}")

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
            "remote_response.json", "corrected_transcript.md", "corrected_transcript.txt", "corrections_log.csv",
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
        draft.update({"status": "running", "phase": "preparing", "nextAction": "transcribe"})
        draft.pop("completion", None)
        draft.pop("refreshPlan", None)
        return draft

    updated = run_state.update_run_state(run_directory, finish_refresh, {"type": "input_refreshed", "revision": plan["revision"], "newSha256": plan["newSha256"]})
    print(json.dumps({"run": str(run_directory), "refreshed": True, "revision": plan["revision"], "nextAction": updated["nextAction"]}, indent=2))


# ---------------------------------------------------------------------------
# export: hand a finished run to vault-transcripts
# ---------------------------------------------------------------------------

def export_timestamp(seconds):
    """A block marker in the shape `vault-transcripts` parses.

    That parser decides between `MM:SS` and `H:MM:SS` by counting colons, so an
    hour-long recording has to grow a field rather than run to `98:14`.
    """
    seconds = max(0, int(float(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, whole = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{whole:02d}"
    return f"{minutes:02d}:{whole:02d}"


def render_vault_transcript(segments, speaker=None):
    """The recording as a voice-app export.

    `vault-transcripts` reads what a transcription app drops into the vault
    inbox: an optional bold speaker label, an italic timestamp, then the text.
    The label is optional here because this service does no diarization --
    `capabilities.diarization` is false on every engine -- so inventing
    "Speaker 1" would be asserting something nobody measured. A solo recording
    can be labelled explicitly with `--speaker`.
    """
    blocks = []
    for segment in segments:
        marker = f"*{export_timestamp(segment['start'])}*"
        head = f"**{speaker}**\n{marker}" if speaker else marker
        blocks.append(f"{head}\n{segment['text']}\n")
    return "\n".join(blocks)


RECORDED_AT_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d %H%M%S", "%Y%m%d-%H%M%S", "%Y-%m-%d %H:%M")


def parse_recorded_at(value):
    for pattern in RECORDED_AT_FORMATS:
        try:
            return datetime.strptime(value, pattern)
        except ValueError:
            continue
    fail(f"could not read --recorded-at '{value}'; expected e.g. 2026-08-10T15:51:00 or '20260810 155100'")


def export_filename(moment, source_hash):
    """`YYYYMMDD HHMMSS-<id>.md`, which is the stamp `parse_filename` reads.

    The id is the source hash rather than a random one, so re-exporting the same
    recording produces the same filename instead of a second inbox note.
    """
    return f"{moment.strftime('%Y%m%d %H%M%S')}-{source_hash[:8].upper()}.md"


def command_export(args):
    run_directory = Path(args.run).expanduser().resolve()
    try:
        state = run_state.load_run_state(run_directory, "transcription")
    except (OSError, ValueError) as error:
        fail(str(error))
    if state.get("status") != "complete":
        fail(f"the run is not complete (status: {state.get('status')}); transcribe it first")

    segments_path = run_directory / "raw_segments.json"
    if not segments_path.is_file():
        fail(f"missing {segments_path.name}; the run did not finish writing its outputs")
    segments = json.loads(segments_path.read_text(encoding="utf-8"))
    if not segments:
        fail("the run produced no segments, so there is nothing to export")

    entries, dictionary_sources = resolve_dictionary(args)
    corrected = []
    correction_count = 0
    for segment in segments:
        text, log = apply_corrections(segment["text"], entries)
        correction_count += sum(row["count"] for row in log)
        corrected.append({**segment, "text": text})

    source = Path(state["input"]["path"])
    source_hash = state["input"]["sha256"]
    if args.recorded_at:
        moment = parse_recorded_at(args.recorded_at)
        date_source = "recorded-at"
    elif source.is_file():
        # A filesystem timestamp is when this machine last touched the file, not
        # when the recording was made -- a copied or re-exported file carries the
        # copy's date. Used because it beats nothing, and flagged because it is
        # a guess the reader has to be able to overrule.
        moment = datetime.fromtimestamp(source.stat().st_mtime)
        date_source = "source-file-mtime"
    else:
        moment = datetime.now()
        date_source = "export-time"

    body = render_vault_transcript(corrected, speaker=args.speaker)
    name = export_filename(moment, source_hash)
    if args.inbox:
        destination = Path(args.inbox).expanduser().resolve()
        if not destination.is_dir():
            fail(f"inbox directory does not exist: {destination}")
        output = destination / name
    elif args.output:
        output = Path(args.output).expanduser().resolve()
    else:
        output = run_directory / "vault_transcript.md"

    if output.exists() and not args.force:
        fail(f"{output} already exists; pass --force to replace it")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_state.atomic_write_text(output, body)

    warnings = []
    if date_source != "recorded-at":
        warnings.append(
            f"The recording date came from {date_source.replace('-', ' ')}, not from the recording itself. "
            "Pass --recorded-at to set it, or rename the file, before vault-transcripts reads the stamp."
        )
    if not args.speaker:
        warnings.append(
            "No speaker labels: this service does no diarization. vault-transcripts will treat the recording "
            "as unattributed unless --speaker names one."
        )

    result = {
        "run": str(run_directory),
        "output": str(output),
        "suggested_filename": name,
        "block_count": len(corrected),
        "correction_count": correction_count,
        "dictionary_sources": dictionary_sources,
        "speaker": args.speaker,
        "recorded_at": moment.isoformat(timespec="seconds"),
        "date_source": date_source,
        "next_step": (
            "Run the vault-transcripts skill; it will pick this up from the inbox."
            if args.inbox
            else f"Copy it into the vault inbox as '{name}', then run the vault-transcripts skill."
        ),
        "warnings": warnings,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# dict subcommands
# ---------------------------------------------------------------------------

def scope_path(scope, project_override=None):
    return global_dictionary_path() if scope == "global" else project_dictionary_path(project_override)


def command_dict_list(args):
    global_entries = load_dictionary(global_dictionary_path())
    project_entries = load_dictionary(project_dictionary_path(args.project_dictionary))
    vault_entries = vault_terms(getattr(args, "vault", None))
    if args.scope == "global":
        entries = global_entries
    elif args.scope == "project":
        entries = project_entries
    elif args.scope == "vault":
        entries = vault_entries
    else:
        entries = merge_dictionaries(merge_dictionaries(global_entries, project_entries), vault_entries)
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
    subparser.add_argument("--vault", help="Obsidian vault whose lexicon note adds terms on top.")
    if include_no_dictionary:
        subparser.add_argument("--no-dictionary", action="store_true", help="Skip dictionary corrections.")


def add_service_arguments(subparser):
    subparser.add_argument("--base-url", help="Transcription service base URL (default: the configured service).")
    subparser.add_argument("--engine", help="Recognition engine id, e.g. parakeet-v3 or faster-whisper.")
    subparser.add_argument(
        "--api",
        choices=("sidecar", "openai"),
        help="Wire protocol: sidecar (async /transcribe, default) or openai (/v1/audio/transcriptions).",
    )
    subparser.add_argument("--model", help="OpenAI model form field (openai api only); default lets the server pick.")


def parser():
    root = argparse.ArgumentParser(
        description="Transcribe audio/video through the llm-stack transcription service, with a user dictionary."
    )
    subparsers = root.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="Report service reachability, engines, and dictionary status.")
    add_service_arguments(doctor)
    doctor.set_defaults(handler=command_doctor)

    transcribe = subparsers.add_parser("transcribe", help="Transcribe an audio or video file.")
    transcribe.add_argument("media")
    transcribe.add_argument("--output", required=True, help="Run directory to create.")
    transcribe.add_argument("--type", choices=TRANSCRIPT_TYPES, default="other", help="Recording type for routing.")
    add_service_arguments(transcribe)
    transcribe.add_argument("--language", help="Optional language hint (Parakeet v3 is multilingual).")
    transcribe.add_argument(
        "--no-word-timestamps",
        action="store_true",
        help="Skip word timestamps. Faster, but NeMo engines then report decode windows instead of utterances.",
    )
    add_dictionary_arguments(transcribe)
    transcribe.set_defaults(handler=command_transcribe)

    export = subparsers.add_parser(
        "export", help="Write a finished run as a voice-app export that vault-transcripts can read."
    )
    export.add_argument("run", help="A completed transcription run directory.")
    destination = export.add_mutually_exclusive_group()
    destination.add_argument("--output", help="Write to this path (default: <run>/vault_transcript.md).")
    destination.add_argument("--inbox", help="Write into this vault inbox directory under the derived filename.")
    export.add_argument("--speaker", help="Label every block with this speaker. Omit for an unattributed recording.")
    export.add_argument("--recorded-at", help="When the recording was made, e.g. 2026-08-10T15:51:00.")
    export.add_argument("--force", action="store_true", help="Replace an existing export.")
    add_dictionary_arguments(export)
    export.set_defaults(handler=command_export)

    status = subparsers.add_parser("status", help="Report resumable transcription state and input drift.")
    status.add_argument("run")
    status.add_argument("--json", action="store_true", help="Accepted for the shared run-state interface.")
    status.set_defaults(handler=command_status)

    retry = subparsers.add_parser("retry", help="Explicitly retry a failed transcription.")
    retry.add_argument("run")
    retry_group = retry.add_mutually_exclusive_group(required=True)
    retry_group.add_argument("--item", help="Item id to retry.")
    retry_group.add_argument("--all-failed", action="store_true", help="Retry all failed items.")
    retry.set_defaults(handler=command_retry)

    refresh = subparsers.add_parser("refresh", help="Adopt a changed source media revision while preserving prior artifacts.")
    refresh.add_argument("run")
    refresh.set_defaults(handler=command_refresh)

    dictionary = subparsers.add_parser("dict", help="Manage the user correction dictionary.")
    dict_sub = dictionary.add_subparsers(dest="dict_command", required=True)

    dict_list = dict_sub.add_parser("list", help="List dictionary entries.")
    dict_list.add_argument("--scope", choices=["global", "project", "vault", "merged"], default="merged")
    dict_list.add_argument("--project-dictionary", help="Path to a project dictionary override.")
    dict_list.add_argument("--vault", help="Obsidian vault whose lexicon note adds terms on top.")
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
