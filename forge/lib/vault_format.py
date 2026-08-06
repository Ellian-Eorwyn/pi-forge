#!/usr/bin/env python3
"""Parse the vault-owned note-format policy, and check its implementations agree.

`99 Meta/99.02 Schemas/0.04 Note Format.md` declares the vault's callout registry:
which blocks exist, what accent and icon each carries, and which are folded. Three
things implement that registry -- the stylesheet that colours them, the code that
emits them, and the templates that use them -- and none of the three can see the
others. A callout added to one and forgotten in the rest renders as stock blue with
a pencil icon, which looks like a design choice rather than a mistake.

The note also declares a *block grammar*: which blocks a note is assembled from,
in what order, and which of them a machine may write. That half is compiled into a
prompt prefix, like `vault_voice` and `vault_profile` do with theirs.

This module used to say it deliberately did not do that -- the registry was "a
contract rather than more prompt budget", because every generator emitted a fixed
section set and could be checked against the registry without ever being told
about it. That reasoning holds exactly as long as no model *chooses* which blocks
a note gets. A composer does choose, so the grammar has to reach the drafting call
as well as the check. The registry half is unchanged and still checked, never
prompted.

The registry is deliberately read from the note rather than from the stylesheet.
The note is the authority a person edits and a model reads; making the CSS
authoritative would put the vault's vocabulary somewhere neither of them looks.
"""

import json
import re
from pathlib import Path

from vault_schema import (
    INBOX_DIR,
    PROTECTED_DIRS,
    UserError,
    section_bounds,
    sha256_file,
    table_after,
)
from vault_voice import append_group

DEFAULT_FORMAT = "99 Meta/99.02 Schemas/0.04 Note Format.md"
FORMAT_BASENAME = "0.04 Note Format.md"
COMPILED_FORMAT_VERSION = 1

REGISTRY_SECTION = "Callout registry"
REGISTRY_COLUMNS = ("Callout", "Means", "Accent", "Icon", "Folded", "Not for")

GRAMMAR_SECTION = "Block grammar"
GRAMMAR_SUBSECTION = "Block order"
GRAMMAR_COLUMNS = ("Block", "Syntax", "Required", "Written by", "Means")
SHAPES_SECTION = "Per-type shapes"
SHAPES_COLUMNS = ("Type", "Shape")
NEVER_SECTION = "Never do"

# Who may put content in a block. `schema` means the block is serialized from
# frontmatter rather than authored at all; `owner` means a generator must leave it
# alone in both directions, which is what `## Notes` needs.
WRITTEN_BY_OWNER = "owner"
WRITTEN_BY_MACHINE = "machine"
WRITTEN_BY_EITHER = "either"
WRITTEN_BY_SCHEMA = "schema"
WRITTEN_BY = (WRITTEN_BY_OWNER, WRITTEN_BY_MACHINE, WRITTEN_BY_EITHER, WRITTEN_BY_SCHEMA)

# Sized so the shipped note's three groups all fit. `append_group` drops trailing
# bullets to stay inside the budget, and the groups are appended in order, so a
# budget that fits only the first two silently spends the whole prefix on the
# block list and never states a single prohibition.
DEFAULT_PREFIX_BUDGET = 2400

# Obsidian's own callout aliases, not a vault invention: `[!tldr]` and `[!summary]`
# are the same built-in, so the stylesheet names all of them to keep one accent
# across whichever word was actually typed. The registry lists only the name this
# vault writes, so agreement has to fold an alias back onto it.
ALIASES = {
    "abstract": "summary",
    "tldr": "summary",
    "help": "question",
    "faq": "question",
    "warning": "caution",
    "attention": "caution",
}

# Built-ins the vault leaves alone. Styling one of these would silently change how
# imported and hand-written notes render, so they are not registry candidates.
STOCK_CALLOUTS = frozenset(
    {"note", "info", "todo", "tip", "hint", "important", "success", "check", "done",
     "failure", "fail", "missing", "danger", "error", "bug", "example", "quote", "cite"}
)

# Namespaces a skill owns outright. `reviewer-2` writes comments *about* an
# article rather than content *in* a note, so its vocabulary is deliberately not
# the note vocabulary -- it borrowed the unprefixed built-ins once and put a
# criticism of an article's structure in the same cyan as a note's summary. A
# prefixed type is checked for internal consistency and exempted from the
# registry, because the registry describes what a note is made of.
NAMESPACE_PREFIXES = ("r2-",)


def is_namespaced(name):
    """Whether a callout belongs to a skill's own vocabulary."""
    return str(name).lower().startswith(NAMESPACE_PREFIXES)

CALLOUT_USE_RE = re.compile(r"^\s*>\s*\[!([a-z][a-z0-9-]*)\]", re.IGNORECASE | re.MULTILINE)
BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_CSS_BLOCK_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_CSS_SELECTOR_RE = re.compile(r'data-callout="([a-z0-9-]+)"')
_CSS_COLOR_RE = re.compile(r"--callout-color:\s*var\(\s*(--[a-z0-9-]+)\s*\)")
_CSS_ICON_RE = re.compile(r"--callout-icon:\s*([a-z0-9-]+)")
_ACCENT_VAR_RE = re.compile(r"`(--[a-z0-9-]+)`")


def canonical(name):
    """The registry name a callout type belongs to."""
    return ALIASES.get(name.lower(), name.lower())


def resolve_format_path(vault, raw_format=None, disabled=False):
    """Return the selected format note, or ``None`` when disabled or absent."""
    if disabled:
        return None
    vault = Path(vault)
    if raw_format:
        path = Path(raw_format).expanduser()
        if not path.is_absolute():
            path = vault / path
        if not path.is_file():
            raise UserError(f"format note does not exist: {path}")
        return path.resolve()
    default = (vault / DEFAULT_FORMAT).resolve()
    if default.is_file():
        return default
    matches = []
    for candidate in vault.rglob(FORMAT_BASENAME):
        parts = candidate.resolve().relative_to(vault.resolve()).parts
        if any(part.startswith(".") or part in PROTECTED_DIRS for part in parts):
            continue
        if parts and parts[0] == INBOX_DIR:
            continue
        matches.append(candidate.resolve())
    if len(matches) > 1:
        listed = ", ".join(str(path) for path in sorted(matches))
        raise UserError(f"more than one '{FORMAT_BASENAME}' in the vault ({listed}); pass --format")
    return matches[0] if matches else None


def parse_format_note(text):
    """The callout registry, as declared by the note.

    Fails closed. A malformed registry means nothing downstream knows what the
    vault's vocabulary is, and guessing produces a check that passes by accident.
    """
    rows = table_after(str(text).splitlines(), REGISTRY_SECTION, REGISTRY_COLUMNS)
    registry = {}
    for row in rows:
        name = row["Callout"].strip().strip("`").lower()
        if not name:
            raise UserError(f"{REGISTRY_SECTION}: a row has no callout name")
        if name in registry:
            raise UserError(f"{REGISTRY_SECTION}: '{name}' is listed twice")
        if name in ALIASES:
            raise UserError(
                f"{REGISTRY_SECTION}: '{name}' is an alias of '{ALIASES[name]}'; list the canonical name"
            )
        if name in STOCK_CALLOUTS:
            raise UserError(
                f"{REGISTRY_SECTION}: '{name}' is a stock Obsidian callout the vault leaves alone"
            )
        accent = _ACCENT_VAR_RE.search(row["Accent"])
        if not accent:
            raise UserError(f"{REGISTRY_SECTION}: '{name}' has no `--variable` in its Accent cell")
        icon = row["Icon"].strip().strip("`")
        if not icon.startswith("lucide-"):
            raise UserError(f"{REGISTRY_SECTION}: '{name}' has icon '{icon}', expected a lucide- name")
        folded = row["Folded"].strip().lower()
        if folded not in ("yes", "no"):
            raise UserError(f"{REGISTRY_SECTION}: '{name}' has Folded '{folded}', expected yes or no")
        registry[name] = {
            "callout": name,
            "means": row["Means"].strip(),
            "accent": accent.group(1),
            "icon": icon,
            "folded": folded == "yes",
            "not_for": row["Not for"].strip(),
        }
    return registry


def _fence_syntax_tokens(text):
    """The syntax column of the `## Block grammar` fence, in order.

    Each fence line is a syntax token, two or more spaces, then a description.
    Only the token is taken: the description is prose that may be reworded, while
    the token is the thing the table has to agree with.

    Deliberately not bounded with `section_bounds`. The fence's own content
    includes `# Title`, `## Sources` and `## Notes`, so a heading-aware scan ends
    the section three lines in, inside the block it was trying to read. Find the
    heading, then take the first fenced block after it and stop at its close.
    """
    lines = str(text).splitlines()
    heading = re.compile(r"^##\s+" + re.escape(GRAMMAR_SECTION) + r"\s*$")
    start = next((index for index, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return []
    tokens = []
    inside = False
    for line in lines[start + 1:]:
        if line.strip().startswith("```"):
            if inside:
                break
            inside = True
            continue
        if not inside:
            continue
        if not line.strip():
            continue
        tokens.append(re.split(r"\s{2,}", line.strip(), maxsplit=1)[0].strip())
    return tokens


def _wrapped_bullets(lines, heading, level=2):
    """Bullets from a section, with wrapped continuation lines folded back in.

    `vault_schema.optional_bullet_lines` reads one bullet per line, which is right
    for the voice note, whose rules are each written on a single long line. This
    note's prohibitions are hard-wrapped, so reading them line-wise yields
    "Never use inline HTML for styling. No `<span style=`, no `<div class=`. The CSS"
    -- a rule that stops mid-sentence, which is worse than no rule at all.
    """
    try:
        start, end = section_bounds(lines, heading, level)
    except UserError:
        return []
    bullets = []
    for line in lines[start + 1:end]:
        match = BULLET_RE.match(line)
        if match:
            bullets.append(match.group(1).strip())
        elif bullets and line.strip() and not line.strip().startswith("#"):
            bullets[-1] = f"{bullets[-1]} {line.strip()}"
    return bullets


def _has_section(lines, heading, level):
    """Whether the note declares a section at all.

    Absence and malformation are different findings here: a vault that has not
    adopted the block grammar is not a vault with a broken one. Told apart by
    looking for the heading rather than by matching the text of an exception,
    which is a contract nobody agreed to.
    """
    try:
        section_bounds(lines, heading, level)
    except UserError:
        return False
    return True


def parse_block_grammar(text):
    """The ordered blocks a note is assembled from, as declared by the note.

    Row order is block order: the table *is* the grammar, not a description of one.
    The fence above it is checked against the table's `Syntax` column, the same
    move the callout registry makes against the stylesheet -- two statements of the
    same thing in one file drift silently otherwise, and the fence is the half a
    person actually reads.

    Returns ``[]`` when the vault has not declared a block order. That is not a
    malformed note; it is a vault that has not adopted the grammar, which is the
    state every vault starts in. A malformed *declared* order still fails closed.
    """
    lines = str(text).splitlines()
    if not _has_section(lines, GRAMMAR_SUBSECTION, 3):
        return []
    rows = table_after(lines, GRAMMAR_SUBSECTION, GRAMMAR_COLUMNS, level=3)
    blocks = []
    seen = set()
    for row in rows:
        name = row["Block"].strip().strip("`").lower()
        if not name:
            raise UserError(f"{GRAMMAR_SUBSECTION}: a row has no block name")
        if name in seen:
            raise UserError(f"{GRAMMAR_SUBSECTION}: '{name}' is listed twice")
        seen.add(name)
        written_by = row["Written by"].strip().strip("`").lower()
        if written_by not in WRITTEN_BY:
            raise UserError(
                f"{GRAMMAR_SUBSECTION}: '{name}' has Written by '{written_by}', expected one of "
                + ", ".join(WRITTEN_BY)
            )
        required = row["Required"].strip().lower()
        if required not in ("yes", "no"):
            raise UserError(f"{GRAMMAR_SUBSECTION}: '{name}' has Required '{required}', expected yes or no")
        blocks.append(
            {
                "block": name,
                "syntax": row["Syntax"].strip().strip("`"),
                "required": required == "yes",
                "written_by": written_by,
                "means": row["Means"].strip(),
            }
        )
    fence = _fence_syntax_tokens(text)
    if fence:
        declared = [entry["syntax"] for entry in blocks]
        if fence != declared:
            raise UserError(
                f"{GRAMMAR_SECTION}: the fence and the {GRAMMAR_SUBSECTION} table disagree; "
                f"fence has [{', '.join(fence)}], table has [{', '.join(declared)}]"
            )
    return blocks


def parse_type_shapes(text):
    """How each note type arranges the blocks, keyed by the `type` property.

    A row may name more than one type (`concept / wiki card`), so the first
    backticked value is the key and the rest of the cell is kept as the label.
    """
    lines = str(text).splitlines()
    if not _has_section(lines, SHAPES_SECTION, 2):
        return {}
    rows = table_after(lines, SHAPES_SECTION, SHAPES_COLUMNS, level=2)
    shapes = {}
    for row in rows:
        label = row["Type"].strip()
        match = re.search(r"`([^`]+)`", label)
        name = (match.group(1) if match else label).strip().lower()
        if not name:
            raise UserError(f"{SHAPES_SECTION}: a row has no type")
        if name in shapes:
            raise UserError(f"{SHAPES_SECTION}: '{name}' is listed twice")
        shapes[name] = {"type": name, "label": label, "shape": row["Shape"].strip()}
    return shapes


def parse_format(text):
    """Everything the note declares: the callout registry, the grammar, the shapes."""
    return {
        "callouts": parse_format_note(text),
        "blocks": parse_block_grammar(text),
        "shapes": parse_type_shapes(text),
        "never": _wrapped_bullets(str(text).splitlines(), NEVER_SECTION, level=2),
    }


def compiled_format_for(vault, format_path, cache_dir=None):
    """Parse the policy, caching the complete compiled representation by hash."""
    if format_path is None:
        return None, None
    format_path = Path(format_path)
    format_hash = sha256_file(format_path)
    cache_path = Path(cache_dir) / "compiled-format.json" if cache_dir else None
    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == COMPILED_FORMAT_VERSION and cached.get("format_hash") == format_hash:
                return cached["format"], format_hash
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    compiled = parse_format(format_path.read_text(encoding="utf-8"))
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"version": COMPILED_FORMAT_VERSION, "format_hash": format_hash, "format": compiled},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
    return compiled, format_hash


def block_index(fmt):
    """``{block name: position}``, for checking that a note's blocks are in order."""
    return {entry["block"]: position for position, entry in enumerate(fmt.get("blocks", []))}


def writable_blocks(fmt):
    """Blocks a generator may put content in, in declared order."""
    return [
        entry
        for entry in fmt.get("blocks", [])
        if entry["written_by"] in (WRITTEN_BY_MACHINE, WRITTEN_BY_EITHER)
    ]


def format_state(format_path, format_hash):
    """Serializable policy identity used by resumable workflows."""
    return {
        "format_path": str(format_path) if format_path else None,
        "format_hash": format_hash,
        "format_compiler_version": COMPILED_FORMAT_VERSION,
    }


def prompt_prefix(fmt, note_type=None, budget=DEFAULT_PREFIX_BUDGET):
    """The block grammar as a context prefix, excluding per-item material.

    Only the blocks a generator may write are listed: telling a model about
    `## Notes` is how a model comes to write one. The registry's accents and icons
    are deliberately absent -- they are a rendering contract with the stylesheet,
    and a model that cannot write CSS has no use for them.
    """
    if not fmt or not fmt.get("blocks"):
        return ""
    lines = [
        "Note format policy for this vault. A note is assembled from the blocks below, "
        "in this order. Every block is optional except the title, and none is ever reordered. "
        "A note that needs only a title and three paragraphs is a finished note."
    ]
    used = len(lines[0])
    used = append_group(
        lines,
        used,
        "Blocks, in order:",
        [f"{entry['block']} ({entry['syntax']}) — {entry['means']}" for entry in writable_blocks(fmt)],
        budget,
    )
    shape = fmt.get("shapes", {}).get(str(note_type or "").lower())
    if shape:
        used = append_group(lines, used, f"Shape for `{shape['type']}`:", [shape["shape"]], budget)
    if fmt.get("never"):
        used = append_group(lines, used, "Never do:", fmt["never"], budget)
    return "\n".join(lines)


def parse_stylesheet(text):
    """Callout identities declared by loom-notes.css.

    Only blocks that set both an accent and an icon count. The stylesheet also has
    a block listing every registered callout to give them a shared border and
    background, and reading that as an identity would report each of them twice.
    """
    found = {}
    for selector, body in _CSS_BLOCK_RE.findall(str(text)):
        names = _CSS_SELECTOR_RE.findall(selector)
        if not names:
            continue
        color = _CSS_COLOR_RE.search(body)
        icon = _CSS_ICON_RE.search(body)
        if not color or not icon:
            continue
        for name in names:
            found[name] = {"accent": color.group(1), "icon": icon.group(1)}
    return found


def callouts_used(text):
    """Callout types a Markdown body actually writes, folded onto registry names."""
    return {canonical(name) for name in CALLOUT_USE_RE.findall(str(text))}


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def check_agreement(note_text, css_text, code_callouts, template_paths=()):
    """Findings where the registry and its implementations disagree.

    Returns a list of ``(severity, message)``. An empty list is the passing state.
    Severities are ``error`` for a divergence that renders wrong, and ``warning``
    for one that is merely untidy.
    """
    registry = parse_format_note(note_text)
    styled = parse_stylesheet(css_text)
    findings = []

    styled_canonical = {}
    for name, identity in styled.items():
        styled_canonical.setdefault(canonical(name), []).append((name, identity))

    for name, entry in sorted(registry.items()):
        variants = styled_canonical.get(name)
        if not variants:
            findings.append(("error", f"callout '{name}' is registered but has no identity in loom-notes.css"))
            continue
        for written, identity in variants:
            label = name if written == name else f"{name} (as '{written}')"
            if identity["accent"] != entry["accent"]:
                findings.append(
                    ("error", f"callout '{label}': registry says accent {entry['accent']}, "
                              f"stylesheet says {identity['accent']}")
                )
            if identity["icon"] != entry["icon"]:
                findings.append(
                    ("error", f"callout '{label}': registry says icon {entry['icon']}, "
                              f"stylesheet says {identity['icon']}")
                )

    for name in sorted(styled_canonical):
        if name in registry or is_namespaced(name):
            continue
        findings.append(("error", f"callout '{name}' is styled in loom-notes.css but not registered"))

    code = {canonical(name) for name in code_callouts}
    for name in sorted(code - set(registry)):
        findings.append(("error", f"callout '{name}' is emitted by vault_reflection but not registered"))
    for name in sorted(set(registry) - code):
        findings.append(("warning", f"callout '{name}' is registered but never emitted by vault_reflection"))

    for path in template_paths:
        used = callouts_used(_read(path))
        for name in sorted(used - set(registry) - STOCK_CALLOUTS):
            if is_namespaced(name):
                findings.append(("error", f"{Path(path).name}: uses '{name}', which belongs to a skill, not a note"))
                continue
            findings.append(("error", f"{Path(path).name}: uses unregistered callout '{name}'"))

    return findings


def load_and_check(vault, css_path=None, templates_dir=None, raw_format=None):
    """Run :func:`check_agreement` against a real vault.

    The stylesheet is read from the vault's own snippets directory rather than the
    repo copy, because the vault's is the one that renders.
    """
    vault = Path(vault)
    note_path = resolve_format_path(vault, raw_format=raw_format)
    if note_path is None:
        raise UserError(f"no format note found; expected {DEFAULT_FORMAT}")
    css_path = Path(css_path) if css_path else vault / ".obsidian" / "snippets" / "loom-notes.css"
    if not css_path.is_file():
        raise UserError(f"stylesheet does not exist: {css_path}")
    templates_dir = Path(templates_dir) if templates_dir else vault / "99 Meta" / "99.03 Templates"
    templates = sorted(templates_dir.glob("*.md")) if templates_dir.is_dir() else []

    import vault_reflection

    return check_agreement(
        _read(note_path),
        _read(css_path),
        vault_reflection.VAULT_CALLOUTS,
        templates,
    )


__all__ = [
    "ALIASES",
    "COMPILED_FORMAT_VERSION",
    "DEFAULT_FORMAT",
    "DEFAULT_PREFIX_BUDGET",
    "FORMAT_BASENAME",
    "GRAMMAR_COLUMNS",
    "GRAMMAR_SECTION",
    "GRAMMAR_SUBSECTION",
    "NAMESPACE_PREFIXES",
    "NEVER_SECTION",
    "REGISTRY_COLUMNS",
    "REGISTRY_SECTION",
    "SHAPES_COLUMNS",
    "SHAPES_SECTION",
    "STOCK_CALLOUTS",
    "WRITTEN_BY",
    "WRITTEN_BY_EITHER",
    "WRITTEN_BY_MACHINE",
    "WRITTEN_BY_OWNER",
    "WRITTEN_BY_SCHEMA",
    "block_index",
    "callouts_used",
    "canonical",
    "check_agreement",
    "compiled_format_for",
    "format_state",
    "is_namespaced",
    "load_and_check",
    "parse_block_grammar",
    "parse_format",
    "parse_format_note",
    "parse_stylesheet",
    "parse_type_shapes",
    "prompt_prefix",
    "resolve_format_path",
    "writable_blocks",
]
