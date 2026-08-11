#!/usr/bin/env python3

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings
import forge_llm
import forge_verify
import obsidian_cli
import run_state
import vault_dates
import vault_format
import vault_lexicon
import vault_profile
from vault_moves import PlainMover, backup_once, resolve_mover
import vault_voice
from vault_classification import (  # noqa: F401  (re-exported for callers and tests)
    DEFAULT_BASE_URL,
    MAX_SUGGESTION_CHARS,
    MAX_SUGGESTIONS,
    THINK_BLOCK_RE,
    THINK_PREFILL,
    build_messages,
    chat_service,
    compact_schema_for_prompt,
    extract_json_content,
    normalize_base_url,
    normalize_metadata,
    request_json_with_retry,
    system_prompt,
    validate_classification,
)
from vault_schema import (  # noqa: F401  (re-exported for callers and tests)
    CREATED_EVIDENCE,
    UNSTAMPED_TYPES,
    COMPILED_SCHEMA_VERSION,
    DEFAULT_SCHEMA,
    INBOX_DIR,
    PROTECTED_DIRS,
    REQUIRED_SECTIONS,
    RESERVED_WINDOWS_NAMES,
    SCHEMA_BASENAME,
    DRIFT_SEVERITY_ORDER,
    UserError,
    check_schema_drift,
    compile_destination,
    compiled_routes,
    compiled_schema_for,
    derive_created,
    derived_properties,
    domain_folder,
    drift_counts,
    existing_folders,
    find_path_references,
    prune_renumber_moves,
    drift_finding_id,
    first_nonempty_line,
    has_control_character,
    heading_index,
    human_owned_properties,
    missing_required_properties,
    WORKSPACE_MARKER,
    is_divider_row,
    is_workspace_dir,
    iter_heading_lines,
    normalize_body_for_hash,
    normalize_project_value,
    note_title,
    optional_bullet_lines,
    pad2,
    parse_assignment,
    parse_bullet_registry,
    parse_frontmatter,
    parse_legacy_output,
    parse_schema_note,
    path_is_inside,
    project_folder,
    project_name,
    property_derived,
    property_human_owned,
    property_shape,
    property_value_mode,
    relative_path,
    renumber_folder_moves,
    renumber_mapping,
    replace_schema_row_number,
    require_number,
    require_safe_label,
    resolve_schema_path,
    revised_note_text,
    sources_routing_enabled,
    safe_basename,
    safe_title,
    section_bounds,
    selected_notes,
    serialize_frontmatter,
    sha256_bytes,
    sha256_file,
    sha256_text,
    split_frontmatter,
    split_markdown_table_row,
    strip_inline_code,
    strip_schema_value,
    subdomain_folder,
    table_after,
    unsafe_filename_reason,
    valid_wikilink,
    validate_derived_paths,
    withheld_properties,
    yaml_quote,
    yaml_scalar,
)
import vault_guide


WORKFLOW = "vault-organizer"
DEFAULT_MODEL = "chat"
PROMPT_VERSION = "vault-organizer-v3"
MAX_BODY_CHARS = 30000
EMBED_MAX_CHARS = 2000
MIN_NEAR_DUPE_CHARS = 100
NEAR_DUPE_AUTO = 0.97
NEAR_DUPE_REVIEW = 0.90
CONTAINMENT_MIN = 0.90
MAX_BLOCK_BUCKET = 50
RUN_STATE_BATCH = 25
QUARANTINE_SUBDIR = "duplicates"
TEMP_NAME_RE = re.compile(
    r"^(untitled|document|extracted|extraction_report|chunk[-_ ]?\d+|new note|note)"
    r"(\s+\d+|\s*\(\d+\)|\s+copy)?$",
    re.IGNORECASE,
)
STEM_SUFFIX_RE = re.compile(r"(?:\s+\d+|\s*\(\d+\)|\s+copy)$", re.IGNORECASE)


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


def log(args, message):
    if args.verbose:
        print(message, file=sys.stderr)


def progress(message):
    print(message, file=sys.stderr, flush=True)


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def unique_run_directory(vault):
    runs = vault / ".vault-organizer" / "runs"
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


def excerpt_body(body):
    if len(body) <= MAX_BODY_CHARS:
        return body, False
    headings = "\n".join(line for line in body.splitlines() if line.startswith("#"))[:6000]
    head = body[:12000]
    tail = body[-12000:]
    excerpt = f"{head}\n\n<!-- headings -->\n{headings}\n\n<!-- tail -->\n{tail}"
    return excerpt[:MAX_BODY_CHARS], True


def blocking_stem(path_text):
    stem = urllib.parse.unquote(Path(path_text).stem).casefold().strip()
    return STEM_SUFFIX_RE.sub("", stem)


def is_temp_basename(path_text):
    stem = urllib.parse.unquote(Path(path_text).stem).strip()
    return TEMP_NAME_RE.fullmatch(stem) is not None


def embedding_text(title, normalized_body):
    return f"{title}\n{normalized_body[:EMBED_MAX_CHARS]}"


def line_containment(short_normalized, long_normalized):
    short_lines = [line for line in short_normalized.split("\n") if line.strip()]
    if not short_lines:
        return 0.0
    available = {}
    for line in long_normalized.split("\n"):
        if line.strip():
            available[line] = available.get(line, 0) + 1
    matched = 0
    for line in short_lines:
        if available.get(line, 0) > 0:
            available[line] -= 1
            matched += 1
    return matched / len(short_lines)


def load_cache(vault):
    path = vault / ".vault-organizer" / "cache" / "classifications.json"
    if not path.exists():
        return {}, path
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return (value if isinstance(value, dict) else {}), path
    except (OSError, json.JSONDecodeError):
        return {}, path


def save_cache(path, cache):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def cache_key(title, body_hash, frontmatter_hash, schema_hash, model, base_url, think_prefill=True,
              profile_hash="none"):
    payload = {
        "title": title,
        "body_hash": body_hash,
        "frontmatter_hash": frontmatter_hash,
        "schema_hash": schema_hash,
        # Shapes the system prompt, so it has to shape the key. Without it,
        # editing a context card leaves every note classified as it was.
        "profile_hash": profile_hash,
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "endpoint": base_url,
        "think_prefill": think_prefill,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def embedding_cache_path(vault):
    return vault / ".vault-organizer" / "cache" / "embeddings.jsonl"


def load_embedding_cache(vault, model):
    rows, _ = run_state.read_jsonl_recover_tail(embedding_cache_path(vault), repair=True)
    vectors = {}
    for row in rows:
        if row.get("model") == model and isinstance(row.get("vector"), list) and row.get("body_hash"):
            vectors[row["body_hash"]] = forge_embeddings.normalize(row["vector"])
    return vectors


def append_embedding_cache(vault, model, body_hash, vector):
    rounded = [round(float(value), 6) for value in vector]
    run_state.append_jsonl_fsync(embedding_cache_path(vault), {"body_hash": body_hash, "model": model, "vector": rounded})


def vault_index_path(vault):
    return vault / ".vault-organizer" / "cache" / "vault-index.json"


def index_entry_from_file(vault, path):
    data = path.read_bytes()
    frontmatter = split_frontmatter(data)
    normalized = normalize_body_for_hash(frontmatter["body"])
    stat = path.stat()
    return {
        "body_hash": sha256_text(normalized),
        "size": stat.st_size,
        "mtime": stat.st_mtime,
        "title": note_title(path, frontmatter["body"]),
        "first_line_hash": sha256_text(first_nonempty_line(normalized)) if normalized else "",
        "body_chars": len(normalized),
    }


def refresh_vault_index(vault, schema_path):
    """Rebuild the filed-note content index (everything outside the inbox), reusing
    unchanged entries by size+mtime so a refresh only re-reads modified files."""
    path = vault_index_path(vault)
    old_entries = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("entries"), dict):
                old_entries = loaded["entries"]
        except (OSError, json.JSONDecodeError):
            old_entries = {}
    entries = {}
    warnings = []
    for note in selected_notes(vault, schema_path, "vault", None):
        rel = relative_path(vault, note)
        if rel.split("/", 1)[0] == INBOX_DIR:
            continue
        stat = note.stat()
        previous = old_entries.get(rel)
        if previous and previous.get("size") == stat.st_size and previous.get("mtime") == stat.st_mtime:
            entries[rel] = previous
            continue
        try:
            entries[rel] = index_entry_from_file(vault, note)
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"vault index skipped {rel}: {error}")
    try:
        schema_rel = relative_path(vault, schema_path)
        entries[schema_rel] = index_entry_from_file(vault, schema_path)
    except (OSError, UnicodeDecodeError, ValueError):
        pass
    run_state.atomic_write_json(path, {"version": 1, "entries": entries})
    return entries, warnings


SYSTEM_INSTRUCTIONS = (
    "You classify Obsidian Markdown notes. Return exactly one JSON object. "
    "Do not return YAML, paths, folder numbers, explanations, markdown, or filesystem instructions. "
    "Choose values only from the approved schema below. Classify by the note's primary purpose. "
    "The note's previous frontmatter is provided as untrusted advisory context only; never copy "
    "unapproved keys or values from it. "
    "Use needs_review true when required classification is genuinely ambiguous. "
    "You may include an optional \"suggestions\" array of short strings, each proposing one schema "
    "addition (a new subdomain, project, or value) only when the schema clearly lacks a needed value; "
    "suggestions are reviewed by a human later and are never applied to this note."
)


def clean_suggestions(raw, warnings):
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("suggestions ignored: not a list")
        return []
    cleaned = []
    for item in raw[:MAX_SUGGESTIONS]:
        if not isinstance(item, str):
            continue
        text = "".join(character for character in item if ord(character) >= 32 or character == "\t").strip()
        if text:
            cleaned.append(text[:MAX_SUGGESTION_CHARS])
    return cleaned


def scan_vault(vault, schema_path, mode, limit, only_sources=False):
    schema_data = schema_path.read_bytes()
    schema_split = split_frontmatter(schema_data)
    schema_body_hash = sha256_text(normalize_body_for_hash(schema_split["body"]))
    items = []
    for path in selected_notes(vault, schema_path, mode, limit):
        rel = relative_path(vault, path)
        stat = path.stat()
        item = {
            "path": rel,
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "sha256": None,
            "body_hash": None,
            "body_chars": 0,
            "empty": False,
            "malformed": False,
            "schema_copy": False,
            "title": path.stem,
            "first_line_hash": "",
            "error": None,
        }
        note_type = None
        try:
            data = path.read_bytes()
            item["sha256"] = sha256_bytes(data)
            frontmatter = split_frontmatter(data)
            note_type = parse_frontmatter(frontmatter["frontmatter_text"]).get("type")
            normalized = normalize_body_for_hash(frontmatter["body"])
            item["body_hash"] = sha256_text(normalized)
            item["body_chars"] = len(normalized)
            item["empty"] = not normalized
            item["malformed"] = frontmatter["malformed"]
            item["schema_copy"] = item["body_hash"] == schema_body_hash and not item["empty"]
            item["title"] = note_title(path, frontmatter["body"])
            item["first_line_hash"] = sha256_text(first_nonempty_line(normalized)) if normalized else ""
        except (OSError, UnicodeDecodeError) as error:
            item["error"] = str(error)
            item["sha256"] = item["sha256"] or sha256_file(path)
        # A migration that only moves sources leaves everything else exactly
        # where it is, including the folder trees somebody arranged by hand
        # below a declared route. Those are legitimate structure the schema
        # does not describe, and a whole-vault run would dissolve them.
        if only_sources and note_type != "source":
            continue
        items.append(item)
    return items, schema_body_hash


def canonical_rank(item):
    return (
        1 if item["path"].split("/", 1)[0] == INBOX_DIR else 0,
        -item["size"],
        1 if is_temp_basename(item["path"]) else 0,
        item["mtime"],
        item["path"],
    )


def near_dupe_rank(item):
    return (0 if item.get("indexed") else 1, -item["body_chars"]) + canonical_rank(item)


def quarantine_root(vault):
    return vault / ".vault-organizer" / QUARANTINE_SUBDIR


def assign_quarantine_path(vault, rel, taken_casefold):
    base = Path(".vault-organizer") / QUARANTINE_SUBDIR / rel
    candidate = base
    suffix = 1
    while (vault / candidate).exists() or candidate.as_posix().casefold() in taken_casefold:
        candidate = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
        suffix += 1
    taken_casefold.add(candidate.as_posix().casefold())
    return candidate.as_posix()


def blocking_buckets(nodes, warnings):
    buckets = {}
    for index, node in enumerate(nodes):
        keys = {("stem", blocking_stem(node["path"]))}
        if node.get("title"):
            keys.add(("title", node["title"].casefold().strip()))
        if node.get("first_line_hash"):
            keys.add(("first", node["first_line_hash"]))
        for key in keys:
            buckets.setdefault(key, []).append(index)
    pairs = set()
    for key, members in buckets.items():
        if len(members) < 2:
            continue
        if len(members) > MAX_BLOCK_BUCKET:
            warnings.append(f"near-dupe blocking skipped an oversized bucket ({key[0]}, {len(members)} notes)")
            continue
        for position, left in enumerate(members):
            for right in members[position + 1:]:
                pairs.add((min(left, right), max(left, right)))
    return sorted(pairs)


def read_normalized_body(vault, node):
    path = vault / node["path"]
    frontmatter = split_frontmatter(path.read_bytes())
    return normalize_body_for_hash(frontmatter["body"])


def plan_dedupe(args, vault, items, index_entries, warnings, schema_label="<schema note>"):
    """Plan exact and near-duplicate resolution. Returns the dedupe manifest;
    no filesystem changes happen here."""
    result = {
        "groups": [],
        "review_pairs": [],
        "embeddings": {"attempted": False, "ok": None, "model": args.embeddings_model, "reason": None},
        "quarantine_root": (Path(".vault-organizer") / QUARANTINE_SUBDIR).as_posix(),
    }
    taken_quarantine = set()
    losers = {}

    def add_group(kind, winner_path, group_losers, score=None):
        entry = {"kind": kind, "winner": winner_path, "losers": []}
        if score is not None:
            entry["score"] = round(score, 4)
        for loser in group_losers:
            quarantine_to = assign_quarantine_path(vault, loser["path"], taken_quarantine)
            entry["losers"].append({"path": loser["path"], "sha256": loser["sha256"], "quarantine_to": quarantine_to})
            losers[loser["path"]] = {"winner": winner_path, "kind": kind, "quarantine_to": quarantine_to, "sha256": loser["sha256"]}
        result["groups"].append(entry)

    eligible = [item for item in items if not item["empty"] and not item.get("error") and item["body_hash"]]

    schema_copies = [item for item in eligible if item.get("schema_copy")]
    if schema_copies:
        add_group("exact", schema_label, schema_copies)
    eligible = [item for item in eligible if not item.get("schema_copy")]

    index_by_hash = {}
    for rel, entry in (index_entries or {}).items():
        index_by_hash.setdefault(entry["body_hash"], rel)
    if index_by_hash:
        matched = {}
        for item in eligible:
            winner = index_by_hash.get(item["body_hash"])
            if winner:
                matched.setdefault(winner, []).append(item)
        for winner, group_losers in sorted(matched.items()):
            add_group("exact", winner, group_losers)
        eligible = [item for item in eligible if item["path"] not in losers]

    by_hash = {}
    for item in eligible:
        by_hash.setdefault(item["body_hash"], []).append(item)
    for body_hash, group in sorted(by_hash.items()):
        if len(group) < 2:
            continue
        winner = min(group, key=canonical_rank)
        add_group("exact", winner["path"], [item for item in group if item is not winner])
    eligible = [item for item in eligible if item["path"] not in losers]

    if args.no_embeddings:
        return result, losers

    nodes = [dict(item) for item in eligible if item["body_chars"] >= MIN_NEAR_DUPE_CHARS]
    if index_entries:
        for rel, entry in sorted(index_entries.items()):
            if entry["body_chars"] >= MIN_NEAR_DUPE_CHARS:
                nodes.append({"path": rel, "indexed": True, "sha256": None, **entry})
    pairs = blocking_buckets(nodes, warnings)
    pairs = [
        (left, right)
        for left, right in pairs
        if nodes[left]["body_hash"] != nodes[right]["body_hash"]
        and not (nodes[left].get("indexed") and nodes[right].get("indexed"))
    ]
    if not pairs:
        return result, losers

    result["embeddings"]["attempted"] = True
    needed_hashes = sorted({nodes[index]["body_hash"] for pair in pairs for index in pair})
    vectors = load_embedding_cache(vault, args.embeddings_model)
    missing = [body_hash for body_hash in needed_hashes if body_hash not in vectors]
    if missing:
        node_by_hash = {}
        for node in nodes:
            node_by_hash.setdefault(node["body_hash"], node)
        texts = []
        text_hashes = []
        for body_hash in missing:
            node = node_by_hash[body_hash]
            try:
                normalized = read_normalized_body(vault, node)
            except (OSError, UnicodeDecodeError) as error:
                warnings.append(f"near-dupe skipped {node['path']}: {error}")
                continue
            texts.append(embedding_text(node["title"], normalized))
            text_hashes.append(body_hash)
        if texts:
            response = forge_embeddings.embed_texts(texts, url=args.embeddings_url, model=args.embeddings_model)
            if not response["ok"]:
                result["embeddings"]["ok"] = False
                result["embeddings"]["reason"] = response["reason"]
                warnings.append(f"embeddings unavailable, near-dupe detection skipped: {response['reason']}")
                return result, losers
            for body_hash, vector in zip(text_hashes, response["vectors"]):
                append_embedding_cache(vault, args.embeddings_model, body_hash, vector)
                vectors[body_hash] = forge_embeddings.normalize(vector)
    result["embeddings"]["ok"] = True

    auto_pairs = []
    body_cache = {}

    def normalized_body_of(node):
        if node["path"] not in body_cache:
            body_cache[node["path"]] = read_normalized_body(vault, node)
        return body_cache[node["path"]]

    for left, right in pairs:
        vector_left = vectors.get(nodes[left]["body_hash"])
        vector_right = vectors.get(nodes[right]["body_hash"])
        if vector_left is None or vector_right is None:
            continue
        score = forge_embeddings.cosine(vector_left, vector_right)
        if score < args.near_dupe_review:
            continue
        try:
            body_left = normalized_body_of(nodes[left])
            body_right = normalized_body_of(nodes[right])
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"near-dupe pair skipped ({nodes[left]['path']}, {nodes[right]['path']}): {error}")
            continue
        shorter, longer = (body_left, body_right) if len(body_left) <= len(body_right) else (body_right, body_left)
        containment = line_containment(shorter, longer)
        pair_record = {
            "a": nodes[left]["path"],
            "b": nodes[right]["path"],
            "score": round(score, 4),
            "containment": round(containment, 4),
        }
        if score >= args.near_dupe_auto and containment >= CONTAINMENT_MIN:
            auto_pairs.append((left, right, pair_record))
        else:
            pair_record["reason"] = "borderline similarity" if score < args.near_dupe_auto else "low containment"
            result["review_pairs"].append(pair_record)

    if auto_pairs:
        adjacency = {}
        for left, right, _ in auto_pairs:
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
        visited = set()
        for start in sorted(adjacency):
            if start in visited:
                continue
            component = []
            stack = [start]
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                stack.extend(adjacency.get(node, ()))
            members = [nodes[index] for index in sorted(component)]
            winner = min(members, key=near_dupe_rank)
            group_losers = []
            for member in members:
                if member is winner or member.get("indexed"):
                    continue
                if winner.get("indexed") and member["body_chars"] > winner["body_chars"]:
                    result["review_pairs"].append({
                        "a": member["path"],
                        "b": winner["path"],
                        "score": None,
                        "containment": None,
                        "reason": "inbox copy is richer than the filed copy",
                    })
                    continue
                group_losers.append(member)
            if group_losers:
                mean_score = sum(record["score"] for l, r, record in auto_pairs if l in component and r in component)
                pair_count = sum(1 for l, r, _ in auto_pairs if l in component and r in component)
                add_group("near", winner["path"], group_losers, score=mean_score / max(pair_count, 1))
    return result, losers


def stamp_created(metadata, previous, schema, path, today=None):
    """Give a note a creation date when nothing carried one forward.

    ``created`` is the one derived property with a derivation rule. A schema that
    marks some other property derived gets it withheld from the classifier and
    carried forward like this one, but nothing invents a value for it: there
    would be no evidence to invent it from.

    Returns the evidence tier that answered, or None when the schema does not
    define ``created`` at all -- vaults that have not adopted it keep working.
    """
    if "created" not in derived_properties(schema):
        return None
    if metadata.get("type") in UNSTAMPED_TYPES:
        return None
    if metadata.get("created"):
        return "carried"
    value, evidence = derive_created(path, metadata, previous, today or datetime.date.today().isoformat())
    metadata["created"] = value
    return evidence


def carry_forward_provenance(validated, frontmatter_text, schema, warnings, path=None):
    """Restore properties the classifier does not own.

    Filing replaces a note's frontmatter wholesale from a model response, so a
    ``capture_type: generated`` or ``processed_by`` mark written by another
    skill would quietly disappear the first time the note was organized. How a
    note was made is not a classification judgment, and a note does not stop
    being machine-made because a later pass read it as prose. Both keys are
    taken from the note's previous frontmatter, and the classifier's own value
    for ``processed_by`` is discarded: scripts are its only writers.

    Properties the schema marks human-owned or derived are restored the same way
    and for the same reason. The classifier is never shown them, so filing would
    drop every one on the first pass; the note's previous frontmatter is their
    only source. Values are re-checked here because they bypass
    ``validate_classification`` entirely, and an unchecked control character
    would fail the whole run inside ``yaml_scalar``.

    Restoring a derived property before stamping it is what makes ``created``
    write-once: the value a note already carries always beats a fresh derivation,
    so filing the same note twice cannot change its birthday. ``path`` unlocks the
    filename and filesystem evidence tiers; without it the stamp falls back to the
    note's own subject date and then to today.
    """
    metadata = validated.get("metadata")
    if not isinstance(metadata, dict):
        return validated
    metadata.pop("processed_by", None)
    previous = parse_frontmatter(frontmatter_text or "")

    for key in withheld_properties(schema):
        metadata.pop(key, None)
        value = previous.get(key)
        if value is None:
            continue
        prop = schema["properties"][key]
        wikilink_required = prop["value_mode"] in {"wikilink", "registered_wikilink"}
        if prop["shape"] == "list":
            items = value if isinstance(value, list) else [value]
            clean = [
                item
                for item in items
                if isinstance(item, str)
                and item
                and not has_control_character(item)
                and (not wikilink_required or valid_wikilink(item))
            ]
            if clean:
                metadata[key] = clean
            elif items:
                warnings.append(f"previous {key} dropped: no valid values")
        elif isinstance(value, list):
            warnings.append(f"previous {key} dropped: schema defines it as a scalar")
        elif not isinstance(value, str) or not value or has_control_character(value):
            warnings.append(f"previous {key} dropped: not a usable scalar")
        elif wikilink_required and not valid_wikilink(value):
            warnings.append(f"previous {key} dropped: not a wikilink")
        else:
            metadata[key] = value

    if previous.get("capture_type") == "generated" and metadata.get("capture_type") != "generated":
        if "generated" in schema["capture_types"]:
            replaced = metadata.get("capture_type")
            metadata["capture_type"] = "generated"
            if replaced:
                warnings.append(f"kept capture_type: generated (classified as {replaced})")
        else:
            warnings.append("previous capture_type: generated dropped: schema no longer defines it")

    carry_forward_source_identity(metadata, previous, schema, warnings)

    processed_by = previous.get("processed_by")
    if isinstance(processed_by, str):
        processed_by = [processed_by]
    if processed_by and schema["properties"].get("processed_by", {}).get("shape") == "list":
        clean = [item for item in processed_by if isinstance(item, str) and item and not has_control_character(item)]
        if clean:
            metadata["processed_by"] = clean
    elif processed_by:
        warnings.append("previous processed_by dropped: schema does not define it as a list property")

    # Last, so it sees the date this pass restored rather than the one it replaced.
    evidence = stamp_created(metadata, previous, schema, path)
    if evidence:
        validated["created_evidence"] = evidence
    return validated


def carry_forward_source_identity(metadata, previous, schema, warnings):
    """Keep a script-declared source note a source note.

    Once ``source_kind`` selects the folder, re-reading it out of the note's text
    is a guess where the note already carries the answer. A transcript's raw half
    and an imported artifact are both written by a script that knew exactly what
    it was making, and a classifier reading a wall of timestamped speech has been
    seen to call it a meeting. Losing that judgment does not merely mislabel the
    note, it moves it to a different tree, and the next run moves it again.

    Only a pairing the schema accepts is kept, so a stale kind cannot pin a note
    to a folder the schema no longer compiles. ``parent`` comes along because
    filing replaces frontmatter wholesale and the back-link to the processed note
    is the only thing tying the two halves together.
    """
    if not sources_routing_enabled(schema):
        return
    kind = previous.get("source_kind")
    if previous.get("type") != "source" or not isinstance(kind, str) or kind not in schema["source_kinds"]:
        return
    if "source" not in schema["types"]:
        warnings.append("previous type: source dropped: schema no longer defines it")
        return
    if metadata.get("type") != "source" or metadata.get("source_kind") != kind:
        classified = metadata.get("type")
        detail = f" (classified as {classified})" if classified and classified != "source" else ""
        warnings.append(f"kept type: source with source_kind: {kind}{detail}")
    metadata["type"] = "source"
    metadata["source_kind"] = kind
    parent = previous.get("parent")
    if "parent" in schema["properties"] and valid_wikilink(parent) and not metadata.get("parent"):
        metadata["parent"] = parent


def load_profile(args, vault):
    """Compile the personal-context layer for classification, or carry on without.

    A broken or ambiguous register costs the layer, not the run. A ``--profile``
    path that does not exist still raises, so a typo cannot silently disable the
    layer the command asked for.
    """
    profile_path, warnings = vault_profile.resolve_profile_or_warn(
        vault, getattr(args, "profile", None), disabled=getattr(args, "no_profile", False)
    )
    profile, profile_hash, compile_warnings = vault_profile.compiled_profile_for(
        vault, profile_path, cache_dir=vault / ".vault-organizer" / "cache"
    )
    args.compiled_profile = profile
    args.profile_path = profile_path
    args.profile_hash = profile_hash
    args.profile_warnings = warnings + compile_warnings
    return profile_path, profile, profile_hash


def classification_site():
    """Classification decides where a note is filed, so it cannot yet know.

    Empty routes mean every route-gated card is refused here -- deliberate, and
    the reason no special-casing is needed to keep clinical history out of a
    classifier that has not decided what it is looking at. What survives is the
    unrestricted always-tier, which is exactly the how-I-file-things material
    that makes routing better.
    """
    return vault_profile.profile_site(vault_voice.CONTEXT_OWNER, stage="classify")


def classification_profile_prefix(args):
    return vault_profile.profile_prefix(getattr(args, "compiled_profile", None), classification_site())


def reuse_frontmatter_classification(schema, frontmatter_text):
    """The note's own frontmatter as a classification, or None if it is not complete.

    A schema change that only moves folders does not change what any note is, so
    re-deriving a thousand already-correct classifications from a model is both
    slow and a way to lose them: the model is not deterministic, and every note it
    hedges on lands in the review queue and gets pulled back to the inbox. Frontmatter
    that already validates is a better answer than a fresh guess, and it compiles
    to a destination the same way.

    Returns None rather than a partial result when anything fails to validate, so
    those notes fall through to the model exactly as they would have.
    """
    if not frontmatter_text:
        return None
    previous = parse_frontmatter(frontmatter_text)
    withheld = set(withheld_properties(schema)) | {"processed_by"}
    candidate = {
        key: previous[key]
        for key in schema["property_order"]
        if key in previous and key not in withheld and previous[key] not in (None, "", [])
    }
    if not candidate:
        return None
    validated, warnings, errors = validate_classification(
        {"metadata": candidate, "needs_review": False, "review_reason": None, "suggestions": []}, schema
    )
    if errors or validated.get("needs_review"):
        return None
    return validated, warnings


def classify_note(args, schema, title, relative_source, frontmatter_text, body, schema_hash, cache,
                  cache_path, path=None):
    body_hash = sha256_text(normalize_body_for_hash(body))
    frontmatter_hash = sha256_text(frontmatter_text or "")
    profile_prefix = classification_profile_prefix(args)
    key = cache_key(
        title, body_hash, frontmatter_hash, schema_hash, args.model, args.base_url, args.think_prefill,
        getattr(args, "profile_hash", "none"),
    )
    if getattr(args, "reuse_frontmatter", False) and not args.force_reclassify:
        reused = reuse_frontmatter_classification(schema, frontmatter_text)
        if reused is not None:
            validated, warnings = reused
            carry_forward_provenance(validated, frontmatter_text, schema, warnings, path)
            return validated, warnings, "frontmatter", key
    if not args.force_reclassify and key in cache:
        cached = cache[key]
        validated, warnings, errors = validate_classification(cached["response"], schema)
        if not errors:
            carry_forward_provenance(validated, frontmatter_text, schema, warnings, path)
            return validated, warnings, "cache", key
    excerpt, excerpted = excerpt_body(body)
    response = request_json_with_retry(
        args,
        build_messages(
            schema, title, relative_source, frontmatter_text, excerpt, think_prefill=args.think_prefill,
            profile_prefix=profile_prefix,
        ),
    )
    validated, warnings, errors = validate_classification(response, schema)
    if errors:
        repair = {"original_response": response, "validation_errors": errors}
        repaired = request_json_with_retry(
            args,
            build_messages(
                schema, title, relative_source, frontmatter_text, excerpt, repair=repair,
                think_prefill=args.think_prefill, profile_prefix=profile_prefix,
            ),
        )
        validated, warnings, errors = validate_classification(repaired, schema)
        response = repaired
    if errors:
        return {
            "metadata": {},
            "needs_review": True,
            "review_reason": "; ".join(errors),
            "suggestions": [],
            "excerpted": excerpted,
        }, warnings, "model", key
    cache[key] = {"response": response, "stored_at": time.time()}
    save_cache(cache_path, cache)
    carry_forward_provenance(validated, frontmatter_text, schema, warnings, path)
    validated["excerpted"] = excerpted
    return validated, warnings, "model", key


def base_record(item):
    return {
        "source": item["path"],
        "destination": item["path"],
        "source_hash": item["sha256"],
        "body_hash": item["body_hash"],
        "classification_source": "none",
        "metadata": {},
        "frontmatter_changed": False,
        "move_required": False,
        "excerpted": False,
        "created_evidence": None,
        "needs_review": False,
        "review_reason": None,
        "suggestions": [],
        "warnings": [],
        "status": "ok",
        "action": "none",
        "seconds": 0.0,
    }


def synthetic_review_record(item, reason, warning=None, status="review"):
    record = base_record(item)
    record["needs_review"] = True
    record["review_reason"] = reason
    record["status"] = status
    if warning:
        record["warnings"] = [warning]
    return record


def classify_items(args, vault, schema, schema_hash, items, losers, run_dir):
    journal_path = run_dir / "classified.jsonl"
    prior, journal_warnings = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    journal = {(row.get("source"), row.get("body_hash")): row for row in prior}
    cache, cache_path = load_cache(vault)
    records = {}
    warnings = list(journal_warnings)
    work = [item for item in items if item["path"] not in losers]
    total = len(work)
    model_durations = []
    since_state_update = 0
    for position, item in enumerate(work, start=1):
        rel = item["path"]
        journal_key = (rel, item["body_hash"])
        if journal_key in journal:
            records[rel] = journal[journal_key]
            continue
        started = time.time()
        if item.get("error"):
            record = synthetic_review_record(item, f"unreadable note: {item['error']}", warning=item["error"], status="failed")
        elif item["empty"]:
            record = synthetic_review_record(item, "empty body")
        elif item["malformed"]:
            record = synthetic_review_record(
                item,
                "opening frontmatter delimiter has no closing delimiter",
                warning="malformed_frontmatter",
            )
        else:
            try:
                path = vault / rel
                data = path.read_bytes()
                source_hash = sha256_bytes(data)
                frontmatter = split_frontmatter(data)
                body = frontmatter["body"]
                title = note_title(path, body)
                classification, record_warnings, classification_source, _ = classify_note(
                    args, schema, title, rel, frontmatter["frontmatter_text"], body, schema_hash, cache, cache_path,
                    path,
                )
                record = base_record(item)
                record["source_hash"] = source_hash
                record["classification_source"] = classification_source
                record["metadata"] = classification["metadata"]
                record["needs_review"] = classification.get("needs_review", False)
                record["review_reason"] = classification.get("review_reason")
                record["suggestions"] = classification.get("suggestions", [])
                record["excerpted"] = bool(classification.get("excerpted"))
                record["created_evidence"] = classification.get("created_evidence")
                record["warnings"] = record_warnings
                missing = missing_required_properties(record["metadata"], schema)
                if missing and not record["needs_review"]:
                    record["needs_review"] = True
                    record["review_reason"] = f"missing required properties: {', '.join(missing)}"
                if record["needs_review"]:
                    record["status"] = "review"
                else:
                    destination_dir = compile_destination(schema, record["metadata"])
                    filed_name = filing_name(record, path.name, record_warnings)
                    record["destination"] = (destination_dir / filed_name).as_posix()
                    revised = revised_note_text(record["metadata"], schema, body)
                    record["frontmatter_changed"] = revised != data.decode("utf-8-sig")
                    record["move_required"] = record["destination"] != rel
            except Exception as error:
                message = str(error)
                warnings.append(f"{rel}: {message}")
                record = synthetic_review_record(item, message, warning=message, status="failed")
        record["seconds"] = round(time.time() - started, 3)
        records[rel] = record
        run_state.append_jsonl_fsync(journal_path, record)
        journal[journal_key] = record
        if record["classification_source"] == "model":
            model_durations.append(record["seconds"])
        remaining = total - position
        if model_durations:
            eta = format_duration(sum(model_durations) / len(model_durations) * remaining)
        else:
            eta = "-"
        progress(f"[{position}/{total}] {rel} ({record['classification_source']}, {record['seconds']:.1f}s, eta {eta})")
        since_state_update += 1
        if since_state_update >= RUN_STATE_BATCH:
            since_state_update = 0
            update_item_statuses(run_dir, records)
    update_item_statuses(run_dir, records)
    return records, warnings


VERIFY_SYSTEM = (
    "You are reviewing where notes were filed in one person's Obsidian vault.\n"
    "A faster model without reasoning proposed a destination and frontmatter for each note.\n"
    "Your job is to catch the ones that are actually wrong: a destination that does not match\n"
    "what the note is about, or metadata that contradicts the note or the schema.\n"
    "Judge only what the evidence shows. A defensible filing is 'ok' even if you would have\n"
    "chosen differently; taste is not an error."
)
VERIFY_EXCERPT_CHARS = 1000


def verifiable_records(records):
    """Records a reviewer can judge: something was actually decided by the model.

    A record reused from the note's own frontmatter is excluded. There is no
    judgment in it to second-guess -- the values were already in the note and
    already validate -- so sending them to the thinking model would spend the
    expensive pass re-deciding notes nobody classified.
    """
    return [
        (rel, record)
        for rel, record in sorted(records.items())
        if record.get("destination")
        and not record.get("needs_review")
        and record.get("status") != "failed"
        and record.get("classification_source") != "frontmatter"
    ]


def verify_payload(vault, rel, record):
    body = ""
    try:
        frontmatter = split_frontmatter((vault / rel).read_bytes())
        body = frontmatter["body"].strip()[:VERIFY_EXCERPT_CHARS]
    except OSError:
        pass
    return {
        "id": rel,
        "title": note_title(vault / rel, body),
        "currentPath": rel,
        "proposedDestination": record["destination"],
        "metadata": record.get("metadata", {}),
        "excerpt": body,
    }


def verify_classifications(args, vault, schema, records, run_dir):
    """Have the thinking model review every classification, and redo the ones it
    flags with reasoning.

    Bulk classification runs without reasoning because it is usually right and
    reasoning about each note costs hundreds of hidden tokens. This is what
    makes "usually" safe: full coverage for a handful of batched calls, with the
    thinking budget spent on the notes that turn out to need it.
    """
    warnings = []
    candidates = verifiable_records(records)
    summary = {"verified": 0, "ok": 0, "flagged": 0, "escalated": 0, "needsReview": 0, "flaggedIds": []}
    if not candidates:
        return summary, warnings

    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    if not think["enabled"]:
        warnings.append("verification skipped: no thinking service is configured")
        summary["skipped"] = "disabled"
        return summary, warnings

    items = [verify_payload(vault, rel, record) for rel, record in candidates]
    by_path = dict(candidates)
    journal = run_dir / "verified.jsonl"
    log(args, f"verifying {len(items)} classifications on {think['url']}")
    try:
        verdicts = forge_verify.verify_packets(
            think,
            VERIFY_SYSTEM,
            items,
            journal_path=journal,
            background=True,
            timeout=args.request_timeout,
            progress=progress,
        )
    except forge_verify.VerificationError as error:
        # An unreachable reviewer must not look like approval.
        warnings.append(f"verification skipped: {error}")
        summary["skipped"] = str(error)
        return summary, warnings

    flagged = [
        (next(item for item in items if item["id"] == rel), entry["reason"])
        for rel, entry in verdicts.items()
        if entry["verdict"] == forge_verify.VERDICT_FLAG and rel in by_path
    ]

    def redo(item, reason):
        rel = item["id"]
        path = vault / rel
        data = path.read_bytes()
        frontmatter = split_frontmatter(data)
        body = frontmatter["body"]
        excerpt, _excerpted = excerpt_body(body)
        messages = build_messages(
            schema,
            note_title(path, body),
            rel,
            frontmatter["frontmatter_text"],
            excerpt,
            repair={"reviewer_objection": reason, "previous_metadata": by_path[rel].get("metadata", {})},
            think_prefill=False,
            profile_prefix=classification_profile_prefix(args),
        )
        response = request_json_with_retry(args, messages, service=think)
        validated, _warnings, errors = validate_classification(response, schema)
        if errors:
            raise UserError("; ".join(errors))
        carry_forward_provenance(
            validated, frontmatter["frontmatter_text"], schema, by_path[rel].setdefault("warnings", []), path
        )
        return validated

    escalations = forge_verify.escalate(flagged, redo, journal_path=journal, progress=progress)
    for rel, outcome in escalations.items():
        if outcome.get("resumed"):
            continue  # recorded when it was first escalated
        record = by_path[rel]
        record["verify_reason"] = next(reason for item, reason in flagged if item["id"] == rel)
        if outcome["ok"]:
            validated = outcome["value"]
            record["metadata"] = validated["metadata"]
            record["classification_source"] = "model-think"
            record["verified"] = "escalated"
            if validated.get("needs_review"):
                record["needs_review"] = True
                record["review_reason"] = validated.get("review_reason")
                record["status"] = "review"
                record["destination"] = None
            else:
                destination_dir = compile_destination(schema, record["metadata"])
                filed_name = filing_name(record, (vault / rel).name, record.setdefault("warnings", []))
                record["destination"] = (destination_dir / filed_name).as_posix()
                record["move_required"] = record["destination"] != rel
        else:
            # Could not be redone, so a human decides rather than shipping a
            # filing the reviewer already objected to.
            record["verified"] = "needs-review"
            record["needs_review"] = True
            record["review_reason"] = f"verification flagged this and re-classification failed: {outcome['detail']}"
            record["status"] = "review"
            record["destination"] = None
            warnings.append(f"{rel}: {record['review_reason']}")
        run_state.append_jsonl_fsync(run_dir / "classified.jsonl", record)
    for rel, entry in verdicts.items():
        if entry["verdict"] == forge_verify.VERDICT_OK and rel in by_path:
            by_path[rel]["verified"] = "ok"
    summary = forge_verify.summarize(verdicts, escalations)
    return summary, warnings


def item_status_for(record):
    if record["status"] == "failed":
        return "failed"
    if record["needs_review"]:
        return "review"
    return "classified"


def update_item_statuses(run_dir, records):
    def mutate(state):
        for item in state.get("items", []):
            record = records.get(item["id"])
            if record:
                item["status"] = item_status_for(record)
        return state

    run_state.update_run_state(run_dir, mutate)


def filing_name(record, name, warnings):
    """The basename to file a note under, repairing an unsafe one.

    Filing is the last moment a note's name is cheap to change: once it is in a
    folder and linked to, renaming it means rewriting every link. A name holding
    `#`, `^`, `[`, `]`, or `|` cannot be linked to and will not sync to mobile,
    so the note would arrive in its folder already unreachable. Otherwise the
    name is preserved exactly, as the schema requires.
    """
    reason = unsafe_filename_reason(name)
    if not reason:
        return name
    repaired = safe_basename(name)
    if not repaired:
        record["needs_review"] = True
        record["review_reason"] = f"filename {reason} and nothing usable remains after repair"
        warnings.append(f"{name}: {reason}; cannot be repaired automatically")
        return name
    record["filename_repaired"] = True
    record["original_name"] = name
    warnings.append(f"{name}: {reason}; filing as {repaired}")
    return repaired


def assign_unique_destination(vault, directory, name, taken_casefold):
    base = Path(directory) / name
    candidate = base
    suffix = 1
    while (vault / candidate).exists() or candidate.as_posix().casefold() in taken_casefold:
        candidate = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
        suffix += 1
    taken_casefold.add(candidate.as_posix().casefold())
    return candidate.as_posix()


def route_records(args, vault, items, losers, class_records, warnings, done_map=None):
    done_map = done_map or {}
    records = []
    for item in items:
        rel = item["path"]
        loser = losers.get(rel)
        if loser:
            record = base_record(item)
            record["source_hash"] = loser["sha256"]
            record["destination"] = loser["quarantine_to"]
            record["action"] = "quarantine"
            record["duplicate_of"] = loser["winner"]
            record["duplicate_kind"] = loser["kind"]
            done = done_map.get(rel)
            if done and done["op"] == "quarantine" and done["destination"] == record["destination"]:
                record["already_applied"] = True
            records.append(record)
            continue
        journaled = class_records.get(rel)
        if journaled is None:
            warnings.append(f"{rel}: missing classification record")
            record = synthetic_review_record(item, "missing classification record", status="failed")
            records.append(record)
            continue
        record = dict(journaled)
        done = done_map.get(rel)
        if done:
            # `rewrite_move` is the second half of a rewrite that also relocates:
            # content is written at the old path first, then the note is moved.
            if done["op"] in {"rewrite", "rewrite_move"} and record["status"] == "ok" and not record["needs_review"] and done["destination"] == record["destination"]:
                record["already_applied"] = True
            elif done["op"] == "move_only" and (record["needs_review"] or record["status"] != "ok"):
                record["destination"] = done["destination"]
                record["already_applied"] = True
        records.append(record)
    validate_plan(vault, records)
    taken = {record["destination"].casefold() for record in records if record["action"] == "quarantine"}
    for record in records:
        if record["action"] == "quarantine":
            continue
        if record.get("already_applied"):
            if record["status"] == "ok" and not record["needs_review"]:
                record["action"] = "rewrite"
            else:
                record["action"] = "move_only"
                record["move_required"] = True
            continue
        if record["status"] == "ok" and not record["needs_review"]:
            record["action"] = "rewrite" if record["frontmatter_changed"] or record["move_required"] else "none"
            continue
        record["action"] = "none"
        if (
            args.mode == "vault"
            and record["status"] != "failed"
            and record["source"].split("/", 1)[0] != INBOX_DIR
        ):
            record["destination"] = assign_unique_destination(vault, INBOX_DIR, Path(record["source"]).name, taken)
            record["action"] = "move_only"
            record["move_required"] = True
    return records


def validate_plan(vault, records):
    seen = {}
    for record in records:
        if record["action"] == "quarantine":
            seen[record["destination"].casefold()] = record["source"]
    for record in records:
        if record["status"] != "ok" or record["needs_review"] or record["action"] == "quarantine":
            continue
        if record.get("already_applied"):
            seen.setdefault(record["destination"].casefold(), record["source"])
            continue
        destination = record["destination"]
        key = destination.casefold()
        if key in seen and destination != record["source"]:
            record["status"] = "review"
            record["warnings"].append(f"duplicate destination also used by {seen[key]}")
            record["needs_review"] = True
            record["review_reason"] = "duplicate destination"
            continue
        seen[key] = record["source"]
        destination_path = vault / destination
        if not path_is_inside(vault, destination_path):
            record["status"] = "failed"
            record["warnings"].append("destination escapes vault")
            record["needs_review"] = True
            record["review_reason"] = "destination escapes vault"
            continue
        if destination_path.exists() and destination != record["source"]:
            record["status"] = "review"
            record["warnings"].append("destination collision")
            record["needs_review"] = True
            record["review_reason"] = "destination collision"


def initial_counts():
    return {
        "selected": 0,
        "classified": 0,
        "cached": 0,
        "reused": 0,
        "unchanged": 0,
        "frontmatter_updates": 0,
        "moves": 0,
        "review_required": 0,
        "failed": 0,
        "applied": 0,
        "skipped": 0,
        "duplicates_exact": 0,
        "duplicates_near": 0,
        "quarantined": 0,
        "duplicate_review": 0,
        "empty": 0,
        "moved_to_inbox": 0,
    }


def recompute_counts(records, dedupe, items):
    counts = initial_counts()
    counts["selected"] = len(items)
    counts["empty"] = sum(1 for item in items if item.get("empty"))
    counts["duplicate_review"] = len(dedupe.get("review_pairs", []))
    for group in dedupe.get("groups", []):
        key = "duplicates_exact" if group["kind"] == "exact" else "duplicates_near"
        counts[key] += len(group["losers"])
    for record in records:
        if record.get("action") == "quarantine":
            counts["quarantined"] += 1
            continue
        if record.get("classification_source") == "cache":
            counts["cached"] += 1
        elif record.get("classification_source") == "frontmatter":
            counts["reused"] += 1
        elif record.get("classification_source") == "model":
            counts["classified"] += 1
        if record.get("status") == "failed":
            counts["failed"] += 1
        if record.get("action") == "move_only":
            counts["moved_to_inbox"] += 1
        if record.get("needs_review"):
            counts["review_required"] += 1
            continue
        if record.get("status") != "ok":
            continue
        if record.get("frontmatter_changed"):
            counts["frontmatter_updates"] += 1
        if record.get("move_required"):
            counts["moves"] += 1
        if not record.get("frontmatter_changed") and not record.get("move_required"):
            counts["unchanged"] += 1
    return counts


def record_for_review(queue_path, record):
    with queue_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_review_queue(run_dir, records):
    review_queue = run_dir / "review-queue.jsonl"
    review_queue.write_text("", encoding="utf-8")
    for record in records:
        if record["needs_review"] or record["status"] == "failed":
            record_for_review(review_queue, record)
    return review_queue


def scan_base_references(vault, records, session=None):
    """Which Bases dashboards this run's moves will disturb.

    Text matching is the fallback, and it is a poor one: a base selects notes by
    property, so it can hold a note whose path appears nowhere in the file, and
    it can mention a path in a filter it never actually matches. When Obsidian is
    running the base is evaluated instead, which answers the real question —
    which notes does this view return, and how many of them are about to move.

    Read-only in both directions. Bases YAML is a moving format and nothing here
    will ever rewrite one.
    """
    moved_sources = [
        record["source"]
        for record in records
        if record["action"] in {"move_only", "quarantine"}
        or (record["action"] == "rewrite" and record.get("move_required"))
    ]
    if not moved_sources:
        return []
    if session is not None and session.get("available"):
        evaluated = evaluate_base_views(vault, session, moved_sources)
        if evaluated is not None:
            return evaluated
    references = []
    for directory, dirnames, filenames in os.walk(vault, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [
            name for name in sorted(dirnames)
            if name not in PROTECTED_DIRS
            and not name.startswith(".")
            and not (dirpath / name).is_symlink()
            and not is_workspace_dir(dirpath / name)
        ]
        for filename in sorted(filenames):
            if not filename.endswith(".base"):
                continue
            path = dirpath / filename
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            hits = [source for source in moved_sources if source in text]
            if hits:
                references.append({"base": relative_path(vault, path), "references": hits})
    return references


def evaluate_base_views(vault, session, moved_sources):
    """Run each base's view and report which of its results this run moves.

    Returns None when the CLI cannot answer, so the caller falls back to text
    matching rather than silently reporting nothing.
    """
    listing = obsidian_cli.run(session, "bases")
    if not listing["ok"]:
        return None
    bases = [line.strip() for line in listing["output"].splitlines() if line.strip().endswith(".base")]
    moving = set(moved_sources)
    references = []
    for base in sorted(bases):
        result = obsidian_cli.run(session, "base:query", path=base, format="paths")
        if not result["ok"]:
            continue
        returned = [line.strip() for line in result["output"].splitlines() if line.strip()]
        hits = [path for path in returned if path in moving]
        if hits:
            references.append({"base": base, "references": hits, "returned": len(returned), "source": "base:query"})
    return references


def collect_suggestions(records):
    suggestions = {}
    for record in records:
        for text in record.get("suggestions", []) or []:
            key = text.casefold()
            entry = suggestions.setdefault(key, {"suggestion": text, "sources": []})
            if len(entry["sources"]) < 3:
                entry["sources"].append(record["source"])
    return [suggestions[key] for key in sorted(suggestions)]


def plan_for_json(records):
    cleaned = []
    for record in records:
        item = dict(record)
        item.pop("revised_text", None)
        cleaned.append(item)
    return cleaned


def append_report_listing(report, entries, formatter, limit=50):
    for entry in entries[:limit]:
        report.append(formatter(entry))
    if len(entries) > limit:
        report.append(f"- … and {len(entries) - limit} more")
    if not entries:
        report.append("- None")


def verification_report(verification, records):
    """The verification section of report.md.

    Says plainly when nothing was verified: an unreachable reviewer must not
    read as approval.
    """
    lines = ["## Verification", ""]
    if verification is None:
        lines.extend(["- Skipped (`--no-verify`). These classifications were not reviewed.", ""])
        return lines
    if verification.get("skipped"):
        lines.extend([f"- **Not verified**: {verification['skipped']}", ""])
        return lines
    lines.extend([
        f"- Reviewed by the thinking model: {verification['verified']}",
        f"- Agreed: {verification['ok']}",
        f"- Flagged: {verification['flagged']}",
        f"- Re-done with reasoning: {verification['escalated']}",
        f"- Left for you to decide: {verification['needsReview']}",
        "",
    ])
    flagged = [record for record in records if record.get("verify_reason")]
    if flagged:
        lines.append("| Note | Objection | Outcome | Destination |")
        lines.append("| --- | --- | --- | --- |")
        for record in flagged:
            outcome = "re-done with reasoning" if record.get("verified") == "escalated" else "needs your review"
            destination = record.get("destination") or "—"
            reason = str(record.get("verify_reason", "")).replace("|", "\\|")
            lines.append(f"| `{record['source']}` | {reason} | {outcome} | `{destination}` |")
        lines.append("")
    return lines


# What each evidence tier actually knows, said once here rather than left for the
# reader to infer from a bare tier name. A backfilled date is only as good as the
# thing it was read off, and the report is where that distinction survives.
CREATED_EVIDENCE_LABELS = {
    "carried": "already carried the date; nothing was derived",
    "filename": "read from a `YYYY-MM-DD` filename prefix",
    "date_property": "taken from the note's own `date` property",
    "filesystem": "read from file timestamps, which a bulk move rewrites — weakest evidence",
    "run_date": "no evidence in the note; stamped with today's date",
}


def created_report_lines(records):
    """Say where every creation date came from, grouped by how well it is known."""
    counts = {}
    for record in records:
        evidence = record.get("created_evidence")
        if evidence:
            counts[evidence] = counts.get(evidence, 0) + 1
    if not counts:
        return []
    lines = ["## Created Dates", ""]
    for evidence in CREATED_EVIDENCE:
        if evidence in counts:
            lines.append(f"- {counts[evidence]} {CREATED_EVIDENCE_LABELS[evidence]}")
    lines.append("")
    return lines


def drift_report_lines(findings):
    """The `## Schema Drift` section: where the schema and the folders disagree."""
    lines = ["## Schema Drift", ""]
    if not findings:
        lines.extend(["- None. Every compiled route matches a folder on disk.", ""])
        return lines
    counts = drift_counts(findings)
    lines.append(
        "- "
        + ", ".join(f"{counts[severity]} {severity}" for severity in ("high", "medium", "low", "info"))
        + ""
    )
    lines.append("")
    for severity in ("high", "medium", "low", "info"):
        group = [finding for finding in findings if finding["severity"] == severity]
        if not group:
            continue
        lines.extend([f"### {severity.capitalize()}", ""])
        for finding in group:
            lines.append(f"- `{finding['id']}` **{finding['kind']}** — `{finding['path']}`")
            lines.append(f"    - {finding['detail']}")
            if finding.get("suggestion"):
                lines.append(f"    - Fix ({finding['fix_side']}): {finding['suggestion']}")
        lines.append("")
    return lines


def write_plan(
    run_dir, records, counts, dedupe, base_references, mode, dry_run, vault, schema_hash, warnings,
    verification=None, schema_drift=None, link_rewrite=("rename", None),
):
    plan_path = run_dir / "plan.json"
    report_path = run_dir / "report.md"
    schema_drift = schema_drift or []
    link_mode, link_reason = link_rewrite
    data = {
        "mode": mode,
        "dry_run": dry_run,
        "vault": str(vault),
        "schema_hash": schema_hash,
        "run_directory": str(run_dir),
        "counts": counts,
        "dedupe": dedupe,
        "verification": verification,
        "base_references": base_references,
        "schema_drift": schema_drift,
        "link_rewrite": {"mode": link_mode, "reason": link_reason},
        "records": plan_for_json(records),
        "warnings": warnings,
    }
    run_state.atomic_write_json(plan_path, data)
    destination_counts = {}
    for record in records:
        if record["status"] == "ok" and not record["needs_review"] and record["action"] != "quarantine":
            first = record["destination"].split("/", 1)[0]
            destination_counts[first] = destination_counts.get(first, 0) + 1
    report = [
        "# Vault Organizer Report",
        "",
        f"- Mode: `{mode}`",
        f"- Dry run: `{str(dry_run).lower()}`",
        f"- Vault: `{vault}`",
        f"- Schema hash: `{schema_hash}`",
        f"- Selected: {counts['selected']}",
        f"- Newly classified: {counts['classified']}",
        f"- Cached classifications: {counts['cached']}",
        f"- Reused from existing frontmatter: {counts['reused']}",
        f"- Frontmatter updates: {counts['frontmatter_updates']}",
        f"- Moves: {counts['moves']}",
        f"- Unchanged: {counts['unchanged']}",
        f"- Review required: {counts['review_required']}",
        f"- Moved to inbox for review: {counts['moved_to_inbox']}",
        f"- Empty notes: {counts['empty']}",
        f"- Exact duplicates quarantined: {counts['duplicates_exact']}",
        f"- Near duplicates quarantined: {counts['duplicates_near']}",
        f"- Duplicate pairs needing review: {counts['duplicate_review']}",
        f"- Failed: {counts['failed']}",
        "",
    ]
    repaired = [record for record in records if record.get("filename_repaired")]
    if repaired:
        report.extend(
            [
                "## Filenames Repaired",
                "",
                "These names could not be linked to with `[[wikilinks]]` and would not"
                " sync to mobile, so they were repaired on the way into the vault.",
                "",
            ]
        )
        for record in sorted(repaired, key=lambda entry: entry["source"]):
            report.append(f"- `{record['original_name']}` → `{Path(record['destination']).name}`")
        report.append("")
    report.extend(created_report_lines(records))
    report.extend(verification_report(verification, records))
    report.extend(["## Destination Counts", ""])
    if destination_counts:
        for key in sorted(destination_counts):
            report.append(f"- {key}: {destination_counts[key]}")
    else:
        report.append("- None")
    report.extend(["", "## Duplicates", ""])
    embeddings_info = dedupe.get("embeddings", {})
    if embeddings_info.get("attempted"):
        state = "ok" if embeddings_info.get("ok") else f"unavailable ({embeddings_info.get('reason')})"
        report.append(f"- Embeddings ({embeddings_info.get('model')}): {state}")
        report.append("- Near-duplicate candidates are blocked on shared basename, title, or first line; renamed near-duplicates are not detected.")
        report.append("")
    append_report_listing(
        report,
        dedupe.get("groups", []),
        lambda group: f"- [{group['kind']}] keep `{group['winner']}` ← quarantine {', '.join('`' + loser['path'] + '`' for loser in group['losers'])}",
    )
    report.extend(["", "## Duplicate Review", ""])
    append_report_listing(
        report,
        dedupe.get("review_pairs", []),
        lambda pair: f"- `{pair['a']}` vs `{pair['b']}` (score {pair.get('score')}, containment {pair.get('containment')}): {pair.get('reason', '')}",
    )
    report.append("")
    report.extend(drift_report_lines(schema_drift))
    suggestions = collect_suggestions(records)
    report.extend(["## Schema Suggestions", "", "Suggestions are advisory only; nothing is applied to the schema.", ""])
    append_report_listing(
        report,
        suggestions,
        lambda entry: f"- {entry['suggestion']} (from {', '.join('`' + source + '`' for source in entry['sources'])})",
    )
    report.extend(["", "## Base File References", ""])
    append_report_listing(
        report,
        base_references,
        lambda entry: (
            f"- `{entry['base']}` returns {entry['returned']} note(s), {len(entry['references'])} of which "
            f"this run moves: {', '.join('`' + hit + '`' for hit in entry['references'][:5])}"
            if entry.get("source") == "base:query"
            else f"- `{entry['base']}` mentions moved notes: "
            f"{', '.join('`' + hit + '`' for hit in entry['references'][:5])} (text match; Obsidian was not "
            f"running, so the view itself was not evaluated)"
        ),
    )
    report.extend(["", "## Link Safety", ""])
    if link_mode == "obsidian-cli":
        report.append(
            "Moves go through the Obsidian CLI, which rewrites every inbound wikilink — in prose and in "
            "frontmatter — to follow the note. Each note that links to a moved note is backed up first, and "
            "any rewrite touching a line without a link is restored from that backup and fails the operation."
        )
    else:
        report.append(
            "Moves use a plain rename"
            + (f" ({link_reason})" if link_reason else "")
            + ". Basename-style Obsidian wikilinks are generally independent of folders, so a folder-only "
            "move is invisible to them; a move that also changes the filename is not, and inbound links to "
            "the old name are left as they were."
        )
    report.extend([
        "",
        "Relative Markdown links containing explicit paths may be affected by moves either way. Run "
        "`attachments` mode afterwards to repair asset links that moves left dangling.",
        "",
        "## Warnings",
        "",
    ])
    if warnings or any(record["warnings"] for record in records):
        for warning in warnings:
            report.append(f"- {warning}")
        for record in records:
            for warning in record["warnings"]:
                report.append(f"- {record['source']}: {warning}")
    else:
        report.append("- None")
    run_state.atomic_write_text(report_path, "\n".join(report) + "\n")
    return plan_path, report_path


def atomic_write_note(path, text):
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def apply_rewrite_content(vault, run_dir, record, schema, expected):
    """Write revised frontmatter at the note's current path. Never moves it.

    Content and location are two steps now, and every content write happens
    before any move. That is what keeps the planning hashes valid: a move can
    rewrite links inside other notes, so once moves start, no other note's
    planning hash can be trusted.
    """
    source = vault / record["source"]
    data = source.read_bytes()
    if sha256_bytes(data) != expected[record["source"]]:
        raise UserError("source changed since planning")
    frontmatter = split_frontmatter(data)
    if frontmatter["malformed"]:
        raise UserError("frontmatter became malformed since planning")
    revised = revised_note_text(record["metadata"], schema, frontmatter["body"])
    backup = backup_once(run_dir, record["source"], source)
    atomic_write_note(source, revised)
    expected[record["source"]] = sha256_bytes(source.read_bytes())
    return backup


def apply_move_operation(vault, run_dir, record, expected, mover):
    source = vault / record["source"]
    destination = vault / record["destination"]
    data = source.read_bytes()
    if sha256_bytes(data) != expected[record["source"]]:
        raise UserError("source changed since planning")
    if destination.exists() and destination.resolve() != source.resolve():
        raise UserError("destination collision")
    backup = backup_once(run_dir, record["source"], source)
    # The CLI's `move` will not create the destination folder for us.
    destination.parent.mkdir(parents=True, exist_ok=True)
    detail = mover.move(vault, run_dir, record["source"], record["destination"], expected)
    return backup, detail


# The journal op each action writes in the move phase. A `rewrite` that also
# relocates journals twice — `rewrite` for the content, `rewrite_move` for the
# move — so a resumed run can tell which half already landed.
MOVE_OPS = {"quarantine": "quarantine", "move_only": "move_only", "rewrite": "rewrite_move"}


def apply_records(args, vault, run_dir, records, counts, schema, mover=None):
    log_path = run_dir / "apply-log.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
    done = {(entry.get("op"), entry.get("source")) for entry in prior if entry.get("status") == "ok"}
    if mover is None:
        mover = PlainMover()
    fallback = PlainMover()
    actionable = [
        record for record in records if record["action"] in {"quarantine", "move_only", "rewrite"}
        and record["status"] != "failed"
    ]
    for record in records:
        if record["action"] == "none" and (record["needs_review"] or record["status"] == "failed"):
            counts["skipped"] += 1

    expected = {record["source"]: record["source_hash"] for record in actionable}
    # Content first, then locations. Within each phase, source order.
    content = sorted(
        (record for record in actionable if record["action"] == "rewrite"), key=lambda record: record["source"]
    )
    moves = sorted(
        (
            record
            for record in actionable
            if record["action"] in {"quarantine", "move_only"}
            or (record["action"] == "rewrite" and record["destination"] != record["source"])
        ),
        key=lambda record: ({"quarantine": 0}.get(record["action"], 1), record["source"]),
    )

    def journal(op, record, status, **extra):
        entry = {"op": op, "status": status, "source": record["source"], "destination": record["destination"]}
        entry.update(extra)
        run_state.append_jsonl_fsync(log_path, entry)

    for record in content:
        if ("rewrite", record["source"]) in done:
            # Re-derive the hash so a resumed run's later move still verifies.
            path = vault / record["source"]
            if path.is_file():
                expected[record["source"]] = sha256_bytes(path.read_bytes())
            continue
        try:
            backup = apply_rewrite_content(vault, run_dir, record, schema, expected)
            journal("rewrite", record, "ok", backup=str(backup))
        except Exception as error:
            record["apply_failed"] = True
            counts["failed"] += 1
            journal("rewrite", record, "error", error=str(error))

    for record in moves:
        op = MOVE_OPS[record["action"]]
        if record.get("apply_failed"):
            continue
        if (op, record["source"]) in done:
            counts["applied"] += 1
            continue
        # Quarantine always uses a plain rename. It moves a duplicate into a
        # dot-directory Obsidian does not index, so rewriting inbound links to
        # chase it there would point them at something the app cannot resolve.
        if op == "quarantine" or getattr(mover, "disabled", False):
            active = fallback
        else:
            active = mover
        try:
            backup, detail = apply_move_operation(vault, run_dir, record, expected, active)
            counts["applied"] += 1
            journal(op, record, "ok", backup=str(backup), **detail)
        except Exception as error:
            counts["failed"] += 1
            journal(op, record, "error", error=str(error))

    # A rewrite that did not need to move still counts as applied; one that did
    # was counted by its move above.
    for record in content:
        if not record.get("apply_failed") and record["destination"] == record["source"]:
            counts["applied"] += 1


def resolved_options(args):
    return {
        "model": args.model,
        "base_url": args.base_url,
        "embeddings_url": args.embeddings_url,
        "embeddings_model": args.embeddings_model,
        "no_embeddings": args.no_embeddings,
        "near_dupe_auto": args.near_dupe_auto,
        "near_dupe_review": args.near_dupe_review,
        "containment_min": CONTAINMENT_MIN,
        "limit": args.limit,
        "prompt_version": PROMPT_VERSION,
        "cache_prompt": args.cache_prompt,
        "think_prefill": args.think_prefill,
        "reuse_frontmatter": args.reuse_frontmatter,
        "only_sources": args.only_sources,
        "schema": args.schema,
        "profile": args.profile,
    }


def run_configuration(args, vault, schema_hash):
    return {
        "workflow": WORKFLOW,
        "command": args.mode,
        "input": {
            "vault": str(vault),
            "mode": args.mode,
            "schema_hash": schema_hash,
            # A changed register would file the rest of the run by different
            # habits than the ones it started with, the same way a changed voice
            # note makes a resumed draft run incompatible in the sibling skills.
            **vault_profile.profile_state(
                getattr(args, "profile_path", None),
                getattr(args, "profile_hash", None),
                classification_site(),
            ),
        },
        "options": resolved_options(args),
    }


RESUMABLE_OPTION_FLAGS = {
    "model": "--model",
    "base_url": "--base-url",
    "embeddings_url": "--embeddings-url",
    "embeddings_model": "--embeddings-model",
    "near_dupe_auto": "--near-dupe-auto",
    "near_dupe_review": "--near-dupe-review",
    "limit": "--limit",
    "schema": "--schema",
    "profile": "--profile",
}


def adopt_stored_options(args, state):
    stored = state.get("options", {})
    for key, flag in RESUMABLE_OPTION_FLAGS.items():
        provided = getattr(args, f"{key}_provided", False)
        current = getattr(args, key)
        if provided and current != stored.get(key):
            raise UserError(
                f"{flag} differs from the original run ({current!r} vs {stored.get(key)!r}); start a new run instead of --run"
            )
        setattr(args, key, stored.get(key))
    if args.no_embeddings != stored.get("no_embeddings") and stored.get("no_embeddings") is not None:
        if args.no_embeddings:
            raise UserError("--no-embeddings differs from the original run; start a new run instead of --run")
        args.no_embeddings = stored.get("no_embeddings")
    if not args.cache_prompt and stored.get("cache_prompt"):
        raise UserError("--no-cache-prompt differs from the original run; start a new run instead of --run")
    args.cache_prompt = stored.get("cache_prompt", args.cache_prompt)
    if args.think_prefill and not stored.get("think_prefill"):
        raise UserError("--think-prefill differs from the original run; start a new run instead of --run")
    args.think_prefill = stored.get("think_prefill", args.think_prefill)


def organize(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    resuming = bool(args.run)
    state = None
    if resuming:
        run_dir = Path(args.run).expanduser().resolve()
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        if state.get("command") != args.mode:
            raise UserError(f"run was started in {state.get('command')} mode, not {args.mode}")
        adopt_stored_options(args, state)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path)
    load_profile(args, vault)
    configuration = run_configuration(args, vault, schema_hash)
    if resuming:
        try:
            run_state.assert_compatible_run(state, configuration)
        except ValueError as error:
            raise UserError(str(error)) from error
    warnings = list(getattr(args, "profile_warnings", []) or [])
    # Before the lock and before any classification: a route naming a folder that
    # does not exist makes filing create a second one, so nothing should be spent
    # on a run that must not apply.
    # Property-vocabulary findings ride along at medium and below, so they inform
    # the report and the warnings without ever reaching the `high` block below.
    schema_drift = merge_drift_findings(
        check_schema_drift(vault, schema), check_property_drift(obsidian_cli.probe(vault), schema)
    )
    # Only what the user must act on becomes a warning. Reserved slots are
    # correct behavior, and a run that warns about them every time trains the
    # reader to skip the section where the real collisions appear. Every
    # finding, at every severity, is still in report.md and plan.json.
    for finding in schema_drift:
        if finding["severity"] in {"high", "medium"}:
            warnings.append(
                f"schema drift [{finding['severity']}] {finding['id']} {finding['path']}: {finding['detail']}"
            )
    blocking = [finding for finding in schema_drift if finding["severity"] == "high"]
    if blocking and args.apply and not args.allow_schema_drift:
        listed = "; ".join(f"{finding['id']} {finding['path']}" for finding in blocking)
        raise UserError(
            f"schema drift would file notes into a second folder ({listed}); fix it with "
            f"`drift --vault {vault}`, or pass --allow-schema-drift to apply anyway"
        )
    with run_state.run_lock(vault / ".vault-organizer"):
        if not resuming:
            run_dir = unique_run_directory(vault)
            state = run_state.create_run_state(
                WORKFLOW,
                args.mode,
                configuration["input"],
                configuration["options"],
                phase="scan",
            )
            run_state.initialize_run_state(run_dir, state)

        scan_path = run_dir / "scan.json"
        if scan_path.is_file():
            scan_data = json.loads(scan_path.read_text(encoding="utf-8"))
            items = scan_data["items"]
            if resuming:
                current_items, _ = scan_vault(vault, schema_path, args.mode, args.limit, args.only_sources)
                # Named for what it is: files moving under the run, not the
                # schema-versus-disk drift checked above. `drift` alone would
                # also shadow the mode function of that name.
                input_drift = run_state.input_drift(items, current_items)
                for added in input_drift["added"]:
                    warnings.append(f"input drift: {added['path']} appeared after the scan; run again to include it")
                for removed in input_drift["removed"]:
                    warnings.append(f"input drift: {removed['path']} disappeared after the scan")
                for changed in input_drift["changed"]:
                    warnings.append(f"input drift: {changed['after']['path']} changed after the scan; it will be refused at apply")
        else:
            items, _ = scan_vault(vault, schema_path, args.mode, args.limit, args.only_sources)
            run_state.atomic_write_json(scan_path, {"items": items})
            run_state.update_run_state(
                run_dir,
                lambda draft: draft.update({
                    "phase": "dedupe",
                    "items": [{"id": item["path"], "status": "pending"} for item in items],
                }) or draft,
                event={"type": "phase", "phase": "scan", "selected": len(items)},
            )
        log(args, f"selected {len(items)} notes")

        dedupe_path = run_dir / "dedupe.json"
        if dedupe_path.is_file():
            dedupe = json.loads(dedupe_path.read_text(encoding="utf-8"))
            losers = {}
            for group in dedupe.get("groups", []):
                for loser in group["losers"]:
                    losers[loser["path"]] = {
                        "winner": group["winner"],
                        "kind": group["kind"],
                        "quarantine_to": loser["quarantine_to"],
                        "sha256": loser["sha256"],
                    }
        else:
            index_entries = None
            if args.mode == "inbox":
                index_entries, index_warnings = refresh_vault_index(vault, schema_path)
                warnings.extend(index_warnings)
            try:
                schema_label = relative_path(vault, schema_path)
            except ValueError:
                schema_label = str(schema_path)
            dedupe, losers = plan_dedupe(args, vault, items, index_entries, warnings, schema_label=schema_label)
            run_state.atomic_write_json(dedupe_path, dedupe)
            run_state.update_run_state(
                run_dir,
                lambda draft: draft.update({"phase": "classify"}) or draft,
                event={
                    "type": "phase",
                    "phase": "dedupe",
                    "groups": len(dedupe["groups"]),
                    "losers": len(losers),
                    "review_pairs": len(dedupe["review_pairs"]),
                },
            )

        class_records, classify_warnings = classify_items(args, vault, schema, schema_hash, items, losers, run_dir)
        warnings.extend(classify_warnings)
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update({"phase": "verify"}) or draft,
            event={"type": "phase", "phase": "classify", "records": len(class_records)},
        )

        verification = None
        if args.verify:
            verification, verify_warnings = verify_classifications(args, vault, schema, class_records, run_dir)
            warnings.extend(verify_warnings)
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update({"phase": "route"}) or draft,
            event={"type": "phase", "phase": "verify", **(verification or {"skipped": "disabled by --no-verify"})},
        )

        applied_log, _ = run_state.read_jsonl_recover_tail(run_dir / "apply-log.jsonl", repair=True)
        done_map = {
            entry["source"]: entry
            for entry in applied_log
            if entry.get("status") == "ok" and entry.get("source") and entry.get("destination")
        }
        records = route_records(args, vault, items, losers, class_records, warnings, done_map=done_map)
        counts = recompute_counts(records, dedupe, items)
        write_review_queue(run_dir, records)
        base_references = scan_base_references(vault, records, session=obsidian_cli.probe(vault))

        # Resolved even for a dry run, so the plan says which move strategy the
        # apply will use rather than leaving it to be discovered afterwards.
        mover, mover_reason = resolve_mover(args.link_rewrite, vault)
        if args.apply:
            if mover_reason:
                warnings.append(f"moves use a plain rename: {mover_reason}")
            apply_records(args, vault, run_dir, records, counts, schema, mover=mover)
            warnings.extend(mover.warnings)
            _, index_warnings = refresh_vault_index(vault, schema_path)
            warnings.extend(index_warnings)
            final_phase = "complete"
        else:
            final_phase = "planned"
        plan_path, report_path = write_plan(
            run_dir, records, counts, dedupe, base_references, args.mode, not args.apply, vault, schema_hash, warnings,
            verification, schema_drift, link_rewrite=(mover.mode, mover_reason),
        )
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update({
                "phase": final_phase,
                "status": "complete" if final_phase == "complete" else "running",
                "nextAction": None if final_phase == "complete" else f"review {report_path.name}, then rerun with --apply --run {run_dir}",
            }) or draft,
            event={"type": "phase", "phase": final_phase, "counts": counts},
        )
    return structured(
        "ok",
        artifacts=[str(plan_path), str(report_path)],
        warnings=warnings,
        data={
            "mode": args.mode,
            "dry_run": not args.apply,
            "vault": str(vault),
            "schema_hash": schema_hash,
            "run_directory": str(run_dir),
            "counts": counts,
            "schema_drift": drift_counts(schema_drift),
            "link_rewrite": {"mode": mover.mode, "reason": mover_reason},
        },
    )


ASSET_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif",
    ".pdf",
    ".mp3", ".m4a", ".wav", ".ogg", ".flac",
    ".mp4", ".mov", ".webm", ".mkv",
}
EXTERNAL_SCHEMES = ("http://", "https://", "mailto:", "data:", "tel:")
MD_EMBED_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]*)\)")
WIKI_EMBED_RE = re.compile(r"!\[\[(?P<target>[^\]|#]+)(?P<rest>[^\]]*)\]\]")
FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
# A line left holding only list scaffolding after an embed is removed.
STRUCTURAL_ONLY_RE = re.compile(r"\s*(?:[-*+]\s*(?:\[[ xX]\]\s*)?|\d+[.)]\s*)?$")


def code_span_ranges(line):
    """Character ranges covered by inline code spans, delimiters included.

    Text inside backticks is documentation, not a link. The schema note alone
    carries fifteen `"[[Project]]"` registry examples, and the Obsidian Bases
    note documents `"[[link/to/attachment.jpg]]"`; rewriting either would be
    corruption, so every match inside these ranges is skipped.
    """
    ranges = []
    index = 0
    length = len(line)
    while index < length:
        if line[index] != "`":
            index += 1
            continue
        start = index
        while index < length and line[index] == "`":
            index += 1
        fence = index - start
        closing = line.find("`" * fence, index)
        while closing != -1 and closing + fence < length and line[closing + fence] == "`":
            closing = line.find("`" * fence, closing + fence)
        if closing == -1:
            break
        ranges.append((start, closing + fence))
        index = closing + fence
    return ranges


def inside_ranges(position, ranges):
    return any(start <= position < end for start, end in ranges)


def asset_index(vault):
    """Basename -> vault-relative paths, for every asset file outside skipped trees."""
    index = {}
    for directory, dirnames, filenames in os.walk(vault, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [
            name for name in sorted(dirnames)
            if name not in PROTECTED_DIRS
            and not name.startswith(".")
            and not (dirpath / name).is_symlink()
            and not is_workspace_dir(dirpath / name)
        ]
        for filename in sorted(filenames):
            path = dirpath / filename
            if path.is_symlink() or path.suffix.lower() not in ASSET_EXTENSIONS:
                continue
            index.setdefault(filename, []).append(path.resolve().relative_to(vault).as_posix())
    return index


def normalize_target(raw):
    """The path part of an embed target, percent-decoded, without a title suffix."""
    text = raw.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    for quote in ('"', "'"):
        marker = f" {quote}"
        if marker in text:
            text = text.split(marker, 1)[0].strip()
    return urllib.parse.unquote(text)


def classify_embed(vault, note, target, assets, kind):
    """`external`, `resolves`, `repairable`, `ambiguous`, or `missing`."""
    if target.startswith(EXTERNAL_SCHEMES):
        return "external", None
    local = target[len("file://"):].lstrip("/") if target.startswith("file://") else target
    if Path(local).suffix.lower() not in ASSET_EXTENSIONS:
        return "external", None
    matches = assets.get(Path(local).name, [])
    if kind == "wiki":
        # Obsidian resolves a wikilink by name across the whole vault, so any
        # existing file of that name means the embed already works. Treating it
        # as repairable would rewrite it to itself and report a healthy vault
        # as broken on every run.
        return ("resolves", None) if matches else ("missing", None)
    if not target.startswith("file://") and ((note.parent / local).exists() or (vault / local).exists()):
        return "resolves", None
    if len(matches) == 1:
        return "repairable", matches[0]
    if len(matches) > 1:
        return "ambiguous", None
    return "missing", None


def scan_note_embeds(vault, note, assets):
    """Every asset embed in one note, with its line, span, and classification."""
    text = note.read_text(encoding="utf-8", errors="strict")
    findings = []
    fenced = False
    for number, line in enumerate(text.splitlines(), 1):
        if FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        spans = code_span_ranges(line)
        for pattern, kind in ((WIKI_EMBED_RE, "wiki"), (MD_EMBED_RE, "md")):
            for match in pattern.finditer(line):
                if inside_ranges(match.start(), spans):
                    continue
                target = normalize_target(match.group("target"))
                if not target:
                    continue
                alt = match.group("alt").strip() if kind == "md" else ""
                classification, resolved = classify_embed(vault, note, target, assets, kind)
                if classification == "external":
                    continue
                findings.append({
                    "line": number,
                    "start": match.start(),
                    "end": match.end(),
                    "kind": kind,
                    "text": match.group(0),
                    "target": target,
                    "alt": alt,
                    "classification": classification,
                    "resolved": resolved,
                })
    return text, findings


def repaired_note_text(text, findings):
    """Apply repairs and strips, returning the new text and the actions taken."""
    actionable = [f for f in findings if f["classification"] in {"repairable", "missing"}]
    if not actionable:
        return text, []
    by_line = {}
    for finding in actionable:
        by_line.setdefault(finding["line"], []).append(finding)

    ends_with_newline = text.endswith("\n")
    lines = text.splitlines()
    actions = []
    dropped = set()
    for number, group in by_line.items():
        line = lines[number - 1]
        stripped_any = False
        # Right to left, so earlier spans keep their offsets.
        for finding in sorted(group, key=lambda item: item["start"], reverse=True):
            if finding["classification"] == "repairable":
                replacement = f"![[{Path(finding['target']).name}]]"
            else:
                # Alt text becomes prose, so it needs the word boundaries the
                # embed syntax used to provide. Word exports put several image
                # embeds back to back, and "DATE" + "Draft Report" must not
                # concatenate into "DATEDraft Report".
                replacement = finding["alt"]
                if replacement:
                    preceding = line[finding["start"] - 1] if finding["start"] > 0 else ""
                    following = line[finding["end"]] if finding["end"] < len(line) else ""
                    if preceding and not preceding.isspace():
                        replacement = f" {replacement}"
                    if following and not following.isspace():
                        replacement = f"{replacement} "
                stripped_any = True
            line = line[: finding["start"]] + replacement + line[finding["end"] :]
            actions.append({
                "line": number,
                "action": "repair" if finding["classification"] == "repairable" else "strip",
                "before": finding["text"],
                "after": replacement,
                "target": finding["target"],
                "resolved": finding["resolved"],
            })
        collapsed = re.sub(r"[ \t]{2,}", " ", line).rstrip()
        if stripped_any and STRUCTURAL_ONLY_RE.fullmatch(collapsed):
            dropped.add(number)
            actions.append({"line": number, "action": "drop_line", "before": lines[number - 1], "after": None})
        else:
            lines[number - 1] = collapsed
    kept = [line for number, line in enumerate(lines, 1) if number not in dropped]
    revised = "\n".join(kept)
    if ends_with_newline and revised:
        revised += "\n"
    return revised, sorted(actions, key=lambda item: (item["line"], item["action"]))


def render_attachment_report(rows, counts):
    lines = [
        "# Attachment link report",
        "",
        f"- Notes scanned: {counts['notes_scanned']}",
        f"- Asset embeds found: {counts['embeds']}",
        f"- Already resolving: {counts['resolves']}",
        f"- Repairable: {counts['repairable']}",
        f"- Ambiguous (left alone): {counts['ambiguous']}",
        f"- Missing: {counts['missing']}",
        "",
        "Every embed below is recorded before any edit, so a stripped link's",
        "filename stays recoverable from this file.",
        "",
    ]
    for row in rows:
        if not row["findings"]:
            continue
        lines.append(f"## {row['note']}")
        lines.append("")
        for finding in row["findings"]:
            lines.append(f"- line {finding['line']} — **{finding['classification']}** — `{finding['text']}`")
            if finding["resolved"]:
                lines.append(f"    - resolves to `{finding['resolved']}`")
        lines.append("")
    return "\n".join(lines)


def attachments(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    assets = asset_index(vault)
    notes = selected_notes(vault, schema_path, "vault", args.limit)

    rows = []
    counts = {"notes_scanned": 0, "embeds": 0, "resolves": 0, "repairable": 0, "ambiguous": 0, "missing": 0}
    warnings = []
    for note in notes:
        counts["notes_scanned"] += 1
        try:
            text, findings = scan_note_embeds(vault, note, assets)
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"unreadable note skipped: {relative_path(vault, note)} ({error})")
            continue
        for finding in findings:
            counts["embeds"] += 1
            counts[finding["classification"]] += 1
        if findings:
            rows.append({"note": relative_path(vault, note), "path": note, "text": text, "findings": findings})

    run_dir = unique_run_directory(vault)
    report_rows = [{"note": row["note"], "findings": row["findings"]} for row in rows]
    # Written before any edit: stripping discards filenames, and this is their record.
    (run_dir / "attachment_report.json").write_text(
        json.dumps({"counts": counts, "notes": report_rows}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (run_dir / "attachment_report.md").write_text(render_attachment_report(report_rows, counts), encoding="utf-8")

    planned = []
    for row in rows:
        revised, actions = repaired_note_text(row["text"], row["findings"])
        if not actions:
            continue
        planned.append({"note": row["note"], "path": row["path"], "revised": revised, "actions": actions})

    applied = []
    if args.apply:
        for entry in planned:
            source = entry["path"]
            backup = run_dir / "backup" / entry["note"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
            handle, temp_name = tempfile.mkstemp(prefix=f".{source.name}.", suffix=".tmp", dir=str(source.parent))
            try:
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    stream.write(entry["revised"])
                os.replace(temp_name, source)
            except BaseException:
                Path(temp_name).unlink(missing_ok=True)
                raise
            applied.append(entry["note"])

    if counts["ambiguous"]:
        warnings.append(f"{counts['ambiguous']} embed(s) match several files by name and were left untouched")
    if counts["missing"] and not args.apply:
        warnings.append(f"{counts['missing']} embed(s) target files that no longer exist and would be stripped")

    return structured(
        "ok",
        artifacts=[str(run_dir / "attachment_report.md"), str(run_dir / "attachment_report.json")],
        warnings=warnings,
        data={
            "vault": str(vault),
            "runDirectory": str(run_dir),
            "dryRun": not args.apply,
            "counts": counts,
            "notesChanged": len(planned),
            "applied": applied,
            "plan": [{"note": entry["note"], "actions": entry["actions"]} for entry in planned],
        },
    )


# --------------------------------------------------------------------------- #
# Date backfill
# --------------------------------------------------------------------------- #


# For these the note's creation date and its subject date are routinely
# different things — a wiki entry on a 1920 event was written last year — so a
# derived date is never written to one without a human naming its id.
SUBJECT_DATE_REVIEW_TYPES = frozenset({"wiki", "source"})


def atomic_write_bytes(path, data):
    """Replace a note's bytes exactly. Text mode would rewrite line endings."""
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def archive_fingerprints(roots, args, warnings):
    """Read every Markdown file under the archive roots, once.

    Nothing is assumed of an archive: no schema, no folder meaning, no
    frontmatter. Files are opened read-only, and this mode never writes to one.
    """
    entries = []
    seen = set()
    for base in roots:
        if not base.is_dir():
            raise UserError(f"archive root does not exist: {base}")
        label = base.name or "archive"
        for directory, dirnames, filenames in os.walk(base, followlinks=False):
            dirpath = Path(directory)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not (dirpath / name).is_symlink() and name not in PROTECTED_DIRS and not name.startswith(".")
            ]
            for filename in sorted(filenames):
                path = dirpath / filename
                if path.is_symlink() or path.suffix.lower() != ".md":
                    continue
                resolved = path.resolve()
                if str(resolved) in seen:
                    continue
                seen.add(str(resolved))
                relative = f"{label}/{resolved.relative_to(base).as_posix()}"
                try:
                    entries.append(
                        vault_dates.note_fingerprint(
                            resolved,
                            relative,
                            resolved.read_bytes(),
                            include_file_times=args.include_file_times,
                            trust_birthtime=args.trust_birthtime,
                        )
                    )
                except (OSError, UnicodeDecodeError) as error:
                    warnings.append(f"archive file skipped: {relative} ({error})")
    return entries


def near_match_archive(args, pending, archive, warnings):
    """Pair by meaning what name and hash could not place.

    Reuses the thresholds the dedupe path already trusts — cosine at or above
    ``--near-dupe-auto`` with line containment at or above 0.90. Anything below
    is not proposed at all, and an unreachable endpoint costs the run nothing
    but the deterministic tiers it already has.
    """
    pool = [entry for entry in archive if len(entry["normalized"]) >= MIN_NEAR_DUPE_CHARS]
    pending = [note for note in pending if len(note["normalized"]) >= MIN_NEAR_DUPE_CHARS]
    if not pending or not pool:
        return {}
    texts = [embedding_text(entry["title"], entry["normalized"]) for entry in pool + pending]
    response = forge_embeddings.embed_texts(texts, url=args.embeddings_url, model=args.embeddings_model)
    if not response["ok"]:
        warnings.append(f"embeddings unavailable, near matching skipped: {response['reason']}")
        return {}
    vectors = [forge_embeddings.normalize(vector) for vector in response["vectors"]]
    archive_vectors = vectors[: len(pool)]
    note_vectors = vectors[len(pool) :]
    found = {}
    for index, note in enumerate(pending):
        best = None
        for position, entry in enumerate(pool):
            score = forge_embeddings.cosine(note_vectors[index], archive_vectors[position])
            if score < args.near_dupe_auto or (best is not None and score <= best[1]):
                continue
            shorter, longer = sorted((note["normalized"], entry["normalized"]), key=len)
            if line_containment(shorter, longer) < CONTAINMENT_MIN:
                continue
            best = (entry, score)
        if best is not None:
            found[note["relative"]] = best[0]
    return found


def render_calibration(calibration, trusted):
    """The birthtime accuracy measurement, in the terms the decision needs."""
    if not calibration["with_birthtime"]:
        return ["## Finder creation dates", "", "No file carries one; this filesystem does not record them.", ""]
    lines = [
        "## Finder creation dates",
        "",
        f"- Archive files carrying one: {calibration['with_birthtime']} of {calibration['files']}",
        f"- Of those, also stating a date in their name or frontmatter: {calibration['labelled']}",
    ]
    if calibration["labelled"]:
        lines += [
            f"- Creation date matches that stated date exactly: {calibration['same_day']}"
            f" ({calibration['agreement']:.0%})",
            f"- Within a day of it: {calibration['within_a_day']} ({calibration['loose_agreement']:.0%})",
        ]
    if calibration["largest_cluster"]:
        day, count = calibration["largest_cluster"]
        lines.append(f"- Largest single-day cluster: {count} files created {day} ({calibration['clustered']:.0%})")
    lines += [
        "",
        "The files that state a date *and* carry a creation date are the labelled",
        "examples: how often those two agree is roughly how often the creation date",
        "is right on the files that state nothing. A large single-day cluster is the",
        "opposite signal — that day is when the archive was copied, and every file in",
        "it lost its original date.",
        "",
    ]
    if trusted:
        lines += ["Creation dates were **trusted** for this run: they count as explicit evidence.", ""]
    else:
        lines += [
            "Creation dates are currently **weak** evidence and are never written",
            "unattended. If the agreement above is high and the clustering low, re-run",
            "with `--trust-birthtime` to promote them.",
            "",
        ]
    return lines


def render_date_report(rows, skipped, counts, property_key, dry_run, calibration=None, trusted=False):
    lines = [
        f"# Date backfill report ({property_key})",
        "",
        f"- Notes scanned: {counts['notes']}",
        f"- Already carrying {property_key}: {counts['already_dated']}",
        f"- Needing one: {counts['needing']}",
        f"- Archive files read: {counts['archive_files']}",
        f"- Dated with confidence (applied by `--apply`): {counts['high']}",
        f"- Held for review (applied by `--apply --ids`): {counts['medium'] + counts['low']}",
        f"- No evidence anywhere: {counts['no_evidence']}",
        "",
        "Confidence is the weaker of two things: how sure we are that an archive",
        "file is an older copy of the note, and how explicitly that file states a",
        "date. Only `high` is written without you naming its id.",
        "",
    ]
    if dry_run:
        lines += ["This was a dry run. Nothing has been written.", ""]
    if calibration is not None:
        lines += render_calibration(calibration, trusted)

    groups = (
        ("Ready to apply", "high", "Exact match on an explicitly dated file."),
        ("Held for review", "medium", "Name it with `--ids` to write it."),
        (
            "Year known, day not",
            "year",
            "A year folder or an import stamp fixes the year; the day is a `-01-01` "
            "placeholder, not a reading. `--year-only` writes these.",
        ),
        ("Weak evidence", "low", "Nothing structural said this; read before naming it."),
    )
    for title, level, blurb in groups:
        chosen = [row for row in rows if row["confidence"] == level]
        if not chosen:
            continue
        lines += [f"## {title} ({len(chosen)})", "", blurb, ""]
        for row in chosen:
            lines.append(f"- `{row['id']}` **{row['date']}** — {row['note']}")
            lines.append(f"    - {row['match']} match on `{row['source']}`, {row['evidence']} evidence: `{row['quote']}`")
            if row["contradicted"]:
                lines.append("    - that file dates itself two different ways; both are listed below")
            if row["review_reason"]:
                lines.append(f"    - held because: {row['review_reason']}")
            for other in row["considered"][1:]:
                lines.append(
                    f"    - also considered {other['date']} from `{other['source']}` "
                    f"({other['match']} match, {other['evidence']}, {other['why']})"
                )
        lines.append("")

    if skipped:
        lines += [f"## No evidence ({len(skipped)})", "", "These need a date typed by hand.", ""]
        for entry in skipped:
            lines.append(f"- {entry['note']}" + (f" — {entry['reason']}" if entry["reason"] else ""))
        lines.append("")
    return "\n".join(lines)


def dates(args):
    """Backfill a human-owned date property from older copies of the notes.

    Deterministic by default: no model, and no embeddings unless ``--near-match``
    asks for them. Dry run is the default, and the report is written before any
    note is touched.
    """
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    if not args.archive and not args.self_only:
        raise UserError("dates requires --archive <path> (repeatable), or --self-only to use each note's own evidence")
    schema_path = resolve_schema_path(vault, args.schema)
    schema, _ = compiled_schema_for(vault, schema_path)

    property_key = args.date_property
    prop = schema["properties"].get(property_key)
    if prop is None:
        approved = ", ".join(schema["property_order"])
        raise UserError(f"{property_key} is not an approved property; the schema approves: {approved}")
    if prop["shape"] != "scalar":
        raise UserError(f"{property_key} is a list property; this mode writes scalars only")

    warnings = []
    if not prop["human_owned"]:
        warnings.append(
            f"{property_key} is not marked human-owned in the schema, so the next filing run will "
            "replace whatever this writes; mark it human-owned first"
        )

    archive_roots = [Path(raw).expanduser().resolve() for raw in args.archive]
    archive = archive_fingerprints(archive_roots, args, warnings)

    notes = []
    for path in selected_notes(vault, schema_path, "vault", args.limit):
        # An archive kept inside the vault is a source here, never a target.
        if any(path_is_inside(root, path) for root in archive_roots):
            continue
        relative = relative_path(vault, path)
        try:
            notes.append(
                vault_dates.note_fingerprint(
                    path,
                    relative,
                    path.read_bytes(),
                    include_file_times=args.include_file_times,
                    trust_birthtime=args.trust_birthtime,
                )
            )
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"note skipped: {relative} ({error})")

    # Two facts belong to the corpus rather than to any file: which days were
    # import events, and which way this vault writes a spaced numeric date.
    # Both are measured over everything read, then fed back through the evidence.
    corpus = archive + notes
    stamps = vault_dates.stamp_days(corpus)
    month_first, convention = vault_dates.filename_convention(corpus)
    vault_dates.recalculate_dates(
        corpus,
        month_first=month_first,
        stamps=set(stamps),
        trust_birthtime=args.trust_birthtime,
        include_file_times=args.include_file_times,
    )
    if stamps:
        biggest = max(stamps.items(), key=lambda item: item[1])
        warnings.append(
            f"{len(stamps)} birthtime day(s) look like import stamps, the largest {biggest[0]} "
            f"covering {biggest[1]} distinct notes; their day is dropped and only the year kept"
        )

    counts = {
        "notes": len(notes),
        "already_dated": 0,
        "needing": 0,
        "archive_files": len(archive),
        "high": 0,
        "medium": 0,
        "year": 0,
        "low": 0,
        "no_evidence": 0,
    }

    pending = []
    skipped = []
    for note in notes:
        if note["malformed"]:
            skipped.append({"note": note["relative"], "reason": "frontmatter has no closing delimiter"})
            continue
        existing = parse_frontmatter(note["frontmatter_text"]).get(property_key)
        if isinstance(existing, str) and existing.strip():
            counts["already_dated"] += 1
            continue
        pending.append(note)
    counts["needing"] = len(pending)

    indexes = vault_dates.build_indexes(archive, notes)
    matched = {note["relative"]: vault_dates.match_archive(note, indexes) for note in pending}
    if args.near_match:
        unplaced = [note for note in pending if not matched[note["relative"]]]
        for relative, entry in near_match_archive(args, unplaced, archive, warnings).items():
            matched[relative] = [(entry, vault_dates.SIMILAR)]

    rows = []
    for note in pending:
        decision = vault_dates.decide(note, matched[note["relative"]])
        if decision is None:
            counts["no_evidence"] += 1
            skipped.append({"note": note["relative"], "reason": ""})
            continue
        note_type = parse_frontmatter(note["frontmatter_text"]).get("type")
        review_reason = ""
        if isinstance(note_type, str) and note_type.strip() in SUBJECT_DATE_REVIEW_TYPES:
            review_reason = f"a {note_type.strip()} note's subject date is not usually the day it was written"
        confidence = decision["confidence"]
        if review_reason and confidence == "high":
            confidence = "medium"
        counts[confidence] += 1
        rows.append(
            {
                "id": vault_dates.decision_id(note["relative"], decision["date"]),
                "note": note["relative"],
                "path": note["path"],
                "hash": note["data_hash"],
                "date": decision["date"],
                "match": decision["match"],
                "evidence": decision["evidence"],
                "source": decision["source"],
                "quote": decision["quote"],
                "contradicted": decision["contradicted"],
                "confidence": confidence,
                "review_reason": review_reason,
                "considered": decision["considered"],
            }
        )

    rank = {"high": 0, "medium": 1, "year": 2, "low": 3}
    rows.sort(key=lambda row: (rank.get(row["confidence"], 9), row["date"], row["note"]))
    by_id = {row["id"]: row for row in rows}
    requested = [value.strip() for value in (args.ids or "").split(",") if value.strip()]
    # An --ids that parses to nothing is a caller whose id list came out empty,
    # not a caller asking for the confident ones. Falling through to those would
    # write the whole tier the caller was trying to narrow.
    if args.ids is not None and not requested:
        raise UserError("--ids was given but names no ids; drop the flag to write the confident ones")
    for value in requested:
        if value not in by_id:
            offered = ", ".join(sorted(by_id)) or "none"
            raise UserError(f"unknown id {value}; this run proposes: {offered}")

    calibration = vault_dates.calibrate_birthtime(archive or notes)
    run_dir = unique_run_directory(vault)
    report = render_date_report(rows, skipped, counts, property_key, not args.apply, calibration, args.trust_birthtime)
    # Written before any edit, so the evidence behind every date outlives the run.
    (run_dir / "date_report.md").write_text(report, encoding="utf-8")
    (run_dir / "date_report.json").write_text(
        json.dumps(
            {
                "property": property_key,
                "counts": counts,
                "birthtimeCalibration": calibration,
                "importStamps": dict(sorted(stamps.items(), key=lambda item: -item[1])),
                "filenameConvention": {"monthFirst": month_first, **convention},
                "proposals": [{key: row[key] for key in row if key != "path"} for row in rows],
                "noEvidence": skipped,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    auto = {"high", "year"} if args.year_only else {"high"}
    targets = [by_id[value] for value in requested] if requested else [row for row in rows if row["confidence"] in auto]
    applied = []
    refused = []
    if args.apply:
        for row in targets:
            source = Path(row["path"])
            try:
                data = source.read_bytes()
            except OSError as error:
                refused.append({"note": row["note"], "reason": str(error)})
                continue
            if sha256_bytes(data) != row["hash"]:
                refused.append({"note": row["note"], "reason": "changed since the report was written"})
                continue
            revised, reason = vault_dates.insert_scalar_property(data, property_key, row["date"], schema["property_order"])
            if revised is None:
                refused.append({"note": row["note"], "reason": reason})
                continue
            backup = run_dir / "backup" / row["note"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
            atomic_write_bytes(source, revised)
            applied.append({"id": row["id"], "note": row["note"], "date": row["date"]})

    if counts["no_evidence"]:
        warnings.append(f"{counts['no_evidence']} note(s) offered no date evidence and need one by hand")
    if refused:
        warnings.append(f"{len(refused)} note(s) were refused at apply; see the result for each reason")
    if prop["required"] != "no":
        warnings.append(
            f"the schema already requires {property_key}; back the vault fill before requiring it, "
            "not after, or every undated note fails validation in the meantime"
        )

    return structured(
        "ok",
        artifacts=[str(run_dir / "date_report.md"), str(run_dir / "date_report.json")],
        warnings=warnings,
        data={
            "vault": str(vault),
            "archive": [str(root) for root in archive_roots],
            "property": property_key,
            "runDirectory": str(run_dir),
            "dryRun": not args.apply,
            "counts": counts,
            "birthtimeCalibration": calibration,
            "trustBirthtime": args.trust_birthtime,
            "proposed": len(rows),
            "wouldApply": len(targets),
            "applied": applied,
            "refused": refused,
            "plan": [
                {
                    "id": row["id"],
                    "note": row["note"],
                    "date": row["date"],
                    "confidence": row["confidence"],
                    "match": row["match"],
                    "evidence": row["evidence"],
                    "source": row["source"],
                }
                for row in rows
            ],
        },
    )


# Obsidian owns these three regardless of what the schema says, so a vault using
# one without declaring it is a smaller thing than an invented key.
OBSIDIAN_BUILTIN_PROPERTIES = frozenset({"aliases", "cssclasses", "tags"})

# Obsidian's registered property types, mapped to the shape the schema speaks in.
OBSIDIAN_LIST_TYPES = frozenset({"aliases", "multitext", "tags"})
OBSIDIAN_SCALAR_TYPES = frozenset({"checkbox", "date", "datetime", "number", "text"})


def check_property_drift(session, schema):
    """Drift between the schema's approved properties and what the vault holds.

    ``check_schema_drift`` compares compiled routes against folders; this compares
    the approved-property vocabulary against the properties actually in use. That
    question needs an index of every note's frontmatter, which Obsidian already
    keeps in memory, so the check exists only when the CLI is available and
    returns nothing at all when it is not.

    Nothing here rises above `medium`. An unapproved property is a conversation
    about vocabulary, not a filing hazard, so it must never block an apply.
    """
    if not session.get("available"):
        return []
    result = obsidian_cli.run_json(session, "properties", format="json", counts=True)
    if not result["ok"] or not isinstance(result.get("data"), list):
        return []

    approved = schema["property_order"]
    approved_set = set(approved)
    findings = []
    seen = set()
    for row in result["data"]:
        if not isinstance(row, dict):
            continue
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        seen.add(name)
        try:
            count = int(row.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        registered_type = row.get("type") if isinstance(row.get("type"), str) else None

        if name not in approved_set:
            if count == 0:
                # Registered as a type but written nowhere. Obsidian keeps these
                # around after the last note using them is cleaned up.
                continue
            builtin = name in OBSIDIAN_BUILTIN_PROPERTIES
            kind = "property_obsidian_builtin" if builtin else "property_unapproved"
            findings.append({
                "id": drift_finding_id(kind, name, ""),
                "severity": "low" if builtin else "medium",
                "kind": kind,
                "path": name,
                "route": None,
                "note_count": count,
                "property_type": registered_type,
                "detail": (
                    f"`{name}` is one of Obsidian's own properties and appears on {count} note(s), but the "
                    f"schema's **Approved properties** table does not list it."
                    if builtin
                    else f"`{name}` appears on {count} note(s) and is not in the schema's **Approved "
                    f"properties** table. A rewrite strips unapproved keys, so those values are one "
                    f"normalization away from being dropped."
                ),
                "suggestion": (
                    f"Add `{name}` to **Approved properties**, or remove it from the {count} note(s) that "
                    f"carry it. Nothing here is filed differently either way."
                ),
                "fix_side": "manual",
                "schema_row": None,
            })
            continue

        if count == 0 or registered_type is None:
            continue
        shape = schema["properties"][name]["shape"]
        registered_shape = (
            "list" if registered_type in OBSIDIAN_LIST_TYPES
            else "scalar" if registered_type in OBSIDIAN_SCALAR_TYPES
            else None
        )
        if registered_shape is None or registered_shape == shape:
            continue
        findings.append({
            "id": drift_finding_id("property_type_mismatch", name, ""),
            "severity": "medium",
            "kind": "property_type_mismatch",
            "path": name,
            "route": None,
            "note_count": count,
            "property_type": registered_type,
            "detail": (
                f"The schema gives `{name}` the shape `{shape}`, but Obsidian has it registered as "
                f"`{registered_type}` ({registered_shape}) across {count} note(s). Obsidian registers the "
                f"shape it actually finds, so some note writes this property the other way."
            ),
            "suggestion": (
                f"Find the notes writing `{name}` as a {registered_shape} and normalize them, or change the "
                f"schema's Shape cell. Until they agree, a merge into `{name}` can read a value it did not "
                f"expect."
            ),
            "fix_side": "manual",
            "schema_row": None,
        })

    for name in approved:
        if name in seen:
            continue
        findings.append({
            "id": drift_finding_id("property_unused", name, ""),
            "severity": "info",
            "kind": "property_unused",
            "path": name,
            "route": None,
            "note_count": 0,
            "property_type": None,
            "detail": f"`{name}` is approved by the schema but no note in the vault uses it.",
            "suggestion": f"Keep `{name}` if it is aspirational, or drop the row to keep the table honest.",
            "fix_side": "manual",
            "schema_row": None,
        })
    return findings


def merge_drift_findings(*groups):
    """Combine drift finding lists back into one severity-ordered list."""
    findings = [finding for group in groups for finding in group]
    return sorted(findings, key=lambda entry: (DRIFT_SEVERITY_ORDER[entry["severity"]], entry["path"]))


def render_drift_report(vault, schema_path, schema, findings, property_check=True):
    lines = [
        "# Schema drift report",
        "",
        f"- Vault: `{vault}`",
        f"- Schema: `{schema_path}`",
        f"- Compiled routes: {len(compiled_routes(schema))}",
        f"- Findings: {len(findings)}",
        "",
        "Every folder path is compiled from the schema's `Number` and `Label` cells;",
        "nothing reads folder names off disk. Where the two disagree, filing a note",
        "creates the missing folder rather than failing, so the notes split in two.",
        "Structure *below* a declared route is legitimate detail and is never reported.",
        "",
    ]
    if not property_check:
        lines.extend([
            "Property vocabulary was not checked: that needs an index of every note's frontmatter,",
            "which the Obsidian CLI supplies and nothing else here does. Folder routes below are",
            "checked the same way regardless.",
            "",
        ])
    lines.extend(drift_report_lines(findings))
    lines.extend([
        "## Fixing",
        "",
        "`--fix-schema <id>,<id>` applies schema-side fixes only, and only the ids you name.",
        "It never renames, moves, or deletes a folder — folder-side corrections are yours to make.",
        "",
    ])
    return "\n".join(lines) + "\n"


def apply_schema_fixes(vault, schema_path, findings, requested, run_dir):
    """Rewrite named registry rows, validating the result before it lands.

    The first code in this workflow that writes the schema note, so the rails are
    the point: named ids only, schema side only, backup first, and a temp file
    that has to parse and re-check clean before it replaces anything.
    """
    by_id = {finding["id"]: finding for finding in findings}
    selected = []
    for identifier in requested:
        finding = by_id.get(identifier)
        if finding is None:
            known = ", ".join(sorted(by_id)) or "none"
            raise UserError(f"unknown finding id {identifier}; this vault currently reports: {known}")
        if finding["fix_side"] != "schema" or not finding["schema_row"]:
            raise UserError(
                f"{identifier} ({finding['path']}) is a {finding['fix_side']}-side fix; --fix-schema only edits "
                f"the schema note. Do this one yourself: {finding['suggestion']}"
            )
        selected.append(finding)

    original = schema_path.read_text(encoding="utf-8")
    backup = run_dir / "backup" / relative_path(vault, schema_path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(schema_path, backup)

    revised = original
    changes = []
    for finding in selected:
        revised, before_line, after_line = replace_schema_row_number(revised, finding["schema_row"])
        changes.append({
            "id": finding["id"],
            "path": finding["path"],
            "route": finding["route"],
            "before": before_line,
            "after": after_line,
        })

    before_high = {finding["id"] for finding in findings if finding["severity"] == "high"}
    handle, temp_name = tempfile.mkstemp(prefix=f".{schema_path.name}.", suffix=".tmp", dir=str(schema_path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(revised)
            stream.flush()
            os.fsync(stream.fileno())
        # Validate what is actually on disk, not what we meant to write.
        candidate = Path(temp_name).read_text(encoding="utf-8-sig")
        try:
            new_schema = parse_schema_note(candidate)
            validate_derived_paths(new_schema)
        except UserError as error:
            raise UserError(f"fix rejected: the edited schema does not parse ({error}); nothing was written") from error
        after = check_schema_drift(vault, new_schema)
        introduced = [
            finding for finding in after if finding["severity"] == "high" and finding["id"] not in before_high
        ]
        if introduced:
            listed = "; ".join(f"{finding['id']} {finding['path']}" for finding in introduced)
            raise UserError(f"fix rejected: it would introduce new high-severity drift ({listed}); nothing was written")
        os.replace(temp_name, schema_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return changes, after


def drift(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    schema, _ = compiled_schema_for(vault, schema_path)
    cli_session = obsidian_cli.probe(vault)
    findings = merge_drift_findings(check_schema_drift(vault, schema), check_property_drift(cli_session, schema))

    run_dir = unique_run_directory(vault)
    # Written before any edit, as with attachments: the report is the record of
    # what the schema said before the fix.
    (run_dir / "drift_report.json").write_text(
        json.dumps({"counts": drift_counts(findings), "findings": findings}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "drift_report.md").write_text(
        render_drift_report(vault, schema_path, schema, findings, property_check=cli_session["available"]),
        encoding="utf-8",
    )

    requested = [part.strip() for part in (args.fix_schema or "").split(",") if part.strip()]
    changes = []
    remaining = findings
    if requested:
        changes, remaining = apply_schema_fixes(vault, schema_path, findings, requested, run_dir)

    warnings = []
    for finding in remaining:
        if finding["severity"] in {"high", "medium"}:
            warnings.append(f"schema drift [{finding['severity']}] {finding['id']} {finding['path']}: {finding['detail']}")
    return structured(
        "ok",
        artifacts=[str(run_dir / "drift_report.md"), str(run_dir / "drift_report.json")],
        warnings=warnings,
        data={
            "vault": str(vault),
            "schema": str(schema_path),
            "runDirectory": str(run_dir),
            "dryRun": not requested,
            "counts": drift_counts(remaining),
            "findings": remaining,
            "applied": changes,
        },
    )


def guide(args):
    """Compile the vault's own orientation skill from its schema and its folders.

    The output is a skill the agent discovers on its own, so this mode is the one
    place in the tool that writes outside the vault's notes and outside its own
    state directory. It stays a dry run by default for the same reason every
    other mode does: the file it replaces may have been read by a session five
    minutes ago, and a diff is cheap to look at.
    """
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path)
    rendered = vault_guide.build(vault, schema, schema_path, schema_hash)
    triggers = vault_guide.render_triggers(vault.name, schema)
    target = vault_guide.guide_path(vault)

    data = {
        "vault": str(vault),
        "schema": str(schema_path),
        "guide": str(target),
        "skill": vault_guide.SKILL_NAME,
        "sections": vault_guide.section_summary(rendered),
    }
    if args.print_guide:
        data["body"] = rendered

    if args.check:
        current, stale = vault_guide.check(vault, rendered)
        data["dryRun"] = True
        data["current"] = current
        return structured(
            "ok" if current else "error",
            warnings=stale,
            errors=[] if current else [error_entry("stale_guide", "; ".join(stale))],
            data=data,
        )

    installed = vault_guide.read_text(target)
    changes = vault_guide.describe_changes(rendered, installed)
    data["changes"] = changes
    data["changed"] = bool(changes)
    data["dryRun"] = not args.apply

    if args.apply:
        vault_guide.write_guide(vault, rendered, triggers)
        return structured("ok", artifacts=[str(target)], data=data)

    candidate = vault_guide.write_candidate(vault, rendered, triggers)
    data["candidate"] = str(candidate)
    warnings = [] if not changes else ["guide is out of date; rerun with --apply to install it"]
    return structured("ok", artifacts=[str(candidate)], warnings=warnings, data=data)


def parse_renumber_moves(raw):
    """``craft=4,writing=5`` -> ``{"craft": 4, "writing": 5}``."""
    moves = {}
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise UserError(f"--set takes domain=number pairs: {part}")
        value, number = part.split("=", 1)
        value = value.strip()
        try:
            moves[value] = int(number.strip())
        except ValueError:
            raise UserError(f"--set number must be an integer: {part}") from None
    if not moves:
        raise UserError("--set needs at least one domain=number pair")
    return moves


def rename_folders(vault, moves):
    """Apply the rename plan, undoing everything already done if any step fails.

    Renames are the reversible half of this operation and the schema note is the
    small atomic half, so the folders move first and the note is written only
    once every one of them has landed. A failure partway leaves the vault exactly
    as it was rather than half-renumbered against a schema that still says the
    old thing.
    """
    done = []
    try:
        for old, new in moves:
            source = vault / old
            target = vault / new
            if not source.is_dir():
                raise UserError(f"cannot rename {old}: it is not a directory")
            if target.exists():
                raise UserError(f"cannot rename {old} to {new}: the destination already exists")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
            done.append((old, new))
    except BaseException:
        for old, new in reversed(done):
            try:
                os.rename(vault / new, vault / old)
            except OSError:
                raise UserError(
                    f"renumbering failed and the rollback could not restore {new} to {old}; "
                    f"the vault is partly renumbered and the schema note was not written. "
                    f"Renames applied: {done}"
                ) from None
        raise
    return done


def write_renumbered_schema(vault, schema_path, schema, mapping, run_dir):
    """Rewrite only the Number cell of each moved Domains row.

    The same rails as ``--fix-schema``: backup first, surgical single-cell edits
    that preserve each row's prose and spacing, and a temp file that has to
    re-parse and re-check clean before it replaces anything.
    """
    original = schema_path.read_text(encoding="utf-8")
    backup = run_dir / "backup" / relative_path(vault, schema_path)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(schema_path, backup)

    revised = original
    changes = []
    for value in sorted(mapping, key=lambda key: schema["domains"][key]["number"]):
        revised, before_line, after_line = replace_schema_row_number(
            revised,
            {
                "table": "Domains",
                "match_column": "Value",
                "value": value,
                "field": "Number",
                "from": schema["domains"][value]["number"],
                "to": mapping[value],
            },
        )
        changes.append({"domain": value, "before": before_line, "after": after_line})

    handle, temp_name = tempfile.mkstemp(prefix=f".{schema_path.name}.", suffix=".tmp", dir=str(schema_path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(revised)
            stream.flush()
            os.fsync(stream.fileno())
        candidate = Path(temp_name).read_text(encoding="utf-8-sig")
        try:
            new_schema = parse_schema_note(candidate)
            validate_derived_paths(new_schema)
        except UserError as error:
            raise UserError(
                f"renumber rejected: the edited schema does not parse ({error}); nothing was written"
            ) from error
        introduced = [
            finding for finding in check_schema_drift(vault, new_schema) if finding["severity"] == "high"
        ]
        if introduced:
            listed = "; ".join(f"{finding['id']} {finding['path']}" for finding in introduced)
            raise UserError(
                f"renumber rejected: the folders and the edited schema still disagree ({listed}); "
                "nothing was written"
            )
        os.replace(temp_name, schema_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return changes, new_schema


def renumber(args):
    """Shift domain numbers, and every folder whose name is derived from them.

    A cascade is a chain of swaps, and ``--fix-schema`` refuses those by design:
    ``schema_side_fix`` returns None when the wanted number belongs to a sibling
    row, so drift correctly says the folders are the only side that can move and
    then declines to move them. This is the mode that moves them.

    Nothing about a note changes. Frontmatter names a domain by value, never by
    number, and Obsidian resolves a wikilink by basename, so no note is refiled,
    reclassified, or relinked -- which is what makes a renumbering cheap enough
    to be worth doing.
    """
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    schema, _ = compiled_schema_for(vault, schema_path)

    if args.insert is not None:
        mapping = renumber_mapping(schema, insert=args.insert)
    else:
        mapping = renumber_mapping(schema, moves=parse_renumber_moves(args.set_numbers))

    warnings = []
    if not mapping:
        return structured(
            "ok",
            warnings=[f"number {args.insert} is already free; nothing has to move"] if args.insert else [],
            data={"vault": str(vault), "dryRun": True, "mapping": {}, "moves": [], "references": {}},
        )

    moves = renumber_folder_moves(schema, mapping)
    plan, absent = prune_renumber_moves(moves, existing_folders(vault))
    if absent:
        warnings.append(
            f"{len(absent)} declared routes have no folder on disk and were skipped: {', '.join(absent)}"
        )

    references = find_path_references(vault, sorted({old for old, _ in moves}))
    if references:
        warnings.append(
            f"{sum(len(hits) for hits in references.values())} files mention a folder path that is about to move; "
            "wikilinks follow a rename but explicit paths, base filters, and Obsidian bookmarks do not"
        )

    run_dir = unique_run_directory(vault)
    report = {
        "mapping": mapping,
        "moves": [{"from": old, "to": new} for old, new in plan],
        "skipped": absent,
        "references": references,
    }
    (run_dir / "renumber_plan.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    applied = []
    schema_changes = []
    if args.apply:
        applied = rename_folders(vault, plan)
        run_state.append_jsonl_fsync(run_dir / "apply-log.jsonl", {"kind": "renumber", "moves": applied})
        try:
            schema_changes, _new_schema = write_renumbered_schema(vault, schema_path, schema, mapping, run_dir)
        except BaseException:
            for old, new in reversed(applied):
                os.rename(vault / new, vault / old)
            raise
        refresh_vault_index(vault, schema_path)

    return structured(
        "ok",
        artifacts=[str(run_dir / "renumber_plan.json")],
        warnings=warnings,
        data={
            "vault": str(vault),
            "schema": str(schema_path),
            "runDirectory": str(run_dir),
            "dryRun": not args.apply,
            "mapping": mapping,
            "moves": report["moves"],
            "skipped": absent,
            "references": references,
            "schemaChanges": schema_changes,
            "applied": len(applied),
        },
    )


def status(args):
    run_dir = Path(args.run).expanduser().resolve()
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    scan_path = run_dir / "scan.json"
    total = None
    if scan_path.is_file():
        try:
            total = len(json.loads(scan_path.read_text(encoding="utf-8"))["items"])
        except (OSError, json.JSONDecodeError, KeyError):
            total = None
    journal, _ = run_state.read_jsonl_recover_tail(run_dir / "classified.jsonl", repair=False)
    model_durations = [row.get("seconds", 0.0) for row in journal if row.get("classification_source") == "model"]
    dedupe_losers = 0
    dedupe_path = run_dir / "dedupe.json"
    if dedupe_path.is_file():
        try:
            dedupe = json.loads(dedupe_path.read_text(encoding="utf-8"))
            dedupe_losers = sum(len(group["losers"]) for group in dedupe.get("groups", []))
        except (OSError, json.JSONDecodeError):
            pass
    applied, _ = run_state.read_jsonl_recover_tail(run_dir / "apply-log.jsonl", repair=False)
    remaining = None
    eta = None
    if total is not None:
        remaining = max(total - dedupe_losers - len(journal), 0)
        if model_durations and remaining:
            eta = format_duration(sum(model_durations) / len(model_durations) * remaining)
    return structured(
        "ok",
        data={
            "run_directory": str(run_dir),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "mode": state.get("command"),
            "selected": total,
            "classified": len(journal),
            "duplicate_losers": dedupe_losers,
            "remaining": remaining,
            "eta": eta,
            "applied_operations": sum(1 for entry in applied if entry.get("status") == "ok"),
            "next_action": state.get("nextAction"),
        },
    )


def format_check(vault, warnings):
    """Whether the vault's callout registry and its implementations still agree.

    The registry is declared by a note, styled by a stylesheet, emitted by
    `vault_reflection`, and used by the templates, and none of the four can see
    the others. A callout added to one and forgotten in the rest renders as stock
    blue with a pencil icon, which reads as a design choice rather than a defect.
    Nothing ran this check before, so it could only ever be run by hand.

    A vault with no format note, or no stylesheet, is not a broken vault -- it is
    a vault that has not adopted the registry, which is the state every vault
    started in. That reports `configured: false` and never fails. Only a vault
    that declares a registry and then contradicts it is an error.
    """
    try:
        format_path = vault_format.resolve_format_path(vault)
    except UserError as error:
        return {"ok": False, "configured": True, "detail": str(error)}
    if format_path is None:
        return {
            "ok": True,
            "configured": False,
            "detail": f"no note-format note; default is {vault_format.DEFAULT_FORMAT}",
        }
    try:
        findings = vault_format.load_and_check(vault, raw_format=str(format_path))
    except UserError as error:
        # The stylesheet is the usual absentee here, and a vault can legitimately
        # declare the registry before writing one.
        return {"ok": True, "configured": True, "path": str(format_path), "detail": str(error)}
    errors = [message for severity, message in findings if severity == "error"]
    notices = [message for severity, message in findings if severity != "error"]
    for message in errors:
        warnings.append(f"note format [error] {message}")
    for message in notices:
        warnings.append(f"note format [warning] {message}")
    return {
        "ok": not errors,
        "configured": True,
        "path": str(format_path),
        "callouts": len(vault_format.parse_format_note(format_path.read_text(encoding="utf-8"))),
        "errors": errors,
        "notices": notices,
    }


def voice_check(vault, warnings):
    """Whether the vault's voice note resolves and parses.

    A vault with no voice note is not a broken vault -- it is a vault that writes
    in the default register, which is where every vault starts. That reports
    `configured: false` and never fails. A note that exists and will not parse is
    an error: every generating skill would silently fall back to the default
    voice the owner believed they had replaced.
    """
    try:
        voice_path = vault_voice.resolve_voice_path(vault)
    except UserError as error:
        return {"ok": False, "configured": True, "detail": str(error)}
    if voice_path is None:
        return {"ok": True, "configured": False, "detail": f"no voice note; default is {vault_voice.DEFAULT_VOICE}"}
    try:
        voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path)
    except (UserError, OSError) as error:
        warnings.append(f"voice note [error] {error}")
        return {"ok": False, "configured": True, "path": str(voice_path), "detail": str(error)}
    # A scope nobody recognizes is a rule that reaches nothing, which reads from
    # the note as though it were in force.
    for scope in voice["unknown_scopes"]:
        warnings.append(f"voice note [warning] unknown scope '{scope}'; its rules apply to nothing")
    return {
        "ok": True,
        "configured": True,
        "path": str(voice_path),
        "voice_hash": voice_hash,
        "rules": {key: len(voice[key]) for key in ("global", "vocabulary", "formatting", "never")},
        "types": len(voice["per_type"]),
        "scopes": voice["recognized_scopes"],
        "unknownScopes": voice["unknown_scopes"],
    }


def lexicon_check(vault, warnings):
    """Whether the vault's shared term and speaker dictionary resolves and parses.

    Same shape as the voice check, and for the same reason: a vault that has not
    written one is not broken, and one that has written an unparseable one is.
    """
    try:
        lexicon_path = vault_lexicon.resolve_lexicon_path(vault)
    except UserError as error:
        return {"ok": False, "configured": True, "detail": str(error)}
    if lexicon_path is None:
        return {
            "ok": True,
            "configured": False,
            "detail": f"no lexicon note; default is {vault_lexicon.DEFAULT_LEXICON}",
        }
    try:
        lexicon = vault_lexicon.parse_lexicon_note(lexicon_path.read_text(encoding="utf-8"))
    except (UserError, OSError) as error:
        warnings.append(f"lexicon note [error] {error}")
        return {"ok": False, "configured": True, "path": str(lexicon_path), "detail": str(error)}
    return {
        "ok": True,
        "configured": True,
        "path": str(lexicon_path),
        "terms": len(lexicon["terms"]),
        "speakers": len(lexicon["speakers"]),
    }


def guide_check(vault, schema, schema_path, schema_hash, warnings):
    """Whether the vault's generated orientation skill is installed and current.

    Never fatal. A vault with no guide is a vault that has not run `guide` yet,
    and a stale one still describes the vault it was generated from -- it is
    wrong about the newest folders, not about the vault. Both are worth saying
    out loud, though: the guide is what a session reads instead of exploring by
    hand, so a vault that quietly has none pays for the same layout twice.
    """
    try:
        rendered = vault_guide.build(vault, schema, schema_path, schema_hash)
        current, stale = vault_guide.check(vault, rendered)
    except (UserError, OSError) as error:
        warnings.append(f"vault guide [warning] {error}")
        return {"ok": True, "installed": False, "current": False, "detail": str(error)}
    installed = vault_guide.guide_path(vault)
    for reason in stale:
        warnings.append(f"vault guide [warning] {reason}; rerun `guide --apply`")
    return {
        "ok": True,
        "installed": installed.is_file(),
        "current": current,
        "path": str(installed),
        "stale": stale,
    }


def skill_state_check(vault):
    """Which vault skills have actually been run here, by the state they leave.

    Informational only, and never part of `ok`: a skill that has never run in
    this vault is a skill that has not been needed. It answers the question a
    second vault raises -- whether it is as worked-in as the first -- which
    nothing else reports.
    """
    present = sorted(
        entry.name for entry in vault.iterdir() if entry.is_dir() and entry.name.startswith(".vault-")
    )
    return {"ok": True, "state": present}


def doctor(args):
    vault = Path(args.vault).expanduser().resolve()
    checks = {}
    warnings = []
    ok = True
    if vault.is_dir() and os.access(vault, os.W_OK):
        checks["vault"] = {"ok": True, "path": str(vault)}
    else:
        checks["vault"] = {"ok": False, "path": str(vault), "detail": "vault root missing or not writable"}
        ok = False
    schema_check = {"ok": False}
    if checks["vault"]["ok"]:
        try:
            schema_path = resolve_schema_path(vault, args.schema)
            schema, schema_hash = compiled_schema_for(vault, schema_path)
            schema_check = {
                "ok": True,
                "path": str(schema_path),
                "schema_hash": schema_hash,
                "domains": len(schema["domains"]),
                "subdomains": sum(len(values) for values in schema["subdomains"].values()),
                "projects": len(schema["projects"]),
                "types": len(schema["types"]),
            }
        except UserError as error:
            schema_check = {"ok": False, "detail": str(error)}
    checks["schema"] = schema_check
    ok = ok and schema_check["ok"]

    # Never fatal: classification runs without it if the register is broken.
    profile_path, profile, profile_hash = load_profile(args, vault)
    if profile_path is None:
        checks["profile"] = {
            "ok": True,
            "configured": False,
            "detail": (
                "disabled with --no-profile"
                if getattr(args, "no_profile", False)
                else f"no personal context note; default is {vault_profile.DEFAULT_PROFILE}"
            ),
        }
    else:
        checks["profile"] = {
            "ok": True,
            "configured": profile is not None,
            "path": str(profile_path),
            "profile_hash": profile_hash,
            "compiler_version": vault_profile.COMPILED_PROFILE_VERSION,
            "cards": vault_profile.profile_digest(profile),
            "reaches_classification": len(
                vault_profile.select_cards(profile, "", classification_site(), tier=vault_profile.TIER_ALWAYS)
            ),
        }
    warnings.extend(getattr(args, "profile_warnings", []) or [])
    checks["format"] = format_check(vault, warnings)
    ok = ok and checks["format"]["ok"]
    checks["voice"] = voice_check(vault, warnings)
    ok = ok and checks["voice"]["ok"]
    checks["lexicon"] = lexicon_check(vault, warnings)
    ok = ok and checks["lexicon"]["ok"]
    # The guide is compiled from the schema, so it can only be checked once the
    # schema has parsed; a vault whose schema is broken has a bigger problem.
    if schema_check["ok"]:
        checks["guide"] = guide_check(vault, schema, schema_path, schema_hash, warnings)
    else:
        checks["guide"] = {"ok": True, "installed": False, "current": False, "detail": "schema did not parse"}
    if checks["vault"]["ok"]:
        checks["skills"] = skill_state_check(vault)

    cli_session = obsidian_cli.probe(vault)
    if schema_check["ok"]:
        findings = merge_drift_findings(
            check_schema_drift(vault, schema), check_property_drift(cli_session, schema)
        )
        counts = drift_counts(findings)
        checks["drift"] = {"ok": counts["high"] == 0, "counts": counts, "findings": findings}
        for finding in findings:
            if finding["severity"] in {"high", "medium"}:
                warnings.append(
                    f"schema drift [{finding['severity']}] {finding['id']} {finding['path']}: {finding['detail']}"
                )
        ok = ok and checks["drift"]["ok"]
    else:
        checks["drift"] = {"ok": False, "detail": "schema did not parse"}
        ok = False
    # Classification is one call per note, so a backend that reasons before
    # answering costs hundreds of hidden tokens per note. Report that here
    # rather than letting a whole-vault run discover it slowly.
    chat_probe = forge_llm.service_doctor(
        chat_service(args), expect_non_thinking=not args.think_prefill, timeout=min(args.request_timeout, 60)
    )
    chat_check = {
        "ok": chat_probe["reachable"],
        "url": chat_probe["url"],
        "model": chat_probe["model"],
        "detail": chat_probe.get("detail"),
    }
    for key in ("thinking", "hiddenTokens", "modelMismatch", "servedModels"):
        if key in chat_probe:
            chat_check[key] = chat_probe[key]
    checks["chat"] = chat_check
    ok = ok and chat_check["ok"]
    if chat_probe.get("warning"):
        warnings.append(chat_probe["warning"])
    if args.no_embeddings:
        checks["embeddings"] = {"ok": True, "skipped": True}
    else:
        embeddings_probe = forge_embeddings.embeddings_doctor(url=args.embeddings_url, model=args.embeddings_model)
        checks["embeddings"] = {
            "ok": embeddings_probe["reachable"],
            "url": embeddings_probe["url"],
            "model": embeddings_probe["model"],
            "detail": embeddings_probe["detail"],
        }
        ok = ok and embeddings_probe["reachable"]
    # An optional accelerator, so it never touches `ok`: a vault with no Obsidian
    # running is a normal vault, not a broken one. It only reports, and warns when
    # the CLI is one setting away from being usable.
    cli_report = obsidian_cli.doctor(vault, session=cli_session)
    if cli_report["available"] and schema_check["ok"]:
        cli_report["frontmatter"] = frontmatter_oracle(cli_session, vault, schema_path)
        for note in cli_report["frontmatter"]["disagreements"]:
            warnings.append(
                "frontmatter parse disagreement in {0}: Obsidian sees keys we miss ({1}); "
                "we see keys it does not ({2}); shape differs on ({3})".format(
                    note["path"],
                    ", ".join(note["missing"]) or "none",
                    ", ".join(note["extra"]) or "none",
                    ", ".join(note["shape"]) or "none",
                )
            )
    checks["obsidianCli"] = cli_report
    warnings.extend(cli_report.pop("warnings", []))
    return structured("ok" if ok else "error", warnings=warnings, data={"checks": checks})


# How many notes the parser oracle samples. Enough to catch a systematic miss,
# few enough that doctor stays a fast command.
FRONTMATTER_ORACLE_SAMPLE = 25


def frontmatter_oracle(session, vault, schema_path):
    """Compare our frontmatter parser against Obsidian's on a sample of notes."""
    try:
        notes = selected_notes(vault, schema_path, "vault", FRONTMATTER_ORACLE_SAMPLE)
    except UserError:
        return {"checked": 0, "disagreements": []}

    def parse(path):
        try:
            frontmatter = split_frontmatter(path.read_bytes())
        except OSError:
            return None
        if frontmatter["malformed"] or not frontmatter["had_frontmatter"]:
            return None
        return parse_frontmatter(frontmatter["frontmatter_text"])

    relatives = [str(note.relative_to(vault)) for note in notes]
    return obsidian_cli.compare_frontmatter(session, vault, relatives, parse)


class TrackingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Classify, dedupe, and organize Obsidian vault notes.")
    parser.add_argument(
        "mode",
        choices=["inbox", "vault", "attachments", "dates", "drift", "renumber", "status", "doctor", "guide"],
    )
    parser.add_argument("--vault")
    parser.add_argument("--schema", action=TrackingAction)
    parser.add_argument("--apply", action="store_true")
    # Deliberately not a TrackingAction: an override granted for one apply must
    # not be adopted into resumed run state and silently persist.
    parser.add_argument(
        "--allow-schema-drift",
        action="store_true",
        help="apply even though the schema and the folders on disk disagree",
    )
    parser.add_argument(
        "--link-rewrite",
        choices=["auto", "off", "require"],
        default="auto",
        help=(
            "how moves handle inbound links: auto uses the Obsidian CLI when it can and falls back to a "
            "plain rename otherwise, off always renames, require fails when link-safe moves are unavailable"
        ),
    )
    parser.add_argument("--fix-schema", help="drift mode: comma-separated finding ids to correct in the schema note")
    parser.add_argument(
        "--insert",
        type=int,
        help=(
            "renumber mode: free this domain number by shifting the contiguous block that starts at it "
            "up by one. The cascade stops at the first free number, so distant domains never move. "
            "The new row itself is yours to add."
        ),
    )
    parser.add_argument(
        "--set",
        dest="set_numbers",
        help="renumber mode: explicit comma-separated domain=number moves, e.g. craft=4,writing=5",
    )
    parser.add_argument(
        "--archive",
        action="append",
        help="dates mode: a folder of older note copies to mine for dates, repeatable; read-only",
    )
    parser.add_argument(
        "--self-only",
        action="store_true",
        help="dates mode: derive from each note's own name, path, and text without an archive",
    )
    parser.add_argument("--date-property", default="date", help="dates mode: the scalar property to fill (default: date)")
    parser.add_argument(
        "--include-file-times",
        action="store_true",
        help="dates mode: offer Finder creation and modification times as weak evidence, never auto-applied",
    )
    parser.add_argument(
        "--trust-birthtime",
        action="store_true",
        help="dates mode: treat Finder's creation date as explicit evidence; read the calibration first",
    )
    parser.add_argument(
        "--near-match",
        action="store_true",
        help="dates mode: also pair notes to archive copies by meaning, for review only",
    )
    parser.add_argument("--ids", help="dates mode: comma-separated proposal ids to write, instead of the confident ones")
    parser.add_argument(
        "--year-only",
        action="store_true",
        help=(
            "dates mode: also write proposals whose year is known but whose day is not, "
            "as YYYY-01-01; the day is a placeholder, so read the report first"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="guide mode: report whether the installed guide still matches the vault, and write nothing",
    )
    parser.add_argument(
        "--print",
        dest="print_guide",
        action="store_true",
        help="guide mode: include the compiled guide in the JSON, instead of only its shape",
    )
    parser.add_argument("--run", help="existing run directory to resume")
    parser.add_argument("--limit", type=int, action=TrackingAction)
    parser.add_argument("--base-url", action=TrackingAction)
    parser.add_argument("--model", action=TrackingAction)
    parser.add_argument("--api-key")
    parser.add_argument("--request-timeout", type=float, default=120)
    parser.add_argument("--embeddings-url", action=TrackingAction)
    parser.add_argument("--embeddings-model", action=TrackingAction)
    parser.add_argument("--no-embeddings", action="store_true")
    parser.add_argument("--near-dupe-auto", type=float, action=TrackingAction)
    parser.add_argument("--near-dupe-review", type=float, action=TrackingAction)
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument("--no-verify", action="store_true", help="skip the thinking-model review of classifications")
    parser.add_argument("--think-url", help="thinking service used for verification (default: connectedServices.think)")
    parser.add_argument("--think-model")
    parser.add_argument("--think-prefill", action="store_true", help="prefill an empty think block (for thinking backends like :8008)")
    parser.add_argument("--force-reclassify", action="store_true")
    parser.add_argument(
        "--only-sources",
        action="store_true",
        help="vault mode: consider only notes their own frontmatter calls sources, leaving every other note alone (--limit still counts notes scanned, not sources kept)",
    )
    parser.add_argument(
        "--reuse-frontmatter",
        action="store_true",
        help="file notes whose existing frontmatter already validates without asking the model (schema migrations)",
    )
    parser.add_argument("--profile", action=TrackingAction, help="personal-context register note (default: the vault's, when present)")
    parser.add_argument("--no-profile", action="store_true", help="disable personal context for this run")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    for key in RESUMABLE_OPTION_FLAGS:
        if not hasattr(args, f"{key}_provided"):
            setattr(args, f"{key}_provided", False)
    if args.limit is not None and args.limit < 0:
        raise UserError("--limit must be non-negative")
    if args.mode == "status":
        if not args.run:
            raise UserError("status requires --run <run-directory>")
        return args
    if not args.vault:
        raise UserError(f"{args.mode} requires --vault")
    args.schema = args.schema or os.environ.get("VAULT_ORGANIZER_SCHEMA") or None
    if args.fix_schema and args.mode != "drift":
        raise UserError("--fix-schema belongs to drift mode")
    if (args.insert is not None or args.set_numbers) and args.mode != "renumber":
        raise UserError("--insert and --set belong to renumber mode")
    if args.mode == "renumber":
        if (args.insert is None) == (not args.set_numbers):
            raise UserError("renumber takes exactly one of --insert <number> or --set <domain=number,...>")
        if args.insert is not None and not 1 <= args.insert <= 99:
            raise UserError("--insert must be a domain number from 1 through 99")
    dates_flags = (
        args.archive,
        args.self_only,
        args.include_file_times,
        args.trust_birthtime,
        args.near_match,
        args.ids,
        args.year_only,
    )
    if args.mode != "dates" and any(dates_flags):
        raise UserError(
            "--archive, --self-only, --include-file-times, --trust-birthtime, --near-match, --ids, "
            "and --year-only belong to dates mode"
        )
    if (args.check or args.print_guide) and args.mode != "guide":
        raise UserError("--check and --print belong to guide mode")
    if args.check and args.apply:
        raise UserError("--check reports whether the guide is current; it never writes, so --apply is meaningless with it")
    if args.mode in {"attachments", "drift", "renumber", "guide"}:
        # Deterministic filesystem work: no classification, so no model or
        # embeddings service is resolved and these run with every endpoint down.
        return args
    if args.mode == "dates":
        # Also deterministic, and no model ever. --near-match is the one path
        # that needs a service, so it is the only one that resolves an endpoint.
        args.archive = args.archive or []
        if args.near_dupe_auto is None:
            args.near_dupe_auto = NEAR_DUPE_AUTO
        if args.near_match:
            if args.no_embeddings:
                raise UserError("--near-match needs the embeddings service; drop --no-embeddings")
            args.embeddings_url = forge_embeddings.endpoint_url(args.embeddings_url)
            args.embeddings_model = forge_embeddings.model_name(args.embeddings_model)
        return args
    # Skill-specific settings win, then the agent's configured chat service, then
    # the built-in non-thinking default.
    resolved = forge_llm.resolve_service(
        "chat",
        base_url=args.base_url or os.environ.get("VAULT_ORGANIZER_BASE_URL") or os.environ.get("OPENAI_BASE_URL"),
        model=args.model or os.environ.get("VAULT_ORGANIZER_MODEL") or os.environ.get("OPENAI_MODEL"),
    )
    args.base_url = resolved["url"]
    args.model = resolved["model"]
    args.api_key = args.api_key or os.environ.get("VAULT_ORGANIZER_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.embeddings_url = forge_embeddings.endpoint_url(args.embeddings_url)
    args.embeddings_model = forge_embeddings.model_name(args.embeddings_model)
    if args.near_dupe_auto is None:
        args.near_dupe_auto = NEAR_DUPE_AUTO
    if args.near_dupe_review is None:
        args.near_dupe_review = NEAR_DUPE_REVIEW
    if not 0 < args.near_dupe_review <= args.near_dupe_auto <= 1:
        raise UserError("--near-dupe-review must be within (0, --near-dupe-auto] and --near-dupe-auto at most 1")
    args.cache_prompt = not args.no_cache_prompt
    args.verify = not args.no_verify
    return args


def run(argv):
    args = parse_args(argv)
    if args.mode == "status":
        result = status(args)
    elif args.mode == "doctor":
        result = doctor(args)
    elif args.mode == "attachments":
        result = attachments(args)
    elif args.mode == "dates":
        result = dates(args)
    elif args.mode == "drift":
        result = drift(args)
    elif args.mode == "guide":
        result = guide(args)
    elif args.mode == "renumber":
        result = renumber(args)
    else:
        result = organize(args)
    print_json(result)
    return 0 if result["status"] == "ok" else 1


def main(argv=None):
    try:
        return run(sys.argv[1:] if argv is None else argv)
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))], data=None))
        return 1
    except Exception as error:
        print_json(structured("error", errors=[error_entry("internal_error", str(error))], data=None))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
