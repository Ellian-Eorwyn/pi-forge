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
- ``FORGE_BASE_CHAT_CONTEXT_TOKENS`` / ``FORGE_THINK_CONTEXT_TOKENS`` override the
  per-request context ceiling, for a service whose backend is smaller than the
  default slot.
- ``FORGE_BASE_CHAT_TEMPLATE_KWARGS`` / ``FORGE_THINK_TEMPLATE_KWARGS`` carry a
  JSON object forwarded as ``chat_template_kwargs``. A reasoning model reached
  this way needs ``{"enable_thinking": false}`` or it answers into a field this
  client does not read; see ``chatTemplateKwargs`` below.
"""

import base64
import http.client
import json
import mimetypes
import os
import re
import threading
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import run_state
import stack_state

# Mirrors SLOT_CONTEXT_TOKENS in connected-services.mjs. One llama-server runs
# with `--ctx-size 262144 --parallel 2` and llama.cpp splits the context evenly
# across slots, so a single request may use half the pool. `chat` and `think`
# are two profiles in front of that one server, not two servers, so they share
# both the slots and this ceiling.
SLOT_CONTEXT_TOKENS = 131072

# Two fields exist because a service is not always the deployment above. Pointing
# `chat` at a smaller model — the 9B on the swapping router, say — changes both
# how much fits and how the backend has to be asked to answer:
#
# - `contextTokens` is the ceiling the preflight check enforces. Left at the
#   default a skill would send a prompt this client believes fits, and read the
#   server's rejection as the model failing the task rather than the harness
#   overfilling it.
# - `chatTemplateKwargs` is forwarded verbatim as `chat_template_kwargs`. A
#   backend running `--reasoning-format deepseek` returns its reasoning in a
#   separate `reasoning_content` field and leaves `content` empty, so there is no
#   `<think>` block for THINK_BLOCK_RE to strip and nothing to parse; measured on
#   the task backend, a 16-token budget went entirely to reasoning. Sending
#   `{"enable_thinking": false}` makes it answer in two tokens like the
#   non-thinking profile does. `reasoning_budget: 0` and a `/no_think` suffix
#   were both tried against the same server and neither had any effect.
DEFAULT_SERVICES = {
    "chat": {
        "enabled": True,
        "url": "http://llms:8004/v1/chat/completions",
        "model": "chat",
        # The primary is served with an mmproj loaded, so this lane accepts images.
        # The bulk fan-out and the routing image guard read this: an image-bearing
        # item must never land on an `images: False` lane, where the agent's
        # transform layer would silently drop it to a placeholder.
        "images": True,
        "contextTokens": SLOT_CONTEXT_TOKENS,
        "chatTemplateKwargs": None,
        "reasoningEffort": None,
        "scheduling": {
            "enabled": True,
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
        "images": True,
        "contextTokens": SLOT_CONTEXT_TOKENS,
        "chatTemplateKwargs": None,
        "reasoningEffort": None,
        "scheduling": {
            "enabled": True,
            "interactiveSlot": 0,
            "backgroundSlot": 1,
            "idleGraceMs": 2000,
            "yieldMs": 1000,
            "backgroundOutputTokens": 4096,
        },
    },
    # A genuinely smaller model, for the stages measured to be *better* on one:
    # faithful cleanup of diarized speech and yes/no pair judgment, where the
    # answer is close to a copy of the input. It is not a cheaper `chat` — on
    # this deployment it is only ~12% faster per call across the real prompt mix,
    # and slower on long prompts, because it generates half again as many tokens.
    #
    # Off by default. Unlike `chat` and `think`, which are two request-shaping
    # profiles in front of one llama-server, this is a separate backend behind a
    # router at MODEL_ROUTER_MAX=1 shared with embed/ocr/rank, so a stage that
    # alternates with embeddings pays a model swap each time. An install that has
    # not deliberately configured it should never silently start paying that.
    "task": {
        "enabled": False,
        "url": "http://llms:8007/v1/chat/completions",
        "model": "task",
        "images": False,
        # Half the chat slot, and read off the live stack rather than assumed:
        # this was recorded as 32,768 for months after the backend moved, which
        # quietly under-budgeted every task-tier prompt.
        "contextTokens": 65538,
        # This backend reasons into `reasoning_content` and returns empty
        # `content` without it. `reasoning_budget: 0` and a `/no_think` suffix
        # were both tried against the same server and neither did anything.
        "chatTemplateKwargs": {"enable_thinking": False},
        "reasoningEffort": None,
        "scheduling": {
            "enabled": True,
            "interactiveSlot": 0,
            "backgroundSlot": 1,
            "idleGraceMs": 2000,
            "yieldMs": 1000,
            "backgroundOutputTokens": 4096,
        },
    },
    # The secondary backend `forge_delegate` offloads to. Off by default, so a
    # stock install falls back to `chat` and this is never touched. `scheduling`
    # is off on purpose: unlike chat/think it is a separate server, so there is no
    # shared prefix cache to protect by pinning, and it runs a single slot — an
    # `id_slot: 1` at a one-slot server is an out-of-range error. With scheduling
    # off the call sends no `id_slot`. `chatTemplateKwargs` mirrors `task`: the
    # secondary reasons into `reasoning_content` unless told not to think.
    "delegate": {
        "enabled": False,
        "url": "http://llms:8104/v1/chat/completions",
        # The id the secondary's non-thinking aggregate serves (its /v1/models
        # reports `chat`; the URL, not the name, selects the secondary backend).
        # `chat-custom2` is the stack's internal alias, not a servable id here.
        "model": "chat",
        # The secondary GPU runs without an mmproj (vision off to save VRAM).
        "images": False,
        "contextTokens": SLOT_CONTEXT_TOKENS,
        "chatTemplateKwargs": {"enable_thinking": False},
        "reasoningEffort": None,
        "scheduling": {
            "enabled": False,
            "interactiveSlot": 0,
            "backgroundSlot": 0,
            "idleGraceMs": 2000,
            "yieldMs": 1000,
            "backgroundOutputTokens": 4096,
        },
    },
    # GPU-2 bulk lane. The second full copy of the model on the other GPU, served
    # non-thinking on :8104 — the same endpoint `delegate` uses, but exposed as a
    # first-class bulk service so `bulk.lanes` can fan per-item batch work across
    # both GPUs at once. Off by default; a two-GPU setup enables it. Scheduling
    # off and `enable_thinking: False` for the same reasons as `delegate`, and
    # `images: False` — no mmproj — so the fan-out keeps image work off it.
    "chat2": {
        "enabled": False,
        "url": "http://llms:8104/v1/chat/completions",
        "model": "chat",
        "images": False,
        "contextTokens": SLOT_CONTEXT_TOKENS,
        "chatTemplateKwargs": {"enable_thinking": False},
        "reasoningEffort": None,
        "scheduling": {
            "enabled": False,
            "interactiveSlot": 0,
            "backgroundSlot": 0,
            "idleGraceMs": 2000,
            "yieldMs": 1000,
            "backgroundOutputTokens": 4096,
        },
    },
    # GPU-2 verify lane. The secondary's thinking configuration on :8108 (its code
    # port), the mirror of primary `think`->:8008. Pointing skill verification here
    # makes the reviewer a genuinely independent instance from the bulk producers,
    # and lets verify overlap with bulk instead of serializing on one GPU.
    # `chatTemplateKwargs` is None: unlike `chat2` this lane is meant to reason, and
    # :8108 returns its reasoning in visible content the way :8008 does. Off by
    # default; scheduling off (separate single-slot server); `images: False`.
    "think2": {
        "enabled": False,
        "url": "http://llms:8108/v1/chat/completions",
        "model": "code",
        "images": False,
        "contextTokens": SLOT_CONTEXT_TOKENS,
        "chatTemplateKwargs": None,
        "reasoningEffort": None,
        "scheduling": {
            "enabled": False,
            "interactiveSlot": 0,
            "backgroundSlot": 0,
            "idleGraceMs": 2000,
            "yieldMs": 1000,
            "backgroundOutputTokens": 4096,
        },
    },
}

SERVICE_URL_ENVIRONMENT = {
    "chat": ("FORGE_BASE_CHAT_URL", "FORGE_CHAT_URL"),
    "think": ("FORGE_THINK_URL",),
    "task": ("FORGE_TASK_URL",),
    "delegate": ("FORGE_DELEGATE_URL",),
    "chat2": ("FORGE_CHAT2_URL",),
    "think2": ("FORGE_THINK2_URL",),
}
SERVICE_MODEL_ENVIRONMENT = {
    "chat": ("FORGE_BASE_MODEL",),
    "think": ("FORGE_THINK_MODEL",),
    "task": ("FORGE_TASK_MODEL",),
    "delegate": ("FORGE_DELEGATE_MODEL",),
    "chat2": ("FORGE_CHAT2_MODEL",),
    "think2": ("FORGE_THINK2_MODEL",),
}
SERVICE_CONTEXT_ENVIRONMENT = {
    "chat": ("FORGE_BASE_CHAT_CONTEXT_TOKENS",),
    "think": ("FORGE_THINK_CONTEXT_TOKENS",),
    "task": ("FORGE_TASK_CONTEXT_TOKENS",),
    "delegate": ("FORGE_DELEGATE_CONTEXT_TOKENS",),
    "chat2": ("FORGE_CHAT2_CONTEXT_TOKENS",),
    "think2": ("FORGE_THINK2_CONTEXT_TOKENS",),
}
SERVICE_TEMPLATE_KWARGS_ENVIRONMENT = {
    "chat": ("FORGE_BASE_CHAT_TEMPLATE_KWARGS",),
    "think": ("FORGE_THINK_TEMPLATE_KWARGS",),
    "task": ("FORGE_TASK_TEMPLATE_KWARGS",),
    "delegate": ("FORGE_DELEGATE_TEMPLATE_KWARGS",),
    "chat2": ("FORGE_CHAT2_TEMPLATE_KWARGS",),
    "think2": ("FORGE_THINK2_TEMPLATE_KWARGS",),
}
SERVICE_REASONING_EFFORT_ENVIRONMENT = {
    "chat": ("FORGE_BASE_CHAT_REASONING_EFFORT",),
    "think": ("FORGE_THINK_REASONING_EFFORT",),
    "task": ("FORGE_TASK_REASONING_EFFORT",),
    "delegate": ("FORGE_DELEGATE_REASONING_EFFORT",),
    "chat2": ("FORGE_CHAT2_REASONING_EFFORT",),
    "think2": ("FORGE_THINK2_REASONING_EFFORT",),
}

# A thinking backend that was asked not to think can still emit a stray block.
# Strip it defensively everywhere rather than trusting any one server's config.
THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
DEFAULT_TIMEOUT = 600.0
MAX_TRANSIENT_ATTEMPTS = 3
# A background call preempted by an interactive turn is retried a few times rather
# than failing the whole run on the first turn. Re-acquiring the background lease
# waits for the interactive burst to pass, so each retry is a fresh windowed attempt;
# only when every attempt is preempted does the InterruptedError propagate, for the
# skill to turn into a resume hint.
PREEMPT_MAX_RETRIES = 3
LEASE_STALE_MS = 15000
# Reasoning is invisible in the response body: llama.cpp strips the think block
# server-side and reports no reasoning_content, so the only evidence is the
# token count. Measured on this deployment, the thinking server spends ~410
# hidden tokens answering a question the non-thinking one answers in 2 — even
# for a one-word reply. Anything well past the visible content is reasoning.
HIDDEN_TOKEN_MARGIN = 32
CHARACTERS_PER_TOKEN = 3.0
# Deliberately different from CHARACTERS_PER_TOKEN above. That one deflates the
# visible-content estimate so hidden reasoning stands out; this one is the
# density actually measured on this model — project-extraction calibrates
# against observed promptTokens and converges near 3.42 — and is used to decide
# whether a prompt fits a slot.
PROMPT_CHARACTERS_PER_TOKEN = 3.42
# What one image contributes to the prompt-fit preflight. There is no character
# count to divide, and the real figure is tokenizer- and tiling-specific (a
# dynamic-tiling vision model spends more on a large image than a small one), so
# this is a single conservative upper-ish estimate rather than a measurement.
# Its only job is to keep estimate_prompt_tokens from budgeting an image at zero
# and letting an image-bearing prompt sail past the check into a server refusal.
IMAGE_TOKENS_ESTIMATE = 1600
# The formats the vision backend and the interactive agent's reader both accept.
_ACCEPTED_IMAGE_MIMES = ("image/png", "image/jpeg", "image/gif", "image/webp")


# Which service URLs have already had their backend identity written to a
# record in this process. A 500-item batch should say which weights it ran
# against once, not five hundred times.
_reported_conditions = set()
_conditions_lock = threading.Lock()


def stack_conditions(service, env=None):
    """Backend identity and stack warnings to attach to this call's record, once.

    Skills journal every ``model_call`` record into their run directory, so this
    is how a run comes to say which weights produced its output and what shape
    the host was in at the time. A batch that crawled because the inference host
    was at 97% swap should read that way in its own journal, rather than looking
    like the model got slow.

    Returns ``None`` on every call after the first for a given endpoint, and on
    any install where the state API is absent. The snapshot read is cached and
    happens at most once per endpoint per process — the preflight that every
    batch skill already runs usually warms it, so by the time real work starts
    this costs nothing.
    """
    url = service["url"]
    with _conditions_lock:
        if url in _reported_conditions:
            return None
        _reported_conditions.add(url)
    snapshot = stack_state.read_snapshot(env=env)
    if snapshot is None:
        return None
    conditions = {}
    identity = stack_state.identity_for_url(snapshot, url)
    if identity:
        conditions["backend"] = identity
    warnings = stack_state.health_warnings(snapshot)
    if warnings:
        conditions["stackWarnings"] = warnings
    return conditions or None


def reset_stack_conditions():
    """Forget which endpoints have been reported. For tests."""
    with _conditions_lock:
        _reported_conditions.clear()


def hidden_token_count(generated_tokens, content):
    """Tokens generated beyond what the visible content can account for."""
    if not isinstance(generated_tokens, (int, float)):
        return None
    visible = len(str(content or "")) / CHARACTERS_PER_TOKEN
    return max(0, int(generated_tokens - visible))


def estimate_prompt_tokens(messages):
    """Approximate the prompt size of a message list, in tokens.

    Character density is a run-to-run estimate, not a tokenizer, so this is only
    accurate enough to catch a prompt that cannot possibly fit. Image parts have
    no text to count, so each adds a flat ``IMAGE_TOKENS_ESTIMATE`` instead of the
    zero the character path would give them.
    """
    characters = 0
    image_tokens = 0
    for message in messages or ():
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            characters += len(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("text"), str):
                    characters += len(part["text"])
                elif part.get("type") == "image_url":
                    image_tokens += IMAGE_TOKENS_ESTIMATE
        elif content is not None:
            characters += len(str(content))
    return int(characters / PROMPT_CHARACTERS_PER_TOKEN) + image_tokens


def _sniff_image_mime(data, path):
    """Best-effort image MIME from magic bytes, falling back to the extension.

    Sniffing beats trusting the suffix — a screenshot saved as ``.img`` is still a
    PNG to the backend — but a truncated or odd file can still name its type, so
    the extension is the fallback before giving up.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    guessed, _ = mimetypes.guess_type(str(path))
    if guessed in _ACCEPTED_IMAGE_MIMES:
        return guessed
    raise ValueError(f"{path!r} is not a supported image (need PNG, JPEG, GIF, or WEBP)")


def image_content_part(path):
    """Load an image file into an OpenAI ``image_url`` content part.

    Returns ``{"type": "image_url", "image_url": {"url": "data:<mime>;base64,…"}}``,
    the shape llama.cpp's OpenAI-compatible endpoint expects for a multimodal
    request. The bytes are inlined as a data URI rather than a link because the
    inference host cannot fetch from wherever a skill happens to be running.

    Accepts PNG, JPEG, GIF, and WEBP; raises ``ValueError`` for anything else so a
    caller learns at build time rather than from a server refusal.
    """
    data = Path(path).read_bytes()
    mime = _sniff_image_mime(data, path)
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def image_message(prompt, images, *, role="user"):
    """Build one chat message carrying ``prompt`` text plus one or more images.

    ``images`` is a single path or an iterable of paths. The result is one message
    whose ``content`` is a parts list — the text followed by an image part per file
    — ready to drop straight into the ``messages`` list ``call`` takes::

        forge_llm.call(service, [forge_llm.image_message("Describe this.", path)])

    Any vision-capable service works; on this deployment the primary backend behind
    ``chat``/``think`` reports ``vision: true``, so no separate service is needed.
    """
    if isinstance(images, (str, os.PathLike)):
        images = [images]
    content = [{"type": "text", "text": prompt}]
    content.extend(image_content_part(path) for path in images)
    return {"role": role, "content": content}


class ChatError(RuntimeError):
    """A chat endpoint could not be reached or returned an unusable response."""


class ContextBudgetError(ChatError):
    """A prompt could not fit the slot it would have run in.

    Subclasses ChatError so existing handlers keep working, while callers that
    know how to split their input can catch this specifically and do so.
    """


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


def _positive_int(value):
    """A positive integer, or None for anything that is not one."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return None
    return number if number > 0 else None


def _reasoning_effort(value):
    """A graded reasoning-effort string, or None. Kept as a bare string rather
    than validated against a fixed set: the shaping proxy owns which levels it
    understands (`none`/`low`/`medium`/`xhigh` here, `high`/`max` elsewhere), and
    a value it does not recognise is its error to report, not this client's to
    swallow. An empty string means unset, matching how the template kwargs read."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _template_kwargs(value):
    """A ``chat_template_kwargs`` object, from a mapping or a JSON string."""
    if isinstance(value, dict):
        return dict(value) or None
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return dict(parsed) if isinstance(parsed, dict) and parsed else None
    return None


def resolve_service(name, base_url=None, model=None, env=None, settings=None):
    """Resolve a named service to ``{enabled, url, model, contextTokens,
    chatTemplateKwargs, scheduling}``.

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

    environment_context = next(
        (environment[key] for key in SERVICE_CONTEXT_ENVIRONMENT[name] if environment.get(key)), None
    )
    resolved_context = (
        _positive_int(environment_context)
        or _positive_int(persisted.get("contextTokens"))
        or defaults["contextTokens"]
    )
    environment_template = next(
        (environment[key] for key in SERVICE_TEMPLATE_KWARGS_ENVIRONMENT[name] if environment.get(key)), None
    )
    resolved_template = (
        _template_kwargs(environment_template)
        or _template_kwargs(persisted.get("chatTemplateKwargs"))
        or defaults["chatTemplateKwargs"]
    )
    environment_effort = next(
        (environment[key] for key in SERVICE_REASONING_EFFORT_ENVIRONMENT[name] if environment.get(key)), None
    )
    resolved_effort = (
        _reasoning_effort(environment_effort)
        or _reasoning_effort(persisted.get("reasoningEffort"))
        or defaults.get("reasoningEffort")
    )

    persisted_scheduling = persisted.get("scheduling") if isinstance(persisted.get("scheduling"), dict) else {}
    scheduling = {**defaults["scheduling"]}
    for key, value in persisted_scheduling.items():
        if key in scheduling and isinstance(value, type(scheduling[key])):
            scheduling[key] = value
    persisted_images = persisted.get("images")
    resolved_images = persisted_images if isinstance(persisted_images, bool) else defaults["images"]
    return {
        "name": name,
        "enabled": bool(persisted.get("enabled", defaults["enabled"])),
        "url": resolved_url,
        "model": resolved_model,
        "images": resolved_images,
        "contextTokens": resolved_context,
        "chatTemplateKwargs": resolved_template,
        "reasoningEffort": resolved_effort,
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


# Where each service's endpoint lands on a command's parsed arguments. Skills
# resolve once in ``parse_args`` and write the result back onto these, so a
# rebuild later has to read the same names to pick the resolution up again.
def resolve_task_or_chat(base_url=None, model=None, env=None, settings=None):
    """The service to use for a stage measured better on a small model, falling
    back to ``chat`` when no task backend is configured.

    The fallback direction is deliberate and matches ``resolve_think_or_chat``:
    an unconfigured tier degrades *toward* the 27B, never away from it. A stage
    routed here is one a small model does better, but "better" was measured
    against `chat`, so `chat` is always an acceptable answer — whereas silently
    demoting `chat` work to a 4B is a quality regression nobody asked for.
    """
    task = resolve_service("task", base_url=base_url, model=model, env=env, settings=settings)
    if task["enabled"] and task["url"]:
        return task
    fallback = resolve_service("chat", env=env, settings=settings)
    fallback["name"] = "task"
    fallback["fallback"] = "chat"
    return fallback


def resolve_delegate_or_chat(base_url=None, model=None, env=None, settings=None):
    """The delegation target, falling back to ``chat`` when no secondary is
    configured — the default.

    Enabled means a second backend on another GPU that a delegated investigation
    runs on in parallel; disabled means it runs on the primary ``chat`` weights,
    the way it did before a secondary was possible. As with the other tiers the
    fallback degrades *toward* the always-present ``chat`` so the tool is never
    unavailable for lack of a secondary.
    """
    delegate = resolve_service("delegate", base_url=base_url, model=model, env=env, settings=settings)
    if delegate["enabled"] and delegate["url"]:
        return delegate
    fallback = resolve_service("chat", env=env, settings=settings)
    fallback["name"] = "delegate"
    fallback["fallback"] = "chat"
    return fallback


def resolve_verify_or_think_or_chat(base_url=None, model=None, env=None, settings=None):
    """The service skill verification/review runs on.

    Prefers the ``connectedServices.verify.service`` lane — normally ``think2`` on
    the second GPU, a genuinely independent instance from the bulk producers — and
    degrades to the primary thinking lane, then ``chat``, exactly the way
    ``resolve_think_or_chat`` does, so a setup without a secondary still verifies.
    The result is named ``verify``; when it landed somewhere other than the
    requested lane it carries ``fallback`` so a run can journal the degrade.
    """
    configured = settings if settings is not None else load_connected_services(env)
    verify = configured.get("verify") if isinstance(configured.get("verify"), dict) else {}
    wanted = verify.get("service") if isinstance(verify.get("service"), str) else None
    # An explicit endpoint (e.g. --think-url) wins over the configured verify lane,
    # matching the module's precedence that an explicit argument beats settings.
    if not base_url and wanted in DEFAULT_SERVICES:
        candidate = resolve_service(wanted, env=env, settings=configured)
        if candidate["enabled"] and candidate["url"]:
            candidate["name"] = "verify"
            return candidate
    think = resolve_think_or_chat(base_url=base_url, model=model, env=env, settings=configured)
    landed = "chat" if think.get("fallback") == "chat" else "think"
    result = dict(think)
    result["name"] = "verify"
    if wanted and wanted != landed:
        result["fallback"] = landed
    elif not wanted and landed == "chat":
        result["fallback"] = "chat"
    else:
        result.pop("fallback", None)
    return result


def resolve_bulk_lanes(env=None, settings=None, carries_image=False):
    """The list of resolved services a batch skill fans per-item bulk work across.

    Reads ``connectedServices.bulk.lanes`` (a list of service names, default
    ``["chat"]``). Disabled lanes drop out; when ``carries_image`` is true every
    text-only (``images: False``) lane drops too, so an image-bearing batch can
    only run on GPU-1 vision lanes — never on a ``:8104``/``:8108`` lane where the
    image would be silently dropped. Always returns at least the ``chat`` lane, so
    a misconfiguration degrades to single-lane rather than to nothing.
    """
    configured = settings if settings is not None else load_connected_services(env)
    bulk = configured.get("bulk") if isinstance(configured.get("bulk"), dict) else {}
    raw = bulk.get("lanes")
    names = []
    if isinstance(raw, list):
        for value in raw:
            if isinstance(value, str) and value in DEFAULT_SERVICES and value not in names:
                names.append(value)
    if not names:
        names = ["chat"]
    lanes = []
    for name in names:
        service = resolve_service(name, env=env, settings=configured)
        if not (service["enabled"] and service["url"]):
            continue
        if carries_image and service.get("images") is False:
            continue
        lanes.append(service)
    if not lanes:
        chat = resolve_service("chat", env=env, settings=configured)
        if chat["enabled"] and chat["url"] and not (carries_image and chat.get("images") is False):
            lanes.append(chat)
    return lanes


SERVICE_ARGUMENT_NAMES = {
    "chat": ("base_url", "model"),
    "think": ("think_url", "think_model"),
    "task": ("task_url", "task_model"),
}


def service_from_args(args, name="chat", env=None, settings=None):
    """A fully resolved service for ``name``, cached on ``args``.

    Skills resolve their endpoint once at parse time and then rebuild a service
    dict wherever one is needed, often inside a per-item loop. Rebuilt by hand
    that dict carried only ``url``, ``model`` and ``scheduling``, so
    ``contextTokens`` and ``chatTemplateKwargs`` were silently dropped and
    ``call`` fell back to a 131,072-token ceiling with no template kwargs.

    Both losses are invisible until the service points somewhere other than the
    deployment those defaults describe. Then the preflight passes a prompt at
    twice the backend's real limit and the server's rejection reads as the model
    failing the task, and a backend that reasons into ``reasoning_content``
    returns empty ``content`` with nothing to parse. Those are the two failure
    modes ``evals/registry.py`` exists to keep out of the registry, and they were
    reachable from four skills at once.

    The result is cached on ``args`` because callers are hot loops and
    ``resolve_service`` reads ``settings.json`` on every call.
    """
    attribute = f"_forge_service_{name}"
    cached = getattr(args, attribute, None)
    if cached is not None:
        return cached
    url_name, model_name = SERVICE_ARGUMENT_NAMES.get(name, (None, None))
    resolved = resolve_service(
        name,
        base_url=getattr(args, url_name, None) if url_name else None,
        model=getattr(args, model_name, None) if model_name else None,
        env=env,
        settings=settings,
    )
    try:
        setattr(args, attribute, resolved)
    except AttributeError:
        # A caller passing something that will not take an attribute still gets
        # a correct service; it just pays the resolution every time.
        pass
    return resolved


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


def _post_preemptible(url, body, timeout, api_key, lease, scheduling, env=None, allow_preemption=True):
    """POST on a worker thread so the socket can be closed the moment an
    interactive turn appears. Abandoning the request is not enough: the server
    would keep generating and keep holding the GPU.

    ``allow_preemption=False`` still claims the slot and refreshes the lease but
    sees the call through. A short diagnostic probe wants the slot without
    being abandoned by the first interactive turn that arrives."""
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
        if allow_preemption and active_interactive_leases(env):
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
    allow_preemption=True,
    session=None,
    timeout=DEFAULT_TIMEOUT,
    api_key=None,
    task=None,
    env=None,
    reasoning_effort=None,
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
    template_kwargs = service.get("chatTemplateKwargs")
    if template_kwargs:
        request["chat_template_kwargs"] = template_kwargs
    # Graded reasoning effort (Qwen 3.8+). Sent as the top-level OpenAI field,
    # which the shaping proxy reads and normalises; it wins over any
    # chat_template_kwargs.reasoning_effort. "none" turns thinking off for the
    # request, "low"/"medium"/"xhigh" set depth. A per-call value wins over the
    # service default, which is how escalation forces `xhigh` on one item without
    # re-resolving the service. Only a template that reads it honours it — verify
    # with backend-check before trusting a run.
    effort = reasoning_effort if reasoning_effort is not None else service.get("reasoningEffort")
    if effort:
        request["reasoning_effort"] = effort
    use_slot = background and scheduling.get("enabled")
    if use_slot:
        request["id_slot"] = scheduling["backgroundSlot"]
    # Refuse a prompt that cannot fit before uploading it and taking a lease.
    # llama.cpp does reject it too, quickly and with the numbers
    # ("exceeds the available context size (131072 tokens)"), but its advice —
    # "try increasing it" — is wrong here: the context is fixed by the
    # deployment, and the knob a skill actually has is how much it sends.
    context_tokens = service.get("contextTokens") or SLOT_CONTEXT_TOKENS
    estimated_prompt = estimate_prompt_tokens(messages)
    reserved_output = max_tokens or 0
    if estimated_prompt + reserved_output > context_tokens:
        raise ContextBudgetError(
            f"prompt is about {estimated_prompt} tokens and reserves {reserved_output} for output, "
            f"over the {context_tokens}-token limit on service {service['name']!r}. Send less text "
            f"per call (lower the skill's packet or chunk size), or lower max_tokens."
        )
    body = json.dumps(request).encode("utf-8")

    started = time.monotonic()
    preempt_attempt = 0
    while True:
        lease = _acquire_background_lease(scheduling, env) if use_slot else None
        try:
            if lease is not None:
                payload = _post_preemptible(service["url"], body, timeout, api_key, lease, scheduling, env, allow_preemption)
            else:
                payload = _post_simple(service["url"], body, timeout, api_key)
            break
        except InterruptedError:
            # Preempted mid-request. Re-acquiring the lease below waits for the
            # interactive burst to pass before the next attempt, so transient activity
            # costs a retry rather than the run; give up only after a few windows.
            preempt_attempt += 1
            if preempt_attempt > PREEMPT_MAX_RETRIES:
                raise
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
    conditions = stack_conditions(service, env)
    if conditions:
        record.update(conditions)
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


def dispatch_bulk(
    lanes,
    items,
    run_one,
    *,
    concurrency_per_lane=1,
    carries_image=None,
    item_id=None,
    on_result=None,
    on_error=None,
    progress=None,
    max_lane_failures=3,
):
    """Fan a batch skill's per-item bulk work across several inference lanes at once.

    The twin of ``dispatchBulk`` in ``forge-llm.mjs``. ``lanes`` is the shaped
    service list from ``resolve_bulk_lanes`` — with two GPUs it is ``[chat, chat2]``,
    so items run on both GPUs in parallel; with one it is ``[chat]`` and this behaves
    like the old serial loop. ``run_one(lane, item, index)`` issues the model call
    for one item on the lane it is handed, so the byte-stable system prefix stays
    per-lane and each lane's server prefix cache stays warm; this function never puts
    two prompt shapes through one lane, and runs ``concurrency_per_lane`` requests
    per lane at a time (one per GPU by default).

    Image-bearing items (``carries_image(item)``) are only ever handed to a vision
    lane; an image item with no vision lane configured is a hard error up front, so
    an image never reaches a text-only ``:8104``/``:8108`` lane where it would be
    silently dropped. A lane that raises requeues the item elsewhere and, after
    ``max_lane_failures`` consecutive failures, drops out; if every capable lane
    dies with work left this raises. ``on_result(item, result, lane, index)`` is the
    caller's journal commit and runs before that worker takes its next item, so
    resume state never runs ahead of committed work — and it is serialized under the
    dispatcher's lock, so the caller's journal append needs no lock of its own.

    Returns ``{"results": [...], "producedBy": {...}}``: ``results`` indexed by item
    order (a failed item is ``{"__dispatch_error": error}``), and ``producedBy``
    keyed by ``item_id(item, index)`` mapping to ``{"url", "model"}`` for the
    verifier's per-item independence check.
    """
    if not lanes:
        raise ChatError("dispatch_bulk needs at least one lane")
    items = list(items)
    total = len(items)
    results = [None] * total
    produced_by = {}
    carries = [bool(carries_image(items[i], i)) if carries_image else False for i in range(total)]
    if any(carries) and not any(lane.get("images") is not False for lane in lanes):
        raise ChatError("an item carries an image but no configured bulk lane accepts images")
    lock = threading.Lock()
    text_queue = deque(i for i in range(total) if not carries[i])
    vision_queue = deque(i for i in range(total) if carries[i])
    attempts = [0] * total
    max_item_attempts = max(2, len(lanes) + 1)
    state = {"remaining": total, "in_flight": 0}

    def claim(lane):
        # Caller holds the lock. A vision lane prefers the vision-only work only it
        # can serve, then the shared text queue; a text-only lane serves text alone.
        if lane.get("images") is not False and vision_queue:
            return vision_queue.popleft()
        if text_queue:
            return text_queue.popleft()
        return -1

    def worker(lane):
        consecutive = 0
        while True:
            with lock:
                index = claim(lane)
                if index == -1:
                    if state["in_flight"] == 0:
                        return
                    idle = True
                else:
                    idle = False
                    attempts[index] += 1
                    state["in_flight"] += 1
            if idle:
                # Work is still in flight and may be requeued on failure; wait and
                # re-check rather than exiting this lane early.
                time.sleep(0.005)
                continue
            item = items[index]
            try:
                result = run_one(lane, item, index)
            except Exception as error:  # noqa: BLE001 - lane isolation is the point
                consecutive += 1
                decision = on_error(item, error, lane, index) if on_error else "retry"
                with lock:
                    state["in_flight"] -= 1
                    if decision == "fail" or attempts[index] >= max_item_attempts:
                        results[index] = {"__dispatch_error": error}
                        state["remaining"] -= 1
                        done = total - state["remaining"]
                    else:
                        (vision_queue if carries[index] else text_queue).append(index)
                        done = None
                if progress and done is not None:
                    progress({"done": done, "total": total, "lane": lane.get("name"), "index": index, "error": error})
                if consecutive >= max_lane_failures:
                    return
                continue
            consecutive = 0
            with lock:
                state["in_flight"] -= 1
                results[index] = result
                state["remaining"] -= 1
                if item_id is not None:
                    produced_by[item_id(item, index)] = {"url": lane["url"], "model": lane["model"]}
                if on_result:
                    on_result(item, result, lane, index)
                done = total - state["remaining"]
            if progress:
                progress({"done": done, "total": total, "lane": lane.get("name"), "index": index})

    threads = []
    for lane in lanes:
        for _ in range(max(1, concurrency_per_lane)):
            thread = threading.Thread(target=worker, args=(lane,), daemon=True)
            thread.start()
            threads.append(thread)
    for thread in threads:
        thread.join()
    if state["remaining"] > 0:
        raise ChatError(
            f"bulk fan-out could not complete {state['remaining']} of {total} item(s): every capable lane failed"
        )
    return {"results": results, "producedBy": produced_by}


def served_models(service, timeout=5.0):
    """List the model ids a service actually serves, via ``GET /v1/models``."""
    root = service["url"].rsplit("/chat/completions", 1)[0]
    try:
        with urllib.request.urlopen(f"{root}/models", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as error:
        raise ChatError(f"could not list models at {root}/models: {error}") from error
    return [entry.get("id") for entry in (payload.get("data") or []) if entry.get("id")]


def doctor_warnings(report):
    """Every warning in a ``service_doctor`` report, in reporting order.

    The report carries several independent warnings under separate keys, because
    a single ``warning`` field means the last writer silently wins. Callers that
    surface warnings to a person should read this rather than one key.
    """
    # `stackDetail` is deliberately absent: it is already folded into `detail`,
    # and a caller printing both would say the same thing twice.
    keys = ("warning", "contextWarning", "slotWarning")
    return [report[key] for key in keys if isinstance(report.get(key), str) and report[key].strip()]


def _describe_backend(report, snapshot, service):
    """Add what the stack knows about this endpoint to a doctor report.

    Three separate findings, each independently useful:

    - ``backend`` names the weights, quantization, and llama.cpp build. The model
      id in the config cannot do that, because llama.cpp answers to whatever name
      it is sent regardless of what is loaded.
    - ``contextMismatch`` catches a configured ceiling that no longer matches the
      slot. Too high and a skill sends a prompt this client believes fits, then
      reads the server's rejection as the model failing the task.
    - ``slotWarning`` catches pinning a slot that does not exist. Nothing else
      checks this: a deployment moving to ``--parallel 1`` leaves every
      background call in forge naming slot 1 forever.

    Each finding gets its own key rather than sharing ``warning``, which the
    probe below overwrites. ``doctor_warnings`` collects them all.
    """
    if snapshot is None:
        return
    identity = stack_state.identity_for_url(snapshot, service["url"])
    if identity:
        report["backend"] = identity
    capacity = stack_state.capacity_for_url(snapshot, service["url"])
    if not capacity:
        return
    report["servedContextTokens"] = capacity["contextTokens"]
    configured = service.get("contextTokens") or SLOT_CONTEXT_TOKENS
    if configured != capacity["contextTokens"]:
        report["contextMismatch"] = True
        report["contextWarning"] = (
            f"this service is configured for {configured} context tokens but the backend serves "
            f"{capacity['contextTokens']} per slot; run the installer to re-read the deployment, or set "
            "contextTokens in connectedServices"
        )
    scheduling = service.get("scheduling") or {}
    total_slots = capacity.get("totalSlots")
    background_slot = scheduling.get("backgroundSlot")
    if scheduling.get("enabled") and isinstance(total_slots, int) and isinstance(background_slot, int):
        if background_slot >= total_slots:
            report["slotWarning"] = (
                f"background work pins slot {background_slot} but the backend runs {total_slots} "
                f"slot{'s' if total_slots != 1 else ''}; every background call is naming a slot that does not exist"
            )


def service_doctor(service, expect_non_thinking=False, timeout=30.0, env=None):
    """Probe a service and report reachability, served model, and — for the batch
    service — whether it actually answers without thinking.

    A wrong model name is worth reporting: llama.cpp serves whatever is loaded
    regardless of the id sent, so a stale name stays invisible until someone
    points the same config at a server that validates it.

    Where the deployment publishes a state API, two more things are reported: the
    weights actually behind the endpoint (a model id proves nothing, a launched
    path does), and — when the probe fails — why, instead of a bare transport
    error. Both are absent on an install without that API, and nothing else
    changes when they are.
    """
    report = {
        "service": service["name"],
        "url": service["url"],
        "model": service["model"],
        "contextTokens": service.get("contextTokens") or SLOT_CONTEXT_TOKENS,
        "chatTemplateKwargs": service.get("chatTemplateKwargs"),
        "reachable": False,
    }
    if not service["enabled"]:
        report["detail"] = "disabled in connectedServices"
        return report
    if service.get("fallback"):
        report["fallback"] = service["fallback"]
    snapshot = stack_state.read_snapshot(env=env)
    _describe_backend(report, snapshot, service)
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
        # The transport error says the call did not land. The stack says why,
        # which is the difference between "connection refused" and "the primary
        # proxy is up but every backend behind it is stopped".
        explanation = stack_state.explain_unreachable(snapshot, service["url"])
        if explanation:
            report["stackDetail"] = explanation
            report["detail"] = f"{report['detail']} — {explanation}"
        return report
    report["reachable"] = True
    report["elapsedMs"] = record["elapsedMs"]
    report["hiddenTokens"] = record["hiddenTokens"]
    thought = record["reasoned"] or bool(THINK_BLOCK_RE.match(content))
    report["thinking"] = thought
    # An empty reply that still burned tokens is the one failure mode worth
    # naming outright: the backend reasoned into `reasoning_content`, which this
    # client never reads, so nothing arrives to parse and every JSON-expecting
    # skill fails on a response the server reported as successful.
    generated = record.get("generatedTokens") or 0
    if not content.strip() and generated > 0:
        report["emptyContent"] = True
        report["warning"] = (
            f"the endpoint generated ~{generated} tokens but returned empty content: it is reasoning "
            "into a field this client does not read. Set chatTemplateKwargs to "
            '{"enable_thinking": false} for this service (connectedServices, or '
            "FORGE_BASE_CHAT_TEMPLATE_KWARGS / FORGE_THINK_TEMPLATE_KWARGS)."
        )
        report["detail"] = "reachable, but answers with no visible content"
        return report
    if expect_non_thinking and thought:
        report["warning"] = (
            f"this endpoint is configured for bulk work but spent ~{record['hiddenTokens']} hidden "
            "reasoning tokens on a one-word reply; point connectedServices.chat at a non-thinking server"
        )
    report["detail"] = (
        f"reachable, thinking (~{record['hiddenTokens']} hidden tokens per call)" if thought else "reachable, non-thinking"
    )
    return report
