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
  (default ``Qwen3-Embedding-0.6B``).
"""

import json
import math
import os
import urllib.error
import urllib.request

DEFAULT_EMBEDDINGS_URL = "http://llms:8005/v1/embeddings"
DEFAULT_EMBEDDINGS_MODEL = "Qwen3-Embedding-0.6B"
DEFAULT_TIMEOUT = 30.0
DEFAULT_BATCH_SIZE = 64


def endpoint_url(explicit=None):
    return explicit or os.environ.get("FORGE_EMBEDDINGS_URL") or DEFAULT_EMBEDDINGS_URL


def model_name(explicit=None):
    return explicit or os.environ.get("FORGE_EMBEDDINGS_MODEL") or DEFAULT_EMBEDDINGS_MODEL


def _post_batch(url, model, batch, timeout):
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


def embed_texts(texts, url=None, model=None, timeout=DEFAULT_TIMEOUT, batch_size=DEFAULT_BATCH_SIZE):
    """Embed a list of texts.

    Returns a dict with ``ok``. On success it also carries ``vectors`` (aligned to
    ``texts``), ``model``, ``url``, and ``dimensions``. On failure it carries
    ``reason`` and the caller should fall back to its non-embedding behavior.
    """
    resolved_url = endpoint_url(url)
    resolved_model = model_name(model)
    if not texts:
        return {"ok": True, "vectors": [], "model": resolved_model, "url": resolved_url, "dimensions": 0}
    vectors = []
    try:
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            vectors.extend(_post_batch(resolved_url, resolved_model, batch, timeout))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError, json.JSONDecodeError) as error:
        return {"ok": False, "reason": f"{type(error).__name__}: {error}", "model": resolved_model, "url": resolved_url}
    return {
        "ok": True,
        "vectors": vectors,
        "model": resolved_model,
        "url": resolved_url,
        "dimensions": len(vectors[0]) if vectors else 0,
    }


def embeddings_doctor(url=None, model=None, timeout=5.0):
    """Probe the endpoint with a tiny request and report reachability."""
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
    return {
        "configured": True,
        "reachable": False,
        "url": resolved_url,
        "model": model_name(model),
        "detail": result["reason"],
    }


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
