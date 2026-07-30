#!/usr/bin/env python3
"""Shared reflection primitives for the note-writing vault skills.

`vault-capture` and `vault-transcripts` both append a reflection to the notes
they generate, and both face the same question first: which of the material in
hand can a reflection actually cite? The answer does not depend on whether the
material arrived as a braindump or a recording, so the harvesting lives here and
each skill keeps only the parts that differ -- how it names its sections, how it
prompts for them, and where it reads its own source text from.

The section headings are shared too, but their *arrangement* is not: capture
maps them by note kind, transcripts by recording type and pairs each with the
response key it validates. Those are genuinely different tables over the same
vocabulary, so only the vocabulary is here.
"""

import re

# A cited line has to say something its excerpt supports, so a bare URL under a
# `## Sources` list is not a citable source: it carries a link and no claim.
OUTSIDE_SOURCE_MIN_CHARS = 20
OUTSIDE_SOURCE_EXCERPT_CHARS = 300
OUTSIDE_SOURCE_LIMIT = 12
OUTSIDE_SOURCE_READ_BYTES = 40000

URL_RE = re.compile(r"https?://[^\s<>\"'\])]+", re.IGNORECASE)

# A journal's sections are introspective. Everything else gets the working set,
# because "Interpretations" on an errand list is either empty or padding.
JOURNAL_HEADINGS = ("Observations", "Interpretations", "Open questions", "Connections")
WORKING_HEADINGS = ("Context", "Open questions", "Next steps", "Connections")
REFLECTION_HEADINGS = frozenset(JOURNAL_HEADINGS) | frozenset(WORKING_HEADINGS)


def cited_lines(text, source):
    """Lines carrying both a URL and enough prose to be a claim.

    A `## Sources` entry is a bare link, so it cites nothing on its own: there is
    no statement for a reflection to stand behind. A quote-plus-URL line from a
    research import is the opposite, and is the shape this looks for.
    """
    found = []
    for line in str(text).splitlines():
        collapsed = re.sub(r"\s+", " ", line).strip().lstrip("-*+> ").strip()
        urls = URL_RE.findall(collapsed)
        if not urls:
            continue
        prose = collapsed
        for url in urls:
            prose = prose.replace(url, " ")
        if len(re.sub(r"[^0-9A-Za-z]+", "", prose)) < OUTSIDE_SOURCE_MIN_CHARS:
            continue
        found.append(
            {
                "url": urls[0].rstrip(".,;:)"),
                "source": source,
                "excerpt": collapsed[:OUTSIDE_SOURCE_EXCERPT_CHARS],
            }
        )
    return found


def outside_sources(material, material_label, vault, candidates):
    """Outside-the-vault text this pipeline is actually holding, with its URLs.

    Nothing here is fetched. There are exactly two ways outside material can be
    in hand without a network call: the person put a link in the material, or the
    material was researched earlier and imported into a vault note that kept its
    citations. Anything a model merely remembers about the world cannot be checked
    against either, so each skill's reflection gate refuses it.

    ``material_label`` is what a citation from the material itself is credited to
    -- "this braindump", "this recording" -- and appears verbatim in the note.
    """
    harvested = list(cited_lines(material, material_label))
    for candidate in candidates:
        try:
            with (vault / candidate["path"]).open("r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(OUTSIDE_SOURCE_READ_BYTES)
        except OSError:
            continue
        harvested.extend(cited_lines(text, candidate["wikilink"]))
    seen, unique = set(), []
    for entry in harvested:
        if entry["url"] in seen:
            continue
        seen.add(entry["url"])
        unique.append(entry)
        if len(unique) >= OUTSIDE_SOURCE_LIMIT:
            break
    return unique


__all__ = [
    "JOURNAL_HEADINGS",
    "OUTSIDE_SOURCE_EXCERPT_CHARS",
    "OUTSIDE_SOURCE_LIMIT",
    "OUTSIDE_SOURCE_MIN_CHARS",
    "OUTSIDE_SOURCE_READ_BYTES",
    "REFLECTION_HEADINGS",
    "URL_RE",
    "WORKING_HEADINGS",
    "cited_lines",
    "outside_sources",
]
