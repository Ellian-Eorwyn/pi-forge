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
``skills/vault-connections`` (search, connection/import proposals, schema-routed
note creation, and additive frontmatter merge).
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
# A directory holding this file contains machine artifacts, not vault notes.
# pi-forge writes it into each workflow category folder under Meta/Workflows so
# that generated run directories are never classified, refiled, or embedded;
# refiling a run's Markdown would break the run's own path references. Drop the
# file into any other folder to exclude it the same way.
WORKSPACE_MARKER = ".forge-workspace"
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
COMPILED_SCHEMA_VERSION = 4
# Declaring a "Sources root" section turns on the sources tree. It is optional so
# that a vault filing sources by domain keeps working untouched, and so the two
# forms cannot be half-adopted: with a root declared the Source kinds registry
# must carry numbers and labels, and without one it stays a plain bullet list.
SOURCES_ROOT_SECTION = "Sources root"
FRONTMATTER_KEY_RE = re.compile(r"^([a-z][a-z0-9_]*):(.*)$")
LIST_ITEM_RE = re.compile(r"^(\s*)-\s+(.*)$")


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


def has_section(lines, heading, level=2):
    try:
        heading_index(lines, heading, level)
    except UserError:
        return False
    return True


def parse_sources_root(lines):
    """The one-row registry declaring the sources tree, or None when absent.

    Its number is reserved the way ``0`` is reserved for the inbox: no domain may
    use it. Keeping the root in the schema rather than in code keeps the rule
    that complete folder strings are derived outputs, never hand-written values.
    """
    if not has_section(lines, SOURCES_ROOT_SECTION):
        return None
    rows = table_after(lines, SOURCES_ROOT_SECTION, ["Number", "Label", "Definition"])
    if len(rows) > 1:
        raise UserError(f"{SOURCES_ROOT_SECTION}: expected one row, found {len(rows)}")
    row = rows[0]
    return {
        "number": require_number(row["Number"], SOURCES_ROOT_SECTION),
        "label": require_safe_label(row["Label"], SOURCES_ROOT_SECTION),
        "definition": row["Definition"].strip(),
    }


def parse_source_kinds(lines, sources_root):
    """Source kinds as ``{value: {value, number, label, definition}}``.

    Both forms compile to the same shape so membership tests and prompt building
    do not care which is in use; ``number`` is None for the bullet form, which is
    what leaves routing off. A declared root with bullet kinds fails closed: half
    the kinds would have nowhere to file.
    """
    start, end = section_bounds(lines, "Source kinds")
    tabular = any(line.strip().startswith("|") for line in lines[start + 1:end])
    if not tabular:
        if sources_root is not None:
            raise UserError(
                f"Source kinds: a declared {SOURCES_ROOT_SECTION} requires a table with Value, "
                f"Number, Label, and Definition columns, not a bullet list"
            )
        registry = parse_bullet_registry(lines, "Source kinds")
        return {
            value: {"value": value, "number": None, "label": None, "definition": definition}
            for value, definition in registry.items()
        }
    rows = table_after(lines, "Source kinds", ["Value", "Number", "Label", "Definition"])
    kinds = {}
    numbers = {}
    for row in rows:
        value = strip_schema_value(row["Value"])
        if not value:
            raise UserError("Source kinds: empty value")
        number = require_number(row["Number"], f"Source kinds {value}")
        label = require_safe_label(row["Label"], f"Source kinds {value}")
        if value in kinds:
            raise UserError(f"Source kinds: duplicate value {value}")
        if number in numbers:
            raise UserError(f"Source kinds: duplicate number {number}")
        numbers[number] = value
        kinds[value] = {
            "value": value,
            "number": number,
            "label": label,
            "definition": row["Definition"].strip(),
        }
    return kinds


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


def property_human_owned(shape_text):
    """Whether a property records a human judgment the classifier must not make.

    Filing replaces a note's frontmatter wholesale from a model response, and
    the response shape is built from every approved property, so a property the
    model cannot know is a property the model will invent. ``date`` is the note's
    own subject date and ``cover`` is a chosen image; neither is derivable from
    the note's text the way ``type`` or ``domain`` are. Marking them in the
    schema keeps them out of the prompt and carries them across filing instead.
    """
    return "human-owned" in shape_text.strip().lower()


def human_owned_properties(schema):
    """Approved property names the classifier neither sees nor sets."""
    return [key for key in schema["property_order"] if schema["properties"][key].get("human_owned")]


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


# Obsidian cannot resolve a [[wikilink]] to a note whose name contains any of
# these, and files carrying them fail to sync to mobile. A note named this way
# is not merely untidy: it is unreachable from the rest of the vault.
LINK_BREAKING_CHARS = ("[", "]", "#", "^", "|")

# Illegal or hostile in a path on at least one platform the vault syncs to.
PATH_UNSAFE_CHARS = ("/", "\\", ":", "*", "?", '"', "<", ">")

# Chosen so a repaired name still reads as the original. The rest have no
# readable equivalent and are dropped.
FILENAME_REPLACEMENTS = {"[": "(", "]": ")", "|": "-"}


def safe_title(value):
    """Return a note title safe to use as a filename and as a wikilink target.

    Every vault skill names notes through this function, so a title generated by
    one survives the others unchanged. It is idempotent, which is what lets it
    double as the validator: `safe_title(text) != text` means `text` is unsafe.
    """
    text = re.sub(r"\s+", " ", str(value)).strip()
    text = "".join(character for character in text if ord(character) >= 32)
    for character in LINK_BREAKING_CHARS + PATH_UNSAFE_CHARS:
        text = text.replace(character, FILENAME_REPLACEMENTS.get(character, ""))
    return re.sub(r"\s+", " ", text).strip(" .")[:120]


def safe_basename(name):
    """Repair a note's filename in place, keeping its extension.

    Returns None when nothing usable survives, so the caller reports the file
    rather than inventing a name for it.
    """
    text = str(name)
    suffix = ".md" if text.casefold().endswith(".md") else ""
    repaired = safe_title(text[: len(text) - len(suffix)] if suffix else text)
    if not repaired or repaired.casefold() in RESERVED_WINDOWS_NAMES:
        return None
    return repaired + suffix


def unsafe_filename_reason(name):
    """Explain why a filename is unsafe, or None when it is fine."""
    stem = str(name)
    stem = stem[:-3] if stem.casefold().endswith(".md") else stem
    breaking = sorted({character for character in stem if character in LINK_BREAKING_CHARS})
    if breaking:
        return f"contains {' '.join(breaking)}, which breaks wikilinks and mobile sync"
    if safe_title(stem) != stem.strip():
        return "contains characters that are unsafe in a filename"
    if stem.casefold() in RESERVED_WINDOWS_NAMES:
        return "is a reserved device name"
    return None


def validate_filename_title(value, label):
    if not isinstance(value, str) or not value.strip():
        raise UserError(f"{label} is empty")
    cleaned = safe_title(value)
    if cleaned != value.strip() or not cleaned:
        raise UserError(f"{label} contains filename-unsafe characters: {value}")
    if cleaned.casefold() in RESERVED_WINDOWS_NAMES:
        raise UserError(f"{label} is a reserved filename: {value}")
    return cleaned


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


def sources_root_folder(root):
    return domain_folder(root)


def source_kind_folder(root, kind):
    return subdomain_folder(root, kind)


def sources_routing_enabled(schema):
    """Whether ``type: source`` notes file by kind instead of by domain."""
    return bool(schema.get("sources_root"))


def source_kind_routes(schema):
    """``{source kind value: vault-relative folder}`` for the sources tree, or {} when off."""
    root = schema.get("sources_root")
    if not root:
        return {}
    base = sources_root_folder(root)
    return {
        value: f"{base}/{source_kind_folder(root, kind)}"
        for value, kind in schema.get("source_kinds", {}).items()
        if isinstance(kind, dict) and kind.get("number")
    }


def routes_as_source(schema, metadata):
    """Whether this note's metadata selects the sources tree rather than a domain folder."""
    if metadata.get("type") != "source" or not sources_routing_enabled(schema):
        return False
    kind = schema.get("source_kinds", {}).get(metadata.get("source_kind") or "")
    return isinstance(kind, dict) and bool(kind.get("number"))


def sources_destination(schema, metadata):
    """``10 Sources/10.01 Book/Academic/Dissertation`` — kind first, domain as detail.

    A source is looked for by what it is before it is looked for by what it is
    about, so ``source_kind`` picks the numbered folder and ``domain`` and
    ``subdomain`` follow as plain labels. Those tails are unnumbered on purpose:
    drift checking treats anything unnumbered below a declared route as detail,
    so the tree can grow a folder per domain without every one of them needing a
    registry row. ``project`` is deliberately not read — a source belongs to its
    kind whichever projects happen to cite it.
    """
    root = schema["sources_root"]
    kind = schema["source_kinds"][metadata["source_kind"]]
    parts = [sources_root_folder(root), source_kind_folder(root, kind)]
    domain = schema["domains"].get(metadata.get("domain") or "")
    if domain:
        parts.append(domain["label"])
        subdomain = schema["subdomains"].get(domain["value"], {}).get(metadata.get("subdomain") or "")
        if subdomain:
            parts.append(subdomain["label"])
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise UserError(f"unsafe derived path component: {part}")
    return Path(*parts)


def compile_destination(schema, metadata):
    if routes_as_source(schema, metadata):
        return sources_destination(schema, metadata)
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
            "human_owned": property_human_owned(row["Shape"]),
            "definition": row["Definition"].strip(),
        }
    for required in ("type", "status", "domain"):
        if required not in properties:
            raise UserError(f"Approved properties: missing required core property {required}")
    for name, prop in properties.items():
        # Nothing would ever satisfy the requirement: the classifier is not shown
        # human-owned properties, so it cannot supply one for a note that lacks it.
        if prop["human_owned"] and prop["required"] != "no":
            raise UserError(f"Approved properties: human-owned property {name} must be Required: no")

    types = parse_bullet_registry(lines, "Note types")
    statuses = parse_bullet_registry(lines, "Status values")
    sources_root = parse_sources_root(lines)
    source_kinds = parse_source_kinds(lines, sources_root)
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
        if sources_root and number == sources_root["number"]:
            raise UserError(
                f"Domains {value}: number {number} is reserved for the {SOURCES_ROOT_SECTION}"
            )
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
        "sources_root": sources_root,
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
    root = schema.get("sources_root")
    if root:
        seen[Path(sources_root_folder(root)).as_posix().lower()] = SOURCES_ROOT_SECTION
        for value, path in source_kind_routes(schema).items():
            key = Path(path).as_posix().lower()
            if key in seen:
                raise UserError(f"duplicate derived path for source kind {value}: {key}")
            seen[key] = f"source_kind/{value}"
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


# --- schema drift -----------------------------------------------------------
#
# ``validate_derived_paths`` only catches two *declared* routes colliding with
# each other. Nothing checks the compiled routes against the folders that
# actually exist, and filing a note creates its destination on demand. So a
# schema that says ``8.02 Organizations`` while the notes live in ``8.03
# Organizations`` silently grows a second folder on the next classification, and
# Johnny Decimal's one-number-one-place guarantee is gone with no error raised.

DRIFT_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}
# Accepts unpadded child numbers too, so a hand-made `9.2 Practices` is still
# recognized as claiming a slot rather than passing as unnumbered detail.
FOLDER_NUMBER_RE = re.compile(r"^(?P<number>\d{1,2}(?:\.\d{1,2})*)\s+(?P<label>.+)$")


def split_folder_name(name):
    """``"8.02 Organizations"`` -> ``("8.02", "Organizations")``; unnumbered -> ``(None, name)``."""
    match = FOLDER_NUMBER_RE.match(name)
    if not match:
        return None, name
    return match.group("number"), match.group("label")


def route_origins(schema):
    """Compiled route -> the registry row it came from, for drift reporting and fixes."""
    # The inbox is a constant, not a registry row, but it is still a route the
    # folders can drift from — so it carries the same fields the reporting reads.
    origins = {
        INBOX_DIR: {
            "kind": "inbox",
            "table": "Folder routing",
            "match_column": "Value",
            "value": INBOX_DIR,
            "number": 0,
            "label": split_folder_name(INBOX_DIR)[1],
            "definition": "",
        }
    }
    root = schema.get("sources_root")
    if root:
        origins[sources_root_folder(root)] = {
            "kind": "sources_root",
            "table": SOURCES_ROOT_SECTION,
            "match_column": "Label",
            "value": root["label"],
            "number": root["number"],
            "label": root["label"],
            "definition": root["definition"],
        }
        for value, path in source_kind_routes(schema).items():
            kind = schema["source_kinds"][value]
            origins[path] = {
                "kind": "source_kind",
                "table": "Source kinds",
                "match_column": "Value",
                "value": value,
                "number": kind["number"],
                "label": kind["label"],
                "definition": kind["definition"],
            }
    for value, domain in schema["domains"].items():
        base = domain_folder(domain)
        origins[base] = {
            "kind": "domain",
            "table": "Domains",
            "match_column": "Value",
            "value": value,
            "number": domain["number"],
            "label": domain["label"],
            "definition": domain["definition"],
        }
        for subdomain_value, subdomain in schema["subdomains"].get(value, {}).items():
            origins[f"{base}/{subdomain_folder(domain, subdomain)}"] = {
                "kind": "subdomain",
                "table": f"Subdomains/{value}",
                "match_column": "Value",
                "value": subdomain_value,
                "domain": value,
                "number": subdomain["number"],
                "label": subdomain["label"],
                "definition": subdomain["definition"],
            }
    for value, project in schema["projects"].items():
        domain = schema["domains"][project["domain"]]
        subdomain = schema["subdomains"][project["domain"]].get(project.get("subdomain", ""))
        parts = [domain_folder(domain)]
        if subdomain:
            parts.append(subdomain_folder(domain, subdomain))
        parts.append(project_folder(domain, subdomain, project))
        origins[Path(*parts).as_posix()] = {
            "kind": "project",
            "table": "Project registry",
            "match_column": "Approved value",
            "value": value,
            "domain": project["domain"],
            "subdomain": project.get("subdomain", ""),
            "number": project["number"],
            "label": project_name(project["value"]),
            "definition": project["definition"],
        }
    return origins


def compiled_routes(schema):
    """Every folder path the schema can compile to, vault-relative posix."""
    return sorted(route_origins(schema))


def existing_folders(vault):
    """Vault-relative posix paths of every folder the organizer would consider a destination.

    Uses the exclusions ``selected_notes`` uses, so a folder invisible to filing
    is invisible here too.
    """
    vault = Path(vault).resolve()
    folders = []
    for directory, dirnames, _ in os.walk(vault, followlinks=False):
        dirpath = Path(directory)
        kept = []
        for name in sorted(dirnames):
            child = dirpath / name
            if child.is_symlink() or name in PROTECTED_DIRS or name.startswith("."):
                continue
            if is_workspace_dir(child):
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in kept:
            folders.append((dirpath / name).relative_to(vault).as_posix())
    return sorted(folders)


def count_notes(path):
    """Markdown notes beneath ``path``, skipping the trees filing also skips."""
    if not path.is_dir():
        return 0
    total = 0
    for directory, dirnames, filenames in os.walk(path, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [
            name
            for name in dirnames
            if not (dirpath / name).is_symlink()
            and name not in PROTECTED_DIRS
            and not name.startswith(".")
            and not is_workspace_dir(dirpath / name)
        ]
        total += sum(
            1 for name in filenames if name.lower().endswith(".md") and not (dirpath / name).is_symlink()
        )
    return total


def ancestor_paths(path):
    """``"a/b/c"`` -> ``["a", "a/b"]``."""
    parts = path.split("/")
    return ["/".join(parts[:index]) for index in range(1, len(parts))]


def drift_finding_id(kind, path, route):
    """Content-derived so an id copied from a report cannot silently address a different finding."""
    return sha256_text(f"{kind}|{path}|{route}")[:8]


def leaf_number(number):
    """``"8.03"`` -> ``3``; ``"98"`` -> ``98``. None when the folder carries no number."""
    if not number:
        return None
    try:
        return int(number.rsplit(".", 1)[-1])
    except ValueError:
        return None


def sibling_numbers(schema, origin):
    """Every number already taken in the registry table ``origin`` belongs to."""
    if origin["kind"] == "domain":
        return {entry["number"] for entry in schema["domains"].values()}
    if origin["kind"] == "subdomain":
        return {entry["number"] for entry in schema["subdomains"].get(origin["domain"], {}).values()}
    if origin["kind"] == "project":
        return {
            entry["number"]
            for entry in schema["projects"].values()
            if entry["domain"] == origin["domain"] and entry.get("subdomain", "") == origin.get("subdomain", "")
        }
    if origin["kind"] == "source_kind":
        return {
            entry["number"]
            for entry in schema.get("source_kinds", {}).values()
            if isinstance(entry, dict) and entry.get("number")
        }
    return set()


def schema_side_fix(schema, origin, target_number):
    """The row edit that would make the schema agree with disk, or None if the registry cannot express it.

    A swap — the wanted number already belongs to another row — has no
    single-cell schema fix, so the folders are the only side that can move.
    """
    # The sources root has no sibling table to renumber within, so like the
    # inbox it is only ever fixed by hand.
    if origin["kind"] not in {"domain", "subdomain", "project", "source_kind"}:
        return None
    if target_number is None or not 1 <= target_number <= 99:
        return None
    if target_number == origin["number"]:
        return None
    if target_number in sibling_numbers(schema, origin) - {origin["number"]}:
        return None
    return {
        "table": origin["table"],
        "match_column": origin["match_column"],
        "value": origin["value"],
        "field": "Number",
        "from": origin["number"],
        "to": target_number,
    }


def cheapest_fix_side(existing_notes, route_notes, row):
    """Change whichever side holds less content; a rename moves notes, a row edit moves none."""
    if row is None:
        return "folder", "the registry cannot express this as a single row edit"
    if route_notes < existing_notes:
        return "schema", f"the folder holds {existing_notes} note(s) and the declared route holds {route_notes}"
    if existing_notes < route_notes:
        return "folder", f"the declared route holds {route_notes} note(s) and the folder holds {existing_notes}"
    if existing_notes == 0:
        return "schema", "both sides are empty, so renaming the folder instead is equally cheap"
    return "schema", f"both sides hold {existing_notes} note(s), so renaming the folder instead is equally cheap"


def rendered_row(origin, number):
    cells = [f"`{origin['value']}`", f"`{number}`"]
    if origin["kind"] != "project":
        cells.append(f"`{origin['label']}`")
    cells.append(origin["definition"])
    return "| " + " | ".join(cells) + " |"


def drift_suggestion(side, reason, origin, row, folder, route):
    if side == "schema":
        return (
            f"Change `{origin['value']}` in the {origin['table']} table from Number "
            f"`{row['from']}` to `{row['to']}` so the schema follows the folder — {reason}. "
            f"Resulting row: {rendered_row(origin, row['to'])}"
        )
    if side == "folder":
        return f"Rename `{folder}` to `{route}` so the folder follows the schema — {reason}."
    return (
        f"Renumber `{origin['value']}` in the {origin['table']} table to a free number, or move "
        f"`{folder}` — the schema already routes this number to `{origin['value']}`."
    )


def check_schema_drift(vault, schema):
    """Findings comparing compiled routes against folders on disk.

    Severity ranking is the point. A naive set difference over a real vault
    reports twenty differences of which three matter, and a checker that lists
    all twenty gets ignored — which is how a live collision survives unnoticed.
    Anything below a compiled route is legitimate detail, never a finding.
    """
    vault = Path(vault).resolve()
    origins = route_origins(schema)
    routes = set(origins)
    present = set(existing_folders(vault))

    siblings = {}
    for folder in sorted(present):
        parent, _, name = folder.rpartition("/")
        siblings.setdefault(parent, []).append((name, folder))

    findings = []
    paired = set()

    def sibling_where(parent, predicate, skip_paired):
        for name, folder in siblings.get(parent, []):
            if folder in routes or (skip_paired and folder in paired):
                continue
            if predicate(name):
                return folder
        return None

    for route in sorted(routes):
        origin = origins[route]
        parent, _, name = route.rpartition("/")
        number, label = split_folder_name(name)
        # The label may only be claimed once — two routes cannot both be "the
        # folder this got renamed from". Occupancy of a number is a property of
        # the disk, so it holds even for a folder already paired elsewhere.
        namesake = sibling_where(
            parent,
            lambda other: split_folder_name(other)[1] == label and split_folder_name(other)[0] != number,
            skip_paired=True,
        )
        # A route that exists already occupies its own number; a second folder
        # sharing it is caught below as an undeclared twin.
        occupant = (
            None
            if route in present
            else sibling_where(
                parent,
                lambda other: number is not None and split_folder_name(other)[0] == number,
                skip_paired=False,
            )
        )
        if namesake is None and occupant is None:
            if route in present:
                continue
            findings.append({
                "id": drift_finding_id("declared_absent", route, route),
                "severity": "info",
                "kind": "declared_absent",
                "path": route,
                "route": route,
                "note_count": 0,
                "detail": "Declared by the schema and not yet created. Filing the first note here creates it.",
                "suggestion": None,
                "fix_side": None,
                "schema_row": None,
            })
            continue
        target = namesake if namesake is not None else occupant
        paired.add(target)
        existing_notes = count_notes(vault / target)
        route_notes = count_notes(vault / route)
        if namesake is None:
            # The number is taken by something unrelated. Renaming it to this
            # route's label would be a guess, so neither side is automatic.
            row, side, reason = None, "manual", ""
        else:
            target_number, _ = split_folder_name(target.rpartition("/")[2])
            row = schema_side_fix(schema, origin, leaf_number(target_number))
            side, reason = cheapest_fix_side(existing_notes, route_notes, row)
        if occupant is not None:
            kind = "number_collision"
            detail = (
                f"The schema compiles `{origin['value']}` to `{route}`, but number `{number}` under "
                f"`{parent or 'the vault root'}` already belongs to `{occupant}`"
            )
            detail += (
                f", and the label `{label}` sits at `{target}`. Filing one note here creates a second "
                f"folder numbered `{number}`."
                if namesake is not None
                else ". Filing one note here creates a second folder with that number."
            )
        else:
            kind = "label_moved"
            detail = (
                f"The schema compiles `{origin['value']}` to `{route}`, but `{target}` holds that label "
                f"and {existing_notes} note(s). "
            )
            detail += (
                f"`{route}` already exists with {route_notes} note(s), so the split has happened and "
                f"`{origin['value']}` notes now live in two places."
                if route in present
                else f"The next `{origin['value']}` classification files into `{route}` instead, "
                f"splitting them across two folders."
            )
        findings.append({
            "id": drift_finding_id(kind, target, route),
            "severity": "high",
            "kind": kind,
            "path": target,
            "route": route,
            "note_count": existing_notes,
            "route_note_count": route_notes,
            "detail": detail,
            "suggestion": drift_suggestion(side, reason, origin, row, target, route),
            "fix_side": side,
            "schema_row": row if side == "schema" else None,
        })

    present_routes = routes & present
    reported = set()
    for folder in sorted(present):
        if folder in routes or folder in paired or folder in reported:
            continue
        parents = ancestor_paths(folder)
        if any(parent in paired or parent in reported for parent in parents):
            continue
        parent, _, name = folder.rpartition("/")
        number, _ = split_folder_name(name)
        # Detail below a declared route is legitimate — `Attachments/Images` is
        # not drift, and reporting it is what makes a checker get ignored. But a
        # folder carrying a Johnny Decimal number is claiming a slot, and an
        # undeclared slot beside declared ones is exactly how a vault splits.
        if number is None and any(ancestor in routes for ancestor in parents):
            continue
        reported.add(folder)
        twin = next(
            (
                route
                for route in sorted(present_routes)
                if number is not None
                and route.rpartition("/")[0] == parent
                and split_folder_name(route.rpartition("/")[2])[0] == number
            ),
            None,
        )
        count = count_notes(vault / folder)
        if twin is not None:
            findings.append({
                "id": drift_finding_id("number_collision", folder, twin),
                "severity": "high",
                "kind": "number_collision",
                "path": folder,
                "route": twin,
                "note_count": count,
                "detail": (
                    f"`{folder}` shares number `{number}` with the declared route `{twin}`. One number "
                    f"names two places, so nothing can route by number alone."
                ),
                "suggestion": (
                    f"Renumber or merge `{folder}`; the schema already routes `{number}` to `{twin}`. "
                    f"No schema row can fix this without moving notes."
                ),
                "fix_side": "manual",
                "schema_row": None,
            })
            continue
        findings.append({
            "id": drift_finding_id("undeclared_with_notes" if count else "undeclared_empty", folder, ""),
            "severity": "medium" if count else "low",
            "kind": "undeclared_with_notes" if count else "undeclared_empty",
            "path": folder,
            "route": None,
            "note_count": count,
            "detail": (
                f"No compiled route names `{folder}` and it holds {count} note(s). A whole-vault run "
                f"pulls them into scope and proposes refiling them out."
                if count
                else f"No compiled route names `{folder}` and it holds no notes."
            ),
            "suggestion": (
                f"Register `{folder}` in the schema, or move its notes into a declared route."
                if count
                else f"Register `{folder}` in the schema, or remove it."
            ),
            "fix_side": "manual",
            "schema_row": None,
        })

    return sorted(findings, key=lambda entry: (DRIFT_SEVERITY_ORDER[entry["severity"]], entry["path"]))


def drift_counts(findings):
    counts = {severity: 0 for severity in DRIFT_SEVERITY_ORDER}
    for finding in findings:
        counts[finding["severity"]] += 1
    return counts


def replace_schema_row_number(text, row):
    """Rewrite one registry row's Number cell and nothing else.

    Surgical by contract: the row is matched by its ``Value`` cell and every
    other byte of the note — including the row's own Definition prose and the
    cell's spacing and backtick style — survives unchanged.
    """
    lines = text.splitlines(keepends=True)
    plain = [line.rstrip("\r\n") for line in lines]
    if row["table"].startswith("Subdomains/"):
        domain = row["table"].split("/", 1)[1]
        outer_start, outer_end = section_bounds(plain, "Subdomains")
        start = None
        for index, level, title in iter_heading_lines(plain[outer_start + 1:outer_end]):
            position = outer_start + 1 + index
            if level == 3 and title == domain:
                start = position
            elif level == 3 and start is not None:
                outer_end = position
                break
        if start is None:
            raise UserError(f"schema has no Subdomains subsection for {domain}")
        bounds = (start, outer_end)
    else:
        bounds = section_bounds(plain, row["table"])

    positions = [index for index in range(bounds[0] + 1, bounds[1]) if plain[index].strip().startswith("|")]
    if len(positions) < 2:
        raise UserError(f"{row['table']}: no Markdown table to edit")
    header = split_markdown_table_row(plain[positions[0]])
    for column in (row["match_column"], row["field"]):
        if column not in header:
            raise UserError(f"{row['table']}: table has no {column} column")
    match_index = header.index(row["match_column"])
    field_index = header.index(row["field"])
    matches = [
        index
        for index in positions[2:]
        if not is_divider_row(split_markdown_table_row(plain[index]))
        and normalize_project_value(split_markdown_table_row(plain[index])[match_index]) == row["value"]
    ]
    if not matches:
        raise UserError(f"{row['table']}: no row with {row['match_column']} {row['value']}")
    if len(matches) > 1:
        raise UserError(f"{row['table']}: {row['value']} matches {len(matches)} rows")
    position = matches[0]
    parts = plain[position].split("|")
    cell = parts[field_index + 1]
    stripped = cell.strip()
    if strip_schema_value(stripped) != str(row["from"]):
        raise UserError(
            f"{row['table']}: {row['value']} has {row['field']} {strip_schema_value(stripped)}, expected {row['from']}"
        )
    lead = cell[: len(cell) - len(cell.lstrip())]
    trail = cell[len(cell.rstrip()):]
    rendered = f"`{row['to']}`" if stripped.startswith("`") and stripped.endswith("`") else str(row["to"])
    parts[field_index + 1] = f"{lead}{rendered}{trail}"
    ending = lines[position][len(plain[position]):]
    lines[position] = "|".join(parts) + ending
    return "".join(lines), plain[position], "|".join(parts)


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
        if is_inside_workspace(vault, candidate.resolve()):
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


def is_workspace_dir(path):
    """True when ``path`` is a marked pi-forge workspace, so its whole tree is skipped."""
    try:
        return (path / WORKSPACE_MARKER).is_file()
    except OSError:
        return False


def is_inside_workspace(vault, path):
    """True when any directory between ``vault`` and ``path`` is a marked workspace."""
    vault = vault.resolve()
    current = path.resolve().parent
    while True:
        if is_workspace_dir(current):
            return True
        if current == vault or current == current.parent:
            return False
        current = current.parent


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
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not (dirpath / name).is_symlink() and not is_workspace_dir(dirpath / name)
            ]
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
                if is_workspace_dir(child):
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


def parse_frontmatter(text):
    """Parse the inner lines of a YAML frontmatter block into {key: str | list}.

    Read-only and advisory: it feeds property lookup and already-linked
    filtering. Writes go through ``serialize_frontmatter`` or an additive text
    merge, so a parse miss can degrade a proposal but can never corrupt a note.
    """
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
