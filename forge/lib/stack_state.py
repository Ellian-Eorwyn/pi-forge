#!/usr/bin/env python3
"""Read-only client for the llm-stack state API.

The deployment behind ``llms`` publishes what it is actually running at
``http://llms:8078/api/v1/``. Without it, everything forge believes about the
backend is a constant someone measured once: the per-slot context window, the
number of slots a background call may pin, and which weights a port is serving.
This module turns those into facts read from the deployment.

**Strictly optional.** pi-forge installs on machines that have no such API. Every
function here returns ``None`` or an empty result when the stack cannot be read,
and every caller must carry on exactly as it did before. A stack that is down is
not an error, it is an absence of extra information.

**Never on the request path.** ``forge_llm.call`` does not consult this module.
Discovery happens where a person is already waiting: install, doctor, run start.

Resolving a forge URL to a backend is the one non-obvious part. ``backends[]``
entries are keyed by the backend's own port -- ``chat-primary`` is
``127.0.0.1:8010`` -- while forge talks to proxy ports (``:8004`` bulk chat,
``:8008`` thinking) that appear in no ``base_url`` and no ``probe.target``. The
snapshot's ``config["Ports"]`` block is what connects them::

    CHAT_BACKEND_PORT 8010   NOTHINK_PORT 8004   CODE_PORT 8008   THINK_PORT 8003
    EMBED_PORT 8005   EMBED2_PORT 8011   RERANK_PORT 8006   TASK_PORT 8007

So a lookup tries the backend's own port first, then falls back to that map.

Configuration:

- ``FORGE_STACK_STATE_URL`` overrides the base URL (default
  ``http://llms:8078``). Set it to the empty string to turn the integration off.
- ``FORGE_STACK_STATE_TOKEN`` is sent as a bearer token, for a deployment that
  sets ``LLM_API_TOKEN``. The API reports its own absence as an ``info`` alert.
- ``PI_FORGE_SKIP_STACK_DISCOVERY=1`` disables every read. Tests set it so the
  suite never depends on a host being up.
"""

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

# The live stack manager on this deployment answers on :8077 (both /api/config
# and the /api/v1 state contract read below); :8078 was stale and left every
# path-based identity check silently unchecked. Override with FORGE_STACK_STATE_URL.
DEFAULT_STACK_STATE_URL = "http://llms:8077"
API_PREFIX = "/api/v1"
# This client is written against the 1.x contract. A major bump may rename or
# restructure anything read below, and a wrong reading is worse than no reading:
# a bogus n_ctx_per_slot would be written into settings as though measured.
SUPPORTED_API_MAJOR = "1"
# Short, because an unreachable host must not make `configure-pi-forge` or a
# skill preflight feel hung. DNS failure returns immediately; this budget only
# matters for a host that accepts packets and never answers.
DEFAULT_TIMEOUT = 3.0
# One doctor pass probes chat, think, and embeddings. Without a cache that is
# three identical GETs, or three timeouts when the stack is down.
CACHE_TTL_SECONDS = 5.0

# Which backend serves a given port role. The `*2_*` roles belong to the
# secondary preset, which is a different backend rather than another profile in
# front of the same one.
PORT_ROLE_BACKENDS = {
    "CHAT_BACKEND_PORT": "chat-primary",
    "NOTHINK_PORT": "chat-primary",
    "CODE_PORT": "chat-primary",
    "THINK_PORT": "chat-primary",
    "CHAT2_BACKEND_PORT": "chat-secondary",
    "NOTHINK2_PORT": "chat-secondary",
    "CODE2_PORT": "chat-secondary",
    "THINK2_PORT": "chat-secondary",
    "EMBED_PORT": "embed",
    "EMBED2_PORT": "embed2",
    "RERANK_PORT": "rerank",
    "TASK_PORT": "task",
    "OCR_PORT": "ocr",
}

# Alerts at these levels describe conditions that change how a run behaves --
# a swapping host, a prompt cache too small to hold the working set. `info` is
# excluded: that is where the API's own "no token configured" notice lives, and
# repeating it on every batch report would train the reader to skip warnings.
REPORTABLE_ALERT_LEVELS = ("error", "warn")

_snapshot_cache = {}


class _Miss(object):
    """Sentinel for a cached failure, so a down stack is not re-probed per call."""


def _environment(env):
    return env if env is not None else os.environ


def resolve_stack_state(env=None, settings=None):
    """Resolve the state API to ``{enabled, baseUrl, token}``.

    Precedence matches every other connected service: environment, then the
    agent's ``connectedServices`` settings, then the built-in default.
    """
    environment = _environment(env)
    if _is_truthy(environment.get("PI_FORGE_SKIP_STACK_DISCOVERY")):
        return {"enabled": False, "baseUrl": "", "token": None}

    persisted = {}
    if isinstance(settings, dict):
        candidate = settings.get("stackState")
        if isinstance(candidate, dict):
            persisted = candidate

    # An env var set to the empty string turns the integration off for this
    # process, the same way FORGE_SEARXNG_URL="" disables search.
    if "FORGE_STACK_STATE_URL" in environment:
        base_url = (environment.get("FORGE_STACK_STATE_URL") or "").strip().rstrip("/")
    else:
        configured = persisted.get("baseUrl")
        base_url = (configured if isinstance(configured, str) else "").strip().rstrip("/")
        if not base_url:
            base_url = DEFAULT_STACK_STATE_URL

    enabled = bool(base_url) and persisted.get("enabled", True) is not False
    token = (environment.get("FORGE_STACK_STATE_TOKEN") or "").strip() or None
    if token is None:
        keys = settings.get("apiKeys") if isinstance(settings, dict) else None
        if isinstance(keys, dict) and isinstance(keys.get("stack-state"), str):
            token = keys["stack-state"].strip() or None
    return {"enabled": enabled, "baseUrl": base_url, "token": token}


def _is_truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _get_json(base_url, path, token, timeout):
    """GET one endpoint, or None for any reason at all.

    Deliberately total: a caller uses this to decide whether extra detail is
    available, never to decide whether to proceed.
    """
    request = urllib.request.Request("{0}{1}{2}".format(base_url, API_PREFIX, path), method="GET")
    request.add_header("Accept", "application/json")
    if token:
        request.add_header("Authorization", "Bearer {0}".format(token))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status >= 400:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    version = str(payload.get("api_version") or "")
    if version.split(".", 1)[0] != SUPPORTED_API_MAJOR:
        return None
    return payload


def health(env=None, settings=None, timeout=DEFAULT_TIMEOUT):
    """Whether the state API is up and speaking a version this client reads."""
    resolved = resolve_stack_state(env, settings)
    if not resolved["enabled"]:
        return False
    payload = _get_json(resolved["baseUrl"], "/health", resolved["token"], timeout)
    return bool(payload and payload.get("ok"))


def read_snapshot(env=None, settings=None, timeout=DEFAULT_TIMEOUT, refresh=False):
    """The whole stack state in one read, or None when it cannot be had.

    ``/api/v1/snapshot`` is used rather than the focused endpoints because the
    port map that resolves a forge URL to a backend lives only in its ``config``
    block, and stitching four responses together would race a restart anyway.

    Both outcomes are cached. Caching the failure is the point of the negative
    branch: a doctor pass over three services against a stack that is down
    should wait one timeout, not three.
    """
    resolved = resolve_stack_state(env, settings)
    if not resolved["enabled"]:
        return None
    key = resolved["baseUrl"]
    now = time.monotonic()
    if not refresh:
        cached = _snapshot_cache.get(key)
        if cached is not None and cached[0] > now:
            return None if cached[1] is _Miss else cached[1]
    payload = _get_json(key, "/snapshot", resolved["token"], timeout)
    _snapshot_cache[key] = (now + CACHE_TTL_SECONDS, _Miss if payload is None else payload)
    return payload


def clear_cache():
    """Drop the memoized snapshot. For tests, and for a long-lived process that
    wants a fresh reading after restarting something."""
    _snapshot_cache.clear()


def _port_of(url):
    """The TCP port a URL addresses, or None if it cannot be determined."""
    text = str(url or "").strip()
    if not text:
        return None
    if "//" not in text:
        text = "//" + text
    try:
        parsed = urllib.parse.urlsplit(text if "://" in text else "http:" + text)
        if parsed.port:
            return parsed.port
    except ValueError:
        return None
    return {"http": 80, "https": 443}.get(parsed.scheme)


def _port_map(snapshot):
    """Every ``*_PORT`` key in the config, as ``{port: role}``.

    Read from every config block rather than only ``Ports``: the secondary
    preset keeps its own port keys in its own block, and a role that moves
    between blocks should not silently stop resolving.
    """
    mapping = {}
    config = snapshot.get("config")
    if not isinstance(config, dict):
        return mapping
    for block in config.values():
        if not isinstance(block, dict):
            continue
        for key, value in block.items():
            if not key.endswith("_PORT"):
                continue
            try:
                mapping[int(str(value).strip())] = key
            except (TypeError, ValueError):
                continue
    return mapping


def _services_by_name(snapshot):
    services = snapshot.get("services")
    if not isinstance(services, list):
        return {}
    return {row.get("name"): row for row in services if isinstance(row, dict) and row.get("name")}


def _service_for_port(snapshot, port):
    """The service whose health probe targets this port -- the proxy, usually."""
    services = snapshot.get("services")
    if not isinstance(services, list) or port is None:
        return None
    for row in services:
        if not isinstance(row, dict):
            continue
        probe = row.get("probe")
        if isinstance(probe, dict) and _port_of(probe.get("target")) == port:
            return row
    return None


def backend_for_url(snapshot, url):
    """Resolve a configured endpoint to what is behind it.

    Returns ``{port, backend, unitService, portService, role}`` or None. The two
    services are different things and both matter: ``portService`` is what
    listens on the port forge dials (a proxy), ``unitService`` is the systemd
    unit actually holding the weights.
    """
    if not isinstance(snapshot, dict):
        return None
    port = _port_of(url)
    if port is None:
        return None
    backends = snapshot.get("backends")
    if not isinstance(backends, list):
        return None

    role = _port_map(snapshot).get(port)
    found = None
    # The backend's own port is the strongest signal and needs no config block:
    # embeddings on :8005 and the task model on :8007 resolve this way.
    for entry in backends:
        if isinstance(entry, dict) and _port_of(entry.get("base_url")) == port:
            found = entry
            break
    if found is None and role:
        wanted = PORT_ROLE_BACKENDS.get(role)
        for entry in backends:
            if isinstance(entry, dict) and entry.get("name") == wanted:
                found = entry
                break
    if found is None:
        return None

    services = _services_by_name(snapshot)
    # Router-managed backends carry no `unit` at all -- they are loaded on demand
    # rather than run as a systemd unit -- but they do have a service row under
    # their own name, and it holds the reason worth reporting.
    unit_service = services.get(found.get("unit")) or services.get(found.get("name"))
    return {
        "port": port,
        "role": role,
        "backend": found,
        "unitService": unit_service,
        "portService": _service_for_port(snapshot, port),
    }


def capacity_for_url(snapshot, url):
    """What one request may use at this endpoint, read from the deployment.

    ``contextTokens`` is the per-slot window rather than the pool: llama.cpp
    divides ``--ctx-size`` across ``--parallel`` slots, so a single request can
    never reach the total.
    """
    located = backend_for_url(snapshot, url)
    if located is None:
        return None
    props = located["backend"].get("props")
    if not isinstance(props, dict):
        return None
    context_tokens = props.get("n_ctx_per_slot")
    total_slots = props.get("total_slots")
    if not isinstance(context_tokens, int) or context_tokens <= 0:
        return None
    return {
        "contextTokens": context_tokens,
        "totalSlots": total_slots if isinstance(total_slots, int) and total_slots > 0 else None,
        "contextTotal": props.get("n_ctx_total"),
        "active": bool(located["backend"].get("active")),
        "isSleeping": bool(props.get("is_sleeping")),
        "backendName": located["backend"].get("name"),
    }


def identity_for_url(snapshot, url):
    """Which weights this endpoint is serving, and which binary is serving them.

    A model id proves nothing -- llama.cpp answers to whatever name it is sent,
    regardless of what is loaded -- so this reads the launched path, its
    quantization, and the llama.cpp build instead.
    """
    located = backend_for_url(snapshot, url)
    if located is None:
        return None
    backend = located["backend"]
    props = backend.get("props")
    if not isinstance(props, dict):
        return None
    identity = {
        "modelPath": props.get("model_path"),
        "modelAlias": props.get("model_alias"),
        "quant": props.get("model_ftype"),
        "buildInfo": props.get("build_info"),
        "backendName": backend.get("name"),
        "unit": backend.get("unit"),
    }
    return {key: value for key, value in identity.items() if value not in (None, "")} or None


def slots_for_url(snapshot, url):
    """The backend's slots, so a caller can check a slot number it means to pin."""
    located = backend_for_url(snapshot, url)
    if located is None:
        return None
    slots = located["backend"].get("slots")
    return slots if isinstance(slots, list) else None


# The router names the reranker `rank` where the backend list calls it `rerank`.
# Everything else agrees, so this is a spelling correction rather than a table.
ROUTER_IDS = {"rerank": "rank"}
# Router states meaning "these weights are not in VRAM right now". The router
# loads on demand, so neither is a fault -- but both explain a first call that
# times out where a later one succeeds.
ROUTER_ABSENT_STATES = {"unloaded", "sleeping"}
# Observed by probing :8005 while the embedding model was cold: the request
# itself moved the router from `unloaded` to `loading`, and the call timed out
# waiting. Worth its own sentence, because it is the one state where retrying
# the same call in a moment is exactly the right response.
ROUTER_LOADING_STATES = {"loading", "starting"}


def _router_state(snapshot, backend):
    """A router-managed model's load state, or None when it is not router-managed."""
    router = snapshot.get("router")
    if not isinstance(router, dict):
        return None
    models = router.get("models")
    if not isinstance(models, list):
        return None
    props = backend.get("props") if isinstance(backend.get("props"), dict) else {}
    name = backend.get("name")
    wanted = {name, ROUTER_IDS.get(name), props.get("model_alias")}
    wanted.discard(None)
    for entry in models:
        if isinstance(entry, dict) and entry.get("id") in wanted:
            return entry.get("state")
    return None


def explain_unreachable(snapshot, url):
    """Why an endpoint is not answering, in one sentence, or None.

    Returns None when the stack looks healthy -- the endpoint may still be
    failing for a reason the stack cannot see, and inventing an explanation
    would be worse than the transport error the caller already has.
    """
    located = backend_for_url(snapshot, url)
    if located is None:
        return None
    backend = located["backend"]
    port_service = located["portService"]
    unit_service = located["unitService"]

    if isinstance(port_service, dict) and port_service.get("state") != "active":
        return _stopped_sentence(port_service, "the service on port {0}".format(located["port"]))

    # A live proxy with nothing behind it: the connection is accepted and every
    # request fails, which reads as the model misbehaving rather than absent.
    if isinstance(port_service, dict):
        for upstream in port_service.get("upstreams") or []:
            if not isinstance(upstream, dict) or upstream.get("ok"):
                continue
            states = upstream.get("states")
            named = ", ".join("{0} is {1}".format(k, v) for k, v in sorted(states.items())) if isinstance(states, dict) else "none are running"
            return "{0} is running but has no live backend ({1})".format(
                port_service.get("label") or port_service.get("name"), named
            )

    # Checked before the unit state, because for these backends the unit is
    # stopped *by design* and the router is the thing that explains the call.
    router_state = _router_state(snapshot, backend)
    if router_state in ROUTER_LOADING_STATES:
        return "the model router is loading '{0}' right now; the call timed out waiting for weights, and retrying shortly should succeed".format(
            backend.get("name")
        )
    if router_state in ROUTER_ABSENT_STATES:
        return "the model router loads '{0}' on demand and it is not resident right now (router state: {1})".format(
            backend.get("name"), router_state
        )

    if isinstance(unit_service, dict) and unit_service.get("state") != "active":
        return _stopped_sentence(unit_service, "the backend serving {0}".format(backend.get("name")))

    if not backend.get("active"):
        return "the stack reports backend '{0}' as inactive".format(backend.get("name"))

    props = backend.get("props")
    if isinstance(props, dict) and props.get("is_sleeping"):
        return "backend '{0}' is sleeping and has to reload before it answers".format(backend.get("name"))

    for service in (port_service, unit_service):
        if not isinstance(service, dict):
            continue
        probe = service.get("probe")
        if isinstance(probe, dict) and probe.get("ok") is False:
            status = probe.get("http_status")
            suffix = " (HTTP {0})".format(status) if status else ""
            detail = probe.get("detail") or ""
            return "the stack's own probe of {0} is failing{1}{2}".format(
                probe.get("target") or service.get("name"), suffix, ": {0}".format(detail) if detail else ""
            )
    return None


def _stopped_sentence(service, role):
    """One sentence for a service that is not active.

    The stack writes its own ``reason`` for people ("held by the model router --
    the model loads on demand and is not run as a unit"), so it is quoted rather
    than reworded. ``expected`` separates the two cases that matter: a service
    that is off on purpose is a configuration answer, one that is off
    unexpectedly is a fault.
    """
    label = service.get("label") or service.get("name")
    state = service.get("state")
    reason = service.get("reason") or service.get("unit_state")
    intent = " (it is configured to be off)" if service.get("expected") == "off" and not reason else ""
    sentence = "{0}, {1}, is {2}".format(role, label, state)
    return "{0}: {1}".format(sentence, reason) if reason else sentence + intent


def health_alerts(snapshot, levels=REPORTABLE_ALERT_LEVELS):
    """The stack's own warnings, as structured rows."""
    if not isinstance(snapshot, dict):
        return []
    alerts = snapshot.get("alerts")
    if not isinstance(alerts, list):
        return []
    return [row for row in alerts if isinstance(row, dict) and row.get("level") in levels]


def health_warnings(snapshot, levels=REPORTABLE_ALERT_LEVELS):
    """The stack's own warnings, as sentences fit for a run report.

    The API already writes these for people ("Host swap is 97% used (7958
    MiB)."), so they are passed through rather than reworded.
    """
    warnings = []
    for row in health_alerts(snapshot, levels):
        text = row.get("text")
        if isinstance(text, str) and text.strip():
            warnings.append(text.strip())
    return warnings


def deployment_summary(snapshot):
    """A compact record of what was running, for a run's provenance.

    Deliberately a handful of named fields. The snapshot's ``config`` block is
    the deployment's environment and has no business being copied into an
    artifact.
    """
    if not isinstance(snapshot, dict):
        return None
    stack = snapshot.get("stack") if isinstance(snapshot.get("stack"), dict) else {}
    summary = {
        "readAt": snapshot.get("generated_at"),
        "hostname": stack.get("hostname"),
        "apiVersion": snapshot.get("api_version"),
    }
    return {key: value for key, value in summary.items() if value is not None} or None
