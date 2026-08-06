#!/usr/bin/env python3
"""Compose a note from material a run actually holds, and prove it stayed rooted.

Every note-writing path in forge is a closed pipeline: `vault-capture` splits one
braindump, `vault-connections import-run` renders four fixed sections, `vault-wiki`
substitutes into a template. Each hardcodes its own section set, its own
frontmatter keys, and its own destination, and none of them can make the note the
vault's own `0.04 Note Format.md` describes. This module is the open half --
`render_note` assembles a note in whatever order that note declares, and the
grounding checks below say whether what was written is allowed to have been.

The load-bearing idea is the **source set**. `vault-capture.invented_specifics`
holds a note for any capitalized token or URL with no root in *the braindump*, and
that is exactly right when there is one braindump. It is also why research notes,
notes synthesized from other notes, and notes made from a conversation cannot be
written at all: their content legitimately comes from somewhere else, so every
sentence reads as invention. Generalizing the *source* rather than weakening the
*check* keeps the property and widens the world -- a specific is grounded when some
unit the run is holding contains it, and a block may only draw on the units it
said it was drawing on.

Nothing here fetches anything. A unit's `text` is bytes some caller already read,
which is what makes the check meaningful: a fact the model merely recalls has no
unit, so it cannot ground, and a note asserting it is held.
"""

import hashlib
import json
import re
import unicodedata
from pathlib import Path

import vault_format
import vault_reflection
from vault_schema import UserError, serialize_frontmatter

SOURCE_SET_VERSION = 1

# What a unit of material is. The kind never changes how grounding works -- it is
# provenance, and it decides only how a citation reads and what an adapter built.
KIND_CHAT = "chat"
KIND_VAULT_NOTE = "vault-note"
KIND_WEB_CLAIM = "web-claim"
KIND_TRANSCRIPT = "transcript"
KIND_FILE = "file"
KIND_BRAINDUMP = "braindump"
SOURCE_KINDS = (KIND_CHAT, KIND_VAULT_NOTE, KIND_WEB_CLAIM, KIND_TRANSCRIPT, KIND_FILE, KIND_BRAINDUMP)

WORD_RE = re.compile(r"[a-z][a-z-]{2,}")
URL_RE = vault_reflection.URL_RE
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
NUMBER_RE = re.compile(r"\d+(?:[.,:/]\d+)*")
PROPER_NOUN_RE = re.compile(r"\b([A-Z][a-zA-Z]*(?:['’][a-zA-Z]+)?)\b")
SENTENCE_START_RE = re.compile(r"(?:^|[.!?:;]\s+|[\n\r]\s*|[-*]\s+|[\"“(\[]\s*)$")
HEADING_RE = re.compile(r"^(#+)\s+(.*)$", re.MULTILINE)
INLINE_HTML_RE = re.compile(r"<(span|div|font|style|br|p|b|i|u)\b[^>]*>", re.IGNORECASE)
LITERAL_COLOUR_RE = re.compile(r"(#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\()")

# Words that open a sentence in English and would otherwise read as names.
COMMON_CAPITALS = {
    "i", "a", "an", "the", "it", "its", "this", "that", "these", "those", "there", "then",
    "and", "but", "or", "so", "if", "when", "while", "because", "after", "before", "since",
    "we", "our", "you", "your", "they", "their", "he", "she", "his", "her", "him", "them",
    "my", "me", "mine", "us", "who", "what", "why", "how", "where", "which", "not", "no",
    "yes", "ok", "okay", "maybe", "also", "still", "just", "one", "two", "three", "some",
    "any", "all", "each", "every", "both", "for", "from", "with", "without", "about",
    "into", "onto", "over", "under", "again", "next", "last", "first", "second", "third",
    "today", "tomorrow", "yesterday", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december",
    "note", "notes", "task", "tasks", "idea", "ideas", "question", "questions", "summary",
    "background", "context", "details", "plan", "plans", "reference", "draft", "open",
    "todo", "to", "do", "of", "in", "on", "at", "by", "as", "is", "are", "was", "were",
    "have", "has", "had", "will", "would", "could", "should", "can", "may", "might",
    "need", "needs", "want", "wants", "think", "thinking", "thought", "thoughts",
}
# Spelled-out numbers, so "three weeks" in a source covers "3 weeks" in a draft.
NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
    "twelve": "12", "thirteen": "13", "fourteen": "14", "fifteen": "15",
    "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60",
    "seventy": "70", "eighty": "80", "ninety": "90", "hundred": "100",
    "thousand": "1000", "million": "1000000",
}

# A unit whose distinctive vocabulary reaches the note below this share was, in
# practice, not used. Deliberately low: a fragment that contributed one line to a
# day's log has genuinely been used, and the failure this catches is a unit that
# was dropped entirely, not one that was summarized hard.
DEFAULT_DROPPED_FLOOR = 0.15


# --------------------------------------------------------------------------- #
# The source set
# --------------------------------------------------------------------------- #


def _sha256_text(text):
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def source_unit(kind, label, text, origin=None, url=None, wikilink=None, occurred_at=None, unit_id=None):
    """One unit of material the run is holding, verbatim.

    ``text`` is the only field a grounding check ever reads. ``origin`` records
    where it came from for the provenance block and is deliberately never read by
    a check: a note does not become true because its source had a plausible path.
    """
    if kind not in SOURCE_KINDS:
        raise UserError(f"unknown source kind: {kind}; expected one of {', '.join(SOURCE_KINDS)}")
    body = str(text)
    if not body.strip():
        raise UserError(f"source unit '{label}' has no text; a unit that says nothing cannot ground anything")
    return {
        "id": unit_id,
        "kind": kind,
        "label": str(label),
        "text": body,
        "sha256": _sha256_text(body),
        "words": len(body.split()),
        "url": str(url) if url else None,
        "wikilink": str(wikilink) if wikilink else None,
        "occurred_at": str(occurred_at) if occurred_at else None,
        "origin": dict(origin or {}),
    }


def source_set(units):
    """The run's closed world, with ids assigned and a fingerprint over it.

    The fingerprint covers each unit's id, kind and content hash and nothing else,
    so relabelling a source or correcting its origin does not invalidate a
    resumable run, while changing what a unit *says* does.
    """
    assembled = []
    seen = set()
    for position, unit in enumerate(units or (), start=1):
        entry = dict(unit)
        if not entry.get("id"):
            entry["id"] = "s-%04d" % position
        if entry["id"] in seen:
            raise UserError(f"duplicate source id: {entry['id']}")
        seen.add(entry["id"])
        if entry.get("kind") not in SOURCE_KINDS:
            raise UserError(f"unknown source kind: {entry.get('kind')}")
        if not str(entry.get("text", "")).strip():
            raise UserError(f"source {entry['id']} has no text")
        assembled.append(entry)
    if not assembled:
        raise UserError("a compose run needs at least one source unit")
    material = json.dumps(
        [[entry["id"], entry["kind"], entry["sha256"]] for entry in assembled],
        sort_keys=True,
        separators=(",", ":"),
    )
    return {"version": SOURCE_SET_VERSION, "units": assembled, "fingerprint": _sha256_text(material)}


def units_by_id(sources, ids=None):
    """The named units, in set order. ``None`` means every unit."""
    if ids is None:
        return list(sources["units"])
    wanted = list(ids)
    known = {entry["id"]: entry for entry in sources["units"]}
    missing = [unit_id for unit_id in wanted if unit_id not in known]
    if missing:
        raise UserError(f"unknown source ids: {', '.join(missing)}")
    return [known[unit_id] for unit_id in wanted]


def set_text(sources, ids=None):
    """The grounding corpus for a block, or for the whole note."""
    return "\n\n".join(entry["text"] for entry in units_by_id(sources, ids))


def permitted_urls(sources, ids=None):
    return {entry["url"] for entry in units_by_id(sources, ids) if entry.get("url")}


def permitted_wikilinks(sources, ids=None):
    return {entry["wikilink"] for entry in units_by_id(sources, ids) if entry.get("wikilink")}


def dump_source_set(sources, path):
    Path(path).write_text(json.dumps(sources, indent=2, ensure_ascii=False), encoding="utf-8")


def load_source_set(path):
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    if loaded.get("version") != SOURCE_SET_VERSION:
        raise UserError(f"source set version {loaded.get('version')}, expected {SOURCE_SET_VERSION}")
    return loaded


# --------------------------------------------------------------------------- #
# Grounding
# --------------------------------------------------------------------------- #


def fold_diacritics(text):
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def content_words(text):
    """Comparable words.

    Apostrophes are dropped so ``members'`` and ``members`` are one token on both
    sides of a comparison, and diacritics are folded so a name survives being
    spelled properly -- a source saying "Jose" and a note writing "José" is a
    correction, not an invention. `vault-transcripts` learned the folding; capture
    never got it, and both read this now.
    """
    return WORD_RE.findall(fold_diacritics(text).casefold().replace("'", "").replace("’", ""))


def normalized_source(text):
    """The source as one comparable blob, with spelled-out numbers as digits."""
    lowered = fold_diacritics(text).casefold().replace("'", "").replace("’", "")
    for word, digits in NUMBER_WORDS.items():
        lowered = lowered.replace(word, f"{word} {digits}")
    return lowered


def capitalized_tokens(text):
    """Capitalized tokens, split by how much their position proves.

    Mid-sentence capitalization is nearly always a name. At the start of a
    sentence it is ambiguous -- an ordinary word gets a capital there too -- so
    the two are kept apart rather than pretending one rule fits both.
    """
    confident = []
    ambiguous = []
    for match in PROPER_NOUN_RE.finditer(str(text)):
        token = match.group(1)
        if token.casefold() in COMMON_CAPITALS or len(token) < 3:
            continue
        preceding = str(text)[max(0, match.start() - 24):match.start()]
        opener = bool(SENTENCE_START_RE.search(preceding)) and not token.isupper()
        (ambiguous if opener else confident).append(token)
    return confident, ambiguous


def strip_structure(markdown):
    """Prose only. Headings and list markers are structure the writer authors."""
    lines = []
    for line in str(markdown).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        stripped = re.sub(r"^[-*+]\s+", "", stripped)
        stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
        stripped = re.sub(r"^>\s*", "", stripped)
        lines.append(stripped)
    return "\n".join(lines)


def _rooted(token, source_lower, source_words):
    folded = fold_diacritics(token).casefold().replace("'", "").replace("’", "")
    if folded in source_lower or folded in source_words:
        return True
    # The plural of a rooted token is rooted. The stem rule below covers this for
    # ordinary words but not for short acronyms: a source saying "PC" and a note
    # saying "PCs" was reported as an invented name, because "pcs" is three
    # characters and the stem rule starts at four. Pluralizing a term the source
    # used is writing, not invention.
    if folded.endswith("s") and len(folded) >= 3:
        singular = folded[:-1]
        if singular in source_lower or singular in source_words:
            return True
    # A word the draft derived from one that was spoken ("order" -> "ordering")
    # shares a stem; a fabricated name shares nothing.
    return len(folded) >= 4 and any(folded[:length] in source_words for length in range(4, len(folded) + 1))


def ungrounded_specifics(sources, body, cited_ids=None, extra_urls=(), extra_wikilinks=()):
    """Specifics in a draft with no root in the run's source set.

    `vault-capture.invented_specifics` with "rooted in the braindump" replaced by
    "rooted in the units this block claims to rest on". Same test, plural sources.

    ``cited_ids`` narrows the corpus to the units a block said it was drawing on,
    so a section may not quietly borrow a name from a source it never cited; that
    is what stops a note whose sections are each individually plausible from being
    collectively a collage. ``None`` checks against the whole set, which is the
    note-level pass.

    Returns ``{"names", "uncertain_names", "links", "wikilinks", "numbers"}``.
    Only ``names``, ``links`` and ``wikilinks`` are strong enough to hold a note
    back; the rest are handed to the reviewer, which reads the sources anyway.
    """
    permitted_link = {str(url).rstrip(".,;:)").casefold() for url in permitted_urls(sources, cited_ids)}
    permitted_link.update(str(url).rstrip(".,;:)").casefold() for url in extra_urls or ())
    permitted_link_targets = {str(link).strip("[]").casefold() for link in permitted_wikilinks(sources, cited_ids)}
    permitted_link_targets.update(str(link).strip("[]").casefold() for link in extra_wikilinks or ())

    corpus = set_text(sources, cited_ids)
    source_lower = normalized_source(corpus)
    source_words = set(content_words(corpus))
    prose = strip_structure(body)

    confident, ambiguous = capitalized_tokens(prose)
    names = []
    uncertain = []
    for token in confident:
        if not _rooted(token, source_lower, source_words) and token not in names:
            names.append(token)
    for token in ambiguous:
        if not _rooted(token, source_lower, source_words) and token not in uncertain:
            uncertain.append(token)

    wikilinks = []
    for match in WIKILINK_RE.findall(body):
        target = str(match).split("|")[0].split("#")[0].strip()
        if target.casefold() in permitted_link_targets:
            continue
        if target not in wikilinks:
            wikilinks.append(target)

    return {
        "names": names,
        "uncertain_names": [token for token in uncertain if token not in names],
        "links": [
            url
            for url in URL_RE.findall(body)
            if url.rstrip(".,;:)").casefold() not in permitted_link
            and url.rstrip(".,;:").casefold() not in source_lower
        ],
        "wikilinks": wikilinks,
        "numbers": [number for number in NUMBER_RE.findall(prose) if number not in source_lower],
    }


def coverage_ratio(source, bodies):
    """How much of a source's distinctive vocabulary survived into the notes."""
    source_words = [word for word in content_words(source) if len(word) >= 5]
    if not source_words:
        return 1.0
    written = set()
    for body in bodies:
        written.update(content_words(body))
    kept = set()
    for word in source_words:
        if word in written or any(word[:length] in written for length in range(4, len(word))):
            kept.add(word)
    return len(kept) / len(set(source_words))


def dropped_units(sources, body, floor=DEFAULT_DROPPED_FLOOR, ids=None):
    """Units whose distinctive vocabulary did not survive into the note.

    `coverage_ratio` measures a corpus as a whole and is blind to one member of it
    being ignored entirely -- which is the failure fan-in actually produces. Six
    recordings merged into a day's log can score well overall while the quietest
    one contributed nothing, and a log that silently omits a recording is worse
    than no log, because nothing about it looks wrong.
    """
    dropped = []
    for unit in units_by_id(sources, ids):
        ratio = coverage_ratio(unit["text"], [body])
        if ratio < floor:
            dropped.append({"id": unit["id"], "label": unit["label"], "coverage": round(ratio, 3)})
    return dropped


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _callout_lines(content):
    if isinstance(content, dict):
        return content.get("title"), list(content.get("lines") or [])
    if isinstance(content, str):
        return None, content.splitlines()
    return None, list(content or [])


def _body_lines(content):
    """A body as written: either flat lines, or `##` sections with their lines."""
    if isinstance(content, str):
        return content.splitlines()
    lines = []
    for entry in content or ():
        if isinstance(entry, str):
            lines.append(entry)
            continue
        heading = str(entry.get("heading") or "").strip()
        if heading:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"## {heading}")
            lines.append("")
        lines.extend(entry.get("lines") or [])
    return lines


def render_note(fmt, schema, metadata, blocks, footnotes=(), strict=True):
    """One note's Markdown, assembled in the grammar's declared order.

    ``blocks`` maps a block name from `0.04 Note Format.md` to its content.
    Nothing else is emitted and nothing is reordered. A block the grammar does not
    declare, or one whose `Written by` is `owner`, raises rather than being
    written: a renderer that quietly tolerates an extra block is how `provenance`
    came to be a folded callout in one skill and a `##` heading in another.
    """
    declared = {entry["block"]: entry for entry in fmt.get("blocks", [])}
    if not declared:
        raise UserError(
            "the vault's note-format note declares no block order, so a note cannot be composed from it; "
            f"add a '### {vault_format.GRAMMAR_SUBSECTION}' table to {vault_format.DEFAULT_FORMAT}"
        )
    registry = fmt.get("callouts", {})
    unknown = [name for name in blocks if name not in declared]
    if unknown and strict:
        raise UserError(f"blocks not declared by the note format: {', '.join(sorted(unknown))}")
    for name in blocks:
        if declared.get(name, {}).get("written_by") == vault_format.WRITTEN_BY_OWNER:
            raise UserError(f"block '{name}' is owner-authored; a generator never writes it")

    if "title" not in blocks or not str(blocks["title"]).strip():
        raise UserError("a note needs a title")

    parts = []
    for name, entry in declared.items():
        if name == "frontmatter" or name not in blocks:
            continue
        content = blocks[name]
        if name == "title":
            parts.append(f"# {str(content).strip()}")
        elif name == "body":
            lines = _body_lines(content)
            if lines:
                parts.append("\n".join(lines).strip())
        elif name == "sources":
            bullets = [line if str(line).startswith("-") else f"- {line}" for line in content or ()]
            if bullets:
                parts.append("## Sources\n\n" + "\n".join(bullets))
        elif name in registry:
            title, lines = _callout_lines(content)
            if lines:
                parts.append(
                    vault_reflection.render_callout(name, title, lines, collapsed=registry[name]["folded"])
                )
        else:
            lines = _body_lines(content)
            if lines:
                parts.append("\n".join(lines).strip())

    text = serialize_frontmatter(metadata, schema) + "\n" + "\n\n".join(part for part in parts if part) + "\n"
    rendered_footnotes = [f"[^{marker}]: {value}" for marker, value in footnotes or ()]
    if rendered_footnotes:
        text += "\n" + "\n".join(rendered_footnotes) + "\n"
    return text


def without_code(text):
    """A note's prose, with fenced blocks and inline code spans blanked out.

    Every check below reads syntax, and a note that *documents* syntax is full of
    it: `0.04 Note Format.md` itself contains `# Title` inside its block-grammar
    fence and `` `<span style=` `` inside a prohibition, so checking it raw
    reports two level-one headings and inline HTML in the note that forbids both.
    Fences are blanked rather than deleted so byte offsets survive, which is what
    keeps the block-order comparison meaningful.
    """
    body = str(text)
    out = []
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```") or line.lstrip().startswith("~~~"):
            fenced = not fenced
            out.append("")
            continue
        out.append("" if fenced else re.sub(r"`[^`\n]*`", lambda match: " " * len(match.group(0)), line))
    return "\n".join(out)


def check_grammar(fmt, text):
    """Findings where a rendered note violates the grammar its vault declares.

    Returns ``(severity, message)`` pairs; an empty list is the passing state.
    `error` is a violation of something the note states as a rule, `warning` a
    shape the note calls a smell.
    """
    declared = {entry["block"]: entry for entry in fmt.get("blocks", [])}
    registry = fmt.get("callouts", {})
    findings = []
    body = without_code(text)

    titles = [match for match in HEADING_RE.finditer(body) if len(match.group(1)) == 1]
    if len(titles) != 1:
        findings.append(("error", f"a note has exactly one level-one heading; this has {len(titles)}"))

    for name in sorted(vault_format.callouts_used(body)):
        if name in registry or name in vault_format.STOCK_CALLOUTS or vault_format.is_namespaced(name):
            continue
        findings.append(("error", f"callout '{name}' is not in the vault's registry"))

    owner_blocks = [
        entry["syntax"] for entry in declared.values() if entry["written_by"] == vault_format.WRITTEN_BY_OWNER
    ]
    for syntax in owner_blocks:
        if re.search(r"^%s\s*$" % re.escape(syntax), body, re.MULTILINE):
            findings.append(("error", f"'{syntax}' is owner-authored: never written, never read"))

    positions = []
    for name, entry in declared.items():
        if name in ("frontmatter", "body"):
            continue
        found = _block_position(body, name, entry, registry)
        if found is not None:
            positions.append((found, name))
    ordered = [name for _, name in sorted(positions)]
    expected = [name for name in declared if name in set(ordered)]
    if ordered != expected:
        findings.append(
            ("error", f"blocks are out of the declared order: found [{', '.join(ordered)}], expected [{', '.join(expected)}]")
        )

    if INLINE_HTML_RE.search(body):
        findings.append(("error", "inline HTML: the stylesheet has uncontested control of appearance"))
    prose = "\n".join(line for line in body.splitlines() if not line.strip().startswith("["))
    if LITERAL_COLOUR_RE.search(prose):
        findings.append(("warning", "a literal colour: everything derives from theme variables"))

    used_callouts = [name for name in vault_format.callouts_used(body) if name in registry]
    if len(used_callouts) >= 3 and len(body.split()) < 400:
        findings.append(("warning", "three or more callouts in a short note reads as a list of fragments"))
    return findings


def _block_position(body, name, entry, registry):
    """Where a block starts in a rendered note, or ``None`` when it is absent."""
    if name in registry:
        pattern = r"^>\s*\[!%s\]" % re.escape(name)
    elif entry["syntax"].startswith("##"):
        pattern = r"^%s\s*$" % re.escape(entry["syntax"])
    elif name == "title":
        pattern = r"^#\s+\S"
    elif name == "footnotes":
        pattern = r"^\[\^[^\]]+\]:"
    else:
        return None
    match = re.search(pattern, str(body), re.MULTILINE)
    return match.start() if match else None


__all__ = [
    "COMMON_CAPITALS",
    "DEFAULT_DROPPED_FLOOR",
    "KIND_BRAINDUMP",
    "KIND_CHAT",
    "KIND_FILE",
    "KIND_TRANSCRIPT",
    "KIND_VAULT_NOTE",
    "KIND_WEB_CLAIM",
    "NUMBER_WORDS",
    "SOURCE_KINDS",
    "SOURCE_SET_VERSION",
    "capitalized_tokens",
    "check_grammar",
    "content_words",
    "coverage_ratio",
    "dropped_units",
    "dump_source_set",
    "fold_diacritics",
    "load_source_set",
    "normalized_source",
    "permitted_urls",
    "permitted_wikilinks",
    "render_note",
    "set_text",
    "source_set",
    "source_unit",
    "strip_structure",
    "ungrounded_specifics",
    "units_by_id",
    "without_code",
]
