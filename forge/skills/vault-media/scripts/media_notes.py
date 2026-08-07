"""Rendering a media candidate into a schema-valid note.

Deterministic and modelless. Everything here is either a fetched field or
something the owner said; the one drafted sentence in a media note comes from
``vault-media.py`` and is passed in already written and already verified.

Two rules shape the whole module.

**The Details table is a body table, not frontmatter.** The vault's approved
property list is global and closed — adding `director` would give every note in
the vault a director, and an unapproved key is stripped on the next filing pass.
So per-medium metadata renders as a Markdown table under `## Details`, the shape
``vault_phenology`` established: it renders in Obsidian, it is one row away from
covering a new field, and nothing strips it.

**A rating the owner did not give does not exist.** Not a null, not a zero, not a
provider's average dressed up as a personal score — the key is absent. Every
media provider hands back a rating of some kind (Metacritic, TMDB vote average,
IGDB aggregate) and every one of them is a fact about other people. They are
recorded in the Details table under their own names, where they cannot be
mistaken for the owner's judgment.
"""

import re

MEDIUM_SUBDOMAIN = {
    "book": "books",
    "music": "music",
    "game": "games",
    "movie": "movies",
    "show": "shows",
}

MEDIUM_HUB = {
    "book": "00 Books",
    "music": "00 Music",
    "game": "00 Games",
    "movie": "00 Movies",
    "show": "00 Shows",
}

# Named by medium rather than by verb, because Obsidian resolves a wikilink by
# basename and two notes called "00 To Watch" — one for films, one for shows —
# would be ambiguous from anywhere else in the vault.
MEDIUM_BACKLOG = {
    "book": "00 Books to Read",
    "music": "00 Music to Hear",
    "game": "00 Games to Play",
    "movie": "00 Films to Watch",
    "show": "00 Shows to Watch",
}

# Which provider fields become Details rows, in order, per medium. A field the
# provider did not return is skipped rather than rendered empty: a table row
# reading "Director: —" asserts that the record has no director, which is a
# different claim from not having asked.
DETAIL_ROWS = {
    "book": [
        ("firstPublished", "First published"),
        ("authors", "Author"),
        ("publisher", "Publisher"),
        ("pages", "Pages"),
        ("isbn", "ISBN"),
        ("subjects", "Subjects"),
    ],
    "music": [
        ("released", "Released"),
        ("artists", "Artist"),
        ("releaseType", "Type"),
        ("secondaryTypes", "Also"),
    ],
    "game": [
        ("released", "Released"),
        ("developers", "Developer"),
        ("publishers", "Publisher"),
        ("genres", "Genres"),
        ("platforms", "Platforms"),
        ("metacritic", "Metacritic"),
        ("rating", "Critic aggregate"),
    ],
    "movie": [
        ("released", "Released"),
        ("originalTitle", "Original title"),
        ("originalLanguage", "Language"),
        ("voteAverage", "TMDB average"),
    ],
    "show": [
        ("premiered", "Premiered"),
        ("ended", "Ended"),
        ("status", "Status"),
        ("network", "Network"),
        ("genres", "Genres"),
        ("runtime", "Runtime"),
        ("language", "Language"),
        ("voteAverage", "TMDB average"),
    ],
}

PROVIDER_LINK_FIELD = {
    "openlibrary": ("Open Library", "openLibraryKey"),
    "musicbrainz": ("MusicBrainz", "mbid"),
    "tvmaze": ("TVmaze", None),
    "steam": ("Steam", "appid"),
    "tmdb": ("TMDB", "tmdbId"),
    "igdb": ("IGDB", "igdbId"),
}

ILLEGAL_FILENAME = re.compile(r'[/\\:*?"<>|]')
UNRESOLVABLE_FILENAME = {"#": "", "^": "", "[": "(", "]": ")", "|": "-"}


def safe_filename(title):
    """A filename Obsidian can reach with a wikilink and sync to mobile.

    The schema's rule, applied at the one moment a name is cheap to change:
    ``[`` and ``]`` become parentheses, ``|`` becomes a dash, and the characters
    that are illegal in a path are removed.
    """
    name = str(title or "").strip()
    for bad, good in UNRESOLVABLE_FILENAME.items():
        name = name.replace(bad, good)
    name = ILLEGAL_FILENAME.sub("", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name


def _render_value(value):
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(v).strip() for v in value if str(v).strip()]
        return ", ".join(parts) if parts else None
    return str(value).strip()


def details_table(medium, item):
    """The ``## Details`` rows for one candidate, as ``[(label, value), ...]``."""
    detail = item.get("detail") or {}
    rows = []
    for key, label in DETAIL_ROWS.get(medium, []):
        rendered = _render_value(detail.get(key))
        if rendered is not None:
            rows.append((label, rendered))
    label, id_field = PROVIDER_LINK_FIELD.get(item.get("provider"), (item.get("provider"), None))
    url = item.get("url")
    if url:
        identifier = detail.get(id_field) if id_field else None
        text = str(identifier).rsplit("/", 1)[-1] if identifier else (item.get("externalId") or "link")
        rows.append((label, f"[{text}]({url})"))
    return rows


def _escape_cell(text):
    # A pipe inside a cell ends the cell. Titles genuinely contain them.
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_details(rows):
    if not rows:
        return ""
    lines = ["| Field | Value |", "| --- | --- |"]
    lines.extend(f"| {_escape_cell(label)} | {_escape_cell(value)} |" for label, value in rows)
    return "\n".join(lines)


def frontmatter(property_order, values):
    """YAML frontmatter with properties in the schema's declared order.

    Order comes from the schema rather than from this file, so a property added
    to the vault lands in the right place here without an edit.
    """
    lines = ["---"]
    for key in property_order:
        if key not in values:
            continue
        value = values[key]
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}:")
            lines.extend(f"  - {_yaml_scalar(v)}" for v in value)
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _yaml_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    # Wikilinks and URLs must be quoted; the schema requires it for the former
    # and a bare `https://…` is a YAML comment hazard after a `#`.
    if text.startswith("[[") or "://" in text or ":" in text or text.startswith(("*", "&", "!", ">", "|", "%", "@")):
        return '"' + text.replace('"', '\\"') + '"'
    return text


def build_note(
    *,
    medium,
    item,
    property_order,
    lead,
    thoughts=None,
    rating=None,
    status="complete",
    date=None,
    parent=None,
    provenance=None,
):
    """A complete media note as ``(filename, text)``.

    ``rating`` and ``thoughts`` are passed through exactly as given and are
    omitted entirely when absent. Nothing in this function can invent either.
    """
    if rating is not None:
        rating = int(rating)
        if not 1 <= rating <= 10:
            raise ValueError(f"rating must be from 1 to 10, got {rating}")

    values = {
        "type": "work",
        "status": status,
        "domain": "entertainment",
        "subdomain": MEDIUM_SUBDOMAIN[medium],
        "parent": f"[[{parent}]]" if parent else None,
        "capture_type": "generated",
        "date": date,
        "rating": rating,
        "cover": item.get("cover"),
    }

    body = [frontmatter(property_order, values), "", f"# {item.get('title')}", ""]
    if lead:
        body.extend(["> [!summary]", *[f"> {line}" for line in str(lead).strip().splitlines()], ""])

    rows = details_table(medium, item)
    if rows:
        body.extend(["## Details", "", render_details(rows), ""])

    body.append("## Thoughts")
    body.append("")
    if thoughts and str(thoughts).strip():
        body.append(str(thoughts).strip())
    body.append("")

    body.extend(["## Sources", ""])
    if item.get("url"):
        label = PROVIDER_LINK_FIELD.get(item.get("provider"), (item.get("provider"), None))[0]
        body.append(f"- [{label}]({item['url']})")
        body.append("")

    body.extend(["## Notes", ""])

    if provenance:
        body.extend(["> [!provenance]-", *[f"> {line}" for line in str(provenance).strip().splitlines()], ""])

    text = "\n".join(body).rstrip() + "\n"
    return safe_filename(item.get("title")) + ".md", text


BACKLOG_COLUMNS = ["Title", "Year", "Why", "Source"]


def backlog_row(item, why=None):
    label, _id_field = PROVIDER_LINK_FIELD.get(item.get("provider"), (item.get("provider"), None))
    source = f"[{label}]({item['url']})" if item.get("url") else ""
    return [
        _escape_cell(item.get("title") or ""),
        str(item.get("year") or ""),
        _escape_cell(why or ""),
        source,
    ]


def render_backlog_table(rows):
    lines = ["| " + " | ".join(BACKLOG_COLUMNS) + " |", "| " + " | ".join("---" for _ in BACKLOG_COLUMNS) + " |"]
    lines.extend("| " + " | ".join(row) + " |" for row in rows)
    return "\n".join(lines)


def parse_backlog_table(text):
    """Read an existing backlog table back into rows, tolerating hand edits.

    The owner will add a line by hand and it must survive a promote. Anything
    that is not a well-formed row is left alone by the caller rather than
    silently dropped, which is why this returns positions as well as values.
    """
    rows = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|") or not stripped.endswith("|"):
            continue
        cells = [c.strip() for c in stripped[1:-1].split("|")]
        if not cells or all(set(c) <= {"-", ":"} for c in cells if c):
            continue
        if [c.casefold() for c in cells[: len(BACKLOG_COLUMNS)]] == [c.casefold() for c in BACKLOG_COLUMNS]:
            continue
        rows.append({"line": index, "cells": cells})
    return rows
