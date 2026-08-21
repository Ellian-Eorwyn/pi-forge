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
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "lib"))
import forge_llm
import forge_routing
import forge_verify
import run_state
import vault_compose
import vault_format
import vault_lexicon
from vault_moves import PlainMover, backup_once, resolve_mover
import vault_profile
import vault_reflection
import vault_review
import vault_voice
from vault_schema import (
    FRONTMATTER_KEY_RE,
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
    resolve_selection,
    safe_title,
    selected_notes,
    serialize_frontmatter,
    sha256_bytes,
    sha256_text,
    split_frontmatter,
    strip_yaml_scalar,
    wikilink_target,
    yaml_quote,
)

WORKFLOW = "vault-transcripts"
PROMPT_VERSION = "vault-transcripts-v7"
# The pipeline's shape, not its prompts: v2 is the one-pass writer. A v1 run
# directory (classify/clean/summarize journals) cannot resume into this code,
# and the fingerprint mismatch this produces is the intended refusal. v3 adds the
# project-led meeting title and the at-a-glance meeting brief.
PIPELINE_VERSION = 3
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
# Meetings are kept as concise minutes, not verbatim cleanup: the recording is
# preserved and linked separately (its own source note), so the processed note
# may paraphrase and compress. That exempts a meeting from the verbatim gate —
# added words, length ratio, rare-word retention, utterance-locatable — because a
# summary is meant to drop and rephrase, and from the verbatim fidelity review.
# The note-level review still checks its title, summary, and speaker names for
# fabrication. Only `meeting` is summarized; conversation and therapy stay
# verbatim, and lecture keeps structured full content.
SUMMARIZED_TYPES = frozenset({"meeting"})


def is_summarized(recording_type):
    return recording_type in SUMMARIZED_TYPES


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
# Two export shapes reach this inbox: the compact `20260724 131748-9788991C` and
# the dashed-and-dotted `2025-08-08 13.07.21 Ellian`. The optional `-` in the date
# and `.`/`:`/`_` in the time absorb both without a second pattern. A time
# component is required, which is what keeps this from matching a processed
# `2026-08-11 - Meeting - Topic` note (a ` - ` follows the date there, not a time).
FILENAME_STAMP_RE = re.compile(
    r"^(\d{4})-?(\d{2})-?(\d{2})[ _-](\d{2})[.:_]?(\d{2})[.:_]?(\d{2})(?:-([0-9A-Fa-f]{6,10}))?"
)
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
SUMMARY_TARGET_WORDS = 90
# The point at which a summary has stopped being a summary, with enough slack
# that a good one at the target length is never rejected.
SUMMARY_MAX_WORDS = 120
# Cleanup deliberately runs without a max_tokens ceiling. A ceiling looks
# obviously right -- cleanup only removes, so a long answer must be a runaway --
# and it is wrong twice over on this stack. Measured: the same chunk costs 730
# completion tokens on `chat` and 7,425 on `think`, because `think` spends
# ~29,000 characters reasoning before it answers, and a ceiling sized for the
# visible answer truncates it into a hard failure. And the runaway it was meant
# to catch does not exist: in a 34-file run the failed chunks averaged 187.9s
# against 219.8s for successes on the same service. Slow chunks are the thinking
# route being itself, not generations going off the rails.
# Meaning-first cleanup may rephrase, so words the source did not contain are
# expected, not a fault. The gate holds only a *wholesale* rewrite: the ceiling
# is a share of the cleaned content (`INVENTED_WORD_FRACTION`), so a long chunk
# tolerates the dozen synonyms real paraphrase produces, while `MAX_INVENTED_WORDS`
# keeps a small floor so a short chunk that is mostly fabricated is still caught.
# The thinking verify pass reads the rest for the fabrication a word count cannot
# tell from a synonym.
MAX_INVENTED_WORDS = 4
INVENTED_WORD_FRACTION = 0.25
# The one gate problem a human can waive: the cleaned text carried more content
# words the source did not than the budget allows. Every other check_chunk
# problem (kept a timestamp, emitted a heading, used a speaker label) is a
# structural defect, not a judgement call, so it is never waivable and keeps its
# automatic corrective retry.
INVENTED_PROBLEM_PREFIX = "these words are not in the chunk"
MIN_RARE_WORDS = 10
RARE_WORD_MIN_SOURCE_WORDS = 400
# Meaning-first cleanup rephrases, so a distinctive source word is often replaced
# by a synonym rather than dropped — indistinguishable to a word-overlap check.
# The floor is set below the retention faithful paraphrase produces on a long
# transcript (measured 0.70-0.77 on real memos the judge rated faithful); a real
# dropped thread falls much further, and the thinking verify pass is the backstop.
RARE_WORD_RETENTION = 0.70
# Spoken-to-written cleanup compresses: filler, false starts, and a circumlocution
# rewritten as the sentence it was reaching for take a real fraction of the words
# out. Measured against the calibration example, a filler-heavy passage lands near
# 0.47, so a 0.5 floor would have held the register's best work for review. The
# floor still exists to catch a cleanup that summarized instead of cleaning.
CLEANED_RATIO_MIN = 0.4
CLEANED_RATIO_MAX = 1.1
TINY_RATIO_MIN = 0.3
# Real speech tops out around five or six words a second. A transcript claiming
# far more than that against its own clip length is corrupt speech-to-text, not a
# recording any cleanup can rescue — it belongs in the re-record list, not the
# review lane a person is meant to act on.
MAX_WORDS_PER_SECOND = 6.0
# The other end of the same sanity check, used only to disambiguate a two-part
# timestamp. One transcription app writes elapsed time as *HH:MM* (a 63-minute
# lecture ends *01:03*), which reads as 63 *seconds* under MM:SS and looks corrupt.
# Rescaling to minutes is only trusted when the resulting rate is real speech: a
# rate this far below normal means the minutes reading is wrong too, so the file is
# left alone rather than coarsened into a plausible-looking lie.
WPS_FLOOR = 0.2
# A timestamp-less export that is still plainly a transcript — a small set of
# speaker labels, each recurring, on their own lines above what they said — is
# reported as its own lane rather than filed as a raw note, and can be processed
# verbatim with --unlabeled. The thresholds keep an ordinary prose note, whose
# paragraph first-lines never recur, out of the lane.
UNLABELED_MIN_WORDS = 200
UNLABELED_MIN_TURNS = 6
FIDELITY_SAMPLES = 4
FIDELITY_MIN_CONTAINMENT = 0.5
FIDELITY_MIN_WORDS = 4
FIDELITY_REVIEW_EFFORT = "medium"
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
        return {"date": None, "time_hhmm": None, "time_hhmmss": None, "recording_id": None}
    year, month, day, hour, minute, second, recording_id = match.groups()
    try:
        date = datetime.date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return {"date": None, "time_hhmm": None, "time_hhmmss": None, "recording_id": None}
    if not (0 <= int(hour) < 24 and 0 <= int(minute) < 60):
        return {"date": date, "time_hhmm": None, "time_hhmmss": None, "recording_id": recording_id}
    # Seconds were parsed and discarded until `daily` needed them. Merging a day's
    # recordings rebases each one's offsets onto its own start, and two memos
    # begun in the same minute have to stay in the order they were spoken.
    time_hhmmss = None
    if 0 <= int(second) < 60:
        time_hhmmss = f"{hour}:{minute}:{second}"
    return {
        "date": date,
        "time_hhmm": f"{hour}{minute}",
        "time_hhmmss": time_hhmmss,
        "recording_id": recording_id,
    }


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


def format_filename(pattern, date, time_hhmm, recording_type, title, include_time=False, project=None):
    """The one place the note-naming convention lives."""
    prefix = date or ""
    if prefix and time_hhmm and (include_time or pattern == "date-time-topic"):
        prefix = f"{prefix} {time_hhmm}"
    if pattern == "date-type-topic":
        # A meeting whose title already leads with its project codename does not
        # also want the generic "Meeting" label in front of it: the project is the
        # better label, and `type: meeting` in the frontmatter still records the
        # kind. Drop the label only when a project was identified; a project-less
        # meeting keeps the `<date> - Meeting - <title>` shape it has always had.
        if recording_type == "meeting" and project:
            return " - ".join(part for part in (prefix, title) if part) + ".md"
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


def assign_unique_name(vault, directory, args, date, time_hhmm, recording_type, title, taken_casefold, source_rel, project=None):
    """Pick a free filename, preferring the configured pattern.

    A same-day second recording on the same topic gets the recording time before
    it gets a numeric suffix: the time is information, ``(2)`` is not.
    """
    candidates = [format_filename(args.filename_pattern, date, time_hhmm, recording_type, title, project=project)]
    with_time = format_filename(args.filename_pattern, date, time_hhmm, recording_type, title, include_time=True, project=project)
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


def parse_transcript(body, allow_unlabeled=False):
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
        # No timestamped blocks. A note the pipeline should skip stays empty; under
        # --unlabeled a speaker-labelled export with no timestamps is parsed instead.
        if allow_unlabeled:
            return parse_unlabeled(body)
        return {"preamble": body, "blocks": [], "trailing": ""}

    first = starts[0][0]
    blocks = []
    part_counts = []
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
        stamp = kinds[offset - 1][1]
        text_lines = [line.strip() for line in lines[offset:end] if line.strip()]
        blocks.append(
            {
                "speaker": speaker,
                "seconds": timestamp_seconds(stamp),
                "text": " ".join(text_lines),
                "raw": "".join(lines[start:end]),
            }
        )
        part_counts.append(stamp.count(":") + 1)
    parsed = {"preamble": "".join(lines[:first]), "blocks": blocks, "trailing": "".join(lines[end:])}
    coarsen_minutes_if_impossible(parsed, part_counts)
    return parsed


def coarsen_minutes_if_impossible(parsed, part_counts):
    """Reinterpret two-part timestamps as ``*HH:MM*`` when the ``*MM:SS*`` reading
    implies an impossible speaking rate, in place.

    One transcription app writes elapsed time as ``*HH:MM*`` — a 63-minute lecture
    ends at ``*01:03*`` — which ``timestamp_seconds`` reads as 63 *seconds*. Left
    alone, thousands of words against a minute look like corrupt speech-to-text and
    the recording is wrongly held for re-recording. The rescale is trusted only when
    the evidence is unambiguous: every timestamp is two-part (a genuine ``HH:MM:SS``
    file is never touched), the span is monotonic, the MM:SS rate is impossible, and
    the ``*60`` reading lands in the real-speech band. When even the minutes reading
    is impossible the file is left untouched and the corrupt-input gate still holds
    it — the reinterpretation only ever rescues a plausible recording, never
    launders a corrupt one into looking fine.

    Seconds are the only thing rewritten; ``raw`` is preserved, so the byte-exact
    round-trip and the verbatim recording are unaffected.
    """
    blocks = parsed["blocks"]
    if not blocks or any(count != 2 for count in part_counts):
        return
    seconds = [block["seconds"] for block in blocks]
    if any(earlier > later for earlier, later in zip(seconds, seconds[1:])):
        return
    duration = max(seconds, default=0)
    words = sum(len(block["text"].split()) for block in blocks)
    if not duration or not words:
        return
    if words / duration <= MAX_WORDS_PER_SECOND:
        return
    if not (WPS_FLOOR <= words / (duration * 60) <= MAX_WORDS_PER_SECOND):
        return
    for block in blocks:
        block["seconds"] *= 60
    parsed["timestamp_style"] = "HH:MM"


def parse_unlabeled(body):
    """Parse a speaker-labelled export that carries no timestamps.

    The transcriber writes each turn as a short label line — a bare name or role,
    ``Ellian`` then ``Sopagna`` — followed by what they said and a blank line. Each
    blank-separated group is one turn whose first line is the speaker; blocks carry
    ``seconds = None`` because there is no clock to read. Slicing stays contiguous, so
    ``preamble + blocks + trailing`` reproduces the body byte for byte, and the
    verbatim recording is preserved exactly as with a timestamped export.
    """
    lines = body.splitlines(keepends=True)
    starts = [
        index
        for index, line in enumerate(lines)
        if line.strip() and (index == 0 or not lines[index - 1].strip())
    ]
    if not starts:
        return {"preamble": body, "blocks": [], "trailing": ""}
    first = starts[0]
    blocks = []
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        text_lines = [line.strip() for line in lines[start + 1 : end] if line.strip()]
        blocks.append(
            {
                "speaker": lines[start].strip().rstrip(":").strip() or None,
                "seconds": None,
                "text": " ".join(text_lines),
                "raw": "".join(lines[start:end]),
            }
        )
    return {"preamble": "".join(lines[:first]), "blocks": blocks, "trailing": "", "timestamp_style": "unlabeled"}


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
    # An unlabeled export has no clock, so its blocks carry no seconds; they drop out
    # here, leaving a zero duration the rate gate correctly declines to judge.
    seconds = [block["seconds"] for block in blocks if block["seconds"] is not None]
    return {
        "blocks": len(blocks),
        "words": words,
        "duration_seconds": max(seconds, default=0),
        # A real recording's timestamps only move forward; if they don't, the span
        # is not a duration and the words-per-second rate cannot be trusted.
        "timestamps_ordered": all(earlier <= later for earlier, later in zip(seconds, seconds[1:])),
        "speaker_labels": dict(labels),
        "has_preamble": bool(parsed["preamble"].strip()),
        "has_trailing": bool(parsed["trailing"].strip()),
        # A file coarsened to minutes carries its style on the parsed dict; without
        # that marker the seconds decide, and a rescaled file would otherwise look
        # like a genuine HH:MM:SS export.
        "timestamp_style": parsed.get("timestamp_style")
        or ("HH:MM:SS" if any((block["seconds"] or 0) >= 3600 for block in blocks) else "MM:SS"),
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


def looks_like_untimestamped_transcript(split, body):
    """A no-frontmatter, no-timestamp note that is still plainly a speaker-labelled
    transcript: a small roster of labels, each recurring, covering most turns.

    A transcript names the same few speakers over and over; an ordinary note's
    blank-separated paragraphs open with a different sentence each time, so their
    first lines do not recur. That difference is the whole test.
    """
    if split["malformed"] or split["had_frontmatter"]:
        return False
    if any(TIMESTAMP_RE.match(line.strip()) for line in body.splitlines()):
        return False
    if len(body.split()) < UNLABELED_MIN_WORDS:
        return False
    turns = parse_unlabeled(body)["blocks"]
    if len(turns) < UNLABELED_MIN_TURNS:
        return False
    speakers = Counter(block["speaker"] for block in turns if block["speaker"])
    # A real speaker label is a short name or role, not a sentence or a heading: that
    # is what separates a transcript's recurring labels from prose whose paragraphs
    # happen to repeat an opening line.
    recurring = {name for name, count in speakers.items() if count >= 2 and _is_label_like(name)}
    if len(recurring) < 2:
        return False
    covered = sum(count for name, count in speakers.items() if name in recurring)
    return covered >= UNLABELED_MIN_TURNS and covered / len(turns) >= 0.6


def _is_label_like(name):
    if not name or name[0] in "#-*>|":
        return False
    if not (1 <= len(name.split()) <= 5):
        return False
    return name[-1] not in ".?!"


def parse_for_item(item, body):
    """Parse a raw body the way the scan decided this item reads.

    The parse mode is a property of the note, decided once at scan and recorded
    on the item (and carried onto its record), not a flag threaded through every
    call site. An unlabeled export must parse identically in cleanup, the
    verifier payloads, the fidelity floor, and the repair gate — the 2026-08-19
    inbox session held four notes precisely because one of those parsed the raw
    a different way and reviewed an empty transcript.
    """
    return parse_transcript(body, allow_unlabeled=bool(item.get("unlabeled")))


# --------------------------------------------------------------------------
# Scan and dedupe
# --------------------------------------------------------------------------


def unusable_input_reason(stats):
    """A deterministic reason the recording is unusable input, or ``None``.

    Corrupt transcription shows up as an impossible speaking rate — thousands of
    words against a minute of audio — which no cleanup can repair. A recording with
    no timestamps has no rate to judge and is left alone.
    """
    if not isinstance(stats, dict) or not stats.get("timestamps_ordered"):
        # No trustworthy duration to judge the rate against.
        return None
    words = stats.get("words") or 0
    duration = stats.get("duration_seconds") or 0
    if duration and words and words / duration > MAX_WORDS_PER_SECOND:
        return (
            f"{words} words against a {int(duration)}s recording is "
            f"{words / duration:.0f} words per second — the transcription is corrupt, not speech"
        )
    return None


def scan_inbox(vault, limit=None, select=None, allow_unlabeled=False):
    """Every Markdown file directly under the inbox tree, with what can be known
    about it without a model. ``select`` scopes the run to a set of vault-relative
    POSIX paths, filtered before any file is read or hashed, so a single-note run
    touches only that note. ``allow_unlabeled`` lets a speaker-labelled export with no
    timestamps count as a transcript rather than being reported as a skipped lane."""
    root = vault / INBOX_DIR
    if not root.is_dir():
        raise UserError(f"inbox directory does not exist: {root}")
    paths = []
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath = Path(directory)
        # The review lane's own surface is not inbox input: the staged proposals
        # in `_Pending Review/` are this run's output waiting for approval, and the
        # control note is a tool artifact. Reprocessing either would grow a twin of
        # every note or file the control note itself.
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if not (dirpath / name).is_symlink()
            and not name.startswith(".")
            and name != vault_review.PENDING_DIRNAME
        ]
        for filename in sorted(filenames):
            path = dirpath / filename
            if path.is_symlink() or path.suffix.lower() != ".md":
                continue
            if dirpath == root and filename == vault_review.REVIEW_NOTE_NAME:
                continue
            paths.append(path)
    paths.sort(key=lambda path: relative_path(vault, path))
    if select is not None:
        wanted = set(select)
        paths = [path for path in paths if relative_path(vault, path) in wanted]
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
            "no_timestamps": False,
            "unlabeled": False,
            "error": None,
            **parse_filename(path.name),
            "filename_hint": filename_title_hint(path.name),
            "stats": None,
        }
        try:
            data = path.read_bytes()
            item["sha256"] = sha256_bytes(data)
            split = split_frontmatter(data)
            body = transcript_source(split["body"], vault)
            parsed = parse_transcript(body, allow_unlabeled=allow_unlabeled)
            transcript, reason = is_transcript(split, parsed)
            item["is_transcript"] = transcript
            item["skip_reason"] = reason
            # Recorded on the item so downstream stages (and a resume that reloads the
            # cached scan) parse this note the same way scan did, without re-reading the
            # run-wide flag.
            item["unlabeled"] = transcript and parsed.get("timestamp_style") == "unlabeled"
            if transcript:
                item["stats"] = transcript_stats(parsed)
                broken = unusable_input_reason(item["stats"])
                if broken:
                    # An impossible speaking rate means the clock is corrupt, not
                    # the words: the app wrote garbage timestamps (the 2026-08-19
                    # inbox had ten such exports, all coherent text). The words are
                    # what gets cleaned and the clock is only derived data — so
                    # distrust the clock and keep the note, rather than holding
                    # readable text for re-recording. The model never sees
                    # timestamps either way (render_turns drops them).
                    item["clock_broken"] = broken
                    item["stats"] = {**item["stats"], "duration_seconds": 0}
            elif reason == "no timestamped transcript blocks" and looks_like_untimestamped_transcript(split, body):
                # A speaker-labelled transcript the transcriber never timestamped.
                # The roster/coverage thresholds above are the admission boundary
                # between a transcript and prose, so a file that clears them is
                # processed verbatim rather than parked behind a flag nobody
                # remembers to pass. --unlabeled still forces admission for
                # exports below those thresholds.
                parsed = parse_transcript(body, allow_unlabeled=True)
                item["is_transcript"] = True
                item["skip_reason"] = None
                item["no_timestamps"] = True
                item["unlabeled"] = True
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
 "project": "<codename for a meeting clearly about one, else null>",
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
lightly normalized, unless the transcript shows it is wrong. For a meeting
clearly about one named project, product, client, or codename, lead the title
with that name and then the subject — "FORGE - Sprint Review", not "Sprint
Review" — so the note sorts and reads under the project. When no single project
dominates, name the subject normally.

project - for a meeting only, the one project, product, client, or codename the
meeting is about, written exactly as you led the title with it. null for every
other recording type, and null for a meeting when no single project clearly
dominates. Advisory: it only shapes the filename and is never invented — when in
doubt, use null.

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

    # A meeting may lead its title with a project codename; the field records that
    # codename so the filename can drop the redundant "Meeting" label. It only ever
    # shapes the name, is never written to frontmatter, and is dropped on anything
    # unexpected so it can never raise or invent a project.
    project = None
    raw_project = value.get("project")
    if recording_type == "meeting" and not needs_review and isinstance(raw_project, str) and raw_project.strip():
        project = safe_title(raw_project)[:MAX_TITLE_CHARS] or None

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
        "project": project,
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
            parsed = parse_for_item(item, transcript_source(split_frontmatter(data)["body"], vault))
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
            raise UserError(
                f"classification was preempted by interactive activity: {error}. The run is "
                f"checkpointed — resume it with `process --vault {vault} --run {run_dir}`, or run the "
                "resume in the foreground so its review does not compete with the interactive session"
            ) from error
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

Fidelity rules, which outrank every style rule below. Two recording types are
exceptions, each with its own fidelity in its style entry: a `meeting` is
summarized into minutes (paraphrase and compress freely; its verbatim recording
is kept and linked separately), and `therapy` stays near-verbatim (keep the
speaker's exact words). For every other type:
- The register is spoken-to-written: what the speaker would have written had they
  typed this instead of saying it. Turn spoken delivery into clear, readable
  first-person prose. Meaning comes first, not the exact words — you may rephrase
  and smooth for readability — but every claim, point, name, number, and shade of
  the speaker's meaning and intent must survive unchanged. Keep their voice and
  register; do not rewrite them into more generic or more formal prose than they
  used.
- Remove filler and verbal scaffolding: "like", "um", "you know", "kind of",
  "sort of", "I mean", "basically", "essentially", "literally", "actually",
  "obviously", "honestly", along with false starts, restarted sentences,
  repeated phrases, and self-echoes that carry no meaning.
- Condense a circumlocution into the plain statement it was reaching for, but
  only when the meaning is unambiguous. "I would also like it to have,
  essentially, if it fails to categorize a note, there should be like a maybe in
  the system" becomes "I would also like a 'failed categorization' folder for
  notes it can't confidently categorize." When two readings are possible, keep
  the one that loses no meaning.
- Preserve the speaker's intent, uncertainty, and nuance. A hedge that qualifies
  a claim is content and stays: "I think", "maybe", "I don't know" mean
  something when they mark how sure the speaker is. A hedge that is pure
  delivery is filler and goes.
- Never add facts, names, dates, conclusions, or numbers that are not in the
  chunk, and never state something more certainly than the speaker did.
  Rephrasing what they said is fine; inventing what they did not say is not.
- Never drop substance, and never delete a whole utterance or exchange. Every
  point made must survive; even small talk survives, in its short readable form.
- Fix obvious transcription punctuation and casing.
- Do not summarize inside "cleaned", except for a meeting, whose "cleaned" is its
  minutes. For every other type the summary belongs in "chunk_summary".
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
- therapy: dialogue as in conversation, with the highest fidelity of all, and the
  exception to the meaning-first license above — keep the speaker's exact words.
  Remove only pure filler and false starts; never condense, and never swap a word
  for a synonym. Keep hesitation and repetition that carries weight. Never add
  clinical language, interpretation, or diagnosis that was not spoken.
- meeting: concise minutes, not a transcript — this is the exception to the
  fidelity rules above, so paraphrase and compress freely. Write what was
  discussed, decided, and assigned as brief prose under "##" topic headings — one
  heading per subject covered, which is also what the note's at-a-glance block is
  built from — attributing points to the speaker who made them where it matters.
  The note already opens with a small project/date/attendees/topics block added
  around your minutes; do not write one yourself, and do not add a title or an
  overview heading. Capture every decision and action item; where the chunk
  contains explicit ones, end it with "## Decisions" and "## Action Items"
  bullets, writing "Unassigned" or "Not stated" rather than inferring an owner or
  a deadline. Do not invent facts, names, numbers, or decisions that were not
  said. Do not reproduce the dialogue turn by turn — the verbatim recording is
  kept and linked separately.
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


def chunk_added_words(cleaned, source, speaker_map, drop_labels, glossary=()):
    """The full, untruncated invented-words list the gate holds on.

    ``check_chunk`` truncates the words into a readable message; the review lane
    and the journal need the whole set so a human sees exactly what a waiver
    would cover. Recomputed with the same allowance as the gate so the two agree.
    """
    if not isinstance(cleaned, str) or not cleaned.strip():
        return []
    allowed = [] if drop_labels else [value for value in (speaker_map or {}).values() if value]
    allowed = allowed + [offer["term"] for offer in glossary]
    return added_words(source, cleaned, allowed)


def invented_over_ceiling(source, cleaned, allowed):
    """The invented content words when they exceed the meaning-first ceiling, else [].

    The chunk gate and the apply-time recheck must agree on what counts as
    fabrication, so both decide it here. A share of the cleaned content
    (``INVENTED_WORD_FRACTION``, floored at ``MAX_INVENTED_WORDS``) is the
    paraphrase meaning-first cleanup allows; more than that is the note reaching
    for words the speaker did not use. Returns the full invented list when over
    the ceiling — the caller truncates it for the message — and [] otherwise.
    """
    if not isinstance(cleaned, str) or not cleaned.strip():
        return []
    invented = added_words(source, cleaned, allowed)
    cleaned_content = len(content_words(strip_structure(cleaned)))
    ceiling = max(MAX_INVENTED_WORDS, int(INVENTED_WORD_FRACTION * cleaned_content))
    return invented if len(invented) > ceiling else []


# A line that is nothing but a bold span (optionally with a colon) is the
# transcription app's speaker marker. On a single-speaker recording it is
# spurious — it carries no words of its own — so it is dropped rather than held.
# An inline label (``**Name:** words``) is left alone: removing it safely would
# mean knowing where the label ends and the speech begins, which this cannot.
SOLO_LABEL_LINE_RE = re.compile(r"^\s*\*\*[^*\n]{1,60}\*\*\s*:?\s*$")


def strip_solo_labels(text):
    """Remove standalone speaker-label lines from a single-speaker chunk.

    The solo determination is already re-checked with the roster withheld
    (`classify_without_roster`), so a bold marker here is the app's, not a real
    second voice, and dropping the line loses nothing.
    """
    if not isinstance(text, str) or "**" not in text:
        return text
    return "\n".join(line for line in text.split("\n") if not SOLO_LABEL_LINE_RE.match(line))


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
    invented = invented_over_ceiling(source, cleaned, allowed)
    if invented:
        problems.append(f"{INVENTED_PROBLEM_PREFIX}: {', '.join(invented[:8])}")
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
    if drop_labels:
        # Clear the app's spurious solo markers before the gate rather than failing
        # the chunk and holding the note for a human over them.
        cleaned = strip_solo_labels(cleaned)
    problems = check_chunk(cleaned, source, speaker_map, drop_labels, tiny, glossary)
    # The full invented-words list is only needed when that is what failed; every
    # other problem is structural and carries its whole message already.
    invented = (
        chunk_added_words(cleaned, source, speaker_map, drop_labels, glossary)
        if any(problem.startswith(INVENTED_PROBLEM_PREFIX) for problem in problems)
        else []
    )
    chunk_summary = value.get("chunk_summary") if isinstance(value, dict) else ""
    if not isinstance(chunk_summary, str):
        chunk_summary = ""
    return cleaned, chunk_summary.strip(), problems, invented


def cleanup_stage(speaker_map, drop_labels):
    """Which cleanup stage this chunk is, which is what decides its model.

    The two directions were measured separately and they disagree. On diarized
    multi-speaker material the small model is the only one that clears the gate
    — 7/8 against the baseline's 1/8, 0.12 invented words, 0.99 rare-word
    retention — while the thinking model scores 0/8, rewriting and compressing
    away a quarter of the transcript. On a single voice that reverses: thinking
    takes 2/8 to 8/8 and invented words from 4.38 to 0.00, and the small model
    is blocked by a silent failure.

    So the routing key is the speaker count, which is already known here.
    `drop_labels` means the labels are not used even when several were detected,
    so it reads as single-speaker for this purpose.
    """
    single = drop_labels or len([value for value in (speaker_map or {}).values() if value]) <= 1
    return "clean-transcript-chunk-single" if single else "clean-transcript-chunk-multi"


def clean_one_chunk(args, service, payload, source, speaker_map, drop_labels, tiny, system=CLEANUP_SYSTEM, summarized=False):
    """One chunk. Returns ``(cleaned, summary)``.

    Meaning-first cleanup does not hold a chunk for a human to waive. A chunk that
    clears the gate is returned as-is; one that fails only the invented-words
    ceiling gets the single corrective retry, and if that does not clear it the
    best-effort text is returned anyway — the note-level thinking verify pass reads
    it for meaning and flags it for review if it is actually unfaithful, which is
    where a wholesale rewrite is caught. A *structural* failure that survives the
    retry still raises UserError.

    ``summarized`` marks a chunk of a meeting, whose "cleaned" is minutes rather
    than verbatim cleanup: the invented-words check does not apply (minutes
    paraphrase by design), so it is dropped, leaving only the structural checks.

    Pass ``service=None`` to use the per-chunk route, which is what ordinary
    cleanup wants: the model that cleans a meeting well is not the one that
    cleans a memo well. A caller that names a service gets it, so an escalation
    can still insist on a particular backend.
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    glossary = payload.get("glossary", ())
    stage = cleanup_stage(speaker_map, drop_labels)
    service = service or forge_routing.service_for(stage, args)
    cleaned, summary, problems, invented = clean_chunk_once(
        args, service, messages, source, speaker_map, drop_labels, tiny, stage, glossary
    )
    if summarized:
        # Minutes paraphrase and compress on purpose, so added words are expected;
        # only the structural checks (no timestamp, no level-one heading) apply.
        problems = [problem for problem in problems if not problem.startswith(INVENTED_PROBLEM_PREFIX)]
        invented = []
    if not problems:
        return cleaned, summary
    # A retry that only says "unusable" gets the same answer back; naming the
    # violation is what changes it. Under the spoken-to-written register the
    # violation is almost always the same one -- reaching for a better word than
    # the speaker's -- so when that is what failed, say so in those terms.
    #
    # The rejected answer goes back in as the assistant turn it was. Without it
    # the model is asked to fix words it cannot see and can only regenerate the
    # chunk blind, which is both a full second generation and a fresh roll of
    # the same dice; with it the repair is an edit of text already in context.
    prior_answer = (
        [{"role": "assistant", "content": json.dumps({"cleaned": cleaned, "chunk_summary": summary}, ensure_ascii=False)}]
        if isinstance(cleaned, str) and cleaned.strip()
        else []
    )
    repair = messages + prior_answer + [
        {
            "role": "user",
            "content": f"That response was unusable: {problems[0]}. Return corrected JSON only. "
            + (
                "You rewrote too much of this into your own words. Stay closer to what the "
                "speaker actually said: keep their points, names, and numbers, add nothing that "
                "was not in the chunk, and do not state anything more certainly than they did."
                if problems[0].startswith(INVENTED_PROBLEM_PREFIX)
                else "Clean and condense how they said it in their own voice; do not restate, "
                "describe, or explain what they said, and do not drop any point they made."
            ),
        }
    ]
    cleaned, summary, retry_problems, retry_invented = clean_chunk_once(
        args, service, repair, source, speaker_map, drop_labels, tiny, f"{stage}-repair", glossary
    )
    if not retry_problems:
        return cleaned, summary
    # The retry did not clear it. A structural defect (a kept timestamp, a level-one
    # heading, a speaker label on a solo note) is a hard failure as before. If what
    # remains is only invented words, commit the best-effort text: the note-level
    # thinking verify pass judges its meaning and escalates a genuine rewrite, which
    # is a better use of the reasoning budget than dropping the file or holding it.
    retry_structural = [problem for problem in retry_problems if not problem.startswith(INVENTED_PROBLEM_PREFIX)]
    if retry_structural or not (isinstance(cleaned, str) and cleaned.strip()):
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


# --------------------------------------------------------------------------
# Chat stage: the one-pass writer
# --------------------------------------------------------------------------

# One call per note replaces the classify → per-chunk clean → summarize chain.
# The 2026-08-19 inbox session paid three to four bulk calls per note plus a
# retry economy around per-chunk gates; on the current model a whole recording
# fits one prompt and the register rules transfer intact, so the pipeline's
# structure was the cost, not the work. Whole-note context also removes the
# chunk-boundary failure modes (headings drifting, a turn split mid-sentence)
# that several gates existed to police. Files past WRITE_BUDGET_CHARS still
# chunk (write_one_oversize); everything else is a single call.
WRITE_BUDGET_CHARS = 100_000


def _rules_between(text, start, end=None):
    """A slice of an existing prompt, anchored on its own opening words.

    The writer prompt reuses the classify/cleanup/summary rule bodies verbatim
    rather than keeping a fourth synchronized copy; slicing them out of the
    stage prompts keeps one authoritative text per rule set."""
    index = text.index(start)
    return text[index : text.index(end)].rstrip() if end else text[index:].rstrip()


def writer_system():
    classify_rules = _rules_between(CLASSIFY_SYSTEM, "recording_type - check these in order")
    cleanup_rules = _rules_between(CLEANUP_SYSTEM, "Fidelity rules,", "\n\nChunk context:")
    summary_rules = _rules_between(SUMMARY_SYSTEM, "The reader is the person")
    return f"""You process one voice recording's transcript into its vault note in a single pass: you classify the recording, write its cleaned body, and write its summary, all from one reading.

Return exactly one JSON object and nothing else:
{{"recording_type": "...", "material_role": "...", "title": "...",
 "project": "<codename for a meeting clearly about one, else null>",
 "speakers": {{"<label exactly as given>": {{"who": "...", "kind": "name" | "role" | "unknown", "confidence": "high" | "medium" | "low", "source": "transcript" | "roster", "evidence": "<quote or null>"}}}},
 "effective_speakers": <n>, "spoken_date": "<YYYY-MM-DD or null>", "evidence": "<quote or null>",
 "needs_review": <true | false>, "review_reason": "<why, only when needs_review is true>",
 "cleaned": "<the whole cleaned note body as Markdown>",
 "summary": "<one paragraph, or null when \\"tiny\\" is true>"}}

"transcript" is the complete recording, rendered as speaker turns. There are no
chunks: where the rules below mention a chunk, they mean this whole transcript,
and "chunk_summary" does not exist — the note's summary belongs in "summary".
"settledType" (when given) is the recording type a person already settled for
this note: classify as it says and write "cleaned" in its register, unless the
transcript plainly shows it wrong — then keep your own reading and say why in
"review_reason" with needs_review true.
(The one exception: an oversized recording arrives in parts, marked by
"chunkIndex"/"chunkCount"; then "cleaned" is that part's cleanup, the first part
also carries the classification, and only the final part carries "summary".)

How to classify — recording_type, material_role, title, speakers,
effective_speakers, spoken_date, evidence, needs_review, review_reason:

{classify_rules}

How to write "cleaned". Use "**Name:**" turn labels only for multi-speaker
types, with the names your own "speakers" answer identifies confidently
(otherwise "Speaker 1", "Speaker 2" in order of first appearance); when
effective_speakers is 1, write plain prose with no labels at all. Treat
"structuredFullContent" as true when your material_role is external-source.
"styleByKind" (when given) maps recording_type to the vault owner's own style
rule for that kind — apply the entry for the type you chose. "ownerVoice" (when
given) describes the owner's written register — apply it only when the note is
the owner's own voice (an owner-authored memo or journal):

{cleanup_rules}

How to write "summary" (null when "tiny" is true — the title carries a tiny
note):

{summary_rules}"""


WRITER_SYSTEM = writer_system()


def strip_solo_labels_inline(text):
    """Remove line-leading bold speaker labels from a single-speaker body.

    ``strip_solo_labels`` drops label-only lines; a writer given the whole note
    can also open a paragraph "**Ellian:** the words" — same spurious marker,
    inline form. The label is dropped and the words keep their line."""
    if not isinstance(text, str) or "**" not in text:
        return text
    stripped = strip_solo_labels(text)
    return re.sub(r"^\s*\*\*[^*\n]{1,60}\*\*:?[ \t]*", "", stripped, flags=re.MULTILINE)


def reconcile_written_labels(cleaned, speaker_map, drop_labels):
    """Deterministically align the written body's speaker labels with the map.

    The writer names speakers in the same response that writes the body, so the
    canonical map (policy, roster spelling, duplicate suffixes) is only derivable
    afterwards. Raw labels echoed verbatim are renamed in place — they are
    structurally anchored at line starts, so the rename cannot touch speech.
    Anything else is left for the reviewer rather than re-generated."""
    if not isinstance(cleaned, str):
        return cleaned
    if drop_labels:
        return strip_solo_labels_inline(cleaned)
    for label, display in (speaker_map or {}).items():
        if not display or label == display:
            continue
        pattern = re.compile(rf"^(\s*)\*\*{re.escape(label)}\*\*", re.MULTILINE)
        cleaned = pattern.sub(lambda match: f"{match.group(1)}**{display}**", cleaned)
    return cleaned


def normalize_written(cleaned):
    """Deterministic cleanups the old chunk gate used to hold notes over.

    A kept timestamp line and a level-one heading are never wanted in a cleaned
    body — the raw transcript keeps the clock, and assembly owns the single
    ``# Transcript`` heading — so both are normalized silently instead of
    burning a corrective model turn."""
    if not isinstance(cleaned, str):
        return cleaned
    lines = [line for line in cleaned.split("\n") if not TIMESTAMP_RE.match(line.strip())]
    return "\n".join(f"#{line}" if re.match(r"^#\s", line) else line for line in lines).strip()


def written_advisories(source_text, cleaned, speaker_map, drop_labels, glossary, tiny, summarized):
    """What the old chunk gate measured, kept as signals for the review pass.

    These stopped holding notes: the reviewer reads them next to the raw and the
    note and judges meaning directly. They still get computed because a named
    suspicion ("31 invented words", "labels survived a solo strip") is exactly
    what makes a single review pass sharp."""
    advisories = []
    if not summarized:
        allowed = [] if drop_labels else [value for value in (speaker_map or {}).values() if value]
        allowed = allowed + [offer["term"] for offer in glossary]
        invented = invented_over_ceiling(source_text, cleaned, allowed)
        if invented:
            advisories.append(f"{INVENTED_PROBLEM_PREFIX}: {', '.join(invented[:8])}")
    if tiny and heading_lines(cleaned):
        advisories.append("added headings to a note too short to need them")
    if drop_labels and re.search(r"^\*\*[^*]{1,60}\*\*", cleaned or "", re.MULTILINE):
        advisories.append("speaker labels survived the solo-label strip")
    return advisories


def writer_payload(args, item, parsed, lexicon=None, voice=None):
    """Everything the one-pass writer needs, from one parse.

    Deliberately absent: the personal-context profile. Cards reach only the
    summary's *reading* and the reflection, and those boundaries are structural
    — a prompt that also writes the body must never see them, or "never state a
    card fact as something the recording said" would rest on instruction alone.
    The reflection call (which may see cards) stays separate for that reason."""
    blocks, corrections = correct_blocks(parsed["blocks"], (lexicon or {}).get("terms", []))
    labels = ordered_labels(blocks)
    rendered = render_turns(collapse_turns(blocks, {label: label for label in labels}))
    tiny = item["stats"]["words"] < args.tiny_words
    payload = {
        "filename": Path(item["path"]).name,
        "labels": labels,
        "stats": {
            "blocks": item["stats"]["blocks"],
            "words": item["stats"]["words"],
            "durationSeconds": item["stats"]["duration_seconds"],
        },
        "transcript": rendered,
    }
    if tiny:
        payload["tiny"] = True
    if item["filename_hint"]:
        payload["filenameTitle"] = item["filename_hint"]
    if item.get("label_type"):
        # Reprocessing a filed note: its `date-type-topic` name was settled by a
        # person, and the body must be written in that register.
        payload["settledType"] = item["label_type"]
    if parsed["preamble"].strip():
        payload["handwrittenPreamble"] = parsed["preamble"].strip()[:1000]
    roster = vault_lexicon.candidate_speakers(rendered, (lexicon or {}).get("speakers", []))
    if roster:
        payload["knownSpeakers"] = vault_lexicon.speaker_offers(roster)
    glossary = vault_lexicon.near_miss_terms(rendered, vault_lexicon.term_candidates(lexicon))
    if glossary:
        payload["glossary"] = glossary
    if voice:
        owner_prefix = vault_voice.prompt_prefix(voice, vault_voice.CONTEXT_OWNER)
        if owner_prefix:
            payload["ownerVoice"] = owner_prefix
        style_by_kind = {}
        vocabulary = []
        for recording_type in RECORDING_TYPES:
            if recording_type == "other":
                continue
            context = vault_voice.CONTEXT_SOURCE if recording_type == "lecture" else vault_voice.CONTEXT_OWNER
            compiled = vault_voice.compile_voice(
                voice, context, note_type=TYPE_TO_NOTE_TYPE[recording_type], material=rendered
            )
            if compiled["per_type_rule"]:
                style_by_kind[recording_type] = compiled["per_type_rule"]
            vocabulary = compiled["vocabulary"] or vocabulary
        if style_by_kind:
            payload["styleByKind"] = style_by_kind
        if vocabulary:
            payload["relevantVocabulary"] = vocabulary
    return payload, corrections, tiny, blocks, rendered, glossary


def written_problems(value, item, parsed, tiny, args, roster_names):
    """Contract problems with a writer response, and the validated classification.

    Returns ``(classification, class_warnings, problems)``; validation errors on
    the classification half surface as problems rather than exceptions so one
    corrective re-ask can name everything at once."""
    problems = []
    classification = None
    class_warnings = []
    try:
        classification, class_warnings = validate_classification(value, item, parsed, roster_names)
    except UserError as error:
        problems.append(str(error))
    problems.extend(body_problems(value, tiny, args, classification))
    return classification, class_warnings, problems


def body_problems(value, tiny, args, classification):
    """Contract problems with the cleaned/summary half of a writer response.

    A needs_review classification is a complete answer — the note holds without
    a body — so nothing further is owed. Separated from the classification half
    because the roster-withheld re-read can clear that hold after the fact, and
    the body contract must then be re-judged on its own.
    """
    if classification is not None and classification["needs_review"]:
        return []
    problems = []
    cleaned = value.get("cleaned") if isinstance(value, dict) else None
    if not isinstance(cleaned, str) or not cleaned.strip():
        problems.append("response had no non-blank cleaned text")
    if not (tiny and args.tiny_summary == "omit"):
        problems.extend(check_summary(value.get("summary") if isinstance(value, dict) else None))
    return problems


def write_one(args, vault, item, run_dir, service, lexicon, voice):
    """One note through the writer: call, validate, one corrective re-ask,
    deterministic label/structure normalization, advisory metrics.

    Returns ``(class_record, clean_result, summary_row, warnings)`` in exactly
    the shapes the old three stages produced, so assembly, the review lane, and
    reprocess consume the writer without knowing it exists."""
    warnings = []
    data = (vault / item["path"]).read_bytes()
    parsed = parse_for_item(item, transcript_source(split_frontmatter(data)["body"], vault))
    payload, corrections, tiny, blocks, rendered, glossary = writer_payload(args, item, parsed, lexicon, voice)
    roster_names = [offer["name"] for offer in payload.get("knownSpeakers", [])]
    if len(rendered) > WRITE_BUDGET_CHARS:
        return write_one_oversize(
            args, vault, item, run_dir, service, parsed, payload, corrections, tiny, blocks, roster_names, glossary
        )
    messages = [
        {"role": "system", "content": WRITER_SYSTEM},
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
        task="write-transcript-note",
    )
    classification, class_warnings, problems = written_problems(value, item, parsed, tiny, args, roster_names)
    if problems:
        # One corrective turn naming every violation at once; a second failure
        # is a hold, not a loop.
        repair = messages + [
            {"role": "assistant", "content": json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)},
            {
                "role": "user",
                "content": "That response was unusable: "
                + "; ".join(problems)
                + ". Return the corrected JSON object only, complete, following the contract exactly.",
            },
        ]
        value, _call = forge_llm.call_json_with_retry(
            service,
            repair,
            temperature=0,
            cache_prompt=args.cache_prompt,
            response_format={"type": "json_object"},
            timeout=args.request_timeout,
            api_key=args.api_key,
            task="write-transcript-note-repair",
        )
        classification, class_warnings, problems = written_problems(value, item, parsed, tiny, args, roster_names)
        if problems:
            raise UserError(f"the writer response stayed unusable: {problems[0]}")
    warnings.extend(f"{item['path']}: {warning}" for warning in class_warnings)
    if roster_names and roster_may_have_split_one_voice(classification):
        # The one hold the writer can clear itself: a roster promise split a solo
        # voice in two. Re-read with the roster withheld — before the hold below,
        # because the second answer usually un-holds the note.
        second = classify_without_roster(service, args, item, parsed)
        if second is not None:
            second_classification, second_warnings = second
            warnings.append(
                f"{item['path']}: re-read without the roster answered one speaker; the roster reading was dropped"
            )
            warnings.extend(f"{item['path']}: {warning}" for warning in second_warnings)
            classification = {**classification, **second_classification}
            problems = body_problems(value, tiny, args, classification)
            if problems:
                raise UserError(f"the writer response stayed unusable: {problems[0]}")
    if classification["needs_review"]:
        record = {
            "path": item["path"],
            "sha256": item["sha256"],
            "source": "model",
            "warnings": list(class_warnings),
            **classification,
        }
        return record, None, None, warnings
    labels = ordered_labels(blocks)
    speaker_map, drop_labels = derive_speaker_map(
        labels, classification["speakers"], classification["effective_speakers"], args.speaker_policy, args.owner, lexicon
    )
    cleaned = normalize_written(reconcile_written_labels(value["cleaned"], speaker_map, drop_labels))
    summarized = is_summarized(classification["recording_type"])
    source_text = corrected_source_text(parsed, args, [])
    advisories = written_advisories(source_text, cleaned, speaker_map, drop_labels, glossary, tiny, summarized)
    proposals = accepted_corrections(source_text, cleaned, glossary)
    summary = value.get("summary")
    if summary is not None:
        summary = re.sub(r"\s+", " ", str(summary)).strip() or None
    if tiny and args.tiny_summary == "omit":
        summary = None
    record = {
        "path": item["path"],
        "sha256": item["sha256"],
        "source": "model",
        "warnings": list(class_warnings),
        **classification,
    }
    clean_result = {
        "cleaned": cleaned,
        "chunk_summaries": [],
        "chunks": 1,
        "speaker_map": speaker_map,
        "drop_labels": drop_labels,
        "tiny": tiny,
        "corrections": corrections,
        "proposals": proposals,
        "advisories": advisories,
        "error": None,
    }
    summary_row = {"path": item["path"], "sha256": item["sha256"], "summary": summary, "reflection": None, "skipped": None}
    reflect_warnings = write_reflection(args, vault, service, record, clean_result, summary_row)
    warnings.extend(reflect_warnings)
    return record, clean_result, summary_row, warnings


def write_reflection(args, vault, service, record, clean_result, summary_row):
    """The reflection call for an owner-voiced memo or journal, unchanged from
    the staged pipeline: it is the one prompt allowed to see personal-context
    cards, so it stays its own call rather than joining the writer's."""
    warnings = []
    if voice_context_for(record) != vault_voice.CONTEXT_OWNER or record["recording_type"] not in REFLECTION_SECTIONS:
        return warnings
    body = clean_result["cleaned"]
    candidates, warning = connection_candidates(vault, f"{record['title']} {summary_row['summary'] or ''}".strip())
    if warning:
        warnings.append(f"{record['path']}: {warning}")
    material = (
        transcript_source(source_body(vault, record["path"], body), vault)
        if getattr(args, "reprocessing", False)
        else source_body(vault, record["path"], body)
    )
    sources = outside_sources(material, vault, candidates)
    try:
        reflection, dropped = reflect_note(args, service, record, body, candidates, sources)
    except (forge_llm.ChatError, UserError, ValueError) as error:
        # The reflection is additive; losing it is a warning, never a hold.
        warnings.append(f"{record['path']}: reflection failed and was omitted ({type(error).__name__}: {error})")
        return warnings
    for detail in dropped:
        warnings.append(f"{record['path']}: dropped a connection — {detail}")
    summary_row["reflection"] = reflection
    return warnings


def write_one_oversize(args, vault, item, run_dir, service, parsed, payload, corrections, tiny, blocks, roster_names, glossary):
    """A recording too large for one call: the writer runs per chunk with the
    continuity fields the chunked cleaner used. The first chunk's response
    supplies the classification, every chunk supplies cleaned text, and the
    final chunk supplies the summary."""
    warnings = []
    chunks = chunk_blocks(blocks)
    classification = None
    class_warnings = []
    cleaned_parts = []
    headings = []
    previous_tail = ""
    summary = None
    for index, chunk in enumerate(chunks, start=1):
        labels = ordered_labels(chunk)
        rendered = render_turns(collapse_turns(chunk, {label: label for label in labels}))
        chunk_payload = {
            **payload,
            "transcript": rendered,
            "labels": ordered_labels(blocks),
            "chunkIndex": index,
            "chunkCount": len(chunks),
        }
        if headings:
            chunk_payload["headingsSoFar"] = headings[-12:]
        if previous_tail:
            chunk_payload["previousTail"] = previous_tail[-300:]
        messages = [
            {"role": "system", "content": WRITER_SYSTEM},
            {"role": "user", "content": json.dumps(chunk_payload, ensure_ascii=False)},
        ]
        value, _call = forge_llm.call_json_with_retry(
            service,
            messages,
            temperature=0,
            cache_prompt=args.cache_prompt,
            response_format={"type": "json_object"},
            timeout=args.request_timeout,
            api_key=args.api_key,
            task="write-transcript-note",
        )
        if index == 1:
            classification, class_warnings, problems = written_problems(value, item, parsed, tiny, args, roster_names)
            summary_problems = [problem for problem in problems if "summary" in problem]
            problems = [problem for problem in problems if problem not in summary_problems]
            if problems:
                raise UserError(f"the writer response was unusable on chunk 1: {problems[0]}")
            if classification["needs_review"]:
                record = {
                    "path": item["path"],
                    "sha256": item["sha256"],
                    "source": "model",
                    "warnings": list(class_warnings),
                    **classification,
                }
                return record, None, None, warnings
        cleaned = value.get("cleaned") if isinstance(value, dict) else None
        if not isinstance(cleaned, str) or not cleaned.strip():
            raise UserError(f"chunk {index} returned no cleaned text")
        cleaned_parts.append(cleaned)
        headings.extend(heading_lines(cleaned))
        previous_tail = cleaned[-300:]
        if index == len(chunks):
            summary = value.get("summary")
    warnings.extend(f"{item['path']}: {warning}" for warning in class_warnings)
    labels = ordered_labels(blocks)
    lexicon = getattr(args, "compiled_lexicon", None)
    speaker_map, drop_labels = derive_speaker_map(
        labels, classification["speakers"], classification["effective_speakers"], args.speaker_policy, args.owner, lexicon
    )
    body = "\n\n".join(part.strip() for part in cleaned_parts).strip()
    cleaned = normalize_written(reconcile_written_labels(body, speaker_map, drop_labels))
    summarized = is_summarized(classification["recording_type"])
    source_text = corrected_source_text(parsed, args, [])
    advisories = written_advisories(source_text, cleaned, speaker_map, drop_labels, glossary, tiny, summarized)
    advisories.append(f"oversize: written in {len(chunks)} chunks")
    if summary is not None:
        summary = re.sub(r"\s+", " ", str(summary)).strip() or None
    if not summary and not (tiny and args.tiny_summary == "omit"):
        problems = check_summary(summary)
        warnings.append(f"{item['path']}: the final chunk's summary was unusable ({problems[0]}); left for the reviewer")
    record = {
        "path": item["path"],
        "sha256": item["sha256"],
        "source": "model",
        "warnings": list(class_warnings),
        **classification,
    }
    clean_result = {
        "cleaned": cleaned,
        "chunk_summaries": [],
        "chunks": len(chunks),
        "speaker_map": speaker_map,
        "drop_labels": drop_labels,
        "tiny": tiny,
        "corrections": corrections,
        "proposals": accepted_corrections(source_text, cleaned, glossary),
        "advisories": advisories,
        "error": None,
    }
    summary_row = {"path": item["path"], "sha256": item["sha256"], "summary": summary, "reflection": None, "skipped": None}
    reflect_warnings = write_reflection(args, vault, service, record, clean_result, summary_row)
    warnings.extend(reflect_warnings)
    return record, clean_result, summary_row, warnings


def write_notes(args, vault, items, run_dir, skip):
    """Every transcript through the one-pass writer, journaled per note.

    Returns ``(class_records, clean_results, summaries, warnings)`` — the same
    three dicts the classify/clean/summarize stages produced."""
    journal_path = run_dir / "written.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    journal = {(row["path"], row["sha256"]): row for row in prior if row.get("path") and row.get("sha256")}
    artifacts = run_dir / "written"
    artifacts.mkdir(exist_ok=True)
    service = chat_service(args)
    lexicon = getattr(args, "compiled_lexicon", None)
    voice = getattr(args, "compiled_voice", None)
    warnings = []
    class_records = {}
    clean_results = {}
    summaries = {}
    pending = [item for item in items if item["is_transcript"] and item["path"] not in skip]
    total = len(pending)
    durations = []
    progress_lock = threading.Lock()
    done = 0

    def restore(row):
        record = dict(row["record"])
        clean_result = None
        summary_row = None
        if row.get("clean") is not None:
            clean_result = dict(row["clean"])
            clean_result["cleaned"] = (artifacts / row["artifact"]).read_text(encoding="utf-8")
        if row.get("summary_row") is not None:
            summary_row = dict(row["summary_row"])
        return record, clean_result, summary_row

    def write_and_journal(item):
        nonlocal done
        key = (item["path"], item["sha256"])
        row = journal.get(key)
        if row is not None and row.get("status") == "ok":
            with progress_lock:
                done += 1
            return item["path"], restore(row)
        if row is not None and row.get("status") == "failed" and not getattr(args, "retry_failed", False):
            # Inherited rather than retried, exactly as the chunked cleaner
            # journaled failures: a resume must not silently re-pay a refusal.
            # --retry-failed re-attempts it deliberately.
            with progress_lock:
                done += 1
                warnings.append(f"{item['path']}: writing failed on a previous attempt ({row.get('error')}); "
                                "pass --retry-failed to try again")
            failed = dict(row.get("record") or {})
            return item["path"], (failed or None, None, None)
        started = time.time()
        try:
            record, clean_result, summary_row, note_warnings = write_one(
                args, vault, item, run_dir, service, lexicon, voice
            )
        except InterruptedError as error:
            raise UserError(
                f"writing was preempted by interactive activity: {error}. The run is checkpointed "
                f"— resume it with `process --vault {vault} --run {run_dir}`, or run the resume in "
                "the foreground so its review does not compete with the interactive session"
            ) from error
        except (forge_llm.ChatError, UserError, OSError, UnicodeDecodeError, ValueError) as error:
            message = f"{type(error).__name__}: {error}"
            failed_record = {
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
                "review_reason": f"writing failed: {message}",
            }
            elapsed = round(time.time() - started, 3)
            with progress_lock:
                done += 1
                warnings.append(f"{item['path']}: writing failed ({message})")
                run_state.append_jsonl_fsync(
                    journal_path,
                    {"path": item["path"], "sha256": item["sha256"], "status": "failed", "error": message,
                     "record": failed_record, "seconds": elapsed},
                )
                progress(f"[write {done}/{total}] {item['path']} FAILED: {message}")
            return item["path"], (failed_record, None, None)
        elapsed = round(time.time() - started, 3)
        entry = {
            "path": item["path"],
            "sha256": item["sha256"],
            "status": "ok",
            "record": record,
            "seconds": elapsed,
            "routing": forge_routing.routing_record(service),
        }
        if clean_result is not None:
            name = f"{sha256_text(item['path'])[:12]}.md"
            (artifacts / name).write_text(clean_result["cleaned"], encoding="utf-8")
            entry["artifact"] = name
            entry["clean"] = {key: value for key, value in clean_result.items() if key != "cleaned"}
            entry["clean"]["cleaned_sha256"] = sha256_text(clean_result["cleaned"])
        if summary_row is not None:
            entry["summary_row"] = summary_row
        with progress_lock:
            done += 1
            warnings.extend(note_warnings)
            run_state.append_jsonl_fsync(journal_path, entry)
            durations.append(elapsed)
            remaining = (sum(durations) / len(durations)) * (total - done) / max(1, workers)
            eta = format_duration(remaining) if durations else "-"
            progress(f"[write {done}/{total}] {item['path']} → {record.get('recording_type')} ({elapsed:.1f}s, eta {eta})")
        return item["path"], (record, clean_result, summary_row)

    workers = max(1, min(int(getattr(args, "jobs", 1) or 1), total or 1))
    results = {}
    if workers == 1:
        for item in pending:
            path, produced = write_and_journal(item)
            results[path] = produced
    else:
        queue = list(enumerate(pending))
        queue_lock = threading.Lock()
        failures = []

        def worker():
            while True:
                with queue_lock:
                    if not queue or failures:
                        return
                    _index, item = queue.pop(0)
                try:
                    path, produced = write_and_journal(item)
                except BaseException as error:  # noqa: BLE001 - carried to the main thread
                    failures.append(error)
                    return
                with queue_lock:
                    results[path] = produced
        threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        if failures:
            raise failures[0]
    for path, (record, clean_result, summary_row) in results.items():
        if record:
            class_records[path] = record
        if clean_result is not None:
            clean_results[path] = clean_result
        if summary_row is not None:
            # A tiny note whose summary was omitted keeps the row shape the old
            # summarize stage produced.
            if summary_row["summary"] is None and clean_result and clean_result["tiny"] and args.tiny_summary == "omit":
                summaries[path] = {**summary_row, "skipped": "tiny"}
            else:
                summaries[path] = summary_row
    return class_records, clean_results, summaries, warnings


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
    return sorted(chosen, key=lambda block: block["seconds"] if block["seconds"] is not None else 0)


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


def raw_metadata(schema, recording_type, processed_stem, date=None):
    """Frontmatter for the recording's own note.

    Deliberately not ``processed_by``: nothing processed this note, that is the
    whole point of it. ``parent`` is the only tie back to the note that was made
    from it, and filing replaces frontmatter wholesale, so the organizer carries
    it forward with ``type`` and ``source_kind``.

    ``date`` is the day of the recording, which for a recording is unambiguously
    both when it was made and what it is about.
    """
    metadata = {
        "type": "source",
        "status": RAW_STATUS,
        "source_kind": RAW_SOURCE_KIND,
        "capture_type": TYPE_TO_CAPTURE[recording_type],
        "parent": f"[[{processed_stem}]]",
        "date": date or datetime.date.today().isoformat(),
    }
    if metadata["status"] not in schema["statuses"]:
        metadata["status"] = "raw" if "raw" in schema["statuses"] else next(iter(schema["statuses"]))
    if metadata["capture_type"] not in schema["capture_types"]:
        metadata.pop("capture_type")
    return {key: value for key, value in metadata.items() if key in schema["properties"]}


def build_raw_note(schema, metadata, raw_body):
    """The recording, with frontmatter and nothing else added."""
    return serialize_frontmatter(metadata, schema) + "\n" + raw_body


def parent_basename_bytes(data):
    """Reduce a recording note's ``parent`` link to a bare basename wikilink.

    Returns ``(new_bytes, old_target, basename)`` with only the ``parent:`` line
    rewritten to ``[[<basename>]]`` -- every other byte preserved -- or ``None``
    when the link is already a basename, there is no ``parent`` to touch, or the
    frontmatter is unreadable.

    The recording's ``parent`` is a uniquely-named processed note, so a basename
    link resolves it wherever the note is later filed. A directory-qualified
    target -- what Obsidian's link-safe move leaves behind when it rewrites the
    link while the processed note is momentarily ambiguous with its staged review
    copy -- points at that transient location and goes stale the moment the note
    moves on. Normalizing back to the basename is what survives filing.
    """
    had_bom = data.startswith(b"\xef\xbb\xbf")
    payload = data[3:] if had_bom else data
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None
    closing = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == "---"), None)
    if closing is None:
        return None
    for index in range(1, closing):
        match = FRONTMATTER_KEY_RE.match(lines[index].rstrip("\r\n"))
        if not match or match.group(1) != "parent":
            continue
        target = wikilink_target(strip_yaml_scalar(match.group(2).strip()))
        if not target:
            return None  # not a wikilink this pass understands; leave it be
        basename = link_basename(target)
        if not basename or basename == target:
            return None  # already a bare basename
        stripped = lines[index].rstrip("\r\n")
        ending = lines[index][len(stripped):] or "\n"
        lines[index] = f"parent: {yaml_quote('[[' + basename + ']]')}{ending}"
        out = "".join(lines).encode("utf-8")
        return (b"\xef\xbb\xbf" + out if had_bom else out), target, basename
    return None


TRANSCRIPT_MARKER = "\n\n# Transcript\n\n"

# The fixed minutes sections a meeting always ends with -- they are structure,
# not a subject the meeting covered, so the at-a-glance "Topics" line skips them.
MEETING_FIXED_HEADINGS = frozenset({"decisions", "action items"})
GENERIC_SPEAKER_DISPLAY_RE = re.compile(r"^Speaker \d+(?: \(\d+\))?$")


def meeting_topics(cleaned):
    """The minutes' own ``##`` topic headings, in order, minus the fixed sections.

    Read from the body the model just wrote rather than asked for separately, so
    the at-a-glance block can never name a topic the note does not actually cover.
    """
    topics = []
    for line in cleaned.splitlines():
        match = re.match(r"^##\s+(.+?)\s*$", line)
        if not match:
            continue
        heading = match.group(1).strip()
        if heading.casefold() in MEETING_FIXED_HEADINGS or heading in topics:
            continue
        topics.append(heading)
    return topics


def meeting_attendees(speaker_map):
    """Distinct named attendees from the speaker map, dropping the unnamed voices.

    The same names the body uses, so the block agrees with the minutes. A voice
    the pipeline could only call "Speaker 2" is not an attendance record, so it is
    left out rather than listed as an unknown.
    """
    names = []
    for display in (speaker_map or {}).values():
        if not display or GENERIC_SPEAKER_DISPLAY_RE.match(display):
            continue
        if display not in names:
            names.append(display)
    return names


def meeting_brief(record, date, time_hhmm, cleaned, speaker_map):
    """A compact at-a-glance callout for a meeting note, or ``None`` for nothing.

    Assembled from what the pipeline already settled -- the project it named, the
    recording's date and time, the speakers it identified, and the minutes' own
    topic headings -- so it never disagrees with the note it sits above and never
    needs a field the model was made to invent. Every line is optional; when none
    survive there is no block at all, never an empty one.
    """
    lines = []
    project = record.get("project")
    if project:
        lines.append(f"**Project:** {project}")
    if date:
        lines.append(f"**Date:** {date}")
    if time_hhmm and len(time_hhmm) >= 4 and time_hhmm[:4].isdigit():
        lines.append(f"**Time:** {time_hhmm[:2]}:{time_hhmm[2:4]}")
    attendees = meeting_attendees(speaker_map)
    if attendees:
        lines.append(f"**Attendees:** {', '.join(attendees)}")
    topics = meeting_topics(cleaned)
    if topics:
        lines.append(f"**Topics:** {', '.join(topics)}")
    if not lines:
        return None
    return render_callout("info", "Meeting", lines, collapsed=False)


def assemble_head(summary, style, preamble, cleaned, reflection=None, brief=None):
    """The generated section of a note, ending at the ``# Transcript`` marker.

    Generated material sits at the top, together and in callouts, and the
    speaker's own words below it: what the machine made of a recording is worth
    a glance before the recording itself, and it should never be mistaken for
    the recording. The handwritten preamble stays next to the cleaned text
    because it is the owner's writing too, not apparatus. A meeting's at-a-glance
    ``brief`` leads even the summary, because it is the fastest read of all.
    """
    sections = []
    if brief:
        sections.append(brief.strip())
    if summary:
        sections.append(render_summary(summary, style))
    if reflection:
        sections.append(reflection.strip())
    if preamble.strip():
        sections.append(preamble.strip())
    sections.append(cleaned.strip())
    return "\n\n".join(sections) + TRANSCRIPT_MARKER


def build_note(schema, metadata, summary, style, preamble, cleaned, raw_body, reflection=None, raw_stem=None, brief=None):
    """Assemble the final note.

    Everything above the ``# Transcript`` marker is generated. Below it goes
    either the recording itself, byte for byte, or -- when the recording is
    getting its own note -- a link to it and nothing else. Splitting them keeps a
    processed note readable at the length of what it says rather than the length
    of what was said, and lets the recording be filed as the source it is instead
    of riding along inside a note about it.
    """
    head = serialize_frontmatter(metadata, schema) + "\n" + assemble_head(summary, style, preamble, cleaned, reflection, brief)
    if raw_stem is None:
        return head + raw_body, head
    return head + f"[[{raw_stem}]]\n", head


def frontmatter_metadata(schema, recording_type, date=None):
    metadata = {
        "type": TYPE_TO_NOTE_TYPE[recording_type],
        "status": "raw",
        "capture_type": TYPE_TO_CAPTURE[recording_type],
    }
    # The recording's own date, not today's: a transcript processed a week late
    # is still about the day it was spoken. Falls back to today only when the
    # filename carried no date and the speaker named none. Written here because
    # `date` is human-owned, so filing can carry it forward but never supply it.
    metadata["date"] = date or datetime.date.today().isoformat()
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


def check_note(
    item, cleaned, summary, note_text, head, parsed, args,
    proposals=(), raw_text=None, raw_stem=None, tail=None, summarized=False,
):
    """Every deterministic check that can fail a note, in one place.

    Returns ``(structural, fidelity, measurements)``. Both lists hold the note
    back, but for different reasons and to different ends:

    - *structural* problems are contract violations the meaning-judge has no say
      over — a wrong heading, a transcript section that is not the source bytes,
      a malformed summary, a dropped preamble. These hold the note immediately.
    - *fidelity* problems are the three verbatim-gate proxies (length ratio,
      rare-word retention, the utterance locator). They measure word overlap, not
      meaning, so a legitimately restructured note trips them without losing
      anything. A note held by these *alone* is marked provisional and deferred to
      the thinking meaning-judge rather than held outright — see ``assemble_items``.

    ``summarized`` marks a meeting, whose note is minutes rather than verbatim
    cleanup. The verbatim (fidelity) checks do not apply to a summary and are
    skipped, so ``fidelity`` is always empty for one; the structural checks still
    run, and the note-level thinking review judges the minutes' substance.
    """
    structural = []
    fidelity = []
    heads = [line for line in head.splitlines() if re.match(r"^#\s", line.strip())]
    if heads != ["# Transcript"]:
        structural.append(f"generated section must contain exactly one level-one heading (found {heads})")
    # Whichever note ends up holding the recording holds all of it. The pair only
    # loses nothing if the processed note points at the recording and the
    # recording note is the source bytes with frontmatter in front. Reprocessing
    # rewrites neither -- it reattaches the section it found, whatever shape it
    # was in -- so there the check is that the section came back untouched.
    if tail is not None:
        if not note_text.endswith(tail):
            structural.append("the transcript section was not reattached unchanged")
    elif raw_text is None:
        if not note_text.endswith(item["raw_body"]):
            structural.append("raw transcript section is not byte-identical to the source body")
    else:
        if not raw_text.endswith(item["raw_body"]):
            structural.append("raw transcript note is not byte-identical to the source body")
        if not note_text.endswith(f"# Transcript\n\n[[{raw_stem}]]\n"):
            structural.append("transcript section must hold exactly one link to the raw transcript note")
    source_text = corrected_source_text(parsed, args, proposals)
    source_words = len(source_text.split())
    # Prose only. A short dialogue carries one "**Name:**" per turn, and counting
    # those as content makes faithful cleanup of a chatty exchange look padded.
    cleaned_words = len(strip_structure(cleaned).split())
    ratio = cleaned_words / source_words if source_words else 1.0
    floor = TINY_RATIO_MIN if item["stats"]["words"] < args.tiny_words else CLEANED_RATIO_MIN
    # Minutes compress and drop wording by design, so the length-ratio,
    # rare-word, and utterance-locatable checks would fail every one of them.
    # They are the verbatim gate, and a summary is not verbatim.
    if not summarized and source_words and not floor <= ratio <= CLEANED_RATIO_MAX:
        fidelity.append(f"cleaned length is {ratio:.2f}x the source, outside {floor}-{CLEANED_RATIO_MAX}")
    retention, missing = rare_word_retention(source_text, cleaned)
    if not summarized and retention < RARE_WORD_RETENTION:
        fidelity.append(
            f"only {retention:.0%} of distinctive source words survived cleanup (missing: {', '.join(missing[:8])})"
        )
    if summary:
        if "\n\n" in summary.strip():
            structural.append("summary is more than one paragraph")
        if len(summary.split()) > SUMMARY_MAX_WORDS:
            structural.append(f"summary is {len(summary.split())} words, over the {SUMMARY_MAX_WORDS}-word limit")
    if parsed["preamble"].strip() and parsed["preamble"].strip() not in head:
        structural.append("handwritten preamble did not survive into the generated section")
    cleaned_word_list = content_words(cleaned)
    weak = []
    if not summarized:
        for block in fidelity_samples(item["path"], parsed["blocks"]):
            score, _at = best_containment(block["text"], cleaned_word_list)
            if score < FIDELITY_MIN_CONTAINMENT:
                weak.append({"seconds": block["seconds"], "score": round(score, 3), "text": block["text"][:200]})
    if weak:
        fidelity.append(f"{len(weak)} sampled utterance(s) could not be located in the cleaned text")
    return structural, fidelity, {
        "cleaned_ratio": round(ratio, 3),
        "rare_word_retention": round(retention, 3),
        "weak_samples": weak,
        "source_words": source_words,
        "cleaned_words": cleaned_words,
    }


def base_record(item):
    stats = item["stats"]
    warnings = []
    if isinstance(stats, dict) and stats.get("timestamp_style") == "HH:MM":
        # The report should say what was assumed: a two-part timestamp read as
        # minutes rather than seconds because the seconds reading was impossible.
        warnings.append(
            "timestamps read as HH:MM (elapsed minutes), not MM:SS — the seconds reading "
            "implied an impossible speaking rate"
        )
    if item.get("clock_broken"):
        warnings.append(f"timestamps ignored — {item['clock_broken']}")
    return {
        # The parse mode travels with the record so every later stage — including
        # a --from-review apply reading plan.json — parses the raw the way the
        # scan did (see parse_for_item).
        "unlabeled": bool(item.get("unlabeled")),
        "no_timestamps": bool(item.get("no_timestamps")),
        "clock_broken": item.get("clock_broken"),
        "source": item["path"],
        "source_hash": item["sha256"],
        "destination": None,
        "action": "none",
        "status": "ok",
        "needs_review": False,
        "review_reason": None,
        "warnings": warnings,
        "recording_type": None,
        "title": None,
        "summary": None,
        "stats": stats,
    }


def review_record(item, reason, status="review", warning=None, reason_code=None):
    record = base_record(item)
    record["status"] = status
    record["needs_review"] = True
    record["review_reason"] = reason
    if reason_code:
        # One of REVIEW_REASON_CODES: a hold a model or person can triage
        # without re-deriving what kind of problem it is.
        record["reason_code"] = reason_code
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
            vault, INBOX_DIR, args, date, item["time_hhmm"], record["recording_type"], record["title"], taken, rel,
            project=record.get("project"),
        )
        raw_stem = raw_note_stem(Path(destination).name) if split_raw else None
        raw_destination = assign_raw_name(vault, INBOX_DIR, raw_stem, taken) if raw_stem else None
        try:
            data = (vault / rel).read_bytes()
            if sha256_bytes(data) != item["sha256"]:
                records.append(review_record(item, "note changed on disk during this run"))
                continue
            raw_body = transcript_source(split_frontmatter(data)["body"], vault)
            parsed = parse_for_item(item, raw_body)
            metadata = frontmatter_metadata(schema, record["recording_type"], date)
            brief = (
                meeting_brief(record, date, item["time_hhmm"], cleaned_result["cleaned"], cleaned_result.get("speaker_map"))
                if record["recording_type"] == "meeting"
                else None
            )
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
                brief=brief,
            )
            raw_text = (
                build_raw_note(
                    schema, raw_metadata(schema, record["recording_type"], Path(destination).stem, date), raw_body
                )
                if raw_stem
                else None
            )
            structural, fidelity, measurements = check_note(
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
                summarized=is_summarized(record["recording_type"]),
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
        assembled["advisories"] = cleaned_result.get("advisories") or []
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
        assembled["checks"] = structural + fidelity
        assembled["classification_source"] = record["source"]
        assembled["warnings"] = list(record.get("warnings") or [])
        if not date:
            assembled["warnings"].append("no recording date in the filename; the new name has no date prefix")
        if record["spoken_date"] and item["date"] and record["spoken_date"] != item["date"]:
            assembled["warnings"].append(
                f"transcript says {record['spoken_date']} but the filename says {item['date']}; kept the filename date"
            )
        # A structural problem is a hard hold. Fidelity problems alone are not a
        # verdict — they are a word-overlap proxy that a restructured note trips
        # without losing meaning — so the note is written and marked provisional
        # for the thinking review pass (`review_and_fix_notes`) to clear, fix, or hold. The
        # one exception is `--no-verify`: with no judge to defer to, an unjudged
        # note must never read as approved, so the floor holds it as before.
        if structural or (fidelity and not args.verify):
            held = structural + fidelity
            assembled["status"] = "review"
            assembled["needs_review"] = True
            assembled["review_reason"] = "; ".join(held)
            assembled["warnings"].extend(held)
            records.append(assembled)
            continue
        if fidelity:
            assembled["fidelity_provisional"] = fidelity
            assembled["warnings"].append(
                "held only by the deterministic fidelity floor; deferred to the thinking meaning-judge: "
                + "; ".join(fidelity)
            )
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
    name never lands on an existing note.

    The review lane's own surface is excluded: the staged proposals in
    `_Pending Review/` carry the very names being assigned, so counting them as
    taken would bump every destination to a `… 1` twin and break the match
    between a record and its review-note checkbox at apply time.
    """
    root = vault / INBOX_DIR
    names = set()
    if not root.is_dir():
        return names
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirpath = Path(directory)
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if not name.startswith(".") and name != vault_review.PENDING_DIRNAME
        ]
        for filename in filenames:
            if dirpath == root and filename == vault_review.REVIEW_NOTE_NAME:
                continue
            names.add(relative_path(vault, dirpath / filename).casefold())
    return names


# --------------------------------------------------------------------------
# Daily log
# --------------------------------------------------------------------------

# Which recordings a day's log may absorb. A memo and a journal entry are pages
# of someone's day; a meeting, a conversation, a lecture and a therapy session
# are each a document in their own right, and folding one into a to-do list
# destroys it. Therapy is additionally carved out of reprocessing elsewhere for
# the same reason.
DAILY_TYPES = ("memo", "journal")
DAILY_ROLE = "owner-authored"
DEFAULT_DAILY_MIN_RECORDINGS = 2
# The heading the day's recordings are listed under. Not a declared grammar
# block: it is a plain `##` section, legal under `body`, and the note format's
# `journal` row is where a shape convention belongs rather than the block table.
SOURCE_RECORDINGS_HEADING = "Source Recordings"
# `2026-08-03` -> `# August 3 — …`. An em dash, matching the hand-built log.
DAILY_TITLE_DASH = "—"
# `(~10:39–10:41)`. An en dash: it is a range, not a break.
DAILY_RANGE_DASH = "–"


def group_daily(items, records, minimum=DEFAULT_DAILY_MIN_RECORDINGS):
    """Same-day owner recordings that belong in one log, in the order spoken.

    The grouping key is deliberately the *day*, not similarity of content. The
    hand-built log this reproduces spans a qualitative-coding tool, a to-do list,
    groceries and two feature ideas -- a similarity threshold would have split
    exactly the group a person sat down and made on purpose. What makes a day's
    memos one note is that they are one day's thinking, not one topic.

    Returns ``[{"date", "recording_type", "members": [item, ...]}]``; days with
    fewer than ``minimum`` recordings are not groups and fall through to ordinary
    per-recording processing.
    """
    buckets = {}
    for item in items:
        if not item.get("is_transcript") or not item.get("date"):
            continue
        record = records.get(item["path"])
        if not record:
            continue
        if record.get("recording_type") not in DAILY_TYPES:
            continue
        # A recording with another person in it is not a page of someone's day.
        if record.get("material_role") != DAILY_ROLE:
            continue
        buckets.setdefault((item["date"], record["recording_type"]), []).append(item)
    groups = []
    for (date, recording_type), members in sorted(buckets.items()):
        if len(members) < minimum:
            continue
        members.sort(key=lambda item: (item.get("time_hhmmss") or "", item["path"]))
        groups.append({"date": date, "recording_type": recording_type, "members": members})
    return groups


def _clock_seconds(time_hhmmss):
    if not time_hhmmss:
        return 0
    hours, minutes, seconds = (int(part) for part in str(time_hhmmss).split(":"))
    return hours * 3600 + minutes * 60 + seconds


def merge_transcripts(members, parsed_by_path):
    """One recording's blocks after another, rebased onto wall-clock time.

    Each member's ``*MM:SS*`` offsets are relative to its own start, so
    concatenating them raw produces a transcript that restarts at zero once per
    recording. Rebasing onto the filename's start stamp gives one monotone clock,
    which is both what makes the merged text readable as a day and what lets a
    section's ``(~HH:MM)`` marker be computed rather than guessed.

    Returns ``[{"unit_id", "path", "clock", "seconds", "speaker", "text"}]``.
    """
    merged = []
    for position, item in enumerate(members, start=1):
        unit_id = "s-%04d" % position
        base = _clock_seconds(item.get("time_hhmmss"))
        for block in parsed_by_path[item["path"]]["blocks"]:
            absolute = base + int(block.get("seconds") or 0)
            merged.append(
                {
                    "unit_id": unit_id,
                    "path": item["path"],
                    "seconds": absolute,
                    "clock": "%02d:%02d:%02d" % (absolute // 3600, (absolute % 3600) // 60, absolute % 60),
                    "speaker": block.get("speaker"),
                    "text": block.get("text", ""),
                }
            )
    return merged


def render_merged_transcript(merged):
    """The merged day as one transcript, with a divider naming each recording.

    This is what cleanup reads. The divider is what lets the composing call map a
    section back to the recordings it came from -- without it the day is one
    undifferentiated wall and every `(~HH:MM)` marker would be a guess.
    """
    lines = []
    current = None
    for block in merged:
        if block["unit_id"] != current:
            current = block["unit_id"]
            if lines:
                lines.append("")
            lines.append(f"--- recording {current} ---")
            lines.append("")
        lines.append(f"*{block['clock']}*")
        lines.append(block["text"])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def daily_marker(members, unit_ids):
    """``(~09:36)`` or ``(~10:39–10:41)`` for the recordings a section came from.

    Computed from the filename start stamps, never from the model: a time is a
    fact about a file, and asking for it invites an invented one. Equal endpoints
    collapse to a single marker rather than reading as a zero-length range.
    """
    by_id = {"s-%04d" % position: item for position, item in enumerate(members, start=1)}
    stamps = sorted(
        (by_id[unit_id].get("time_hhmmss") or "")[:5] for unit_id in unit_ids if unit_id in by_id
    )
    stamps = [stamp for stamp in stamps if stamp]
    if not stamps:
        return ""
    if stamps[0] == stamps[-1]:
        return f"(~{stamps[0]})"
    return f"(~{stamps[0]}{DAILY_RANGE_DASH}{stamps[-1]})"


def daily_title(date, title):
    """``2026-08-03`` + ``Thoughts & To-Dos`` -> ``August 3 — Thoughts & To-Dos``.

    The note's own heading names the day in words; the filename carries the ISO
    date. Obsidian shows a filename above the note, so repeating `2026-08-03`
    inside it would say the same thing twice in two formats.
    """
    parsed = datetime.date.fromisoformat(date)
    spoken = f"{parsed.strftime('%B')} {parsed.day}"
    return f"{spoken} {DAILY_TITLE_DASH} {title}"


def daily_source_lines(members, raw_names):
    """The `## Source Recordings` bullets, in the order the day was spoken.

    Basename wikilinks, not the full paths the hand-built log used: filing moves
    a recording into the sources tree, and a full-path link does not survive that
    while a basename one does.
    """
    lines = []
    for position, item in enumerate(members, start=1):
        unit_id = "s-%04d" % position
        stem = Path(raw_names[item["path"]]).stem
        marker = daily_marker(members, [unit_id])
        lines.append(f"- [[{stem}]] {marker}".rstrip())
    return lines


def daily_metadata(schema, date):
    """Frontmatter for a day's log.

    ``capture_type`` records the channel the material arrived by -- these were
    spoken -- and the machine's hand in the note is recorded by the mandatory
    provenance block instead. Overloading one property to mean both leaves a
    reader unable to tell a voice log from a research note without opening it.
    """
    metadata = {"type": "journal", "status": "raw", "capture_type": "voice", "date": date}
    if metadata["type"] not in schema["types"]:
        raise UserError("schema does not define note type 'journal'; a day's log cannot be written")
    if metadata["status"] not in schema["statuses"]:
        metadata["status"] = "raw" if "raw" in schema["statuses"] else next(iter(schema["statuses"]))
    if metadata["capture_type"] not in schema["capture_types"]:
        metadata.pop("capture_type")
    return {key: value for key, value in metadata.items() if key in schema["properties"]}


def daily_provenance(group, sources, run_directory):
    """How the log was made. Written by code, never by the model.

    `0.04 Note Format.md` requires this block to be accurate about what made a
    note, and a model cannot be accurate about that.
    """
    lines = [
        f"Composed from {len(group['members'])} {group['recording_type']} recordings made on {group['date']}, "
        "merged into one transcript and cleaned as a whole.",
        "",
        f"Source set `{sources['fingerprint'][:12]}`; run `{Path(run_directory).name}`.",
    ]
    return {"title": "How this note was made", "lines": lines}


def build_daily_note(fmt, schema, group, composition, sources, raw_names, run_directory):
    """A day's log, assembled in the order the vault's note format declares.

    The model supplies a title, a summary paragraph, and which recordings each
    section drew on. Everything that has to be exact -- the heading, the time
    markers, the recording list, the provenance -- is written here.
    """
    members = group["members"]
    body = []
    for section in composition["sections"]:
        marker = daily_marker(members, section.get("sourceIds") or [])
        heading = f"{section['heading']} {marker}".strip()
        body.append({"heading": heading, "lines": section["lines"]})
    body.append({"heading": SOURCE_RECORDINGS_HEADING, "lines": daily_source_lines(members, raw_names)})
    blocks = {
        "title": daily_title(group["date"], composition["title"]),
        "body": body,
        "provenance": daily_provenance(group, sources, run_directory),
    }
    if composition.get("summary"):
        # A plain paragraph, not a `> [!summary]` callout: the note format's
        # `journal` row asks for the owner's language first, and the hand-built
        # log leads with prose.
        blocks["body"] = [composition["summary"], ""] + blocks["body"]
    return vault_compose.render_note(fmt, schema, daily_metadata(schema, group["date"]), blocks)


COMPOSE_DAILY_SYSTEM = """You organize one day of a person's voice memos into the sections of a single note.

The transcript you get is that person's whole day, several recordings merged into
one and already cleaned. Dividers reading `--- recording s-0001 ---` mark where
each recording begins. Your job is only to decide the shape: what the day should
be called, one paragraph saying what it covered, and which topics it breaks into.

Rules:
- Group by topic, not by recording. One topic that spans three recordings is one
  section; one recording holding three topics becomes three sections.
- A section's `sourceIds` are every recording that contributed to it, in order.
- Do not invent a topic the day did not contain, and do not drop one it did.
- Write nothing for the sections themselves except the text belonging to them.
  Keep the speaker's own words and their own register; condense by dropping
  words, never by replacing one with a word you prefer.
- Headings are sentence case, name what is under them, and carry no timestamp:
  times are added afterwards from the recording files.
- The summary is one paragraph naming what the day covered. No preamble.

Return JSON only:
{"title": "...", "summary": "...", "sections": [{"heading": "...", "sourceIds": ["s-0001"], "lines": ["..."]}]}"""


def compose_daily(args, vault, group, cleaned, sources):
    """One chat call deciding a day's shape. Everything exact is written in code.

    The model gets the cleaned day and returns a title, a summary, and topical
    sections with the recordings each rests on. It is never asked for a time, a
    filename, or a link: those are facts about files, and asking invites an
    invented one.
    """
    payload = {
        "date": group["date"],
        "recordingCount": len(group["members"]),
        "recordingIds": [unit["id"] for unit in sources["units"]],
        "day": cleaned,
    }
    compiled = vault_voice.compile_voice(
        getattr(args, "compiled_voice", None),
        vault_voice.CONTEXT_OWNER,
        note_type="journal",
        material=cleaned,
    )
    if compiled["per_type_rule"]:
        payload["styleForThisKind"] = compiled["per_type_rule"]
    if compiled["vocabulary"]:
        payload["relevantVocabulary"] = compiled["vocabulary"]
    value, _call = forge_llm.call_json_with_retry(
        chat_service(args),
        [
            {"role": "system", "content": COMPOSE_DAILY_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        cache_prompt=args.cache_prompt,
        response_format={"type": "json_object"},
        timeout=args.request_timeout,
        api_key=args.api_key,
        task="compose-daily-log",
    )
    return validate_daily_composition(value, sources)


def validate_daily_composition(value, sources):
    """The composing response, or a UserError naming what was wrong with it."""
    if not isinstance(value, dict):
        raise UserError("composing response was not an object")
    title = validate_title(value.get("title"))
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise UserError("composing response has no summary")
    raw_sections = value.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise UserError("composing response has no sections")
    known = {unit["id"] for unit in sources["units"]}
    sections = []
    seen = set()
    dropped = []
    for entry in raw_sections:
        if not isinstance(entry, dict):
            raise UserError("a section is not an object")
        heading = str(entry.get("heading") or "").strip()
        if not heading:
            raise UserError("a section has no heading")
        if heading.casefold() in seen:
            raise UserError(f"two sections are both called {heading!r}")
        seen.add(heading.casefold())
        if heading.casefold() == SOURCE_RECORDINGS_HEADING.casefold():
            raise UserError(f"'{SOURCE_RECORDINGS_HEADING}' is written in code, not by the model")
        lines = entry.get("lines")
        if isinstance(lines, str):
            lines = lines.splitlines()
        if not isinstance(lines, list) or not any(str(line).strip() for line in lines):
            raise UserError(f"section {heading!r} has no text")
        # An id the day does not have is dropped rather than refused. A six
        # recording day drew `s-0007` through `s-0011` on a real run -- the model
        # counting sections rather than reading the dividers. The ids feed the
        # `(~HH:MM)` markers and nothing else, so an invented one contributes
        # nothing and costs nothing to discard; a section left citing *no* real
        # recording is the substantive failure, and `check_daily_note` holds the
        # day for it. Absorbing the noise here keeps that signal readable.
        source_ids = []
        for unit_id in entry.get("sourceIds") or []:
            unit_id = str(unit_id)
            if unit_id in known and unit_id not in source_ids:
                source_ids.append(unit_id)
            elif unit_id not in known:
                dropped.append(f"{heading}: {unit_id}")
        sections.append({"heading": heading, "sourceIds": source_ids, "lines": [str(line) for line in lines]})
    return {
        "title": title,
        "summary": summary.strip(),
        "sections": sections,
        "dropped_source_ids": dropped,
    }


def clean_daily_group(args, group, merged, run_dir, journal_key):
    """Clean a day's merged transcript as one document, one chunk per chat call.

    Mirrors the per-file chunked cleaner, but over blocks that belong to no single file. The
    tiny threshold is measured on the merged day rather than per recording, which
    is the whole point of merging first: a 61-word fragment gets no summary and
    the lightest possible touch on its own, and a real edit as part of a day.
    """
    journal_path = run_dir / "cleaned.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    journal = {
        (row["path"], row["chunk"]): row
        for row in prior
        if row.get("path") == journal_key and row.get("chunk") is not None and row.get("status") == "ok"
    }
    artifacts = run_dir / "cleaned"
    artifacts.mkdir(exist_ok=True)
    blocks = [{"speaker": None, "seconds": entry["seconds"], "text": entry["text"]} for entry in merged]
    words = sum(len(entry["text"].split()) for entry in merged)
    tiny = words < args.tiny_words
    record = {"recording_type": group["recording_type"], "material_role": DAILY_ROLE}
    chunks = chunk_blocks(blocks)
    service = chat_service(args)
    cleaned_chunks = []
    headings = []
    previous_tail = ""
    warnings = []
    for index, chunk in enumerate(chunks, start=1):
        row = journal.get((journal_key, index))
        if row is not None:
            cleaned = (artifacts / row["artifact"]).read_text(encoding="utf-8")
            cleaned_chunks.append(cleaned)
            headings.extend(heading_lines(cleaned))
            previous_tail = cleaned[-300:]
            continue
        started = time.time()
        payload, source = cleanup_payload(
            record, chunk, index, len(chunks), headings, previous_tail, {}, False, tiny,
            getattr(args, "compiled_voice", None), getattr(args, "compiled_lexicon", None),
        )
        cleaned, _summary = clean_one_chunk(
            args, None, payload, source, {}, False, tiny,
            system=cleanup_system(getattr(args, "compiled_voice", None), vault_voice.CONTEXT_OWNER),
        )
        name = f"{sha256_text(journal_key)[:12]}-{index:04d}.md"
        (artifacts / name).write_text(cleaned, encoding="utf-8")
        run_state.append_jsonl_fsync(
            journal_path,
            {
                "path": journal_key, "chunk": index, "chunks": len(chunks), "status": "ok",
                "artifact": name, "cleaned_sha256": sha256_text(cleaned),
                "seconds": round(time.time() - started, 3),
            },
        )
        cleaned_chunks.append(cleaned)
        headings.extend(heading_lines(cleaned))
        previous_tail = cleaned[-300:]
        progress(f"[daily {group['date']}] chunk {index}/{len(chunks)}")
    return "\n\n".join(part.strip() for part in cleaned_chunks).strip(), tiny, warnings


def composed_prose(composition):
    """Only the text the model actually wrote, per section, with its citations.

    The grounding check must read this rather than the rendered note. A rendered
    note also contains a title the model chose, `## Source Recordings` links built
    from filenames code generated, and a provenance block code wrote -- and
    checking those means checking the pipeline's own output against the sources,
    which reports the note's title as an invented name every time it is not a
    phrase somebody said out loud. Naming a thing is exactly the part of writing
    that does not quote.
    """
    written = [(None, composition["summary"])] if composition.get("summary") else []
    written.extend((section.get("sourceIds") or None, "\n".join(section["lines"])) for section in composition["sections"])
    return written


def check_daily_note(fmt, sources, text, composition, members):
    """Deterministic findings against a composed day's log."""
    review = []
    known = {unit["id"] for unit in sources["units"]}
    for section in composition["sections"]:
        unknown = [unit_id for unit_id in section.get("sourceIds") or [] if unit_id not in known]
        if unknown:
            review.append(f"section {section['heading']!r} cites unknown recordings: {', '.join(unknown)}")
        if not section.get("sourceIds"):
            review.append(f"section {section['heading']!r} cites no recording")
    # Grounded against the whole day, not per section. `cited_ids` is the right
    # gate where each source is fetched and quoted separately, but a day is merged
    # and cleaned as one document *before* the model sees any section boundary, so
    # its `sourceIds` attribute an already-unified text rather than claiming where
    # each sentence came from. Narrowing by them reports the day's own vocabulary
    # as invented -- a real run flagged Herder, Mochi, Claude, ITO, Mac Whisper and
    # Linux, every one of them spoken that morning, in a different recording than
    # the section that carried them. The citations earn their keep on the
    # `(~HH:MM)` markers instead, and are checked above for existing at all.
    written = "\n\n".join(prose for _ids, prose in composed_prose(composition))
    found = vault_compose.ungrounded_specifics(sources, written)
    for name in found["names"]:
        review.append(f"name not in any recording: {name}")
    for link in found["links"]:
        review.append(f"link not in any recording: {link}")
    for dropped in vault_compose.dropped_units(sources, written):
        review.append(f"recording {dropped['id']} ({dropped['label']}) did not reach the note")
    for severity, message in vault_compose.check_grammar(fmt, text):
        if severity == "error":
            review.append(f"note format: {message}")
    if len(composition["sections"]) > len(members) * 3:
        review.append("more sections than the day plausibly held; the log is fragmenting")
    return review


def daily_raw_destination(group, item, title, taken):
    """Where one recording's own note goes: the inbox, beside the log.

    Deliberately not routed into the sources tree. Doing that means choosing a
    domain, and the only way to choose one here is to hardcode it -- an earlier
    version said `personal`, which is wrong for a workday of memos about a
    dissertation. `vault-organizer` owns filing and reads each note to decide, so
    both the log and its recordings leave here unfiled, exactly as the
    per-recording path already leaves its raw twins.
    """
    stem = safe_title(
        format_filename("date-time-topic", group["date"], item["time_hhmm"], group["recording_type"], title)[:-3]
        + RAW_NOTE_SUFFIX
    )
    return assign_raw_name_in(INBOX_DIR, stem, taken)


def assign_raw_name_in(folder, stem, taken):
    suffix = 1
    while True:
        candidate = stem if suffix == 1 else f"{stem} ({suffix})"
        rel = (Path(folder) / f"{candidate}.md").as_posix()
        if rel.casefold() not in taken:
            taken.add(rel.casefold())
            return rel
        suffix += 1


def assemble_daily(args, vault, schema, fmt, group, parsed_by_path, class_records, composition, sources, merged, run_dir):
    """A day's log plus one source note per recording, ready to write.

    The recordings keep their own notes -- the log says what the day was about,
    and what was actually said stays available underneath it. Merging happens in
    memory and never on disk, which is what satisfies both halves of the request:
    cleanup reads one transcript, and the vault keeps six.
    """
    artifacts = run_dir / "assembled"
    artifacts.mkdir(exist_ok=True)
    taken = set()
    raw_names = {}
    raw_records = []
    for position, item in enumerate(group["members"], start=1):
        unit_id = "s-%04d" % position
        # Each recording keeps a name describing *itself*, from the per-recording
        # classification. Naming them after the day's log gives six files called
        # the same thing, which is exactly the state merging was meant to end.
        record = class_records.get(item["path"]) or {}
        title = record.get("title") or item.get("filename_hint") or composition["title"]
        destination = daily_raw_destination(group, item, title, taken)
        raw_names[item["path"]] = destination
        metadata = raw_metadata(schema, group["recording_type"], "", date=group["date"])
        metadata.pop("parent", None)
        body = (vault / item["path"]).read_bytes()
        text = build_raw_note(schema, metadata, transcript_source(split_frontmatter(body)["body"], vault))
        name = f"raw-{sha256_text(item['path'])[:12]}.md"
        (artifacts / name).write_text(text, encoding="utf-8")
        raw_records.append(
            {
                "unit_id": unit_id,
                "source": item["path"],
                "source_hash": sha256_bytes(body),
                "artifact": name,
                "final_hash": sha256_text(text),
                "destination": destination,
            }
        )
    # The log's `parent` cannot be filled until the log has a name, and the raw
    # notes carry it, so it is stamped after the log is named rather than in
    # `raw_metadata` the way the per-recording path does it.
    note_text = build_daily_note(fmt, schema, group, composition, sources, raw_names, run_dir)
    review = check_daily_note(fmt, sources, note_text, composition, group["members"])
    log_name = format_filename(
        args.filename_pattern, group["date"], None, group["recording_type"], composition["title"]
    )
    log_destination = (Path(INBOX_DIR) / log_name).as_posix()
    if (vault / log_destination).exists():
        review.append(f"a note already exists at {log_destination}")
    artifact = f"log-{sha256_text(group['date'])[:12]}.md"
    (artifacts / artifact).write_text(note_text, encoding="utf-8")
    return {
        "date": group["date"],
        "recording_type": group["recording_type"],
        "members": len(group["members"]),
        "raw": raw_records,
        "log": {
            "artifact": artifact,
            "final_hash": sha256_text(note_text),
            "destination": log_destination,
            "title": composition["title"],
        },
        "source_fingerprint": sources["fingerprint"],
        "merged_words": sum(len(entry["text"].split()) for entry in merged),
        "sections": len(composition["sections"]),
        "review": review,
        "needs_review": bool(review),
    }


def apply_daily(vault, run_dir, plan):
    """Write a day's source notes and then its log.

    The recordings are written first, for the same reason the per-recording path
    writes the raw note before the processed one: an interruption between the two
    leaves the recordings safe on disk and the log missing, which resuming can
    finish. The other order loses recordings to a log that claims to link them.
    """
    log_path = run_dir / "apply-log.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
    done = {(entry.get("op"), entry.get("source")) for entry in prior if entry.get("status") == "ok"}
    written = 0
    for record in plan["raw"]:
        if ("daily-raw", record["source"]) in done:
            continue
        source = vault / record["source"]
        data = source.read_bytes()
        if sha256_bytes(data) != record["source_hash"]:
            raise UserError(f"{record['source']} changed since planning")
        text = (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
        if sha256_text(text) != record["final_hash"]:
            raise UserError(f"{record['source']}: assembled note changed since planning")
        destination = vault / record["destination"]
        if destination.exists() and sha256_text(destination.read_text(encoding="utf-8")) != record["final_hash"]:
            raise UserError(f"destination collision: {record['destination']}")
        backup = run_dir / "backup" / record["source"]
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(source, backup)
        destination.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(destination, text)
        source.unlink()
        run_state.append_jsonl_fsync(
            log_path,
            {"op": "daily-raw", "status": "ok", "source": record["source"], "destination": record["destination"]},
        )
        written += 1
    entry = plan["log"]
    if ("daily-log", entry["destination"]) not in done:
        text = (run_dir / "assembled" / entry["artifact"]).read_text(encoding="utf-8")
        destination = vault / entry["destination"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create: a log is only ever new, so a name that is taken is a
        # collision to report rather than a file to overwrite.
        with open(destination, "xb") as handle:
            handle.write(text.encode("utf-8"))
        run_state.append_jsonl_fsync(
            log_path,
            {"op": "daily-log", "status": "ok", "source": entry["destination"], "destination": entry["destination"]},
        )
        written += 1
    return written


# --------------------------------------------------------------------------
# Thinking stage: whole-note review that fixes in place
# --------------------------------------------------------------------------

# One think call per note replaces the batched note-verify packets, the
# utterance spot-check, the whole-note meaning-judge, the chat-repair /
# think-re-verify loop, and the xhigh escalation redo. The reviewer reads the
# raw and the note together and either signs off, returns the corrected body
# itself, or holds with a reason code — fixing directly is what all five stages
# were approximating. The deterministic floors still run at assembly, but as
# advisories in this payload rather than holds; the byte-identity of the
# transcript section stays a hard structural gate on whatever the reviewer
# returns.
REVIEW_REASON_CODES = (
    "source_defect",
    "structural_after_fix",
    "oversize",
    "unusable_input",
    "dedupe_conflict",
    "writer_invalid",
    "review_unavailable",
)

REVIEW_FIX_SYSTEM = """You review one processed voice-recording note against its raw transcript, and you fix what you can.

Return exactly one JSON object and nothing else:
{"verdict": "ok" | "fixed" | "hold",
 "body": "<the complete corrected note body — only when verdict is fixed>",
 "summary": "<the corrected one-paragraph summary — only when fixed AND the summary itself needed correcting>",
 "reason_code": "source_defect",
 "reason": "<one or two sentences: what you fixed, or what a human must look at>"}
("reason_code" only when verdict is hold; "reason" always except a plain ok.)

What you are given:
- "rawTranscript": the recording, verbatim. The only source of truth.
- "note": the cleaned note body as assembled, without frontmatter or the verbatim transcript section.
- "summary": the one-paragraph summary that sits at the top of the note (null when the note is too short to need one).
- "classification": what the pipeline decided this recording is.
- "register": the fidelity contract that applies — judge against it, not against word overlap.
- "advisories": deterministic measurements (length ratio, rare-word retention, invented-word candidates, sampled utterances). Signals only, often false alarms; judge meaning yourself.
- "rawTruncated": when true, "rawTranscript" is only the head and tail of a recording too large to review whole. You cannot responsibly fix what you cannot fully read: verdict is then "ok" or "hold" only.

The registers:
- verbatim (therapy): the note keeps the speaker's exact words; only pure filler was removed. Any condensing, synonym swaps, or added clinical language is a defect.
- minutes (meeting): the note is concise minutes; paraphrase and compression are its job. Judge whether every decision, action item, and materially discussed topic is captured and attributed truly — never word overlap.
- spoken-to-written (everything else): meaning-first cleanup. Every claim, point, name, number, quote-worthy line, and shade of the speaker's intent must survive; rephrasing is fine, inventing or dropping substance is not.

Your verdict:
- "ok": the note tells the transcript's truth in its register — nothing material missing, nothing invented, names right, summary faithful. Wording differences alone are never a flag.
- "fixed": something is wrong that the transcript lets you repair — a dropped point, verse, term, or closing line; an invented claim or number; a wrong speaker name the transcript itself states; a summary that misstates the recording. Return the COMPLETE corrected body (never a fragment or a diff), leaving everything that was right untouched, and restore missing material in the note's own register — never paste raw transcript into cleaned prose. Include "summary" only if you corrected it.
- "hold": the defect is in the source, not the note — audio garbled past reconstruction, speaker attribution the transcript itself has wrong, content too corrupt to trust. reason_code "source_defect"; say plainly what a human should look at. Prefer fixing over holding: hold only what the transcript cannot settle.

Structure rules for a fixed body: headings "##" or deeper (never "#"), no transcript timestamps, speaker labels only for multi-speaker registers, exactly as the note already uses them."""


def review_register(record):
    if is_summarized(record["recording_type"]):
        return "minutes"
    if record["recording_type"] == "therapy":
        return "verbatim"
    return "spoken-to-written"


def review_payload(vault, record, run_dir, args):
    """One reviewer item: the whole source and the whole assembled body.

    ``None`` only when the artifact or source cannot be read — the caller turns
    that into a hold rather than a silent pass. A recording too large to review
    whole is sent head-and-tail with ``rawTruncated`` set, and the prompt
    forbids fixing in that state."""
    try:
        note_text = (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
    except (OSError, KeyError, TypeError):
        return None
    body = strip_callout_lines(note_text.split("\n# Transcript\n", 1)[0]).strip()
    try:
        raw = transcript_source(split_frontmatter((vault / record["source"]).read_bytes())["body"], vault)
    except OSError:
        return None
    parsed = parse_for_item(record, raw)
    # Corrected the same way the cleanup saw it, so a successful term correction
    # does not read to the reviewer as drift from the source.
    source = corrected_source_text(parsed, args, record.get("proposals") or [])
    truncated = False
    if len(source) + len(body) > WHOLE_NOTE_REVIEW_MAX_CHARS:
        budget = max(WHOLE_NOTE_REVIEW_MAX_CHARS - len(body), 20_000)
        head = source[: budget // 2]
        tail = source[-(budget - len(head)):]
        source = f"{head}\n\n[... middle of the recording elided ...]\n\n{tail}"
        truncated = True
    advisories = list(record.get("fidelity_provisional") or [])
    advisories.extend(record.get("advisories") or [])
    measurements = record.get("measurements") or {}
    payload = {
        "id": record["source"],
        "rawTranscript": source,
        "note": body,
        "summary": record.get("summary"),
        "classification": {
            "recordingType": record["recording_type"],
            "title": record["title"],
            "speakers": {key: value for key, value in (record.get("speaker_map") or {}).items() if value},
        },
        "register": review_register(record),
    }
    if truncated:
        payload["rawTruncated"] = True
    if advisories:
        payload["advisories"] = advisories
    if measurements:
        payload["measurements"] = measurements
    return payload


def review_verdict_problems(value):
    if not isinstance(value, dict):
        return ["response was not a JSON object"]
    verdict = value.get("verdict")
    if verdict not in {"ok", "fixed", "hold"}:
        return [f'verdict must be "ok", "fixed", or "hold" (got {verdict!r})']
    if verdict == "fixed":
        body = value.get("body")
        if not isinstance(body, str) or not body.strip():
            return ['verdict "fixed" must carry the complete corrected body in "body"']
    if verdict == "hold" and not isinstance(value.get("reason"), str):
        return ['verdict "hold" must say why in "reason"']
    return []


def hold_reviewed(record, warnings, reason, reason_code):
    """Send one reviewed note to a human, with its taxonomy code."""
    record.pop("fidelity_provisional", None)
    record["needs_review"] = True
    record["status"] = "review"
    record["verified"] = "reviewed-hold"
    record["reason_code"] = reason_code if reason_code in REVIEW_REASON_CODES else "source_defect"
    record["review_reason"] = reason
    record["destination"] = None
    record["action"] = "none"
    warnings.append(f"{record['source']}: held by review ({record['reason_code']}) — {reason}")


def apply_review_fix(args, vault, schema, record, item, value, think, run_dir, warnings):
    """Rebuild the note with the reviewer's corrected body, structurally gated.

    The reviewer just read the whole raw; its fix is the meaning authority here,
    so the old invented-words ceiling does not gate it — but the structural
    contract (single transcript heading, byte-identical tail, summary shape)
    still does, and a fix that breaks structure gets exactly one corrective
    re-ask at ESCALATION_EFFORT before the note holds."""
    fixed_summary = value.get("summary")
    if isinstance(fixed_summary, str) and fixed_summary.strip():
        record["summary"] = re.sub(r"\s+", " ", fixed_summary).strip()
    body = value["body"].strip()
    problems = rebuild_note_with_cleaned(vault, schema, record, item, body, args, run_dir)
    if problems:
        retry = [
            {"role": "system", "content": REVIEW_FIX_SYSTEM},
            {"role": "user", "content": json.dumps(review_payload(vault, record, run_dir, args), ensure_ascii=False)},
            {"role": "assistant", "content": json.dumps(value, ensure_ascii=False)},
            {
                "role": "user",
                "content": "Your fixed body broke the note's structural contract: "
                + "; ".join(problems[:4])
                + '. Return the corrected JSON again — same verdict "fixed", complete "body", structure rules respected.',
            },
        ]
        retried, _call = forge_llm.call_json_with_retry(
            think,
            retry,
            temperature=0,
            cache_prompt=args.cache_prompt,
            response_format={"type": "json_object"},
            timeout=args.request_timeout,
            api_key=args.api_key,
            task="review-transcript-note-repair",
            reasoning_effort=ESCALATION_EFFORT,
            background=getattr(args, "background", False),
        )
        if review_verdict_problems(retried) or retried.get("verdict") != "fixed":
            hold_reviewed(record, warnings, f"the review's fix kept breaking structure: {problems[0]}", "structural_after_fix")
            return False
        body = retried["body"].strip()
        fixed_summary = retried.get("summary")
        if isinstance(fixed_summary, str) and fixed_summary.strip():
            record["summary"] = re.sub(r"\s+", " ", fixed_summary).strip()
        problems = rebuild_note_with_cleaned(vault, schema, record, item, body, args, run_dir)
        if problems:
            hold_reviewed(record, warnings, f"the review's fix kept breaking structure: {problems[0]}", "structural_after_fix")
            return False
    record.pop("fidelity_provisional", None)
    record["verified"] = "reviewed-fixed"
    return True


def review_and_fix_notes(args, vault, schema, items_by_path, records, run_dir):
    """Review every processed note whole against its raw, fixing in place.

    Returns ``(summary, warnings)``. Journaled per note to ``review.jsonl``,
    keyed by the assembled artifact's hash, so a resume re-reviews nothing and a
    re-written note is re-reviewed."""
    warnings = []
    candidates = [record for record in records if record["action"] == "process" and not record["needs_review"]]
    summary = {"reviewed": 0, "ok": 0, "fixed": 0, "held": 0, "flaggedIds": []}
    if not candidates:
        return summary, warnings
    think = forge_llm.resolve_think_or_chat(base_url=args.think_url, model=args.think_model)
    if not think["enabled"]:
        warnings.append("review skipped: no thinking service is configured")
        summary["skipped"] = "disabled"
        summary["provisionalHeld"] = hold_provisional_unjudged(candidates, warnings, "no thinking service")
        return summary, warnings
    journal_path = run_dir / "review.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(journal_path, repair=True)
    journal = {(row.get("path"), row.get("note_sha256")): row for row in prior}
    produced_by = note_producers([record["source"] for record in candidates], run_dir)
    same = [path for path, producer in produced_by.items() if forge_verify.same_backend(think, producer)]
    if same:
        # The same split forge_verify documents: a clean verdict from the model
        # that wrote the note is not independent evidence; fixes and holds still act.
        warnings.append(
            f"{len(same)} note(s) are reviewed by the same backend that wrote them, so a clean "
            "verdict is not independent evidence. Route the writer off the reviewer's service to "
            "restore the split."
        )
    log(args, f"reviewing {len(candidates)} notes against their recordings on {think['url']}")
    total = len(candidates)
    durations = []
    for position, record in enumerate(candidates, start=1):
        item = items_by_path.get(record["source"]) or {}
        key = (record["source"], record.get("final_hash"))
        row = journal.get(key)
        if row is not None:
            # A settled verdict for these exact bytes; adopt it without a call.
            summary["reviewed"] += 1
            outcome = row.get("verdict")
            if outcome == "ok":
                summary["ok"] += 1
                record.pop("fidelity_provisional", None)
                record["verified"] = "reviewed-ok"
            elif outcome == "fixed":
                summary["fixed"] += 1
                record.pop("fidelity_provisional", None)
                record["verified"] = "reviewed-fixed"
                record["review_fix_reason"] = row.get("reason")
                if row.get("summary"):
                    record["summary"] = row["summary"]
                if row.get("fixed_hash") and record.get("final_hash") != row["fixed_hash"]:
                    # The journal's fix superseded these bytes; rebuild from it.
                    body = (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
                    record["final_hash"] = sha256_text(body)
            else:
                summary["held"] += 1
                summary["flaggedIds"].append(record["source"])
                hold_reviewed(record, warnings, row.get("reason") or "held by review", row.get("reason_code"))
            continue
        started = time.time()
        payload = review_payload(vault, record, run_dir, args)
        if payload is None:
            summary["reviewed"] += 1
            summary["held"] += 1
            summary["flaggedIds"].append(record["source"])
            hold_reviewed(record, warnings, "the note or its recording could not be read for review", "review_unavailable")
            run_state.append_jsonl_fsync(
                journal_path,
                {"path": record["source"], "note_sha256": record.get("final_hash"), "verdict": "hold",
                 "reason_code": "review_unavailable", "reason": "unreadable payload"},
            )
            continue
        messages = [
            {"role": "system", "content": REVIEW_FIX_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            value, _call = forge_llm.call_json_with_retry(
                think,
                messages,
                temperature=0,
                cache_prompt=args.cache_prompt,
                response_format={"type": "json_object"},
                timeout=args.request_timeout,
                api_key=args.api_key,
                task="review-transcript-note",
                reasoning_effort=FIDELITY_REVIEW_EFFORT,
                background=getattr(args, "background", False),
            )
            problems = review_verdict_problems(value)
            if problems:
                repair = messages + [
                    {"role": "assistant", "content": json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else str(value)},
                    {"role": "user", "content": f"That response was unusable: {problems[0]}. Return corrected JSON only, following the contract exactly."},
                ]
                value, _call = forge_llm.call_json_with_retry(
                    think,
                    repair,
                    temperature=0,
                    cache_prompt=args.cache_prompt,
                    response_format={"type": "json_object"},
                    timeout=args.request_timeout,
                    api_key=args.api_key,
                    task="review-transcript-note-repair",
                    background=getattr(args, "background", False),
                )
                problems = review_verdict_problems(value)
        except InterruptedError as error:
            raise UserError(
                f"the review was preempted by interactive activity: {error}. The run is checkpointed "
                f"— resume it with `process --vault {vault} --run {run_dir}`, or run the resume in "
                "the foreground so its review does not compete with the interactive session"
            ) from error
        except (forge_llm.ChatError, ValueError) as error:
            # An unreachable reviewer must not read as approval: the floors
            # re-arm for provisional notes, everything else is held rather than
            # silently shipped unreviewed.
            warnings.append(f"review failed: {type(error).__name__}: {error}")
            summary["skipped"] = str(error)
            summary["provisionalHeld"] = hold_provisional_unjudged(candidates[position - 1 :], warnings, "the review failed")
            break
        elapsed = round(time.time() - started, 3)
        summary["reviewed"] += 1
        if problems:
            summary["held"] += 1
            summary["flaggedIds"].append(record["source"])
            hold_reviewed(record, warnings, f"the review returned no usable verdict: {problems[0]}", "review_unavailable")
            entry = {"path": record["source"], "note_sha256": record.get("final_hash"), "verdict": "hold",
                     "reason_code": "review_unavailable", "reason": problems[0], "seconds": elapsed}
        elif value["verdict"] == "ok":
            summary["ok"] += 1
            record.pop("fidelity_provisional", None)
            record["verified"] = "reviewed-ok"
            entry = {"path": record["source"], "note_sha256": record.get("final_hash"), "verdict": "ok", "seconds": elapsed}
        elif value["verdict"] == "fixed":
            before_hash = record.get("final_hash")
            if apply_review_fix(args, vault, schema, record, item, value, think, run_dir, warnings):
                summary["fixed"] += 1
                record["review_fix_reason"] = value.get("reason")
                entry = {"path": record["source"], "note_sha256": before_hash, "verdict": "fixed",
                         "reason": value.get("reason"), "fixed_hash": record.get("final_hash"),
                         "summary": record.get("summary"), "seconds": elapsed}
            else:
                summary["held"] += 1
                summary["flaggedIds"].append(record["source"])
                entry = {"path": record["source"], "note_sha256": before_hash, "verdict": "hold",
                         "reason_code": "structural_after_fix", "reason": record.get("review_reason"), "seconds": elapsed}
        else:
            summary["held"] += 1
            summary["flaggedIds"].append(record["source"])
            hold_reviewed(record, warnings, value.get("reason") or "held by review", value.get("reason_code"))
            entry = {"path": record["source"], "note_sha256": record.get("final_hash"), "verdict": "hold",
                     "reason_code": record.get("reason_code"), "reason": record.get("review_reason"), "seconds": elapsed}
        run_state.append_jsonl_fsync(journal_path, entry)
        durations.append(elapsed)
        eta = format_duration(sum(durations) / len(durations) * (total - position)) if durations else "-"
        progress(f"[review {position}/{total}] {record['source']} → {entry['verdict']} ({elapsed:.1f}s, eta {eta})")
    return summary, warnings



ESCALATION_EFFORT = "xhigh"

def hold_provisional_unjudged(candidates, warnings, why):
    """Fall a provisional note back onto its deterministic hold.

    A provisional note is written on the promise that the meaning-judge will look
    at it. When the judge cannot run — no thinking service, or the review itself
    failed — that promise is broken, and an unjudged note must never read as
    approved. So the deterministic fidelity floor that flagged it holds after all,
    which is exactly the pre-provisional behaviour.
    """
    held = 0
    for record in candidates:
        flags = record.pop("fidelity_provisional", None)
        if not flags:
            continue
        record["status"] = "review"
        record["needs_review"] = True
        record["review_reason"] = "; ".join(flags)
        record["action"] = "none"
        record["destination"] = None
        record["warnings"] = list(record.get("warnings") or []) + flags
        warnings.append(f"{record['source']}: held by the fidelity floor ({why}): {record['review_reason']}")
        held += 1
    return held


# The whole cleaned note plus its whole source go to the meaning-judge in one
# call. That is what lets it see content that *moved* when the note was regrouped
# — the failure the per-utterance window could not — but it caps how much can be
# reviewed at once. A note over this size is held rather than sent: reviewing only
# part of it and approving the whole would be the silent pass the gate exists to
# stop. ~120k characters is ~30k tokens, well under the slot context with room for
# the reasoning and the restored output.
WHOLE_NOTE_REVIEW_MAX_CHARS = 120_000


def note_producers(sources, run_dir):
    """Which service cleaned each note, keyed by source path, for the judge's
    independence check. A note the thinking service both cleaned and now judges
    is not independently reviewed; a flag from it still acts, an ``ok`` does not
    read as approval."""
    by_path = {}
    for journal in ("cleaned.jsonl", "written.jsonl"):
        rows, _warnings = run_state.read_jsonl_recover_tail(run_dir / journal, repair=True)
        for row in rows:
            routing = row.get("routing")
            if row.get("path") and isinstance(routing, dict) and routing.get("url"):
                by_path[row["path"]] = {"url": routing["url"], "model": routing.get("model")}
    return {source: by_path[source] for source in sources if source in by_path}


def rebuild_note_with_cleaned(vault, schema, record, item, revised_cleaned, args, run_dir):
    """Rebuild a note's artifact with a revised cleaned body, structurally gated.

    The reviewer that produced the revision just read the whole raw transcript,
    so it — not a word counter — is the meaning authority; the old invented-words
    ceiling here is what silently failed every repair in the 2026-08-19 session.
    Structure still gates absolutely: one transcript heading, the verbatim tail,
    the summary shape. Returns ``[]`` on success (artifact and hashes updated on
    the record), or the structural problems, in which case nothing is written."""
    raw_body = transcript_source(split_frontmatter((vault / record["source"]).read_bytes())["body"], vault)
    parsed = parse_for_item(record, raw_body)
    metadata = frontmatter_metadata(schema, record["recording_type"], record.get("date"))
    raw_stem = raw_note_stem(Path(record["destination"]).name) if record.get("raw_artifact") else None
    note_text, head = build_note(
        schema, metadata, record["summary"], args.summary_style, parsed["preamble"],
        revised_cleaned, raw_body, reflection=record.get("reflection"), raw_stem=raw_stem,
    )
    raw_text = (
        build_raw_note(
            schema, raw_metadata(schema, record["recording_type"], Path(record["destination"]).stem, record.get("date")), raw_body
        )
        if raw_stem
        else None
    )
    structural, _fidelity, measurements = check_note(
        {**item, "raw_body": raw_body}, revised_cleaned, record["summary"], note_text, head, parsed, args,
        record.get("proposals") or [], raw_text=raw_text, raw_stem=raw_stem,
        summarized=is_summarized(record["recording_type"]),
    )
    problems = list(structural)
    if problems:
        return problems
    (run_dir / "assembled" / record["artifact"]).write_text(note_text, encoding="utf-8")
    record["final_hash"] = sha256_text(note_text)
    record["measurements"] = measurements
    if raw_text is not None:
        (run_dir / "assembled" / record["raw_artifact"]).write_text(raw_text, encoding="utf-8")
        record["raw_final_hash"] = sha256_text(raw_text)
    return []


# --------------------------------------------------------------------------
# Plan, report, apply
# --------------------------------------------------------------------------


def initial_counts():
    return {
        "selected": 0,
        "transcripts": 0,
        "skipped_non_transcript": 0,
        "untimestamped_transcript": 0,
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
    counts["untimestamped_transcript"] = sum(1 for record in records if record.get("no_timestamps"))
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
                            "reason_code": record.get("reason_code"),
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


APPLY_COMMAND_ID_ENV = "VAULT_TRANSCRIPTS_APPLY_COMMAND_ID"
SHELLCOMMANDS_DATA = (".obsidian", "plugins", "obsidian-shellcommands", "data.json")


def discover_apply_command_id(vault):
    """The shell-commands plugin command that runs this apply, found by its text.

    So the one-click link needs no manual id: create the command in the plugin
    and the review note finds it. Reads the plugin's own config read-only and
    matches a command whose text runs this script with ``--from-review``. The
    ``VAULT_TRANSCRIPTS_APPLY_COMMAND_ID`` env var overrides this when set.
    """
    data_path = vault.joinpath(*SHELLCOMMANDS_DATA)
    try:
        data = json.loads(data_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    commands = data.get("shell_commands")
    if isinstance(commands, dict):
        commands = list(commands.values())
    if not isinstance(commands, list):
        return None
    for command in commands:
        if not isinstance(command, dict) or not command.get("id"):
            continue
        texts = []
        platform_specific = command.get("platform_specific_commands")
        if isinstance(platform_specific, dict):
            texts.extend(str(value) for value in platform_specific.values() if value)
        if command.get("shell_command"):
            texts.append(str(command["shell_command"]))
        blob = " ".join(texts)
        if "vault-transcripts" in blob and "--from-review" in blob:
            return str(command["id"])
    return None


def resolve_apply_command_id(vault):
    return os.environ.get(APPLY_COMMAND_ID_ENV) or discover_apply_command_id(vault)


def review_facts(record):
    """The compact one-line facts the review note shows under a proposal."""
    stats = record.get("stats") or {}
    facts = [record.get("recording_type") or "note"]
    clock = format_clock(stats.get("duration_seconds"))
    if clock:
        facts.append(clock)
    facts.append(f"{stats.get('words', 0)} words")
    speakers = len(stats.get("speaker_labels") or {})
    if speakers:
        facts.append(f"{speakers} speaker{'s' if speakers != 1 else ''}")
    return " · ".join(facts)


def review_item(record):
    name = Path(record["destination"]).stem if record.get("destination") else Path(record["source"]).stem
    reason = record.get("review_reason") or ""
    if record.get("reason_code"):
        # The taxonomy code up front, so a person (or a model) can triage the
        # held set without re-deriving what kind of problem each row is.
        reason = f"[{record['reason_code']}] {reason}" if reason else f"[{record['reason_code']}]"
    return vault_review.ReviewItem(
        name=name,
        source=Path(record["source"]).name,
        summary=record.get("summary") or "",
        facts=review_facts(record),
        reason=reason,
    )


def apply_command_line(vault):
    script = Path(__file__).resolve()
    return (
        f"python3 {shlex.quote(str(script))} process "
        f"--vault {shlex.quote(str(vault))} --apply --from-review"
    )


def stage_pending_note(pending, run_dir, record):
    """Copy an assembled proposal into the visible staging folder under the name
    it would take in the vault, so the review note can link to it and the reviewer
    can open and edit it in place."""
    source = run_dir / "assembled" / record["artifact"]
    destination = pending / Path(record["destination"]).name
    shutil.copyfile(source, destination)


def stage_review_note(vault, run_dir, records, generated_at):
    """Stage the run's proposals into `_Pending Review/` and write the control
    note at the top of the inbox. Dry-run only. Returns any warnings.

    A prior, unapplied review from a *different* run is moved into this run's
    backup rather than mixed in, so the note on disk always describes one run.
    """
    warnings = []
    inbox = vault / INBOX_DIR
    pending = inbox / vault_review.PENDING_DIRNAME
    review_path = inbox / vault_review.REVIEW_NOTE_NAME
    run_rel = os.path.relpath(run_dir, vault)
    if review_path.is_file():
        prior = vault_review.parse_review_note(review_path.read_text(encoding="utf-8"))
        if prior.run_directory and prior.run_directory != run_rel:
            backup = run_dir / "backup" / "prior-review"
            backup.mkdir(parents=True, exist_ok=True)
            shutil.move(str(review_path), str(backup / vault_review.REVIEW_NOTE_NAME))
            if pending.is_dir():
                shutil.move(str(pending), str(backup / vault_review.PENDING_DIRNAME))
            warnings.append(
                f"replaced an unapplied Inbox Review from {prior.run_directory}; its note and staged "
                f"proposals were moved to {backup}"
            )
    pending.mkdir(parents=True, exist_ok=True)
    # A proposal dropped since a previous staging of this run must not linger as a
    # stale, still-linkable note. The staged files are copies; the run directory
    # remains the record of what was proposed.
    for stale in pending.glob("*.md"):
        stale.unlink()
    to_process, decisions = [], []
    for record in records:
        if record.get("already_applied"):
            continue
        item = review_item(record)
        if record["action"] == "process" and record.get("artifact"):
            stage_pending_note(pending, run_dir, record)
            to_process.append(item)
        elif record["needs_review"] or record["status"] == "failed":
            decisions.append(item)
    command_id = resolve_apply_command_id(vault)
    note = vault_review.render_review_note(
        generated_at=generated_at,
        run_directory=run_rel,
        to_process=to_process,
        decisions=decisions,
        apply_uri=vault_review.apply_uri(vault.name, command_id),
        apply_command=apply_command_line(vault),
        empty=not (to_process or decisions),
    )
    write_atomic(review_path, note)
    return warnings


def write_autonomous_review(vault, run_dir, records, generated_at):
    """After an autonomous run has filed its routine work, write the standing Inbox
    Review note as a receipt plus the held set. Unlike the dry-run stager nothing is
    staged into `_Pending Review/`: the filed notes are already in place, and a held
    note stays in the inbox with its reason shown here for a person to resolve.
    Returns any warnings.
    """
    warnings = []
    inbox = vault / INBOX_DIR
    pending = inbox / vault_review.PENDING_DIRNAME
    review_path = inbox / vault_review.REVIEW_NOTE_NAME
    run_rel = os.path.relpath(run_dir, vault)
    # A prior dry run's staged proposals would be stale now that this run filed on
    # its own, so clear them; the note then describes only this autonomous run.
    if pending.is_dir():
        backup = run_dir / "backup" / "prior-review"
        backup.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pending), str(backup / vault_review.PENDING_DIRNAME))
        warnings.append(f"cleared staged proposals from an earlier dry run; moved to {backup}")
    applied, decisions = [], []
    for record in records:
        if record["action"] == "process" and record.get("artifact"):
            applied.append(review_item(record))
        elif record["needs_review"] or record["status"] == "failed":
            decisions.append(review_item(record))
    note = vault_review.render_review_note(
        generated_at=generated_at,
        run_directory=run_rel,
        applied=applied,
        decisions=decisions,
        apply_uri=None,
        apply_command=apply_command_line(vault),
        empty=not (applied or decisions),
    )
    write_atomic(review_path, note)
    return warnings


def review_timestamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def resolve_run_from_review(vault):
    """The run directory the standing review note points at.

    `--from-review` carries no `--run`, because the shell-command URI that fires
    it cannot pass one; the run is read from the note's frontmatter instead.
    """
    review_path = vault / INBOX_DIR / vault_review.REVIEW_NOTE_NAME
    if not review_path.is_file():
        raise UserError(f"--from-review needs {vault_review.REVIEW_NOTE_NAME} in the inbox; none is there")
    decisions = vault_review.parse_review_note(review_path.read_text(encoding="utf-8"))
    if not decisions.run_directory:
        raise UserError(f"{vault_review.REVIEW_NOTE_NAME} has no run reference in its frontmatter")
    run_dir = (vault / decisions.run_directory).resolve()
    if not run_dir.is_dir():
        raise UserError(f"the run {decisions.run_directory} the review note points at is gone; run a fresh dry run")
    return str(run_dir)


def extract_cleaned_prose(note_text, preamble=""):
    """The model-written prose from an assembled note, for the invented-words
    recompute: everything above `# Transcript`, minus the apparatus callouts and
    the owner's own handwritten preamble."""
    body = split_frontmatter(note_text.encode("utf-8"))["body"]
    index = body.find("# Transcript")
    if index != -1:
        body = body[:index]
    kept = [line for line in body.splitlines() if not line.strip().startswith((">", "#"))]
    prose = "\n".join(kept).strip()
    if preamble.strip():
        prose = prose.replace(preamble.strip(), "").strip()
    return prose


def recheck_reviewed_note(vault, record, note_text, args):
    """Recompute the gate over the bytes that will reach the vault.

    Like `vault-compose apply`, the check is recomputed from the file on disk,
    never read from a stored verdict — that recompute is the safety property. It
    is the same meaning-first gate cleanup applied: paraphrase is fine, but
    fabrication past the ceiling (``invented_over_ceiling``) or a broken raw
    transcript holds the note for another look. Returns a list of problems.
    """
    data = (vault / record["source"]).read_bytes()
    raw_body = transcript_source(split_frontmatter(data)["body"], vault)
    parsed = parse_for_item(record, raw_body)
    source_text = corrected_source_text(parsed, args, record.get("proposals") or [])
    allowed = [value for value in (record.get("speaker_map") or {}).values() if value]
    allowed += [row.get("correct") for row in (record.get("proposals") or []) if row.get("correct")]
    cleaned = extract_cleaned_prose(note_text, parsed.get("preamble", ""))
    problems = []
    # Word overlap is advisory at apply time: a person just reviewed and ticked
    # this note, so a distinctive word is logged, never a re-hold. Meetings are
    # minutes and skip even the log.
    if not is_summarized(record.get("recording_type")):
        invented = invented_over_ceiling(source_text, cleaned, allowed)
        if invented:
            record.setdefault("warnings", []).append(
                f"applied with reviewer approval; {INVENTED_PROBLEM_PREFIX}: {', '.join(invented[:8])}"
            )
    # The verbatim recording must survive an edit untouched. This is the one
    # invariant an edit is never allowed to break.
    if record.get("raw_destination"):
        raw_stem = Path(record["raw_destination"]).stem
        if not note_text.endswith(f"# Transcript\n\n[[{raw_stem}]]\n"):
            problems.append("the transcript link at the end of the note was changed")
    elif not note_text.endswith(raw_body):
        problems.append("the raw transcript section is no longer byte-identical to the recording")
    return problems


def apply_from_review(vault, run_dir, records, args):
    """Reconcile the run's records with the reviewer's decisions before apply.

    Ticked notes are applied from their staged (possibly edited) bytes after the
    gate is recomputed; everything unticked is left where it is. Returns warnings.
    """
    warnings = []
    review_path = vault / INBOX_DIR / vault_review.REVIEW_NOTE_NAME
    decisions = vault_review.parse_review_note(review_path.read_text(encoding="utf-8"))
    pending = vault / INBOX_DIR / vault_review.PENDING_DIRNAME
    for record in records:
        if record["action"] != "process" or not record.get("artifact") or not record.get("destination"):
            continue
        name = Path(record["destination"]).stem
        if name not in decisions.approved:
            # Left unticked: never applied, and re-surfaced as needing a decision
            # so a plain apply cannot pick it up.
            record["action"] = "none"
            record["needs_review"] = True
            if record["status"] == "ok":
                record["status"] = "review"
                record["review_reason"] = "left unticked in the review note"
            continue
        staged = pending / Path(record["destination"]).name
        note_text = (
            staged.read_text(encoding="utf-8")
            if staged.is_file()
            else (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
        )
        problems = recheck_reviewed_note(vault, record, note_text, args)
        if problems:
            record["action"] = "none"
            record["status"] = "review"
            record["needs_review"] = True
            record["review_reason"] = "; ".join(problems)
            warnings.append(f"{record['destination']}: not applied — {problems[0]}")
            continue
        # The reconciled bytes become the assembled artifact so the existing apply
        # path — which trusts the frozen hash — writes exactly what was reviewed.
        (run_dir / "assembled" / record["artifact"]).write_text(note_text, encoding="utf-8")
        record["final_hash"] = sha256_text(note_text)
        record["action"] = "process"
        record["status"] = "ok"
        record["needs_review"] = False
        record["review_reason"] = None
    return warnings


def finish_review(vault, run_dir):
    """After a `--from-review` apply, clear the staged proposals and reset the
    standing review note to its empty state. Nothing is deleted: the staged
    copies are moved into the run's backup."""
    inbox = vault / INBOX_DIR
    pending = inbox / vault_review.PENDING_DIRNAME
    review_path = inbox / vault_review.REVIEW_NOTE_NAME
    if pending.is_dir():
        backup = run_dir / "backup" / "applied-review"
        backup.mkdir(parents=True, exist_ok=True)
        for staged in pending.glob("*.md"):
            shutil.move(str(staged), str(backup / staged.name))
        try:
            pending.rmdir()
        except OSError:
            pass
    note = vault_review.render_review_note(
        generated_at=review_timestamp(),
        run_directory=os.path.relpath(run_dir, vault),
        to_process=[],
        decisions=[],
        apply_uri=None,
        apply_command=apply_command_line(vault),
        empty=True,
    )
    write_atomic(review_path, note)


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
            f"- Notes reviewed whole against their recordings: {verification['reviewed']}",
            f"- Sound as written: {verification['ok']}",
            f"- Fixed in place by the reviewer: {verification['fixed']}",
            f"- Held for you, with a reason code: {verification['held']}",
        ]
    )
    if verification.get("provisionalHeld"):
        lines.append(
            f"- Deterministic fidelity floors re-armed as holds (no reviewer ran): {verification['provisionalHeld']}"
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
    held_all = [record for record in records if record["needs_review"] or record["status"] == "failed"]
    review = held_all
    no_timestamps = [record for record in records if record.get("no_timestamps")]
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
        f"- Untimestamped transcripts (processed verbatim): {counts['untimestamped_transcript']}",
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
    if no_timestamps:
        report.extend(
            [
                "",
                "## Untimestamped Transcripts",
                "",
                "Speaker-labelled exports with no timestamps, admitted automatically and "
                "cleaned verbatim (there were no clock markers to drop).",
                "",
            ]
        )
        append_listing(report, no_timestamps, lambda record: f"- `{record['source']}`")
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


def write_atomic_bytes(path, data):
    """Write ``data`` to ``path`` through a temporary file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_id, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(handle_id, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_atomic(path, text):
    """Write ``text`` to ``path`` through a temporary file in the same directory."""
    write_atomic_bytes(path, text.encode("utf-8"))


def apply_process(vault, run_dir, record):
    """Write one processed note's content, in place, without moving it.

    The original is copied to the run's backup directory first, and the new
    content lands through a temporary file in the same directory.

    The recording's own note is written first, so an interruption between the two
    writes leaves the recording duplicated rather than lost: the original note
    still holds it, and resuming finds the recording note already there with the
    planned bytes and finishes the rewrite.

    Content and location are separate steps. This one keeps the note where it is;
    the rename that gives it its real title follows, and follows *after* every
    note in the run has been rewritten, because a rename can rewrite links inside
    other notes and would invalidate their planning hashes.
    """
    source = vault / record["source"]
    data = source.read_bytes()
    if sha256_bytes(data) != record["source_hash"]:
        raise UserError("source changed since planning")
    note_text = (run_dir / "assembled" / record["artifact"]).read_text(encoding="utf-8")
    if sha256_text(note_text) != record["final_hash"]:
        raise UserError("assembled note changed since planning")
    destination = vault / record["destination"]
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
    if not backup.exists():
        shutil.copy2(source, backup)
    if raw_text is not None:
        write_atomic(raw_destination, raw_text)
    write_atomic(source, note_text)
    return backup


def apply_process_move(vault, run_dir, record, mover):
    """Give a processed note its real filename, taking inbound links with it.

    This is the rename that matters. A transcript arrives named for when it was
    recorded and leaves named for what it says, so the basename changes — and a
    changed basename is exactly what Obsidian's wikilink resolution cannot paper
    over. Without the CLI this is a plain rename and inbound `[[links]]` to the
    old name are left behind, which is what has always happened here.
    """
    source = vault / record["source"]
    destination = vault / record["destination"]
    if destination.exists() and destination.resolve() != source.resolve():
        raise UserError("destination collision")
    destination.parent.mkdir(parents=True, exist_ok=True)
    expected = {record["source"]: sha256_bytes(source.read_bytes())}
    return mover.move(vault, run_dir, record["source"], record["destination"], expected)


def apply_records(vault, run_dir, records, counts, mover=None):
    log_path = run_dir / "apply-log.jsonl"
    renames_path = run_dir / "renames.jsonl"
    prior, _ = run_state.read_jsonl_recover_tail(log_path, repair=True)
    done = {(entry.get("op"), entry.get("source")) for entry in prior if entry.get("status") == "ok"}
    if mover is None:
        mover = PlainMover()
    fallback = PlainMover()
    actionable = sorted(
        (
            record
            for record in records
            if record["action"] in {"quarantine", "process"} and record["status"] not in {"failed", "review"}
        ),
        key=lambda record: ({"quarantine": 0}.get(record["action"], 1), record["source"]),
    )

    def journal(op, record, status, **extra):
        entry = {"op": op, "status": status, "source": record["source"], "destination": record["destination"]}
        entry.update(extra)
        run_state.append_jsonl_fsync(log_path, entry)

    # Quarantine and content first; every rename afterwards.
    for record in actionable:
        op = record["action"]
        if (op, record["source"]) in done:
            if op == "quarantine":
                counts["applied"] += 1
            continue
        try:
            backup = (
                apply_quarantine(vault, run_dir, record)
                if op == "quarantine"
                else apply_process(vault, run_dir, record)
            )
            if op == "quarantine":
                counts["applied"] += 1
            journal(op, record, "ok", backup=str(backup))
        except Exception as error:  # noqa: BLE001 - every failure is reported, never silent
            record["apply_failed"] = True
            counts["apply_failed"] += 1
            record["warnings"].append(f"apply failed: {error}")
            journal(op, record, "error", error=str(error))

    for record in actionable:
        if record["action"] != "process" or record.get("apply_failed"):
            continue
        if ("process_move", record["source"]) in done:
            counts["applied"] += 1
            continue
        if record["destination"] == record["source"]:
            counts["applied"] += 1
            continue
        active = fallback if getattr(mover, "disabled", False) else mover
        try:
            detail = apply_process_move(vault, run_dir, record, active)
            counts["applied"] += 1
            journal("process_move", record, "ok", **detail)
            run_state.append_jsonl_fsync(
                renames_path,
                {
                    "at": run_state.utc_now(),
                    "old": record["source"],
                    "new": record["destination"],
                    **detail,
                },
            )
        except Exception as error:  # noqa: BLE001 - every failure is reported, never silent
            counts["apply_failed"] += 1
            record["warnings"].append(f"rename failed: {error}")
            journal("process_move", record, "error", error=str(error))


def normalize_recording_parents(vault, run_dir, records):
    """Put each recording note's ``parent`` back to a bare basename wikilink.

    Runs after every rename in the apply has completed and the link-safe move has
    done its link rewriting. A rename performed while the processed note is
    momentarily ambiguous with its staged review copy leaves the recording note's
    ``parent`` directory-qualified (``[[00 Inbox/_Pending Review/X]]``); that path
    goes stale the instant the staging is cleared, and the organizer then objects
    that the note's parent points somewhere it is not. This pass strips the
    qualification the CLI added so the link is the basename the pipeline wrote.

    Each note is backed up before it is touched and the change is journaled. A
    ``parent`` that is already a basename is left exactly as it is, so the pass is
    idempotent and a no-op on a plain-rename run.
    """
    warnings = []
    journal_path = run_dir / "parent-normalize.jsonl"
    for record in records:
        if record.get("action") != "process" or record.get("apply_failed"):
            continue
        raw_destination = record.get("raw_destination")
        if not raw_destination:
            continue
        path = vault / raw_destination
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
            result = parent_basename_bytes(data)
            if result is None:
                continue
            rewritten, old_target, basename = result
            backup_once(run_dir, raw_destination, path)
            write_atomic_bytes(path, rewritten)
        except OSError as error:
            warnings.append(f"{raw_destination}: could not normalize the recording note's parent link ({error})")
            continue
        run_state.append_jsonl_fsync(
            journal_path,
            {"at": run_state.utc_now(), "note": raw_destination, "from": old_target, "to": basename},
        )
    return warnings


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def chat_service(args):
    return forge_llm.service_from_args(args, "chat")


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
    options = {
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
        "pipeline": PIPELINE_VERSION,
        "cache_prompt": args.cache_prompt,
        "schema": args.schema,
        "voice": args.voice,
        "no_voice": args.no_voice,
        "lexicon": args.lexicon,
        "no_lexicon": args.no_lexicon,
        "profile": args.profile,
        "no_profile": args.no_profile,
    }
    if getattr(args, "notes", None):
        # A scoped run pins its selection into the fingerprint so a resume with the
        # same --note is compatible and a different one is a new run. Present only
        # when scoping is active, so a whole-inbox run's fingerprint is unchanged and
        # runs started before this option stay resumable.
        options["notes"] = args.notes
    return options


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
    # A scoped run resumes with the selection it was started with; --note on a
    # resume is ignored so a re-specified (and possibly since-filed) note cannot
    # silently change the work-list.
    args.notes = stored.get("notes")


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


def scan_filed_exports(vault, schema, limit=None):
    """Frontmatter-less exports already sitting in the sources tree.

    Opt-in, and never the default. `scan_inbox` walks `00 Inbox`, which is where
    a recording is supposed to arrive -- but a person who filed a day's memos by
    hand before this mode existed has them under the sources tree with no
    frontmatter, invisible to every pass. This finds those and nothing else: a
    note with frontmatter has been processed and is not an export.
    """
    folder = compile_destination(schema, {"type": "source", "domain": "personal", "source_kind": RAW_SOURCE_KIND})
    root = vault / folder
    if not root.is_dir():
        raise UserError(f"sources folder does not exist: {root}")
    items = []
    for path in sorted(root.rglob("*.md")):
        if path.is_symlink() or is_workspace_dir(path.parent):
            continue
        rel = relative_path(vault, path)
        try:
            data = path.read_bytes()
            split = split_frontmatter(data)
            parsed = parse_transcript(transcript_source(split["body"], vault))
        except (OSError, UnicodeDecodeError):
            continue
        ok, reason = is_transcript(split, parsed)
        if not ok:
            continue
        stat = path.stat()
        items.append(
            {
                "path": rel,
                "sha256": sha256_bytes(data),
                "size": stat.st_size,
                "mtime": int(stat.st_mtime),
                "is_transcript": True,
                "skip_reason": reason,
                "error": None,
                **parse_filename(path.name),
                "filename_hint": filename_title_hint(path.name),
                "stats": transcript_stats(parsed),
            }
        )
    items.sort(key=lambda item: item["path"])
    return items[:limit] if limit is not None else items


def daily(args):
    """Merge a day's short voice memos into one log, keeping each recording.

    A memo recorded on the way out the door is a fragment: one real recording is
    61 words and ends "I don't remember what it is". The pipeline could only ever
    make one note per file, so a day of thinking became a dozen notes that mean
    nothing apart. This groups a day, merges it onto one clock, cleans it as a
    whole, and writes one note plus the recordings it was made from.
    """
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
    format_path = vault_format.resolve_format_path(vault, args.format, disabled=args.no_format)
    fmt, format_hash = vault_format.compiled_format_for(vault, format_path, cache_dir=vault / STATE_DIR / "cache")
    if not fmt or not fmt.get("blocks"):
        raise UserError(
            "a day's log is assembled from the vault's declared block order, and this vault declares none; "
            f"add a '### {vault_format.GRAMMAR_SUBSECTION}' table to {vault_format.DEFAULT_FORMAT}"
        )
    voice_path = vault_voice.resolve_voice_path(vault, args.voice, disabled=args.no_voice)
    voice, voice_hash = vault_voice.compiled_voice_for(vault, voice_path, cache_dir=vault / STATE_DIR / "cache")
    args.compiled_voice = voice
    lexicon_path = vault_lexicon.resolve_lexicon_path(vault, args.lexicon, disabled=args.no_lexicon)
    lexicon, lexicon_hash = vault_lexicon.load_lexicon(
        vault, lexicon_path, schema=schema, cache_dir=vault / STATE_DIR / "cache",
        dictionary_path=dictionary_path(args),
    )
    args.compiled_lexicon = lexicon
    profile_path, resolve_warnings = vault_profile.resolve_profile_or_warn(vault, args.profile, disabled=args.no_profile)
    profile, profile_hash, compile_warnings = vault_profile.compiled_profile_for(
        vault, profile_path, cache_dir=vault / STATE_DIR / "cache"
    )
    args.compiled_profile = profile
    configuration = run_configuration(
        args, vault, schema_hash, voice_path, voice_hash, lexicon_path, lexicon_hash,
        profile_path, profile_hash, command="daily",
    )
    configuration["input"].update(vault_format.format_state(format_path, format_hash))
    if resuming:
        try:
            run_state.assert_compatible_run(state, configuration)
        except ValueError as error:
            raise UserError(str(error)) from error
    warnings = list(resolve_warnings + compile_warnings)
    plans = []
    with run_state.run_lock(vault / STATE_DIR):
        if not resuming:
            run_dir = unique_run_directory(vault)
            run_state.initialize_run_state(
                run_dir,
                run_state.create_run_state(
                    WORKFLOW, "daily", configuration["input"], configuration["options"], phase="scan"
                ),
            )
        scan_path = run_dir / "scan.json"
        if scan_path.is_file():
            items = json.loads(scan_path.read_text(encoding="utf-8"))["items"]
        else:
            items = scan_filed_exports(vault, schema, args.limit) if args.scan == "filed" else scan_inbox(vault, args.limit)
            run_state.atomic_write_json(scan_path, {"items": items})
            phase(run_dir, "dedupe", event={"type": "phase", "phase": "scan", "selected": len(items)})
        log(args, f"scanned {len(items)} notes, {sum(1 for item in items if item['is_transcript'])} transcripts")

        dedupe_path = run_dir / "dedupe.json"
        if dedupe_path.is_file():
            dedupe = json.loads(dedupe_path.read_text(encoding="utf-8"))
        else:
            dedupe, _losers, _held = plan_dedupe(vault, items)
            run_state.atomic_write_json(dedupe_path, dedupe)
            phase(run_dir, "classify", event={"type": "phase", "phase": "dedupe", "groups": len(dedupe["groups"])})
        losers = {loser["path"] for group in dedupe.get("groups", []) for loser in group["losers"]}
        held = {member["path"] for pair in dedupe.get("review_pairs", []) for member in pair["members"]}

        class_records, stage_warnings = classify_items(args, vault, items, run_dir, losers | held)
        warnings.extend(stage_warnings)
        phase(run_dir, "group", event={"type": "phase", "phase": "classify", "records": len(class_records)})

        groups = group_daily(items, class_records, args.daily_min_recordings)
        for group in groups:
            # A day's log silently built from five of six recordings is worse than
            # no log, because nothing about it looks wrong. A held member holds
            # the day, and those recordings fall through to ordinary processing.
            blocked = [item["path"] for item in group["members"] if item["path"] in held or item["path"] in losers]
            if blocked:
                warnings.append(
                    f"{group['date']}: not merged; {len(blocked)} of {len(group['members'])} recordings are held "
                    f"for review ({', '.join(blocked)}). Run `process` for that day instead."
                )
                continue
            parsed_by_path = {}
            for item in group["members"]:
                split = split_frontmatter((vault / item["path"]).read_bytes())
                parsed_by_path[item["path"]] = parse_transcript(transcript_source(split["body"], vault))
            merged = merge_transcripts(group["members"], parsed_by_path)
            sources = vault_compose.source_set(
                [
                    vault_compose.source_unit(
                        vault_compose.KIND_TRANSCRIPT,
                        Path(item["path"]).stem,
                        "\n".join(block["text"] for block in parsed_by_path[item["path"]]["blocks"]),
                        occurred_at=item.get("time_hhmmss"),
                        origin={"path": item["path"], "sha256": item["sha256"]},
                    )
                    for item in group["members"]
                ]
            )
            try:
                cleaned, tiny, stage_warnings = clean_daily_group(args, group, merged, run_dir, group["date"])
                warnings.extend(stage_warnings)
                composition = compose_daily(args, vault, group, cleaned, sources)
            except (forge_llm.ChatError, UserError, ValueError) as error:
                warnings.append(f"{group['date']}: could not be composed ({type(error).__name__}: {error})")
                continue
            if tiny:
                warnings.append(f"{group['date']}: the merged day is still under --tiny-words; the log will be brief")
            for dropped in composition.get("dropped_source_ids") or []:
                warnings.append(f"{group['date']}: dropped a recording id the day does not have ({dropped})")
            plans.append(
                assemble_daily(
                    args, vault, schema, fmt, group, parsed_by_path, class_records, composition, sources, merged, run_dir
                )
            )
        phase(run_dir, "plan", event={"type": "phase", "phase": "assemble", "days": len(plans)})

        applied = 0
        if args.apply:
            for plan in plans:
                if plan["needs_review"]:
                    warnings.append(f"{plan['date']}: held for review, not written")
                    continue
                applied += apply_daily(vault, run_dir, plan)
        run_state.atomic_write_json(run_dir / "daily.json", {"days": plans})
        report_path = write_daily_report(run_dir, plans, warnings, not args.apply)
        final_phase = "complete" if args.apply else "planned"
        run_state.update_run_state(
            run_dir,
            lambda draft: draft.update(
                {
                    "phase": final_phase,
                    "status": "complete" if args.apply else "running",
                    "nextAction": None if args.apply else f"review {report_path.name}, then rerun with --apply --run {run_dir}",
                }
            )
            or draft,
            event={"type": "phase", "phase": final_phase, "days": len(plans), "written": applied},
        )
    return structured(
        "ok",
        artifacts=[str(report_path)],
        warnings=warnings,
        data={
            "dry_run": not args.apply,
            "vault": str(vault),
            "run_directory": str(run_dir),
            "counts": {
                "days": len(plans),
                "recordings": sum(plan["members"] for plan in plans),
                "held": sum(1 for plan in plans if plan["needs_review"]),
                "written": applied,
            },
            "days": [
                {
                    "date": plan["date"],
                    "title": plan["log"]["title"],
                    "destination": plan["log"]["destination"],
                    "recordings": plan["members"],
                    "sections": plan["sections"],
                    "needs_review": plan["needs_review"],
                    "review": plan["review"],
                }
                for plan in plans
            ],
        },
    )


def write_daily_report(run_dir, plans, warnings, dry_run):
    lines = ["# Daily log run", "", f"Mode: {'dry run' if dry_run else 'applied'}", f"Days: {len(plans)}", ""]
    for plan in plans:
        lines.append(f"## {plan['date']} — {plan['log']['title']}")
        lines.append("")
        lines.append(f"- {plan['members']} recordings, {plan['sections']} sections, {plan['merged_words']} words merged")
        lines.append(f"- Log: `{plan['log']['destination']}`")
        for record in plan["raw"]:
            lines.append(f"- Recording {record['unit_id']}: `{record['source']}` -> `{record['destination']}`")
        if plan["review"]:
            lines.append("")
            lines.append("Held for review:")
            lines.extend(f"- {line}" for line in plan["review"])
        lines.append("")
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        lines.extend(f"- {line}" for line in warnings)
        lines.append("")
    path = run_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def process(args):
    vault = Path(args.vault).expanduser().resolve()
    if not vault.is_dir():
        raise UserError(f"vault root does not exist: {vault}")
    from_review = bool(getattr(args, "from_review", False))
    if from_review:
        # Applying a reviewed run: it is always an apply, and the run to resume is
        # the one the standing review note names.
        args.apply = True
        if not args.run:
            args.run = resolve_run_from_review(vault)
    if getattr(args, "autonomous", None) is None:
        # A scoped single-note request is autonomous by default; --no-autonomous
        # opts back into the dry-run-then-approve flow, and a whole-inbox run only
        # goes autonomous when --autonomous is given explicitly.
        args.autonomous = bool(getattr(args, "notes", None))
    if args.autonomous:
        # An autonomous run files its own routine work; only held items wait, and
        # the Inbox Review note becomes a receipt plus that held set.
        args.apply = True
    resuming = bool(args.run)
    state = None
    if resuming:
        run_dir = Path(args.run).expanduser().resolve()
        state = run_state.load_run_state(run_dir, workflow=WORKFLOW)
        adopt_stored_options(args, state)
    if getattr(args, "notes", None) and not resuming:
        # Canonicalize --note selectors (a typo fails loudly) so scoping is a
        # first-class, fingerprintable option; a resume adopts the stored selection.
        args.notes = resolve_selection(vault, args.notes, root=vault / INBOX_DIR)
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
                drift = run_state.input_drift(items, scan_inbox(vault, args.limit, select=getattr(args, "notes", None), allow_unlabeled=getattr(args, "unlabeled", False)))
                for added in drift["added"]:
                    warnings.append(f"input drift: {added['path']} appeared after the scan; run again to include it")
                for removed in drift["removed"]:
                    warnings.append(f"input drift: {removed['path']} disappeared after the scan")
                for changed in drift["changed"]:
                    warnings.append(
                        f"input drift: {changed['after']['path']} changed after the scan; it will be refused at apply"
                    )
        else:
            items = scan_inbox(vault, args.limit, select=getattr(args, "notes", None), allow_unlabeled=getattr(args, "unlabeled", False))
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
        # One writer call per note does what classify → chunked clean → summarize
        # did in three to four; the shared WRITER_SYSTEM keeps the prefix cache
        # warm across notes the same way the per-stage prompts did.
        class_records, clean_results, summaries, stage_warnings = write_notes(args, vault, items, run_dir, skip)
        warnings.extend(stage_warnings)
        phase(run_dir, "assemble", event={"type": "phase", "phase": "write", "notes": len(class_records)})

        records, stage_warnings = assemble_items(
            args, vault, schema, items, class_records, clean_results, summaries, losers, held, applied, run_dir
        )
        warnings.extend(stage_warnings)
        phase(run_dir, "verify", event={"type": "phase", "phase": "assemble", "records": len(records)})

        items_by_path = {item["path"]: item for item in items}
        verification = None
        if args.verify:
            verification, stage_warnings = review_and_fix_notes(
                args, vault, schema, items_by_path, records, run_dir
            )
            warnings.extend(stage_warnings)
        phase(
            run_dir,
            "plan",
            event={"type": "phase", "phase": "verify", **(verification or {"skipped": "disabled by --no-verify"})},
        )

        counts = recompute_counts(records, dedupe, items)
        counts["applied"] = sum(1 for record in records if record.get("already_applied") and not record["needs_review"])
        write_review_queue(run_dir, records)
        # Resolved even for a dry run, so the plan says whether the renames will
        # take inbound links with them rather than leaving it to be found out.
        mover, mover_reason = resolve_mover(args.link_rewrite, vault)
        if args.apply:
            if from_review:
                warnings.extend(apply_from_review(vault, run_dir, records, args))
                counts = recompute_counts(records, dedupe, items)
                counts["applied"] = sum(
                    1 for record in records if record.get("already_applied") and not record["needs_review"]
                )
            if mover_reason:
                warnings.append(f"renames use a plain rename: {mover_reason}")
            apply_records(vault, run_dir, records, counts, mover=mover)
            warnings.extend(mover.warnings)
            warnings.extend(normalize_recording_parents(vault, run_dir, records))
            if from_review:
                # Only retire the review surface when something actually went in.
                # If nothing was approved (or every approval re-held), leave the
                # note and staging in place so the review is not silently lost.
                if any(record["action"] == "process" for record in records):
                    finish_review(vault, run_dir)
                else:
                    warnings.append(
                        "nothing was applied from the review — the review note and staged proposals "
                        "were left in place. Tick at least one note, or resolve the reasons shown."
                    )
            elif getattr(args, "autonomous", False):
                # The run filed its routine work; the standing note now reports what
                # went in and lists only what a person still has to resolve.
                warnings.extend(write_autonomous_review(vault, run_dir, records, review_timestamp()))
            final_phase = "complete"
        else:
            final_phase = "planned"
            warnings.extend(stage_review_note(vault, run_dir, records, review_timestamp()))
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
    # The processed notes stay in the inbox (with frontmatter now) for the organizer
    # to file; a scoped autonomous run reports their paths so the caller can hand
    # exactly those to `vault-organizer inbox --note ...` without re-scanning.
    filed = sorted(
        {
            dest
            for record in records
            if record.get("action") == "process" and not record.get("needs_review")
            for dest in (record.get("destination"), record.get("raw_destination"))
            if dest
        }
    )
    held = [
        {"note": record["source"], "reason": record.get("review_reason") or "held for review"}
        for record in records
        if record.get("needs_review")
    ]
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
            "filed": filed,
            "held": held,
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
    raw = raw_metadata(schema, "other", stem, previous.get("date"))
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
            brief = (
                meeting_brief(
                    record,
                    record.get("spoken_date"),
                    item.get("time_hhmm"),
                    cleaned_result["cleaned"],
                    cleaned_result.get("speaker_map"),
                )
                if record.get("recording_type") == "meeting"
                else None
            )
            head = assemble_head(
                summary,
                args.summary_style,
                parsed["preamble"],
                cleaned_result["cleaned"],
                summary_row.get("reflection"),
                brief,
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
            structural, fidelity, measurements = check_note(
                {**item, "raw_body": raw_body},
                cleaned_result["cleaned"],
                summary,
                note_text,
                head,
                parsed,
                args,
                cleaned_result.get("proposals") or [],
                tail=item["tail"],
                summarized=is_summarized(record.get("recording_type")),
            )
            # Reprocessing an already-filed note has no thinking meaning-judge to
            # defer to — its verify pass reviews nothing (these records are
            # `reprocess`, not `process`, candidates) — so the fidelity floor is
            # the only backstop here and every problem holds, as before.
            problems = structural + fidelity
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
        assembled["advisories"] = cleaned_result.get("advisories") or []
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

        class_records, clean_results, summaries, stage_warnings = write_notes(args, vault, selected, run_dir, set())
        warnings.extend(stage_warnings)
        # The name has been reviewed by a person and is what the rest of the
        # vault links to; a fresh classification does not get to overrule it. A
        # therapy reading is the exception: it is a stop, never an override.
        # (The writer was already told the settled type — see "settledType" —
        # so a disagreement here is the exception, and the register drift it
        # leaves in an overridden body is an advisory for review.)
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
        phase(run_dir, "assemble", event={"type": "phase", "phase": "write", "notes": len(class_records)})

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
            verification, stage_warnings = review_and_fix_notes(
                args, vault, schema, items_by_path, records, run_dir
            )
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
    written, _ = run_state.read_jsonl_recover_tail(run_dir / "written.jsonl", repair=False)
    classified, _ = run_state.read_jsonl_recover_tail(run_dir / "classified.jsonl", repair=False)
    cleaned, _ = run_state.read_jsonl_recover_tail(run_dir / "cleaned.jsonl", repair=False)
    summarized, _ = run_state.read_jsonl_recover_tail(run_dir / "summaries.jsonl", repair=False)
    applied, _ = run_state.read_jsonl_recover_tail(run_dir / "apply-log.jsonl", repair=False)
    # A v2 run journals one written.jsonl row per note; a v1 run directory
    # still answers from its per-stage journals.
    progressed = written or classified
    durations = [row.get("seconds", 0.0) for row in progressed if row.get("seconds")]
    remaining = max(total - len(progressed), 0) if total is not None else None
    return structured(
        "ok",
        data={
            "run_directory": str(run_dir),
            "phase": state.get("phase"),
            "status": state.get("status"),
            "transcripts": total,
            "written": sum(1 for row in written if row.get("status") == "ok"),
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

    # The review lane: is the one-click apply link wired, and is a review pending?
    command_id = resolve_apply_command_id(vault)
    review_check = {
        "ok": True,
        "control_note": f"{INBOX_DIR}/{vault_review.REVIEW_NOTE_NAME}",
        "apply_command_id": command_id,
        "apply_command_source": (
            "env" if os.environ.get(APPLY_COMMAND_ID_ENV) else "shell-commands plugin" if command_id else None
        ),
    }
    if not command_id:
        warnings.append(
            "the one-click apply link is off: add a shell-commands command that runs this script with "
            f"--from-review (the review note finds it automatically), or set {APPLY_COMMAND_ID_ENV}. "
            "The review note still prints the terminal command."
        )
    review_path = vault / INBOX_DIR / vault_review.REVIEW_NOTE_NAME
    if review_path.is_file():
        pending = vault_review.parse_review_note(review_path.read_text(encoding="utf-8"))
        review_check["pending_run"] = pending.run_directory
        review_check["approved_in_note"] = len(pending.approved)
    checks["review"] = review_check
    return structured("ok" if ok else "error", warnings=warnings, data={"checks": checks})


class TrackingAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
        setattr(namespace, f"{self.dest}_provided", True)


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Rename, clean, and summarize voice-note transcripts in an Obsidian inbox.")
    parser.add_argument("mode", choices=["process", "daily", "reprocess", "split", "reconcile", "status", "doctor"])
    parser.add_argument("--vault")
    parser.add_argument("--schema", action=TrackingAction)
    parser.add_argument("--voice", action=TrackingAction, help="voice-and-style note (default: the vault's, when present)")
    parser.add_argument("--no-voice", action="store_true", help="disable the vault voice policy for this run")
    parser.add_argument("--lexicon", action=TrackingAction, help="speakers-and-terms note (default: the vault's, when present)")
    parser.add_argument("--no-lexicon", action="store_true", help="disable term correction and the speaker roster")
    parser.add_argument("--profile", action=TrackingAction, help="personal-context register note (default: the vault's, when present)")
    parser.add_argument("--no-profile", action="store_true", help="disable personal context for this run")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--autonomous",
        action="store_true",
        help="apply routine results without review, stopping only for held items; the report and the "
        "Inbox Review note say what was filed and what still needs you",
    )
    parser.add_argument(
        "--no-autonomous",
        dest="autonomous",
        action="store_false",
        help="force the dry-run-then-approve flow even when a scoped run would otherwise go autonomous",
    )
    # Default None (not False) so a single-note run can default to autonomous while
    # --no-autonomous still forces the review flow; resolved in process().
    parser.set_defaults(autonomous=None)
    parser.add_argument(
        "--unlabeled",
        action="store_true",
        help="force unlabeled parsing for exports below the automatic-detection thresholds (a clearly "
        "speaker-labelled export with no timestamps is admitted without this flag); no clock markers are "
        "written, since there are none to read",
    )
    parser.add_argument(
        "--link-rewrite",
        choices=["auto", "off", "require"],
        default="auto",
        help=(
            "how renames handle inbound links: auto uses the Obsidian CLI when it can and falls back to a "
            "plain rename otherwise, off always renames, require fails when link-safe renames are unavailable"
        ),
    )
    parser.add_argument("--run", help="existing run directory to resume")
    parser.add_argument(
        "--daily-min-recordings",
        type=int,
        action=TrackingAction,
        help="daily: how many same-day recordings make a log (default 2)",
    )
    parser.add_argument(
        "--scan",
        choices=["inbox", "filed"],
        action=TrackingAction,
        help="daily: where to look for exports; filed finds ones already moved into the sources tree",
    )
    parser.add_argument("--format", action=TrackingAction, help="note-format note (default: the vault's, when present)")
    parser.add_argument("--no-format", action="store_true", help="disable the vault note-format policy")
    parser.add_argument("--limit", type=int, action=TrackingAction)
    parser.add_argument(
        "--note",
        action="append",
        dest="notes",
        help="scope the run to this note (repeatable): a vault-relative path, a filename, or a stem. "
        "A selector that matches no note fails loudly rather than widening to the whole inbox",
    )
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
    parser.add_argument(
        "--background",
        action="store_true",
        help="run model calls as preemptible background inference; the default is foreground, because a "
        "backgrounded run is preempted by the very interactive session that usually launches it",
    )
    parser.add_argument("--no-cache-prompt", action="store_true")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="re-attempt chunks a previous run recorded as failed instead of inheriting them",
    )
    parser.add_argument(
        "--from-review",
        action="store_true",
        help=(
            "process: apply exactly what the inbox review note approves, reading the run from the "
            "note; recomputes the meaning-first gate on the reviewed bytes before applying"
        ),
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help=(
            "how many files to clean at once (default 1). Chunks within a file stay serial because each "
            "one is written against the tail of the last. The chat backend serves 2 slots, so 2 is the "
            "ceiling that helps, and it competes with interactive turns on the same server"
        ),
    )
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
    args.scan = args.scan or "inbox"
    if args.daily_min_recordings is None:
        args.daily_min_recordings = DEFAULT_DAILY_MIN_RECORDINGS
    if args.daily_min_recordings < 2:
        raise UserError("--daily-min-recordings must be at least 2; one recording is not a day")
    if args.mode == "status":
        if not args.run:
            raise UserError("status requires --run <run-directory>")
        return args
    if not args.vault:
        raise UserError(f"{args.mode} requires --vault")
    args.schema = args.schema or os.environ.get("VAULT_TRANSCRIPTS_SCHEMA") or None
    args.format = args.format or os.environ.get("VAULT_TRANSCRIPTS_FORMAT") or None
    if args.no_format and args.format and args.format_provided:
        raise UserError("--format and --no-format cannot be used together")
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
    if args.mode == "daily":
        return daily(args)
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
