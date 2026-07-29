#!/usr/bin/env python3
"""Parse and compile a vault-owned voice-and-style policy."""

import hashlib
import json
import re
from pathlib import Path

from vault_schema import INBOX_DIR, PROTECTED_DIRS, UserError, section_bounds, sha256_file, sha256_text, strip_inline_code

DEFAULT_VOICE = "99 Meta/99.02 Schemas/0.01 Voice and Style.md"
VOICE_BASENAME = "0.01 Voice and Style.md"
COMPILED_VOICE_VERSION = 2

GLOBAL_SECTION = "Global voice"
PER_TYPE_SECTION = "Per-type style"
VOCABULARY_SECTION = "Vocabulary"
FORMATTING_SECTION = "Formatting"
NEVER_SECTION = "Never do"
KNOWN_SECTIONS = (GLOBAL_SECTION, PER_TYPE_SECTION, VOCABULARY_SECTION, FORMATTING_SECTION, NEVER_SECTION)
SECTION_KEYS = {
    GLOBAL_SECTION: "global",
    PER_TYPE_SECTION: "per_type",
    VOCABULARY_SECTION: "vocabulary",
    FORMATTING_SECTION: "formatting",
    NEVER_SECTION: "never",
}

CONTEXT_OWNER = "owner"
CONTEXT_SOURCE = "source"
CONTEXT_NONE = "none"
CONTEXT_MODES = (CONTEXT_OWNER, CONTEXT_SOURCE, CONTEXT_NONE)
SCOPE_UNIVERSAL = "universal"
SCOPE_OWNER = "owner-authored"
SCOPE_SOURCE = "source-derived"
KNOWN_SCOPES = (SCOPE_UNIVERSAL, SCOPE_OWNER, SCOPE_SOURCE)
SCOPE_TO_CONTEXT = {
    SCOPE_UNIVERSAL: {CONTEXT_OWNER, CONTEXT_SOURCE},
    SCOPE_OWNER: {CONTEXT_OWNER},
    SCOPE_SOURCE: {CONTEXT_SOURCE},
}

DEFAULT_PREFIX_BUDGET = 1800
DEFAULT_CONTEXT_BUDGET = 900
BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
SCOPE_RE = re.compile(r"^###\s+(.+?)\s*$")
SECTION_RE = re.compile(r"^##\s+(.+?)\s*$")


def resolve_voice_path(vault, raw_voice=None, disabled=False):
    """Return the selected voice note, or ``None`` when disabled or absent."""
    if disabled:
        return None
    vault = Path(vault)
    if raw_voice:
        path = Path(raw_voice).expanduser()
        if not path.is_absolute():
            path = vault / path
        if not path.is_file():
            raise UserError(f"voice note does not exist: {path}")
        return path.resolve()
    default = (vault / DEFAULT_VOICE).resolve()
    if default.is_file():
        return default
    matches = []
    for candidate in vault.rglob(VOICE_BASENAME):
        parts = candidate.resolve().relative_to(vault.resolve()).parts
        if any(part.startswith(".") or part in PROTECTED_DIRS for part in parts):
            continue
        if parts and parts[0] == INBOX_DIR:
            continue
        matches.append(candidate.resolve())
    if len(matches) > 1:
        listed = ", ".join(str(path) for path in sorted(matches))
        raise UserError(f"more than one '{VOICE_BASENAME}' in the vault ({listed}); pass --voice")
    return matches[0] if matches else None


def _section_lines(lines, heading):
    start, end = section_bounds(lines, heading)
    return start, end, lines[start + 1:end]


def _scope_name(raw):
    normalized = raw.strip().lower().replace("–", "-").replace("—", "-")
    return normalized if normalized in KNOWN_SCOPES else None


def _scoped_bullets(lines, heading):
    _start, _end, body = _section_lines(lines, heading)
    rules = []
    scopes = []
    unknown_scopes = []
    scope = SCOPE_UNIVERSAL
    for line in body:
        heading_match = SCOPE_RE.match(line)
        if heading_match:
            parsed_scope = _scope_name(heading_match.group(1))
            if parsed_scope:
                scope = parsed_scope
            else:
                scope = None
                unknown_scopes.append(heading_match.group(1).strip())
            continue
        match = BULLET_RE.match(line)
        if match and scope:
            text = match.group(1).strip()
            if text:
                rules.append(text)
                scopes.append(scope)
    if not rules and not unknown_scopes:
        raise UserError(f"{heading}: section has no bullets")
    return rules, scopes, unknown_scopes


def _split_table_row(line):
    raw = line.strip()
    if not raw.startswith("|") or not raw.endswith("|"):
        return None
    return [cell.strip() for cell in raw[1:-1].split("|")]


def _is_separator(cells):
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def _scoped_type_rows(lines):
    _start, _end, body = _section_lines(lines, PER_TYPE_SECTION)
    rows = []
    unknown_scopes = []
    scope = SCOPE_UNIVERSAL
    index = 0
    while index < len(body):
        heading_match = SCOPE_RE.match(body[index])
        if heading_match:
            parsed_scope = _scope_name(heading_match.group(1))
            if parsed_scope:
                scope = parsed_scope
            else:
                scope = None
                unknown_scopes.append(heading_match.group(1).strip())
            index += 1
            continue
        header = _split_table_row(body[index])
        if header and [cell.lower() for cell in header] == ["type", "style"]:
            separator = _split_table_row(body[index + 1]) if index + 1 < len(body) else None
            if not separator or not _is_separator(separator):
                raise UserError(f"{PER_TYPE_SECTION}: malformed table separator")
            index += 2
            while index < len(body):
                cells = _split_table_row(body[index])
                if not cells:
                    break
                if len(cells) != 2:
                    raise UserError(f"{PER_TYPE_SECTION}: malformed row")
                if scope:
                    rows.append((scope, strip_inline_code(cells[0]).strip(), cells[1].strip()))
                index += 1
            continue
        index += 1
    if not rows and not unknown_scopes:
        raise UserError(f"{PER_TYPE_SECTION}: expected a | Type | Style | table")
    return rows, unknown_scopes


def _has_section(lines, heading):
    try:
        section_bounds(lines, heading)
    except UserError:
        return False
    return True


def parse_voice_note(text):
    """Parse managed rules while retaining their optional scope metadata."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    present = [heading for heading in KNOWN_SECTIONS if _has_section(lines, heading)]
    if not present:
        raise UserError(
            "voice note has none of its sections: " + ", ".join(f"## {heading}" for heading in KNOWN_SECTIONS)
        )
    voice = {
        "global": [],
        "per_type": {},
        "vocabulary": [],
        "formatting": [],
        "never": [],
        "scope_map": {"global": [], "per_type": {}, "vocabulary": [], "formatting": [], "never": []},
        "recognized_scopes": [],
        "unknown_scopes": [],
    }
    recognized = set()
    unknown = set()
    for heading in (GLOBAL_SECTION, VOCABULARY_SECTION, FORMATTING_SECTION, NEVER_SECTION):
        if heading not in present:
            continue
        key = SECTION_KEYS[heading]
        rules, scopes, unknown_scopes = _scoped_bullets(lines, heading)
        voice[key] = rules
        voice["scope_map"][key] = scopes
        recognized.update(scopes)
        unknown.update(unknown_scopes)
    if PER_TYPE_SECTION in present:
        rows, unknown_scopes = _scoped_type_rows(lines)
        unknown.update(unknown_scopes)
        for scope, note_type, style in rows:
            if not note_type:
                raise UserError(f"{PER_TYPE_SECTION}: a row has no type")
            if note_type in voice["per_type"]:
                raise UserError(f"{PER_TYPE_SECTION}: duplicate type {note_type}")
            if style:
                voice["per_type"][note_type] = style
                voice["scope_map"]["per_type"][note_type] = scope
                recognized.add(scope)
    voice["recognized_scopes"] = [scope for scope in KNOWN_SCOPES if scope in recognized]
    voice["unknown_scopes"] = sorted(unknown)
    return voice


def compiled_voice_for(vault, voice_path, cache_dir=None):
    """Parse the policy, caching the complete compiled representation by hash."""
    if voice_path is None:
        return None, None
    voice_path = Path(voice_path)
    voice_hash = sha256_file(voice_path)
    cache_path = Path(cache_dir) / "compiled-voice.json" if cache_dir else None
    if cache_path and cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("version") == COMPILED_VOICE_VERSION and cached.get("voice_hash") == voice_hash:
                return cached["voice"], voice_hash
        except (OSError, json.JSONDecodeError, KeyError):
            pass
    voice = parse_voice_note(voice_path.read_text(encoding="utf-8"))
    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"version": COMPILED_VOICE_VERSION, "voice_hash": voice_hash, "voice": voice}, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
    return voice, voice_hash


def _applies(scope, context_mode):
    if context_mode not in CONTEXT_MODES:
        raise UserError(f"unknown voice context mode: {context_mode}")
    return context_mode in SCOPE_TO_CONTEXT.get(scope, set())


def _rules_for(voice, key, context_mode):
    if not voice or context_mode == CONTEXT_NONE:
        return []
    rules = voice.get(key, [])
    scopes = voice.get("scope_map", {}).get(key, [])
    if not scopes:
        scopes = [SCOPE_UNIVERSAL] * len(rules)
    return [rule for rule, scope in zip(rules, scopes) if _applies(scope, context_mode)]


def append_group(lines, used, title, rules, budget):
    """Append a titled bullet group to ``lines``, stopping at ``budget`` characters.

    Returns the running character count. Shared with the other vault-owned
    policy compilers, which face the same problem: a prompt segment assembled
    from a note the owner keeps extending has to stay inside a fixed budget,
    and dropping whole trailing bullets reads better than truncating one.
    """
    rendered = []
    group_cost = len(title) + 2
    for rule in rules:
        entry = f"- {rule}"
        if used + group_cost + sum(len(item) + 1 for item in rendered) + len(entry) + 1 > budget:
            break
        rendered.append(entry)
    if not rendered:
        return used
    lines.extend(["", title, *rendered])
    return used + group_cost + sum(len(item) + 1 for item in rendered)


def prompt_prefix(voice, context_mode=CONTEXT_OWNER, budget=DEFAULT_PREFIX_BUDGET):
    """Return the byte-stable context prefix, excluding per-item material."""
    if not voice or context_mode == CONTEXT_NONE:
        return ""
    header = (
        "The vault owner maintains the following writing policy. Apply only the rules selected for this "
        f"{context_mode} context. The policy never overrides source fidelity, preserved-text, or provenance rules."
    )
    lines = [header]
    used = len(header)
    used = append_group(lines, used, "Never:", _rules_for(voice, "never", context_mode), budget)
    used = append_group(lines, used, "Voice:", _rules_for(voice, "global", context_mode), budget)
    return "\n".join(lines) if len(lines) > 1 else ""


def _vocabulary_term(rule):
    return re.split(r"\s+[—–-]\s+", rule, maxsplit=1)[0].strip().lower()


def relevant_vocabulary(voice, material, context_mode=CONTEXT_OWNER):
    """Select vocabulary whose defined term occurs in the current material."""
    haystack = material.lower()
    selected = []
    for rule in _rules_for(voice, "vocabulary", context_mode):
        term = _vocabulary_term(rule)
        if term and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", haystack, re.IGNORECASE):
            selected.append(rule)
    return selected


def compile_voice(voice, context_mode, note_type=None, material="", prefix_budget=DEFAULT_PREFIX_BUDGET,
                  context_budget=DEFAULT_CONTEXT_BUDGET):
    """Compile stable prefix plus item-specific rules and verification rules."""
    if context_mode == CONTEXT_NONE or not voice:
        return {"prefix": "", "context": "", "per_type_rule": None, "vocabulary": [], "formatting": []}
    per_type_rule = voice.get("per_type", {}).get(note_type) if note_type else None
    per_type_scope = voice.get("scope_map", {}).get("per_type", {}).get(note_type, SCOPE_UNIVERSAL)
    if per_type_rule and not _applies(per_type_scope, context_mode):
        per_type_rule = None
    vocabulary = relevant_vocabulary(voice, material, context_mode)
    lines = []
    used = 0
    if per_type_rule:
        used = append_group(lines, used, f"For a `{note_type}` note:", [per_type_rule], context_budget)
    append_group(lines, used, "Relevant vocabulary:", vocabulary, context_budget)
    return {
        "prefix": prompt_prefix(voice, context_mode, prefix_budget),
        "context": "\n".join(lines).lstrip("\n"),
        "per_type_rule": per_type_rule,
        "vocabulary": vocabulary,
        "formatting": formatting_rules(voice, context_mode),
    }


def prompt_segment(voice, note_type=None, budget=DEFAULT_PREFIX_BUDGET, context_mode=CONTEXT_OWNER, material=""):
    """Compatibility wrapper returning the selected prefix and item rules."""
    compiled = compile_voice(
        voice,
        context_mode,
        note_type=note_type,
        material=material,
        prefix_budget=budget,
        context_budget=budget,
    )
    return "\n\n".join(part for part in (compiled["prefix"], compiled["context"]) if part)


def formatting_rules(voice, context_mode=CONTEXT_OWNER):
    """Return only formatting rules applicable to the selected context."""
    return _rules_for(voice, "formatting", context_mode)


def voice_fingerprint(voice_hash):
    return voice_hash or "none"


def voice_state(voice_path, voice_hash, context_mode):
    """Serializable policy identity used by resumable workflows."""
    return {
        "voice_path": str(voice_path) if voice_path else None,
        "voice_hash": voice_fingerprint(voice_hash),
        "voice_compiler_version": COMPILED_VOICE_VERSION,
        "voice_context_mode": context_mode,
    }


def _render_scoped_bullets(voice, key):
    rules = voice.get(key, [])
    scopes = voice.get("scope_map", {}).get(key, [])
    if not scopes:
        scopes = [SCOPE_UNIVERSAL] * len(rules)
    lines = []
    for scope in KNOWN_SCOPES:
        selected = [rule for rule, rule_scope in zip(rules, scopes) if rule_scope == scope]
        if selected:
            lines.extend([f"### {scope.title()}", "", *(f"- {rule}" for rule in selected), ""])
    return lines


def _render_managed_section(voice, heading):
    key = SECTION_KEYS[heading]
    lines = [f"## {heading}", ""]
    if key == "per_type":
        scope_map = voice.get("scope_map", {}).get("per_type", {})
        for scope in KNOWN_SCOPES:
            selected = [
                (note_type, style)
                for note_type, style in voice.get("per_type", {}).items()
                if scope_map.get(note_type, SCOPE_UNIVERSAL) == scope
            ]
            if selected:
                lines.extend([f"### {scope.title()}", "", "| Type | Style |", "| --- | --- |"])
                lines.extend(f"| `{note_type}` | {style} |" for note_type, style in selected)
                lines.append("")
    else:
        lines.extend(_render_scoped_bullets(voice, key))
    return "\n".join(lines).rstrip() + "\n"


def _unknown_scope_blocks(section_lines):
    blocks = []
    index = 1
    while index < len(section_lines):
        match = SCOPE_RE.match(section_lines[index].rstrip("\n"))
        if not match or _scope_name(match.group(1)):
            index += 1
            continue
        next_index = index + 1
        while next_index < len(section_lines):
            if SCOPE_RE.match(section_lines[next_index].rstrip("\n")):
                break
            next_index += 1
        blocks.append("".join(section_lines[index:next_index]).strip())
        index = next_index
    return [block for block in blocks if block]


def render_voice_note(voice, original_text=None):
    """Render managed sections while preserving frontmatter and unknown sections."""
    rendered = {heading: _render_managed_section(voice, heading) for heading in KNOWN_SECTIONS if voice.get(SECTION_KEYS[heading])}
    if original_text is None:
        sections = [rendered[heading].rstrip() for heading in KNOWN_SECTIONS if heading in rendered]
        return "# Voice and Style\n\n" + "\n\n".join(sections) + "\n"

    text = original_text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.splitlines(keepends=True)
    output = []
    index = 0
    replaced = set()
    while index < len(lines):
        match = SECTION_RE.match(lines[index].rstrip("\n"))
        if match and match.group(1).strip() in KNOWN_SECTIONS:
            heading = match.group(1).strip()
            next_index = index + 1
            while next_index < len(lines):
                next_match = SECTION_RE.match(lines[next_index].rstrip("\n"))
                if next_match:
                    break
                next_index += 1
            if heading in rendered:
                replacement = rendered[heading].rstrip()
                unknown_blocks = _unknown_scope_blocks(lines[index:next_index])
                if unknown_blocks:
                    replacement += "\n\n" + "\n\n".join(unknown_blocks)
                output.append(replacement + "\n")
                if next_index < len(lines):
                    output.append("\n")
            replaced.add(heading)
            index = next_index
            continue
        output.append(lines[index])
        index += 1
    missing = [heading for heading in KNOWN_SECTIONS if heading in rendered and heading not in replaced]
    if missing:
        if output and not output[-1].endswith("\n\n"):
            output.append("\n")
        output.append("\n".join(rendered[heading].rstrip() for heading in missing) + "\n")
    return "".join(output)


def voice_digest(voice):
    return hashlib.sha256(json.dumps(voice, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


__all__ = [
    "COMPILED_VOICE_VERSION",
    "CONTEXT_MODES",
    "CONTEXT_NONE",
    "CONTEXT_OWNER",
    "CONTEXT_SOURCE",
    "DEFAULT_VOICE",
    "KNOWN_SCOPES",
    "VOICE_BASENAME",
    "append_group",
    "compile_voice",
    "compiled_voice_for",
    "formatting_rules",
    "parse_voice_note",
    "prompt_prefix",
    "prompt_segment",
    "relevant_vocabulary",
    "render_voice_note",
    "resolve_voice_path",
    "sha256_text",
    "voice_digest",
    "voice_fingerprint",
    "voice_state",
]
