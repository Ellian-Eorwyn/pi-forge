#!/usr/bin/env python3

"""Derive a note's date from the evidence older copies of it carry.

Deterministic, standard library only, no model and no network — the same
commitment ``vault_schema`` makes, and for the same reason: a date written into
a note is not a proposal a later run revisits, so it has to be reproducible from
the files alone.

The vocabulary is two independent axes. *Evidence* is how explicitly a file
states its own date; *match* is how sure we are that a given archive file is an
older copy of a given vault note. A backfill is only as trustworthy as the
weaker of the two, which is what ``confidence`` returns and what the apply gate
reads.
"""

import datetime
import re
from pathlib import Path

from vault_schema import (
    FRONTMATTER_KEY_RE,
    LIST_ITEM_RE,
    normalize_body_for_hash,
    note_title,
    sha256_bytes,
    sha256_text,
    split_frontmatter,
    yaml_scalar,
)


# Evidence tiers, most explicit first. ``explicit`` is a date a machine wrote
# somewhere structural — a filename, a path, a frontmatter key. ``stated`` is a
# date a person typed into the prose under a label. ``weak`` is everything we
# would only be guessing from.
EXPLICIT = "explicit"
STATED = "stated"
WEAK = "weak"
EVIDENCE_RANK = {EXPLICIT: 0, STATED: 1, WEAK: 2}

# Match tiers, strongest first. ``self`` is the vault note's own evidence: there
# is no matching step to get wrong, so it ranks with an identical body.
SELF = "self"
IDENTICAL = "identical"
NAMED = "named"
TITLED = "titled"
SIMILAR = "similar"
MATCH_RANK = {SELF: 0, IDENTICAL: 0, NAMED: 1, TITLED: 2, SIMILAR: 3}

# The two tiers that may be written without a human naming the note's id.
AUTO_EVIDENCE = frozenset({EXPLICIT})
AUTO_MATCH = frozenset({SELF, IDENTICAL, NAMED, TITLED})

# How far into a note we look for a stated date. A "Created:" line lives in the
# header if it lives anywhere; further down, a date is prose about the subject.
HEAD_LINES = 20

MIN_YEAR = 1970
MAX_YEAR = 2999

MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
MONTH_ALTERNATION = "|".join(sorted(MONTH_NAMES, key=len, reverse=True))

# YYYY-MM-DD, YYYY/MM/DD, YYYY.MM.DD. Unambiguous in every locale.
ISO_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?!\d)")

# YYYYMMDD standing alone, and the same eight digits leading Obsidian's
# unique-note id (YYYYMMDDHHmm / YYYYMMDDHHmmss).
COMPACT_DATE_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)")
ZETTEL_ID_RE = re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})\d{4,6}(?!\d)")

# DD-MM-YYYY or MM-DD-YYYY. Which one is unknowable from the string, so this is
# only ever read when exactly one of the two positions can be a month.
NUMERIC_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})(?!\d)")

TEXT_DATE_RE = re.compile(
    r"(?<!\w)(?:"
    r"(?P<day1>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<month1>" + MONTH_ALTERNATION + r")\.?,?\s+(?P<year1>\d{4})"
    r"|"
    r"(?P<month2>" + MONTH_ALTERNATION + r")\.?\s+(?P<day2>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year2>\d{4})"
    r")(?!\w)",
    re.IGNORECASE,
)

# Frontmatter keys that mean "when this note came into being", normalized to
# letters so ``Created``, ``date created``, ``date-created`` and ``createdAt``
# all land on the same name.
CREATION_KEYS = frozenset({"created", "datecreated", "creationdate", "createdat", "ctime", "date"})

# Deliberately lenient: an archive file's frontmatter was written by whatever
# tool made it, so it may carry capitals, spaces, and hyphens that the vault's
# own ``FRONTMATTER_KEY_RE`` rightly refuses.
LOOSE_KEY_RE = re.compile(r"^\s{0,3}([A-Za-z][A-Za-z0-9 _-]*?)\s*:\s*(.*)$")

# "Created: ...", "**Date:**  ...", "> created:: ..." — a labelled date in prose.
STATED_LABEL_RE = re.compile(
    r"^[\s>*_#-]*\**\s*(created|creation date|date created|date)\s*\**\s*::?\s*(.+?)\s*$",
    re.IGNORECASE,
)

# A daily-note backlink in the opening lines is the note saying where it was written.
DAILY_LINK_RE = re.compile(r"\[\[(\d{4})-(\d{1,2})-(\d{1,2})(?:\|[^\]]*)?\]\]")

# Trailing copy markers, so "Note (1)" and "Note copy 2" reduce to "note".
COPY_SUFFIX_RE = re.compile(r"(?:\s*\(\d+\)|\s+\d+|\s+copy(?:\s+\d+)?|\s+-\s+copy)+$", re.IGNORECASE)

# Leading and trailing date-ish tokens, stripped before two names are compared:
# the archive's habit of prefixing a date is exactly what must not block a match.
NAME_DATE_EDGE_RE = re.compile(
    r"^(?:\d{4}[-_./]\d{1,2}[-_./]\d{1,2}|\d{8,14}|\d{1,2}[-_./]\d{1,2}[-_./]\d{4})[\s._-]*"
    r"|[\s._-]*(?:\d{4}[-_./]\d{1,2}[-_./]\d{1,2}|\d{8,14})$"
)


def valid_date(year, month, day):
    """A ``datetime.date``, or None when the numbers are not one."""
    if not (MIN_YEAR <= year <= MAX_YEAR):
        return None
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def parse_numeric_dmy(first, second, year):
    """Read ``11-04-2023`` only when one reading is impossible.

    Day-first and month-first are both in live use and nothing in the string
    says which. Where both readings are dates we drop the candidate rather than
    pick one: a silently wrong date is worse than a note left for review.
    """
    if first > 12 and second <= 12:
        return valid_date(year, second, first)
    if second > 12 and first <= 12:
        return valid_date(year, first, second)
    return None


def dates_in_text(text):
    """Every unambiguous date in ``text``, as ``(date, matched substring)``."""
    found = []
    for match in ZETTEL_ID_RE.finditer(text):
        value = valid_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if value:
            found.append((value, match.group(0)))
    for match in ISO_DATE_RE.finditer(text):
        value = valid_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if value:
            found.append((value, match.group(0)))
    for match in COMPACT_DATE_RE.finditer(text):
        value = valid_date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if value:
            found.append((value, match.group(0)))
    for match in NUMERIC_DMY_RE.finditer(text):
        value = parse_numeric_dmy(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if value:
            found.append((value, match.group(0)))
    for match in TEXT_DATE_RE.finditer(text):
        if match.group("day1"):
            day, month, year = match.group("day1"), match.group("month1"), match.group("year1")
        else:
            day, month, year = match.group("day2"), match.group("month2"), match.group("year2")
        value = valid_date(int(year), MONTH_NAMES[month.lower()], int(day))
        if value:
            found.append((value, match.group(0)))
    return found


def first_date_in(text):
    """The earliest-positioned unambiguous date in ``text``, or None."""
    found = dates_in_text(text)
    return found[0] if found else None


def candidate(value, tier, source, quote):
    return {"date": value.isoformat(), "tier": tier, "source": source, "quote": quote.strip()[:120]}


def loose_frontmatter_pairs(frontmatter_text):
    """``(normalized key, raw value)`` for every scalar line in a frontmatter block."""
    pairs = []
    for line in frontmatter_text.splitlines():
        if LIST_ITEM_RE.match(line):
            continue
        match = LOOSE_KEY_RE.match(line)
        if not match:
            continue
        key = re.sub(r"[^a-z]", "", match.group(1).lower())
        value = match.group(2).strip()
        if value:
            pairs.append((key, value))
    return pairs


def frontmatter_evidence(frontmatter_text):
    """Dates under a creation-ish frontmatter key."""
    candidates = []
    for key, raw in loose_frontmatter_pairs(frontmatter_text):
        if key not in CREATION_KEYS:
            continue
        found = first_date_in(raw)
        if found:
            candidates.append(candidate(found[0], EXPLICIT, "frontmatter:" + key, raw))
    return candidates


def filename_evidence(path):
    """A date carried by the file's own name."""
    stem = Path(path).stem
    found = first_date_in(stem)
    return [candidate(found[0], EXPLICIT, "filename", found[1])] if found else []


def path_evidence(relative):
    """A date spelled by a daily-note folder layout: ``2023/04/11.md``.

    Only read when the folders supply year and month and the filename is a bare
    day, which is the one shape where the path means a date rather than merely
    containing numbers.
    """
    parts = Path(relative).parts
    if len(parts) < 3:
        return []
    stem = Path(parts[-1]).stem.strip()
    if not re.fullmatch(r"\d{1,2}", stem):
        return []
    year = month = None
    for part in parts[:-1]:
        head = part.strip()
        if year is None and re.fullmatch(r"\d{4}", head):
            year = int(head)
            continue
        if year is not None and month is None:
            numeric = re.match(r"^(\d{1,2})(?!\d)", head)
            if numeric and 1 <= int(numeric.group(1)) <= 12:
                month = int(numeric.group(1))
                continue
            name = re.sub(r"[^a-z]", "", head.lower())
            if name in MONTH_NAMES:
                month = MONTH_NAMES[name]
    if year is None or month is None:
        return []
    value = valid_date(year, month, int(stem))
    return [candidate(value, EXPLICIT, "daily-note path", str(relative))] if value else []


def body_evidence(body):
    """A labelled date in the note's opening lines, then any date at all."""
    candidates = []
    head = []
    for line in body.splitlines():
        if line.strip():
            head.append(line)
        if len(head) >= HEAD_LINES:
            break
    for line in head:
        label = STATED_LABEL_RE.match(line)
        if label:
            found = first_date_in(label.group(2))
            if found:
                candidates.append(candidate(found[0], STATED, "body:" + label.group(1).lower(), line))
                continue
        for link in DAILY_LINK_RE.finditer(line):
            value = valid_date(int(link.group(1)), int(link.group(2)), int(link.group(3)))
            if value:
                candidates.append(candidate(value, STATED, "daily-note link", link.group(0)))
    if not candidates:
        found = first_date_in(body)
        if found:
            candidates.append(candidate(found[0], WEAK, "body text", found[1]))
    return candidates


def mtime_evidence(path):
    """The filesystem's timestamp, which is ``weak`` and stays that way.

    Copying a folder rewrites mtime, so on a reorganized archive this records
    the day of the reorganization. It is here to break ties in a report, never
    to be written.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return []
    stamp = min(stat.st_mtime, getattr(stat, "st_birthtime", stat.st_mtime))
    value = datetime.datetime.fromtimestamp(stamp).date()
    if not (MIN_YEAR <= value.year <= MAX_YEAR):
        return []
    return [candidate(value, WEAK, "filesystem mtime", value.isoformat())]


def extract_dates(path, relative, frontmatter_text, body, include_mtime=False):
    """Every date candidate a single file offers, best evidence first.

    Duplicates on (date, tier) collapse to the first source that found them, so
    a filename and a frontmatter key agreeing counts once rather than twice.
    """
    candidates = []
    candidates.extend(frontmatter_evidence(frontmatter_text))
    candidates.extend(filename_evidence(path))
    candidates.extend(path_evidence(relative))
    candidates.extend(body_evidence(body))
    if include_mtime:
        candidates.extend(mtime_evidence(path))
    seen = set()
    unique = []
    for entry in candidates:
        key = (entry["date"], entry["tier"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    unique.sort(key=lambda entry: (EVIDENCE_RANK[entry["tier"]], entry["date"]))
    return unique


def self_contradicts(candidates):
    """Whether one file's own explicit evidence disagrees with itself.

    Two archive copies carrying different dates is not a contradiction — the
    older one is the creation, and that is the rule. One *file* whose filename
    and frontmatter disagree is a contradiction, because only one of them can be
    describing this file.
    """
    explicit = {entry["date"] for entry in candidates if entry["tier"] == EXPLICIT}
    return len(explicit) > 1


def match_stem(name):
    """A filename reduced to what two copies of one note would share."""
    stem = Path(name).stem.casefold().strip()
    stem = COPY_SUFFIX_RE.sub("", stem)
    stem = NAME_DATE_EDGE_RE.sub("", stem)
    stem = re.sub(r"[\s_]+", " ", stem).strip(" -._")
    return stem


def note_fingerprint(path, relative, data, include_mtime=False):
    """Everything the matcher and the extractor need from one file, read once."""
    split = split_frontmatter(data)
    body = split["body"]
    normalized = normalize_body_for_hash(body)
    title = note_title(Path(path), body)
    return {
        "path": str(path),
        "relative": relative,
        "malformed": split["malformed"],
        "had_frontmatter": split["had_frontmatter"],
        "frontmatter_text": split["frontmatter_text"],
        # The bytes as planned against, so apply can refuse a note edited since.
        "data_hash": sha256_bytes(data),
        "body_hash": sha256_text(normalized) if normalized else "",
        "normalized": normalized,
        "title": title.casefold().strip(),
        "stem": match_stem(path),
        "candidates": extract_dates(path, relative, split["frontmatter_text"], body, include_mtime),
    }


def unique_by(entries, key):
    """Values of ``key`` that exactly one entry holds, mapped to that entry."""
    seen = {}
    for entry in entries:
        value = entry[key]
        if not value:
            continue
        seen.setdefault(value, []).append(entry)
    return {value: found[0] for value, found in seen.items() if len(found) == 1}


def value_counts(entries, key):
    counts = {}
    for entry in entries:
        value = entry[key]
        if value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def build_indexes(archive, notes):
    """Lookup tables for the deterministic match tiers.

    Name and title matching needs uniqueness on *both* sides, so the vault's own
    counts are part of the index: an archive holding one file called "Notes" is
    still no help to a vault holding four.
    """
    by_hash = {}
    for entry in archive:
        if entry["body_hash"]:
            by_hash.setdefault(entry["body_hash"], []).append(entry)
    return {
        "by_hash": by_hash,
        "by_stem": unique_by(archive, "stem"),
        "by_title": unique_by(archive, "title"),
        "vault_stems": value_counts(notes, "stem"),
        "vault_titles": value_counts(notes, "title"),
    }


def match_archive(note, indexes):
    """Archive fingerprints that are older copies of ``note``, as (entry, tier).

    Hash matches are collected in full — several archive copies of one note is
    the normal case, and the earliest of them is the answer. A file already
    claimed by a stronger tier is not offered again by a weaker one.
    """
    matches = []
    claimed = set()
    for entry in indexes["by_hash"].get(note["body_hash"], []) if note["body_hash"] else []:
        matches.append((entry, IDENTICAL))
        claimed.add(entry["relative"])
    for tier, index_key, counts_key, field in (
        (NAMED, "by_stem", "vault_stems", "stem"),
        (TITLED, "by_title", "vault_titles", "title"),
    ):
        value = note[field]
        if not value or indexes[counts_key].get(value, 0) != 1:
            continue
        entry = indexes[index_key].get(value)
        if entry is not None and entry["relative"] not in claimed:
            matches.append((entry, tier))
            claimed.add(entry["relative"])
    return matches


def confidence(match_tier, evidence_tier):
    """The weaker of the two axes, named for the report and the apply gate."""
    if evidence_tier == WEAK:
        return "low"
    if match_tier in AUTO_MATCH and evidence_tier in AUTO_EVIDENCE:
        return "high"
    return "medium"


def decide(note, matches):
    """Choose a date for one note, or explain why there is not one.

    Evidence tier decides first and the earliest date wins inside it: the oldest
    copy of a note is the one that dates its creation, and a later revision
    saved under a newer name must not overwrite that.
    """
    pool = [(entry, SELF, note) for entry in note["candidates"]]
    for source, tier in matches:
        pool.extend((entry, tier, source) for entry in source["candidates"])
    if not pool:
        return None
    pool.sort(key=lambda item: (EVIDENCE_RANK[item[0]["tier"]], item[0]["date"], MATCH_RANK[item[1]], item[2]["relative"]))
    chosen, chosen_match, chosen_source = pool[0]
    # Only the files that actually spoke at the winning tier can undermine it.
    best_evidence = EVIDENCE_RANK[chosen["tier"]]
    deciding = {item[2]["relative"] for item in pool if EVIDENCE_RANK[item[0]["tier"]] == best_evidence}
    contradicted = any(
        self_contradicts(source["candidates"])
        for _, _, source in pool
        if source["relative"] in deciding
    )
    level = confidence(chosen_match, chosen["tier"])
    if contradicted and level == "high":
        level = "medium"
    return {
        "date": chosen["date"],
        "evidence": chosen["tier"],
        "match": chosen_match,
        "source": chosen_source["relative"],
        "quote": chosen["quote"],
        "confidence": level,
        "contradicted": contradicted,
        "considered": [
            {
                "source": item[2]["relative"],
                "match": item[1],
                "date": item[0]["date"],
                "evidence": item[0]["tier"],
                "why": item[0]["source"],
                "quote": item[0]["quote"],
            }
            for item in pool
        ],
    }


def decision_id(relative, value):
    """A stable id derived from content, so one copied out of a report cannot
    come to address a different note before it is applied."""
    return sha256_text(relative + "\n" + value)[:12]


def insertion_index(block, order, key):
    """Where ``key`` belongs in a frontmatter block, following the schema's order."""
    if key not in order:
        return len(block)
    limit = order.index(key)
    insert_at = 0
    for index, line in enumerate(block):
        match = FRONTMATTER_KEY_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name in order and order.index(name) < limit:
            insert_at = index + 1
            while insert_at < len(block) and LIST_ITEM_RE.match(block[insert_at]):
                insert_at += 1
    return insert_at


def insert_scalar_property(data, key, value, order):
    """Add one scalar property to a note's frontmatter, changing nothing else.

    Returns ``(new_bytes, reason)``; ``new_bytes`` is None when the note is
    refused. The body, the delimiters, the BOM, the line endings, and every
    other property survive byte-for-byte — this writes exactly one line.
    """
    had_bom = data.startswith(b"\xef\xbb\xbf")
    prefix = data[:3] if had_bom else b""
    try:
        text = (data[3:] if had_bom else data).decode("utf-8")
    except UnicodeDecodeError as error:
        return None, "not valid UTF-8: {0}".format(error)

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, "no frontmatter block; run vault-organizer on this note first"
    close = None
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r\n") == "---":
            close = index
            break
    if close is None:
        return None, "frontmatter has no closing delimiter"

    newline = "\r\n" if lines[0].endswith("\r\n") else "\n"
    block = lines[1:close]
    rendered = "{0}: {1}{2}".format(key, yaml_scalar(value), newline)
    for index, line in enumerate(block):
        match = FRONTMATTER_KEY_RE.match(line)
        if not match or match.group(1) != key:
            continue
        existing = match.group(2).strip()
        if existing:
            return None, "{0} is already set to {1}".format(key, existing)
        # Obsidian writes a bare `key:` for a property it knows about and has no
        # value for. That is the empty slot this mode exists to fill, not a value
        # to protect — but a bare key above list items is a list, and is not.
        if index + 1 < len(block) and LIST_ITEM_RE.match(block[index + 1]):
            return None, "{0} is already set to a list".format(key)
        new_block = block[:index] + [rendered] + block[index + 1 :]
        rebuilt = "".join([lines[0]] + new_block + lines[close:])
        return prefix + rebuilt.encode("utf-8"), None

    insert_at = insertion_index(block, order, key)
    new_block = block[:insert_at] + [rendered] + block[insert_at:]
    rebuilt = "".join([lines[0]] + new_block + lines[close:])
    return prefix + rebuilt.encode("utf-8"), None
