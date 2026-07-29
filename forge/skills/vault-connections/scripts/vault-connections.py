#!/usr/bin/env python3
"""Semantic search, reviewed research imports, and a wiki entity layer for an Obsidian vault.

Companion to ``vault-organizer``. The organizer decides where a note *lives*;
this decides what a note is *connected to* and can propose completed research
artifacts as new vault notes. It never moves, renames, or deletes existing notes.

Existing-note mutations remain additive frontmatter merges. Imported notes are
new, explicitly accepted files with schema-ordered frontmatter and source bodies
preserved exactly.
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
import subprocess
import sys
import tempfile
import time
from array import array
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings
import forge_llm
import forge_verify
import run_state
import vault_profile
import vault_voice
from vault_classification import (
    DEFAULT_BASE_URL,
    build_messages as classification_messages,
    chat_service,
    request_json_with_retry as classification_request,
    validate_classification,
)
from vault_schema import (
    FRONTMATTER_KEY_RE,
    INBOX_DIR,
    LIST_ITEM_RE,
    RESERVED_WINDOWS_NAMES,
    UserError,
    compile_destination,
    compiled_schema_for,
    link_basename,
    normalize_body_for_hash,
    note_title,
    parse_frontmatter,
    parse_schema_note,
    path_is_inside,
    project_name,
    relative_path,
    resolve_schema_path,
    safe_title,
    selected_notes,
    serialize_frontmatter,
    sha256_bytes,
    sha256_file,
    sha256_text,
    split_flow_items,
    split_frontmatter,
    strip_yaml_scalar,
    valid_wikilink,
    validate_filename_title,
    wikilink_target,
    yaml_scalar,
)

WORKFLOW = "vault-connections"
STATE_DIR = ".vault-connections"
DEFAULT_MODEL = "chat"
PROMPT_VERSION = "vault-connections-v2"

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
# One research run is a handful of subjects, not a whole shelf. Past this the
# notes stop being about one thing each.
DEFAULT_SUBTOPIC_NOTES = 6
DEFAULT_SEARCH_LIMIT = 10
SEARCH_RRF_K = 60
MIN_BODY_CHARS = 80
MAX_REASON_CHARS = 200
MAX_TRANSIENT_ATTEMPTS = 3
MAX_STUB_RELATED = 8
JUDGE_BATCH_STATE = 20
ENTITY_BATCH_MAX_RECORDS = 100
ENTITY_BATCH_MAX_CHARS = 30000

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
    "figure": "figures",
}
WIKI_KIND_TYPE = {
    "concept": "concept",
    "practice": "concept",
    "place": "place",
    "event": "event",
    "term": "concept",
    "work": "work",
    "figure": "person",
}
WIKI_TEMPLATE_NAMES = {
    "concept": "Wiki Concept.md",
    "practice": "Wiki Practice.md",
    "place": "Wiki Place.md",
    "event": "Wiki Event.md",
    "term": "Wiki Term.md",
    "work": "Wiki Work.md",
    "figure": "Wiki Figure.md",
}
WIKI_TEMPLATE_FIELDS = ("title", "summary", "evidence", "sources", "provenance")
DEFAULT_WIKI_KINDS = ("concept", "term")
IMPORT_DEFAULT_ARTIFACTS = {
    "literature": ("literature_summary.md", "key_terms.md"),
    "meta-literature": ("meta_synthesis.md", "concept_register.md"),
    "deep-research": ("deep_research_report.md",),
}
IMPORT_ARTIFACT_ROLES = {
    "literature_summary.md": "Literature Overview",
    "key_terms.md": "Key Terms",
    "meta_synthesis.md": "Meta Synthesis",
    "concept_register.md": "Concept Register",
    "deep_research_report.md": "Deep Research Report",
}
DIRECTORY_KINDS = ("person", "organization")

THINK_PREFILL = "<think>\n\n</think>\n\n"
THINK_BLOCK_RE = forge_llm.THINK_BLOCK_RE
WIKILINK_RE = re.compile(r"\[\[([^\]\r\n]+)\]\]")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
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


def atomic_create_bytes(path, data):
    """Atomically publish a new file without ever replacing an existing path."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, destination)
        os.unlink(temporary)
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
# Wikilink and frontmatter helpers
#
# The frontmatter reader itself lives in vault_schema, so every vault skill
# reads a note's properties the same way; merge_related still edits the
# frontmatter text directly rather than round-tripping through it.
# --------------------------------------------------------------------------- #


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


def extract_json_content(content):
    text = forge_llm.extract_json_content(content)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise UserError(f"model did not return a JSON object: {text[:200]}")
    return json.loads(text[start:end + 1])


def request_with_retry(args, messages, service=None):
    last = None
    for attempt in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
        try:
            content, _record = forge_llm.call(
                service or chat_service(args),
                messages,
                cache_prompt=args.cache_prompt,
                timeout=args.request_timeout,
                api_key=args.api_key,
                task="judge",
            )
            return extract_json_content(content)
        except (forge_llm.ChatError, UserError, json.JSONDecodeError) as error:
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
    "The most valuable connections carry a concept from one area of a person's life into\n"
    "another — an idea from their reading explaining a problem in their work. Rate those\n"
    "'strong'. Whatever background you are given about the vault owner is what tells you\n"
    "which cross-domain moves are theirs; without it, judge the shared idea on its merits.\n"
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
    "- figure: a person treated as a recurring subject in the wiki.\n"
    "- person / organization: an individual human, or an institution, company, lab, or group.\n"
    "- skip: a file path, a date, a fragment, a typo, or anything too vague to define.\n"
    "\n"
    "title is the canonical display name — fix capitalization, expand nothing you are not sure\n"
    "of, and keep it as a filename-safe line. summary states what the thing is, in the vault\n"
    "owner's context, using only what the surrounding mentions support. Never invent facts; if\n"
    "the mentions do not tell you what it is, return kind 'skip'."
)


def load_voice(args, vault):
    voice_path = vault_voice.resolve_voice_path(
        vault,
        getattr(args, "voice", None),
        disabled=getattr(args, "no_voice", False),
    )
    voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path, cache_dir=cache_dir(vault))
    args.compiled_voice = voice
    args.voice_path = voice_path
    args.voice_hash = voice_hash
    return voice_path, voice, voice_hash


def load_profile(args, vault):
    """Compile the personal-context layer, or carry on without it.

    Unlike the voice policy, a broken register never fails the run: the
    warnings go to the report and every stage behaves as it did before.
    """
    profile_path = vault_profile.resolve_profile_path(
        vault,
        getattr(args, "profile", None),
        disabled=getattr(args, "no_profile", False),
    )
    profile, profile_hash, warnings = vault_profile.compiled_profile_for(vault, profile_path, cache_dir=cache_dir(vault))
    args.compiled_profile = profile
    args.profile_path = profile_path
    args.profile_hash = profile_hash
    args.profile_warnings = warnings
    return profile_path, profile, profile_hash


def judge_site(*entries):
    """Where a judgment sits: the owner's own notes, filed where the index says.

    Both notes are the owner's and the output is a short reason written into
    the owner's own vault, so the mode is ``owner`` even though the voice
    policy treats this stage as source-derived — that axis is about a note's
    provenance, which is a different question. Routes are the union, so a
    therapy-gated card enters only when a therapy note is actually in the pair.
    """
    routes = []
    for entry in entries:
        domain = (entry or {}).get("domain")
        subdomain = (entry or {}).get("subdomain")
        if domain:
            routes.append(f"{domain}/{subdomain}" if subdomain else domain)
    return vault_profile.profile_site(vault_voice.CONTEXT_OWNER, routes=routes, stage="judge")


def connection_system(args):
    """The judgment system prompt, byte-stable for a run so the cache holds."""
    prefix = vault_profile.profile_prefix(
        getattr(args, "compiled_profile", None),
        vault_profile.profile_site(vault_voice.CONTEXT_OWNER, stage="judge"),
    )
    return f"{CONNECTION_SYSTEM}\n\n{prefix}" if prefix else CONNECTION_SYSTEM


def source_system(args, base):
    prefix = vault_voice.prompt_prefix(getattr(args, "compiled_voice", None), vault_voice.CONTEXT_SOURCE)
    return f"{base}\n\n{prefix}" if prefix else base


def source_context(args, note_type, material):
    compiled = vault_voice.compile_voice(
        getattr(args, "compiled_voice", None),
        vault_voice.CONTEXT_SOURCE,
        note_type=note_type,
        material=material,
    )
    return {
        "styleForThisKind": compiled["per_type_rule"],
        "relevantVocabulary": compiled["vocabulary"],
    }


def note_brief(entry, body):
    location = " / ".join(part for part in [entry.get("domain"), entry.get("subdomain")] if part) or "unfiled"
    excerpt = re.sub(r"\n{3,}", "\n\n", body).strip()[:JUDGE_EXCERPT_CHARS]
    return f"title: {entry['title']}\npath: {entry['path']}\nfiled under: {location}\n---\n{excerpt}"


def judge_pair(args, vault, left, right):
    left_body = normalize_body_for_hash(split_frontmatter((vault / left["path"]).read_bytes())["body"])
    right_body = normalize_body_for_hash(split_frontmatter((vault / right["path"]).read_bytes())["body"])
    user = f"NOTE A\n{note_brief(left, left_body)}\n\nNOTE B\n{note_brief(right, right_body)}"
    # Per-pair cards go in the user message: they vary with the pair, and the
    # system message has to stay byte-identical for the server-side prefix cache.
    cards = vault_profile.select_cards(
        getattr(args, "compiled_profile", None),
        f"{left['title']}\n{left_body}\n{right['title']}\n{right_body}",
        judge_site(left, right),
    )
    context = [card for card in cards if card["tier"] != vault_profile.TIER_ALWAYS]
    if context:
        user += "\n\nABOUT THE VAULT OWNER\n" + json.dumps(vault_profile.profile_offers(context), ensure_ascii=False)
    messages = with_prefill(
        args,
        [
            {"role": "system", "content": connection_system(args)},
            {"role": "user", "content": user},
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


def load_decisions(vault, repair=True):
    rows, _ = run_state.read_jsonl_recover_tail(decisions_path(vault), repair=repair)
    return {row["key"] for row in rows if isinstance(row, dict) and row.get("key")}


def pair_key(left, right):
    return "|".join(sorted([left, right]))


def decision_key(proposal):
    if proposal.get("action") == "link":
        return pair_key(proposal["left"], proposal["right"])
    if proposal.get("sourceRunFingerprint") and proposal.get("destination"):
        return f"import:{proposal['sourceRunFingerprint']}:{proposal['destination'].casefold()}"
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
    material = f"{target}\n" + "\n".join(mention_lines)
    messages = with_prefill(
        args,
        [
            {"role": "system", "content": source_system(args, WIKI_SYSTEM)},
            {
                "role": "user",
                "content": "LINK TARGET: "
                + target
                + "\n\nMENTIONED IN:\n"
                + "\n".join(mention_lines)
                + "\n\nVOICE POLICY FOR GENERATED SOURCE PROSE:\n"
                + json.dumps(source_context(args, "concept", material), ensure_ascii=False),
            },
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


def clean_summary(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:600]


def stub_note_text(schema, title, kind, summary, mentions):
    metadata = {
        "type": WIKI_KIND_TYPE[kind],
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
# Reviewed research imports
# --------------------------------------------------------------------------- #


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserError(f"could not read JSON from {path}: {error}") from error


def read_jsonl(path):
    try:
        rows, warnings = run_state.read_jsonl_recover_tail(path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise UserError(f"could not read JSONL from {path}: {error}") from error
    if warnings:
        raise UserError(f"{path} has an incomplete JSONL record")
    return rows


def detect_source_run(run_directory):
    markers = []
    if (run_directory / "meta_config.json").is_file() and (run_directory / "meta_items.jsonl").is_file():
        markers.append("meta-literature")
    if (run_directory / "run_config.json").is_file() and (run_directory / "item_index.jsonl").is_file():
        markers.append("literature")
    if (run_directory / "research_run.json").is_file() and (run_directory / "claim_register.jsonl").is_file():
        markers.append("deep-research")
    if not markers:
        raise UserError(
            f"unsupported run directory: {run_directory}; expected a completed literature, "
            "meta-literature, or deep-research run"
        )
    if len(markers) != 1:
        raise UserError(f"ambiguous run directory has markers for: {', '.join(markers)}")
    return markers[0]


def invoke_upstream_validator(run_directory, run_type):
    skills_root = Path(__file__).resolve().parents[2]
    if run_type == "literature":
        command = [
            sys.executable,
            str(skills_root / "literature-extraction" / "scripts" / "literature-extraction.py"),
            "validate",
            str(run_directory),
            "--json",
            "--read-only",
        ]
    elif run_type == "meta-literature":
        command = [
            sys.executable,
            str(skills_root / "literature-extraction" / "scripts" / "literature-extraction.py"),
            "meta-validate",
            str(run_directory),
            "--json",
            "--read-only",
        ]
    else:
        command = [
            "node",
            str(skills_root / "web-research" / "scripts" / "web-research.mjs"),
            "validate",
            str(run_directory),
            "--read-only",
        ]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    output = completed.stdout.strip()
    try:
        validation = json.loads(output) if output else {}
    except json.JSONDecodeError as error:
        raise UserError(f"{run_type} validator returned invalid JSON: {output[:300]}") from error
    if completed.returncode != 0 or not validation.get("valid"):
        details = validation.get("errors") or [completed.stderr.strip() or "validator failed"]
        raise UserError(f"{run_type} run is invalid: {'; '.join(str(item) for item in details)}")
    if run_type != "deep-research" and not validation.get("complete"):
        raise UserError(f"{run_type} run is incomplete")
    if run_type == "deep-research":
        state_path = run_directory / "run_state.json"
        if not state_path.is_file():
            raise UserError("deep-research run has no run_state.json")
        if read_json(state_path).get("status") != "complete":
            raise UserError("deep-research run is not complete")
    return validation


def template_folder(schema):
    if "meta" not in schema["domains"] or "templates" not in schema["subdomains"].get("meta", {}):
        raise UserError("the schema does not define the required meta/templates route")
    return compile_destination(schema, {"domain": "meta", "subdomain": "templates"})


def inspect_wiki_template(vault, schema, kind):
    relative = template_folder(schema) / WIKI_TEMPLATE_NAMES[kind]
    path = vault / relative
    result = {"kind": kind, "path": relative.as_posix(), "ok": False, "errors": []}
    if not path.is_file():
        result["errors"].append(f"missing template: {path}")
        return result
    if path.is_symlink() or not path_is_inside(vault, path.resolve()):
        result["errors"].append(f"template must be a vault-owned regular file: {path}")
        return result
    split = split_frontmatter(path.read_bytes())
    if not split["had_frontmatter"] or split["malformed"]:
        result["errors"].append(f"template has invalid frontmatter: {path}")
        return result
    metadata = parse_frontmatter(split["frontmatter_text"])
    required = {
        "type": "template",
        "status": "active",
        "domain": "meta",
        "subdomain": "templates",
        "capture_type": "manual",
    }
    for key, value in required.items():
        if metadata.get(key) != value:
            result["errors"].append(f"{path} requires {key}: {value}")
    body = split["body"]
    for field in WIKI_TEMPLATE_FIELDS:
        if f"{{{{{field}}}}}" not in body:
            result["errors"].append(f"{path} is missing {{{{{field}}}}}")
    unknown = sorted(set(re.findall(r"\{\{([^{}\r\n]+)\}\}", body)) - set(WIKI_TEMPLATE_FIELDS))
    if unknown:
        result["errors"].append(f"{path} has unknown placeholders: {', '.join(unknown)}")
    result["ok"] = not result["errors"]
    result["sha256"] = sha256_file(path)
    result["body"] = body
    return result


def require_wiki_templates(vault, schema, kinds):
    templates = {}
    errors = []
    for kind in kinds:
        result = inspect_wiki_template(vault, schema, kind)
        if result["ok"]:
            templates[kind] = result
        else:
            errors.extend(result["errors"])
    if errors:
        raise UserError("wiki template readiness failed: " + "; ".join(errors))
    return templates


def existing_basenames(vault, schema_path):
    return {
        path.stem.casefold(): relative_path(vault, path)
        for path in selected_notes(vault, schema_path, "vault", None)
    }


def source_title_prefix(run_directory, run_type, explicit):
    if explicit is not None:
        return validate_filename_title(explicit, "--title-prefix")
    if run_type == "literature":
        config = read_json(run_directory / "run_config.json")
        source = config.get("input")
        if isinstance(source, dict):
            source = source.get("path") or source.get("root") or source.get("name")
        candidate = Path(str(source)).stem if source else run_directory.name
    elif run_type == "meta-literature":
        config = read_json(run_directory / "meta_config.json")
        candidate = config.get("researchQuestion") or config.get("research_question") or run_directory.name
    else:
        config = read_json(run_directory / "research_run.json")
        candidate = config.get("question") or config.get("researchQuestion") or run_directory.name
    cleaned = safe_title(str(candidate))
    if not cleaned:
        raise UserError(f"could not derive a safe title from {run_directory}")
    return cleaned


def selected_import_artifacts(run_directory, run_type, additions):
    names = list(IMPORT_DEFAULT_ARTIFACTS[run_type])
    for value in additions or []:
        if value not in names:
            names.append(value)
    selected = []
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix.casefold() != ".md":
            raise UserError(f"unsafe import artifact path: {name}")
        path = (run_directory / relative).resolve()
        if not path_is_inside(run_directory, path) or not path.is_file():
            raise UserError(f"import artifact is missing or outside the run: {name}")
        selected.append((relative.as_posix(), path))
    return selected


def source_support_files(run_directory, run_type, artifacts):
    names = {
        "literature": ("run_config.json", "run_state.json", "item_index.jsonl"),
        "meta-literature": ("meta_config.json", "run_state.json", "meta_items.jsonl", "meta_artifacts.jsonl"),
        "deep-research": (
            "research_run.json",
            "run_state.json",
            "source_index.json",
            "evidence_items.jsonl",
            "claim_register.jsonl",
        ),
    }[run_type]
    paths = {name: run_directory / name for name in names if (run_directory / name).is_file()}
    paths.update({name: path for name, path in artifacts})
    return [{"path": str(path), "relativePath": name, "sha256": sha256_file(path)} for name, path in sorted(paths.items())]


def add_markdown_table_context(records, path):
    if not path.is_file() or not records:
        return
    record_by_id = {record["id"]: record for record in records}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        raise UserError(f"could not read candidate table {path}: {error}") from error
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|") or re.fullmatch(r"[\s|:-]+", stripped):
            continue
        for identifier, record in record_by_id.items():
            if identifier not in stripped:
                continue
            text = re.sub(r"\s+", " ", stripped.strip("|").replace("|", " — ")).strip()
            if text and text not in record["text"]:
                record["text"] = f"{record['text']}\nTable context: {text}".strip()


def import_source_records(run_directory, run_type):
    records = []
    if run_type == "literature":
        for row in read_jsonl(run_directory / "item_index.jsonl"):
            item_id = row.get("itemId")
            if not item_id:
                continue
            records.append(
                {
                    "id": item_id,
                    "kind": row.get("itemType"),
                    "text": row.get("itemText") or row.get("interpretation") or "",
                    "quote": row.get("directQuotes") or "",
                    "sourceIds": [value for value in [row.get("documentId")] if value],
                    "source": row.get("sourceTitle") or row.get("sourcePath") or "",
                }
            )
        add_markdown_table_context(records, run_directory / "key_terms.md")
    elif run_type == "meta-literature":
        for row in read_jsonl(run_directory / "meta_items.jsonl"):
            item_id = row.get("itemId")
            if not item_id:
                continue
            records.append(
                {
                    "id": item_id,
                    "kind": row.get("itemType"),
                    "text": row.get("itemText") or row.get("text") or "",
                    "quote": row.get("directQuotes") or row.get("directQuote") or "",
                    "sourceIds": [value for value in [row.get("documentId"), row.get("metaSourceId")] if value],
                    "source": row.get("sourceTitle") or row.get("sourcePath") or "",
                }
            )
        add_markdown_table_context(records, run_directory / "concept_register.md")
    else:
        evidence = read_jsonl(run_directory / "evidence_items.jsonl")
        claims = read_jsonl(run_directory / "claim_register.jsonl")
        for row in claims:
            if row.get("claimId"):
                records.append(
                    {
                        "id": row["claimId"],
                        "kind": "claim",
                        "text": row.get("text") or "",
                        "quote": "",
                        "sourceIds": row.get("sourceIds") or [],
                        "evidenceIds": row.get("evidenceIds") or [],
                        "source": "",
                        "confidence": row.get("confidence") or "",
                        # The source run's own reviewer already judged this. Its
                        # verdict travels with the record so a note can leave a
                        # doubted claim out and say that it did.
                        "verification": row.get("verification") or None,
                    }
                )
        for row in evidence:
            if row.get("evidenceId"):
                records.append(
                    {
                        "id": row["evidenceId"],
                        "kind": "evidence",
                        "text": row.get("text") or "",
                        "quote": row.get("directQuote") or "",
                        "sourceIds": [value for value in [row.get("sourceId")] if value],
                        "source": "",
                        "confidence": row.get("confidence") or "",
                        "verification": row.get("verification") or None,
                    }
                )
    return records


ENTITY_SYSTEM = (
    "You identify wiki entities in validated research records. Return exactly one JSON object with an entities array. "
    "Every entity must contain kind, title, summary, evidenceIds, and sourceIds. Use only the allowed kinds and IDs "
    "provided by the user. evidenceIds must cite at least one record ID; sourceIds may contain only source IDs attached "
    "to those cited records. "
    "Do not infer unsupported facts or invent identifiers. Return no entity when the records do not support one."
)


def entity_record_batches(records, limit):
    max_records = ENTITY_BATCH_MAX_RECORDS
    if limit:
        max_records = min(max_records, max(1, limit * 10))
    batches = []
    current = []
    current_chars = 0
    for record in records:
        prompt_record = dict(record)
        for key, maximum in (("text", 8000), ("quote", 4000), ("source", 1000)):
            value = prompt_record.get(key)
            if isinstance(value, str):
                prompt_record[key] = value[:maximum]
            elif value is not None:
                prompt_record[key] = json.dumps(value, ensure_ascii=False)[:maximum]
        record_chars = len(json.dumps(prompt_record, ensure_ascii=False))
        if current and (len(current) >= max_records or current_chars + record_chars > ENTITY_BATCH_MAX_CHARS):
            batches.append(current)
            current = []
            current_chars = 0
        current.append((record, prompt_record))
        current_chars += record_chars
    if current:
        batches.append(current)
    return batches


def harvest_entity_candidates(args, records, kinds, limit=None):
    if not records:
        return [], ["no structured records were available for wiki candidate harvesting"]
    warnings = []
    merged = {}
    entity_number = 0
    for batch in entity_record_batches(records, limit):
        batch_records = [record for record, _ in batch]
        payload = {
            "allowedKinds": list(kinds),
            "records": [prompt_record for _, prompt_record in batch],
            "responseShape": {
                "entities": [
                    {
                        "kind": "concept",
                        "title": "Canonical title",
                        "summary": "Evidence-grounded summary",
                        "evidenceIds": ["upstream-record-id"],
                        "sourceIds": ["source-id-attached-to-that-record"],
                    }
                ]
            },
        }
        raw = request_with_retry(
            args,
            with_prefill(
                args,
                [
                    {"role": "system", "content": source_system(args, ENTITY_SYSTEM)},
                    {
                        "role": "user",
                        "content": "RESEARCH ENTITY CANDIDATES\n"
                        + json.dumps(
                            {
                                **payload,
                                "voicePolicy": source_context(
                                    args, "source", json.dumps(payload["records"], ensure_ascii=False)
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
            ),
        )
        entities = raw.get("entities") if isinstance(raw, dict) else None
        if not isinstance(entities, list):
            raise UserError("entity harvesting response must contain an entities array")
        record_by_id = {record["id"]: record for record in batch_records}
        record_ids = set(record_by_id)
        known_source_ids = {
            source_id
            for record in batch_records
            for source_id in (record.get("sourceIds") or [])
            if isinstance(source_id, str)
        }
        for raw_entity in entities:
            entity_number += 1
            if not isinstance(raw_entity, dict) or raw_entity.get("kind") not in kinds:
                warnings.append(f"discarded entity {entity_number}: kind is not selected")
                continue
            raw_title = raw_entity.get("title")
            if not isinstance(raw_title, str) or safe_title(raw_title) != raw_title.strip():
                warnings.append(f"discarded entity {entity_number}: title is filename-unsafe")
                continue
            title = raw_title.strip()
            if not title or title.casefold() in RESERVED_WINDOWS_NAMES:
                warnings.append(f"discarded entity {entity_number}: title is empty or reserved")
                continue
            evidence_ids = raw_entity.get("evidenceIds")
            source_ids = raw_entity.get("sourceIds") or []
            if (
                not isinstance(evidence_ids, list)
                or not evidence_ids
                or any(not isinstance(value, str) or value not in record_ids for value in evidence_ids)
                or not isinstance(source_ids, list)
                or any(not isinstance(value, str) or value not in known_source_ids for value in source_ids)
            ):
                warnings.append(f"discarded entity {title}: unsupported provenance IDs")
                continue
            related_source_ids = {
                source_id
                for identifier in evidence_ids
                for source_id in (record_by_id[identifier].get("sourceIds") or [])
            }
            if any(source_id not in related_source_ids for source_id in source_ids):
                warnings.append(f"discarded entity {title}: source IDs are unrelated to cited records")
                continue
            summary = clean_summary(raw_entity.get("summary"))
            if not summary:
                warnings.append(f"discarded entity {title}: summary is empty")
                continue
            key = title.casefold()
            candidate = merged.setdefault(
                key,
                {
                    "kind": raw_entity["kind"],
                    "title": title,
                    "summary": summary,
                    "evidenceIds": [],
                    "sourceIds": [],
                },
            )
            if candidate["kind"] != raw_entity["kind"]:
                warnings.append(f"merged duplicate entity {title} using kind {candidate['kind']}")
            for value in evidence_ids:
                if value not in candidate["evidenceIds"]:
                    candidate["evidenceIds"].append(value)
            for value in source_ids:
                if value not in candidate["sourceIds"]:
                    candidate["sourceIds"].append(value)
        if limit and len(merged) >= limit:
            break
    candidates = list(merged.values())
    return candidates[:limit] if limit else candidates, warnings


def render_wiki_entity(schema, template, candidate, records, source_run, source_fingerprint):
    record_by_id = {record["id"]: record for record in records}
    evidence_lines = []
    for identifier in candidate["evidenceIds"]:
        record = record_by_id.get(identifier)
        text = clean_summary((record or {}).get("quote") or (record or {}).get("text") or "")
        evidence_lines.append(f"- `{identifier}`" + (f" — {text}" if text else ""))
    sources = candidate["sourceIds"]
    if not sources:
        sources = sorted(
            {
                source_id
                for identifier in candidate["evidenceIds"]
                for source_id in (record_by_id.get(identifier) or {}).get("sourceIds", [])
            }
        )
    replacements = {
        "title": candidate["title"],
        "summary": candidate["summary"],
        "evidence": "\n".join(evidence_lines) or "- No supported evidence.",
        "sources": "\n".join(f"- `{identifier}`" for identifier in sources) or "- No separate source ID.",
        "provenance": (
            f"- Source run: `{source_run}`\n"
            f"- Source fingerprint: `{source_fingerprint}`\n"
            f"- Upstream IDs: {', '.join(f'`{value}`' for value in candidate['evidenceIds'])}"
        ),
    }
    body = template["body"]
    for key, value in replacements.items():
        body = body.replace(f"{{{{{key}}}}}", value)
    metadata = {
        "type": WIKI_KIND_TYPE[candidate["kind"]],
        "status": "active",
        "domain": WIKI_DOMAIN,
        "subdomain": WIKI_KIND_SUBDOMAIN[candidate["kind"]],
        "capture_type": "generated",
    }
    return serialize_frontmatter(
        {key: value for key, value in metadata.items() if key in schema["properties"]},
        schema,
    ) + body


def classify_import_artifact(args, schema, relative_name, path):
    split = split_frontmatter(path.read_bytes())
    if split["malformed"]:
        raise UserError(f"artifact has malformed frontmatter: {relative_name}")
    return classify_import_body(args, schema, relative_name, note_title(path, split["body"]), split)


def classify_import_body(args, schema, relative_name, title, split):
    messages = classification_messages(
        schema,
        title,
        relative_name,
        split["frontmatter_text"],
        split["body"][:30000],
        think_prefill=args.think_prefill,
    )
    raw = classification_request(args, messages)
    prepared = prepare_import_classification(raw)
    classified, warnings, errors = validate_classification(prepared, schema)
    if errors:
        repair = {"original_response": raw, "validation_errors": errors}
        raw = classification_request(
            args,
            classification_messages(
                schema,
                title,
                relative_name,
                split["frontmatter_text"],
                split["body"][:30000],
                repair=repair,
                think_prefill=args.think_prefill,
            ),
        )
        prepared = prepare_import_classification(raw)
        classified, repair_warnings, errors = validate_classification(prepared, schema)
        warnings.extend(repair_warnings)
    if errors:
        raise UserError(f"schema classification failed for {relative_name}: {'; '.join(errors)}")
    if classified["needs_review"]:
        raise UserError(
            f"schema classification needs review for {relative_name}: "
            f"{classified.get('review_reason') or 'no reason supplied'}"
        )
    return serialize_frontmatter(classified["metadata"], schema) + split["body"], warnings


def prepare_import_classification(response):
    if not isinstance(response, dict) or not isinstance(response.get("metadata"), dict):
        return response
    prepared = json.loads(json.dumps(response))
    metadata = prepared["metadata"]
    metadata["status"] = "complete"
    metadata["capture_type"] = "generated"
    if metadata.get("type") == "source":
        metadata["source_kind"] = "generated"
    else:
        metadata.pop("source_kind", None)
    return prepared


# --------------------------------------------------------------------------- #
# Subtopic notes from a deep-research run
#
# A completed deep run already produces one long report. That is the right shape
# for reading once and the wrong shape for a vault, where a note is found by
# being about one thing. These build one note per subtopic instead, each carrying
# the claims it covers, the quotes behind them, and where they came from.
# --------------------------------------------------------------------------- #

TOPIC_NOTES_SYSTEM = (
    "You group the claims from one research run into the notes they should become in a personal knowledge vault. "
    "A note is about one thing, and is found later by someone looking for that thing. "
    "Work through the claims and group the ones that answer the same question, describe the same mechanism, or "
    "cover the same entity. Check separately for: definitions and background; findings and results; disagreement "
    "between sources; methods; limitations; and practical implications. A group is a note. "
    "Every claim belongs to exactly one note, and no note repeats what another covers. Never create a note that "
    "summarizes the run as a whole: the run's own report already does that. "
    "Return exactly one JSON object: "
    '{"notes": [{"title": "What the note is about", "claimIds": ["cl-0001"]}]}. '
    "Titles name the subject in plain words, under 60 characters, with no characters that would break a filename."
)

NOTE_SUMMARY_SYSTEM = (
    "You write the opening paragraph of a research note for a personal knowledge vault. "
    "You are given the note's title and the claims it covers, each with the quotes behind it. "
    "Write one paragraph saying what this note establishes and how confident the evidence is. "
    "Use only what the claims say. Never add a fact, number, date, or name that is not in them, and never cite a "
    "source that is not listed. Where the claims disagree or the support is thin, say so plainly. "
    'Return exactly one JSON object: {"summary": "the paragraph"}.'
)

VERIFY_NOTES_SYSTEM = (
    "You are reviewing research notes before they are proposed for someone's Obsidian vault. "
    "Each item shows a note's title, its opening summary, and every claim it covers with the quotes behind them. "
    "Flag a note only when: the summary states something the claims do not support; it presents contested findings "
    "as settled; its title does not describe what the note actually covers; or the claims grouped together are not "
    "about the same thing. "
    "Do not flag a note because you would have grouped the claims differently, written the summary differently, or "
    "included more. A narrow, hedged note is doing its job."
)


def deep_run_sources(run_directory):
    """Source id -> {url, title}, so a note can say where a quote came from."""
    path = run_directory / "source_index.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    sources = {}
    for source in payload.get("sources") or []:
        source_id = source.get("sourceId")
        if source_id:
            sources[source_id] = {
                "url": source.get("finalUrl") or source.get("sourceUrl") or "",
                "title": source.get("title") or "",
            }
    return sources


def deep_run_claims(records):
    claims = {record["id"]: record for record in records if record.get("kind") == "claim"}
    evidence = {record["id"]: record for record in records if record.get("kind") == "evidence"}
    return claims, evidence


def flagged_ids(records):
    """Records the source run's own reviewer rejected."""
    return {
        record["id"]
        for record in records
        if (record.get("verification") or {}).get("verdict") == "flag"
    }


def claim_detail(claim, evidence, sources, flagged=frozenset()):
    """One claim with the quotes and URLs behind it, for a prompt or a note.

    Evidence the source run's reviewer rejected is dropped here rather than
    carried into a note. A flag on an evidence item is a finding about the claim
    that cites it: the claim itself may have passed review only because the
    reviewer was judging its wording, not the extraction underneath it.
    """
    quotes = []
    dropped = []
    for evidence_id in claim.get("evidenceIds") or []:
        item = evidence.get(evidence_id)
        if not item:
            continue
        if evidence_id in flagged:
            dropped.append(evidence_id)
            continue
        quote = (item.get("quote") or "").strip()
        source_id = (item.get("sourceIds") or [None])[0]
        quotes.append(
            {
                "evidenceId": evidence_id,
                "quote": quote or (item.get("text") or "").strip(),
                "exact": bool(quote),
                "url": sources.get(source_id, {}).get("url", ""),
                "sourceId": source_id or "",
            }
        )
    return {
        "claimId": claim["id"],
        "text": claim.get("text") or "",
        "quotes": quotes,
        "droppedEvidenceIds": dropped,
    }


def validate_topic_notes(value, claims, limit):
    """The model groups; this decides what is representable.

    Unassigned claims are not dropped — they become one final note — because a
    claim the run bothered to register is a claim the vault should be able to
    find.
    """
    if not isinstance(value, dict) or not isinstance(value.get("notes"), list):
        raise UserError("subtopic grouping response has no notes list")
    seen_claims = set()
    notes = []
    for position, entry in enumerate(value["notes"], start=1):
        if not isinstance(entry, dict):
            raise UserError(f"note {position} is not an object")
        title = entry.get("title")
        if not isinstance(title, str) or not safe_title(title).strip():
            raise UserError(f"note {position} has no usable title")
        title = safe_title(title)[:60].strip()
        claim_ids = [value for value in (entry.get("claimIds") or []) if isinstance(value, str)]
        claim_ids = [claim_id for claim_id in claim_ids if claim_id in claims]
        if not claim_ids:
            continue
        duplicated = [claim_id for claim_id in claim_ids if claim_id in seen_claims]
        if duplicated:
            raise UserError(f"note {position} reuses claims already covered: {', '.join(duplicated[:3])}")
        seen_claims.update(claim_ids)
        notes.append({"title": title, "claimIds": claim_ids})
    if limit:
        notes = notes[:limit]
        seen_claims = {claim_id for note in notes for claim_id in note["claimIds"]}
    leftover = [claim_id for claim_id in claims if claim_id not in seen_claims]
    if leftover:
        notes.append({"title": "Further Findings", "claimIds": leftover, "fallback": True})
    if not notes:
        raise UserError("subtopic grouping produced no notes")
    titles = [note["title"].casefold() for note in notes]
    if len(set(titles)) != len(titles):
        raise UserError("subtopic grouping returned two notes with the same title")
    return notes


def render_subtopic_note(note, summary, claims, evidence, sources, run_directory, fingerprint, flagged=frozenset()):
    """Deterministic body. The model writes the summary; code writes everything
    that has to be exact."""
    lines = ["## Synthesis", "", summary.strip(), "", "## Findings", ""]
    used_sources = []
    dropped_quotes = []
    for claim_id in note["claimIds"]:
        detail = claim_detail(claims[claim_id], evidence, sources, flagged)
        dropped_quotes.extend(detail["droppedEvidenceIds"])
        confidence = claims[claim_id].get("confidence") or ""
        lines.append(f"- {detail['text']}" + (f" (confidence: {confidence})" if confidence else ""))
        for quote in detail["quotes"]:
            if not quote["quote"]:
                continue
            citation = f" — {quote['url']}" if quote["url"] else ""
            lines.append(f'  - "{quote["quote"]}"{citation}')
            if quote["url"] and quote["url"] not in used_sources:
                used_sources.append(quote["url"])
    if used_sources:
        lines.extend(["", "## Sources", ""])
        lines.extend(f"- {url}" for url in used_sources)
    lines.extend(
        [
            "",
            "## Provenance",
            "",
            f"- Source run: `{run_directory}`",
            f"- Source fingerprint: `{fingerprint}`",
            f"- Claims: {', '.join(f'`{claim_id}`' for claim_id in note['claimIds'])}",
        ]
    )
    excluded = [claim_id for claim_id in note.get("excludedClaimIds") or []]
    if excluded:
        lines.append(f"- Claims excluded as flagged in review: {', '.join(f'`{claim_id}`' for claim_id in excluded)}")
    if dropped_quotes:
        lines.append(f"- Quotes excluded as flagged in review: {', '.join(f'`{item}`' for item in dropped_quotes)}")
    return "\n".join(lines) + "\n"


def check_subtopic_note(note, summary, claims, evidence):
    """Deterministic gate, run before the thinking model sees anything."""
    problems = []
    if not summary.strip():
        problems.append("the summary is empty")
    if "\n\n" in summary.strip():
        problems.append("the summary is more than one paragraph")
    for claim_id in note["claimIds"]:
        if claim_id not in claims:
            problems.append(f"cites a claim that is not in the run: {claim_id}")
    for claim_id in note["claimIds"]:
        for evidence_id in claims.get(claim_id, {}).get("evidenceIds") or []:
            if evidence_id not in evidence:
                problems.append(f"{claim_id} cites evidence that is not in the run: {evidence_id}")
    try:
        validate_filename_title(note["title"], "note title")
    except UserError as error:
        problems.append(str(error))
    return problems


def harvest_subtopic_notes(args, run_directory, records, fingerprint, limit=None):
    """Group a deep run's claims into notes and write each one's summary."""
    claims, evidence = deep_run_claims(records)
    if not claims:
        return [], ["no claims were available to build subtopic notes from"]
    warnings = []
    # Anything the upstream reviewer rejected is left out of a note body and
    # said so under Provenance, rather than quietly carried into the vault.
    # A claim goes too when every piece of evidence under it was rejected:
    # the claim's own verdict judged its wording, not the extraction beneath it.
    flagged_claims = flagged_ids(claims.values())
    flagged_evidence = flagged_ids(evidence.values())
    unsupported = {
        claim_id
        for claim_id, claim in claims.items()
        if (claim.get("evidenceIds") or []) and set(claim["evidenceIds"]) <= flagged_evidence
    }
    excluded = {claim_id: claims[claim_id] for claim_id in flagged_claims | unsupported}
    usable = {claim_id: claim for claim_id, claim in claims.items() if claim_id not in excluded}
    if flagged_claims:
        warnings.append(f"{len(flagged_claims)} claim(s) flagged in the source run were left out of the notes")
    if unsupported - flagged_claims:
        warnings.append(
            f"{len(unsupported - flagged_claims)} claim(s) were left out because every piece of evidence "
            "under them was flagged in the source run"
        )
    if flagged_evidence:
        warnings.append(f"{len(flagged_evidence)} quote(s) flagged in the source run were left out of the notes")
    if not usable:
        return [], warnings + ["every claim in the source run was flagged in review"]
    sources = deep_run_sources(run_directory)
    grouping = classification_request(
        args,
        [
            {"role": "system", "content": TOPIC_NOTES_SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "maxNotes": limit or DEFAULT_SUBTOPIC_NOTES,
                        "claims": [
                            {"claimId": claim_id, "text": claim.get("text") or "", "confidence": claim.get("confidence")}
                            for claim_id, claim in usable.items()
                        ],
                    },
                    ensure_ascii=False,
                ),
            },
        ],
    )
    notes = validate_topic_notes(grouping, usable, limit or DEFAULT_SUBTOPIC_NOTES)
    built = []
    for note in notes:
        details = [claim_detail(usable[claim_id], evidence, sources, flagged_evidence) for claim_id in note["claimIds"]]
        summary_value = classification_request(
            args,
            [
                {"role": "system", "content": source_system(args, NOTE_SUMMARY_SYSTEM)},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "title": note["title"],
                            "claims": details,
                            "voicePolicy": source_context(
                                args, "source", json.dumps(details, ensure_ascii=False)
                            ),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        )
        summary = summary_value.get("summary") if isinstance(summary_value, dict) else None
        summary = summary.strip() if isinstance(summary, str) else ""
        problems = check_subtopic_note(note, summary, usable, evidence)
        if problems:
            warnings.append(f"{note['title']}: held back, {'; '.join(problems)}")
            continue
        note = dict(note, excludedClaimIds=sorted(excluded))
        built.append(
            {
                "title": note["title"],
                "summary": summary,
                "claimIds": note["claimIds"],
                "fallback": bool(note.get("fallback")),
                "body": render_subtopic_note(
                    note, summary, usable, evidence, sources, run_directory, fingerprint, flagged_evidence
                ),
                "verifyItem": {"title": note["title"], "summary": summary, "claims": details},
            }
        )
    return built, warnings


def verify_subtopic_notes(args, notes, run_dir):
    """Review the notes on the thinking model, annotating rather than dropping."""
    if not notes or not args.verify:
        return {"skipped": "verification disabled" if notes else None}, []
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    if not think["enabled"]:
        return {"skipped": "no thinking service is configured"}, ["subtopic notes were not verified"]
    items = [{"id": note["title"], **note["verifyItem"]} for note in notes]
    try:
        verdicts = forge_verify.verify_packets(
            think,
            VERIFY_NOTES_SYSTEM,
            items,
            journal_path=run_dir / "verified-notes.jsonl",
            background=True,
            timeout=args.request_timeout,
        )
    except forge_verify.VerificationError as error:
        # An unreachable reviewer must never read as approval.
        return {"skipped": str(error)}, [f"subtopic notes were not verified: {error}"]
    warnings = []
    for note in notes:
        verdict = verdicts.get(note["title"])
        if verdict and verdict["verdict"] == forge_verify.VERDICT_FLAG:
            note["needsReview"] = verdict["reason"]
            warnings.append(f"{note['title']}: flagged in review, {verdict['reason']}")
    return forge_verify.summarize(verdicts), warnings


def subtopic_report_line(proposal):
    line = f"- `{proposal['id']}` **{proposal['title']}** → `{proposal['destination']}`"
    line += f"\n  - {len(proposal['claimIds'])} claim(s): {', '.join(proposal['claimIds'][:6])}"
    if proposal.get("needsReview"):
        line += f"\n  - **flagged in review**: {proposal['needsReview']}"
    return line


def verification_report_rows(verification):
    """A section saying what the reviewer did, including when it did nothing."""
    if verification is None:
        return []
    if verification.get("skipped"):
        return [
            (
                "Verification",
                [f"Nothing was reviewed: {verification['skipped']}. That is not the same as approval."],
                lambda row: f"- {row}",
            )
        ]
    rows = [
        f"Reviewed by the thinking model: {verification.get('verified', 0)}",
        f"Accepted: {verification.get('ok', 0)}",
        f"Flagged for your attention: {verification.get('flagged', 0)}",
    ]
    return [("Verification", rows, lambda row: f"- {row}")]


def command_import_run(args):
    vault = resolve_vault(args, initialize_state=False)
    schema_path, schema, schema_hash = load_schema(args, vault, use_cache=False)
    voice_path, _voice, voice_hash = load_voice(args, vault)
    run_directory = Path(args.query).expanduser().resolve()
    if not run_directory.is_dir():
        raise UserError(f"run directory does not exist: {run_directory}")
    if args.limit is not None and args.limit < 1:
        raise UserError("--limit must be at least 1")
    run_type = detect_source_run(run_directory)
    validation = invoke_upstream_validator(run_directory, run_type)
    warnings = [f"validator: {warning}" for warning in validation.get("warnings") or []]
    kinds = tuple(dict.fromkeys(split_ids(args.wiki_kinds)))
    unknown_kinds = sorted(set(kinds) - set(WIKI_KIND_SUBDOMAIN))
    if unknown_kinds:
        raise UserError(f"unknown --wiki-kinds: {', '.join(unknown_kinds)}")
    if not kinds:
        raise UserError("--wiki-kinds must select at least one kind")
    # Wiki entity notes are rendered from vault-owned templates, and a vault
    # that has not written them yet cannot have them invented. That is a hard
    # failure for the wiki half — but subtopic notes use no templates at all, so
    # a --notes run degrades to the half it can actually do rather than
    # refusing work the vault is ready for.
    try:
        templates = require_wiki_templates(vault, schema, kinds)
    except UserError as error:
        if not args.notes:
            raise
        templates = None
        warnings.append(f"wiki notes skipped: {error}")
    artifacts = selected_import_artifacts(run_directory, run_type, args.include_artifact)
    source_files = source_support_files(run_directory, run_type, artifacts)
    source_fingerprint = run_state.configuration_fingerprint(
        {"runType": run_type, "files": [{"path": item["relativePath"], "sha256": item["sha256"]} for item in source_files]}
    )
    prefix = source_title_prefix(run_directory, run_type, args.title_prefix)
    basenames = existing_basenames(vault, schema_path)
    planned = set()
    decided = load_decisions(vault, repair=False)
    inbox_proposals = []
    blocked = []
    for relative_name, path in artifacts:
        role = IMPORT_ARTIFACT_ROLES.get(Path(relative_name).name) or safe_title(Path(relative_name).stem.replace("_", " ").title())
        filename = f"{prefix} - {role}.md"
        validate_filename_title(Path(filename).stem, f"destination for {relative_name}")
        destination = (Path(INBOX_DIR) / filename).as_posix()
        collision = basenames.get(Path(filename).stem.casefold())
        if collision or Path(filename).stem.casefold() in planned:
            blocked.append(
                {
                    "action": "blocked",
                    "title": Path(filename).stem,
                    "reason": f"case-insensitive basename collision with `{collision or destination}`",
                }
            )
            continue
        content, classification_warnings = classify_import_artifact(args, schema, relative_name, path)
        warnings.extend(f"{relative_name}: {warning}" for warning in classification_warnings)
        proposal = {
            "id": f"i-{len(inbox_proposals) + 1:03d}",
            "action": "create_inbox_note",
            "title": Path(filename).stem,
            "destination": destination,
            "sourceArtifact": relative_name,
            "sourceArtifactSha256": sha256_file(path),
            "sourceRunFingerprint": source_fingerprint,
            "content": content,
        }
        if decision_key(proposal) in decided:
            warnings.append(f"previously decided import proposal suppressed: {destination}")
            continue
        planned.add(Path(filename).stem.casefold())
        inbox_proposals.append(proposal)

    records = import_source_records(run_directory, run_type)
    run_dir = unique_run_directory(vault)
    note_proposals = []
    verification = None
    if args.notes:
        if run_type != "deep-research":
            raise UserError(f"--notes builds subtopic notes from a deep-research run; this is a {run_type} run")
        built, note_warnings = harvest_subtopic_notes(args, run_directory, records, source_fingerprint, args.notes_limit)
        warnings.extend(note_warnings)
        verification, verify_warnings = verify_subtopic_notes(args, built, run_dir)
        warnings.extend(verify_warnings)
        for note in built:
            filename = f"{prefix} - {note['title']}.md"
            validate_filename_title(Path(filename).stem, f"destination for {note['title']}")
            destination = (Path(INBOX_DIR) / filename).as_posix()
            collision = basenames.get(Path(filename).stem.casefold())
            if collision or Path(filename).stem.casefold() in planned:
                blocked.append(
                    {
                        "action": "blocked",
                        "title": Path(filename).stem,
                        "reason": f"case-insensitive basename collision with `{collision or destination}`",
                    }
                )
                continue
            body = note["body"]
            if note.get("needsReview"):
                body = f"> [!warning] Flagged in review\n> {note['needsReview']}\n\n{body}"
            content, classification_warnings = classify_import_body(
                args,
                schema,
                Path(filename).stem,
                note["title"],
                {"malformed": False, "frontmatter_text": "", "body": body, "had_frontmatter": False},
            )
            warnings.extend(f"{note['title']}: {warning}" for warning in classification_warnings)
            proposal = {
                "id": f"n-{len(note_proposals) + 1:03d}",
                "action": "create_inbox_note",
                "title": Path(filename).stem,
                "destination": destination,
                "claimIds": note["claimIds"],
                "needsReview": note.get("needsReview"),
                "sourceRunFingerprint": source_fingerprint,
                "content": content,
            }
            if decision_key(proposal) in decided:
                warnings.append(f"previously decided import proposal suppressed: {destination}")
                continue
            planned.add(Path(filename).stem.casefold())
            note_proposals.append(proposal)

    candidates = []
    if templates is not None:
        candidates, candidate_warnings = harvest_entity_candidates(args, records, kinds, limit=args.limit)
        warnings.extend(candidate_warnings)
    wiki_proposals = []
    for candidate in candidates:
        key = candidate["title"].casefold()
        destination = wiki_destination(schema, candidate["kind"], candidate["title"])
        collision = basenames.get(key)
        if collision or key in planned:
            blocked.append(
                {
                    "action": "blocked",
                    "title": candidate["title"],
                    "reason": f"case-insensitive basename collision with `{collision or destination}`",
                }
            )
            continue
        content = render_wiki_entity(
            schema,
            templates[candidate["kind"]],
            candidate,
            records,
            run_directory,
            source_fingerprint,
        )
        proposal = {
            "id": f"w-{len(wiki_proposals) + 1:03d}",
            "action": "create_wiki_note",
            "title": candidate["title"],
            "kind": candidate["kind"],
            "summary": candidate["summary"],
            "destination": destination,
            "evidenceIds": candidate["evidenceIds"],
            "sourceIds": candidate["sourceIds"],
            "mentionCount": len(candidate["evidenceIds"]),
            "sourceRunFingerprint": source_fingerprint,
            "content": content,
        }
        if decision_key(proposal) in decided:
            warnings.append(f"previously decided import proposal suppressed: {destination}")
            continue
        planned.add(key)
        wiki_proposals.append(proposal)

    proposals = inbox_proposals + note_proposals + wiki_proposals
    input_config = {
        "vault": str(vault),
        "schemaPath": str(schema_path),
        "schemaHash": schema_hash,
        "sourceRun": str(run_directory),
        "sourceRunType": run_type,
        "sourceRunFingerprint": source_fingerprint,
        "sourceFiles": source_files,
        "templateFiles": [
            {"kind": kind, "path": templates[kind]["path"], "sha256": templates[kind]["sha256"]}
            for kind in (kinds if templates is not None else ())
        ],
        **vault_voice.voice_state(voice_path, voice_hash, vault_voice.CONTEXT_SOURCE),
    }
    run_state.initialize_run_state(
        run_dir,
        run_state.create_run_state(WORKFLOW, "import-run", input_config, resolved_options(args), phase="proposed"),
    )
    counts = {
        "inbox_notes_proposed": len(inbox_proposals),
        "subtopic_notes_proposed": len(note_proposals),
        "wiki_notes_proposed": len(wiki_proposals),
        "blocked_by_collision": len(blocked),
        "validator_warnings": len(validation.get("warnings") or []),
    }
    extra = None
    if args.notes:
        extra = [("Subtopic notes", note_proposals, subtopic_report_line), *verification_report_rows(verification)]
    finish_run(run_dir, proposals + blocked, counts, None, warnings, vault, f"{run_type} import", extra)
    return structured(
        "ok",
        artifacts=[str(run_dir / "report.md"), str(run_dir / "proposals.jsonl")],
        warnings=warnings,
        data={
            "runDirectory": str(run_dir),
            "sourceRunType": run_type,
            "sourceRunFingerprint": source_fingerprint,
            "counts": counts,
            "verification": verification,
            "proposals": proposals,
            "blocked": blocked,
        },
    )


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
    inbox = [item for item in proposals if item["action"] == "create_inbox_note"]
    stubs = [item for item in proposals if item["action"] == "create_wiki_note"]
    doubted = [item for item in proposals if item.get("verified") == "flag"]
    if doubted:
        report.extend(["", f"## Needs attention ({len(doubted)})", "",
                       "The thinking model doubts these. Still yours to accept or reject — start here.", ""])
        for item in doubted:
            title = item.get("title") or f"{item.get('leftTitle')} ↔ {item.get('rightTitle')}"
            report.append(f"- `{item['id']}` **{title}** — {item.get('verifyReason', '')}")
            report.append(f"  - proposed because: {item.get('reason', '')}")
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


def resolve_vault(args, initialize_state=True):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault directory does not exist: {vault}")
    if initialize_state:
        cache_dir(vault).mkdir(parents=True, exist_ok=True)
    return vault


def load_schema(args, vault, use_cache=True):
    schema_path = resolve_schema_path(vault, args.schema)
    if use_cache:
        schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=cache_dir(vault))
    else:
        schema_bytes = schema_path.read_bytes()
        schema = parse_schema_note(schema_bytes.decode("utf-8-sig"))
        schema_hash = sha256_bytes(schema_bytes)
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
    profile_path, _profile, profile_hash = load_profile(args, vault)
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
            {
                "vault": str(vault),
                "schemaHash": schema_hash,
                **vault_profile.profile_state(
                    profile_path, profile_hash, vault_profile.profile_site(vault_voice.CONTEXT_OWNER, stage="judge")
                ),
            },
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

    verification = annotate_proposals(args, proposals, run_dir, warnings) if args.verify else None
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
    if verification:
        counts["verification_flagged"] = verification["flagged"]
    finish_run(run_dir, proposals, counts, histogram, warnings, vault, "connection proposals")
    return structured(
        "ok",
        artifacts=[str(run_dir / "report.md"), str(run_dir / "proposals.jsonl")],
        warnings=warnings,
        data={"runDirectory": str(run_dir), "counts": counts, "proposals": proposals},
    )


ANNOTATE_SYSTEM = (
    "You are reviewing proposed links between notes in one person's Obsidian vault.\n"
    "A faster model without reasoning judged each pair worth linking and gave a reason.\n"
    "Flag a proposal when the stated reason does not actually justify a link: the two notes\n"
    "merely share vocabulary, the claimed relationship is not supported, or the link would\n"
    "be noise. A genuine connection is 'ok' even if it is obvious or modest."
)


def annotate_proposals(args, proposals, run_dir, warnings):
    """Sort proposals by whether the thinking model agrees they are real links.

    This is annotation, never a gate: every proposal still reaches the human
    review loop, flagged ones just arrive marked. The pair judgments themselves
    are already human-approved before anything is written, so the value here is
    ordering attention, not filtering.
    """
    if not proposals:
        return None
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    if not think["enabled"]:
        return None
    items = [
        {
            "id": proposal["id"],
            "left": proposal["leftTitle"],
            "right": proposal["rightTitle"],
            "strength": proposal["strength"],
            "kind": proposal["kind"],
            "reason": proposal["reason"],
            "similarity": round(proposal["similarity"], 3),
        }
        for proposal in proposals
    ]
    progress(f"[{WORKFLOW}] reviewing {len(items)} proposals on {think['url']}")
    try:
        verdicts = forge_verify.verify_packets(
            think,
            ANNOTATE_SYSTEM,
            items,
            journal_path=run_dir / "verified.jsonl",
            background=True,
            timeout=args.request_timeout,
            progress=progress,
        )
    except forge_verify.VerificationError as error:
        warnings.append(f"proposal review skipped: {error}")
        return None
    for proposal in proposals:
        verdict = verdicts.get(proposal["id"])
        if not verdict:
            continue
        proposal["verified"] = verdict["verdict"]
        if verdict["verdict"] == forge_verify.VERDICT_FLAG:
            proposal["verifyReason"] = verdict["reason"]
    # Flagged first: the human is reviewing ten at a time, so the ones a
    # reasoning model doubts should be in the first ten.
    proposals.sort(key=lambda proposal: (proposal.get("verified") != forge_verify.VERDICT_FLAG, proposal["id"]))
    return forge_verify.summarize(verdicts)


def command_wiki(args):
    vault = resolve_vault(args)
    schema_path, schema, schema_hash = load_schema(args, vault)
    voice_path, _voice, voice_hash = load_voice(args, vault)
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
            WORKFLOW,
            "wiki",
            {
                "vault": str(vault),
                "schemaHash": schema_hash,
                **vault_voice.voice_state(voice_path, voice_hash, vault_voice.CONTEXT_SOURCE),
            },
            resolved_options(args),
            phase="proposed",
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
    proposals_sha256 = sha256_file(run_dir / "proposals.jsonl")
    run_state.update_run_state(
        run_dir,
        lambda state: state.update(
            {
                "phase": "proposed",
                "status": "awaiting-review",
                "nextAction": "apply --accept <ids>",
                "input": {**(state.get("input") or {}), "proposalsSha256": proposals_sha256},
            }
        )
        or state,
        event={"type": "proposals_written", "count": len(proposals)},
    )


def verify_import_state(vault, schema_hash, state, voice_path=None, voice_hash=None):
    recorded = state.get("input") or {}
    errors = []
    recorded_vault = recorded.get("vault")
    if not recorded_vault or Path(recorded_vault).expanduser().resolve() != vault.resolve():
        errors.append(f"apply vault differs from originating vault: {recorded_vault or '<missing>'}")
    if recorded.get("schemaHash") != schema_hash:
        errors.append("schema hash changed after proposal generation")
    if "voice_hash" in recorded:
        current_voice = vault_voice.voice_state(voice_path, voice_hash, recorded.get("voice_context_mode") or "source")
        for key in ("voice_path", "voice_hash", "voice_compiler_version", "voice_context_mode"):
            if recorded.get(key) != current_voice.get(key):
                errors.append(f"{key} changed after proposal generation")
    for item in recorded.get("sourceFiles") or []:
        path = Path(item.get("path") or "")
        if not path.is_file():
            errors.append(f"source artifact is missing: {path}")
        elif sha256_file(path) != item.get("sha256"):
            errors.append(f"source artifact changed: {path}")
    for item in recorded.get("templateFiles") or []:
        path = vault / str(item.get("path") or "")
        if not path.is_file():
            errors.append(f"wiki template is missing: {path}")
        elif path.is_symlink() or not path_is_inside(vault, path.resolve()):
            errors.append(f"wiki template is not a vault-owned regular file: {path}")
        elif sha256_file(path) != item.get("sha256"):
            errors.append(f"wiki template changed: {path}")
    if errors:
        raise UserError("import apply preflight failed: " + "; ".join(errors))


def preflight_import_creates(vault, schema_path, schema, state, creates):
    errors = []
    seen_destinations = set()
    accepted_stems = {}
    existing = existing_basenames(vault, schema_path)
    already_present = set()
    source_fingerprint = (state.get("input") or {}).get("sourceRunFingerprint")
    for proposal in creates:
        destination_rel = proposal.get("destination") or ""
        destination = (vault / destination_rel).resolve()
        key = destination_rel.casefold()
        stem = destination.stem.casefold()
        if not isinstance(proposal.get("content"), str):
            errors.append(f"{proposal['id']} content is not text")
            continue
        if proposal.get("sourceRunFingerprint") != source_fingerprint:
            errors.append(f"{proposal['id']} source-run fingerprint does not match the import run")
        try:
            title = validate_filename_title(proposal.get("title"), f"{proposal['id']} title")
        except UserError as error:
            errors.append(str(error))
            continue
        if proposal.get("action") == "create_inbox_note":
            expected = (Path(INBOX_DIR) / f"{title}.md").as_posix()
        elif proposal.get("action") == "create_wiki_note" and proposal.get("kind") in WIKI_KIND_SUBDOMAIN:
            expected = wiki_destination(schema, proposal["kind"], title)
        else:
            errors.append(f"{proposal['id']} has an invalid import action or wiki kind")
            continue
        if destination_rel != expected:
            errors.append(f"{proposal['id']} destination does not match its schema-compiled route: {destination_rel}")
        if not path_is_inside(vault, destination):
            errors.append(f"{proposal['id']} destination escapes the vault: {destination_rel}")
            continue
        if key in seen_destinations:
            errors.append(f"duplicate accepted destination: {destination_rel}")
        seen_destinations.add(key)
        if stem in accepted_stems and accepted_stems[stem] != key:
            errors.append(f"case-insensitive accepted basename collision: {destination.name}")
        accepted_stems[stem] = key
        collision = existing.get(stem)
        if destination.is_file():
            if destination.read_bytes() == proposal["content"].encode("utf-8"):
                already_present.add(proposal["id"])
                continue
            errors.append(f"{proposal['id']} destination already exists with different content: {destination_rel}")
        elif destination.exists():
            errors.append(f"{proposal['id']} destination exists and is not a file: {destination_rel}")
        elif collision:
            errors.append(f"{proposal['id']} basename collides with existing note: {collision}")
    if errors:
        raise UserError("import apply preflight failed: " + "; ".join(errors))
    return already_present


def preflight_link_edits(vault, schema, proposals):
    errors = []
    for proposal in proposals:
        for path_key, hash_key, link_key in (
            ("left", "leftSha256", "rightLink"),
            ("right", "rightSha256", "leftLink"),
        ):
            relative = proposal.get(path_key) or ""
            path = (vault / relative).resolve()
            if not path_is_inside(vault, path):
                errors.append(f"{proposal['id']} link destination escapes the vault: {relative}")
                continue
            if not path.is_file():
                errors.append(f"{proposal['id']} link source is missing: {relative}")
                continue
            data = path.read_bytes()
            if sha256_bytes(data) == proposal.get(hash_key):
                continue
            _, _, reason = merge_related(data, [proposal.get(link_key) or ""], schema)
            if reason != "already linked":
                errors.append(f"{proposal['id']} link source changed after proposal generation: {relative}")
    if errors:
        raise UserError("link apply preflight failed: " + "; ".join(errors))


def command_apply(args):
    vault = resolve_vault(args)
    schema_path, schema, schema_hash = load_schema(args, vault)
    voice_path, _voice, voice_hash = load_voice(args, vault)
    run_dir = Path(args.run).expanduser().resolve()
    if not (run_dir / "proposals.jsonl").is_file():
        raise UserError(f"no proposals.jsonl in {run_dir}")
    state = run_state.load_run_state(run_dir, WORKFLOW)
    recorded_proposals_hash = (state.get("input") or {}).get("proposalsSha256")
    if state.get("command") == "import-run" and not recorded_proposals_hash:
        raise UserError("import apply preflight failed: proposal manifest hash is missing")
    if recorded_proposals_hash and sha256_file(run_dir / "proposals.jsonl") != recorded_proposals_hash:
        raise UserError("apply preflight failed: proposals changed after proposal generation")
    rows, _ = run_state.read_jsonl_recover_tail(run_dir / "proposals.jsonl", repair=True)
    by_id = {row["id"]: row for row in rows if isinstance(row, dict) and row.get("id")}

    accepted_ids = split_ids(args.accept)
    rejected_ids = split_ids(args.reject)
    conflicting = sorted(set(accepted_ids) & set(rejected_ids))
    if conflicting:
        raise UserError(f"proposal ids cannot be both accepted and rejected: {', '.join(conflicting)}")
    unknown = sorted((set(accepted_ids) | set(rejected_ids)) - set(by_id))
    if unknown:
        raise UserError(f"unknown proposal ids: {', '.join(unknown)}")
    if not accepted_ids and not rejected_ids:
        raise UserError("apply needs --accept <ids> and/or --reject <ids>")

    warnings = []
    applied = []
    edits = {}
    creates = []
    link_proposals = []
    for proposal_id in accepted_ids:
        proposal = by_id[proposal_id]
        if proposal["action"] == "link":
            link_proposals.append(proposal)
            edits.setdefault(proposal["left"], []).append((proposal_id, proposal["rightLink"]))
            edits.setdefault(proposal["right"], []).append((proposal_id, proposal["leftLink"]))
        elif proposal["action"] in {"create_wiki_note", "create_inbox_note"}:
            creates.append(proposal)
        else:
            warnings.append(f"{proposal_id} is not an applicable proposal ({proposal['action']})")

    import_apply = state.get("command") == "import-run"
    already_present = set()
    if link_proposals:
        preflight_link_edits(vault, schema, link_proposals)
    if state.get("command") in {"import-run", "wiki"} and creates:
        verify_import_state(vault, schema_hash, state, voice_path, voice_hash)
    if import_apply and creates:
        already_present = preflight_import_creates(vault, schema_path, schema, state, creates)

    results = {"notes_updated": 0, "links_added": 0, "notes_created": 0, "skipped": 0}
    created_paths = []
    try:
        for proposal in creates:
            destination = vault / proposal["destination"]
            if proposal["id"] in already_present:
                warnings.append(f"{proposal['id']}: identical destination already exists; treated as applied")
                applied.append({"id": proposal["id"], "action": "already-present", "path": proposal["destination"]})
                results["skipped"] += 1
                continue
            if destination.exists():
                warnings.append(f"{proposal['id']}: {proposal['destination']} already exists; not overwritten")
                results["skipped"] += 1
                continue
            if args.dry_run:
                applied.append({"id": proposal["id"], "action": "create", "path": proposal["destination"], "dryRun": True})
                results["notes_created"] += 1
                continue
            atomic_create_bytes(destination, proposal["content"].encode("utf-8"))
            created_paths.append(destination)
            if not import_apply:
                run_state.append_jsonl_fsync(
                    run_dir / "apply-log.jsonl",
                    {"id": proposal["id"], "operation": "create", "path": proposal["destination"], "status": "ok"},
                )
            applied.append({"id": proposal["id"], "action": "create", "path": proposal["destination"]})
            results["notes_created"] += 1
    except BaseException:
        if import_apply:
            for path in reversed(created_paths):
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
        raise
    if import_apply and not args.dry_run:
        for operation in applied:
            if operation["action"] == "create":
                run_state.append_jsonl_fsync(
                    run_dir / "apply-log.jsonl",
                    {"id": operation["id"], "operation": "create", "path": operation["path"], "status": "ok"},
                )

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
    warnings = []
    ok = checks["vault"]["ok"]
    schema = {}
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
        try:
            template_checks = [inspect_wiki_template(vault, schema, kind) for kind in WIKI_KIND_SUBDOMAIN]
        except UserError as error:
            template_checks = [
                {"kind": kind, "path": WIKI_TEMPLATE_NAMES[kind], "ok": False, "errors": [str(error)]}
                for kind in WIKI_KIND_SUBDOMAIN
            ]
        checks["wikiTemplates"] = {
            "ok": all(item["ok"] for item in template_checks),
            "ready": [item["kind"] for item in template_checks if item["ok"]],
            "details": [
                {key: value for key, value in item.items() if key not in {"body", "sha256"}}
                for item in template_checks
            ],
            "requiredFor": "import-run only",
        }
    except UserError as error:
        checks["schema"] = {"ok": False, "detail": str(error)}
        ok = False

    voice_check = {
        "ok": True,
        "configured": False,
        "stages": {
            "wiki definitions and research-note synthesis": "source",
            "search, link judgment, frontmatter, provenance, imported bodies": "none",
        },
    }
    try:
        voice_path, voice, voice_hash = load_voice(args, vault)
        if voice_path is None:
            voice_check["detail"] = (
                "disabled with --no-voice"
                if getattr(args, "no_voice", False)
                else f"no voice note; default is {vault_voice.DEFAULT_VOICE}"
            )
        else:
            unknown_types = sorted(set(voice.get("per_type", {})) - set(schema.get("types", {})))
            voice_check = {
                "ok": True,
                "configured": True,
                "path": str(voice_path),
                "voice_hash": voice_hash,
                "compiler_version": vault_voice.COMPILED_VOICE_VERSION,
                "recognized_scopes": voice.get("recognized_scopes", []),
                "types_with_style": sorted(voice.get("per_type", {})),
                "unknown_scopes": voice.get("unknown_scopes", []),
                "unknown_schema_types": unknown_types,
                "stages": {
                    "wiki definitions and research-note synthesis": "source",
                    "search, link judgment, frontmatter, provenance, imported bodies": "none",
                },
            }
            if unknown_types:
                warnings.append("voice note has unknown schema note types: " + ", ".join(unknown_types))
    except UserError as error:
        voice_check = {"ok": False, "configured": True, "detail": str(error)}
        warnings.append(f"voice note could not be read: {error}")
    checks["voice"] = voice_check
    ok = ok and voice_check["ok"]

    # The profile never fails a run, so it never fails doctor either: a broken
    # register reports itself as a warning and the run judges without it.
    profile_check = {"ok": True, "configured": False, "stages": {"link judgment": "owner"}}
    try:
        profile_path, profile, profile_hash = load_profile(args, vault)
        if profile_path is None:
            profile_check["detail"] = (
                "disabled with --no-profile"
                if getattr(args, "no_profile", False)
                else f"no personal context note; default is {vault_profile.DEFAULT_PROFILE}"
            )
        else:
            digest = vault_profile.profile_digest(profile)
            profile_check = {
                "ok": True,
                "configured": profile is not None,
                "path": str(profile_path),
                "profile_hash": profile_hash,
                "compiler_version": vault_profile.COMPILED_PROFILE_VERSION,
                "cards": digest,
                "budgets": {
                    "prefix": vault_profile.DEFAULT_PREFIX_BUDGET,
                    "context": vault_profile.DEFAULT_CONTEXT_BUDGET,
                    "per_card": vault_profile.MAX_CARD_CHARS,
                },
                "stages": {"link judgment": "owner"},
            }
        warnings.extend(getattr(args, "profile_warnings", []) or [])
    except UserError as error:
        profile_check = {"ok": True, "configured": True, "detail": str(error)}
        warnings.append(f"personal context note could not be read: {error}")
    checks["profile"] = profile_check

    # One call per candidate pair, so hidden reasoning is charged per pair.
    chat_probe = forge_llm.service_doctor(
        chat_service(args), expect_non_thinking=not args.think_prefill, timeout=min(args.request_timeout, 60)
    )
    chat = {
        "ok": chat_probe["reachable"],
        "url": chat_probe["url"],
        "model": chat_probe["model"],
        "detail": chat_probe.get("detail"),
    }
    for key in ("thinking", "hiddenTokens", "modelMismatch", "servedModels"):
        if key in chat_probe:
            chat[key] = chat_probe[key]
    checks["chat"] = chat
    ok = ok and chat["ok"]
    if chat_probe.get("warning"):
        warnings.append(chat_probe["warning"])

    probe = forge_embeddings.embeddings_doctor(url=args.embeddings_url, model=args.embeddings_model)
    checks["embeddings"] = {"ok": probe["reachable"], "url": probe["url"], "model": probe["model"], "detail": probe["detail"]}
    ok = ok and probe["reachable"]

    store = load_vectors(vault, args.embeddings_model)
    checks["vectorStore"] = {"ok": True, "cachedVectors": len(store["rows"]), "dimensions": store["dims"]}
    return structured("ok" if ok else "error", warnings=warnings, data={"checks": checks})


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
        "wikiKinds": split_ids(args.wiki_kinds),
        "includeArtifacts": args.include_artifact,
        "titlePrefix": args.title_prefix,
        "promptVersion": PROMPT_VERSION,
        "voice": getattr(args, "voice", None),
        "noVoice": getattr(args, "no_voice", False),
    }


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Search an Obsidian vault, propose connections, import completed research, and maintain a wiki layer."
    )
    parser.add_argument("command", choices=["index", "search", "propose", "wiki", "import-run", "apply", "status", "doctor"])
    parser.add_argument("query", nargs="?", help="search query, or source run directory for import-run")
    parser.add_argument("--vault")
    parser.add_argument("--schema")
    parser.add_argument("--voice", help="voice-and-style note (default: the vault's, when present)")
    parser.add_argument("--no-voice", action="store_true", help="disable the vault voice policy for this run")
    parser.add_argument("--profile", help="personal-context register note (default: the vault's, when present)")
    parser.add_argument("--no-profile", action="store_true", help="disable personal context for this run")
    parser.add_argument("--run", help="run directory (apply, status)")
    parser.add_argument("--accept", help="comma-separated proposal ids to apply")
    parser.add_argument("--reject", help="comma-separated proposal ids to record as rejected")
    parser.add_argument("--dry-run", action="store_true", help="show what apply would write without writing")
    parser.add_argument(
        "--limit",
        type=int,
        help="cap judged pairs (propose), classified targets (wiki), or harvested entities (import-run)",
    )
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
    parser.add_argument("--wiki-kinds", default=",".join(DEFAULT_WIKI_KINDS), help="comma-separated wiki kinds for import-run")
    parser.add_argument(
        "--include-artifact",
        action="append",
        default=[],
        help="additional run-relative Markdown artifact for import-run; may be repeated",
    )
    parser.add_argument("--title-prefix", help="filename prefix for imported inbox notes")
    parser.add_argument(
        "--notes",
        action="store_true",
        help="import-run: also propose one note per subtopic from a deep-research run's claims",
    )
    parser.add_argument(
        "--notes-limit",
        type=int,
        default=DEFAULT_SUBTOPIC_NOTES,
        help=f"cap subtopic notes per import-run (default {DEFAULT_SUBTOPIC_NOTES})",
    )
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    parser.add_argument("--request-timeout", type=float, default=120)
    parser.add_argument("--embeddings-url")
    parser.add_argument("--embeddings-model")
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument("--no-verify", action="store_true", help="skip the thinking-model review of proposals")
    parser.add_argument("--think-url", help="thinking service used for review (default: connectedServices.think)")
    parser.add_argument("--think-model")
    parser.add_argument("--think-prefill", action="store_true", help="prefill an empty think block (for thinking backends like :8008)")
    args = parser.parse_args(argv)

    if args.command == "search" and not args.query:
        raise UserError("search requires a query argument")
    if args.command == "import-run" and not args.query:
        raise UserError("import-run requires a run-directory argument")
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

    # Skill-specific settings win, then the agent's configured chat service, then
    # the built-in non-thinking default.
    resolved = forge_llm.resolve_service(
        "chat",
        base_url=args.base_url or os.environ.get("VAULT_CONNECTIONS_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
        model=args.model or os.environ.get("VAULT_CONNECTIONS_MODEL") or os.environ.get("OPENAI_MODEL"),
    )
    args.base_url = resolved["url"]
    args.model = resolved["model"]
    args.api_key = args.api_key or os.environ.get("VAULT_CONNECTIONS_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.voice = args.voice or os.environ.get("VAULT_CONNECTIONS_VOICE") or None
    if args.no_voice and args.voice and "--voice" in argv:
        raise UserError("--voice and --no-voice cannot be used together")
    args.profile = args.profile or os.environ.get("VAULT_CONNECTIONS_PROFILE") or None
    if args.no_profile and args.profile and "--profile" in argv:
        raise UserError("--profile and --no-profile cannot be used together")
    args.embeddings_url = forge_embeddings.endpoint_url(args.embeddings_url)
    args.embeddings_model = forge_embeddings.model_name(args.embeddings_model)
    args.cache_prompt = not args.no_cache_prompt
    args.verify = not args.no_verify
    return args


COMMANDS = {
    "index": command_index,
    "search": command_search,
    "propose": command_propose,
    "wiki": command_wiki,
    "import-run": command_import_run,
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
