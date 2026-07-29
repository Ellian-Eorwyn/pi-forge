#!/usr/bin/env python3

"""Bibliographic filenames for Forge literature workflows.

Produces `Author - Date - Title` stems that survive Obsidian, mobile sync, and
Windows unchanged, by assembling the parts and then handing the whole string to
`vault_schema.safe_title`. Cleaning the assembled stem rather than each part is
what makes the result idempotent and byte-identical to the name every other
vault skill would compute for the same string.

Pure functions, no I/O, so the caller owns run state and the filesystem.
"""

import re

from vault_schema import safe_title, validate_filename_title


# `safe_title` truncates at 120 characters. Budgeting against that number here
# means truncation happens at a word boundary rather than mid-word, and the
# caller learns it happened instead of discovering a silently shortened title.
MAX_STEM = 120
MAX_SURNAME = 40
MIN_TITLE_BUDGET = 24

# Used when a year is genuinely absent. Scholarly convention, path-safe, and it
# sorts after digits so undated items cluster at the end of a directory listing.
NO_DATE = "n.d."

# Stripped from the end of a name before taking the last token as the surname.
NAME_SUFFIXES = frozenset(
    {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv", "v", "phd", "ph.d.", "md", "m.d.", "esq", "esq.", "mba", "dphil"}
)

# Kept with the surname: "M. van der Heijden" is filed under "van der Heijden",
# not under "Heijden". Matched case-insensitively but emitted as written.
NAME_PARTICLES = frozenset(
    {"van", "von", "de", "del", "della", "di", "da", "dos", "das", "du", "la", "le", "ten", "ter", "bin", "al", "der", "den", "op"}
)

CORPORATE_PATTERN = re.compile(
    r"\b(institute|university|universit|organi[sz]ation|committee|association|society|commission|ministry"
    r"|department|council|bank|agency|bureau|foundation|centre|center|consortium|network|group|team"
    r"|WHO|UNESCO|UNICEF|OECD|NATO|NIH|CDC|EPA|UN)\b",
    re.IGNORECASE,
)
CORPORATE_TAIL_PATTERN = re.compile(r"\b(inc\.?|ltd\.?|llc|plc|gmbh|co\.)$", re.IGNORECASE)

YEAR_LETTERS = "abcdefghijklmnopqrstuvwxyz"


def looks_corporate(text):
    """Decide whether a name is an organization rather than a person.

    Deliberately conservative: a personal name almost never trips these, and a
    false positive only costs a `needs_review` flag, while a false negative
    files "World Health Organization" under "Organization".
    """
    stripped = text.strip()
    if not stripped:
        return False
    if CORPORATE_PATTERN.search(stripped) or CORPORATE_TAIL_PATTERN.search(stripped):
        return True
    return "," not in stripped and len(stripped.split()) > 4


def surname_from_display_name(text):
    """Extract a filing surname from a free-form author string."""
    cleaned = re.sub(r"\s+", " ", str(text)).strip().strip(",")
    if not cleaned:
        return None
    if "," in cleaned:
        # RIS canonical form is "Family, Given", so everything before the first
        # comma is the surname, particles and hyphens included.
        return cleaned.partition(",")[0].strip() or None

    tokens = cleaned.split()
    while tokens and tokens[-1].strip(".").lower() in {suffix.strip(".") for suffix in NAME_SUFFIXES}:
        tokens.pop()
    if not tokens:
        return None

    index = len(tokens) - 1
    while index > 0 and tokens[index - 1].lower() in NAME_PARTICLES:
        index -= 1
    return " ".join(tokens[index:]) or None


def author_surname(author):
    """Return (surname, kind) for one author record, or (None, None).

    Prefers the `family` field, which academic-run records already carry from
    Crossref and EuropePMC, so those need no name parsing at all.
    """
    if author is None:
        return None, None
    if isinstance(author, str):
        display = author
    else:
        family = (author.get("family") or "").strip()
        display = (author.get("name") or "").strip()
        if family and not looks_corporate(family):
            return family, "person"
        if not display:
            display = family
    if not display:
        return None, None
    if looks_corporate(display):
        return display.strip(), "corporate"
    surname = surname_from_display_name(display)
    return (surname, "person") if surname else (None, None)


def truncate_words(text, budget):
    """Trim to a word boundary within budget, with no ellipsis added.

    An ellipsis would be three characters of the budget spent saying nothing,
    and the full title is preserved in the manifest either way.
    """
    if budget <= 0:
        return ""
    if len(text) <= budget:
        return text
    window = text[:budget]
    cut = window.rfind(" ")
    return (window[:cut] if cut >= MIN_TITLE_BUDGET // 2 else window).rstrip(" ,;:-–—")


def first_author_surname(record):
    """Resolve the filing surname for a record, with review flags."""
    flags = []
    for author in record.get("authors") or ():
        surname, kind = author_surname(author)
        if surname:
            if kind == "corporate":
                original = surname
                surname = truncate_words(surname, MAX_SURNAME) or surname[:MAX_SURNAME]
                flags.append(f"corporate author filed as {surname!r}")
                if surname != original:
                    flags.append("corporate author name truncated")
            return surname, kind, flags
    flags.append("no usable author; filed as Unknown")
    return "Unknown", "unknown", flags


def year_label(record):
    """Resolve the filing year, falling back to the no-date convention."""
    year = record.get("publication_year")
    if isinstance(year, int) and 1000 <= year <= 2999:
        return str(year), []
    match = re.search(r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b", str(record.get("publication_date") or ""))
    if match:
        return match.group(1), []
    return NO_DATE, ["no usable year; filed as n.d."]


def build_stem(surname, year, title):
    """Assemble one stem and report what the length budget cost.

    The surname is shortened before the title only when the prefix alone would
    leave the title unreadable, which in practice means a long corporate author.
    """
    flags = []
    surname = (surname or "Unknown").strip()
    title = re.sub(r"\s+", " ", str(title or "")).strip()

    if len(surname) > MAX_SURNAME:
        surname = truncate_words(surname, MAX_SURNAME) or surname[:MAX_SURNAME]
        flags.append("surname truncated to fit the filename budget")

    prefix = f"{surname} - {year} - "
    budget = MAX_STEM - len(prefix)
    if budget < MIN_TITLE_BUDGET:
        surname = truncate_words(surname, max(8, MAX_STEM - MIN_TITLE_BUDGET - len(f" - {year} - "))) or surname[:8]
        prefix = f"{surname} - {year} - "
        budget = MAX_STEM - len(prefix)
        flags.append("surname shortened further to leave room for the title")

    title_part = truncate_words(title, budget) if title else ""
    truncated = bool(title) and title_part != title
    if truncated:
        flags.append("title truncated to fit the 120-character filename limit")
    if not title_part:
        title_part = "Untitled"
        if not title:
            flags.append("no title; filed as Untitled")

    stem = safe_title(prefix + title_part)
    return {"stem": stem, "surname": surname, "year": year, "title_used": title_part, "title_truncated": truncated, "flags": flags}


def _stem_for(record, year, reserved_check):
    surname, kind, author_flags = first_author_surname(record)
    built = build_stem(surname, year, record.get("canonical_title"))
    built["author_kind"] = kind
    built["flags"] = author_flags + built["flags"]
    built["available"] = not reserved_check(built["stem"])
    return built


def derive_stems(records, reserved=None):
    """Derive a unique stem for every record, in source order.

    Collisions are resolved citation-style by lettering the year -- `2014a`,
    `2014b` -- rather than by appending `-2`, because the result still reads
    correctly in a bibliography. Both sides of a collision are lettered, which
    is why this works over the whole batch rather than one record at a time.

    `reserved` holds stems already published by an earlier run. A new record
    colliding with one of those is lettered on its own, because relettering a
    file that already exists on disk would break the publish journal.
    """
    reserved_fold = {value.casefold() for value in (reserved or ())}
    naming = []
    for record in records:
        year, year_flags = year_label(record)
        built = _stem_for(record, year, lambda stem: stem.casefold() in reserved_fold)
        built["flags"] = year_flags + built["flags"]
        built["year_letter"] = None
        naming.append(built)

    # Group on the assembled stem rather than on author+year: two 2014 papers by
    # the same author have different titles and therefore different filenames,
    # so lettering them would be noise. Only a real filename collision -- which
    # truncation can also cause -- needs disambiguating.
    groups = {}
    for index, built in enumerate(naming):
        groups.setdefault(built["stem"].casefold(), []).append(index)

    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        for position, index in enumerate(indexes):
            record = records[index]
            letter = YEAR_LETTERS[position] if position < len(YEAR_LETTERS) else f"-{position + 1}"
            year = f"{naming[index]['year']}{letter}"
            rebuilt = _stem_for(record, year, lambda stem: stem.casefold() in reserved_fold)
            rebuilt["flags"] = naming[index]["flags"] + rebuilt["flags"] + ["disambiguated from a colliding filename"]
            rebuilt["year_letter"] = letter
            naming[index] = rebuilt

    # Anything still colliding with a previously published stem gets its own
    # letter, applied after batch disambiguation so the two never fight.
    taken = set(reserved_fold)
    for index, built in enumerate(naming):
        if built["stem"].casefold() not in taken:
            taken.add(built["stem"].casefold())
            continue
        base_year = f"{built['year']}".rstrip(YEAR_LETTERS) or built["year"]
        for letter in YEAR_LETTERS:
            candidate = _stem_for(records[index], f"{base_year}{letter}", lambda stem: False)
            if candidate["stem"].casefold() not in taken:
                candidate["flags"] = built["flags"] + ["disambiguated from a stem published by an earlier run"]
                candidate["year_letter"] = letter
                naming[index] = candidate
                break
        taken.add(naming[index]["stem"].casefold())

    for index, built in enumerate(naming):
        # safe_title is idempotent, so this is the module's own validator: if the
        # assembled stem is not already clean, the assembly logic is wrong.
        validate_filename_title(built["stem"], f"record {index} filename")
        built["needs_review"] = [flag for flag in built["flags"] if "truncated" in flag or "Unknown" in flag or "corporate" in flag]
    return naming
