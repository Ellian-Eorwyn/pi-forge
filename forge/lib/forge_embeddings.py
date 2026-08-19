#!/usr/bin/env python3
"""Shared, standard-library client for the forge embeddings endpoint.

Forge skills use a local, always-available embedding model (Qwen3-Embedding by
default) exposed through an OpenAI-compatible ``/v1/embeddings`` endpoint. This
module is the single place that knows how to reach it, so skills add content
similarity without each reimplementing the HTTP, batching, and vector math.

Design rules:

- Standard library only (``urllib``), so skills stay installable without extra
  dependencies.
- Never raise on a network or protocol failure during normal use. ``embed_texts``
  returns a structured result and callers degrade to their non-embedding path.
- Embeddings only ever feed a reviewable artifact (a manifest, a candidate-pair
  report). This module computes similarity; it never merges or deletes anything.

Configuration:

- ``FORGE_EMBEDDINGS_URL`` overrides the endpoint
  (default ``http://llms:8005/v1/embeddings``).
- ``FORGE_EMBEDDINGS_MODEL`` overrides the served model name
  (default ``embed``, matching ``connectedServices.embeddings.model``).

The server answers with whatever is loaded regardless of the name it is sent, so
this name is not a request — but it keys the vault embedding caches, so changing
it invalidates them and forces one full re-embed.
"""

import json
import math
import os
import time
import urllib.error
import urllib.request

import stack_state

DEFAULT_EMBEDDINGS_URL = "http://llms:8005/v1/embeddings"
DEFAULT_EMBEDDINGS_MODEL = "embed"
DEFAULT_TIMEOUT = 30.0
# Small batches on purpose. The embed backend serves one request at a time
# (``EMBED_N_PARALLEL=1``) and shares GPU 0 with the always-on chat/think weights,
# so a large batch buys no parallelism and only raises peak VRAM. 16 keeps each
# request light enough to ride a moment's contention.
DEFAULT_BATCH_SIZE = 16
# The model loads on demand behind the stack's router — a cold load is ~4s, and it
# can 500 under a moment's GPU pressure. Both are transient, so retry rather than
# fail the whole run for it.
DEFAULT_RETRIES = 3
RETRY_BACKOFF_BASE = 1.0
# The backend pools each input in a single ubatch (``EMBED_UBATCH_SIZE`` ≈ 512
# tokens), so an input past that width fails outright — a 500, not a server-side
# truncation. Dense text (speaker-labelled transcripts, markup) hits it near ~1300
# characters. This is the length a too-long input is retried at; for the similarity
# these vectors feed, a note's opening is the load-bearing part.
SAFE_INPUT_CHARS = 1000


def _connected_embeddings(env=None):
    """The persisted ``connectedServices.embeddings`` block, or ``{}``.

    Read lazily and defensively: this module is standard-library-only and used by
    skills that may run without ``forge_llm`` importable, so a missing module or an
    unreadable settings file degrades to the env/default path rather than raising.
    Precedence stays explicit arg > env > this > built-in default, matching every
    other connected service (transcription resolves the same way).
    """
    try:
        import forge_llm
    except ImportError:
        return {}
    services = forge_llm.load_connected_services(env)
    candidate = services.get("embeddings") if isinstance(services, dict) else None
    return candidate if isinstance(candidate, dict) else {}


def endpoint_url(explicit=None, env=None):
    if explicit:
        return explicit
    environment = env if env is not None else os.environ
    from_env = environment.get("FORGE_EMBEDDINGS_URL")
    if from_env:
        return from_env
    persisted = _connected_embeddings(env).get("url")
    if isinstance(persisted, str) and persisted.strip():
        return persisted.strip()
    return DEFAULT_EMBEDDINGS_URL


def model_name(explicit=None, env=None):
    if explicit:
        return explicit
    environment = env if env is not None else os.environ
    from_env = environment.get("FORGE_EMBEDDINGS_MODEL")
    if from_env:
        return from_env
    persisted = _connected_embeddings(env).get("model")
    if isinstance(persisted, str) and persisted.strip():
        return persisted.strip()
    return DEFAULT_EMBEDDINGS_MODEL


# Errors worth retrying: a 5xx, a 400 that means the router is still loading the
# on-demand model, and any connection/timeout error. A 400 for a bad model name is
# not retryable — repeating it only wastes time.
_ROUTER_LOADING = ("not resident", "sleeping", "loading", "router")


def _retryable(error):
    if isinstance(error, urllib.error.HTTPError):
        if error.code >= 500:
            return True
        if error.code == 400:
            try:
                body = error.read().decode("utf-8", "replace").lower()
            except OSError:
                body = ""
            return any(token in body for token in _ROUTER_LOADING)
        return False
    # URLError wraps connection-refused and socket timeouts; OSError covers the rest.
    return isinstance(error, (urllib.error.URLError, OSError))


def _do_request(url, model, batch, timeout):
    payload = json.dumps({"model": model, "input": batch, "encoding_format": "float"}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    parsed = json.loads(body)
    data = parsed.get("data")
    if not isinstance(data, list) or len(data) != len(batch):
        raise ValueError("embeddings response did not return one vector per input")
    vectors = []
    for item in sorted(data, key=lambda entry: entry.get("index", 0)):
        vector = item.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ValueError("embeddings response contained an empty vector")
        vectors.append([float(value) for value in vector])
    return vectors


def _post_batch(url, model, batch, timeout, retries=DEFAULT_RETRIES):
    """One request for ``batch``, retried on transient failure; raises on final fail.

    Backoff spans the router's ~4s cold load: with the default 3 retries the waits
    are 1s, 2s, 4s.
    """
    last_error = None
    for attempt in range(retries + 1):
        try:
            return _do_request(url, model, batch, timeout)
        except (
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            last_error = error
            if attempt < retries and _retryable(error):
                time.sleep(RETRY_BACKOFF_BASE * (2**attempt))
                continue
            raise
    raise last_error  # unreachable: the loop returns a value or raises


def _embed_one(url, model, text, timeout, retries):
    """Embed a single text, retried at ``SAFE_INPUT_CHARS`` if it is too long.

    The backend fails an over-width input outright rather than truncating it, so a
    note longer than one ubatch would otherwise be lost from the run. A shorter
    slice still embeds, and the opening of a note carries the similarity signal, so
    fall back to it before giving up.
    """
    try:
        return _post_batch(url, model, [text], timeout, retries)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        if len(text) <= SAFE_INPUT_CHARS:
            raise
        return _post_batch(url, model, [text[:SAFE_INPUT_CHARS]], timeout, retries)


def embed_texts(
    texts,
    url=None,
    model=None,
    timeout=DEFAULT_TIMEOUT,
    batch_size=DEFAULT_BATCH_SIZE,
    retries=DEFAULT_RETRIES,
    warmup=True,
):
    """Embed a list of texts.

    Returns a dict with ``ok``. On success it also carries ``vectors`` (aligned to
    ``texts``), ``model``, ``url``, and ``dimensions``. On failure it carries
    ``reason`` and the caller should fall back to its non-embedding behavior.

    Resilience, because the endpoint is a router-loaded model on a shared GPU: each
    request is retried on a transient failure; a batch that still fails is retried
    one text at a time, so a single bad input cannot lose the whole run's near-dupe
    pass; and one warm-up request triggers the on-demand load up front so the first
    real batch does not race it. Pass ``warmup=False`` when calling in a tight loop.
    """
    resolved_url = endpoint_url(url)
    resolved_model = model_name(model)
    if not texts:
        return {"ok": True, "vectors": [], "model": resolved_model, "url": resolved_url, "dimensions": 0}
    if warmup:
        # Ride the ~4s cold load once, here, rather than inside the first real batch.
        # A warm-up that still fails is not fatal — the batch loop retries too.
        try:
            _post_batch(resolved_url, resolved_model, ["ping"], min(timeout, 15.0), retries)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError):
            pass
    vectors = []
    try:
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            try:
                vectors.extend(_post_batch(resolved_url, resolved_model, batch, timeout, retries))
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError):
                # A batch fails if any one member is unservable (usually too long for
                # one ubatch). Retry each text on its own, truncating an over-width
                # one, so a single note cannot lose the whole run's vectors.
                for text in batch:
                    vectors.extend(_embed_one(resolved_url, resolved_model, text, timeout, retries))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as error:
        return {"ok": False, "reason": f"{type(error).__name__}: {error}", "model": resolved_model, "url": resolved_url}
    return {
        "ok": True,
        "vectors": vectors,
        "model": resolved_model,
        "url": resolved_url,
        "dimensions": len(vectors[0]) if vectors else 0,
    }


def embeddings_doctor(url=None, model=None, timeout=5.0, env=None):
    """Probe the endpoint with a tiny request and report reachability.

    The embedding model is normally held by the stack's model router rather than
    run as a service of its own, so a cold endpoint is the ordinary case and not
    a fault. Where the deployment publishes a state API, say that instead of
    reporting a bare connection error against a port nothing is listening on.
    """
    resolved_url = endpoint_url(url)
    result = embed_texts(["ping"], url=resolved_url, model=model, timeout=timeout)
    if result["ok"]:
        return {
            "configured": True,
            "reachable": True,
            "url": resolved_url,
            "model": result["model"],
            "dimensions": result["dimensions"],
            "detail": f"reachable ({result['dimensions']}-dimensional vectors)",
        }
    report = {
        "configured": True,
        "reachable": False,
        "url": resolved_url,
        "model": model_name(model),
        "detail": result["reason"],
    }
    explanation = stack_state.explain_unreachable(stack_state.read_snapshot(env=env), resolved_url)
    if explanation:
        report["stackDetail"] = explanation
        report["detail"] = f"{report['detail']} — {explanation}"
    return report


def normalize(vector):
    """Return a unit-length copy of ``vector``; a zero vector is returned as-is."""
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0.0:
        return list(vector)
    return [value / norm for value in vector]


def cosine(left, right):
    """Cosine similarity. Assumes inputs are already normalized for speed."""
    return sum(a * b for a, b in zip(left, right))


def _union_find_components(count, pairs):
    parent = list(range(count))

    def find(node):
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[max(root_left, root_right)] = min(root_left, root_right)

    components = {}
    for node in range(count):
        components.setdefault(find(node), []).append(node)
    return list(components.values())


def similar_pairs(normalized_vectors, threshold):
    """Return ``(i, j, similarity)`` for every pair with ``i < j`` at or above the
    threshold. O(n^2); intended for per-run scales (thousands of items)."""
    pairs = []
    count = len(normalized_vectors)
    for i in range(count):
        vector_i = normalized_vectors[i]
        for j in range(i + 1, count):
            score = cosine(vector_i, normalized_vectors[j])
            if score >= threshold:
                pairs.append((i, j, score))
    return pairs


def cluster_components(normalized_vectors, threshold):
    """Group items into connected components linked by similarity at or above the
    threshold. Returns a list of index lists, each of length >= 1."""
    pairs = [(i, j) for i, j, _ in similar_pairs(normalized_vectors, threshold)]
    return _union_find_components(len(normalized_vectors), pairs)
