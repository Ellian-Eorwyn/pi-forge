#!/usr/bin/env python3
"""Wiki entity vocabulary, templates, and managed-section merging.

The wiki layer is shared. ``vault-connections`` creates wiki notes from research
evidence; ``vault-wiki`` expands existing ones from canonical sources. Both need
the same kind table, the same template contract, and the same idea of which
headings a generator may touch, so all three live here rather than in either
script.

The merge below is the only place in pi-forge that writes into an existing
note's *body*, so it is arranged so that a bug narrows rather than widens the
damage. Sections are matched by their visible heading text, never by injected
markers, because a marker that drifts leaves a generator writing into the wrong
place. An existing section is never moved. And ``assert_only_managed_changed``
re-parses the result and refuses it unless every byte outside the managed
sections survived, which turns a merge bug into one skipped note instead of
overwritten prose.
"""

import json
import re
from pathlib import Path

from vault_schema import (
    UserError,
    compile_destination,
    parse_frontmatter,
    path_is_inside,
    sha256_file,
    split_frontmatter,
)

WIKI_DOMAIN = "wiki"
WIKI_KIND_SUBDOMAIN = {
    "concept": "concepts",
    "practice": "practices",
    "place": "places",
    "event": "events",
    "term": "terms",
    "work": "works",
    "figure": "figures",
    "animal": "animals",
    "plant": "plants",
    "fungus": "fungi",
}
# Ten kinds collapse into five note types: a practice and a term both file as
# `concept`, a figure files as `person`, and all three species kinds file as
# `organism`. Type therefore cannot identify a kind — resolve it from the
# subdomain (see ``kind_for_metadata``).
WIKI_KIND_TYPE = {
    "concept": "concept",
    "practice": "concept",
    "place": "place",
    "event": "event",
    "term": "concept",
    "work": "work",
    "figure": "person",
    "animal": "organism",
    "plant": "organism",
    "fungus": "organism",
}
WIKI_TEMPLATE_NAMES = {
    "concept": "Wiki Concept.md",
    "practice": "Wiki Practice.md",
    "place": "Wiki Place.md",
    "event": "Wiki Event.md",
    "term": "Wiki Term.md",
    "work": "Wiki Work.md",
    "figure": "Wiki Figure.md",
    "animal": "Wiki Animal.md",
    "plant": "Wiki Plant.md",
    "fungus": "Wiki Fungus.md",
}
# The kinds whose subject is a living thing. They share the phenology table and
# the rule that an edibility judgment is the owner's to write, so several checks
# want to ask "is this a species card" without listing three strings again.
SPECIES_KINDS = ("animal", "plant", "fungus")
# The five fields ``import-run`` renders. A template may declare more, but these
# must stay present so the research-import path keeps working unchanged.
WIKI_TEMPLATE_FIELDS = ("title", "summary", "evidence", "sources", "provenance")
WIKI_KINDS = tuple(WIKI_KIND_SUBDOMAIN)
SUBDOMAIN_WIKI_KIND = {value: key for key, value in WIKI_KIND_SUBDOMAIN.items()}
DEFAULT_WIKI_KINDS = ("concept", "term")

TEMPLATE_FRONTMATTER = {
    "type": "template",
    "status": "active",
    "domain": "meta",
    "subdomain": "templates",
    "capture_type": "manual",
}

LEAD_SECTION = "_lead"
FOOTNOTES_SECTION = "_footnotes"
PSEUDO_SECTIONS = (LEAD_SECTION, FOOTNOTES_SECTION)
FILL_MODES = ("lead", "prose", "bullets", "links", "footnotes", "table")
# Fill modes a model writes. ``links`` and ``footnotes`` are rendered from the
# note's own properties and from the citations a draft actually used, so asking
# a model for them would invite it to invent both.
DRAFTED_FILL_MODES = ("lead", "prose", "bullets", "table")

PLACEHOLDER_RE = re.compile(r"\{\{([^{}\r\n]+)\}\}")
H1_RE = re.compile(r"^#\s+\S")
H2_RE = re.compile(r"^##(?!#)\s*(.*?)\s*$")
FOOTNOTE_DEF_RE = re.compile(r"^\[\^[^\]\r\n]+\]:")
FOOTNOTE_REF_RE = re.compile(r"\[\^([^\]\r\n]+)\](?!:)")


class MergeError(UserError):
    """A merge would have changed something outside the managed sections."""


# --------------------------------------------------------------------------- #
# Routing
# --------------------------------------------------------------------------- #


def kind_for_metadata(metadata):
    """The wiki kind a note's frontmatter implies, or None.

    Keyed on subdomain because ``type`` cannot distinguish a term from a
    concept from a practice.
    """
    if metadata.get("domain") != WIKI_DOMAIN:
        return None
    return SUBDOMAIN_WIKI_KIND.get(metadata.get("subdomain"))


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


def wiki_kind_folder(schema, kind):
    subdomain = WIKI_KIND_SUBDOMAIN[kind]
    if subdomain not in schema["subdomains"].get(WIKI_DOMAIN, {}):
        raise UserError(f"the schema note has no '{WIKI_DOMAIN}/{subdomain}' subdomain")
    return compile_destination(schema, {"domain": WIKI_DOMAIN, "subdomain": subdomain})


# --------------------------------------------------------------------------- #
# Templates
# --------------------------------------------------------------------------- #


def template_folder(schema):
    if "meta" not in schema["domains"] or "templates" not in schema["subdomains"].get("meta", {}):
        raise UserError("the schema does not define the required meta/templates route")
    return compile_destination(schema, {"domain": "meta", "subdomain": "templates"})


def template_placeholders(body):
    return sorted(set(PLACEHOLDER_RE.findall(body)))


def inspect_wiki_template(vault, schema, kind, required_fields=WIKI_TEMPLATE_FIELDS, known_fields=None):
    """Validate one vault-owned template.

    ``required_fields`` must all be present; ``known_fields`` is the universe of
    placeholders allowed to appear. A caller that knows only the five import
    fields passes neither and keeps the original behaviour, where anything else
    in the body is refused. ``vault-wiki`` passes its kind's full set so a
    richer template validates for both callers.
    """
    allowed = set(known_fields if known_fields is not None else required_fields)
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
    for key, value in TEMPLATE_FRONTMATTER.items():
        if metadata.get(key) != value:
            result["errors"].append(f"{path} requires {key}: {value}")
    body = split["body"]
    for field in required_fields:
        if f"{{{{{field}}}}}" not in body:
            result["errors"].append(f"{path} is missing {{{{{field}}}}}")
    unknown = sorted(set(template_placeholders(body)) - allowed)
    if unknown:
        result["errors"].append(f"{path} has unknown placeholders: {', '.join(unknown)}")
    result["ok"] = not result["errors"]
    result["sha256"] = sha256_file(path)
    result["body"] = body
    return result


def require_wiki_templates(vault, schema, kinds, specs=None):
    """Every selected kind's template, or a refusal naming each problem."""
    templates = {}
    errors = []
    for kind in kinds:
        spec = (specs or {}).get(kind)
        problems = []
        if spec:
            result = inspect_wiki_template(
                vault,
                schema,
                kind,
                required_fields=spec["required_placeholders"],
                known_fields=spec["placeholders"],
            )
            if result["ok"]:
                problems = template_spec_drift(result["body"], spec, result["path"])
        else:
            result = inspect_wiki_template(vault, schema, kind)
        problems = list(result["errors"]) + problems
        if problems:
            errors.extend(problems)
        else:
            templates[kind] = result
    if errors:
        raise UserError("wiki template readiness failed: " + "; ".join(errors))
    return templates


def template_spec_drift(body, spec, path):
    """Disagreements between a template file and the kind spec that drives it.

    The spec decides which headings a generator owns and the template decides
    what a note looks like. Nothing enforces that they describe the same note,
    so this checks both directions: a managed heading the template omits would
    silently never be written, and a placeholder the spec does not declare would
    never be filled.
    """
    errors = []
    headings = {normalize_heading(match.group(1)) for match in (H2_RE.match(line) for line in body.splitlines()) if match}
    for section in spec["sections"]:
        if section["id"] in PSEUDO_SECTIONS:
            continue
        if normalize_heading(section["heading"]) not in headings:
            errors.append(f"{path} has no '## {section['heading']}' heading declared by the {spec['kind']} spec")
    declared = set(spec["placeholders"])
    for name in template_placeholders(body):
        if name not in declared:
            errors.append(f"{path} uses undeclared placeholder {{{{{name}}}}}")
    for name in declared:
        if f"{{{{{name}}}}}" not in body:
            errors.append(f"{path} never uses declared placeholder {{{{{name}}}}}")
    return errors


def strip_unfilled(body, keep_headings=()):
    """Drop placeholders the caller did not fill, and any heading they emptied.

    A renderer that knows only the five import fields leaves a kind's own
    placeholders behind; without this the note would ship literal
    ``{{key_works}}`` text. Removing the token empties its section, and an empty
    heading on a reference card is worse than no heading, so the heading goes
    with it.

    ``keep_headings`` survives being empty. Owner sections like ``## Notes`` are
    *meant* to arrive blank — the empty heading is the slot the owner writes
    into — so dropping them would delete the affordance.
    """
    stripped = PLACEHOLDER_RE.sub("", body) if PLACEHOLDER_RE.search(body) else body
    keep = {normalize_heading(value) for value in keep_headings}
    parsed = parse_sections(stripped)
    kept = [parsed["blocks"][0]]
    dropped = False
    for block in parsed["blocks"][1:]:
        if "".join(block["content"]).strip() or normalize_heading(block["heading"]) in keep:
            kept.append(block)
        else:
            dropped = True
    if stripped == body and not dropped:
        return body
    parsed["blocks"] = kept
    return assemble(parsed)


# --------------------------------------------------------------------------- #
# Kind specs
# --------------------------------------------------------------------------- #


def load_kind_specs(path):
    """Read and validate the per-kind section specification."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserError(f"could not read wiki kind specs from {path}: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("kinds"), dict):
        raise UserError(f"{path} must be an object with a 'kinds' object")
    specs = {}
    for kind in WIKI_KINDS:
        if kind not in raw["kinds"]:
            raise UserError(f"{path} has no spec for wiki kind '{kind}'")
        specs[kind] = _validate_kind_spec(kind, raw["kinds"][kind], path)
    return specs


def validate_proposed_kind_spec(kind, raw, path="<proposal>"):
    """Validate a spec for a kind the library does not know about yet.

    ``load_kind_specs`` only validates the registered kinds, because a spec for
    an unregistered one cannot be filed. A proposal has to be checked *before*
    the kind exists — registering it means editing ``WIKI_KIND_SUBDOMAIN``,
    ``WIKI_KIND_TYPE``, and ``WIKI_TEMPLATE_NAMES``, which is a code change and
    a human's to make — so this runs the same checks without the registry.
    """
    if not isinstance(raw, dict):
        raise UserError(f"{path}: {kind} spec must be an object")
    return _validate_kind_spec(kind, raw, path)


def _validate_kind_spec(kind, raw, path):
    sections = raw.get("sections")
    if not isinstance(sections, list) or not sections:
        raise UserError(f"{path}: {kind} needs a non-empty 'sections' list")
    seen_ids = set()
    seen_headings = {}
    validated = []
    for entry in sections:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            raise UserError(f"{path}: {kind} has a section without a string id")
        identifier = entry["id"]
        if identifier in seen_ids:
            raise UserError(f"{path}: {kind} declares section '{identifier}' twice")
        seen_ids.add(identifier)
        owner = bool(entry.get("owner"))
        if identifier not in PSEUDO_SECTIONS and not isinstance(entry.get("heading"), str):
            raise UserError(f"{path}: {kind} section '{identifier}' needs a heading")
        fill = entry.get("fill", "prose")
        if fill not in FILL_MODES:
            raise UserError(f"{path}: {kind} section '{identifier}' has unknown fill '{fill}'")
        columns = _validate_columns(kind, identifier, entry, fill, path)
        if owner and entry.get("placeholder"):
            raise UserError(f"{path}: {kind} owner section '{identifier}' must not name a placeholder")
        if not owner and not isinstance(entry.get("placeholder"), str):
            raise UserError(f"{path}: {kind} section '{identifier}' needs a placeholder")
        headings = [entry["heading"]] + list(entry.get("aliases") or []) if identifier not in PSEUDO_SECTIONS else []
        for heading in headings:
            normalized = normalize_heading(heading)
            if normalized in seen_headings:
                raise UserError(
                    f"{path}: {kind} maps heading '{heading}' to both "
                    f"'{seen_headings[normalized]}' and '{identifier}'"
                )
            seen_headings[normalized] = identifier
        validated.append(
            {
                "id": identifier,
                "heading": entry.get("heading"),
                "aliases": tuple(entry.get("aliases") or ()),
                "placeholder": entry.get("placeholder"),
                "fill": fill,
                "owner": owner,
                "optional": bool(entry.get("optional")),
                "max_bullets": entry.get("max_bullets"),
                "max_chars": entry.get("max_chars"),
                "columns": columns,
                "guidance": entry.get("guidance", ""),
            }
        )
    placeholders = [section["placeholder"] for section in validated if section["placeholder"]]
    placeholders.extend(name for name in WIKI_TEMPLATE_FIELDS if name not in placeholders)
    return {
        "kind": kind,
        # `.get` rather than `[]` so a proposed kind, which has no registered
        # template name yet, validates instead of raising KeyError.
        "template": WIKI_TEMPLATE_NAMES.get(kind, f"Wiki {kind.title()}.md"),
        "sections": validated,
        "placeholders": tuple(placeholders),
        "required_placeholders": tuple(WIKI_TEMPLATE_FIELDS),
        "max_managed_chars": raw.get("max_managed_chars", 1600),
        "lead_guidance": raw.get("lead_guidance", ""),
    }


def _validate_columns(kind, identifier, entry, fill, path):
    """The declared columns of a ``table`` section, or () for every other fill.

    A table is the one managed section whose content is structured rather than
    prose, and the columns are what make it readable by something other than a
    person. Declaring them here means the renderer, the drafting prompt, and the
    compiler that reads the table back all agree on one list.
    """
    raw = entry.get("columns")
    if fill != "table":
        if raw:
            raise UserError(f"{path}: {kind} section '{identifier}' declares columns but is not a table")
        return ()
    if not isinstance(raw, list) or not raw:
        raise UserError(f"{path}: {kind} table section '{identifier}' needs a non-empty 'columns' list")
    columns = []
    seen = set()
    for column in raw:
        if not isinstance(column, dict) or not isinstance(column.get("id"), str):
            raise UserError(f"{path}: {kind} section '{identifier}' has a column without a string id")
        column_id = column["id"]
        if not re.fullmatch(r"[a-z][a-z0-9_]*", column_id):
            raise UserError(f"{path}: {kind} section '{identifier}' column id must be snake_case: {column_id}")
        if column_id in seen:
            raise UserError(f"{path}: {kind} section '{identifier}' declares column '{column_id}' twice")
        seen.add(column_id)
        if not isinstance(column.get("heading"), str) or not column["heading"].strip():
            raise UserError(f"{path}: {kind} section '{identifier}' column '{column_id}' needs a heading")
        values = column.get("values")
        if values is not None and (not isinstance(values, list) or not all(isinstance(v, str) for v in values)):
            raise UserError(f"{path}: {kind} section '{identifier}' column '{column_id}' values must be strings")
        columns.append(
            {
                "id": column_id,
                "heading": column["heading"].strip(),
                "values": tuple(values) if values else (),
                "guidance": column.get("guidance", ""),
            }
        )
    return tuple(columns)


def render_table(rows, columns):
    """A managed table section as Markdown, or "" when there are no rows.

    An empty section is written as nothing rather than as a header with no body:
    a species with no phenology researched yet should read as a gap, not as a
    table asserting that it has no seasons.
    """
    if not rows:
        return ""
    lines = [
        "| " + " | ".join(column["heading"] for column in columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in rows:
        cells = []
        for column in columns:
            value = row.get(column["id"], "") if isinstance(row, dict) else ""
            # A literal pipe would end the cell early and silently reshape the row.
            cells.append(re.sub(r"\s+", " ", str(value or "").replace("|", "\\|")).strip())
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def parse_table(text, columns):
    """Read a managed table back into rows, ignoring the header and divider.

    Returns ``(rows, problems)``. A row with the wrong cell count is reported
    rather than padded, because guessing which column went missing is how a
    mating window becomes a birth window.
    """
    rows, problems = [], []
    headings = [column["heading"].strip().casefold() for column in columns]
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(set(cell) <= {"-", ":"} and cell for cell in cells):
            continue
        if [cell.casefold() for cell in cells] == headings:
            continue
        if len(cells) != len(columns):
            problems.append(f"row has {len(cells)} cells, expected {len(columns)}: {stripped}")
            continue
        rows.append({column["id"]: cell.replace("\\|", "|") for column, cell in zip(columns, cells)})
    return rows, problems


def section_by_id(spec, identifier):
    for section in spec["sections"]:
        if section["id"] == identifier:
            return section
    raise KeyError(identifier)


def normalize_heading(value):
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def resolve_section_id(heading, spec):
    """Which spec section a note's existing heading corresponds to, or None."""
    normalized = normalize_heading(heading)
    for section in spec["sections"]:
        if section["id"] in PSEUDO_SECTIONS:
            continue
        if normalized == normalize_heading(section["heading"]):
            return section["id"]
        if any(normalized == normalize_heading(alias) for alias in section["aliases"]):
            return section["id"]
    return None


# --------------------------------------------------------------------------- #
# Body parsing
# --------------------------------------------------------------------------- #


def newline_of(text):
    """The terminator a newly written line in this body should use."""
    return "\r\n" if "\r\n" in text else "\n"


def split_footnotes(lines):
    """Peel the trailing footnote-definition block off the end of a body.

    Obsidian hoists definitions into its own rendered block, so they belong at
    the very end of the file and are managed as a unit rather than as part of
    whichever section happens to precede them. Only a well-formed run of
    single-line definitions is claimed; anything unusual stays body, which is
    the conservative direction because unclaimed bytes are preserved.
    """
    start = len(lines)
    for index in range(len(lines) - 1, -1, -1):
        line = lines[index]
        if not line.strip():
            continue
        if not FOOTNOTE_DEF_RE.match(line):
            break
        start = index
    if start >= len(lines):
        return list(lines), []
    return list(lines[:start]), list(lines[start:])


def parse_sections(body):
    """Split a body into a preamble plus one block per level-two heading.

    A level-three or deeper heading stays inside its parent block, so replacing
    a managed section replaces its subsections along with it. The split is
    lossless: ``assemble(parse_sections(x)) == x``.
    """
    lines, footnotes = split_footnotes(body.splitlines(keepends=True))
    blocks = []
    current = {"heading": None, "heading_line": None, "content": []}
    for line in lines:
        match = H2_RE.match(line.rstrip("\r\n"))
        if match:
            blocks.append(current)
            current = {"heading": match.group(1).strip(), "heading_line": line, "content": []}
            continue
        current["content"].append(line)
    blocks.append(current)
    return {"blocks": blocks, "footnotes": footnotes, "ends_with_newline": body.endswith(("\n", "\r"))}


def find_section_text(body, heading, aliases=()):
    """The content of one level-two section, or None when the note has no such
    heading.

    Matching is on normalized heading text, the same rule the merge uses, so a
    reader and a writer of the same section always agree on which block it is.
    None and "" mean different things to a caller: a note with no Phenology
    heading has not been researched, where one with an empty heading has been
    and yielded nothing.
    """
    wanted = {normalize_heading(heading)} | {normalize_heading(alias) for alias in aliases}
    for block in parse_sections(body)["blocks"]:
        if block["heading"] is not None and normalize_heading(block["heading"]) in wanted:
            return "".join(block["content"])
    return None


def assemble(parsed):
    pieces = []
    for block in parsed["blocks"]:
        if block["heading_line"] is not None:
            pieces.append(block["heading_line"])
        pieces.extend(block["content"])
    pieces.extend(parsed["footnotes"])
    return "".join(pieces)


def split_preamble(content):
    """Separate the level-one title from the lead paragraph beneath it.

    Returns ``(title_lines, lead_lines)`` where the title keeps the blank lines
    that follow it, so replacing the lead never disturbs the spacing under the
    heading.
    """
    for index, line in enumerate(content):
        if H1_RE.match(line):
            cut = index + 1
            while cut < len(content) and not content[cut].strip():
                cut += 1
            return list(content[:cut]), list(content[cut:])
    return [], list(content)


def render_lines(text, newline):
    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return []
    return [line + newline for line in cleaned.split("\n")]


# --------------------------------------------------------------------------- #
# Managed-section merge
# --------------------------------------------------------------------------- #


def merge_sections(body, spec, filled):
    """Rewrite only the sections the spec declares managed.

    ``filled`` maps a section id to its rendered Markdown. A section absent from
    ``filled``, or mapped to None, is left exactly as it is — so a partial draft
    updates what it has and touches nothing else.

    Two rules keep this predictable on notes that already exist. A managed
    section that is present keeps its own heading line, so a note saying
    ``## Key Ideas`` where the spec says ``## Key Points`` is updated in place
    instead of growing a near-duplicate heading. And an existing section is
    never moved, only rewritten, because reordering a note the owner arranged is
    a change they did not ask for.

    One honest caveat on "preserved exactly": inserting a section has to be able
    to put a blank line in front of its heading, so the run of blank lines that
    *separates* two sections is not preserved. Everything else is — every
    non-blank line of an unmanaged section, and all whitespace inside it, comes
    through untouched, and ``assert_only_managed_changed`` enforces exactly that
    boundary.
    """
    newline = newline_of(body) if body else "\n"
    parsed = parse_sections(body)
    blocks = parsed["blocks"]
    title_lines, lead_lines = split_preamble(blocks[0]["content"])

    if LEAD_SECTION in filled and filled[LEAD_SECTION] is not None:
        rendered = render_lines(filled[LEAD_SECTION], newline)
        lead_lines = [*rendered, newline] if rendered else []
    blocks[0] = {"heading": None, "heading_line": None, "content": [*title_lines, *lead_lines]}

    order = [section["id"] for section in spec["sections"] if section["id"] not in PSEUDO_SECTIONS]
    for identifier in order:
        if identifier not in filled or filled[identifier] is None:
            continue
        section = section_by_id(spec, identifier)
        if section["owner"]:
            raise MergeError(f"section '{identifier}' is owner-authored and must never be written")
        rendered = render_lines(filled[identifier], newline)
        if not rendered:
            continue
        present = _present_indexes(blocks, spec)
        if identifier in present:
            index = present[identifier]
            blocks[index] = {
                "heading": blocks[index]["heading"],
                "heading_line": blocks[index]["heading_line"],
                "content": [newline, *rendered, newline],
            }
            continue
        index = _insertion_index(order, identifier, present, len(blocks))
        # A heading needs a blank line above it, and plenty of notes on disk end
        # a section without one.
        _end_with_one_blank(blocks[index - 1], newline)
        blocks.insert(
            index,
            {
                "heading": section["heading"],
                "heading_line": f"## {section['heading']}{newline}",
                "content": [newline, *rendered, newline],
            },
        )

    if FOOTNOTES_SECTION in filled and filled[FOOTNOTES_SECTION] is not None:
        rendered = render_lines(filled[FOOTNOTES_SECTION], newline)
        if rendered:
            _end_with_one_blank(blocks[-1], newline)
            parsed["footnotes"] = rendered
        else:
            parsed["footnotes"] = []

    parsed["blocks"] = blocks
    merged = assemble(parsed)
    merged = merged.rstrip("\r\n")
    if parsed["ends_with_newline"] or not body:
        merged += newline
    return merged


def _end_with_one_blank(block, newline):
    """Leave exactly one blank line at the end of a block's content.

    Only ever changes the blank run between two sections, which is the one piece
    of whitespace ``assert_only_managed_changed`` deliberately does not police.
    """
    content = block["content"]
    while content and not content[-1].strip():
        content.pop()
    if not content and block["heading_line"] is None:
        return
    if content and not content[-1].endswith(("\n", "\r")):
        # A file that ends without a newline leaves its last line unterminated,
        # so terminate it before adding the blank rather than merely closing it.
        content[-1] += newline
    content.append(newline)


def _present_indexes(blocks, spec):
    present = {}
    for index, block in enumerate(blocks):
        if block["heading_line"] is None:
            continue
        identifier = resolve_section_id(block["heading"], spec)
        if identifier is not None and identifier not in present:
            present[identifier] = index
    return present


def _insertion_index(order, identifier, present, fallback):
    """Where a missing managed section goes among the sections already there."""
    position = order.index(identifier)
    for candidate in reversed(order[:position]):
        if candidate in present:
            return present[candidate] + 1
    for candidate in order[position + 1:]:
        if candidate in present:
            return present[candidate]
    return fallback


def unmanaged_signature(body, spec):
    """Every byte of a body that a merge is forbidden to change.

    Trailing line terminators are trimmed from each part because the blank run
    between two sections is whitespace the merge legitimately owns — it cannot
    insert a heading otherwise. Trailing *spaces* survive the trim, so a
    deliberate Markdown hard break still counts as content.
    """
    parsed = parse_sections(body)
    title_lines, _ = split_preamble(parsed["blocks"][0]["content"])
    blocks = []
    for block in parsed["blocks"][1:]:
        identifier = resolve_section_id(block["heading"], spec)
        if identifier is not None and not section_by_id(spec, identifier)["owner"]:
            continue
        blocks.append((block["heading_line"], "".join(block["content"]).rstrip("\r\n")))
    return ("".join(title_lines).rstrip("\r\n"), tuple(blocks))


def assert_only_managed_changed(original, merged, spec):
    """Refuse a merge that changed anything outside the managed sections.

    The safety net under everything else here: cheap, exact, and it fails one
    note rather than the run, so a merge bug can never quietly rewrite prose the
    owner wrote. Compares the level-one title and every unmanaged section —
    including owner sections like ``## Notes`` — byte for byte and in order.
    """
    before = unmanaged_signature(original, spec)
    after = unmanaged_signature(merged, spec)
    if before == after:
        return
    if before[0] != after[0]:
        raise MergeError("merge changed the note title or the lines above the lead")
    kept = [heading for heading, _ in after[1]]
    lost = [heading.strip() for heading, _ in before[1] if heading not in kept]
    if lost:
        raise MergeError(f"merge dropped unmanaged section(s): {', '.join(lost)}")
    raise MergeError("merge changed content outside the managed sections")


def footnote_references(text):
    return [match.group(1) for match in FOOTNOTE_REF_RE.finditer(text or "")]


def footnote_definitions(text):
    labels = []
    for line in (text or "").splitlines():
        match = FOOTNOTE_DEF_RE.match(line)
        if match:
            labels.append(match.group(0)[2:-2])
    return labels
