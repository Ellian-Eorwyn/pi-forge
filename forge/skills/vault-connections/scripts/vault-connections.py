#!/usr/bin/env python3
"""Semantic search, connection proposals, and a wiki entity layer for an Obsidian vault.

Companion to ``vault-organizer``. The organizer decides where a note *lives*;
this decides what a note is *connected to*. It never moves, renames, deletes, or
reclassifies anything, and it never rewrites a note body.

Every mutation is an additive frontmatter merge: quoted wikilinks are appended to
the ``related`` property of an existing frontmatter block, with every other byte
of the file preserved. Notes without frontmatter are refused rather than given
one — run ``vault-organizer`` on those first.
"""

import argparse
import contextlib
import datetime
import heapq
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from array import array
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings
import run_state
from vault_schema import (
    INBOX_DIR,
    UserError,
    compile_destination,
    compiled_schema_for,
    link_basename,
    normalize_body_for_hash,
    note_title,
    project_name,
    relative_path,
    resolve_schema_path,
    selected_notes,
    serialize_frontmatter,
    sha256_bytes,
    sha256_text,
    split_frontmatter,
    valid_wikilink,
    wikilink_target,
    yaml_scalar,
)

WORKFLOW = "vault-connections"
STATE_DIR = ".vault-connections"
DEFAULT_BASE_URL = "http://llms:8004/v1/chat/completions"
DEFAULT_MODEL = "code"
PROMPT_VERSION = "vault-connections-v1"

EMBED_BODY_CHARS = 2000
EMBED_HEADING_CHARS = 600
SEARCH_TEXT_CHARS = 2000
JUDGE_EXCERPT_CHARS = 1200
NOTES_INDEX_VERSION = 1
VECTOR_STORE_VERSION = 1
COMPACT_LIVE_RATIO = 0.8

DEFAULT_PER_NOTE = 5
# Qwen3-Embedding scores a personal vault high and narrow: on a 1,051-note vault the
# whole-corpus distribution peaks near 0.65, so a low floor admits everything and lets
# the per-note top-K do all the work. 0.75 is where pairs start being topically real.
DEFAULT_MIN_SIMILARITY = 0.75
# At or above this, a "connection" is really a duplicate — vault-organizer's job.
DEFAULT_MAX_SIMILARITY = 0.97
DEFAULT_MAX_CANDIDATES = 400
# Priority adjustments that pull cross-cutting pairs above near-identical siblings.
CROSS_DOMAIN_BONUS = 0.06
CROSS_SUBDOMAIN_BONUS = 0.02
SAME_FOLDER_PENALTY = 0.04
DEFAULT_MIN_MENTIONS = 2
DEFAULT_SEARCH_LIMIT = 10
SEARCH_RRF_K = 60
MIN_BODY_CHARS = 80
MAX_REASON_CHARS = 200
MAX_TRANSIENT_ATTEMPTS = 3
MAX_STUB_RELATED = 8
JUDGE_BATCH_STATE = 20

STRENGTHS = ("strong", "moderate", "weak")
CONNECTION_KINDS = ("same-topic", "generalization", "application", "contrast", "shared-entity")
WIKI_DOMAIN = "wiki"
WIKI_KIND_SUBDOMAIN = {
    "concept": "concepts",
    "practice": "practices",
    "place": "places",
    "event": "events",
    "term": "terms",
    "work": "works",
}
DIRECTORY_KINDS = ("person", "organization")

THINK_PREFILL = "<think>\n\n</think>\n\n"
THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]\r\n]+)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
FRONTMATTER_KEY_RE = re.compile(r"^([a-z][a-z0-9_]*):(.*)$")
LIST_ITEM_RE = re.compile(r"^(\s*)-\s+(.*)$")
TOKEN_RE = re.compile(r"[a-z0-9]+")


# --------------------------------------------------------------------------- #
# Output plumbing
# --------------------------------------------------------------------------- #


def structured(status, artifacts=None, warnings=None, errors=None, data=None):
    return {
        "status": status,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "data": data,
    }


def error_entry(code, message):
    return {"code": code, "message": message}


def print_json(value):
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def progress(message):
    print(message, file=sys.stderr, flush=True)


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def atomic_write_bytes(path, data):
    """Byte-exact atomic write. Notes may carry a BOM that text writers would eat."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


def format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def state_root(vault):
    return vault / STATE_DIR


def cache_dir(vault):
    return state_root(vault) / "cache"


def decisions_path(vault):
    return state_root(vault) / "decisions.jsonl"


def unique_run_directory(vault):
    runs = state_root(vault) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = utc_timestamp()
    candidate = runs / base
    suffix = 1
    while candidate.exists():
        candidate = runs / f"{base}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    (candidate / "backup").mkdir()
    return candidate


# --------------------------------------------------------------------------- #
# Minimal frontmatter reader
#
# Read-only and advisory: it feeds already-linked filtering and property lookup.
# Writes never go through it — merge_related edits the frontmatter text directly,
# so a parse miss can degrade a proposal but can never corrupt a note.
# --------------------------------------------------------------------------- #


def parse_frontmatter(text):
    """Parse the inner lines of a YAML frontmatter block into {key: str | list}."""
    values = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        match = FRONTMATTER_KEY_RE.match(lines[index])
        if not match:
            index += 1
            continue
        key, inline = match.group(1), match.group(2).strip()
        if inline.startswith("[") and inline.endswith("]"):
            body = inline[1:-1].strip()
            values[key] = [strip_yaml_scalar(part) for part in split_flow_items(body)] if body else []
            index += 1
            continue
        if inline:
            values[key] = strip_yaml_scalar(inline)
            index += 1
            continue
        items = []
        index += 1
        while index < len(lines):
            item = LIST_ITEM_RE.match(lines[index])
            if not item:
                break
            items.append(strip_yaml_scalar(item.group(2).strip()))
            index += 1
        values[key] = items
    return values


def split_flow_items(body):
    items = []
    current = ""
    quote = ""
    for character in body:
        if quote:
            current += character
            if character == quote:
                quote = ""
            continue
        if character in "\"'":
            quote = character
            current += character
            continue
        if character == ",":
            items.append(current)
            current = ""
            continue
        current += character
    if current.strip():
        items.append(current)
    return [item for item in (part.strip() for part in items) if item]


def strip_yaml_scalar(value):
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return inner.replace('\\"', '"').replace("\\\\", "\\") if text[0] == '"' else inner
    return text


def link_targets_in(text):
    """Wikilink targets in ``text``, keeping their original casing."""
    targets = set()
    for match in WIKILINK_RE.finditer(text):
        target = link_basename(re.split(r"[|#^]", match.group(1), maxsplit=1)[0])
        if target:
            targets.add(target)
    return targets


def frontmatter_link_targets(values):
    """Every wikilink target mentioned anywhere in the parsed frontmatter."""
    targets = set()
    for value in values.values():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                targets |= link_targets_in(item)
    return targets


# --------------------------------------------------------------------------- #
# Additive frontmatter merge — the only way this tool writes to a note
# --------------------------------------------------------------------------- #


def merge_related(data, additions, schema):
    """Append quoted wikilinks to a note's ``related`` property.

    Returns ``(new_bytes, added, reason)``. ``new_bytes`` is None when the note
    is refused; ``reason`` says why. The body, the delimiters, the BOM, the line
    endings, and every other property are preserved byte-for-byte.
    """
    had_bom = data.startswith(b"\xef\xbb\xbf")
    prefix = data[:3] if had_bom else b""
    try:
        text = (data[3:] if had_bom else data).decode("utf-8")
    except UnicodeDecodeError as error:
        return None, [], f"not valid UTF-8: {error}"

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, [], "no frontmatter block; run vault-organizer on this note first"
    close = None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            close = index
            break
    if close is None:
        return None, [], "frontmatter has no closing delimiter"

    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    block = lines[1:close]
    existing = parse_frontmatter("".join(block))
    present = {target.casefold() for target in frontmatter_link_targets(existing)}
    wanted = []
    for link in additions:
        if not valid_wikilink(link):
            continue
        target = link_basename(wikilink_target(link)).casefold()
        if not target or target in present:
            continue
        present.add(target)
        wanted.append(link)
    if not wanted:
        return None, [], "already linked"

    rendered = [f"  - {yaml_scalar(link, force_quote=True)}{newline}" for link in wanted]
    start, end = related_block_bounds(block)

    if start is None:
        insert_at = insertion_index(block, schema)
        new_block = block[:insert_at] + [f"related:{newline}"] + rendered + block[insert_at:]
    else:
        inline = FRONTMATTER_KEY_RE.match(block[start]).group(2).strip()
        if inline and inline != "[]":
            return None, [], f"related is an inline value this tool will not rewrite: {inline}"
        header = [f"related:{newline}"] if inline == "[]" else [block[start]]
        indent = existing_indent(block[start + 1:end]) or "  "
        if indent != "  ":
            rendered = [f"{indent}- {yaml_scalar(link, force_quote=True)}{newline}" for link in wanted]
        new_block = block[:start] + header + block[start + 1:end] + rendered + block[end:]

    rebuilt = "".join([lines[0]] + new_block + lines[close:])
    return prefix + rebuilt.encode("utf-8"), wanted, None


def related_block_bounds(block):
    """(index of the ``related:`` line, index just past its list items), or (None, None)."""
    for index, line in enumerate(block):
        match = FRONTMATTER_KEY_RE.match(line)
        if not match or match.group(1) != "related":
            continue
        end = index + 1
        while end < len(block) and LIST_ITEM_RE.match(block[end]):
            end += 1
        return index, end
    return None, None


def existing_indent(item_lines):
    for line in item_lines:
        match = LIST_ITEM_RE.match(line)
        if match:
            return match.group(1)
    return ""


def insertion_index(block, schema):
    """Where a new ``related:`` key belongs, following the schema's property order."""
    order = schema.get("property_order") or []
    if "related" not in order:
        return len(block)
    limit = order.index("related")
    insert_at = 0
    for index, line in enumerate(block):
        match = FRONTMATTER_KEY_RE.match(line)
        if not match:
            continue
        key = match.group(1)
        if key in order and order.index(key) < limit:
            insert_at = index + 1
            while insert_at < len(block) and LIST_ITEM_RE.match(block[insert_at]):
                insert_at += 1
    return insert_at


# --------------------------------------------------------------------------- #
# Note index
# --------------------------------------------------------------------------- #


def heading_outline(body, limit=EMBED_HEADING_CHARS):
    headings = []
    for line in body.splitlines():
        match = HEADING_RE.match(line)
        if match:
            headings.append(match.group(2).strip())
    return " · ".join(headings)[:limit]


def read_note(vault, path):
    data = path.read_bytes()
    frontmatter = split_frontmatter(data)
    body = frontmatter["body"]
    normalized = normalize_body_for_hash(body)
    values = {} if frontmatter["malformed"] else parse_frontmatter(frontmatter["frontmatter_text"])
    outline = heading_outline(body)
    stat = path.stat()
    return {
        "path": relative_path(vault, path),
        "stem": path.stem,
        "title": note_title(path, body),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": sha256_bytes(data),
        "body_hash": sha256_text(normalized),
        "body_chars": len(normalized),
        "malformed": frontmatter["malformed"],
        "has_frontmatter": frontmatter["had_frontmatter"] and not frontmatter["malformed"],
        "type": values.get("type") if isinstance(values.get("type"), str) else None,
        "domain": values.get("domain") if isinstance(values.get("domain"), str) else None,
        "subdomain": values.get("subdomain") if isinstance(values.get("subdomain"), str) else None,
        "headings": outline,
        "links": sorted(frontmatter_link_targets(values) | link_targets_in(body)),
        "search_text": normalized[:SEARCH_TEXT_CHARS],
        "embed_text": f"{note_title(path, body)}\n{outline}\n{normalized[:EMBED_BODY_CHARS]}",
    }


def notes_index_path(vault):
    return cache_dir(vault) / "notes.json"


def refresh_notes_index(vault, schema_path, limit=None):
    """Rebuild the note index, reusing unchanged entries by size and mtime."""
    path = notes_index_path(vault)
    previous = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and loaded.get("version") == NOTES_INDEX_VERSION:
                previous = loaded.get("entries") or {}
        except (OSError, json.JSONDecodeError):
            previous = {}
    entries = {}
    warnings = []
    for note in selected_notes(vault, schema_path, "vault", limit):
        rel = relative_path(vault, note)
        try:
            stat = note.stat()
            cached = previous.get(rel)
            if cached and cached.get("size") == stat.st_size and cached.get("mtime") == stat.st_mtime:
                entries[rel] = cached
                continue
            entries[rel] = read_note(vault, note)
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"skipped {rel}: {error}")
    run_state.atomic_write_json(path, {"version": NOTES_INDEX_VERSION, "entries": entries})
    return entries, warnings


# --------------------------------------------------------------------------- #
# Vector store: float32 rows in one binary file, hash -> row index in a sidecar
# --------------------------------------------------------------------------- #


def vector_paths(vault):
    return cache_dir(vault) / "vectors.json", cache_dir(vault) / "vectors.f32"


def load_vectors(vault, model):
    meta_path, bin_path = vector_paths(vault)
    empty = {"model": model, "dims": 0, "rows": {}, "data": array("f")}
    if not meta_path.is_file() or not bin_path.is_file():
        return empty
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if meta.get("version") != VECTOR_STORE_VERSION or meta.get("model") != model:
        return empty
    dims = meta.get("dims") or 0
    rows = meta.get("rows") or {}
    if not isinstance(dims, int) or dims <= 0 or not isinstance(rows, dict):
        return empty
    data = array("f")
    try:
        data.frombytes(bin_path.read_bytes())
    except (OSError, ValueError):
        return empty
    if len(data) != dims * len(rows):
        return empty
    return {"model": model, "dims": dims, "rows": rows, "data": data}


def vector_for(store, body_hash):
    index = store["rows"].get(body_hash)
    if index is None:
        return None
    dims = store["dims"]
    return store["data"][index * dims:(index + 1) * dims]


def save_vectors(vault, store, live_hashes=None):
    """Persist the store, compacting when enough rows have gone stale."""
    meta_path, bin_path = vector_paths(vault)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    rows, data, dims = store["rows"], store["data"], store["dims"]
    if live_hashes is not None and rows and len(live_hashes) < COMPACT_LIVE_RATIO * len(rows):
        compact_rows = {}
        compact_data = array("f")
        for body_hash in rows:
            if body_hash not in live_hashes:
                continue
            index = rows[body_hash]
            compact_rows[body_hash] = len(compact_rows)
            compact_data.extend(data[index * dims:(index + 1) * dims])
        rows, data = compact_rows, compact_data
        store["rows"], store["data"] = rows, data
    atomic_write_bytes(bin_path, data.tobytes())
    run_state.atomic_write_json(
        meta_path,
        {"version": VECTOR_STORE_VERSION, "model": store["model"], "dims": dims, "rows": rows},
    )


def ensure_vectors(args, vault, entries, store=None):
    """Embed every indexed note that has no cached vector. Degrades, never raises."""
    model = args.embeddings_model
    store = store if store is not None else load_vectors(vault, model)
    eligible = {rel: entry for rel, entry in entries.items() if entry.get("body_chars", 0) >= MIN_BODY_CHARS}
    missing = {}
    for rel, entry in eligible.items():
        if entry["body_hash"] not in store["rows"] and entry["body_hash"] not in missing:
            missing[entry["body_hash"]] = entry["embed_text"]
    info = {
        "model": model,
        "url": args.embeddings_url,
        "cached": len(store["rows"]),
        "embedded": 0,
        "skipped_short": len(entries) - len(eligible),
        "reason": None,
    }
    if not missing:
        info["dimensions"] = store["dims"]
        return store, info
    hashes = list(missing)
    progress(f"[{WORKFLOW}] embedding {len(hashes)} notes")
    result = forge_embeddings.embed_texts(
        [missing[body_hash] for body_hash in hashes],
        url=args.embeddings_url,
        model=model,
        timeout=args.request_timeout,
    )
    if not result.get("ok"):
        info["reason"] = result.get("reason")
        return store, info
    dims = result["dimensions"]
    if store["dims"] and dims != store["dims"]:
        progress(f"[{WORKFLOW}] embedding dimensions changed ({store['dims']} -> {dims}); rebuilding store")
        store = {"model": model, "dims": dims, "rows": {}, "data": array("f")}
    store["dims"] = dims
    for body_hash, vector in zip(hashes, result["vectors"]):
        store["rows"][body_hash] = len(store["rows"])
        store["data"].extend(forge_embeddings.normalize(vector))
    info["embedded"] = len(hashes)
    info["dimensions"] = dims
    save_vectors(vault, store, live_hashes={entry["body_hash"] for entry in eligible.values()})
    return store, info


def ensure_index(args, vault, schema_path):
    entries, warnings = refresh_notes_index(vault, schema_path, args.limit if args.command == "index" else None)
    store, embedding_info = ensure_vectors(args, vault, entries)
    if embedding_info.get("reason"):
        warnings.append(f"embeddings unavailable; semantic ranking is off: {embedding_info['reason']}")
    return entries, store, embedding_info, warnings


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #


def search_tokens(value):
    return TOKEN_RE.findall(value.lower())


def lexical_scores(query, entries):
    query_terms = search_tokens(query)
    if not query_terms:
        return {}
    counts = {rel: Counter(search_tokens(f"{entry['title']} {entry['headings']} {entry['search_text']}")) for rel, entry in entries.items()}
    document_frequency = Counter()
    for value in counts.values():
        document_frequency.update(set(value))
    total = max(1, len(entries))
    scores = {}
    for rel, value in counts.items():
        length = max(1, sum(value.values()))
        score = 0.0
        for term in query_terms:
            frequency = value.get(term, 0)
            if frequency:
                inverse = 1.0 + math.log((total + 1) / (document_frequency[term] + 1))
                score += inverse * frequency / (frequency + 1.2 * (0.25 + 0.75 * length / 200))
        if score:
            scores[rel] = score
    return scores


def semantic_scores(args, entries, store, query):
    if not store["rows"] or not store["dims"]:
        return {}, "no embeddings are cached; run index"
    result = forge_embeddings.embed_texts(
        [query], url=args.embeddings_url, model=args.embeddings_model, timeout=min(args.request_timeout, 30)
    )
    if not result.get("ok"):
        return {}, result.get("reason")
    query_vector = forge_embeddings.normalize(result["vectors"][0])
    if len(query_vector) != store["dims"]:
        return {}, f"query vector is {len(query_vector)}-dimensional but the store holds {store['dims']}"
    scores = {}
    for rel, entry in entries.items():
        vector = vector_for(store, entry["body_hash"])
        if vector is not None:
            scores[rel] = forge_embeddings.cosine(query_vector, vector)
    return scores, None


def rank_by_fusion(entries, lexical, semantic, query, limit):
    lexical_rank = {rel: index for index, (rel, _) in enumerate(sorted(lexical.items(), key=lambda row: (-row[1], row[0])), 1)}
    semantic_rank = {rel: index for index, (rel, _) in enumerate(sorted(semantic.items(), key=lambda row: (-row[1], row[0])), 1)}
    query_lower = query.lower().strip()
    ranked = []
    for rel in set(lexical_rank) | set(semantic_rank):
        entry = entries.get(rel)
        if entry is None:
            continue
        score = 0.0
        if rel in lexical_rank:
            score += 1 / (SEARCH_RRF_K + lexical_rank[rel])
        if rel in semantic_rank:
            score += 1 / (SEARCH_RRF_K + semantic_rank[rel])
        if query_lower and query_lower == entry["stem"].lower():
            score += 1
        elif query_lower and query_lower in entry["title"].lower():
            score += 0.25
        ranked.append(
            {
                "path": rel,
                "title": entry["title"],
                "type": entry["type"],
                "domain": entry["domain"],
                "subdomain": entry["subdomain"],
                "score": round(score, 8),
                "lexicalScore": round(lexical.get(rel, 0.0), 6),
                "semanticScore": round(semantic[rel], 6) if rel in semantic else None,
                "snippet": re.sub(r"\s+", " ", entry["search_text"]).strip()[:320],
            }
        )
    ranked.sort(key=lambda row: (-row["score"], row["path"]))
    return ranked[:limit]


# --------------------------------------------------------------------------- #
# Chat endpoint
# --------------------------------------------------------------------------- #


def normalize_base_url(value):
    text = (value or "").rstrip("/")
    if not text:
        return DEFAULT_BASE_URL
    if text.endswith("/chat/completions"):
        return text
    if text.endswith("/v1"):
        return f"{text}/chat/completions"
    return f"{text}/v1/chat/completions"


def request_json(base_url, model, api_key, timeout, messages, cache_prompt=True):
    payload = {"model": model, "messages": messages, "temperature": 0, "stream": False}
    if cache_prompt:
        payload["cache_prompt"] = True
    request = urllib.request.Request(
        base_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        raise UserError(f"HTTP {error.code} from {base_url}: {error.read().decode('utf-8', 'replace')[:400]}") from error
    except (urllib.error.URLError, OSError) as error:
        raise UserError(f"request to {base_url} failed: {error}") from error
    parsed = json.loads(body)
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        raise UserError("chat response contained no choices")
    return choices[0].get("message", {}).get("content") or ""


def extract_json_content(content):
    text = THINK_BLOCK_RE.sub("", content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise UserError(f"model did not return a JSON object: {text[:200]}")
    return json.loads(text[start:end + 1])


def request_with_retry(args, messages):
    last = None
    for attempt in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
        try:
            content = request_json(args.base_url, args.model, args.api_key, args.request_timeout, messages, args.cache_prompt)
            return extract_json_content(content)
        except (UserError, json.JSONDecodeError) as error:
            last = error
            if attempt < MAX_TRANSIENT_ATTEMPTS and run_state.is_transient_failure(error):
                time.sleep(min(2 ** attempt, 8))
                continue
            break
    raise UserError(str(last))


def with_prefill(args, messages):
    return [*messages, {"role": "assistant", "content": THINK_PREFILL}] if args.think_prefill else messages


CONNECTION_SYSTEM = (
    "You judge whether two notes from one person's Obsidian vault deserve an explicit link.\n"
    "Return exactly one JSON object and nothing else:\n"
    '{"connect": true, "strength": "strong", "kind": "generalization", "reason": "<short phrase>"}\n'
    f"strength must be one of: {', '.join(STRENGTHS)}.\n"
    f"kind must be one of: {', '.join(CONNECTION_KINDS)}.\n"
    f"reason must be a single clause under {MAX_REASON_CHARS} characters naming the shared idea.\n"
    "\n"
    "The vault owner applies abstract ideas — philosophical, religious, epistemic — across\n"
    "unrelated areas of life. The most valuable connections are the ones that carry a concept\n"
    "from one domain into another: a Buddhist idea informing research methods, an epistemology\n"
    "reading explaining a work problem. Rate those 'strong'.\n"
    "\n"
    "Set connect=false when the notes merely share vocabulary, a date, a file format, or\n"
    "boilerplate; when one note is an empty stub; or when the overlap is too generic to be\n"
    "worth a permanent link. Being conservative is correct — a rejected pair is never shown\n"
    "again. Judge only the two notes given; never invent titles or link a third note."
)

WIKI_SYSTEM = (
    "You classify the target of an unresolved Obsidian wikilink so it can be filed.\n"
    "Return exactly one JSON object and nothing else:\n"
    '{"kind": "concept", "title": "Dependent Origination", "summary": "<one or two sentences>"}\n'
    f"kind must be one of: {', '.join(sorted(WIKI_KIND_SUBDOMAIN))}, {', '.join(DIRECTORY_KINDS)}, skip.\n"
    "\n"
    "- concept: a named idea, theory, doctrine, or framework.\n"
    "- practice: a named method, technique, discipline, or exercise.\n"
    "- place: a geographic or physical location.\n"
    "- event: a named or dated happening — a conference, retreat, trip, or historical event.\n"
    "- term: jargon, an acronym, a program name, or a piece of domain vocabulary.\n"
    "- work: a named book, film, game, album, or text treated as a recurring subject.\n"
    "- person / organization: an individual human, or an institution, company, lab, or group.\n"
    "- skip: a file path, a date, a fragment, a typo, or anything too vague to define.\n"
    "\n"
    "title is the canonical display name — fix capitalization, expand nothing you are not sure\n"
    "of, and keep it as a filename-safe line. summary states what the thing is, in the vault\n"
    "owner's context, using only what the surrounding mentions support. Never invent facts; if\n"
    "the mentions do not tell you what it is, return kind 'skip'."
)


def note_brief(entry, body):
    location = " / ".join(part for part in [entry.get("domain"), entry.get("subdomain")] if part) or "unfiled"
    excerpt = re.sub(r"\n{3,}", "\n\n", body).strip()[:JUDGE_EXCERPT_CHARS]
    return f"title: {entry['title']}\npath: {entry['path']}\nfiled under: {location}\n---\n{excerpt}"


def judge_pair(args, vault, left, right):
    left_body = normalize_body_for_hash(split_frontmatter((vault / left["path"]).read_bytes())["body"])
    right_body = normalize_body_for_hash(split_frontmatter((vault / right["path"]).read_bytes())["body"])
    messages = with_prefill(
        args,
        [
            {"role": "system", "content": CONNECTION_SYSTEM},
            {"role": "user", "content": f"NOTE A\n{note_brief(left, left_body)}\n\nNOTE B\n{note_brief(right, right_body)}"},
        ],
    )
    return validate_judgment(request_with_retry(args, messages))


def validate_judgment(raw):
    if not isinstance(raw, dict):
        raise UserError("judgment was not a JSON object")
    connect = raw.get("connect")
    if not isinstance(connect, bool):
        raise UserError("judgment.connect must be a boolean")
    if not connect:
        return {"connect": False, "strength": None, "kind": None, "reason": clean_reason(raw.get("reason"))}
    strength = raw.get("strength")
    kind = raw.get("kind")
    if strength not in STRENGTHS:
        raise UserError(f"judgment.strength must be one of {STRENGTHS}")
    if kind not in CONNECTION_KINDS:
        raise UserError(f"judgment.kind must be one of {CONNECTION_KINDS}")
    return {"connect": True, "strength": strength, "kind": kind, "reason": clean_reason(raw.get("reason"))}


def clean_reason(value):
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    text = "".join(character for character in text if ord(character) >= 32 or character == "\t")
    return text[:MAX_REASON_CHARS]


# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #


def candidate_priority(entries, left, right, similarity, prefer):
    """Rank candidates by how interesting the pair is, not just how similar.

    Raw similarity ranks near-identical documents in the same folder highest,
    which are the least useful links to add. The whole point of the skill is the
    idea that travels between areas of life, so a cross-domain pair outranks a
    same-folder pair at equal similarity.
    """
    if prefer != "cross-domain":
        return similarity
    left_entry, right_entry = entries[left], entries[right]
    priority = similarity
    left_domain, right_domain = left_entry.get("domain"), right_entry.get("domain")
    if left_domain and right_domain and left_domain != right_domain:
        priority += CROSS_DOMAIN_BONUS
    elif left_entry.get("subdomain") and left_entry.get("subdomain") != right_entry.get("subdomain"):
        priority += CROSS_SUBDOMAIN_BONUS
    if Path(left).parent == Path(right).parent:
        priority -= SAME_FOLDER_PENALTY
    return priority


def similarity_candidates(entries, store, per_note, min_similarity, max_candidates, max_similarity=1.1, prefer="cross-domain"):
    """Top-K neighbors per note, unioned, ranked by priority, and capped.

    Returns (pairs, histogram, near_duplicates)."""
    items = []
    for rel, entry in sorted(entries.items()):
        vector = vector_for(store, entry["body_hash"])
        if vector is not None:
            items.append((rel, vector))
    histogram = Counter()
    count = len(items)
    # Bounded min-heaps: a hub note similar to hundreds of others still costs O(per_note).
    neighbors = [[] for _ in items]

    def offer(index, score, other):
        heap = neighbors[index]
        if len(heap) < per_note:
            heapq.heappush(heap, (score, other))
        elif score > heap[0][0]:
            heapq.heapreplace(heap, (score, other))

    for i in range(count):
        vector_i = items[i][1]
        for j in range(i + 1, count):
            score = forge_embeddings.cosine(vector_i, items[j][1])
            histogram[round(math.floor(score * 20) / 20, 2)] += 1
            if score < min_similarity:
                continue
            offer(i, score, j)
            offer(j, score, i)
    pairs = {}
    near_duplicates = []
    for i in range(count):
        for score, j in neighbors[i]:
            key = (items[i][0], items[j][0]) if items[i][0] < items[j][0] else (items[j][0], items[i][0])
            pairs[key] = max(pairs.get(key, 0.0), score)
    ranked = []
    for (left, right), score in pairs.items():
        if score >= max_similarity:
            near_duplicates.append({"left": left, "right": right, "similarity": round(score, 6)})
            continue
        ranked.append(
            {
                "left": left,
                "right": right,
                "similarity": round(score, 6),
                "priority": round(candidate_priority(entries, left, right, score, prefer), 6),
            }
        )
    ranked.sort(key=lambda row: (-row["priority"], row["left"], row["right"]))
    near_duplicates.sort(key=lambda row: (-row["similarity"], row["left"]))
    return ranked[:max_candidates], histogram, near_duplicates


def already_linked(left, right):
    left_links = {target.casefold() for target in left["links"]}
    right_links = {target.casefold() for target in right["links"]}
    left_keys = {left["stem"].casefold(), left["title"].casefold()}
    right_keys = {right["stem"].casefold(), right["title"].casefold()}
    return bool(left_links & right_keys) or bool(right_links & left_keys)


def load_decisions(vault):
    rows, _ = run_state.read_jsonl_recover_tail(decisions_path(vault), repair=True)
    return {row["key"] for row in rows if isinstance(row, dict) and row.get("key")}


def pair_key(left, right):
    return "|".join(sorted([left, right]))


def decision_key(proposal):
    if proposal.get("action") == "link":
        return pair_key(proposal["left"], proposal["right"])
    if proposal.get("destination"):
        return f"wiki:{proposal['destination']}"
    return f"proposal:{proposal.get('id')}"


def record_decision(vault, key, decision, detail=None):
    run_state.append_jsonl_fsync(
        decisions_path(vault),
        {"key": key, "decision": decision, "at": run_state.utc_now(), "detail": detail},
    )


# --------------------------------------------------------------------------- #
# Wiki candidates
# --------------------------------------------------------------------------- #


def unresolved_targets(entries, min_mentions):
    """Wikilink targets with no note of that basename.

    Returns ``{casefolded: {"display": str, "sources": [path]}}``; the display
    name is the first original casing seen, so the stub keeps the user's own
    capitalization.
    """
    known = {Path(rel).stem.casefold() for rel in entries}
    mentions = {}
    for rel, entry in sorted(entries.items()):
        for target in entry["links"]:
            key = target.casefold()
            if key in known:
                continue
            record = mentions.setdefault(key, {"display": target, "sources": []})
            if rel not in record["sources"]:
                record["sources"].append(rel)
    return {key: record for key, record in mentions.items() if len(record["sources"]) >= min_mentions}


def classify_target(args, target, mention_lines):
    messages = with_prefill(
        args,
        [
            {"role": "system", "content": WIKI_SYSTEM},
            {"role": "user", "content": f"LINK TARGET: {target}\n\nMENTIONED IN:\n" + "\n".join(mention_lines)},
        ],
    )
    raw = request_with_retry(args, messages)
    if not isinstance(raw, dict):
        raise UserError("classification was not a JSON object")
    kind = raw.get("kind")
    if kind not in set(WIKI_KIND_SUBDOMAIN) | set(DIRECTORY_KINDS) | {"skip"}:
        raise UserError(f"classification.kind is not a known kind: {kind}")
    title = raw.get("title") if isinstance(raw.get("title"), str) else target
    return {"kind": kind, "title": safe_title(title) or target, "summary": clean_summary(raw.get("summary"))}


def safe_title(value):
    text = re.sub(r"\s+", " ", value).strip()
    text = "".join(character for character in text if ord(character) >= 32)
    for bad in ("/", "\\", ":", "*", "?", '"', "<", ">", "|", "[", "]", "#", "^"):
        text = text.replace(bad, "")
    return text.strip(" .")[:120]


def clean_summary(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:600]


def stub_note_text(schema, title, kind, summary, mentions):
    metadata = {
        "type": kind if kind in schema["types"] else "note",
        "status": "active",
        "domain": WIKI_DOMAIN,
        "subdomain": WIKI_KIND_SUBDOMAIN[kind],
        "related": [f"[[{Path(rel).stem}]]" for rel in mentions[:MAX_STUB_RELATED]],
        "capture_type": "generated",
    }
    metadata = {key: value for key, value in metadata.items() if key in schema["properties"]}
    lines = [f"# {title}", "", summary or "_Stub created from existing links. Definition pending._", "", "## Mentioned in", ""]
    lines.extend(f"- [[{Path(rel).stem}]]" for rel in mentions)
    return serialize_frontmatter(metadata, schema) + "\n".join(lines) + "\n"


def wiki_destination(schema, kind, title):
    subdomain = WIKI_KIND_SUBDOMAIN[kind]
    if WIKI_DOMAIN not in schema["domains"]:
        raise UserError(f"the schema note has no '{WIKI_DOMAIN}' domain; add it before running wiki")
    if subdomain not in schema["subdomains"].get(WIKI_DOMAIN, {}):
        raise UserError(f"the schema note has no '{WIKI_DOMAIN}/{subdomain}' subdomain; add it before running wiki")
    folder = compile_destination(schema, {"domain": WIKI_DOMAIN, "subdomain": subdomain})
    return (folder / f"{title}.md").as_posix()


def wiki_notes(schema, entries):
    """Notes already filed in the wiki domain."""
    if WIKI_DOMAIN not in schema["domains"]:
        return {}
    prefix = compile_destination(schema, {"domain": WIKI_DOMAIN}).as_posix() + "/"
    return {rel: entry for rel, entry in entries.items() if rel.startswith(prefix) or entry.get("domain") == WIKI_DOMAIN}


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def write_report(run_dir, proposals, counts, histogram, warnings, vault, mode, extra=None):
    report = [
        f"# Vault connections — {mode}",
        "",
        f"- Vault: `{vault}`",
        f"- Run: `{run_dir}`",
        f"- Generated: {run_state.utc_now()}",
        "",
        "## Counts",
        "",
    ]
    for key, value in counts.items():
        report.append(f"- {key.replace('_', ' ')}: {value}")
    links = [item for item in proposals if item["action"] == "link"]
    stubs = [item for item in proposals if item["action"] == "create_wiki_note"]
    for strength in STRENGTHS:
        group = [item for item in links if item.get("strength") == strength]
        if not group:
            continue
        report.extend(["", f"## {strength.capitalize()} connections ({len(group)})", ""])
        for item in group:
            report.append(f"- `{item['id']}` **{item['leftTitle']}** ↔ **{item['rightTitle']}** — {item['reason']}")
            report.append(f"  - {item['left']}  ·  {item['right']}  ·  similarity {item['similarity']}")
    if stubs:
        report.extend(["", f"## Proposed wiki notes ({len(stubs)})", ""])
        for item in stubs:
            report.append(f"- `{item['id']}` **{item['title']}** ({item['kind']}) → `{item['destination']}`")
            report.append(f"  - {item['mentionCount']} mentions · {item['summary'][:160]}")
    blocked = [item for item in proposals if item["action"] == "blocked"]
    if blocked:
        report.extend(["", f"## Reported, not proposed ({len(blocked)})", ""])
        for item in blocked:
            report.append(f"- **{item['title']}** — {item['reason']}")
    if extra:
        for heading, rows, formatter in extra:
            if rows:
                report.extend(["", f"## {heading} ({len(rows)})", ""])
                report.extend(formatter(row) for row in rows)
    if histogram:
        report.extend(["", "## Similarity distribution", "", "| bucket | pairs |", "| --- | ---: |"])
        for bucket in sorted(histogram, reverse=True):
            if histogram[bucket] and bucket >= 0.3:
                report.append(f"| {bucket:.2f} | {histogram[bucket]} |")
    if warnings:
        report.extend(["", "## Warnings", ""])
        report.extend(f"- {warning}" for warning in warnings)
    report.extend(
        [
            "",
            "## Next",
            "",
            "Review the proposals above, then apply the ones you agree with:",
            "",
            "```",
            f"vault-connections.py apply --vault {vault} --run {run_dir} --accept <ids>",
            "```",
            "",
            "Rejected ids should be passed with `--reject` so they are never proposed again.",
            "",
        ]
    )
    (run_dir / "report.md").write_text("\n".join(report), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def resolve_vault(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault directory does not exist: {vault}")
    cache_dir(vault).mkdir(parents=True, exist_ok=True)
    return vault


def load_schema(args, vault):
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=cache_dir(vault))
    return schema_path, schema, schema_hash


def command_index(args):
    vault = resolve_vault(args)
    schema_path, _, _ = load_schema(args, vault)
    entries, store, embedding_info, warnings = ensure_index(args, vault, schema_path)
    return structured(
        "ok",
        artifacts=[str(notes_index_path(vault)), str(vector_paths(vault)[1])],
        warnings=warnings,
        data={"notes": len(entries), "embeddings": embedding_info},
    )


def command_search(args):
    vault = resolve_vault(args)
    schema_path, _, _ = load_schema(args, vault)
    entries, store, embedding_info, warnings = ensure_index(args, vault, schema_path)
    lexical = lexical_scores(args.query, entries)
    semantic, semantic_warning = semantic_scores(args, entries, store, args.query)
    if semantic_warning:
        warnings.append(f"semantic ranking unavailable; lexical results remain: {semantic_warning}")
    hits = rank_by_fusion(entries, lexical, semantic, args.query, args.search_limit)
    return structured(
        "ok",
        warnings=warnings,
        data={
            "query": args.query,
            "ranking": "lexical" if semantic_warning else "hybrid",
            "notes": len(entries),
            "hits": hits,
        },
    )


def command_propose(args):
    vault = resolve_vault(args)
    schema_path, schema, schema_hash = load_schema(args, vault)
    entries, store, embedding_info, warnings = ensure_index(args, vault, schema_path)
    if embedding_info.get("reason"):
        raise UserError(f"propose needs embeddings: {embedding_info['reason']}")

    started = time.time()
    progress(f"[{WORKFLOW}] scoring {len(entries)} notes pairwise")
    candidates, histogram, near_duplicates = similarity_candidates(
        entries, store, args.per_note, args.min_similarity, args.max_candidates, args.max_similarity, args.prefer
    )
    progress(f"[{WORKFLOW}] {len(candidates)} candidate pairs in {format_duration(time.time() - started)}")

    decided = load_decisions(vault)
    filtered = []
    skipped = {"already_linked": 0, "already_decided": 0, "inbox": 0, "near_duplicate": len(near_duplicates)}
    for candidate in candidates:
        left, right = entries[candidate["left"]], entries[candidate["right"]]
        if pair_key(candidate["left"], candidate["right"]) in decided:
            skipped["already_decided"] += 1
            continue
        if already_linked(left, right):
            skipped["already_linked"] += 1
            continue
        if candidate["left"].startswith(INBOX_DIR + "/") or candidate["right"].startswith(INBOX_DIR + "/"):
            skipped["inbox"] += 1
            continue
        filtered.append(candidate)
    if near_duplicates:
        warnings.append(
            f"{len(near_duplicates)} pairs scored at or above {args.max_similarity} and were treated as "
            "near-duplicates rather than connections; run vault-organizer to de-duplicate them"
        )
    selected = filtered[:args.limit] if args.limit else filtered

    run_dir = unique_run_directory(vault)
    run_state.initialize_run_state(
        run_dir,
        run_state.create_run_state(
            WORKFLOW,
            "propose",
            {"vault": str(vault), "schemaHash": schema_hash},
            resolved_options(args),
            items=[{"key": pair_key(row["left"], row["right"]), "status": "pending"} for row in selected],
            phase="judging",
        ),
    )
    run_state.atomic_write_json(
        run_dir / "candidates.json",
        {"selected": selected, "eligible": filtered, "nearDuplicates": near_duplicates, "skipped": skipped},
    )

    proposals = []
    rejected = 0
    failed = 0
    for index, candidate in enumerate(selected, 1):
        left, right = entries[candidate["left"]], entries[candidate["right"]]
        elapsed = time.time() - started
        eta = format_duration(elapsed / index * (len(selected) - index)) if index > 1 else "?"
        progress(f"[{WORKFLOW}] judging {index}/{len(selected)} (eta {eta}): {left['title']} ↔ {right['title']}")
        try:
            judgment = judge_pair(args, vault, left, right)
        except UserError as error:
            failed += 1
            warnings.append(f"judgment failed for {candidate['left']} ↔ {candidate['right']}: {error}")
            run_state.append_jsonl_fsync(run_dir / "judged.jsonl", {**candidate, "error": str(error)})
            continue
        run_state.append_jsonl_fsync(run_dir / "judged.jsonl", {**candidate, **judgment})
        if not judgment["connect"]:
            rejected += 1
            continue
        proposals.append(
            {
                "id": f"c-{len(proposals) + 1:03d}",
                "action": "link",
                "left": candidate["left"],
                "right": candidate["right"],
                "leftTitle": left["title"],
                "rightTitle": right["title"],
                "leftLink": f"[[{left['stem']}]]",
                "rightLink": f"[[{right['stem']}]]",
                "leftSha256": left["sha256"],
                "rightSha256": right["sha256"],
                "similarity": candidate["similarity"],
                "strength": judgment["strength"],
                "kind": judgment["kind"],
                "reason": judgment["reason"],
            }
        )
        if index % JUDGE_BATCH_STATE == 0:
            run_state.update_run_state(run_dir, lambda state: state.update({"phase": "judging"}) or state)

    counts = {
        "notes_indexed": len(entries),
        "candidate_pairs": len(candidates),
        "eligible_pairs": len(filtered),
        "judged": len(selected),
        "proposed": len(proposals),
        "model_rejected": rejected,
        "judgment_failed": failed,
        **{f"skipped_{key}": value for key, value in skipped.items()},
    }
    finish_run(run_dir, proposals, counts, histogram, warnings, vault, "connection proposals")
    return structured(
        "ok",
        artifacts=[str(run_dir / "report.md"), str(run_dir / "proposals.jsonl")],
        warnings=warnings,
        data={"runDirectory": str(run_dir), "counts": counts, "proposals": proposals},
    )


def command_wiki(args):
    vault = resolve_vault(args)
    schema_path, schema, schema_hash = load_schema(args, vault)
    entries, store, embedding_info, warnings = ensure_index(args, vault, schema_path)
    if WIKI_DOMAIN not in schema["domains"]:
        raise UserError(
            f"the schema note has no '{WIKI_DOMAIN}' domain. Add the domain and its subdomains "
            f"({', '.join(sorted(set(WIKI_KIND_SUBDOMAIN.values())))}) to the schema note first."
        )

    started = time.time()
    known_stems = {Path(rel).stem.casefold(): rel for rel in entries}
    # A registered project's wikilink is not a wiki entity. If its note is missing,
    # that is a gap in the project tree for vault-organizer, not a concept stub.
    registered_projects = {project_name(value).casefold(): value for value in schema["projects"]}
    targets = unresolved_targets(entries, args.min_mentions)
    proposals = []
    blocked = []
    directory_candidates = []
    project_candidates = []
    skipped = 0
    ordered_targets = sorted(targets.items(), key=lambda row: (-len(row[1]["sources"]), row[0]))
    for index, (_, record) in enumerate(ordered_targets, 1):
        if args.limit and len(proposals) >= args.limit:
            break
        display, sources = record["display"], record["sources"]
        if display.casefold() in registered_projects:
            project_candidates.append(
                {"title": display, "project": registered_projects[display.casefold()], "mentions": len(sources)}
            )
            continue
        progress(f"[{WORKFLOW}] classifying {index}/{len(targets)}: {display} ({len(sources)} mentions)")
        mention_lines = [f"- {entries[rel]['title']} ({entries[rel].get('domain') or 'unfiled'})" for rel in sources[:12]]
        try:
            classified = classify_target(args, display, mention_lines)
        except UserError as error:
            warnings.append(f"classification failed for {display}: {error}")
            continue
        if classified["kind"] == "skip":
            skipped += 1
            continue
        if classified["kind"] in DIRECTORY_KINDS:
            directory_candidates.append({"title": classified["title"], "kind": classified["kind"], "mentions": len(sources)})
            continue
        collision = known_stems.get(classified["title"].casefold())
        if collision:
            blocked.append(
                {
                    "action": "blocked",
                    "title": classified["title"],
                    "reason": f"a note with this basename already exists at `{collision}` — link to it instead of creating a stub",
                }
            )
            continue
        destination = wiki_destination(schema, classified["kind"], classified["title"])
        if (vault / destination).exists():
            blocked.append({"action": "blocked", "title": classified["title"], "reason": f"`{destination}` already exists"})
            continue
        proposals.append(
            {
                "id": f"w-{len(proposals) + 1:03d}",
                "action": "create_wiki_note",
                "title": classified["title"],
                "kind": classified["kind"],
                "summary": classified["summary"],
                "destination": destination,
                "mentions": sources,
                "mentionCount": len(sources),
                "content": stub_note_text(schema, classified["title"], classified["kind"], classified["summary"], sources),
            }
        )

    backfill, backfill_warnings = backfill_proposals(args, vault, schema, entries, store, len(proposals))
    warnings.extend(backfill_warnings)
    proposals.extend(backfill)

    counts = {
        "notes_indexed": len(entries),
        "unresolved_targets": len(targets),
        "stubs_proposed": sum(1 for item in proposals if item["action"] == "create_wiki_note"),
        "backfill_proposed": len(backfill),
        "directory_candidates": len(directory_candidates),
        "registered_project_notes_missing": len(project_candidates),
        "blocked_by_collision": len(blocked),
        "classified_skip": skipped,
        "elapsed": format_duration(time.time() - started),
    }
    run_dir = unique_run_directory(vault)
    run_state.initialize_run_state(
        run_dir,
        run_state.create_run_state(
            WORKFLOW, "wiki", {"vault": str(vault), "schemaHash": schema_hash}, resolved_options(args), phase="proposed"
        ),
    )
    run_state.atomic_write_json(
        run_dir / "other-candidates.json",
        {"directory": directory_candidates, "registeredProjects": project_candidates},
    )
    finish_run(
        run_dir,
        proposals + blocked,
        counts,
        None,
        warnings,
        vault,
        "wiki layer",
        extra=[
            (
                "People and organizations for 08 Directory, not created here",
                directory_candidates,
                lambda row: f"- **{row['title']}** ({row['kind']}) — {row['mentions']} mentions",
            ),
            (
                "Registered projects whose project note is missing",
                project_candidates,
                lambda row: f"- **{row['title']}** — {row['mentions']} mentions; registered as `{row['project']}`. Create the project note with vault-organizer, not here.",
            ),
        ],
    )
    return structured(
        "ok",
        artifacts=[str(run_dir / "report.md"), str(run_dir / "proposals.jsonl")],
        warnings=warnings,
        data={
            "runDirectory": str(run_dir),
            "counts": counts,
            "proposals": proposals,
            "blocked": blocked,
            "directoryCandidates": directory_candidates,
            "registeredProjectsMissingNotes": project_candidates,
        },
    )


def backfill_proposals(args, vault, schema, entries, store, offset):
    """Link existing wiki notes into the notes that correspond to them."""
    warnings = []
    wiki = wiki_notes(schema, entries)
    if not wiki:
        return [], warnings
    decided = load_decisions(vault)
    proposals = []
    for rel, entry in sorted(wiki.items()):
        vector = vector_for(store, entry["body_hash"])
        matches = []
        for other_rel, other in sorted(entries.items()):
            if other_rel == rel or other_rel in wiki or other_rel.startswith(INBOX_DIR + "/"):
                continue
            if already_linked(entry, other):
                continue
            if pair_key(rel, other_rel) in decided:
                continue
            mentions = entry["stem"].casefold() in other["search_text"].casefold()
            other_vector = vector_for(store, other["body_hash"])
            similarity = forge_embeddings.cosine(vector, other_vector) if vector is not None and other_vector is not None else 0.0
            if mentions or similarity >= args.min_similarity:
                matches.append((similarity, mentions, other_rel, other))
        matches.sort(key=lambda row: (-row[1], -row[0]))
        for similarity, mentions, other_rel, other in matches[:args.per_note]:
            proposals.append(
                {
                    "id": f"b-{len(proposals) + 1:03d}",
                    "action": "link",
                    "left": rel,
                    "right": other_rel,
                    "leftTitle": entry["title"],
                    "rightTitle": other["title"],
                    "leftLink": f"[[{entry['stem']}]]",
                    "rightLink": f"[[{other['stem']}]]",
                    "leftSha256": entry["sha256"],
                    "rightSha256": other["sha256"],
                    "similarity": round(similarity, 6),
                    "strength": "strong" if mentions else "moderate",
                    "kind": "shared-entity",
                    "reason": "names this wiki note in its text" if mentions else "closely related to this wiki note",
                }
            )
    return proposals, warnings


def finish_run(run_dir, proposals, counts, histogram, warnings, vault, mode, extra=None):
    # Always create the file, so a run with no proposals is still inspectable by
    # status and rejected cleanly by apply rather than looking like a broken run.
    (run_dir / "proposals.jsonl").touch()
    for proposal in proposals:
        run_state.append_jsonl_fsync(run_dir / "proposals.jsonl", proposal)
    write_report(run_dir, proposals, counts, histogram, warnings, vault, mode, extra)
    run_state.update_run_state(
        run_dir,
        lambda state: state.update({"phase": "proposed", "status": "awaiting-review", "nextAction": "apply --accept <ids>"}) or state,
        event={"type": "proposals_written", "count": len(proposals)},
    )


def command_apply(args):
    vault = resolve_vault(args)
    schema_path, schema, _ = load_schema(args, vault)
    run_dir = Path(args.run).expanduser().resolve()
    if not (run_dir / "proposals.jsonl").is_file():
        raise UserError(f"no proposals.jsonl in {run_dir}")
    rows, _ = run_state.read_jsonl_recover_tail(run_dir / "proposals.jsonl", repair=True)
    by_id = {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}

    accepted_ids = split_ids(args.accept)
    rejected_ids = split_ids(args.reject)
    unknown = sorted((set(accepted_ids) | set(rejected_ids)) - set(by_id))
    if unknown:
        raise UserError(f"unknown proposal ids: {', '.join(unknown)}")
    if not accepted_ids and not rejected_ids:
        raise UserError("apply needs --accept <ids> and/or --reject <ids>")

    warnings = []
    applied = []
    edits = {}
    creates = []
    for proposal_id in accepted_ids:
        proposal = by_id[proposal_id]
        if proposal["action"] == "link":
            edits.setdefault(proposal["left"], []).append((proposal_id, proposal["rightLink"]))
            edits.setdefault(proposal["right"], []).append((proposal_id, proposal["leftLink"]))
        elif proposal["action"] == "create_wiki_note":
            creates.append(proposal)
        else:
            warnings.append(f"{proposal_id} is not an applicable proposal ({proposal['action']})")

    results = {"notes_updated": 0, "links_added": 0, "notes_created": 0, "skipped": 0}
    for proposal in creates:
        destination = vault / proposal["destination"]
        if destination.exists():
            warnings.append(f"{proposal['id']}: {proposal['destination']} already exists; not overwritten")
            results["skipped"] += 1
            continue
        if args.dry_run:
            applied.append({"id": proposal["id"], "action": "create", "path": proposal["destination"], "dryRun": True})
            results["notes_created"] += 1
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        run_state.atomic_write_text(destination, proposal["content"])
        run_state.append_jsonl_fsync(run_dir / "apply-log.jsonl", {"id": proposal["id"], "operation": "create", "path": proposal["destination"], "status": "ok"})
        applied.append({"id": proposal["id"], "action": "create", "path": proposal["destination"]})
        results["notes_created"] += 1

    for rel, items in sorted(edits.items()):
        path = vault / rel
        if not path.is_file():
            warnings.append(f"{rel} no longer exists; skipped")
            results["skipped"] += 1
            continue
        data = path.read_bytes()
        merged, added, reason = merge_related(data, [link for _, link in items], schema)
        if merged is None:
            warnings.append(f"{rel}: {reason}")
            results["skipped"] += 1
            continue
        if args.dry_run:
            applied.append({"action": "link", "path": rel, "added": added, "dryRun": True})
            results["notes_updated"] += 1
            results["links_added"] += len(added)
            continue
        backup = run_dir / "backup" / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        atomic_write_bytes(path, merged)
        run_state.append_jsonl_fsync(
            run_dir / "apply-log.jsonl",
            {"operation": "merge_related", "path": rel, "added": added, "sha256Before": sha256_bytes(data), "status": "ok"},
        )
        applied.append({"action": "link", "path": rel, "added": added})
        results["notes_updated"] += 1
        results["links_added"] += len(added)

    if not args.dry_run:
        for proposal_id in accepted_ids:
            record_decision(vault, decision_key(by_id[proposal_id]), "accepted", proposal_id)
        for proposal_id in rejected_ids:
            record_decision(vault, decision_key(by_id[proposal_id]), "rejected", proposal_id)
        run_state.update_run_state(
            run_dir,
            lambda state: state.update({"phase": "applied", "status": "complete"}) or state,
            event={"type": "applied", "accepted": len(accepted_ids), "rejected": len(rejected_ids)},
        )
        refresh_notes_index(vault, schema_path)

    return structured(
        "ok",
        artifacts=[str(run_dir / "apply-log.jsonl")] if not args.dry_run else [],
        warnings=warnings,
        data={
            "runDirectory": str(run_dir),
            "dryRun": args.dry_run,
            "accepted": accepted_ids,
            "rejected": rejected_ids,
            "results": results,
            "operations": applied,
        },
    )


def split_ids(value):
    if not value:
        return []
    return [item.strip() for item in re.split(r"[,\s]+", value) if item.strip()]


def command_status(args):
    run_dir = Path(args.run).expanduser().resolve()
    state = run_state.load_run_state(run_dir, WORKFLOW)
    proposals, _ = run_state.read_jsonl_recover_tail(run_dir / "proposals.jsonl")
    applied, _ = run_state.read_jsonl_recover_tail(run_dir / "apply-log.jsonl")
    return structured(
        "ok",
        data={
            "runDirectory": str(run_dir),
            "workflow": state.get("workflow"),
            "command": state.get("command"),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "nextAction": state.get("nextAction"),
            "proposals": len(proposals),
            "applied": len(applied),
        },
    )


def command_doctor(args):
    vault = resolve_vault(args)
    checks = {"vault": {"ok": os.access(vault, os.W_OK), "path": str(vault)}}
    ok = checks["vault"]["ok"]
    try:
        schema_path, schema, schema_hash = load_schema(args, vault)
        checks["schema"] = {
            "ok": True,
            "path": str(schema_path),
            "schemaHash": schema_hash,
            "domains": len(schema["domains"]),
            "wikiDomain": WIKI_DOMAIN in schema["domains"],
            "wikiSubdomains": sorted(schema["subdomains"].get(WIKI_DOMAIN, {})),
        }
        if WIKI_DOMAIN not in schema["domains"]:
            checks["schema"]["detail"] = f"no '{WIKI_DOMAIN}' domain yet; search and propose work, wiki does not"
    except UserError as error:
        checks["schema"] = {"ok": False, "detail": str(error)}
        ok = False

    chat = {"ok": False, "url": args.base_url, "model": args.model}
    try:
        started = time.time()
        request_json(
            args.base_url,
            args.model,
            args.api_key,
            min(args.request_timeout, 60),
            with_prefill(args, [
                {"role": "system", "content": 'Reply with exactly {"ok": true} as JSON.'},
                {"role": "user", "content": "ping"},
            ]),
            cache_prompt=False,
        )
        chat["ok"] = True
        chat["seconds"] = round(time.time() - started, 2)
    except (UserError, json.JSONDecodeError) as error:
        chat["detail"] = str(error)
    checks["chat"] = chat
    ok = ok and chat["ok"]

    probe = forge_embeddings.embeddings_doctor(url=args.embeddings_url, model=args.embeddings_model)
    checks["embeddings"] = {"ok": probe["reachable"], "url": probe["url"], "model": probe["model"], "detail": probe["detail"]}
    ok = ok and probe["reachable"]

    store = load_vectors(vault, args.embeddings_model)
    checks["vectorStore"] = {"ok": True, "cachedVectors": len(store["rows"]), "dimensions": store["dims"]}
    return structured("ok" if ok else "error", data={"checks": checks})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def resolved_options(args):
    return {
        "model": args.model,
        "baseUrl": args.base_url,
        "embeddingsModel": args.embeddings_model,
        "embeddingsUrl": args.embeddings_url,
        "thinkPrefill": args.think_prefill,
        "perNote": args.per_note,
        "minSimilarity": args.min_similarity,
        "maxSimilarity": args.max_similarity,
        "prefer": args.prefer,
        "maxCandidates": args.max_candidates,
        "minMentions": args.min_mentions,
        "limit": args.limit,
        "promptVersion": PROMPT_VERSION,
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Search an Obsidian vault, propose note connections, and maintain a wiki entity layer.")
    parser.add_argument("command", choices=["index", "search", "propose", "wiki", "apply", "status", "doctor"])
    parser.add_argument("query", nargs="?", help="search query (search only)")
    parser.add_argument("--vault")
    parser.add_argument("--schema")
    parser.add_argument("--run", help="run directory (apply, status)")
    parser.add_argument("--accept", help="comma-separated proposal ids to apply")
    parser.add_argument("--reject", help="comma-separated proposal ids to record as rejected")
    parser.add_argument("--dry-run", action="store_true", help="show what apply would write without writing")
    parser.add_argument("--limit", type=int, help="cap judged pairs (propose) or classified targets (wiki)")
    parser.add_argument("--search-limit", type=int, default=DEFAULT_SEARCH_LIMIT)
    parser.add_argument("--per-note", type=int, default=DEFAULT_PER_NOTE)
    parser.add_argument("--min-similarity", type=float, default=DEFAULT_MIN_SIMILARITY)
    parser.add_argument("--max-similarity", type=float, default=DEFAULT_MAX_SIMILARITY, help="pairs at or above this are near-duplicates, not connections")
    parser.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    parser.add_argument(
        "--prefer",
        choices=["cross-domain", "similarity"],
        default="cross-domain",
        help="rank candidates by cross-cutting interest (default) or by raw similarity",
    )
    parser.add_argument("--min-mentions", type=int, default=DEFAULT_MIN_MENTIONS)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    parser.add_argument("--request-timeout", type=float, default=120)
    parser.add_argument("--embeddings-url")
    parser.add_argument("--embeddings-model")
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument("--think-prefill", action="store_true", help="prefill an empty think block (for thinking backends like :8008)")
    args = parser.parse_args(argv)

    if args.command == "search" and not args.query:
        raise UserError("search requires a query argument")
    if args.command in {"apply", "status"} and not args.run:
        raise UserError(f"{args.command} requires --run <run-directory>")
    if args.command == "status":
        return args
    if not args.vault:
        raise UserError(f"{args.command} requires --vault")
    if args.limit is not None and args.limit < 1:
        raise UserError("--limit must be at least 1")
    if args.per_note < 1:
        raise UserError("--per-note must be at least 1")
    if not 0 < args.min_similarity <= 1:
        raise UserError("--min-similarity must be within (0, 1]")

    args.base_url = normalize_base_url(
        args.base_url or os.environ.get("VAULT_CONNECTIONS_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or DEFAULT_BASE_URL
    )
    args.model = args.model or os.environ.get("VAULT_CONNECTIONS_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    args.api_key = args.api_key or os.environ.get("VAULT_CONNECTIONS_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.embeddings_url = forge_embeddings.endpoint_url(args.embeddings_url)
    args.embeddings_model = forge_embeddings.model_name(args.embeddings_model)
    args.cache_prompt = not args.no_cache_prompt
    return args


COMMANDS = {
    "index": command_index,
    "search": command_search,
    "propose": command_propose,
    "wiki": command_wiki,
    "apply": command_apply,
    "status": command_status,
    "doctor": command_doctor,
}


def run(argv):
    args = parse_args(argv)
    result = COMMANDS[args.command](args)
    print_json(result)
    return 0 if result["status"] == "ok" else 1


def main(argv=None):
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 1
    except Exception as error:  # noqa: BLE001 - CLI boundary
        print_json(structured("error", errors=[error_entry("internal_error", f"{type(error).__name__}: {error}")]))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
