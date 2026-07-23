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
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_embeddings
import run_state
from vault_schema import (  # noqa: F401  (re-exported for callers and tests)
    COMPILED_SCHEMA_VERSION,
    DEFAULT_SCHEMA,
    INBOX_DIR,
    PROTECTED_DIRS,
    REQUIRED_SECTIONS,
    RESERVED_WINDOWS_NAMES,
    SCHEMA_BASENAME,
    UserError,
    compile_destination,
    compiled_schema_for,
    domain_folder,
    first_nonempty_line,
    has_control_character,
    heading_index,
    is_divider_row,
    iter_heading_lines,
    normalize_body_for_hash,
    normalize_project_value,
    note_title,
    optional_bullet_lines,
    pad2,
    parse_assignment,
    parse_bullet_registry,
    parse_legacy_output,
    parse_schema_note,
    path_is_inside,
    project_folder,
    project_name,
    property_shape,
    property_value_mode,
    relative_path,
    require_number,
    require_safe_label,
    resolve_schema_path,
    revised_note_text,
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
    valid_wikilink,
    validate_derived_paths,
    yaml_quote,
    yaml_scalar,
)


WORKFLOW = "vault-organizer"
DEFAULT_BASE_URL = "http://llms:8004/v1/chat/completions"
DEFAULT_MODEL = "code"
PROMPT_VERSION = "vault-organizer-v3"
MAX_BODY_CHARS = 30000
MAX_ADVISORY_FRONTMATTER_CHARS = 2000
EMBED_MAX_CHARS = 2000
MIN_NEAR_DUPE_CHARS = 100
NEAR_DUPE_AUTO = 0.97
NEAR_DUPE_REVIEW = 0.90
CONTAINMENT_MIN = 0.90
MAX_BLOCK_BUCKET = 50
MAX_SUGGESTIONS = 8
MAX_SUGGESTION_CHARS = 200
MAX_TRANSIENT_ATTEMPTS = 3
RUN_STATE_BATCH = 25
# The default backend (:8004) is a non-thinking configuration. Pointing at a
# thinking backend (e.g. :8008) instead needs --think-prefill: a closed empty
# think block prefilled as the assistant turn so the server skips reasoning.
# The response parser strips a stray leading think block regardless, so a
# thinking backend used without the flag still parses (just slowly).
THINK_PREFILL = "<think>\n\n</think>\n\n"
THINK_BLOCK_RE = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
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


def cache_key(title, body_hash, frontmatter_hash, schema_hash, model, base_url, think_prefill=True):
    payload = {
        "title": title,
        "body_hash": body_hash,
        "frontmatter_hash": frontmatter_hash,
        "schema_hash": schema_hash,
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


def compact_schema_for_prompt(schema):
    return {
        "properties": schema["properties"],
        "property_order": schema["property_order"],
        "types": schema["types"],
        "statuses": schema["statuses"],
        "domains": schema["domains"],
        "subdomains": schema["subdomains"],
        "projects": schema["projects"],
        "source_kinds": schema["source_kinds"],
        "capture_types": schema["capture_types"],
    }


def system_prompt(schema):
    shape = {
        "metadata": {key: None for key in schema["property_order"]},
        "needs_review": False,
        "review_reason": None,
        "suggestions": [],
    }
    sections = [
        SYSTEM_INSTRUCTIONS,
        "Schema:\n" + run_state.canonical_json(compact_schema_for_prompt(schema)),
    ]
    if schema.get("domain_rules"):
        sections.append("Domain decision rules:\n" + "\n".join(f"- {rule}" for rule in schema["domain_rules"]))
    if schema.get("project_rules"):
        sections.append("Project assignment rules:\n" + "\n".join(f"- {rule}" for rule in schema["project_rules"]))
    sections.append("Required response shape:\n" + run_state.canonical_json(shape))
    return "\n\n".join(sections)


def build_messages(schema, title, current_path, frontmatter_text, body_excerpt, repair=None, think_prefill=True):
    payload = {
        "title": title,
        "current_relative_path": current_path,
        "untrusted_existing_frontmatter": frontmatter_text[:MAX_ADVISORY_FRONTMATTER_CHARS],
        "body": body_excerpt,
    }
    if repair:
        payload["repair"] = repair
    messages = [
        {"role": "system", "content": system_prompt(schema)},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    if think_prefill:
        messages.append({"role": "assistant", "content": THINK_PREFILL})
    return messages


def normalize_base_url(value):
    url = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return url


def request_json(base_url, model, api_key, timeout, messages, cache_prompt=True):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    if cache_prompt:
        payload["cache_prompt"] = True
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(base_url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if api_key:
        request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise UserError(f"model endpoint returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise UserError(f"model endpoint request failed: {error.reason}") from error
    parsed = json.loads(response_body)
    content = parsed.get("choices", [{}])[0].get("message", {}).get("content")
    if not isinstance(content, str):
        raise UserError("model response did not contain choices[0].message.content")
    return json.loads(extract_json_content(content))


def extract_json_content(content):
    text = THINK_BLOCK_RE.sub("", content).strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text


def request_json_with_retry(args, messages):
    last_error = None
    for attempt in range(1, MAX_TRANSIENT_ATTEMPTS + 1):
        try:
            return request_json(args.base_url, args.model, args.api_key, args.request_timeout, messages, cache_prompt=args.cache_prompt)
        except UserError as error:
            last_error = error
            if attempt < MAX_TRANSIENT_ATTEMPTS and run_state.is_transient_failure(error):
                time.sleep(min(2.0 * attempt, 10.0))
                continue
            raise
    raise last_error


def normalize_metadata(metadata, schema):
    normalized = {}
    warnings = []
    for key in schema["property_order"]:
        value = metadata.get(key)
        if value is None or value == "" or value == []:
            continue
        if key == "project":
            value = normalize_project_value(str(value))
        if isinstance(value, str):
            legacy = schema["legacy"].get(f"{key}:{value}")
            if legacy:
                for target_key, target_value in legacy.items():
                    normalized[target_key] = target_value
                warnings.append(f"normalized legacy {key}: {value}")
                continue
        normalized[key] = value
    if normalized.get("project"):
        project = schema["projects"].get(normalized["project"])
        if project:
            if normalized.get("domain") != project["domain"]:
                warnings.append(f"project {normalized['project']} overrode domain {normalized.get('domain')} -> {project['domain']}")
            normalized["domain"] = project["domain"]
            if project.get("subdomain"):
                if normalized.get("subdomain") != project["subdomain"]:
                    warnings.append(f"project {normalized['project']} overrode subdomain {normalized.get('subdomain')} -> {project['subdomain']}")
                normalized["subdomain"] = project["subdomain"]
            else:
                normalized.pop("subdomain", None)
    return normalized, warnings


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


def validate_classification(response, schema):
    errors = []
    warnings = []
    if not isinstance(response, dict):
        return None, [], ["response is not a JSON object"]
    required = {"metadata", "needs_review", "review_reason"}
    allowed = required | {"suggestions"}
    actual = set(response)
    if not required.issubset(actual) or not actual.issubset(allowed):
        errors.append(f"top-level keys must be {sorted(required)} plus optional suggestions")
    metadata = response.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata must be an object")
        return None, warnings, errors
    extra_keys = sorted(set(metadata) - set(schema["property_order"]))
    if extra_keys:
        errors.append(f"metadata contains unapproved keys: {', '.join(extra_keys)}")
    normalized, normalize_warnings = normalize_metadata(metadata, schema)
    warnings.extend(normalize_warnings)
    for key in ("type", "status", "domain"):
        if not normalized.get(key):
            errors.append(f"missing required metadata: {key}")
    for key, value in normalized.items():
        prop = schema["properties"].get(key)
        if not prop:
            continue
        if prop["shape"] == "list":
            if not isinstance(value, list):
                errors.append(f"{key} must be a list")
                continue
            seen = set()
            clean = []
            for item in value:
                if not isinstance(item, str) or has_control_character(item):
                    errors.append(f"{key} contains an invalid item")
                    continue
                if prop["value_mode"] == "wikilink" and not valid_wikilink(item):
                    errors.append(f"{key} item must be a wikilink: {item}")
                if item in seen:
                    errors.append(f"{key} contains duplicate item: {item}")
                seen.add(item)
                clean.append(item)
            normalized[key] = clean
        else:
            if not isinstance(value, str) or has_control_character(value):
                errors.append(f"{key} must be a scalar string")
                continue
            if prop["value_mode"] in {"wikilink", "registered_wikilink"} and not valid_wikilink(value):
                errors.append(f"{key} must be a wikilink: {value}")
    if normalized.get("type") and normalized["type"] not in schema["types"]:
        errors.append(f"invalid type: {normalized['type']}")
    if normalized.get("status") and normalized["status"] not in schema["statuses"]:
        errors.append(f"invalid status: {normalized['status']}")
    if normalized.get("domain") and normalized["domain"] not in schema["domains"]:
        errors.append(f"invalid domain: {normalized['domain']}")
    if normalized.get("subdomain"):
        domain = normalized.get("domain")
        if not domain or normalized["subdomain"] not in schema["subdomains"].get(domain, {}):
            errors.append(f"invalid subdomain for domain {domain}: {normalized['subdomain']}")
    if normalized.get("project") and normalized["project"] not in schema["projects"]:
        errors.append(f"invalid project: {normalized['project']}")
    if normalized.get("source_kind"):
        if normalized["source_kind"] not in schema["source_kinds"]:
            errors.append(f"invalid source_kind: {normalized['source_kind']}")
        if normalized.get("type") != "source":
            errors.append("source_kind is forbidden unless type is source")
    elif normalized.get("type") == "source":
        errors.append("source_kind is required when type is source")
    if normalized.get("capture_type") and normalized["capture_type"] not in schema["capture_types"]:
        errors.append(f"invalid capture_type: {normalized['capture_type']}")
    needs_review = response.get("needs_review")
    if not isinstance(needs_review, bool):
        errors.append("needs_review must be a boolean")
        needs_review = True
    review_reason = response.get("review_reason")
    if review_reason is not None and not isinstance(review_reason, str):
        errors.append("review_reason must be null or string")
    suggestions = clean_suggestions(response.get("suggestions"), warnings)
    return {
        "metadata": normalized,
        "needs_review": needs_review,
        "review_reason": review_reason,
        "suggestions": suggestions,
    }, warnings, errors


def scan_vault(vault, schema_path, mode, limit):
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
        try:
            data = path.read_bytes()
            item["sha256"] = sha256_bytes(data)
            frontmatter = split_frontmatter(data)
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


def classify_note(args, schema, title, relative_source, frontmatter_text, body, schema_hash, cache, cache_path):
    body_hash = sha256_text(normalize_body_for_hash(body))
    frontmatter_hash = sha256_text(frontmatter_text or "")
    key = cache_key(title, body_hash, frontmatter_hash, schema_hash, args.model, args.base_url, args.think_prefill)
    if not args.force_reclassify and key in cache:
        cached = cache[key]
        validated, warnings, errors = validate_classification(cached["response"], schema)
        if not errors:
            return validated, warnings, "cache", key
    excerpt, excerpted = excerpt_body(body)
    response = request_json_with_retry(
        args, build_messages(schema, title, relative_source, frontmatter_text, excerpt, think_prefill=args.think_prefill)
    )
    validated, warnings, errors = validate_classification(response, schema)
    if errors:
        repair = {"original_response": response, "validation_errors": errors}
        repaired = request_json_with_retry(
            args,
            build_messages(schema, title, relative_source, frontmatter_text, excerpt, repair=repair, think_prefill=args.think_prefill),
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
                    args, schema, title, rel, frontmatter["frontmatter_text"], body, schema_hash, cache, cache_path
                )
                record = base_record(item)
                record["source_hash"] = source_hash
                record["classification_source"] = classification_source
                record["metadata"] = classification["metadata"]
                record["needs_review"] = classification.get("needs_review", False)
                record["review_reason"] = classification.get("review_reason")
                record["suggestions"] = classification.get("suggestions", [])
                record["excerpted"] = bool(classification.get("excerpted"))
                record["warnings"] = record_warnings
                if record["needs_review"]:
                    record["status"] = "review"
                else:
                    destination_dir = compile_destination(schema, record["metadata"])
                    record["destination"] = (destination_dir / path.name).as_posix()
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
            if done["op"] == "rewrite" and record["status"] == "ok" and not record["needs_review"] and done["destination"] == record["destination"]:
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


def scan_base_references(vault, records):
    moved_sources = [
        record["source"]
        for record in records
        if record["action"] in {"move_only", "quarantine"}
        or (record["action"] == "rewrite" and record.get("move_required"))
    ]
    if not moved_sources:
        return []
    references = []
    for directory, dirnames, filenames in os.walk(vault, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [
            name for name in sorted(dirnames)
            if name not in PROTECTED_DIRS and not name.startswith(".") and not (dirpath / name).is_symlink()
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


def write_plan(run_dir, records, counts, dedupe, base_references, mode, dry_run, vault, schema_hash, warnings):
    plan_path = run_dir / "plan.json"
    report_path = run_dir / "report.md"
    data = {
        "mode": mode,
        "dry_run": dry_run,
        "vault": str(vault),
        "schema_hash": schema_hash,
        "run_directory": str(run_dir),
        "counts": counts,
        "dedupe": dedupe,
        "base_references": base_references,
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
        "## Destination Counts",
        "",
    ]
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
    suggestions = collect_suggestions(records)
    report.extend(["", "## Schema Suggestions", "", "Suggestions are advisory only; nothing is applied to the schema.", ""])
    append_report_listing(
        report,
        suggestions,
        lambda entry: f"- {entry['suggestion']} (from {', '.join('`' + source + '`' for source in entry['sources'])})",
    )
    report.extend(["", "## Base File References", ""])
    append_report_listing(
        report,
        base_references,
        lambda entry: f"- `{entry['base']}` references moved notes: {', '.join('`' + hit + '`' for hit in entry['references'][:5])}",
    )
    report.extend([
        "",
        "## Link Safety",
        "",
        "Basename-style Obsidian wikilinks are generally independent of folders. Relative Markdown links containing explicit paths may be affected by moves. This version records moves but does not repair path-based links.",
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


def apply_move_operation(vault, run_dir, record):
    source = vault / record["source"]
    destination = vault / record["destination"]
    data = source.read_bytes()
    if sha256_bytes(data) != record["source_hash"]:
        raise UserError("source changed since planning")
    if destination.exists():
        raise UserError("destination collision")
    backup = run_dir / "backup" / record["source"]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, destination)
    return backup


def apply_rewrite_operation(vault, run_dir, record, schema):
    source = vault / record["source"]
    destination = vault / record["destination"]
    data = source.read_bytes()
    if sha256_bytes(data) != record["source_hash"]:
        raise UserError("source changed since planning")
    frontmatter = split_frontmatter(data)
    if frontmatter["malformed"]:
        raise UserError("frontmatter became malformed since planning")
    revised = revised_note_text(record["metadata"], schema, frontmatter["body"])
    backup = run_dir / "backup" / record["source"]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.resolve() != source.resolve():
        raise UserError("destination collision")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(revised)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    if destination.resolve() != source.resolve() and source.exists():
        source.unlink()
    return backup


def apply_records(args, vault, run_dir, records, counts, schema):
    log_path = run_dir / "apply-log.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
    done = {(entry.get("op"), entry.get("source")) for entry in prior if entry.get("status") == "ok"}
    order = {"quarantine": 0, "move_only": 1, "rewrite": 2}
    actionable = sorted(
        (record for record in records if record["action"] in order and record["status"] != "failed"),
        key=lambda record: (order[record["action"]], record["source"]),
    )
    for record in records:
        if record["action"] == "none" and (record["needs_review"] or record["status"] == "failed"):
            counts["skipped"] += 1
    for record in actionable:
        op = record["action"]
        if (op, record["source"]) in done:
            counts["applied"] += 1
            continue
        try:
            if op == "rewrite":
                backup = apply_rewrite_operation(vault, run_dir, record, schema)
            else:
                backup = apply_move_operation(vault, run_dir, record)
            counts["applied"] += 1
            entry = {
                "op": op,
                "status": "ok",
                "source": record["source"],
                "destination": record["destination"],
                "backup": str(backup),
            }
        except Exception as error:
            counts["failed"] += 1
            entry = {
                "op": op,
                "status": "error",
                "source": record["source"],
                "destination": record["destination"],
                "error": str(error),
            }
        run_state.append_jsonl_fsync(log_path, entry)


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
        "schema": args.schema,
    }


def run_configuration(args, vault, schema_hash):
    return {
        "workflow": WORKFLOW,
        "command": args.mode,
        "input": {"vault": str(vault), "mode": args.mode, "schema_hash": schema_hash},
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
    configuration = run_configuration(args, vault, schema_hash)
    if resuming:
        try:
            run_state.assert_compatible_run(state, configuration)
        except ValueError as error:
            raise UserError(str(error)) from error
    warnings = []
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
                current_items, _ = scan_vault(vault, schema_path, args.mode, args.limit)
                drift = run_state.input_drift(items, current_items)
                for added in drift["added"]:
                    warnings.append(f"input drift: {added['path']} appeared after the scan; run again to include it")
                for removed in drift["removed"]:
                    warnings.append(f"input drift: {removed['path']} disappeared after the scan")
                for changed in drift["changed"]:
                    warnings.append(f"input drift: {changed['after']['path']} changed after the scan; it will be refused at apply")
        else:
            items, _ = scan_vault(vault, schema_path, args.mode, args.limit)
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
            lambda draft: draft.update({"phase": "route"}) or draft,
            event={"type": "phase", "phase": "classify", "records": len(class_records)},
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
        base_references = scan_base_references(vault, records)

        if args.apply:
            apply_records(args, vault, run_dir, records, counts, schema)
            _, index_warnings = refresh_vault_index(vault, schema_path)
            warnings.extend(index_warnings)
            final_phase = "complete"
        else:
            final_phase = "planned"
        plan_path, report_path = write_plan(
            run_dir, records, counts, dedupe, base_references, args.mode, not args.apply, vault, schema_hash, warnings
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


def doctor(args):
    vault = Path(args.vault).expanduser().resolve()
    checks = {}
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
    chat_check = {"ok": False, "url": args.base_url, "model": args.model}
    try:
        started = time.time()
        probe_messages = [
            {"role": "system", "content": "Reply with exactly {\"ok\": true} as JSON."},
            {"role": "user", "content": "ping"},
        ]
        if args.think_prefill:
            probe_messages.append({"role": "assistant", "content": THINK_PREFILL})
        request_json(
            args.base_url,
            args.model,
            args.api_key,
            min(args.request_timeout, 60),
            probe_messages,
            cache_prompt=False,
        )
        chat_check["ok"] = True
        chat_check["seconds"] = round(time.time() - started, 2)
    except (UserError, json.JSONDecodeError) as error:
        chat_check["detail"] = str(error)
    checks["chat"] = chat_check
    ok = ok and chat_check["ok"]
    if args.no_embeddings:
        checks["embeddings"] = {"ok": True, "skipped": True}
    else:
        probe = forge_embeddings.embeddings_doctor(url=args.embeddings_url, model=args.embeddings_model)
        checks["embeddings"] = {
            "ok": probe["reachable"],
            "url": probe["url"],
            "model": probe["model"],
            "detail": probe["detail"],
        }
        ok = ok and probe["reachable"]
    return structured("ok" if ok else "error", data={"checks": checks})


class TrackingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Classify, dedupe, and organize Obsidian vault notes.")
    parser.add_argument("mode", choices=["inbox", "vault", "status", "doctor"])
    parser.add_argument("--vault")
    parser.add_argument("--schema", action=TrackingAction)
    parser.add_argument("--apply", action="store_true")
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
    parser.add_argument("--think-prefill", action="store_true", help="prefill an empty think block (for thinking backends like :8008)")
    parser.add_argument("--force-reclassify", action="store_true")
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
    args.base_url = normalize_base_url(
        args.base_url
        or os.environ.get("VAULT_ORGANIZER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    args.model = args.model or os.environ.get("VAULT_ORGANIZER_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    args.api_key = args.api_key or os.environ.get("VAULT_ORGANIZER_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.schema = args.schema or os.environ.get("VAULT_ORGANIZER_SCHEMA") or None
    args.embeddings_url = forge_embeddings.endpoint_url(args.embeddings_url)
    args.embeddings_model = forge_embeddings.model_name(args.embeddings_model)
    if args.near_dupe_auto is None:
        args.near_dupe_auto = NEAR_DUPE_AUTO
    if args.near_dupe_review is None:
        args.near_dupe_review = NEAR_DUPE_REVIEW
    if not 0 < args.near_dupe_review <= args.near_dupe_auto <= 1:
        raise UserError("--near-dupe-review must be within (0, --near-dupe-auto] and --near-dupe-auto at most 1")
    args.cache_prompt = not args.no_cache_prompt
    return args


def run(argv):
    args = parse_args(argv)
    if args.mode == "status":
        result = status(args)
    elif args.mode == "doctor":
        result = doctor(args)
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
