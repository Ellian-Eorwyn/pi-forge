#!/usr/bin/env python3
"""Compile the vault's ``## Phenology`` tables into a queryable index.

A species wiki card answers "what is a raccoon". Phenology answers "what is it
doing in March, here", and that second question is the one a naturalism practice
is actually built on. The answer varies by region -- raccoons breed two months
apart across their range -- so a window with no region attached is not a fact
about anywhere, and the table carries one per row.

The tables live in note bodies rather than in frontmatter because the vault's
approved-property list is closed and global: a ``phenology`` property would be
inherited by every note type in the vault, and a nested per-region structure
would be stripped on the next filing pass. A body table is cited with footnotes
like every other managed section, renders in Obsidian, and is one row away from
covering a new region when the owner moves.

This module is the compiler between the two. It follows the same rules as
``vault_schema``, which the whole vault toolchain already depends on staying
true:

- Standard library only.
- Fail closed on a malformed row, and say which row and why. A row that cannot
  be read is reported, never silently dropped -- a phenology index that quietly
  omits a species reads exactly like a species with no seasons.
- Deterministic and modelless. The Markdown always wins; the compiled JSON is an
  accelerator keyed by a hash of the inputs.
"""

import json
import re
from pathlib import Path

import vault_wiki
from vault_schema import (
    UserError,
    parse_frontmatter,
    relative_path,
    selected_notes,
    sha256_bytes,
    sha256_text,
    split_frontmatter,
    wikilink_target,
)

COMPILED_PHENOLOGY_VERSION = 1
PHENOLOGY_SECTION = "phenology"
GLOBAL_REGION = "global"

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
MONTH_NUMBERS = {name: index for index, name in enumerate(MONTHS, start=1)}
MONTH_NUMBERS.update({
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "sept": 9,
})
YEAR_ROUND = "year-round"
# En dash and em dash as well as the hyphen: Obsidian's editor and every source a
# window is copied out of use all three interchangeably.
RANGE_SPLIT_RE = re.compile(r"\s*[-‒–—]\s*")

# A scientific name in a note title, after the comma the vault's naming
# convention puts it behind: "Raccoon, Procyon lotor".
TITLE_SPLIT_RE = re.compile(r"^(?P<common>[^,]+?)\s*,\s*(?P<scientific>.+)$")


def load_event_vocabulary(path):
    """The controlled event names per species kind, from phenology-events.json."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise UserError(f"{path}: unreadable event vocabulary: {error}")
    events = raw.get("events")
    if not isinstance(events, dict):
        raise UserError(f"{path} must be an object with an 'events' object")
    vocabulary = {}
    for kind in vault_wiki.SPECIES_KINDS:
        if not isinstance(events.get(kind), dict) or not events[kind]:
            raise UserError(f"{path} has no events for species kind '{kind}'")
        vocabulary[kind] = dict(events[kind])
    return vocabulary


def parse_window(value):
    """A window as an inclusive ``(start_month, end_month)`` pair, or None.

    A range that wraps the new year keeps its direction: ``nov-feb`` compiles to
    ``(11, 2)``, not to ``(2, 11)``. Sorting the pair would turn a four-month
    winter window into an eight-month spring-to-autumn one, which is the same
    error as having no window at all but harder to notice.
    """
    text = (value or "").strip().casefold()
    if not text:
        return None
    if text in (YEAR_ROUND, "year round", "all year", "resident"):
        return (1, 12)
    parts = [part for part in RANGE_SPLIT_RE.split(text) if part]
    if len(parts) == 1:
        month = MONTH_NUMBERS.get(parts[0])
        return (month, month) if month else None
    if len(parts) != 2:
        return None
    start, end = (MONTH_NUMBERS.get(part) for part in parts)
    return (start, end) if start and end else None


def months_in(window):
    """Every month a window covers, wrapping the new year when it has to."""
    start, end = window
    if start <= end:
        return list(range(start, end + 1))
    return list(range(start, 13)) + list(range(1, end + 1))


def note_names(title):
    """A species note's common and scientific names, from its title.

    The vault titles wiki notes ``Canonical Name, Gloss``; for a species that
    reads ``Raccoon, Procyon lotor``. A title with no comma keeps its whole text
    as the common name rather than being refused -- the naming convention is a
    convention, not a schema rule.
    """
    match = TITLE_SPLIT_RE.match(title.strip())
    if not match:
        return title.strip(), ""
    return match.group("common").strip(), match.group("scientific").strip()


def region_of(cell):
    """The region a row names: a wikilink target, or ``global``.

    Anything else is refused. A bare region name looks right and links to
    nothing, so it would quietly become a region the rest of the vault has no
    note for and no way to reach.
    """
    text = (cell or "").strip()
    if not text:
        return None, "region is empty"
    if text.casefold() == GLOBAL_REGION:
        return GLOBAL_REGION, None
    target = wikilink_target(text)
    if not target:
        hint = " (a wikilink inside backticks is not a link)" if "`" in text else ""
        return None, f"region must be a [[wikilink]] or '{GLOBAL_REGION}': {text}{hint}"
    return target.split("|")[0].split("#")[0].strip(), None


def compile_note(rel, title, kind, body, vocabulary, columns):
    """One note's phenology rows, plus the problems that stopped any of them."""
    section = vault_wiki.find_section_text(body, "Phenology")
    if section is None:
        return None, []
    rows, problems = vault_wiki.parse_table(section, columns)
    problems = [f"{rel}: {problem}" for problem in problems]
    common, scientific = note_names(title)
    events = []
    for row in rows:
        event = (row.get("event") or "").strip().casefold()
        if event not in vocabulary[kind]:
            problems.append(f"{rel}: unknown {kind} event '{row.get('event')}'")
            continue
        window = parse_window(row.get("window"))
        if not window:
            problems.append(f"{rel}: unreadable window '{row.get('window')}' for event '{event}'")
            continue
        region, region_problem = region_of(row.get("region"))
        if region_problem:
            problems.append(f"{rel}: {region_problem}")
            continue
        basis = (row.get("basis") or "").strip().casefold()
        # Footnote markers ride in the basis cell, where they attribute the row.
        basis = re.sub(r"\[\^[^\]]+\]", "", basis).strip()
        if basis not in ("sourced", "observed", "inferred"):
            problems.append(f"{rel}: unknown basis '{row.get('basis')}' for event '{event}'")
            continue
        events.append(
            {
                "event": event,
                "start_month": window[0],
                "end_month": window[1],
                "months": months_in(window),
                "region": region,
                "basis": basis,
            }
        )
    record = {
        "note": rel,
        "title": title,
        "common": common,
        "scientific": scientific,
        "kind": kind,
        "events": events,
    }
    return record, problems


def build_index(vault, schema_path, specs, vocabulary):
    """Every species card's phenology, and every row that could not be read.

    Notes are visited in path order so the index is byte-stable across runs and
    its hash means something. The column spec comes from each kind's own entry in
    ``wiki-kinds.json``, so the compiler cannot drift from what the drafter wrote.
    """
    columns = {
        kind: vault_wiki.section_by_id(specs[kind], PHENOLOGY_SECTION)["columns"]
        for kind in vault_wiki.SPECIES_KINDS
    }
    species, problems = [], []
    for path in sorted(selected_notes(vault, schema_path, "vault", None)):
        rel = relative_path(vault, path)
        try:
            split = split_frontmatter(path.read_bytes())
        except OSError as error:
            problems.append(f"{rel}: {error}")
            continue
        if split["malformed"] or not split["had_frontmatter"]:
            continue
        metadata = parse_frontmatter(split["frontmatter_text"])
        kind = vault_wiki.kind_for_metadata(metadata)
        if kind not in vault_wiki.SPECIES_KINDS:
            continue
        record, note_problems = compile_note(
            rel, path.stem, kind, split["body"], vocabulary, columns[kind]
        )
        problems.extend(note_problems)
        if record is not None:
            species.append(record)
    regions = sorted({event["region"] for record in species for event in record["events"]})
    return {
        "version": COMPILED_PHENOLOGY_VERSION,
        "species": species,
        "regions": regions,
        "counts": {
            "species": len(species),
            "events": sum(len(record["events"]) for record in species),
            "unreadable_rows": len(problems),
        },
        "problems": problems,
    }


def index_hash(index):
    return sha256_text(json.dumps(index["species"], ensure_ascii=False, sort_keys=True))


def write_index(cache_dir, index):
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / "phenology.json"
    payload = dict(index)
    payload["hash"] = index_hash(index)
    path.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
    return path, payload["hash"]


def read_index(cache_dir):
    path = Path(cache_dir) / "phenology.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if payload.get("version") == COMPILED_PHENOLOGY_VERSION else None


def expected_in_month(index, month, region=None):
    """Every species event whose window covers a month, optionally in one region.

    ``global`` rows always match: a fact stated range-wide holds wherever the
    owner is. A species with rows only for somewhere else does not appear at all,
    rather than appearing with another region's months -- the whole point of the
    region column is that those are different claims.
    """
    if month not in range(1, 13):
        raise UserError(f"month must be 1 through 12: {month}")
    matches = []
    for record in index.get("species", []):
        events = [
            event
            for event in record["events"]
            if month in event["months"]
            and (region is None or event["region"] == region or event["region"] == GLOBAL_REGION)
        ]
        if events:
            matches.append({**record, "events": events})
    return matches


def species_without_region(index, region):
    """Species cards carrying no phenology for a region -- the work still to do."""
    missing = []
    for record in index.get("species", []):
        if not any(event["region"] in (region, GLOBAL_REGION) for event in record["events"]):
            missing.append(record["note"])
    return sorted(missing)


def source_hash(vault, schema_path, notes):
    """A hash over the inputs, so a stale cache is detectable without a rescan."""
    digest = [sha256_bytes(schema_path.read_bytes())]
    for path in sorted(notes):
        try:
            digest.append(sha256_bytes(path.read_bytes()))
        except OSError:
            continue
    return sha256_text("".join(digest))
