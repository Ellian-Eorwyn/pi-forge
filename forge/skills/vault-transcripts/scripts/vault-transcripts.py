#!/usr/bin/env python3
"""Turn raw voice-note transcripts in an Obsidian inbox into readable notes.

A transcription app drops files named ``20260724 131748-9788991C.md`` whose
bodies are a long run of ``**Speaker 1**`` / ``*00:04*`` / one-line-of-text
blocks: no frontmatter, no headings, no paragraphs, and a filename that says
nothing about the recording. This pipeline gives each one a dated descriptive
filename, schema-valid frontmatter, a one-paragraph summary, and a cleaned
readable transcript — while keeping the original transcription verbatim under a
``# Transcript`` heading, because the cleanup is a convenience and the raw text
is the record.

It runs before ``vault-organizer``: this skill decides what a recording is
called and how it reads, the organizer decides where it belongs.

Bulk per-file work (classification, cleanup, summary) runs on the non-thinking
``chat`` service one stage at a time, then the whole batch is reviewed once on
``think``. Deterministic checks run before either, because a byte-exact
invariant is worth more than a model's opinion and costs nothing.
"""

import argparse
import datetime
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_llm
import forge_verify
import run_state
import vault_lexicon
import vault_profile
import vault_reflection
import vault_voice
from vault_schema import (
    INBOX_DIR,
    PROTECTED_DIRS,
    RESERVED_WINDOWS_NAMES,
    UserError,
    compile_destination,
    compiled_schema_for,
    is_workspace_dir,
    link_basename,
    normalize_body_for_hash,
    parse_frontmatter,
    path_is_inside,
    relative_path,
    resolve_schema_path,
    safe_title,
    selected_notes,
    serialize_frontmatter,
    sha256_bytes,
    sha256_text,
    split_frontmatter,
    valid_wikilink,
    wikilink_target,
)

WORKFLOW = "vault-transcripts"
PROMPT_VERSION = "vault-transcripts-v5"
# What a processed note keeps where the recording used to be, and what the
# recording's own note is called: "<processed name> - Transcript".
RAW_NOTE_SUFFIX = " - Transcript"
RAW_SOURCE_KIND = "transcript"
# A verbatim record is finished the moment it is written -- there is no later
# pass that revises it, so filing it as still-in-progress would be a lie.
RAW_STATUS = "complete"
STATE_DIR = ".vault-transcripts"
QUARANTINE_SUBDIR = "duplicates"
RUN_STATE_BATCH = 25

# Recording kinds. The cleanup contract in references/transcript-note-format.md
# defines one output style per kind; TYPE_LABELS is what reaches a filename.
RECORDING_TYPES = ("memo", "journal", "conversation", "meeting", "therapy", "lecture", "other")
MATERIAL_ROLES = ("owner-authored", "personal-exchange", "external-source", "unknown")
TYPE_LABELS = {
    "memo": "Memo",
    "journal": "Journal",
    "conversation": "Conversation",
    "meeting": "Meeting",
    "therapy": "Therapy",
    "lecture": "Lecture",
    "other": "Note",
}
# The organizer replaces frontmatter wholesale but reads ours as advisory
# context, so these are hints for its classifier, not final metadata.
TYPE_TO_NOTE_TYPE = {
    "memo": "note",
    "journal": "journal",
    "conversation": "meeting",
    "meeting": "meeting",
    "therapy": "meeting",
    "lecture": "meeting",
    "other": "note",
}
TYPE_TO_CAPTURE = {
    "memo": "voice",
    "journal": "voice",
    "conversation": "meeting",
    "meeting": "meeting",
    "therapy": "meeting",
    "lecture": "meeting",
    "other": "voice",
}
# Where a recording sits in the vault, for the personal-context gate. Only the
# two types that are personal by definition assert a route; everything else
# asserts nothing, which is what refuses every route-gated card in a work
# meeting or a lecture without any per-card configuration. A recording is not
# filed yet when this runs, so these are the only routes anyone can be sure of.
TYPE_TO_ROUTES = {
    "journal": ("personal", "personal/journal"),
    "therapy": ("personal", "personal/therapy"),
    "memo": (),
    "conversation": (),
    "meeting": (),
    "lecture": (),
    "other": (),
}

FILENAME_PATTERNS = ("date-type-topic", "date-topic", "date-time-topic")
SUMMARY_STYLES = ("callout", "paragraph", "heading")
SPEAKER_POLICIES = ("names", "roles", "generic")
TINY_SUMMARY_CHOICES = ("omit", "one-line")

TIMESTAMP_RE = re.compile(r"^\*(\d{1,2}(?::\d{2}){1,2})\*$")
SPEAKER_RE = re.compile(r"^\*\*(.+)\*\*$")
GENERIC_SPEAKER_RE = re.compile(r"^speaker\s*\d+$", re.IGNORECASE)
# Patterns A (date + time + hex), B (A with an export stamp appended), and C
# (date + time). D ("New Recording 41") and E (media/human titles) carry no date.
FILENAME_STAMP_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})[ _-](\d{2})(\d{2})(\d{2})(?:-([0-9A-Fa-f]{6,10}))?")
EXPORT_STAMP_RE = re.compile(r"\s*\d{4}-\d{2}-\d{2}\s+\d{2}[_:]\d{2}[_:]\d{2}\s*$")
COPY_SUFFIX_RE = re.compile(r"\s*\((\d+)\)$")
MEDIA_EXTENSION_RE = re.compile(r"\.(mov|m4v|mp4|m4a|mp3|wav|aac|webm)$", re.IGNORECASE)
URL_RE = vault_reflection.URL_RE
# Shared with vault-capture: a reader who meets both kinds of note in one vault
# should meet the same apparatus, so how a generated section is marked is one
# decision made in one place.
render_callout = vault_reflection.render_callout
WIKILINK_RE = re.compile(r"\[\[[^\]]+\]\]")
# The marker `build_note` writes, matched where a *line* is exactly that, so a
# passing mention inside the recording is not mistaken for the boundary.
TRANSCRIPT_MARKER_RE = re.compile(r"(?:\A|\r?\n)# Transcript[ \t]*\r?\n\r?\n?")
GENERIC_CAPTURE_NAME_RE = re.compile(
    r"^(new recording|recording|voice memo|audio|new note|untitled)(\s*\d+)?$|^img[_-]?\d+$",
    re.IGNORECASE,
)
BANAL_TITLES = {
    "transcript",
    "transcription",
    "recording",
    "new recording",
    "voice note",
    "voice memo",
    "audio",
    "untitled",
    "notes",
    "note",
    "conversation",
    "meeting",
    "memo",
}

MAX_TITLE_CHARS = 60
# Sized so one cleaned chunk comes back well inside a default generation limit:
# cleanup output is about as long as its input, and a truncated response costs a
# whole retry. Collapsing repeated speaker labels first means even a 57-minute
# meeting is only a handful of chunks.
CHUNK_BUDGET_CHARS = 12000
SUMMARY_INPUT_CHARS = 24000
CLASSIFY_HEAD_CHARS = 3500
CLASSIFY_TAIL_CHARS = 600
VERIFY_HEAD_CHARS = 500
VERIFY_TAIL_CHARS = 300
SUMMARY_TARGET_WORDS = 90
# The point at which a summary has stopped being a summary, with enough slack
# that a good one at the target length is never rejected.
SUMMARY_MAX_WORDS = 120
# Filler removal and sentence joining legitimately reshape a few words per
# chunk; wholesale invention does not look like this.
MAX_INVENTED_WORDS = 4
MIN_RARE_WORDS = 10
RARE_WORD_MIN_SOURCE_WORDS = 400
RARE_WORD_RETENTION = 0.85
# Spoken-to-written cleanup compresses: filler, false starts, and a circumlocution
# rewritten as the sentence it was reaching for take a real fraction of the words
# out. Measured against the calibration example, a filler-heavy passage lands near
# 0.47, so a 0.5 floor would have held the register's best work for review. The
# floor still exists to catch a cleanup that summarized instead of cleaning.
CLEANED_RATIO_MIN = 0.4
CLEANED_RATIO_MAX = 1.1
TINY_RATIO_MIN = 0.3
FIDELITY_SAMPLES = 4
FIDELITY_MIN_CONTAINMENT = 0.5
FIDELITY_MIN_WORDS = 4
FIDELITY_SAMPLE_RATE = 3
WORD_RE = re.compile(r"[a-z][a-z-]{2,}")
# Words a transcript editor inserts to turn fragments into sentences. They say
# nothing about whether content was invented, so the added-words check ignores
# them rather than spending its budget on grammar.
STOPWORDS = {
    "and", "but", "for", "nor", "yet", "the", "that", "this", "these", "those", "then", "than",
    "with", "from", "into", "onto", "about", "after", "before", "because", "while", "when", "where",
    "which", "who", "whom", "whose", "what", "was", "were", "been", "being", "are", "have", "has",
    "had", "does", "did", "not", "you", "your", "they", "them", "their", "there", "her", "his",
    "hers", "its", "our", "ours", "she", "him", "would", "could", "should", "will", "can", "may",
    "might", "must", "shall", "just", "also", "more", "most", "some", "any", "all", "one", "two",
    "very", "much", "such", "own", "too", "now", "get", "got", "going", "gone", "come", "came",
    "said", "say", "says", "like", "want", "know", "think", "thing", "things", "really", "actually",
    "still", "even", "back", "out", "off", "over", "under", "again", "how", "why", "yes", "yeah",
    "okay", "well", "here", "let", "put", "take", "make", "made", "way", "lot", "bit", "kind",
    "sort", "something", "anything", "everything", "nothing", "someone", "anyone", "everyone",
    "mean", "add", "look", "need", "number", "purpose", "people", "person", "part", "point",
    "place", "case", "fact", "other", "another", "different", "general", "whole", "same", "next",
    # Discourse adverbs. Long enough to look distinctive to `rare_words`, which
    # would then read the register's whole job -- deleting them -- as losing the
    # substance. They are also now spendable by the added-words check, which is
    # the accepted cost: an inserted "definitely" is caught by the prompt's
    # never-add-certainty rule and by the reviewer, not by a word budget.
    "basically", "literally", "essentially", "obviously", "honestly", "seriously", "totally",
    "apparently", "certainly", "definitely", "probably", "frankly", "presumably",
}
STRUCTURAL_WORDS = {
    "summary",
    "overview",
    "topics",
    "topic",
    "decisions",
    "decision",
    "action",
    "actions",
    "items",
    "item",
    "next",
    "steps",
    "agenda",
    "attendees",
    "participants",
    "questions",
    "unassigned",
    "stated",
    "unclear",
    "inaudible",
    "continued",
    "speaker",
}


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
    sys.stdout.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def log(args, message):
    if args.verbose:
        print(message, file=sys.stderr)


def progress(message):
    print(message, file=sys.stderr, flush=True)


def utc_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")


def format_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def format_clock(seconds):
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60}:{seconds % 60:02d}"
    return f"{seconds // 3600}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def unique_run_directory(vault):
    runs = vault / STATE_DIR / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    base = utc_timestamp()
    candidate = runs / base
    suffix = 1
    while candidate.exists():
        candidate = runs / f"{base}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True)
    (candidate / "backup").mkdir()
    return candidate


# --------------------------------------------------------------------------
# Filenames
# --------------------------------------------------------------------------


def validate_title(value):
    """Return a filename-safe title or raise. Rejects titles that name the
    medium instead of the content: 'Voice Note' tells the reader nothing the
    folder did not already say."""
    if not isinstance(value, str) or not value.strip():
        raise UserError("title is empty")
    cleaned = safe_title(value)
    if not cleaned:
        raise UserError(f"title contains only filename-unsafe characters: {value!r}")
    if cleaned.casefold() in RESERVED_WINDOWS_NAMES:
        raise UserError(f"title is a reserved filename: {value!r}")
    if cleaned.casefold() in BANAL_TITLES:
        raise UserError(f"title describes the medium, not the recording: {value!r}")
    if len(cleaned) > MAX_TITLE_CHARS:
        cleaned = cleaned[:MAX_TITLE_CHARS].rsplit(" ", 1)[0].strip(" .,-") or cleaned[:MAX_TITLE_CHARS]
    return cleaned


def parse_filename(name):
    """Extract the recording date, time, and app id from an export filename.

    The filename stamp is the only trustworthy date: these files are copied and
    re-exported, so filesystem timestamps say when the vault last saw them.
    """
    stem = name[:-3] if name.lower().endswith(".md") else name
    match = FILENAME_STAMP_RE.match(stem)
    if not match:
        return {"date": None, "time_hhmm": None, "recording_id": None}
    year, month, day, hour, minute, _second, recording_id = match.groups()
    try:
        date = datetime.date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return {"date": None, "time_hhmm": None, "recording_id": None}
    if not (0 <= int(hour) < 24 and 0 <= int(minute) < 60):
        return {"date": date, "time_hhmm": None, "recording_id": recording_id}
    return {"date": date, "time_hhmm": f"{hour}{minute}", "recording_id": recording_id}


def filename_title_hint(name):
    """The human-meaningful part of an export filename, if there is one.

    ``VPP Insiders #9: Brattle Report discussion.md`` already has a better title
    than a model will invent from the first two minutes of audio. A hex id or
    ``New Recording 41`` has none.
    """
    stem = name[:-3] if name.lower().endswith(".md") else name
    stem = FILENAME_STAMP_RE.sub("", stem)
    stem = EXPORT_STAMP_RE.sub("", stem)
    stem = COPY_SUFFIX_RE.sub("", stem)
    stem = MEDIA_EXTENSION_RE.sub("", stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -_")
    if not stem or GENERIC_CAPTURE_NAME_RE.fullmatch(stem):
        return None
    if re.fullmatch(r"[0-9A-Fa-f-]{6,}", stem):
        return None
    return stem


def format_filename(pattern, date, time_hhmm, recording_type, title, include_time=False):
    """The one place the note-naming convention lives."""
    prefix = date or ""
    if prefix and time_hhmm and (include_time or pattern == "date-time-topic"):
        prefix = f"{prefix} {time_hhmm}"
    if pattern == "date-type-topic":
        label = TYPE_LABELS.get(recording_type, TYPE_LABELS["other"])
        return " - ".join(part for part in (prefix, label, title) if part) + ".md"
    return (f"{prefix} {title}" if prefix else title) + ".md"


def recording_date(record, item):
    """The date the new name carries.

    The filename wins a disagreement: the spoken date is whatever the speaker
    said out loud, and people misremember what day it is more often than an
    export stamp is wrong.
    """
    if record["spoken_date"] and item["date"] and record["spoken_date"] != item["date"]:
        return item["date"]
    return record["spoken_date"] or item["date"]


def assign_raw_name(vault, directory, stem, taken_casefold):
    """A free name for the recording's own note, beside the note made from it."""
    suffix = 1
    while True:
        candidate = stem if suffix == 1 else f"{stem} ({suffix})"
        rel = (Path(directory) / f"{candidate}.md").as_posix()
        if rel.casefold() not in taken_casefold and not (vault / rel).exists():
            taken_casefold.add(rel.casefold())
            return rel
        suffix += 1


def assign_unique_name(vault, directory, args, date, time_hhmm, recording_type, title, taken_casefold, source_rel):
    """Pick a free filename, preferring the configured pattern.

    A same-day second recording on the same topic gets the recording time before
    it gets a numeric suffix: the time is information, ``(2)`` is not.
    """
    candidates = [format_filename(args.filename_pattern, date, time_hhmm, recording_type, title)]
    with_time = format_filename(args.filename_pattern, date, time_hhmm, recording_type, title, include_time=True)
    if with_time != candidates[0]:
        candidates.append(with_time)
    for candidate in candidates:
        rel = (Path(directory) / candidate).as_posix()
        if rel == source_rel:
            taken_casefold.add(rel.casefold())
            return rel
        if rel.casefold() not in taken_casefold and not (vault / rel).exists():
            taken_casefold.add(rel.casefold())
            return rel
    base = Path(candidates[-1])
    suffix = 2
    while True:
        candidate = f"{base.stem} ({suffix}){base.suffix}"
        rel = (Path(directory) / candidate).as_posix()
        if rel == source_rel or (rel.casefold() not in taken_casefold and not (vault / rel).exists()):
            taken_casefold.add(rel.casefold())
            return rel
        suffix += 1


# --------------------------------------------------------------------------
# Transcript parsing
# --------------------------------------------------------------------------


def timestamp_seconds(text):
    """``*04:12*`` is 4m12s and ``*1:04:12*`` is 1h4m12s: the colon count
    decides, never the magnitude."""
    parts = [int(part) for part in text.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return parts[0] * 60 + parts[1]


def transcript_link_target(section):
    """The single wikilink a split note keeps under its marker, or None.

    Only a section that is *nothing but* one link is a pointer. A recording whose
    first line happens to mention ``[[Somebody]]`` is still a recording.
    """
    text = section.strip()
    if not text or "\n" in text:
        return None
    match = re.fullmatch(r"!?\[\[([^\]\r\n]+)\]\]", text)
    return link_basename(wikilink_target(f"[[{match.group(1)}]]")) if match else None


def find_note_by_basename(vault, basename):
    """The note a wikilink resolves to, searched the way Obsidian resolves one."""
    if not basename:
        return None
    inbox = vault / INBOX_DIR / f"{basename}.md"
    if inbox.is_file():
        return inbox
    for directory, dirnames, filenames in os.walk(vault, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if not (dirpath / name).is_symlink()
            and name not in PROTECTED_DIRS
            and not name.startswith(".")
            and not is_workspace_dir(dirpath / name)
        ]
        candidate = dirpath / f"{basename}.md"
        if candidate.is_file():
            return candidate
    return None


def transcript_source(body, vault=None):
    """The recording itself, wherever this note keeps it.

    A body holding the ``# Transcript`` marker has been through this pipeline
    before: everything after the marker is the recording, everything before it
    was written by a previous run. Normally such a note also has frontmatter and
    ``is_transcript`` skips it, but the two can come apart -- strip the
    frontmatter off a processed note and the marker is still sitting there,
    which is how 32 notes in one real inbox arrived.

    Since the split, the marker section holds a link to the recording rather than
    the recording itself, so following it is how a processed note is re-read. The
    link is followed exactly one level: a recording that happens to contain a
    marker is a recording, not another pointer. A link that resolves to nothing
    returns empty, which reads downstream as "no timestamped blocks" and skips
    the note rather than processing a stub as if it were speech.

    Re-processing has to start from the recording. Left alone, a leftover marker
    is not part of any timestamped block, so ``parse_transcript`` reads it as
    handwritten preamble, ``build_note`` copies it into the generated head and
    then adds the real marker after it, and every note in the run is held for a
    level-one heading it never wrote.
    """
    match = TRANSCRIPT_MARKER_RE.search(body)
    if not match:
        return body
    section = body[match.end():]
    target = transcript_link_target(section)
    if target is None:
        return section
    if vault is None:
        return ""
    path = find_note_by_basename(Path(vault), target)
    if path is None:
        return ""
    try:
        return split_frontmatter(path.read_bytes())["body"].lstrip("\n")
    except (OSError, UnicodeDecodeError):
        return ""


def _line_kind(line):
    stripped = line.strip()
    timestamp = TIMESTAMP_RE.match(stripped)
    if timestamp:
        return "timestamp", timestamp.group(1)
    speaker = SPEAKER_RE.match(stripped)
    if speaker:
        return "speaker", speaker.group(1).strip()
    return "text", stripped


def parse_transcript(body):
    """Split an exported transcript into preamble, blocks, and trailing text.

    Blocks are the app's unit: an optional bold speaker label, an italic
    timestamp, and the text. Anything before the first block is preamble the
    user typed by hand, which must survive processing untouched — one real
    export has a numbered to-do list sitting above the audio.

    Slicing is contiguous by construction, so ``preamble + blocks + trailing``
    reproduces the body byte for byte.
    """
    lines = body.splitlines(keepends=True)
    kinds = [_line_kind(line.rstrip("\r\n")) for line in lines]
    starts = []
    index = 0
    while index < len(lines):
        kind, _value = kinds[index]
        if kind == "timestamp":
            starts.append((index, None))
            index += 1
            continue
        if kind == "speaker" and index + 1 < len(lines) and kinds[index + 1][0] == "timestamp":
            starts.append((index, kinds[index][1]))
            index += 2
            continue
        index += 1
    if not starts:
        return {"preamble": body, "blocks": [], "trailing": ""}

    first = starts[0][0]
    blocks = []
    end = first
    for position, (start, speaker) in enumerate(starts):
        if position + 1 < len(starts):
            end = starts[position + 1][0]
        else:
            # The last block owns its text and the blank lines after it; whatever
            # follows is trailing text that was never part of the transcript.
            end = start + (2 if speaker is not None else 1)
            while end < len(lines) and lines[end].strip():
                end += 1
            while end < len(lines) and not lines[end].strip():
                end += 1
        offset = start + (2 if speaker is not None else 1)
        text_lines = [line.strip() for line in lines[offset:end] if line.strip()]
        blocks.append(
            {
                "speaker": speaker,
                "seconds": timestamp_seconds(kinds[offset - 1][1]),
                "text": " ".join(text_lines),
                "raw": "".join(lines[start:end]),
            }
        )
    return {"preamble": "".join(lines[:first]), "blocks": blocks, "trailing": "".join(lines[end:])}


def serialize_parsed(parsed):
    """Inverse of parse_transcript, for the round-trip test."""
    return parsed["preamble"] + "".join(block["raw"] for block in parsed["blocks"]) + parsed["trailing"]


def correct_blocks(blocks, entries):
    """Replace recorded mistranscriptions in the text the model will read.

    Only ``text`` is touched. ``raw`` is what the note preserves verbatim under
    ``# Transcript`` and what the fidelity check reads, so leaving it alone
    keeps the original export byte-exact while the cleaned copy gets the
    spelling the owner actually uses.
    """
    if not entries:
        return blocks, []
    totals = {}
    corrected = []
    for block in blocks:
        text, rows = vault_lexicon.apply_corrections(block["text"], entries)
        for row in rows:
            key = (row["correct"], row["variant"])
            totals[key] = totals.get(key, 0) + row["count"]
        corrected.append({**block, "text": text})
    summary = [
        {"correct": correct, "variant": variant, "count": count}
        for (correct, variant), count in sorted(totals.items(), key=lambda item: (-item[1], item[0][0].lower()))
    ]
    return corrected, summary


def transcript_stats(parsed):
    blocks = parsed["blocks"]
    labels = Counter(block["speaker"] for block in blocks if block["speaker"])
    words = sum(len(block["text"].split()) for block in blocks)
    return {
        "blocks": len(blocks),
        "words": words,
        "duration_seconds": max((block["seconds"] for block in blocks), default=0),
        "speaker_labels": dict(labels),
        "has_preamble": bool(parsed["preamble"].strip()),
        "has_trailing": bool(parsed["trailing"].strip()),
        "timestamp_style": "HH:MM:SS" if any(block["seconds"] >= 3600 for block in blocks) else "MM:SS",
    }


def ordered_labels(blocks):
    """Speaker labels in order of first appearance."""
    seen = []
    for block in blocks:
        if block["speaker"] and block["speaker"] not in seen:
            seen.append(block["speaker"])
    return seen


def collapse_turns(blocks, speaker_map):
    """Merge consecutive blocks that share a label into one turn.

    The app re-labels every utterance, so a 56-minute meeting carries 640
    identical speaker lines. Merging them in code costs nothing, shrinks the
    prompt several-fold, and removes the most common thing the model could get
    wrong.
    """
    turns = []
    for block in blocks:
        label = speaker_map.get(block["speaker"]) if block["speaker"] else None
        text = block["text"].strip()
        if not text:
            continue
        if turns and turns[-1]["speaker"] == label:
            turns[-1]["text"] = f"{turns[-1]['text']} {text}".strip()
            continue
        turns.append({"speaker": label, "text": text})
    return turns


def render_turns(turns):
    parts = []
    for turn in turns:
        parts.append(f"**{turn['speaker']}**\n{turn['text']}" if turn["speaker"] else turn["text"])
    return "\n\n".join(parts)


def chunk_blocks(blocks, budget=CHUNK_BUDGET_CHARS):
    """Group blocks into prompt-sized chunks, never splitting a block."""
    chunks = []
    current = []
    size = 0
    for block in blocks:
        length = len(block["text"]) + 24
        if current and size + length > budget:
            chunks.append(current)
            current, size = [], 0
        current.append(block)
        size += length
    if current:
        chunks.append(current)
    return chunks or [[]]


def is_transcript(split, parsed):
    """A raw export has no frontmatter and at least one timestamped block.

    Processed notes gain frontmatter, so re-running the pipeline over the same
    inbox skips its own output instead of cleaning it twice.
    """
    if split["malformed"]:
        return False, "frontmatter is malformed"
    if split["had_frontmatter"]:
        return False, "already has frontmatter"
    if not parsed["blocks"]:
        return False, "no timestamped transcript blocks"
    return True, None


# --------------------------------------------------------------------------
# Scan and dedupe
# --------------------------------------------------------------------------


def scan_inbox(vault, limit=None):
    """Every Markdown file directly under the inbox tree, with what can be known
    about it without a model."""
    root = vault / INBOX_DIR
    if not root.is_dir():
        raise UserError(f"inbox directory does not exist: {root}")
    paths = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [name for name in sorted(dirnames) if not (dirpath / name).is_symlink() and not name.startswith(".")]
        for filename in sorted(filenames):
            path = dirpath / filename
            if path.is_symlink() or path.suffix.lower() != ".md":
                continue
            paths.append(path)
    paths.sort(key=lambda path: relative_path(vault, path))
    if limit is not None:
        paths = paths[:limit]
    items = []
    for path in paths:
        rel = relative_path(vault, path)
        stat = path.stat()
        item = {
            "path": rel,
            "sha256": "",
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "is_transcript": False,
            "skip_reason": None,
            "error": None,
            **parse_filename(path.name),
            "filename_hint": filename_title_hint(path.name),
            "stats": None,
        }
        try:
            data = path.read_bytes()
            item["sha256"] = sha256_bytes(data)
            split = split_frontmatter(data)
            parsed = parse_transcript(transcript_source(split["body"], vault))
            transcript, reason = is_transcript(split, parsed)
            item["is_transcript"] = transcript
            item["skip_reason"] = reason
            if transcript:
                item["stats"] = transcript_stats(parsed)
        except (OSError, UnicodeDecodeError) as error:
            item["error"] = str(error)
            item["skip_reason"] = f"unreadable: {error}"
        items.append(item)
    return items


LABEL_TO_TYPE = {label: recording_type for recording_type, label in TYPE_LABELS.items()}


def filename_recording_type(name):
    """The recording type a `date-type-topic` name states, or None.

    The name was settled when the note was first processed and has been reviewed
    by a person since. It is a better answer than a fresh classification for the
    one decision that must not drift -- whether this is a therapy session.
    """
    stem = name[:-3] if name.lower().endswith(".md") else name
    parts = [part.strip() for part in stem.split(" - ")]
    for part in parts[1:2]:
        if part in LABEL_TO_TYPE:
            return LABEL_TO_TYPE[part]
    return None


def scan_processed(vault, schema_path, limit=None):
    """Every filed note this pipeline has already written, with its recording.

    The inbox is left alone: those notes are ``process``'s input, and a note that
    has just been processed does not want cleaning again in the same breath. What
    is selected here is the filed corpus -- notes whose cleaned text was written
    by an older prompt and whose recording is still readable, either inline or
    through the link left by ``split``.
    """
    items = []
    # The limit counts transcript notes, not files walked: a vault-wide walk
    # reaches a thousand notes that are not transcripts before it reaches one
    # that is, so limiting the walk would return a trial run of nothing.
    for path in selected_notes(vault, schema_path, "vault", None):
        if limit is not None and sum(1 for entry in items if entry["is_transcript"]) >= limit:
            break
        rel = relative_path(vault, path)
        if rel == INBOX_DIR or rel.startswith(f"{INBOX_DIR}/"):
            continue
        try:
            data = path.read_bytes()
            split = split_frontmatter(data)
        except (OSError, UnicodeDecodeError):
            continue
        if split["malformed"] or not split["had_frontmatter"]:
            continue
        match = TRANSCRIPT_MARKER_RE.search(split["body"])
        if not match:
            continue
        # The frontmatter is never re-serialized. The organizer's classification
        # -- domain, subdomain, project, people, related, date -- lives there,
        # and rebuilding it from this skill's three keys would throw all of it
        # away. Kept as the exact bytes that precede the body.
        text = data.decode("utf-8-sig")
        prefix = text[: len(text) - len(split["body"])]
        item = {
            "path": rel,
            "sha256": sha256_bytes(data),
            "is_transcript": True,
            "skip_reason": None,
            "error": None,
            "date": None,
            "time_hhmm": None,
            "recording_id": None,
            "filename_hint": filename_title_hint(path.name),
            "stats": None,
            # Everything from the marker onward is reattached untouched, so a
            # linked recording stays linked and an inline one stays inline.
            "tail": split["body"][match.start():],
            "frontmatter_prefix": prefix,
            "label_type": filename_recording_type(path.name),
        }
        if item["label_type"] == "therapy":
            item["is_transcript"] = False
            item["skip_reason"] = "therapy: kept in the verbatim contract"
            items.append(item)
            continue
        raw_body = transcript_source(split["body"], vault)
        parsed = parse_transcript(raw_body)
        if not parsed["blocks"]:
            item["is_transcript"] = False
            item["skip_reason"] = "no timestamped transcript blocks in the recording"
            items.append(item)
            continue
        item["stats"] = transcript_stats(parsed)
        items.append(item)
    return items


def assign_quarantine_path(vault, rel, taken_casefold):
    base = Path(STATE_DIR) / QUARANTINE_SUBDIR / Path(rel).name
    candidate = base
    suffix = 1
    while (vault / candidate).exists() or candidate.as_posix().casefold() in taken_casefold:
        candidate = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
        suffix += 1
    taken_casefold.add(candidate.as_posix().casefold())
    return candidate.as_posix()


def duplicate_rank(item):
    """Prefer the original over a Finder copy: no ``(1)`` suffix, then the
    shorter name, then alphabetical."""
    name = Path(item["path"]).name
    return (1 if COPY_SUFFIX_RE.search(name[:-3]) else 0, len(name), item["path"])


def plan_dedupe(vault, items):
    """Plan duplicate handling before any model call. No filesystem changes.

    Byte-identical copies are quarantined here rather than left for the
    organizer: once the surviving copy is renamed and rewritten, nothing pairs
    it with the leftover again — not its body hash, not its basename, not its
    first line.
    """
    result = {
        "groups": [],
        "review_pairs": [],
        "quarantine_root": (Path(STATE_DIR) / QUARANTINE_SUBDIR).as_posix(),
    }
    losers = {}
    taken = set()
    transcripts = [item for item in items if item["is_transcript"]]

    by_hash = {}
    for item in transcripts:
        by_hash.setdefault(item["sha256"], []).append(item)
    for _hash, group in sorted(by_hash.items()):
        if len(group) < 2:
            continue
        winner = min(group, key=duplicate_rank)
        entry = {"kind": "exact", "winner": winner["path"], "losers": []}
        for loser in sorted((item for item in group if item is not winner), key=duplicate_rank):
            quarantine_to = assign_quarantine_path(vault, loser["path"], taken)
            entry["losers"].append({"path": loser["path"], "sha256": loser["sha256"], "quarantine_to": quarantine_to})
            losers[loser["path"]] = {
                "winner": winner["path"],
                "kind": "exact",
                "quarantine_to": quarantine_to,
                "sha256": loser["sha256"],
            }
        result["groups"].append(entry)

    remaining = [item for item in transcripts if item["path"] not in losers]
    by_recording = {}
    for item in remaining:
        if item["recording_id"]:
            by_recording.setdefault(item["recording_id"].upper(), []).append(item)
    for recording_id, group in sorted(by_recording.items()):
        if len(group) < 2:
            continue
        # Same recording, different bytes: usually a truncated re-export, and
        # sometimes the truncated one is the copy carrying handwritten notes.
        # Neither is a superset, so a human decides and both are left alone.
        result["review_pairs"].append(
            {
                "recording_id": recording_id,
                "members": [
                    {
                        "path": item["path"],
                        "blocks": (item["stats"] or {}).get("blocks"),
                        "words": (item["stats"] or {}).get("words"),
                        "has_preamble": (item["stats"] or {}).get("has_preamble"),
                    }
                    for item in sorted(group, key=lambda entry: entry["path"])
                ],
                "reason": "same recording id with different content; neither copy contains the other",
            }
        )
    held = {member["path"] for pair in result["review_pairs"] for member in pair["members"]}
    return result, losers, held


# --------------------------------------------------------------------------
# Chat stage 1: classification and title
# --------------------------------------------------------------------------

CLASSIFY_SYSTEM = """You read one voice recording's transcript and report what it is.

Return exactly one JSON object and nothing else:
{"recording_type": "memo" | "journal" | "conversation" | "meeting" | "therapy" | "lecture" | "other",
 "material_role": "owner-authored" | "personal-exchange" | "external-source" | "unknown",
 "title": "<short descriptive title>",
 "speakers": {"<label exactly as given>": {"who": "<name, role, or unknown>", "kind": "name" | "role" | "unknown", "confidence": "high" | "medium" | "low", "source": "transcript" | "roster", "evidence": "<the words that identify them, or null>"}},
 "effective_speakers": <how many people are actually speaking>,
 "spoken_date": "<YYYY-MM-DD or null>",
 "evidence": "<the exact sentence from the transcript that states that date, or null>",
 "needs_review": <true | false>,
 "review_reason": "<why, only when needs_review is true>"}

recording_type - check these in order and take the first that fits:
- lecture: a class, talk, or taught session with an instructor presenting material.
- meeting: a work meeting, webinar, standup, interview, or call with an agenda or business purpose.
- therapy: a counselling or therapy session between a client and a practitioner.
- conversation: any other exchange between two or more people, including friends and family.
- journal: one person reflecting on their life, feelings, or experience at length.
- memo: one person capturing a task, idea, reminder, plan, or working thought.
- other: none of the above fits.
Do not default to memo because the recording is short. A two-minute reflection
on a hard day is a journal; a two-minute list of errands is a memo.

material_role:
- owner-authored: a single-speaker memo or journal made by the vault owner.
- personal-exchange: a conversation, therapy session, or meeting involving the owner.
- external-source: a lecture, podcast, video, webinar, or other recording primarily imported as a source.
- unknown: evidence is insufficient. Never guess owner authorship.

title - what the recording is ABOUT, in 3 to 8 words, title case. Name the
subject the way the person would look for it later. Never name the medium:
"Voice Note", "Recording", "Transcript", and "Conversation" are not titles. Do
not open with "Discussing", "Talking About", "Thoughts On", "Notes On", or any
other wrapper for the act of recording — go straight to the subject. If
"filenameTitle" is given it is a real title someone already chose; prefer it,
lightly normalized, unless the transcript shows it is wrong.

speakers - one entry for every label in "labels", using the label text exactly.
Name a person only from evidence: someone is addressed by name, introduces
themselves, is named as the speaker, or is identified by a roster cue as
described below. Never infer a name from the subject matter — speaking about
someone is not evidence that they are speaking. Use kind "role" for an unnamed
but identifiable role ("Therapist", "Interviewer", "Instructor"), and
{"who": "unknown", "kind": "unknown"} when there is no evidence.

Set "source" to where the identification came from: "transcript" when the
recording names them, "roster" when it took a "knownSpeakers" cue to know who
the voice is.

"evidence" is always a quote from the transcript, never from the roster. Quoting
a cue back is not evidence — the cue is the claim, and the transcript is what
has to support it. Quote the words that tie this identification to THIS label:
what this speaker said about themselves, or what another speaker said to or
about them.

knownSpeakers - people the vault owner has recorded, given when any of them may
be here. It is a candidate list, not an attendance list, and being on it is
never by itself evidence that someone is present. Use it two ways:
- Spelling. A name the transcript states in mangled form takes the roster
  spelling: "Alexei Miller" heard for a listed "Alexi Miller" is that person.
- Identity. An entry's "cue" and "role" describe one particular voice. Work out
  which label is that voice by what each label actually says: whose work, whose
  meeting, whose title, who is addressed about what. Name that label with
  confidence "medium" and quote the transcript words that made it that label and
  not another one. When the cue fits every voice equally, or none clearly, it
  fits nobody: leave them unknown.

The person recording is usually one of the speakers and is not on the roster, so
a cue phrased from their side ("the other voice", "my therapist", "my manager")
describes someone other than whoever is recording. Only ever use a name spelled
as "knownSpeakers" gives it. If two entries fit one voice equally well, name
neither.

A cue that places someone in a kind of recording — "the second voice in home
recordings", "the other voice in therapy" — tells you which voice is theirs when
a second voice is there. It is not evidence that one is. Work out how many people
are talking first, then ask the roster who they are.

effective_speakers - how many people are really talking. This transcriber splits
one voice into several labels, so labels are an upper bound, not an answer. If
every label reads as the same person, answer 1. Settle this from the transcript
alone, before you look at knownSpeakers: the roster says who a voice belongs to,
never that a second voice is there. A solo memo that trails off into "um, yeah,
that's pretty much it" under a new label is still one person talking.

spoken_date - fill this in only when someone says the date out loud, and quote
the sentence in "evidence" exactly as it appears. Otherwise both are null.

needs_review - true when the recording is too garbled, too short, or too
ambiguous to title honestly."""


def classify_payload(item, parsed, lexicon=None):
    blocks = parsed["blocks"]
    labels = ordered_labels(blocks)
    rendered = render_turns(collapse_turns(blocks, {label: label for label in labels}))
    payload = {
        "filename": Path(item["path"]).name,
        "labels": labels,
        "stats": {
            "blocks": item["stats"]["blocks"],
            "words": item["stats"]["words"],
            "durationSeconds": item["stats"]["duration_seconds"],
        },
        "head": rendered[:CLASSIFY_HEAD_CHARS],
    }
    if item["filename_hint"]:
        payload["filenameTitle"] = item["filename_hint"]
    if parsed["preamble"].strip():
        payload["handwrittenPreamble"] = parsed["preamble"].strip()[:1000]
    if len(rendered) > CLASSIFY_HEAD_CHARS:
        payload["tail"] = rendered[-CLASSIFY_TAIL_CHARS:]
    # Roster matching reads the whole recording, not the excerpt the model gets:
    # a name is said once, usually nowhere near the beginning.
    roster = vault_lexicon.candidate_speakers(rendered, (lexicon or {}).get("speakers", []))
    if roster:
        payload["knownSpeakers"] = vault_lexicon.speaker_offers(roster)
    return payload


def normalize_body_text(body):
    return re.sub(r"\s+", " ", body).casefold()


def validate_classification(value, item, parsed, roster_names=()):
    """Validate untrusted classification output. Returns (record, warnings)."""
    warnings = []
    if not isinstance(value, dict):
        raise UserError("response was not a JSON object")
    recording_type = value.get("recording_type")
    if recording_type not in RECORDING_TYPES:
        raise UserError(f"recording_type must be one of {', '.join(RECORDING_TYPES)} (got {recording_type!r})")
    needs_review = bool(value.get("needs_review"))
    review_reason = value.get("review_reason") if needs_review else None
    if needs_review and not isinstance(review_reason, str):
        review_reason = "model asked for review without giving a reason"
    title = None
    if not needs_review:
        title = validate_title(value.get("title"))

    labels = ordered_labels(parsed["blocks"])
    offered = {vault_lexicon.fold(name): name for name in roster_names}
    spoken = normalize_body_text(" ".join(block["text"] for block in parsed["blocks"]))
    speakers = {}
    raw_speakers = value.get("speakers") if isinstance(value.get("speakers"), dict) else {}
    for label in labels:
        entry = raw_speakers.get(label) if isinstance(raw_speakers.get(label), dict) else {}
        who = entry.get("who") if isinstance(entry.get("who"), str) else "unknown"
        kind = entry.get("kind") if entry.get("kind") in {"name", "role", "unknown"} else "unknown"
        confidence = entry.get("confidence") if entry.get("confidence") in {"high", "medium", "low"} else "low"
        source = entry.get("source") if entry.get("source") in {"transcript", "roster"} else "transcript"
        evidence = entry.get("evidence") if isinstance(entry.get("evidence"), str) else None
        who = safe_title(who)[:40]
        if not who or who.casefold() == "unknown":
            who, kind = "unknown", "unknown"
        if who != "unknown" and source == "roster":
            # A roster identification is only as good as the roster. Anything
            # else claiming that provenance is the model inventing a person,
            # which is the one failure this shortcut could introduce.
            match = offered.get(vault_lexicon.fold(who))
            if match is None:
                warnings.append(f"dropped roster speaker {who!r} for {label}: not in the offered roster")
                who, kind, confidence = "unknown", "unknown", "low"
            elif not evidence or normalize_body_text(evidence) not in spoken:
                # The roster says who may be here; only the transcript says
                # which voice they are. Quoting the cue back proves the model
                # never did that second step, and the observed failure is
                # attaching a real person to the wrong label.
                warnings.append(
                    f"dropped roster speaker {match!r} for {label}: evidence is not from the transcript"
                )
                who, kind, confidence = "unknown", "unknown", "low"
            else:
                who = match
        if who == "unknown":
            source, evidence = "transcript", None
        speakers[label] = {
            "who": who,
            "kind": kind,
            "confidence": confidence,
            "source": source,
            "evidence": (evidence or "")[:200] or None,
        }
    unexpected = sorted(set(raw_speakers) - set(labels))
    if unexpected:
        warnings.append(f"classification named speakers that are not in the transcript: {', '.join(unexpected[:5])}")

    effective = value.get("effective_speakers")
    if not isinstance(effective, int) or effective < 0:
        effective = len(labels) or 1
    effective = max(1, min(effective, len(labels) or 1))

    material_role = value.get("material_role")
    if material_role not in MATERIAL_ROLES:
        if recording_type in {"memo", "journal"} and effective == 1:
            material_role = "owner-authored"
        elif recording_type in {"conversation", "meeting", "therapy"}:
            material_role = "personal-exchange"
        elif recording_type == "lecture":
            material_role = "external-source"
        else:
            material_role = "unknown"
        warnings.append(f"classification omitted material_role; inferred {material_role}")
    if material_role == "owner-authored" and (recording_type not in {"memo", "journal"} or effective != 1):
        needs_review = True
        review_reason = "owner-authored classification is inconsistent with a single-speaker memo or journal"
        warnings.append(review_reason)

    spoken_date = None
    raw_date = value.get("spoken_date")
    if isinstance(raw_date, str) and raw_date.strip():
        evidence = value.get("evidence")
        try:
            spoken_date = datetime.date.fromisoformat(raw_date.strip()).isoformat()
        except ValueError:
            warnings.append(f"ignored an unparseable spoken date: {raw_date!r}")
            spoken_date = None
        if spoken_date and not (
            isinstance(evidence, str)
            and evidence.strip()
            and normalize_body_text(evidence) in normalize_body_text(" ".join(block["text"] for block in parsed["blocks"]))
        ):
            # A date nobody said is a date the model invented.
            warnings.append(f"ignored spoken date {spoken_date} because its evidence is not in the transcript")
            spoken_date = None

    return {
        "recording_type": recording_type,
        "material_role": material_role,
        "title": title,
        "speakers": speakers,
        "effective_speakers": effective,
        "spoken_date": spoken_date,
        "needs_review": needs_review,
        "review_reason": review_reason,
    }, warnings


def derive_speaker_map(labels, speakers, effective, policy, owner, lexicon=None):
    """Decide what each original label is called in the cleaned transcript.

    Labels the transcriber invented get merged and relabelled; labels that are
    already someone's real name are kept under every policy, because the source
    knew something the model is only guessing at.

    A roster identification is accepted at "medium" confidence where a
    transcript-only one needs "high". The roster is the owner's own knowledge of
    who they record, so it is better evidence than the model's reading of a
    voice, and it is the only thing that can name the second voice in a session
    where nobody says a name aloud.
    """
    if not labels:
        return {}, False
    if effective <= 1:
        return {label: None for label in labels}, True
    mapping = {}
    used = []
    for position, label in enumerate(labels, start=1):
        if not GENERIC_SPEAKER_RE.match(label):
            display = safe_title(label)[:40] or f"Speaker {position}"
        else:
            entry = speakers.get(label, {})
            who, kind, confidence = entry.get("who", "unknown"), entry.get("kind"), entry.get("confidence")
            from_roster = entry.get("source") == "roster"
            display = f"Speaker {position}"
            if who != "unknown" and (confidence == "high" or (from_roster and confidence == "medium")):
                if policy == "names":
                    display = vault_lexicon.canonical_name(lexicon, who) or who
                elif policy == "roles" and (kind == "role" or (owner and who.casefold() == owner.casefold())):
                    display = who
        if display in used:
            display = f"{display} ({position})"
        used.append(display)
        mapping[label] = display
    return mapping, False


def roster_may_have_split_one_voice(classification):
    """Whether this reads as a solo recording the roster talked into being two.

    Owner authorship and a memo or journal reading are the model's own answers,
    and they agree that one person is talking. Only the count dissents, which is
    the shape a roster promise leaves behind.
    """
    return (
        classification["material_role"] == "owner-authored"
        and classification["recording_type"] in {"memo", "journal"}
        and classification["effective_speakers"] != 1
    )


def classify_without_roster(service, args, item, parsed):
    """Ask again with the roster withheld, for a recording it may have split.

    An `always` entry whose cue reads "the second voice in home recordings" says
    which voice is theirs when there is one; the model takes it as a promise that
    there is one, and files a solo memo's trailing "um, yeah, that's pretty much
    it" under their name. Removing the roster asks the same question without that
    pressure. On this corpus every note held for the inconsistency answered one
    speaker when no roster was offered.

    Returns `(classification, warnings)` when the answer is a clean solo
    recording, or None to keep the answer we already have. A second voice the
    model finds unprompted is a real disagreement and stays held.
    """
    payload = classify_payload(item, parsed)
    messages = [
        {"role": "system", "content": CLASSIFY_SYSTEM},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        value, _call = forge_llm.call_json_with_retry(
            service,
            messages,
            temperature=0,
            cache_prompt=args.cache_prompt,
            response_format={"type": "json_object"},
            timeout=args.request_timeout,
            api_key=args.api_key,
            task="classify-transcript-no-roster",
        )
        classification, warnings = validate_classification(value, item, parsed)
    except (forge_llm.ChatError, UserError, ValueError):
        # The first answer is still usable; a failed second look just leaves it.
        return None
    # Owner-authored and unheld already implies a memo or journal: any other
    # reading of one voice would have been held on the way out of validation.
    solo = classification["material_role"] == "owner-authored" and classification["effective_speakers"] == 1
    if classification["needs_review"] or not solo:
        return None
    return classification, warnings


def classify_items(args, vault, items, run_dir, skip):
    """One non-thinking call per transcript. Journaled so a resumed run pays for
    nothing it already bought."""
    journal_path = run_dir / "classified.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    journal = {(row["path"], row["sha256"]): row for row in prior if row.get("path") and row.get("sha256")}
    service = chat_service(args)
    pending = [item for item in items if item["is_transcript"] and item["path"] not in skip]
    records = {}
    warnings = []
    durations = []
    total = len(pending)
    for position, item in enumerate(pending, start=1):
        key = (item["path"], item["sha256"])
        if key in journal:
            records[item["path"]] = journal[key]
            continue
        started = time.time()
        try:
            data = (vault / item["path"]).read_bytes()
            parsed = parse_transcript(transcript_source(split_frontmatter(data)["body"], vault))
            payload = classify_payload(item, parsed, getattr(args, "compiled_lexicon", None))
            roster_names = [offer["name"] for offer in payload.get("knownSpeakers", [])]
            messages = [
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ]
            value, _call = forge_llm.call_json_with_retry(
                service,
                messages,
                temperature=0,
                cache_prompt=args.cache_prompt,
                response_format={"type": "json_object"},
                timeout=args.request_timeout,
                api_key=args.api_key,
                task="classify-transcript",
            )
            try:
                classification, record_warnings = validate_classification(value, item, parsed, roster_names)
            except UserError as error:
                repair = messages + [
                    {
                        "role": "user",
                        "content": f"That response was unusable: {error}. Return corrected JSON only, following the schema exactly.",
                    }
                ]
                value, _call = forge_llm.call_json_with_retry(
                    service,
                    repair,
                    temperature=0,
                    cache_prompt=args.cache_prompt,
                    response_format={"type": "json_object"},
                    timeout=args.request_timeout,
                    api_key=args.api_key,
                    task="classify-transcript-repair",
                )
                classification, record_warnings = validate_classification(value, item, parsed, roster_names)
            if roster_names and roster_may_have_split_one_voice(classification):
                # Without a roster there is nothing to withhold, and the same
                # question at temperature 0 would only buy the same answer.
                second = classify_without_roster(service, args, item, parsed)
                if second is not None:
                    split = classification["effective_speakers"]
                    classification, retry_warnings = second
                    record_warnings = [
                        *record_warnings,
                        f"re-read without the roster: {split} speakers became 1",
                        *retry_warnings,
                    ]
            record = {
                "path": item["path"],
                "sha256": item["sha256"],
                "source": "model",
                "warnings": record_warnings,
                **classification,
            }
        except (forge_llm.ChatError, UserError, OSError, UnicodeDecodeError, ValueError) as error:
            message = f"{type(error).__name__}: {error}"
            warnings.append(f"{item['path']}: classification failed ({message})")
            record = {
                "path": item["path"],
                "sha256": item["sha256"],
                "source": "failed",
                "warnings": [message],
                "recording_type": "other",
                "material_role": "unknown",
                "title": None,
                "speakers": {},
                "effective_speakers": 1,
                "spoken_date": None,
                "needs_review": True,
                "review_reason": f"classification failed: {message}",
            }
        except InterruptedError as error:
            raise UserError(f"classification was preempted by interactive activity: {error}") from error
        record["seconds"] = round(time.time() - started, 3)
        records[item["path"]] = record
        run_state.append_jsonl_fsync(journal_path, record)
        durations.append(record["seconds"])
        eta = format_duration(sum(durations) / len(durations) * (total - position)) if durations else "-"
        progress(f"[classify {position}/{total}] {item['path']} → {record['recording_type']} ({record['seconds']:.1f}s, eta {eta})")
    return records, warnings


# --------------------------------------------------------------------------
# Chat stage 2: chunked cleanup
# --------------------------------------------------------------------------

CLEANUP_SYSTEM = """You are a meticulous transcript editor. You turn one chunk of a speech-to-text transcript into readable written prose in the speaker's own voice.

This is the machine copy of references/transcript-note-format.md. Both must say the same thing.

Return exactly one JSON object and nothing else:
{"cleaned": "<the cleaned chunk as Markdown>", "chunk_summary": "<at most two sentences on what this chunk covers>"}

Fidelity rules, which outrank every style rule below:
- The register is spoken-to-written: what the speaker would have written had they
  typed this instead of saying it. Reshape the delivery, never the content. You
  are editing with a delete key, not a thesaurus: every content word in your
  answer must be one they said.
- Remove filler and verbal scaffolding: "like", "um", "you know", "kind of",
  "sort of", "I mean", "basically", "essentially", "literally", "actually",
  "obviously", "honestly", along with false starts, restarted sentences,
  repeated phrases, and self-echoes that carry no meaning.
- Condense a circumlocution into the plain statement it was reaching for, but
  only when the meaning is unambiguous and only using the speaker's own words.
  "I would also like it to have, essentially, if it fails to categorize a note,
  there should be like a maybe in the system" becomes "I would also like a
  'failed categorization' folder for notes it can't confidently categorize."
  When two readings are possible, keep the longer wording.
- Condensing means dropping words, never swapping them. Do not replace a word
  the speaker used with a synonym you prefer: not "focus" for what they called
  paying attention to, not "desire" for "want", not "utilize" for "use". Every
  content word in your output should be one they said. This is what keeps the
  cleaned text sounding like them, and a chunk that reads better in someone
  else's vocabulary has failed.
- Preserve the speaker's intent, uncertainty, and nuance. A hedge that qualifies
  a claim is content and stays: "I think", "maybe", "I don't know" mean
  something when they mark how sure the speaker is. A hedge that is pure
  delivery is filler and goes.
- Never add facts, names, dates, conclusions, or certainty that are not in the chunk.
- Never drop substance, and never delete a whole utterance or exchange. Every
  point made must survive; even small talk survives, in its short readable form.
- Fix obvious transcription punctuation and casing.
- Do not summarize inside "cleaned". The summary belongs in "chunk_summary".
- Leave garbled or uncertain passages visible rather than repairing them by guessing.
- Drop the timestamps. The original transcript is kept elsewhere in the note.
- Headings must be "##" or deeper. Never emit a level-one "#" heading.
- Use a table only when the speaker is genuinely listing tabular data.

Style by "recordingType":
- memo: readable first-person prose — the note the speaker would have typed. Add
  "##" headings only when the memo clearly moves between several distinct
  topics. No speaker labels.
- journal: chronological first-person paragraphs in the writer's own register.
  Preserve voice, emotion, and meaningful self-correction; remove the filler and
  false starts a written entry would never have contained.
- conversation: dialogue as "**Name:** what they said" paragraphs, one per turn,
  each turn in readable written form.
- therapy: dialogue as in conversation, with the highest fidelity of all. Remove
  only pure filler and false starts, and never condense. Keep hesitation and
  repetition that carries weight. Never add clinical language, interpretation,
  or diagnosis that was not spoken.
- meeting: "##" headings per topic. If, and only if, the chunk contains explicit
  decisions or assignments, end with "## Decisions" and "## Action Items"
  bullets drawn from what was actually said. Write "Unassigned" or "Not stated"
  rather than inferring an owner or a deadline.
- lecture: "##" and "###" headings following the material, with the lecturer's
  own examples kept. Audience questions as dialogue.
- external source (when "structuredFullContent" is true): remove filler and
  redundancy, regroup related passages, add headings, and improve readability.
  This is structured full-content cleanup, not a condensed study note: preserve
  every substantive claim, example, qualification, and disagreement.
- tiny (when "tiny" is true): the chunk is a few sentences. Fix punctuation and
  casing, remove filler, join it into one short paragraph, and stop. No
  headings, no lists.

Chunk context:
- "chunkIndex" of "chunkCount" tells you where you are. You are cleaning part of
  a transcript, not writing a document: do not add a title, an introduction, or
  a conclusion, and do not restate what earlier chunks covered.
- "headingsSoFar" lists headings already used earlier in this transcript. Match
  their depth, and do not repeat one.
- "previousTail" is the end of the previous cleaned chunk. If this chunk
  continues that sentence or that speaker's turn, continue it cleanly.
- "speakers" maps each label to the name to use. Use those names exactly. When a
  label maps to null there is one speaker only: use no labels at all.
- "glossary" lists specialist terms and names the vault owner uses that this
  chunk appears to contain in mistranscribed form. Each entry gives the correct
  spelling and the words the transcriber produced instead. Where that passage
  really is that term — the sound and the sense both fit — write the correct
  spelling. Where it is not, leave the text alone. Never introduce a glossary
  term into a passage that did not say it; a list of likely terms is not
  permission to add one, and this is the one rule here that outranks tidiness."""


def voice_context_for(record):
    """Select policy mode without inferring owner voice from ambiguous material."""
    if (
        record.get("material_role") == "owner-authored"
        and record.get("recording_type") in {"memo", "journal"}
        and record.get("effective_speakers") == 1
    ):
        return vault_voice.CONTEXT_OWNER
    if record.get("material_role") == "external-source":
        return vault_voice.CONTEXT_SOURCE
    return vault_voice.CONTEXT_NONE


def profile_site_for(record):
    """Where this recording sits, for the personal-context gate.

    Deliberately not ``voice_context_for``. That one answers "whose writing
    style should this follow", and for a two-voice recording the answer is
    nobody, so it returns ``none`` for therapy and meetings. This asks whose
    *life* the material is about, and for a therapy session that is
    unambiguously the owner -- the recording the layer helps most. Sharing the
    voice function would switch the profile off exactly there.
    """
    role = record.get("material_role")
    if role == "external-source":
        mode = vault_voice.CONTEXT_SOURCE
    elif role in {"owner-authored", "personal-exchange"}:
        mode = vault_voice.CONTEXT_OWNER
    else:
        mode = vault_voice.CONTEXT_NONE
    return vault_profile.profile_site(
        mode,
        routes=TYPE_TO_ROUTES.get(record.get("recording_type"), ()),
        stage="summary",
    )


def voice_note_type_for(record):
    if voice_context_for(record) == vault_voice.CONTEXT_SOURCE:
        return "source"
    return TYPE_TO_NOTE_TYPE[record["recording_type"]]


def cleanup_system(voice, context_mode):
    prefix = vault_voice.prompt_prefix(voice, context_mode)
    return f"{CLEANUP_SYSTEM}\n\n{prefix}" if prefix else CLEANUP_SYSTEM


def cleanup_payload(
    record,
    chunk,
    chunk_index,
    chunk_count,
    headings,
    previous_tail,
    speaker_map,
    drop_labels,
    tiny,
    voice=None,
    lexicon=None,
):
    turns = collapse_turns(chunk, {} if drop_labels else speaker_map)
    payload = {
        "recordingType": record["recording_type"],
        "chunkIndex": chunk_index,
        "chunkCount": chunk_count,
        "chunk": render_turns(turns),
        "materialRole": record.get("material_role", "unknown"),
    }
    context_mode = voice_context_for(record)
    compiled = vault_voice.compile_voice(
        voice,
        context_mode,
        note_type=voice_note_type_for(record),
        material=payload["chunk"],
    )
    if compiled["per_type_rule"]:
        payload["styleForThisKind"] = compiled["per_type_rule"]
    if compiled["vocabulary"]:
        payload["relevantVocabulary"] = compiled["vocabulary"]
    if context_mode == vault_voice.CONTEXT_SOURCE:
        payload["structuredFullContent"] = True
    if tiny:
        payload["tiny"] = True
    if drop_labels:
        payload["speakers"] = {label: None for label in speaker_map}
    elif speaker_map:
        payload["speakers"] = speaker_map
    if headings:
        payload["headingsSoFar"] = headings[-12:]
    if previous_tail:
        payload["previousTail"] = previous_tail[-300:]
    # Only terms this chunk plausibly garbled. Recorded variants were already
    # replaced in code before the chunk got here, so whatever is left is a
    # mistranscription nobody has written down yet.
    glossary = vault_lexicon.near_miss_terms(payload["chunk"], vault_lexicon.term_candidates(lexicon))
    if glossary:
        payload["glossary"] = glossary
    return payload, payload["chunk"]


def fold_diacritics(text):
    """``Śāntideva`` -> ``santideva``, ``sūtras`` -> ``sutras``.

    Every comparison in this file runs on ASCII word tokens, and a transcriber
    that writes "Shantideva" is describing the same person as a cleanup that
    writes "Śāntideva". Folding the marks away is what lets the two compare
    equal; without it the tokenizer cut at the first accented letter and handed
    back ``ntidevas``, a fragment with no root in the source, which then read as
    a fabricated word.
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def content_words(text):
    """Comparable words. Apostrophes are dropped so ``members'`` and ``members``
    are the same token on both sides of a comparison, and diacritics are folded
    so a name survives being spelled properly."""
    return WORD_RE.findall(fold_diacritics(text).casefold().replace("'", "").replace("’", ""))


def strip_structure(markdown):
    """Prose only: heading text is structure the editor is allowed to author, so
    it is excluded from the added-words check."""
    lines = []
    for line in str(markdown).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        stripped = re.sub(r"^[-*+]\s+", "", stripped)
        stripped = re.sub(r"^>\s*\[![a-zA-Z]+\]\s*", "", stripped)
        stripped = re.sub(r"^>\s*", "", stripped)
        stripped = re.sub(r"^\*\*[^*]{1,60}:?\*\*:?\s*", "", stripped)
        stripped = stripped.replace("|", " ")
        lines.append(stripped)
    return "\n".join(lines)


def added_words(source, cleaned, allowed):
    """Content words in the cleaned prose that were not in the source.

    Cleanup has one exact invariant available, worth more than any prompt rule: a
    content word was either spoken or invented. Dropped words are not checked
    here, because removing filler is the job.

    Turning speech into sentences legitimately adds words, though, and this has
    to see past that or it flags good work:

    - grammatical glue ("and", "which", "they") is inserted whenever fragments
      become sentences, so function words are never evidence of invention;
    - inflection changes ("expand" becoming "expands") come with the same repair;
    - the transcriber splits and joins names, so "rpg net" may be cleaned to
      "rpgnet" and "application" shortened to "app".

    What survives all three is a word with no root in the source at all, which is
    what fabrication actually looks like.
    """
    source_words = set(content_words(source))
    prefixes = {word[:length] for word in source_words for length in range(3, len(word) + 1)}
    allowance = {word for value in allowed for word in content_words(value)} | STRUCTURAL_WORDS | STOPWORDS

    def known(word):
        if word in allowance or word in source_words or word in prefixes:
            return True
        if any(word[:length] in source_words for length in range(3, len(word))):
            return True
        return len(word) >= 5 and word[:5] in prefixes

    invented = []
    for word in content_words(strip_structure(cleaned)):
        if not known(word) and word not in invented:
            invented.append(word)
    return invented


def heading_lines(markdown):
    return [line.strip() for line in str(markdown).splitlines() if line.strip().startswith("#")]


def check_chunk(cleaned, source, speaker_map, drop_labels, tiny, glossary=()):
    """Deterministic gate on one cleaned chunk. Returns a list of problems."""
    problems = []
    if not isinstance(cleaned, str) or not cleaned.strip():
        return ["response had no non-blank cleaned text"]
    for heading in heading_lines(cleaned):
        if re.match(r"^#\s", heading):
            problems.append(f"emitted a level-one heading: {heading!r}")
            break
    if tiny and heading_lines(cleaned):
        problems.append("added headings to a note too short to need them")
    for line in cleaned.splitlines():
        if TIMESTAMP_RE.match(line.strip()):
            problems.append(f"kept a transcript timestamp: {line.strip()!r}")
            break
    allowed = [] if drop_labels else [value for value in speaker_map.values() if value]
    # A corrected term is a word the source does not contain — that is the whole
    # point of correcting it — so the terms actually offered for this chunk have
    # to be allowed, or every successful correction reads as fabrication.
    allowed = allowed + [offer["term"] for offer in glossary]
    invented = added_words(source, cleaned, allowed)
    if len(invented) > MAX_INVENTED_WORDS:
        problems.append(f"these words are not in the chunk: {', '.join(invented[:8])}")
    # Any line opening with a bold span is a speaker label in this format,
    # whether the label is on its own line or inline as "**Name:** said this".
    if drop_labels and re.search(r"^\*\*[^*]{1,60}\*\*", cleaned, re.MULTILINE):
        problems.append("used speaker labels for a single-speaker recording")
    return problems


def clean_chunk_once(args, service, messages, source, speaker_map, drop_labels, tiny, task, glossary=()):
    value, _call = forge_llm.call_json_with_retry(
        service,
        messages,
        temperature=0,
        cache_prompt=args.cache_prompt,
        response_format={"type": "json_object"},
        timeout=args.request_timeout,
        api_key=args.api_key,
        task=task,
    )
    cleaned = value.get("cleaned") if isinstance(value, dict) else None
    problems = check_chunk(cleaned, source, speaker_map, drop_labels, tiny, glossary)
    chunk_summary = value.get("chunk_summary") if isinstance(value, dict) else ""
    if not isinstance(chunk_summary, str):
        chunk_summary = ""
    return cleaned, chunk_summary.strip(), problems


def clean_one_chunk(args, service, payload, source, speaker_map, drop_labels, tiny, system=CLEANUP_SYSTEM):
    """One chunk, with a single corrective retry that shows the model its own
    violation. Raises UserError when the retry fails too."""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    glossary = payload.get("glossary", ())
    cleaned, summary, problems = clean_chunk_once(
        args, service, messages, source, speaker_map, drop_labels, tiny, "clean-transcript-chunk", glossary
    )
    if not problems:
        return cleaned, summary
    # A retry that only says "unusable" gets the same answer back; naming the
    # violation is what changes it. Under the spoken-to-written register the
    # violation is almost always the same one -- reaching for a better word than
    # the speaker's -- so when that is what failed, say so in those terms.
    repair = messages + [
        {
            "role": "user",
            "content": f"That response was unusable: {problems[0]}. Return corrected JSON only. "
            + (
                "Those words are yours, not the speaker's. Put their wording back: condense by "
                "dropping words, never by replacing one with a word you prefer. Every content "
                "word in your answer must be one they said."
                if problems[0].startswith("these words are not in the chunk")
                else "Stay inside the speaker's own words and their own voice. Clean and condense "
                "how they said it; do not restate, describe, or explain what they said, and do "
                "not drop any point they made."
            ),
        }
    ]
    cleaned, summary, retry_problems = clean_chunk_once(
        args, service, repair, source, speaker_map, drop_labels, tiny, "clean-transcript-chunk-repair", glossary
    )
    if retry_problems:
        raise UserError(retry_problems[0])
    return cleaned, summary


def accepted_corrections(source, cleaned, glossary):
    """Offered terms the model actually applied to this chunk.

    The offer named the words the transcriber produced, so an accepted offer is
    a variant worth recording: next run it is corrected in code for free,
    without a model deciding anything.
    """
    if not glossary or not isinstance(cleaned, str):
        return []
    before = {vault_lexicon.fold(word) for word in content_words(source)}
    after = {vault_lexicon.fold(word) for word in content_words(cleaned)}
    accepted = []
    for offer in glossary:
        parts = [vault_lexicon.fold(word) for word in content_words(offer["term"])]
        if parts and all(part in after for part in parts) and not all(part in before for part in parts):
            accepted.append({"correct": offer["term"], "variant": offer["heardAs"]})
    return accepted


def clean_items(args, vault, items, class_records, run_dir, skip):
    """Clean every chunk of every transcript, one chunk per chat call."""
    journal_path = run_dir / "cleaned.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    journal = {}
    for row in prior:
        if row.get("path") and row.get("sha256") and row.get("chunk") is not None:
            journal[(row["path"], row["sha256"], row["chunk"])] = row
    service = chat_service(args)
    artifacts = run_dir / "cleaned"
    artifacts.mkdir(exist_ok=True)
    warnings = []
    results = {}
    plans = []
    for item in items:
        record = class_records.get(item["path"])
        if not item["is_transcript"] or item["path"] in skip or record is None:
            continue
        if record["needs_review"] or record["source"] == "failed":
            continue
        plans.append((item, record))
    total_chunks = 0
    prepared = []
    for item, record in plans:
        try:
            data = (vault / item["path"]).read_bytes()
            parsed = parse_transcript(transcript_source(split_frontmatter(data)["body"], vault))
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"{item['path']}: could not re-read for cleanup ({error})")
            results[item["path"]] = {"error": str(error)}
            continue
        lexicon = getattr(args, "compiled_lexicon", None)
        labels = ordered_labels(parsed["blocks"])
        speaker_map, drop_labels = derive_speaker_map(
            labels, record["speakers"], record["effective_speakers"], args.speaker_policy, args.owner, lexicon
        )
        tiny = item["stats"]["words"] < args.tiny_words
        # Corrections land before chunking, so the model reads the right
        # spelling and the added-words gate compares against the same text.
        blocks, corrections = correct_blocks(parsed["blocks"], (lexicon or {}).get("terms", []))
        chunks = chunk_blocks(blocks)
        prepared.append((item, record, parsed, speaker_map, drop_labels, tiny, chunks, corrections))
        total_chunks += len(chunks)
    done = 0
    durations = []
    for item, record, parsed, speaker_map, drop_labels, tiny, chunks, corrections in prepared:
        cleaned_chunks = []
        summaries = []
        headings = []
        proposals = []
        previous_tail = ""
        failure = None
        for index, chunk in enumerate(chunks, start=1):
            done += 1
            key = (item["path"], item["sha256"], index)
            row = journal.get(key)
            if row is not None and row.get("status") == "ok":
                cleaned = (artifacts / row["artifact"]).read_text(encoding="utf-8")
                cleaned_chunks.append(cleaned)
                summaries.append(row.get("chunk_summary", ""))
                headings.extend(heading_lines(cleaned))
                proposals.extend(row.get("proposals") or [])
                previous_tail = cleaned[-300:]
                continue
            if row is not None and row.get("status") == "failed":
                failure = row.get("error", "cleanup failed")
                break
            started = time.time()
            payload, source = cleanup_payload(
                record,
                chunk,
                index,
                len(chunks),
                headings,
                previous_tail,
                speaker_map,
                drop_labels,
                tiny,
                getattr(args, "compiled_voice", None),
                getattr(args, "compiled_lexicon", None),
            )
            try:
                cleaned, chunk_summary = clean_one_chunk(
                    args,
                    service,
                    payload,
                    source,
                    speaker_map,
                    drop_labels,
                    tiny,
                    system=cleanup_system(getattr(args, "compiled_voice", None), voice_context_for(record)),
                )
            except InterruptedError as error:
                raise UserError(f"cleanup was preempted by interactive activity: {error}") from error
            except (forge_llm.ChatError, UserError, ValueError) as error:
                failure = f"{type(error).__name__}: {error}"
                run_state.append_jsonl_fsync(
                    journal_path,
                    {
                        "path": item["path"],
                        "sha256": item["sha256"],
                        "chunk": index,
                        "status": "failed",
                        "error": failure,
                        "seconds": round(time.time() - started, 3),
                    },
                )
                warnings.append(f"{item['path']} chunk {index}: cleanup failed ({failure})")
                progress(f"[clean {done}/{total_chunks}] {item['path']} chunk {index}/{len(chunks)} FAILED: {failure}")
                break
            name = f"{sha256_text(item['path'])[:12]}-{index:04d}.md"
            (artifacts / name).write_text(cleaned, encoding="utf-8")
            elapsed = round(time.time() - started, 3)
            accepted = accepted_corrections(source, cleaned, payload.get("glossary", []))
            proposals.extend(accepted)
            run_state.append_jsonl_fsync(
                journal_path,
                {
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "chunk": index,
                    "chunks": len(chunks),
                    "status": "ok",
                    "artifact": name,
                    "cleaned_sha256": sha256_text(cleaned),
                    "chunk_summary": chunk_summary,
                    "proposals": accepted,
                    "seconds": elapsed,
                },
            )
            cleaned_chunks.append(cleaned)
            summaries.append(chunk_summary)
            headings.extend(heading_lines(cleaned))
            previous_tail = cleaned[-300:]
            durations.append(elapsed)
            eta = format_duration(sum(durations) / len(durations) * (total_chunks - done)) if durations else "-"
            progress(f"[clean {done}/{total_chunks}] {item['path']} chunk {index}/{len(chunks)} ({elapsed:.1f}s, eta {eta})")
        results[item["path"]] = {
            "cleaned": "\n\n".join(part.strip() for part in cleaned_chunks).strip() if not failure else None,
            "chunk_summaries": summaries,
            "chunks": len(chunks),
            "speaker_map": speaker_map,
            "drop_labels": drop_labels,
            "tiny": tiny,
            "corrections": corrections,
            "proposals": proposals,
            "error": failure,
        }
    return results, warnings


# --------------------------------------------------------------------------
# Chat stage 3: the summary paragraph
# --------------------------------------------------------------------------

SUMMARY_SYSTEM = """You write the one-paragraph summary that sits at the top of a transcribed recording's note.

Return exactly one JSON object and nothing else:
{"summary": "<one paragraph>"}

The reader is the person who made the recording, months later. Reading this
paragraph alone should tell them what the recording was and what mattered in it.

Rules:
- Exactly one paragraph. No headings, no bullets, no line breaks.
- At most 90 words, and fewer when the recording is slight. Density matters more
  than coverage: this is what the recording was about, not everything in it.
- Not a walkthrough. Never narrate the recording turn by turn or speaker by
  speaker ("Speaker 1 describes... Speaker 2 replies..." is a failure). Say what
  the recording is about and what mattered in it.
- Concrete. Name the actual subjects, decisions, and questions. "Various topics
  were discussed" is also a failure.
- Only what the transcript supports. Never add outcomes, feelings, diagnoses, or
  conclusions that were not said.
- Open with the substance, not with "This recording" or "In this transcript".
- Match the recording: for a memo, what the speaker is working out or wants to
  do; for a meeting, who met, what was decided, and what is outstanding; for a
  lecture, the material taught; for a conversation, what the people talked
  through; for therapy, the topics worked on, described plainly and without
  clinical interpretation.
- Use the names in "speakers" when they carry information. When the speakers are
unnamed, write about the subject matter instead of about "Speaker 1" — generic
labels tell the reader nothing.
- "personalContext" is standing background about the vault owner, given so you
  can tell what a passing reference means. It is not part of the recording:
  never state one of its facts as something the recording said, and never let it
  supply an outcome or a feeling the transcript does not."""

# Where a connection is allowed to come from. Shared by every reflection prompt
# so the citation contract is written once. The rule the vault owner asked for:
# the vault first, and anything from outside it has to be text this pipeline
# actually read, carrying the URL it came from. There is no live fetch here, so a
# fact the model merely remembers can never be checked and is not admissible.
REFLECTION_SOURCE_RULES = """Where connections may come from:
- The vault first. Use a "vaultCandidates" wikilink exactly as it is given to you.
- "outsideSources" is the only material from outside the vault available to you.
  Each entry is text this pipeline actually read, with the URL it came from. Use
  one only when its excerpt genuinely supports what you write.
- A connection drawn from "outsideSources" must begin `Outside vault:` and end
  with that entry's URL in parentheses. Never cite a URL that is not listed there.
- Never state a fact from outside the vault on your own authority. If you know
  something relevant and it is not in "outsideSources", leave it out. When
  "outsideSources" is empty, every connection is a vault wikilink or there are none.
- Do not manufacture content to fill a section. Empty arrays are correct and common."""

# The owner asked for this paragraph to read the same in every reflection prompt:
# the register is what stops standing background from being mistaken for
# something the recording said.
REFLECTION_CONTEXT_RULE = """- "personalContext" is standing background about the owner, given so a passing
  reference can be read for what it is. Use it to understand this recording, not
  as material to reflect on: it is not something the owner produced today, so
  never present one of its facts as an observation, and never let it turn a
  tentative reading into a settled one."""

JOURNAL_REFLECTION_SYSTEM = f"""You add a careful reflection after the owner's cleaned journal text.

Return exactly one JSON object:
{{"observations": ["..."], "interpretations": ["..."], "open_questions": ["..."], "connections": ["..."]}}

Rules:
- Observations state what the owner directly described before interpreting it.
- Interpretations are tentative and never diagnose, override, or claim privileged access to the owner's meaning.
- Open questions preserve uncertainty and invite later reflection.
- Connections must be directly relevant.
- Keep each item concise. Do not repeat the cleaned journal.
{REFLECTION_CONTEXT_RULE}

{REFLECTION_SOURCE_RULES}
"""

MEMO_REFLECTION_SYSTEM = f"""You add a short reflection after the owner's cleaned memo.

A memo is a working note — a task, an idea, a plan, a thought caught before it was
lost. It is not introspection, and this is not a summary: the memo is already
there. Your job is to place it against the rest of the vault and name what it
leaves open.

Return exactly one JSON object:
{{"context": ["..."], "open_questions": ["..."], "next_steps": ["..."], "connections": ["..."]}}

Rules:
- Context: what this memo belongs to — the project, thread, or earlier note it
  continues. One or two lines. Never a restatement of the memo.
- Open questions: what the memo leaves unresolved — a decision not made, a fact
  not known, a dependency not named. Only what the memo itself raises. Do not
  invent doubt the owner did not express.
- Next steps: an action the memo implies but did not state as a step. When the
  memo already lists its own steps, this section is empty.
- Connections: vault notes this relates to, with a few words on why.
- Keep each item concise. Do not repeat the cleaned memo.
- A two-line memo usually gets Connections only, and often nothing at all.
- Say nothing about the owner's state of mind. That is a journal's business, not
  a memo's.
{REFLECTION_CONTEXT_RULE}

{REFLECTION_SOURCE_RULES}
"""

# Which reflection a recording type gets, and the order its sections render in.
# Membership here is also the gate: a type absent from this table gets no
# reflection at all. Journal sections are introspective; memo sections are not,
# because "Interpretations" on an errand list is either empty or padding.
REFLECTION_SECTIONS = {
    "journal": (
        ("observations", "Observations"),
        ("interpretations", "Interpretations"),
        ("open_questions", "Open questions"),
        ("connections", "Connections"),
    ),
    "memo": (
        ("context", "Context"),
        ("open_questions", "Open questions"),
        ("next_steps", "Next steps"),
        ("connections", "Connections"),
    ),
}

REFLECTION_SYSTEMS = {"journal": JOURNAL_REFLECTION_SYSTEM, "memo": MEMO_REFLECTION_SYSTEM}


def summary_system(voice, context_mode, profile=None, site=None):
    parts = [SUMMARY_SYSTEM]
    prefix = vault_voice.prompt_prefix(voice, context_mode)
    if prefix:
        parts.append(prefix)
    if site is not None:
        background = vault_profile.profile_prefix(profile, site)
        if background:
            parts.append(background)
    return "\n\n".join(parts)


def connections_script():
    return Path(__file__).resolve().parents[2] / "vault-connections" / "scripts" / "vault-connections.py"


def connection_candidates(vault, query):
    script = connections_script()
    if not script.is_file():
        return [], "vault-connections is not installed; reflection has no vault candidates"
    try:
        completed = subprocess.run(
            [sys.executable, str(script), "search", query, "--vault", str(vault), "--search-limit", "10"],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return [], f"vault search failed: {error}"
    if completed.returncode != 0:
        return [], "vault search failed; reflection has no vault candidates"
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [], "vault search returned unreadable output"
    candidates = []
    for hit in (payload.get("data") or {}).get("hits") or []:
        relative = hit.get("path") if isinstance(hit, dict) else None
        if (
            relative
            and relative.endswith(".md")
            and not relative.startswith(f"{INBOX_DIR}/")
            and (vault / relative).is_file()
        ):
            candidates.append({"path": relative, "wikilink": f"[[{Path(relative).stem}]]"})
    return candidates[:8], None


def source_body(vault, relative, fallback=""):
    """The note's body as it sits on disk, for harvesting what the owner wrote.

    The cleaned text would nearly always do, but a link is exactly the kind of
    token a cleanup pass can wrap or truncate, and the raw file is the record.
    """
    try:
        return split_frontmatter((vault / relative).read_bytes())["body"]
    except (OSError, ValueError):
        return fallback


def outside_sources(material, vault, candidates):
    """This skill's half of the shared harvest: the recording plus its candidates.

    What counts as a citable line, and the refusal to fetch anything, live in
    ``vault_reflection`` because they are the same for a braindump. Only the
    label a recording's own citation carries is this skill's.
    """
    return vault_reflection.outside_sources(material, "this recording", vault, candidates)


def validate_reflection(value, recording_type, allowed_wikilinks, allowed_urls=()):
    """Render the reflection, dropping connections that cite nothing checkable.

    Returns ``(markdown, dropped)``. A malformed response is still fatal, but a
    single bad connection is not: raising here costs the note its summary *and*
    its reflection, which is a wildly disproportionate price for one line the
    model oversold. Dropped lines are returned so the caller can report them --
    excluded and recorded, never silently swallowed.
    """
    if not isinstance(value, dict):
        raise UserError("reflection response is not an object")
    sections, dropped = [], []
    for key, heading in REFLECTION_SECTIONS[recording_type]:
        raw = value.get(key, [])
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise UserError(f"reflection {key} must be an array of strings")
        items = [re.sub(r"\s+", " ", item).strip() for item in raw if item.strip()]
        if key == "connections":
            items = [item for item in items if keep_connection(item, allowed_wikilinks, allowed_urls, dropped)]
        if items:
            # A callout rather than a heading, and collapsed: as a heading this
            # was indistinguishable from one the cleanup wrote, which put the
            # model's reading of a recording on the same footing as the words
            # actually spoken.
            kind = vault_reflection.callout_type_for(heading)
            sections.append(render_callout(kind, heading, [f"- {item}" for item in items]))
    return "\n\n".join(sections).strip(), dropped


def keep_connection(item, allowed_wikilinks, allowed_urls, dropped):
    """A connection survives only if it points at something that can be checked."""
    links = WIKILINK_RE.findall(item)
    if links:
        unknown = [link for link in links if link not in allowed_wikilinks]
        if unknown:
            dropped.append(f"wikilink not in the vault: {unknown[0]}")
            return False
        return True
    if not item.startswith("Outside vault:"):
        dropped.append(f"no vault link and not labelled `Outside vault:`: {item[:80]}")
        return False
    cited = [url.rstrip(".,;:)") for url in URL_RE.findall(item)]
    if not any(url in allowed_urls for url in cited):
        detail = f"cites {cited[0]}" if cited else "cites no source"
        dropped.append(f"outside connection {detail}, which this run never read: {item[:80]}")
        return False
    return True


def reflect_note(args, service, record, cleaned, candidates, sources=()):
    recording_type = record["recording_type"]
    payload = {
        "title": record["title"],
        "cleanedText": cleaned[:12000],
        "vaultCandidates": candidates,
        "outsideSources": list(sources),
    }
    site = profile_site_for(record)
    profile = getattr(args, "compiled_profile", None)
    selected = [
        card
        for card in vault_profile.select_cards(profile, cleaned, site)
        if card["tier"] != vault_profile.TIER_ALWAYS
    ]
    if selected:
        payload["personalContext"] = vault_profile.profile_offers(selected)
    system = REFLECTION_SYSTEMS[recording_type]
    prefix = vault_voice.prompt_prefix(getattr(args, "compiled_voice", None), vault_voice.CONTEXT_OWNER)
    if prefix:
        system += "\n\n" + prefix
    background = vault_profile.profile_prefix(profile, site)
    if background:
        system += "\n\n" + background
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    allowed_wikilinks = {entry["wikilink"] for entry in candidates}
    allowed_urls = {entry["url"] for entry in payload["outsideSources"]}
    value, _call = forge_llm.call_json_with_retry(
        service,
        messages,
        temperature=0,
        cache_prompt=args.cache_prompt,
        response_format={"type": "json_object"},
        timeout=args.request_timeout,
        api_key=args.api_key,
        task=f"reflect-{recording_type}",
    )
    try:
        return validate_reflection(value, recording_type, allowed_wikilinks, allowed_urls)
    except UserError as error:
        # The reasoning validate_reflection already gives for a single bad
        # connection applies harder to a malformed section: this costs the note
        # its summary too, which is far too much for one line the model got
        # wrong in shape rather than substance. One more ask, as classification
        # and cleanup both get.
        repair = messages + [
            {
                "role": "user",
                "content": f"That response was unusable: {error}. Return corrected JSON only, following the schema exactly.",
            }
        ]
        value, _call = forge_llm.call_json_with_retry(
            service,
            repair,
            temperature=0,
            cache_prompt=args.cache_prompt,
            response_format={"type": "json_object"},
            timeout=args.request_timeout,
            api_key=args.api_key,
            task=f"reflect-{recording_type}-repair",
        )
        return validate_reflection(value, recording_type, allowed_wikilinks, allowed_urls)


def check_summary(summary):
    """Deterministic gate on a summary. Returns a list of problems."""
    if not isinstance(summary, str) or not summary.strip():
        return ["response had no non-blank summary"]
    problems = []
    if "\n\n" in summary.strip():
        problems.append("summary is more than one paragraph")
    words = len(summary.split())
    if words > SUMMARY_MAX_WORDS:
        problems.append(f"summary is {words} words, over the {SUMMARY_MAX_WORDS}-word limit")
    for opener in ("this recording", "in this transcript", "this transcript", "this conversation"):
        if summary.strip().casefold().startswith(opener):
            problems.append(f"summary opens with {opener!r} instead of the substance")
            break
    return problems


def summarize_once(args, service, messages, task):
    value, _call = forge_llm.call_json_with_retry(
        service,
        messages,
        temperature=0,
        cache_prompt=args.cache_prompt,
        response_format={"type": "json_object"},
        timeout=args.request_timeout,
        api_key=args.api_key,
        task=task,
    )
    summary = value.get("summary") if isinstance(value, dict) else None
    return summary, check_summary(summary)


def summarize_one(args, service, messages):
    """One summary, with a single corrective retry that shows the model its own
    violation. A summary that runs long is worth asking again for; it is not
    worth holding back the whole note over."""
    summary, problems = summarize_once(args, service, messages, "summarize-transcript")
    if not problems:
        return re.sub(r"\s+", " ", summary).strip()
    # A retry that only says "unusable" gets the same length back. Name the target.
    repair = messages + [
        {
            "role": "user",
            "content": f"That response was unusable: {problems[0]}. Return corrected JSON only, "
            f"with the summary rewritten as one paragraph of at most {SUMMARY_TARGET_WORDS} words, "
            "keeping only what the recording was most about.",
        }
    ]
    summary, retry_problems = summarize_once(args, service, repair, "summarize-transcript-repair")
    if retry_problems:
        raise UserError(retry_problems[0])
    return re.sub(r"\s+", " ", summary).strip()


def summarize_items(args, vault, items, class_records, clean_results, run_dir, skip):
    journal_path = run_dir / "summaries.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    journal = {(row["path"], row["sha256"]): row for row in prior if row.get("path") and row.get("sha256")}
    service = chat_service(args)
    warnings = []
    results = {}
    pending = []
    for item in items:
        record = class_records.get(item["path"])
        cleaned = clean_results.get(item["path"])
        if record is None or cleaned is None or cleaned.get("error") or not cleaned.get("cleaned"):
            continue
        if item["path"] in skip:
            continue
        if cleaned["tiny"] and args.tiny_summary == "omit":
            results[item["path"]] = {"summary": None, "skipped": "tiny"}
            continue
        pending.append((item, record, cleaned))
    total = len(pending)
    durations = []
    for position, (item, record, cleaned) in enumerate(pending, start=1):
        key = (item["path"], item["sha256"])
        if key in journal:
            results[item["path"]] = journal[key]
            continue
        started = time.time()
        body = cleaned["cleaned"]
        payload = {
            "recordingType": record["recording_type"],
            "materialRole": record.get("material_role", "unknown"),
            "title": record["title"],
            "durationSeconds": item["stats"]["duration_seconds"],
        }
        context_mode = voice_context_for(record)
        site = profile_site_for(record)
        compiled = vault_voice.compile_voice(
            getattr(args, "compiled_voice", None),
            context_mode,
            note_type=voice_note_type_for(record),
            material=body,
        )
        if compiled["per_type_rule"]:
            payload["styleForThisKind"] = compiled["per_type_rule"]
        if compiled["vocabulary"]:
            payload["relevantVocabulary"] = compiled["vocabulary"]
        selected = [
            card
            for card in vault_profile.select_cards(getattr(args, "compiled_profile", None), body, site)
            if card["tier"] != vault_profile.TIER_ALWAYS
        ]
        if selected:
            payload["personalContext"] = vault_profile.profile_offers(selected)
        if cleaned["speaker_map"] and not cleaned["drop_labels"]:
            payload["speakers"] = sorted({value for value in cleaned["speaker_map"].values() if value})
        if cleaned["tiny"]:
            payload["oneSentence"] = True
        if cleaned["chunks"] > 1 or len(body) > SUMMARY_INPUT_CHARS:
            # A recording that needed several cleanup calls is summarized from
            # those sections rather than by pushing the whole thing back through.
            payload["sectionSummaries"] = [text for text in cleaned["chunk_summaries"] if text]
            payload["cleanedHead"] = body[:6000]
        else:
            payload["cleaned"] = body
        messages = [
            {
                "role": "system",
                "content": summary_system(
                    getattr(args, "compiled_voice", None),
                    context_mode,
                    getattr(args, "compiled_profile", None),
                    site,
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            summary = summarize_one(args, service, messages)
            reflection = None
            if context_mode == vault_voice.CONTEXT_OWNER and record["recording_type"] in REFLECTION_SECTIONS:
                candidates, warning = connection_candidates(vault, f"{record['title']} {summary}")
                if warning:
                    warnings.append(f"{item['path']}: {warning}")
                # Reprocessing reads the recording, never the note on disk: the
                # head there is a previous run's output, and its own
                # `Outside vault:` lines would launder URLs this run never read
                # back into admissibility.
                material = (
                    transcript_source(source_body(vault, item["path"], body), vault)
                    if getattr(args, "reprocessing", False)
                    else source_body(vault, item["path"], body)
                )
                sources = outside_sources(material, vault, candidates)
                reflection, dropped = reflect_note(args, service, record, body, candidates, sources)
                for detail in dropped:
                    warnings.append(f"{item['path']}: dropped a connection — {detail}")
            row = {
                "path": item["path"],
                "sha256": item["sha256"],
                "summary": summary,
                "reflection": reflection,
                "skipped": None,
            }
        except InterruptedError as error:
            raise UserError(f"summarizing was preempted by interactive activity: {error}") from error
        except (forge_llm.ChatError, UserError, ValueError) as error:
            message = f"{type(error).__name__}: {error}"
            warnings.append(f"{item['path']}: summary failed ({message})")
            row = {"path": item["path"], "sha256": item["sha256"], "summary": None, "skipped": message}
        row["seconds"] = round(time.time() - started, 3)
        results[item["path"]] = row
        run_state.append_jsonl_fsync(journal_path, row)
        durations.append(row["seconds"])
        eta = format_duration(sum(durations) / len(durations) * (total - position)) if durations else "-"
        progress(f"[summarize {position}/{total}] {item['path']} ({row['seconds']:.1f}s, eta {eta})")
    return results, warnings


# --------------------------------------------------------------------------
# Assembly and per-file deterministic checks
# --------------------------------------------------------------------------


def rare_words(text):
    """Long, infrequent, content-bearing words: names, subjects, technical terms.

    Stopwords are excluded however rarely they appear. In a two-minute memo
    "people" and "going" occur once or twice and would otherwise count as
    distinctive content, so dropping them as filler — which is the job — would
    read as losing the substance.
    """
    def ordinary(word):
        # Prefix-matched so inflections go with their root: "thats" with "that",
        # "looking" with "look".
        return any(word[:length] in STOPWORDS for length in range(3, len(word) + 1))

    counts = Counter(word for word in content_words(text) if len(word) >= 5 and not ordinary(word))
    return {word for word, count in counts.items() if count <= 3}


def rare_word_retention(source, cleaned):
    """Fraction of the source's distinctive words that survived cleanup.

    Only meaningful with enough source to be distinctive about: on a short memo
    the rare-word set is mostly ordinary vocabulary, and the utterance locator
    covers those files better than this does.
    """
    if len(source.split()) < RARE_WORD_MIN_SOURCE_WORDS:
        return 1.0, []
    source_rare = rare_words(source)
    if len(source_rare) < MIN_RARE_WORDS:
        return 1.0, []
    present = set(content_words(cleaned))
    missing = sorted(source_rare - present)
    return (len(source_rare) - len(missing)) / len(source_rare), missing


def best_containment(block_text, cleaned_words):
    """How much of one source utterance can be found in any one window of the
    cleaned text. A dropped passage scores near zero wherever you look."""
    wanted = set(content_words(block_text))
    if len(wanted) < FIDELITY_MIN_WORDS:
        return 1.0, 0
    window = max(len(wanted) * 3, 20)
    step = max(1, window // 4)
    # The end-anchored window is not optional: without it a cleaned text shorter
    # than one window plus one step never gets its tail looked at, and the last
    # utterance of every short note reads as missing.
    starts = list(range(0, max(1, len(cleaned_words) - window + 1), step))
    final = max(0, len(cleaned_words) - window)
    if final not in starts:
        starts.append(final)
    best, best_at = 0.0, 0
    for start in starts:
        found = len(wanted & set(cleaned_words[start : start + window]))
        score = found / len(wanted)
        if score > best:
            best, best_at = score, start
    return best, best_at


def fidelity_samples(path, blocks, count=FIDELITY_SAMPLES):
    """Sample source utterances to spot-check, seeded by path so a resumed run
    checks the same ones."""
    usable = [block for block in blocks if len(content_words(block["text"])) >= FIDELITY_MIN_WORDS]
    if not usable:
        return []
    rng = random.Random(sha256_text(path))
    chosen = rng.sample(usable, min(count, len(usable)))
    return sorted(chosen, key=lambda block: block["seconds"])


def strip_callout_lines(text):
    """Generated apparatus out of a passage that is supposed to be the cleanup.

    The summary and the reflection sit above the cleaned text, and everything
    the pipeline writes there is a callout. Cleanup never authors a blockquote,
    so dropping the quoted lines leaves the cleaned prose alone -- which is what
    the fidelity check is supposed to be comparing the recording against. Left
    in, a summary paraphrasing an utterance could answer for a cleanup that
    dropped it.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith(">"))


def render_summary(summary, style):
    if style == "callout":
        return render_callout("summary", None, [summary], collapsed=False)
    if style == "heading":
        return f"## Summary\n\n{summary}"
    return summary


def raw_note_stem(processed_name):
    """``2026-07-24 - Therapy - Family.md`` -> ``2026-07-24 - Therapy - Family - Transcript``."""
    stem = processed_name[:-3] if processed_name.lower().endswith(".md") else processed_name
    return safe_title(stem + RAW_NOTE_SUFFIX)


def raw_supported(schema):
    """Whether this vault can hold the recording as its own source note.

    A vault whose schema has no ``source`` type or no ``transcript`` source kind
    cannot describe the recording's own note, so the pipeline keeps writing the
    single combined note it always did rather than inventing vocabulary.
    """
    return "source" in schema["types"] and RAW_SOURCE_KIND in schema["source_kinds"]


def raw_metadata(schema, recording_type, processed_stem):
    """Frontmatter for the recording's own note.

    Deliberately not ``processed_by``: nothing processed this note, that is the
    whole point of it. ``parent`` is the only tie back to the note that was made
    from it, and filing replaces frontmatter wholesale, so the organizer carries
    it forward with ``type`` and ``source_kind``.
    """
    metadata = {
        "type": "source",
        "status": RAW_STATUS,
        "source_kind": RAW_SOURCE_KIND,
        "capture_type": TYPE_TO_CAPTURE[recording_type],
        "parent": f"[[{processed_stem}]]",
    }
    if metadata["status"] not in schema["statuses"]:
        metadata["status"] = "raw" if "raw" in schema["statuses"] else next(iter(schema["statuses"]))
    if metadata["capture_type"] not in schema["capture_types"]:
        metadata.pop("capture_type")
    return {key: value for key, value in metadata.items() if key in schema["properties"]}


def build_raw_note(schema, metadata, raw_body):
    """The recording, with frontmatter and nothing else added."""
    return serialize_frontmatter(metadata, schema) + "\n" + raw_body


TRANSCRIPT_MARKER = "\n\n# Transcript\n\n"


def assemble_head(summary, style, preamble, cleaned, reflection=None):
    """The generated section of a note, ending at the ``# Transcript`` marker.

    Generated material sits at the top, together and in callouts, and the
    speaker's own words below it: what the machine made of a recording is worth
    a glance before the recording itself, and it should never be mistaken for
    the recording. The handwritten preamble stays next to the cleaned text
    because it is the owner's writing too, not apparatus.
    """
    sections = []
    if summary:
        sections.append(render_summary(summary, style))
    if reflection:
        sections.append(reflection.strip())
    if preamble.strip():
        sections.append(preamble.strip())
    sections.append(cleaned.strip())
    return "\n\n".join(sections) + TRANSCRIPT_MARKER


def build_note(schema, metadata, summary, style, preamble, cleaned, raw_body, reflection=None, raw_stem=None):
    """Assemble the final note.

    Everything above the ``# Transcript`` marker is generated. Below it goes
    either the recording itself, byte for byte, or -- when the recording is
    getting its own note -- a link to it and nothing else. Splitting them keeps a
    processed note readable at the length of what it says rather than the length
    of what was said, and lets the recording be filed as the source it is instead
    of riding along inside a note about it.
    """
    head = serialize_frontmatter(metadata, schema) + "\n" + assemble_head(summary, style, preamble, cleaned, reflection)
    if raw_stem is None:
        return head + raw_body, head
    return head + f"[[{raw_stem}]]\n", head


def frontmatter_metadata(schema, recording_type):
    metadata = {
        "type": TYPE_TO_NOTE_TYPE[recording_type],
        "status": "raw",
        "capture_type": TYPE_TO_CAPTURE[recording_type],
    }
    # capture_type stays the recording's own channel: this note did enter the
    # vault as voice or as a meeting, and that stays true however much cleanup
    # ran. What the pipeline did to it is a separate fact, and a separate
    # property, so a reader can tell a hand-typed note from a model-cleaned one
    # without losing how either arrived. Vaults whose schema note predates the
    # property simply do not carry it.
    if schema["properties"].get("processed_by", {}).get("shape") == "list":
        metadata["processed_by"] = [WORKFLOW]
    metadata = {key: value for key, value in metadata.items() if key in schema["properties"]}
    if metadata.get("type") not in schema["types"]:
        raise UserError(f"schema does not define note type {metadata.get('type')!r}")
    if metadata.get("status") not in schema["statuses"]:
        raise UserError("schema does not define status 'raw'")
    if "capture_type" in metadata and metadata["capture_type"] not in schema["capture_types"]:
        raise UserError(f"schema does not define capture type {metadata['capture_type']!r}")
    return metadata


def corrected_source_text(parsed, args, proposals=()):
    """The source as the cleanup actually saw it.

    Corrected terms are distinctive by definition, which is exactly what the
    rare-word check counts. Measuring the cleaned text against the uncorrected
    source would score every successful correction as a dropped word and hold
    back the term-heavy recordings the lexicon helps most.
    """
    text = " ".join(block["text"] for block in parsed["blocks"])
    lexicon = getattr(args, "compiled_lexicon", None)
    entries = list((lexicon or {}).get("terms", []))
    # What the model corrected on offer is not in the dictionary yet, so it has
    # to be applied here too or it reads the same way.
    entries.extend(
        vault_lexicon.normalize_entry({"correct": row["correct"], "variants": [row["variant"]]})
        for row in proposals or ()
    )
    return vault_lexicon.apply_corrections(text, entries)[0] if entries else text


def check_note(item, cleaned, summary, note_text, head, parsed, args, proposals=(), raw_text=None, raw_stem=None, tail=None):
    """Every deterministic check that can fail a note, in one place.

    Returns (problems, measurements). A problem holds the note back: it keeps
    its name and its body, and lands in the review queue.
    """
    problems = []
    heads = [line for line in head.splitlines() if re.match(r"^#\s", line.strip())]
    if heads != ["# Transcript"]:
        problems.append(f"generated section must contain exactly one level-one heading (found {heads})")
    # Whichever note ends up holding the recording holds all of it. The pair only
    # loses nothing if the processed note points at the recording and the
    # recording note is the source bytes with frontmatter in front. Reprocessing
    # rewrites neither -- it reattaches the section it found, whatever shape it
    # was in -- so there the check is that the section came back untouched.
    if tail is not None:
        if not note_text.endswith(tail):
            problems.append("the transcript section was not reattached unchanged")
    elif raw_text is None:
        if not note_text.endswith(item["raw_body"]):
            problems.append("raw transcript section is not byte-identical to the source body")
    else:
        if not raw_text.endswith(item["raw_body"]):
            problems.append("raw transcript note is not byte-identical to the source body")
        if not note_text.endswith(f"# Transcript\n\n[[{raw_stem}]]\n"):
            problems.append("transcript section must hold exactly one link to the raw transcript note")
    source_text = corrected_source_text(parsed, args, proposals)
    source_words = len(source_text.split())
    # Prose only. A short dialogue carries one "**Name:**" per turn, and counting
    # those as content makes faithful cleanup of a chatty exchange look padded.
    cleaned_words = len(strip_structure(cleaned).split())
    ratio = cleaned_words / source_words if source_words else 1.0
    floor = TINY_RATIO_MIN if item["stats"]["words"] < args.tiny_words else CLEANED_RATIO_MIN
    if source_words and not floor <= ratio <= CLEANED_RATIO_MAX:
        problems.append(f"cleaned length is {ratio:.2f}x the source, outside {floor}-{CLEANED_RATIO_MAX}")
    retention, missing = rare_word_retention(source_text, cleaned)
    if retention < RARE_WORD_RETENTION:
        problems.append(
            f"only {retention:.0%} of distinctive source words survived cleanup (missing: {', '.join(missing[:8])})"
        )
    if summary:
        if "\n\n" in summary.strip():
            problems.append("summary is more than one paragraph")
        if len(summary.split()) > SUMMARY_MAX_WORDS:
            problems.append(f"summary is {len(summary.split())} words, over the {SUMMARY_MAX_WORDS}-word limit")
    if parsed["preamble"].strip() and parsed["preamble"].strip() not in head:
        problems.append("handwritten preamble did not survive into the generated section")
    cleaned_word_list = content_words(cleaned)
    weak = []
    for block in fidelity_samples(item["path"], parsed["blocks"]):
        score, _at = best_containment(block["text"], cleaned_word_list)
        if score < FIDELITY_MIN_CONTAINMENT:
            weak.append({"seconds": block["seconds"], "score": round(score, 3), "text": block["text"][:200]})
    if weak:
        problems.append(f"{len(weak)} sampled utterance(s) could not be located in the cleaned text")
    return problems, {
        "cleaned_ratio": round(ratio, 3),
        "rare_word_retention": round(retention, 3),
        "weak_samples": weak,
        "source_words": source_words,
        "cleaned_words": cleaned_words,
    }


def base_record(item):
    return {
        "source": item["path"],
        "source_hash": item["sha256"],
        "destination": None,
        "action": "none",
        "status": "ok",
        "needs_review": False,
        "review_reason": None,
        "warnings": [],
        "recording_type": None,
        "title": None,
        "summary": None,
        "stats": item["stats"],
    }


def review_record(item, reason, status="review", warning=None):
    record = base_record(item)
    record["status"] = status
    record["needs_review"] = True
    record["review_reason"] = reason
    if warning:
        record["warnings"].append(warning)
    return record


def already_applied_record(item, entry, vault):
    """A record for work a previous run already committed.

    Per the run contract an operation counts as complete only when the
    filesystem agrees; a missing destination blocks for review rather than being
    quietly re-applied over whatever is there now.
    """
    record = base_record(item)
    record["destination"] = entry.get("destination")
    record["already_applied"] = True
    record["action"] = "none"
    if not record["destination"] or not (vault / record["destination"]).exists():
        record["status"] = "review"
        record["needs_review"] = True
        record["review_reason"] = (
            f"a previous run recorded {entry.get('op')} to {record['destination']!r}, "
            "but that file is not there now"
        )
    return record


def assemble_items(args, vault, schema, items, class_records, clean_results, summaries, losers, held, applied, run_dir):
    """Build each final note, name it, and run every deterministic check."""
    artifacts = run_dir / "assembled"
    artifacts.mkdir(exist_ok=True)
    warnings = []
    records = []
    split_raw = raw_supported(schema)
    if not split_raw:
        warnings.append(
            "schema has no source type or transcript source kind; keeping the recording inside each note"
        )
    taken = set()
    for existing in scan_existing_names(vault):
        taken.add(existing)
    planned = []
    for item in items:
        rel = item["path"]
        if rel in applied:
            records.append(already_applied_record(item, applied[rel], vault))
            continue
        loser = losers.get(rel)
        if loser:
            record = base_record(item)
            record["destination"] = loser["quarantine_to"]
            record["action"] = "quarantine"
            record["duplicate_of"] = loser["winner"]
            records.append(record)
            continue
        if rel in held:
            records.append(review_record(item, "same recording id as another inbox note; resolve by hand"))
            continue
        if not item["is_transcript"]:
            record = base_record(item)
            record["status"] = "skipped"
            record["review_reason"] = item["skip_reason"]
            records.append(record)
            continue
        planned.append(item)

    for item in planned:
        rel = item["path"]
        record = class_records.get(rel)
        cleaned_result = clean_results.get(rel)
        if record is None:
            records.append(review_record(item, "no classification record"))
            continue
        if record["needs_review"] or record["source"] == "failed":
            records.append(review_record(item, record["review_reason"] or "classification asked for review"))
            continue
        if cleaned_result is None or cleaned_result.get("error") or not cleaned_result.get("cleaned"):
            reason = (cleaned_result or {}).get("error") or "cleanup produced nothing"
            records.append(review_record(item, f"cleanup failed: {reason}"))
            continue
        summary_row = summaries.get(rel) or {}
        summary = summary_row.get("summary")
        if summary is None and summary_row.get("skipped") and summary_row["skipped"] != "tiny":
            records.append(review_record(item, f"summary failed: {summary_row['skipped']}"))
            continue
        # The recording's note is named after the processed one and the processed
        # one links to it by name, so both names are settled before either note is
        # built. A note that fails its checks below keeps its original name and
        # leaves these reserved but unused, which costs at most a plainer name for
        # a later recording of the same day, type, and topic.
        date = recording_date(record, item)
        destination = assign_unique_name(
            vault, INBOX_DIR, args, date, item["time_hhmm"], record["recording_type"], record["title"], taken, rel
        )
        raw_stem = raw_note_stem(Path(destination).name) if split_raw else None
        raw_destination = assign_raw_name(vault, INBOX_DIR, raw_stem, taken) if raw_stem else None
        try:
            data = (vault / rel).read_bytes()
            if sha256_bytes(data) != item["sha256"]:
                records.append(review_record(item, "note changed on disk during this run"))
                continue
            raw_body = transcript_source(split_frontmatter(data)["body"], vault)
            parsed = parse_transcript(raw_body)
            metadata = frontmatter_metadata(schema, record["recording_type"])
            note_text, head = build_note(
                schema,
                metadata,
                summary,
                args.summary_style,
                parsed["preamble"],
                cleaned_result["cleaned"],
                raw_body,
                reflection=summary_row.get("reflection"),
                raw_stem=raw_stem,
            )
            raw_text = (
                build_raw_note(
                    schema, raw_metadata(schema, record["recording_type"], Path(destination).stem), raw_body
                )
                if raw_stem
                else None
            )
            problems, measurements = check_note(
                {**item, "raw_body": raw_body},
                cleaned_result["cleaned"],
                summary,
                note_text,
                head,
                parsed,
                args,
                cleaned_result.get("proposals") or [],
                raw_text=raw_text,
                raw_stem=raw_stem,
            )
        except (OSError, UnicodeDecodeError, UserError, ValueError) as error:
            message = f"{type(error).__name__}: {error}"
            warnings.append(f"{rel}: assembly failed ({message})")
            records.append(review_record(item, f"assembly failed: {message}", status="failed", warning=message))
            continue

        assembled = base_record(item)
        assembled["recording_type"] = record["recording_type"]
        assembled["title"] = record["title"]
        assembled["summary"] = summary
        assembled["reflection"] = summary_row.get("reflection")
        assembled["material_role"] = record.get("material_role", "unknown")
        assembled["voice_context_mode"] = voice_context_for(record)
        assembled["voice_applied"] = bool(
            getattr(args, "compiled_voice", None) and assembled["voice_context_mode"] != vault_voice.CONTEXT_NONE
        )
        assembled["voice_reason"] = (
            "single-speaker owner memo or journal"
            if assembled["voice_context_mode"] == vault_voice.CONTEXT_OWNER
            else "external source"
            if assembled["voice_context_mode"] == vault_voice.CONTEXT_SOURCE
            else "personal exchange or ambiguous material; owner voice not applied"
        )
        assembled["speaker_map"] = cleaned_result["speaker_map"]
        assembled["corrections"] = cleaned_result.get("corrections") or []
        assembled["proposals"] = cleaned_result.get("proposals") or []
        assembled["roster_speakers"] = sorted(
            {
                entry["who"]
                for entry in (record.get("speakers") or {}).values()
                if entry.get("source") == "roster" and entry.get("who") not in (None, "unknown")
            }
        )
        assembled["chunks"] = cleaned_result["chunks"]
        assembled["tiny"] = cleaned_result["tiny"]
        assembled["measurements"] = measurements
        assembled["checks"] = problems
        assembled["classification_source"] = record["source"]
        assembled["warnings"] = list(record.get("warnings") or [])
        if not date:
            assembled["warnings"].append("no recording date in the filename; the new name has no date prefix")
        if record["spoken_date"] and item["date"] and record["spoken_date"] != item["date"]:
            assembled["warnings"].append(
                f"transcript says {record['spoken_date']} but the filename says {item['date']}; kept the filename date"
            )
        if problems:
            assembled["status"] = "review"
            assembled["needs_review"] = True
            assembled["review_reason"] = "; ".join(problems)
            assembled["warnings"].extend(problems)
            records.append(assembled)
            continue
        name = f"{sha256_text(rel)[:12]}.md"
        (artifacts / name).write_text(note_text, encoding="utf-8")
        assembled["artifact"] = name
        assembled["final_hash"] = sha256_text(note_text)
        assembled["date"] = date
        assembled["destination"] = destination
        if raw_text is not None:
            raw_name = f"{sha256_text(rel)[:12]}.raw.md"
            (artifacts / raw_name).write_text(raw_text, encoding="utf-8")
            assembled["raw_artifact"] = raw_name
            assembled["raw_final_hash"] = sha256_text(raw_text)
            assembled["raw_destination"] = raw_destination
        destinations = [assembled["destination"]] + ([raw_destination] if raw_destination else [])
        if any(not path_is_inside(vault, vault / candidate) for candidate in destinations):
            assembled["status"] = "failed"
            assembled["needs_review"] = True
            assembled["review_reason"] = "destination escapes the vault"
            records.append(assembled)
            continue
        assembled["action"] = "process"
        records.append(assembled)

    ordered = {record["source"]: record for record in records}
    result = [ordered[item["path"]] for item in items if item["path"] in ordered]
    for assembled in result:
        classification = class_records.get(assembled["source"])
        if not classification:
            continue
        context_mode = voice_context_for(classification)
        assembled.setdefault("material_role", classification.get("material_role", "unknown"))
        assembled.setdefault("voice_context_mode", context_mode)
        assembled.setdefault(
            "voice_applied",
            bool(getattr(args, "compiled_voice", None) and context_mode != vault_voice.CONTEXT_NONE),
        )
        assembled.setdefault(
            "voice_reason",
            "single-speaker owner memo or journal"
            if context_mode == vault_voice.CONTEXT_OWNER
            else "external source"
            if context_mode == vault_voice.CONTEXT_SOURCE
            else "personal exchange or ambiguous material; owner voice not applied",
        )
    run_state.atomic_write_json(run_dir / "assembled.json", {"records": plan_for_json(result)})
    return result, warnings


def scan_existing_names(vault):
    """Case-folded relative paths of everything already in the inbox, so a new
    name never lands on an existing note."""
    root = vault / INBOX_DIR
    names = set()
    if not root.is_dir():
        return names
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [name for name in sorted(dirnames) if not name.startswith(".")]
        for filename in filenames:
            names.add(relative_path(vault, dirpath / filename).casefold())
    return names


# --------------------------------------------------------------------------
# Think verification
# --------------------------------------------------------------------------

VERIFY_NOTES_SYSTEM = """You are reviewing how voice recordings were titled and summarized for one person's Obsidian vault.

A faster model without reasoning read each transcript and proposed a recording
type, a filename, a one-paragraph summary, and speaker names. You get the head
and tail of the original transcript as evidence.

Flag an item only when it is actually wrong on that evidence:
- the recording type does not match what the transcript plainly is,
- the title does not describe this recording, or names the medium instead of the content,
- the summary states something the transcript does not support, or is too vague
  to tell the reader what the recording was,
- a speaker is given a real name that neither the transcript nor the roster justifies.

Names in "rosterSpeakers" come from the vault owner's own roster of people they
record. The roster settles that this person may be in the recording, so do not
flag such a name merely because the excerpt never says it aloud — that is what
the roster is for, and it is not a contradiction.

The roster settles nothing about **which** speaker they are. Check that
attribution against the excerpt like any other claim: whose work, whose meeting,
whose title, who is addressed about what. Flag it when the excerpt shows the
name is on the wrong voice, and say which voice it belongs to.

A defensible title or summary is 'ok' even if you would have written it
differently; taste is not an error. You are seeing excerpts, so do not flag a
summary merely for covering material outside them."""

VERIFY_FIDELITY_SYSTEM = """You are checking whether a cleaned-up transcript still says what was actually said.

For each item you get one verbatim utterance from the raw transcript and the
passage of the cleaned version that best matches it.

The source is raw speech-to-text of one person talking. It is full of false
starts, repeated words, mid-sentence restarts, and outright transcription errors
that are not English. Reading it as if it were written prose will make you flag
good work.

Flag the item only when the cleaned passage misstates what the person meant,
drops a point they made, or attributes it to the wrong speaker.

These are never errors:
- removing filler, stutters, false starts, and repetition,
- rewording, re-punctuating, or joining fragments into a sentence,
- condensing a roundabout phrasing into the plain statement it was reaching for,
  in the speaker's own words,
- resolving a garbled or ungrammatical fragment into the reading it plainly had,
- a missing word that does not change the meaning,
- small talk or a fragment whose substance is present in the passage."""


def verify_note_payload(vault, record, item):
    raw = ""
    try:
        raw = split_frontmatter((vault / record["source"]).read_bytes())["body"]
    except OSError:
        pass
    parsed = parse_transcript(raw)
    rendered = render_turns(collapse_turns(parsed["blocks"], {label: label for label in ordered_labels(parsed["blocks"])}))
    payload = {
        "id": record["source"],
        "proposedName": Path(record["destination"]).name,
        "recordingType": record["recording_type"],
        "title": record["title"],
        "summary": record["summary"] or "(none: too short to summarize)",
        "stats": {
            "durationSeconds": (item["stats"] or {}).get("duration_seconds"),
            "words": (item["stats"] or {}).get("words"),
            "labels": len((item["stats"] or {}).get("speaker_labels") or {}),
        },
        "speakers": {key: value for key, value in (record.get("speaker_map") or {}).items() if value},
        "rawHead": rendered[:VERIFY_HEAD_CHARS],
    }
    if record.get("roster_speakers"):
        payload["rosterSpeakers"] = record["roster_speakers"]
    if len(rendered) > VERIFY_HEAD_CHARS:
        payload["rawTail"] = rendered[-VERIFY_TAIL_CHARS:]
    return payload


def fidelity_payloads(vault, record, run_dir, lexicon=None):
    """One packet item per sampled utterance, paired with the cleaned window it
    should appear in. Long files are sampled rather than re-read whole.

    Samples are corrected the same way the cleaned copy was. Comparing a
    corrected passage against the mistranscription it came from would read every
    successful correction as the cleanup drifting from the source.
    """
    entries = (lexicon or {}).get("terms", [])
    try:
        cleaned_note = (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
    except (OSError, KeyError, TypeError):
        return []
    cleaned = strip_callout_lines(cleaned_note.split("\n# Transcript\n", 1)[0])
    try:
        raw = transcript_source(split_frontmatter((vault / record["source"]).read_bytes())["body"], vault)
    except OSError:
        return []
    parsed = parse_transcript(raw)
    words = content_words(cleaned)
    items = []
    for position, block in enumerate(fidelity_samples(record["source"], parsed["blocks"]), start=1):
        utterance = vault_lexicon.apply_corrections(block["text"], entries)[0] if entries else block["text"]
        score, at = best_containment(utterance, words)
        window = " ".join(words[max(0, at - 40) : at + 120])
        items.append(
            {
                "id": f"{record['source']}#s{position}",
                "sourceUtterance": utterance,
                "cleanedPassage": window,
                "containment": round(score, 3),
            }
        )
    return items


def verify_records(args, vault, items_by_path, records, run_dir):
    """Have the thinking model review the batch, and redo what it flags.

    Bulk work runs without reasoning because it is usually right; this is what
    makes "usually" safe. Full coverage costs a handful of batched calls, and the
    reasoning budget goes to the items that turn out to need it.
    """
    warnings = []
    candidates = [record for record in records if record["action"] == "process" and not record["needs_review"]]
    summary = {"verified": 0, "ok": 0, "flagged": 0, "escalated": 0, "needsReview": 0, "flaggedIds": []}
    if not candidates:
        return summary, warnings
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    if not think["enabled"]:
        warnings.append("verification skipped: no thinking service is configured")
        summary["skipped"] = "disabled"
        return summary, warnings

    by_path = {record["source"]: record for record in candidates}
    note_items = [verify_note_payload(vault, record, items_by_path[record["source"]]) for record in candidates]
    fidelity_items = []
    for position, record in enumerate(candidates):
        # Every chunked file, plus a sample of the single-chunk ones: a file the
        # model read in one pass has already passed the deterministic locator.
        if record.get("chunks", 1) > 1 or position % FIDELITY_SAMPLE_RATE == 0:
            fidelity_items.extend(
                fidelity_payloads(vault, record, run_dir, getattr(args, "compiled_lexicon", None))
            )

    journal = run_dir / "verified.jsonl"
    fidelity_journal = run_dir / "verified-fidelity.jsonl"
    log(args, f"verifying {len(note_items)} notes and {len(fidelity_items)} utterances on {think['url']}")
    try:
        verdicts = forge_verify.verify_packets(
            think,
            VERIFY_NOTES_SYSTEM,
            note_items,
            journal_path=journal,
            background=True,
            timeout=args.request_timeout,
            progress=progress,
        )
        fidelity_verdicts = (
            forge_verify.verify_packets(
                think,
                VERIFY_FIDELITY_SYSTEM,
                fidelity_items,
                journal_path=fidelity_journal,
                background=True,
                timeout=args.request_timeout,
                progress=progress,
            )
            if fidelity_items
            else {}
        )
    except forge_verify.VerificationError as error:
        # An unreachable reviewer must not read as approval.
        warnings.append(f"verification skipped: {error}")
        summary["skipped"] = str(error)
        return summary, warnings

    flagged = [
        (next(entry for entry in note_items if entry["id"] == path), verdict["reason"])
        for path, verdict in verdicts.items()
        if verdict["verdict"] == forge_verify.VERDICT_FLAG and path in by_path
    ]

    def redo(payload, reason):
        path = payload["id"]
        record = by_path[path]
        item = items_by_path[path]
        parsed = parse_transcript(transcript_source(split_frontmatter((vault / path).read_bytes())["body"], vault))
        messages = [
            {"role": "system", "content": CLASSIFY_SYSTEM},
            {"role": "user", "content": json.dumps(classify_payload(item, parsed), ensure_ascii=False)},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "reviewerObjection": reason,
                        "previous": {"recording_type": record["recording_type"], "title": record["title"]},
                    },
                    ensure_ascii=False,
                ),
            },
        ]
        value, _call = forge_llm.call_json_with_retry(
            think,
            messages,
            temperature=0,
            cache_prompt=args.cache_prompt,
            response_format={"type": "json_object"},
            background=True,
            timeout=args.request_timeout,
            api_key=args.api_key,
            task="reclassify-transcript",
        )
        classification, _warnings = validate_classification(value, item, parsed)
        if classification["needs_review"] or not classification["title"]:
            raise UserError(classification["review_reason"] or "re-classification asked for review")
        summary_value, _record = forge_llm.call_json_with_retry(
            think,
            [
                {"role": "system", "content": SUMMARY_SYSTEM},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "recordingType": classification["recording_type"],
                            "title": classification["title"],
                            "reviewerObjection": reason,
                            # Its own predecessor's summary is not evidence for
                            # the replacement, so the callouts come out first.
                            "cleaned": strip_callout_lines(
                                (run_dir / "assembled" / record["artifact"])
                                .read_text(encoding="utf-8")
                                .split("\n# Transcript\n", 1)[0]
                            )[:SUMMARY_INPUT_CHARS],
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            cache_prompt=args.cache_prompt,
            response_format={"type": "json_object"},
            background=True,
            timeout=args.request_timeout,
            api_key=args.api_key,
            task="resummarize-transcript",
        )
        new_summary = summary_value.get("summary") if isinstance(summary_value, dict) else None
        if record["summary"] is not None:
            problems = check_summary(new_summary)
            if problems:
                raise UserError(f"re-summarizing produced an unusable summary: {problems[0]}")
        return {
            "recording_type": classification["recording_type"],
            "title": classification["title"],
            "summary": re.sub(r"\s+", " ", new_summary).strip() if record["summary"] is not None else None,
        }

    escalations = forge_verify.escalate(flagged, redo, journal_path=journal, progress=progress)
    for path, outcome in escalations.items():
        record = by_path[path]
        record["verify_reason"] = next(reason for payload, reason in flagged if payload["id"] == path)
        if outcome.get("resumed"):
            continue  # committed when it was first escalated
        if outcome["ok"]:
            record.update(outcome["value"])
            record["classification_source"] = "model-think"
            record["verified"] = "escalated"
            record["needs_reassembly"] = True
        else:
            record["verified"] = "needs-review"
            record["needs_review"] = True
            record["review_reason"] = f"verification flagged this and redoing it failed: {outcome['detail']}"
            record["status"] = "review"
            record["action"] = "none"
            record["destination"] = None
            warnings.append(f"{path}: {record['review_reason']}")

    for identifier, verdict in fidelity_verdicts.items():
        if verdict["verdict"] != forge_verify.VERDICT_FLAG:
            continue
        path = identifier.split("#s", 1)[0]
        record = by_path.get(path)
        if record is None or record["needs_review"]:
            continue
        # Cleanup fidelity is not something a title rewrite can fix, so the note
        # keeps its original name and body and a human looks at it.
        record["verified"] = "needs-review"
        record["needs_review"] = True
        record["review_reason"] = f"cleaned transcript may not be faithful: {verdict['reason']}"
        record["verify_reason"] = verdict["reason"]
        record["status"] = "review"
        record["action"] = "none"
        record["destination"] = None
        warnings.append(f"{path}: {record['review_reason']}")

    for path, verdict in verdicts.items():
        if verdict["verdict"] == forge_verify.VERDICT_OK and path in by_path and not by_path[path].get("verified"):
            by_path[path]["verified"] = "ok"
    summary = forge_verify.summarize(verdicts, escalations)
    summary["fidelityChecked"] = len(fidelity_verdicts)
    summary["fidelityFlagged"] = sum(
        1 for verdict in fidelity_verdicts.values() if verdict["verdict"] == forge_verify.VERDICT_FLAG
    )
    return summary, warnings


def reassemble_escalated(args, vault, schema, items_by_path, clean_results, records, run_dir):
    """Rebuild the notes whose type, title, or summary the reviewer replaced.

    The cleaned transcript itself is reused unchanged — the reviewer objected to
    how the recording was described, not to the cleanup — so this rebuilds from
    the same cleaned text rather than re-reading its own output.
    """
    warnings = []
    artifacts = run_dir / "assembled"
    taken = set(scan_existing_names(vault))
    for record in records:
        if not record.pop("needs_reassembly", False):
            continue
        if record["needs_review"]:
            # Something else rejected this note after its title was redone — a
            # flagged utterance, say. Rebuilding it would put a destination back
            # in the plan for a note that is being held.
            continue
        path = record["source"]
        item = items_by_path[path]
        cleaned = (clean_results.get(path) or {}).get("cleaned")
        try:
            if not cleaned:
                raise UserError("cleaned transcript is no longer available in this run")
            # A new title renames the note, and the recording's note is named
            # after it, so both halves are renamed together or the link breaks.
            destination = assign_unique_name(
                vault,
                INBOX_DIR,
                args,
                record.get("date"),
                item["time_hhmm"],
                record["recording_type"],
                record["title"],
                taken,
                path,
            )
            raw_stem = raw_note_stem(Path(destination).name) if record.get("raw_artifact") else None
            raw_destination = assign_raw_name(vault, INBOX_DIR, raw_stem, taken) if raw_stem else None
            raw_body = transcript_source(split_frontmatter((vault / path).read_bytes())["body"], vault)
            parsed = parse_transcript(raw_body)
            metadata = frontmatter_metadata(schema, record["recording_type"])
            note_text, head = build_note(
                schema,
                metadata,
                record["summary"],
                args.summary_style,
                parsed["preamble"],
                cleaned,
                raw_body,
                reflection=record.get("reflection"),
                raw_stem=raw_stem,
            )
            raw_text = (
                build_raw_note(
                    schema, raw_metadata(schema, record["recording_type"], Path(destination).stem), raw_body
                )
                if raw_stem
                else None
            )
            problems, measurements = check_note(
                {**item, "raw_body": raw_body},
                cleaned,
                record["summary"],
                note_text,
                head,
                parsed,
                args,
                record.get("proposals") or [],
                raw_text=raw_text,
                raw_stem=raw_stem,
            )
            record["measurements"] = measurements
            record["checks"] = problems
            if problems:
                raise UserError("; ".join(problems))
            (artifacts / record["artifact"]).write_text(note_text, encoding="utf-8")
            record["final_hash"] = sha256_text(note_text)
            record["destination"] = destination
            if raw_text is not None:
                (artifacts / record["raw_artifact"]).write_text(raw_text, encoding="utf-8")
                record["raw_final_hash"] = sha256_text(raw_text)
                record["raw_destination"] = raw_destination
        except (OSError, UnicodeDecodeError, UserError, ValueError) as error:
            message = f"{type(error).__name__}: {error}"
            warnings.append(f"{path}: rebuilding after verification failed ({message})")
            record["status"] = "review"
            record["needs_review"] = True
            record["review_reason"] = f"rebuilding after verification failed: {message}"
            record["action"] = "none"
            record["destination"] = None
    return warnings


# --------------------------------------------------------------------------
# Plan, report, apply
# --------------------------------------------------------------------------


def initial_counts():
    return {
        "selected": 0,
        "transcripts": 0,
        "skipped_non_transcript": 0,
        "duplicates_exact": 0,
        "duplicate_review": 0,
        "undated": 0,
        "tiny": 0,
        "processed": 0,
        "review_required": 0,
        "failed": 0,
        "applied": 0,
        "apply_failed": 0,
    }


def recompute_counts(records, dedupe, items):
    counts = initial_counts()
    counts["selected"] = len(items)
    counts["transcripts"] = sum(1 for item in items if item["is_transcript"])
    counts["duplicate_review"] = sum(len(pair["members"]) for pair in dedupe.get("review_pairs", []))
    counts["duplicates_exact"] = sum(len(group["losers"]) for group in dedupe.get("groups", []))
    for record in records:
        if record["action"] == "quarantine":
            continue
        if record["status"] == "skipped":
            counts["skipped_non_transcript"] += 1
            continue
        if record["action"] == "process":
            counts["processed"] += 1
            if not record.get("date"):
                counts["undated"] += 1
            if record.get("tiny"):
                counts["tiny"] += 1
        if record["status"] == "failed":
            counts["failed"] += 1
        elif record["needs_review"]:
            counts["review_required"] += 1
    return counts


def write_review_queue(run_dir, records):
    path = run_dir / "review-queue.jsonl"
    path.write_text("", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            if record["needs_review"] or record["status"] == "failed":
                handle.write(
                    json.dumps(
                        {
                            "source": record["source"],
                            "reason": record["review_reason"],
                            "status": record["status"],
                            "recording_type": record["recording_type"],
                            "warnings": record["warnings"],
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                )
    return path


def verification_report(verification, records):
    """The verification section of report.md.

    Says plainly when nothing was verified: an unreachable reviewer must not
    read as approval.
    """
    lines = ["## Verification", ""]
    if verification is None:
        lines.extend(["- Skipped (`--no-verify`). Nothing here was reviewed.", ""])
        return lines
    if verification.get("skipped"):
        lines.extend([f"- **Not verified**: {verification['skipped']}", ""])
        return lines
    lines.extend(
        [
            f"- Notes reviewed by the thinking model: {verification['verified']}",
            f"- Agreed: {verification['ok']}",
            f"- Flagged: {verification['flagged']}",
            f"- Re-done with reasoning: {verification['escalated']}",
            f"- Left for you to decide: {verification['needsReview']}",
        ]
    )
    if verification.get("fidelityChecked"):
        lines.append(
            f"- Utterances spot-checked against the cleaned text: {verification['fidelityChecked']}"
            f" ({verification.get('fidelityFlagged', 0)} flagged)"
        )
    lines.append("")
    flagged = [record for record in records if record.get("verify_reason")]
    if flagged:
        lines.extend(["| Note | Objection | Outcome |", "| --- | --- | --- |"])
        for record in flagged:
            outcome = "re-done with reasoning" if record.get("verified") == "escalated" else "needs your review"
            reason = str(record.get("verify_reason", "")).replace("|", "\\|")
            lines.append(f"| `{record['source']}` | {reason} | {outcome} |")
        lines.append("")
    return lines


def lexicon_report(records):
    """The lexicon section of report.md: what was corrected, who was named from
    the roster, and what is worth adding to the dictionary."""
    applied = {}
    proposed = {}
    named = []
    for record in records:
        for row in record.get("corrections") or []:
            key = (row["correct"], row["variant"])
            applied[key] = applied.get(key, 0) + row["count"]
        for row in record.get("proposals") or []:
            key = (row["correct"], row["variant"])
            proposed[key] = proposed.get(key, 0) + 1
        for name in record.get("roster_speakers") or []:
            named.append((record["source"], name))
    if not applied and not proposed and not named:
        return []
    lines = ["## Lexicon", ""]
    if applied:
        lines.extend([f"- Corrected in code: {sum(applied.values())} across {len(applied)} spellings"])
        for (correct, variant), count in sorted(applied.items(), key=lambda item: (-item[1], item[0][0].lower()))[:20]:
            lines.append(f"  - `{variant}` → `{correct}` ×{count}")
    if named:
        lines.append(f"- Speakers named from the roster: {len(named)}")
        for source, name in named[:20]:
            lines.append(f"  - `{Path(source).name}`: {name}")
    if proposed:
        lines.extend(
            [
                "",
                "These spellings the model fixed are not in the dictionary yet. Adding one",
                "makes it a free code-level correction on every future run:",
                "",
            ]
        )
        for (correct, variant), count in sorted(proposed.items(), key=lambda item: (-item[1], item[0][0].lower()))[:20]:
            lines.append(f"- `{variant}` → `{correct}` (seen {count}×)")
    lines.append("")
    return lines


def append_listing(report, entries, formatter, limit=60):
    for entry in entries[:limit]:
        report.append(formatter(entry))
    if len(entries) > limit:
        report.append(f"- … and {len(entries) - limit} more")
    if not entries:
        report.append("- None")


def plan_for_json(records):
    cleaned = []
    for record in records:
        item = dict(record)
        item.pop("raw_body", None)
        cleaned.append(item)
    return cleaned


def write_plan(run_dir, records, counts, dedupe, dry_run, vault, schema_hash, options, warnings, verification=None):
    plan_path = run_dir / "plan.json"
    report_path = run_dir / "report.md"
    run_state.atomic_write_json(
        plan_path,
        {
            "dry_run": dry_run,
            "vault": str(vault),
            "schema_hash": schema_hash,
            "run_directory": str(run_dir),
            "options": options,
            "counts": counts,
            "dedupe": dedupe,
            "verification": verification,
            "records": plan_for_json(records),
            "warnings": warnings,
        },
    )
    processed = [record for record in records if record["action"] == "process"]
    review = [record for record in records if record["needs_review"] or record["status"] == "failed"]
    report = [
        "# Vault Transcripts Report",
        "",
        f"- Dry run: `{str(dry_run).lower()}`",
        f"- Vault: `{vault}`",
        f"- Filename pattern: `{options['filename_pattern']}`",
        f"- Summary style: `{options['summary_style']}`",
        f"- Speaker policy: `{options['speaker_policy']}`",
        f"- Inbox notes seen: {counts['selected']}",
        f"- Transcripts found: {counts['transcripts']}",
        f"- Notes to process: {counts['processed']}",
        f"- Held for review: {counts['review_required']}",
        f"- Failed: {counts['failed']}",
        f"- Exact duplicates to quarantine: {counts['duplicates_exact']}",
        f"- Duplicate pairs needing your decision: {counts['duplicate_review']}",
        f"- Not transcripts (left alone): {counts['skipped_non_transcript']}",
        f"- Without a date in the filename: {counts['undated']}",
        "",
    ]
    report.extend(verification_report(verification, records))
    report.extend(lexicon_report(records))
    # One block per note rather than a table: the summary is the thing worth
    # reading before approving, and a table cell would truncate it.
    report.extend(["## Notes To Process", ""])
    if processed:
        for record in processed:
            stats = record.get("stats") or {}
            speakers = len(stats.get("speaker_labels") or {})
            facts = [
                record["recording_type"],
                format_clock(stats.get("duration_seconds")),
                f"{stats.get('words', 0)} words",
            ]
            if speakers:
                facts.append(f"{speakers} labelled speaker{'s' if speakers != 1 else ''}")
                mapped = sorted({value for value in (record.get("speaker_map") or {}).values() if value})
                if mapped:
                    facts.append("as " + ", ".join(mapped))
            report.extend(
                [
                    f"### {Path(record['destination']).name}",
                    "",
                    f"- Was `{Path(record['source']).name}` · {' · '.join(facts)}",
                    f"- Voice policy: `{record.get('voice_context_mode', 'none')}` — {record.get('voice_reason', 'not applicable')}",
                    f"- {record.get('summary') or '_No summary: short enough that the title says it._'}",
                    "",
                ]
            )
            if record.get("raw_destination"):
                report.insert(
                    len(report) - 1,
                    f"- Recording kept as `{Path(record['raw_destination']).name}`, linked from the note",
                )
    else:
        report.extend(["- None", ""])
    report.extend(["## Renames", ""])
    append_listing(report, processed, lambda record: f"- `{record['source']}` → `{record['destination']}`")
    report.extend(["", "## Held For Review", "", "These notes were not renamed or rewritten.", ""])
    append_listing(
        report,
        review,
        lambda record: f"- `{record['source']}`: {record['review_reason']} "
        f"(voice `{record.get('voice_context_mode', 'none')}`: {record.get('voice_reason', 'not applicable')})",
    )
    report.extend(["", "## Duplicates", "", f"Quarantined into `{dedupe.get('quarantine_root')}` and recoverable.", ""])
    append_listing(
        report,
        dedupe.get("groups", []),
        lambda group: f"- keep `{group['winner']}` ← quarantine "
        + ", ".join(f"`{loser['path']}`" for loser in group["losers"]),
    )
    report.extend(["", "## Same Recording, Different Content", "", "Left untouched for you to resolve.", ""])
    append_listing(
        report,
        dedupe.get("review_pairs", []),
        lambda pair: f"- {pair['reason']}: "
        + ", ".join(
            f"`{member['path']}` ({member['blocks']} blocks, {member['words']} words"
            + (", handwritten notes above the transcript" if member["has_preamble"] else "")
            + ")"
            for member in pair["members"]
        ),
    )
    report.extend(["", "## Warnings", ""])
    entries = list(warnings)
    for record in records:
        for warning in record["warnings"]:
            entries.append(f"{record['source']}: {warning}")
    append_listing(report, entries, lambda entry: f"- {entry}")
    run_state.atomic_write_text(report_path, "\n".join(report) + "\n")
    return plan_path, report_path


def apply_quarantine(vault, run_dir, record):
    source = vault / record["source"]
    destination = vault / record["destination"]
    data = source.read_bytes()
    if sha256_bytes(data) != record["source_hash"]:
        raise UserError("source changed since planning")
    if destination.exists():
        raise UserError("quarantine destination collision")
    backup = run_dir / "backup" / record["source"]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(source, destination)
    return backup


def write_atomic(path, text):
    """Write ``text`` to ``path`` through a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_id, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle_id, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def apply_process(vault, run_dir, record):
    """Rename and rewrite one note, atomically and recoverably.

    The original is copied to the run's backup directory before anything moves,
    the new content lands through a temporary file in the same directory, and the
    old name is only unlinked once the new one exists.

    The recording's own note is written first, so an interruption between the two
    writes leaves the recording duplicated rather than lost: the original note
    still holds it, and resuming finds the recording note already there with the
    planned bytes and finishes the rewrite.
    """
    source = vault / record["source"]
    destination = vault / record["destination"]
    data = source.read_bytes()
    if sha256_bytes(data) != record["source_hash"]:
        raise UserError("source changed since planning")
    note_text = (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
    if sha256_text(note_text) != record["final_hash"]:
        raise UserError("assembled note changed since planning")
    if destination.exists() and destination.resolve() != source.resolve():
        raise UserError("destination collision")
    raw_text = None
    raw_destination = None
    if record.get("raw_artifact"):
        raw_text = (run_dir / "assembled" / record["raw_artifact"]).read_text(encoding="utf-8")
        if sha256_text(raw_text) != record["raw_final_hash"]:
            raise UserError("raw transcript note changed since planning")
        raw_destination = vault / record["raw_destination"]
        if raw_destination.exists():
            existing = raw_destination.read_text(encoding="utf-8")
            if sha256_text(existing) != record["raw_final_hash"]:
                raise UserError("raw transcript destination collision")
            raw_text = None  # already written by an interrupted run
    backup = run_dir / "backup" / record["source"]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    if raw_text is not None:
        write_atomic(raw_destination, raw_text)
    write_atomic(destination, note_text)
    if destination.resolve() != source.resolve() and source.exists():
        source.unlink()
    return backup


def apply_records(vault, run_dir, records, counts):
    log_path = run_dir / "apply-log.jsonl"
    renames_path = run_dir / "renames.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
    done = {(entry.get("op"), entry.get("source")) for entry in prior if entry.get("status") == "ok"}
    order = {"quarantine": 0, "process": 1}
    actionable = sorted(
        (record for record in records if record["action"] in order and record["status"] not in {"failed", "review"}),
        key=lambda record: (order[record["action"]], record["source"]),
    )
    for record in actionable:
        op = record["action"]
        if (op, record["source"]) in done:
            counts["applied"] += 1
            continue
        try:
            backup = apply_quarantine(vault, run_dir, record) if op == "quarantine" else apply_process(vault, run_dir, record)
            counts["applied"] += 1
            entry = {
                "op": op,
                "status": "ok",
                "source": record["source"],
                "destination": record["destination"],
                "backup": str(backup),
            }
            if op == "process":
                run_state.append_jsonl_fsync(
                    renames_path,
                    {"at": run_state.utc_now(), "old": record["source"], "new": record["destination"]},
                )
        except Exception as error:  # noqa: BLE001 - every failure is reported, never silent
            counts["apply_failed"] += 1
            record["warnings"].append(f"apply failed: {error}")
            entry = {
                "op": op,
                "status": "error",
                "source": record["source"],
                "destination": record["destination"],
                "error": str(error),
            }
        run_state.append_jsonl_fsync(log_path, entry)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def chat_service(args):
    return {
        "name": "chat",
        "enabled": True,
        "url": args.base_url,
        "model": args.model,
        "scheduling": forge_llm.DEFAULT_SERVICES["chat"]["scheduling"],
    }


def dictionary_path(args):
    """The standalone transcription dictionary, merged under the vault note.

    That skill corrects a different engine's output than this inbox carries, so
    the two variant lists barely overlap; a variant this engine never produces
    costs nothing, which makes merging strictly more coverage.
    """
    if args.no_lexicon:
        return None
    path = vault_lexicon.default_dictionary_path()
    return path if path.is_file() else None


def resolved_options(args):
    return {
        "model": args.model,
        "base_url": args.base_url,
        "filename_pattern": args.filename_pattern,
        "summary_style": args.summary_style,
        "speaker_policy": args.speaker_policy,
        "owner": args.owner,
        "tiny_words": args.tiny_words,
        "tiny_summary": args.tiny_summary,
        "limit": args.limit,
        "prompt_version": PROMPT_VERSION,
        "cache_prompt": args.cache_prompt,
        "schema": args.schema,
        "voice": args.voice,
        "no_voice": args.no_voice,
        "lexicon": args.lexicon,
        "no_lexicon": args.no_lexicon,
        "profile": args.profile,
        "no_profile": args.no_profile,
    }


RESUMABLE_OPTION_FLAGS = {
    "model": "--model",
    "base_url": "--base-url",
    "filename_pattern": "--filename-pattern",
    "summary_style": "--summary-style",
    "speaker_policy": "--speaker-policy",
    "owner": "--owner",
    "tiny_words": "--tiny-words",
    "tiny_summary": "--tiny-summary",
    "limit": "--limit",
    "schema": "--schema",
    "voice": "--voice",
    "no_voice": "--no-voice",
    "lexicon": "--lexicon",
    "no_lexicon": "--no-lexicon",
    "profile": "--profile",
    "no_profile": "--no-profile",
}


def adopt_stored_options(args, state):
    """Resuming keeps the original run's options. Changing how notes are named
    or cleaned halfway through a run would produce a batch that disagrees with
    itself, so it is refused rather than merged."""
    stored = state.get("options", {})
    for key, flag in RESUMABLE_OPTION_FLAGS.items():
        if getattr(args, f"{key}_provided", False) and getattr(args, key) != stored.get(key):
            raise UserError(
                f"{flag} differs from the original run ({getattr(args, key)!r} vs {stored.get(key)!r}); "
                "start a new run instead of --run"
            )
        if key in stored:
            setattr(args, key, stored[key])
    if not args.cache_prompt and stored.get("cache_prompt"):
        raise UserError("--no-cache-prompt differs from the original run; start a new run instead of --run")
    args.cache_prompt = stored.get("cache_prompt", args.cache_prompt)


def run_configuration(args, vault, schema_hash, voice_path, voice_hash, lexicon_path, lexicon_hash,
                      profile_path=None, profile_hash=None, command="process"):
    return {
        "workflow": WORKFLOW,
        "command": command,
        "input": {
            "vault": str(vault),
            "schema_hash": schema_hash,
            **vault_voice.voice_state(voice_path, voice_hash, "per-transcript"),
            **vault_lexicon.lexicon_state(lexicon_path, lexicon_hash, dictionary_path(args)),
            **vault_profile.profile_state(profile_path, profile_hash, None),
        },
        "options": resolved_options(args),
    }


def phase(run_dir, name, event=None):
    run_state.update_run_state(run_dir, lambda draft: draft.update({"phase": name}) or draft, event=event)


def process(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    resuming = bool(args.run)
    state = None
    if resuming:
        run_dir = Path(args.run).expanduser().resolve()
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        adopt_stored_options(args, state)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
    voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path, cache_dir=vault / STATE_DIR / "cache")
    args.compiled_voice = voice
    lexicon_path = vault_lexicon.resolve_lexicon_path(vault, args.lexicon, disabled=args.no_lexicon)
    lexicon, lexicon_hash = vault_lexicon.load_lexicon(
        vault,
        lexicon_path,
        schema=schema,
        cache_dir=vault / STATE_DIR / "cache",
        dictionary_path=dictionary_path(args),
    )
    args.compiled_lexicon = lexicon
    profile_path, resolve_warnings = vault_profile.resolve_profile_or_warn(vault, args.profile, disabled=args.no_profile)
    profile, profile_hash, compile_warnings = vault_profile.compiled_profile_for(
        vault, profile_path, cache_dir=vault / STATE_DIR / "cache"
    )
    profile_warnings = resolve_warnings + compile_warnings
    args.compiled_profile = profile
    configuration = run_configuration(
        args, vault, schema_hash, voice_path, voice_hash, lexicon_path, lexicon_hash, profile_path, profile_hash
    )
    if resuming:
        try:
            run_state.assert_compatible_run(state, configuration)
        except ValueError as error:
            raise UserError(str(error)) from error
    warnings = list(profile_warnings)
    with run_state.run_lock(vault / STATE_DIR):
        if not resuming:
            run_dir = unique_run_directory(vault)
            run_state.initialize_run_state(
                run_dir,
                run_state.create_run_state(
                    WORKFLOW, "process", configuration["input"], configuration["options"], phase="scan"
                ),
            )

        scan_path = run_dir / "scan.json"
        if scan_path.is_file():
            items = json.loads(scan_path.read_text(encoding="utf-8"))["items"]
            if resuming:
                drift = run_state.input_drift(items, scan_inbox(vault, args.limit))
                for added in drift["added"]:
                    warnings.append(f"input drift: {added['path']} appeared after the scan; run again to include it")
                for removed in drift["removed"]:
                    warnings.append(f"input drift: {removed['path']} disappeared after the scan")
                for changed in drift["changed"]:
                    warnings.append(
                        f"input drift: {changed['after']['path']} changed after the scan; it will be refused at apply"
                    )
        else:
            items = scan_inbox(vault, args.limit)
            run_state.atomic_write_json(scan_path, {"items": items})
            run_state.update_run_state(
                run_dir,
                lambda draft: draft.update(
                    {
                        "phase": "dedupe",
                        "items": [
                            {"id": item["path"], "status": "pending" if item["is_transcript"] else "skipped"}
                            for item in items
                        ],
                    }
                )
                or draft,
                event={
                    "type": "phase",
                    "phase": "scan",
                    "selected": len(items),
                    "transcripts": sum(1 for item in items if item["is_transcript"]),
                },
            )
        log(args, f"selected {len(items)} inbox notes, {sum(1 for item in items if item['is_transcript'])} transcripts")

        dedupe_path = run_dir / "dedupe.json"
        if dedupe_path.is_file():
            dedupe = json.loads(dedupe_path.read_text(encoding="utf-8"))
            losers = {
                loser["path"]: {
                    "winner": group["winner"],
                    "kind": group["kind"],
                    "quarantine_to": loser["quarantine_to"],
                    "sha256": loser["sha256"],
                }
                for group in dedupe.get("groups", [])
                for loser in group["losers"]
            }
            held = {member["path"] for pair in dedupe.get("review_pairs", []) for member in pair["members"]}
        else:
            dedupe, losers, held = plan_dedupe(vault, items)
            run_state.atomic_write_json(dedupe_path, dedupe)
            phase(
                run_dir,
                "classify",
                event={
                    "type": "phase",
                    "phase": "dedupe",
                    "groups": len(dedupe["groups"]),
                    "losers": len(losers),
                    "review_pairs": len(dedupe["review_pairs"]),
                },
            )

        applied_log, _ = run_state.read_jsonl_recover_tail(run_dir / "apply-log.jsonl", repair=True)
        applied = {
            entry["source"]: entry
            for entry in applied_log
            if entry.get("status") == "ok" and entry.get("source") and entry.get("destination")
        }
        skip = set(losers) | held | set(applied)
        # All chat work first, one stage at a time: each stage has its own stable
        # system prompt, and interleaving them would invalidate the prefix cache
        # this split exists to keep warm.
        class_records, stage_warnings = classify_items(args, vault, items, run_dir, skip)
        warnings.extend(stage_warnings)
        phase(run_dir, "clean", event={"type": "phase", "phase": "classify", "records": len(class_records)})

        clean_results, stage_warnings = clean_items(args, vault, items, class_records, run_dir, skip)
        warnings.extend(stage_warnings)
        phase(run_dir, "summarize", event={"type": "phase", "phase": "clean", "notes": len(clean_results)})

        summaries, stage_warnings = summarize_items(args, vault, items, class_records, clean_results, run_dir, skip)
        warnings.extend(stage_warnings)
        phase(run_dir, "assemble", event={"type": "phase", "phase": "summarize", "notes": len(summaries)})

        records, stage_warnings = assemble_items(
            args, vault, schema, items, class_records, clean_results, summaries, losers, held, applied, run_dir
        )
        warnings.extend(stage_warnings)
        phase(run_dir, "verify", event={"type": "phase", "phase": "assemble", "records": len(records)})

        items_by_path = {item["path"]: item for item in items}
        verification = None
        if args.verify:
            verification, stage_warnings = verify_records(args, vault, items_by_path, records, run_dir)
            warnings.extend(stage_warnings)
            warnings.extend(
                reassemble_escalated(args, vault, schema, items_by_path, clean_results, records, run_dir)
            )
        phase(
            run_dir,
            "plan",
            event={"type": "phase", "phase": "verify", **(verification or {"skipped": "disabled by --no-verify"})},
        )

        counts = recompute_counts(records, dedupe, items)
        counts["applied"] = sum(1 for record in records if record.get("already_applied") and not record["needs_review"])
        write_review_queue(run_dir, records)
        if args.apply:
            apply_records(vault, run_dir, records, counts)
            final_phase = "complete"
        else:
            final_phase = "planned"
        plan_path, report_path = write_plan(
            run_dir,
            records,
            counts,
            dedupe,
            not args.apply,
            vault,
            schema_hash,
            {
                **resolved_options(args),
                **vault_voice.voice_state(voice_path, voice_hash, "per-transcript"),
            },
            warnings,
            verification,
        )
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update(
                {
                    "phase": final_phase,
                    "status": "complete" if final_phase == "complete" else "running",
                    "nextAction": None
                    if final_phase == "complete"
                    else f"review {report_path.name}, then rerun with --apply --run {run_dir}",
                }
            )
            or draft,
            event={"type": "phase", "phase": final_phase, "counts": counts},
        )
    return structured(
        "ok",
        artifacts=[str(plan_path), str(report_path)],
        warnings=warnings,
        data={
            "dry_run": not args.apply,
            "vault": str(vault),
            "run_directory": str(run_dir),
            "options": resolved_options(args),
            "counts": counts,
            "verification": verification,
        },
    )


# --------------------------------------------------------------------------
# Split: separating recordings out of notes already processed
# --------------------------------------------------------------------------


def plan_split(vault, schema, path, taken_casefold):
    """Plan one note's split, or return None when there is nothing to split.

    Entirely deterministic: the recording is the bytes after the marker and the
    note keeps everything before it, so the two halves put back together are the
    note that was there. Nothing can be lost between them because of how the two
    slices are taken -- they are the body split at one index -- so the guarantee
    is in the construction rather than in a check. No model reads either half:
    what the recording is about was already decided when the note was first
    processed, and re-deciding it now would be a second opinion nobody asked for.
    """
    data = path.read_bytes()
    split = split_frontmatter(data)
    if split["malformed"] or not split["had_frontmatter"]:
        return None
    match = TRANSCRIPT_MARKER_RE.search(split["body"])
    if not match:
        return None
    head = split["body"][: match.end()]
    raw_body = split["body"][match.end():]
    if transcript_link_target(raw_body) is not None:
        return None  # already split
    if not raw_body.strip():
        return {"path": path, "skip": "the transcript section is empty"}

    previous = parse_frontmatter(split["frontmatter_text"])
    metadata = {key: value for key, value in previous.items() if key in schema["properties"]}
    # A note that is mostly a summary of a recording is a note about a source,
    # not the source; the recording it points at is the source now. The schema
    # forbids source_kind anywhere but a source, so it goes with the type.
    converted = metadata.get("type") == "source"
    if converted:
        metadata["type"] = "note" if "note" in schema["types"] else metadata["type"]
        if metadata["type"] != "source":
            metadata.pop("source_kind", None)

    stem = path.stem
    raw_stem = raw_note_stem(path.name)
    raw = raw_metadata(schema, "other", stem)
    raw["capture_type"] = previous.get("capture_type") if "capture_type" in schema["properties"] else None
    if raw["capture_type"] not in schema["capture_types"]:
        raw.pop("capture_type", None)
    domain = previous.get("domain")
    if domain not in schema["domains"]:
        return {"path": path, "skip": f"domain {domain!r} is not in the schema; file the note first"}
    raw["domain"] = domain
    subdomain = previous.get("subdomain")
    warnings = []
    if subdomain and subdomain in schema["subdomains"].get(domain, {}):
        raw["subdomain"] = subdomain
    elif subdomain:
        warnings.append(f"subdomain {subdomain!r} is not in the schema; the recording is filed at the domain")
    raw = {key: value for key, value in raw.items() if key in schema["properties"] and value}

    directory = compile_destination(schema, raw).as_posix()
    raw_destination = assign_raw_name(vault, directory, raw_stem, taken_casefold)
    processed_text = serialize_frontmatter(metadata, schema) + head + f"[[{Path(raw_destination).stem}]]\n"
    raw_text = build_raw_note(schema, raw, raw_body)
    return {
        "path": path,
        "source": relative_path(vault, path),
        "source_hash": sha256_bytes(data),
        "destination": relative_path(vault, path),
        "raw_destination": raw_destination,
        "processed_text": processed_text,
        "raw_text": raw_text,
        "final_hash": sha256_text(processed_text),
        "raw_final_hash": sha256_text(raw_text),
        "converted_from_source": converted,
        "warnings": warnings,
    }


def apply_split(vault, run_dir, record):
    """Write the recording's note, then rewrite the note it came out of.

    In that order, so an interruption between the two leaves the original note
    intact and holding the recording; resuming finds the recording already
    written with the planned bytes and finishes.
    """
    source = vault / record["source"]
    data = source.read_bytes()
    if sha256_bytes(data) != record["source_hash"]:
        raise UserError("source changed since planning")
    backup = run_dir / "backup" / record["source"]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    raw_destination = vault / record["raw_destination"]
    if raw_destination.exists():
        # An interrupted run already wrote it; anything else with that name is
        # somebody's note and is not ours to overwrite.
        if sha256_text(raw_destination.read_text(encoding="utf-8")) != record["raw_final_hash"]:
            raise UserError("raw transcript destination collision")
    else:
        write_atomic(raw_destination, record["raw_text"])
    write_atomic(source, record["processed_text"])
    return backup


def assemble_reprocessed(args, vault, schema, items, class_records, clean_results, summaries, run_dir):
    """Rebuild the generated head of each filed note, keeping everything else.

    Three things are reattached rather than regenerated, and each for the same
    reason -- the pipeline is not the authority on them. The frontmatter is the
    organizer's classification, byte for byte. The recording section is the
    record. And the name is what every wikilink in the vault points at, so a
    better title the classifier produced on this pass is discarded.
    """
    artifacts = run_dir / "assembled"
    artifacts.mkdir(exist_ok=True)
    warnings = []
    records = []
    for item in items:
        rel = item["path"]
        record = class_records.get(rel)
        cleaned_result = clean_results.get(rel)
        if not record:
            records.append(review_record(item, "no classification record"))
            continue
        if record["needs_review"] or record["source"] == "failed":
            records.append(review_record(item, record["review_reason"] or "classification asked for review"))
            continue
        if cleaned_result is None or cleaned_result.get("error") or not cleaned_result.get("cleaned"):
            reason = (cleaned_result or {}).get("error") or "cleanup produced nothing"
            records.append(review_record(item, f"cleanup failed: {reason}"))
            continue
        summary_row = summaries.get(rel) or {}
        summary = summary_row.get("summary")
        if summary is None and summary_row.get("skipped") and summary_row["skipped"] != "tiny":
            records.append(review_record(item, f"summary failed: {summary_row['skipped']}"))
            continue
        try:
            data = (vault / rel).read_bytes()
            if sha256_bytes(data) != item["sha256"]:
                records.append(review_record(item, "note changed on disk during this run"))
                continue
            raw_body = transcript_source(split_frontmatter(data)["body"], vault)
            parsed = parse_transcript(raw_body)
            head = assemble_head(
                summary,
                args.summary_style,
                parsed["preamble"],
                cleaned_result["cleaned"],
                summary_row.get("reflection"),
            )
            # The blank line after the frontmatter is part of the body, so it is
            # re-added here exactly as `build_note` does. The head's own marker
            # comes off by removing the suffix it is known to end with -- never
            # by searching for the words, which would cut at the first time the
            # speaker happened to say them -- and the captured tail supplies it.
            assert head.endswith(TRANSCRIPT_MARKER)
            note_text = (
                item["frontmatter_prefix"] + "\n" + head[: -len(TRANSCRIPT_MARKER)] + "\n\n" + item["tail"]
            )
            problems, measurements = check_note(
                {**item, "raw_body": raw_body},
                cleaned_result["cleaned"],
                summary,
                note_text,
                head,
                parsed,
                args,
                cleaned_result.get("proposals") or [],
                tail=item["tail"],
            )
        except (OSError, UnicodeDecodeError, UserError, ValueError) as error:
            message = f"{type(error).__name__}: {error}"
            warnings.append(f"{rel}: reassembly failed ({message})")
            records.append(review_record(item, f"reassembly failed: {message}", status="failed", warning=message))
            continue

        assembled = base_record(item)
        assembled["recording_type"] = record["recording_type"]
        assembled["title"] = Path(rel).stem
        assembled["summary"] = summary
        assembled["previous_summary"] = item.get("previous_summary")
        assembled["reflection"] = summary_row.get("reflection")
        assembled["material_role"] = record.get("material_role", "unknown")
        assembled["voice_context_mode"] = voice_context_for(record)
        assembled["speaker_map"] = cleaned_result["speaker_map"]
        assembled["corrections"] = cleaned_result.get("corrections") or []
        assembled["proposals"] = cleaned_result.get("proposals") or []
        assembled["chunks"] = cleaned_result["chunks"]
        assembled["tiny"] = cleaned_result["tiny"]
        assembled["measurements"] = measurements
        assembled["checks"] = problems
        assembled["classification_source"] = record["source"]
        assembled["warnings"] = list(record.get("warnings") or [])
        if problems:
            assembled["status"] = "review"
            assembled["needs_review"] = True
            assembled["review_reason"] = "; ".join(problems)
            assembled["warnings"].extend(problems)
            records.append(assembled)
            continue
        name = f"{sha256_text(rel)[:12]}.md"
        (artifacts / name).write_text(note_text, encoding="utf-8")
        assembled["artifact"] = name
        assembled["final_hash"] = sha256_text(note_text)
        assembled["destination"] = rel
        assembled["action"] = "reprocess"
        records.append(assembled)
    return records, warnings


def apply_reprocess(vault, run_dir, record):
    """Rewrite one filed note in place, atomically and recoverably."""
    source = vault / record["source"]
    data = source.read_bytes()
    if sha256_bytes(data) != record["source_hash"]:
        raise UserError("source changed since planning")
    note_text = (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
    if sha256_text(note_text) != record["final_hash"]:
        raise UserError("assembled note changed since planning")
    backup = run_dir / "backup" / record["source"]
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    write_atomic(source, note_text)
    return backup


def reprocess_notes(args):
    """Regenerate the cleaned text and summary of every filed transcript note.

    The recording never changed; what the pipeline made of it did. This runs the
    same classify/clean/summarize stages over notes that already exist, and
    rebuilds only the generated head -- so a note keeps its name, its place, and
    the classification the organizer gave it.
    """
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    args.reprocessing = True
    resuming = bool(args.run)
    state = None
    if resuming:
        run_dir = Path(args.run).expanduser().resolve()
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        adopt_stored_options(args, state)
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
    voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path, cache_dir=vault / STATE_DIR / "cache")
    args.compiled_voice = voice
    lexicon_path = vault_lexicon.resolve_lexicon_path(vault, args.lexicon, disabled=args.no_lexicon)
    lexicon, lexicon_hash = vault_lexicon.load_lexicon(
        vault, lexicon_path, schema=schema, cache_dir=vault / STATE_DIR / "cache", dictionary_path=dictionary_path(args)
    )
    args.compiled_lexicon = lexicon
    profile_path, resolve_warnings = vault_profile.resolve_profile_or_warn(vault, args.profile, disabled=args.no_profile)
    profile, profile_hash, compile_warnings = vault_profile.compiled_profile_for(
        vault, profile_path, cache_dir=vault / STATE_DIR / "cache"
    )
    args.compiled_profile = profile
    configuration = run_configuration(
        args, vault, schema_hash, voice_path, voice_hash, lexicon_path, lexicon_hash, profile_path, profile_hash,
        command="reprocess",
    )
    if resuming:
        try:
            run_state.assert_compatible_run(state, configuration)
        except ValueError as error:
            raise UserError(str(error)) from error
    warnings = list(resolve_warnings + compile_warnings)
    with run_state.run_lock(vault / STATE_DIR):
        if not resuming:
            run_dir = unique_run_directory(vault)
            run_state.initialize_run_state(
                run_dir,
                run_state.create_run_state(
                    WORKFLOW, "reprocess", configuration["input"], configuration["options"], phase="scan"
                ),
            )
        scan_path = run_dir / "scan.json"
        if scan_path.is_file():
            items = json.loads(scan_path.read_text(encoding="utf-8"))["items"]
        else:
            items = scan_processed(vault, schema_path, args.limit)
            run_state.atomic_write_json(scan_path, {"items": items})
        selected = [item for item in items if item["is_transcript"]]
        skipped = [item for item in items if not item["is_transcript"]]
        for item in skipped:
            warnings.append(f"{item['path']}: skipped — {item['skip_reason']}")
        log(args, f"selected {len(selected)} filed transcript notes to reprocess, {len(skipped)} skipped")
        phase(run_dir, "classify", event={"type": "phase", "phase": "scan", "selected": len(selected)})

        applied_log, _ = run_state.read_jsonl_recover_tail(run_dir / "apply-log.jsonl", repair=True)
        applied = {entry["source"] for entry in applied_log if entry.get("status") == "ok"}

        class_records, stage_warnings = classify_items(args, vault, selected, run_dir, set())
        warnings.extend(stage_warnings)
        # The name has been reviewed by a person and is what the rest of the
        # vault links to; a fresh classification does not get to overrule it. A
        # therapy reading is the exception: it is a stop, never an override.
        held = set()
        for item in selected:
            record = class_records.get(item["path"])
            if not record or not item["label_type"]:
                continue
            if record.get("recording_type") == "therapy" and item["label_type"] != "therapy":
                warnings.append(f"{item['path']}: classified as therapy but named {item['label_type']}; held")
                held.add(item["path"])
            elif record.get("recording_type") != item["label_type"]:
                warnings.append(
                    f"{item['path']}: classified as {record.get('recording_type')} but named "
                    f"{item['label_type']}; kept the name's reading"
                )
                record["recording_type"] = item["label_type"]
        selected = [item for item in selected if item["path"] not in held]
        phase(run_dir, "clean", event={"type": "phase", "phase": "classify", "notes": len(class_records)})

        clean_results, stage_warnings = clean_items(args, vault, selected, class_records, run_dir, set())
        warnings.extend(stage_warnings)
        phase(run_dir, "summarize", event={"type": "phase", "phase": "clean", "notes": len(clean_results)})

        summaries, stage_warnings = summarize_items(args, vault, selected, class_records, clean_results, run_dir, set())
        warnings.extend(stage_warnings)
        phase(run_dir, "assemble", event={"type": "phase", "phase": "summarize", "notes": len(summaries)})

        for item in selected:
            item["previous_summary"] = previous_summary(vault, item["path"])
        records, stage_warnings = assemble_reprocessed(
            args, vault, schema, selected, class_records, clean_results, summaries, run_dir
        )
        warnings.extend(stage_warnings)
        for path in sorted(held):
            item = next(entry for entry in items if entry["path"] == path)
            records.append(review_record(item, "classified as therapy but not named as one; left untouched"))
        phase(run_dir, "verify", event={"type": "phase", "phase": "assemble", "records": len(records)})

        items_by_path = {item["path"]: item for item in selected}
        verification = None
        if args.verify:
            verification, stage_warnings = verify_records(args, vault, items_by_path, records, run_dir)
            warnings.extend(stage_warnings)
        phase(
            run_dir,
            "plan",
            event={"type": "phase", "phase": "verify", **(verification or {"skipped": "disabled by --no-verify"})},
        )

        counts = {
            "selected": len(items),
            "reprocessed": sum(1 for record in records if record["action"] == "reprocess"),
            "skipped": len(skipped),
            "review_required": sum(1 for record in records if record["needs_review"]),
            "failed": sum(1 for record in records if record["status"] == "failed"),
            "applied": 0,
        }
        write_review_queue(run_dir, records)
        if args.apply:
            log_path = run_dir / "apply-log.jsonl"
            for record in records:
                if record["action"] != "reprocess" or record["status"] in {"failed", "review"}:
                    continue
                if record["source"] in applied:
                    counts["applied"] += 1
                    continue
                try:
                    backup = apply_reprocess(vault, run_dir, record)
                    counts["applied"] += 1
                    entry = {
                        "op": "reprocess",
                        "status": "ok",
                        "source": record["source"],
                        "backup": relative_path(run_dir, backup),
                    }
                except (OSError, UserError) as error:
                    entry = {"op": "reprocess", "status": "failed", "source": record["source"], "error": str(error)}
                    warnings.append(f"{record['source']}: reprocessing failed ({error})")
                run_state.append_jsonl_fsync(log_path, entry)

        plan_path, report_path = write_reprocess_plan(
            run_dir, records, counts, skipped, not args.apply, vault, schema_hash,
            resolved_options(args), warnings, verification,
        )
        final_phase = "complete" if args.apply else "planned"
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update(
                {
                    "phase": final_phase,
                    "status": "complete" if final_phase == "complete" else "running",
                    "nextAction": None
                    if final_phase == "complete"
                    else f"review {report_path.name}, then rerun with --apply --run {run_dir}",
                }
            )
            or draft,
            event={"type": "phase", "phase": final_phase, "counts": counts},
        )
    return structured(
        "ok",
        artifacts=[str(plan_path), str(report_path)],
        warnings=warnings,
        data={
            "dry_run": not args.apply,
            "vault": str(vault),
            "run_directory": str(run_dir),
            "options": resolved_options(args),
            "counts": counts,
            "verification": verification,
        },
    )


def previous_summary(vault, rel):
    """The summary the note carries now, so the report can show it beside the new one."""
    try:
        body = split_frontmatter((vault / rel).read_bytes())["body"]
    except (OSError, ValueError):
        return None
    match = re.search(r"^>\s*\[!summary\][-+]?\s*\n((?:>.*\n?)*)", body, re.MULTILINE)
    if match:
        return re.sub(r"^>\s?", "", match.group(1), flags=re.MULTILINE).strip() or None
    match = re.search(r"^##\s+Summary\s*\n+(.+?)(?:\n\n|\Z)", body, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else None


def write_reprocess_plan(run_dir, records, counts, skipped, dry_run, vault, schema_hash, options, warnings, verification):
    plan_path = run_dir / "reprocess-plan.json"
    report_path = run_dir / "reprocess-report.md"
    run_state.atomic_write_json(
        plan_path,
        {
            "dry_run": dry_run,
            "vault": str(vault),
            "schema_hash": schema_hash,
            "run_directory": str(run_dir),
            "options": options,
            "counts": counts,
            "verification": verification,
            "records": plan_for_json(records),
            "skipped": [{"path": item["path"], "reason": item["skip_reason"]} for item in skipped],
            "warnings": warnings,
        },
    )
    done = [record for record in records if record["action"] == "reprocess"]
    review = [record for record in records if record["needs_review"] or record["status"] == "failed"]
    report = [
        "# Vault Transcripts Reprocess Report",
        "",
        f"- Dry run: `{str(dry_run).lower()}`",
        f"- Vault: `{vault}`",
        f"- Notes to reprocess: {counts['reprocessed']}",
        f"- Skipped: {counts['skipped']}",
        f"- Held for review: {counts['review_required']}",
        f"- Failed: {counts['failed']}",
        "",
    ]
    report.extend(verification_report(verification, records))
    report.extend(lexicon_report(records))
    # The summaries side by side: the one thing a reader has to judge before
    # approving is whether the new pass understood the recording as well.
    report.extend(["## Summaries, Before And After", ""])
    if done:
        for record in done:
            report.extend(
                [
                    f"### {Path(record['source']).name}",
                    "",
                    f"- Was: {record.get('previous_summary') or '_no summary_'}",
                    f"- Now: {record.get('summary') or '_no summary: short enough that the title says it._'}",
                    "",
                ]
            )
    else:
        report.extend(["- None", ""])
    report.extend(["## Held For Review", "", "These notes were not rewritten.", ""])
    append_listing(report, review, lambda record: f"- `{record['source']}` — {record['review_reason']}")
    report.extend(["", "## Skipped", ""])
    append_listing(report, skipped, lambda item: f"- `{item['path']}` — {item['skip_reason']}")
    if warnings:
        report.extend(["", "## Warnings", ""])
        report.extend(f"- {warning}" for warning in warnings)
    report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return plan_path, report_path


def reconcile_notes(args):
    """Set aside inbox exports whose recording is already in the vault.

    A transcription app re-exports: the same recording arrives again, sometimes
    a byte or two different, long after the note made from it was filed. Left
    alone these process a second time and the vault grows a near-twin of every
    note it already had. Matching is on the recording's normalized text, not on
    the filename, because the export names drift too. Nothing is deleted -- a
    match moves to the recoverable quarantine, and anything that does not match
    stays exactly where it is for a person to look at.
    """
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    warnings = []
    known = {}
    for path in selected_notes(vault, schema_path, "vault", None):
        rel = relative_path(vault, path)
        if rel == INBOX_DIR or rel.startswith(f"{INBOX_DIR}/"):
            continue
        try:
            split = split_frontmatter(path.read_bytes())
        except (OSError, UnicodeDecodeError):
            continue
        if not split["had_frontmatter"] or split["malformed"]:
            continue
        recording = transcript_source(split["body"], vault)
        if not parse_transcript(recording)["blocks"]:
            continue
        known.setdefault(sha256_text(normalize_body_for_hash(recording)), rel)

    matched, unmatched = [], []
    taken = set()
    for item in scan_inbox(vault, args.limit):
        # Only a raw export is a candidate. Anything already carrying
        # frontmatter has been processed and belongs to `reprocess`.
        if not item["is_transcript"]:
            continue
        try:
            body = split_frontmatter((vault / item["path"]).read_bytes())["body"]
        except (OSError, UnicodeDecodeError) as error:
            warnings.append(f"{item['path']}: unreadable ({error})")
            continue
        digest = sha256_text(normalize_body_for_hash(transcript_source(body, vault)))
        if digest in known:
            matched.append(
                {
                    "source": item["path"],
                    "source_hash": item["sha256"],
                    "destination": assign_quarantine_path(vault, item["path"], taken),
                    "already_in": known[digest],
                }
            )
        else:
            unmatched.append({"source": item["path"]})

    counts = {"matched": len(matched), "unmatched": len(unmatched), "quarantined": 0}
    with run_state.run_lock(vault / STATE_DIR):
        run_dir = Path(args.run).expanduser().resolve() if args.run else unique_run_directory(vault)
        if args.apply:
            log_path = run_dir / "apply-log.jsonl"
            prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
            done = {entry.get("source") for entry in prior if entry.get("status") == "ok"}
            for record in matched:
                if record["source"] in done:
                    counts["quarantined"] += 1
                    continue
                try:
                    backup = apply_quarantine(vault, run_dir, record)
                    counts["quarantined"] += 1
                    entry = {
                        "op": "quarantine",
                        "status": "ok",
                        "source": record["source"],
                        "destination": record["destination"],
                        "backup": relative_path(run_dir, backup),
                    }
                except (OSError, UserError) as error:
                    entry = {"op": "quarantine", "status": "failed", "source": record["source"], "error": str(error)}
                    warnings.append(f"{record['source']}: quarantine failed ({error})")
                run_state.append_jsonl_fsync(log_path, entry)

        plan_path = run_dir / "reconcile-plan.json"
        run_state.atomic_write_json(
            plan_path,
            {
                "dry_run": not args.apply,
                "vault": str(vault),
                "run_directory": str(run_dir),
                "counts": counts,
                "matched": matched,
                "unmatched": unmatched,
                "warnings": warnings,
            },
        )
        report = [
            "# Vault Transcripts Reconcile Report",
            "",
            f"- Dry run: `{str(not args.apply).lower()}`",
            f"- Vault: `{vault}`",
            f"- Re-exports already in the vault: {counts['matched']}",
            f"- Exports with no match: {counts['unmatched']}",
            "",
            "## Already In The Vault",
            "",
            "These move to the recoverable quarantine; the filed note keeps the recording.",
            "",
        ]
        report.extend(
            [f"- `{record['source']}` — already at `{record['already_in']}`" for record in matched] or ["- None"]
        )
        report.extend(["", "## No Match", "", "Left exactly where they are. Process them, or look at them first.", ""])
        report.extend([f"- `{record['source']}`" for record in unmatched] or ["- None"])
        report_path = run_dir / "reconcile-report.md"
        report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return structured(
        "ok",
        artifacts=[str(plan_path), str(report_path)],
        warnings=warnings,
        data={
            "dry_run": not args.apply,
            "vault": str(vault),
            "run_directory": str(run_dir),
            "counts": counts,
        },
    )


def split_notes(args):
    """Move the recording out of every note that still carries one inline."""
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    schema_path = resolve_schema_path(vault, args.schema)
    schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
    if not raw_supported(schema):
        raise UserError(
            "schema defines no 'source' note type or no 'transcript' source kind, "
            "so a recording has no note of its own to move into"
        )
    warnings = []
    records = []
    skipped = []
    # Only this run's own planned names; ``assign_raw_name`` checks the disk for
    # everything already there.
    taken = set()
    with run_state.run_lock(vault / STATE_DIR):
        run_dir = Path(args.run).expanduser().resolve() if args.run else unique_run_directory(vault)
        for path in selected_notes(vault, schema_path, "vault", args.limit):
            try:
                planned = plan_split(vault, schema, path, taken)
            except (OSError, UnicodeDecodeError, UserError, ValueError) as error:
                warnings.append(f"{relative_path(vault, path)}: {type(error).__name__}: {error}")
                continue
            if planned is None:
                continue
            if planned.get("skip"):
                skipped.append({"source": relative_path(vault, path), "reason": planned["skip"]})
                warnings.append(f"{relative_path(vault, path)}: {planned['skip']}")
                continue
            warnings.extend(f"{planned['source']}: {warning}" for warning in planned["warnings"])
            records.append(planned)

        counts = {
            "notes_to_split": len(records),
            "skipped": len(skipped),
            "converted_from_source": sum(1 for record in records if record["converted_from_source"]),
            "applied": 0,
        }
        if args.apply:
            log_path = run_dir / "apply-log.jsonl"
            prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
            done = {entry.get("source") for entry in prior if entry.get("status") == "ok"}
            for record in records:
                if record["source"] in done:
                    counts["applied"] += 1
                    continue
                try:
                    backup = apply_split(vault, run_dir, record)
                    counts["applied"] += 1
                    entry = {
                        "op": "split",
                        "status": "ok",
                        "source": record["source"],
                        "raw_destination": record["raw_destination"],
                        "backup": relative_path(run_dir, backup),
                    }
                except (OSError, UserError) as error:
                    entry = {"op": "split", "status": "failed", "source": record["source"], "error": str(error)}
                    warnings.append(f"{record['source']}: split failed ({error})")
                run_state.append_jsonl_fsync(log_path, entry)

        plan = {
            "dry_run": not args.apply,
            "vault": str(vault),
            "schema_hash": schema_hash,
            "run_directory": str(run_dir),
            "counts": counts,
            "skipped": skipped,
            "warnings": warnings,
            "records": [
                {key: value for key, value in record.items() if key not in {"path", "processed_text", "raw_text"}}
                for record in records
            ],
        }
        plan_path = run_dir / "split-plan.json"
        run_state.atomic_write_json(plan_path, plan)
        report = [
            "# Vault Transcripts Split Report",
            "",
            f"- Dry run: `{str(not args.apply).lower()}`",
            f"- Vault: `{vault}`",
            f"- Notes to split: {counts['notes_to_split']}",
            f"- Notes changing from `type: source` to `type: note`: {counts['converted_from_source']}",
            f"- Skipped: {counts['skipped']}",
            "",
            "## Splits",
            "",
        ]
        if records:
            for record in records:
                report.append(f"- `{record['source']}` → recording moved to `{record['raw_destination']}`")
        else:
            report.append("- None")
        report.extend(["", "## Skipped", ""])
        report.extend(
            [f"- `{entry['source']}` — {entry['reason']}" for entry in skipped] if skipped else ["- None"]
        )
        report_path = run_dir / "split-report.md"
        report_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return structured(
        "ok",
        artifacts=[str(plan_path), str(report_path)],
        warnings=warnings,
        data={
            "dry_run": not args.apply,
            "vault": str(vault),
            "run_directory": str(run_dir),
            "counts": counts,
        },
    )


def status(args):
    run_dir = Path(args.run).expanduser().resolve()
    state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
    total = None
    scan_path = run_dir / "scan.json"
    if scan_path.is_file():
        try:
            items = json.loads(scan_path.read_text(encoding="utf-8"))["items"]
            total = sum(1 for item in items if item.get("is_transcript"))
        except (OSError, json.JSONDecodeError, KeyError):
            total = None
    classified, _ = run_state.read_jsonl_recover_tail(run_dir / "classified.jsonl", repair=False)
    cleaned, _ = run_state.read_jsonl_recover_tail(run_dir / "cleaned.jsonl", repair=False)
    summarized, _ = run_state.read_jsonl_recover_tail(run_dir / "summaries.jsonl", repair=False)
    applied, _ = run_state.read_jsonl_recover_tail(run_dir / "apply-log.jsonl", repair=False)
    durations = [row.get("seconds", 0.0) for row in classified if row.get("seconds")]
    remaining = max(total - len(classified), 0) if total is not None else None
    return structured(
        "ok",
        data={
            "run_directory": str(run_dir),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "transcripts": total,
            "classified": len(classified),
            "cleaned_chunks": sum(1 for row in cleaned if row.get("status") == "ok"),
            "summarized": len(summarized),
            "remaining": remaining,
            "eta": format_duration(sum(durations) / len(durations) * remaining) if durations and remaining else None,
            "applied_operations": sum(1 for entry in applied if entry.get("status") == "ok"),
            "next_action": state.get("nextAction"),
        },
    )


def doctor(args):
    vault = Path(args.vault).expanduser().resolve()
    checks = {}
    warnings = []
    ok = True
    if vault.is_dir() and os.access(vault, os.W_OK):
        checks["vault"] = {"ok": True, "path": str(vault)}
    else:
        checks["vault"] = {"ok": False, "path": str(vault), "detail": "vault root missing or not writable"}
        ok = False
    inbox = vault / INBOX_DIR
    checks["inbox"] = {"ok": inbox.is_dir(), "path": str(inbox)}
    ok = ok and checks["inbox"]["ok"]
    if checks["inbox"]["ok"]:
        items = scan_inbox(vault, args.limit)
        checks["inbox"]["notes"] = len(items)
        checks["inbox"]["transcripts"] = sum(1 for item in items if item["is_transcript"])
    schema = {}
    schema_check = {"ok": False}
    if checks["vault"]["ok"]:
        try:
            schema_path = resolve_schema_path(vault, args.schema)
            schema, schema_hash = compiled_schema_for(vault, schema_path, cache_dir=vault / STATE_DIR / "cache")
            schema_check = {"ok": True, "path": str(schema_path), "schema_hash": schema_hash}
            for recording_type in RECORDING_TYPES:
                frontmatter_metadata(schema, recording_type)
        except UserError as error:
            schema_check = {"ok": False, "detail": str(error)}
    checks["schema"] = schema_check
    ok = ok and schema_check["ok"]
    voice_check = {
        "ok": True,
        "configured": False,
        "stages": {
            "owner memo/journal cleanup, summary, reflection": "owner",
            "external-source cleanup and summary": "source",
            "meeting, conversation, therapy, ambiguous": "none",
        },
    }
    if checks["vault"]["ok"]:
        try:
            voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
            if voice_path is None:
                voice_check["detail"] = (
                    "disabled with --no-voice" if args.no_voice else f"no voice note; default is {vault_voice.DEFAULT_VOICE}"
                )
            else:
                voice, voice_hash = vault_voice.compiled_voice_for(
                    vault, voice_path, cache_dir=vault / STATE_DIR / "cache"
                )
                unknown_types = sorted(set(voice.get("per_type", {})) - set(schema.get("types", {})))
                voice_check = {
                    "ok": True,
                    "configured": True,
                    "path": str(voice_path),
                    "voice_hash": voice_hash,
                    "compiler_version": vault_voice.COMPILED_VOICE_VERSION,
                    "recognized_scopes": voice.get("recognized_scopes", []),
                    "types_with_style": sorted(voice.get("per_type", {})),
                    "unknown_scopes": voice.get("unknown_scopes", []),
                    "unknown_schema_types": unknown_types,
                    "stages": {
                        "owner memo/journal cleanup, summary, reflection": "owner",
                        "external-source cleanup and summary": "source",
                        "meeting, conversation, therapy, ambiguous": "none",
                    },
                }
                if unknown_types:
                    warnings.append("voice note has unknown schema note types: " + ", ".join(unknown_types))
        except UserError as error:
            voice_check = {"ok": False, "configured": True, "detail": str(error)}
            warnings.append(f"voice note could not be read: {error}")
    checks["voice"] = voice_check
    ok = ok and voice_check["ok"]

    # Reported but never fatal: a broken register costs the layer, not the run.
    profile_check = {
        "ok": True,
        "configured": False,
        "stages": {
            "summary and memo/journal reflection": "owner",
            "cleanup and classification": "never — both run behind a fabrication gate",
        },
    }
    if checks["vault"]["ok"]:
        try:
            profile_path = vault_profile.resolve_profile_path(vault, args.profile, disabled=args.no_profile)
            if profile_path is None:
                profile_check["detail"] = (
                    "disabled with --no-profile"
                    if args.no_profile
                    else f"no personal context note; default is {vault_profile.DEFAULT_PROFILE}"
                )
            else:
                profile, profile_hash, profile_warnings = vault_profile.compiled_profile_for(
                    vault, profile_path, cache_dir=vault / STATE_DIR / "cache"
                )
                profile_check = {
                    **profile_check,
                    "configured": profile is not None,
                    "path": str(profile_path),
                    "profile_hash": profile_hash,
                    "compiler_version": vault_profile.COMPILED_PROFILE_VERSION,
                    "owner": (profile or {}).get("owner"),
                    "cards": vault_profile.profile_digest(profile),
                    "budgets": {
                        "prefix": vault_profile.DEFAULT_PREFIX_BUDGET,
                        "context": vault_profile.DEFAULT_CONTEXT_BUDGET,
                        "per_card": vault_profile.MAX_CARD_CHARS,
                    },
                }
                warnings.extend(profile_warnings)
        except UserError as error:
            profile_check = {**profile_check, "configured": True, "detail": str(error)}
            warnings.append(f"personal context note could not be read: {error}")
    checks["profile"] = profile_check

    lexicon_check = {"ok": True, "configured": False}
    if checks["vault"]["ok"]:
        try:
            lexicon_path = vault_lexicon.resolve_lexicon_path(vault, args.lexicon, disabled=args.no_lexicon)
            dictionary = dictionary_path(args)
            lexicon, lexicon_hash = vault_lexicon.load_lexicon(
                vault,
                lexicon_path,
                schema=schema,
                cache_dir=vault / STATE_DIR / "cache",
                dictionary_path=dictionary,
            )
            if lexicon is None:
                lexicon_check["detail"] = (
                    "disabled with --no-lexicon"
                    if args.no_lexicon
                    else f"no terms or speakers; the note default is {vault_lexicon.DEFAULT_LEXICON}"
                )
            else:
                lexicon_check = {
                    "ok": True,
                    "configured": True,
                    "path": str(lexicon_path) if lexicon_path else None,
                    "dictionary": str(dictionary) if dictionary else None,
                    "lexicon_hash": lexicon_hash,
                    "compiler_version": vault_lexicon.COMPILED_LEXICON_VERSION,
                    **vault_lexicon.lexicon_digest(lexicon),
                }
                if lexicon_path is None:
                    warnings.append(
                        f"no lexicon note; the roster is the directory notes and the terms are the "
                        f"standalone dictionary. Create {vault_lexicon.DEFAULT_LEXICON} to add "
                        "nicknames, cues, and who actually appears in recordings"
                    )
        except UserError as error:
            lexicon_check = {"ok": False, "configured": True, "detail": str(error)}
            warnings.append(f"lexicon note could not be read: {error}")
    checks["lexicon"] = lexicon_check
    ok = ok and lexicon_check["ok"]
    # Cleanup is one call per chunk, so a backend that reasons first costs
    # hundreds of hidden tokens per chunk. Report that before a long run, not
    # halfway through one.
    chat_probe = forge_llm.service_doctor(
        chat_service(args), expect_non_thinking=True, timeout=min(args.request_timeout, 60)
    )
    checks["chat"] = {
        "ok": chat_probe["reachable"],
        "url": chat_probe["url"],
        "model": chat_probe["model"],
        "detail": chat_probe.get("detail"),
    }
    for key in ("thinking", "hiddenTokens", "modelMismatch", "servedModels"):
        if key in chat_probe:
            checks["chat"][key] = chat_probe[key]
    ok = ok and chat_probe["reachable"]
    if chat_probe.get("warning"):
        warnings.append(chat_probe["warning"])
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
        warnings.append("no thinking service is configured; verification would run on the bulk service")
    if not think_probe["reachable"]:
        warnings.append("thinking service is unreachable; runs would report that nothing was verified")
    return structured("ok" if ok else "error", warnings=warnings, data={"checks": checks})


class TrackingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Rename, clean, and summarize voice-note transcripts in an Obsidian inbox.")
    parser.add_argument("mode", choices=["process", "reprocess", "split", "reconcile", "status", "doctor"])
    parser.add_argument("--vault")
    parser.add_argument("--schema", action=TrackingAction)
    parser.add_argument("--voice", action=TrackingAction, help="voice-and-style note (default: the vault's, when present)")
    parser.add_argument("--no-voice", action="store_true", help="disable the vault voice policy for this run")
    parser.add_argument("--lexicon", action=TrackingAction, help="speakers-and-terms note (default: the vault's, when present)")
    parser.add_argument("--no-lexicon", action="store_true", help="disable term correction and the speaker roster")
    parser.add_argument("--profile", action=TrackingAction, help="personal-context register note (default: the vault's, when present)")
    parser.add_argument("--no-profile", action="store_true", help="disable personal context for this run")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--run", help="existing run directory to resume")
    parser.add_argument("--limit", type=int, action=TrackingAction)
    parser.add_argument("--filename-pattern", choices=FILENAME_PATTERNS, action=TrackingAction)
    parser.add_argument("--summary-style", choices=SUMMARY_STYLES, action=TrackingAction)
    parser.add_argument("--speaker-policy", choices=SPEAKER_POLICIES, action=TrackingAction)
    parser.add_argument("--owner", action=TrackingAction, help="the recorder's own name, preferred over a generic label")
    parser.add_argument("--tiny-words", type=int, action=TrackingAction, help="notes under this many words get light cleanup")
    parser.add_argument("--tiny-summary", choices=TINY_SUMMARY_CHOICES, action=TrackingAction)
    parser.add_argument("--base-url", action=TrackingAction)
    parser.add_argument("--model", action=TrackingAction)
    parser.add_argument("--api-key")
    parser.add_argument("--request-timeout", type=float, default=600)
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument("--no-verify", action="store_true", help="skip the thinking-model review")
    parser.add_argument("--think-url", help="thinking service used for verification (default: connectedServices.think)")
    parser.add_argument("--think-model")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    for key in RESUMABLE_OPTION_FLAGS:
        if not hasattr(args, f"{key}_provided"):
            setattr(args, f"{key}_provided", False)
    if args.limit is not None and args.limit < 0:
        raise UserError("--limit must be non-negative")
    if args.tiny_words is not None and args.tiny_words < 0:
        raise UserError("--tiny-words must be non-negative")
    args.filename_pattern = args.filename_pattern or FILENAME_PATTERNS[0]
    args.summary_style = args.summary_style or SUMMARY_STYLES[0]
    args.speaker_policy = args.speaker_policy or SPEAKER_POLICIES[0]
    args.tiny_summary = args.tiny_summary or TINY_SUMMARY_CHOICES[0]
    if args.tiny_words is None:
        args.tiny_words = 120
    if args.mode == "status":
        if not args.run:
            raise UserError("status requires --run <run-directory>")
        return args
    if not args.vault:
        raise UserError(f"{args.mode} requires --vault")
    args.schema = args.schema or os.environ.get("VAULT_TRANSCRIPTS_SCHEMA") or None
    if args.mode in {"split", "reconcile"}:
        # Deterministic text work: no model reads a note in either mode, so both
        # run with every endpoint down.
        return args
    resolved = forge_llm.resolve_service("chat", base_url=args.base_url, model=args.model)
    args.base_url = resolved["url"]
    args.model = resolved["model"]
    args.api_key = args.api_key or os.environ.get("VAULT_TRANSCRIPTS_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    args.voice = args.voice or os.environ.get("VAULT_TRANSCRIPTS_VOICE") or None
    args.profile = args.profile or os.environ.get("VAULT_TRANSCRIPTS_PROFILE") or None
    if args.no_voice and args.voice and args.voice_provided:
        raise UserError("--voice and --no-voice cannot be used together")
    args.lexicon = args.lexicon or os.environ.get("VAULT_TRANSCRIPTS_LEXICON") or None
    if args.no_lexicon and args.lexicon and args.lexicon_provided:
        raise UserError("--lexicon and --no-lexicon cannot be used together")
    args.cache_prompt = not args.no_cache_prompt
    args.verify = not args.no_verify
    return args


def run(argv):
    args = parse_args(argv)
    if args.mode == "status":
        return status(args)
    if args.mode == "doctor":
        return doctor(args)
    if args.mode == "split":
        return split_notes(args)
    if args.mode == "reconcile":
        return reconcile_notes(args)
    if args.mode == "reprocess":
        return reprocess_notes(args)
    return process(args)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        result = run(argv)
    except UserError as error:
        print_json(structured("error", errors=[error_entry("user_error", str(error))]))
        return 2
    except ValueError as error:
        print_json(structured("error", errors=[error_entry("run_state_error", str(error))]))
        return 2
    except forge_llm.ChatError as error:
        print_json(structured("error", errors=[error_entry("chat_error", str(error))]))
        return 2
    except KeyboardInterrupt:
        print_json(structured("error", errors=[error_entry("interrupted", "interrupted")]))
        return 130
    print_json(result)
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
