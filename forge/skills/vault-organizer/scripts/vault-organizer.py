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
import urllib.request
from pathlib import Path


DEFAULT_SCHEMA = "99 System/0.00 Vault Schema.md"
DEFAULT_BASE_URL = "http://llms:8008/v1/chat/completions"
DEFAULT_MODEL = "code"
PROMPT_VERSION = "vault-organizer-v1"
MAX_BODY_CHARS = 30000
PROTECTED_DIRS = {".obsidian", ".git", ".vault-organizer", "node_modules"}
RESERVED_WINDOWS_NAMES = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}
REQUIRED_SECTIONS = [
    "Approved properties",
    "Note types",
    "Status values",
    "Domains",
    "Subdomains",
    "Project registry",
    "Source kinds",
    "Capture types",
    "Legacy normalization map",
    "Folder routing",
]


class UserError(Exception):
    pass


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


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_text_file(path):
    return path.read_text(encoding="utf-8-sig")


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


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


def strip_inline_code(value):
    text = value.strip()
    if text.startswith("`") and text.endswith("`") and len(text) >= 2:
        text = text[1:-1].strip()
    return text


def strip_schema_value(value):
    text = strip_inline_code(value)
    if text.startswith('"') and text.endswith('"') and text[1:-1].startswith("[[") and text[1:-1].endswith("]]"):
        return text[1:-1]
    return text


def split_markdown_table_row(line):
    text = line.strip()
    if not text.startswith("|") or not text.endswith("|"):
        raise UserError(f"malformed table row: {line}")
    return [cell.strip() for cell in text.strip("|").split("|")]


def is_divider_row(cells):
    return all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def iter_heading_lines(lines):
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            yield index, len(match.group(1)), match.group(2).strip()


def heading_index(lines, title, level=2):
    for index, found_level, found_title in iter_heading_lines(lines):
        if found_level == level and found_title == title:
            return index
    raise UserError(f"missing required section: {title}")


def section_bounds(lines, title, level=2):
    start = heading_index(lines, title, level)
    end = len(lines)
    for index, found_level, _ in iter_heading_lines(lines[start + 1:]):
        actual = start + 1 + index
        if found_level <= level:
            end = actual
            break
    return start, end


def table_after(lines, heading, required_columns, level=2):
    start, end = section_bounds(lines, heading, level)
    table_lines = []
    for line in lines[start + 1:end]:
        if line.strip().startswith("|"):
            table_lines.append(line)
        elif table_lines and line.strip():
            break
    if len(table_lines) < 2:
        raise UserError(f"{heading}: missing Markdown table")
    header = split_markdown_table_row(table_lines[0])
    divider = split_markdown_table_row(table_lines[1])
    if not is_divider_row(divider):
        raise UserError(f"{heading}: table divider is malformed")
    missing = [column for column in required_columns if column not in header]
    if missing:
        raise UserError(f"{heading}: missing required columns: {', '.join(missing)}")
    rows = []
    for line in table_lines[2:]:
        cells = split_markdown_table_row(line)
        if len(cells) != len(header):
            raise UserError(f"{heading}: malformed row has {len(cells)} cells, expected {len(header)}")
        rows.append(dict(zip(header, cells)))
    if not rows:
        raise UserError(f"{heading}: table is empty")
    return rows


def parse_bullet_registry(lines, heading):
    start, end = section_bounds(lines, heading)
    values = {}
    for line in lines[start + 1:end]:
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if not match:
            continue
        raw = match.group(1)
        if " — " in raw:
            value, definition = raw.split(" — ", 1)
        elif " – " in raw:
            value, definition = raw.split(" – ", 1)
        elif " - " in raw:
            value, definition = raw.split(" - ", 1)
        elif ": " in raw:
            value, definition = raw.split(": ", 1)
        else:
            value, definition = raw, ""
        key = strip_schema_value(value)
        if not key:
            raise UserError(f"{heading}: empty value")
        if key in values:
            raise UserError(f"{heading}: duplicate value {key}")
        values[key] = definition.strip()
    if not values:
        raise UserError(f"{heading}: controlled vocabulary is empty")
    return values


def parse_assignment(value):
    text = strip_inline_code(value)
    if ":" not in text:
        raise UserError(f"invalid legacy assignment: {value}")
    key, raw = text.split(":", 1)
    return key.strip(), strip_schema_value(raw.strip())


def parse_legacy_output(value):
    parts = [part.strip() for part in value.split("+")]
    output = {}
    for part in parts:
        key, raw = parse_assignment(part)
        output[key] = raw
    return output


def property_shape(shape_text):
    text = shape_text.strip().lower()
    if text.startswith("list"):
        return "list"
    return "scalar"


def property_value_mode(shape_text):
    text = shape_text.strip().lower()
    if "controlled" in text:
        return "controlled"
    if "registered" in text and "wikilink" in text:
        return "registered_wikilink"
    if "wikilink" in text:
        return "wikilink"
    return "free"


def pad2(number):
    return str(number).zfill(2)


def require_number(value, context):
    text = strip_schema_value(value)
    if not re.fullmatch(r"\d{1,2}", text):
        raise UserError(f"{context}: number must be an integer from 1 through 99")
    number = int(text)
    if number < 1 or number > 99:
        raise UserError(f"{context}: number must be from 1 through 99")
    return number


def require_safe_label(label, context):
    text = strip_schema_value(label)
    if not text:
        raise UserError(f"{context}: label is empty")
    if any(character in text for character in ("/", "\\", "\0")):
        raise UserError(f"{context}: label contains a path separator")
    if any(ord(character) < 32 for character in text):
        raise UserError(f"{context}: label contains a control character")
    if text.rstrip(" .") != text:
        raise UserError(f"{context}: label has unsafe trailing punctuation")
    if text.lower() in RESERVED_WINDOWS_NAMES:
        raise UserError(f"{context}: label uses a reserved device name")
    return text


def normalize_project_value(value):
    text = strip_schema_value(value)
    if text.startswith('"') and text.endswith('"') and text[1:-1].startswith("[["):
        text = text[1:-1]
    return text


def project_name(value):
    if not re.fullmatch(r"\[\[[^\]\n\r]+\]\]", value):
        raise UserError(f"project value must be a wikilink: {value}")
    return value[2:-2]


def domain_folder(domain):
    return f"{pad2(domain['number'])} {domain['label']}"


def subdomain_folder(domain, subdomain):
    return f"{domain['number']}.{pad2(subdomain['number'])} {subdomain['label']}"


def project_folder(domain, subdomain, project):
    name = project_name(project["value"])
    if subdomain:
        return f"{domain['number']}.{pad2(subdomain['number'])}.{pad2(project['number'])} {name}"
    return f"{domain['number']}.{pad2(project['number'])} {name}"


def compile_destination(schema, metadata):
    domain = schema["domains"][metadata["domain"]]
    parts = [domain_folder(domain)]
    subdomain = None
    project = None
    if metadata.get("project"):
        project = schema["projects"][metadata["project"]]
        if project.get("subdomain"):
            subdomain = schema["subdomains"][project["domain"]][project["subdomain"]]
        parts.append(project_folder(domain, subdomain, project) if not subdomain else subdomain_folder(domain, subdomain))
        if subdomain:
            parts.append(project_folder(domain, subdomain, project))
    elif metadata.get("subdomain"):
        subdomain = schema["subdomains"][metadata["domain"]][metadata["subdomain"]]
        parts.append(subdomain_folder(domain, subdomain))
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise UserError(f"unsafe derived path component: {part}")
    return Path(*parts)


def parse_schema_note(text):
    lines = text.splitlines()
    for section in REQUIRED_SECTIONS:
        heading_index(lines, section)

    properties = {}
    property_order = []
    for row in table_after(lines, "Approved properties", ["Property", "Required", "Shape", "Definition"]):
        name = strip_schema_value(row["Property"])
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise UserError(f"Approved properties: invalid property name {name}")
        if name in properties:
            raise UserError(f"Approved properties: duplicate property {name}")
        property_order.append(name)
        properties[name] = {
            "required": row["Required"].strip().lower(),
            "shape": property_shape(row["Shape"]),
            "value_mode": property_value_mode(row["Shape"]),
            "definition": row["Definition"].strip(),
        }
    for required in ("type", "status", "domain"):
        if required not in properties:
            raise UserError(f"Approved properties: missing required core property {required}")

    types = parse_bullet_registry(lines, "Note types")
    statuses = parse_bullet_registry(lines, "Status values")
    source_kinds = parse_bullet_registry(lines, "Source kinds")
    capture_types = parse_bullet_registry(lines, "Capture types")

    domains = {}
    domain_numbers = {}
    for row in table_after(lines, "Domains", ["Value", "Number", "Label", "Definition"]):
        value = strip_schema_value(row["Value"])
        number = require_number(row["Number"], f"Domains {value}")
        label = require_safe_label(row["Label"], f"Domains {value}")
        if value in domains:
            raise UserError(f"Domains: duplicate value {value}")
        if number == 0 or number in domain_numbers:
            raise UserError(f"Domains: duplicate or reserved number {number}")
        domain_numbers[number] = value
        domains[value] = {"value": value, "number": number, "label": label, "definition": row["Definition"].strip()}
    if not domains:
        raise UserError("Domains: controlled vocabulary is empty")

    subdomains = {domain: {} for domain in domains}
    subdomain_start, subdomain_end = section_bounds(lines, "Subdomains")
    headings = [
        (subdomain_start + 1 + index, found_level, found_title)
        for index, found_level, found_title in iter_heading_lines(lines[subdomain_start + 1:subdomain_end])
        if found_level == 3
    ]
    for heading_pos, _, domain_value in headings:
        if domain_value not in domains:
            raise UserError(f"Subdomains: subsection references unknown domain {domain_value}")
        next_pos = subdomain_end
        for candidate, _, _ in headings:
            if candidate > heading_pos:
                next_pos = candidate
                break
        table_lines = lines[heading_pos:next_pos]
        rows = table_after(table_lines, domain_value, ["Value", "Number", "Label", "Definition"], level=3)
        numbers = {}
        for row in rows:
            value = strip_schema_value(row["Value"])
            number = require_number(row["Number"], f"Subdomains {domain_value}/{value}")
            label = require_safe_label(row["Label"], f"Subdomains {domain_value}/{value}")
            if value in subdomains[domain_value]:
                raise UserError(f"Subdomains {domain_value}: duplicate value {value}")
            if number in numbers:
                raise UserError(f"Subdomains {domain_value}: duplicate number {number}")
            numbers[number] = value
            subdomains[domain_value][value] = {
                "value": value,
                "domain": domain_value,
                "number": number,
                "label": label,
                "definition": row["Definition"].strip(),
            }

    projects = {}
    project_numbers = {}
    for row in table_after(lines, "Project registry", ["Approved value", "Domain", "Subdomain", "Number", "Definition"]):
        value = normalize_project_value(row["Approved value"])
        project_name(value)
        domain_value = strip_schema_value(row["Domain"])
        subdomain_value = strip_schema_value(row["Subdomain"]) if row["Subdomain"].strip() else ""
        number = require_number(row["Number"], f"Project registry {value}")
        if domain_value not in domains:
            raise UserError(f"Project registry {value}: unknown domain {domain_value}")
        if subdomain_value and subdomain_value not in subdomains.get(domain_value, {}):
            raise UserError(f"Project registry {value}: unknown subdomain {domain_value}/{subdomain_value}")
        parent = (domain_value, subdomain_value)
        if (parent, number) in project_numbers:
            raise UserError(f"Project registry {value}: duplicate number {number} beneath {parent}")
        if value in projects:
            raise UserError(f"Project registry: duplicate project {value}")
        project_numbers[(parent, number)] = value
        projects[value] = {
            "value": value,
            "domain": domain_value,
            "subdomain": subdomain_value,
            "number": number,
            "definition": row["Definition"].strip(),
        }

    legacy = {}
    for row in table_after(lines, "Legacy normalization map", ["Legacy input", "Canonical output"]):
        key, old_value = parse_assignment(row["Legacy input"])
        legacy[f"{key}:{old_value}"] = parse_legacy_output(row["Canonical output"])

    schema = {
        "properties": properties,
        "property_order": property_order,
        "types": types,
        "statuses": statuses,
        "domains": domains,
        "subdomains": subdomains,
        "projects": projects,
        "source_kinds": source_kinds,
        "capture_types": capture_types,
        "legacy": legacy,
    }
    validate_derived_paths(schema)
    return schema


def validate_derived_paths(schema):
    seen = {}
    for value, domain in schema["domains"].items():
        path = Path(domain_folder(domain)).as_posix().lower()
        if path in seen:
            raise UserError(f"duplicate derived path for domain {value}: {path}")
        seen[path] = value
        for subdomain_value, subdomain in schema["subdomains"].get(value, {}).items():
            subpath = Path(domain_folder(domain), subdomain_folder(domain, subdomain)).as_posix().lower()
            if subpath in seen:
                raise UserError(f"duplicate derived path for subdomain {value}/{subdomain_value}: {subpath}")
            seen[subpath] = f"{value}/{subdomain_value}"
    for value, project in schema["projects"].items():
        domain = schema["domains"][project["domain"]]
        subdomain = schema["subdomains"][project["domain"]].get(project.get("subdomain", ""))
        parts = [domain_folder(domain)]
        if subdomain:
            parts.append(subdomain_folder(domain, subdomain))
        parts.append(project_folder(domain, subdomain, project))
        path = Path(*parts).as_posix().lower()
        if path in seen:
            raise UserError(f"duplicate derived path for project {value}: {path}")
        seen[path] = value


def compiled_schema_for(vault, schema_path):
    schema_bytes = schema_path.read_bytes()
    schema_hash = sha256_bytes(schema_bytes)
    cache_dir = vault / ".vault-organizer" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    compiled_path = cache_dir / "compiled-schema.json"
    if compiled_path.exists():
        try:
            cached = json.loads(compiled_path.read_text(encoding="utf-8"))
            if cached.get("schema_hash") == schema_hash:
                return cached["schema"], schema_hash
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    schema = parse_schema_note(schema_bytes.decode("utf-8-sig"))
    compiled_path.write_text(json.dumps({"schema_hash": schema_hash, "schema": schema}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return schema, schema_hash


def resolve_schema_path(vault, raw_schema):
    path = Path(raw_schema or DEFAULT_SCHEMA).expanduser()
    if not path.is_absolute():
        path = vault / path
    path = path.resolve()
    if not path.is_file():
        raise UserError(f"schema note does not exist: {path}")
    return path


def relative_path(vault, path):
    return path.resolve().relative_to(vault).as_posix()


def path_is_inside(parent, child):
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def selected_notes(vault, schema_path, mode, limit):
    vault = vault.resolve()
    schema_path = schema_path.resolve()
    if mode == "inbox":
        root = vault / "00 Inbox"
        if not root.is_dir():
            raise UserError(f"inbox directory does not exist: {root}")
        candidates = []
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirpath = Path(directory)
            dirnames[:] = [name for name in sorted(dirnames) if not (dirpath / name).is_symlink()]
            for filename in sorted(filenames):
                path = dirpath / filename
                if path.is_symlink() or path.suffix.lower() != ".md":
                    continue
                if path.resolve() == schema_path.resolve():
                    continue
                candidates.append(path.resolve())
    else:
        candidates = []
        for directory, dirnames, filenames in os.walk(vault, followlinks=False):
            dirpath = Path(directory)
            kept = []
            for name in sorted(dirnames):
                child = dirpath / name
                if child.is_symlink() or name in PROTECTED_DIRS or name.startswith("."):
                    continue
                kept.append(name)
            dirnames[:] = kept
            for filename in sorted(filenames):
                path = dirpath / filename
                if path.is_symlink() or path.suffix.lower() != ".md":
                    continue
                if path.resolve() == schema_path.resolve():
                    continue
                relative = path.resolve().relative_to(vault)
                if any(part.startswith(".") for part in relative.parts):
                    continue
                candidates.append(path.resolve())
    candidates = sorted(candidates, key=lambda item: item.relative_to(vault).as_posix())
    return candidates[:limit] if limit is not None else candidates


def split_frontmatter(data):
    had_bom = data.startswith(b"\xef\xbb\xbf")
    raw = data[3:] if had_bom else data
    text = raw.decode("utf-8")
    lines = text.splitlines(keepends=True)
    if lines and lines[0].rstrip("\r\n") == "---":
        for index in range(1, len(lines)):
            if lines[index].rstrip("\r\n") == "---":
                body = "".join(lines[index + 1:])
                return {"malformed": False, "body": body, "had_frontmatter": True, "had_bom": had_bom}
        return {"malformed": True, "body": text, "had_frontmatter": True, "had_bom": had_bom}
    return {"malformed": False, "body": text, "had_frontmatter": False, "had_bom": had_bom}


def note_title(path, body):
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return path.stem


def excerpt_body(body):
    if len(body) <= MAX_BODY_CHARS:
        return body, False
    headings = "\n".join(line for line in body.splitlines() if line.startswith("#"))[:6000]
    head = body[:12000]
    tail = body[-12000:]
    excerpt = f"{head}\n\n<!-- headings -->\n{headings}\n\n<!-- tail -->\n{tail}"
    return excerpt[:MAX_BODY_CHARS], True


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


def cache_key(title, body_hash, schema_hash, model, base_url):
    payload = {
        "title": title,
        "body_hash": body_hash,
        "schema_hash": schema_hash,
        "prompt_version": PROMPT_VERSION,
        "model": model,
        "endpoint": base_url,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


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


def build_messages(schema, title, current_path, body_excerpt, repair=None):
    system = (
        "You classify Obsidian Markdown notes. Return exactly one JSON object. "
        "Do not return YAML, paths, folder numbers, explanations, markdown, or filesystem instructions. "
        "Choose values only from the approved schema. Classify by the note's primary purpose. "
        "Use needs_review true when required classification is genuinely ambiguous."
    )
    payload = {
        "schema": compact_schema_for_prompt(schema),
        "title": title,
        "current_relative_path": current_path,
        "body": body_excerpt,
        "required_response_shape": {
            "metadata": {key: None for key in schema["property_order"]},
            "needs_review": False,
            "review_reason": None,
        },
    }
    if repair:
        payload["repair"] = repair
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def normalize_base_url(value):
    url = (value or DEFAULT_BASE_URL).strip().rstrip("/")
    if url.endswith("/chat/completions"):
        return url
    if url.endswith("/v1"):
        return f"{url}/chat/completions"
    return url


def request_json(base_url, model, api_key, timeout, messages):
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
        "response_format": {"type": "json_object"},
    }
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
    return json.loads(content)


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


def has_control_character(value):
    return any(ord(character) < 32 and character not in "\t" for character in value)


def valid_wikilink(value):
    return isinstance(value, str) and re.fullmatch(r"\[\[[^\]\r\n]+\]\]", value) is not None


def validate_classification(response, schema):
    errors = []
    warnings = []
    if not isinstance(response, dict):
        return None, [], ["response is not a JSON object"]
    expected = {"metadata", "needs_review", "review_reason"}
    actual = set(response)
    if actual != expected:
        errors.append(f"top-level keys must be exactly {sorted(expected)}")
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
    return {
        "metadata": normalized,
        "needs_review": needs_review,
        "review_reason": review_reason,
    }, warnings, errors


def classify_note(args, schema, title, relative_source, body, schema_hash, cache, cache_path):
    body_hash = sha256_bytes(body.encode("utf-8"))
    key = cache_key(title, body_hash, schema_hash, args.model, args.base_url)
    if not args.force_reclassify and key in cache:
        cached = cache[key]
        validated, warnings, errors = validate_classification(cached["response"], schema)
        if not errors:
            return validated, warnings, "cache", key
    excerpt, excerpted = excerpt_body(body)
    response = request_json(args.base_url, args.model, args.api_key, args.request_timeout, build_messages(schema, title, relative_source, excerpt))
    validated, warnings, errors = validate_classification(response, schema)
    if errors:
        repair = {"original_response": response, "validation_errors": errors}
        repaired = request_json(args.base_url, args.model, args.api_key, args.request_timeout, build_messages(schema, title, relative_source, excerpt, repair=repair))
        validated, warnings, errors = validate_classification(repaired, schema)
        response = repaired
    if errors:
        return {"metadata": {}, "needs_review": True, "review_reason": "; ".join(errors), "excerpted": excerpted}, warnings, "model", key
    cache[key] = {"response": response, "stored_at": time.time()}
    save_cache(cache_path, cache)
    validated["excerpted"] = excerpted
    return validated, warnings, "model", key


def yaml_quote(value):
    if "\n" in value or "\r" in value:
        raise UserError("YAML scalar contains newline")
    if has_control_character(value):
        raise UserError("YAML scalar contains a control character")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_scalar(value, force_quote=False):
    if force_quote or valid_wikilink(value) or ":" in value or "'" in value or '"' in value:
        return yaml_quote(value)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", value):
        return yaml_quote(value)
    return value


def serialize_frontmatter(metadata, schema):
    lines = ["---"]
    for key in schema["property_order"]:
        if key not in metadata:
            continue
        value = metadata[key]
        prop = schema["properties"][key]
        if value is None or value == "" or value == []:
            continue
        if prop["shape"] == "list":
            if not value:
                continue
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {yaml_scalar(item, force_quote=True)}")
        else:
            lines.append(f"{key}: {yaml_scalar(value, force_quote=prop['value_mode'] in {'wikilink', 'registered_wikilink'})}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def revised_note_text(metadata, schema, body):
    return serialize_frontmatter(metadata, schema) + body


def record_for_review(queue_path, record):
    with queue_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


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
    }


def process_notes(args, vault, schema, schema_hash, notes, run_dir):
    cache, cache_path = load_cache(vault)
    records = []
    warnings = []
    counts = initial_counts()
    counts["selected"] = len(notes)
    review_queue = run_dir / "review-queue.jsonl"
    review_queue.write_text("", encoding="utf-8")
    for path in notes:
        rel = relative_path(vault, path)
        try:
            data = path.read_bytes()
            source_hash = sha256_bytes(data)
            frontmatter = split_frontmatter(data)
            if frontmatter["malformed"]:
                record = {
                    "source": rel,
                    "destination": rel,
                    "source_hash": source_hash,
                    "body_hash": None,
                    "classification_source": "none",
                    "frontmatter_changed": False,
                    "move_required": False,
                    "excerpted": False,
                    "needs_review": True,
                    "review_reason": "opening frontmatter delimiter has no closing delimiter",
                    "warnings": ["malformed_frontmatter"],
                    "status": "review",
                }
                records.append(record)
                counts["review_required"] += 1
                record_for_review(review_queue, record)
                continue
            body = frontmatter["body"]
            title = note_title(path, body)
            classification, record_warnings, classification_source, _ = classify_note(args, schema, title, rel, body, schema_hash, cache, cache_path)
            counts["cached" if classification_source == "cache" else "classified"] += 1
            metadata = classification["metadata"]
            needs_review = classification.get("needs_review", False)
            review_reason = classification.get("review_reason")
            destination_relative = rel
            frontmatter_changed = False
            move_required = False
            revised_text = None
            status = "ok"
            if needs_review:
                status = "review"
            else:
                destination_dir = compile_destination(schema, metadata)
                destination_relative = (destination_dir / path.name).as_posix()
                revised_text = revised_note_text(metadata, schema, body)
                original_text = data.decode("utf-8-sig")
                frontmatter_changed = revised_text != original_text
                move_required = destination_relative != rel
                if not frontmatter_changed and not move_required:
                    counts["unchanged"] += 1
                else:
                    if frontmatter_changed:
                        counts["frontmatter_updates"] += 1
                    if move_required:
                        counts["moves"] += 1
            record = {
                "source": rel,
                "destination": destination_relative,
                "source_hash": source_hash,
                "body_hash": sha256_bytes(body.encode("utf-8")),
                "classification_source": classification_source,
                "metadata": metadata,
                "frontmatter_changed": frontmatter_changed,
                "move_required": move_required,
                "excerpted": bool(classification.get("excerpted")),
                "needs_review": needs_review,
                "review_reason": review_reason,
                "warnings": record_warnings,
                "status": status,
                "revised_text": revised_text,
            }
            records.append(record)
            if needs_review:
                counts["review_required"] += 1
                record_for_review(review_queue, record)
        except Exception as error:
            message = str(error)
            warnings.append(f"{rel}: {message}")
            record = {
                "source": rel,
                "destination": rel,
                "source_hash": sha256_file(path),
                "body_hash": None,
                "classification_source": "error",
                "frontmatter_changed": False,
                "move_required": False,
                "excerpted": False,
                "needs_review": True,
                "review_reason": message,
                "warnings": [message],
                "status": "failed",
            }
            records.append(record)
            counts["failed"] += 1
            record_for_review(review_queue, record)
    validate_plan(vault, records)
    review_queue.write_text("", encoding="utf-8")
    for record in records:
        if record["needs_review"] or record["status"] == "failed":
            record_for_review(review_queue, record)
    counts = recompute_counts(records)
    counts["selected"] = len(notes)
    return records, warnings, counts


def recompute_counts(records):
    counts = initial_counts()
    for record in records:
        if record.get("classification_source") == "cache":
            counts["cached"] += 1
        elif record.get("classification_source") == "model":
            counts["classified"] += 1
        if record.get("status") == "failed":
            counts["failed"] += 1
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


def validate_plan(vault, records):
    seen = {}
    for record in records:
        if record["status"] != "ok" or record["needs_review"]:
            continue
        destination = record["destination"]
        key = destination.lower()
        if key in seen:
            record["status"] = "failed"
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
            record["status"] = "failed"
            record["warnings"].append("destination collision")
            record["needs_review"] = True
            record["review_reason"] = "destination collision"


def plan_for_json(records):
    cleaned = []
    for record in records:
        item = dict(record)
        item.pop("revised_text", None)
        cleaned.append(item)
    return cleaned


def write_plan(run_dir, records, counts, mode, dry_run, vault, schema_hash, warnings):
    plan_path = run_dir / "plan.json"
    report_path = run_dir / "report.md"
    data = {
        "mode": mode,
        "dry_run": dry_run,
        "vault": str(vault),
        "schema_hash": schema_hash,
        "run_directory": str(run_dir),
        "counts": counts,
        "records": plan_for_json(records),
        "warnings": warnings,
    }
    plan_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    destination_counts = {}
    for record in records:
        if record["status"] == "ok" and not record["needs_review"]:
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
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return plan_path, report_path


def apply_records(vault, run_dir, records, counts):
    log_path = run_dir / "apply-log.jsonl"
    log_path.write_text("", encoding="utf-8")
    for record in records:
        if record["status"] != "ok" or record["needs_review"]:
            counts["skipped"] += 1
            continue
        source = vault / record["source"]
        destination = vault / record["destination"]
        try:
            data = source.read_bytes()
            if sha256_bytes(data) != record["source_hash"]:
                raise UserError("source changed since planning")
            backup = run_dir / "backup" / record["source"]
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, backup)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() and destination.resolve() != source.resolve():
                raise UserError("destination collision")
            fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(record["revised_text"])
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
            counts["applied"] += 1
            entry = {"status": "ok", "source": record["source"], "destination": record["destination"], "backup": str(backup)}
        except Exception as error:
            counts["failed"] += 1
            entry = {"status": "error", "source": record["source"], "destination": record["destination"], "error": str(error)}
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Classify and organize Obsidian vault notes.")
    parser.add_argument("mode", choices=["inbox", "vault"])
    parser.add_argument("--vault", required=True)
    parser.add_argument("--schema")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--api-key")
    parser.add_argument("--request-timeout", type=float, default=60)
    parser.add_argument("--force-reclassify", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        raise UserError("--limit must be non-negative")
    args.base_url = normalize_base_url(
        args.base_url
        or os.environ.get("VAULT_ORGANIZER_BASE_URL")
        or os.environ.get("OPENAI_BASE_URL")
        or DEFAULT_BASE_URL
    )
    args.model = args.model or os.environ.get("VAULT_ORGANIZER_MODEL") or os.environ.get("OPENAI_MODEL") or DEFAULT_MODEL
    args.api_key = args.api_key or os.environ.get("VAULT_ORGANIZER_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.schema = args.schema or os.environ.get("VAULT_ORGANIZER_SCHEMA") or DEFAULT_SCHEMA
    return args


def run(argv):
    args = parse_args(argv)
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path)
    notes = selected_notes(vault, schema_path, args.mode, args.limit)
    run_dir = unique_run_directory(vault)
    log(args, f"selected {len(notes)} notes")
    records, warnings, counts = process_notes(args, vault, schema, schema_hash, notes, run_dir)
    if args.apply:
        apply_records(vault, run_dir, records, counts)
    plan_path, report_path = write_plan(run_dir, records, counts, args.mode, not args.apply, vault, schema_hash, warnings)
    result = structured(
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
    print_json(result)
    return 0


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
