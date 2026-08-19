#!/usr/bin/env python3
"""Client for the llm-stack speech-to-text sidecar.

The deployment behind ``llms`` serves transcription at ``http://llms:8014``,
separately from the chat/embed/rerank ports. It is not an inference service in
the sense ``forge_llm`` means: there is no chat completion, no context window,
and no slot to pin, so its defaults live here rather than in
``forge_llm.DEFAULT_SERVICES`` -- the same arrangement ``stack_state`` uses.

Standard library only, so the transcription skill stays installable without
extra dependencies.

Two things about this service shape everything below.

**The weights are not resident.** ``/engines`` reports ``yield_mode: "asr"`` and
``idle_unload_seconds: 300``: the ASR model yields VRAM to the model router that
serves embed/ocr/rank/task, and unloads itself when idle. So ``resident: null``
is the normal state, not a fault, and the first call after a quiet period pays a
model load of roughly 25 seconds before decoding starts. Nothing here ever asks
for a swap -- the service arbitrates that itself. What this client owes it is
patience: a timeout in minutes, and no tight retry loop making GPU pressure
worse.

**Long audio does not answer with a transcript.** Above the service's threshold
(900s today) ``/transcribe`` answers ``202`` with a job id, and the transcript
arrives from ``/jobs/<id>``. A caller that reads ``text`` off that first response
stores an empty string and does not find out until something reads it back, so
``transcribe`` below resolves the job before returning and no caller ever sees
the envelope.
"""

import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

DEFAULT_TRANSCRIPTION_URL = "http://llms:8014"

# Named rather than inherited. The service's own default was `faster-whisper`
# until 2026-08-09 and is `parakeet-v3` now, and the two normalize text
# differently -- Whisper writes "July 21st, 1969" where Parakeet has written
# "July twenty first, nineteen sixty nine" on the same audio. Following whatever
# the server currently prefers would change how dates and figures reach a note
# without anything in forge recording that it had changed.
DEFAULT_ENGINE = "parakeet-v3"

# Generous because a cold call is an upload, then a ~25s model load, then the
# decode. The decode itself is fast (~210x realtime on Parakeet), so this is
# almost entirely budget for the other two.
DEFAULT_TIMEOUT = 900.0

# `/health`, `/engines` and `/jobs` are cheap and never load a model, so they
# get a short budget: a doctor pass against a host that is down should not feel
# like a hang.
CONTROL_TIMEOUT = 30.0

# The service asks for no faster than 1s. A 70-minute file decodes in about 20s,
# so this costs a handful of polls on even a long job.
POLL_INTERVAL = 2.5
MINIMUM_POLL_DEADLINE = 600.0
# `estimated_seconds` is a hint, not a deadline -- the same file has measured
# between 208x and 240x realtime depending on what else holds the GPU -- so the
# deadline is a multiple of it rather than it.
POLL_DEADLINE_FACTOR = 4.0

UPLOAD_CAP_BYTES = 512 * 1024 * 1024

# Failures that will not fix themselves. `engine_unavailable` means a runtime is
# not installed on the host and no amount of retrying installs it; the others
# mean the request itself was wrong. `decode_failed` is here because the bytes
# do not change between attempts -- sending the same unreadable container three
# times was measured doing exactly that against the live service.
PERMANENT_ERROR_TYPES = frozenset(
    {"bad_request", "too_large", "unsupported_capability", "engine_unavailable", "decode_failed"}
)


class TranscribeError(Exception):
    """A transcription request that did not produce a transcript.

    ``error_type`` is the service's own ``error.type``. Branch on it, never on
    the message text. ``transient`` says whether trying again could plausibly
    help, and is what the skill's retry loop reads -- ``run_state`` classifies
    by looking for ``http 5xx`` in the message, which would retry an
    uninstalled runtime forever.
    """

    def __init__(self, message, error_type=None, hint=None, status=None, transient=False):
        super().__init__(message)
        self.error_type = error_type
        self.hint = hint
        self.status = status
        self.transient = transient


def _environment(env):
    return env if env is not None else os.environ


def resolve_transcription(base_url=None, engine=None, env=None, settings=None, api=None, model=None):
    """Resolve the service to ``{enabled, baseUrl, engine, api, model, token, timeoutSeconds}``.

    Precedence matches every other connected service: explicit argument, then
    environment, then the agent's ``connectedServices`` settings, then the
    built-in default. An env var set to the empty string turns the integration
    off for this process, the way ``FORGE_SEARXNG_URL=""`` disables search.

    ``api`` selects the wire protocol: ``"sidecar"`` (default) is the pi-forge
    async ``/transcribe`` + ``/jobs`` API; ``"openai"`` is a single synchronous
    ``POST /v1/audio/transcriptions`` (mlx-audio and other OpenAI-compatible ASR
    servers). ``model`` is the OpenAI ``model`` form field; unset lets the server
    pick its default, and it is unused on the sidecar route.
    """
    environment = _environment(env)

    persisted = {}
    if settings is None:
        import forge_llm

        settings = forge_llm.load_connected_services(environment)
    if isinstance(settings, dict):
        candidate = settings.get("transcription")
        if isinstance(candidate, dict):
            persisted = candidate

    if base_url is not None:
        resolved_url = _clean(base_url)
    elif "FORGE_TRANSCRIPTION_URL" in environment:
        resolved_url = _clean(environment.get("FORGE_TRANSCRIPTION_URL"))
    else:
        resolved_url = _clean(persisted.get("baseUrl")) or DEFAULT_TRANSCRIPTION_URL

    resolved_engine = (
        _clean(engine)
        or _clean(environment.get("FORGE_TRANSCRIPTION_ENGINE"))
        or _clean(persisted.get("engine"))
        or DEFAULT_ENGINE
    )
    # Presence, not truthiness: FORGE_TRANSCRIPTION_TOKEN="" clears a persisted
    # token for this process rather than falling back to it.
    if "FORGE_TRANSCRIPTION_TOKEN" in environment:
        token = _clean(environment.get("FORGE_TRANSCRIPTION_TOKEN"))
    else:
        token = _clean(persisted.get("token"))
    timeout = _positive_number(persisted.get("timeoutSeconds")) or DEFAULT_TIMEOUT

    resolved_api = (
        _clean(api)
        or _clean(environment.get("FORGE_TRANSCRIPTION_API"))
        or _clean(persisted.get("api"))
        or "sidecar"
    ).lower()
    if resolved_api not in ("sidecar", "openai"):
        # An unknown protocol name would silently pick a route; fall back to the
        # safe default rather than guess. The doctor surfaces the real setting.
        resolved_api = "sidecar"
    resolved_model = (
        _clean(model)
        or _clean(environment.get("FORGE_TRANSCRIPTION_MODEL"))
        or _clean(persisted.get("model"))
    )

    enabled = bool(persisted.get("enabled", True)) and bool(resolved_url)
    return {
        "name": "transcription",
        "enabled": enabled,
        "baseUrl": resolved_url,
        "engine": resolved_engine,
        "api": resolved_api,
        "model": resolved_model,
        "token": token,
        "timeoutSeconds": timeout,
    }


def _clean(value):
    if not isinstance(value, str):
        return None
    trimmed = value.strip().rstrip("/")
    return trimmed or None


def _positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _seconds(value):
    """A timeline offset. Unlike ``_positive_number``, zero is a real answer."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _request(service, path, *, method="GET", data=None, headers=None, timeout=CONTROL_TIMEOUT):
    """One request, returning ``(status, parsed_body)``.

    A non-2xx answer is returned rather than raised: the service reports its
    failures as a JSON body with a typed ``error``, and that body is more useful
    than the status alone. Only a transport failure raises here.
    """
    url = f"{service['baseUrl']}{path}"
    request = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    if service.get("token"):
        request.add_header("Authorization", f"Bearer {service['token']}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, _parse(response.read(), response.headers.get("Content-Type"))
    except urllib.error.HTTPError as error:
        return error.code, _parse(error.read(), error.headers.get("Content-Type") if error.headers else None)
    except urllib.error.URLError as error:
        raise TranscribeError(
            f"the transcription service at {service['baseUrl']} could not be reached: {error.reason}",
            error_type="unreachable",
            transient=True,
        ) from error
    except OSError as error:
        raise TranscribeError(
            f"the transcription service at {service['baseUrl']} could not be reached: {error}",
            error_type="unreachable",
            transient=True,
        ) from error


def _parse(body, content_type):
    text = body.decode("utf-8", errors="replace") if isinstance(body, (bytes, bytearray)) else str(body)
    if content_type and "json" not in content_type.lower():
        return {"text": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def _raise_for_error(payload, status):
    """Turn the service's error envelope into a typed exception.

    ``{"ok": false, "error": {"type", "message", "hint"}}`` on the native route;
    OpenAI's ``{"error": {"message", "type", "code"}}`` on ``/v1/*``; and
    FastAPI's ``{"detail": ...}`` validation shape that an mlx-audio server
    returns for a 4xx. All three are read the same way.
    """
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        error = {}
    error_type = error.get("type")
    message = error.get("message") or payload.get("text")
    if not message and isinstance(payload, dict) and payload.get("detail") is not None:
        message = _detail_message(payload["detail"])
        # A FastAPI 4xx is the request's own fault -- a wrong field or a rejected
        # file -- so it is permanent; a 5xx keeps the transient default.
        if error_type is None and 400 <= status < 500:
            error_type = "bad_request"
    error_type = error_type or "upstream_error"
    if not message:
        message = f"the transcription service answered HTTP {status}"
    hint = error.get("hint")
    transient = error_type not in PERMANENT_ERROR_TYPES
    raise TranscribeError(message, error_type=error_type, hint=hint, status=status, transient=transient)


def _detail_message(detail):
    """A human message from FastAPI's ``detail`` -- a string or a list of errors."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if not isinstance(item, dict):
                continue
            location = ".".join(str(part) for part in (item.get("loc") or []) if part not in (None, "body"))
            message = item.get("msg") or "invalid"
            parts.append(f"{location}: {message}" if location else message)
        if parts:
            return "; ".join(parts)
    return None


def health(service):
    """``GET /health``, or ``None`` when the service cannot be reached.

    Never requires a token and never loads a model, so it is safe to poll.
    """
    try:
        status, payload = _request(service, "/health")
    except TranscribeError:
        return None
    return payload if status == 200 and isinstance(payload, dict) else None


def engines(service):
    """``GET /engines``, or ``None`` when the service cannot be reached.

    The authority on what is usable. An engine can be registered and still
    unusable in two different ways -- its runtime missing, or its model unset --
    and asking here is how a run finds out before spending an upload on it.
    """
    try:
        status, payload = _request(service, "/engines")
    except TranscribeError:
        return None
    return payload if status == 200 and isinstance(payload, dict) else None


def engine_status(report, engine):
    """Whether ``engine`` is usable, from an ``/engines`` report.

    Returns ``(usable, reason)``. "Registered" is not "usable": ``hf-asr`` ships
    registered with an empty ``model``, which fails at load time as
    ``model_load_failed`` rather than as the ``engine_unavailable`` a missing
    runtime gives. Both are worth catching before an upload, and they need
    different remediation.
    """
    if not isinstance(report, dict):
        return False, "the engine list could not be read"
    listed = report.get("engines")
    entries = [item for item in listed if isinstance(item, dict)] if isinstance(listed, list) else []
    match = next((item for item in entries if item.get("id") == engine), None)
    if match is None:
        known = ", ".join(sorted(str(item.get("id")) for item in entries if item.get("id"))) or "none"
        return False, f"'{engine}' is not registered on this service (registered: {known})"
    if not _clean(match.get("model")):
        return False, f"'{engine}' has no model configured on the host, so loading it would fail"
    return True, None


# ---------------------------------------------------------------------------
# Transcription
# ---------------------------------------------------------------------------

class _MultipartBody:
    """A multipart body that reads the file from disk as it is sent.

    ``urllib`` will send any object with ``read``, but it will not work out the
    length of one, so ``Content-Length`` is computed up front from the file size
    and set by the caller. Building the body as one ``bytes`` instead would mean
    holding a half-gigabyte upload in memory to send it.
    """

    def __init__(self, boundary, fields, file_path, file_field="file"):
        self.boundary = boundary
        self._path = Path(file_path)
        prologue = []
        for name, value in fields.items():
            if value is None:
                continue
            prologue.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n"
            )
        content_type = mimetypes.guess_type(self._path.name)[0] or "application/octet-stream"
        prologue.append(
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{file_field}\"; filename=\"{self._path.name}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        )
        self._prologue = "".join(prologue).encode("utf-8")
        self._epilogue = f"\r\n--{boundary}--\r\n".encode("utf-8")
        self._size = self._path.stat().st_size
        self._handle = None
        self._state = "prologue"

    @property
    def content_length(self):
        return len(self._prologue) + self._size + len(self._epilogue)

    def read(self, amount=-1):
        if self._state == "prologue":
            chunk, self._prologue = self._prologue, b""
            self._state = "file"
            self._handle = self._path.open("rb")
            return chunk
        if self._state == "file":
            chunk = self._handle.read(amount if amount and amount > 0 else 1024 * 1024)
            if chunk:
                return chunk
            self._handle.close()
            self._handle = None
            self._state = "epilogue"
        if self._state == "epilogue":
            chunk, self._epilogue = self._epilogue, b""
            self._state = "done"
            return chunk
        return b""

    def close(self):
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def transcribe(
    service,
    path,
    *,
    engine=None,
    language=None,
    word_timestamps=True,
    response_format="json",
    on_wait=None,
):
    """Transcribe one file, returning the service's full result envelope.

    ``word_timestamps`` defaults to true deliberately. NeMo returns no timeline
    at all without it -- "segments" become the decoder's own 60-second windows,
    every subtitle cue spans a whole window, and ``words`` comes back empty --
    and the capability being advertised as available does not mean it is
    supplied unasked.

    A long file answers ``202`` with a job id; this polls it to completion and
    returns the same shape either way, so callers never handle two.

    When ``service["api"] == "openai"`` the request instead goes to a single
    synchronous ``POST /v1/audio/transcriptions`` and its response is adapted to
    the same envelope, so callers -- and ``segments_from_result`` below -- are
    unchanged regardless of which server answered.
    """
    source = Path(path)
    size = source.stat().st_size
    if size > UPLOAD_CAP_BYTES:
        raise TranscribeError(
            f"{source.name} is {size / 1024 / 1024:.0f} MB, over the service's "
            f"{UPLOAD_CAP_BYTES // 1024 // 1024} MB upload cap",
            error_type="too_large",
            transient=False,
        )

    if (service.get("api") or "sidecar") == "openai":
        return _openai_transcribe(service, source, language=language, word_timestamps=word_timestamps)

    boundary = f"----pi-forge-{uuid.uuid4().hex}"
    fields = {
        "engine": engine or service.get("engine") or DEFAULT_ENGINE,
        "response_format": response_format,
        "word_timestamps": "true" if word_timestamps else "false",
    }
    if language:
        fields["language"] = language
    body = _MultipartBody(boundary, fields, source)
    try:
        status, payload = _request(
            service,
            "/transcribe",
            method="POST",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(body.content_length),
            },
            timeout=service.get("timeoutSeconds") or DEFAULT_TIMEOUT,
        )
    finally:
        body.close()

    if status == 202 or (isinstance(payload, dict) and payload.get("job_id") and not payload.get("text")):
        return _await_job(service, payload, on_wait=on_wait)
    if status >= 400 or not (isinstance(payload, dict) and payload.get("ok")):
        _raise_for_error(payload if isinstance(payload, dict) else {}, status)
    return payload


def _openai_transcribe(service, source, *, language=None, word_timestamps=True):
    """One synchronous ``POST /v1/audio/transcriptions``, adapted to the envelope.

    ``verbose_json`` is requested unconditionally: it returns ``segments`` in the
    exact ``{start, end, text}`` shape the sidecar route does (mlx-audio also
    nests per-word times under each segment), so ``segments_from_result`` reads
    it with no special case. A server that ignores the format and returns only
    ``{"text": ...}`` still works -- the adapter falls back to the flat text.
    """
    boundary = f"----pi-forge-{uuid.uuid4().hex}"
    fields = {
        "response_format": "verbose_json",
        "word_timestamps": "true" if word_timestamps else "false",
    }
    model = _clean(service.get("model"))
    if model:
        fields["model"] = model
    if language:
        fields["language"] = language
    body = _MultipartBody(boundary, fields, source)
    try:
        status, payload = _request(
            service,
            "/v1/audio/transcriptions",
            method="POST",
            data=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(body.content_length),
            },
            timeout=service.get("timeoutSeconds") or DEFAULT_TIMEOUT,
        )
    finally:
        body.close()

    # No ``ok`` discriminator on this route -- a 2xx with a JSON body is success.
    if status >= 400 or not isinstance(payload, dict):
        _raise_for_error(payload if isinstance(payload, dict) else {}, status)
    return _openai_to_envelope(payload, service)


def _openai_to_envelope(payload, service):
    """Reshape an OpenAI transcription response into the sidecar's result envelope.

    Fills the fields the transcription skill reads -- ``segments`` (native shape),
    ``text``, ``duration`` (no top-level field on this route, so the last segment
    end), ``language``, ``capabilities``, ``engine``/``model``/``device`` -- so a
    text-only server degrades to one 0:00 block (via ``segments_from_result``)
    rather than failing.
    """
    text = (payload.get("text") or "").strip()
    segments = payload.get("segments")
    segments = segments if isinstance(segments, list) else []
    duration = 0.0
    for segment in segments:
        if isinstance(segment, dict):
            duration = max(duration, _seconds(segment.get("end")))
    has_words = any(isinstance(s, dict) and s.get("words") for s in segments) or bool(payload.get("words"))
    return {
        "ok": True,
        "text": text,
        "language": payload.get("language"),
        "duration": duration,
        "segments": segments,
        "engine": service.get("engine") or DEFAULT_ENGINE,
        "model": payload.get("model") or _clean(service.get("model")) or "",
        "device": payload.get("backend") or "openai",
        "capabilities": {"word_timestamps": bool(has_words), "diarization": False, "translate": False},
        "timings": {},
        # A response with no timeline is a flat block: mark it so the skill warns,
        # the same way the sidecar flags a synthetic timeline.
        "degraded": not segments,
    }


def _await_job(service, envelope, on_wait=None):
    """Poll a queued job to a terminal state and return its result."""
    job_id = envelope.get("job_id")
    poll_path = envelope.get("poll") or (f"/jobs/{job_id}" if job_id else None)
    if not poll_path:
        raise TranscribeError(
            "the service queued the audio but returned no job to poll",
            error_type="upstream_error",
            transient=False,
        )
    estimated = _positive_number(envelope.get("estimated_seconds")) or 0.0
    deadline = time.monotonic() + max(MINIMUM_POLL_DEADLINE, estimated * POLL_DEADLINE_FACTOR)
    if on_wait:
        on_wait({"job_id": job_id, "status": envelope.get("status") or "queued", "estimated_seconds": estimated})

    while True:
        time.sleep(POLL_INTERVAL)
        status, payload = _request(service, poll_path)
        if status >= 400:
            _raise_for_error(payload if isinstance(payload, dict) else {}, status)
        state = payload.get("status") if isinstance(payload, dict) else None
        if state == "done":
            result = payload.get("result")
            if not isinstance(result, dict):
                raise TranscribeError(
                    f"job {job_id} finished without a result",
                    error_type="upstream_error",
                    transient=False,
                )
            return result
        if state == "error":
            _raise_for_error(payload if isinstance(payload, dict) else {}, status)
        if time.monotonic() > deadline:
            raise TranscribeError(
                f"job {job_id} was still '{state}' after waiting "
                f"{max(MINIMUM_POLL_DEADLINE, estimated * POLL_DEADLINE_FACTOR):.0f}s",
                error_type="timeout",
                transient=True,
            )
        if on_wait:
            on_wait({"job_id": job_id, "status": state, "estimated_seconds": estimated})


def segments_from_result(result):
    """The result's segments in the skill's ``[{start, end, text}]`` shape.

    Empty segments are dropped: a decode window containing only silence comes
    back with an empty ``text`` and would otherwise become a blank subtitle cue.
    """
    segments = []
    for item in result.get("segments") or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue
        segments.append({"start": _seconds(item.get("start")), "end": _seconds(item.get("end")), "text": text})
    if not segments:
        text = (result.get("text") or "").strip()
        if text:
            segments.append({"start": 0.0, "end": 0.0, "text": text})
    return segments


def load_seconds(result):
    """How long this call spent loading weights, or 0.

    Non-zero means the model was not resident: it had been unloaded after idling
    or had yielded the GPU to the model router. Worth reporting, because it is
    the difference between a run that looked slow and a run that was slow.
    """
    timings = result.get("timings")
    if not isinstance(timings, dict):
        return 0.0
    return (_positive_number(timings.get("load_ms")) or 0.0) / 1000.0
