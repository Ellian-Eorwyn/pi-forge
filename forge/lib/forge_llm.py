#!/usr/bin/env python3
"""Shared, standard-library client for the forge chat endpoints.

One local model is served twice: a non-thinking configuration for bulk work and
a thinking configuration for judgment. Skills reach them through two named
services rather than hard-coded URLs:

- ``chat``  - every per-file batch call. Spends no hidden reasoning tokens.
- ``think`` - verification, review, and escalation of flagged batch work. Also
  the interactive agent's own server, which is why background calls against it
  pin a slot and yield to interactive turns.

A skill that processes hundreds of files calls ``chat`` in a tight serial loop
with a byte-stable system prompt, then hands the whole batch to ``think`` once.
Splitting the work that way keeps each server's prefix cache warm instead of
invalidating it on every item.

Design rules (shared with ``forge_embeddings``):

- Standard library only, so skills stay installable without extra dependencies.
- Resolution is layered: explicit argument, then environment, then the agent's
  ``connectedServices`` settings, then the built-in default. A skill therefore
  honors a user's configuration without knowing it exists.
- ``think`` falls back to ``chat`` when no thinking backend is configured. A
  single-endpoint install still verifies its batch work; it just loses the
  thinking/non-thinking split. Bulk work never silently falls back the other
  way, because quietly thinking per file is the cost this module exists to
  avoid.

Configuration:

- ``FORGE_BASE_CHAT_URL`` / ``FORGE_CHAT_URL`` and ``FORGE_BASE_MODEL`` override
  the batch service (default ``http://llms:8004/v1/chat/completions``, ``chat``).
- ``FORGE_THINK_URL`` / ``FORGE_THINK_MODEL`` override the thinking service
  (default ``http://llms:8008/v1/chat/completions``, ``code``).
"""

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import run_state

DEFAULT_SERVICES = {
    "chat": {
        "enabled": True,
        "url": "http://llms:8004/v1/chat/completions",
        "model": "chat",
        "scheduling": {
            "enabled": False,
            "interactiveSlot": 0,
            "backgroundSlot": 1,
            "idleGraceMs": 2000,
            "yieldMs": 1000,
            "backgroundOutputTokens": 4096,
        },
    },
    "think": {
        "enabled": True,
        "url": "http://llms:8008/v1/chat/completions",
        "model": "code",
        "scheduling": {
            "enabled": True,
            "interactiveSlot": 0,
            "backgroundSlot": 1,
            "idleGraceMs": 2000,
            "yieldMs": 1000,
            "backgroundOutputTokens": 4096,
        },
    },
}

SERVICE_URL_ENVIRONMENT = {
    "chat": ("FORGE_BASE_CHAT_URL", "FORGE_CHAT_URL"),
    "think": ("FORGE_THINK_URL",),
}
SERVICE_MODEL_ENVIRONMENT = {"chat": ("FORGE_BASE_MODEL",), "think": ("FORGE_THINK_MODEL",)}

# A thinking backend that was asked not to think can still emit a stray block.
# Strip it defensively everywhere rather than trusting any one server's config.
THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
DEFAULT_TIMEOUT = 600.0
MAX_TRANSIENT_ATTEMPTS = 3
LEASE_STALE_MS = 15000
# Reasoning is invisible in the response body: llama.cpp strips the think block
# server-side and reports no reasoning_content, so the only evidence is the
# token count. Measured on this deployment, the thinking server spends ~410
# hidden tokens answering a question the non-thinking one answers in 2 — even
# for a one-word reply. Anything well past the visible content is reasoning.
HIDDEN_TOKEN_MARGIN = 32
CHARACTERS_PER_TOKEN = 3.0


def hidden_token_count(generated_tokens, content):
    """Tokens generated beyond what the visible content can account for."""
    if not isinstance(generated_tokens, (int, float)):
        return None
    visible = len(str(content or "")) / CHARACTERS_PER_TOKEN
    return max(0, int(generated_tokens - visible))


class ChatError(RuntimeError):
    """A chat endpoint could not be reached or returned an unusable response."""


def forge_agent_directory(env=None):
    environment = env if env is not None else os.environ
    root = environment.get("PI_FORGE_HOME")
    if root:
        return Path(root).expanduser() / "agent"
    explicit = environment.get("PI_CODING_AGENT_DIR") or environment.get("PI_FORGE_AGENT_DIR")
    return Path(explicit).expanduser() if explicit else Path.home() / ".pi-forge" / "agent"


def load_connected_services(env=None):
    settings_path = forge_agent_directory(env) / "settings.json"
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    services = settings.get("connectedServices")
    return services if isinstance(services, dict) else {}


def normalize_base_url(value, default=None):
    """Accept a bare ``/v1`` base or a full chat-completions URL."""
    url = (value or default or "").strip().rstrip("/")
    if not url or url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return url


def resolve_service(name, base_url=None, model=None, env=None, settings=None):
    """Resolve a named service to ``{enabled, url, model, scheduling}``.

    Precedence: explicit argument, environment, ``connectedServices`` settings,
    built-in default.
    """
    environment = env if env is not None else os.environ
    defaults = DEFAULT_SERVICES.get(name)
    if defaults is None:
        raise KeyError(f"unknown service: {name}")
    configured = settings if settings is not None else load_connected_services(environment)
    persisted = configured.get(name) if isinstance(configured.get(name), dict) else {}

    environment_url = next((environment[key] for key in SERVICE_URL_ENVIRONMENT[name] if environment.get(key)), None)
    environment_model = next((environment[key] for key in SERVICE_MODEL_ENVIRONMENT[name] if environment.get(key)), None)
    resolved_url = normalize_base_url(base_url or environment_url or persisted.get("baseUrl"), defaults["url"])
    resolved_model = model or environment_model or persisted.get("model") or defaults["model"]

    persisted_scheduling = persisted.get("scheduling") if isinstance(persisted.get("scheduling"), dict) else {}
    scheduling = {**defaults["scheduling"]}
    for key, value in persisted_scheduling.items():
        if key in scheduling and isinstance(value, type(scheduling[key])):
            scheduling[key] = value
    return {
        "name": name,
        "enabled": bool(persisted.get("enabled", defaults["enabled"])),
        "url": resolved_url,
        "model": resolved_model,
        "scheduling": scheduling,
    }


def resolve_think_or_chat(base_url=None, model=None, env=None, settings=None):
    """The service to use for judgment, falling back to ``chat`` when no thinking
    backend is configured."""
    think = resolve_service("think", base_url=base_url, model=model, env=env, settings=settings)
    if think["enabled"] and think["url"]:
        return think
    fallback = resolve_service("chat", env=env, settings=settings)
    fallback["name"] = "think"
    fallback["fallback"] = "chat"
    return fallback


def extract_json_content(content):
    """Strip a stray think block and any code fence, returning JSON text."""
    text = THINK_BLOCK_RE.sub("", str(content or "")).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def parse_json_content(content):
    """Parse a model response as JSON, tolerating prose around the payload."""
    text = extract_json_content(content)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((position for position in (text.find("{"), text.find("[")) if position >= 0), default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start < 0 or end < start:
            raise
        return json.loads(text[start : end + 1])


def _lease_directory(env=None):
    return forge_agent_directory(env) / "inference-leases"


def active_interactive_leases(env=None):
    directory = _lease_directory(env)
    if not directory.is_dir():
        return []
    now_ms = time.time() * 1000
    active = []
    for path in directory.glob("*.json"):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("kind", "interactive") == "interactive" and now_ms - float(row.get("updatedAtMs", 0)) <= LEASE_STALE_MS:
            active.append(row)
    return active


def wait_for_interactive_idle(scheduling, env=None):
    """Block until no interactive session is generating, then observe the grace
    period. Returns milliseconds waited."""
    started = time.monotonic()
    grace = max(0, scheduling.get("idleGraceMs", 0)) / 1000
    while True:
        while active_interactive_leases(env):
            time.sleep(0.2)
        if not grace:
            break
        time.sleep(grace)
        if not active_interactive_leases(env):
            break
    return int((time.monotonic() - started) * 1000)


def _write_background_lease(path, slot):
    run_state.atomic_write_text(
        path,
        json.dumps({"pid": os.getpid(), "kind": "background", "slot": slot, "updatedAtMs": int(time.time() * 1000)}) + "\n",
    )


def _acquire_background_lease(scheduling, env=None):
    """Claim the background slot, retrying if an interactive turn starts during
    the claim. Returns the lease path, or None if leases cannot be written.

    An unwritable agent directory costs cooperative scheduling, not the call:
    the work still runs, it just cannot announce itself.
    """
    directory = _lease_directory(env)
    while True:
        wait_for_interactive_idle(scheduling, env)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            lease = directory / f"background-{os.getpid()}-{threading.get_ident()}.json"
            _write_background_lease(lease, scheduling["backgroundSlot"])
        except OSError:
            return None
        if not active_interactive_leases(env):
            return lease
        lease.unlink(missing_ok=True)


def _post_simple(url, body, timeout, api_key):
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {api_key or 'local'}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise ChatError(f"chat endpoint returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise ChatError(f"chat endpoint request failed: {error.reason}") from error
    except json.JSONDecodeError as error:
        raise ChatError(f"chat endpoint returned invalid JSON: {error}") from error


def _post_preemptible(url, body, timeout, api_key, lease, scheduling, env=None):
    """POST on a worker thread so the socket can be closed the moment an
    interactive turn appears. Abandoning the request is not enough: the server
    would keep generating and keep holding the GPU."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ChatError(f"unsupported chat URL: {url}")
    connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    connection = connection_class(parsed.hostname, parsed.port, timeout=timeout)
    result = {}
    failure = {}

    def execute():
        try:
            request_path = parsed.path or "/"
            if parsed.query:
                request_path += f"?{parsed.query}"
            connection.request(
                "POST",
                request_path,
                body=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key or 'local'}"},
            )
            response = connection.getresponse()
            payload = response.read()
            if response.status >= 400:
                raise ChatError(f"chat endpoint returned HTTP {response.status}: {payload.decode('utf-8', errors='replace')[:500]}")
            result["payload"] = json.loads(payload)
        except BaseException as error:  # noqa: BLE001 - re-raised on the calling thread
            failure["error"] = error

    thread = threading.Thread(target=execute, daemon=True)
    thread.start()
    preempted = False
    last_refresh = time.monotonic()
    while thread.is_alive():
        thread.join(0.1)
        if time.monotonic() - last_refresh >= 1:
            _write_background_lease(lease, scheduling["backgroundSlot"])
            last_refresh = time.monotonic()
        if active_interactive_leases(env):
            preempted = True
            connection.close()
            break
    if preempted:
        thread.join(2)
        raise InterruptedError("background inference preempted by interactive activity")
    thread.join()
    connection.close()
    if failure:
        raise failure["error"]
    return result["payload"]


def call(
    service,
    messages,
    *,
    temperature=0,
    max_tokens=None,
    response_format=None,
    cache_prompt=True,
    background=False,
    session=None,
    timeout=DEFAULT_TIMEOUT,
    api_key=None,
    task=None,
    env=None,
):
    """Post one chat completion and return ``(content, record)``.

    ``background=True`` claims the service's background slot and yields the GPU
    to interactive turns, raising ``InterruptedError`` if one starts mid-request.
    Use it for verification against the thinking backend, which is the same
    server the interactive session is using.
    """
    scheduling = service["scheduling"]
    request = {
        "model": service["model"],
        "messages": messages,
        "temperature": temperature,
        "stream": False,
    }
    if session:
        request["user"] = session
    if response_format:
        request["response_format"] = response_format
    if cache_prompt:
        request["cache_prompt"] = True
    if max_tokens:
        request["max_tokens"] = max_tokens
    use_slot = background and scheduling.get("enabled")
    if use_slot:
        request["id_slot"] = scheduling["backgroundSlot"]
    body = json.dumps(request).encode("utf-8")

    lease = _acquire_background_lease(scheduling, env) if use_slot else None
    started = time.monotonic()
    try:
        if lease is not None:
            payload = _post_preemptible(service["url"], body, timeout, api_key, lease, scheduling, env)
        else:
            payload = _post_simple(service["url"], body, timeout, api_key)
    finally:
        if lease is not None:
            lease.unlink(missing_ok=True)
        elif use_slot:
            use_slot = False  # no lease was held, so do not report this as scheduled

    usage = payload.get("usage") or {}
    details = usage.get("prompt_tokens_details") or {}
    timings = payload.get("timings") or {}
    choices = payload.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    content = message.get("content")
    if not isinstance(content, str):
        raise ChatError("chat response did not contain choices[0].message.content")
    generated = timings.get("predicted_n", usage.get("completion_tokens"))
    hidden = hidden_token_count(generated, content)
    record = {
        "at": run_state.utc_now(),
        "event": "model_call",
        "task": task,
        "service": service["name"],
        "endpoint": service["url"],
        "model": service["model"],
        "mode": "background" if use_slot else "foreground",
        "slot": scheduling["backgroundSlot"] if use_slot else None,
        "promptTokens": usage.get("prompt_tokens"),
        "cachedTokens": details.get("cached_tokens", timings.get("cache_n")),
        "generatedTokens": generated,
        "hiddenTokens": hidden,
        "prefillMs": timings.get("prompt_ms"),
        "generationMs": timings.get("predicted_ms"),
        "elapsedMs": int((time.monotonic() - started) * 1000),
        "finishReason": choices[0].get("finish_reason") if choices else None,
        "reasoned": bool(message.get("reasoning_content") or message.get("reasoning"))
        or (hidden is not None and hidden > HIDDEN_TOKEN_MARGIN),
    }
    return content, record


def call_json(service, messages, **options):
    """``call`` with the response parsed as JSON. Returns ``(value, record)``."""
    content, record = call(service, messages, **options)
    try:
        return parse_json_content(content), record
    except json.JSONDecodeError as error:
        if record.get("finishReason") == "length":
            raise ChatError("chat response was truncated before valid JSON (raise max_tokens or split the input)") from error
        raise ChatError(f"chat response was not valid JSON: {error}") from error


def call_json_with_retry(service, messages, attempts=MAX_TRANSIENT_ATTEMPTS, **options):
    """``call_json`` retrying transient transport failures with backoff."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            return call_json(service, messages, **options)
        except InterruptedError:
            raise
        except (ChatError, OSError) as error:
            last_error = error
            if attempt < attempts and run_state.is_transient_failure(error):
                time.sleep(min(2.0 * attempt, 10.0))
                continue
            raise
    raise last_error


def served_models(service, timeout=5.0):
    """List the model ids a service actually serves, via ``GET /v1/models``."""
    root = service["url"].rsplit("/chat/completions", 1)[0]
    try:
        with urllib.request.urlopen(f"{root}/models", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as error:
        raise ChatError(f"could not list models at {root}/models: {error}") from error
    return [entry.get("id") for entry in (payload.get("data") or []) if entry.get("id")]


def service_doctor(service, expect_non_thinking=False, timeout=30.0):
    """Probe a service and report reachability, served model, and — for the batch
    service — whether it actually answers without thinking.

    A wrong model name is worth reporting: llama.cpp serves whatever is loaded
    regardless of the id sent, so a stale name stays invisible until someone
    points the same config at a server that validates it.
    """
    report = {"service": service["name"], "url": service["url"], "model": service["model"], "reachable": False}
    if not service["enabled"]:
        report["detail"] = "disabled in connectedServices"
        return report
    if service.get("fallback"):
        report["fallback"] = service["fallback"]
    try:
        available = served_models(service, timeout=min(timeout, 10.0))
        report["servedModels"] = available
        if available and service["model"] not in available:
            report["modelMismatch"] = True
            report["warning"] = f"configured model {service['model']!r} is not served here (available: {', '.join(available)})"
    except ChatError as error:
        report["warning"] = str(error)

    try:
        content, record = call(
            service,
            [{"role": "user", "content": "Reply with the single word: ready"}],
            timeout=timeout,
        )
    except (ChatError, InterruptedError, OSError) as error:
        report["detail"] = f"{type(error).__name__}: {error}"
        return report
    report["reachable"] = True
    report["elapsedMs"] = record["elapsedMs"]
    report["hiddenTokens"] = record["hiddenTokens"]
    thought = record["reasoned"] or bool(THINK_BLOCK_RE.match(content))
    report["thinking"] = thought
    if expect_non_thinking and thought:
        report["warning"] = (
            f"this endpoint is configured for bulk work but spent ~{record['hiddenTokens']} hidden "
            "reasoning tokens on a one-word reply; point connectedServices.chat at a non-thinking server"
        )
    report["detail"] = (
        f"reachable, thinking (~{record['hiddenTokens']} hidden tokens per call)" if thought else "reachable, non-thinking"
    )
    return report
