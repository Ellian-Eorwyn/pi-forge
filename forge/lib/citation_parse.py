#!/usr/bin/env python3

"""Deterministic citation-file parsing for Forge literature workflows.

Reference managers disagree about almost everything except the shape of a tag
line, so this module is written to be permissive about layout and strict about
provenance: every deviation from the canonical form is recorded rather than
silently accepted, and nothing about the source bytes is repaired unless the
caller asks for it.

The record this produces is deliberately a field-name projection of the
`works.jsonl` rows that `web-research.mjs` `createCanonicalWork` emits. That is
what lets a citation file and an academic run feed the same downstream code with
no adapter between them. Standard library only, so skills stay installable
without extra dependencies.
"""

import codecs
import re


# Decoded in order. `utf-8-sig` is first because it is a strict UTF-8 decode
# that also drops a BOM, so it subsumes plain UTF-8. cp1252 comes next because
# Windows reference managers still emit it and it fails loudly on only five
# undefined bytes. latin-1 cannot fail, so it is the terminal fallback and is
# the only one that can introduce replacement characters of our own making.
ENCODING_LADDER = ("utf-8-sig", "cp1252", "latin-1")

# RIS has no formal continuation syntax, but Zotero, EndNote, and Mendeley all
# wrap long `AB` and `N1` values, so an unrecognized non-blank line is treated
# as a continuation of the previous tag rather than as an error.
TAG_LINE_PATTERN = re.compile(r"^([A-Z][A-Z0-9])(\s{1,2})-(?:\s(.*)|)$")

# A line matching the tag pattern is only accepted as a tag when the tag is
# known or the strict two-space form was used. Without this, a wrapped abstract
# line such as "US - based studies" would be misread as a tag named US.
KNOWN_TAGS = frozenset(
    """
    TY TI T1 T2 T3 CT BT ST TT
    AU A1 A2 A3 A4 ED
    AB N1 N2
    PY DA Y1 Y2
    JO JF JA J1 J2
    VL IS SP EP CP
    PB PP CY
    DO M1 M2 M3
    SN AN ID
    UR L1 L2 L3 L4
    KW LA DB DP RN RP ET SE VO
    C1 C2 C3 C4 C5 C6 C7 C8
    ER
    """.split()
)

# `TY` values map onto the same vocabulary `createCanonicalWork` uses for its
# `type` field, so a parsed citation and an academic work sort together.
RIS_TYPE_MAP = {
    "JOUR": "journal-article",
    "EJOUR": "journal-article",
    "BOOK": "book",
    "EBOOK": "book",
    "CHAP": "book-chapter",
    "ECHAP": "book-chapter",
    "CONF": "conference-paper",
    "CPAPER": "conference-paper",
    "THES": "thesis",
    "RPRT": "report",
    "ELEC": "webpage",
    "MGZN": "magazine-article",
    "NEWS": "newspaper-article",
    "UNPB": "unpublished",
    "MANSCPT": "manuscript",
    "GEN": "generic",
}

TITLE_TAGS = ("TI", "T1", "CT", "BT", "ST", "TT")
AUTHOR_TAGS = ("AU", "A1")
EDITOR_TAGS = ("A2", "ED", "A3", "A4")
VENUE_TAGS = ("JO", "JF", "JA", "T2", "T3", "J1", "J2")
ABSTRACT_TAGS = ("AB", "N2")
URL_TAGS = ("UR", "L2", "L3", "L4")
FILE_TAGS = ("L1",)

DOI_PREFIX_PATTERN = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*|info:doi/)", re.IGNORECASE)
DOI_SHAPE_PATTERN = re.compile(r"^10\.\d{4,9}/\S+$")
ARXIV_PATTERN = re.compile(r"(?:arxiv[:/\s]*)?(\d{4}\.\d{4,5}(?:v\d+)?)", re.IGNORECASE)

# A replacement character flanked by letters is nearly always a mangled
# apostrophe: the source text said "it's" and something between the publisher
# and the export destroyed it. Repairing it is still a guess, so it is opt-in
# and every substitution is recorded per record.
LONE_REPLACEMENT_PATTERN = re.compile(r"(?<=[^\W\d_])�(?=[^\W\d_])")
REPLACEMENT_SUBSTITUTE = "’"


class CitationParseError(ValueError):
    """The file is not a citation file we can read, or a record is malformed."""


def decode_citation_bytes(raw):
    """Decode citation bytes, reporting which encoding worked and what it cost.

    Returns the text plus the provenance a run needs to explain itself later:
    which encoding was used, whether a BOM was present, and how many U+FFFD
    characters exist. The count matters because a replacement character that
    arrived in the source bytes is destroyed information, while one produced by
    our own latin-1 fallback is a decoding artifact -- and the caller cannot
    tell them apart from the text alone.
    """
    had_bom = raw.startswith(codecs.BOM_UTF8)
    for encoding in ENCODING_LADDER:
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        return {
            "text": text,
            "encoding_detected": encoding,
            "had_bom": had_bom,
            "replacement_char_count": text.count("�"),
            "decode_errors_replaced": False,
        }
    text = raw.decode("latin-1", errors="replace")
    return {
        "text": text,
        "encoding_detected": "latin-1",
        "had_bom": had_bom,
        "replacement_char_count": text.count("�"),
        "decode_errors_replaced": True,
    }


def repair_replacement_chars(text):
    """Replace lone letter-flanked U+FFFD with a right single quote.

    Returns the repaired text and one entry per substitution so the run can show
    exactly what it guessed. Never call this on an identifier.
    """
    changes = []

    def substitute(match):
        start = max(0, match.start() - 12)
        end = min(len(text), match.end() + 12)
        changes.append({"before": text[start:end], "after": text[start:end].replace("�", REPLACEMENT_SUBSTITUTE)})
        return REPLACEMENT_SUBSTITUTE

    return LONE_REPLACEMENT_PATTERN.sub(substitute, text), changes


def normalize_doi(value):
    """Strip the many ways a DOI is written down; return None if it isn't one."""
    if not value:
        return None
    text = DOI_PREFIX_PATTERN.sub("", str(value).strip()).strip()
    text = text.rstrip(".,;)]}>").strip()
    if not text:
        return None
    # DOIs are case-insensitive by specification and lowercase by convention,
    # which also makes them usable as a dedupe key.
    text = text.lower()
    return text if DOI_SHAPE_PATTERN.match(text) else None


def _split_tag_lines(text):
    """Split decoded text into (line_number, tag, value) plus continuations.

    Continuations are yielded with a tag of None so the record builder can
    decide what they attach to; deciding here would lose the line number.
    """
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        match = TAG_LINE_PATTERN.match(line)
        if match and (match.group(1) in KNOWN_TAGS or len(match.group(2)) == 2):
            yield number, match.group(1), (match.group(3) or "").strip(), len(match.group(2))
            continue
        yield number, None, line.strip(), 0


def parse_ris_tags(text):
    """Group RIS tag lines into raw per-record tag maps.

    Returns (records, warnings) where each record is {tag: [values]} plus a
    `_line` key holding the line the record started on. Structural problems that
    make a record unreadable raise; problems we can survive become warnings, so
    one bad record never costs the caller the other forty-eight.
    """
    records = []
    warnings = []
    current = None
    last_tag = None
    loose_spacing_reported = False
    unknown_tags = set()

    for number, tag, value, spacing in _split_tag_lines(text):
        if tag is None:
            if current is None or last_tag is None:
                raise CitationParseError(f"line {number}: continuation text before any tag: {value[:60]!r}")
            current[last_tag][-1] = f"{current[last_tag][-1]} {value}".strip()
            continue
        if spacing != 2 and not loose_spacing_reported:
            warnings.append(f"Line {number} uses a single space before the tag separator instead of two.")
            loose_spacing_reported = True
        if tag not in KNOWN_TAGS:
            unknown_tags.add(tag)
        if tag == "TY":
            if current is not None:
                raise CitationParseError(f"line {number}: a new TY record started before the previous ER terminator")
            current = {"_line": number, "TY": [value]}
            last_tag = "TY"
            continue
        if current is None:
            warnings.append(f"Ignored tag {tag} at line {number} because it appears before the first TY.")
            continue
        if tag == "ER":
            records.append(current)
            current = None
            last_tag = None
            continue
        current.setdefault(tag, []).append(value)
        last_tag = tag

    if current is not None:
        warnings.append(
            f"The record starting at line {current['_line']} has no ER terminator; recovered it from end of file."
        )
        records.append(current)
    if unknown_tags:
        warnings.append(f"Kept {len(unknown_tags)} unrecognized tag(s) as provenance only: {' '.join(sorted(unknown_tags))}.")
    return records, warnings


def _first(tags, names):
    for name in names:
        for value in tags.get(name, ()):
            if value:
                return value
    return None


def _collect(tags, names):
    values = []
    for name in names:
        for value in tags.get(name, ()):
            if value and value not in values:
                values.append(value)
    return values


def parse_author(value):
    """Split one RIS author string into the {family, given, name} shape.

    `createCanonicalWork` produces `family`/`given` from Crossref and only
    `name` from Semantic Scholar, so both are preserved here and the naming
    module prefers `family` when it exists. RIS canonically writes
    "Family, Given", which both author forms in a Consensus export use.
    """
    text = re.sub(r"\s+", " ", str(value)).strip().rstrip(",")
    if not text:
        return None
    if "," in text:
        family, _, given = text.partition(",")
        return {"family": family.strip(), "given": given.strip() or None, "name": text}
    return {"family": None, "given": None, "name": text}


def _year_from(value):
    if not value:
        return None
    match = re.search(r"\b(1[0-9]{3}|20[0-9]{2}|21[0-9]{2})\b", str(value))
    return int(match.group(1)) if match else None


def _identifiers(tags):
    identifiers = {}
    doi = None
    for name in ("DO", "M3", "M1", "M2", "AN", "ID"):
        for value in tags.get(name, ()):
            doi = normalize_doi(value)
            if doi:
                break
        if doi:
            break
    if doi:
        identifiers["doi"] = doi

    for value in _collect(tags, ("AN", "ID", "C1")):
        lowered = value.lower()
        if "pmid" in lowered:
            digits = re.search(r"\d{5,9}", value)
            if digits:
                identifiers.setdefault("pmid", digits.group(0))
        if "pmc" in lowered:
            pmc = re.search(r"PMC\d+", value, re.IGNORECASE)
            if pmc:
                identifiers.setdefault("pmcid", pmc.group(0).upper())

    for value in _collect(tags, ("UR", "L2", "AN", "ID", "DO")) + ([doi] if doi else []):
        if value and "arxiv" in str(value).lower():
            match = ARXIV_PATTERN.search(str(value))
            if match:
                identifiers.setdefault("arxiv_id", match.group(1))
                break

    for value in tags.get("SN", ()):
        compact = value.replace("-", "").replace(" ", "")
        if len(compact) in (10, 13) and compact[:-1].isdigit():
            identifiers.setdefault("isbn", value.strip())
        elif re.match(r"^\d{7}[\dxX]$", compact):
            identifiers.setdefault("issn", value.strip())
        else:
            identifiers.setdefault("serial", value.strip())
    return identifiers


def build_record(tags, index):
    """Turn one raw tag map into the canonical works.jsonl-shaped record."""
    warnings = []
    needs_review = []

    ris_type = (_first(tags, ("TY",)) or "GEN").strip().upper()
    if ris_type not in RIS_TYPE_MAP:
        warnings.append(f"Unrecognized RIS type {ris_type}; treated as generic.")

    title = _first(tags, TITLE_TAGS)
    if not title:
        needs_review.append("no title")

    authors = [author for author in (parse_author(value) for value in _collect(tags, AUTHOR_TAGS)) if author]
    if not authors:
        needs_review.append("no author")

    publication_date = _first(tags, ("DA", "Y1"))
    publication_year = _year_from(_first(tags, ("PY",))) or _year_from(publication_date)
    if publication_year is None:
        needs_review.append("no year")

    identifiers = _identifiers(tags)
    urls = _collect(tags, URL_TAGS)

    # An L1 file link is a full-text candidate we already have a URL for, which
    # is the single biggest free win when the input came from Zotero rather than
    # from a search service.
    candidates = [{"url": value, "source": "ris-file-link"} for value in _collect(tags, FILE_TAGS)]

    pages = None
    start_page = _first(tags, ("SP",))
    end_page = _first(tags, ("EP",))
    if start_page and end_page:
        pages = f"{start_page}-{end_page}"
    elif start_page:
        pages = start_page

    return {
        "record_index": index,
        "type": RIS_TYPE_MAP.get(ris_type, "generic"),
        "ris_type": ris_type,
        "canonical_title": title,
        "authors": authors,
        "editors": [author for author in (parse_author(value) for value in _collect(tags, EDITOR_TAGS)) if author],
        "publication_year": publication_year,
        "publication_date": publication_date,
        "venue_name": _first(tags, VENUE_TAGS),
        "publisher": _first(tags, ("PB",)),
        "place": _first(tags, ("CY", "PP")),
        "volume": _first(tags, ("VL",)),
        "issue": _first(tags, ("IS", "CP")),
        "pages": pages,
        "edition": _first(tags, ("ET",)),
        "language": _first(tags, ("LA",)),
        "abstract_best": _first(tags, ABSTRACT_TAGS),
        "identifiers": identifiers,
        "urls": urls,
        "keywords": _collect(tags, ("KW",)),
        "notes": _collect(tags, ("N1",)),
        # Filled in by the acquisition ladder's resolve stage; present here so
        # the shape matches works.jsonl from the start.
        "oa_status": None,
        "oa_locations": [],
        "full_text_candidates": candidates,
        "source_tags": {tag: values for tag, values in tags.items() if tag != "_line"},
        "source_line": tags.get("_line"),
        "warnings": warnings,
        "needs_review": needs_review,
    }


def parse_ris(text):
    """Parse RIS text into canonical records plus run-level warnings."""
    raw_records, warnings = parse_ris_tags(text)
    records = [build_record(tags, index) for index, tags in enumerate(raw_records)]
    return {"records": records, "warnings": warnings}


def parse_bibtex(text):
    """Not implemented yet.

    BibTeX needs a real tokenizer rather than a line grammar: values are
    brace-balanced, `@string` macros expand, accents are LaTeX escapes, author
    lists are ` and `-separated with `{}`-protected corporate names, and
    `crossref` fields inherit. Failing loudly is better than half-parsing it.
    """
    raise CitationParseError("BibTeX parsing is not implemented yet; export RIS from your reference manager")


def sniff_format(text):
    """Identify the citation format from content, not from the file extension."""
    head = text.lstrip("﻿").lstrip()
    if re.match(r"^@\w+\s*[{(]", head):
        return "bibtex"
    if TAG_LINE_PATTERN.match(head.splitlines()[0] if head.splitlines() else ""):
        return "ris"
    return None


def parse_text(text, citation_format=None, repair_replacements=False):
    """Parse decoded citation text, optionally repairing replacement characters."""
    normalizations = []
    if repair_replacements:
        text, normalizations = repair_replacement_chars(text)

    detected = citation_format or sniff_format(text)
    if detected == "bibtex":
        parsed = parse_bibtex(text)
    elif detected == "ris":
        parsed = parse_ris(text)
    else:
        raise CitationParseError("unrecognized citation format: expected RIS tag lines or a BibTeX @entry")

    parsed["format"] = detected
    parsed["normalizations"] = normalizations
    return parsed


def parse_file(path, repair_replacements=False):
    """Parse a citation file, returning records plus full decoding provenance."""
    with open(path, "rb") as handle:
        raw = handle.read()
    decoded = decode_citation_bytes(raw)
    parsed = parse_text(
        decoded["text"],
        citation_format=None,
        repair_replacements=repair_replacements,
    )
    parsed.update(
        {
            "encoding_detected": decoded["encoding_detected"],
            "had_bom": decoded["had_bom"],
            "replacement_char_count": decoded["replacement_char_count"],
            "decode_errors_replaced": decoded["decode_errors_replaced"],
        }
    )
    if decoded["decode_errors_replaced"]:
        parsed["warnings"].append(
            "The file did not decode cleanly in any known encoding; unreadable bytes became replacement characters."
        )
    elif decoded["replacement_char_count"]:
        parsed["warnings"].append(
            f"The source file already contained {decoded['replacement_char_count']} replacement character(s); "
            "that text was lost before export and cannot be recovered."
        )
    return parsed
