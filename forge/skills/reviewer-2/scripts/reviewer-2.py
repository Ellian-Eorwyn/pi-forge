#!/usr/bin/env python3
"""Peer-review an article note without touching it.

Reviewer 2 is a joke about a real failure: the reviewer who is right about the
problem and useless about the remedy. This pipeline keeps the criticality and
adds what the joke is missing — every objection arrives with the fix, and where
the fix needs literature, the literature is real and was actually retrieved.

The agent authors the review. That is deliberate: a critique is a deliverable,
and deliverables stay agent-authored. This script is the machinery around it —
it splits the article into anchorable blocks, refuses comments that misquote it,
refuses citations that no research run contains, batches the whole set past the
thinking model for review, and renders the result.

Nothing here can modify the article. The output is a new note: the article's
body reproduced byte for byte with comment callouts interleaved, a meta review
appended, and a fix plan the author can work through in order. The guarantee is
mechanical rather than promised — before anything is written, the rendered copy
has this run's comment blocks stripped back out and the result must equal the
original body byte for byte. A render that cannot prove it writes nothing.
"""

import argparse
import datetime
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_llm
import forge_verify
import run_state
from vault_schema import (
    INBOX_DIR,
    PROTECTED_DIRS,
    UserError,
    compiled_schema_for,
    note_title,
    path_is_inside,
    relative_path,
    resolve_schema_path,
    safe_title,
    serialize_frontmatter,
    sha256_bytes,
    split_frontmatter,
)

WORKFLOW = "reviewer-2"
PROMPT_VERSION = "reviewer-2-v1"
STATE_DIR = ".reviewer-2"
COMMENTS_SCHEMA_VERSION = 1

# Each category gets its own callout type, so a reader scanning the margin sees
# what kind of objection it is before opening it. The labels are part of the
# marker line and therefore part of the strip grammar: changing one changes what
# an already-rendered review copy round-trips as, which is why LEGACY_CALLOUTS
# below still exists.
#
# The types are prefixed because a review comment is apparatus about an article,
# not a claim inside a note, and the vault's own vocabulary
# (`99 Meta/99.02 Schemas/0.04 Note Format.md`) reads the unprefixed names as the
# latter. Borrowing them put a criticism of an article's structure in the same
# cyan as a note's summary, and a `strength` in the green that means "sourced and
# checkable". Prefixing keeps the two vocabularies from ever colliding again.
CATEGORIES = {
    "gap": ("r2-gap", "Research gap"),
    "evidence": ("r2-evidence", "Thin evidence"),
    "logic": ("r2-logic", "Logic"),
    "theory": ("r2-theory", "Theory"),
    "structure": ("r2-structure", "Structure"),
    "strength": ("r2-strength", "Strength"),
}
# The unprefixed types this skill wrote before the rename. Read, never written: a
# review copy rendered by an earlier version has to keep round-tripping, and the
# strip grammar is the only thing standing between its comments and the author's
# own prose.
LEGACY_CALLOUTS = ("question", "warning", "failure", "example", "abstract", "success")
SEVERITIES = ("major", "minor")
# A criticism that the literature does not yet say enough about this needs the
# literature attached. A logical or structural repair does not: a bridge
# paragraph the author writes themselves cites nothing.
CITATION_REQUIRED_CATEGORIES = {"gap", "evidence", "theory"}
HEDGE_PHRASE = "verify against full text"
EVIDENCE_LEVELS = ("metadata", "abstract", "full_text")

MAX_TITLE_CHARS = 120
FILENAME_SUFFIX = "Reviewer 2"
INVENTORY_KINDS = {"paragraph", "quote", "list"}
INVENTORY_MIN_CHARS = 120

CALLOUT_ALTERNATION = "|".join(
    sorted({callout for callout, _label in CATEGORIES.values()} | set(LEGACY_CALLOUTS), key=len, reverse=True)
)
MARKER_RE = re.compile(rf"^> \[!(?:{CALLOUT_ALTERNATION})\]- R2 (r-\d{{3}}) · [^·\n]+?(?: · (?:major|minor))?\s*$")
COMMENT_ID_RE = re.compile(r"^r-\d{3}$")
SENTINEL_PREFIX = "%% R2 review boundary "
SENTINEL_RE = re.compile(r"^%% R2 review boundary \S+ - content below is reviewer-generated %%\s*$")

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^ {0,3}#{1,6}(\s|$)")
RULE_RE = re.compile(r"^ {0,3}(\*[ \t]*){3,}$|^ {0,3}(-[ \t]*){3,}$|^ {0,3}(_[ \t]*){3,}$")
SETEXT_RE = re.compile(r"^ {0,3}(=+|-+)\s*$")
LIST_RE = re.compile(r"^ {0,3}([-*+]|\d{1,9}[.)])(\s|$)")
QUOTE_RE = re.compile(r"^ {0,3}>")
HTML_RE = re.compile(r"^ {0,3}<")
TABLE_DIVIDER_RE = re.compile(r"^ {0,3}\|?[\s:|-]*-[\s:|-]*\|?\s*$")


# --------------------------------------------------------------------------- #
# Output plumbing
# --------------------------------------------------------------------------- #


class CommentsError(Exception):
    """Everything wrong with a comments file, reported at once.

    A validation pass that stops at the first problem makes the agent fix one
    comment per round trip. All of them at once is one revision.
    """

    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def structured(status, artifacts=None, warnings=None, errors=None, data=None):
    return {
        "status": status,
        "artifacts": artifacts or [],
        "warnings": warnings or [],
        "errors": errors or [],
        "data": data,
    }


def error_entry(code, message):
    return {"code": code, "message": message}


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def log(args, message):
    if getattr(args, "verbose", False):
        print(message, file=sys.stderr, flush=True)


def progress(message):
    print(message, file=sys.stderr, flush=True)


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def unique_run_directory(vault):
    base = vault / STATE_DIR / "runs"
    base.mkdir(parents=True, exist_ok=True)
    stamp = utc_timestamp()
    candidate = base / f"{stamp}-review"
    suffix = 2
    while candidate.exists():
        candidate = base / f"{stamp}-review-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    return candidate


def one_line(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalized(value):
    """Whitespace- and case-insensitive form, for quote containment.

    Markdown hard-wraps, so a quotation that is byte-exact in the source is not
    byte-exact in a JSON field that reflowed it. Matching on this form keeps the
    check honest without punishing the wrap.
    """
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def contains_quote(haystack, needle):
    quote = normalized(needle)
    return not quote or quote in normalized(haystack)


# --------------------------------------------------------------------------- #
# Indexing
# --------------------------------------------------------------------------- #


def closing_fence(plain, opener):
    match = FENCE_RE.match(plain)
    if not match:
        return False
    marker = match.group(1)
    if marker[0] != opener[0] or len(marker) < len(opener):
        return False
    return not plain.strip()[len(marker):].strip()


def parse_blocks(body):
    """Split a note body into the units a comment can anchor to.

    Fences are atomic and everything inside one is invisible to the rest of the
    parser, which is the only reason an article may quote this skill's own
    comment syntax in a code block without confusing it.
    """
    plain = [line.rstrip("\n").rstrip("\r") for line in body.splitlines()]
    blocks = []
    index = 0
    counter = 0
    while index < len(plain):
        if not plain[index].strip():
            index += 1
            continue
        start = index
        fence = FENCE_RE.match(plain[index])
        if fence:
            opener = fence.group(1)
            index += 1
            while index < len(plain):
                if closing_fence(plain[index], opener):
                    index += 1
                    break
                index += 1
            kind = "fence"
        elif HEADING_RE.match(plain[index]):
            kind = "heading"
            index += 1
        elif RULE_RE.match(plain[index]):
            kind = "rule"
            index += 1
        elif QUOTE_RE.match(plain[index]):
            kind = "r2-comment" if MARKER_RE.match(plain[index]) else "quote"
            index += 1
            while index < len(plain) and QUOTE_RE.match(plain[index]):
                index += 1
        else:
            if LIST_RE.match(plain[index]):
                kind = "list"
            elif HTML_RE.match(plain[index]):
                kind = "html"
            elif (
                "|" in plain[index]
                and index + 1 < len(plain)
                and "|" in plain[index + 1]
                and TABLE_DIVIDER_RE.match(plain[index + 1])
            ):
                kind = "table"
            else:
                kind = "paragraph"
            index += 1
            while index < len(plain) and plain[index].strip():
                if SETEXT_RE.match(plain[index]) and kind == "paragraph":
                    index += 1
                    break
                if (
                    FENCE_RE.match(plain[index])
                    or HEADING_RE.match(plain[index])
                    or QUOTE_RE.match(plain[index])
                    or RULE_RE.match(plain[index])
                ):
                    break
                index += 1
        counter += 1
        blocks.append(
            {
                "id": f"b-{counter:03d}",
                "kind": kind,
                "text": "\n".join(plain[start:index]),
                "start_line": start + 1,
                "end_line": index,
            }
        )
    return blocks


def heading_outline(blocks):
    outline = []
    for block in blocks:
        if block["kind"] != "heading":
            continue
        text = block["text"].strip()
        level = len(text) - len(text.lstrip("#"))
        outline.append({"id": block["id"], "level": level, "text": text.lstrip("#").strip()})
    return outline


def heading_for(blocks, anchor_id):
    """The nearest heading above a block, so a reviewer of the comment knows
    which section the passage sits in."""
    current = ""
    for block in blocks:
        if block["kind"] == "heading":
            current = block["text"].lstrip("#").strip()
        if block["id"] == anchor_id:
            return current
    return current


def reserved_comment_ids(blocks):
    ids = []
    for block in blocks:
        if block["kind"] != "r2-comment":
            continue
        match = MARKER_RE.match(block["text"].splitlines()[0])
        if match:
            ids.append(match.group(1))
    return sorted(set(ids))


def next_comment_id(reserved):
    highest = max((int(value.split("-")[1]) for value in reserved), default=0)
    return f"r-{highest + 1:03d}"


def read_article(path):
    return read_article_bytes(path.read_bytes(), path.name)


def read_article_text(text):
    return read_article_bytes(text.encode("utf-8"), "article")


def read_article_bytes(data, label):
    split = split_frontmatter(data)
    if split["malformed"]:
        raise UserError(f"{label} opens a frontmatter block that never closes")
    body = split["body"]
    return {
        "body": body,
        "frontmatter_text": split["frontmatter_text"],
        "had_bom": split["had_bom"],
        "file_sha256": sha256_bytes(data),
        "body_sha256": sha256_bytes(body.encode("utf-8")),
        "final_newline": body.endswith("\n"),
    }


# --------------------------------------------------------------------------- #
# Citation register
# --------------------------------------------------------------------------- #


def normalize_doi(value):
    text = re.sub(r"^https?://(dx\.)?doi\.org/", "", str(value or "").strip(), flags=re.IGNORECASE)
    text = re.sub(r"^doi:\s*", "", text, flags=re.IGNORECASE).lower()
    return text or None


def normalize_arxiv(value):
    text = str(value or "").strip()
    text = re.sub(r"^https?://arxiv\.org/abs/", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^arxiv:", "", text, flags=re.IGNORECASE)
    text = re.sub(r"v\d+$", "", text, flags=re.IGNORECASE)
    return text or None


def normalize_title_key(value):
    text = unicodedata.normalize("NFKD", re.sub(r"\s+", " ", str(value or "")).strip().lower())
    text = "".join(character for character in text if not unicodedata.combining(character))
    return re.sub(r"\s+", " ", re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)).strip()


def author_display(author):
    if isinstance(author, str):
        return one_line(author)
    if not isinstance(author, dict):
        return ""
    parts = [author.get("family"), author.get("given")]
    named = [one_line(part) for part in parts if part]
    if named:
        return ", ".join(named)
    return one_line(author.get("name"))


def author_family(author):
    if isinstance(author, dict) and author.get("family"):
        return one_line(author["family"])
    display = author_display(author)
    return display.split(",")[0].strip() if display else ""


def work_keys(work):
    identifiers = work.get("identifiers") or {}
    keys = []
    doi = normalize_doi(identifiers.get("doi"))
    if doi:
        keys.append(f"doi:{doi}")
    if identifiers.get("pmid"):
        keys.append(f"pmid:{one_line(identifiers['pmid'])}")
    arxiv = normalize_arxiv(identifiers.get("arxiv_id"))
    if arxiv:
        keys.append(f"arxiv:{arxiv}")
    title_key = work.get("normalized_title") or normalize_title_key(work.get("canonical_title"))
    keys.append(f"title:{title_key}|{work.get('publication_year') or ''}")
    if work.get("work_id"):
        keys.append(f"work:{work['work_id']}")
    return keys


def read_jsonl(path):
    rows, _warnings = run_state.read_jsonl_recover_tail(path, repair=False)
    return rows


def load_academic_run(directory, register, warnings):
    works = read_jsonl(directory / "works.jsonl")
    if not works:
        warnings.append(f"{directory.name} is an academic run with no works")
    for work in works:
        abstract = one_line(work.get("abstract_best"))
        urls = work.get("urls") or []
        oa = [entry.get("url") for entry in work.get("oa_locations") or [] if isinstance(entry, dict)]
        doi = normalize_doi((work.get("identifiers") or {}).get("doi"))
        entry = {
            "run": str(directory),
            "run_kind": "academic",
            "title": one_line(work.get("canonical_title")),
            "authors": [author_display(author) for author in work.get("authors") or []],
            "families": [author_family(author) for author in work.get("authors") or []],
            "year": work.get("publication_year"),
            "venue": one_line(work.get("venue_name")),
            "publisher": one_line(work.get("publisher")),
            "doi": doi,
            "url": next((url for url in [*urls, *oa] if url), None),
            "abstract": abstract,
            # An academic run reads catalogue metadata, never the article. It
            # can prove a work exists and what it is about; it cannot prove what
            # the work found.
            "evidence_level": "abstract" if abstract else "metadata",
            "quotes": [],
            "text": "",
        }
        keys = work_keys(work)
        entry["key"] = keys[0]
        for key in keys:
            register.setdefault(key, entry)


def source_doi(source):
    for field in ("canonicalUrl", "finalUrl", "sourceUrl"):
        doi = normalize_doi(source.get(field))
        if doi and doi.startswith("10."):
            return doi
    return None


def load_deep_run(directory, register, warnings):
    index = json.loads((directory / "source_index.json").read_text(encoding="utf-8"))
    sources = index.get("sources") or []
    evidence = read_jsonl(directory / "evidence_items.jsonl")
    quotes = {}
    excluded = 0
    for item in evidence:
        quote = one_line(item.get("directQuote"))
        if not quote:
            continue
        # A quote the upstream reviewer doubted is not support this skill may
        # lean on. It stays in the research run; it does not reach a citation.
        if (item.get("verification") or {}).get("verdict") == forge_verify.VERDICT_FLAG:
            excluded += 1
            continue
        quotes.setdefault(item.get("sourceId"), []).append(quote)
    if excluded:
        warnings.append(f"{directory.name}: {excluded} quote(s) flagged in the research run were left out of the register")
    for source in sources:
        source_id = source.get("sourceId")
        if not source_id:
            continue
        text = ""
        output_path = source.get("outputPath")
        if output_path:
            candidate = (directory / output_path).resolve()
            if path_is_inside(directory, candidate) and candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
        entry = {
            "run": str(directory),
            "run_kind": "deep",
            "title": one_line(source.get("title")) or one_line(source.get("finalUrl")),
            "authors": [],
            "families": [],
            "year": (str(source.get("accessDate") or "")[:4] or None),
            "venue": "",
            "publisher": "",
            "doi": source_doi(source),
            "url": source.get("canonicalUrl") or source.get("finalUrl") or source.get("sourceUrl"),
            "abstract": "",
            "accessed": source.get("accessDate"),
            # Archived text is the only evidence level that lets a citation
            # assert what a source actually says.
            "evidence_level": "full_text" if text.strip() else "metadata",
            "quotes": quotes.get(source_id, []),
            "text": text,
        }
        entry["key"] = f"source:{source_id}"
        register.setdefault(entry["key"], entry)
        if entry["doi"]:
            register.setdefault(f"doi:{entry['doi']}", entry)


def load_register(paths, warnings):
    register = {}
    runs = []
    for raw in paths:
        directory = Path(raw).expanduser().resolve()
        if not directory.is_dir():
            raise UserError(f"research run directory does not exist: {directory}")
        if (directory / "academic_run.json").is_file():
            load_academic_run(directory, register, warnings)
        elif (directory / "source_index.json").is_file():
            load_deep_run(directory, register, warnings)
        else:
            raise UserError(
                f"{directory} is not a web-research run; expected academic_run.json or source_index.json in it"
            )
        runs.append(str(directory))
    return register, runs


def resolve_citation(register, work):
    text = one_line(work)
    if not text:
        return None
    if text.lower().startswith("doi:"):
        doi = normalize_doi(text[4:])
        return register.get(f"doi:{doi}") if doi else None
    if text.lower().startswith("arxiv:"):
        arxiv = normalize_arxiv(text[6:])
        return register.get(f"arxiv:{arxiv}") if arxiv else None
    if text.lower().startswith("pmid:"):
        return register.get(f"pmid:{text[5:].strip()}")
    return register.get(text)


def citation_label(entry):
    families = [family for family in entry.get("families") or [] if family]
    year = entry.get("year") or "n.d."
    if not families:
        return f"({entry.get('title') or entry.get('url') or 'source'}, {year})"
    if len(families) == 1:
        return f"({families[0]}, {year})"
    if len(families) == 2:
        return f"({families[0]} & {families[1]}, {year})"
    return f"({families[0]} et al., {year})"


def reference_line(entry):
    parts = []
    authors = [author for author in entry.get("authors") or [] if author]
    if authors:
        listed = authors[:3]
        text = "; ".join(listed)
        if len(authors) > 3:
            text += "; et al."
        parts.append(text)
    parts.append(f"({entry.get('year') or 'n.d.'})")
    if entry.get("title"):
        parts.append(f"{entry['title'].rstrip('.')}.")
    if entry.get("venue"):
        parts.append(f"{entry['venue'].rstrip('.')}.")
    if entry.get("publisher") and entry.get("publisher") != entry.get("venue"):
        parts.append(f"{entry['publisher'].rstrip('.')}.")
    if entry.get("doi"):
        parts.append(f"https://doi.org/{entry['doi']}")
    elif entry.get("url"):
        parts.append(entry["url"])
    if entry.get("accessed"):
        parts.append(f"(accessed {entry['accessed'][:10]})")
    return " ".join(part for part in parts if part)


# --------------------------------------------------------------------------- #
# Comment validation
# --------------------------------------------------------------------------- #


def renders_as_marker(line):
    return bool(MARKER_RE.match(f"> {line}") or MARKER_RE.match(f"> > {line}"))


def check_free_text(label, value, problems):
    for line in str(value or "").splitlines():
        if renders_as_marker(line):
            problems.append(f"{label} contains a line that would render as a comment marker: {one_line(line)!r}")
        if SENTINEL_PREFIX in line:
            problems.append(f"{label} contains the review boundary marker")


def validate_comments(payload, index, blocks, register):
    """Everything that can be decided without a model, decided before one runs."""
    problems = []
    warnings = []
    if not isinstance(payload, dict):
        raise CommentsError(["comments file is not a JSON object"])
    if payload.get("schema_version") != COMMENTS_SCHEMA_VERSION:
        problems.append(f"schema_version must be {COMMENTS_SCHEMA_VERSION}")
    article = payload.get("article") if isinstance(payload.get("article"), dict) else {}
    if article.get("path") and article["path"] != index["article"]["path"]:
        problems.append(f"article.path {article['path']!r} is not the indexed article {index['article']['path']!r}")
    if article.get("body_sha256") and article["body_sha256"] != index["article"]["body_sha256"]:
        problems.append("article.body_sha256 does not match the indexed article; re-run index and re-read the article")

    anchorable = {block["id"] for block in blocks if block["kind"] != "r2-comment"}
    block_by_id = {block["id"]: block for block in blocks}
    reserved = set(index.get("reserved_ids") or [])

    raw_comments = payload.get("comments")
    if not isinstance(raw_comments, list) or not raw_comments:
        problems.append("comments must be a non-empty array")
        raise CommentsError(problems)

    comments = []
    seen_ids = set()
    for position, entry in enumerate(raw_comments, start=1):
        label = f"comment {position}"
        if not isinstance(entry, dict):
            problems.append(f"{label} is not an object")
            continue
        identifier = one_line(entry.get("id"))
        label = f"comment {identifier or position}"
        if not COMMENT_ID_RE.match(identifier):
            problems.append(f"{label} has an id that is not r-NNN")
        elif identifier in seen_ids:
            problems.append(f"{label} repeats an id")
        elif identifier in reserved:
            problems.append(f"{label} reuses an id already present in the article from an earlier review")
        seen_ids.add(identifier)

        category = entry.get("category")
        if category not in CATEGORIES:
            problems.append(f"{label} has an unknown category {category!r}")
            continue
        severity = entry.get("severity")
        if category == "strength":
            if severity:
                problems.append(f"{label} is a strength and takes no severity")
            severity = None
        elif severity not in SEVERITIES:
            problems.append(f"{label} must have severity major or minor")

        anchor = one_line(entry.get("anchor"))
        if anchor not in block_by_id:
            problems.append(f"{label} anchors to {anchor!r}, which is not a block in this article")
        elif anchor not in anchorable:
            problems.append(f"{label} anchors to {anchor}, which is a comment from an earlier review")

        critique = str(entry.get("critique") or "").strip()
        if not critique:
            problems.append(f"{label} has no critique")
        check_free_text(f"{label} critique", critique, problems)

        fix = str(entry.get("fix") or "").strip()
        if category == "strength":
            if fix:
                problems.append(f"{label} is a strength and takes no fix")
            fix = ""
        elif not fix:
            problems.append(f"{label} names a problem without saying what to do about it")
        check_free_text(f"{label} fix", fix, problems)

        insert_text = str(entry.get("insert_text") or "").strip()
        if insert_text and category == "strength":
            problems.append(f"{label} is a strength and takes no suggested text")
            insert_text = ""
        check_free_text(f"{label} insert_text", insert_text, problems)

        quoted = str(entry.get("quoted_text") or "").strip()
        if quoted and anchor in block_by_id and not contains_quote(block_by_id[anchor]["text"], quoted):
            problems.append(f"{label} quotes text that is not in {anchor}")

        citations = entry.get("citations") or []
        if not isinstance(citations, list):
            problems.append(f"{label} citations must be an array")
            citations = []
        resolved = []
        for citation_position, citation in enumerate(citations, start=1):
            citation_label_text = f"{label} citation {citation_position}"
            if not isinstance(citation, dict):
                problems.append(f"{citation_label_text} is not an object")
                continue
            key = one_line(citation.get("key"))
            work = one_line(citation.get("work"))
            if not key:
                problems.append(f"{citation_label_text} has no key")
            entry_record = resolve_citation(register, work)
            if entry_record is None:
                problems.append(
                    f"{citation_label_text} cites {work!r}, which is not in any linked research run; "
                    "run web-research for it or drop the citation"
                )
                continue
            quote = one_line(citation.get("quote"))
            if quote:
                if entry_record["evidence_level"] != "full_text":
                    problems.append(
                        f"{citation_label_text} quotes a work whose full text was never retrieved; "
                        "cite the finding without a quotation, or run a deep research pass on it"
                    )
                elif not contains_quote(entry_record["text"], quote):
                    problems.append(f"{citation_label_text} quotes text that is not in the archived source")
            resolved.append({"key": key, "work": work, "entry": entry_record, "quote": quote})

        if insert_text and category in CITATION_REQUIRED_CATEGORIES and not resolved:
            problems.append(
                f"{label} suggests text for a {category} comment without citing anything; "
                "a claim about what the literature says needs the literature"
            )
        for record in resolved:
            record_entry = record["entry"]
            year = str(record_entry.get("year") or "")
            families = [family for family in record_entry.get("families") or [] if family]
            if not insert_text:
                continue
            if year and year not in insert_text:
                problems.append(f"{label} cites {record['key']} but its year {year} does not appear in the suggested text")
            if families and not any(family in insert_text for family in families):
                problems.append(
                    f"{label} cites {record['key']} but no author name from it appears in the suggested text"
                )

        thin = [record for record in resolved if record["entry"]["evidence_level"] != "full_text"]
        if thin and HEDGE_PHRASE not in f"{critique}\n{fix}".casefold():
            problems.append(
                f"{label} rests on {len(thin)} citation(s) read only as metadata or abstract, "
                f"so the comment must tell the author to {HEDGE_PHRASE}"
            )

        comments.append(
            {
                "id": identifier,
                "anchor": anchor,
                "category": category,
                "severity": severity,
                "quoted_text": quoted,
                "critique": critique,
                "fix": fix,
                "insert_text": insert_text,
                "citations": resolved,
            }
        )

    meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    assessment = str(meta.get("assessment") or "").strip()
    if not assessment:
        problems.append("meta.assessment is required; the review needs an overall verdict")
    check_free_text("meta.assessment", assessment, problems)
    weaknesses = validate_meta_list(meta.get("weaknesses"), "weakness", "rank", seen_ids, problems)
    fix_plan = validate_meta_list(meta.get("fix_plan"), "fix_plan step", "step", seen_ids, problems)
    if not fix_plan:
        problems.append("meta.fix_plan is required; a review without an order of operations is a list of complaints")

    if problems:
        raise CommentsError(problems)

    planned = {identifier for step in fix_plan for identifier in step["comment_ids"]}
    unplanned = [comment["id"] for comment in comments if comment["severity"] == "major" and comment["id"] not in planned]
    if unplanned:
        warnings.append(f"major comments missing from the fix plan: {', '.join(unplanned)}")
    comments.sort(key=lambda comment: comment["id"])
    return comments, {"assessment": assessment, "weaknesses": weaknesses, "fix_plan": fix_plan}, warnings


def validate_meta_list(value, label, number_key, known_ids, problems):
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"meta.{label} must be an array")
        return []
    entries = []
    for position, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            problems.append(f"{label} {position} is not an object")
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            problems.append(f"{label} {position} has no text")
        check_free_text(f"{label} {position}", text, problems)
        identifiers = entry.get("comment_ids") or []
        if not isinstance(identifiers, list):
            problems.append(f"{label} {position} comment_ids must be an array")
            identifiers = []
        unknown = [identifier for identifier in identifiers if identifier not in known_ids]
        if unknown:
            problems.append(f"{label} {position} names comments that do not exist: {', '.join(map(str, unknown))}")
        entries.append(
            {
                "number": entry.get(number_key) if isinstance(entry.get(number_key), int) else position,
                "text": text,
                "comment_ids": [identifier for identifier in identifiers if identifier in known_ids],
            }
        )
    entries.sort(key=lambda entry: entry["number"])
    return entries


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def quoted_lines(text, prefix):
    lines = []
    for line in str(text or "").splitlines():
        lines.append(prefix.rstrip() if not line.strip() else f"{prefix}{line}")
    return lines


def comment_block_lines(comment, flag_reason=None):
    callout, label = CATEGORIES[comment["category"]]
    title = f"> [!{callout}]- R2 {comment['id']} · {label}"
    if comment["severity"]:
        title += f" · {comment['severity']}"
    lines = [title]
    if flag_reason:
        lines.append(f"> **Flagged in verification:** {one_line(flag_reason)}")
    if comment["quoted_text"]:
        lines.append(f"> On: “{one_line(comment['quoted_text'])}”")
        lines.append(">")
    lines.extend(quoted_lines(comment["critique"], "> "))
    if comment["fix"]:
        lines.append(">")
        lines.extend(quoted_lines(f"**What to do:** {comment['fix']}", "> "))
    if comment["insert_text"]:
        lines.append(">")
        lines.append("> > [!quote]+ Suggested text")
        lines.extend(quoted_lines(comment["insert_text"], "> > "))
        if comment["citations"]:
            labels = []
            for citation in comment["citations"]:
                text = citation_label(citation["entry"])
                if text not in labels:
                    labels.append(text)
            lines.append("> >")
            lines.append(f"> > — cites {'; '.join(labels)}")
    return lines


def build_tail(stamp, meta, references, provenance):
    lines = [f"{SENTINEL_PREFIX}{stamp} - content below is reviewer-generated %%", "", "---", ""]
    lines.append("## Reviewer 2 · Meta Review")
    lines.append("")
    lines.extend(meta["assessment"].splitlines())
    lines.append("")
    if meta["weaknesses"]:
        lines.extend(["### Biggest weaknesses", ""])
        for position, entry in enumerate(meta["weaknesses"], start=1):
            reference = f" ({', '.join(entry['comment_ids'])})" if entry["comment_ids"] else ""
            lines.append(f"{position}. {entry['text']}{reference}")
        lines.append("")
    lines.extend(["### Fix plan", ""])
    for position, entry in enumerate(meta["fix_plan"], start=1):
        reference = f" ({', '.join(entry['comment_ids'])})" if entry["comment_ids"] else ""
        lines.append(f"{position}. {entry['text']}{reference}")
    lines.append("")
    if references:
        lines.extend(["### References", ""])
        lines.extend(f"- {line}" for line in references)
        lines.append("")
    lines.extend(["## Provenance", ""])
    lines.extend(f"- {line}" for line in provenance)
    return lines


def render_body(body, blocks, comments, tail_lines, flags=None):
    """The article, unchanged, with comment blocks between its paragraphs.

    Every inserted run is wrapped in blank lines and every inserted line begins
    with ``>``. Both are load-bearing. The blank lines make the insertion exactly
    reversible, and the leading one is also what keeps a callout from being read
    as a lazy continuation of the paragraph it follows.
    """
    flags = flags or {}
    lines = body.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] = lines[-1] + "\n"
    block_by_id = {block["id"]: block for block in blocks}
    grouped = {}
    for comment in comments:
        grouped.setdefault(comment["anchor"], []).append(comment)
    inserts = {}
    for anchor, group in grouped.items():
        position = block_by_id[anchor]["end_line"]
        chunk = ["\n"]
        for comment in sorted(group, key=lambda entry: entry["id"]):
            chunk.extend(f"{line}\n" for line in comment_block_lines(comment, flags.get(comment["id"])))
            chunk.append("\n")
        inserts.setdefault(position, []).extend(chunk)
    output = []
    for position, line in enumerate(lines):
        output.extend(inserts.get(position, []))
        output.append(line)
    output.extend(inserts.get(len(lines), []))
    return "".join(output) + "\n" + "".join(f"{line}\n" for line in tail_lines)


def strip_body(text, ids=None, final_newline=True):
    """Remove comment blocks and the reviewer-generated tail.

    ``ids`` scopes the removal to one review's comments, which is what lets a
    second review of an already-reviewed draft still prove it changed nothing:
    the earlier reviewer's blocks are ordinary body text to this one.
    """
    lines = text.splitlines(keepends=True)
    output = []
    removed = []
    index = 0
    fence = None
    while index < len(lines):
        plain = lines[index].rstrip("\n").rstrip("\r")
        if fence is not None:
            if closing_fence(plain, fence):
                fence = None
            output.append(lines[index])
            index += 1
            continue
        opening = FENCE_RE.match(plain)
        if opening:
            fence = opening.group(1)
            output.append(lines[index])
            index += 1
            continue
        if SENTINEL_RE.match(plain):
            if output and not output[-1].strip():
                output.pop()
            break
        marker = MARKER_RE.match(plain)
        if marker and (ids is None or marker.group(1) in ids):
            removed.append(marker.group(1))
            if output and not output[-1].strip():
                output.pop()
            index += 1
            while index < len(lines) and lines[index].lstrip().startswith(">"):
                index += 1
            if index < len(lines) and not lines[index].strip():
                index += 1
            continue
        output.append(lines[index])
        index += 1
    result = "".join(output)
    if not final_newline and result.endswith("\n"):
        result = result[:-1]
    return result, removed


def count_markers(text):
    total = 0
    fence = None
    for raw in text.splitlines():
        plain = raw.rstrip("\r")
        if fence is not None:
            if closing_fence(plain, fence):
                fence = None
            continue
        opening = FENCE_RE.match(plain)
        if opening:
            fence = opening.group(1)
            continue
        if MARKER_RE.match(plain):
            total += 1
    return total


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #

VERIFY_SYSTEM = """You review peer-review comments written about a scholarly article in the social sciences or humanities, before the author sees them.

For each item you are shown the full text of the passage the comment is anchored to, the heading it sits under, the comment itself, any text the reviewer suggests the author insert, and the real bibliographic metadata, abstracts, and archived quotations of every work the comment cites. Each citation carries an evidence_level: "full_text" means the source text was retrieved and archived, "abstract" means only an abstract was read, "metadata" means only catalogue metadata was read.

Flag an item only when it is wrong on the evidence shown:

- The critique misreads the anchored passage, argues against a claim the passage does not make, or asks for something the passage already does.
- The comment is about grammar, wording, spelling, or prose style rather than about evidence, logic, structure, or theoretical engagement.
- The suggested text attributes to a cited work a finding, argument, author, or year that its metadata, abstract, or archived quotations cannot support.
- The suggested text states what a source found as established fact when that source's evidence_level is "abstract" or "metadata" and the comment does not tell the author to verify against full text.
- The fix does not actually address the critique, or the suggested text does not do what the fix says it does.

Do not flag an item because you would have phrased the critique differently, because you would have rated its severity differently, because you disagree with the reviewer's theoretical commitments, or because you can think of a source you would have cited instead. A defensible comment that engages the passage in front of it stands, including a harsh one."""

INVENTORY_SYSTEM = """You read one paragraph from a scholarly article in the social sciences or humanities and list the empirical claims it makes.

An empirical claim asserts something about the world that could in principle be checked: a description of what some group does, a historical assertion, a causal claim, a statistic, or a report of what another scholar found. A definition, a normative judgment, a statement of the author's own argumentative intentions, and a question are not empirical claims.

Check each of these, because a quick reading misses them:

- claims carried by an example or an anecdote rather than stated outright
- claims in a subordinate clause of a sentence whose main clause is about something else
- attributions to a named scholar or a tradition with no citation attached
- quantitative language with no number behind it, such as most, increasingly, or the majority
- claims about a period, a place, or a population offered as uncontroversial background

For each claim, say whether this paragraph itself attaches a citation to it.

Return exactly one JSON object:

{"claims": [{"text": "<the claim, quoted or closely paraphrased>", "cited": true | false, "note": "<what a reader would need in order to be convinced, one short sentence>"}]}

An empty list is a legitimate answer for a paragraph that makes no empirical claims.

List what this paragraph claims, not what you know about the subject. Do not judge whether a claim is true, do not supply the literature it should have cited, and do not let a claim you happen to agree with pass as adequately supported. "cited" is about whether this paragraph attaches a citation, not about whether the claim is citable. The "note" says what a reader would need in order to be convinced — a kind of evidence, not the evidence itself."""


def verify_items(comments, blocks):
    """One item per comment, carrying the passage in full.

    A reviewer shown a paraphrase of the passage has nothing to check the
    critique against and approves whatever it is given, so the anchor text is
    never summarized or truncated here.
    """
    block_by_id = {block["id"]: block for block in blocks}
    items = []
    for comment in comments:
        citations = []
        for citation in comment["citations"]:
            entry = citation["entry"]
            citations.append(
                {
                    "key": citation["key"],
                    "title": entry.get("title"),
                    "authors": entry.get("authors"),
                    "year": entry.get("year"),
                    "venue": entry.get("venue"),
                    "doi": entry.get("doi"),
                    "evidence_level": entry.get("evidence_level"),
                    "abstract": entry.get("abstract") or "",
                    "quotes": ([citation["quote"]] if citation["quote"] else entry.get("quotes") or [])[:4],
                }
            )
        items.append(
            {
                "id": comment["id"],
                "context_heading": heading_for(blocks, comment["anchor"]),
                "anchor_text": block_by_id[comment["anchor"]]["text"],
                "comment": {
                    "category": comment["category"],
                    "severity": comment["severity"] or "",
                    "quoted_text": comment["quoted_text"],
                    "critique": comment["critique"],
                    "fix": comment["fix"],
                },
                "insert_text": comment["insert_text"],
                "citations": citations,
            }
        )
    return items


def verify(args, comments, blocks, run_dir):
    if not args.verify:
        return {"skipped": "verification was disabled with --no-verify"}, {}
    service = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    items = verify_items(comments, blocks)
    verdicts = forge_verify.verify_packets(
        service,
        VERIFY_SYSTEM,
        items,
        journal_path=run_dir / "verified.jsonl",
        background=True,
        timeout=args.request_timeout,
        progress=progress if args.verbose else None,
    )
    summary = forge_verify.summarize(verdicts)
    if service.get("fallback"):
        summary["fallback"] = service["fallback"]
    flags = {
        identifier: entry["reason"]
        for identifier, entry in verdicts.items()
        if entry["verdict"] == forge_verify.VERDICT_FLAG
    }
    return summary, flags


# --------------------------------------------------------------------------- #
# Note assembly
# --------------------------------------------------------------------------- #


def frontmatter_metadata(schema, related=None):
    """Minimal, advisory, and honest about who wrote it.

    `vault-organizer` decides where this note belongs and replaces the rest.
    `capture_type: generated` is not advisory: a review copy is machine-written
    and no run of this skill can produce one that says otherwise.
    """
    note_type = "note" if "note" in schema["types"] else None
    if note_type is None:
        raise UserError("schema does not define note type 'note'")
    metadata = {"type": note_type, "status": "raw", "capture_type": "generated"}
    if related and schema["properties"].get("related", {}).get("shape") == "list":
        metadata["related"] = related
    metadata = {key: value for key, value in metadata.items() if key in schema["properties"]}
    if metadata.get("status") != "raw" or "raw" not in schema["statuses"]:
        raise UserError("schema does not define status 'raw'")
    if metadata.get("capture_type") != "generated" or "generated" not in schema["capture_types"]:
        raise UserError("schema does not define capture type 'generated'; the review copy cannot say a model wrote it")
    return metadata


def existing_basenames(vault):
    """Every note name in the vault, cased down.

    Obsidian resolves ``[[Name]]`` by basename alone, so a name taken anywhere
    is taken everywhere.
    """
    taken = set()
    for root, directories, files in os.walk(vault):
        directories[:] = [
            directory
            for directory in directories
            if directory not in PROTECTED_DIRS and directory != STATE_DIR and not directory.startswith(".")
        ]
        for name in files:
            if name.endswith(".md"):
                taken.add(name[:-3].casefold())
    return taken


def review_copy_name(article_title, date, taken_casefold):
    suffix = f" - {FILENAME_SUFFIX} - {date}"
    stem = safe_title(article_title)[: MAX_TITLE_CHARS - len(suffix)].strip(" .")
    if not stem:
        stem = "Article"
    candidate = safe_title(f"{stem}{suffix}")
    base = candidate
    number = 2
    while candidate.casefold() in taken_casefold:
        candidate = safe_title(f"{base} ({number})")
        number += 1
    taken_casefold.add(candidate.casefold())
    return f"{candidate}.md"


def provenance_lines(index, run_dir, research_runs, verification, note_link):
    lines = [
        f'Source note: "[[{note_link}]]"',
        f"Source body SHA-256: `{index['article']['body_sha256']}`",
        f"Review run: `{run_dir}`",
    ]
    if research_runs:
        lines.append("Research runs: " + ", ".join(f"`{path}`" for path in research_runs))
    else:
        lines.append("Research runs: none")
    if verification.get("skipped"):
        lines.append(
            f"Verification: nothing was reviewed ({verification['skipped']}). That is not the same as approval."
        )
    else:
        lines.append(
            f"Verification: {verification.get('verified', 0)} comment(s) reviewed by the thinking model, "
            f"{verification.get('flagged', 0)} flagged"
        )
    lines.append("This note is a review copy. The article it reviews was not modified.")
    return lines


def collect_references(comments):
    references = []
    seen = set()
    for comment in comments:
        for citation in comment["citations"]:
            entry = citation["entry"]
            if entry["key"] in seen:
                continue
            seen.add(entry["key"])
            references.append(reference_line(entry))
    return sorted(references)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def verification_report(verification):
    if verification.get("skipped"):
        return [
            "## Verification",
            "",
            f"Nothing was reviewed: {verification['skipped']}.",
            "These comments carry no thinking-model review. That is not the same as approval.",
            "",
        ]
    lines = [
        "## Verification",
        "",
        f"- Reviewed by the thinking model: {verification.get('verified', 0)}",
        f"- Accepted: {verification.get('ok', 0)}",
        f"- Flagged: {verification.get('flagged', 0)}",
    ]
    if verification.get("fallback"):
        lines.append("- No thinking service was configured; the review ran on the bulk service")
    lines.append("")
    return lines


def comment_heading(comment):
    parts = [comment["id"], CATEGORIES[comment["category"]][1]]
    if comment["severity"]:
        parts.append(comment["severity"])
    return " · ".join(parts)


def write_report(run_dir, index, comments, meta, flags, verification, destination, options, warnings, dry_run):
    counts_category = {}
    counts_severity = {}
    for comment in comments:
        counts_category[comment["category"]] = counts_category.get(comment["category"], 0) + 1
        if comment["severity"]:
            counts_severity[comment["severity"]] = counts_severity.get(comment["severity"], 0) + 1
    lines = [
        "# Reviewer 2",
        "",
        f"- Article: `{index['article']['path']}`",
        f"- Run: `{run_dir}`",
        f"- Dry run: `{str(dry_run).lower()}`",
        f"- Review copy: `{destination or '—'}`",
        f"- Comments: {len(comments)}",
        f"- By severity: {', '.join(f'{key} {value}' for key, value in sorted(counts_severity.items())) or 'none'}",
        f"- By category: {', '.join(f'{key} {value}' for key, value in sorted(counts_category.items())) or 'none'}",
        "",
        "The article was not modified. Everything below describes the review copy.",
        "",
    ]
    flagged = [comment for comment in comments if comment["id"] in flags]
    if flagged:
        lines.extend(["## Flagged in verification", "", "These are still in the review copy, marked. Read them first.", ""])
        for comment in flagged:
            lines.append(f"### {comment_heading(comment)}")
            lines.append("")
            lines.append(f"- Anchor: `{comment['anchor']}`")
            lines.append(f"- Reason: {one_line(flags[comment['id']])}")
            lines.append("")
    lines.extend(["## Comments", ""])
    for comment in comments:
        marker = " (flagged)" if comment["id"] in flags else ""
        lines.append(f"### {comment_heading(comment)}{marker}")
        lines.append("")
        lines.append(f"- Anchor: `{comment['anchor']}`")
        lines.append(f"- Suggested text: {'yes' if comment['insert_text'] else 'no'}")
        if comment["citations"]:
            lines.append(
                "- Cites: "
                + ", ".join(f"{citation['key']} ({citation['entry']['evidence_level']})" for citation in comment["citations"])
            )
        lines.append("")
        lines.append(f"> {one_line(comment['critique'])[:400]}")
        lines.append("")
    lines.extend(["## Fix plan", ""])
    for position, step in enumerate(meta["fix_plan"], start=1):
        reference = f" ({', '.join(step['comment_ids'])})" if step["comment_ids"] else ""
        lines.append(f"{position}. {step['text']}{reference}")
    lines.append("")
    lines.extend(verification_report(verification))
    if warnings:
        lines.extend(["## Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
        lines.append("")
    lines.extend(["## Options", "", "```json", json.dumps(options, indent=2, ensure_ascii=False), "```", ""])
    path = run_dir / "report.md"
    run_state.atomic_write_text(path, "\n".join(lines))
    return path


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def resolved_options(args):
    return {
        "prompt_version": PROMPT_VERSION,
        "filename_pattern": args.filename_pattern,
        "schema": args.schema,
    }


RESUMABLE_OPTION_FLAGS = {"filename_pattern": "--filename-pattern", "schema": "--schema"}


def adopt_stored_options(args, state):
    stored = state.get("options", {})
    for key, flag in RESUMABLE_OPTION_FLAGS.items():
        if getattr(args, f"{key}_provided", False) and getattr(args, key) != stored.get(key):
            raise UserError(
                f"{flag} differs from the run being resumed ({getattr(args, key)!r} vs {stored.get(key)!r}); "
                "index the article again instead"
            )
        if key in stored:
            setattr(args, key, stored[key])


def phase(run_dir, name, event=None):
    run_state.update_run_state(run_dir, lambda draft: draft.update({"phase": name}) or draft, event=event)


def resolve_vault(value):
    vault = Path(value).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    return vault


def index_command(args):
    vault = resolve_vault(args.vault)
    if not args.paths:
        raise UserError("index requires the path to an article note")
    article = Path(args.paths[0]).expanduser().resolve()
    if not article.is_file():
        raise UserError(f"article note does not exist: {article}")
    if not path_is_inside(vault, article):
        raise UserError(f"article note is outside the vault: {article}")
    parsed = read_article(article)
    blocks = parse_blocks(parsed["body"])
    if not blocks:
        raise UserError(f"{article.name} has no body to review")
    reserved = reserved_comment_ids(blocks)
    stamp = utc_timestamp()
    # The boundary line has to be findable and unambiguous. If the article
    # already contains one, this run picks a different stamp rather than
    # rendering a copy with two boundaries in it.
    while f"{SENTINEL_PREFIX}{stamp}" in parsed["body"]:
        stamp += "-2"

    index = {
        "schema_version": COMMENTS_SCHEMA_VERSION,
        "article": {
            "path": relative_path(vault, article),
            "title": note_title(article, parsed["body"]),
            "body_sha256": parsed["body_sha256"],
            "file_sha256": parsed["file_sha256"],
            "final_newline": parsed["final_newline"],
            "had_bom": parsed["had_bom"],
        },
        "stamp": stamp,
        "blocks": blocks,
        "reserved_ids": reserved,
        "next_comment_id": next_comment_id(reserved),
        "outline": heading_outline(blocks),
    }

    configuration = {
        "workflow": WORKFLOW,
        "command": "review",
        "input": {
            "vault": str(vault),
            "article": index["article"]["path"],
            "body_sha256": parsed["body_sha256"],
        },
        "options": resolved_options(args),
    }
    run_dir = unique_run_directory(vault)
    run_state.initialize_run_state(
        run_dir,
        run_state.create_run_state(
            WORKFLOW,
            "review",
            configuration["input"],
            configuration["options"],
            phase="indexed",
            next_action="author comments.json, then run render --run <run-directory> --comments <file>",
        ),
    )
    run_state.atomic_write_json(run_dir / "index.json", index)

    anchorable = [block for block in blocks if block["kind"] != "r2-comment"]
    warnings = []
    if reserved:
        warnings.append(
            f"this article already carries {len(reserved)} comment(s) from an earlier review; "
            "they are kept verbatim and cannot be anchored to"
        )
    return structured(
        "ok",
        artifacts=[str(run_dir / "index.json")],
        warnings=warnings,
        data={
            "run_directory": str(run_dir),
            "article": index["article"],
            "blocks": len(blocks),
            "anchorable": len(anchorable),
            "kinds": {kind: sum(1 for block in blocks if block["kind"] == kind) for kind in sorted({block["kind"] for block in blocks})},
            "reserved_ids": reserved,
            "next_comment_id": index["next_comment_id"],
            "outline": index["outline"],
            "next_action": "author comments.json, then render",
        },
    )


def load_run(args):
    run_dir = Path(args.run).expanduser().resolve()
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    index_path = run_dir / "index.json"
    if not index_path.is_file():
        raise UserError(f"run has no index.json: {run_dir}")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    vault = Path(state["input"]["vault"])
    if args.vault and resolve_vault(args.vault) != vault:
        raise UserError(f"this run was indexed against {vault}, not {resolve_vault(args.vault)}")
    return run_dir, state, index, vault


def already_written(run_dir):
    rows, _warnings = run_state.read_jsonl_recover_tail(run_dir / "created.jsonl", repair=True)
    return next((row for row in reversed(rows) if row.get("status") == "ok"), None)


def render_command(args):
    run_dir, state, index, vault = load_run(args)
    adopt_stored_options(args, state)
    article = vault / index["article"]["path"]
    if not article.is_file():
        raise UserError(f"the article has moved or been renamed since it was indexed: {article}")
    parsed = read_article(article)
    if parsed["body_sha256"] != index["article"]["body_sha256"]:
        raise UserError(
            "the article has changed since it was indexed, so the block anchors no longer describe it; "
            "run index again and re-read it"
        )
    configuration = {
        "workflow": WORKFLOW,
        "command": "review",
        "input": state["input"],
        "options": resolved_options(args),
    }
    try:
        run_state.assert_compatible_run(state, configuration)
    except ValueError as error:
        raise UserError(str(error)) from error

    comments_path = Path(args.comments).expanduser().resolve()
    if not comments_path.is_file():
        raise UserError(f"comments file does not exist: {comments_path}")
    payload = json.loads(comments_path.read_text(encoding="utf-8"))

    warnings = []
    schema_path = resolve_schema_path(vault, args.schema)
    schema, _schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    register, research_runs = load_register(payload.get("research_runs") or [], warnings)
    blocks = index["blocks"]
    comments, meta, validation_warnings = validate_comments(payload, index, blocks, register)
    warnings.extend(validation_warnings)

    with run_state.run_lock(vault / STATE_DIR):
        existing = already_written(run_dir)
        if existing and not args.dry_run:
            destination = vault / existing["destination"]
            if destination.is_file() and sha256_bytes(destination.read_bytes()) == existing.get("sha256"):
                return structured(
                    "ok",
                    artifacts=[str(destination)],
                    warnings=[*warnings, "this run already wrote its review copy; nothing was written again"],
                    data={
                        "run_directory": str(run_dir),
                        "note": Path(existing["destination"]).stem,
                        "note_path": existing["destination"],
                        "comments": len(comments),
                        "resumed": True,
                    },
                )
            raise UserError(
                f"this run wrote {existing['destination']}, which has since changed or been moved; "
                "it was left alone"
            )

        phase(run_dir, "verify")
        verification, flags = verify(args, comments, blocks, run_dir)

        phase(run_dir, "render")
        references = collect_references(comments)
        article_link = article.stem
        provenance = provenance_lines(index, run_dir, research_runs, verification, article_link)
        tail = build_tail(index["stamp"], meta, references, provenance)
        rendered = render_body(parsed["body"], blocks, comments, tail, flags)

        # The whole promise of this skill in one comparison: take the comments
        # back out and the article must be exactly what it was.
        restored, _removed = strip_body(
            rendered, ids={comment["id"] for comment in comments}, final_newline=index["article"]["final_newline"]
        )
        if restored.encode("utf-8") != parsed["body"].encode("utf-8"):
            raise UserError(
                "the rendered review copy does not strip back to the original article body; nothing was written"
            )
        expected_markers = len(comments) + len(index.get("reserved_ids") or [])
        if count_markers(rendered) != expected_markers:
            raise UserError(
                f"the rendered review copy carries {count_markers(rendered)} comment markers where "
                f"{expected_markers} were expected; nothing was written"
            )

        related = [f"[[{article_link}]]"]
        metadata = frontmatter_metadata(schema, related=related)
        # No separator between the frontmatter and the body. The article's own
        # blank line after its frontmatter is part of the body being reproduced,
        # and adding a second one would mean `strip` on the review copy no
        # longer returns the article byte for byte.
        note_text = serialize_frontmatter(metadata, schema) + rendered
        date = datetime.date.today().isoformat()
        run_state.atomic_write_text(run_dir / "review-copy.md", note_text)

        destination_relative = None
        if args.dry_run:
            warnings.append("dry run: the review copy is in the run directory and was not added to the vault")
        else:
            taken = existing_basenames(vault)
            filename = review_copy_name(index["article"]["title"], date, taken)
            destination_relative = f"{INBOX_DIR}/{filename}"
            destination = vault / destination_relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload_bytes = note_text.encode("utf-8")
            try:
                with open(destination, "xb") as handle:
                    handle.write(payload_bytes)
                    handle.flush()
                    os.fsync(handle.fileno())
            except FileExistsError as error:
                run_state.append_jsonl_fsync(
                    run_dir / "created.jsonl",
                    {"at": run_state.utc_now(), "destination": destination_relative, "status": "collision"},
                )
                raise UserError(f"a note already exists at {destination_relative}; it was left alone") from error
            run_state.append_jsonl_fsync(
                run_dir / "created.jsonl",
                {
                    "at": run_state.utc_now(),
                    "destination": destination_relative,
                    "sha256": sha256_bytes(payload_bytes),
                    "status": "ok",
                },
            )

        report = write_report(
            run_dir,
            index,
            comments,
            meta,
            flags,
            verification,
            destination_relative,
            resolved_options(args),
            warnings,
            args.dry_run,
        )
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update(
                {
                    "phase": "dry-run" if args.dry_run else "complete",
                    "status": "running" if args.dry_run else "complete",
                    "nextAction": None if args.dry_run else "read report.md and relay the flagged comments first",
                }
            )
            or draft,
            event={"type": "rendered", "comments": len(comments), "dryRun": args.dry_run},
        )

    counts = {"total": len(comments), "flagged": len(flags), "by_category": {}, "by_severity": {}}
    for comment in comments:
        counts["by_category"][comment["category"]] = counts["by_category"].get(comment["category"], 0) + 1
        if comment["severity"]:
            counts["by_severity"][comment["severity"]] = counts["by_severity"].get(comment["severity"], 0) + 1
    artifacts = [str(run_dir / "review-copy.md"), str(report)]
    if destination_relative:
        artifacts.insert(0, str(vault / destination_relative))
    return structured(
        "ok",
        artifacts=artifacts,
        warnings=warnings,
        data={
            "run_directory": str(run_dir),
            "article": index["article"]["path"],
            "note_path": destination_relative,
            "dry_run": args.dry_run,
            "comments": counts,
            "flagged_ids": sorted(flags),
            "references": len(references),
            "research_runs": research_runs,
            "verification": verification,
            "report": str(report),
        },
    )


def strip_command(args):
    if not args.paths:
        raise UserError("strip requires the path to a review copy")
    source = Path(args.paths[0]).expanduser().resolve()
    if not source.is_file():
        raise UserError(f"file does not exist: {source}")
    data = source.read_bytes()
    split = split_frontmatter(data)
    if split["malformed"]:
        raise UserError(f"{source.name} opens a frontmatter block that never closes")
    cleaned, removed = strip_body(split["body"])
    return structured(
        "ok",
        data={
            "source": str(source),
            "removed_comment_ids": removed,
            "markdown": cleaned,
        },
    )


def inventory_command(args):
    """A census of empirical claims, so thin evidence is found by reading rather
    than by noticing."""
    run_dir, _state, index, vault = load_run(args)
    service = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
    blocks = [
        block
        for block in index["blocks"]
        if block["kind"] in INVENTORY_KINDS and len(block["text"]) >= INVENTORY_MIN_CHARS
    ]
    if args.limit:
        blocks = blocks[: args.limit]
    journal = run_dir / "inventory.jsonl"
    rows, warnings = run_state.read_jsonl_recover_tail(journal, repair=True)
    done = {row["id"]: row for row in rows if row.get("id")}
    results = []
    for position, block in enumerate(blocks, start=1):
        if block["id"] in done:
            results.append(done[block["id"]])
            continue
        messages = [
            {"role": "system", "content": INVENTORY_SYSTEM},
            {"role": "user", "content": json.dumps({"paragraph": block["text"]}, ensure_ascii=False)},
        ]
        value, _record = forge_llm.call_json_with_retry(
            service,
            messages,
            timeout=args.request_timeout,
            task="inventory",
            response_format={"type": "json_object"},
        )
        claims = value.get("claims") if isinstance(value, dict) else None
        row = {
            "at": run_state.utc_now(),
            "id": block["id"],
            "claims": [
                {
                    "text": one_line(claim.get("text")),
                    "cited": bool(claim.get("cited")),
                    "note": one_line(claim.get("note")),
                }
                for claim in (claims if isinstance(claims, list) else [])
                if isinstance(claim, dict) and one_line(claim.get("text"))
            ],
        }
        run_state.append_jsonl_fsync(journal, row)
        results.append(row)
        if args.verbose:
            progress(f"[inventory {position}/{len(blocks)}] {block['id']}: {len(row['claims'])} claim(s)")
    uncited = [
        {"anchor": row["id"], "claims": [claim for claim in row["claims"] if not claim["cited"]]}
        for row in results
        if any(not claim["cited"] for claim in row["claims"])
    ]
    return structured(
        "ok",
        artifacts=[str(journal)],
        warnings=warnings,
        data={
            "run_directory": str(run_dir),
            "blocks_examined": len(results),
            "claims": sum(len(row["claims"]) for row in results),
            "uncited_claims": sum(len(entry["claims"]) for entry in uncited),
            "blocks_with_uncited_claims": uncited,
        },
    )


def status_command(args):
    run_dir, state, index, _vault = load_run(args)
    verdicts = forge_verify.load_verdicts(run_dir / "verified.jsonl")
    created, _warnings = run_state.read_jsonl_recover_tail(run_dir / "created.jsonl", repair=False)
    written = [row for row in created if row.get("status") == "ok"]
    return structured(
        "ok",
        data={
            "run_directory": str(run_dir),
            "article": index["article"]["path"],
            "phase": state.get("phase"),
            "status": state.get("status"),
            "blocks": len(index["blocks"]),
            "comments_verified": len(verdicts),
            "comments_flagged": sum(1 for entry in verdicts.values() if entry["verdict"] == forge_verify.VERDICT_FLAG),
            "review_copy": written[-1]["destination"] if written else None,
            "next_action": state.get("nextAction"),
        },
    )


def doctor_command(args):
    vault = resolve_vault(args.vault)
    checks = {}
    warnings = []
    ok = True
    checks["vault"] = {"ok": os.access(vault, os.W_OK), "path": str(vault)}
    ok = ok and checks["vault"]["ok"]
    inbox = vault / INBOX_DIR
    checks["inbox"] = {"ok": inbox.is_dir() and os.access(inbox, os.W_OK), "path": str(inbox)}
    ok = ok and checks["inbox"]["ok"]
    schema_check = {"ok": False}
    try:
        schema_path = resolve_schema_path(vault, args.schema)
        schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
        frontmatter_metadata(schema)
        schema_check = {"ok": True, "path": str(schema_path), "schema_hash": schema_hash}
    except UserError as error:
        schema_check = {"ok": False, "detail": str(error)}
    checks["schema"] = schema_check
    ok = ok and schema_check["ok"]

    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    think_probe = forge_llm.service_doctor(think, timeout=min(args.request_timeout, 60))
    checks["think"] = {
        "ok": think_probe["reachable"],
        "url": think_probe["url"],
        "model": think_probe["model"],
        "detail": think_probe.get("detail"),
    }
    if think.get("fallback"):
        checks["think"]["fallback"] = think["fallback"]
        warnings.append("no thinking service is configured; comment review would run on the bulk service")
    if not think_probe["reachable"]:
        warnings.append("thinking service is unreachable; render would refuse unless you pass --no-verify")
    ok = ok and think_probe["reachable"]

    # Only the optional claim census talks to the bulk endpoint, so this reports
    # rather than decides.
    chat = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
    chat_probe = forge_llm.service_doctor(chat, expect_non_thinking=True, timeout=min(args.request_timeout, 60))
    checks["chat"] = {
        "ok": chat_probe["reachable"],
        "url": chat_probe["url"],
        "model": chat_probe["model"],
        "detail": chat_probe.get("detail"),
        "needed_for": "inventory",
    }
    for key in ("thinking", "hiddenTokens", "modelMismatch", "servedModels"):
        if key in chat_probe:
            checks["chat"][key] = chat_probe[key]
    if chat_probe.get("warning"):
        warnings.append(chat_probe["warning"])
    if not chat_probe["reachable"]:
        warnings.append("bulk service is unreachable; the optional inventory census would not run")

    research = Path(__file__).resolve().parents[2] / "web-research" / "scripts" / "web-research.mjs"
    checks["web_research"] = {"ok": research.is_file(), "path": str(research)}
    if not research.is_file():
        warnings.append("web-research is not installed alongside this skill; citations cannot be gathered")
    return structured("ok" if ok else "error", warnings=warnings, data={"checks": checks})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


class TrackingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Peer-review an article note into a separate review copy.")
    parser.add_argument("mode", choices=["index", "render", "strip", "inventory", "status", "doctor"])
    parser.add_argument("paths", nargs="*", help="the article note (index) or a review copy (strip)")
    parser.add_argument("--vault")
    parser.add_argument("--run", help="run directory from index")
    parser.add_argument("--comments", help="the comments file to render")
    parser.add_argument("--schema", action=TrackingAction)
    parser.add_argument("--filename-pattern", choices=["topic-date"], action=TrackingAction)
    parser.add_argument("--dry-run", action="store_true", help="render into the run directory without adding a note")
    parser.add_argument("--no-verify", action="store_true", help="skip the thinking-model review of the comments")
    parser.add_argument("--limit", type=int, help="blocks to examine (inventory)")
    parser.add_argument("--base-url", help="bulk service used by inventory")
    parser.add_argument("--model")
    parser.add_argument("--think-url", help="thinking service used for review (default: connectedServices.think)")
    parser.add_argument("--think-model")
    parser.add_argument("--request-timeout", type=float, default=600)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    for key in RESUMABLE_OPTION_FLAGS:
        if not hasattr(args, f"{key}_provided"):
            setattr(args, f"{key}_provided", False)
    args.filename_pattern = args.filename_pattern or "topic-date"
    args.schema = args.schema or os.environ.get("REVIEWER_2_SCHEMA") or None
    args.verify = not args.no_verify
    if args.mode == "index" and not args.vault:
        raise UserError("index requires --vault")
    if args.mode == "doctor" and not args.vault:
        raise UserError("doctor requires --vault")
    if args.mode in ("render", "status", "inventory") and not args.run:
        raise UserError(f"{args.mode} requires --run <run-directory>")
    if args.mode == "render" and not args.comments:
        raise UserError("render requires --comments <file>")
    return args


def run(argv):
    args = parse_args(argv)
    if args.mode == "index":
        return index_command(args)
    if args.mode == "render":
        return render_command(args)
    if args.mode == "strip":
        return strip_command(args)
    if args.mode == "inventory":
        return inventory_command(args)
    if args.mode == "status":
        return status_command(args)
    return doctor_command(args)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        result = run(argv)
    except CommentsError as error:
        print_json(structured("error", errors=[error_entry("invalid_comments", problem) for problem in error.problems]))
        return 2
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 2
    except forge_verify.VerificationError as error:
        print_json(structured("error", errors=[error_entry("verify_error", str(error))]))
        return 2
    except forge_llm.ChatError as error:
        print_json(structured("error", errors=[error_entry("chat_error", str(error))]))
        return 2
    except ValueError as error:
        print_json(structured("error", errors=[error_entry("run_state_error", str(error))]))
        return 2
    except KeyboardInterrupt:
        print_json(structured("error", errors=[error_entry("interrupted", "interrupted")]))
        return 130
    print_json(result)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
