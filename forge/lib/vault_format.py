#!/usr/bin/env python3
"""Parse the vault-owned note-format policy, and check its implementations agree.

`99 Meta/99.02 Schemas/0.04 Note Format.md` declares the vault's callout registry:
which blocks exist, what accent and icon each carries, and which are folded. Three
things implement that registry -- the stylesheet that colours them, the code that
emits them, and the templates that use them -- and none of the three can see the
others. A callout added to one and forgotten in the rest renders as stock blue with
a pencil icon, which looks like a design choice rather than a mistake.

So unlike its siblings, this module does not compile a prompt prefix. `vault_voice`
and `vault_profile` exist to tell a model something; this one exists to prove four
files still say the same thing. `check_agreement` is the whole point, and the
parsing below is in service of it.

The registry is deliberately read from the note rather than from the stylesheet.
The note is the authority a person edits and a model reads; making the CSS
authoritative would put the vault's vocabulary somewhere neither of them looks.
"""

import re
from pathlib import Path

from vault_schema import INBOX_DIR, PROTECTED_DIRS, UserError, table_after

DEFAULT_FORMAT = "99 Meta/99.02 Schemas/0.04 Note Format.md"
FORMAT_BASENAME = "0.04 Note Format.md"

REGISTRY_SECTION = "Callout registry"
REGISTRY_COLUMNS = ("Callout", "Means", "Accent", "Icon", "Folded", "Not for")

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
    "DEFAULT_FORMAT",
    "FORMAT_BASENAME",
    "NAMESPACE_PREFIXES",
    "REGISTRY_COLUMNS",
    "REGISTRY_SECTION",
    "STOCK_CALLOUTS",
    "callouts_used",
    "canonical",
    "check_agreement",
    "is_namespaced",
    "load_and_check",
    "parse_format_note",
    "parse_stylesheet",
    "resolve_format_path",
]
