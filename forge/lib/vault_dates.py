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
# A year with no day behind it: a year folder in the archive path, or a
# birthtime whose day is an import stamp. Rendered as ``YYYY-01-01``, where the
# day is a placeholder and says so, never a claim about January.
YEAR = "year"
WEAK = "weak"
EVIDENCE_RANK = {EXPLICIT: 0, STATED: 1, YEAR: 2, WEAK: 3}

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

# The same two orderings written with spaces, as in `Dream 05 15 2013`. Kept
# apart from NUMERIC_DMY_RE because a separator-free run of numbers is far
# likelier to be something other than a date, so this is only read from a
# filename -- never from body prose, where "call me at 5 15 2013" would match.
SPACED_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})\s+(\d{1,2})\s+((?:19|20)\d{2})(?!\d)")

# A bare four-digit folder name: `.../12.01 Daily/2015/Depression.md`. Says the
# year and nothing more.
YEAR_DIR_RE = re.compile(r"^((?:19|20)\d{2})$")

# A birthtime day shared by more than this many distinct archive notes is when
# an import ran, not when anything was written. Measured on Ellie's archive the
# split is not close: 377 days carry one or two notes, and 28 carry thirteen or
# more -- one of them 1,419.
STAMP_NOTE_THRESHOLD = 12

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


def spaced_date_readings(stem):
    """``(month_first, day_first, quote)`` for the last spaced run in a filename.

    Both readings are returned rather than one, because which is meant is a
    property of the corpus and not of the string. The *last* run wins so that
    ``Day 1 7 15 2013`` reads the date and not the "Day 1".
    """
    matches = list(SPACED_DMY_RE.finditer(stem))
    if not matches:
        return None
    match = matches[-1]
    first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    return valid_date(year, first, second), valid_date(year, second, first), match.group(0)


def resolve_spaced_date(stem, month_first=None):
    """The date a spaced filename means, or None while it stays ambiguous.

    Unambiguous on its own when only one ordering is a real date, or when both
    orderings land on the same day (``6 6 2013``). Otherwise it takes
    ``month_first``, which the corpus decides -- see ``corpus_calibration``.
    """
    readings = spaced_date_readings(stem)
    if not readings:
        return None
    month_reading, day_reading, quote = readings
    if month_reading and not day_reading:
        return month_reading, quote, True
    if day_reading and not month_reading:
        return day_reading, quote, True
    if month_reading and day_reading:
        if month_reading == day_reading:
            return month_reading, quote, True
        if month_first is True:
            return month_reading, quote, False
        if month_first is False:
            return day_reading, quote, False
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


def frontmatter_evidence(frontmatter_text, today=None):
    """Dates under a creation-ish frontmatter key.

    A creation date in the future is not a date this file was created on, so it
    is dropped rather than believed. These keys are written by importers and by
    models, and both produce nonsense from time to time: an organization note
    named "Architecture 2030" arrived carrying ``created: '2030-01-01'``.
    """
    horizon = today or datetime.date.today()
    candidates = []
    for key, raw in loose_frontmatter_pairs(frontmatter_text):
        if key not in CREATION_KEYS:
            continue
        found = first_date_in(raw)
        if found and found[0] <= horizon:
            candidates.append(candidate(found[0], EXPLICIT, "frontmatter:" + key, raw))
    return candidates


def filename_evidence(path, month_first=None):
    """A date carried by the file's own name.

    A separated date wins outright. Only when there is none does the spaced
    form get a look, so ``2013-05-15 notes 1 2 2014`` still reads as May.
    """
    stem = Path(path).stem
    found = first_date_in(stem)
    if found:
        return [candidate(found[0], EXPLICIT, "filename", found[1])]
    spaced = resolve_spaced_date(stem, month_first)
    if spaced:
        value, quote, self_evident = spaced
        source = "filename" if self_evident else "filename (corpus reads month first)"
        return [candidate(value, EXPLICIT, source, quote)]
    return []


def year_directory(relative):
    """The year named by a bare four-digit folder on the path, or None."""
    for part in Path(relative).parts[:-1]:
        match = YEAR_DIR_RE.match(part.strip())
        if match:
            return int(match.group(1))
    return None


def year_evidence(relative):
    """A year folder standing over the file: ``12.01 Daily/2015/Depression.md``.

    Says the year and refuses to invent the day. January 1st here is the
    placeholder the schema's date format forces, not a reading of the evidence.
    """
    year = year_directory(relative)
    if year is None or not (MIN_YEAR <= year <= MAX_YEAR):
        return []
    return [candidate(datetime.date(year, 1, 1), YEAR, "year folder", str(relative))]


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


def to_date(stamp):
    """A POSIX timestamp as a local date, or None when it is not a plausible one."""
    if not stamp:
        return None
    try:
        value = datetime.datetime.fromtimestamp(stamp).date()
    except (OSError, OverflowError, ValueError):
        return None
    return value if MIN_YEAR <= value.year <= MAX_YEAR else None


def file_times(path):
    """``(birthtime, mtime)`` as dates, either of which may be None.

    ``st_birthtime`` is what Finder shows as Date Created. macOS records it and
    Linux does not, so the absence of the attribute is normal rather than an
    error.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return None, None
    return to_date(getattr(stat, "st_birthtime", None)), to_date(stat.st_mtime)


def filesystem_evidence(birthtime, mtime, trust_birthtime=False, stamp_days=frozenset(), surface_stamped=False):
    """Dates the filesystem carries, which are only as good as what made the copy.

    A Finder *move* preserves birthtime; a *copy* resets it to the day of the
    copy, and most archive tools reset it too. Neither outcome is guessable from
    one file alone -- but it is plain across a whole archive, because a reset
    lands thousands of files on a single day. ``stamp_days`` holds the days that
    happened on, so a birthtime is read three ways rather than two:

    * on a stamp day -- the year survived the copy and the day did not, so it is
      ``year`` evidence and the day is dropped;
    * off a stamp day, with the caller trusting birthtimes -- ``explicit``;
    * off a stamp day otherwise -- ``weak``, as before.

    A modification time is never promotable. Nothing makes it a creation date.
    """
    candidates = []
    if birthtime is not None:
        stamped = birthtime.isoformat() in stamp_days
        if stamped:
            # Not even the year survives a stamp. The day the copy ran carries
            # the copy's year, so a note written in 2013 and copied in 2026
            # reads as 2026. A year folder can say the year; this cannot. It is
            # left out entirely rather than filling the review pile with a
            # thousand rows nobody can act on -- unless file times were asked
            # for by name, which is a request to see exactly this.
            if surface_stamped:
                candidates.append(candidate(birthtime, WEAK, "finder created (import stamp)", birthtime.isoformat()))
        else:
            tier = EXPLICIT if trust_birthtime else WEAK
            candidates.append(candidate(birthtime, tier, "finder created", birthtime.isoformat()))
    if mtime is not None:
        candidates.append(candidate(mtime, WEAK, "filesystem modified", mtime.isoformat()))
    return candidates


def stamp_days(entries, threshold=STAMP_NOTE_THRESHOLD):
    """The birthtime days that are import events rather than creation dates.

    Counted in distinct notes, not files: an archive holding four copies of one
    note must not look like four things made that day. A day carrying more
    distinct notes than a person writes in a day is when a copy ran.
    """
    per_day = {}
    for entry in entries:
        birth = entry.get("birthtime")
        if not birth:
            continue
        per_day.setdefault(birth, set()).add(entry.get("body_hash") or entry.get("relative"))
    return {day: len(notes) for day, notes in per_day.items() if len(notes) > threshold}


def filename_convention(entries):
    """Whether spaced filename dates in this corpus read month-first.

    Decided only by the filenames that need no deciding -- the ones where one
    ordering is not a date, or both orderings agree. ``None`` when they
    disagree or there are none, which leaves every ambiguous name unread.
    """
    month_first = day_first = 0
    for entry in entries:
        readings = spaced_date_readings(Path(entry["path"]).stem if entry.get("path") else "")
        if not readings:
            continue
        month_reading, day_reading, _ = readings
        if month_reading and not day_reading:
            month_first += 1
        elif day_reading and not month_reading:
            day_first += 1
    if month_first and not day_first:
        return True, {"month_first": month_first, "day_first": day_first}
    if day_first and not month_first:
        return False, {"month_first": month_first, "day_first": day_first}
    return None, {"month_first": month_first, "day_first": day_first}


def calibrate_birthtime(entries):
    """Measure birthtime against the files that also state a date in their text.

    Whether an archive's creation dates survived is not a thing to assume in
    either direction — it depends on how the archive was made, and the answer
    is in the files. Every file carrying *both* an explicit text date and a
    birthtime is a labelled example, so the agreement rate across them
    estimates how often birthtime is right on the files that state nothing.

    ``clustered`` is the other half of the question: if a large share of the
    archive shares one birthtime, that day is when the copy happened, and the
    agreement rate is being measured on survivors of a different history.
    """
    labelled = same_day = within_a_day = 0
    days = {}
    with_birthtime = 0
    for entry in entries:
        birth = entry.get("birthtime")
        if not birth:
            continue
        with_birthtime += 1
        days[birth] = days.get(birth, 0) + 1
        stated = [item["date"] for item in entry["candidates"] if item["tier"] == EXPLICIT and item["source"] != "finder created"]
        if not stated:
            continue
        labelled += 1
        best = min(abs((datetime.date.fromisoformat(value) - datetime.date.fromisoformat(birth)).days) for value in stated)
        if best == 0:
            same_day += 1
        if best <= 1:
            within_a_day += 1
    clusters = sorted(days.items(), key=lambda item: (-item[1], item[0]))[:5]
    return {
        "files": len(entries),
        "with_birthtime": with_birthtime,
        "labelled": labelled,
        "same_day": same_day,
        "within_a_day": within_a_day,
        "agreement": round(same_day / labelled, 4) if labelled else None,
        "loose_agreement": round(within_a_day / labelled, 4) if labelled else None,
        "largest_cluster": clusters[0] if clusters else None,
        "clustered": round(clusters[0][1] / with_birthtime, 4) if clusters and with_birthtime else None,
        "top_days": [{"date": day, "files": count} for day, count in clusters],
    }


def dedupe_candidates(candidates):
    """Best evidence first, one entry per (date, tier)."""
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


def extract_dates(
    path,
    relative,
    frontmatter_text,
    body,
    times=None,
    include_file_times=False,
    trust_birthtime=False,
    month_first=None,
    stamp_days=frozenset(),
):
    """Every date candidate a single file offers, best evidence first.

    Duplicates on (date, tier) collapse to the first source that found them, so
    a filename and a frontmatter key agreeing counts once rather than twice.

    ``month_first`` and ``stamp_days`` are corpus facts and are not known on the
    first read of a file; ``recalculate_dates`` supplies them on a second pass.
    """
    candidates = []
    candidates.extend(frontmatter_evidence(frontmatter_text))
    candidates.extend(filename_evidence(path, month_first))
    candidates.extend(path_evidence(relative))
    candidates.extend(year_evidence(relative))
    candidates.extend(body_evidence(body))
    if include_file_times or trust_birthtime:
        birthtime, mtime = times if times is not None else file_times(path)
        candidates.extend(
            filesystem_evidence(
                birthtime,
                mtime if include_file_times else None,
                trust_birthtime=trust_birthtime,
                stamp_days=stamp_days,
                surface_stamped=include_file_times,
            )
        )
    return dedupe_candidates(candidates)


def recalculate_dates(entries, month_first=None, stamps=frozenset(), trust_birthtime=False, include_file_times=False):
    """Re-read every entry's evidence now that the corpus has been measured.

    Which filename ordering this corpus uses, and which days were import events,
    are facts about the whole archive that no single file can report. Rather
    than read every file twice, the candidates that depend on them are recomputed
    from what the fingerprint already carries: the path, the year folder, and the
    birthtime. Frontmatter, body, and separated-filename evidence are untouched.
    """
    for entry in entries:
        keep = [
            item
            for item in entry["candidates"]
            if item["source"] not in ("year folder",)
            and not item["source"].startswith("finder created")
            and not item["source"].startswith("filename (corpus")
            and not item["source"].startswith("frontmatter:")
        ]
        # A note's own name or daily-note path is independent of whatever an
        # importer wrote into its frontmatter, so it can vouch for a date that
        # happens to fall on an import day.
        vouched = {
            item["date"]
            for item in keep
            if item["source"] in ("filename", "daily-note path")
        }
        added = []
        for item in frontmatter_evidence(entry.get("frontmatter_text") or ""):
            # An importer that stamped birthtimes also wrote those stamps into
            # `created:` keys, so the key inherits the copy date and reads as
            # explicit. The year in it is still good; the day is the copy's.
            if item["tier"] == EXPLICIT and item["date"] in stamps and item["date"] not in vouched:
                year = int(item["date"][:4])
                added.append(
                    candidate(
                        datetime.date(year, 1, 1),
                        YEAR,
                        item["source"] + " (import stamp, year only)",
                        item["quote"],
                    )
                )
            else:
                added.append(item)
        added.extend(year_evidence(entry["relative"]))
        if entry.get("birthtime") or entry.get("mtime"):
            birth = datetime.date.fromisoformat(entry["birthtime"]) if entry.get("birthtime") else None
            mod = datetime.date.fromisoformat(entry["mtime"]) if entry.get("mtime") else None
            if include_file_times or trust_birthtime:
                added.extend(
                    filesystem_evidence(
                        birth,
                        mod if include_file_times else None,
                        trust_birthtime=trust_birthtime,
                        stamp_days=stamps,
                        surface_stamped=include_file_times,
                    )
                )
        if not any(item["source"] == "filename" for item in keep):
            added.extend(
                item
                for item in filename_evidence(entry["path"], month_first)
                if item["source"].startswith("filename (corpus")
            )
        entry["candidates"] = dedupe_candidates(keep + added)
    return entries


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


def note_fingerprint(
    path, relative, data, include_file_times=False, trust_birthtime=False, month_first=None, stamps=frozenset()
):
    """Everything the matcher and the extractor need from one file, read once."""
    split = split_frontmatter(data)
    body = split["body"]
    normalized = normalize_body_for_hash(body)
    title = note_title(Path(path), body)
    # Always recorded, even when it is not evidence: calibration needs the
    # birthtime of every file, including the ones that also state a date.
    birthtime, mtime = file_times(path)
    return {
        "birthtime": birthtime.isoformat() if birthtime else "",
        "mtime": mtime.isoformat() if mtime else "",
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
        "candidates": extract_dates(
            path,
            relative,
            split["frontmatter_text"],
            body,
            times=(birthtime, mtime),
            include_file_times=include_file_times,
            trust_birthtime=trust_birthtime,
            month_first=month_first,
            stamp_days=stamps,
        ),
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
    """The weaker of the two axes, named for the report and the apply gate.

    ``year`` never reaches ``high`` however good the match is. The year behind it
    may be certain, but the day is a placeholder, and the apply gate exists to
    keep invented days out of the vault unless someone asks for them by name.
    """
    if evidence_tier == WEAK:
        return "low"
    if evidence_tier == YEAR:
        return "year"
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
