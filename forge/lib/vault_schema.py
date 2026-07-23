#!/usr/bin/env python3
"""Shared Obsidian vault schema parsing, folder routing, and frontmatter I/O.

An Obsidian vault organized by forge keeps one human-maintained Markdown schema
note as its sole source of truth for frontmatter properties, controlled values,
and folder routing. This module is the single implementation of how that note is
read and how folder paths are derived from it, so every skill that touches the
vault agrees byte-for-byte about where a note belongs.

Design rules:

- Standard library only, so skills stay installable without extra dependencies.
- Fail closed. A malformed section, duplicate number, unsafe label, or colliding
  derived path raises ``UserError`` rather than guessing.
- Parsing is deterministic and does not use a model. Stable headings and table
  columns are the contract; prose examples are never used to reconstruct routes.
- Compiled JSON caches are accelerators keyed by the schema note's SHA-256. The
  Markdown note always wins.

Consumers: ``skills/vault-organizer`` (classify, route, replace frontmatter) and
``skills/vault-connections`` (search, connection proposals, additive frontmatter
merge).
"""

import hashlib
import json
import os
import re
import urllib.parse
from pathlib import Path

DEFAULT_SCHEMA = "99 Meta/99.02 Schemas/0.00 Vault Schema.md"
SCHEMA_BASENAME = "0.00 Vault Schema.md"
INBOX_DIR = "00 Inbox"
PROTECTED_DIRS = {".obsidian", ".git", ".vault-organizer", ".vault-connections", "node_modules"}
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
COMPILED_SCHEMA_VERSION = 2


class UserError(Exception):
    pass


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_text(text):
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def optional_bullet_lines(lines, heading, level=3):
    try:
        start, end = section_bounds(lines, heading, level)
    except UserError:
        return []
    bullets = []
    for line in lines[start + 1:end]:
        match = re.match(r"^\s*[-*]\s+(.+?)\s*$", line)
        if match:
            bullets.append(match.group(1).strip())
    return bullets


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
        "domain_rules": optional_bullet_lines(lines, "Domain decision rules"),
        "project_rules": optional_bullet_lines(lines, "Project assignment rules"),
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


def compiled_schema_for(vault, schema_path, cache_dir=None):
    """Parse the schema note, memoized in ``cache_dir`` keyed by its SHA-256.

    ``cache_dir`` defaults to the vault-organizer cache so its on-disk layout is
    unchanged; other skills pass their own directory.
    """
    schema_bytes = schema_path.read_bytes()
    schema_hash = sha256_bytes(schema_bytes)
    cache_dir = Path(cache_dir) if cache_dir else vault / ".vault-organizer" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    compiled_path = cache_dir / "compiled-schema.json"
    if compiled_path.exists():
        try:
            cached = json.loads(compiled_path.read_text(encoding="utf-8"))
            if cached.get("schema_hash") == schema_hash and cached.get("version") == COMPILED_SCHEMA_VERSION:
                return cached["schema"], schema_hash
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    schema = parse_schema_note(schema_bytes.decode("utf-8-sig"))
    compiled_path.write_text(
        json.dumps({"version": COMPILED_SCHEMA_VERSION, "schema_hash": schema_hash, "schema": schema}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return schema, schema_hash


def resolve_schema_path(vault, raw_schema):
    if raw_schema:
        path = Path(raw_schema).expanduser()
        if not path.is_absolute():
            path = vault / path
        path = path.resolve()
        if not path.is_file():
            raise UserError(f"schema note does not exist: {path}")
        return path
    default = (vault / DEFAULT_SCHEMA).resolve()
    if default.is_file():
        return default
    matches = []
    for candidate in vault.rglob(SCHEMA_BASENAME):
        relative = candidate.resolve().relative_to(vault)
        parts = relative.parts
        if any(part.startswith(".") or part in PROTECTED_DIRS for part in parts):
            continue
        if parts and parts[0] == INBOX_DIR:
            continue
        matches.append(candidate.resolve())
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise UserError(
            f"schema note not found: no {vault / DEFAULT_SCHEMA} and no unique '{SCHEMA_BASENAME}' in the vault; pass --schema"
        )
    listed = ", ".join(str(match) for match in sorted(matches))
    raise UserError(f"multiple schema notes found ({listed}); pass --schema")


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
        root = vault / INBOX_DIR
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
                frontmatter_text = "".join(lines[1:index])
                return {
                    "malformed": False,
                    "body": body,
                    "frontmatter_text": frontmatter_text,
                    "had_frontmatter": True,
                    "had_bom": had_bom,
                }
        return {"malformed": True, "body": text, "frontmatter_text": "", "had_frontmatter": True, "had_bom": had_bom}
    return {"malformed": False, "body": text, "frontmatter_text": "", "had_frontmatter": False, "had_bom": had_bom}


def note_title(path, body):
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return path.stem


def normalize_body_for_hash(body):
    lines = [line.rstrip() for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def first_nonempty_line(normalized_body):
    for line in normalized_body.split("\n"):
        if line.strip():
            return line.strip()
    return ""


def has_control_character(value):
    return any(ord(character) < 32 and character not in "\t" for character in value)


def valid_wikilink(value):
    return isinstance(value, str) and re.fullmatch(r"\[\[[^\]\r\n]+\]\]", value) is not None


def wikilink_target(value):
    """``"[[Note|alias]]"`` or ``"[[Note#Heading]]"`` -> ``"Note"``. Empty when unparseable."""
    match = re.fullmatch(r"\[\[([^\]\r\n]+)\]\]", value.strip()) if isinstance(value, str) else None
    if not match:
        return ""
    return re.split(r"[|#^]", match.group(1), maxsplit=1)[0].strip()


def link_basename(target):
    """Normalize a wikilink target to the basename Obsidian resolves it by."""
    text = urllib.parse.unquote(target).strip()
    if not text:
        return ""
    if text.endswith(".md"):
        text = text[:-3]
    return text.rsplit("/", 1)[-1].strip()


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
