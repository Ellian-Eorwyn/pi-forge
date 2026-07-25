#!/usr/bin/env python3
"""Shared parsing of a vault's voice-and-style note.

A vault organized by forge already keeps one human-maintained note that says how
its notes are *structured*. This module reads the companion note that says how
they are *written*: the person's own rules about voice, vocabulary, and what a
generated note must never do.

It exists for the same reason the schema parser does. Style guidance that lives
in a skill's prompt belongs to whoever wrote the skill; style guidance that lives
in the vault belongs to the person whose vault it is, and they can change it
without touching code.

Design rules, matching ``vault_schema``:

- Standard library only.
- Fail closed. A malformed section raises ``UserError`` rather than being
  silently skipped, because a half-read style note produces notes that are
  subtly wrong rather than obviously wrong.
- **Absent is not an error.** A vault with no voice note is a supported vault:
  ``resolve_voice_path`` returns ``None`` and every consumer writes notes the
  way it would have anyway.
- Parsing is deterministic and does not use a model.
- ``prompt_segment`` output is byte-stable for a given note and note type, so it
  can sit inside a system prompt without breaking the prefix cache.

Consumers: ``skills/vault-capture`` (drafting and the preferences loop) and
``skills/vault-connections`` (research note summaries).
"""

import hashlib
import json
import re
from pathlib import Path

from vault_schema import (
    INBOX_DIR,
    PROTECTED_DIRS,
    UserError,
    section_bounds,
    sha256_file,
    sha256_text,
    strip_inline_code,
    table_after,
)

DEFAULT_VOICE = "99 Meta/99.02 Schemas/0.01 Voice and Style.md"
VOICE_BASENAME = "0.01 Voice and Style.md"
COMPILED_VOICE_VERSION = 1

GLOBAL_SECTION = "Global voice"
PER_TYPE_SECTION = "Per-type style"
VOCABULARY_SECTION = "Vocabulary"
FORMATTING_SECTION = "Formatting"
NEVER_SECTION = "Never do"
KNOWN_SECTIONS = (GLOBAL_SECTION, PER_TYPE_SECTION, VOCABULARY_SECTION, FORMATTING_SECTION, NEVER_SECTION)

# A style note is guidance, not a specification. Past this it stops being read
# as rules and starts crowding out the material the model is working from.
DEFAULT_PROMPT_BUDGET = 2000
MAX_BULLET_CHARS = 300
BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def resolve_voice_path(vault, raw_voice=None):
    """The vault's voice note, or ``None`` when it does not have one.

    Unlike the schema note this is optional, so a missing file is an answer
    rather than a failure. An explicitly requested path that does not exist is
    still an error: the caller asked for a specific note.
    """
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
            continue  # an unfiled draft of the note is not the note
        matches.append(candidate.resolve())
    if len(matches) > 1:
        listed = ", ".join(str(path) for path in sorted(matches))
        raise UserError(f"more than one '{VOICE_BASENAME}' in the vault ({listed}); pass --voice")
    return matches[0] if matches else None


def _bullets(lines, heading):
    start, end = section_bounds(lines, heading)
    found = []
    for line in lines[start + 1:end]:
        match = BULLET_RE.match(line)
        if match:
            text = match.group(1).strip()
            if text:
                found.append(text[:MAX_BULLET_CHARS])
    if not found:
        raise UserError(f"{heading}: section has no bullets")
    return found


def _has_section(lines, heading):
    try:
        section_bounds(lines, heading)
    except UserError:
        return False
    return True


def parse_voice_note(text):
    """Parse a voice note into ``{global, per_type, vocabulary, formatting, never}``.

    Every section is optional; a note with none of them is not a voice note and
    is refused, because silently reading nothing out of a file the user wrote is
    worse than saying so.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    present = [heading for heading in KNOWN_SECTIONS if _has_section(lines, heading)]
    if not present:
        raise UserError(
            "voice note has none of its sections: " + ", ".join(f"## {heading}" for heading in KNOWN_SECTIONS)
        )
    voice = {"global": [], "per_type": {}, "vocabulary": [], "formatting": [], "never": []}
    if GLOBAL_SECTION in present:
        voice["global"] = _bullets(lines, GLOBAL_SECTION)
    if NEVER_SECTION in present:
        voice["never"] = _bullets(lines, NEVER_SECTION)
    if FORMATTING_SECTION in present:
        voice["formatting"] = _bullets(lines, FORMATTING_SECTION)
    if VOCABULARY_SECTION in present:
        voice["vocabulary"] = _bullets(lines, VOCABULARY_SECTION)
    if PER_TYPE_SECTION in present:
        for row in table_after(lines, PER_TYPE_SECTION, ["Type", "Style"]):
            note_type = strip_inline_code(row["Type"]).strip()
            style = row["Style"].strip()
            if not note_type:
                raise UserError(f"{PER_TYPE_SECTION}: a row has no type")
            if note_type in voice["per_type"]:
                raise UserError(f"{PER_TYPE_SECTION}: duplicate type {note_type}")
            if style:
                voice["per_type"][note_type] = style[:MAX_BULLET_CHARS]
    return voice


def compiled_voice_for(vault, voice_path, cache_dir=None):
    """Parse the voice note, caching the result against its SHA-256.

    Returns ``(voice, voice_hash)``, or ``(None, None)`` when the vault has no
    voice note.
    """
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
            pass  # a cache that cannot be written is not a reason to fail a run
    return voice, voice_hash


def prompt_segment(voice, note_type=None, budget=DEFAULT_PROMPT_BUDGET):
    """Render the voice note as a block for a system prompt.

    Priority order is what survives truncation: prohibitions first, because a
    rule about what never to do is worth more than a preference about how to
    phrase things; then the global voice; then the one style row that applies to
    this note; then vocabulary. Whole bullets are dropped rather than cut, so the
    model never reads half a rule.

    Deterministic for a given voice and type, which is what lets this sit in a
    byte-stable system prompt.
    """
    if not voice:
        return ""
    groups = []
    if voice.get("never"):
        groups.append(("Never, in any note:", list(voice["never"])))
    if voice.get("global"):
        groups.append(("How this person writes:", list(voice["global"])))
    style = voice.get("per_type", {}).get(note_type) if note_type else None
    if style:
        groups.append((f"For a `{note_type}` note:", [style]))
    if voice.get("vocabulary"):
        groups.append(("Vocabulary:", list(voice["vocabulary"])))

    header = "The owner of this vault keeps a note on how their notes should read. It is their instruction, not a suggestion, and it outranks your own style preferences. It never overrides the fidelity rules above."
    lines = [header]
    used = len(header)
    for title, bullets in groups:
        rendered = []
        for bullet in bullets:
            entry = f"- {bullet}"
            if used + len(title) + len(entry) + 2 > budget:
                break
            rendered.append(entry)
            used += len(entry) + 1
        if rendered:
            used += len(title) + 1
            lines.append("")
            lines.append(title)
            lines.extend(rendered)
    return "\n".join(lines) if len(lines) > 1 else ""


def formatting_rules(voice):
    """Formatting preferences, which are checked rather than prompted.

    Naming a formatting rule in a prompt makes a non-thinking model apply it
    everywhere it half-fits (``docs/service-split-handoff.md`` §7.3), so these
    stay out of ``prompt_segment`` and are for callers that can check them
    deterministically after the fact.
    """
    return list(voice.get("formatting", [])) if voice else []


def voice_fingerprint(voice_hash):
    """What a run records so a resumed run can refuse a changed voice note."""
    return voice_hash or "none"


def render_voice_note(voice):
    """Serialize a parsed voice note back to Markdown.

    Round-trips through ``parse_voice_note``, which is what lets the preferences
    loop propose an edit and prove the result still parses before offering it.
    """
    lines = ["# Voice and Style", ""]
    if voice.get("global"):
        lines.extend([f"## {GLOBAL_SECTION}", ""])
        lines.extend(f"- {bullet}" for bullet in voice["global"])
        lines.append("")
    if voice.get("per_type"):
        lines.extend([f"## {PER_TYPE_SECTION}", "", "| Type | Style |", "| --- | --- |"])
        lines.extend(f"| `{note_type}` | {style} |" for note_type, style in voice["per_type"].items())
        lines.append("")
    if voice.get("vocabulary"):
        lines.extend([f"## {VOCABULARY_SECTION}", ""])
        lines.extend(f"- {bullet}" for bullet in voice["vocabulary"])
        lines.append("")
    if voice.get("formatting"):
        lines.extend([f"## {FORMATTING_SECTION}", ""])
        lines.extend(f"- {bullet}" for bullet in voice["formatting"])
        lines.append("")
    if voice.get("never"):
        lines.extend([f"## {NEVER_SECTION}", ""])
        lines.extend(f"- {bullet}" for bullet in voice["never"])
        lines.append("")
    return "\n".join(lines)


def voice_digest(voice):
    """A stable hash of parsed content, for comparing two parses."""
    return hashlib.sha256(
        json.dumps(voice, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


__all__ = [
    "COMPILED_VOICE_VERSION",
    "DEFAULT_VOICE",
    "VOICE_BASENAME",
    "compiled_voice_for",
    "formatting_rules",
    "parse_voice_note",
    "prompt_segment",
    "render_voice_note",
    "resolve_voice_path",
    "sha256_text",
    "voice_digest",
    "voice_fingerprint",
]
